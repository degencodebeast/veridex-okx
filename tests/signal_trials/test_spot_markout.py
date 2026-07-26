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
