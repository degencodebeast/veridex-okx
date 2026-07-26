"""The three frozen contestants and the leakage-safe ``ext`` feature (H3.3).

Three deterministic agents, frozen a priori at the §5.3 coefficients. "Frozen" is the whole point:
these are the house players in a fairness claim, so the coefficients are fixed BEFORE any pack
outcome is observed and are never tuned afterwards. Same pack + same versions -> identical ``p``.
``CONTESTANT_VERSIONS`` and ``config_hash`` are what let a receipt state which frozen configuration
produced a number, so a later retune cannot be passed off as the original run.

**Every input is observable at t0.** The contestants read only ``CanonicalSignal`` fields, which
``challenge_spec`` has already restricted to the ``visible_at_decision`` tier, plus ``ext`` — the
one derived feature, and the only place in this module where leakage is even possible. ``ext``
answers "has this token already run up?", and answering it requires reaching into a price series,
which is exactly the operation that can accidentally read the future.

**The close-boundary law, restated here because the leak is subtle.** OKX's ``ts`` is the candle
OPEN time, so a candle that OPENS before ``t0`` can still CLOSE after it, and its close is a price
that did not exist at decision time. Selecting on ``ts_open <= t0`` would therefore hand an agent a
few minutes of the future on every trial — undetectably, because the number looks like a normal
price. Selection is on ``close_ts = ts_open + bar_ms`` instead, and unconfirmed candles are refused
outright because an in-progress bar's close is a moving number. This mirrors ``spot_markout``'s
settlement law; the two must agree, or the feature an agent sees and the price it is scored against
would be anchored to different clocks.

**Declining is never an error.** A missing or unusable input makes a contestant emit the neutral
``0.5`` rather than raise. That asymmetry against ``challenge_spec``, which raises on everything, is
deliberate and matches §5.3: a malformed EVIDENCE hash is a false receipt and must fail loudly,
whereas a contestant that cannot form a view has a defined honest answer — no view. The neutral
value is also what ``0.40 < p < 0.60`` renders as ABSTAIN, so declining and abstaining coincide.
"""

from __future__ import annotations

import hashlib
import json
import math

from veridex.signal_trials.challenge_spec import CanonicalSignal
from veridex.signal_trials.okx_client import CandleSeries

# The probability a contestant emits when it declines a trial. Deliberately its own constant rather
# than a reuse of the 0.50 formula bases below: they are numerically equal today but semantically
# unrelated, and collapsing them would let a change to one silently move the other.
_NEUTRAL_PROBABILITY = 0.5

# `top10_holder_percent` arrives as a percentage (31.5), while every §5.3 formula is written in
# fractions (0.315). A unit, not a tunable coefficient — see `config_hash` on what the hash covers.
_PERCENT_TO_FRACTION = 100.0

# --- the `ext` feature law (§5.3, anchored on the §7 close boundary) ---
_EXT_LOOKBACK_MS = 3_600_000
_EXT_THRESHOLD = 0.20

# --- FlowFollower v1: more wallets / larger flow -> higher follow conviction ---
_FLOW_BASE = 0.50
_FLOW_PER_WALLET = 0.06
_FLOW_WALLET_BASELINE = 2
_FLOW_PER_DECADE_OF_FLOW = 0.04
_FLOW_AMOUNT_FLOOR_USD = 1000.0
_FLOW_CLAMP_LOW = 0.50
_FLOW_CLAMP_HIGH = 0.85

# --- CrowdingFader v1: concentrated / micro-cap / already-pumped -> fade ---
_FADE_BASE = 0.50
_FADE_PER_CONCENTRATION = 0.35
_FADE_MICROCAP_PENALTY = 0.15
_FADE_MICROCAP_THRESHOLD_USD = 500_000
_FADE_PER_EXT = 0.20
_FADE_CLAMP_LOW = 0.15
_FADE_CLAMP_HIGH = 0.50

# --- SelectiveCalibrator v1: a narrow band that frequently lands in the ABSTAIN zone ---
_CALIB_BASE = 0.50
_CALIB_PER_WALLET_STEP = 0.08
_CALIB_WALLET_CAP = 6
_CALIB_WALLET_BASELINE = 2
_CALIB_WALLET_DIVISOR = 4
_CALIB_PER_CONCENTRATION = 0.15
_CALIB_CONCENTRATION_CENTRE = 0.5
_CALIB_PER_EXT = 0.10
_CALIB_CLAMP_LOW = 0.35
_CALIB_CLAMP_HIGH = 0.65

# The table `config_hash` hashes. It is built FROM the constants above rather than restating their
# values, which is the only reason the digest is trustworthy: a second copy of the numbers could
# drift from the ones the formulas actually evaluate, and the hash would then attest to a
# configuration that never ran. That is a false receipt, which is worse than having no hash at all.
_FROZEN_COEFFICIENTS: dict[str, dict[str, float | int]] = {
    "ext": {
        "lookback_ms": _EXT_LOOKBACK_MS,
        "threshold": _EXT_THRESHOLD,
    },
    "flow_follower": {
        "base": _FLOW_BASE,
        "per_wallet": _FLOW_PER_WALLET,
        "wallet_baseline": _FLOW_WALLET_BASELINE,
        "per_decade_of_flow": _FLOW_PER_DECADE_OF_FLOW,
        "amount_floor_usd": _FLOW_AMOUNT_FLOOR_USD,
        "clamp_low": _FLOW_CLAMP_LOW,
        "clamp_high": _FLOW_CLAMP_HIGH,
    },
    "crowding_fader": {
        "base": _FADE_BASE,
        "per_concentration": _FADE_PER_CONCENTRATION,
        "microcap_penalty": _FADE_MICROCAP_PENALTY,
        "microcap_threshold_usd": _FADE_MICROCAP_THRESHOLD_USD,
        "per_ext": _FADE_PER_EXT,
        "clamp_low": _FADE_CLAMP_LOW,
        "clamp_high": _FADE_CLAMP_HIGH,
    },
    "selective_calibrator": {
        "base": _CALIB_BASE,
        "per_wallet_step": _CALIB_PER_WALLET_STEP,
        "wallet_cap": _CALIB_WALLET_CAP,
        "wallet_baseline": _CALIB_WALLET_BASELINE,
        "wallet_divisor": _CALIB_WALLET_DIVISOR,
        "per_concentration": _CALIB_PER_CONCENTRATION,
        "concentration_centre": _CALIB_CONCENTRATION_CENTRE,
        "per_ext": _CALIB_PER_EXT,
        "clamp_low": _CALIB_CLAMP_LOW,
        "clamp_high": _CALIB_CLAMP_HIGH,
    },
    "shared": {
        "neutral_probability": _NEUTRAL_PROBABILITY,
    },
}

# Bumped only when a contestant's ARITHMETIC changes. Carried in the manifest alongside
# `config_hash` because the two answer different questions: the version identifies the formula, the
# hash identifies the numbers fed into it.
CONTESTANT_VERSIONS: dict[str, str] = {
    "flow_follower": "v1",
    "crowding_fader": "v1",
    "selective_calibrator": "v1",
}


def config_hash() -> str:
    """sha256 hex digest over the canonical JSON encoding of the frozen coefficient table.

    ``sort_keys=True`` plus the tightest separators make the encoding independent of declaration
    order and whitespace, matching ``challenge_spec.evidence_hash`` so a manifest carries one
    hashing convention rather than two.

    **What it covers and what it does not.** It covers every §5.3 coefficient, clamp bound and
    ``ext`` constant — the tunables. It does NOT cover formula STRUCTURE: reordering terms, swapping
    an operator, or changing a unit conversion leaves the digest untouched. ``CONTESTANT_VERSIONS``
    is what identifies structure, which is why both are persisted and neither substitutes for the
    other. Stating this matters because the digest's job is to make a silent RETUNE impossible, and
    a reader who believed it also froze the arithmetic would trust it for something it cannot do.
    """
    canonical = json.dumps(_FROZEN_COEFFICIENTS, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clamp(value: float, low: float, high: float) -> float:
    """Constrain ``value`` to ``[low, high]``.

    Spelled ``min(high, max(low, value))`` to match the §5.3 ``clamp(x, lo, hi)`` reading order.
    """
    return min(high, max(low, value))


def _latest_close_at_or_before(series: CandleSeries, boundary_ms: int) -> float | None:
    """The close of the confirmed candle with the greatest ``close_ts`` not past ``boundary_ms``.

    ``close_ts = ts_open_ms + series.bar_ms``, with ``bar_ms`` taken from the series' request
    provenance rather than inferred from the wire — the same timestamps under a different bar width
    describe different candles, so a hard-coded width would silently change which prices are
    visible at ``t0``.

    The ``+ series.bar_ms`` in the ``max`` key is arithmetically INERT — every candle in a series
    shares one bar width, so a constant offset cannot change which element is greatest — and it is
    **deliberately retained**: it makes the selection key read as ``close_ts``, matching the law. Do
    not simplify it. Removing it costs nothing behaviourally and reopens exactly the question this
    law exists to settle, namely whether selection is by OPEN time or CLOSE time. In the FILTER
    immediately above it, by contrast, the same term is fully load-bearing.

    Ties are possible only when two candles share a ``ts_open_ms``, in which case ``max`` keeps
    whichever the wire delivered first and the price follows wire order. Guaranteeing
    ``ts_open_ms`` uniqueness belongs upstream in the client that fetched the series, not here.

    Returns:
        The close, or ``None`` when no confirmed candle closes by the boundary.
    """
    eligible = [
        candle for candle in series.candles if candle.confirmed and candle.ts_open_ms + series.bar_ms <= boundary_ms
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda candle: candle.ts_open_ms + series.bar_ms).close


def compute_ext(series: CandleSeries, t0_ms: int) -> int | None:
    """The "already extended" feature: ``1`` if the hour before ``t0`` ran up more than 20%.

    ``p0`` is the close of the confirmed candle with the greatest ``close_ts <= t0_ms`` and ``p1``
    the same one hour earlier; ``ext = 1`` iff ``p0 / p1 - 1 > 0.20``. Both boundaries are
    INCLUSIVE — a candle closing exactly at ``t0`` was complete at decision time and is legitimately
    visible, so excluding it would discard the most informative bar the law allows.

    Both prices must be strictly positive. The predicate is spelled ``not price > 0`` rather than
    ``price <= 0`` because ``NaN`` fails every comparison it takes part in, so ``price <= 0`` would
    wave it through; it would then propagate to ``nan > 0.20``, which is ``False``, and the function
    would return a confident ``0``. An unusable price has to produce ``None`` — no view — rather
    than a fabricated one.

    Args:
        series: Candles plus their bar provenance. Only confirmed candles are considered.
        t0_ms: Trial open time, epoch milliseconds. The upper boundary of what is observable.

    Returns:
        ``1`` or ``0``, or ``None`` when either price is absent or not positive. ``None`` means the
        feature is unavailable for this trial, and every contestant that consumes it answers with
        the neutral probability rather than substituting a value.
    """
    p0 = _latest_close_at_or_before(series, t0_ms)
    p1 = _latest_close_at_or_before(series, t0_ms - _EXT_LOOKBACK_MS)
    if p0 is None or p1 is None:
        return None
    if not p0 > 0 or not p1 > 0:
        return None
    return 1 if p0 / p1 - 1 > _EXT_THRESHOLD else 0


def flow_follower(signal: CanonicalSignal) -> float:
    """FlowFollower v1 — biases FOLLOW. ``clamp(0.50 + 0.06*(w-2) + 0.04*log10(max(a,1000)/1000))``.

    More triggering wallets and larger dollar flow read as stronger conviction. Flow enters
    logarithmically because the interesting difference is between $10k and $100k, not between $1.0m
    and $1.1m; the ``max(a, 1000)`` floor exists because ``log10`` of a sub-dollar amount would
    return a large negative and let one tiny signal dominate the sum. It takes no ``ext``: this
    contestant is defined on flow alone, so it has no input that can go missing and no neutral path.
    """
    conviction = (
        _FLOW_BASE
        + _FLOW_PER_WALLET * (signal.trigger_wallet_count - _FLOW_WALLET_BASELINE)
        + _FLOW_PER_DECADE_OF_FLOW * math.log10(max(signal.amount_usd, _FLOW_AMOUNT_FLOOR_USD) / _FLOW_AMOUNT_FLOOR_USD)
    )
    return _clamp(conviction, _FLOW_CLAMP_LOW, _FLOW_CLAMP_HIGH)


def crowding_fader(signal: CanonicalSignal, ext: int | None) -> float:
    """CrowdingFader v1 — biases FADE. ``clamp(0.50 - 0.35*c10 - 0.15*[mc<500k] - 0.20*ext)``.

    Reads three crowding tells and subtracts for each: top-10 concentration, a micro-cap flag, and
    whether the token has already run up. The micro-cap term is a step, not a slope — the threshold
    is strict, so a cap of exactly 500_000 is NOT micro-cap.

    ``ext`` of ``None`` means the price history needed to judge the run-up was unavailable, and this
    contestant declines the trial with the neutral probability rather than treating the absence as
    "not extended". Those are different claims, and substituting one for the other would quietly
    bias the agent toward fading whenever candle data happened to be missing.
    """
    if ext is None:
        return _NEUTRAL_PROBABILITY
    concentration = signal.top10_holder_percent / _PERCENT_TO_FRACTION
    is_microcap = 1 if signal.market_cap_usd < _FADE_MICROCAP_THRESHOLD_USD else 0
    conviction = (
        _FADE_BASE
        - _FADE_PER_CONCENTRATION * concentration
        - _FADE_MICROCAP_PENALTY * is_microcap
        - _FADE_PER_EXT * ext
    )
    return _clamp(conviction, _FADE_CLAMP_LOW, _FADE_CLAMP_HIGH)


def selective_calibrator(signal: CanonicalSignal, ext: int | None) -> float:
    """SelectiveCalibrator v1 — tends to ABSTAIN. ``clamp(0.50 + 0.08*(min(w,6)-2)/4 - 0.15*(c10-0.5) - 0.10*ext, 0.35, 0.65)``.

    The narrowest band of the three, by construction: its clamps sit at 0.35 and 0.65, so most
    inputs land inside the ``0.40 < p < 0.60`` ABSTAIN zone. Wallet count is capped at 6 before the
    baseline is subtracted, so a hundred triggering wallets say no more than six do — this
    contestant is defined not to be impressed by a crowd. Concentration is centred on 0.5 rather
    than 0, making it a signed adjustment either side of an evenly-held token instead of a penalty
    that always subtracts.

    ``ext`` of ``None`` declines the trial, for the same reason as ``crowding_fader``.
    """
    if ext is None:
        return _NEUTRAL_PROBABILITY
    concentration = signal.top10_holder_percent / _PERCENT_TO_FRACTION
    capped_wallets = min(signal.trigger_wallet_count, _CALIB_WALLET_CAP)
    conviction = (
        _CALIB_BASE
        + _CALIB_PER_WALLET_STEP * (capped_wallets - _CALIB_WALLET_BASELINE) / _CALIB_WALLET_DIVISOR
        - _CALIB_PER_CONCENTRATION * (concentration - _CALIB_CONCENTRATION_CENTRE)
        - _CALIB_PER_EXT * ext
    )
    return _clamp(conviction, _CALIB_CLAMP_LOW, _CALIB_CLAMP_HIGH)
