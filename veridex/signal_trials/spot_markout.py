"""SpotMarkoutLaw — close-boundary settlement and symmetric cost for Signal Trials (H3.2).

Two laws live here, and both exist to make a spot trial hard to flatter.

**The close-boundary law.** A trial opened at ``t0`` with horizon ``H`` settles at ``T = t0 + H``
against the FIRST candle whose close lands at or after ``T``. OKX's ``ts`` is the candle OPEN time,
so the close is one bar later: ``close_ts = ts_open + series.bar_ms``. The accepted lag window is
half-open, ``0 <= close_ts - T < bar_ms``: past a full bar of lag we can no longer show that THIS is
the bar that first closed after ``T``. Either another bar closed in between, or the series has a hole
where that bar would have been — for an illiquid token (no trades, no candle) the hole is the commoner
cause — and in neither case is this the settlement bar, so the trial is left UNSCORED rather than
settled against the wrong price. Unconfirmed candles are never eligible either: an in-progress bar's
close is a moving number, and settling on it would let the same trial score differently on two reads.
No eligible candle (including an empty series) is ``None`` — unscored, never guessed.

**The symmetric-cost law.** ``follow`` and ``fade`` are mirror-image directional stances on the same
gross move, and BOTH pay ``cost_bps``. Charging cost only to the follow leg would turn the fee into a
*benefit* on the fade side, so an agent could farm score by fading everything. Only ``abstain`` is
free, and it is exactly 0 — abstaining is the honest way to decline a trial, and it must never be
worth more or less than nothing.

Rounding to integer bps happens ONCE, on the gross move, to the nearest integer with ties to even.
The two legs are then pure integer arithmetic off that single value, which makes
``follow + fade == -2 * cost_bps`` exact by construction — no float rounding can drift the two legs
apart and quietly break the symmetry above. The MODE matters as much as the single application:
see ``spot_markout`` for why ``math.floor`` would reintroduce a directional bias.
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
    probability. A token can legitimately trade at ``3620.5`` or at ``0.000004``; what this rejects
    is the non-positive.

    What the guard buys is a NAMED refusal in place of an anonymous crash. Left to the arithmetic,
    ``0.0`` raises a bare ``ZeroDivisionError`` and ``NaN`` reaches ``round()`` and raises
    ``ValueError: cannot convert float NaN to integer``. Neither names the offending field or its
    value. This does. The predicate is spelled ``not x > 0`` rather than ``x <= 0`` because ``NaN``
    fails every comparison it takes part in, so ``x <= 0`` would wave it through.

    KNOWN GAP, stated rather than implied: ``inf > 0`` is ``True``, so ``+inf`` PASSES this guard.
    It surfaces downstream as ``OverflowError`` from ``round()``, which is not a ``ValueError`` and
    so is NOT caught by the contract ``SpotMarkoutError`` documents. ``-inf`` and ``NaN`` are
    rejected here; ``+inf`` alone is not. The input is reachable — ``okx_client`` builds prices with
    ``float()`` over wire strings and ``float("1e400")`` is ``inf`` — so a finiteness check belongs
    at that boundary. Until one exists, do not assume a non-finite price has been refused here.

    Args:
        x: The candidate price.
        name: Field name used in the error message (e.g. ``"entry"``), so a failure identifies
            which side of the markout was malformed.

    Returns:
        ``x`` unchanged, so this can wrap an expression inline.

    Raises:
        SpotMarkoutError: If ``x`` is not strictly greater than zero — ``0.0``, negatives, ``-inf``
            and ``NaN``. NOT raised for ``+inf``; see the known gap above.
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

    Selection depends only on ``close_ts``, so GIVEN DISTINCT ``ts_open_ms`` it is order-independent
    and a re-fetch that paginates differently settles identically. That guarantee does NOT extend to
    duplicate ``ts_open_ms``: two such rows tie on ``close_ts``, ``min`` keeps whichever the wire put
    first, and if their closes differ the settlement PRICE follows wire order. Deduping the wire is
    not this module's job — a caller that needs cross-re-fetch determinism has to guarantee
    ``ts_open_ms`` uniqueness upstream.

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

    ``gross = (future - entry) / entry * 1e4`` basis points, rounded once to the NEAREST integer with
    ties to even — Python's built-in ``round``. The mode is load-bearing rather than incidental: it
    has to satisfy ``fade(+m) == follow(-m)``, and ``math.floor`` does not (``floor(0.5) == 0`` while
    ``-floor(-0.5) == 1``). Under ``floor``, fading an up-move and following the mirror-image
    down-move stop paying the same, which is a farmable directional bias. Then
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
