import pytest

from veridex.signal_trials.okx_client import Candle, CandleSeries
from veridex.signal_trials.spot_markout import (
    SpotMarkoutError,
    assert_positive_price,
    select_settlement_candle,
    spot_markout,
)

BAR = 60_000; T0 = 1_000_000; H = 3_600_000; T = T0 + H
def _series(*cands): return CandleSeries(bar="1m", bar_ms=BAR, candles=tuple(cands))
def _c(open_ms, close=1.1, confirmed=True): return Candle(open_ms, 1.0, 1.2, 0.9, close, 5.0, 5.5, confirmed)

def test_follow_and_fade_both_pay_cost():
    r = spot_markout(entry=100.0, future=101.0, cost_bps=25)   # gross +100bps
    assert r.follow_markout_bps == 75 and r.fade_markout_bps == -125 and r.abstain_markout_bps == 0
    assert r.follow_profitable is True

def test_exact_close_boundary_is_valid():
    c = _c(T - BAR)   # close_ts == T
    assert select_settlement_candle(_series(c), t0_ms=T0, horizon_ms=H) is c

def test_positive_sub_bar_lag_accepted():
    c = _c(T - BAR + 30_000)   # close_ts = T + 30s → 0 < lag < BAR
    assert select_settlement_candle(_series(c), t0_ms=T0, horizon_ms=H) is c

def test_lag_of_full_bar_or_more_rejected():
    late = _c(T + BAR)   # close_ts = T + 2*BAR
    assert select_settlement_candle(_series(late), t0_ms=T0, horizon_ms=H) is None

def test_unconfirmed_rejected_and_missing_unscored():
    assert select_settlement_candle(_series(_c(T - BAR, confirmed=False)), t0_ms=T0, horizon_ms=H) is None
    assert select_settlement_candle(_series(), t0_ms=T0, horizon_ms=H) is None

def test_spot_prices_not_probability_bounded():
    assert assert_positive_price(3620.5, "entry") == 3620.5
    with pytest.raises(SpotMarkoutError): assert_positive_price(0.0, "entry")


# --- Regression pins appended after the QUALITY review of 2cfc047 (Q1, INFO-1, INFO-2). ---
# The mandated block above is byte-identical to the REGION A mandate and is not touched. Each pin
# below was captured FAILING against the specific mutation it exists to kill: absence-RED is
# impossible for behaviour that already exists and is already correct, so mutant-RED stands in.
# These pins require NO production-code change; they pass against the implementation as reviewed.


def test_min_close_ts_wins_regardless_of_wire_order():
    """The MIN-selection rule itself, which every mandated test leaves unpinned.

    Each mandated selection test builds a series of at most ONE candle, so any function returning
    *some* eligible candle passes them: min -> max, min -> first-in-wire-order and
    min -> last-in-wire-order all survive. Two eligible candles at distinct close_ts, asserted in
    both wire orders, kill all three at once.
    """
    early = _c(T - BAR, close=100.0)  # close_ts == T, lag 0
    late = _c(T - BAR + 30_000, close=999.0)  # close_ts == T + 30s, still inside the window
    assert select_settlement_candle(_series(early, late), t0_ms=T0, horizon_ms=H) is early
    assert select_settlement_candle(_series(late, early), t0_ms=T0, horizon_ms=H) is early


def test_follow_profitable_is_false_at_exact_breakeven():
    """A markout of exactly 0 is a wash, not a win. Kills ``> 0`` -> ``>= 0``."""
    r = spot_markout(entry=100.0, future=100.25, cost_bps=25)  # gross +25bps, follow exactly 0
    assert r.follow_markout_bps == 0
    assert r.follow_profitable is False


def test_rounding_is_nearest_ties_to_even_and_mirrors_under_negation():
    """Pins the rounding MODE, which the mandated vectors leave free.

    The mirror symmetry ``fade(+m) == follow(-m)`` is the property that makes the choice
    load-bearing: ``math.floor`` breaks it and would reintroduce a farmable directional bias while
    every mandated test stays green.
    """
    up = spot_markout(entry=10_000.0, future=10_002.5, cost_bps=0)  # gross exactly +2.5
    down = spot_markout(entry=10_000.0, future=9_997.5, cost_bps=0)  # gross exactly -2.5
    assert up.follow_markout_bps == 2  # ties to even, not 3
    assert down.follow_markout_bps == -2  # math.floor would give -3
    assert up.fade_markout_bps == down.follow_markout_bps  # fade(+m) == follow(-m)
    truncating = spot_markout(entry=16_384.0, future=16_386.5, cost_bps=0)  # gross 1.52587890625
    assert truncating.follow_markout_bps == 2  # int() and math.floor() would both give 1


# --- Eligibility-predicate pins appended after the Law milestone CODEX review of 4b0b2f3. ---
# The pins above fix the SELECTION rule (min). These fix the ELIGIBILITY PREDICATE, which was
# unpinned in three directions: the lower bound, the exclusive upper edge, and bar provenance.
# No production-code change: all three pass against the implementation as reviewed.


def test_close_strictly_before_t_is_rejected():
    """Lower bound of the half-open window. Kills deleting the ``0 <=`` term.

    No mandated case presents a candle that closes BEFORE ``T`` — every fixture closes at or after
    it — so a predicate testing only ``close_ts - T < bar_ms`` passes all of them unchanged. This
    candle closes at ``T - 1``, one millisecond short, which is the tightest form of the case.
    """
    early = _c(T - BAR - 1)  # close_ts == T - 1, lag == -1
    assert select_settlement_candle(_series(early), t0_ms=T0, horizon_ms=H) is None


def test_lag_of_exactly_one_bar_is_rejected():
    """The upper bound is EXCLUSIVE. Kills ``< bar_ms`` -> ``<= bar_ms``.

    ``test_lag_of_full_bar_or_more_rejected`` uses ``ts_open = T + BAR``, i.e. lag ``2 * BAR``,
    which an inclusive bound still rejects — so the excluded edge itself is never exercised.
    ``close_ts == T + BAR`` is exactly that edge: a whole bar closed between ``T`` and this one.
    """
    edge = _c(T)  # close_ts == T + BAR, lag == BAR exactly
    assert select_settlement_candle(_series(edge), t0_ms=T0, horizon_ms=H) is None


def test_non_1m_series_settles_by_its_own_bar_ms():
    """Bar provenance. Kills ``series.bar_ms`` -> a hard-coded ``60_000``.

    Every other fixture uses ``bar_ms == 60_000``, so a hard-coded one-minute width is
    indistinguishable from the series' own. Under the ``1H`` fallback the two disagree: this candle
    closes 30 minutes after ``T`` and is eligible on a 3_600_000 window, whereas a 60_000 width
    would place its close 1_740_000 ms BEFORE ``T`` and reject it. ``_series`` is not reusable here
    because it hard-codes the 1m bar, so the series is built directly.
    """
    hour_ms = 3_600_000
    c = Candle(T - 1_800_000, 1.0, 1.2, 0.9, 42.0, 5.0, 5.5, True)  # close_ts == T + 1_800_000
    picked = select_settlement_candle(
        CandleSeries(bar="1H", bar_ms=hour_ms, candles=(c,)), t0_ms=T0, horizon_ms=H
    )
    assert picked is c
    assert picked.close == 42.0  # settled on the 1H candle's close, not on a 1m-window miss
