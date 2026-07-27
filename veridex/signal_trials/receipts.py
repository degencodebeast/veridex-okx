"""Two-phase journaled commit store — the durable half of the settlement-atomic paid commit.

Six directories, and the split between them is the whole design:

``slots/``
    One file per ``(payer, trial_id)`` DECISION SLOT. This is the state machine, and it is
    durable and state-bearing rather than a transient lock — a process that dies holding one
    leaves the state behind on purpose, because the state is the only record of how far the
    payment got. States: ``in_flight``, ``settle_attempted``, ``quarantined``,
    ``finalized(receipt_id)``.
``staged/``
    One file per staging id, holding the payer's verbatim request. Never readable by any free
    read. A staged row is a commitment that has been *received*, not one that has been *paid*.
``journal/``
    One file per staging id, written only after a settlement RETURNED SUCCESS, carrying the
    transaction hash. The journal is the durable proof that money moved; finalization is
    bookkeeping over it, and that ordering is what makes a crash recoverable.
``finalized/``
    One file per receipt id. The only rows any free read may serve.
``outcomes/``
    One file per TRIAL id, holding what the market did plus the provenance it was derived from.
    Event level, so one file serves every participant in the trial. A ``settled`` or ``UNSCORED``
    outcome is TERMINAL and may never be rewritten — see :meth:`ReceiptStore.record_outcome`.
``settlements/``
    One file per receipt id, holding the participant-level join of a finalized commit to its
    trial's outcome. An immutable append: the row is a cache of a recomputable derivation, which
    is why nothing downstream is allowed to trust it in place of the two artifacts it came from.

Three rules govern the slot's lifetime, and every one of them exists to make a specific double
charge impossible:

* **A slot is RELEASED only where no payment can exist** — a validation 4xx, or a settlement
  that returned a definitive failure. Those are the two cases with a *proof* that no money
  moved.
* **``finalized`` is RETAINED PERMANENTLY**, as the idempotency pointer. Deleting it on success
  would let a later retry bearing a fresh signature see "no slot", pass every check, and settle
  a second time for the same commitment. The pointer *is* the anti-double-charge mechanism; the
  receipt is just what it points at.
* **``quarantined`` is RETAINED** until an operator resolves it by hand. It means a settlement
  was attempted and its outcome is UNKNOWN — the facilitator may hold a real payment, and the
  only way to ask is by a transaction hash this state does not have. It is never auto-deleted,
  never served, and it blocks every future settle for that slot.

The asymmetry between the last two bullets and the first is the point of the module. An
exception from ``settle`` is **not evidence of failure**; it is absence of evidence. Deleting on
absence of evidence is how a paid commit becomes an unpaid one.

**Write order, and it is this module's obligation as a writer:** the payload is written before
the state that advertises it, everywhere the two are separate. Staging precedes the attempt
marker; the journal precedes finalization; the finalized payload precedes the slot transition
that publishes it. A crash in that order leaves a payload nothing points at, which the
reconciler completes or sweeps. The reverse order leaves a pointer to a payload that does not
exist, which nothing can repair.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel

from veridex.chain.anchor import run_manifest_hash
from veridex.signal_trials.challenge_spec import CanonicalSignal, evidence_hash, visible_at_decision
from veridex.signal_trials.okx_client import BAR_MS, HISTORICAL_CANDLES_PATH
from veridex.signal_trials.spot_markout import spot_markout

#: The slot states, as runtime values. ``SlotState`` is erased at runtime, so membership tests
#: need this alongside it.
SlotState = Literal["in_flight", "settle_attempted", "quarantined", "finalized"]
SLOT_STATES: Final[frozenset[str]] = frozenset({"in_flight", "settle_attempted", "quarantined", "finalized"})

#: States in which NO settle call may ever be made for the slot, because an earlier payment for
#: it MAY already have settled. This set is what makes a post-quarantine retry unable to
#: double-charge, and it is deliberately a named constant rather than two inline comparisons:
#: adding a fifth indeterminate state must extend the refusal by construction.
INDETERMINATE_STATES: Final[frozenset[str]] = frozenset({"settle_attempted", "quarantined"})

#: The trial mode a PAID commit is permitted to bind to (frozen spec section 11: paid external
#: commits are live-only). Spelled here rather than imported from ``live`` because ``live``
#: imports THIS module, so the dependency only runs one way. ``test_the_live_mode_constant_matches
#: _the_trial_module`` is what keeps the two copies equal.
LIVE_TRIAL_MODE: Final[str] = "live"

#: How each field of the payer's canonical commit body is recovered from a finalized record,
#: as ``body field -> stored record key``.
#:
#: All but one are the same name. ``trial_id`` is not: the body carries the PAYER'S spelling of
#: the id and the record is keyed on the RESOLVED one, and resolution is explicitly permitted to
#: canonicalize between them (see the slot-acquisition comment in ``payments.py``). The body hash
#: was taken over what the payer sent, so re-deriving it from the resolved id would report a
#: tamper on every honest receipt the first time resolution changed a character.
#:
#: **This mapping is coupled to ``CommitRequest``'s shape**, because the staged hash is over the
#: whole dumped request. A field added there and not here makes every receipt fail ``body_hash``
#: — fail-closed, and loud, but still wrong;
#: ``test_the_body_derivation_covers_every_field_a_commit_request_can_carry`` pins the two
#: together so the obligation cannot be missed while editing either one.
COMMIT_BODY_FIELDS: Final[dict[str, str]] = {
    "trial_id": "committed_trial_id",
    "p_follow_profitable": "p_follow_profitable",
    "methodology_version": "methodology_version",
}

#: The commit-time facts the receipt's manifest hash BINDS, sealed at finalization.
#:
#: The payer's probability and methodology are deliberately absent: the manifest carries
#: ``body_hash``, so it binds the commitment BY REFERENCE. That keeps the two checks separable — a
#: rewritten probability is ``body_hash``'s finding and a rewritten payment is the manifest's,
#: rather than one compound verdict reported under two names.
COMMIT_MANIFEST_FIELDS: Final[tuple[str, ...]] = (
    "receipt_id",
    "trial_id",
    "committed_trial_id",
    "payer",
    "body_hash",
    "payment_tx_hash",
    "committed_at_ms",
    "commit_deadline_ms",
    "trial_mode",
)

#: The COMMIT-TIME checks :func:`verify_receipt` reports, in report order. Each reads facts the
#: receipt itself carries, so each is always decidable.
VERIFY_COMMIT_CHECKS: Final[tuple[str, ...]] = ("body_hash", "manifest", "deadline_respected", "live_mode")

#: The SETTLEMENT-TIME checks, reported after the commit-time ones in the same map. Each
#: re-derives one half of §7's persisted settlement provenance:
#:
#: ``bar_version``
#:     The recorded ``(bar, bar_ms)`` is one of the §5.1 frozen pairs. A season uses ONE bar and
#:     never mixes them, so a width that does not belong to its own label is not a settlement this
#:     law produced.
#: ``law_version``
#:     The outcome was produced under the law this build implements. A record settled under an
#:     older law is not wrong, but it is not re-derivable HERE, and saying so is the honest report.
#: ``evidence_equality``
#:     The outcome row's sealed evidence re-derives its own hash, is a well-formed canonical
#:     signal with no future field, and is filed under the trial it names. This is the "identical
#:     evidence" half of §9's Fair-Play claim: every participant in a trial was scored against the
#:     same frozen, pre-decision evidence, and that evidence is still the evidence.
#:
#:     **What it does NOT bind, stated so the name cannot be over-read:** it does not tie the
#:     RECEIPT to the trial. The finalized receipt row carries no evidence hash of its own — adding
#:     one would change what ``stage`` writes, which is H4.1's sealed shape — so a receipt whose
#:     resolved ``trial_id`` was rewritten is ``manifest``'s finding, not this one's. ``manifest``
#:     binds the resolved trial id, so the join IS covered; it is covered somewhere else.
#: ``outcome_source``
#:     **The only check that re-derives what the law OUTPUT, and the reason it carries three
#:     obligations rather than one.** The other three verify the law's METADATA — which bar, which
#:     law version, which evidence — and a receipt can satisfy all of them while stating a result
#:     the law never produced. So this one re-derives the RESULT:
#:
#:     * the close boundary: ``close_ts == ts_open + bar_ms``, landing in §7's half-open window
#:       ``[T, T + bar)``, with ``observation_lag`` equal to the distance from ``T``;
#:     * the law's outputs: ``entry`` is the sealed evidence's ``trigger_price``, both markout legs
#:       and ``follow_profitable`` recompute from ``(entry, future, cost_bps)`` through the SAME
#:       :func:`~veridex.signal_trials.spot_markout.spot_markout` the settler ran; and
#:     * the fetch's source: the recorded ``chain_index`` is the chain the sealed evidence names,
#:       and ``source_endpoint`` is the one candles endpoint this build settles from.
#:
#:     Three obligations under one name is a COST, and it is paid deliberately: the eight-key map
#:     is frozen, so a ninth name is not available, and the alternative — leaving the law's outputs
#:     unchecked — is what let a forged ``follow_profitable`` verify with all eight checks passing.
#:     A reader of a ``fail`` here must consult the row to learn which of the three moved.
VERIFY_OUTCOME_CHECKS: Final[tuple[str, ...]] = ("bar_version", "law_version", "evidence_equality", "outcome_source")

#: A single check's verdict. THREE values, and the third is not a hedge.
#:
#: Every commit-time check reads facts the receipt carries, so none of them is ever ``pending``.
#: The outcome checks have nothing to re-derive until a SETTLED outcome exists, and reporting them
#: as ``fail`` before then would say a receipt failed verification when the market has simply not
#: reached its horizon — the same conflation between a finding and a state that this module
#: refuses everywhere else.
#:
#: **``pending`` does not distinguish "not yet" from "never".** An UNSCORED trial reports the same
#: ``pending`` as one whose horizon has not arrived, because no outcome verdict was computed in
#: either case and the frozen triple has no fourth value. The distinction is carried by the
#: participant settlement's ``status``, which the verify response serves beside the checks. Stated
#: here because it is the one thing this value cannot say for itself.
CheckState = Literal["pass", "fail", "pending"]

#: The settlement law every recorded outcome is stamped with. §7 requires the law version to be
#: persisted with every settlement, and this is the value ``law_version`` re-derives against.
#:
#: Bumping it is a deliberate act with a cost: every ALREADY-RECORDED outcome then reports
#: ``law_version: fail`` until it is re-derived under the new law. That is the point of versioning
#: it — a silently changed settlement rule would leave old and new records indistinguishable while
#: meaning different things.
SETTLEMENT_LAW_VERSION: Final[str] = "spot_markout_close_boundary_v1"

#: The stored outcome row's two halves, as ``field -> stored key`` in row order.
#:
#: :data:`OUTCOME_FIELDS` is exactly :class:`TrialOutcome`'s field list — what a reader rebuilds
#: the outcome FROM. :data:`OUTCOME_PROVENANCE_FIELDS` is what the outcome was DERIVED from, and
#: is what the four outcome checks re-derive over.
#:
#: The two are disjoint, and ``test_the_stored_outcome_row_covers_the_outcome_and_its_provenance``
#: pins that. A field claimed by both would be written twice into one flat row, and the later
#: write would silently repair the earlier one — so a tamper on the derived half could be masked
#: by the source half, or the reverse, and neither check could be trusted to have read what it
#: names.
OUTCOME_FIELDS: Final[tuple[str, ...]] = (
    "trial_id",
    "status",
    "entry",
    "future",
    "close_ts_ms",
    "observation_lag_ms",
    "follow_markout_bps",
    "fade_markout_bps",
    "follow_profitable",
)
OUTCOME_PROVENANCE_FIELDS: Final[tuple[str, ...]] = (
    "bar",
    "bar_ms",
    "chain_index",
    "source_endpoint",
    "t0_ms",
    "horizon_ms",
    "cost_bps",
    "settlement_ts_open_ms",
    "evidence",
    "evidence_hash",
    "law_version",
)

#: The fields a stored participant settlement carries. Same coupling role as the two above.
SETTLEMENT_FIELDS: Final[tuple[str, ...]] = (
    "receipt_id",
    "trial_id",
    "payer",
    "p_follow_profitable",
    "action",
    "brier",
    "chosen_markout_bps",
    "status",
)

#: An outcome's lifecycle state. ``settled`` and ``UNSCORED`` are both TERMINAL — see
#: :meth:`ReceiptStore.record_outcome`.
TrialStatus = Literal["pending", "settled", "UNSCORED"]

#: The statuses no later write may replace. ``UNSCORED`` is an ANSWER — "the window closed with no
#: settlement candle" — not the absence of one, so it is as final as a settled price.
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"settled", "UNSCORED"})

#: The display stance derived from a payer's own probability (§8.1). Never submitted, never a
#: Veridex-originated recommendation (§3.8) — a rendering of what the caller themselves sent.
Action = Literal["FOLLOW", "FADE", "ABSTAIN"]

#: How long after staging a row with NO attempt marker becomes sweepable. Derived, not picked:
#: the commit window is 300_000 ms (frozen spec section 11), and 600_000 ms of grace covers
#: clock skew and a slow request, matching the plan's ``FETCH_GRACE_MS``. A row younger than
#: this may still belong to a request that is mid-flight, and sweeping it would delete a
#: commitment out from under a live caller.
STALE_AFTER_MS: Final[int] = 300_000 + 600_000

_SLOTS_DIRNAME = "slots"
_STAGED_DIRNAME = "staged"
_JOURNAL_DIRNAME = "journal"
_FINALIZED_DIRNAME = "finalized"
_OUTCOMES_DIRNAME = "outcomes"
_SETTLEMENTS_DIRNAME = "settlements"


@dataclass(frozen=True)
class CommitRecord:
    """One FINALIZED paid commitment. The only shape a free read may serve.

    Frozen because it is the thing a receipt attests to: a record that could be mutated after
    its ``body_hash`` was computed would let the receipt and its subject drift apart silently.

    ``body_hash`` is over the payer's canonical request, so H4.2 can re-derive it and report
    tampering without trusting this row's own fields. ``commit_deadline_ms`` and ``trial_mode``
    are carried rather than re-looked-up for the same reason: the trial may be long closed by
    the time anyone verifies, and a check that had to consult a live trial store could not
    verify a historical receipt at all.
    """

    receipt_id: str
    trial_id: str
    payer: str
    p_follow_profitable: float
    methodology_version: str | None
    body_hash: str
    payment_tx_hash: str
    committed_at_ms: int
    commit_deadline_ms: int | None
    trial_mode: str | None


@dataclass(frozen=True)
class TrialOutcome:
    """What the market did on one trial. EVENT level — no participant data, ever.

    The absence of a payer, a probability or a Brier is the design, not an omission. A trial has
    ONE outcome and many participants; putting a probability here would force one participant's
    view into the event's record, and the second committer's Brier would have nowhere to live.
    :class:`ParticipantSettlement` is the join that carries the participant half.

    Frozen because a settled outcome is evidence. An outcome that could be mutated after agents
    were told what happened is not a record of what happened.

    ``status`` is three-valued and the two non-settled values are DIFFERENT FACTS carrying
    IDENTICAL metrics:

    ``pending``
        The answer is not knowable yet. The settlement candle closes in ``[T, T + bar)`` and may
        still be forming, or may have closed and not yet be fetchable.
    ``UNSCORED``
        The window and its fetch grace both expired and no eligible candle was found (§7, §12).
        Never interpolated, never a guessed price.

    Every metric field is ``None`` in both, so a consumer — or a test — that branches on
    ``future is None`` cannot tell them apart and will render a permanently unscored trial as one
    still awaiting its result. Branch on ``status``.
    """

    trial_id: str
    status: TrialStatus
    entry: float
    future: float | None
    close_ts_ms: int | None
    observation_lag_ms: int | None
    follow_markout_bps: int | None
    fade_markout_bps: int | None
    follow_profitable: bool | None


@dataclass(frozen=True)
class OutcomeProvenance:
    """What a :class:`TrialOutcome` was DERIVED FROM — §7's persisted settlement provenance.

    Separate from the outcome rather than folded into it, and the separation is what makes
    verification possible at all. The outcome states a result; this states the inputs that
    produced it, so :func:`verify_receipt` can re-derive the first from the second instead of
    comparing a stored value against a stored copy of itself. §7 requires exactly these to be
    persisted with every settlement: the bar and its width, the close boundary, the observation
    lag, and the law version.

    ``settlement_ts_open_ms`` is the OPEN time of the candle the trial settled against — OKX's
    ``ts`` — and is ``None`` when nothing settled. It is never a placeholder zero: a zero would
    re-derive as a close at ``bar_ms`` past the epoch and ``outcome_source`` would report a tamper
    on an honestly unsettled trial.

    ``evidence`` is the trial's frozen decision-time payload, carried verbatim so
    ``evidence_equality`` can re-hash it. Storing only the hash would leave the check comparing a
    digest against itself, which passes for any payload at all.

    ``chain_index`` and ``source_endpoint`` are §7's persisted SOURCE: which chain the settlement
    candles were fetched for, and from which endpoint. They record WHAT THE FETCH ACTUALLY USED and
    are therefore supplied by the caller that performed it — never re-derived here from
    ``evidence``, which is the whole point. A ``chain_index`` copied off the sealed evidence at
    write time would agree with it by construction, so ``outcome_source``'s chain comparison could
    only ever catch a later tamper and never a settlement fetched from the wrong chain. This is the
    same reasoning :func:`~veridex.signal_trials.live.settle_trial` already applies to re-selecting
    the settlement candle rather than back-deriving its open time.

    **What ``source_endpoint`` can and cannot be verified against, stated rather than implied.** Its
    PATH half is checkable — this build settles from exactly one candles endpoint, and
    ``outcome_source`` requires the recorded value to end with it. Its HOST half is not: no stored
    artifact independently knows which host is legitimate, so the host is recorded and PUBLISHED
    rather than re-derived. A reader can see which endpoint served a settlement; a verifier cannot
    attest that it was the right one.
    """

    bar: str
    bar_ms: int
    chain_index: str
    source_endpoint: str
    t0_ms: int
    horizon_ms: int
    cost_bps: int
    settlement_ts_open_ms: int | None
    evidence: dict[str, Any]
    evidence_hash: str
    law_version: str = SETTLEMENT_LAW_VERSION


@dataclass(frozen=True)
class SettledTrial:
    """One recordable settlement: the outcome, together with what produced it.

    The pair travels as one value because recording either half without the other produces an
    artifact nothing can check — an outcome with no provenance cannot be re-derived, and
    provenance with no outcome states nothing.
    """

    outcome: TrialOutcome
    provenance: OutcomeProvenance


@dataclass(frozen=True)
class ParticipantSettlement:
    """One finalized commit joined to one trial outcome. PARTICIPANT level.

    This is where a payer's probability meets the event's result, and it is per-participant by
    construction: two payers on the same trial produce two of these, with different Briers and
    different chosen legs, from ONE :class:`TrialOutcome`.

    ``action`` is derived from ``p_follow_profitable`` and is known the moment the commit is
    finalized — it does not wait on the market, so it is populated in every status.

    ``brier`` and ``chosen_markout_bps`` are ``None`` unless ``status == "settled"``, and the
    status is the ONLY field that separates a ``pending`` row from an ``UNSCORED`` one.
    """

    receipt_id: str
    trial_id: str
    payer: str
    p_follow_profitable: float
    action: Action
    brier: float | None
    chosen_markout_bps: int | None
    status: TrialStatus


@dataclass(frozen=True)
class VerifyReport:
    """The verdict on one finalized receipt: every check, and what each one found.

    ``checks`` is a mapping rather than named fields because a caller's job is to display or audit
    them uniformly, not to branch per check. The key set is
    :data:`VERIFY_COMMIT_CHECKS` + :data:`VERIFY_OUTCOME_CHECKS`, in that order — the commit-time
    checks first because they are the ones that are always decidable.

    Every value is ``"pass"``, ``"fail"`` or ``"pending"``. There is no "error" state and no
    exception path for a receipt that fails: **a tampered receipt is a successfully computed report
    that says so.** Reporting a verification failure as an API failure would make tampering
    indistinguishable from an outage, which is the one confusion a trust surface cannot afford.
    See :data:`CheckState` for what ``pending`` does and does not distinguish.
    """

    receipt_id: str
    checks: dict[str, CheckState]


def canonical_body_hash(body: BaseModel | dict[str, Any]) -> str:
    """Hash a commit body so that "identical canonical body" is a decidable question.

    Frozen spec section 11 makes idempotency turn on canonical-body identity: the same payer and
    trial with an IDENTICAL body returns the original receipt, and a DIFFERENT body is a 409
    that writes nothing. That comparison has to be insensitive to things the payer does not
    control the spelling of — key order, insignificant whitespace, ``0.60`` versus ``0.6`` —
    and sensitive to every value that is part of the commitment.

    Sorted-key JSON with no whitespace over the JSON-mode dump gives exactly that: field order
    and formatting vanish, while every value survives. ``mode="json"`` rather than the default
    so the digest is over the same representation that crossed the wire.

    Args:
        body: The commit request, as a pydantic model or an already-dumped mapping.

    Returns:
        The hex sha256 digest of the canonical serialization.
    """
    payload = body.model_dump(mode="json") if isinstance(body, BaseModel) else dict(body)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def committed_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the canonical commit body a finalized row attests to.

    Rebuilt from the fields the receipt SERVES — via :data:`COMMIT_BODY_FIELDS` — rather than from
    a second stored copy of the request, and that choice is the whole strength of the check. If the
    row carried its own verbatim body and the hash were taken over that, rewriting the
    ``p_follow_profitable`` a reader is actually shown would leave the hash intact and the
    verifier would report ``pass`` on a receipt that serves a probability nobody committed to.
    Deriving from the served fields means every value a consumer can see is a hashed input.

    Args:
        payload: The stored finalized row, verbatim.

    Returns:
        The body as it would have to have been for this row's ``body_hash`` to be correct.
    """
    return {field: payload.get(source) for field, source in COMMIT_BODY_FIELDS.items()}


def commit_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the commit-time manifest for a finalized row.

    Total by construction — every field is read with ``.get`` and nothing is coerced — because
    this runs on the money path inside :meth:`ReceiptStore.finalize_from_journal`, AFTER a
    settlement has succeeded. A manifest builder that could raise on an odd field would turn a
    paid commit into an unfinalized one.

    The same function serves the writer and the verifier, over the same stored shape, so the two
    cannot normalize differently. Two separate field lists would drift, and the drift would
    surface as ``manifest: fail`` on receipts nobody touched.

    Args:
        payload: The finalized row. ``manifest_hash`` itself is never an input — it is the OUTPUT
            of hashing this — so it is absent from :data:`COMMIT_MANIFEST_FIELDS`.

    Returns:
        The manifest, ready for :func:`~veridex.chain.anchor.run_manifest_hash`.
    """
    return {field: payload.get(field) for field in COMMIT_MANIFEST_FIELDS}


def _rehash_reproduces(rehash: Callable[[], str], sealed: object) -> bool:
    """Return whether a canonical re-hash reproduces ``sealed``, treating an unhashable value as no.

    Guards ONE check's re-derivation, and that scope is the point. A stored value nested past the
    recursion budget defeats the canonical serializer, and the honest reading of that is narrow: the
    check whose input could not be serialized did not re-derive, so it fails. The three checks that
    do not read that value are unaffected and must still report their own real result — a receipt
    whose ``payer`` cannot be re-hashed has not thereby been shown to serve a rewritten probability,
    and saying so would discard three answers the code successfully computed.

    ``RecursionError`` only. Every other exception is left to propagate, because every other
    exception here would be the service failing rather than the row being unhashable, and reporting
    that as ``fail`` would tell a holder their receipt is invalid when what broke was the check.

    **Why that is complete, and what it depends on.** ``TypeError`` is not the risk: the payload came
    out of ``json.loads``, so every value in it has a type ``json.dumps`` accepts. The risk at a
    canonical ``json.dumps(...).encode("utf-8")`` pair is the ``ValueError`` FAMILY, and the values
    that trigger it are ones ``json.loads`` produces every day — a lone surrogate (``"\\ud800"``),
    ``NaN``, an integer past ``sys.get_int_max_str_digits()``, a circular reference. None of them can
    arise HERE, but not for a reason about the payload's type:

    * **Lone surrogates** are escaped rather than emitted because both serializers keep
      ``ensure_ascii=True``. Under ``ensure_ascii=False`` the ``dumps`` still succeeds and the
      ``.encode("utf-8")`` beside it raises ``UnicodeEncodeError``, which IS a ``ValueError``.
    * **NaN and the infinities** serialize to ``NaN``/``Infinity`` because both keep
      ``allow_nan=True``. Under ``allow_nan=False`` ``dumps`` raises ``ValueError``.
    * **Oversized integers** cannot reach here at all: ``loads`` and ``dumps`` share the same digit
      limit, so a literal that would defeat the encoder already defeated the decoder upstream.
    * **Circular references** cannot arise from a freshly parsed document.

    So this guard's completeness rests on FOUR unwritten default arguments at TWO call sites —
    :func:`canonical_body_hash` here, and :func:`~veridex.chain.anchor.run_manifest_hash` in
    ``veridex/chain/anchor.py``, which is in ANOTHER SUBSYSTEM and frames its canonical form purely
    as a determinism concern. ``ensure_ascii=False`` is the most common edit made to a canonical-JSON
    serializer — RFC 8785 specifies it — and making it at either site would raise straight through
    this guard and answer 500 for a receipt that could have been reported on honestly, which is the
    tampering-versus-outage conflation this module refuses everywhere else.
    ``test_a_value_the_serializer_would_reject_UNDER_OTHER_FLAGS_still_reaches_a_verdict`` pins all
    four combinations, so that edit turns a test red instead of turning a verdict into an outage.

    Args:
        rehash: The canonical re-derivation, deferred so the failure is caught rather than raised
            while the surrounding report is being built.
        sealed: The digest stored on the row, compared verbatim. Typed ``object`` rather than
            ``Any``: the row is untrusted, so this may be any JSON value at all, and ``object``
            says the only thing done with it is the comparison — ``Any`` would silently let a
            later edit call a string method on whatever a tamper put there.

    Returns:
        ``True`` when the re-derivation ran and matched.
    """
    try:
        return rehash() == sealed
    except RecursionError:
        return False


def _exact_int(value: Any) -> int | None:
    """Return ``value`` when it is EXACTLY an ``int``, else ``None``.

    ``type(value) is int`` rather than ``isinstance``, which would admit ``bool``: ``True`` would
    otherwise arithmetic as ``1``, so a field stored as ``true`` would be read as the millisecond 1
    or the bar width 1. A string, a float or an absent field is likewise not one of these
    quantities, and every caller turns ``None`` into ``fail`` — a value nothing can establish has
    not thereby been established.
    """
    return value if type(value) is int else None


def _finite_probability(value: Any) -> float:
    """Return ``value`` as a finite ``float``, refusing the non-finite.

    ``float()`` alone is not enough at a trust boundary that reads untrusted rows. ``NaN`` and the
    infinities are legal Python floats and ``json.loads`` produces them from the bare ``NaN``,
    ``Infinity`` and ``-Infinity`` literals, so a tampered row parses, coerces and Brier-scores as
    if it carried a real commitment. It then reaches the JSON RESPONSE renderer, which is
    RFC-compliant and refuses to emit it — a failure that happens after every handler has returned
    and therefore cannot be turned into an honest verdict by anything downstream.

    Raises:
        ValueError: ``value`` is not a number at all, or is not finite. ``ValueError`` rather than a
            type of its own because every caller already reads a ``ValueError`` from this module as
            "this stored row is corrupt", which is exactly what a non-finite probability is.
    """
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(
            f"stored commit carries a non-finite p_follow_profitable ({number!r}); a probability "
            "that is not a number is a corrupt row, not a commitment"
        )
    return number


def _epoch_ms(value: Any) -> int | None:
    """Return ``value`` when it is usable as an epoch-millisecond stamp, else ``None``.

    A timestamp is an exact integer and nothing else; see :func:`_exact_int` for why ``bool`` is
    excluded. A receipt whose timing nothing can establish has not been shown to be timely.
    """
    return _exact_int(value)


def _trial_status(value: object) -> TrialStatus:
    """Return ``value`` when it is a known outcome status, else raise.

    Enumerated rather than cast, for the same reason :meth:`ReceiptStore._require_known_state`
    enumerates slot states: an unrecognized status must never be interpreted, and in particular
    must never be read as ``pending`` — a stored row whose status is unreadable would then present
    as a trial still awaiting settlement.

    Raises:
        ValueError: ``value`` is not one of the three statuses.
    """
    if value == "pending":
        return "pending"
    if value == "settled":
        return "settled"
    if value == "UNSCORED":
        return "UNSCORED"
    raise ValueError(f"stored outcome carries an unknown status {value!r}; expected pending, settled or UNSCORED")


def _action(value: object) -> Action:
    """Return ``value`` when it is a known display action, else raise.

    Raises:
        ValueError: ``value`` is not one of the three §8.1 actions.
    """
    if value == "FOLLOW":
        return "FOLLOW"
    if value == "FADE":
        return "FADE"
    if value == "ABSTAIN":
        return "ABSTAIN"
    raise ValueError(f"stored settlement carries an unknown action {value!r}; expected FOLLOW, FADE or ABSTAIN")


def outcome_row(settled: SettledTrial) -> dict[str, Any]:
    """Flatten a settlement into the row that is stored on disk.

    ONE flat object rather than two nested ones, because every reader of it — the verifier, the
    typed read, and a human looking at the file — wants a field, not a half. The two halves stay
    distinguishable through :data:`OUTCOME_FIELDS` and :data:`OUTCOME_PROVENANCE_FIELDS`, which
    are disjoint, so flattening cannot let one half overwrite the other.

    Built by walking those tuples rather than by dumping the dataclasses, so the row's shape is
    stated in one place that a reader can check against. A field added to a dataclass and not to
    its tuple is dropped here and caught by
    ``test_the_stored_outcome_row_covers_the_outcome_and_its_provenance``.
    """
    row: dict[str, Any] = {field: getattr(settled.outcome, field) for field in OUTCOME_FIELDS}
    row.update({field: getattr(settled.provenance, field) for field in OUTCOME_PROVENANCE_FIELDS})
    return row


def outcome_from_row(row: dict[str, Any]) -> TrialOutcome:
    """Rebuild the :class:`TrialOutcome` half of a stored row.

    Coerces as it loads, which is why the VERIFIER does not use it — verification has to see the
    raw stored value, and a check fed a coerced one could not tell a stamp stored as a string from
    one stored as an integer. This is the typed READ path, whose caller wants a usable record.

    Raises:
        ValueError: The row's status is not one of the three, or a field it must carry is missing
            or is not the type it must be. Corruption is never quietly repaired into a default: a
            row that read as ``pending`` because its status was unparseable would present a
            destroyed settlement as a trial still awaiting one.
    """
    return TrialOutcome(
        trial_id=str(row["trial_id"]),
        status=_trial_status(row.get("status")),
        entry=float(row["entry"]),
        future=None if row.get("future") is None else float(row["future"]),
        close_ts_ms=_exact_int(row.get("close_ts_ms")),
        observation_lag_ms=_exact_int(row.get("observation_lag_ms")),
        follow_markout_bps=_exact_int(row.get("follow_markout_bps")),
        fade_markout_bps=_exact_int(row.get("fade_markout_bps")),
        follow_profitable=None if row.get("follow_profitable") is None else bool(row["follow_profitable"]),
    )


def settlement_row(settlement: ParticipantSettlement) -> dict[str, Any]:
    """Flatten a participant settlement into the row that is stored on disk."""
    return {field: getattr(settlement, field) for field in SETTLEMENT_FIELDS}


def settlement_from_row(row: dict[str, Any]) -> ParticipantSettlement:
    """Rebuild a :class:`ParticipantSettlement` from a stored row.

    Raises:
        ValueError: The row's status or action is not a known value, or a required field is
            missing.
    """
    return ParticipantSettlement(
        receipt_id=str(row["receipt_id"]),
        trial_id=str(row["trial_id"]),
        payer=str(row["payer"]),
        p_follow_profitable=float(row["p_follow_profitable"]),
        action=_action(row.get("action")),
        brier=None if row.get("brier") is None else float(row["brier"]),
        chosen_markout_bps=_exact_int(row.get("chosen_markout_bps")),
        status=_trial_status(row.get("status")),
    )


def _evidence_reproduces(evidence: object, sealed: object) -> bool:
    """Return whether ``evidence`` is a canonical signal that re-derives ``sealed`` and itself.

    TWO derivations, and the second is what makes the first mean anything. Re-hashing the stored
    evidence and comparing to the stored hash catches a rewritten payload. Comparing
    ``visible_at_decision`` of the rebuilt signal back against the stored payload catches the rest:
    a key the model does not declare (pydantic ignores extras, so it would not change the hash), a
    value that only survived because the model coerced it, and any forbidden evidence field, which
    ``visible_at_decision`` refuses outright. Either check alone leaves a payload a forger can
    edit without moving the digest.

    Total by construction. Every failure to RE-DERIVE is ``False``, because the row is untrusted
    input and "this could not be re-derived" is exactly what the ``fail`` verdict states. The
    boundary is that clause: ``ValueError`` (which pydantic's ``ValidationError`` subclasses),
    ``TypeError`` from expanding a mapping whose keys are not usable as keywords, and
    ``RecursionError`` from a payload nested past what the serializer can walk are the ways a
    re-derivation over parsed JSON fails. Anything outside that set propagates, because it would
    be the service failing rather than the row being unre-derivable, and answering ``fail`` to it
    would tell a holder their receipt is invalid when what broke was the check.
    """
    if not isinstance(evidence, dict):
        return False
    try:
        signal = CanonicalSignal(**evidence)
        return visible_at_decision(signal) == evidence and evidence_hash(signal) == sealed
    except (ValueError, TypeError, RecursionError):
        return False


def _exact_float(value: Any) -> float | None:
    """Return ``value`` as a ``float`` when it is a stored JSON NUMBER, else ``None``.

    The float twin of :func:`_exact_int`, and it refuses the same things for the same reason: a
    price stored as the STRING ``"0.0125"`` is a different artifact from one stored as a number,
    and coercing it would let a re-derivation succeed over a row the writer could not have
    produced. ``bool`` is excluded explicitly because it is an ``int`` subclass, so ``True`` would
    otherwise read as the price ``1.0``.

    An integer too large to be a float — a tampered row can carry one — raises ``OverflowError``
    rather than returning, so it is absorbed here: a value this reader cannot represent has not
    been shown to re-derive anything.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value)
    except OverflowError:
        return None


def _close_boundary_reproduces(row: dict[str, Any]) -> bool:
    """Return whether the stored close boundary re-derives from the stored settlement candle.

    Three relations, all of §7, and each catches something the others do not:

    * ``close_ts == ts_open + bar_ms`` — OKX's ``ts`` is the candle OPEN, so the close is one bar
      later.
    * ``observation_lag == close_ts - (t0 + horizon)`` — the displayed lag is not an independent
      number; it is the distance from the settlement target, and a lag that disagreed with the
      close would understate how late a settlement was.
    * ``0 <= lag < bar_ms`` — the half-open eligibility window. A candle a full bar or more late is
      not the bar that first closed after ``T``.

    Every input is required to be an exact ``int``. A missing or non-integer field is ``False``:
    a boundary nothing can re-derive has not been shown to re-derive.
    """
    close_ts = _exact_int(row.get("close_ts_ms"))
    ts_open = _exact_int(row.get("settlement_ts_open_ms"))
    lag = _exact_int(row.get("observation_lag_ms"))
    bar_ms = _exact_int(row.get("bar_ms"))
    t0_ms = _exact_int(row.get("t0_ms"))
    horizon_ms = _exact_int(row.get("horizon_ms"))
    if close_ts is None or ts_open is None or lag is None or bar_ms is None or t0_ms is None or horizon_ms is None:
        return False
    return close_ts == ts_open + bar_ms and lag == close_ts - (t0_ms + horizon_ms) and 0 <= lag < bar_ms


def _law_outputs_reproduce(row: dict[str, Any]) -> bool:
    """Return whether the row's SCORE-BEARING fields re-derive from independently recorded inputs.

    **This is the check the whole benchmark's central claim rests on**, and it exists because every
    other check verified the law's inputs and metadata while leaving its OUTPUT unexamined. A
    receipt could report all eight Fair-Play checks passing while ``follow_profitable`` had been
    flipped, moving a published Brier from 0.04 to 0.64 — the displayed score and the agent record
    materially rewritten, the proof surface reporting nothing.

    Two derivations, and the SEPARATION of their sources is what makes either mean anything:

    * ``entry`` is compared to the sealed evidence's ``trigger_price``. §7 makes the trigger price
      the entry, and the evidence is bound by its own hash under ``evidence_equality`` — so this
      ties the outcome's starting price to an artifact frozen at ``t0``, not to a stored copy of
      itself.
    * ``follow_markout_bps``, ``fade_markout_bps`` and ``follow_profitable`` are recomputed by
      running :func:`~veridex.signal_trials.spot_markout.spot_markout` over
      ``(entry, future, cost_bps)`` — the SAME function the settler ran, so the law has one
      implementation here as everywhere else, and a rounding rule that changed would move both
      sides together rather than reporting a false tamper.

    ``follow_profitable`` is compared with ``is`` rather than ``==``, so a row carrying the integer
    ``1`` where the writer stored ``true`` is a ``fail``: the two are equal in Python and are not
    the same artifact.

    **The boundary this check does NOT reach, stated rather than implied.** ``future`` is the only
    law input with no independent record — the settlement candle's close price is not attested by
    anything else on disk — so an adversary who rewrites ``future`` AND recomputes both legs and
    the verdict consistently produces a self-consistent row that re-derives perfectly. What is
    closed is the far commoner and far cheaper forgery: changing a RESULT without changing its
    inputs. Attesting ``future`` itself would need a signed candle from the venue, which §7 does not
    provide and this function cannot invent. ``settlement_ts_open_ms`` and the close boundary do pin
    WHICH candle was claimed, so the surviving forgery has to restate an entire coherent settlement
    rather than nudge one number.

    Total by construction, in the same sense as :func:`_evidence_reproduces`: every way the
    re-derivation can fail over untrusted parsed JSON is ``False``. ``ValueError`` covers a
    non-positive or ``NaN`` price refused by the law's own guard, ``OverflowError`` an infinite one
    reaching ``round()``, and ``TypeError`` a value the arithmetic cannot take at all.
    """
    evidence = row.get("evidence")
    if not isinstance(evidence, dict):
        return False
    entry = _exact_float(row.get("entry"))
    trigger_price = _exact_float(evidence.get("trigger_price"))
    future = _exact_float(row.get("future"))
    cost_bps = _exact_int(row.get("cost_bps"))
    if entry is None or trigger_price is None or future is None or cost_bps is None:
        return False
    if entry != trigger_price:
        return False
    try:
        markout = spot_markout(entry, future, cost_bps)
    except (ValueError, TypeError, OverflowError):
        return False
    return (
        _exact_int(row.get("follow_markout_bps")) == markout.follow_markout_bps
        and _exact_int(row.get("fade_markout_bps")) == markout.fade_markout_bps
        and row.get("follow_profitable") is markout.follow_profitable
    )


def _settlement_source_binds(row: dict[str, Any]) -> bool:
    """Return whether the recorded fetch source belongs to the trial the outcome is filed under.

    §7 requires the settlement's source to be persisted, and persisting it is only half the job:
    an operator who ran the settler with the wrong ``--chain-index`` fetched a DIFFERENT token's
    market and settled this trial against it, and every other check passes on the result. The
    sealed evidence names the chain the signal was observed on, so the recorded chain has an
    independent artifact to be compared against.

    ``source_endpoint`` is required to END WITH the one candles path this build settles from. The
    host half is deliberately not constrained — see :class:`OutcomeProvenance` for why nothing on
    disk can attest it — so this is the same kind of statement as ``bar_version``'s: the value is
    one this law could have produced, rather than one re-derived from a second source.

    Both halves are type-checked before they are compared. Two absent keys would otherwise both
    read ``None`` and compare equal, which is a pass for a row that recorded no source at all.
    """
    evidence = row.get("evidence")
    if not isinstance(evidence, dict):
        return False
    chain_index = row.get("chain_index")
    source_endpoint = row.get("source_endpoint")
    return (
        isinstance(chain_index, str)
        and chain_index == evidence.get("chain_index")
        and isinstance(source_endpoint, str)
        and source_endpoint.endswith(HISTORICAL_CANDLES_PATH)
    )


def _outcome_source_reproduces(row: dict[str, Any]) -> bool:
    """Return whether the settled outcome re-derives from everything recorded beside it.

    The conjunction of the three obligations :data:`VERIFY_OUTCOME_CHECKS` documents for
    ``outcome_source``: the close boundary, the law's outputs, and the fetch's source. Spelled as
    three named predicates rather than one expression so each can be read, tested and mutated on
    its own, and so a reader can see that all three have to hold.
    """
    return _close_boundary_reproduces(row) and _law_outputs_reproduce(row) and _settlement_source_binds(row)


def _slot_key(payer: str, trial_id: str) -> str:
    """Return the filesystem-safe digest naming the ``(payer, trial_id)`` slot.

    Hashed rather than concatenated because both halves are caller-supplied strings: a payer or
    trial id containing a path separator would otherwise escape the slots directory, and one
    containing the separator used to join them could collide with a different pair. The digest
    is over a length-prefixed join so ``("ab", "c")`` and ``("a", "bc")`` cannot map together.
    """
    return hashlib.sha256(f"{len(payer)}:{payer}|{len(trial_id)}:{trial_id}".encode()).hexdigest()


def receipt_id_for(staging_id: str) -> str:
    """Derive the receipt id a staging id finalizes to.

    Deterministic on purpose. Finalization happens on the request path OR later inside
    :meth:`ReceiptStore.reconcile`, and both must produce the SAME receipt id — a random id
    would make a crash-recovered receipt a different receipt, so the payer's original 200 would
    reference an id that never materialized.
    """
    return "rcpt_" + hashlib.sha256(f"receipt:{staging_id}".encode()).hexdigest()[:32]


class ReceiptStore:
    """Durable two-phase commit store with a journal and a reconciler.

    One store owns one directory tree. Every write is atomic per file — serialize, write to a
    temporary file in the same directory, ``fsync``, then ``os.replace`` — so a reader (the API
    serves the same tree a settler writes to) can never observe a half-written row.
    """

    def __init__(self, root: Path | str) -> None:
        """Create a store rooted at ``root``, creating the six subdirectories on demand.

        Args:
            root: The directory this store owns.
        """
        self.root = Path(root)
        self._slots = self.root / _SLOTS_DIRNAME
        self._staged = self.root / _STAGED_DIRNAME
        self._journal = self.root / _JOURNAL_DIRNAME
        self._finalized = self.root / _FINALIZED_DIRNAME
        self._outcomes = self.root / _OUTCOMES_DIRNAME
        self._settlements = self.root / _SETTLEMENTS_DIRNAME
        for directory in (self._slots, self._staged, self._journal, self._finalized, self._outcomes, self._settlements):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ atomic primitives

    @staticmethod
    def _write_atomic(path: Path, payload: dict[str, Any], *, fsync_dir: bool = False) -> None:
        """Write ``payload`` to ``path`` atomically, optionally fsyncing the directory too.

        ``fsync_dir`` is not decoration. ``os.replace`` makes the CONTENT visible atomically,
        but the directory entry itself can still be lost to a power failure until the directory
        is synced. The one transition where that distinction matters is the attempt marker: if
        it were lost, a crash past the settle call would be misread as "never settled" and a
        retry could charge again. Every other write is recoverable from the reconciler, so they
        do not pay the cost.
        """
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
            # Leave no half-written temp behind for the next writer or a directory listing.
            Path(tmp_name).unlink(missing_ok=True)
            raise
        if fsync_dir:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        """Load ``path`` as a JSON object, failing loudly if it is not one.

        THREE ways the parse can fail, and CPython reports them under unrelated exception trees. A
        malformed byte raises ``JSONDecodeError``, which IS a ``ValueError``. A well-formed but
        deeply nested document instead exhausts the recursion budget and raises ``RecursionError``,
        which is a ``RuntimeError`` and shares no ancestor with the first — so a caller written to
        absorb corruption as a ``ValueError`` absorbs the malformed row and is bypassed entirely by
        the nested one. An integer literal longer than ``sys.get_int_max_str_digits()`` raises a
        BARE ``ValueError`` with no type of its own, so neither named clause sees it. All three are
        the same fact about the row: these bytes do not yield a payload. Normalizing here rather
        than at each caller is what keeps that one fact under one exception, and the ``read_text``
        beside the parse is deliberately NOT inside the conversion — an ``OSError`` from the disk is
        a fact about the SERVICE and must stay distinguishable.

        The last clause is ``ValueError`` and not a named type BECAUSE the digit limit has no named
        type, and it is written as a clause rather than left to the caller deliberately: that case
        already reached a correct verdict, but only because ``ValueError`` happens to be what
        :func:`verify_receipt` absorbs — correct by coincidence of exception ancestry rather than by
        enumeration. Catching it here costs nothing in breadth, since the only statement inside the
        ``try`` is the parse and every ``ValueError`` out of ``json.loads`` is by construction a
        statement about the BYTES. Nothing that indicts the service is a ``ValueError``, so
        ``OSError`` still escapes untouched — ``test_an_OSError_from_the_READ_is_not_normalized_
        into_a_verdict`` is what holds that line.

        Raises:
            ValueError: ``path`` is unreadable as JSON — malformed, nested past what this
                interpreter can decode, or carrying a number it refuses to parse — or does not hold
                an object. Corruption is never reported as absence: a slot that reads as "missing"
                would be a slot that permits a fresh settle.
        """
        text = path.read_text(encoding="utf-8")
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(f"commit-store artifact {path.name} is not readable JSON") from error
        except RecursionError as error:
            raise ValueError(f"commit-store artifact {path.name} is nested too deeply to parse") from error
        except ValueError as error:
            # Ordered last on purpose: ``JSONDecodeError`` is a ``ValueError``, so its clause has to
            # come first or this one would swallow the malformed case and lose its message.
            raise ValueError(f"commit-store artifact {path.name} carries a number this reader cannot parse") from error
        if not isinstance(loaded, dict):
            raise ValueError(f"commit-store artifact {path.name} must hold a JSON object")
        return loaded

    @staticmethod
    def _require_known_state(state: object, source: str) -> str:
        """Return ``state`` when it is a known slot state, else raise.

        Raises:
            ValueError: ``state`` is outside :data:`SLOT_STATES`. An unrecognized state must
                never be treated as releasable or as settleable; refusing to interpret it at
                all is the only fail-closed reading.
        """
        if not isinstance(state, str) or state not in SLOT_STATES:
            raise ValueError(f"{source} carries an unknown slot state; expected one of {sorted(SLOT_STATES)}")
        return state

    def _slot_path(self, payer: str, trial_id: str) -> Path:
        """Return the path of the ``(payer, trial_id)`` slot file."""
        return self._slots / f"{_slot_key(payer, trial_id)}.json"

    def _iter_slots(self) -> list[dict[str, Any]]:
        """Read every slot record, validating each one's state on the way out."""
        records = []
        for path in sorted(self._slots.glob("*.json")):
            record = self._read_json(path)
            self._require_known_state(record.get("state"), f"slot artifact {path.name}")
            records.append(record)
        return records

    # ------------------------------------------------------------------ slot lifecycle

    def acquire_slot(self, payer: str, trial_id: str, *, now_ms: int) -> tuple[bool, str]:
        """Take the decision slot for ``(payer, trial_id)``, or report who already holds it.

        The create is ``O_CREAT | O_EXCL``, which is a single atomic filesystem operation: two
        concurrent requests for the same slot cannot both succeed, and the loser learns the
        winner's state rather than a lock-acquisition failure. That distinction is what lets a
        loser answer honestly — ``commit_in_flight`` versus ``payment_indeterminate`` versus the
        original receipt — instead of retrying into a double charge.

        Args:
            payer: The VERIFIED payer address. Never a caller-supplied one.
            trial_id: The resolved trial's id.
            now_ms: Creation timestamp for a newly created slot.

        Returns:
            ``(True, "in_flight")`` when this call created the slot, else ``(False, state)``
            with the existing state.
        """
        path = self._slot_path(payer, trial_id)
        record = {
            "payer": payer,
            "trial_id": trial_id,
            "state": "in_flight",
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
            "receipt_id": None,
            "staging_id": None,
        }
        encoded = json.dumps(record, indent=2, sort_keys=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            existing = self._read_json(path)
            return False, self._require_known_state(existing.get("state"), f"slot artifact {path.name}")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return True, "in_flight"

    def create_slot(
        self,
        payer: str,
        trial_id: str,
        *,
        state: str = "in_flight",
        created_at_ms: int | None = None,
        receipt_id: str | None = None,
    ) -> None:
        """Write a slot in an arbitrary state, overwriting any existing one.

        The recovery-drill and operator entry point: it is how a crash-orphaned slot is
        reproduced for a test and how an operator reconstructs one. Distinct from
        :meth:`acquire_slot`, which is the request path and must never overwrite.
        """
        self._require_known_state(state, "create_slot")
        stamp = 0 if created_at_ms is None else created_at_ms
        self._write_atomic(
            self._slot_path(payer, trial_id),
            {
                "payer": payer,
                "trial_id": trial_id,
                "state": state,
                "created_at_ms": stamp,
                "updated_at_ms": stamp,
                "receipt_id": receipt_id,
                "staging_id": None,
            },
        )

    def _transition_slot(self, payer: str, trial_id: str, state: str, **extra: Any) -> None:
        """Move an existing slot to ``state``, preserving its creation stamp."""
        self._require_known_state(state, "_transition_slot")
        path = self._slot_path(payer, trial_id)
        record: dict[str, Any] = self._read_json(path) if path.is_file() else {"payer": payer, "trial_id": trial_id}
        record.update(state=state, **extra)
        record.setdefault("created_at_ms", 0)
        record.setdefault("updated_at_ms", 0)
        record.setdefault("receipt_id", None)
        record.setdefault("staging_id", None)
        # The attempt marker is the one transition whose loss would be misread as
        # "never settled", so it alone syncs the directory entry as well as the content.
        self._write_atomic(path, record, fsync_dir=state == "settle_attempted")

    def release_slot(self, payer: str, trial_id: str) -> None:
        """Delete the slot. Legitimate ONLY where no payment can possibly exist.

        The two lawful callers are a validation 4xx (settlement was provably never reached) and
        a settlement that RETURNED a definitive failure. Releasing in any other circumstance —
        in particular on a raised settle, or after a successful one — reopens the slot to a
        fresh-signature retry and is a double charge.
        """
        self._slot_path(payer, trial_id).unlink(missing_ok=True)

    def slot_state(self, payer: str, trial_id: str) -> str | None:
        """Return the slot's state, or ``None`` when no slot exists."""
        path = self._slot_path(payer, trial_id)
        if not path.is_file():
            return None
        return self._require_known_state(self._read_json(path).get("state"), f"slot artifact {path.name}")

    def slot_receipt_id(self, payer: str, trial_id: str) -> str | None:
        """Return the receipt id a ``finalized`` slot points at, else ``None``."""
        path = self._slot_path(payer, trial_id)
        if not path.is_file():
            return None
        value = self._read_json(path).get("receipt_id")
        return value if isinstance(value, str) else None

    def slot_count(self) -> int:
        """Return the number of slots of any state."""
        return len(list(self._slots.glob("*.json")))

    # ------------------------------------------------------------------ staging

    def stage(
        self,
        *,
        staging_id: str,
        trial_id: str,
        payer: str,
        body: BaseModel | dict[str, Any],
        staged_at_ms: int | None = None,
        commit_deadline_ms: int | None = None,
        trial_mode: str | None = None,
    ) -> None:
        """Record a received commitment that has NOT been paid for. Never readable.

        Written before the attempt marker and before any settle call, so the payload always
        exists by the time anything can point at it.

        ``trial_id`` is the RESOLVED trial, which is what the record binds to; ``body`` is the
        payer's verbatim request, which is what the body hash is taken over. In production the
        two agree by construction, because the trial was resolved FROM the body — they are
        separate parameters because the record must bind to what was resolved, not to what was
        claimed.

        Args:
            staging_id: ``sha256`` of the payment payload; unique per payment.
            trial_id: The resolved trial's id.
            payer: The verified payer address.
            body: The payer's commit request.
            staged_at_ms: Staging timestamp; drives sweep eligibility. Defaults to 0, which
                makes a row immediately sweepable and is safe only because a real caller always
                supplies the clock.
            commit_deadline_ms: The trial's deadline, carried so a receipt stays verifiable
                after the trial is gone.
            trial_mode: The trial's mode, carried for the same reason.
        """
        self._write_atomic(
            self._staged / f"{staging_id}.json",
            {
                "staging_id": staging_id,
                "trial_id": trial_id,
                "payer": payer,
                "body": body.model_dump(mode="json") if isinstance(body, BaseModel) else dict(body),
                "body_hash": canonical_body_hash(body),
                "staged_at_ms": 0 if staged_at_ms is None else staged_at_ms,
                "commit_deadline_ms": commit_deadline_ms,
                "trial_mode": trial_mode,
            },
        )

    def staged_row(self, staging_id: str) -> dict[str, Any] | None:
        """Return the staged row, or ``None`` when it is absent."""
        path = self._staged / f"{staging_id}.json"
        return self._read_json(path) if path.is_file() else None

    def staged_payers(self) -> list[str]:
        """Return the payers holding staged rows, in stable order. Diagnostics only."""
        return [str(self._read_json(p)["payer"]) for p in sorted(self._staged.glob("*.json"))]

    def delete_staged(self, staging_id: str) -> None:
        """Delete a staged row. Lawful only alongside :meth:`release_slot`."""
        (self._staged / f"{staging_id}.json").unlink(missing_ok=True)

    def mark_settle_attempted(self, staging_id: str) -> None:
        """Move the row's slot to ``settle_attempted``, durably, BEFORE the settle call.

        This is the single most important write in the module, and its ORDER is the reason. Once
        it is on disk, a process that dies anywhere past it — including inside the settle call,
        including after the facilitator moved real money — comes back to a slot that says "a
        settlement was attempted and I do not know how it ended". Without it, the same crash
        comes back to a slot that says "nothing happened", and the next retry charges again.

        Derives the slot key from the staged row rather than taking it as an argument, so the
        marker cannot be attached to a different slot than the one the row belongs to.

        Raises:
            KeyError: No staged row with that id. An attempt marker with no payload behind it
                would be a state nothing can reconcile.
        """
        row = self.staged_row(staging_id)
        if row is None:
            raise KeyError(f"cannot mark settle_attempted: no staged row {staging_id!r}")
        self._transition_slot(str(row["payer"]), str(row["trial_id"]), "settle_attempted", staging_id=staging_id)

    def count_pending(self) -> int:
        """Return the number of staged rows. Pending is never public."""
        return len(list(self._staged.glob("*.json")))

    # ------------------------------------------------------------------ journal

    def journal(self, staging_id: str, *, payer: str, tx_hash: str) -> None:
        """Record that settlement SUCCEEDED, with the transaction that proves it.

        Written after settle returns success and before finalization, so the durable proof
        exists before anything depends on it. If the process dies here, the reconciler finds a
        journal entry with no finalized record and completes it — the settlement is never
        re-attempted, because it is known to have happened.
        """
        self._write_atomic(
            self._journal / f"{staging_id}.json",
            {"staging_id": staging_id, "payer": payer, "tx_hash": tx_hash},
        )

    def journal_len(self) -> int:
        """Return the number of unconsumed journal entries."""
        return len(list(self._journal.glob("*.json")))

    def finalize_from_journal(self, staging_id: str) -> str:
        """Materialize the finalized record from the staged row and its journal entry.

        Order within this method is load-bearing: the finalized PAYLOAD is written first, then
        the slot is transitioned to point at it, and only then are the staged row and journal
        entry deleted. A crash at any point leaves either a completable journal entry or a
        finalized record that is already correct — never a pointer to a payload that does not
        exist.

        Returns:
            The receipt id, derived deterministically so a reconciler-completed finalization
            produces the same id the request path would have.

        Raises:
            KeyError: The staged row or the journal entry is missing.
        """
        row = self.staged_row(staging_id)
        if row is None:
            raise KeyError(f"cannot finalize: no staged row {staging_id!r}")
        journal_path = self._journal / f"{staging_id}.json"
        if not journal_path.is_file():
            raise KeyError(f"cannot finalize: no journal entry {staging_id!r}")
        entry = self._read_json(journal_path)
        body = row["body"] if isinstance(row.get("body"), dict) else {}
        receipt_id = receipt_id_for(staging_id)
        record = {
            "receipt_id": receipt_id,
            "trial_id": row["trial_id"],
            # The payer's own spelling of the trial id, carried ALONGSIDE the resolved one because
            # the body hash was taken over it. Resolution may canonicalize, so the two are not
            # interchangeable, and dropping this would leave ``body_hash`` unverifiable.
            "committed_trial_id": body.get("trial_id"),
            "payer": row["payer"],
            "p_follow_profitable": body.get("p_follow_profitable"),
            "methodology_version": body.get("methodology_version"),
            "body_hash": row.get("body_hash"),
            "payment_tx_hash": entry.get("tx_hash"),
            "committed_at_ms": row.get("staged_at_ms"),
            "commit_deadline_ms": row.get("commit_deadline_ms"),
            "trial_mode": row.get("trial_mode"),
        }
        # Sealed here, in the one place a finalized row is created, and over the row itself rather
        # than over the arguments that built it — so the hash commits to exactly what gets
        # written. Reconciler-completed finalizations run this same line, so a crash-recovered
        # receipt is sealed identically to one finalized on the request path.
        record["manifest_hash"] = run_manifest_hash(commit_manifest(record))
        self._write_atomic(self._finalized / f"{receipt_id}.json", record)
        self._transition_slot(
            str(row["payer"]),
            str(row["trial_id"]),
            "finalized",
            receipt_id=receipt_id,
            staging_id=staging_id,
        )
        journal_path.unlink(missing_ok=True)
        self.delete_staged(staging_id)
        return receipt_id

    # ------------------------------------------------------------------ finalized reads

    def _record_from(self, payload: dict[str, Any]) -> CommitRecord:
        """Build a :class:`CommitRecord` from a stored finalized payload.

        Raises:
            ValueError: A field this record must carry is missing, or its probability is not a
                FINITE number. The finiteness clause is not decoration: Python's JSON decoder
                accepts the bare ``NaN`` literal, ``float("nan")`` succeeds, and the value then
                travels all the way to the response renderer — which refuses to emit it and turns a
                tampered row into a 500 from OUTSIDE any handler's reach. Refusing it here makes it
                what it is, a corrupt row, at the boundary where every caller already treats a
                corrupt row as a ``ValueError``.
        """
        return CommitRecord(
            receipt_id=str(payload["receipt_id"]),
            trial_id=str(payload["trial_id"]),
            payer=str(payload["payer"]),
            p_follow_profitable=_finite_probability(payload["p_follow_profitable"]),
            methodology_version=payload.get("methodology_version"),
            body_hash=str(payload.get("body_hash")),
            payment_tx_hash=str(payload.get("payment_tx_hash")),
            committed_at_ms=int(payload.get("committed_at_ms") or 0),
            commit_deadline_ms=payload.get("commit_deadline_ms"),
            trial_mode=payload.get("trial_mode"),
        )

    def _public_iter(self) -> list[CommitRecord]:
        """Every row a free read may serve — finalized only, and by CONSTRUCTION.

        The single gate for public visibility. Both public accessors go through it, so widening
        what is served means editing one function whose name says what it does, rather than
        adding a directory to one of several call sites and missing the others.
        """
        return [self._record_from(self._read_json(p)) for p in sorted(self._finalized.glob("*.json"))]

    def finalized(self) -> tuple[CommitRecord, ...]:
        """Return every finalized record."""
        return tuple(self._public_iter())

    def public_records(self, payer: str) -> list[CommitRecord]:
        """Return the finalized records for ``payer``. Staged and quarantined rows are absent."""
        return [record for record in self._public_iter() if record.payer == payer]

    def _finalized_path(self, receipt_id: str) -> Path | None:
        """Return the file ``receipt_id`` names, or ``None`` when the id could not name one.

        The verify route takes this id straight from a URL path segment, so this is the boundary
        where a traversal attempt has to stop: without the guard, ``../../secrets`` would resolve
        outside the finalized directory and any readable JSON file on the host would be served as
        a receipt.

        Refused with ``None`` rather than by raising — unlike
        :meth:`LiveTrialRepository._trial_path`, which raises. The difference is what the caller
        can honestly say: an id that cannot name a receipt simply has no receipt behind it, and
        "no such receipt" is both the truth and a 404. Raising would answer a probe with a 500,
        which distinguishes a rejected id from an unknown one for whoever is probing.

        The guard itself lives in :meth:`_segment_path`, shared with the outcome and settlement
        directories. One implementation rather than three copies, because three copies of a
        traversal guard is three places a later edit can strengthen two of.
        """
        return self._segment_path(self._finalized, receipt_id)

    def finalized_payload(self, receipt_id: str) -> dict[str, Any] | None:
        """Return the stored finalized row VERBATIM, or ``None`` when there is none.

        The verifier's read path. Verbatim rather than through :class:`CommitRecord` because
        verification has to see what is ON DISK: :meth:`_record_from` coerces as it loads, and a
        check fed coerced values could not tell a stamp stored as ``"1700000000000"`` from one
        stored as an integer. It is also what keeps the writer and the verifier hashing the same
        bytes — both sides read this shape.
        """
        path = self._finalized_path(receipt_id)
        if path is None or not path.is_file():
            return None
        return self._read_json(path)

    def record(self, receipt_id: str) -> CommitRecord | None:
        """Return one finalized record by receipt id, or ``None``."""
        payload = self.finalized_payload(receipt_id)
        return None if payload is None else self._record_from(payload)

    def finalized_for(self, payer: str, trial_id: str) -> CommitRecord | None:
        """Return the finalized record for ``(payer, trial_id)``, or ``None``.

        Resolved THROUGH the slot pointer rather than by scanning the finalized directory for a
        matching payer and trial. The slot is the single source of truth for "has this payer
        already committed to this trial", and idempotency has to be decided by the same artifact
        that blocks a second settle — otherwise the two could disagree, and the disagreement
        would surface as either a double charge or a lost receipt.
        """
        if self.slot_state(payer, trial_id) != "finalized":
            return None
        receipt_id = self.slot_receipt_id(payer, trial_id)
        return None if receipt_id is None else self.record(receipt_id)

    def count_finalized(self) -> int:
        """Return the number of finalized records."""
        return len(list(self._finalized.glob("*.json")))

    def count_all_public(self) -> int:
        """Return how many rows a free read would actually serve.

        Deliberately NOT ``count_finalized``, which counts a directory. This walks the public
        read path, so a read path widened to include staged, attempted or quarantined rows
        changes this number while the directory count stays put.
        """
        return len(self._public_iter())

    def count_all(self) -> int:
        """Return every commit ROW of any lifecycle state: staged plus finalized.

        Slots and journal entries are pointers, not rows, and are counted separately. This is
        the number the settlement-failure assertions use, because "zero records of any kind"
        has to mean the staged row was deleted too — not merely that nothing was published.
        """
        return self.count_pending() + self.count_finalized()

    def count_settle_attempted(self) -> int:
        """Return the number of slots that reached the settle call with an unknown outcome."""
        return sum(1 for record in self._iter_slots() if record["state"] == "settle_attempted")

    def count_quarantined(self) -> int:
        """Return the number of quarantined slots awaiting operator resolution."""
        return sum(1 for record in self._iter_slots() if record["state"] == "quarantined")

    # ------------------------------------------------------------------ settlement

    @staticmethod
    def _segment_path(directory: Path, identifier: str) -> Path | None:
        """Return the file ``identifier`` names inside ``directory``, or ``None`` if it could escape.

        Trial ids and receipt ids both arrive from URL path segments, so this is the boundary where
        a traversal attempt has to stop: without it, ``../../secrets`` would resolve outside the
        directory and any readable JSON file on the host would be served as an outcome.

        Refused with ``None`` rather than by raising, for the same reason
        :meth:`_finalized_path` refuses that way: an id that cannot name a record simply has no
        record behind it, and "nothing here" is both the truth and the answer a caller can act on.
        Raising would answer a probe with a 500 and thereby distinguish a rejected id from an
        unknown one for whoever is probing.
        """
        if not identifier or "/" in identifier or "\\" in identifier or "\0" in identifier:
            return None
        if identifier in {".", ".."}:
            return None
        return directory / f"{identifier}.json"

    def record_outcome(self, trial_id: str, settled: SettledTrial) -> None:
        """Record what the market did on ``trial_id``, with the provenance it was derived from.

        Args:
            trial_id: The trial this outcome belongs to. Carried separately from
                ``settled.outcome.trial_id`` because it is the STORAGE KEY, and refused when the
                two disagree: filing trial A's outcome under trial B would join every one of B's
                participants to A's market move, and every downstream artifact would still look
                ordinary.
            settled: The outcome and its provenance.

        Raises:
            ValueError: ``trial_id`` disagrees with the outcome's own; ``trial_id`` cannot name a
                file; or a TERMINAL outcome is already recorded and this one differs from it.

        **A terminal outcome is never rewritten.** ``settled`` and ``UNSCORED`` are both terminal
        and both refuse replacement, including replacement by each other and including demotion
        back to ``pending``. Agents were told what happened on this trial; moving the answer
        afterwards is the one thing a benchmark record cannot do, and UNSCORED is as much an answer
        as a price — "the window closed with no settlement candle" — rather than an absence of one.
        A window that could reopen would let a late-arriving candle rewrite a trial that was
        already published as unscored, which is §7's "never interpolate" rule applied to time.

        Re-recording an IDENTICAL outcome is a no-op, so a settler that runs twice over the same
        trial is not a conflict. Replacing a ``pending`` outcome is allowed and is the settler's
        whole job.
        """
        if settled.outcome.trial_id != trial_id:
            raise ValueError(
                f"refusing to file an outcome whose trial_id is {settled.outcome.trial_id!r} under the key "
                f"{trial_id!r}; the key and the payload name the same trial and may never disagree"
            )
        path = self._segment_path(self._outcomes, trial_id)
        if path is None:
            raise ValueError(f"trial_id {trial_id!r} cannot name an outcome record")
        row = outcome_row(settled)
        if path.is_file():
            existing = self._read_json(path)
            if existing == row:
                return
            if existing.get("status") in TERMINAL_STATUSES:
                raise ValueError(
                    f"trial {trial_id!r} is already settled terminally as {existing.get('status')!r}; "
                    "refusing to move a published outcome under the commitments already scored against it"
                )
        self._write_atomic(path, row)

    def outcome_payload(self, trial_id: str) -> dict[str, Any] | None:
        """Return the stored outcome row VERBATIM, or ``None`` when there is none.

        The verifier's read path, verbatim for the same reason :meth:`finalized_payload` is:
        verification has to see what is ON DISK, and a check fed coerced values could not tell a
        ``close_ts_ms`` stored as ``"1700000000000"`` from one stored as an integer.

        Raises:
            ValueError: The row exists and is unreadable as JSON. Corruption is never reported as
                absence — absence means "no settlement has been recorded", which is a false
                statement about a row that was recorded and then destroyed.
        """
        path = self._segment_path(self._outcomes, trial_id)
        if path is None or not path.is_file():
            return None
        return self._read_json(path)

    def outcome(self, trial_id: str) -> TrialOutcome | None:
        """Return the typed outcome for ``trial_id``, or ``None`` when none is recorded.

        ``None`` is "nothing has been recorded", which is a WEAKER statement than a recorded
        ``pending`` outcome: the first says no settler has run, the second says one ran and the
        answer is not knowable yet. A store that synthesized a pending outcome for an unknown trial
        would make the two indistinguishable and let an agent record count trials that do not exist.
        """
        payload = self.outcome_payload(trial_id)
        return None if payload is None else outcome_from_row(payload)

    def record_settlement(self, settlement: ParticipantSettlement) -> None:
        """Append one participant settlement, keyed by receipt id. Written once.

        Raises:
            ValueError: The receipt id cannot name a file, or a DIFFERENT settlement is already
                recorded under it. A participant's scored record is the artifact they paid for;
                rewriting it is the participant-level twin of rewriting an outcome.

        Re-recording an identical settlement is a no-op, so a recompute pass is idempotent.

        This row is a CACHE of a derivation that is recomputable from the finalized commit and the
        recorded outcome, and nothing downstream reads it in place of those two — see
        :func:`~veridex.signal_trials.live.build_agent_record`. If a record trusted this row, a
        single rewritten settlement would inflate an agent's standing while every primary artifact
        still verified.
        """
        path = self._segment_path(self._settlements, settlement.receipt_id)
        if path is None:
            raise ValueError(f"receipt_id {settlement.receipt_id!r} cannot name a settlement record")
        row = settlement_row(settlement)
        if path.is_file():
            existing = self._read_json(path)
            if existing == row:
                return
            raise ValueError(
                f"a different settlement is already recorded for receipt {settlement.receipt_id!r}; "
                "a participant's scored record is written once"
            )
        self._write_atomic(path, row)

    def settlement(self, receipt_id: str) -> ParticipantSettlement | None:
        """Return the recorded participant settlement for ``receipt_id``, or ``None``."""
        path = self._segment_path(self._settlements, receipt_id)
        if path is None or not path.is_file():
            return None
        return settlement_from_row(self._read_json(path))

    # ------------------------------------------------------------------ reconciler

    def reconcile(self, now_ms: int | None = None) -> None:
        """Bring the tree to a consistent state. Safe to run at startup and repeatedly.

        Startup is the important caller: a crash means the request path's ``finally`` never ran,
        so orphaned rows and slots exist precisely when nobody is around to notice them.

        The four branches, in the order they must run:

        1. **Journal entry with no finalized record -> COMPLETE it.** Settlement is known to
           have happened; finishing the bookkeeping is the only correct action, and it must run
           before quarantine so a completable row is never quarantined instead.
        2. **``settle_attempted`` with no journal entry -> QUARANTINE.** The facilitator may
           hold a real payment and the outcome is unknowable without a transaction hash. Never
           deleted, never served, and it blocks all future settles for the slot. Time-independent
           on purpose: waiting longer produces no new information, so there is nothing to wait
           for.
        3. **``in_flight``, no attempt marker, and stale -> RELEASE.** Provably never reached
           settle, so no payment can exist and the slot is safe to reopen.
        4. **Staged row with no attempt marker and stale -> SWEEP.** Same proof, applied to the
           payload.

        Branches 3 and 4 need a clock and are skipped for rows that are not yet stale; branches
        1 and 2 do not. ``now_ms=None`` means "use the real clock", which is what a startup
        caller wants.

        Args:
            now_ms: The current time. ``None`` reads the wall clock.
        """
        clock = int(time.time() * 1000) if now_ms is None else now_ms

        # 1. Complete every journalled settlement first.
        for path in sorted(self._journal.glob("*.json")):
            staging_id = path.stem
            if self.record(receipt_id_for(staging_id)) is None:
                self.finalize_from_journal(staging_id)

        # 2. Quarantine every attempted slot that has no journal entry behind it.
        for record in self._iter_slots():
            if record["state"] != "settle_attempted":
                continue
            marker_id = record.get("staging_id")
            if isinstance(marker_id, str) and (self._journal / f"{marker_id}.json").is_file():
                continue
            self._transition_slot(str(record["payer"]), str(record["trial_id"]), "quarantined")

        attempted_or_worse = {
            str(record.get("staging_id"))
            for record in self._iter_slots()
            if record["state"] in INDETERMINATE_STATES or record["state"] == "finalized"
        }

        # 3. Release stale in_flight slots that never reached the settle call.
        for record in self._iter_slots():
            if record["state"] != "in_flight":
                continue
            if clock - int(record.get("created_at_ms") or 0) > STALE_AFTER_MS:
                self.release_slot(str(record["payer"]), str(record["trial_id"]))

        # 4. Sweep stale staged rows that never reached the settle call.
        for path in sorted(self._staged.glob("*.json")):
            row = self._read_json(path)
            if path.stem in attempted_or_worse:
                continue
            state = self.slot_state(str(row["payer"]), str(row["trial_id"]))
            if state in INDETERMINATE_STATES or state == "finalized":
                continue
            if clock - int(row.get("staged_at_ms") or 0) > STALE_AFTER_MS:
                path.unlink(missing_ok=True)


def _outcome_checks(payload: dict[str, Any], store: ReceiptStore) -> dict[str, CheckState]:
    """Evaluate the four SETTLEMENT-TIME checks for the receipt row ``payload``.

    Three states are reachable here and each says something different:

    * **No outcome row, or one that is not ``settled``** -> four ``pending``. Nothing has been
      shown to be wrong; there is simply no settlement to re-derive. This covers a recorded
      ``UNSCORED`` outcome too, and that is a deliberate LIMIT rather than an oversight — see
      :data:`CheckState`, and note that the participant ``status`` served beside these checks is
      what distinguishes "not yet" from "never".
    * **An outcome row that EXISTS and is unreadable** -> four ``fail``. Same reading
      :func:`verify_receipt` already applies to an unreadable receipt row: a row that exists and
      re-derives nothing is exactly what ``fail`` means, and ``pending`` would claim no settlement
      had been recorded when one was recorded and then destroyed.
    * **A settled outcome** -> each check reports what IT found, independently.

    An unreadable RECEIPT row never reaches here (:func:`verify_receipt` short-circuits), because
    a row that cannot be read cannot name its trial — so no outcome could be looked up, and
    claiming its bar or law was wrong would be a finding nothing supports.

    Args:
        payload: The stored finalized receipt row, verbatim.
        store: The commit store, read-only.

    Returns:
        The four verdicts, in :data:`VERIFY_OUTCOME_CHECKS` order.
    """
    pending: dict[str, CheckState] = dict.fromkeys(VERIFY_OUTCOME_CHECKS, "pending")
    trial_id = payload.get("trial_id")
    if not isinstance(trial_id, str):
        # The receipt does not name a trial, so no outcome can be looked up. Nothing about a
        # settlement was examined; the ``manifest`` check is what reports the missing binding.
        return pending
    try:
        row = store.outcome_payload(trial_id)
    except ValueError:
        return dict.fromkeys(VERIFY_OUTCOME_CHECKS, "fail")
    if row is None or row.get("status") != "settled":
        return pending
    bar = row.get("bar")
    bar_ms = _exact_int(row.get("bar_ms"))
    verdicts: dict[str, bool] = {
        "bar_version": isinstance(bar, str) and bar_ms is not None and BAR_MS.get(bar) == bar_ms,
        "law_version": row.get("law_version") == SETTLEMENT_LAW_VERSION,
        # BOTH halves. The hash half catches a rewritten evidence payload. The trial-id half
        # catches a row whose FILENAME and whose CONTENT disagree — ``record_outcome`` refuses to
        # create one, but the adversary this whole report assumes can write to the store directly,
        # and a row filed under trial A while claiming trial B would otherwise re-hash perfectly.
        "evidence_equality": (
            row.get("trial_id") == trial_id and _evidence_reproduces(row.get("evidence"), row.get("evidence_hash"))
        ),
        "outcome_source": _outcome_source_reproduces(row),
    }
    return {check: "pass" if verdicts[check] else "fail" for check in VERIFY_OUTCOME_CHECKS}


def verify_receipt(receipt_id: str, store: ReceiptStore) -> VerifyReport:
    """Verify a finalized receipt's commit-time AND settlement-time claims. Never raises on a fail.

    EIGHT checks. The four commit-time ones are always evaluated and always decidable; the four
    settlement-time ones report ``pending`` until a settled outcome exists for the receipt's trial,
    and are delegated to :func:`_outcome_checks`.

    The commit-time four, each reporting only what it covers:

    ``body_hash``
        The canonical body rebuilt from the fields the receipt SERVES re-hashes to the hash taken
        over the payer's request at staging time. Catches any rewrite of the probability, the
        methodology, the payer's trial-id spelling, or the stored hash itself.
    ``manifest``
        The receipt's binding facts re-hash to the manifest hash sealed at finalization. Catches a
        rewritten payer, payment transaction, receipt id or resolved trial — none of which is
        inside the signed body, which is why this is a check of its own.
    ``deadline_respected``
        The recorded commit instant is STRICTLY before the recorded deadline. Frozen spec section
        11 makes the boundary instant late (``received_at >= commit_deadline`` is rejected), so the
        comparison is ``<``, not ``<=``.
    ``live_mode``
        The recorded mode is exactly :data:`LIVE_TRIAL_MODE`. Paid commits are live-only: a replay
        outcome is publicly knowable, so a paid "prediction" of one is not a prediction.

    The last two read facts the receipt CARRIES rather than consulting the live trial, and that is
    what makes a historical receipt verifiable at all — the trial it belongs to may have closed
    long ago, and a check that needed the trial store could only verify recent commitments.

    **A failure is a returned verdict, not an exception.** The only ``raise`` here is for a
    receipt that does not exist, because "no such receipt" is a different statement from "this
    receipt does not verify" and the route answers them with different status codes.

    An UNREADABLE row — bytes that are not JSON, JSON that is not an object, or JSON nested past
    what this interpreter can decode — reports the four COMMIT checks as ``fail`` and the four
    OUTCOME checks as ``pending``. The commit four fail because every one of their inputs is
    unreadable. The outcome four are ``pending`` because the row that would name this receipt's
    trial is gone, so no settlement was examined at all and there is no finding to report about
    one; the four ``fail``s beside them are what carry the verdict. It is neither an exception nor
    a 404, and both of those were considered:

    * Not an exception, because a row that exists and re-derives nothing is precisely what ``fail``
      means. Letting it escape makes the route answer 500, and a 500 says "this service is
      broken" when the truth is "this receipt does not verify" — the same conflation between an
      outage and a finding that this function is built to avoid everywhere else. This module
      already reasons this way for an absent ``manifest_hash`` and for absent timestamps.
    * Not a ``KeyError``, because that maps to 404 and :meth:`ReceiptStore._read_json` fixes the
      principle that corruption is never reported as absence. A destroyed receipt and a receipt
      that never existed are different facts, acted on differently, and must not share an answer.

    Only ``ValueError`` is absorbed, and only here. An ``OSError`` — an unreadable disk, a
    permissions fault — is left to propagate: that genuinely IS the service being broken rather
    than a statement about the receipt, and a 500 is the honest answer to it. The tolerance is also
    scoped to this function: :meth:`ReceiptStore.finalized_payload` and :meth:`ReceiptStore.record`
    still raise, because the payment path reads a ``None`` from ``record`` as "the slot points at a
    receipt with no record behind it", which would be the wrong diagnosis on the money path.

    A row can also defeat the canonical serializer AFTER parsing cleanly, and that case is answered
    per-check rather than here. :func:`_rehash_reproduces` fails the one check whose input could not
    be re-derived and leaves the other three to report what they actually found — collapsing it to
    four ``fail``s would be a LESS honest report than this function can produce, since three of the
    checks completed. Guarding at the two re-hashes rather than around the whole report is what
    makes that attribution possible, and it is also why no catch belongs on the route: a route-level
    catch cannot see which check was affected, so it could only ever answer all-or-nothing, and it
    would swallow the ``OSError`` this paragraph exists to preserve.

    Args:
        receipt_id: The receipt to verify. Arrives from a URL path segment on the free verify
            route, and is resolved through :meth:`ReceiptStore.finalized_payload`, which refuses
            an id that could escape the finalized directory.
        store: The commit store, read-only throughout.

    Returns:
        The report. Its ``receipt_id`` is the id that RESOLVED to a stored row — not the row's
        self-reported one, which a tamper could have rewritten and which the ``manifest`` check
        covers.

    Raises:
        KeyError: No FINALIZED row answers to ``receipt_id``. A staged row is a commitment that
            was received and not yet paid for, and a quarantined slot is a settlement whose
            outcome is unknown; neither is a receipt, and reporting checks over one would let an
            unpaid commitment be quoted as a verified one.
    """
    try:
        payload = store.finalized_payload(receipt_id)
    except ValueError:
        # The row EXISTS — the file is there — but nothing in it can be re-derived. Every COMMIT
        # check fails because every commit check's input is unreadable, which is a verdict about
        # the receipt and not an error in the service. The outcome checks are pending because the
        # trial id they would join on is among the bytes that were destroyed.
        unreadable: dict[str, CheckState] = {
            **dict.fromkeys(VERIFY_COMMIT_CHECKS, "fail"),
            **dict.fromkeys(VERIFY_OUTCOME_CHECKS, "pending"),
        }
        return VerifyReport(receipt_id=receipt_id, checks=unreadable)
    if payload is None:
        raise KeyError(f"no finalized receipt {receipt_id!r}; pending and quarantined rows are not receipts")

    committed_at_ms = _epoch_ms(payload.get("committed_at_ms"))
    commit_deadline_ms = _epoch_ms(payload.get("commit_deadline_ms"))
    verdicts: dict[str, bool] = {
        "body_hash": _rehash_reproduces(lambda: canonical_body_hash(committed_body(payload)), payload.get("body_hash")),
        "manifest": _rehash_reproduces(
            lambda: run_manifest_hash(commit_manifest(payload)), payload.get("manifest_hash")
        ),
        "deadline_respected": (
            committed_at_ms is not None and commit_deadline_ms is not None and committed_at_ms < commit_deadline_ms
        ),
        "live_mode": payload.get("trial_mode") == LIVE_TRIAL_MODE,
    }
    checks: dict[str, CheckState] = {check: "pass" if verdicts[check] else "fail" for check in VERIFY_COMMIT_CHECKS}
    checks.update(_outcome_checks(payload, store))
    return VerifyReport(receipt_id=receipt_id, checks=checks)
