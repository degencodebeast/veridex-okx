"""API surface contract freeze — fixtures + live responses validate against the pinned models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from veridex.api.demo_fixtures import build_demo_ticks, contrarian_agent
from veridex.api.router import create_app
from veridex.api.schemas import (
    CompetitionStateResponse,
    FeedHealthResponse,
    InspectorRecord,
    LeaderboardResponse,
    ProofArtifactResponse,
    RuntimeEventsResponse,
    VerifyResponse,
)
from veridex.api.signal_trials_schemas import (
    OpenTrialResponse,
    SignalTrialsRowModel,
    SignalTrialsSeasonResponse,
)
from veridex.store import InMemoryStore

# The H4.3 half of the frozen Signal Trials family lands on this branch with the payments lane
# merge. Importing it unconditionally would break the WHOLE contract suite on a tree that simply
# has not merged yet, so it is probed — and the test that needs it SKIPS WITH A NAMED REASON
# rather than silently passing. The skip converts itself into a hard assertion the moment the
# models arrive; see test_signal_trials_trial_fixture_validates_against_trial_response.
try:
    from veridex.api.signal_trials_schemas import TrialResponse as _TrialResponse
except ImportError:  # pragma: no cover - exercised only before the payments lane merge
    _TrialResponse = None

_FIXTURES = Path("contracts/fixtures")

# filename -> the response model it must validate against (drift in either side fails the freeze).
_REGISTRY = {
    "leaderboard.json": LeaderboardResponse,
    "competition_state.json": CompetitionStateResponse,
    "proof_artifact.json": ProofArtifactResponse,
    "verify_response.json": VerifyResponse,
    "inspector_record.json": InspectorRecord,
    "feed_health.json": FeedHealthResponse,
    "runtime_events.json": RuntimeEventsResponse,
    "signal_trials_season.json": SignalTrialsSeasonResponse,
}


def test_every_committed_fixture_validates_against_its_model() -> None:
    for name, model in _REGISTRY.items():
        data = json.loads((_FIXTURES / name).read_text())
        model.model_validate(data)  # raises on contract drift


def test_verify_endpoint_matches_verify_response_and_carries_proof_artifact() -> None:
    client = TestClient(create_app(store=InMemoryStore()))
    run_id = client.post("/demo/run").json()["run_id"]

    verify = client.post(f"/runs/{run_id}/verify")
    assert verify.status_code == 200
    body = verify.json()
    VerifyResponse.model_validate(body)  # live response conforms to the pinned model

    # WD-1: the recompute confirms the sealed hash, and the embedded ProofArtifact carries the
    # exact fields C1's VerifyResult/ProofArtifact bind to.
    assert body["verified"] is True
    assert body["recomputed_evidence_hash"] == body["evidence_hash"]
    ProofArtifactResponse.model_validate(body["proof_card"])
    assert {"verifier_version", "run", "lineage", "evidence", "checks", "anchor"} <= set(body["proof_card"])


def test_verify_unknown_run_is_404() -> None:
    client = TestClient(create_app(store=InMemoryStore()))
    assert client.post("/runs/nope/verify").status_code == 404


def test_proof_artifact_route_is_get_runs() -> None:
    """Pinned decision: the ProofArtifact source is GET /runs/{id} (no /api/proof route)."""
    client = TestClient(create_app(store=InMemoryStore()))
    run_id = client.post("/demo/run").json()["run_id"]
    pc = client.get(f"/runs/{run_id}")
    assert pc.status_code == 200
    ProofArtifactResponse.model_validate(pc.json())
    assert client.get(f"/api/proof/{run_id}").status_code == 404  # the alternate route is NOT added


# The 7 frozen Proof-Check ids (spec §4.3 / SEC-001). CLV is NOT one of them — it lives in metrics.
_SEVEN_CHECK_IDS = {
    "evidence_integrity",
    "llm_boundary",
    "metrics_recomputed",
    "manifest_bound",
    "policy_obeyed",
    "receipt_separation",
    "anchor",
}


def test_live_checks_block_is_sec001_compliant() -> None:
    """SEC-001 target: the live ``checks`` block holds ONLY the 7 CheckId; CLV lives in ``metrics``.

    This is the FINAL shape the frozen contract (``contracts/veridex_api.contract.ts`` + the
    ``verify_response``/``proof_artifact`` fixtures) pins. Task 5 (WD-5b) migrated the live backend
    so both ``POST /runs/{id}/verify`` and ``GET /runs/{id}`` now emit the 7-CheckId block with CLV
    relocated to ``metrics`` — so this assertion now genuinely passes (the prior strict-xfail guard
    is removed).
    """
    client = TestClient(create_app(store=InMemoryStore()))
    run_id = client.post("/demo/run").json()["run_id"]

    verify_checks = client.post(f"/runs/{run_id}/verify").json()["checks"]
    assert "clv" not in verify_checks  # SEC-001: CLV must never appear in the checks block
    assert set(verify_checks) >= _SEVEN_CHECK_IDS

    proof_checks = client.get(f"/runs/{run_id}").json()["checks"]
    assert "clv" not in proof_checks
    assert set(proof_checks) >= _SEVEN_CHECK_IDS


def test_verify_manifest_bound_passes_on_honest_run() -> None:
    """BLOCKER: verify must CONFIRM the manifest hash on an honest run.

    The endpoint reconstructs the manifest + manifest_hash; passing them to ``build_check_results``
    makes MANIFEST_BOUND ``pass``. The prior 2-arg ``read_path_check_block`` omitted them → ``not_applicable``
    → the Proof Card false-red "manifest hash mismatch" on a perfectly honest run. Regression guard.
    """
    client = TestClient(create_app(store=InMemoryStore()))
    run_id = client.post("/demo/run").json()["run_id"]

    body = client.post(f"/runs/{run_id}/verify").json()
    assert body["verified"] is True  # honest run still verifies
    assert body["checks"]["manifest_bound"]["result"] == "pass"  # not "not_applicable"


async def test_verify_route_manifest_hash_matches_seal_time() -> None:
    """Carry #3 (DRY): the verify route rebuilds the manifest with the seal-time helpers.

    The ``POST /runs/{id}/verify`` handler derives ``manifest_hash`` via ``_score_root`` +
    ``_fixture_or_window_id`` (the authoritative ``competition.py`` helpers) over the persisted run,
    so it MUST equal the ``manifest_hash`` computed when the run was originally sealed.
    """
    from veridex.runtime.competition import run_demo_competition
    from veridex.runtime.orchestrator import deterministic_agent

    store = InMemoryStore()
    sealed = await run_demo_competition(
        build_demo_ticks(),
        [deterministic_agent("agent-alpha"), contrarian_agent("agent-beta")],
        source_mode="replay",
        store=store,
        anchor_fn=None,
    )

    client = TestClient(create_app(store=store))
    verify = client.post(f"/runs/{sealed.run.run_id}/verify")
    assert verify.status_code == 200
    assert verify.json()["manifest_hash"] == sealed.manifest_hash


# ---------------------------------------------------------------------------
# SIGNAL TRIALS (H5.1) — the contract seam between the frozen models and the
# TypeScript wire family in apps/web/lib/wire.ts.
# ---------------------------------------------------------------------------


def test_signal_trials_models_carry_no_defaults() -> None:
    """SCHEMA_FREEZE's load-bearing property: ZERO of the frozen fields carry a default.

    This is why the TypeScript mirror types every nullable field ``| null`` and never optional
    (``?``) — a field that may be absent and a field that is present-and-null are different
    contracts, and only the second one is what the backend serves. Asserted by runtime
    ``model_fields`` introspection, the same method that produced the freeze packet, because two
    prior regex passes over this source each produced a different wrong answer and one silently
    dropped ``t0_ms``.
    """
    for model in (SignalTrialsRowModel, SignalTrialsSeasonResponse, OpenTrialResponse):
        for name, field in model.model_fields.items():
            assert field.is_required(), f"{model.__name__}.{name} acquired a default; the freeze says zero do"


def test_signal_trials_season_fixture_preserves_null_and_zero_as_DIFFERENT_values() -> None:
    """The fixture must carry both a null metric AND a real zero metric, and keep them apart.

    A zero markout is a real FLAT outcome and a zero Brier is a PERFECT score, so a fixture
    carrying only nulls could not catch a consumer that renders ``0`` for ``null`` — the failure
    this whole freeze exists to prevent. Both controls are asserted here for the same reason the
    TypeScript adapter tests assert both.
    """
    data = json.loads((_FIXTURES / "signal_trials_season.json").read_text())
    season = SignalTrialsSeasonResponse.model_validate(data)
    rows = {row.agent_id: row for row in season.rows}

    unsettled = rows["agent-unsettled"]
    assert unsettled.avg_brier is None
    assert unsettled.capped_avg_markout_bps is None

    flat = rows["agent-flat"]
    assert flat.avg_brier == 0.0
    assert flat.capped_avg_markout_bps == 0
    assert flat.avg_brier is not None  # a real zero is NOT an absent value
    assert flat.capped_avg_markout_bps is not None

    # ...and a control row is present, so a consumer's control flag has something to bind to.
    assert any(row.is_control for row in season.rows)
    assert any(not row.is_control for row in season.rows)


@pytest.mark.skipif(
    _TrialResponse is None,
    reason=(
        "TrialResponse lands with the payments lane (H4.3) merge; it is absent at this branch base. "
        "contracts/fixtures/signal_trials_trial.json is committed and this assertion activates "
        "automatically once veridex/api/signal_trials_schemas.py carries the H4.3 models."
    ),
)
def test_signal_trials_trial_fixture_validates_against_trial_response() -> None:
    """The trial fixture is the TrialWire contract seam: seven fields, outcome nullable."""
    data = json.loads((_FIXTURES / "signal_trials_trial.json").read_text())
    trial = _TrialResponse.model_validate(data)

    # SEVEN fields and no participants list — `ParticipantSettlement` does not exist at the frozen
    # head, and the design handoff's three mentions of it are a design note, not a field.
    assert set(_TrialResponse.model_fields) == {
        "trial_id", "trial_mode", "t0_ms", "commit_deadline_ms",
        "evidence", "evidence_hash", "outcome",
    }
    assert trial.outcome is not None
    assert trial.outcome.status == "settled"
    # `entry` is the one NON-nullable outcome field; the rest are populated only once settled.
    assert trial.outcome.entry is not None
    assert trial.outcome.observation_lag_ms is not None

    # A null outcome is a WEAKER statement than a recorded `pending` and must validate as None.
    absent = _TrialResponse.model_validate({**data, "outcome": None})
    assert absent.outcome is None
