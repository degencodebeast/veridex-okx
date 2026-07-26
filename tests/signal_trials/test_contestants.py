import math, pytest
from veridex.signal_trials.contestants import flow_follower, crowding_fader, selective_calibrator, compute_ext
from veridex.signal_trials.okx_client import Candle, CandleSeries

def _sig(w=3, a=10_000.0, c10=31.5, mc=2_000_000.0):
    from veridex.signal_trials.challenge_spec import CanonicalSignal
    return CanonicalSignal(t0_ms=1, chain_index="501", token_address="t", symbol="TOK", name="Tok",
        market_cap_usd=mc, holders=900, top10_holder_percent=c10, trigger_price=0.042,
        wallet_type="1", trigger_wallet_count=w, trigger_wallet_address="0xa", amount_usd=a)

def test_flow_follower_formula_frozen():
    expected = min(0.85, max(0.50, 0.50 + 0.06 * (3 - 2) + 0.04 * math.log10(10_000 / 1000)))
    assert flow_follower(_sig()) == pytest.approx(expected)
    assert flow_follower(_sig(w=30, a=10**9)) == 0.85               # upper clamp

def test_crowding_fader_formula_and_clamps():
    # exact frozen formula, mid-range (no clamp): 0.50 - 0.35*c10 - 0.15*[mc<500k] - 0.20*ext
    assert crowding_fader(_sig(c10=40.0, mc=2_000_000.0), ext=0) == pytest.approx(0.50 - 0.35 * 0.40)
    assert crowding_fader(_sig(c10=80.0, mc=400_000.0), ext=1) == pytest.approx(0.15)   # lower clamp
    assert crowding_fader(_sig(c10=0.0, mc=2_000_000.0), ext=0) == pytest.approx(0.50)  # upper clamp

def test_selective_calibrator_formula_and_clamps():
    # exact frozen formula: 0.50 + 0.08*(min(w,6)-2)/4 - 0.15*(c10-0.5) - 0.10*ext
    assert selective_calibrator(_sig(w=4, c10=50.0), ext=0) == pytest.approx(0.50 + 0.08 * (4 - 2) / 4)
    assert selective_calibrator(_sig(w=6, c10=0.0), ext=0) == pytest.approx(0.65)       # upper clamp
    assert selective_calibrator(_sig(w=2, c10=100.0), ext=1) == pytest.approx(0.35)     # lower clamp

def test_versions_and_config_hash_are_stable():
    from veridex.signal_trials.contestants import CONTESTANT_VERSIONS, config_hash
    assert CONTESTANT_VERSIONS == {"flow_follower": "v1", "crowding_fader": "v1", "selective_calibrator": "v1"}
    assert config_hash() == config_hash() and len(config_hash()) == 64   # deterministic sha256 over frozen coefficients

def test_missing_ext_yields_neutral():
    assert crowding_fader(_sig(), ext=None) == 0.5 and selective_calibrator(_sig(), ext=None) == 0.5

def test_ext_excludes_candle_closing_after_t0():
    BAR = 60_000; t0 = 10_000_000
    leaky = Candle(t0 - 1, 1, 1, 1, 1.05, 1, 1, True)     # closes AFTER t0; 5% → would flip ext to 0 if leaked
    p0ok  = Candle(t0 - BAR, 1, 1, 1, 1.3, 1, 1, True)     # close_ts == t0; 30% → ext = 1
    p1ok  = Candle(t0 - 3_600_000 - BAR, 1, 1, 1, 1.0, 1, 1, True)
    series = CandleSeries(bar="1m", bar_ms=BAR, candles=(leaky, p0ok, p1ok))
    assert compute_ext(series, t0_ms=t0) == 1


# --- Pins authored with the mandated block above, in the SAME RED capture. ---
# Everything above this line is byte-identical to the plan's REGION A mandate (lines 552-593) and
# is never touched. Everything below closes a property the mandated vectors leave free. The two
# recurring shapes are (a) a coefficient whose mandated vector is CLAMPED, so the coefficient is
# invisible and deleting it passes, and (b) a coefficient whose mandated multiplier happens to be
# 1 or 0, so it is interchangeable with its neighbour. Each pin below names the property it fixes.
#
# ``compute_ext`` reads a close boundary, so its constants are pinned with vectors that VARY the
# bar width (60_000, 900_000, 3_600_000) and vary which candle sits nearest the boundary. Expected
# values are HARD-CODED rather than recomputed from the module's constants: a vector derived from
# the constant under test cannot detect that constant moving.


def _candle_closing_at(close_ts_ms: int, bar_ms: int, close: float, confirmed: bool = True) -> Candle:
    """A candle whose close lands exactly at ``close_ts_ms`` in a ``bar_ms``-wide series.

    The law is written in CLOSE time while ``Candle`` is written in OPEN time, so every fixture
    here would otherwise repeat the ``ts_open = close_ts - bar_ms`` conversion by hand. Stating
    the close directly is what makes the boundary cases below readable as the law reads.
    """
    return Candle(close_ts_ms - bar_ms, 1.0, 1.0, 1.0, close, 1.0, 1.0, confirmed)


def _series_of(bar: str, bar_ms: int, *candles: Candle) -> CandleSeries:
    return CandleSeries(bar=bar, bar_ms=bar_ms, candles=candles)


# --- compute_ext: the close-boundary feature law ---


def test_ext_is_zero_when_the_move_is_below_threshold():
    """The ``ext == 0`` branch, which no mandated vector reaches.

    ``test_ext_excludes_candle_closing_after_t0`` is the only mandated ``compute_ext`` case and it
    asserts ``== 1``, so a body of ``return 1`` passes the whole mandated suite. A 10% move on a
    5-minute bar is the smallest thing that refutes it.
    """
    bar_ms = 300_000
    t0 = 20_000_000
    series = _series_of(
        "5m",
        bar_ms,
        _candle_closing_at(t0, bar_ms, 110.0),
        _candle_closing_at(t0 - 3_600_000, bar_ms, 100.0),
    )
    assert compute_ext(series, t0_ms=t0) == 0


def test_ext_threshold_is_twenty_percent():
    """Brackets the 0.20 threshold to within half a percentage point, on a 5-minute bar.

    The mandated fixture moves 30%, which any threshold from 0 to 0.29 would also call extended.
    These two vectors sit either side of 0.20 and are written as literal prices, so a threshold
    that drifts to 0.19 or 0.21 flips one of them.
    """
    bar_ms = 300_000
    t0 = 20_000_000

    def _ext_for(p0_close: float) -> int | None:
        return compute_ext(
            _series_of(
                "5m",
                bar_ms,
                _candle_closing_at(t0, bar_ms, p0_close),
                _candle_closing_at(t0 - 3_600_000, bar_ms, 100.0),
            ),
            t0_ms=t0,
        )

    assert _ext_for(119.5) == 0  # +19.5%, below the threshold
    assert _ext_for(120.5) == 1  # +20.5%, above it


def test_ext_lookback_is_one_hour_before_t0():
    """Pins the 3_600_000 ms lookback in BOTH directions, on a 15-minute bar.

    Only one candle in the mandated fixture is a ``p1`` candidate, so any lookback that still
    selects it passes. Here a half-hour lookback would select the 125.0 candle (+4%, ext 0) and a
    two-hour lookback would select the 200.0 candle (-35%, ext 0), while the correct one-hour
    lookback selects 100.0 (+30%, ext 1).
    """
    bar_ms = 900_000
    t0 = 50_000_000
    series = _series_of(
        "15m",
        bar_ms,
        _candle_closing_at(t0, bar_ms, 130.0),
        _candle_closing_at(t0 - 1_800_000, bar_ms, 125.0),
        _candle_closing_at(t0 - 3_600_000, bar_ms, 100.0),
        _candle_closing_at(t0 - 7_200_000, bar_ms, 200.0),
    )
    assert compute_ext(series, t0_ms=t0) == 1


def test_p1_is_the_latest_candle_at_or_before_the_lookback_boundary():
    """``p1`` selection is a MAX over ``close_ts``, and is independent of wire order.

    The mandated fixture offers a single ``p1`` candidate, so max, min, first-in-wire-order and
    last-in-wire-order are indistinguishable. Two candidates whose closes disagree separate them:
    the correct latest candle (100.0) gives +10% and ``ext`` 0, the older one (50.0) gives +120%
    and ``ext`` 1.
    """
    bar_ms = 60_000
    t0 = 30_000_000
    p0 = _candle_closing_at(t0, bar_ms, 110.0)
    p1_latest = _candle_closing_at(t0 - 3_600_000, bar_ms, 100.0)
    p1_older = _candle_closing_at(t0 - 5_000_000, bar_ms, 50.0)
    assert compute_ext(_series_of("1m", bar_ms, p0, p1_latest, p1_older), t0_ms=t0) == 0
    assert compute_ext(_series_of("1m", bar_ms, p1_older, p1_latest, p0), t0_ms=t0) == 0


def test_ext_uses_the_series_bar_ms_not_a_hard_coded_one_minute():
    """Bar provenance, and the leakage law restated at the 1H fallback width.

    Every mandated ``compute_ext`` vector uses ``bar_ms == 60_000``, so a hard-coded one-minute
    width is indistinguishable from the series' own. Under the 1H bar the two disagree about which
    candle even closes by ``t0``: the 100.0 candle opens one minute before ``t0`` and truly closes
    3_540_000 ms AFTER it, so the law excludes it and ``p0`` is 150.0 (+50%, ext 1). A hard-coded
    60_000 would place that candle's close exactly at ``t0``, make it the latest, and return 0 —
    which is the leak the close-boundary law exists to prevent, at the width where it is easiest
    to introduce.
    """
    bar_ms = 3_600_000
    t0 = 100_000_000
    series = _series_of(
        "1H",
        bar_ms,
        _candle_closing_at(t0 + 3_540_000, bar_ms, 100.0),  # opens t0 - 60_000, closes AFTER t0
        _candle_closing_at(t0, bar_ms, 150.0),
        _candle_closing_at(t0 - 3_600_000, bar_ms, 100.0),
    )
    assert compute_ext(series, t0_ms=t0) == 1


def test_ext_is_none_when_either_price_is_missing():
    """Unscored rather than guessed. No mandated vector reaches any ``None`` branch."""
    bar_ms = 60_000
    t0 = 10_000_000
    assert compute_ext(_series_of("1m", bar_ms), t0_ms=t0) is None  # empty series
    only_future = _series_of("1m", bar_ms, _candle_closing_at(t0 + bar_ms, bar_ms, 100.0))
    assert compute_ext(only_future, t0_ms=t0) is None  # no p0
    only_p0 = _series_of("1m", bar_ms, _candle_closing_at(t0, bar_ms, 100.0))
    assert compute_ext(only_p0, t0_ms=t0) is None  # p0 present, nothing an hour back


def test_ext_ignores_unconfirmed_candles():
    """An in-progress bar's close is a moving number and is never a price here.

    The unconfirmed candle is deliberately the one that would change the answer: counting it makes
    ``p0`` 130.0 and ``ext`` 1, whereas the confirmed pair gives a flat 0.
    """
    bar_ms = 60_000
    t0 = 10_000_000
    unconfirmed_p0 = _candle_closing_at(t0, bar_ms, 130.0, confirmed=False)
    confirmed_p0 = _candle_closing_at(t0 - bar_ms, bar_ms, 100.0)
    confirmed_p1 = _candle_closing_at(t0 - 3_600_000, bar_ms, 100.0)
    assert compute_ext(_series_of("1m", bar_ms, unconfirmed_p0, confirmed_p0, confirmed_p1), t0_ms=t0) == 0
    unconfirmed_only = _series_of(
        "1m",
        bar_ms,
        _candle_closing_at(t0, bar_ms, 130.0, confirmed=False),
        _candle_closing_at(t0 - 3_600_000, bar_ms, 100.0, confirmed=False),
    )
    assert compute_ext(unconfirmed_only, t0_ms=t0) is None


def test_ext_is_none_when_a_price_is_not_positive():
    """``p0, p1 > 0`` is required on both sides. Zero would divide, a negative would invert."""
    bar_ms = 60_000
    t0 = 10_000_000

    def _ext_for(p0_close: float, p1_close: float) -> int | None:
        return compute_ext(
            _series_of(
                "1m",
                bar_ms,
                _candle_closing_at(t0, bar_ms, p0_close),
                _candle_closing_at(t0 - 3_600_000, bar_ms, p1_close),
            ),
            t0_ms=t0,
        )

    assert _ext_for(0.0, 100.0) is None
    assert _ext_for(100.0, 0.0) is None
    assert _ext_for(-5.0, 100.0) is None
    assert _ext_for(100.0, -5.0) is None


def test_nan_price_is_refused_rather_than_compared():
    """Pins the ``not x > 0`` spelling of the positivity test against ``x <= 0``.

    ``NaN`` fails every comparison it takes part in, so ``x <= 0`` waves it through. It then
    propagates silently: ``nan > 0.20`` is ``False``, so the mutant returns a confident ``ext`` of
    0 — a wrong answer on the money path rather than an unscored trial.
    """
    bar_ms = 60_000
    t0 = 10_000_000

    def _ext_for(p0_close: float, p1_close: float) -> int | None:
        return compute_ext(
            _series_of(
                "1m",
                bar_ms,
                _candle_closing_at(t0, bar_ms, p0_close),
                _candle_closing_at(t0 - 3_600_000, bar_ms, p1_close),
            ),
            t0_ms=t0,
        )

    assert _ext_for(math.nan, 100.0) is None
    assert _ext_for(100.0, math.nan) is None


# --- FlowFollower ---


def test_flow_follower_wallet_and_flow_terms_are_separately_pinned():
    """Separates 0.06 from 0.04, which the mandated vector cannot.

    The mandated case uses ``w=3`` and ``a=10_000``, making BOTH multipliers exactly 1: the wallet
    term contributes 0.06 and the flow term 0.04, so only their sum is fixed. Swapping the two
    coefficients, or replacing both terms with a single ``0.10 * (w - 2)``, reproduces 0.60 and
    passes. These vectors zero one term at a time.
    """
    assert flow_follower(_sig(w=5, a=1000.0)) == pytest.approx(0.68)  # 0.50 + 0.06*3, flow term 0
    assert flow_follower(_sig(w=2, a=100_000.0)) == pytest.approx(0.58)  # 0.50 + 0.04*2, wallet term 0


def test_flow_follower_floors_amount_at_one_thousand_usd():
    """``max(a, 1000)`` — never exercised by the mandated vectors, which are all above the floor.

    Below the floor the log term must contribute exactly nothing. Without the floor these would be
    0.608 and 0.54; with the floor set to 100 instead of 1000 the first would be 0.648.
    """
    assert flow_follower(_sig(w=4, a=1000.0)) == pytest.approx(0.62)
    assert flow_follower(_sig(w=4, a=500.0)) == pytest.approx(0.62)
    assert flow_follower(_sig(w=4, a=10.0)) == pytest.approx(0.62)


def test_flow_follower_lower_clamp_holds_at_fifty():
    """The lower clamp, which the mandated vectors never reach — both sit at or above 0.60.

    Deleting ``max(0.50, ...)`` returns 0.44 and 0.38 here, below the floor a FOLLOW-biased agent
    is defined to have.
    """
    assert flow_follower(_sig(w=1, a=1000.0)) == 0.50
    assert flow_follower(_sig(w=0, a=1000.0)) == 0.50


# --- CrowdingFader ---


def test_crowding_fader_charges_the_microcap_penalty():
    """The 0.15 micro-cap term, which deleting entirely passes every mandated vector.

    The only mandated case with ``mc < 500_000`` also has ``c10=80`` and ``ext=1``, which drives
    the result to -0.13 and CLAMPS it to 0.15 — the penalty is invisible there. These two vectors
    differ in nothing but the market cap and land clear of both clamps.
    """
    assert crowding_fader(_sig(c10=10.0, mc=400_000.0), ext=0) == pytest.approx(0.315)
    assert crowding_fader(_sig(c10=10.0, mc=600_000.0), ext=0) == pytest.approx(0.465)


def test_crowding_fader_microcap_threshold_is_strictly_below_five_hundred_thousand():
    """Pins the 500_000 boundary and its strictness. A cap exactly at 500_000 is NOT micro."""
    assert crowding_fader(_sig(c10=10.0, mc=500_000.0), ext=0) == pytest.approx(0.465)
    assert crowding_fader(_sig(c10=10.0, mc=499_999.0), ext=0) == pytest.approx(0.315)


def test_crowding_fader_charges_the_ext_term():
    """The 0.20 ``ext`` term, also invisible in the mandated suite.

    Its only mandated appearance with ``ext=1`` is the clamped -0.13 case, so deleting the term
    passes. Here the two vectors differ in nothing but ``ext`` and the gap is exactly 0.20.
    """
    assert crowding_fader(_sig(c10=10.0, mc=2_000_000.0), ext=1) == pytest.approx(0.265)
    assert crowding_fader(_sig(c10=10.0, mc=2_000_000.0), ext=0) == pytest.approx(0.465)


def test_crowding_fader_upper_clamp_binds_on_a_negative_concentration():
    """The 0.50 upper clamp, which NOTHING else in this file exercises.

    Found by the mutation drill: raising the bound to 1.0 was killed only by the config-hash
    tripwire, so the bound itself was behaviourally free. The reason is that the mandated "upper
    clamp" vector (``c10=0``) is VACUOUS — every term in this formula subtracts, so the raw value
    at ``c10=0`` is already exactly 0.50 and the clamp has nothing to do. The bound only binds on a
    NEGATIVE concentration, which the wire could deliver and ``CanonicalSignal`` does not refuse.
    §5.3 states the clamp unconditionally, so this pins it on the one input class that reaches it:
    the raw value here is 0.85.
    """
    assert crowding_fader(_sig(c10=-100.0), ext=0) == 0.50


# --- SelectiveCalibrator ---


def test_selective_calibrator_charges_the_concentration_term():
    """The 0.15 coefficient AND the 0.5 centring, neither of which the mandated suite fixes.

    The unclamped mandated vector uses ``c10=50``, i.e. ``c10 - 0.5 == 0``, so the whole term
    vanishes; the other two clamp. Centring at 0 instead of 0.5 gives 0.435 and 0.495 here, and
    dropping the term gives 0.54 for both.
    """
    assert selective_calibrator(_sig(w=4, c10=70.0), ext=0) == pytest.approx(0.51)
    assert selective_calibrator(_sig(w=4, c10=30.0), ext=0) == pytest.approx(0.57)


def test_selective_calibrator_charges_the_ext_term():
    """The 0.10 ``ext`` term. Its only mandated ``ext=1`` vector clamps to 0.35, hiding it."""
    assert selective_calibrator(_sig(w=4, c10=50.0), ext=1) == pytest.approx(0.44)
    assert selective_calibrator(_sig(w=4, c10=50.0), ext=0) == pytest.approx(0.54)


def test_selective_calibrator_caps_wallet_count_at_six():
    """``min(w, 6)`` — the mandated ``w=6`` vector clamps to 0.65, so the cap itself is free.

    Without the cap, ``w=20`` reaches 0.83 and clamps to 0.65 rather than staying level with
    ``w=6`` at 0.55. This vector also separates the baseline-2 / divisor-4 pair, which the
    mandated ``w=4`` case leaves interchangeable with baseline 3 / divisor 2.
    """
    assert selective_calibrator(_sig(w=6, c10=70.0), ext=0) == pytest.approx(0.55)
    assert selective_calibrator(_sig(w=20, c10=70.0), ext=0) == pytest.approx(0.55)


def test_neutral_is_exactly_one_half_and_ignores_the_signal():
    """A declined trial is 0.5 regardless of how extreme the signal is.

    The mandated neutral vector uses the default signal, whose formula value differs from 0.5
    anyway; these use signals that would otherwise CLAMP, so a body that fell through to the
    formula would return 0.15 and 0.35 rather than 0.5.
    """
    extreme = _sig(w=2, c10=100.0, mc=100_000.0)
    assert crowding_fader(extreme, ext=None) == 0.5
    assert selective_calibrator(extreme, ext=None) == 0.5


def test_config_hash_is_frozen_at_a_literal_digest():
    """A change-detector over the frozen coefficient table.

    The mandated hash test fixes only determinism and length, so it holds for a table of any
    contents. This literal is the fingerprint of the §5.3 coefficients as frozen; any edit to a
    coefficient, a clamp bound or the ``ext`` law's constants changes it and fails here. It is a
    TRIPWIRE, not a formula pin — the behavioural pins above are what fix the arithmetic, and this
    exists so a silent retune cannot pass them one at a time.

    DISCLOSED: unlike every other pin in this file, this literal was RECORDED at GREEN rather than
    predicted at RED — a digest is a fingerprint of the table, not a behaviour derivable from the
    spec. Its value is entirely forward-looking: from here on, any coefficient edit fails here.
    """
    from veridex.signal_trials.contestants import config_hash

    assert config_hash() == "6f6da52be95abd90a20892c63fbb513a7589f64de81bd6d8bfdb593ff450a598"
