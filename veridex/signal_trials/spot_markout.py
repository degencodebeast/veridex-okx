"""SpotMarkoutLaw — close-boundary settlement and symmetric cost for Signal Trials (H3.2).

Two laws live here, and both exist to make a spot trial hard to flatter.

**The close-boundary law.** A trial opened at ``t0`` with horizon ``H`` settles at ``T = t0 + H``
against the FIRST candle whose close lands at or after ``T``. OKX's ``ts`` is the candle OPEN time,
so the close is one bar later: ``close_ts = ts_open + series.bar_ms``. The accepted lag window is
half-open, ``0 <= close_ts - T < bar_ms``: a lag of a full bar or more means some other bar already
closed between ``T`` and this one, so this is not the settlement bar and the trial is left UNSCORED
rather than settled against the wrong price. Unconfirmed candles are never eligible — an in-progress
bar's close is a moving number, and settling on it would let the same trial score differently on two
reads. No eligible candle (including an empty series) is ``None``: unscored, never guessed.

**The symmetric-cost law.** ``follow`` and ``fade`` are mirror-image directional stances on the same
gross move, and BOTH pay ``cost_bps``. Charging cost only to the follow leg would turn the fee into a
*benefit* on the fade side, so an agent could farm score by fading everything. Only ``abstain`` is
free, and it is exactly 0 — abstaining is the honest way to decline a trial, and it must never be
worth more or less than nothing.

Rounding to integer bps happens ONCE, on the gross move. The two legs are then pure integer
arithmetic off that single value, which makes ``follow + fade == -2 * cost_bps`` exact by
construction — no float rounding can drift the two legs apart and quietly break the symmetry above.
"""

from __future__ import annotations

from dataclasses import dataclass

from veridex.signal_trials.okx_client import Candle, CandleSeries


class SpotMarkoutError(ValueError):
    """A spot markout input violates the law's preconditions.

    Subclasses ``ValueError`` so callers that already treat bad inputs as ``ValueError`` keep
    working, while trial-settlement failures stay separately catchable.
    """


def assert_positive_price(x: float, name: str) -> float:
    """Validate a spot price and return it unchanged.

    Spot prices are UNBOUNDED above — this is deliberately not the ``[0, 1]`` check that guards a
    probability. A token can legitimately trade at ``3620.5`` or at ``0.000004``; only non-positive
    values are impossible. The bound matters because a ``0.0`` entry would otherwise reach the
    ``(future - entry) / entry`` division, and ``NaN`` would propagate silently through it. ``not
    x > 0`` rejects ``NaN`` too, which ``x <= 0`` would let through.

    Args:
        x: The candidate price.
        name: Field name used in the error message (e.g. ``"entry"``), so a failure identifies
            which side of the markout was malformed.

    Returns:
        ``x`` unchanged, so this can wrap an expression inline.

    Raises:
        SpotMarkoutError: If ``x`` is not strictly greater than zero (including ``NaN``).
    """
    if not x > 0:
        raise SpotMarkoutError(f"{name} must be a positive spot price, got {x!r}")
    return x


def select_settlement_candle(series: CandleSeries, *, t0_ms: int, horizon_ms: int) -> Candle | None:
    """Pick the candle a trial settles against, or ``None`` if the trial cannot be scored.

    Implements the close-boundary law described in the module docstring: with ``T = t0_ms +
    horizon_ms`` and ``close_ts = candle.ts_open_ms + series.bar_ms``, a candle is eligible iff it is
    confirmed and ``0 <= close_ts - T < series.bar_ms``; the eligible candle with the MINIMUM
    ``close_ts`` wins. ``bar_ms`` comes from the series (request provenance, never inferred from the
    wire) — the same timestamps under a different bar width are a different settlement.

    Selection is order-independent: it depends only on ``close_ts``, not on the order OKX returned
    the rows in, so a re-fetch that paginates differently settles identically. Duplicate rows sharing
    a ``ts_open_ms`` resolve to the first occurrence, which is stable for a fixed input.

    Args:
        series: The candles plus their bar provenance.
        t0_ms: Trial open time, epoch milliseconds.
        horizon_ms: Trial horizon in milliseconds.

    Returns:
        The settlement candle, or ``None`` when no candle qualifies — an empty series, only
        unconfirmed candles, or a gap that pushed the next close a full bar or more past ``T``.
        ``None`` means UNSCORED; it is never a substitute for a price.
    """
    settlement_ts = t0_ms + horizon_ms
    eligible = [
        candle
        for candle in series.candles
        if candle.confirmed and 0 <= (candle.ts_open_ms + series.bar_ms) - settlement_ts < series.bar_ms
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda candle: candle.ts_open_ms + series.bar_ms)


@dataclass(frozen=True)
class MarkoutResult:
    """The three stances' markouts in integer basis points, plus the follow verdict.

    Frozen: a settled markout is evidence, and evidence that can be mutated after the fact is not
    evidence. ``abstain_markout_bps`` is carried explicitly rather than assumed, so a receipt states
    the abstain payoff instead of leaving a reader to infer it.
    """

    follow_markout_bps: int
    fade_markout_bps: int
    abstain_markout_bps: int
    follow_profitable: bool


def spot_markout(entry: float, future: float, cost_bps: int) -> MarkoutResult:
    """Score the three stances on one spot trial.

    ``gross = (future - entry) / entry * 1e4`` basis points, rounded once to an integer. Then
    ``follow = gross - cost_bps`` and ``fade = -gross - cost_bps``: the fade leg mirrors the gross
    move but STILL pays the cost, because a stance that is charged nothing to take is a stance an
    agent will take for free. ``abstain`` is exactly 0.

    Args:
        entry: Spot price at trial open. Must be positive.
        future: Spot price at the settlement candle's close. Must be positive.
        cost_bps: Round-trip cost charged to each directional stance, in basis points.

    Returns:
        A ``MarkoutResult`` whose ``follow_profitable`` is ``True`` iff the follow leg cleared cost
        strictly — a markout of exactly 0 is a wash, not a win.

    Raises:
        SpotMarkoutError: If either price is not positive, or if ``cost_bps`` is negative. A
            negative cost would pay agents to trade and would invert the symmetric-cost law, so it
            is rejected rather than applied.
    """
    assert_positive_price(entry, "entry")
    assert_positive_price(future, "future")
    if cost_bps < 0:
        raise SpotMarkoutError(f"cost_bps must be non-negative, got {cost_bps!r}")

    gross_bps = round((future - entry) / entry * 1e4)
    follow_bps = gross_bps - cost_bps
    fade_bps = -gross_bps - cost_bps
    return MarkoutResult(
        follow_markout_bps=follow_bps,
        fade_markout_bps=fade_bps,
        abstain_markout_bps=0,
        follow_profitable=follow_bps > 0,
    )
