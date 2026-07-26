"""FastAPI surface — B11b (REQ-115 / AC-115) + Phase-2A competition endpoints (Task 6 / P2A-6).

Factory: ``create_app(store)`` returns a configured FastAPI application.  The module-level
``_default_store`` is used when no store is injected (running under Uvicorn).  For testing,
pass an ``InMemoryStore`` to keep everything in-process and offline.

Phase-1 endpoints (unchanged — REQ-222 / AC-212)
-------------------------------------------------
``POST /demo/run``
    Run a demo competition over two deterministic agents (no LLM, no network).
``GET /leaderboard``
    Aggregate scores from all previously stored runs into a ranked board.
``GET /runs/{run_id}``
    Load a persisted run and return its proof card.  404 if unknown.

Phase-2A competition endpoints (additive)
-----------------------------------------
``POST /competitions``
    Create a new DRAFT competition from a ``CompetitionConfig`` body.
``POST /competitions/{competition_id}/agents``
    Register an agent entry on the roster.  Pins ``config_hash`` + normalises ``proof_mode``.
``POST /competitions/{competition_id}/start``
    Run the competition offline/deterministically (2A simplification — see comment in handler)
    and return the finalized state with ``run_id`` set.
``GET /competitions/{competition_id}``
    Return full competition state including leaderboard derived from the canonical event log.
``GET /competitions``
    List all competitions with an optional ``?status=`` filter.
``GET /competitions/{competition_id}/events``
    Return the ordered event log tail (``seq > since_seq``); mirrors WS replay parity.

No auth / Redis / rate-limiting — Phase-2 only (CON-009).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from veridex.api.auth_privy import PrivyPrincipal, make_require_principal
from veridex.api.demo_fixtures import (
    build_agents_from_roster,
    build_demo_ticks,
    contrarian_agent,
)
from veridex.api.deploy import DeployDeps, cancel_deploy_tasks, register_deploy_routes
from veridex.api.fixture_labels import fixture_metadata_row
from veridex.api.maker_router import register_maker_routes
from veridex.api.schemas import (
    AgentRegisterResponse,
    AgentRosterEntry,
    AgentRosterResponse,
    ApprovalResponse,
    BacktestRunRequest,
    BacktestRunResponse,
    CompetitionCreateResponse,
    CompetitionLeaderboardRow,
    CompetitionStartResponse,
    CompetitionStateResponse,
    CompetitionSummaryResponse,
    DemoRunResponse,
    ExplainRequest,
    FeedHealthResponse,
    InspectorRecord,
    KillSwitchResponse,
    LeaderboardResponse,
    LeaderboardRow,
    ReplayMarketRow,
    ReplayMarketsResponse,
    ReplayPackInfo,
    ReplayPackListResponse,
    RuntimeEventsResponse,
    VerifyResponse,
)
from veridex.api.signal_trials_router import register_signal_trials_routes
from veridex.api.ws import ArenaConnectionManager, register_arena_routes
from veridex.backtest.report import BacktestReport
from veridex.backtest.runner import run_backtest
from veridex.chain.anchor import anchor_memo, explorer_tx_url
from veridex.checks.build import (
    build_check_results,
    build_performance_metrics,
    check_results_to_proof_block,
)
from veridex.competition.events import (
    CompetitionEvent,
    EventType,
    build_competition_started_event,
    event_payload_hash,
)
from veridex.competition.models import (
    AgentEntry,
    Competition,
    CompetitionConfig,
    CompetitionStatus,
    ExecutionMode,
    ReplayBinding,
)
from veridex.competition.service import (
    CompetitionConflictError,
    CompetitionIntegrityError,
    CompetitionStateError,
    RosterInstanceNotFoundError,
    RosterInstanceNotOwnedError,
    _default_policy_envelope,
    declared_config_hash,
    register_agent,
    start_competition,
)
from veridex.config import Settings, get_settings
from veridex.deploy.instance import AgentInstance, DeployStatus
from veridex.execution.models import ExecutionRecord, ExecutionStatus
from veridex.execution.runner import resolve_approval
from veridex.explainer import GLOSSARY_DEFINITIONS, explain_proof
from veridex.ingest.feed_health import LiveFeedStatus
from veridex.ingest.replay_catalog import (
    CatalogEntry,
    ReplayCatalog,
    ReplayResolutionError,
    ResolvedReplaySource,
    build_catalog,
    load_resolved_marketstates,
    resolve_replay_source,
)
from veridex.ingest.replay_pack import load_pack_fixture_states_bound
from veridex.leaderboard import leaderboard as _build_leaderboard
from veridex.public_agent import PublicAgent, Visibility, owner_public_label
from veridex.public_projection import BoardKind, directional_board
from veridex.runtime.arena_comparison import (
    DET_DRIFT_CONTESTANT,
    LLM_DRIFT_CONTESTANT,
    run_arena_comparison,
)
from veridex.runtime.competition import (
    DEFAULT_CLUSTER,
    SCHEMA_VERSIONS,
    read_path_check_block,
    run_demo_competition,
)
from veridex.runtime.llm_checkpoint import CheckpointPolicy
from veridex.runtime.orchestrator import deterministic_agent
from veridex.runtime.runtime_events import RuntimeEvent, RuntimeEventType
from veridex.runtime.runtime_store import DurableRuntimeEventStore
from veridex.runtime.window import RunWindow
from veridex.scoring import score_run
from veridex.store import CompetitionStatusClaimError, InMemoryStore, RosterAdmissionError, Store
from veridex.strategies.llm_drift import default_model_launcher
from veridex.venues.sx_bet import FakeVenueAdapter, SXBetAdapter
from veridex.verifier.proof_card import proof_card_from_run_result
from veridex.verifier.recompute import verify_run as _verify_run_core

# Module-level default store — shared across requests when the app runs under Uvicorn.
# Tests always inject their own store via ``create_app(store=InMemoryStore())``.
_default_store: InMemoryStore = InMemoryStore()


def get_store() -> Store:
    """Dependency provider for the module-level default store.

    Returns:
        The module-level :class:`~veridex.store.InMemoryStore` instance.
    """
    return _default_store


def _cors_origins_from_env() -> list[str]:
    """Parse the ``CORS_ORIGINS`` env (comma-separated exact origins) into an allow-list.

    Fail-closed default: an unset/blank value yields an EMPTY allow-list, so no cross-origin request
    is granted ``access-control-allow-origin`` (same-origin still works). The public entrypoint
    (``veridex.api.server``) additionally REQUIRES this env before it will serve.
    """
    raw = os.environ.get("CORS_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


#: The STRICT intrinsic-arena roster contract: the FROZEN roster must declare EXACTLY these two
#: contestants — the intrinsic det-Drift (id ``det-drift`` / strategy ``cumulative-drift``) and
#: LLM-Drift (id ``llm-drift`` / strategy ``llm``), no more, no less, no id/strategy substitution.
#: The intrinsic arena runs these exact contestants; requiring the roster to declare EXACTLY them makes
#: the started event's ``agent_ids`` BY CONSTRUCTION the report's contestant ids — never a run that
#: reports one identity while the event claims another (Codex M2 identity substitution). The
#: owner-scoped ``POST /competitions/{id}/arena`` FAILS CLOSED on any mismatch, extra, or missing entry.
_ARENA_CONTESTANT_CONTRACT: dict[str, str] = {
    DET_DRIFT_CONTESTANT: "cumulative-drift",
    LLM_DRIFT_CONTESTANT: "llm",
}

#: The intrinsic arena's canonical proof mode. The checkpointed det-Drift vs LLM-Drift comparison is a
#: reproducible snapshot replay (no per-contestant proof anchoring), so both intrinsic contestants pin
#: ``"reproducible"``. Folded into each contestant's EXPECTED ``config_hash`` so the strict contract binds
#: proof_mode as well as model + id/strategy (Codex M2).
_ARENA_PROOF_MODE = "reproducible"


class _ArenaRosterContractError(Exception):
    """A strict intrinsic-arena roster contract violation, carrying the HTTP status + detail to surface.

    Raised by :func:`_verify_intrinsic_arena_roster` so the SAME strict contract can run BOTH pre-claim
    (translated to an ``HTTPException`` before any claim) AND post-claim (caught so the claim is recovered
    and the run fails closed) — never a run over a roster different from the one frozen at RUNNING.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _verify_intrinsic_arena_roster(
    entries: list[AgentEntry], *, expected_models: dict[str, str | None]
) -> dict[str, dict[str, Any]]:
    """FAIL CLOSED unless ``entries`` declares EXACTLY the intrinsic arena contestants at the pinned
    model/config identity; return the per-contestant config on success (Codex M1/M2).

    The strict contract binds THREE things so the roster the report attests is provably the roster that
    ran: (1) EXACTLY the two intrinsic ids -> strategies (cardinality 2, no extras, no substitution),
    (2) each entry's pinned ``config_hash`` equals the EXPECTED hash for that intrinsic identity (folding
    ``expected_models`` + ``_ARENA_PROOF_MODE``), and (3) no entry carries a deployed-instance binding the
    intrinsic arena does not run. Any mismatch raises :class:`_ArenaRosterContractError` (409).

    Args:
        entries: The roster snapshot to validate (pre-claim, or the post-claim frozen roster).
        expected_models: The pinned intrinsic model identity per contestant id (det-Drift -> ``None``,
            LLM-Drift -> the arena launcher's real ``model_id``).

    Returns:
        ``{agent_id: {"strategy", "model", "proof_mode", "config_hash"}}`` for the two contestants.

    Raises:
        _ArenaRosterContractError: 409 on any cardinality / id / config / instance-binding mismatch.
    """
    declared = {entry.agent_id: entry.strategy for entry in entries}
    if len(entries) != len(_ARENA_CONTESTANT_CONTRACT) or declared != _ARENA_CONTESTANT_CONTRACT:
        raise _ArenaRosterContractError(
            409,
            "competition roster must declare EXACTLY the intrinsic arena contestants "
            f"(required {sorted(_ARENA_CONTESTANT_CONTRACT.items())}; got {sorted(declared.items())})",
        )
    contestant_config: dict[str, dict[str, Any]] = {}
    for entry in entries:
        strategy = _ARENA_CONTESTANT_CONTRACT[entry.agent_id]  # safe: declared == contract above
        model = expected_models[entry.agent_id]
        expected_hash = declared_config_hash(
            agent_id=entry.agent_id, strategy=strategy, model=model, proof_mode=_ARENA_PROOF_MODE
        )
        if entry.instance_id is not None:
            raise _ArenaRosterContractError(
                409,
                f"roster entry {entry.agent_id!r} carries an incompatible instance binding; the "
                "intrinsic arena runs no deployed instance",
            )
        if entry.config_hash != expected_hash:
            raise _ArenaRosterContractError(
                409,
                f"roster entry {entry.agent_id!r} pins a model/config identity the intrinsic arena "
                f"does not run (expected config_hash {expected_hash}, got {entry.config_hash})",
            )
        contestant_config[entry.agent_id] = {
            "strategy": strategy,
            "model": model,
            "proof_mode": _ARENA_PROOF_MODE,
            "config_hash": expected_hash,
        }
    return contestant_config


async def _recover_arena_claim(store: Store, competition_id: str) -> None:
    """Fail-closed recovery after a post-claim arena failure (Codex M3).

    A model/drain/persistence failure AFTER the atomic claim must never strand the competition in
    ``RUNNING`` with a reserved run_id, zero events, and a permanent 409. This best-effort reconciliation:

    - If NO canonical events were committed for the claimed run (the reproduced ``ArenaDrainError`` case,
      where the failure precedes any append), CAS-roll the competition back ``RUNNING -> DRAFT`` and clear
      the reserved run_id, so the run is RETRYABLE rather than a permanently dead competition.
    - If events WERE committed (the append landed but a later step failed), finalize the competition as
      terminal so it is completed-as-failed, never stranded mid-``RUNNING``.

    All store calls are best-effort — recovery must never raise over the original failure being surfaced.

    Args:
        store: The async repository.
        competition_id: The competition to reconcile.
    """
    try:
        events = await store.list_competition_events(competition_id, since_seq=-1)
    except Exception:
        events = []
    if events:
        with contextlib.suppress(Exception):
            await store.update_competition_status(competition_id, CompetitionStatus.FINALIZED)
    else:
        with contextlib.suppress(CompetitionStatusClaimError, KeyError):
            await store.release_competition_run(
                competition_id, expected=CompetitionStatus.RUNNING, new=CompetitionStatus.DRAFT
            )


def _derive_leaderboard(events: list[CompetitionEvent]) -> list[CompetitionLeaderboardRow]:
    """Derive a ranked leaderboard from ``SCORE_UPDATE`` events in the canonical log.

    Single source of truth (CON-203): ranking is derived purely from persisted
    ``SCORE_UPDATE`` event payloads — no re-scoring from raw events.  Rows are ranked by
    ``mean_clv_bps`` descending (``None`` treated as ``-inf``), then ``agent_id`` ascending
    as a stable tie-breaker.

    Args:
        events: The full competition event log (any order).

    Returns:
        Ranked :class:`~veridex.api.schemas.CompetitionLeaderboardRow` list, rank 1 first.
    """
    score_updates = [e for e in events if e.event_type == EventType.SCORE_UPDATE]

    def _sort_key(event: CompetitionEvent) -> tuple[float, str]:
        mean_clv = event.payload.get("mean_clv_bps")
        score = -(mean_clv if isinstance(mean_clv, (int, float)) else float("-inf"))
        return (score, str(event.payload.get("agent_id", "")))

    rows: list[CompetitionLeaderboardRow] = []
    for rank, event in enumerate(sorted(score_updates, key=_sort_key), 1):
        p = event.payload
        rows.append(
            CompetitionLeaderboardRow(
                rank=rank,
                agent_id=str(p.get("agent_id", "")),
                total_clv_bps=int(p.get("total_clv_bps", 0)),
                mean_clv_bps=p.get("mean_clv_bps"),
                valid_count=int(p.get("valid_count", 0)),
                proof_mode=p.get("proof_mode"),
            )
        )
    return rows


def _build_execution_attachment(records: list[ExecutionRecord]) -> dict[str, Any] | None:
    """Build the NON-SCORING execution attachment from execution records (REQ-2B-20 / AC-2B-12).

    The attachment is explicitly labelled ``non_scoring`` + ``derived`` and is an OFF-CHAIN venue
    artifact (``venue_artifact=True``) — distinct from the Phase-1 Memo anchor and excluded from
    every evidence/scoring hash. Receipts are surfaced ONLY here, never in the skill-block proof
    card.

    Args:
        records: The competition's execution records (any order).

    Returns:
        ``None`` when there are no records; otherwise a dict with the records, their receipts, and
        the pinned ``policy_hash`` set (REQ-2B-03).
    """
    if not records:
        return None
    receipts = [r.receipt.model_dump(mode="json") for r in records if r.receipt is not None]
    policy_hashes = sorted({r.policy_hash for r in records})
    return {
        "non_scoring": True,
        "derived": True,
        "venue_artifact": True,
        "off_chain": True,
        "records": [r.model_dump(mode="json") for r in records],
        "receipts": receipts,
        "policy_hashes": policy_hashes,
    }


def _replay_pack_info(entry: CatalogEntry) -> ReplayPackInfo:
    """Project a verified :class:`CatalogEntry` into the read-only ``/replay-packs`` view.

    Surfaces the verified ``content_hash``, HONEST provenance, and catalogued fixtures — but NEVER the
    internal ``pack_dir`` filesystem path (the browser addresses packs by ``pack_id`` only).
    """
    return ReplayPackInfo(
        pack_id=entry.pack_id,
        content_hash=entry.content_hash,
        provenance=entry.provenance,
        is_genuine=entry.is_genuine,
        fixtures=list(entry.fixtures),
        fixture_metadata=[fixture_metadata_row(int(f)) for f in entry.fixtures],
    )


def _agent_roster_entry(
    agent: PublicAgent,
    instance: AgentInstance,
    scored: dict[str, Any] | None,
) -> AgentRosterEntry:
    """Project one HONEST public-directory row keyed on the immutable ``public_agent_id``.

    Joins the public identity (``agent``) to its SEALED deployment (``instance``) and, when present, the
    agent's aggregated PUBLIC_AGENTS board row (``scored``). The owner is rendered ONLY via the safe
    :func:`~veridex.public_agent.owner_public_label` — the raw ``operator_id`` / ``owner_ref`` NEVER
    leaves the server. ``origin`` is the REAL identity value; performance columns and ``proof_state``
    stay honestly absent (``None`` / ``"unscored"``) until the agent has scored rows, never fabricated.
    """
    return AgentRosterEntry(
        public_agent_id=agent.public_agent_id,
        display_name=agent.display_name,
        owner_public_label=owner_public_label(agent),
        origin=agent.origin.value,
        proof_state=str(scored["proof_mode"]) if scored is not None else "unscored",
        agent_id=instance.agent_id,
        type=instance.template_id,
        source_mode=instance.source_mode,
        execution_mode=instance.execution_mode,
        status=instance.status.value.lower(),
        config_hash_present=bool(instance.config_hash),
        avg_clv_bps=scored["avg_clv_bps"] if scored is not None else None,
        runs=scored["runs"] if scored is not None else None,
        valid_pct=scored["valid_pct"] if scored is not None else None,
    )


def create_app(
    store: Store | None = None,
    settings: Settings | None = None,
    runtime_events: DurableRuntimeEventStore | None = None,
    deploy_deps: DeployDeps | None = None,
    arena_model_launcher: Any | None = None,
    replay_catalog: ReplayCatalog | None = None,
) -> FastAPI:
    """Create the Veridex demo FastAPI application.

    Factory pattern: inject ``store`` / ``settings`` in tests; omit for the default in-process
    store and env-backed settings.

    Args:
        store: Optional :class:`~veridex.store.Store` override.  Defaults to the
            module-level ``_default_store`` (an :class:`~veridex.store.InMemoryStore`).
        settings: Optional :class:`~veridex.config.Settings` override carrying the operator
            control-plane credentials.  Defaults to :func:`~veridex.config.get_settings`.
        runtime_events: Optional :class:`~veridex.runtime.runtime_store.DurableRuntimeEventStore`
            override — the crash-safe OPS-channel spool (I-4) whose sink the deploy/runtime path emits
            through and whose durable rows the owner-scoped drawer route reads (SEC-003). Defaults to a
            fresh per-app store over ``resolved_store`` + ``WAL_DIR``. Distinct from the evidence log.

    Returns:
        A configured :class:`fastapi.FastAPI` application: the Phase-1 demo trio, the six
        Phase-2A competition endpoints, plus the Phase-2B control-plane endpoints
        (``GET /competitions/{id}/executions``, ``GET /executions/{id}``,
        ``POST /competitions/{id}/kill-switch``, ``POST /executions/{id}/approve``).
    """
    resolved_store: Store = store if store is not None else _default_store
    resolved_settings: Settings = settings if settings is not None else get_settings()
    # ONE durable OPS spool shared across the app: its sink is threaded into the deploy/runtime path
    # (below) so events are actually PRODUCED, and its durable rows back the owner-scoped read route —
    # NEVER a fresh per-app store that nothing writes (the "empty drawer" bug this task prevents).
    resolved_runtime_events: DurableRuntimeEventStore = (
        runtime_events if runtime_events is not None else DurableRuntimeEventStore(store=resolved_store)
    )
    # I-1 Privy auth boundary reused for the owner-scoped drawer route (instance owner == principal.did).
    require_principal = make_require_principal(resolved_settings)
    # II-9 arena model seam: the owner-scoped arena endpoint drives det-Drift vs LLM-Drift over this
    # launcher. Defaults to the lazy production Agno launcher (agno-free at import; a network call
    # happens only on ``launch``); tests inject a hand-controlled offline fake so no LLM/network runs.
    resolved_arena_model_launcher: Any = (
        arena_model_launcher if arena_model_launcher is not None else default_model_launcher()
    )

    @contextlib.asynccontextmanager
    async def _lifespan(app_: FastAPI) -> AsyncIterator[None]:
        """App lifespan — start the durable OPS flusher; on shutdown drain it + cancel deploy tasks."""
        await resolved_runtime_events.start()
        yield
        await resolved_runtime_events.aclose()
        await cancel_deploy_tasks(app_)

    app = FastAPI(
        title="Veridex Demo API",
        description="TxLINE Agent Proof Arena — Phase 1 demo surface (REQ-115 / AC-115).",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # I-5: browser reach from the web origin. Exact-origin allow-list from ``CORS_ORIGINS`` (never a
    # wildcard with credentials — Starlette would silently disable ``*`` under allow_credentials).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins_from_env(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # III-3: the observable last-seen of the ACTIVE live stream. Default is honestly DISCONNECTED
    # (no stream running); a live launcher passes THIS object as ``run_live_window(feed_status=...)``
    # so /feed/health derives its state from real ingestion, never from credential presence. Read-only
    # OPERATIONAL TELEMETRY — never scored, never in evidence. Exposed on app.state for the launcher.
    live_feed_status = LiveFeedStatus()
    app.state.live_feed_status = live_feed_status

    # R-3: the AUTHORITATIVE, hash-verified R-2 ReplayPack catalog this app serves replay identity from.
    # PROVIDED path (the served composition + tests): ``build_agentos_app`` threads the ALREADY-BUILT
    # catalog through, so it is USED as-is and NO env catalog is built — the served path hash-verifies /
    # copies packs exactly ONCE (in ``create_server_app``), never a second env-built throwaway. FALLBACK
    # path (a standalone ``create_app`` caller with no catalog): build one from the read-only
    # ``REPLAY_PACK_ROOT`` (+ optional writable ``REPLAY_CAPTURE_ROOT``). Either way ``/replay-packs`` and
    # the ``pack_id``-bound ``POST /backtests`` resolve packs SERVER-side against the verified catalog —
    # never a client filesystem path. A blank/unset root yields an empty catalog (fail-closed: every
    # pack_id is an unknown 404).
    resolved_replay_catalog = (
        replay_catalog
        if replay_catalog is not None
        else build_catalog(
            os.environ.get("REPLAY_PACK_ROOT", ""),
            capture_root=os.environ.get("REPLAY_CAPTURE_ROOT", "") or None,
        )
    )
    app.state.replay_catalog = resolved_replay_catalog

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Generic liveness probe (I-5): always 200, never gated on auth or the DB."""
        return {"status": "ok"}

    # Per-app registry: run_id → {anchor_status, source_mode}.
    # Populated by POST /demo/run; consumed by GET /leaderboard.
    _run_meta: dict[str, dict[str, str]] = {}

    # Per-app registry: backtest_id → BacktestReport (T15).
    # Populated by POST /backtests; consumed by GET /backtests/{backtest_id}.
    _backtest_reports: dict[str, BacktestReport] = {}

    # Per-app live-fanout manager (owns per-client bounded broadcast queues). The live producer
    # (start_competition's broadcast callback) persists each event BEFORE broadcasting it.
    arena_manager = ArenaConnectionManager()

    # --- Dependency -------------------------------------------------------

    def _get_store() -> Store:
        """Closure-captured store dependency for this app instance.

        Returns:
            The resolved :class:`~veridex.store.Store` for this application.
        """
        return resolved_store

    # --- Control-plane auth (REQ-2B-18/19; fail-closed) -------------------

    def _authenticate(authorization: str | None) -> str | None:
        """Validate a ``Bearer`` operator token; return the principal ``operator_id``.

        Fail-closed: a missing/malformed header, or a token that does not match the configured
        ``operator_token`` (or no token configured at all), raises 401.

        Args:
            authorization: The raw ``Authorization`` header value, if present.

        Returns:
            The authenticated principal's ``operator_id`` (``settings.operator_id``).

        Raises:
            HTTPException: 401 if authentication fails.
        """
        token = resolved_settings.operator_token
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing or malformed operator bearer token")
        presented = authorization.removeprefix("Bearer ")
        if token is None or presented != token:
            raise HTTPException(status_code=401, detail="invalid operator token")
        return resolved_settings.operator_id

    def _require_operator(authorization: str | None = Header(default=None)) -> str | None:  # noqa: B008
        """FastAPI dependency: require a valid operator bearer token; return the principal id."""
        return _authenticate(authorization)

    def _check_owner(competition: Competition, principal_operator_id: str | None) -> None:
        """Enforce per-competition ownership: 403 if the competition is owned by another operator.

        A competition with ``operator_id is None`` is owner-less, so ANY authenticated operator
        may act on it (the single-operator-token model — authentication is already enforced by
        ``_require_operator`` / ``_authenticate`` upstream).

        Args:
            competition: The loaded competition.
            principal_operator_id: The authenticated principal's ``operator_id``.

        Raises:
            HTTPException: 403 if ``competition.config.operator_id`` is set and differs from the
                principal.
        """
        owner = competition.config.operator_id
        if owner is not None and owner != principal_operator_id:
            raise HTTPException(status_code=403, detail="operator does not own this competition")

    def _require_competition_owner(competition: Competition, principal_did: str) -> None:
        """Enforce Privy owner-scoping on a competition write (I-7b; fail-closed).

        The competition's ``owner_id`` is SERVER-DERIVED (set to the creator's principal DID at
        create time). A competition whose ``owner_id`` is ``None`` is legacy/UNOWNED: it is refused
        to EVERY caller (403) — an unowned competition is never silently inherited (mirror I-2's
        fail-closed None handling). This is distinct from :func:`_check_owner`, which gates the
        Phase-2B control-plane on the operator-token ``config.operator_id``.

        Args:
            competition: The loaded competition.
            principal_did: The authenticated Privy principal's DID.

        Raises:
            HTTPException: 403 if the competition is unowned or owned by a different principal.
        """
        if competition.owner_id is None or competition.owner_id != principal_did:
            raise HTTPException(status_code=403, detail="principal does not own this competition")

    # --- POST /demo/run ---------------------------------------------------

    @app.post("/demo/run", response_model=DemoRunResponse)
    async def demo_run(dep_store: Store = Depends(_get_store)) -> DemoRunResponse:  # noqa: B008
        """Run a demo competition and return the full artifact bundle.

        Drives two deterministic agents (no LLM, no network) over the bundled
        fixture ticks.  Anchoring is skipped (``anchor_fn=None``) so the call
        completes offline in < 1 s.

        Args:
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.DemoRunResponse` with ``run_id``,
            ``anchor_status``, ``leaderboard``, and ``proof_card``.
        """
        ticks = build_demo_ticks()
        agents = [
            deterministic_agent("agent-alpha"),
            contrarian_agent("agent-beta"),
        ]
        result = await run_demo_competition(
            ticks,
            agents,
            source_mode="replay",
            store=dep_store,
            anchor_fn=None,  # offline: not_anchored (no Solana creds needed)
        )

        _run_meta[result.run.run_id] = {
            "anchor_status": result.anchor_status,
            "source_mode": "replay",
        }

        rows = [LeaderboardRow(**row) for row in result.leaderboard]
        return DemoRunResponse(
            run_id=result.run.run_id,
            anchor_status=result.anchor_status,
            leaderboard=rows,
            proof_card=result.proof_card,
        )

    # --- GET /leaderboard -------------------------------------------------

    @app.get("/leaderboard", response_model=LeaderboardResponse)
    async def get_leaderboard(dep_store: Store = Depends(_get_store)) -> LeaderboardResponse:  # noqa: B008
        """Return the cross-run leaderboard aggregated from all stored runs.

        Iterates over every run registered by ``POST /demo/run``, re-scores each
        run from persisted events, tags rows with anchor/source metadata, and
        delegates aggregation + ranking to :func:`veridex.leaderboard.leaderboard`.

        Args:
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.LeaderboardResponse` with ranked rows.
        """
        all_score_rows: list[dict[str, Any]] = []

        for run_id, meta in _run_meta.items():
            try:
                run_result = await dep_store.load_run(run_id)
            except KeyError:
                continue
            for row in score_run(run_result):
                all_score_rows.append(
                    {
                        **row,
                        "anchor_status": meta["anchor_status"],
                        "source_mode": meta["source_mode"],
                    }
                )

        rows_data = _build_leaderboard(all_score_rows) if all_score_rows else []
        return LeaderboardResponse(rows=[LeaderboardRow(**r) for r in rows_data])

    # --- GET /replay-packs (R-3 — the verified R-2 catalog, read-only) ----------

    @app.get("/replay-packs", response_model=ReplayPackListResponse)
    async def list_replay_packs() -> ReplayPackListResponse:
        """List the AUTHORITATIVE, hash-verified R-2 ReplayPack catalog (read-only, deterministic).

        A pure projection of ``app.state.replay_catalog`` — every entry's verified ``content_hash``,
        HONEST ``provenance``, ``is_genuine`` marker, and catalogued ``fixtures``, sorted by ``pack_id``.
        NOT a filesystem scan: it surfaces ONLY the packs the R-2 catalog already hash-verified and
        allowlisted (a tampered/unverified pack was fail-closed excluded and is never listed). The
        internal ``pack_dir`` filesystem path is DELIBERATELY not exposed — the browser addresses packs
        by ``pack_id`` only.
        """
        catalog: ReplayCatalog = app.state.replay_catalog
        snapshot = catalog.snapshot()
        packs = [_replay_pack_info(snapshot[pack_id]) for pack_id in sorted(snapshot)]
        return ReplayPackListResponse(packs=packs)

    @app.get("/replay-packs/{pack_id}", response_model=ReplayPackInfo)
    async def get_replay_pack(pack_id: str) -> ReplayPackInfo:
        """Return one verified R-2 pack's hash + provenance + fixtures by ``pack_id`` (unknown -> 404)."""
        entry = app.state.replay_catalog.get(pack_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown pack_id: {pack_id}")
        return _replay_pack_info(entry)

    @app.get(
        "/replay-packs/{pack_id}/fixtures/{fixture_id}/markets",
        response_model=ReplayMarketsResponse,
    )
    async def get_replay_fixture_markets(pack_id: str, fixture_id: int) -> ReplayMarketsResponse:
        """Project one replay fixture's LAST-KNOWN odds per market, folded across the WHOLE tape (M11).

        Resolves the pack SERVER-side against the verified R-2 catalog (never a filesystem path): an
        unknown ``pack_id`` or a fixture not catalogued for the pack is a 404. The fixture tape is loaded
        via :func:`~veridex.ingest.replay_pack.load_pack_fixture_states_bound`, binding the replayed bytes
        to the catalog's verified ``content_hash`` — a tampered pack (bytes swapped after admission) fails
        closed with a 422.

        The fold keeps the LAST-SEEN value per ``market_key`` over ALL tape states (the final tick alone
        carries only the markets present at that instant — 1 of ~30 here), plus the ``ts`` and per-market
        ``in_running`` of the state each market was last seen in. HONESTY (M4): a suspended market keeps
        its EMPTY ``stable_prob_bps`` (never back-filled) while retaining last-known ``stable_price``.
        """
        entry = app.state.replay_catalog.get(pack_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown pack_id: {pack_id}")
        if fixture_id not in entry.fixtures:
            raise HTTPException(
                status_code=404,
                detail=f"fixture_id {fixture_id} is not catalogued for pack {pack_id!r}",
            )
        try:
            states = load_pack_fixture_states_bound(
                entry.pack_dir, fixture_id, expected_content_hash=entry.content_hash
            )
        except ValueError as exc:
            # Fail closed: a tampered/corrupt pack (content_hash drift, fixture<->file swap) is a 422,
            # never a silently-served partial projection. PackIntegrityError subclasses ValueError and the
            # loader wraps every file read into a content_hash_drift PackIntegrityError, so no bare
            # FileNotFoundError escapes — a single ``except ValueError`` fails closed on both.
            #
            # SECURITY: surface the STABLE machine reason code, NEVER str(exc). The raw message embeds the
            # absolute internal pack_dir path, which per the invariant above is DELIBERATELY not exposed —
            # the browser addresses packs by pack_id only. A bare ValueError with no .reason falls back to
            # the generic code (still no path).
            reason = getattr(exc, "reason", "pack_integrity_error")
            raise HTTPException(status_code=422, detail=reason) from exc

        # M11 fold: LAST-SEEN value per market_key across ALL states (NOT just states[-1], which carries
        # only the markets present at the final tick). in_running is derived from the state's phase — at
        # batch_size=1 (the load default) each state is folded from ONE record, so its phase is exactly
        # that record's InRunning; MarketState carries no per-market flag, so this is the honest source.
        folded: dict[str, ReplayMarketRow] = {}
        for state in states:
            for key, market in state.markets.items():
                folded[key] = ReplayMarketRow(
                    market_key=key,
                    in_running=bool(state.phase),
                    suspended=bool(market["suspended"]),
                    ts=int(state.ts),
                    # PRESERVE the empty prob map for a suspended market — never fill it from the price.
                    stable_prob_bps=dict(market["stable_prob_bps"]),
                    stable_price=dict(market["stable_price"]),
                )
        return ReplayMarketsResponse(fixture_id=fixture_id, markets=list(folded.values()))

    # --- GET /agents/roster (PUBLIC — the deployed-agent roster, read-only) -----

    @app.get("/agents/roster", response_model=AgentRosterResponse)
    async def list_agents_roster(
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> AgentRosterResponse:
        """PUBLIC (unauthenticated) HONEST agent directory, keyed on the immutable ``public_agent_id``.

        Mirrors ``/replay-packs`` (read-only, NO auth). Sources from ``store.list_public_agents()``
        joined to deployment state and admits an agent ONLY when its ``visibility == PUBLIC`` AND it has
        a linked instance whose deploy status is ``SEALED`` — private / pending / failed / running /
        unlinked agents are EXCLUDED. TRUST SURFACE: the owner is rendered ONLY via the safe
        ``owner_public_label``; a raw ``operator_id`` / ``owner_ref`` NEVER appears. Performance columns
        and ``proof_state`` are honestly absent (``None`` / ``"unscored"``) until the agent carries
        scored PUBLIC_AGENTS board rows, then the REAL pooled values — never fabricated. No public+sealed
        agents -> honest-empty. Rows are sorted deterministically by ``public_agent_id``.
        """
        # Reverse the instance→public_agent_id link into {public_agent_id: sealed instance}. Only SEALED
        # instances qualify; first sealed instance (by instance_id) wins deterministically per agent.
        instances = await dep_store.list_agent_instances()
        sealed_by_agent: dict[str, AgentInstance] = {}
        for inst in sorted(instances, key=lambda i: i.instance_id):
            if inst.status is not DeployStatus.SEALED:
                continue
            pub_id = await dep_store.get_instance_public_agent_id(inst.instance_id)
            if pub_id is None or pub_id in sealed_by_agent:
                continue
            sealed_by_agent[pub_id] = inst

        # Join the PUBLIC_AGENTS directional board (visibility-filtered, pooled) by public_agent_id.
        board = await directional_board(dep_store, board_kind=BoardKind.PUBLIC_AGENTS)
        scored_by_agent = {row["public_agent_id"]: row for row in board}

        rows = [
            _agent_roster_entry(agent, sealed_by_agent[agent.public_agent_id],
                                scored_by_agent.get(agent.public_agent_id))
            for agent in await dep_store.list_public_agents()
            if agent.visibility is Visibility.PUBLIC and agent.public_agent_id in sealed_by_agent
        ]
        rows.sort(key=lambda r: r.public_agent_id)
        return AgentRosterResponse(agents=rows)

    # --- GET /leaderboard/directional (Official Replay League completion layer) --

    @app.get("/leaderboard/directional")
    async def get_directional_leaderboard(
        board_kind: BoardKind = Query(...),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> dict[str, Any]:
        """Return the directional leaderboard for a closed ``board_kind``, visibility-joined at read time.

        A thin HTTP shell over :func:`veridex.public_projection.directional_board`: it loads durable
        projected rows, joins CURRENT public-agent visibility (and, for
        :attr:`~veridex.public_projection.BoardKind.OFFICIAL_BENCHMARK`, ``operator_class``), and hands
        the survivors to the pure aggregator. The route does NOT rank or aggregate — that is
        ``directional_board``'s job; it only serializes the result. ``board_kind`` is the closed
        :class:`~veridex.public_projection.BoardKind` enum, so an unknown value is a 422 (never a silent
        default). No scored public rows -> ``rows: []`` (honest-empty, never fabricated).
        """
        rows = await directional_board(dep_store, board_kind=board_kind)
        return {"board_kind": board_kind.value, "rows": rows}

    # --- POST /backtests (T15 — replay a ReplayPack → honest BacktestReport) ----

    @app.post("/backtests", response_model=BacktestRunResponse)
    async def create_backtest(body: BacktestRunRequest) -> BacktestRunResponse:
        """Replay a catalogued ReplayPack fixture through the live core and store its BacktestReport.

        Deterministic + offline: the run is driven over a single reproducible baseline agent (no
        LLM, no network). The report is a pure projection of the sealed run (SEC-003) and its mode
        label is always ``"Backtest"`` (REQ-2D-304 — a replay is never dressed up as live).

        R-3 — pack-bound competition identity (trust boundary): the browser sends a ``pack_id`` (a
        catalog KEY), never a filesystem path. The pack is resolved SERVER-side against the verified
        R-2 catalog (``app.state.replay_catalog``); an unknown ``pack_id`` is a 404 and a ``fixture_id``
        not catalogued for the pack is a 422 — a client can NEVER point the replay loader at an arbitrary
        directory (the former ``pack_dir`` path-traversal surface is gone). The report's replay identity
        is pinned to the catalog: the bound ``content_hash`` is SERVER-derived from the verified entry.

        Args:
            body: The backtest request (``pack_id``, ``fixture_id``, window spec).

        Returns:
            A :class:`~veridex.api.schemas.BacktestRunResponse` with the ``backtest_id`` to fetch.

        Raises:
            HTTPException: 404 if ``pack_id`` is not in the verified catalog; 422 if the fixture is not
                catalogued for the pack; 400 if the window spec is invalid or the pack fails to replay.
        """
        catalog: ReplayCatalog = app.state.replay_catalog
        entry = catalog.get(body.pack_id)
        if entry is None:
            # Unknown/unverified pack_id — never a filesystem read. Fail-closed 404.
            raise HTTPException(status_code=404, detail=f"unknown pack_id: {body.pack_id}")
        if body.fixture_id not in entry.fixtures:
            raise HTTPException(
                status_code=422,
                detail=f"fixture_id {body.fixture_id} is not catalogued for pack {body.pack_id!r}",
            )
        try:
            window = RunWindow(
                window_id=body.window_id,
                fixture_id=body.fixture_id,
                market_allowlist=body.market_allowlist,
                end_rule=body.end_rule,  # type: ignore[arg-type]  # validated by RunWindow
                duration_s=body.duration_s,
                min_clv_horizon_s=body.min_clv_horizon_s,
            )
            # Resolve the pack SERVER-side from the verified catalog entry (its OWNED, immutable path) —
            # never a client-provided directory. The report's bound content_hash is therefore derived
            # from the R-2-verified bytes, pinning the run's replay identity to the catalogued pack.
            _, report = await run_backtest(
                Path(entry.pack_dir),
                body.fixture_id,
                [deterministic_agent("baseline")],
                window=window,
            )
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Binding invariant (defense-in-depth): the sealed report's identity MUST be the catalog's
        # verified identity, not anything client-influenced. This can only trip on a server-side
        # catalog/pack inconsistency (the catalog admits only recompute-verified packs), so a mismatch
        # is a 500 — never silently served.
        if report.pack_id != body.pack_id or report.content_hash != entry.content_hash:
            raise HTTPException(status_code=500, detail="replay identity did not match the verified catalog entry")

        _backtest_reports[report.run_id] = report
        return BacktestRunResponse(backtest_id=report.run_id, mode_label=report.mode_label, run_id=report.run_id)

    # --- GET /backtests/{backtest_id} (fetch the stored BacktestReport) --------

    @app.get("/backtests/{backtest_id}", response_model=BacktestReport)
    async def get_backtest(backtest_id: str) -> BacktestReport:
        """Return a previously-produced :class:`BacktestReport` by its id (404 if unknown)."""
        report = _backtest_reports.get(backtest_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"unknown backtest_id: {backtest_id}")
        return report

    # --- GET /feed/health (WD-4: live-feed shown + judge-testable) --------

    @app.get("/feed/health", response_model=FeedHealthResponse)
    async def feed_health_endpoint(source_mode: str = Query(default="replay")) -> FeedHealthResponse:  # noqa: B008
        """Report live/replay TxLINE feed health from the ACTIVE stream's last-seen (III-3 / REQ-053).

        Read-only OPERATIONAL TELEMETRY: nothing here enters ``evidence_hash``, the proof checks,
        scoring, or the leaderboard. Surfaces ``txline_configured`` from credential PRESENCE only
        (never the secret values — COM-001).

        III-3 HONESTY: ``connected`` / ``last_tick_ts`` / ``feed_state`` are derived from the
        observable :class:`~veridex.ingest.feed_health.LiveFeedStatus` that the live runner records
        into — NOT from credential presence. Credentials being configured does NOT make the feed
        live; only an ACTIVE stream does. The honest FIVE-STATE ``feed_state``:

        * ``live`` — a recent odds RECORD was received;
        * ``heartbeat_only`` — a recent HEARTBEAT, no recent odds (liveness, no market data);
        * ``stale`` — the last-seen frame is beyond the staleness budget;
        * ``disconnected`` — no active connection (the offline/no-stream default);
        * ``recorded_replay`` — replay mode; NEVER labelled live.

        A judge can curl this against the deployed devnet API: with no active live run it honestly
        reads ``disconnected`` (live) / ``recorded_replay`` (replay), and it flips to ``live`` while
        a windowed live run is streaming records.

        Args:
            source_mode: ``"replay"`` (default) or ``"live"``.

        Returns:
            A :class:`~veridex.api.schemas.FeedHealthResponse`.
        """
        configured = resolved_settings.txline_jwt is not None and resolved_settings.txline_api_token is not None
        now_ts = int(time.time())
        report = live_feed_status.report(
            source_mode=source_mode, txline_configured=configured, now_ts=now_ts
        )
        state = live_feed_status.feed_state(source_mode=source_mode, now_ts=now_ts)
        return FeedHealthResponse(
            source_mode=report.source_mode,
            events_per_min=None,  # A's throughput view — no live counter wired yet (honest None)
            ws_live=report.connected,  # both views agree: ws live == connected
            last_tick_ts=report.last_tick_ts,
            anchor_status="not_anchored",  # honest default for the offline/health surface
            txline_configured=report.txline_configured,
            connected=report.connected,
            ticks_seen=report.ticks_seen,
            fixture_id=report.fixture_id,
            staleness_s=report.staleness_s,
            stale=report.stale,
            feed_state=state.value,
        )

    # --- GET /runs/{run_id} -----------------------------------------------

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str, dep_store: Store = Depends(_get_store)) -> dict[str, Any]:  # noqa: B008
        """Return the proof card for a previously persisted run.

        Loads the run from the store, recomputes checks from the score rows,
        and assembles a full proof card with an anchor block.

        Args:
            run_id: The run identifier returned by ``POST /demo/run``.
            dep_store: Injected store dependency.

        Returns:
            A proof-card dict with ``verifier_version``, ``run``, ``lineage``,
            ``evidence``, ``checks``, and ``anchor``.

        Raises:
            HTTPException: 404 when no run with ``run_id`` is found in the store.
        """
        try:
            run_result = await dep_store.load_run(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None

        scores = score_run(run_result)
        checks = read_path_check_block(scores, run_result)
        metrics = build_performance_metrics(scores)  # SEC-001: CLV lives here, not in checks
        # Recover anchor_status from the registry; fall back to not_anchored for
        # runs loaded from an externally supplied store.
        meta = _run_meta.get(run_id, {})
        anchor_status = meta.get("anchor_status", "not_anchored")
        anchor_block: dict[str, Any] = {
            "status": anchor_status,
            "signature": None,
            "cluster": DEFAULT_CLUSTER,
        }
        return proof_card_from_run_result(
            run_result,
            checks=checks,
            anchor=anchor_block,
            schema_versions=dict(SCHEMA_VERSIONS),
            metrics=metrics,
        )

    # --- GET /runs/{run_id}/actions/{seq} (C1 Inspector — per-action forensic view) -------

    @app.get("/runs/{run_id}/actions/{seq}", response_model=InspectorRecord)
    async def get_inspector_record(
        run_id: str,
        seq: int,
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> InspectorRecord:
        """Per-action forensic record for one sealed decision (C1 Inspector — killer-flow step).

        Read-only over the sealed run. ``seq`` is the decision event's ``sequence_no`` in the run's
        event log (the canonical-stream seq the cockpit links from). Serializes from SEALED data:
        the :class:`AgentAction` + its entry ``market_state`` from ``run_events``, and the
        law-recomputed ``clv_bps`` from the matching ``score_rows`` entry. The action's
        ``reason``/``confidence``/``claimed_edge_bps`` are surfaced as ``untrusted_llm_metadata`` —
        recorded-but-NEVER-scored (SEC-003/007); the frontend fences them. Never mutates the seal,
        never recomputes the evidence hash, never calls an LLM (it reads recorded params).

        Args:
            run_id: The sealed run identifier.
            seq: The decision event's ``sequence_no`` in the run's event log.
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.InspectorRecord`.

        Raises:
            HTTPException: 404 when the run is unknown, or ``seq`` is not a decision/action event.
        """
        try:
            run_result = await dep_store.load_run(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None

        # Walk the sealed log in sequence order; track the most recent tick snapshot (the action's
        # entry market state) and stop at the requested decision event.
        entry_snapshot: dict[str, Any] | None = None
        decision: dict[str, Any] | None = None
        for event in sorted(run_result.run_events, key=lambda e: e["sequence_no"]):
            if event.get("event_type") == "tick" and event.get("state_snapshot_json"):
                entry_snapshot = json.loads(event["state_snapshot_json"])
            if event.get("sequence_no") == seq:
                decision = event
                break

        if decision is None or decision.get("event_type") != "decision" or not decision.get("action_payload_json"):
            raise HTTPException(status_code=404, detail=f"no action at seq {seq} in run {run_id!r}")

        agent_action = json.loads(decision["action_payload_json"])
        result_payload = json.loads(decision["result_payload_json"]) if decision.get("result_payload_json") else {}
        agent_id = str(result_payload.get("agent_id", ""))
        tick_seq = int(entry_snapshot["tick_seq"]) if entry_snapshot and "tick_seq" in entry_snapshot else -1

        # SCORED metric: the deterministic law-recomputed clv from the matching sealed score row —
        # NEVER the agent's claim. "pending" (non-numeric) for a valid WAIT/abstention.
        score_row = next(
            (r for r in run_result.score_rows if r.get("tick_seq") == tick_seq and r.get("agent_id") == agent_id),
            {},
        )
        clv_bps = score_row.get("clv_bps", "pending")
        recompute = {
            "recomputed_edge_bps": score_row.get("recomputed_edge_bps"),
            "clv_bps": clv_bps,
            "valid": score_row.get("valid"),
        }

        # UNTRUSTED (SEC-003/007): reason/confidence/claimed_edge_bps are recorded-but-NEVER-scored.
        params = agent_action.get("params", {}) or {}
        untrusted = {key: params[key] for key in ("reason", "confidence", "claimed_edge_bps") if key in params}

        return InspectorRecord(
            run_id=run_id,
            agent_id=agent_id,
            tick_seq=tick_seq,
            market_state=entry_snapshot or {},
            agent_action=agent_action,
            recompute=recompute,
            clv_bps=clv_bps,
            untrusted_llm_metadata=untrusted,
        )

    # --- POST /runs/{run_id}/verify (WD-1 authoritative recompute, REQ-050 / AC-020) ------

    @app.post("/runs/{run_id}/verify", response_model=VerifyResponse)
    async def verify_run_endpoint(run_id: str, dep_store: Store = Depends(_get_store)) -> VerifyResponse:  # noqa: B008
        """Authoritatively recompute a sealed run: confirm the evidence hash + rebuild the proof.

        The frontend NEVER reimplements the law (CON-003): it calls this, renders the returned
        recompute + hash-confirmation, and opens the Solana tx. ``verified`` is ``True`` iff the
        recomputed evidence hash matches the sealed one (fail-closed on any recompute error).

        Delegates the deterministic recompute to the WD-1 trust-path core
        (:func:`veridex.verifier.recompute.verify_run`): it recomputes the evidence hash over the
        sealed ``run_events`` prefix, re-derives the score root, and rebuilds the run manifest
        (base + ``root_forest``) so ``manifest_hash`` is byte-identical to the anchored Memo
        payload — receipt-independent (SEC-004) and read-only over the seal. The Proof-Checks block
        (7 ``CheckId``, no clv — SEC-001) + the separate Performance-Metrics block (clv lives here)
        are composed via the SAME builders the Proof-Card GET uses, so both endpoints return one
        consistent shape to the Proof Card (Plan C1).
        """
        try:
            run_result = await dep_store.load_run(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None

        report = _verify_run_core(run_result)
        metrics = build_performance_metrics(report.score_rows)  # SEC-001: CLV lives here, not in checks

        meta = _run_meta.get(run_id, {})
        signature = meta.get("signature")  # present only after a real on-chain anchor
        anchor_block: dict[str, Any] = {
            "status": meta.get("anchor_status", "not_anchored"),
            "signature": signature,
            "cluster": DEFAULT_CLUSTER,
            "explorer_url": explorer_tx_url(signature, cluster=DEFAULT_CLUSTER),
        }
        # Bind the FULL verdict: pass the reconstructed manifest/manifest_hash + anchor so
        # MANIFEST_BOUND confirms the seal — the 2-arg read_path_check_block omits them, leaving
        # manifest_bound=not_applicable, which false-reds the manifest hash on an honest run.
        check_results = build_check_results(
            scores=report.score_rows,
            run=run_result,
            manifest=report.manifest,
            manifest_hash=report.manifest_hash,
            anchor=anchor_block,
            source_mode=report.source_mode,
        )
        checks = check_results_to_proof_block(check_results)
        proof_card = proof_card_from_run_result(
            run_result,
            checks=checks,
            anchor=anchor_block,
            schema_versions=dict(SCHEMA_VERSIONS),
            metrics=metrics,
        )
        return VerifyResponse(
            run_id=report.run_id,
            verified=report.verified,
            evidence_hash=report.evidence_hash_sealed,
            recomputed_evidence_hash=(
                report.evidence_hash_recomputed
                if report.evidence_hash_recomputed is not None
                else f"recompute_error:{report.evidence_error}"
            ),
            manifest_hash=report.manifest_hash,
            checks=checks,
            metrics=metrics,
            anchor=anchor_block,
            proof_card=proof_card,
        )

    # --- POST /runs/{run_id}/explain (Proof Explainer — educational LLM narration) --------

    @app.post("/runs/{run_id}/explain")
    async def explain_run_endpoint(
        run_id: str,
        body: ExplainRequest | None = None,
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> dict[str, str]:
        """Narrate an already-produced proof in plain language — the Proof Explainer (NOT a verifier).

        READ-ONLY and NON-scoring: this endpoint recomputes the served proof artifact + verify
        view-models exactly as ``GET /runs/{id}`` and ``POST /runs/{id}/verify`` do, assembles a
        SANITIZED read-model (ONLY the served ``ProofArtifactResponse`` + ``VerifyResponse`` fields
        + the pinned glossary — never the raw ``RunResult``, unsealed/live state, or the store
        handle), and hands that dict to the LLM explainer. It performs NO writes, NO mutation, and
        NO control. The deterministic Verify result remains the source of truth; the explanation is
        educational only and carries that disclaimer.

        Args:
            run_id: The sealed run identifier.
            body: Optional ``{"question": str, "target_field": str}`` to focus the narration.
            dep_store: Injected store dependency.

        Returns:
            ``{"explanation": ..., "disclaimer": ..., "footer": ...}``.

        Raises:
            HTTPException: 404 when no run with ``run_id`` is found.
        """
        try:
            run_result = await dep_store.load_run(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None

        # Rebuild the SERVED verify view-model via the SAME trust-path core the verify route uses.
        report = _verify_run_core(run_result)
        metrics = build_performance_metrics(report.score_rows)  # SEC-001: CLV lives here, not checks
        meta = _run_meta.get(run_id, {})
        signature = meta.get("signature")
        anchor_block: dict[str, Any] = {
            "status": meta.get("anchor_status", "not_anchored"),
            "signature": signature,
            "cluster": DEFAULT_CLUSTER,
            "explorer_url": explorer_tx_url(signature, cluster=DEFAULT_CLUSTER),
        }
        check_results = build_check_results(
            scores=report.score_rows,
            run=run_result,
            manifest=report.manifest,
            manifest_hash=report.manifest_hash,
            anchor=anchor_block,
            source_mode=report.source_mode,
        )
        checks = check_results_to_proof_block(check_results)
        proof_artifact = proof_card_from_run_result(
            run_result,
            checks=checks,
            anchor=anchor_block,
            schema_versions=dict(SCHEMA_VERSIONS),
            metrics=metrics,
        )

        # SANITIZED read-model: ONLY served ProofArtifactResponse + VerifyResponse fields + glossary.
        # No raw RunResult, no run_events, no unsealed/live state, no store handle crosses this line.
        read_model: dict[str, Any] = {
            "proof_artifact": proof_artifact,
            "verify": {
                "run_id": report.run_id,
                "verified": report.verified,
                "evidence_hash": report.evidence_hash_sealed,
                "recomputed_evidence_hash": (
                    report.evidence_hash_recomputed
                    if report.evidence_hash_recomputed is not None
                    else f"recompute_error:{report.evidence_error}"
                ),
                "manifest_hash": report.manifest_hash,
                "checks": checks,
                "metrics": metrics,
                "anchor": anchor_block,
            },
            "glossary": GLOSSARY_DEFINITIONS,
        }

        req = body or ExplainRequest()
        return await explain_proof(
            read_model,
            question=req.question,
            target_field=req.target_field,
            settings=resolved_settings,
        )

    # --- POST /competitions -----------------------------------------------

    @app.post("/competitions", response_model=CompetitionCreateResponse)
    async def create_competition_endpoint(
        config: CompetitionConfig,
        principal: PrivyPrincipal = Depends(require_principal),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> CompetitionCreateResponse:
        """Create a new DRAFT competition owned by the authenticated principal (I-7b).

        The ``owner_id`` is SERVER-DERIVED from the verified Privy principal (``principal.did``); a
        client-supplied owner is structurally unable to reach it (the request body is a
        :class:`~veridex.competition.models.CompetitionConfig`, which has no ``owner_id`` field, so
        any forged key is dropped by the model). Constructed inline (rather than via
        ``service.create_competition``) so the owner is set at construction — the service signature
        is frozen and out of scope for I-7b.

        Args:
            config: Immutable :class:`~veridex.competition.models.CompetitionConfig`.
            principal: The authenticated Privy principal (injected by ``require_principal``).
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.CompetitionCreateResponse` with ``competition_id``
            and ``status="draft"``.
        """
        # R-4: FREEZE the production-replay identity at the admission boundary when the client NAMES a
        # pack. Resolved SERVER-side from the verified R-2 catalog (fail-closed on unknown/unverified),
        # the frozen ReplayBinding (pack_id + resolved fixture_id + SERVER-derived content_hash) is
        # persisted with the competition and REUSED verbatim at start — never re-selected, so an R-0b
        # promotion between create and start can never change the tape. An UNBOUND competition (no pack
        # named) stays unbound here and resolves ONCE at start (single-pack only). content_hash is
        # server-owned: the client sends only pack_id + fixture_id (CompetitionConfig has no hash field).
        replay_binding: ReplayBinding | None = None
        if config.pack_id is not None:
            try:
                resolved = resolve_replay_source(
                    app.state.replay_catalog, pack_id=config.pack_id, fixture_id=config.fixture_id
                )
            except ReplayResolutionError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "replay_unresolved", "reason": exc.reason, "message": str(exc)},
                ) from None
            replay_binding = ReplayBinding(
                pack_id=resolved.pack_id, fixture_id=resolved.fixture_id, content_hash=resolved.content_hash
            )
        comp = Competition(
            competition_id=f"c_{uuid4().hex}",
            config=config,
            status=CompetitionStatus.DRAFT,
            entries=[],
            run_id=None,
            owner_id=principal.did,
            replay_binding=replay_binding,
        )
        await dep_store.create_competition(comp)
        return CompetitionCreateResponse(
            competition_id=comp.competition_id,
            status=comp.status.value,
        )

    # --- POST /competitions/{competition_id}/agents -----------------------

    @app.post("/competitions/{competition_id}/agents", response_model=AgentRegisterResponse)
    async def register_agent_endpoint(
        competition_id: str,
        entry: AgentEntry,
        principal: PrivyPrincipal = Depends(require_principal),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> AgentRegisterResponse:
        """Register an agent on a competition's roster (owner-scoped + lifecycle-enforced; I-7b).

        Only the competition's owner may mutate its roster, and only while it is still forming.
        Enforced in order (authorization before state): owner-gate (403), then the lifecycle gates
        (409) — the roster is frozen once the competition has started/finalized, an agent cannot be
        registered twice, and the roster cannot exceed ``config.roster_size``. On success delegates
        to :func:`~veridex.competition.service.register_agent`, which pins ``config_hash`` (CON-207)
        and normalises ``proof_mode`` to the two canonical Phase-2A values.

        Args:
            competition_id: The owning competition.
            entry: Raw agent entry from the wire boundary.
            principal: The authenticated Privy principal (injected by ``require_principal``).
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.AgentRegisterResponse` with ``agent_id``,
            ``config_hash``, and the normalised ``proof_mode``.

        Raises:
            HTTPException: 404 (unknown competition; OR an instance-bound entry whose referenced
                deployed instance is absent/unowned-legacy — mirror I-2, no existence leak), 403
                (caller is not the competition owner; OR the referenced deployed instance is owned by
                another principal — mirror I-2), 409 (roster frozen post-start, duplicate agent, or
                roster cap exceeded), 400 (other domain rejection, e.g. an unrecognised proof_mode).
        """
        try:
            competition = await dep_store.get_competition(competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None

        _require_competition_owner(competition, principal.did)

        # Lifecycle gates (409): the roster is only mutable while the competition is still forming.
        if competition.status not in (CompetitionStatus.DRAFT, CompetitionStatus.OPEN):
            raise HTTPException(
                status_code=409, detail="roster is frozen: competition has already started"
            )
        if any(existing.agent_id == entry.agent_id for existing in competition.entries):
            raise HTTPException(status_code=409, detail=f"agent {entry.agent_id!r} is already registered")
        if len(competition.entries) >= competition.config.roster_size:
            raise HTTPException(
                status_code=409, detail=f"roster is full (cap {competition.config.roster_size})"
            )

        try:
            finalized = await register_agent(dep_store, competition_id, entry, principal_did=principal.did)
        except RosterAdmissionError as exc:
            # Atomic status-guarded admission refused the append (Codex M1): the roster left the mutable
            # window (a racing arena/start claim froze it), a duplicate id, or the roster is full. The
            # pre-checks above catch the common non-racing case; this catches the RACE (status changed
            # between the pre-check read and the guarded write) — fail closed with 409, never a silent
            # post-freeze mutation.
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except RosterInstanceNotOwnedError:
            # Instance-bound entry references a deployed instance owned by another principal — mirror I-2
            # (deploy.py GET /agents/instances/{id}): 403, and no config_hash was read/returned.
            raise HTTPException(status_code=403, detail="principal does not own this agent instance") from None
        except RosterInstanceNotFoundError:
            # Absent OR unowned-legacy(None) instance — mirror I-2: 404, INDISTINGUISHABLE, no existence
            # leak and no config_hash disclosure to a non-owner.
            raise HTTPException(status_code=404, detail="agent instance not found") from None
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None
        except ValueError as exc:
            # Any other domain rejection (e.g. an unrecognised proof_mode) — fail closed with 400.
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return AgentRegisterResponse(
            agent_id=finalized.agent_id,
            config_hash=finalized.config_hash,
            proof_mode=finalized.proof_mode,
        )

    # --- POST /competitions/{competition_id}/start ------------------------

    @app.post("/competitions/{competition_id}/start", response_model=CompetitionStartResponse)
    async def start_competition_endpoint(
        competition_id: str,
        principal: PrivyPrincipal = Depends(require_principal),  # noqa: B008
        authorization: str | None = Header(default=None),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> CompetitionStartResponse:
        """Run a competition offline/deterministically and return the finalized state.

        Offline replay: market data is the SELECTED verified ReplayPack tape (R-4), resolved
        SERVER-side from the R-2 catalog via the FROZEN :class:`~veridex.competition.models.ReplayBinding`
        (reused if bound at create, else resolved ONCE here for a single-pack catalog and persisted).
        Roster entries are built by their DECLARED strategy. This produces a real ≥2-row leaderboard
        without any LLM or network calls, over the SAME ``load_pack_marketstates`` normalizer +
        downstream pipeline (``build_demo_ticks`` stays a CI/test fixture, no longer the production source).

        Auth (fail-closed): EVERY start requires the authenticated Privy owner (I-7b — a non-owner
        is refused 403, regardless of execution mode). ADDITIONALLY, a ``dry_run`` / ``live_guarded``
        start requires a valid operator bearer token (401) AND control-plane ownership (403). The
        live executor lane runs DOWNSTREAM of the seal as a separate derived block and is broadcast
        live (persist-before-broadcast).

        Args:
            competition_id: The competition to start.
            principal: The authenticated Privy principal (injected by ``require_principal``).
            authorization: Operator bearer header (required only for non-paper modes).
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.CompetitionStartResponse` with
            ``status="finalized"`` and ``run_id`` set.

        Raises:
            HTTPException: 404 (unknown competition), 409 (already finalized/running),
                401/403 (control-plane auth for non-paper), 501 (live venue not enabled),
                500 (evidence-prefix integrity breach).
        """
        try:
            competition = await dep_store.get_competition(competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None

        # I-7b: only the Privy owner may start their competition (fail-closed; applies to every
        # execution mode, including paper). The operator-token gate below is an ADDITIONAL,
        # orthogonal control-plane check that stays scoped to non-paper modes.
        _require_competition_owner(competition, principal.did)

        # Fail-closed auth gate for non-paper modes (paper stays public at the operator layer).
        if competition.config.execution_mode != ExecutionMode.PAPER:
            operator_principal = _authenticate(authorization)
            _check_owner(competition, operator_principal)

        # R-4: PRODUCTION replay serves the SELECTED verified pack, not the synthetic demo tape.
        # REUSE the frozen ReplayBinding if present (bound at create, or by a prior start); otherwise
        # this is a legacy/unbound DRAFT — resolve ONCE from the catalog (single-pack only; multiple
        # packs fail closed "pack_id required") and PERSIST that binding BEFORE the claim so the run
        # commits to exactly the tape it replays. The tape is then LOADED from the FROZEN identity
        # (content_hash re-checked against the live catalog) — never re-selected, so an R-0b promotion
        # can neither change the tape nor let the sealed identity diverge from the replayed bytes.
        try:
            if competition.replay_binding is not None:
                resolved = ResolvedReplaySource(
                    pack_id=competition.replay_binding.pack_id,
                    fixture_id=competition.replay_binding.fixture_id,
                    content_hash=competition.replay_binding.content_hash,
                    provenance="",  # observability-only; not part of the frozen identity triple
                    is_genuine=False,
                )
            else:
                resolved = resolve_replay_source(
                    app.state.replay_catalog,
                    pack_id=competition.config.pack_id,
                    fixture_id=competition.config.fixture_id,
                )
                await dep_store.update_competition_replay_binding(
                    competition_id, ReplayBinding(
                pack_id=resolved.pack_id, fixture_id=resolved.fixture_id, content_hash=resolved.content_hash
            )
                )
                # The persist is an only-if-unbound CAS: it is a NO-OP if a CONCURRENT unbound start froze
                # the binding first. That concurrent winner — not necessarily this start — owns the
                # AUTHORITATIVE identity and may WIN the run claim below. Re-read the persisted binding and
                # replay/report THAT, never this start's local resolution, so the tape LOADED and the
                # identity SURFACED both match the single persisted authority (the mirror of the clobber the
                # CAS closed: the honesty invariant that GET never reports an identity the run did not replay).
                authoritative = await dep_store.get_competition(competition_id)
                if authoritative.replay_binding is not None:
                    resolved = ResolvedReplaySource(
                        pack_id=authoritative.replay_binding.pack_id,
                        fixture_id=authoritative.replay_binding.fixture_id,
                        content_hash=authoritative.replay_binding.content_hash,
                        provenance="",  # observability-only; not part of the frozen identity triple
                        is_genuine=False,
                    )
            ticks = load_resolved_marketstates(app.state.replay_catalog, resolved)
        except ReplayResolutionError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": "replay_unresolved", "reason": exc.reason, "message": str(exc)},
            ) from None
        # Build the DECLARED roster (by strategy, position-independent). An entry referencing a
        # Studio-deployed instance runs the ACTUAL deployed contestant (pinned config_hash +
        # effective_config), never a same-named reconstruction — fail-closed on an unknown strategy
        # or a config drift (never a silent substitution). This runs AFTER the I-7b owner-gate above.
        try:
            agents = await build_agents_from_roster(
                competition.entries, get_instance=dep_store.get_agent_instance
            )
        except KeyError as exc:
            # A roster entry references a deployed instance that no longer exists — fail closed.
            raise HTTPException(
                status_code=409, detail=f"rostered deployed instance not found: {exc}"
            ) from None
        except ValueError as exc:
            # Unknown declared strategy or a config drift from the pinned deployed identity — refuse
            # rather than silently substitute or run a drifted contestant.
            raise HTTPException(status_code=400, detail=str(exc)) from None

        async def _broadcast(event: CompetitionEvent) -> None:
            await arena_manager.broadcast(competition_id, event)

        try:
            finalized = await start_competition(dep_store, competition_id, ticks, agents, broadcast=_broadcast)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None
        except CompetitionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except CompetitionIntegrityError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None
        except CompetitionStateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        return CompetitionStartResponse(
            competition_id=finalized.competition_id,
            status=finalized.status.value,
            run_id=finalized.run_id,
            replay_binding=ReplayBinding(
                pack_id=resolved.pack_id, fixture_id=resolved.fixture_id, content_hash=resolved.content_hash
            ).model_dump(mode="json"),
        )

    # --- POST /competitions/{competition_id}/arena ------------------------

    @app.post("/competitions/{competition_id}/arena")
    async def run_competition_arena_endpoint(
        competition_id: str,
        principal: PrivyPrincipal = Depends(require_principal),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> dict[str, Any]:
        """Run the II-9 checkpointed det-Drift vs LLM-Drift arena comparison for an OWNED competition.

        This is the AUTHENTICATED, owner-scoped surface that makes the II-9 fairness/accounting payload
        REACHABLE and OBSERVABLE over HTTP (Codex M1). It is deliberately SEPARATE from the legacy
        unauthenticated ``POST /demo/run`` (an offline deterministic/contrarian demo — the wrong roster
        and authority surface) and does not perturb the golden ``POST /start`` scored lifecycle.

        The endpoint genuinely OPERATES ON the owned competition — it is NOT authenticated label
        substitution. It FAILS CLOSED unless the competition's FROZEN roster declares EXACTLY the
        intrinsic arena contestants — the STRICT intrinsic-arena contract (Codex M2): the exact ids
        ``det-drift`` (``cumulative-drift``) + ``llm-drift`` (``llm``), cardinality 2, no extras, no
        id/strategy substitution. An empty, unrelated, mismatched-id, or extra-entry roster is refused
        409, so the started event's ``agent_ids`` are BY CONSTRUCTION the report's contestant ids — never
        a run that reports one identity while the event claims another. The roster is validated through
        the SAME machinery ``POST /competitions/{id}/start`` uses (:func:`build_agents_from_roster` —
        fail-closed on an unknown strategy / a missing-or-drifted deployed instance). Concurrent starts
        are claimed ATOMICALLY (Codex M1): the competition is compare-and-set DRAFT -> RUNNING and the
        run identity reserved BEFORE the model runs, so two simultaneous owner POSTs never both drive the
        comparison — exactly one runs and the loser returns 409 WITHOUT launching (no duplicate provider
        work, no unhandled duplicate-seq 500). The honest report is PERSISTED as a canonical
        :class:`~veridex.competition.events.CompetitionEvent` under a real ``run_id`` so it is OBSERVABLE
        from ``GET /competitions/{id}`` (state + event stream), not only this POST body.

        Auth (fail-closed, AC-27/AC-29 preserved): an anonymous caller is refused 401 (``require_principal``
        rejects a missing/invalid Privy bearer BEFORE any work), and only the Privy competition OWNER may
        run it (``_require_competition_owner`` → 403 for a non-owner or an unowned/legacy competition). An
        unknown competition is 404. A competition that has already run (RUNNING/FINALIZED) is 409 (the
        arena run seals canonical events + a run_id exactly once — mirror ``start_competition``).

        The comparison drives det-Drift AND LLM-Drift at the SAME pinned checkpoints from the SAME shared
        snapshot over the competition's configured (offline demo) tape, using the injected model launcher
        (``arena_model_launcher``; the lazy production Agno launcher by default). The payload is the HONEST
        :class:`~veridex.runtime.arena_comparison.ArenaComparisonReport` — eligible checkpoints,
        per-contestant actions-vs-WAITs, AUTHORITATIVE scoreable decisions, fixture count, clustered
        uncertainty, and the identical-opportunity flag — NEVER a bare average CLV (addendum §3).

        Args:
            competition_id: The owned competition to run the arena comparison for.
            principal: The authenticated Privy principal (injected by ``require_principal``; 401 if absent).
            dep_store: Injected store dependency.

        Returns:
            A JSON object ``{"competition_id", "run_id", "arena_comparison"}`` where ``arena_comparison``
            is the honest report payload (never ``None``) and ``run_id`` is the persisted run identifier.

        Raises:
            HTTPException: 404 (unknown competition), 401 (anonymous), 403 (non-owner / unowned),
                409 (already run / concurrent-start loser / roster does not declare EXACTLY the intrinsic
                arena contestants / rostered deployed instance missing), 400 (unknown strategy / drift).
        """
        try:
            competition = await dep_store.get_competition(competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None

        # Owner-scoping (fail-closed): only the Privy owner may run their competition's arena comparison
        # (a non-owner or an unowned/legacy competition is refused 403 — mirror the start/kill lifecycle).
        _require_competition_owner(competition, principal.did)

        # Idempotency (mirror start_competition): the arena comparison seals canonical events + a run_id
        # exactly ONCE. A competition that has already run is refused rather than double-appending seqs.
        if competition.status in (CompetitionStatus.RUNNING, CompetitionStatus.FINALIZED):
            raise HTTPException(status_code=409, detail="competition already run")

        # BIND the pinned MODEL/CONFIG identity, not just id -> strategy (Codex M2). id/strategy labels
        # ALONE are insufficient: the declared-entry ``config_hash`` folds ``model`` + ``proof_mode``, and
        # an instance-bound entry pins an entire deployed ``effective_config`` the intrinsic arena does not
        # run. The intrinsic arena runs det-Drift (no model) + LLM-Drift over the injected launcher whose
        # model identity is ``arena_model_id`` (its HONEST ``model_id``, ``None`` only for a launcher that
        # exposes none). ``expected_models`` pins each contestant's intrinsic model identity.
        arena_model_id = getattr(resolved_arena_model_launcher, "model_id", None)
        expected_models: dict[str, str | None] = {DET_DRIFT_CONTESTANT: None, LLM_DRIFT_CONTESTANT: arena_model_id}

        # FAIL CLOSED (pre-claim) unless the FROZEN roster snapshot declares EXACTLY the intrinsic arena
        # contestants at the pinned model/config identity — the STRICT intrinsic-arena contract (Codex M2).
        # Requiring EXACTLY them (exact ids -> strategies, cardinality 2, no extras, config_hash bound, no
        # instance binding) makes the started event's agent_ids BY CONSTRUCTION the report's contestant ids.
        # An empty, unrelated, mismatched-id (e.g. desk-rules-v7), extra-entry, wrong-model, or
        # instance-bound roster is refused 409, never a substitution. RE-RUN post-claim (below) closes the
        # pre-claim TOCTOU window — this snapshot may be stale by the time the claim freezes the roster.
        try:
            _verify_intrinsic_arena_roster(competition.entries, expected_models=expected_models)
        except _ArenaRosterContractError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None

        # VALIDATE the declared roster through the SAME machinery start_competition uses — fail-closed on
        # an unknown strategy / a missing-or-drifted deployed instance (never a same-named reconstruction,
        # never a silent substitution). The strict contract above already pinned the ids + strategies.
        try:
            await build_agents_from_roster(
                competition.entries, get_instance=dep_store.get_agent_instance
            )
        except KeyError as exc:
            raise HTTPException(status_code=409, detail=f"rostered deployed instance not found: {exc}") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        # ATOMICALLY CLAIM the competition BEFORE the model runs (Codex M1 + M3). ONE guarded write sets
        # status DRAFT -> RUNNING AND reserves the run_id together (no crash window between the two writes,
        # Codex M3). Two concurrent owner POSTs can never both pass the claim, so exactly ONE drives the
        # physical comparison and the LOSER returns a controlled 409 WITHOUT launching. The claim also
        # freezes the roster against concurrent mutation for the duration of the run (the register path's
        # atomic status-guarded admission refuses any append once status left the mutable window).
        run_id = uuid4().hex
        try:
            await dep_store.claim_competition_run(
                competition_id, expected=CompetitionStatus.DRAFT, new=CompetitionStatus.RUNNING, run_id=run_id
            )
        except CompetitionStatusClaimError:
            raise HTTPException(status_code=409, detail="competition already run") from None

        # POST-CLAIM ROSTER RE-VERIFY (Codex M1 pre-claim TOCTOU). The strict contract above ran against
        # the roster snapshot fetched BEFORE the claim; between that check and the claim a concurrent
        # ``register_agent`` can LEGALLY commit an extra entry (status still DRAFT) — so the claim could
        # freeze a roster (e.g. ``[det, llm, extra-x]``) DIFFERENT from the one the report attests
        # (``[det, llm]``). RE-FETCH the now-frozen roster and RE-RUN the SAME strict contract against it;
        # on ANY mismatch FAIL CLOSED — recover the claim (``_recover_arena_claim`` CAS-rolls RUNNING ->
        # DRAFT and clears the run_id since no events landed yet) and refuse 409, never a run over a mutated
        # roster. The post-claim result is the AUTHORITATIVE ``contestant_config``: it attests EXACTLY the
        # roster frozen at RUNNING. (The claim freezes the roster, so this is the last possible mutation.)
        try:
            claimed = await dep_store.get_competition(competition_id)
            contestant_config = _verify_intrinsic_arena_roster(
                claimed.entries, expected_models=expected_models
            )
        except _ArenaRosterContractError as exc:
            await _recover_arena_claim(dep_store, competition_id)
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
        except KeyError:
            await _recover_arena_claim(dep_store, competition_id)
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None

        # FAIL-CLOSED post-claim recovery (Codex M3): wrap model execution + report construction + event
        # persistence so ANY failure (run_arena_comparison, ArenaDrainError, report/append error) RECOVERS
        # instead of stranding the competition in RUNNING with a dead run_id, zero events, and a permanent
        # 409. On failure ``_recover_arena_claim`` CAS-rolls RUNNING -> DRAFT and clears the run_id (when no
        # events were committed) so the run is retryable, or finalizes as terminal (when events landed).
        try:
            # Derive the tape from the competition's configured (offline) source — the SAME source
            # start_competition drives the scored lifecycle over. Drive the checkpointed comparison HERE.
            ticks = build_demo_ticks()
            det_policy = CheckpointPolicy(cadence_s=1.0, evidence_age_limit_s=100_000.0)
            arena = await run_arena_comparison(
                ticks, model=resolved_arena_model_launcher, det_policy=det_policy
            )
            report = arena.report().to_payload()
            # REFLECT the ACTUAL verified model/config identity that ran (Codex M2) in the report payload.
            report["contestant_config"] = contestant_config

            # PERSIST the comparison through canonical competition state so it is OBSERVABLE from
            # GET /competitions/{id} (a real run_id) and the canonical event stream — not ONLY this POST
            # body. Mirror start_competition's finalize: emit the seq=0 COMPETITION_STARTED, append a
            # derived event carrying the honest report, then FINALIZE. The arena comparison is checkpoint-
            # based (not the scored orchestrator), so it produces no SCORE_UPDATE rows — the leaderboard
            # stays empty, never a fabricated ranking. The started event's agent_ids ARE the run's ACTUAL
            # contestant ids (the report's contestant identities) — BYTE-EQUAL, and (by the strict
            # contract) exactly the declared roster ids: no identity substitution (Codex M2).
            base_ts = int(ticks[0].ts) if ticks else 0
            agent_ids = list(report["contestants"].keys())
            started = build_competition_started_event(
                competition_id=competition_id,
                run_id=run_id,
                source_mode=competition.config.source_mode,
                agent_ids=agent_ids,
                base_ts=base_ts,
            )
            # Reflect the verified model/config identity in the started event too (Codex M2) — the started
            # event now declares WHICH model/config each contestant ran, not just the ids.
            started_payload = dict(started.payload)
            started_payload["contestant_config"] = contestant_config
            started = started.model_copy(
                update={"payload": started_payload, "payload_hash": event_payload_hash(started_payload)}
            )
            arena_payload: dict[str, Any] = {
                "competition_id": competition_id,
                "run_id": run_id,
                "agent_ids": agent_ids,
                "arena_comparison": report,
            }
            arena_event = CompetitionEvent(
                competition_id=competition_id,
                run_id=run_id,
                seq=1,
                event_type=EventType.COMPETITION_FINALIZED,
                event_ts=base_ts,
                evidence=False,
                source_sequence_no=None,
                derived_from=[f"arena_comparison:{run_id}"],
                payload=arena_payload,
                payload_hash=event_payload_hash(arena_payload),
            )
            await dep_store.append_competition_events(competition_id, [started, arena_event])
            # run_id was reserved+persisted at the atomic claim above; RUNNING -> FINALIZED seals the run.
            await dep_store.update_competition_status(competition_id, CompetitionStatus.FINALIZED)
        except Exception as exc:
            await _recover_arena_claim(dep_store, competition_id)
            raise HTTPException(
                status_code=503, detail="arena run failed; competition rolled back and retryable"
            ) from exc

        return {"competition_id": competition_id, "run_id": run_id, "arena_comparison": report}

    # --- GET /competitions/{competition_id} -------------------------------

    @app.get("/competitions/{competition_id}", response_model=CompetitionStateResponse)
    async def get_competition_state_endpoint(
        competition_id: str,
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> CompetitionStateResponse:
        """Return full competition state with a leaderboard derived from the canonical event log.

        Leaderboard derivation (single source of truth — CON-203): reads ``SCORE_UPDATE``
        events from the persisted log, ranks them by ``mean_clv_bps`` descending (``None``
        treated as ``-inf``), then ``agent_id`` ascending as a stable tie-breaker.

        ``anchor_status`` comes from the ``PROOF_ANCHOR`` event payload; defaults to
        ``"not_anchored"`` when the event is absent (e.g. competition not yet started).

        Args:
            competition_id: The competition to inspect.
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.CompetitionStateResponse`.

        Raises:
            HTTPException: 404 when ``competition_id`` is not found.
        """
        try:
            competition = await dep_store.get_competition(competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None

        # Read entire log (since_seq=-1 includes seq=0).
        events = await dep_store.list_competition_events(competition_id, since_seq=-1)

        leaderboard = _derive_leaderboard(events)
        latest_seq = max((e.seq for e in events), default=0)

        anchor_event = next((e for e in events if e.event_type == EventType.PROOF_ANCHOR), None)
        anchor_status = anchor_event.payload.get("anchor_status", "not_anchored") if anchor_event else "not_anchored"

        # Proof-card SKILL/scoring block (REQ-2B-20): byte-identical with or without fills, because
        # it is built purely from the SEALED run. Execution receipts live ONLY under the separate
        # ``execution`` attachment below (non-scoring, derived, off-chain) — never in this block.
        proof_card: dict[str, Any] | None = None
        if competition.run_id is not None:
            try:
                run_result = await dep_store.load_run(competition.run_id)
            except KeyError:
                run_result = None
            if run_result is not None:
                scores = score_run(run_result)
                checks = read_path_check_block(scores, run_result)
                metrics = build_performance_metrics(scores)  # SEC-001: CLV lives here, not in checks
                anchor_block: dict[str, Any] = {
                    "status": str(anchor_status),
                    "signature": None,
                    "cluster": DEFAULT_CLUSTER,
                }
                proof_card = proof_card_from_run_result(
                    run_result,
                    checks=checks,
                    anchor=anchor_block,
                    schema_versions=dict(SCHEMA_VERSIONS),
                    metrics=metrics,
                )

        execution_records = await dep_store.list_executions(competition_id)
        execution = _build_execution_attachment(execution_records)

        return CompetitionStateResponse(
            competition_id=competition.competition_id,
            status=competition.status.value,
            config=competition.config.model_dump(mode="json"),
            roster=[e.model_dump(mode="json") for e in competition.entries],
            leaderboard=leaderboard,
            latest_seq=latest_seq,
            anchor_status=str(anchor_status),
            run_id=competition.run_id,
            proof_card=proof_card,
            execution=execution,
            replay_binding=(
                competition.replay_binding.model_dump(mode="json")
                if competition.replay_binding is not None
                else None
            ),
        )

    # --- GET /competitions ------------------------------------------------

    @app.get("/competitions", response_model=list[CompetitionSummaryResponse])
    async def list_competitions_endpoint(
        status: CompetitionStatus | None = Query(default=None),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> list[CompetitionSummaryResponse]:
        """List all competitions with an optional status filter.

        Args:
            status: When provided, only competitions with this lifecycle status are returned.
                FastAPI validates the value against :class:`~veridex.competition.models.CompetitionStatus`
                and returns 422 for unknown values.
            dep_store: Injected store dependency.

        Returns:
            A list of :class:`~veridex.api.schemas.CompetitionSummaryResponse` items.
        """
        competitions = await dep_store.list_competitions(status=status)
        return [
            CompetitionSummaryResponse(
                competition_id=c.competition_id,
                status=c.status.value,
                config=c.config.model_dump(mode="json"),
                run_id=c.run_id,
            )
            for c in competitions
        ]

    # --- GET /competitions/{competition_id}/events ------------------------

    @app.get("/competitions/{competition_id}/events")
    async def get_competition_events_endpoint(
        competition_id: str,
        since_seq: int = Query(default=0),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> list[dict[str, Any]]:
        """Return the ordered event log tail (``seq > since_seq``).

        Mirrors :func:`~veridex.competition.events.replay_from` semantics: strict-greater
        bound, ascending ``seq`` order.  The default ``since_seq=0`` returns all events with
        ``seq >= 1`` (excluding the ``COMPETITION_STARTED`` event at ``seq=0``).

        Args:
            competition_id: The owning competition.
            since_seq: Exclusive lower bound on ``seq`` (default ``0`` → seq ≥ 1).
            dep_store: Injected store dependency.

        Returns:
            JSON-serialized :class:`~veridex.competition.events.CompetitionEvent` list,
            ordered ascending by ``seq``.

        Raises:
            HTTPException: 404 when ``competition_id`` is not found.
        """
        try:
            await dep_store.get_competition(competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None

        events = await dep_store.list_competition_events(competition_id, since_seq=since_seq)
        return [e.model_dump(mode="json") for e in events]

    # --- GET /competitions/{competition_id}/executions --------------------

    @app.get("/competitions/{competition_id}/executions", response_model=list[ExecutionRecord])
    async def list_executions_endpoint(
        competition_id: str,
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> list[ExecutionRecord]:
        """Return all execution records for a competition (read-only, public).

        Args:
            competition_id: The owning competition.
            dep_store: Injected store dependency.

        Returns:
            The competition's :class:`~veridex.execution.models.ExecutionRecord` list, sorted by
            ``execution_id``.
        """
        return await dep_store.list_executions(competition_id)

    # --- GET /executions/{execution_id} -----------------------------------

    @app.get("/executions/{execution_id}", response_model=ExecutionRecord)
    async def get_execution_endpoint(
        execution_id: str,
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> ExecutionRecord:
        """Return a single execution record (with its receipt, if any) — read-only, public.

        Args:
            execution_id: The execution record id.
            dep_store: Injected store dependency.

        Returns:
            The :class:`~veridex.execution.models.ExecutionRecord`.

        Raises:
            HTTPException: 404 when no record with ``execution_id`` exists.
        """
        try:
            return await dep_store.get_execution_record(execution_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"execution {execution_id!r} not found") from None

    # --- GET /agents/instances/{instance_id}/runtime-events (OPS channel; OWNER-SCOPED) ----

    @app.get("/agents/instances/{instance_id}/runtime-events", response_model=RuntimeEventsResponse)
    async def get_instance_runtime_events(  # noqa: B008
        instance_id: str,
        since: int = Query(default=0),  # noqa: B008
        limit: int | None = Query(default=None),  # noqa: B008
        principal: PrivyPrincipal = Depends(require_principal),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> RuntimeEventsResponse:
        """Serve the caller's OWN durable OPS-channel RuntimeEvents for a deployed instance (I-4).

        REPLACES the former PUBLIC ``/agents/{id}/runtime-events`` — a trust boundary: ownership is
        resolved SERVER-SIDE from the persisted instance (never the request), and the durable feed is
        read through the store's ``BIGSERIAL`` cursor. Fail-closed exactly like the sibling
        ``GET /agents/instances/{id}`` (I-2): 404 for absent OR unowned/legacy (no existence leak),
        403 for owned-by-another. Channel-pure (SEC-003): the rows carry no evidence fields.

        Args:
            instance_id: The deployed instance whose OPS telemetry to read.
            since: The last consumed durable ``id`` (exclusive cursor); ``0`` returns from the start.
            limit: When set, the FIRST ``limit`` events after ``since`` (forward paging).
            principal: The authenticated Privy principal (injected by ``require_principal``).
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.RuntimeEventsResponse` wrapping the ``events`` list (each a
            RuntimeEvent dict plus its durable ``id`` cursor).

        Raises:
            HTTPException: 401 (unauthenticated), 404 (absent or unowned — no leak), 403 (wrong owner).
        """
        try:
            instance = await dep_store.get_agent_instance(instance_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="agent instance not found") from None
        # Fail-closed: an unowned / legacy row is never inherited — hide it as if it does not exist.
        if instance.operator_id is None:
            raise HTTPException(status_code=404, detail="agent instance not found")
        if instance.operator_id != principal.did:
            raise HTTPException(status_code=403, detail="principal does not own this agent instance")
        # RUN-scoped, not agent_id-scoped: agent_id is a reusable template constant
        # (studio-{template}) shared across deploys/owners, so reading by it would leak another
        # run's/owner's OPS events. run_id is the server-minted per-instance authority (derived here
        # from the OWNED instance, never the client) and also spans the run's lifecycle events emitted
        # under a different veridex-mm-{instance} agent_id — so no lifecycle evidence is truncated.
        rows = await dep_store.list_runtime_events_for_run(instance.run_id, since=since, limit=limit)
        events: list[dict[str, Any]] = []
        for row in rows:
            # Reconstruct through RuntimeEvent so the served shape is channel-pure by construction
            # (RuntimeEvent has NO sequence_no / payload_hash), then attach the durable read cursor.
            payload = RuntimeEvent(
                type=RuntimeEventType(row.event_type),
                agent_id=row.agent_id,
                run_id=row.run_id,
                session_id=row.session_id,
                ts=row.ts,
                payload=row.payload,
            ).model_dump(mode="json")
            payload["id"] = row.id
            events.append(payload)
        return RuntimeEventsResponse(events=events)

    # --- POST /competitions/{competition_id}/kill-switch (auth) -----------

    @app.post("/competitions/{competition_id}/kill-switch", response_model=KillSwitchResponse)
    async def kill_switch_endpoint(
        competition_id: str,
        principal: str | None = Depends(_require_operator),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> KillSwitchResponse:
        """ENGAGE the competition's policy-envelope kill-switch (control-plane write; fail-closed).

        Engage-ONLY and idempotent (SAF-004): this SETS the kill-switch True — it never toggles.
        The first engage stops trading; every subsequent engage (e.g. a client retry after an
        uncertain control-plane ACK) is a no-op that KEEPS the stop engaged and re-enables nothing.
        Clearing the stop is impossible here — it requires the SEPARATE, reconciled
        ``POST /competitions/{id}/re-arm`` operation. (A toggle would let a duplicate call flip the
        stop OFF and re-open trading — precisely the bug this endpoint forbids.)

        Args:
            competition_id: The competition to update.
            principal: The authenticated operator principal (injected by ``_require_operator``).
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.KillSwitchResponse` with ``kill_switch`` always True.

        Raises:
            HTTPException: 401 (unauthenticated), 403 (wrong owner), 404 (unknown competition).
        """
        try:
            competition = await dep_store.get_competition(competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None
        _check_owner(competition, principal)

        envelope = competition.config.policy_envelope or _default_policy_envelope()
        # ENGAGE-ONLY: explicitly SET True (never `not envelope.kill_switch`). A repeat engage on an
        # already-engaged switch is an idempotent no-op that keeps the stop on — it cannot re-open.
        engaged = envelope.model_copy(update={"kill_switch": True})
        new_config = competition.config.model_copy(update={"policy_envelope": engaged})
        await dep_store.update_competition_config(competition_id, new_config)

        return KillSwitchResponse(
            competition_id=competition_id,
            kill_switch=True,
            status="kill_switch_on",
        )

    # --- POST /competitions/{competition_id}/re-arm (auth) ----------------

    @app.post("/competitions/{competition_id}/re-arm", response_model=KillSwitchResponse)
    async def re_arm_endpoint(
        competition_id: str,
        body: dict[str, Any] | None = None,
        principal: str | None = Depends(_require_operator),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> KillSwitchResponse:
        """Re-arm (clear the kill-switch) — a SEPARATE, fail-closed control-plane op (SAF-004/002c).

        Deliberately NOT the kill-switch endpoint: engaging the stop can never re-arm it, and
        re-arming is the dangerous direction, so it is gated by three preconditions that must ALL
        be positively satisfied before trading may resume:

        1. **Explicit operator authorization** — the request body MUST carry ``{"authorize": true}``
           (a valid operator token alone is not enough; re-arm is a deliberate act).
        2. **Open-order reconciliation** — the venue must be confirmed to hold ZERO resting orders.
        3. **Risk-state reload (SAF-002c)** — the realized-loss caps must be rebuilt from the
           durable ledger via ``reconstruct_risk`` before the stop clears.

        Full reconciliation + risk-reload wiring for a live R4-A session lands in E4. Until then a
        control-plane competition has NO attached live-execution runtime to reconcile against, so
        preconditions (2)/(3) cannot be positively satisfied and this op FAILS CLOSED with 409 —
        the kill-switch is left ENGAGED. This endpoint therefore NEVER re-opens trading in this
        build; it exists to prove re-arm is structurally separate and fail-closed.

        Args:
            competition_id: The competition to re-arm.
            body: JSON body; must include ``{"authorize": true}`` for explicit operator auth.
            principal: The authenticated operator principal (injected by ``_require_operator``).
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.KillSwitchResponse` (only on a future successful re-arm).

        Raises:
            HTTPException: 401 (unauthenticated), 403 (wrong owner / not authorized),
                404 (unknown competition), 409 (fail-closed: reconciliation/risk-reload unmet).
        """
        try:
            competition = await dep_store.get_competition(competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None
        _check_owner(competition, principal)

        # Precondition 1: explicit operator authorization (beyond mere authentication).
        if (body or {}).get("authorize") is not True:
            raise HTTPException(
                status_code=403,
                detail="re-arm requires explicit operator authorization ({'authorize': true})",
            )

        # Preconditions 2 & 3 (open-order reconciliation + SAF-002c risk-state reload) require a
        # live R4-A execution runtime attached to the competition. That wiring lands in E4; absent
        # it, re-arm cannot positively confirm zero resting orders or reload the loss caps, so it
        # FAILS CLOSED and leaves the kill-switch engaged (never re-opens trading).
        raise HTTPException(
            status_code=409,
            detail=(
                "re-arm fail-closed: open-order reconciliation and risk-state reload (SAF-002c) "
                "cannot be satisfied without a live execution runtime; kill-switch stays engaged"
            ),
        )

    # --- POST /executions/{execution_id}/approve (auth) -------------------

    @app.post("/executions/{execution_id}/approve", response_model=ApprovalResponse)
    async def approve_execution_endpoint(
        execution_id: str,
        body: dict[str, Any] | None = None,
        principal: str | None = Depends(_require_operator),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> ApprovalResponse:
        """Resolve an ``awaiting_human`` execution: re-check law+policy+eligibility, then submit-or-reject.

        Control-plane write (fail-closed): requires a valid operator token (401) and competition
        ownership (403). The resolution emits a NON-SCORING approval audit event, independently
        re-derives the proposal from the SEALED run, re-evaluates the CURRENT envelope (kill-switch
        included) + eligibility, and either advances to submission or rejects (no submit).

        Args:
            execution_id: The ``awaiting_human`` execution record to resolve.
            body: Optional JSON body with an operator ``note``.
            principal: The authenticated operator principal.
            dep_store: Injected store dependency.

        Returns:
            An :class:`~veridex.api.schemas.ApprovalResponse` with the decision + resulting status.

        Raises:
            HTTPException: 401/403 (auth/owner), 404 (unknown execution/run), 409 (not awaiting).
        """
        try:
            record = await dep_store.get_execution_record(execution_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"execution {execution_id!r} not found") from None

        try:
            competition = await dep_store.get_competition(record.competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {record.competition_id!r} not found") from None
        _check_owner(competition, principal)

        if record.status != ExecutionStatus.AWAITING_HUMAN:
            raise HTTPException(
                status_code=409,
                detail=f"execution {execution_id!r} is not awaiting_human (status={record.status.value})",
            )

        try:
            run_result = await dep_store.load_run(record.run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"run {record.run_id!r} not found") from None

        envelope = competition.config.policy_envelope or _default_policy_envelope()
        execution_mode = competition.config.execution_mode
        adapter = FakeVenueAdapter() if execution_mode == ExecutionMode.DRY_RUN else SXBetAdapter()
        entry = next((e for e in competition.entries if e.agent_id == record.agent_id), None)
        note = (body or {}).get("note")

        # Append the resolution block AFTER the current log tail (contiguous seqs).
        existing = await dep_store.list_competition_events(record.competition_id, since_seq=-1)
        next_seq = max((e.seq for e in existing), default=-1) + 1

        updated, events, decision = await resolve_approval(
            dep_store,
            record=record,
            run_result=run_result,
            envelope=envelope,
            adapter=adapter,
            entry=entry,
            execution_mode=execution_mode.value,
            base_seq=next_seq,
            event_ts=0,
            approver_id=principal,
            note=note,
        )
        await dep_store.append_competition_events(record.competition_id, events)
        for event in events:
            await arena_manager.broadcast(record.competition_id, event)

        return ApprovalResponse(execution_id=execution_id, decision=decision, status=updated.status.value)

    # --- WS /competitions/{competition_id}/arena --------------------------
    # Read-only spectator projection (P2A-7). The per-app ``arena_manager`` (created above) owns
    # per-client bounded broadcast queues; the route closes over ``resolved_store`` for replay.
    # The Phase-2B live producer (start_competition / approve) PERSISTS each event BEFORE calling
    # ``arena_manager.broadcast`` for it (persist-before-broadcast), so spectators see a gapless
    # projection of the sealed log. ``broadcast`` never blocks the run loop (REQ-2B-30).
    register_arena_routes(app, store=resolved_store, manager=arena_manager)

    # --- POST /agents/deploy (Studio deploy → pinned instance → async run → one flow to proof) ----
    # Preflight fail-closed (422 named); pins config_hash+policy_hash+template+modes as an
    # AgentInstance; launches via the SINGLE runner seam (standalone_run) and persists the sealed run
    # to ``resolved_store`` so the deployed run verifies via the SAME /runs/{id}/verify path (T21).
    #
    # Default deps (T21c): so the headline replay/paper deploy runs end-to-end from the REAL app
    # (no injected deps), the default route degrades anchoring HONESTLY — it anchors on-chain only
    # when a Solana keypair is configured, else the run is legitimately ``not_anchored`` (offline
    # replay), NEVER a fabricated anchor and NEVER a crash on a missing keypair. The replay SOURCE
    # is the SELECTED verified ReplayPack (R-4), resolved from the R-2 catalog and frozen onto the
    # deployed instance, loaded through ``load_pack_marketstates``, wired inside ``register_deploy_routes``.
    resolved_deploy_deps = deploy_deps
    if resolved_deploy_deps is None:
        anchor_fn = anchor_memo if resolved_settings.solana_keypair_path is not None else None
        resolved_deploy_deps = DeployDeps(anchor_fn=anchor_fn)
    register_deploy_routes(
        app,
        store=resolved_store,
        settings=resolved_settings,
        deploy_deps=resolved_deploy_deps,
        runtime_event_sink=resolved_runtime_events.sink(),
    )

    # --- GET /maker/arena-result (read-only maker-UI bridge; SEC-005 isolated lane) ----
    # Separate namespace over the SEALED maker arena artifact — never re-runs the arena, never
    # imports the directional scorer/leaderboard. Registered last so it composes like the deploy
    # and arena route groups above.
    register_maker_routes(app, store=resolved_store, require_principal=require_principal)

    # --- Signal Trials free reads + honest commit stub (separate payments lane) ----------
    # Reads the published-season repository out of SIGNAL_TRIALS_DATA_DIR (the persistent
    # volume the offline scorer publishes into), mirroring how REPLAY_PACK_ROOT is resolved
    # above: the environment is read HERE, at the composition root, so the router itself
    # stays injectable. A blank/unset value means no repository is configured, which reads
    # as an honest ``not_built`` rather than an error.
    register_signal_trials_routes(app, data_dir=os.environ.get("SIGNAL_TRIALS_DATA_DIR", "") or None)

    return app
