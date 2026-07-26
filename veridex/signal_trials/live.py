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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from veridex.api.signal_trials_schemas import CommitRequest
from veridex.signal_trials.challenge_spec import CanonicalSignal, evidence_hash, visible_at_decision
from veridex.signal_trials.receipts import CommitRecord, ReceiptStore, canonical_body_hash

#: The commit window, frozen by spec section 11: ``decision_window_seconds = 300``, so
#: ``commit_deadline = t0 + 300s``. A commit is rejected at ``received_at >= commit_deadline``,
#: exclusive at the top — the boundary instant is already too late.
DECISION_WINDOW_MS: Final[int] = 300_000

#: The only mode a paid external commit may be placed on. Spec section 11: replay outcomes are
#: publicly knowable, so a paid replay "prediction" would fake a record.
LIVE_MODE: Final[str] = "live"

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
