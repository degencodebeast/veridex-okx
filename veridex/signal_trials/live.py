"""Live trials — opening one, publishing it for free discovery, and deciding a paid commit.

Three things live here, and the division of labour between this module and
:class:`~veridex.signal_trials.payments.SignalTrialsPaymentASGI` is deliberate:

:class:`LiveTrial` / :func:`open_live_trial`
    The trial itself and its commit window. A trial is opened over evidence that was observable
    at ``t0`` and nothing else.
:class:`LiveTrialRepository`
    The published open trial, on disk, because the process that opens a trial (an operator
    script) is not the process that serves it (the API).
:func:`handle_commit`
    The commit DECISION, over a resolved trial, a verified payer and a clock. It validates and
    it resolves idempotency. **It writes nothing, ever** — not on the accept path either.

That last point is the load-bearing one. Everything that can create or destroy a record lives
in the payment wrapper, in one place, in the order settlement requires: stage, mark the attempt,
settle, journal, finalize. A validator that could also write would make "a rejected commit wrote
nothing" a property of two modules agreeing rather than a property of one module's shape.

The split of responsibility with the wrapper, stated once so neither side re-implements the
other:

* the **wrapper** owns the DECISION SLOT — acquiring it, and answering for the states it can
  already be in (``in_flight``, the two indeterminate states, ``finalized``);
* this module owns the REQUEST — is the trial real, live and still open, is the probability a
  probability, and does an existing finalized record for this payer and trial carry the same
  canonical body.

Neither consults the other's half, so there is no second opinion to disagree with.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel

from veridex.api.signal_trials_schemas import CommitRequest
from veridex.signal_trials.challenge_spec import CanonicalSignal, evidence_hash, visible_at_decision
from veridex.signal_trials.okx_client import BAR_MS, CandleSeries
from veridex.signal_trials.preflight import FROZEN_HORIZON_MS
from veridex.signal_trials.primitives import brier_score
from veridex.signal_trials.receipts import (
    Action,
    CommitRecord,
    OutcomeProvenance,
    ParticipantSettlement,
    ReceiptStore,
    SettledTrial,
    TrialOutcome,
    TrialStatus,
    canonical_body_hash,
)
from veridex.signal_trials.spot_markout import assert_positive_price, select_settlement_candle, spot_markout

#: The commit window, frozen by spec section 11: ``decision_window_seconds = 300``, so
#: ``commit_deadline = t0 + 300s``. A commit is rejected at ``received_at >= commit_deadline``,
#: exclusive at the top — the boundary instant is already too late.
DECISION_WINDOW_MS: Final[int] = 300_000

#: The only mode a paid external commit may be placed on. Spec section 11: replay outcomes are
#: publicly knowable, so a paid replay "prediction" would fake a record.
LIVE_MODE: Final[str] = "live"

#: How long past a settlement candle's CLOSE it may take for that candle to become fetchable.
#: Frozen at 10 minutes. Absence is only evidence of absence once a completed candle has had time
#: to appear on the endpoint; before that, absence is evidence of nothing.
FETCH_GRACE_MS: Final[int] = 600_000

#: The bar width assumed when NO series — and therefore no bar provenance — was supplied. §7 takes
#: the bar from the request, so with no request there is nothing to take it from; the widest frozen
#: bar is the only choice that cannot declare a window closed while a valid candle might still be
#: forming. See :func:`settle_trial_outcome`.
WIDEST_FROZEN_BAR_MS: Final[int] = max(BAR_MS.values())

#: The §8.1 display bands. Inclusive on both edges: ``p >= 0.60`` FOLLOW, ``p <= 0.40`` FADE, and
#: strictly between them ABSTAIN. Symmetric around 0.5 by construction.
FOLLOW_BAND: Final[float] = 0.60
FADE_BAND: Final[float] = 0.40

#: The official declared cost, §8.2. The ``[0, 10, 25, 50]`` bps sweep is a DIAGNOSTIC DISPLAY and
#: never the recorded outcome, which is why ``cost_bps`` is an argument and this is its default
#: rather than the sweep being a separate code path.
DECLARED_COST_BPS: Final[int] = 25

#: The §8.3 per-event markout cap, applied symmetrically. One extreme memecoin move must not
#: dominate a record, in either direction — a cap applied only above zero would let a single
#: collapse do exactly that.
MARKOUT_CAP_BPS: Final[int] = 500

_TRIALS_DIRNAME = "trials"
_OPEN_POINTER_FILENAME = "open.json"


@dataclass(frozen=True)
class LiveTrial:
    """One trial, open for paid commitments until :attr:`commit_deadline_ms`.

    Frozen because ``sig`` is the evidence a receipt binds to, through
    :func:`~veridex.signal_trials.challenge_spec.evidence_hash`. A trial whose deadline or
    evidence could be mutated after publication would let a commit be re-judged against
    different terms than the ones it was placed under.

    ``trial_mode`` is a plain ``str`` rather than a ``Literal`` so a replay trial is
    REPRESENTABLE — the frozen lifecycle test constructs one precisely in order to watch the
    paid path refuse it. A type that made the illegal state unconstructable would also make the
    refusal untestable.
    """

    trial_id: str
    sig: CanonicalSignal
    trial_mode: str
    t0_ms: int
    commit_deadline_ms: int

    @property
    def evidence(self) -> dict[str, Any]:
        """The decision-time evidence, with no future field reachable."""
        return visible_at_decision(self.sig)

    @property
    def evidence_hash(self) -> str:
        """The hash binding this trial's evidence, so a receipt can be re-derived."""
        return evidence_hash(self.sig)


@dataclass(frozen=True)
class CommitOutcome:
    """The decision :func:`handle_commit` reached, and nothing it did.

    ``status == 200`` with ``receipt_id is None`` means *validated, proceed to payment*.
    ``status == 200`` with a ``receipt_id`` means *this is a replay of a commitment already
    finalized* — return the ORIGINAL receipt and settle nothing. The two are distinguished by
    the receipt id rather than by a separate flag so a caller cannot read one as the other by
    forgetting to check a boolean.
    """

    status: int
    error: str | None = None
    receipt_id: str | None = None


def open_live_trial(sig: CanonicalSignal, *, now_ms: int, trial_id: str | None = None) -> LiveTrial:
    """Open a live trial over ``sig`` at ``now_ms``.

    The deadline is measured from ``now_ms`` — the instant the trial OPENS, which is its ``t0``
    for commit purposes — not from ``sig.t0_ms``. The two are usually equal and are allowed to
    differ, because the signal is observed and the trial is opened by different code at
    different moments; what must never happen is a window that starts before the evidence was
    observable, which is why evidence from the future is refused outright.

    ``trial_id`` defaults to a digest of the evidence hash and the open instant, which makes
    re-opening the same trial idempotent rather than duplicative. An explicit id is accepted so
    an operator can name a trial, and so a test can commit to a known id without re-deriving
    the default rule and agreeing with a broken derivation by construction.

    Args:
        sig: The canonicalized signal, every field observable at its own ``t0``.
        now_ms: The instant the trial opens; becomes ``t0_ms``.
        trial_id: An explicit id, or ``None`` to derive one.

    Returns:
        The open live trial.

    Raises:
        ValueError: ``sig`` was observed AFTER ``now_ms``. Opening a window over evidence from
            the future would mean the commit-before-outcome seal (spec section 13) never held,
            and the direction of that check is chosen to fail closed: a clock skew refuses a
            trial rather than publishing one whose evidence a committer could not have seen.
    """
    if sig.t0_ms > now_ms:
        raise ValueError(
            f"cannot open a live trial at {now_ms} over evidence observed later, at {sig.t0_ms}; "
            "evidence from the future breaks the commit-before-outcome seal"
        )
    resolved_id = trial_id if trial_id is not None else f"trial_{evidence_hash(sig)[:24]}_{now_ms}"
    return LiveTrial(
        trial_id=resolved_id,
        sig=sig,
        trial_mode=LIVE_MODE,
        t0_ms=now_ms,
        commit_deadline_ms=now_ms + DECISION_WINDOW_MS,
    )


def handle_commit(
    request: CommitRequest,
    *,
    trial: LiveTrial | None,
    payer: str,
    now_ms: int,
    store: ReceiptStore,
) -> CommitOutcome:
    """Decide a paid commit against a resolved trial. Writes nothing.

    The checks run in the order a caller can act on, cheapest and most fundamental first, and
    each refusal names its own reason so an agent can tell them apart:

    ``404 trial_not_found``
        The caller looked the trial up and found nothing. Resolution is the CALLER's job, which
        is why the trial arrives as an argument and the request's ``trial_id`` is never
        re-resolved here — one lookup, one answer, no chance of the two disagreeing.
    ``409 replay_trials_read_only``
        Spec section 11: paid external commits are live-only. Checked before the deadline
        because a replay trial is refused whatever its window says, so reporting a *stale
        window* on a trial that could never be committed to would be the wrong reason.

    Then IDEMPOTENCY, over the FINALIZED record for this payer and trial only — a staged,
    attempted or quarantined row is not a commitment and never answers this question:

    * identical canonical body -> ``200`` carrying the ORIGINAL receipt id;
    * different canonical body -> ``409 different_commit_body``, writing nothing (spec
      section 11 makes this explicit: same payer and trial with a different body writes
      nothing).

    **Idempotency is resolved BEFORE the deadline, and the order is deliberate.** A finalized
    record is proof that this commitment was accepted while the window was open — it could not
    have been finalized otherwise, because the deadline check below is what gated it. Answering
    a replay of that commitment with ``410 commit_window_closed`` would therefore report
    something false: not "your commit was late" but "the commit you already made never
    happened". Reporting a past acceptance is not accepting a late commit, and no settle can
    occur on this path either way, so the safe answer and the honest answer agree.

    Only then the clock and the value, which decide whether a NEW commitment may be made:

    ``410 commit_window_closed``
        ``received_at >= commit_deadline``, exactly as frozen — the boundary instant is late.
        ``410`` rather than ``409``: the window is permanently gone, not a conflict to retry.
    ``422 invalid_probability``
        Defence in depth. A ``CommitRequest`` built through pydantic cannot carry a probability
        outside ``[0, 1]``, but one built through ``model_construct`` can, and the plan lists
        this as a handler validation. On the money path a bound worth stating is worth checking
        where it is used, not only where it is parsed.

    Args:
        request: The payer's commit request.
        trial: The resolved trial, or ``None`` when the id matched nothing.
        payer: The VERIFIED payer address, already checked non-empty by the caller.
        now_ms: Receipt time of the request.
        store: The commit store, read-only in this function.

    Returns:
        The decision. ``200`` with no receipt id means "validated, proceed".

    Raises:
        ValueError: ``payer`` is empty. A precondition, not a status: an empty payer would key
            every anonymous commit to ONE shared decision slot, so the payment layer is
            required to have fail-closed on it long before this point, and a caller that did
            not is a defect rather than a bad request.
    """
    if not payer:
        raise ValueError("handle_commit requires a verified non-empty payer; the caller must fail closed on None/empty")
    if trial is None:
        return CommitOutcome(status=404, error="trial_not_found")
    if trial.trial_mode != LIVE_MODE:
        return CommitOutcome(status=409, error="replay_trials_read_only")

    existing = store.finalized_for(payer, trial.trial_id)
    if existing is not None:
        if existing.body_hash == canonical_body_hash(request):
            return CommitOutcome(status=200, receipt_id=existing.receipt_id)
        return CommitOutcome(status=409, error="different_commit_body")

    if now_ms >= trial.commit_deadline_ms:
        return CommitOutcome(status=410, error="commit_window_closed")
    if not 0.0 <= request.p_follow_profitable <= 1.0:
        return CommitOutcome(status=422, error="invalid_probability")
    return CommitOutcome(status=200)


class LiveTrialRepository:
    """The published open trial, on disk, readable by a process that did not open it.

    An operator script opens a trial; a separately running API serves it. Nothing is held in
    memory, and the pointer is re-read on every call, because a snapshot taken at import time
    would keep answering "no open trial" after one was opened, until somebody restarted the API.

    **Payload before pointer, always.** :meth:`publish` writes the trial document and only then
    the pointer that advertises it. A crash in that order leaves a trial document nobody
    references, which is invisible and harmless. The reverse order leaves a pointer to a
    document that does not exist, which :meth:`current` would have to either crash on or lie
    about. This is the same obligation ``read_season`` documents for the published-season
    repository, discharged here by the writer that owns it.
    """

    def __init__(self, root: Path | str) -> None:
        """Create a repository rooted at ``root``, creating its directory on demand."""
        self.root = Path(root)
        self._trials = self.root / _TRIALS_DIRNAME
        self._trials.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
        """Write ``payload`` to ``path`` atomically; a reader must never see it half-written."""
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, indent=2, sort_keys=True)
        handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _trial_path(self, trial_id: str) -> Path:
        """Return the document path for ``trial_id``, refusing an id that could escape the tree.

        Raises:
            ValueError: ``trial_id`` is empty or contains a path separator or ``..``. Trial ids
                reach this from a URL path segment, so this is the boundary where a traversal
                attempt has to stop.
        """
        if not trial_id or "/" in trial_id or "\\" in trial_id or trial_id in {".", ".."}:
            raise ValueError("trial_id must be a non-empty single path segment")
        return self._trials / f"{trial_id}.json"

    def publish(self, trial: LiveTrial) -> None:
        """Publish ``trial`` as the open trial: document first, then the pointer.

        Args:
            trial: The trial to publish.

        Raises:
            ValueError: A DIFFERENT trial is already published under the same id. Re-publishing
                the identical trial is idempotent and allowed, because opening the same trial
                twice is a normal retry; silently replacing a published trial's terms would
                move the deadline or the evidence under commitments already placed against it.
        """
        path = self._trial_path(trial.trial_id)
        document = {
            "trial_id": trial.trial_id,
            "trial_mode": trial.trial_mode,
            "t0_ms": trial.t0_ms,
            "commit_deadline_ms": trial.commit_deadline_ms,
            "sig": trial.sig.model_dump(mode="json"),
        }
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != document:
                raise ValueError(
                    f"trial {trial.trial_id!r} is already published with different terms; "
                    "refusing to move the deadline or evidence under existing commitments"
                )
        self._write_atomic(path, document)
        self._write_atomic(self.root / _OPEN_POINTER_FILENAME, {"trial_id": trial.trial_id})

    def _load(self, path: Path) -> LiveTrial:
        """Rebuild a :class:`LiveTrial` from its document."""
        document = json.loads(path.read_text(encoding="utf-8"))
        return LiveTrial(
            trial_id=str(document["trial_id"]),
            sig=CanonicalSignal(**document["sig"]),
            trial_mode=str(document["trial_mode"]),
            t0_ms=int(document["t0_ms"]),
            commit_deadline_ms=int(document["commit_deadline_ms"]),
        )

    def get(self, trial_id: str) -> LiveTrial | None:
        """Return the trial with ``trial_id``, or ``None`` when it is unknown."""
        try:
            path = self._trial_path(trial_id)
        except ValueError:
            # A malformed id cannot name a trial. Answering "unknown" is the honest reply to a
            # lookup and keeps a traversal attempt indistinguishable from a typo to the caller.
            return None
        return self._load(path) if path.is_file() else None

    def all_trials(self) -> list[LiveTrial]:
        """Every published trial, oldest first, then by id.

        The settler's read path: settlement is not about the trial that happens to be OPEN, it is
        about every trial whose horizon has passed, and by the time one is settleable a newer trial
        has usually replaced it at the pointer. Reading only :meth:`current` would leave every
        superseded trial permanently unsettled.

        Ordered by ``(t0_ms, trial_id)`` so a settling run processes trials chronologically and two
        runs over the same directory do the same work in the same order. Sorted rather than left to
        the filesystem, which orders by name and would interleave trials by their id digests.
        """
        trials = [self._load(path) for path in self._trials.glob("*.json")]
        return sorted(trials, key=lambda trial: (trial.t0_ms, trial.trial_id))

    def current(self) -> LiveTrial | None:
        """Return the currently open trial, or ``None`` when none is published.

        Absence of the pointer is the only thing read as "none open". A pointer naming a
        document that is missing is NOT absence — it is an inconsistent published set, and it is
        refused rather than reported as "no trial open", exactly as ``read_season`` refuses the
        mirror case. Reporting it as absence would hide the one failure the write order was
        chosen to prevent.

        Raises:
            ValueError: The pointer names a trial whose document is missing.
        """
        pointer = self.root / _OPEN_POINTER_FILENAME
        if not pointer.is_file():
            return None
        trial_id = str(json.loads(pointer.read_text(encoding="utf-8"))["trial_id"])
        trial = self.get(trial_id)
        if trial is None:
            raise ValueError(
                f"open-trial pointer names {trial_id!r} but its document is missing; "
                "refusing to report an asserted open trial as merely absent"
            )
        return trial


def finalized_commits_for_trial(store: ReceiptStore, trial_id: str) -> list[CommitRecord]:
    """Return the FINALIZED commitments on ``trial_id``, in stable receipt order.

    Goes through the store's public read path, so a staged, attempted or quarantined row can
    never appear here — a free read serves paid commitments only.
    """
    return sorted((r for r in store.finalized() if r.trial_id == trial_id), key=lambda r: r.receipt_id)


# ---------------------------------------------------------------------------- settlement, EVENT level


def unscored_boundary_ms(t0_ms: int, horizon_ms: int, bar_ms: int) -> int:
    """The instant at or after which a trial with no settlement candle is UNSCORED.

    ``T = t0 + horizon`` is when the trial's horizon lands, and it is NOT this boundary. §7 settles
    against the first confirmed candle whose close falls in ``[T, T + bar)``, so at ``T + 1ms`` the
    valid candle may not have closed yet — it cannot exist as a completed bar before its own close.
    Declaring UNSCORED there would report "no answer" about a bar that had not finished forming.
    One further bar covers the candle's own formation; :data:`FETCH_GRACE_MS` covers the interval
    in which a closed candle is not yet retrievable from the endpoint.

    So: ``T + bar_ms + FETCH_GRACE_MS``. The ``bar_ms`` term is the load-bearing one — a boundary
    that omitted it would be early by EXACTLY ``bar_ms``: 60 minutes under the 1H fallback, and
    only one minute under 1m, which is precisely why it is easy to omit and hard to notice.

    Args:
        t0_ms: The trial's open instant.
        horizon_ms: The settlement horizon (§8.3 fixes the sole ranking horizon at 1h).
        bar_ms: The settlement bar's width, from the series' request provenance.

    Returns:
        The first instant at which absence of a candle is evidence of absence.
    """
    return t0_ms + horizon_ms + bar_ms + FETCH_GRACE_MS


def settle_trial_outcome(
    trial: LiveTrial,
    series: CandleSeries | None,
    *,
    now_ms: int,
    cost_bps: int = DECLARED_COST_BPS,
) -> TrialOutcome:
    """Settle one trial at the EVENT level. Carries no participant data of any kind.

    Three outcomes, and the order they are decided in is the whole law:

    **A candle was found -> ``settled``, at ANY clock.** Eligibility is §7's close-boundary law,
    delegated to :func:`~veridex.signal_trials.spot_markout.select_settlement_candle` so there is
    one implementation of it. ``now_ms`` is not consulted on this path, and deliberately: a candle
    that exists exists, and gating settlement on the clock would leave a settleable trial reported
    as pending merely because the grace period had not elapsed.

    **No candle and ``now_ms < unscored_boundary`` -> ``pending``.** The valid candle may still be
    open, or closed and not yet fetchable.

    **No candle and ``now_ms >= unscored_boundary`` -> ``UNSCORED``.** The window and its grace
    both expired empty-handed. Never interpolated, never a guessed price (§7, §12).

    ``pending`` and ``UNSCORED`` carry IDENTICAL ``None`` metrics and differ only in ``status``.
    That is not an accident of representation — it is the honest shape, because neither state has
    a number to report — and it is why every caller and every test must branch on the status.

    Args:
        trial: The trial, whose sealed ``trigger_price`` is the entry (§7).
        series: The candles fetched for this trial, WITH their bar provenance. An EMPTY series is
            the honest "we looked and found nothing" and still carries the bar. ``None`` means no
            series is available at all — see below.
        now_ms: The clock, used ONLY to decide whether absence has become UNSCORED.
        cost_bps: The modeled round-trip cost charged to both directional legs. Defaults to §8.2's
            official 25; the ``[0, 10, 25, 50]`` sweep passes other values and is a diagnostic
            display that never becomes the recorded outcome.

    Returns:
        The outcome. Its ``entry`` is populated in every status, because the entry is sealed at
        ``t0`` and is known long before the settlement candle is.

    Raises:
        SpotMarkoutError: The trial's sealed entry is not a positive spot price. Refused before any
            status is decided — not even UNSCORED, which asserts something specific about a
            WELL-FORMED trial whose settlement candle is missing. A zero or negative entry is a
            data-integrity fault at ``t0``, and filing it under a market outcome would hide it.

    **``series=None`` and the bar width.** With no series there is no bar provenance, and §7 takes
    the bar from the request rather than inferring it — so the boundary is computed at
    :data:`WIDEST_FROZEN_BAR_MS`. The plan's contract does not name a width for this case and this
    is the resolution: it is the only choice that cannot declare UNSCORED early, because under any
    narrower bar the window closes SOONER. It keeps UNSCORED reachable rather than making it an
    unreachable state, at the cost of holding a never-fetched trial ``pending`` for up to 59
    minutes longer than a 1m trial would be. In production the settler always supplies a series,
    even an empty one, so this path is the fallback for a caller that had nothing to supply.
    """
    entry = assert_positive_price(trial.sig.trigger_price, "entry")
    settlement_target_ms = trial.t0_ms + FROZEN_HORIZON_MS
    if series is not None:
        candle = select_settlement_candle(series, t0_ms=trial.t0_ms, horizon_ms=FROZEN_HORIZON_MS)
        if candle is not None:
            close_ts_ms = candle.ts_open_ms + series.bar_ms
            markout = spot_markout(entry, candle.close, cost_bps)
            return TrialOutcome(
                trial_id=trial.trial_id,
                status="settled",
                entry=entry,
                future=candle.close,
                close_ts_ms=close_ts_ms,
                observation_lag_ms=close_ts_ms - settlement_target_ms,
                follow_markout_bps=markout.follow_markout_bps,
                fade_markout_bps=markout.fade_markout_bps,
                follow_profitable=markout.follow_profitable,
            )
        bar_ms = series.bar_ms
    else:
        bar_ms = WIDEST_FROZEN_BAR_MS
    status: TrialStatus = (
        "pending" if now_ms < unscored_boundary_ms(trial.t0_ms, FROZEN_HORIZON_MS, bar_ms) else "UNSCORED"
    )
    return TrialOutcome(
        trial_id=trial.trial_id,
        status=status,
        entry=entry,
        future=None,
        close_ts_ms=None,
        observation_lag_ms=None,
        follow_markout_bps=None,
        fade_markout_bps=None,
        follow_profitable=None,
    )


def settle_trial(
    trial: LiveTrial,
    series: CandleSeries,
    *,
    now_ms: int,
    cost_bps: int = DECLARED_COST_BPS,
) -> SettledTrial:
    """Settle ``trial`` and record WHAT IT WAS DERIVED FROM alongside the result.

    :func:`settle_trial_outcome` answers the scoring question and its return type is frozen to the
    nine fields H5.1 mirrors. This wraps it with §7's persisted provenance — the bar, the close
    boundary's inputs, the sealed evidence and the law version — which is what
    :func:`~veridex.signal_trials.receipts.verify_receipt` re-derives the outcome FROM. Keeping the
    two apart is what stops verification from comparing a stored value against a stored copy of
    itself: the outcome states a result, the provenance states its inputs, and a check that could
    read both from one field would pass for any value at all.

    **``series`` is REQUIRED here where :func:`settle_trial_outcome` accepts ``None``**, and the
    difference is the point of this function. A recordable settlement has to state which bar it was
    settled under; a settlement whose bar provenance is unknown cannot be verified and must not be
    written. Scoring can proceed without a series and record nothing — recording cannot.

    Args:
        trial: The trial to settle.
        series: The fetched candles WITH their bar provenance. An empty series is the honest
            "we looked and found nothing" and still carries the bar.
        now_ms: The clock.
        cost_bps: The modeled cost; §8.2's official 25 by default.

    Returns:
        The outcome paired with its provenance, ready for
        :meth:`~veridex.signal_trials.receipts.ReceiptStore.record_outcome`.
    """
    outcome = settle_trial_outcome(trial, series, now_ms=now_ms, cost_bps=cost_bps)
    # Re-selected rather than back-derived from ``outcome.close_ts_ms``. Deriving the open as
    # ``close_ts - bar_ms`` would make ``outcome_source``'s ``close_ts == ts_open + bar_ms``
    # arithmetically true at WRITE time whatever the outcome said, so the check could only ever
    # catch a later tamper and never a settlement computed against the wrong candle. Reading the
    # candle again costs one selection and makes the stored provenance an independent record of
    # the source.
    candle = select_settlement_candle(series, t0_ms=trial.t0_ms, horizon_ms=FROZEN_HORIZON_MS)
    return SettledTrial(
        outcome=outcome,
        provenance=OutcomeProvenance(
            bar=series.bar,
            bar_ms=series.bar_ms,
            t0_ms=trial.t0_ms,
            horizon_ms=FROZEN_HORIZON_MS,
            cost_bps=cost_bps,
            settlement_ts_open_ms=None if candle is None else candle.ts_open_ms,
            evidence=trial.evidence,
            evidence_hash=trial.evidence_hash,
        ),
    )


# ------------------------------------------------------------------- settlement, PARTICIPANT level


def commit_action(p_follow_profitable: float) -> Action:
    """Derive the §8.1 display stance from a payer's own probability.

    ``>= 0.60`` FOLLOW, ``<= 0.40`` FADE, strictly between ABSTAIN. Both edges are INCLUSIVE and
    the band is symmetric around 0.5.

    Derived rather than submitted so the displayed action and the committed confidence cannot
    contradict each other, and so this is never a Veridex-originated FOLLOW/FADE: it is a rendering
    of what the caller themselves sent (§3.8, §8.1).
    """
    if p_follow_profitable >= FOLLOW_BAND:
        return "FOLLOW"
    if p_follow_profitable <= FADE_BAND:
        return "FADE"
    return "ABSTAIN"


def settle_commit(commit: CommitRecord, outcome: TrialOutcome) -> ParticipantSettlement:
    """Join ONE finalized commit to ONE trial outcome.

    This is the per-participant half, and it is a separate function from
    :func:`settle_trial_outcome` for a reason that is easy to lose: a trial has one outcome and
    many participants. Two payers who committed 0.8 and 0.3 against the same event get two
    different Briers and two OPPOSITE markout legs out of the same ``outcome``. An event-level
    record could not express either difference.

    ``brier = (p - follow_profitable)^2`` on the settled path, computed through
    :func:`~veridex.signal_trials.primitives.brier_score` so this lane has one implementation of
    the arithmetic rather than a second copy that could drift. ``chosen_markout_bps`` is the leg
    the derived action took: the follow leg, the fade leg, or exactly 0 for ABSTAIN — abstaining is
    the honest way to decline a trial and must be worth neither more nor less than nothing (§8.2).

    An ABSTAIN commit is STILL Brier-scored. §8.2 admits no excluded sample, including neutral
    probabilities, so dropping abstainers would shrink the calibration sample by exactly the agents
    who declined.

    On a ``pending`` or ``UNSCORED`` outcome the status propagates with ``None`` metrics, and the
    action is still derived — it comes from the payer's probability, which was known at commit time
    and does not wait on the market.

    Args:
        commit: The FINALIZED commit record. A staged or quarantined row is not a commitment that
            was paid for, and the caller is responsible for never passing one.
        outcome: The outcome of the SAME trial.

    Returns:
        The participant settlement.

    Raises:
        ValueError: ``commit`` and ``outcome`` name different trials, or ``outcome`` claims
            ``settled`` without the metrics a settled outcome must carry. Both are defects rather
            than results: a cross-trial join would score a payer against an event they never
            committed to, and Brier-scoring against a missing verdict would have to invent an
            outcome indicator, which is interpolation under another name. Neither would leave any
            trace in the row it produced.
    """
    if commit.trial_id != outcome.trial_id:
        raise ValueError(
            f"receipt {commit.receipt_id!r} committed to trial {commit.trial_id!r} and cannot be settled against a "
            f"different trial's outcome ({outcome.trial_id!r})"
        )
    action = commit_action(commit.p_follow_profitable)
    if outcome.status != "settled":
        return ParticipantSettlement(
            receipt_id=commit.receipt_id,
            trial_id=commit.trial_id,
            payer=commit.payer,
            p_follow_profitable=commit.p_follow_profitable,
            action=action,
            brier=None,
            chosen_markout_bps=None,
            status=outcome.status,
        )
    if outcome.follow_profitable is None or outcome.follow_markout_bps is None or outcome.fade_markout_bps is None:
        raise ValueError(
            f"trial {outcome.trial_id!r} claims status 'settled' without follow_profitable and both markout legs; "
            "a settled outcome that carries no verdict cannot be scored, and inventing one would be interpolation"
        )
    chosen: dict[Action, int] = {
        "FOLLOW": outcome.follow_markout_bps,
        "FADE": outcome.fade_markout_bps,
        "ABSTAIN": 0,
    }
    return ParticipantSettlement(
        receipt_id=commit.receipt_id,
        trial_id=commit.trial_id,
        payer=commit.payer,
        p_follow_profitable=commit.p_follow_profitable,
        action=action,
        brier=brier_score([commit.p_follow_profitable], [1 if outcome.follow_profitable else 0]),
        chosen_markout_bps=chosen[action],
        status="settled",
    )


def unsettled_commit(commit: CommitRecord) -> ParticipantSettlement:
    """The participant view of a finalized commit whose trial has NO recorded outcome.

    ``pending``, because nothing has been settled — not absent, because the commitment itself is
    real and paid for. Spelled as its own function rather than by widening
    :func:`settle_commit` to accept ``None``, so that "there is no outcome" stays a statement the
    caller makes explicitly instead of a null flowing into the join.
    """
    return ParticipantSettlement(
        receipt_id=commit.receipt_id,
        trial_id=commit.trial_id,
        payer=commit.payer,
        p_follow_profitable=commit.p_follow_profitable,
        action=commit_action(commit.p_follow_profitable),
        brier=None,
        chosen_markout_bps=None,
        status="pending",
    )


# ---------------------------------------------------------------------------- agent records


class AgentRecord(BaseModel):
    """One payer's live participant record, over their FINALIZED commits only.

    ``qualified`` is ALWAYS ``False`` here and that is a claim boundary, not a placeholder waiting
    to be wired up. §8.4 gates qualification on a ``qualified`` SEASON plus ≥20 active decisions
    and ≥50% coverage — a live exhibition is a handful of trials with no season, no controls and no
    chronological replay behind it, so no live record could truthfully claim skill. It is carried
    explicitly rather than omitted so a consumer reads a stated ``false``.

    ``avg_brier`` and ``capped_avg_markout_bps`` are ``None`` until something settles: a zero Brier
    is a PERFECT score, so a default zero would publish a result where there is none.
    """

    payer: str
    commits: int
    settled: int
    pending: int
    unscored: int
    avg_brier: float | None
    capped_avg_markout_bps: int | None
    qualified: bool


def build_agent_record(payer: str, store: ReceiptStore) -> AgentRecord:
    """Aggregate ``payer``'s FINALIZED commits into one record. Idempotent by construction.

    **Recomputed from the primary artifacts** — the finalized commits and the recorded outcomes —
    and never from the persisted participant settlements, even though those exist. The settlement
    rows are a cache of this same derivation; if the record trusted them, a single rewritten
    settlement row would inflate an agent's standing while every primary artifact still verified.

    **Finalized only.** The payer's rows are read through
    :meth:`~veridex.signal_trials.receipts.ReceiptStore.public_records`, the store's single public
    read gate, so a staged, attempted or quarantined row cannot be counted — a commitment that was
    received is not a commitment that was paid for.

    The three status tallies partition ``commits``, so "the staged row was excluded" is checkable
    against the totals rather than assumed. A commit whose trial has no recorded outcome at all
    counts as ``pending``.

    The averages cover the SETTLED rows only. Averaging an UNSCORED trial in as a zero would credit
    an agent for a trial that was never scored — the same error §8.6 forbids in the coverage
    direction. Each event's markout is clamped to ``±``:data:`MARKOUT_CAP_BPS` BEFORE averaging
    (§8.3), so one extreme move cannot dominate.

    Args:
        payer: The verified payer address the record belongs to.
        store: The commit store, read-only throughout.

    Returns:
        The record. A payer with no finalized commits gets a record with zero commits and no
        scores, which is a fact about that payer rather than an error; the ROUTE is what turns it
        into a 404.
    """
    settlements = [
        unsettled_commit(record)
        if (outcome := store.outcome(record.trial_id)) is None
        else settle_commit(record, outcome)
        for record in store.public_records(payer)
    ]
    tallies = Counter(settlement.status for settlement in settlements)
    briers = [s.brier for s in settlements if s.status == "settled" and s.brier is not None]
    markouts = [
        max(-MARKOUT_CAP_BPS, min(MARKOUT_CAP_BPS, s.chosen_markout_bps))
        for s in settlements
        if s.status == "settled" and s.chosen_markout_bps is not None
    ]
    return AgentRecord(
        payer=payer,
        commits=len(settlements),
        settled=tallies["settled"],
        pending=tallies["pending"],
        unscored=tallies["UNSCORED"],
        avg_brier=sum(briers) / len(briers) if briers else None,
        # Rounded to the nearest integer with ties to even, matching the single rounding the
        # markout law itself performs, so the average and the per-event legs cannot round apart.
        capped_avg_markout_bps=round(sum(markouts) / len(markouts)) if markouts else None,
        # Never derived, never conditional. See the class docstring: there is no live record that
        # could truthfully claim skill, so there is no expression here to get wrong.
        qualified=False,
    )
