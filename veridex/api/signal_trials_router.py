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

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from veridex.api.signal_trials_schemas import (
    AgentRecordResponse,
    CommitReceiptResponse,
    CommitRequest,
    OpenTrialResponse,
    SignalTrialsSeasonResponse,
    TrialOutcomeModel,
    TrialResponse,
    VerifyReceiptResponse,
)
from veridex.signal_trials.live import (
    build_agent_record,
    finalized_commits_for_trial,
    settle_commit,
    unsettled_commit,
)
from veridex.signal_trials.published import read_season, read_state
from veridex.signal_trials.receipts import TERMINAL_STATUSES, verify_receipt

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from veridex.signal_trials.live import LiveTrial, LiveTrialRepository
    from veridex.signal_trials.receipts import CommitRecord, ReceiptStore, TrialOutcome

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


def _trial_response(trial: LiveTrial, outcome: TrialOutcome | None) -> TrialResponse:
    """Render a trial with its settlement, or with ``outcome=None`` when none is recorded.

    ``None`` is served rather than a synthesized ``pending`` outcome, and the two are different
    claims: ``null`` says no settler has run on this trial, a recorded ``pending`` says one ran and
    the answer is not knowable yet. Manufacturing the second from the first would publish a
    settlement state nothing computed — the placeholder H1.2 refused to serve.
    """
    return TrialResponse(
        trial_id=trial.trial_id,
        trial_mode="live",
        t0_ms=trial.t0_ms,
        commit_deadline_ms=trial.commit_deadline_ms,
        evidence=trial.evidence,
        evidence_hash=trial.evidence_hash,
        outcome=None if outcome is None else TrialOutcomeModel(**asdict(outcome)),
    )


def _commit_receipt_response(receipt_id: str, store: ReceiptStore) -> CommitReceiptResponse | None:
    """Render one finalized receipt joined to its trial's outcome, or ``None`` if unreadable.

    ``None`` means there is no renderable receipt. Two states reach it and they are the same fact
    to a caller: the row's bytes cannot be read, or it parses into values that cannot be coerced
    into a record. Letting either escape would answer 500 for a receipt whose verify report has
    eight perfectly good verdicts to publish — the exact tampering-versus-outage conflation this
    route exists to avoid. Rendering a partial receipt out of the wreckage was the alternative and
    is worse: it would serve fields nothing can re-derive.

    The ``record is None`` branch is a third path to ``None`` and deliberately NOT a third
    published meaning. The only caller answers 404 for an id it holds no row for, before this
    helper renders anything, so the branch is reachable only when the row is unlinked BETWEEN that
    check and this read — it is the guard for exactly that race and nothing else.
    :class:`~veridex.api.signal_trials_schemas.VerifyReceiptResponse` states the frozen contract,
    and it says an ABSENT receipt is never a ``null`` here. Do not widen this prose back into a
    claim that absence is one of the states served.

    **The catch is bounded, and this names the boundary.** Building a record out of an untrusted
    parsed row fails in three ways: ``ValueError`` (a row that is not readable JSON, or a value
    ``float()`` refuses), ``TypeError`` (a value of a type the coercion cannot take at all — a
    ``p_follow_profitable`` stored as a list), and ``RecursionError`` (a value nested past what the
    interpreter can walk, which a tampered row can carry and which is not a ``ValueError``). That
    is a claim about the ROW being unrenderable in every case. Anything outside the three
    propagates — in particular ``OSError``, which is an unreadable disk or a permissions fault and
    genuinely IS the service being broken, so a 500 is the honest answer to it.

    ``store.outcome`` is inside the same guard on purpose: a corrupt OUTCOME row also leaves this
    receipt unrenderable, and the four outcome checks beside it already report that corruption as
    ``fail``.

    **The guard spans the JOIN and the MODEL, and not only the two reads**, which is the correction
    to a version that stopped at ``store.outcome``. Everything after a read is still derived from
    the same untrusted row and fails on the same class of input:

    * :func:`~veridex.signal_trials.live.settle_commit` raises ``ValueError`` on an outcome that
      claims ``settled`` while carrying no verdict — a tamper the four outcome checks report as
      ``fail``, and one that reached this function as an exception instead.
    * ``CommitReceiptResponse(...)`` raises pydantic's ``ValidationError`` (a ``ValueError``) on a
      row whose nullable or string fields are the wrong SHAPE. Those pass through
      :meth:`~veridex.signal_trials.receipts.ReceiptStore._record_from` uncoerced by design —
      ``null`` there is a tamper signal the model is meant to serve — so the model is the first
      thing that sees a ``commit_deadline_ms`` stored as a list.

    Leaving either outside meant a receipt whose verify report had eight perfectly good verdicts to
    publish was answered with a 500, which is precisely the outage-versus-tamper conflation this
    route exists to refuse. The boundary is still the SAME three exception types, so the
    ``OSError`` discrimination is unchanged: widening WHAT is guarded is not widening WHAT IS
    CAUGHT, and an infrastructure fault anywhere in here still reaches the client as a 500.
    """
    try:
        record = store.record(receipt_id)
        if record is None:
            return None
        return _render_commit_receipt(record, store)
    except (ValueError, TypeError, RecursionError):
        return None


def _render_commit_receipt(record: CommitRecord, store: ReceiptStore) -> CommitReceiptResponse:
    """Join one finalized ``record`` to its trial's outcome and render it. RAISES on an unreadable row.

    Extracted from :func:`_commit_receipt_response` so the FIELD MAPPING EXISTS EXACTLY ONCE while
    two callers get the two different failure meanings they each need:

    * the verify route wraps this in its bounded catch and serves ``None``, because a receipt whose
      bytes are damaged still has eight perfectly good verdicts to publish about that damage;
    * the participant-join route calls it BARE, because an unreadable row there must fail the whole
      request rather than quietly shrink a published participant set.

    The alternative was a second copy of the fourteen-field construction, which is how two spellings
    of one thing drift apart — the defect class this milestone has already paid for more than once.
    Extracting keeps ``_commit_receipt_response``'s guard spanning both the READ and the JOIN AND the
    MODEL exactly as before: nothing about what it catches has changed, only where the body lives.
    """
    outcome = store.outcome(record.trial_id)
    settlement = unsettled_commit(record) if outcome is None else settle_commit(record, outcome)
    return CommitReceiptResponse(
        receipt_id=record.receipt_id,
        trial_id=record.trial_id,
        payer=record.payer,
        p_follow_profitable=record.p_follow_profitable,
        methodology_version=record.methodology_version,
        action=settlement.action,
        status=settlement.status,
        brier=settlement.brier,
        chosen_markout_bps=settlement.chosen_markout_bps,
        committed_at_ms=record.committed_at_ms,
        commit_deadline_ms=record.commit_deadline_ms,
        trial_mode=record.trial_mode,
        body_hash=record.body_hash,
        payment_tx_hash=record.payment_tx_hash,
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
            receipt_store = _store()
            outcome = None if receipt_store is None else receipt_store.outcome(trial.trial_id)
            if outcome is not None and outcome.status in TERMINAL_STATUSES:
                return _error(404, "no_open_trial")
            return _open_trial_response(trial)
        provided = None if open_trial_provider is None else open_trial_provider()
        if provided is None:
            return _error(404, "no_open_trial")
        return provided

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/trials/{{trial_id}}", response_model=None)
    async def signal_trials_trial(trial_id: str) -> JSONResponse | TrialResponse:
        """Serve a known trial's decision-time evidence and its outcome, or 404 for an unknown id.

        The evidence half is byte-for-byte what ``/open-trial`` serves: a trial's terms do not
        change when it settles, and a reader has to be able to compare a receipt against the same
        evidence the committer saw. The outcome half is ``null`` until one is recorded.

        A CORRUPT outcome row is left to raise rather than served as ``null``, and that is the
        deliberate reading: ``null`` claims no settlement has been recorded, which is false about a
        row that was recorded and then destroyed. The service cannot answer this trial honestly, so
        it says so — unlike the verify route, whose entire purpose is to publish a verdict about a
        damaged artifact.

        The id is never echoed into the refusal: it arrives from a URL path segment, so echoing
        it would reflect caller-controlled text back into logs and responses.
        """
        repo = _live_trials()
        trial = None if repo is None else repo.get(trial_id)
        if trial is None:
            return _error(404, "trial_not_found")
        commit_store = _store()
        return _trial_response(trial, None if commit_store is None else commit_store.outcome(trial.trial_id))

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/trials/{{trial_id}}/receipts", response_model=None)
    async def signal_trials_trial_receipts(trial_id: str) -> JSONResponse | list[CommitReceiptResponse]:
        """Serve the FINALIZED participant commitments on one trial, in stable receipt order.

        This is the JOIN the frozen models deliberately leave out. ``TrialOutcomeModel`` carries no
        payer, probability or Brier because two agents who committed opposite probabilities against
        one event share exactly that outcome and differ only in their participant records — so the
        event level stays event-level and the join lives here, in its own route, rather than as an
        array bolted onto ``TrialResponse``.

        **Finalized ONLY, and by construction rather than by this route remembering.**
        :func:`~veridex.signal_trials.live.finalized_commits_for_trial` reads
        :meth:`~veridex.signal_trials.receipts.ReceiptStore.finalized`, the store's single public
        visibility gate, so a staged, in-flight, settle-attempted or quarantined row cannot appear
        here. The private slot iterator reads every state and was the first thing proposed for this
        route; using it would have published commitments that were never paid for.

        **An absent store is 503, NEVER ``200 []``.** An empty array is a positive claim — nobody
        committed to this trial — and a deployment with no store mounted has no basis for it. 404 is
        equally wrong when the trial itself is known, so the refusal names its own missing
        precondition, the way the commit route already separates "no trials" from "no payment gate".

        **A row nothing can read fails the whole request.** ``finalized()`` does not swallow, and
        :func:`_render_commit_receipt` is called BARE here, so a corrupt receipt or outcome row
        raises instead of dropping out of the array. Serving the survivors would publish a
        complete-looking participant set that is missing a paid commitment — smaller and cleaner than
        the artifacts support, and indistinguishable to a caller from a trial that only ever had one
        participant. This route has no verdict to publish about the damage; that is the verify
        route's job. The honest answer is that the set cannot be served.

        **The join filters on the RESOLVED ``trial.trial_id``, never on the raw path segment**, which
        is the same read the trial route above uses for its outcome. Resolution is permitted to
        canonicalize: ``LiveTrialRepository.get`` returns a trial whose id comes from the stored
        DOCUMENT, and on a case-insensitive filesystem — macOS APFS by default, every Windows volume —
        a case-variant URL resolves to it. Filtering on the caller's spelling there matched no record
        and answered ``200 []``, which is the one answer this route must never invent: an empty array
        claims nobody committed, and it was making that claim about a trial with a paid participant
        while the trial route beside it served that same trial's settled outcome.

        The id is never echoed into the refusal: it arrives from a URL path segment, so echoing it
        would reflect caller-controlled text back into logs and responses.
        """
        repo = _live_trials()
        trial = None if repo is None else repo.get(trial_id)
        if trial is None:
            return _error(404, "trial_not_found")
        commit_store = _store()
        if commit_store is None:
            return _error(503, "participant_store_unavailable")
        return [
            _render_commit_receipt(record, commit_store)
            for record in finalized_commits_for_trial(commit_store, trial.trial_id)
        ]

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/agents/{{payer_id}}", response_model=None)
    async def signal_trials_agent(payer_id: str) -> JSONResponse | AgentRecordResponse:
        """Serve a payer's live participant record, or 404 when they have no FINALIZED commit.

        Zero finalized commits is 404 rather than a 200 carrying zeros, because an all-zero record
        asserts that this payer participated and scored nothing — a different claim from having no
        record at all, and one that would let any address be quoted as a Veridex participant.

        A STAGED row does not create a record. It is a commitment that was received and not paid
        for, and the aggregation reads the store's public gate, so this is a property of one read
        path rather than of this route remembering to filter.

        A CORRUPT outcome row on any of this payer's trials is left to raise, exactly as on the
        trial route: the record is an AGGREGATE, so serving it while silently skipping the trial
        nothing could read would publish a smaller, better-looking record than the artifacts
        support. This route has no verdict to publish about the damage — that is the verify
        route's job — so the honest answer is that the record cannot be computed.
        """
        commit_store = _store()
        if commit_store is None:
            return _error(404, "agent_not_found")
        record = build_agent_record(payer_id, commit_store)
        if record.commits == 0:
            return _error(404, "agent_not_found")
        return AgentRecordResponse(**record.model_dump())

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

    @app.get(f"{SIGNAL_TRIALS_PREFIX}/receipts/{{receipt_id}}/verify", response_model=None)
    async def signal_trials_verify_receipt(receipt_id: str) -> JSONResponse | VerifyReceiptResponse:
        """Re-derive a finalized receipt's commit-time and settlement-time claims. Free, and honest.

        **A failed check is a 200 carrying a ``fail``**, never a 500. The verdict is what this
        route exists to publish, so reporting a tampered receipt as a server error would make
        tampering indistinguishable from an outage — and reporting it as a success with no checks
        would make it indistinguishable from an intact receipt. Both are the same lie in opposite
        directions, and this is the trust surface the whole benchmark rests on.

        That holds for a row whose bytes are DESTROYED as well as one that was edited: an
        unreadable row verifies as four commit ``fail``s and four ``pending`` outcome checks rather
        than escaping as an exception, which is decided in
        :func:`~veridex.signal_trials.receipts.verify_receipt` rather than papered over here. Its
        ``receipt`` is ``null`` for the same reason — see :func:`_commit_receipt_response`. This handler deliberately does NOT catch broadly. A ``ValueError`` reaching it would
        mean the verifier stopped honouring that contract, and swallowing it here would hide the
        regression while leaving the route looking correct. What can still legitimately produce a
        500 is an ``OSError`` — an unreadable disk or a permissions fault — which genuinely is the
        service being broken rather than a statement about the receipt.

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
        return VerifyReceiptResponse(
            receipt_id=report.receipt_id,
            checks=dict(report.checks),
            receipt=_commit_receipt_response(receipt_id, commit_store),
        )
