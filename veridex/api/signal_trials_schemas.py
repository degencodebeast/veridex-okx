"""Wire schemas for the signal-trials API surface.

The first four models were frozen by the implementation plan at H1.2. The remaining five
are frozen at H4.3, now that the settlement path exists and there is something truthful
for them to carry: :class:`TrialOutcomeModel`, :class:`TrialResponse`,
:class:`CommitReceiptResponse`, :class:`AgentRecordResponse` and
:class:`VerifyReceiptResponse`.

The constraints below are not decoration; each one is a claim boundary:

* ``p_follow_profitable`` is bounded to ``[0, 1]`` because it is scored as a
  probability. The bound also rejects ``NaN`` and the infinities, since every
  comparison against ``NaN`` is false — which matters because Python's JSON decoder
  accepts the ``NaN`` literal, and an unbounded float would be Brier-scored as if it
  were a real commitment.
* ``trial_mode`` admits only ``"live"``. Frozen spec §11 restricts paid external
  commits to live trials: replay outcomes are publicly knowable, so advertising a
  replay trial for discovery would invite a paid "prediction" of a known result.
* ``season_status`` admits only the three PUBLISHED statuses. ``not_built`` is a
  health state describing a directory with no season in it, and can never be the
  status of a season document that exists.

**This module imports nothing from ``signal_trials``, and cannot.**
``veridex.signal_trials.live`` imports :class:`CommitRequest` from here, so the
dependency runs one way only. That is why :class:`TrialOutcomeModel` is a hand-written
mirror of the :class:`~veridex.signal_trials.receipts.TrialOutcome` dataclass rather than
a generated one, and why the mirror is held to the dataclass by
``test_the_outcome_model_mirrors_the_dataclass_FIELD_FOR_FIELD`` — nothing else can
notice a field added on one side and not the other, and H5.1's frontend types mirror
these shapes EXACTLY.

**Every settlement-bearing field is nullable, and the nulls are not defaults.** A trial
with no recorded outcome carries ``outcome: null``, which is a different claim from an
outcome whose ``status`` is ``"pending"``: the first says nothing has been computed, the
second says a settlement attempt ran and the answer is not knowable yet. The same
distinction runs through ``brier`` and ``chosen_markout_bps`` — a zero there would read
as a real result rather than as the absence of one.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CommitRequest(BaseModel):
    """A paid benchmark commitment: one probability, bound to one trial."""

    trial_id: str
    p_follow_profitable: float = Field(ge=0, le=1)
    methodology_version: str | None = None


class SignalTrialsRowModel(BaseModel):
    """One agent's standing in a season.

    ``avg_brier`` and ``capped_avg_markout_bps`` are nullable on purpose: an agent
    with no settled trials has no score, and a zero there would read as a real result.
    """

    agent_id: str
    qualified: bool
    avg_brier: float | None
    capped_avg_markout_bps: int | None
    active_decisions: int
    active_coverage: float
    unscored: int
    is_control: bool


class SignalTrialsSeasonResponse(BaseModel):
    """The published season document served by ``GET /signal-trials/season``.

    ``combo`` is the chain x bar selection the season was built under. It is typed
    ``dict[str, Any]`` rather than the plan's bare ``dict``: ``mypy --strict`` requires
    type arguments for generics, and this is the minimum parameterization that
    satisfies it without narrowing what a caller may pass (``PKT-DEC-C16``).
    """

    season_id: str
    season_status: Literal["qualified", "exploratory", "no_season"]
    combo: dict[str, Any]
    sample_size: int
    rows: list[SignalTrialsRowModel]


class OpenTrialResponse(BaseModel):
    """The currently open live trial, as free discovery (frozen spec §11).

    Carries only ``visible_at_decision`` evidence plus the hash that binds it. The
    ``evidence`` mapping is typed ``dict[str, Any]`` for the same ``--strict`` reason
    as ``combo`` above (``PKT-DEC-C16``); its leakage tiers are enforced upstream by
    the ChallengeSpec canonicalizer, not by this wire model.
    """

    trial_id: str
    trial_mode: Literal["live"]
    t0_ms: int
    commit_deadline_ms: int
    evidence: dict[str, Any]
    evidence_hash: str


class TrialOutcomeModel(BaseModel):
    """The EVENT-level settlement of one trial. No participant data appears here.

    Mirrors :class:`~veridex.signal_trials.receipts.TrialOutcome` field for field. The
    absence of any payer, probability or Brier is the point: two agents who committed
    opposite probabilities against this event share exactly this outcome, and their
    records differ only in the participant join that :class:`CommitReceiptResponse`
    carries.

    ``status`` is three-valued and the two non-settled values are NOT interchangeable.
    ``pending`` means the settlement candle may still be forming or may not yet be
    fetchable; ``UNSCORED`` means the window and its fetch grace both expired without
    one. Every metric field is ``None`` in BOTH states, so a consumer that branched on
    ``future === null`` would render an unscored trial as one still awaiting its result.
    Branch on ``status``.
    """

    trial_id: str
    status: Literal["pending", "settled", "UNSCORED"]
    entry: float
    future: float | None
    close_ts_ms: int | None
    observation_lag_ms: int | None
    follow_markout_bps: int | None
    fade_markout_bps: int | None
    follow_profitable: bool | None


class TrialResponse(BaseModel):
    """One trial: its decision-time evidence, the hash binding it, and its outcome.

    The evidence half is byte-for-byte what :class:`OpenTrialResponse` serves, because a
    trial's terms do not change when it settles — a reader must be able to compare a
    receipt against the same evidence the committer saw.

    ``outcome`` is ``None`` when NO outcome has been recorded for this trial, which is a
    weaker statement than a recorded ``pending``: nothing has been computed at all.
    Synthesizing a pending outcome here would publish a settlement state no settler
    produced.
    """

    trial_id: str
    trial_mode: Literal["live"]
    t0_ms: int
    commit_deadline_ms: int
    evidence: dict[str, Any]
    evidence_hash: str
    outcome: TrialOutcomeModel | None


class CommitReceiptResponse(BaseModel):
    """One finalized paid commitment, joined to its trial's outcome.

    The commit half (``body_hash``, ``payment_tx_hash``, ``committed_at_ms``,
    ``commit_deadline_ms``, ``trial_mode``) is what a verifier re-derives, so it is served
    rather than hidden: a receipt whose binding facts are not visible cannot be audited
    against the check map beside it. All of it is already public — the payer address is
    the record's identity, and the transaction is on a public chain.

    ``action`` is DERIVED from ``p_follow_profitable`` (§8.1 bands), never submitted, so
    the displayed stance and the committed confidence cannot contradict each other. It is
    a display of the caller's own probability and not a Veridex recommendation (§3.8).

    ``brier`` and ``chosen_markout_bps`` are ``None`` unless ``status == "settled"``.

    ``status`` COLLAPSES two states that :class:`TrialResponse` is careful to keep
    apart, and the collapse is stated here because that model teaches the reader to expect the
    distinction. A commitment against a trial with NO outcome recorded at all reads ``"pending"``,
    identically to one against a recorded ``pending`` outcome — both carry ``brier`` and
    ``chosen_markout_bps`` as ``null``, and the two are indistinguishable on this wire.
    ``"pending"`` is the honest word for both, because nothing has been settled; it is stated
    rather than left absent because the commitment itself is real and was paid for. What it does
    NOT assert is that a settler has run. A consumer needing that distinction reads the trial's
    ``outcome`` field, which is ``null`` in the first case and populated in the second.

    ``commit_deadline_ms`` and ``trial_mode`` are nullable HERE and not on
    :class:`OpenTrialResponse` or :class:`TrialResponse`, and those are the only two field names in
    this surface whose annotation differs between models. The divergence is faithful: this model is
    rendered from the STORED commit row and passes both through uncoerced, so ``null`` means that
    row lacks the key. A well-formed receipt always carries both. ``null`` is therefore not
    "optional, render a dash" — it is a corruption or tamper signal, and the ``deadline_respected``
    and ``live_mode`` checks served in the very same response read ``fail`` beside it.

    ``trial_mode`` is widened to ``str`` for that reason alone and §11's live-only constraint still
    holds: a paid external commit can only ever have been taken against a live trial, so any value
    other than ``"live"`` is a finding about the stored row rather than a variant to switch on.
    """

    receipt_id: str
    trial_id: str
    payer: str
    p_follow_profitable: float
    methodology_version: str | None
    action: Literal["FOLLOW", "FADE", "ABSTAIN"]
    status: Literal["pending", "settled", "UNSCORED"]
    brier: float | None
    chosen_markout_bps: int | None
    committed_at_ms: int
    commit_deadline_ms: int | None
    trial_mode: str | None
    body_hash: str
    payment_tx_hash: str


class AgentRecordResponse(BaseModel):
    """One payer's live participant record, aggregated over FINALIZED commits only.

    ``qualified`` is ALWAYS ``False`` on a live record and is carried explicitly rather
    than omitted, so a consumer reads a stated ``false`` instead of inferring one from a
    missing key. §8.4 gates qualification on a ``qualified`` SEASON with ≥20 active
    decisions and ≥50% coverage; a live exhibition is none of those things, so there is
    no live record that could truthfully claim skill.

    ``avg_brier`` and ``capped_avg_markout_bps`` are ``None`` until something settles. A
    zero Brier is a PERFECT score, so defaulting either to zero would publish a result
    where there is none.
    """

    payer: str
    commits: int
    settled: int
    pending: int
    unscored: int
    avg_brier: float | None
    capped_avg_markout_bps: int | None
    qualified: bool


class VerifyReceiptResponse(BaseModel):
    """The verdict on one receipt: every Fair-Play check, and the receipt it is about.

    ``checks`` is a mapping rather than named fields because a consumer's job is to
    display or audit them uniformly.

    **The key set is exactly these eight names, in this order:** ``body_hash``,
    ``manifest``, ``deadline_respected``, ``live_mode``, ``bar_version``,
    ``law_version``, ``evidence_equality``, ``outcome_source``. The four commit-time
    checks come first, then the four settlement-time ones. The ANNOTATION cannot say
    this — it publishes unconstrained ``str`` keys, and it is the one place in this
    surface where the contract is looser than the thing it describes. The reason is
    structural rather than an oversight: this module imports nothing from
    ``signal_trials`` and cannot, so it cannot reach the ``VERIFY_COMMIT_CHECKS`` and
    ``VERIFY_OUTCOME_CHECKS`` tuples that are the source of truth, and a hand-written
    ``Literal`` key type would be a second copy of those names carrying exactly the drift
    exposure this list would otherwise have. So the list is held to the tuples by
    ``test_the_published_check_names_match_the_frozen_tuples`` instead: a name added on
    the receipts side and not here fails the suite. A mirror reads its key names from
    HERE and declares nothing out of band.

    It is THREE-valued. ``pending`` is not a hedge: the
    four commit-time checks read facts the receipt itself carries and are therefore always
    decidable, while the four outcome checks have nothing to re-derive until a settled
    outcome exists. Reporting them as ``fail`` before then would tell a receipt holder
    their receipt is invalid because the market has not moved on yet.

    ``pending`` does NOT distinguish "not settled yet" from "settled UNSCORED and never
    will be" — the triple has no fourth value and this one is frozen. That distinction is
    carried by ``receipt.status``, which must be read alongside the checks. The boundary
    is stated here rather than left implied because it is the one thing this map cannot
    say for itself.

    ``receipt`` is ``None`` only when the stored row cannot be RENDERED — its bytes are
    unreadable, or they parse into values that cannot be coerced. An ABSENT receipt is
    never a ``null`` here: this route answers 404 on an id it holds no row for, before the
    receipt is rendered at all. A row that is present and unrenderable still answers 200
    carrying eight verdicts, because a destroyed receipt is a finding about the receipt and
    not an outage; rendering a partial receipt out of it would publish fields nothing can
    re-derive.
    """

    receipt_id: str
    checks: dict[str, Literal["pass", "fail", "pending"]]
    receipt: CommitReceiptResponse | None
