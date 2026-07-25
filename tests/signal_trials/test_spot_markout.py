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
