"""H3.5 — the season scorer: what a Signal Trials leaderboard is allowed to claim.

This module turns a sealed pack into a season: one row per agent, in rank order, plus the
qualification verdict that says whether any of it may be presented as a result. Everything here
exists to stop a leaderboard from making a claim its evidence does not support, and each rule below
names the specific false claim it prevents.

**The roster is CLOSED and is always scored in full.** Seven members — the three frozen contestants
and the four controls — are scored on every season, unconditionally, whatever the pack contains.
A roster that could silently shrink is a leaderboard that flatters whoever remains: drop the control
that happened to beat the field this season and every surviving row looks better, with nothing on
the page to show that anything was removed. :data:`FROZEN_ROSTER_IDS` is therefore a module
constant, not an argument, and :func:`score_season` has no parameter that can narrow it.

**Climatology is fed STRICTLY PRIOR outcomes, and the enforcement is HERE.**
``controls.prior_only_climatology`` is a pure function of the list it is handed — by construction it
cannot see the pack, and so by construction it cannot check that the list it received was honest.
That check is the caller's, which is this module, and it is a loop-ordering property: the outcome of
trial *k* is appended to the running prior AFTER trial *k*'s probability has been taken, never
before. A climatology fed the whole pack returns a perfectly ordinary probability — nothing about
the number, the row, or the resulting Brier score reveals the leak — and it would beat honest agents
for reasons that have nothing to do with skill, which invalidates every comparison drawn against it.
:func:`score_season` records what climatology actually saw on every trial in
``SeasonResult.debug_climatology_inputs`` so the property is measurable from outside rather than
merely asserted here.

**Qualification is a conjunction, and the season's own status is one of the conjuncts.**
``qualified = season_status == "qualified" AND active >= min_active AND coverage >= min_coverage``.
An exploratory season emits ZERO qualified rows no matter how well anyone scored, which is the same
honesty gate ``AgentRecord.qualified`` enforces in the payments lane: a small or unpredeclared
sample does not become a result by being impressive.

**UNSCORED is a third bucket, not a miss and not a hit.** A trial with no settlement candle leaves
the coverage denominator entirely. Counting it as a miss would punish agents for a data gap they had
no part in; counting it as a hit would reward them for it. It is reported separately, per row, so a
reader can see how much of the season was actually measured.

**The rank key is TOTAL.** avg Brier asc -> capped markout desc -> active count desc -> ``agent_id``.
The final key is what makes it total, and it is not decoration: without it two agents equal on the
first three keys are ordered by whatever the sort happened to see first, and the published
leaderboard reorders between two runs over identical data.

**DECLARED DEVIATION FROM THE FROZEN RULE — the rank key carries a FIFTH, LEADING term.**
Frozen plan line 633 states the ordering as those four terms and does NOT restrict it to qualified
rows. :func:`_rank_key` nonetheless sorts on ``(not qualified, <the frozen four>)``, placing every
qualified row ahead of every unqualified one. This is a deliberate departure, declared here at the
top of the module rather than left inside a private function, because a reader comparing this
scorer against the frozen text will otherwise find a term the text does not mention and have no way
to tell whether it was intended.

*Why it stays.* The qualification gate exists to say that an unqualified agent's score is not a
result. Applying the frozen key alone at the point of DISPLAY undoes that. Measured on this
repository's own 44-trial fixture: ``selective_calibrator`` — 0 active decisions, UNQUALIFIED —
scores a 0.2055 Brier, ahead of two QUALIFIED agents at 0.3905 and 0.9318, so the pure frozen
ordering renders an unmeasured agent above measured ones. The gate would be enforced in the
``qualified`` column and contradicted by the row order on the same page.

*What the deviation does NOT do.* It must not reorder anything WITHIN the qualified set, where the
frozen four terms remain the whole rule. Both halves are pinned by
``test_the_qualification_partition_is_a_declared_deviation_pinned_in_both_directions``: that the
partition is present and does observable work, and that the frozen ordering is untouched inside the
qualified set. Under the frozen thresholds the two orderings COINCIDE on qualified rows, so a test
that filters to qualified rows — which the plan's own mandated test does — cannot tell them apart,
and the term would otherwise be unpinned in BOTH directions.

**Markouts are capped PER EVENT.** The cap bounds what a single lucky trial can contribute; applying
it to the average instead would let one uncapped outlier carry a whole season and then be trimmed
only at the end, which is a different — and much weaker — claim.

Scope boundary, stated so its absence is not read as an oversight: this module verifies nothing
about the pack. Digest, bar provenance and format version are ``pack.load_pack``'s obligations and
are already discharged before a :class:`~veridex.signal_trials.pack.SealedPack` exists. What this
module refuses is a pack that is internally coherent but cannot be scored honestly — one declaring
``no_season``, or one carrying a diagnostic agent that would shadow a roster member.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from veridex.rank_guards import R3_R4_RANK_DENYLIST
from veridex.signal_trials.challenge_spec import CanonicalSignal
from veridex.signal_trials.contestants import (
    compute_ext,
    crowding_fader,
    flow_follower,
    selective_calibrator,
)
from veridex.signal_trials.controls import (
    always_fade,
    always_follow,
    assert_not_full_pack,
    neutral,
    prior_only_climatology,
)
from veridex.signal_trials.pack import SealedPack
from veridex.signal_trials.preflight import ComboSelection
from veridex.signal_trials.primitives import brier_score
from veridex.signal_trials.spot_markout import MarkoutResult, select_settlement_candle, spot_markout

#: The three frozen contestants (H3.3), in their declared order.
CONTESTANT_IDS: tuple[str, ...] = ("flow_follower", "crowding_fader", "selective_calibrator")

#: The four frozen controls (H3.4), in their declared order.
CONTROL_IDS: tuple[str, ...] = ("always_follow", "always_fade", "neutral", "climatology")

#: The closed roster every season scores. Held as a module constant rather than a parameter so no
#: caller can narrow it; :func:`score_season` reads it and offers no way to override it.
FROZEN_ROSTER_IDS: tuple[str, ...] = CONTESTANT_IDS + CONTROL_IDS

#: CLV and realized-execution field names that must never appear on a ranked season row.
#:
#: The R3/R4 execution half is IMPORTED from :mod:`veridex.rank_guards` rather than re-spelled here.
#: That module is the repo's canonical, frozen denylist and is deliberately neutral so both ranked
#: lanes can share it; a second hand-maintained copy would drift from it, and the copy that drifted
#: would be the one guarding this lane. The CLV half is added here because Signal Trials is scored on
#: spot markout and Brier alone: a season row carrying a CLV number would be presenting a metric from
#: the directional sports lane, measured against a closing line this lane does not have.
#:
#: Note one name that is DELIBERATELY not a contradiction: ``markout_bps`` is denied (it is the R4-A
#: post-trade markout of a REAL fill), while ``capped_avg_markout_bps`` — this lane's own field — is
#: the modelled markout of a hypothetical stance against a settlement candle. Different measurements,
#: different names, and the guard compares exact keys, so neither shadows the other.
CLV_RANK_DENYLIST: frozenset[str] = (
    frozenset(
        {
            "clv_bps",
            "clvBps",
            "avg_clv_bps",
            "total_clv_bps",
            "avg_window_clv_bps",
            "total_window_clv_bps",
        }
    )
    | R3_R4_RANK_DENYLIST
)

#: Display bands. ``p >= 0.60`` renders FOLLOW, ``p <= 0.40`` FADE, and everything strictly between
#: is ABSTAIN. Only FOLLOW and FADE are ACTIVE decisions.
_FOLLOW = "FOLLOW"
_FADE = "FADE"
_ABSTAIN = "ABSTAIN"


@dataclass(frozen=True)
class AgentSeasonRow:
    """One agent's standing in a season.

    ``avg_brier`` is ``None`` exactly when the agent had no scored trial at all, and
    ``capped_avg_markout_bps`` is ``None`` exactly when it took no ACTIVE decision. Both are
    nullable rather than zeroed because zero is a real and very good value in each case — a zero
    Brier is a perfect forecast — and emitting one for an agent that was never measured would put an
    unmeasured agent at the top of the leaderboard.

    ``unscored`` counts trials with no settlement candle. It is identical across every row of a
    season, because settlement is a property of the pack and not of the agent; it is carried per row
    anyway, since the row is the unit a reader actually sees.
    """

    agent_id: str
    is_control: bool
    qualified: bool
    avg_brier: float | None
    capped_avg_markout_bps: int | None
    active_decisions: int
    active_coverage: float
    unscored: int


@dataclass(frozen=True)
class SeasonResult:
    """A scored season: the verdict, the market it was scored under, and the ranked rows.

    ``debug_climatology_inputs`` maps a trial's 0-BASED chronological position to the probability
    climatology emitted there. It is not part of the published document; it exists so the
    no-lookahead law is checkable from OUTSIDE this module — the alternative is a property that can
    only be confirmed by reading the loop, which is exactly the kind of claim this program has
    repeatedly found to be right in behaviour and wrong in its stated reason.

    **The index base here is 0, and it differs from the base passed to the climatology guard.** The
    probe map is keyed by position (``probes[0]`` is the first trial) while
    ``controls.assert_not_full_pack`` takes the 1-based ordinal; the conversion happens at that call
    and nowhere else. Both bases are recorded in docstrings, matching how every other index base in
    this repository is recorded.

    Carrying a ``Mapping`` makes this dataclass comparable but NOT hashable, which is the right way
    round: ``==`` is what determinism is stated in, and nothing hashes a season.
    """

    season_id: str
    season_status: str
    combo: ComboSelection
    sample_size: int
    rows: tuple[AgentSeasonRow, ...]
    debug_climatology_inputs: Mapping[int, float]


@dataclass(frozen=True)
class DiagnosticAgent:
    """An additional agent supplied as a FIXED probability vector — data, never code.

    A vector rather than a callable, deliberately. A callable handed to the scorer could read the
    pack, and therefore could read outcomes it was not entitled to see, and nothing in its returned
    probability would reveal that it had — the same leak ``prior_only_climatology``'s purity exists
    to prevent. An inert vector cannot: it is fixed before scoring starts and is auditable by
    reading it.

    Diagnostics can only ADD rows. They cannot shadow a frozen roster member, cannot duplicate each
    other, and are always flagged ``is_control=True``, because ``is_control=False`` is a positive
    claim that a row is one of the three frozen contestants.

    KNOWN GAP, stated rather than implied: a CALLABLE passed as ``probabilities`` is REJECTED but
    not NAMED. Measured, both shapes raise: a callable as the whole vector gives ``TypeError:
    object of type 'function' has no len()``, and one hidden inside the tuple gives ``TypeError:
    '<=' not supported between instances of 'float' and 'function'``. Rejection is therefore total,
    but neither message identifies the offending ``agent_id``, which every other refusal in
    :func:`_validate_diagnostics` does. Deliberately left as-is for now rather than overlooked: the
    only constructor of :class:`PackWithDiagnostics` is test code — ``pack.load_pack`` returns a
    plain :class:`~veridex.signal_trials.pack.SealedPack` — so this improves a message on a path
    production cannot reach.

    Attributes:
        agent_id: The row's id. Must not collide with a roster member or another diagnostic.
        probabilities: One probability per trial, in the season's CHRONOLOGICAL order — the same
            order the probe map is keyed by, which is not necessarily the pack's file order.
    """

    agent_id: str
    probabilities: tuple[float, ...]


@dataclass(frozen=True)
class PackWithDiagnostics(SealedPack):
    """A sealed pack plus diagnostic agents.

    A distinct type rather than an extra parameter on :func:`score_season`, because the frozen
    signature takes the pack alone. Production never constructs one: ``pack.load_pack`` returns a
    plain :class:`~veridex.signal_trials.pack.SealedPack`, so a published season is scored over the
    frozen roster and nothing else.
    """

    diagnostic_agents: tuple[DiagnosticAgent, ...] = ()


@dataclass(frozen=True)
class _Trial:
    """One trial, resolved against settlement. ``markout is None`` means UNSCORED."""

    signal: CanonicalSignal
    ext: int | None
    markout: MarkoutResult | None

    @property
    def outcome(self) -> int | None:
        """``1`` if following cleared cost, ``0`` if it did not, ``None`` if unscored."""
        return None if self.markout is None else int(self.markout.follow_profitable)


def assert_no_clv_fields(row: Mapping[str, Any]) -> None:
    """Raise-only rank guard: reject a season row carrying a CLV or execution field.

    A strict no-op on a clean row, so wiring it into the rank path leaves every legitimate output
    unchanged. It is called on every row :func:`score_season` ranks — a guard that is merely
    available but never invoked from the path it protects is decoration, which is why
    ``maker.leaderboard.rank_makers`` calls its own guard the same way.

    **It raises ``ValueError``, where ``rank_guards.assert_no_r3r4_in_rank`` raises
    ``AssertionError``.** The frozen plan specifies ``ValueError`` for this guard and its mandated
    test asserts it, so the two are NOT interchangeable and a caller catching one does not catch the
    other. Stated here rather than left to be discovered.

    Args:
        row: One season row, as a mapping of field name to value. Typed ``Mapping`` rather than
            ``dict`` to match the neutral guard in :mod:`veridex.rank_guards`; every ``dict`` is
            accepted.

    Raises:
        ValueError: If any key of ``row`` is in :data:`CLV_RANK_DENYLIST`. The message names every
            offending field, so the refusal identifies which key was wrong rather than only that one
            was.
    """
    offending = CLV_RANK_DENYLIST.intersection(row)
    if offending:
        raise ValueError(f"CLV/execution field(s) {sorted(offending)} must never enter a season rank row")


def _stance(probability: float, follow_at: float, fade_at: float) -> str:
    """The display action a probability renders as.

    FOLLOW is tested first and both bounds are INCLUSIVE, matching the frozen ">=0.60 / <=0.40"
    spelling. The bands are checked for overlap in :func:`score_season` before this is ever called,
    so the ordering of the two tests here cannot silently decide a contested probability.
    """
    if probability >= follow_at:
        return _FOLLOW
    if probability <= fade_at:
        return _FADE
    return _ABSTAIN


def _settle(pack: SealedPack) -> tuple[_Trial, ...]:
    """Resolve every trial against settlement, in CHRONOLOGICAL order.

    Sorted by ``t0_ms`` with Python's stable sort, so a pack already written in order is unchanged
    and one that is not is still scored chronologically. This matters beyond tidiness: "strictly
    prior" is defined against the chronological order, and taking file order for it would make the
    same pack score differently after a re-seal that wrote its trials in a different sequence.

    A trial is UNSCORED when its token has no settlement series, or when no candle satisfies the
    close-boundary law. A malformed PRICE is not absorbed as unscored: ``spot_markout`` raises, and
    that exception propagates, because a non-positive entry price is a broken pack rather than a
    data gap and silently reporting it as "not measured" would hide it.
    """
    settled: list[_Trial] = []
    for signal in sorted(pack.trials, key=lambda trial: trial.t0_ms):
        series = pack.settlement.get(signal.token_address)
        if series is None:
            settled.append(_Trial(signal=signal, ext=None, markout=None))
            continue
        candle = select_settlement_candle(series, t0_ms=signal.t0_ms, horizon_ms=pack.meta.horizon_ms)
        markout = (
            None
            if candle is None
            else spot_markout(entry=signal.trigger_price, future=candle.close, cost_bps=pack.meta.cost_bps)
        )
        settled.append(_Trial(signal=signal, ext=compute_ext(series, signal.t0_ms), markout=markout))
    return tuple(settled)


def _roster_probabilities(trials: tuple[_Trial, ...], pack_len: int) -> tuple[dict[str, list[float]], dict[int, float]]:
    """Every frozen roster member's probability on every trial, plus the climatology probe map.

    **This is where the no-lookahead law is enforced.** The running prior is extended with trial
    *k*'s outcome only AFTER climatology has answered for trial *k*, so the list handed over on any
    trial holds outcomes from strictly earlier trials and nothing else. Only SETTLED trials
    contribute: an unscored trial produced no outcome, so there is nothing honest to add.

    ``assert_not_full_pack`` is called on every trial with ``position + 1`` — the 1-BASED
    chronological ordinal. The guard raises on ``prior_len >= trial_index``, and that comparison is
    coherent only 1-based: at 0-based position *n* a correctly-fed caller has exactly *n* strictly
    prior outcomes, so ``n >= n`` would reject every legitimate call and the guard would be a total
    rejector. The conversion happens here, at the call, and the probe map stays 0-based.

    Returns:
        A ``(probabilities, probes)`` pair: probabilities keyed by agent id, each a list in
        chronological order; probes keyed by 0-based trial position.
    """
    probabilities: dict[str, list[float]] = {agent_id: [] for agent_id in FROZEN_ROSTER_IDS}
    probes: dict[int, float] = {}
    prior_outcomes: list[int] = []

    for position, trial in enumerate(trials):
        probabilities["flow_follower"].append(flow_follower(trial.signal))
        probabilities["crowding_fader"].append(crowding_fader(trial.signal, trial.ext))
        probabilities["selective_calibrator"].append(selective_calibrator(trial.signal, trial.ext))
        probabilities["always_follow"].append(always_follow())
        probabilities["always_fade"].append(always_fade())
        probabilities["neutral"].append(neutral())

        assert_not_full_pack(prior_len=len(prior_outcomes), pack_len=pack_len, trial_index=position + 1)
        # `prior_only_climatology` is annotated `list[int]`; the running prior is already a list, and
        # a copy is passed so the control cannot retain a reference to a list that keeps growing.
        climatology_p = prior_only_climatology(list(prior_outcomes))
        probes[position] = climatology_p
        probabilities["climatology"].append(climatology_p)

        outcome = trial.outcome
        if outcome is not None:
            prior_outcomes.append(outcome)

    return probabilities, probes


def _validate_diagnostics(diagnostics: tuple[DiagnosticAgent, ...], trial_count: int) -> None:
    """Refuse a diagnostic set that could displace, duplicate or mis-cover a roster row.

    Every refusal names the offending ``agent_id``, because a diagnostic set is assembled by hand
    and "one of these is wrong" is not an actionable message.
    """
    seen: set[str] = set()
    for agent in diagnostics:
        if agent.agent_id in FROZEN_ROSTER_IDS:
            raise ValueError(
                f"diagnostic agent {agent.agent_id!r} collides with a frozen roster member; a "
                f"diagnostic may add a row but never shadow one"
            )
        if agent.agent_id in seen:
            raise ValueError(f"diagnostic agent {agent.agent_id!r} is declared twice; agent ids must be unique")
        seen.add(agent.agent_id)
        if len(agent.probabilities) != trial_count:
            raise ValueError(
                f"diagnostic agent {agent.agent_id!r} supplies {len(agent.probabilities)} probabilities "
                f"for a {trial_count}-trial season; a vector that does not cover the season would be "
                f"scored over a prefix of it and shown beside agents scored over all of it"
            )
        for position, probability in enumerate(agent.probabilities):
            if not 0.0 <= probability <= 1.0:
                raise ValueError(
                    f"diagnostic agent {agent.agent_id!r} emits {probability!r} at trial {position}, which "
                    f"is not a probability in [0, 1]; NaN and out-of-range values Brier-score to a "
                    f"defined but meaningless number"
                )


def _score_agent(
    agent_id: str,
    is_control: bool,
    probabilities: list[float],
    trials: tuple[_Trial, ...],
    *,
    season_status: str,
    min_active: int,
    min_coverage: float,
    markout_cap_bps: int,
    follow_at: float,
    fade_at: float,
) -> AgentSeasonRow:
    """Aggregate one agent's season into a row.

    Three populations, deliberately kept apart, because all three can present as "not counted":

    * every SETTLED trial is Brier-scored, INCLUDING the ones the agent abstained on — abstaining is
      a real forecast of 0.5 and scores the 0.25 floor;
    * only FOLLOW and FADE trials are ACTIVE, and only they contribute a markout;
    * UNSCORED trials are in none of the above and are counted separately.

    Coverage is ACTIVE over SCORED, never over the whole pack. A season with a settlement gap would
    otherwise punish every agent for a data outage none of them caused.

    **An agent with NO scored trial never qualifies**, and that conjunct is spelled out rather than
    left to follow from the others. At the frozen thresholds it is redundant — ``active >= 20``
    already implies twenty scored trials — but the thresholds are parameters, and at
    ``min_active=0, min_coverage=0.0`` every agent in an entirely unsettled season would otherwise
    qualify with ``avg_brier=None``: an unmeasured agent presented as a ranked result, which is the
    single outcome the qualification gate exists to prevent. It also establishes the invariant
    ``qualified => avg_brier is not None`` that the rank key and the published document both rely on
    (without it, sorting the qualified rows by Brier raises ``TypeError`` on the ``None``).
    """
    scored_probabilities: list[float] = []
    scored_outcomes: list[int] = []
    markouts: list[int] = []
    active = 0

    for probability, trial in zip(probabilities, trials, strict=True):
        outcome = trial.outcome
        if outcome is None or trial.markout is None:
            continue
        scored_probabilities.append(probability)
        scored_outcomes.append(outcome)
        stance = _stance(probability, follow_at, fade_at)
        if stance == _FOLLOW:
            active += 1
            markouts.append(_cap(trial.markout.follow_markout_bps, markout_cap_bps))
        elif stance == _FADE:
            active += 1
            markouts.append(_cap(trial.markout.fade_markout_bps, markout_cap_bps))

    scored = len(scored_probabilities)
    avg_brier = brier_score(scored_probabilities, scored_outcomes) if scored else None
    coverage = active / scored if scored else 0.0
    # Rounded once, at the end, over already-capped per-event values. `round` is half-to-even,
    # matching the single rounding `spot_markout` applies to a gross move.
    capped_avg = round(sum(markouts) / len(markouts)) if markouts else None

    return AgentSeasonRow(
        agent_id=agent_id,
        is_control=is_control,
        qualified=(
            season_status == "qualified"
            and scored > 0
            and active >= min_active
            and coverage >= min_coverage
        ),
        avg_brier=avg_brier,
        capped_avg_markout_bps=capped_avg,
        active_decisions=active,
        active_coverage=coverage,
        unscored=len(trials) - scored,
    )


def _cap(markout_bps: int, cap_bps: int) -> int:
    """Bound one event's markout to ``[-cap_bps, +cap_bps]``.

    PER EVENT, before any averaging. Capping the average instead would let a single outlier carry a
    whole season and be trimmed only after it had already moved the mean.
    """
    return max(-cap_bps, min(cap_bps, markout_bps))


def _rank_key(row: AgentSeasonRow) -> tuple[Any, ...]:
    """The season's total sort key, ascending, best first.

    Order: qualified before unqualified -> avg Brier asc (``None`` last) -> capped markout desc
    (``None`` last) -> active count desc -> ``agent_id`` asc.

    ``agent_id`` is what makes the key TOTAL, and dropping it is not a cosmetic simplification: two
    agents equal on every metric would then be ordered by whatever the sort happened to encounter
    first, and the same data would publish in a different order on the next run.

    The qualified/unqualified partition leads the key so that row 0 is the season's actual leader. It
    is a DEPARTURE from reading the frozen key in isolation, and it is deliberate: the gate exists
    precisely to say that an unqualified agent's score is not a result, so interleaving one above a
    qualified agent on a good-looking small sample would undo the gate at the point of display. The
    frozen key still orders the qualified set exactly as specified.

    ``None`` sorts last on both metric keys via a ``(present, value)`` pair — the same construction
    ``maker.leaderboard.maker_rank_key`` uses — rather than by substituting a sentinel number, which
    would rank a missing score as though it were a real one.
    """
    assert_no_clv_fields(dataclasses.asdict(row))  # guard the KEY itself: closes the direct-sort bypass
    brier_key = (1, 0.0) if row.avg_brier is None else (0, row.avg_brier)
    markout_key = (1, 0.0) if row.capped_avg_markout_bps is None else (0, -row.capped_avg_markout_bps)
    return (not row.qualified, brier_key, markout_key, -row.active_decisions, row.agent_id)


def score_season(
    pack: SealedPack,
    *,
    min_active: int = 20,
    min_coverage: float = 0.50,
    markout_cap_bps: int = 500,
    thresholds: tuple[float, float] = (0.60, 0.40),
) -> SeasonResult:
    """Score a sealed pack into a ranked season.

    The full frozen roster is scored on every call. Trials are taken chronologically; every settled
    trial is Brier-scored for every agent; climatology sees strictly prior outcomes only.

    Args:
        pack: The sealed pack to score. A :class:`PackWithDiagnostics` additionally contributes its
            diagnostic agents as extra rows; a plain pack contributes none.
        min_active: Active decisions required to qualify. Inclusive.
        min_coverage: Active-over-scored ratio required to qualify. Inclusive.
        markout_cap_bps: Per-event markout bound, applied before averaging.
        thresholds: ``(follow_at, fade_at)`` display bands, inclusive on both sides.

    Returns:
        The scored season, rows in rank order.

    Raises:
        ValueError: The pack declares ``no_season``; the thresholds overlap or the cap is negative;
            or a diagnostic agent shadows a roster member, is duplicated, mis-covers the season, or
            emits a value outside ``[0, 1]``. Every one of these would publish a season that
            misstates what was measured, so none is repaired by guessing.
    """
    season_status = pack.meta.combo.season_status
    if season_status == "no_season":
        raise ValueError(
            f"pack {pack.meta.season_id!r} declares season_status 'no_season' while carrying "
            f"{len(pack.trials)} trials; a no_season verdict means the scorer never ran, so a pack "
            f"asserting it is a contradiction and is refused rather than scored"
        )

    follow_at, fade_at = thresholds
    if not fade_at < follow_at:
        raise ValueError(
            f"thresholds must be (follow_at, fade_at) with fade_at < follow_at, got {thresholds!r}; "
            f"overlapping bands would make the display action depend on which test ran first"
        )
    if markout_cap_bps < 0:
        raise ValueError(f"markout_cap_bps must be non-negative, got {markout_cap_bps!r}")
    if not math.isfinite(min_coverage):
        raise ValueError(f"min_coverage must be finite, got {min_coverage!r}")

    trials = _settle(pack)
    diagnostics = pack.diagnostic_agents if isinstance(pack, PackWithDiagnostics) else ()
    _validate_diagnostics(diagnostics, len(trials))

    probabilities, probes = _roster_probabilities(trials, len(pack.trials))

    rows = [
        _score_agent(
            agent_id,
            agent_id not in CONTESTANT_IDS,
            probabilities[agent_id],
            trials,
            season_status=season_status,
            min_active=min_active,
            min_coverage=min_coverage,
            markout_cap_bps=markout_cap_bps,
            follow_at=follow_at,
            fade_at=fade_at,
        )
        for agent_id in FROZEN_ROSTER_IDS
    ]
    rows.extend(
        _score_agent(
            agent.agent_id,
            True,  # a diagnostic is never one of the three frozen contestants
            list(agent.probabilities),
            trials,
            season_status=season_status,
            min_active=min_active,
            min_coverage=min_coverage,
            markout_cap_bps=markout_cap_bps,
            follow_at=follow_at,
            fade_at=fade_at,
        )
        for agent in diagnostics
    )

    return SeasonResult(
        season_id=pack.meta.season_id,
        season_status=season_status,
        combo=pack.meta.combo,
        sample_size=sum(1 for trial in trials if trial.markout is not None),
        rows=tuple(sorted(rows, key=_rank_key)),
        debug_climatology_inputs=probes,
    )


def season_document(season: SeasonResult) -> dict[str, Any]:
    """Render a season as the document ``GET /signal-trials/season`` serves.

    The shape is the frozen H1.2 ``SignalTrialsSeasonResponse``. ``debug_climatology_inputs`` is
    deliberately NOT included: it is scorer-internal evidence for the no-lookahead law, not part of
    the published contract, and adding an unrecognised key to a frozen wire model is a contract
    change rather than extra helpfulness.

    Every row passes :func:`assert_no_clv_fields` on the way out. It has already passed on the way
    into the rank key; repeating it here is cheap and covers the case of a future caller assembling
    a document from rows that never went through ranking.
    """
    rows: list[dict[str, Any]] = []
    for row in season.rows:
        rendered = dataclasses.asdict(row)
        assert_no_clv_fields(rendered)
        rows.append(rendered)
    return {
        "season_id": season.season_id,
        "season_status": season.season_status,
        "combo": dataclasses.asdict(season.combo),
        "sample_size": season.sample_size,
        "rows": rows,
    }
