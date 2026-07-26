"""Signal-trials API lane — free reads, open-trial discovery, and the honest commit stub.

Registered onto the shared app via :func:`register_signal_trials_routes`, mirroring the
``register_maker_routes`` composition pattern already used by the FastAPI factory.

Everything this module serves at H1.2 is either read from the published-season
repository or an honest "nothing here yet". Nothing is fabricated, and in particular:

* **The commit route never succeeds.** Until the payment wrapper and the two-phase
  commit store land at H4.1 there is nothing to commit to, so both gated methods return
  ``503 trials_not_open``. A free ``200`` here would be a record of a benchmark
  commitment that was never paid for and never scored.
* **Absence is 404 with a named reason**, never an empty success. Each route uses its
  own error code, so a caller can tell "no season has been published" apart from "no
  trial is open" apart from a route that was never mounted at all (which would yield
  FastAPI's ``{"detail": "Not Found"}``).
* **The repository is read per request, not snapshotted at startup.** The season is
  built by an offline scorer into the same directory a running API serves from, so a
  start-up snapshot would keep serving ``404`` after a season was published, until
  someone restarted the process.

``response_model=None`` appears on the routes that can answer either way: their return
annotation is a union with :class:`~starlette.responses.JSONResponse`, which FastAPI
cannot turn into a response field. Validation is not lost — the models are constructed
explicitly, so a malformed artifact raises here rather than being served.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from veridex.api.signal_trials_schemas import (
    CommitRequest,
    OpenTrialResponse,
    SignalTrialsSeasonResponse,
)
from veridex.signal_trials.published import read_season, read_state
from veridex.signal_trials.receipts import verify_receipt

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from veridex.signal_trials.live import LiveTrial, LiveTrialRepository
    from veridex.signal_trials.receipts import ReceiptStore

#: Every route this lane owns lives under this prefix.
SIGNAL_TRIALS_PREFIX = "/signal-trials"


def _error(status_code: int, code: str) -> JSONResponse:
    """Build the lane's error envelope: a stable machine-readable ``error`` code.

    Deliberately not ``HTTPException``, whose envelope is ``{"detail": ...}``. The
    frozen contract is ``{"error": "<code>"}``, and agents match on that code.
    """
    return JSONResponse(status_code=status_code, content={"error": code})


def _open_trial_response(trial: LiveTrial) -> OpenTrialResponse:
    """Render a trial as frozen §11 discovery: evidence, its hash, and the deadline.

    ``evidence`` comes from ``visible_at_decision`` and ``evidence_hash`` binds exactly that
    payload, so the two cannot describe different snapshots. Nothing derived after the horizon is
    reachable through either — the leakage boundary is enforced in the canonicalizer, and this
    function does not widen it by assembling its own view of the signal.
    """
    return OpenTrialResponse(
        trial_id=trial.trial_id,
        trial_mode="live",
        t0_ms=trial.t0_ms,
        commit_deadline_ms=trial.commit_deadline_ms,
        evidence=trial.evidence,
        evidence_hash=trial.evidence_hash,
    )


def register_signal_trials_routes(
    app: FastAPI,
    *,
    data_dir: Path | str | None = None,
    open_trial_provider: Callable[[], OpenTrialResponse | None] | None = None,
    live_trials: LiveTrialRepository | None = None,
    store: ReceiptStore | None = None,
) -> None:
    """Register the signal-trials routes onto ``app``.

    Args:
        app: The FastAPI application to mount the routes on.
        data_dir: The signal-trials data directory holding the published-season
            repository. ``None`` means none is configured, which reads as an honest
            ``not_built`` rather than an error — a fresh deployment with no volume
            attached still answers ``/health``.
        open_trial_provider: Returns the currently open live trial, or ``None`` when
            none is open. Retained from H1.2 as the injectable seam; ``live_trials``
            takes precedence when both are supplied.
        live_trials: The published open-trial repository. ``None`` means this deployment
            has no live-trial store, which reads as ``trials_not_open`` — an honest
            statement, and the only state in which the commit route may refuse without
            naming a misconfiguration.
        store: The two-phase commit store, needed for the commit route to be servable at
            all. Present so this function can tell "no trials here" apart from "trials
            but no payment gate", which are fixed differently.

    **Dependencies are resolved PER REQUEST, preferring the composition root's.** These routes
    are registered from inside the AgentOS composition, which runs BEFORE the composition root
    can build the durable Signal Trials dependencies — so a value captured at registration time
    would permanently be the one that was available too early. Each handler therefore consults
    ``app.state`` first and falls back to whatever was injected here, which is what lets ONE
    store and ONE repository serve both the free reads and the payment wrapper without
    threading constructor arguments through ``build_agentos_app``. Injected values remain
    authoritative for any caller that supplies them and sets no state, so every existing test
    composition is unaffected.
    """

    def _live_trials() -> LiveTrialRepository | None:
        """The live-trial repository the composition root built, else the injected one."""
        return getattr(app.state, "signal_trials_live", None) or live_trials

    def _store() -> ReceiptStore | None:
        """The commit store the composition root built, else the injected one."""
        return getattr(app.state, "signal_trials_store", None) or store

    def _data_dir() -> Path | str | None:
        """The published-season root, preferring the one the composition root resolved.

        The composition root resolves ``SIGNAL_TRIALS_DATA_DIR`` from the same mapping it
        resolves everything else from. Preferring it here keeps one variable to one source: a
        second independent read is how the season repository and the commit store could come to
        disagree about which directory this deployment actually uses.
        """
        return getattr(app.state, "signal_trials_data_dir", None) or data_dir

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/health")
    async def signal_trials_health() -> dict[str, Any]:
        """Liveness plus the one fact a caller needs before reading anything else.

        ``season_state`` distinguishes an intentional ``no_season`` — the preflight ran
        and declined to build a season — from ``not_built``, where it never ran. Both
        answer ``404`` on ``/season``, so health is the only place they differ.
        """
        return {"ok": True, "season_state": read_state(_data_dir())["state"]}

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/season", response_model=None)
    async def signal_trials_season() -> SignalTrialsSeasonResponse | JSONResponse:
        """Serve the published season, or 404 when none has been published.

        "None has been published" is decided by the published STATE, not by whether a
        payload file happens to exist. Under ``not_built`` or ``no_season`` this route
        answers 404 even if an older ``season.json`` is still on disk — otherwise a
        declined preflight would leave ``/health`` reporting ``no_season`` while this
        route served a stale ``qualified`` season, and the API would be asserting both
        at once. See :func:`~veridex.signal_trials.published.read_season`.
        """
        season = read_season(_data_dir())
        if season is None:
            return _error(404, "no_season_published")
        return SignalTrialsSeasonResponse(**season)

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/open-trial", response_model=None)
    async def signal_trials_open_trial() -> OpenTrialResponse | JSONResponse:
        """Frozen §11 discovery: the open live trial's no-future evidence, hash and deadline.

        The repository is consulted first and the callable second, so a deployment that has a
        real published trial serves it while a test supplying a provider still works. Both are
        read per request: a live trial is opened by a separate process, so a value captured at
        registration would keep reporting ``no_open_trial`` after one was published.
        """
        repo = _live_trials()
        trial = repo.current() if repo is not None else None
        if trial is not None:
            return _open_trial_response(trial)
        provided = None if open_trial_provider is None else open_trial_provider()
        if provided is None:
            return _error(404, "no_open_trial")
        return provided

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/trials/{{trial_id}}", response_model=None)
    async def signal_trials_trial(trial_id: str) -> JSONResponse | OpenTrialResponse:
        """Serve a known trial's decision-time evidence, or 404 for an id nothing matches.

        Carries the same no-future evidence as ``/open-trial`` and nothing more. Outcomes,
        markouts and participant records are H4.3's, and serving a placeholder for them here
        would publish a settlement state that has not been computed.

        The id is never echoed into the refusal: it arrives from a URL path segment, so echoing
        it would reflect caller-controlled text back into logs and responses.
        """
        repo = _live_trials()
        trial = None if repo is None else repo.get(trial_id)
        if trial is None:
            return _error(404, "trial_not_found")
        return _open_trial_response(trial)

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/agents/{{payer_id}}")
    async def signal_trials_agent(payer_id: str) -> JSONResponse:
        """No participant records exist until a paid commit can settle (H4.1/H4.3)."""
        return _error(404, "agent_not_found")

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/commit")
    async def signal_trials_commit_get() -> JSONResponse:
        """Refuse honestly when no payment gate is in front of this route.

        Both commit handlers are UNREACHABLE on a properly composed app: H4.1 mounts
        :class:`~veridex.signal_trials.payments.SignalTrialsPaymentASGI` around the app, and it
        answers every gated method on this path itself — a 402 challenge when unpaid, and the
        settlement-atomic commit path when paid. These handlers are what remains when no gate is
        mounted, and their only job is to make that state impossible to mistake for a success.
        """
        return _commit_unavailable()

    @app.post(f"{SIGNAL_TRIALS_PREFIX}/commit")
    async def signal_trials_commit_post(commit: CommitRequest) -> JSONResponse:
        """Refuse honestly when no payment gate is in front of this route.

        The body is still parsed, so the frozen request contract stays live in the OpenAPI
        schema and a malformed body is still a 422 rather than a 503 — a caller debugging its
        request shape gets the same answer whether or not a gate happens to be mounted.
        """
        return _commit_unavailable()

    def _commit_unavailable() -> JSONResponse:
        """Name WHICH precondition is missing, because the two are fixed differently.

        ``trials_not_open`` means this deployment has no live-trial store at all, so there is
        nothing to commit to — the H1.2 answer, and still the truthful one.
        ``payment_gate_not_configured`` means trials exist but no payment layer is in the request
        path, which is an operator misconfiguration rather than a quiet period: a free 200 here
        would publish a benchmark commitment that was never paid for.
        """
        if _live_trials() is None or _store() is None:
            return _error(503, "trials_not_open")
        return _error(503, "payment_gate_not_configured")

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/receipts/{{receipt_id}}/verify")
    async def signal_trials_verify_receipt(receipt_id: str) -> JSONResponse:
        """Re-derive a finalized receipt's commit-time claims. Free, and honest about failure.

        **A failed check is a 200 carrying a ``fail``**, never a 500. The verdict is what this
        route exists to publish, so reporting a tampered receipt as a server error would make
        tampering indistinguishable from an outage — and reporting it as a success with no checks
        would make it indistinguishable from an intact receipt. Both are the same lie in opposite
        directions, and this is the trust surface the whole benchmark rests on.

        404 covers everything that is NOT a finalized receipt, under ONE code, because the
        distinctions are not the caller's business and some of them are nobody's: a staged row is
        a commitment that was received and not yet paid for, a quarantined slot is a settlement
        whose outcome is unknown, an unmounted store means no receipt can exist at all, and an
        unknown id may be a probe. Naming which one applied would confirm the existence of a
        pending payment to whoever asked.

        The id is never echoed into the refusal — it arrives from a URL path segment, so echoing it
        would reflect caller-controlled text back into logs and responses.
        """
        commit_store = _store()
        if commit_store is None:
            return _error(404, "receipt_not_found")
        try:
            report = verify_receipt(receipt_id, commit_store)
        except KeyError:
            return _error(404, "receipt_not_found")
        return JSONResponse(status_code=200, content={"receipt_id": report.receipt_id, "checks": dict(report.checks)})
