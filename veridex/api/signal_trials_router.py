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

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: Every route this lane owns lives under this prefix.
SIGNAL_TRIALS_PREFIX = "/signal-trials"


def _error(status_code: int, code: str) -> JSONResponse:
    """Build the lane's error envelope: a stable machine-readable ``error`` code.

    Deliberately not ``HTTPException``, whose envelope is ``{"detail": ...}``. The
    frozen contract is ``{"error": "<code>"}``, and agents match on that code.
    """
    return JSONResponse(status_code=status_code, content={"error": code})


def register_signal_trials_routes(
    app: FastAPI,
    *,
    data_dir: Path | str | None = None,
    open_trial_provider: Callable[[], OpenTrialResponse | None] | None = None,
) -> None:
    """Register the signal-trials routes onto ``app``.

    Args:
        app: The FastAPI application to mount the routes on.
        data_dir: The signal-trials data directory holding the published-season
            repository. ``None`` means none is configured, which reads as an honest
            ``not_built`` rather than an error — a fresh deployment with no volume
            attached still answers ``/health``.
        open_trial_provider: Returns the currently open live trial, or ``None`` when
            none is open. H1.2 ships no live-trial store, so the default is ``None``
            and ``/open-trial`` honestly reports ``no_open_trial``; H4.1 supplies the
            real provider without changing this route's contract.
    """

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/health")
    async def signal_trials_health() -> dict[str, Any]:
        """Liveness plus the one fact a caller needs before reading anything else.

        ``season_state`` distinguishes an intentional ``no_season`` — the preflight ran
        and declined to build a season — from ``not_built``, where it never ran. Both
        answer ``404`` on ``/season``, so health is the only place they differ.
        """
        return {"ok": True, "season_state": read_state(data_dir)["state"]}

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
        season = read_season(data_dir)
        if season is None:
            return _error(404, "no_season_published")
        return SignalTrialsSeasonResponse(**season)

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/open-trial", response_model=None)
    async def signal_trials_open_trial() -> OpenTrialResponse | JSONResponse:
        """Frozen §11 discovery: the open live trial's no-future evidence, hash and deadline."""
        trial = None if open_trial_provider is None else open_trial_provider()
        if trial is None:
            return _error(404, "no_open_trial")
        return trial

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/trials/{{trial_id}}")
    async def signal_trials_trial(trial_id: str) -> JSONResponse:
        """No trial store exists until H4.1, so every id is honestly unknown."""
        return _error(404, "trial_not_found")

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/agents/{{payer_id}}")
    async def signal_trials_agent(payer_id: str) -> JSONResponse:
        """No participant records exist until a paid commit can settle (H4.1/H4.3)."""
        return _error(404, "agent_not_found")

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/commit")
    async def signal_trials_commit_get() -> JSONResponse:
        """GET is gated alongside POST so the OKX review probe meets the paywall, not a 200."""
        return _error(503, "trials_not_open")

    @app.post(f"{SIGNAL_TRIALS_PREFIX}/commit")
    async def signal_trials_commit_post(commit: CommitRequest) -> JSONResponse:
        """Validate the frozen request shape, then refuse honestly.

        The body is parsed even though nothing is done with it: it keeps the frozen
        request contract live in the OpenAPI schema and exercised by tests, so the
        shape H4.1 inherits has been checked against real callers rather than assumed.
        """
        return _error(503, "trials_not_open")

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/receipts/{{receipt_id}}/verify")
    async def signal_trials_verify_receipt(receipt_id: str) -> JSONResponse:
        """No receipts can exist before a payment can settle."""
        return _error(404, "receipt_not_found")
