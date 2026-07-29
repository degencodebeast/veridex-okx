"""H3.5 — the season scorer: qualification gate, rank key, determinism.

Two regions, kept visibly apart.

**Region A** is the plan's mandated RED block from the frozen implementation plan (lines 639-685).
Every test body, every assertion and the ``FROZEN_ROSTER`` literal below are the plan's own text,
byte for byte. Two mechanical concessions were required to get it past the lane's own ruff gate,
and both are recorded here rather than left to be discovered:

* the block's two ``import`` lines are hoisted into the module import block at the top of the file
  (ruff ``E402`` refuses a module-level import below other statements) and the two imported NAMES
  are alphabetized there (ruff ``I001``). **The scoring import also carries FOUR NAMES BEYOND the
  plan's two** — ``AgentSeasonRow``, ``DiagnosticAgent``, ``PackWithDiagnostics`` and
  ``SeasonResult`` — because Region B's pins and the fixtures need them; the plan's own
  ``score_season`` and ``assert_no_clv_fields`` are unchanged and are still imported from the same
  module. Stated because it is otherwise accurate-but-invisible: a reader told only that "the names
  are alphabetized" would reasonably infer the statement is the plan's two and nothing else.
  The extension is what licenses the reordering. **Reordering never licenses itself** — the
  alphabetisation is permitted here only because the statement was already being extended for a
  reason independent of ruff, and it would not be permitted on a statement left otherwise untouched;
* the file carries ``# ruff: noqa: E701, SIM300``. Both codes are triggered ONLY by the plan's own
  text — ``E701`` by the single-line ``with pytest.raises(...): ...`` in ``test_clv_field_guard``
  and ``SIM300`` by ``assert FROZEN_ROSTER <= ids`` — and a per-line ``# noqa`` would have edited
  the mandated lines themselves. The suppression is file-wide because ruff has no region scope.
  **As measured when this note was written, neither code occurs in Region B — but that is a
  MEASUREMENT AT A HEAD, NOT A STANDING PROPERTY**, and the directive applies file-wide either way.
  This is the same claim shape that was once falsified by the very commit that wrote it, in the
  mypy note below; it is qualified here too because THE DOCSTRING IS WHAT A READER MEETS FIRST and
  an unqualified claim here would undo the caveat that appears eighty lines lower down.

**Region B** is this implementer's additional pins, labelled as pins rather than as RED — they were
green the moment they were written (C46) and they exist to close the gaps Region A leaves open. The
largest of those gaps is a C52 DISCRIMINATION failure in the plan's own
``test_exploratory_season_never_emits_qualified_rows``: it runs against a SIX-trial pack, where no
agent could reach ``min_active=20`` even under a ``qualified`` status, so the test passes whether or
not ``season_status`` is wired into the gate at all. ``test_exploratory_status_alone_suppresses_a
_season_that_would_otherwise_qualify`` supplies the missing control by running the ``qualified``
fixture's exact 44 trials under an ``exploratory`` status and requiring zero qualified rows against
a five-row qualified twin.

**The fixtures are DESIGNED, and their design is pinned.** Every probability the frozen contestants
emit here is a hand-computed consequence of the §5.3 formulas over constant signal inputs, and
``test_fixture_contestant_probabilities_are_as_designed`` asserts those values directly. A fixture
whose assumptions are merely believed is a fixture that stops testing what its name says the moment
a coefficient moves; these assert what they assume.

**The outcome vector is a DECLARED ORACLE, not a derived one.** ``outcomes`` on the fixture packs is
a hand-written literal describing the outcome each trial was CONSTRUCTED to produce, never a value
computed by the code under test. That is what makes the plan's
``test_climatology_is_fed_strictly_prior_outcomes`` a real measurement: it compares the scorer's
internally-derived climatology feed against an expectation the scorer had no hand in producing. See
``_OraclePack`` for why the oracle rides a test-only subclass.
"""

from __future__ import annotations

# Needed BECAUSE OF Region A; FILE-SCOPED in effect. E701 is the plan's single-line
# `with pytest.raises(ValueError): ...`; SIM300 is its `assert FROZEN_ROSTER <= ids`. As measured
# when this note was written, neither code occurs in Region B — but that is a POINT-IN-TIME
# MEASUREMENT, NOT A BOUND, and the directive below applies to the whole file either way. See the
# longer note on the mypy directive: the same scope-versus-measurement distinction applies here.
# ruff: noqa: E701, SIM300
#
# Likewise for mypy, and for the same reason. Region A compares the two NULLABLE metrics directly —
# `briers == sorted(briers)` over `list[float | None]`, and `>=` between two `int | None` markouts —
# which mypy correctly reports as unsafe in general. They are safe HERE because `score_season`
# enforces `qualified => avg_brier is not None` and `qualified => active >= 1 => markout is not
# None`, and both assertions run only over qualified rows. That invariant was NOT holding when this
# file was written: mypy's complaint is what exposed it, and `_score_agent`'s `scored > 0` conjunct
# plus `test_an_all_unscored_season_reports_no_score_rather_than_a_zero` are the fix and its pin.
# THE SITES, CORRECTED. The two codes arise at `assert briers == sorted(briers)` (type-var) and at
# `assert ranked[0].capped_avg_markout_bps >= ranked[1].capped_avg_markout_bps` (operator), both
# inside Region A, over `AgentSeasonRow.avg_brier: float | None` and `capped_avg_markout_bps:
# int | None` (scoring.py). An earlier version of this note cited lines 341 and 346, which was
# wrong and had already drifted: line numbers move whenever anything above them is edited. The
# assertions are named here instead, because a NAME survives an edit and a line number does not.
#
# READ THIS BEFORE TRUSTING THE PARAGRAPH ABOVE. The directive below is FILE-SCOPED: it suppresses
# both codes everywhere in this module, including Region B, because ruff and mypy have no
# region-scoped form, and a per-line suppression comment (`noqa` / `type: ignore`, spelled without
# the leading hash here so ruff does not try to parse this sentence as a directive) would have
# EDITED the plan's mandated lines.
# The claim that "neither code occurs in Region B" is a POINT-IN-TIME MEASUREMENT
# taken when this note was written, NOT A BOUND: a future Region B addition that trips either code
# will be silently suppressed and no gate will say so. Scope and measurement are different things,
# and this comment is the only thing distinguishing them. The residual is a program-level limitation
# (no region-scoped suppression), not something this file can close.
#
# THAT WARNING WAS FALSIFIED BY THE VERY COMMIT THAT WROTE IT, WHICH IS WHY IT IS KEPT VERBATIM
# ABOVE RATHER THAN SOFTENED. The same commit added the F2 pin, whose
# `assert witness.avg_brier < other.avg_brier` tripped `operator` in REGION B over an unnarrowed
# `other.avg_brier` -- and the file-scoped directive swallowed it in silence, exactly as predicted,
# one commit after the prediction. The warned-of future arrived inside the warning.
# FIXED, not merely noted: `assert other.avg_brier is not None` now precedes the comparison, which
# asserts the real invariant (a qualified row has a Brier) and narrows the type as a side effect.
# RE-MEASURED after the fix, by stripping this directive from a `git archive` export and running
# the type checker over it: the only remaining diagnostics are the two Region A sites named above.
# So "nothing of Region B is hidden" is a TRUE statement again -- true because it was CHECKED, not
# because it was claimed. Anyone adding to Region B should re-run that check, not trust this note.
#
# (This paragraph is deliberately NOT wrapped so that any line begins with the type checker's own
# directive prefix. An earlier wrap put that prefix at the start of a line, and the tool parsed
# this PROSE as a configuration comment and raised a real error. Same class as the ruff `noqa`
# case above: a tool's directive syntax appearing inside a sentence about that directive.)
# mypy: disable-error-code="type-var, operator"
import inspect
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from veridex.signal_trials.challenge_spec import CanonicalSignal
from veridex.signal_trials.okx_client import Candle, CandleSeries
from veridex.signal_trials.pack import PackMeta, PackRef
from veridex.signal_trials.preflight import ComboSelection
from veridex.signal_trials.scoring import (
    AgentSeasonRow,
    DiagnosticAgent,
    PackWithDiagnostics,
    SeasonResult,
    assert_no_clv_fields,
    score_season,
)

# --------------------------------------------------------------------------------------------
# Fixture construction. Every number here is chosen so the resulting probabilities, outcomes and
# markouts are hand-computable; the pins in Region B assert those hand computations.
# --------------------------------------------------------------------------------------------

_BAR = "1H"
_BAR_MS = 3_600_000
_HORIZON_MS = 3_600_000
_COST_BPS = 10

#: Every candle in every fixture closes here, which makes ``compute_ext`` return 0 on every trial
#: (the one-hour lookback ratio is exactly 1.0, never above the 0.20 threshold). Holding ``ext``
#: constant is deliberate: it takes the ``ext``-dependent contestants off a moving input so their
#: probabilities are a function of the signal fields alone.
_CLOSE = 100.0

_TOKEN = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
#: A token with NO settlement series, used to manufacture UNSCORED trials.
_UNSETTLED_TOKEN = "0xfeedfacefeedfacefeedfacefeedfacefeedface"

# Entry prices, chosen so ``spot_markout`` lands on exact integers against a 100.0 close.
#   99.0  -> gross +101 bps, follow +91  (profitable -> outcome 1), fade -111
#  101.0  -> gross  -99 bps, follow -109 (unprofitable -> outcome 0), fade  +89
_WIN_ENTRY = 99.0
_LOSS_ENTRY = 101.0
#   90.0  -> gross +1111 bps, follow +1101 -> CAPPED to +500, fade -1121 -> CAPPED to -500
#  100.5  -> gross   -50 bps, follow   -60 (outcome 0),        fade    +40 (uncapped)
_BIG_WIN_ENTRY = 90.0
_SMALL_LOSS_ENTRY = 100.5

# Signal fields held constant across every trial, so each frozen contestant emits one probability
# for the whole season. The values are picked to place the three contestants in three DIFFERENT
# display bands — FOLLOW, FADE and ABSTAIN — so the qualification gate has something to separate.
_WALLET_COUNT = 4
_AMOUNT_USD = 1000.0
_TOP10_PERCENT = 40.0
_MARKET_CAP_USD = 1_000_000.0

# Hand-computed from the §5.3 formulas at the constants above, with ext == 0 throughout.
#   flow_follower        0.50 + 0.06*(4-2) + 0.04*log10(1000/1000)          = 0.62  -> FOLLOW
#   crowding_fader       0.50 - 0.35*0.40  - 0.15*0 - 0.20*0                = 0.36  -> FADE
#   selective_calibrator 0.50 + 0.08*(4-2)/4 - 0.15*(0.40-0.50) - 0.10*0    = 0.555 -> ABSTAIN
_EXPECTED_FLOW_FOLLOWER_P = 0.62
_EXPECTED_CROWDING_FADER_P = 0.36
_EXPECTED_SELECTIVE_CALIBRATOR_P = 0.555

#: The QUALIFIED season's declared outcome oracle: seven wins, three losses, then 34 wins.
#: The first-ten mean is 0.7 while the whole-season mean is 41/44 = 0.9318, so a climatology fed
#: the FULL pack instead of the strict prefix produces a visibly different number at trial 10.
_OUTCOMES_QUALIFIED: tuple[int, ...] = (1,) * 7 + (0,) * 3 + (1,) * 34

#: The TIES season's declared oracle: strict alternation, so the running base rate hovers at 0.5
#: and climatology abstains for the whole season (keeping it out of the ranked rows under test).
_OUTCOMES_TIES: tuple[int, ...] = tuple(1 if index % 2 == 0 else 0 for index in range(44))

#: The exploratory mini-pack's declared oracle (six trials, per the plan's fixture description).
_OUTCOMES_MINI: tuple[int, ...] = (1, 1, 1, 0, 1, 0)


@dataclass(frozen=True)
class _OraclePack(PackWithDiagnostics):
    """A fixture pack carrying the DECLARED outcome oracle alongside the pack itself.

    **This type is the resolution of a contradiction in the frozen plan, and it is test-only.**
    Plan line 655 reads ``full_pack_qualified.outcomes[:10]``, but ``SealedPack`` exposes exactly
    ``ref``, ``meta``, ``trials`` and ``settlement`` — there is no ``outcomes`` attribute and there
    must not be one, because an outcome is DERIVED from settlement by ``spot_markout`` rather than
    stored, and ``pack.py`` is owned by another lane and immutable here.

    Carrying the oracle on a test-only frozen subclass keeps three properties at once: the plan's
    mandated expression works unchanged; ``score_season(pack: SealedPack, ...)`` still typechecks
    under mypy because an ``_OraclePack`` IS a ``SealedPack``; and production gains no ``outcomes``
    surface that could be mistaken for a stored truth.

    ``outcomes`` is never read by the code under test — only by the tests, as the independent
    expectation the scorer's own derivation is measured against.
    """

    outcomes: tuple[int, ...] = ()


def _signal(t0_ms: int, trigger_price: float, token: str = _TOKEN) -> CanonicalSignal:
    """One canonical trial at ``t0_ms``, entered at ``trigger_price``."""
    return CanonicalSignal(
        t0_ms=t0_ms,
        chain_index="501",
        token_address=token,
        symbol="FIXT",
        name="Fixture Token",
        market_cap_usd=_MARKET_CAP_USD,
        holders=1000,
        top10_holder_percent=_TOP10_PERCENT,
        trigger_price=trigger_price,
        wallet_type="1",
        trigger_wallet_count=_WALLET_COUNT,
        trigger_wallet_address="0xabc",
        amount_usd=_AMOUNT_USD,
    )


def _t0_for(position: int) -> int:
    """The open time of the trial at 0-based chronological ``position``.

    Trials open on candle-close boundaries starting two bars in, so that every trial has BOTH a
    settlement candle (the bar closing one horizon later) and the two earlier closes ``compute_ext``
    needs for its one-hour lookback.
    """
    return (position + 2) * _BAR_MS


def _candles(trial_count: int, closes: tuple[float, ...] | None = None) -> CandleSeries:
    """A confirmed, gapless series covering every trial's ext lookback and settlement bar.

    ``closes`` supplies a per-candle close so a fixture can make ``ext`` VARY across trials. The
    default flat series holds ``ext`` at 0 everywhere, which is what every fixture but
    ``pack_ext_varies_by_trial`` wants — a constant ``ext`` keeps the ext-dependent contestants on
    a fixed probability so the other pins can reason about them.
    """
    prices = closes if closes is not None else (_CLOSE,) * (trial_count + 2)
    return CandleSeries(
        bar=_BAR,
        bar_ms=_BAR_MS,
        candles=tuple(
            Candle(
                ts_open_ms=index * _BAR_MS,
                open=prices[index],
                high=prices[index],
                low=prices[index],
                close=prices[index],
                vol=1.0,
                vol_usd=100.0,
                confirmed=True,
            )
            # Trial ``trial_count - 1`` settles against the candle at index ``trial_count + 1``.
            for index in range(trial_count + 2)
        ),
    )


def _meta(season_id: str, season_status: str) -> PackMeta:
    return PackMeta(
        season_id=season_id,
        combo=ComboSelection(chain_index="501", bar=_BAR, season_status=season_status),  # type: ignore[arg-type]
        probe_counts={"synthetic": True},
        filters={"synthetic": True},
        cost_bps=_COST_BPS,
        horizon_ms=_HORIZON_MS,
        bar=_BAR,
        bar_ms=_BAR_MS,
        versions={"fixture": "synthetic"},
    )


def _build_pack(
    season_id: str,
    season_status: str,
    outcomes: tuple[int, ...],
    *,
    win_entry: float = _WIN_ENTRY,
    loss_entry: float = _LOSS_ENTRY,
    unsettled_positions: frozenset[int] = frozenset(),
    diagnostic_agents: tuple[DiagnosticAgent, ...] = (),
    closes: tuple[float, ...] | None = None,
) -> _OraclePack:
    """Assemble a synthetic pack whose settled outcomes are exactly ``outcomes``.

    With a flat price series each trial's entry comes from the declared outcome: ``win_entry`` for
    a 1 and ``loss_entry`` for a 0. Trials at ``unsettled_positions`` are pointed at a token with
    no settlement series, which is what makes them UNSCORED.

    When ``closes`` supplies a RISING series the fixed entries would no longer produce the declared
    outcomes, so each entry is derived from that trial's own settlement close instead —
    ``close * 0.99`` for a declared 1 and ``close * 1.01`` for a 0. Those factors give gross moves
    of +101 and -99 bps at EVERY price level, so the oracle holds however far the series has run.
    """
    def _entry(position: int, outcome: int) -> float:
        if closes is None:
            return win_entry if outcome == 1 else loss_entry
        # Trial ``position`` settles against the candle at ``position + 2``.
        return closes[position + 2] * (0.99 if outcome == 1 else 1.01)

    trials = tuple(
        _signal(
            _t0_for(position),
            _entry(position, outcome),
            token=_UNSETTLED_TOKEN if position in unsettled_positions else _TOKEN,
        )
        for position, outcome in enumerate(outcomes)
    )
    return _OraclePack(
        ref=PackRef(dir=Path("/nonexistent/fixture") / season_id, content_hash="0" * 64),
        meta=_meta(season_id, season_status),
        trials=trials,
        settlement={_TOKEN: _candles(len(outcomes), closes)},
        diagnostic_agents=diagnostic_agents,
        outcomes=outcomes,
    )


@pytest.fixture
def mini_pack_exploratory() -> _OraclePack:
    """Six synthetic trials under an EXPLORATORY status."""
    return _build_pack("season-mini", "exploratory", _OUTCOMES_MINI)


@pytest.fixture
def full_pack_qualified() -> _OraclePack:
    """44 synthetic trials, qualified status, the full frozen roster plus ``always_neutral``.

    ``always_neutral`` is supplied as a DIAGNOSTIC agent — a fixed probability vector, never a
    callable — so the fixture can demonstrate the qualification gate on an agent that is fully
    scored and still not ranked, without the scorer gaining any way to run test-supplied code.
    """
    return _build_pack(
        "season-full",
        "qualified",
        _OUTCOMES_QUALIFIED,
        diagnostic_agents=(DiagnosticAgent(agent_id="always_neutral", probabilities=(0.5,) * 44),),
    )


@pytest.fixture
def full_pack_qualified_with_brier_ties() -> _OraclePack:
    """44 trials carrying two designed ties.

    ``mo_high`` and ``mo_low`` are near-perfect forecasters that each get exactly ONE trial wrong,
    so their Brier sums are both exactly 1.0 — bit-for-bit equal, because every per-trial error is
    either 0.0 or 1.0 and float addition of exact values is exact. They differ in WHICH trial they
    miss, and the two misses have very different prices: ``mo_high`` misses a small loser (-60 bps
    instead of +40) while ``mo_low`` misses a big winner (a capped -500 instead of a capped +500).
    That is what separates them on the second rank key while leaving the first key tied.

    ``tie_alpha`` and ``tie_beta`` carry IDENTICAL probability vectors, so they are equal on every
    rank key there is and can only be separated by ``agent_id``. They are DECLARED IN REVERSE ORDER
    — ``tie_beta`` first — and that is load-bearing rather than tidy: Python's sort is stable, so
    declaring them alphabetically would let insertion order silently supply the ordering a missing
    ``agent_id`` key was supposed to provide, and a mutant that dropped the final key would survive
    the whole suite. Declared reversed, only the key itself can put them right.
    """
    outcomes = _OUTCOMES_TIES
    perfect = tuple(1.0 if outcome == 1 else 0.0 for outcome in outcomes)
    # mo_high FOLLOWs trial 1 (a loser); mo_low FADEs trial 0 (a winner).
    mo_high = perfect[:1] + (1.0,) + perfect[2:]
    mo_low = (0.0,) + perfect[1:]
    return _build_pack(
        "season-ties",
        "qualified",
        outcomes,
        win_entry=_BIG_WIN_ENTRY,
        loss_entry=_SMALL_LOSS_ENTRY,
        diagnostic_agents=(
            DiagnosticAgent(agent_id="mo_high", probabilities=mo_high),
            DiagnosticAgent(agent_id="mo_low", probabilities=mo_low),
            # Reversed on purpose — see the docstring. Stability must not stand in for the key.
            DiagnosticAgent(agent_id="tie_beta", probabilities=(0.70,) * 44),
            DiagnosticAgent(agent_id="tie_alpha", probabilities=(0.70,) * 44),
            # Exactly ON the two display bounds, which nothing else in any fixture sits on. The
            # frozen rule is ">= 0.60" and "<= 0.40"; without these an exclusive `>` / `<` would
            # change no observable behaviour anywhere in this suite.
            DiagnosticAgent(agent_id="edge_follow_at_bound", probabilities=(0.60,) * 44),
            DiagnosticAgent(agent_id="edge_fade_at_bound", probabilities=(0.40,) * 44),
        ),
    )


#: Every trial in the ACTIVE-COUNT fixture settles profitable-to-follow. That is what lets two
#: agents reach an IDENTICAL capped markout on DIFFERENT numbers of decisions: every active trial
#: pays the same capped +500, so the mean is 500 regardless of how many were taken.
_OUTCOMES_ALL_PROFITABLE: tuple[int, ...] = (1,) * 44


#: A +50% step every OTHER candle. ``compute_ext`` compares the close at ``t0`` against the close
#: one hour earlier, and those two land on consecutive candles here, so the step makes ``ext``
#: alternate 0,1,0,1,… across trials instead of sitting at 0 everywhere.
_RISING_CLOSES: tuple[float, ...] = tuple(100.0 * (1.5 ** (index // 2)) for index in range(46))

#: The ext each trial is CONSTRUCTED to see, declared rather than derived from the code under test.
#: ``test_scorer_feeds_each_trial_its_OWN_ext`` verifies this against ``compute_ext`` before using
#: it, so it is a checked property of the fixture and not an assumption about it.
_EXT_ALTERNATING: tuple[int, ...] = tuple(0 if position % 2 == 0 else 1 for position in range(44))


@pytest.fixture
def pack_ext_varies_by_trial() -> _OraclePack:
    """44 trials on a RISING price series, so each trial's correct ``ext`` differs from its neighbour.

    **This fixture exists because every other fixture in this module supplies ``ext == 0`` on every
    trial, and that made the scorer's per-trial ``ext`` wiring untestable.** §5.3 makes ``ext`` a
    load-bearing input to CrowdingFader and SelectiveCalibrator. H3.3 tests ``compute_ext`` and both
    formulas in isolation, and this module tested them by calling all three DIRECTLY — never through
    ``score_season``. The function was right, the formulas were right, and the WIRING BETWEEN THEM
    was unpinned.

    Measured on the unmodified head: forcing ``ext = 1`` on a single trial inside ``_settle``, after
    the real ``compute_ext`` call, moved ``crowding_fader``'s Brier from 0.3905090909090909 to
    0.39723636363636367 and ``selective_calibrator``'s from 0.20552499999999999 to
    0.20777499999999993 — the PRIMARY RANK METRIC — while the whole suite stayed green at 825.

    A flat price series cannot see that: with ``ext`` constant, the two contestants emit a constant
    probability and their Brier is invariant to WHICH trial got which ``ext``. The rising series
    makes ``ext`` alternate, so the two contestants take DIFFERENT probabilities on adjacent trials
    and any mis-wiring changes their scores.

    Entries are derived from each trial's own settlement close (see ``_build_pack``), so the
    declared outcome oracle holds unchanged however far the price has run.
    """
    return _build_pack(
        "season-ext",
        "qualified",
        _OUTCOMES_QUALIFIED,
        closes=_RISING_CLOSES,
    )


@pytest.fixture
def pack_signal_join_observable() -> _OraclePack:
    """44 trials carrying an agent whose probabilities VARY BY POSITION, so a JOIN error is visible.

    **This fixture exists because every other fixture is PERMUTATION-INVARIANT, and that hid a real
    hole.** Everywhere else in this module the agents emit a CONSTANT probability — the frozen
    contestants because their signal inputs are held constant, the controls by definition. With a
    constant ``p``, an agent's Brier is ``mean((p - o)**2)`` over the outcome MULTISET, so it depends
    only on HOW MANY outcomes were 1 and not at all on WHICH TRIAL each belonged to. Every
    aggregate-based assertion in this module is therefore blind to a signal/outcome JOIN error.

    Measured, on the unmodified head: transposing the settled markouts of positions 1 and 7 — which
    carry OPPOSITE declared outcomes — while leaving the signals in place attaches each outcome to
    the WRONG signal and preserves the total hit count. The whole suite stayed green, 823 passed.

    ``join_witness`` breaks that invariance by construction: its probability at each position is
    derived FROM THE DECLARED ORACLE AT THAT POSITION (1.0 where the trial was built to settle
    profitable, 0.0 where it was not). Its Brier is therefore exactly 0.0 if and only if the
    scorer's derived outcome at EVERY position equals the oracle's at that same position. Move any
    outcome to a different trial and the witness becomes confidently wrong at both ends, so the
    Brier leaves 0 immediately. It is not "two observable positions" — every position is observable.

    Note this is still an ORACLE comparison, not a self-check: the probabilities come from the
    hand-written oracle, and the outcomes come from the scorer deriving them out of settlement. The
    two paths remain independent, exactly as ``test_the_outcome_oracle_is_INDEPENDENT_of_the_scorer
    _derivation_path`` requires.
    """
    witness = tuple(1.0 if outcome == 1 else 0.0 for outcome in _OUTCOMES_QUALIFIED)
    return _build_pack(
        "season-join",
        "qualified",
        _OUTCOMES_QUALIFIED,
        diagnostic_agents=(DiagnosticAgent(agent_id="join_witness", probabilities=witness),),
    )


@pytest.fixture
def pack_tied_on_brier_and_markout() -> _OraclePack:
    """44 trials where two qualified agents tie on Brier AND markout, differing ONLY on active count.

    **This fixture exists because rank term 3 was unpinned in DIRECTION, and no fixture could see
    it.** A mutant flipping only the sign of ``-row.active_decisions`` SURVIVED the entire 819-test
    suite, load-proven. It hid because term 3 never decided anything: in ``full_pack_qualified``
    every qualified row has a distinct Brier, and in ``full_pack_qualified_with_brier_ties`` the
    Brier tie is broken by markout before term 3 is ever consulted. A mutant without a
    discriminating fixture only re-reports SURVIVED — it proves the gap rather than closing it.

    The construction, with every quantity exact in binary floating point:

    * every trial settles profitable-to-follow, so EVERY active decision pays a capped ``+500`` and
      the mean markout is 500 for ANY number of decisions — that is what unties markout from count;
    * ``zz_active_32`` FOLLOWs at ``p=0.75`` on 32 trials and abstains on 12;
    * ``aa_active_24`` FOLLOWs at ``p=1.00`` on 24 trials and abstains on 20.

    Both Brier sums are EXACTLY equal — not approximately — because every term is a dyadic rational
    (``(0.75-1)**2 == 0.0625 == 2**-4`` and the abstain error ``0.25 == 2**-2``), so every partial
    sum is exactly representable and the tie cannot drift with summation order.

    **The numbers are deliberately NOT written out here.** An arithmetic claim in a docstring is a
    claim about what its author believed, and this one was stated wrongly once while still describing
    a correct design — a check that reaches the right answer down a wrong path is a coincidence, not
    a verification. ``test_active_count_is_the_THIRD_rank_key_and_sorts_DESCENDING`` RECOMPUTES both
    sums from these vectors instead, so the tie is re-proved on every run. Both clear the gate (active 32 and 24
    are >= 20; coverage 32/44 and 24/44 are >= 0.50). Term 3 is therefore the ONLY key left that
    can order them.

    **The names are load-bearing and deliberately anti-alphabetical.** Correct order is
    ``zz_active_32`` first (more active decisions rank higher). Alphabetically ``aa_`` sorts BEFORE
    ``zz_``, so the final ``agent_id`` term would produce the WRONG order. That makes the fixture
    discriminate against BOTH failure modes: flipping term 3's sign, and deleting term 3 entirely
    so that ``agent_id`` decides. A fixture that only caught the sign flip would be half a pin.
    """
    active_high = (0.75,) * 32 + (0.5,) * 12
    active_low = (1.0,) * 24 + (0.5,) * 20
    # A SECOND pair, for a different defect: here MARKOUT and ACTIVE COUNT DISAGREE about the order,
    # which is what makes the POSITION of terms 2 and 3 observable at all. `swap_hi_markout` takes
    # FEWER decisions but every one pays a capped +500; `swap_lo_markout` takes MORE decisions and
    # fades two of them, dragging its mean to +433. Their Brier sums are exactly tied, so:
    #   frozen  (markout desc BEFORE active desc) -> swap_hi first
    #   swapped (active desc BEFORE markout desc) -> swap_lo first
    # Without a pair where the two terms PULL IN OPPOSITE DIRECTIONS, swapping them reorders
    # nothing and the mutant is invisible — which is exactly how it survived when first drilled.
    swap_hi = (1.0,) * 22 + (0.5,) * 22
    swap_lo = (1.0,) * 28 + (0.0,) * 2 + (0.5,) * 14
    return _build_pack(
        "season-active-count",
        "qualified",
        _OUTCOMES_ALL_PROFITABLE,
        win_entry=_BIG_WIN_ENTRY,
        diagnostic_agents=(
            DiagnosticAgent(agent_id="zz_active_32", probabilities=active_high),
            DiagnosticAgent(agent_id="aa_active_24", probabilities=active_low),
            DiagnosticAgent(agent_id="swap_hi_markout", probabilities=swap_hi),
            DiagnosticAgent(agent_id="swap_lo_markout", probabilities=swap_lo),
        ),
    )


# ==============================================================================================
# REGION A — THE PLAN'S MANDATED RED BLOCK, VERBATIM (frozen plan lines 639-685).
# The two import lines belonging to this block are at the top of the file, unmodified.
# ==============================================================================================

FROZEN_ROSTER = {"flow_follower", "crowding_fader", "selective_calibrator",
                 "always_follow", "always_fade", "neutral", "climatology"}

def test_season_scores_exactly_the_frozen_roster(full_pack_qualified):
    season = score_season(full_pack_qualified)
    ids = {r.agent_id for r in season.rows}
    assert FROZEN_ROSTER <= ids                                    # 3 contestants + 4 controls all present
    assert {r.agent_id for r in season.rows if r.is_control} >= {"always_follow", "always_fade", "neutral", "climatology"}

def test_climatology_is_fed_strictly_prior_outcomes(full_pack_qualified):
    # scorer-level (not just the helper): climatology's per-trial input is outcomes settled strictly earlier
    probes = score_season(full_pack_qualified).debug_climatology_inputs   # {trial_index: p_used}
    assert probes[0] == 0.5 and probes[9] == 0.5                    # cold start through trial 10 (min_prior_trials=10)
    expected_11 = sum(full_pack_qualified.outcomes[:10]) / 10       # mean of the FIRST TEN outcomes only
    assert probes[10] == pytest.approx(expected_11)

def test_exploratory_season_never_emits_qualified_rows(mini_pack_exploratory):
    season = score_season(mini_pack_exploratory)
    assert season.season_status == "exploratory" and all(r.qualified is False for r in season.rows)

def test_qualification_gate(full_pack_qualified):
    season = score_season(full_pack_qualified)
    lazy = next(r for r in season.rows if r.agent_id == "always_neutral")
    assert lazy.qualified is False and lazy.avg_brier is not None   # scored, not ranked-qualified

def test_rank_key_is_brier_then_capped_markout(full_pack_qualified):
    season = score_season(full_pack_qualified)
    ranked = [r for r in season.rows if r.qualified]
    assert len(ranked) >= 3                       # non-vacuous: fixtures guarantee ranked rows
    briers = [r.avg_brier for r in ranked]
    assert briers == sorted(briers)

def test_tiebreaks_markout_then_count_then_id(full_pack_qualified_with_brier_ties):
    season = score_season(full_pack_qualified_with_brier_ties)
    ranked = [r for r in season.rows if r.qualified]
    assert ranked[0].capped_avg_markout_bps >= ranked[1].capped_avg_markout_bps
    tie_pair = [r.agent_id for r in ranked if r.agent_id.startswith("tie_")]
    assert tie_pair == sorted(tie_pair)

def test_determinism(full_pack_qualified):
    assert score_season(full_pack_qualified) == score_season(full_pack_qualified)

def test_clv_field_guard():
    with pytest.raises(ValueError): assert_no_clv_fields({"agent_id": "x", "clv_bps": 10})


# ==============================================================================================
# REGION B — THIS IMPLEMENTER'S PINS. Labelled pins, not RED (C46): each was green when written.
# They close gaps Region A leaves open; every one names the specific gap it closes.
# ==============================================================================================


# --- B1. Fixture-validity pins. A designed fixture that is only BELIEVED to be as designed stops
# --- testing what its name claims the moment a §5.3 coefficient moves. These assert the design.


def test_fixture_contestant_probabilities_are_as_designed(full_pack_qualified):
    """PIN: the three frozen contestants land in three DIFFERENT display bands on this fixture.

    Without this, ``test_rank_key_is_brier_then_capped_markout`` could silently degrade into a
    two-agent comparison if a coefficient change collapsed the contestants into one band.
    """
    from veridex.signal_trials.contestants import compute_ext, crowding_fader, flow_follower, selective_calibrator

    series = full_pack_qualified.settlement[_TOKEN]
    exts = [compute_ext(series, trial.t0_ms) for trial in full_pack_qualified.trials]
    assert exts == [0] * 44, "fixture assumes ext == 0 on every trial; a non-zero ext moves two contestants"

    signal = full_pack_qualified.trials[0]
    assert flow_follower(signal) == pytest.approx(_EXPECTED_FLOW_FOLLOWER_P)
    assert crowding_fader(signal, 0) == pytest.approx(_EXPECTED_CROWDING_FADER_P)
    assert selective_calibrator(signal, 0) == pytest.approx(_EXPECTED_SELECTIVE_CALIBRATOR_P)
    # FOLLOW / FADE / ABSTAIN respectively, against the frozen (0.60, 0.40) thresholds.
    assert _EXPECTED_FLOW_FOLLOWER_P >= 0.60
    assert _EXPECTED_CROWDING_FADER_P <= 0.40
    assert 0.40 < _EXPECTED_SELECTIVE_CALIBRATOR_P < 0.60


def test_scorer_feeds_each_trial_its_OWN_ext(pack_ext_varies_by_trial):
    """PIN (CODEX R2 MAJOR): the scorer feeds each contestant the ``ext`` of THAT trial.

    **Observed THROUGH ``score_season``, not around it.** The pre-existing check called
    ``compute_ext`` and the contestant formulas directly, so it could confirm the function and the
    formulas while saying nothing about the wiring between them — and every fixture supplied
    ``ext == 0`` everywhere, so no datum could have made a wiring error visible anyway.

    Structure of the comparison, which keeps the two sides independent:
      * the DECLARED ext vector is verified against ``compute_ext`` over the fixture's own candles,
        so the expectation is a checked property of the fixture rather than an assumption;
      * the EXPECTED Brier is then computed from that declared vector through the frozen §5.3
        formulas;
      * the ACTUAL Brier comes out of ``score_season``, which derived its own ``ext`` internally.
    Feed a trial the wrong ``ext`` and the two diverge.
    """
    from veridex.signal_trials.contestants import compute_ext, crowding_fader, selective_calibrator

    pack = pack_ext_varies_by_trial
    trials = sorted(pack.trials, key=lambda trial: trial.t0_ms)
    series = pack.settlement[_TOKEN]

    # The fixture really does vary ext, and the declared vector really is what compute_ext sees.
    derived = [compute_ext(series, trial.t0_ms) for trial in trials]
    assert derived == list(_EXT_ALTERNATING), "the fixture's construction must produce the declared ext"
    assert derived.count(0) == 22 and derived.count(1) == 22, "BOTH ext values must be present"

    season = score_season(pack)
    for agent_id, formula in (("crowding_fader", crowding_fader), ("selective_calibrator", selective_calibrator)):
        expected = sum(
            (formula(signal, ext) - outcome) ** 2
            for signal, ext, outcome in zip(trials, _EXT_ALTERNATING, pack.outcomes, strict=True)
        ) / len(pack.outcomes)
        row = next(r for r in season.rows if r.agent_id == agent_id)
        assert row.avg_brier == pytest.approx(expected), (
            f"{agent_id}'s season Brier does not match the frozen formula evaluated on each trial's "
            f"OWN ext; the scorer is feeding some trial the wrong ext"
        )

    # DISCRIMINATION: the assertion above must be capable of failing. Evaluating the same formulas
    # against the INVERTED ext vector — the cheapest possible wiring error — gives a different
    # number, so agreement above is evidence rather than arithmetic that could not have differed.
    inverted = tuple(1 - ext for ext in _EXT_ALTERNATING)
    for agent_id, formula in (("crowding_fader", crowding_fader), ("selective_calibrator", selective_calibrator)):
        wrong = sum(
            (formula(signal, ext) - outcome) ** 2
            for signal, ext, outcome in zip(trials, inverted, pack.outcomes, strict=True)
        ) / len(pack.outcomes)
        row = next(r for r in season.rows if r.agent_id == agent_id)
        assert row.avg_brier != pytest.approx(wrong), f"{agent_id} cannot distinguish the ext vector"


def test_scorer_derived_outcome_COUNTS_match_the_declared_oracle(full_pack_qualified):
    """PIN: the scorer derives the right NUMBER of profitable and unprofitable trials.

    Region A's climatology test only pins the first ten outcomes (through ``probes[10]``); a
    divergence in the COUNT at trial 11 or later would be invisible to it. ``always_follow`` emits
    p=1.0 on every trial so its Brier is exactly the miss rate, and ``always_fade`` mirrors it.

    **THIS TEST IS PERMUTATION-INVARIANT AND DOES NOT PIN THE PER-TRIAL JOIN. Stated because an
    earlier version of this docstring claimed it did.** It called the pair a "total readout" that
    "pins every trial". It does not: both agents emit a CONSTANT probability, so each Brier is a
    function of the outcome MULTISET alone. Any permutation of the same outcomes across trials
    leaves both values identical. Measured on the unmodified head — transposing the settled
    markouts of positions 1 and 7, which carry opposite declared outcomes, left the entire suite
    green at 823 passed.

    The per-trial join is pinned by
    ``test_each_outcome_is_joined_to_ITS_OWN_signal_not_merely_counted``, which needs an agent whose
    probabilities vary by position to see it at all. What THIS test pins is narrower and still
    worth having: the counts, which that test would also catch but which this states directly.
    """
    season = score_season(full_pack_qualified)
    always_follow = next(r for r in season.rows if r.agent_id == "always_follow")
    misses = len(_OUTCOMES_QUALIFIED) - sum(_OUTCOMES_QUALIFIED)
    assert always_follow.avg_brier == pytest.approx(misses / len(_OUTCOMES_QUALIFIED))
    # The mirror: always_fade's Brier is exactly the HIT rate. Both together pin the two COUNTS.
    always_fade = next(r for r in season.rows if r.agent_id == "always_fade")
    assert always_fade.avg_brier == pytest.approx(sum(_OUTCOMES_QUALIFIED) / len(_OUTCOMES_QUALIFIED))


def test_each_outcome_is_joined_to_ITS_OWN_signal_not_merely_counted(pack_signal_join_observable):
    """PIN (MAJOR-1): every derived outcome belongs to the trial it was derived FROM.

    **The trust statement.** A signal/outcome join error is not a test-quality nit: real signals do
    not emit the fixture's constant probabilities, so attaching an outcome to the wrong signal
    changes Brier scores and therefore rankings — silently, with every aggregate assertion still
    green. Ordering and association are different properties. M11 ("score in file order instead of
    chronologically") pins ORDERING; nothing pinned ASSOCIATION until this.

    ``join_witness``'s probability at each position comes from the DECLARED ORACLE at that position,
    so its Brier is 0.0 exactly when the scorer's derived outcome matches the oracle AT EVERY
    POSITION — and leaves 0 the moment any outcome moves to a different trial, because the witness
    is then confidently wrong at both ends of the move.
    """
    season = score_season(pack_signal_join_observable)
    witness = next(row for row in season.rows if row.agent_id == "join_witness")

    # NON-VACUOUS: the witness must actually have been scored over the whole season.
    assert witness.avg_brier is not None
    assert season.sample_size == len(_OUTCOMES_QUALIFIED) == 44
    assert witness.active_decisions == 44, "the witness commits on every trial; nothing is abstained"

    # THE JOIN. Exactly zero — not approximately — because every per-trial error is (1-1)**2 or
    # (0-0)**2, both exactly 0.0, so the sum is exact regardless of summation order.
    assert witness.avg_brier == 0.0, (
        "a non-zero Brier here means at least one derived outcome is attached to a different trial "
        "than the one it was derived from"
    )

    # DISCRIMINATION, RUN rather than asserted as arithmetic. An earlier version of this block
    # ended with `assert 2 / len(_OUTCOMES_QUALIFIED) != 0.0`, which is true of the integer 2 and
    # says nothing about the scorer — a tautology, and the only one in this file.
    #
    # The real check: build a pack whose SETTLEMENT is constructed from a transposed oracle, and
    # score the witness vector derived from the ORIGINAL oracle against it. The witness is then
    # confidently wrong at exactly the two transposed positions, so its Brier is exactly 2/44 —
    # the value the `== 0.0` assertion above rejects, produced by the scorer rather than by hand.
    assert _OUTCOMES_QUALIFIED[1] != _OUTCOMES_QUALIFIED[7], "the fixture must contain such a pair"
    transposed = list(_OUTCOMES_QUALIFIED)
    transposed[1], transposed[7] = transposed[7], transposed[1]
    original_witness = tuple(1.0 if outcome == 1 else 0.0 for outcome in _OUTCOMES_QUALIFIED)
    mismatched = _build_pack(
        "season-join-transposed",
        "qualified",
        tuple(transposed),
        diagnostic_agents=(DiagnosticAgent(agent_id="join_witness", probabilities=original_witness),),
    )
    stale = next(r for r in score_season(mismatched).rows if r.agent_id == "join_witness")
    assert stale.avg_brier == pytest.approx(2 / len(_OUTCOMES_QUALIFIED))
    assert stale.avg_brier != witness.avg_brier, "the two must be distinguishable, which is the point"


# --- B2. The C52 DISCRIMINATION control Region A is missing.


def test_exploratory_status_alone_suppresses_a_season_that_would_otherwise_qualify(full_pack_qualified):
    """PIN (C52 DISCRIMINATION): the ``season_status`` conjunct of the gate is load-bearing.

    The plan's own exploratory test runs on SIX trials, where ``min_active=20`` is unreachable and
    every row is unqualified for a reason that has nothing to do with ``season_status``. It would
    pass identically against a scorer that never consulted the status at all. This runs the exact
    same 44 trials under both statuses: the qualified twin must produce ranked rows, the
    exploratory twin must produce none.
    """
    qualified = score_season(full_pack_qualified)
    assert sum(1 for row in qualified.rows if row.qualified) >= 3  # ACCEPTANCE: the gate can FIRE

    exploratory_pack = replace(full_pack_qualified, meta=_meta("season-full", "exploratory"))
    exploratory = score_season(exploratory_pack)
    assert exploratory.season_status == "exploratory"
    assert [row.qualified for row in exploratory.rows] == [False] * len(exploratory.rows)

    # DISCRIMINATION: the two runs differ in the `qualified` FLAG and in nothing else measured.
    # Compared by agent id rather than positionally, because the two ORDERINGS legitimately differ:
    # the rank key leads with the qualified/unqualified partition, and under an exploratory status
    # that partition is empty, so rows that were held behind the qualified block move up. The row
    # SET and every per-agent metric are identical; only the verdict changed.
    qualified_by_id = {row.agent_id: row for row in qualified.rows}
    exploratory_by_id = {row.agent_id: row for row in exploratory.rows}
    assert set(exploratory_by_id) == set(qualified_by_id)
    for agent_id, exploratory_row in exploratory_by_id.items():
        assert exploratory_row == replace(qualified_by_id[agent_id], qualified=False), agent_id
    assert any(qualified_by_id[agent_id].qualified for agent_id in qualified_by_id), (
        "non-vacuous: at least one row DID qualify under the qualified status"
    )


def test_active_count_and_coverage_each_independently_block_qualification(full_pack_qualified):
    """PIN (C52): each of the other two conjuncts can also FIRE and SEPARATE on its own.

    ``min_active`` is raised above every agent's active count, then ``min_coverage`` above every
    agent's coverage, each in isolation, with the season status left ``qualified``. If either
    conjunct were dropped, its run would still emit qualified rows.
    """
    baseline = score_season(full_pack_qualified)
    assert sum(1 for row in baseline.rows if row.qualified) >= 3

    by_active = score_season(full_pack_qualified, min_active=45)
    assert [row.qualified for row in by_active.rows] == [False] * len(by_active.rows)

    by_coverage = score_season(full_pack_qualified, min_coverage=1.01)
    assert [row.qualified for row in by_coverage.rows] == [False] * len(by_coverage.rows)


def test_qualification_gate_uses_inclusive_thresholds(full_pack_qualified):
    """PIN: ``active >= 20`` and ``coverage >= 0.50`` are INCLUSIVE, as the frozen rule spells them.

    ``climatology`` is the discriminating row: it takes exactly 34 active decisions out of 44
    scored trials, so setting the thresholds to exactly its own values must leave it qualified,
    while one step above either must not.
    """
    climatology = next(r for r in score_season(full_pack_qualified).rows if r.agent_id == "climatology")
    assert climatology.active_decisions == 34
    assert climatology.active_coverage == pytest.approx(34 / 44)

    at_bound = score_season(full_pack_qualified, min_active=34, min_coverage=34 / 44)
    assert next(r for r in at_bound.rows if r.agent_id == "climatology").qualified is True
    above_bound = score_season(full_pack_qualified, min_active=35)
    assert next(r for r in above_bound.rows if r.agent_id == "climatology").qualified is False


# --- B3. C26: assert WHICH bucket a trial landed in, never merely that it was "not counted".
# --- `qualified`, `unscored` and coverage-excluded rows can all present as "not counted".


def test_unscored_is_excluded_from_coverage_rather_than_counted_as_a_miss():
    """PIN (C26): an UNSCORED trial leaves the coverage DENOMINATOR, and is not a silent miss.

    Four of 44 trials are pointed at a token with no settlement series. ``always_follow`` takes a
    directional stance on every trial it can see, so if unscored trials were counted as coverage
    misses its coverage would fall to 40/44; if they were counted as hits, ``sample_size`` would
    stay 44. Asserting all three of ``sample_size``, ``active_decisions`` and ``unscored`` together
    is what distinguishes the three ways a trial can fail to be counted.
    """
    unsettled = frozenset({3, 11, 27, 40})
    pack = _build_pack("season-holes", "qualified", _OUTCOMES_QUALIFIED, unsettled_positions=unsettled)
    season = score_season(pack)

    assert season.sample_size == 40, "sample_size counts SETTLED trials only"
    row = next(r for r in season.rows if r.agent_id == "always_follow")
    assert row.unscored == 4
    assert row.active_decisions == 40
    assert row.active_coverage == pytest.approx(1.0), "coverage is over SCORED trials, not all trials"
    # The invariant that makes both numbers recoverable by a reader of a single row.
    assert season.sample_size + row.unscored == len(pack.trials)


def test_abstain_is_scored_for_brier_but_excluded_from_active_coverage(full_pack_qualified):
    """PIN (C26): ABSTAIN is a THIRD bucket — Brier-scored, coverage-excluded, never unscored.

    ``neutral`` abstains on all 44 trials. All three of its counters must say different things:
    it has a real Brier (every eligible trial is scored, including neutral), zero active decisions,
    zero coverage, and zero unscored trials.
    """
    neutral = next(r for r in score_season(full_pack_qualified).rows if r.agent_id == "neutral")
    assert neutral.avg_brier == pytest.approx(0.25), "abstaining is SCORED, at the p=0.5 Brier floor"
    assert neutral.active_decisions == 0
    assert neutral.active_coverage == pytest.approx(0.0)
    assert neutral.unscored == 0, "an abstained trial settled; it is not unscored"
    assert neutral.capped_avg_markout_bps is None, "no active decision means no markout, not a zero"
    assert neutral.qualified is False


def test_an_all_unscored_season_reports_no_score_rather_than_a_zero():
    """PIN: with nothing settled, every metric is ``None``/0 and no row qualifies.

    A zero Brier is the best possible score; emitting one for an agent that was never scored would
    put an unmeasured agent at the top of the leaderboard.

    **Run at ``min_active=0, min_coverage=0.0``, which is the DISCRIMINATING setting.** At the frozen
    thresholds every row here is unqualified because ``active=0 < 20`` — a reason that has nothing to
    do with being unmeasured, so the test would pass against a scorer that qualified unmeasured
    agents. At zero thresholds the count and coverage conjuncts are both satisfied, and only the
    "no scored trial" conjunct can still refuse. This defect was live until mypy flagged the plan's
    own ``sorted(briers)`` as unsafe over ``float | None``: seven rows qualified with
    ``avg_brier=None``, and Region A's own assertion raised ``TypeError`` on them.
    """
    pack = _build_pack(
        "season-empty",
        "qualified",
        _OUTCOMES_QUALIFIED,
        unsettled_positions=frozenset(range(44)),
    )
    season = score_season(pack, min_active=0, min_coverage=0.0)
    assert season.sample_size == 0
    for row in season.rows:
        assert row.avg_brier is None
        assert row.capped_avg_markout_bps is None
        assert row.active_decisions == 0
        assert row.active_coverage == 0.0
        assert row.unscored == 44
        assert row.qualified is False, f"{row.agent_id} was never measured and must not be ranked"
    # The invariant Region A's `briers == sorted(briers)` and the rank key both depend on.
    assert all(row.avg_brier is not None for row in season.rows if row.qualified)


# --- B4. The climatology no-lookahead law, at the boundary Region A does not reach.


def test_the_outcome_oracle_is_INDEPENDENT_of_the_scorer_derivation_path():
    """PIN (C58 CONDITION): this module never imports or calls the scorer's outcome derivation.

    **This is the property that makes the climatology test worth having.** The fixture chooses a
    list of intended outcomes FIRST and constructs its synthetic settlement FROM it (``_build_pack``
    picks ``win_entry`` or ``loss_entry`` per declared outcome). The scorer then derives its own
    outcomes back out of ``settlement`` via ``select_settlement_candle`` + ``spot_markout``. The two
    are independent paths, so ``expected_11 = sum(pack.outcomes[:10]) / 10`` compares a construction
    against a derivation.

    If instead the fixture re-derived ``outcomes`` the scorer's way, that comparison would be the
    scorer's derivation against itself: the test would bind only the WINDOW — that ten priors were
    used — while APPEARING to bind the derivation too, and would pass identically with a broken
    derivation. That is a fixture symmetric under the transformation it claims to detect.

    Structural rather than a promise, following ``test_controls.py``'s import-purity pin: a future
    editor "simplifying" the fixture by calling ``spot_markout`` breaks this loudly instead of
    silently hollowing out three other tests.
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_modules: set[str] = set()
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_modules.add(node.module or "")
            imported_names.update(alias.name for alias in node.names)

    # NON-VACUOUS: the parser must actually have found this module's imports, or the assertions
    # below would be trivially true over an empty set.
    assert len(imported_modules) >= 10, f"examined only {len(imported_modules)} imported modules"
    assert "veridex.signal_trials.scoring" in imported_modules, "parser sanity: a known import is present"
    assert "score_season" in imported_names, "parser sanity: a known imported name is present"

    forbidden_modules = {"veridex.signal_trials.spot_markout", "veridex.signal_trials.primitives"}
    forbidden_names = {"spot_markout", "select_settlement_candle", "MarkoutResult", "brier_score"}
    assert not imported_modules & forbidden_modules, (
        f"the fixture must not reach the scorer's derivation path: {sorted(imported_modules & forbidden_modules)}"
    )
    assert not imported_names & forbidden_names, (
        f"the fixture must not reach the scorer's derivation path: {sorted(imported_names & forbidden_names)}"
    )

    # Belt and braces: no CALL to any of those names either, however it was reached.
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } | {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called, "a call set examined must be non-empty to mean anything"
    assert not called & forbidden_names, f"derivation path called directly: {sorted(called & forbidden_names)}"


def test_the_oracle_and_the_derivation_disagree_when_the_construction_changes():
    """PIN (C52 DISCRIMINATION): the oracle is genuinely capable of contradicting the scorer.

    The independence pin above is structural. This one is behavioural: it builds a pack whose
    DECLARED oracle deliberately disagrees with the settlement it was constructed from, and shows
    the scorer's derived outcomes follow the SETTLEMENT rather than the declaration. If the fixture
    were secretly re-deriving the oracle, the two could never disagree and this would be impossible
    to write.
    """
    honest = _build_pack("season-honest", "qualified", _OUTCOMES_QUALIFIED)
    # Same settlement construction, but the pack now DECLARES the opposite outcome vector.
    lying = replace(honest, outcomes=tuple(1 - outcome for outcome in _OUTCOMES_QUALIFIED))

    season = score_season(lying)
    probes = season.debug_climatology_inputs
    # The scorer read the SETTLEMENT, so its climatology matches the TRUE construction ...
    assert probes[10] == pytest.approx(sum(_OUTCOMES_QUALIFIED[:10]) / 10)
    # ... and NOT the pack's (false) declaration. The two are different numbers, which is exactly
    # what proves the plan's mandated assertion is comparing two independent things.
    assert probes[10] != pytest.approx(sum(lying.outcomes[:10]) / 10)


def test_climatology_probe_at_every_index_is_the_strict_prefix_mean(full_pack_qualified):
    """PIN: EVERY probe equals the mean of the strictly-prior outcomes, not just index 10.

    Region A pins indices 0, 9 and 10. A scorer that fed the strict prefix for the first eleven
    trials and the full pack thereafter would satisfy it completely. This checks all 44.
    """
    probes = score_season(full_pack_qualified).debug_climatology_inputs
    assert len(probes) == 44
    for index in range(44):
        prior = _OUTCOMES_QUALIFIED[:index]
        expected = 0.5 if len(prior) < 10 else sum(prior) / len(prior)
        assert probes[index] == pytest.approx(expected), f"climatology probe at trial {index}"


def test_climatology_is_not_the_full_pack_rate(full_pack_qualified):
    """PIN (C52 DISCRIMINATION): the honest feed is DISTINGUISHABLE from the leaked one.

    A test that only checked "probes[10] is a mean" would pass for a full-pack climatology too.
    The fixture is built so the two differ: the first-ten mean is 0.70 and the whole-season mean
    is 41/44 = 0.9318.
    """
    probes = score_season(full_pack_qualified).debug_climatology_inputs
    full_pack_rate = sum(_OUTCOMES_QUALIFIED) / len(_OUTCOMES_QUALIFIED)
    assert full_pack_rate == pytest.approx(41 / 44)
    assert probes[10] == pytest.approx(0.70)
    assert probes[10] != pytest.approx(full_pack_rate), "a full-pack feed would be indistinguishable"
    # No probe anywhere in the season may equal the full-pack rate reached only at the end.
    assert all(probes[index] != pytest.approx(full_pack_rate) for index in range(44))


def test_climatology_feed_is_guarded_at_the_call_boundary(full_pack_qualified, monkeypatch):
    """PIN (OBLIGATION 1): the scorer calls ``assert_not_full_pack`` with the 1-BASED ordinal.

    ``assert_not_full_pack`` raises on ``prior_len >= trial_index``. With ``n`` strictly-prior
    outcomes at 0-based position ``n``, a 0-based index makes that ``n >= n`` — true at EVERY
    position, so a 0-based caller is a total rejector. This records the conversion by capturing
    every call the scorer makes and asserting the invariant a correctly-fed 1-based caller
    satisfies: ``prior_len == trial_index - 1``.
    """
    seen: list[tuple[int, int, int]] = []
    import veridex.signal_trials.scoring as scoring_module

    # Bound from its DEFINING module: `scoring` imports this name but does not re-export it,
    # and `mypy --strict` refuses an implicit re-export. Same object either way — the
    # monkeypatch target below is still `scoring_module`, which is what the scorer calls.
    from veridex.signal_trials.controls import assert_not_full_pack as original

    def _recording(prior_len: int, pack_len: int, trial_index: int) -> None:
        seen.append((prior_len, pack_len, trial_index))
        original(prior_len=prior_len, pack_len=pack_len, trial_index=trial_index)

    monkeypatch.setattr(scoring_module, "assert_not_full_pack", _recording)
    score_season(full_pack_qualified)

    assert len(seen) == 44, "the guard runs once per trial, not once per season"
    for prior_len, pack_len, trial_index in seen:
        assert pack_len == 44
        assert prior_len == trial_index - 1, "1-based ordinal at the boundary, 0-based prefix behind it"
    assert [trial_index for _, _, trial_index in seen] == list(range(1, 45))


def test_the_climatology_guard_actually_fires_on_a_full_pack_feed():
    """PIN (C52 ACCEPTANCE): the guard the scorer relies on can refuse, and names the leak.

    A guard that is wired but cannot fire is decoration. This is the ACCEPTANCE half of the pin
    above, which only proves the guard was CALLED correctly.
    """
    from veridex.signal_trials.controls import FullPackClimatologyError, assert_not_full_pack

    with pytest.raises(FullPackClimatologyError, match="strictly prior"):
        assert_not_full_pack(prior_len=44, pack_len=44, trial_index=11)


# --- B5. The rank key: totality, and the exact ordering it claims.


def test_rank_key_is_total_over_fully_equal_agents(full_pack_qualified_with_brier_ties):
    """PIN: two agents equal on EVERY metric are still ordered, deterministically, by ``agent_id``.

    A rank key that is not total lets the leaderboard reorder between runs on nothing but dict or
    sort accident. ``tie_alpha`` and ``tie_beta`` are equal on every field but their id.
    """
    season = score_season(full_pack_qualified_with_brier_ties)
    rows = {row.agent_id: row for row in season.rows}
    alpha, beta = rows["tie_alpha"], rows["tie_beta"]
    assert alpha.avg_brier == beta.avg_brier
    assert alpha.capped_avg_markout_bps == beta.capped_avg_markout_bps
    assert alpha.active_decisions == beta.active_decisions
    ordered = [row.agent_id for row in season.rows]
    assert ordered.index("tie_alpha") < ordered.index("tie_beta")
    assert ordered.index("tie_beta") == ordered.index("tie_alpha") + 1, "the equal pair ranks adjacently"


def test_rank_order_is_exactly_the_designed_order(full_pack_qualified):
    """PIN (FIXTURE DESIGN): this fixture's five qualified rows land in their hand-computed order.

    Region A checks ``briers == sorted(briers)``, which a scorer emitting a single qualified row
    would satisfy vacuously and which says nothing about the later keys.

    **THIS TEST DOES NOT CARRY THE FROZEN ORDERING RULE, and the distinction is load-bearing.** A
    hard-coded list is only as strong as the fixture behind it: swap the *markout* and *active-count*
    terms and this fixture happens to emit the same order, so the assertion below would keep passing
    over a violated rule. The rule is carried by
    ``test_the_frozen_four_term_ordering_holds_within_the_qualified_set``, which RECOMPUTES the
    frozen key over every fixture in this module that emits ranked rows — DESCRIBED RATHER THAN
    COUNTED, because an embedded count is one more number that drifts the moment a fixture is
    added, exactly as the line numbers in the suppression note did.

    What this test pins is narrower and still worth having: that the fixture's designed order is
    the one it actually produces, so the other tests built on that design are reasoning about the
    season they think they are.
    """
    season = score_season(full_pack_qualified)
    ranked = [row.agent_id for row in season.rows if row.qualified]
    assert ranked == ["always_follow", "climatology", "flow_follower", "crowding_fader", "always_fade"]
    # Unqualified rows sort AFTER every qualified one, so rows[0] is the actual season leader.
    qualified_flags = [row.qualified for row in season.rows]
    assert qualified_flags == sorted(qualified_flags, reverse=True)


def _frozen_rank_key(row):
    """The frozen plan's FOUR-term key, line 633, with no qualification partition.

    Written out here INDEPENDENTLY of ``scoring._rank_key`` on purpose. A pin that imported the
    production key would be comparing the implementation against itself and would hold whatever the
    implementation happened to do; this is the plan's rule, transcribed from the plan.
    """
    brier_key = (1, 0.0) if row.avg_brier is None else (0, row.avg_brier)
    markout_key = (1, 0.0) if row.capped_avg_markout_bps is None else (0, -row.capped_avg_markout_bps)
    return (brier_key, markout_key, -row.active_decisions, row.agent_id)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "full_pack_qualified",
        "full_pack_qualified_with_brier_ties",
        "pack_tied_on_brier_and_markout",
        "pack_signal_join_observable",
        "pack_ext_varies_by_trial",
    ],
)
def test_the_frozen_four_term_ordering_holds_within_the_qualified_set(fixture_name, request):
    """PIN (F2(b)): the emitted qualified order EQUALS the frozen four-term order — RECOMPUTED.

    **Recomputed, never restated.** ``test_rank_order_is_exactly_the_designed_order`` asserts one
    hard-coded five-agent list, and a hard-coded list is only as strong as the fixture that produced
    it: swap the *markout* and *active-count* terms and that fixture happens to emit the same order,
    so the assertion would keep passing over a violated rule. Sorting by an independently
    transcribed key cannot go stale that way — it re-derives the expectation from the RULE on every
    run rather than from one remembered result.

    **Run over ALL THREE fixtures**, because each reaches terms the others do not:
    ``full_pack_qualified`` has five qualified rows with distinct Briers (term 1); the ties fixture
    reaches terms 2 and 4; and ``pack_tied_on_brier_and_markout`` is the only one carrying a pair
    whose MARKOUT and ACTIVE COUNT DISAGREE about the order, which is what makes the POSITION of
    terms 2 and 3 observable. Without that pair, swapping those two terms reorders nothing anywhere
    and the mutant survives — measured, it did.

    Note what this does and does not establish. The leading ``not row.qualified`` term CANNOT
    displace the frozen order — it is constant ``False`` across every qualified row, and a constant
    leading key is order-preserving within the group where it is constant. So this holds by
    construction rather than by luck. Its value is as a REGRESSION barrier on the frozen rule
    itself, not as a check on the partition term.
    """
    pack = request.getfixturevalue(fixture_name)
    season = score_season(pack)

    qualified = [row for row in season.rows if row.qualified]
    assert len(qualified) >= 5, "non-vacuous: the frozen key must have something to order"
    assert qualified == sorted(qualified, key=_frozen_rank_key)


def test_the_qualification_partition_is_a_declared_deviation_pinned_in_both_directions(full_pack_qualified):
    """PIN (F2): the leading ``not qualified`` term is PRESENT, and has NOT displaced the frozen order.

    ``_rank_key`` sorts on ``(not qualified, <the frozen four>)``. The leading term is a declared
    deviation from frozen plan line 633 — see the DECLARED DEVIATION section of ``scoring``'s module
    docstring for why it stays. It was previously unpinned in BOTH directions, and this closes both.

    **DIRECTION 2 is what this test uniquely contributes. DIRECTION 1 is REDUNDANT against this
    fixture set, and that is recorded rather than left to imply otherwise.** Measured:
    ``test_rank_key_is_brier_then_capped_markout`` and ``test_rank_order_is_exactly_the_designed_order``
    already catch the same key-order swap DIRECTION 1 catches, so DIRECTION 1 fires but is not
    independently load-bearing. It is kept because it states the property where a reader looks for
    it, not because it closes a gap of its own. **It also does NOT catch the term-3 DIRECTION
    defect** — that needed its own fixture and pin
    (``test_active_count_is_the_THIRD_rank_key_and_sorts_DESCENDING``).

    **Why DIRECTION 2 could not be closed by anything existing.** Under the frozen thresholds the
    shipped ordering and the pure frozen ordering COINCIDE on the qualified subsequence, and every
    existing rank test — including the plan's own mandated one — filters to qualified rows before
    looking. So nothing else can see the partition term at all.
    """
    season = score_season(full_pack_qualified)
    shipped = [row.agent_id for row in season.rows]

    # DIRECTION 1 — the frozen ordering is untouched inside the qualified set — is owned by
    # `test_the_frozen_four_term_ordering_holds_within_the_qualified_set`, which RECOMPUTES the key
    # and runs over BOTH fixtures. It is not repeated here.

    # --- DIRECTION 2: the partition term is PRESENT and does OBSERVABLE work. ---
    # The witness: an UNQUALIFIED row whose Brier beats two QUALIFIED rows. Under the pure frozen
    # key it outranks them; under the shipped key it must sit behind them.
    rows = {row.agent_id: row for row in season.rows}
    witness = rows["selective_calibrator"]
    assert witness.qualified is False
    assert witness.active_decisions == 0, "unqualified because it never took a directional decision"
    assert witness.avg_brier is not None

    outranked = ["crowding_fader", "always_fade"]
    for agent_id in outranked:
        other = rows[agent_id]
        assert other.qualified is True
        # A QUALIFIED ROW ALWAYS HAS A BRIER. Asserted rather than assumed: it is the invariant
        # `_score_agent`'s `scored > 0` conjunct exists to hold, and the rank key depends on it.
        # It also narrows `float | None` to `float`, which is what keeps the comparison below from
        # tripping `operator` in REGION B — where the file-scoped directive would have swallowed it
        # silently. A pin first, a type narrowing second.
        assert other.avg_brier is not None
        # NON-VACUOUS: the witness genuinely has the better score, so being placed behind it is a
        # decision the partition made and not an accident of the Brier ordering agreeing anyway.
        assert witness.avg_brier < other.avg_brier
        assert shipped.index("selective_calibrator") > shipped.index(agent_id)

    # And stated as the difference between the two orderings, so the term's effect is exhibited
    # rather than inferred: they must AGREE on the qualified rows and DISAGREE over the whole set.
    frozen_order = [row.agent_id for row in sorted(season.rows, key=_frozen_rank_key)]
    assert shipped != frozen_order, "the partition term must be observable somewhere"
    assert [a for a in shipped if rows[a].qualified] == [a for a in frozen_order if rows[a].qualified]


def test_active_count_is_the_THIRD_rank_key_and_sorts_DESCENDING(pack_tied_on_brier_and_markout):
    """PIN (finding B): rank term 3 is ACTIVE COUNT DESCENDING, and its DIRECTION is pinned.

    **This closes a real gap rather than restating a covered one.** A mutant flipping only the sign
    of ``-row.active_decisions`` SURVIVED the entire 819-test suite at two consecutive heads,
    load-proven — so it was a genuine SURVIVED, not an inconclusive. No fixture exercised term 3:
    everywhere else the first two keys had already decided the order before it was reached.

    Three assertions, each closing a different way term 3 can be wrong:

    * the two rows really are tied on keys 1 and 2 (otherwise this test is about something else);
    * MORE active decisions ranks HIGHER — the direction the frozen rule mandates;
    * the winner is the alphabetically LATER id, so the result cannot be produced by the
      ``agent_id`` term standing in for a deleted term 3.
    """
    season = score_season(pack_tied_on_brier_and_markout)
    rows = {row.agent_id: row for row in season.rows}
    high, low = rows["zz_active_32"], rows["aa_active_24"]

    assert high.qualified is True and low.qualified is True, "both must be RANKED for term 3 to apply"

    # Keys 1 and 2 are EXACTLY tied — asserted with `==`, not approx, because the construction is
    # exact in binary floating point. If either ever drifts, this test is silently about key 1 or 2.
    assert high.avg_brier == low.avg_brier, "key 1 (avg Brier) must be exactly tied"
    assert high.capped_avg_markout_bps == low.capped_avg_markout_bps, "key 2 (markout) must be tied"
    assert high.capped_avg_markout_bps == 500

    # RECOMPUTED FROM THE FIXTURE'S OWN VECTORS, never restated. An arithmetic claim written out by
    # hand is a claim about what the author believed, and this one was in fact stated wrongly once
    # while still describing a correct design — a check that reaches the right answer down a wrong
    # path is a coincidence, not a verification. Deriving both sums here means the tie is re-proved
    # from the fixture on every run, and any edit to the vectors that breaks it fails loudly.
    pack = pack_tied_on_brier_and_markout
    by_id = {agent.agent_id: agent.probabilities for agent in pack.diagnostic_agents}
    expected = {}
    for agent_id, probabilities in by_id.items():
        expected[agent_id] = sum((p - o) ** 2 for p, o in zip(probabilities, pack.outcomes, strict=True)) / len(
            pack.outcomes
        )
    assert expected["zz_active_32"] == expected["aa_active_24"], "the tie must be EXACT, not approximate"
    assert high.avg_brier == expected["zz_active_32"]
    assert low.avg_brier == expected["aa_active_24"]

    # Key 3 differs, and it is the only thing left that can order them.
    assert high.active_decisions == 32
    assert low.active_decisions == 24

    ranked = [row.agent_id for row in season.rows if row.qualified]
    assert ranked.index("zz_active_32") < ranked.index("aa_active_24"), (
        "MORE active decisions must rank HIGHER: term 3 is DESCENDING"
    )
    assert ranked.index("aa_active_24") == ranked.index("zz_active_32") + 1, "the tied pair ranks adjacently"

    # DISCRIMINATION against term-3 DELETION, not just sign inversion: the correct winner is the
    # alphabetically LATER id, so a key that fell through to `agent_id` would order them backwards.
    assert "zz_active_32" > "aa_active_24", "the fixture's names are deliberately anti-alphabetical"


def test_markout_breaks_a_brier_tie_before_active_count(full_pack_qualified_with_brier_ties):
    """PIN: the SECOND rank key is exercised with an exactly-equal first key and unequal markout.

    Region A's tiebreak test asserts ``ranked[0].markout >= ranked[1].markout``, which holds for
    any pair whose markouts happen to be ordered — including one that never tied on Brier at all.
    This pins the equality of the first key, so the assertion is about the SECOND key.
    """
    season = score_season(full_pack_qualified_with_brier_ties)
    ranked = [row for row in season.rows if row.qualified]
    assert [ranked[0].agent_id, ranked[1].agent_id] == ["mo_high", "mo_low"]
    assert ranked[0].avg_brier == ranked[1].avg_brier, "the first rank key must be EXACTLY tied here"
    assert ranked[0].capped_avg_markout_bps == 268
    assert ranked[1].capped_avg_markout_bps == 247


def test_per_event_markout_is_capped_before_averaging(full_pack_qualified_with_brier_ties):
    """PIN: the ±500 cap is applied PER EVENT, not to the average.

    ``always_follow`` sees 22 winners worth +1101 bps each and 22 losers worth -60. Capping per
    event gives (22*500 + 22*-60)/44 = 220. Capping the AVERAGE instead would give
    (22*1101 + 22*-60)/44 = 520 capped to 500 — a number 2.3x larger, from the same trials.
    """
    season = score_season(full_pack_qualified_with_brier_ties)
    always_follow = next(r for r in season.rows if r.agent_id == "always_follow")
    assert always_follow.capped_avg_markout_bps == 220
    assert always_follow.capped_avg_markout_bps != 500


def test_display_bands_are_inclusive_at_both_bounds(full_pack_qualified_with_brier_ties):
    """PIN: ``p == 0.60`` is FOLLOW and ``p == 0.40`` is FADE — both bounds INCLUSIVE.

    Nothing else in any fixture sits exactly on a bound, so without this an exclusive ``>`` / ``<``
    in ``_stance`` would change no observable behaviour anywhere in the suite and a mutant flipping
    either comparison would survive it entirely.
    """
    rows = {row.agent_id: row for row in score_season(full_pack_qualified_with_brier_ties).rows}
    for agent_id in ("edge_follow_at_bound", "edge_fade_at_bound"):
        row = rows[agent_id]
        assert row.active_decisions == 44, f"{agent_id} sits ON the bound and must be ACTIVE"
        assert row.active_coverage == pytest.approx(1.0)
        assert row.capped_avg_markout_bps is not None

    # DISCRIMINATION: a probability one step INSIDE the band is ABSTAIN, so the assertion above is
    # about the bound and not about the scorer treating every probability as active.
    inside = _build_pack(
        "season-inside",
        "qualified",
        _OUTCOMES_TIES,
        win_entry=_BIG_WIN_ENTRY,
        loss_entry=_SMALL_LOSS_ENTRY,
        diagnostic_agents=(
            DiagnosticAgent(agent_id="just_inside_follow", probabilities=(0.5999999,) * 44),
            DiagnosticAgent(agent_id="just_inside_fade", probabilities=(0.4000001,) * 44),
        ),
    )
    inside_rows = {row.agent_id: row for row in score_season(inside).rows}
    assert inside_rows["just_inside_follow"].active_decisions == 0
    assert inside_rows["just_inside_fade"].active_decisions == 0


def test_score_season_signature_is_pinned():
    """PIN: ``score_season``'s signature — parameter NAMES, ORDER and defaults — is frozen.

    Recorded because every call in this suite is by keyword, which leaves parameter ORDER unpinned
    by construction: a mutant that reorders keyword-only parameters survives a suite that only ever
    calls them by name. H3.4 lost a round to exactly that.
    """
    signature = inspect.signature(score_season)
    assert list(signature.parameters) == ["pack", "min_active", "min_coverage", "markout_cap_bps", "thresholds"]
    assert signature.parameters["min_active"].default == 20
    assert signature.parameters["min_coverage"].default == 0.50
    assert signature.parameters["markout_cap_bps"].default == 500
    assert signature.parameters["thresholds"].default == (0.60, 0.40)
    for name in ("min_active", "min_coverage", "markout_cap_bps", "thresholds"):
        assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY

    guard = inspect.signature(assert_no_clv_fields)
    assert list(guard.parameters) == ["row"]


# --- B6. The roster is CLOSED, and the diagnostic extension can only ADD.


def test_the_frozen_roster_is_always_scored_even_with_no_diagnostics(full_pack_qualified):
    """PIN: a pack carrying NO diagnostic agents still scores all seven frozen roster members.

    A roster that silently shrinks is a leaderboard that flatters whoever remains.
    """
    plain = _build_pack("season-plain", "qualified", _OUTCOMES_QUALIFIED)
    ids = {row.agent_id for row in score_season(plain).rows}
    assert ids == FROZEN_ROSTER, "exactly the frozen roster, no more and no less, with no diagnostics"


def test_a_diagnostic_agent_cannot_shadow_a_frozen_roster_member():
    """PIN: a diagnostic whose id collides with a roster member is REFUSED, not silently merged.

    Shadowing is the one way the extension could become a flattery mechanism: replacing
    ``climatology`` with a hand-picked vector would move every comparison drawn against it.
    """
    pack = _build_pack(
        "season-shadow",
        "qualified",
        _OUTCOMES_QUALIFIED,
        diagnostic_agents=(DiagnosticAgent(agent_id="climatology", probabilities=(1.0,) * 44),),
    )
    with pytest.raises(ValueError, match="climatology"):
        score_season(pack)


def test_a_diagnostic_agent_with_the_wrong_vector_length_is_refused():
    """PIN: a probability vector that does not cover the season is refused, not zip-truncated.

    Silent truncation would score an agent over a prefix of the season while presenting it beside
    agents scored over all of it.
    """
    pack = _build_pack(
        "season-short",
        "qualified",
        _OUTCOMES_QUALIFIED,
        diagnostic_agents=(DiagnosticAgent(agent_id="short_vector", probabilities=(0.5,) * 43),),
    )
    with pytest.raises(ValueError, match="short_vector"):
        score_season(pack)


def test_duplicate_diagnostic_ids_are_refused():
    """PIN: two diagnostics sharing an id would make the rank key non-total again."""
    pack = _build_pack(
        "season-dupe",
        "qualified",
        _OUTCOMES_QUALIFIED,
        diagnostic_agents=(
            DiagnosticAgent(agent_id="dupe", probabilities=(0.5,) * 44),
            DiagnosticAgent(agent_id="dupe", probabilities=(0.9,) * 44),
        ),
    )
    with pytest.raises(ValueError, match="dupe"):
        score_season(pack)


def test_diagnostic_agents_are_flagged_as_controls(full_pack_qualified):
    """PIN: a diagnostic is never presented as one of the three frozen contestants."""
    season = score_season(full_pack_qualified)
    contestants = {row.agent_id for row in season.rows if not row.is_control}
    assert contestants == {"flow_follower", "crowding_fader", "selective_calibrator"}


# --- B7. The CLV rank-guard.


def test_clv_guard_rejects_every_denied_field_and_accepts_a_clean_row():
    """PIN (C52, both halves): the guard SEPARATES — it fires on each denied name and no-ops clean.

    ``pytest.raises(ValueError)`` alone cannot tell a guard that rejects everything from one that
    rejects the right thing, so the clean-row leg is not optional. ``match=`` binds each refusal to
    the field that caused it.
    """
    from veridex.signal_trials.scoring import CLV_RANK_DENYLIST

    assert len(CLV_RANK_DENYLIST) >= 3, "a denylist examined must be non-empty to mean anything"
    for field in sorted(CLV_RANK_DENYLIST):
        with pytest.raises(ValueError, match=field):
            assert_no_clv_fields({"agent_id": "x", field: 1})

    clean = {
        "agent_id": "flow_follower",
        "is_control": False,
        "qualified": True,
        "avg_brier": 0.1,
        "capped_avg_markout_bps": 12,
        "active_decisions": 30,
        "active_coverage": 0.7,
        "unscored": 0,
    }
    assert_no_clv_fields(clean)  # a clean row is a strict no-op: this must not raise


def test_the_clv_guard_is_invoked_from_the_RANK_KEY_itself(full_pack_qualified, monkeypatch):
    """PIN: ``_rank_key`` CALLS the guard — proven by counting, not by reading the source.

    Added because a mutation drill found the gap: replacing the guard call inside ``_rank_key`` with
    ``pass`` left the entire suite green. Every other CLV test either calls the guard directly or
    checks rows that are already clean, so none of them could tell a wired guard from a removed one
    — which is the exact "the guard is decoration" failure the rank-guard pattern exists to prevent.
    ``rank_makers`` guards its key for the same reason: it closes the direct-sort bypass.
    """
    import veridex.signal_trials.scoring as scoring_module

    calls: list[str] = []
    original = scoring_module.assert_no_clv_fields

    def _counting(row: dict[str, Any]) -> None:
        calls.append(str(row.get("agent_id")))
        original(row)

    monkeypatch.setattr(scoring_module, "assert_no_clv_fields", _counting)
    season = score_season(full_pack_qualified)

    # `score_season` does not call `season_document`, so every call counted here came from the
    # rank key — once per row, which is what `sorted(key=...)` does.
    assert sorted(calls) == sorted(row.agent_id for row in season.rows)
    assert len(calls) == 8, "one guard call per ranked row"


def test_the_published_row_shape_passes_its_own_clv_guard(full_pack_qualified):
    """PIN: the guard is LIVE on the rows the scorer actually emits, not merely available.

    A rank guard that is never called from the rank path is decoration; ``rank_makers`` calls its
    guard on every row for exactly this reason.
    """
    import dataclasses

    rows = score_season(full_pack_qualified).rows
    assert len(rows) == 8, "a row set examined must be non-empty to mean anything"
    for row in rows:
        assert_no_clv_fields(dataclasses.asdict(row))  # must not raise on any emitted row


def test_agent_season_row_carries_no_clv_or_execution_field():
    """PIN: the ROW TYPE itself cannot carry a denied field, checked against its annotations."""
    from veridex.signal_trials.scoring import CLV_RANK_DENYLIST

    fields = set(AgentSeasonRow.__dataclass_fields__)
    assert fields, "a field set examined must be non-empty"
    assert not fields & CLV_RANK_DENYLIST


# --- B8. Determinism, beyond object equality.


def test_determinism_holds_across_independently_built_equal_packs():
    """PIN: determinism is a property of the PACK CONTENT, not of reusing one Python object.

    Region A's ``test_determinism`` calls the scorer twice on the SAME object, which a scorer
    caching its result on the instance would also pass. These are two separately constructed packs.
    """
    first = _build_pack("season-det", "qualified", _OUTCOMES_QUALIFIED)
    second = _build_pack("season-det", "qualified", _OUTCOMES_QUALIFIED)
    assert first is not second
    assert score_season(first) == score_season(second)


def test_trials_are_scored_chronologically_regardless_of_pack_order():
    """PIN: the season is scored in CHRONOLOGICAL order, which is what makes 'strictly prior' mean
    anything. A pack whose trials arrive shuffled must produce the identical season.

    Without this, 'strictly prior' would silently mean 'earlier in the file', and a re-sealed pack
    with a different write order would score differently.
    """
    ordered = _build_pack("season-order", "qualified", _OUTCOMES_QUALIFIED)
    shuffled_trials = tuple(reversed(ordered.trials))
    shuffled = replace(ordered, trials=shuffled_trials)
    assert shuffled.trials != ordered.trials
    assert score_season(shuffled) == score_season(ordered)


# --- B9. OBLIGATION 3 — CanonicalSignal validation, BOTH legs, all four scorer-relevant fields.


@pytest.mark.parametrize(
    "field",
    ["trigger_wallet_count", "amount_usd", "top10_holder_percent", "market_cap_usd"],
)
def test_canonical_signal_field_is_required_when_removed(field):
    """PIN (OBLIGATION 3, leg a): the field REMOVED must fail validation.

    §5.3's "any missing field -> 0.5" rule holds only because none of these is ``Optional``. If one
    became optional the frozen rule would be silently falsified with no test failing — so both this
    leg and the nullability leg below are required.
    """
    payload: dict[str, Any] = _signal(_t0_for(0), _WIN_ENTRY).model_dump()
    del payload[field]
    with pytest.raises(ValidationError, match=field):
        CanonicalSignal(**payload)


@pytest.mark.parametrize(
    "field",
    ["trigger_wallet_count", "amount_usd", "top10_holder_percent", "market_cap_usd"],
)
def test_canonical_signal_field_is_not_nullable_when_present_as_none(field):
    """PIN (OBLIGATION 3, leg b): the field PRESENT AS ``None`` must fail validation.

    The leg that catches what the removal leg cannot: a Pydantic annotation can stay REQUIRED while
    becoming NULLABLE. ``X | None`` with no default is still required, so leg (a) would keep passing
    while every contestant formula started receiving ``None``.
    """
    payload: dict[str, Any] = _signal(_t0_for(0), _WIN_ENTRY).model_dump()
    payload[field] = None
    with pytest.raises(ValidationError, match=field):
        CanonicalSignal(**payload)


# --- B10. The season document and the no_season branch of the publish script.


def test_season_document_matches_the_wire_schema_the_router_serves(full_pack_qualified, tmp_path):
    """PIN: the published document validates against the H1.2 frozen response model.

    The scorer writes into the same repository the router reads. A document that does not satisfy
    ``SignalTrialsSeasonResponse`` would publish a season the API cannot serve.
    """
    from scripts.signal_trials.score_and_publish import publish_season
    from veridex.api.signal_trials_schemas import SignalTrialsSeasonResponse
    from veridex.signal_trials.published import read_season, read_state

    season = score_season(full_pack_qualified)
    publish_season(tmp_path, season, full_pack_qualified.ref.content_hash)

    assert read_state(tmp_path)["state"] == "qualified"
    document = read_season(tmp_path)
    assert document is not None
    validated = SignalTrialsSeasonResponse(**document)
    assert validated.season_status == "qualified"
    assert validated.sample_size == 44
    assert [row.agent_id for row in validated.rows] == [row.agent_id for row in season.rows]


def test_debug_climatology_inputs_NEVER_REACHES_the_published_document(full_pack_qualified, tmp_path):
    """PIN (C58, Hazard A binding addition): the debug field is scorer-internal and must not publish.

    ``SeasonResult`` is what ``write_season`` publishes and the H1.2 router serves, so a
    ``debug_``-prefixed introspection field appearing in a public response would be a schema break in
    the one contract the frontend is about to mirror.

    **Checked against the BYTES ON DISK, not the serializer's current behaviour.** Two reasons that
    matters. ``SignalTrialsSeasonResponse`` is a plain pydantic model, so it IGNORES unknown keys
    rather than rejecting them — validating the document would therefore stay green while the extra
    key sat in the published artifact. And a refactor to a dataclass-walking serializer
    (``asdict(season)`` instead of the explicit dict ``season_document`` builds) would start
    including the field with no test noticing. A substring check over the written file survives both.
    """
    from scripts.signal_trials.score_and_publish import publish_season
    from veridex.api.signal_trials_schemas import SignalTrialsSeasonResponse
    from veridex.signal_trials.scoring import season_document

    season = score_season(full_pack_qualified)

    # NON-VACUOUS / C52 ACCEPTANCE: the field really is present and non-trivial on the SOURCE
    # object, so its absence downstream is a fact about the boundary and not about an empty field.
    assert len(season.debug_climatology_inputs) == 44
    assert season.debug_climatology_inputs[10] == pytest.approx(0.70)

    document = season_document(season)
    assert "debug_climatology_inputs" not in document
    # The document's keys are EXACTLY the frozen wire schema's — no extras of any kind, derived
    # from the model rather than restated, so a schema change moves this test with it.
    assert set(document) == set(SignalTrialsSeasonResponse.model_fields)

    # Nothing debug-prefixed anywhere in the structure, at any depth.
    def _walk(node: Any) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).startswith("debug"):
                    found.append(str(key))
                found.extend(_walk(value))
        elif isinstance(node, list):
            for item in node:
                found.extend(_walk(item))
        return found

    assert _walk(document) == []

    # And the decisive check: the BYTES that actually land on disk.
    publish_season(tmp_path, season, full_pack_qualified.ref.content_hash)
    written = (tmp_path / "published" / "season.json").read_text(encoding="utf-8")
    assert written, "an artifact examined must be non-empty to mean anything"
    assert "debug" not in written
    assert "climatology_inputs" not in written
    # DISCRIMINATION: the artifact IS the season — it carries the roster — so the absence above is
    # not the absence of everything.
    assert "climatology" in written, "the climatology ROW is published; only the debug probe is not"


def test_score_and_publish_skips_and_exits_zero_under_no_season(tmp_path, capsys):
    """PIN (C54): under a ``no_season`` state the scorer DECLINES; it does not fail and does not run.

    ``not_built`` means the scorer never ran; ``no_season`` means it ran and declined. Exiting
    non-zero here would make an intentional refusal look like a broken pipeline.
    """
    from scripts.signal_trials.score_and_publish import main
    from veridex.signal_trials.published import read_state, write_state

    write_state(tmp_path, "no_season", {"reason": "preflight declined"})
    exit_code = main(["--data-dir", str(tmp_path), "--pack-dir", str(tmp_path / "no-such-pack")])

    assert exit_code == 0
    assert "skipped: no_season" in capsys.readouterr().out
    # The state is left exactly as the preflight wrote it — the scorer records nothing.
    assert read_state(tmp_path)["state"] == "no_season"


def test_score_and_publish_does_not_read_the_pack_under_no_season(tmp_path):
    """PIN: the ``no_season`` branch returns BEFORE touching the pack.

    The pack directory named here does not exist. If the branch were checked after the load, this
    would raise instead of exiting 0 — which is the difference between 'declined' and 'crashed'.
    """
    from scripts.signal_trials.score_and_publish import main
    from veridex.signal_trials.published import write_state

    write_state(tmp_path, "no_season", {"reason": "preflight declined"})
    missing_pack = tmp_path / "definitely-absent"
    assert not missing_pack.exists()
    assert main(["--data-dir", str(tmp_path), "--pack-dir", str(missing_pack)]) == 0


def test_score_and_publish_scores_when_the_state_is_not_built(tmp_path, full_pack_qualified, monkeypatch):
    """PIN (C54 DISCRIMINATION): ``not_built`` does NOT skip — it is the ordinary first run.

    The sibling of the test above. Without it, a scorer that skipped on every state would pass the
    ``no_season`` test and never score anything.
    """
    import scripts.signal_trials.score_and_publish as script

    monkeypatch.setattr(script, "load_pack", lambda ref: full_pack_qualified)
    exit_code = script.main(
        [
            "--data-dir",
            str(tmp_path),
            "--pack-dir",
            str(tmp_path / "pack"),
            "--expected-content-hash",
            full_pack_qualified.ref.content_hash,
        ]
    )
    assert exit_code == 0

    from veridex.signal_trials.published import read_state

    assert read_state(tmp_path)["state"] == "qualified"


def test_scoreable_publish_refuses_without_the_named_human_approved_hash(
    tmp_path, full_pack_qualified, monkeypatch, capsys
):
    """A missing approval stops before any pack bytes are inspected."""
    import scripts.signal_trials.score_and_publish as script

    touched: list[str] = []

    def _unexpected_read(_value):
        touched.append("pack")
        return full_pack_qualified

    monkeypatch.setattr(script, "load_pack", _unexpected_read)

    exit_code = script.main(["--data-dir", str(tmp_path), "--pack-dir", str(tmp_path / "pack")])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert touched == []
    assert "--expected-content-hash" in captured.err


def test_scoring_loads_the_exact_approved_pack_reference(
    tmp_path, full_pack_qualified, monkeypatch
):
    """The human-approved digest, not a digest re-read from disk, reaches verification."""
    import argparse

    import scripts.signal_trials.score_and_publish as script
    from veridex.signal_trials.pack import PackRef

    pack_dir = tmp_path / "pack"
    approved_hash = "a" * 64
    disk_hash = "b" * 64
    seen: list[PackRef] = []
    monkeypatch.setattr(
        script,
        "parse_args",
        lambda _argv=None: argparse.Namespace(
            data_dir=tmp_path,
            pack_dir=pack_dir,
            expected_content_hash=approved_hash,
        ),
    )
    monkeypatch.setattr(
        script, "read_pack_ref", lambda _path: PackRef(pack_dir, disk_hash), raising=False
    )

    def _load(ref: PackRef):
        seen.append(ref)
        return full_pack_qualified

    monkeypatch.setattr(script, "load_pack", _load)

    exit_code = script.main([])

    assert exit_code == 0
    assert seen == [PackRef(pack_dir, approved_hash)]


def test_publish_writes_the_payload_before_the_state(tmp_path, full_pack_qualified):
    """PIN: write ORDER is payload-then-state, which is the writer's half of ``read_season``'s contract.

    ``published.read_season`` absorbs a payload with a stale state silently but REFUSES a state
    asserting a season that is not there. So a crash between the two writes must leave the
    absorbable arrangement, never the refused one.
    """
    from scripts.signal_trials.score_and_publish import publish_season

    written: list[str] = []
    import veridex.signal_trials.published as published_module

    original_write_json = published_module._atomic_write_json

    def _recording(path: Path, payload: dict[str, Any]) -> None:
        written.append(path.name)
        original_write_json(path, payload)

    published_module._atomic_write_json = _recording
    try:
        publish_season(
            tmp_path,
            score_season(full_pack_qualified),
            full_pack_qualified.ref.content_hash,
        )
    finally:
        published_module._atomic_write_json = original_write_json

    assert written == ["season.json", "state.json"]


def test_season_result_reports_the_combo_it_was_scored_under(full_pack_qualified):
    """PIN: the season carries the preflight combo verbatim, so a reader can see which market it
    settled against rather than inferring it from the trials."""
    season = score_season(full_pack_qualified)
    assert isinstance(season, SeasonResult)
    assert season.combo == full_pack_qualified.meta.combo
    assert season.season_id == full_pack_qualified.meta.season_id


def test_a_pack_declaring_no_season_is_refused_by_the_scorer():
    """PIN: a pack whose combo says ``no_season`` is a contradiction — the pack exists.

    Refused rather than scored, because publishing a ``no_season`` season document would put a
    payload on disk under a state that ``published`` defines as carrying none.
    """
    pack = _build_pack("season-contradiction", "no_season", _OUTCOMES_MINI)
    with pytest.raises(ValueError, match="no_season"):
        score_season(pack)
