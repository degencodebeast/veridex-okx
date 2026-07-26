"""(chain x bar) matrix preflight for Signal Trials (H2.3).

The frozen spec (§5.1) makes the season's chain and bar a **probe-gated** decision rather than an
assertion: neither chain's signal density nor the candle endpoint's retention depth is documented,
so both are measured before a season is sealed. This module is that measurement and the decision
taken from it.

Four responsibilities, and each one exists because of a specific way this can go quietly wrong.

1. **The predeclared order is the authority, not the data.** ``COMBO_ORDER`` is frozen ahead of any
   observation. ``select_combo`` iterates ``COMBO_ORDER`` and looks each combo up by key; it never
   iterates ``MatrixProbeResult.counts``. The two coincide whenever a probe builds its counts in
   order, which is exactly why the distinction has to be structural: a later refactor to a dict, or
   a probe that appends counts as they complete, would silently move the tie-break onto whatever
   order the data happened to arrive in. Ties are broken by ``COMBO_ORDER`` because the ordering was
   predeclared — resolving them by data order would let the observation choose its own tie-break.

2. **The three season branches are a closed set, and ``no_season`` has two INDEPENDENT routes.**
   All-zero counts, and unconfirmed direction semantics. Either alone forces ``no_season``, and the
   unconfirmed-direction gate outranks the counts entirely: a matrix that would otherwise qualify is
   still ``no_season`` if we could not read the feed's polarity. §5.1 forbids ever lowering a
   threshold after seeing outcomes, so ``min_trials`` below 1 is rejected rather than honoured —
   a threshold that cannot gate would manufacture a ``qualified`` season out of nothing.

3. **Counting applies the frozen rules, and every rejection is attributed.** Filters, the 4h
   same-token cooldown dedup, a non-null and positive trigger price, and a valid completed
   settlement candle at the ranked horizon under the combo's bar (via H3.2's close-boundary law).
   A rejected signal is never merely dropped: it lands in ``rejection_reasons`` under a named
   reason, because "0 eligible" and "0 eligible because nothing settled" are different findings and
   only the second one tells the operator that retention, not density, is the constraint.

4. **The artifact distinguishes ABSENCE, EMPTINESS and FAILURE.** ``preflight_result.json`` is read
   by a later stage that cannot see the run. This lane's Gate 1 MAJOR was an empty page that could
   not be told apart from genuine absence; here the same shape is worse, because a reader has no
   second source. So: **absence** is the file not being there at all (the write is atomic, so a
   crash leaves the previous artifact or nothing — never a truncated one); **emptiness** is
   ``probe_status="completed"`` with ``season_status="no_season"`` and four real zero counts, which
   is an OBSERVATION; **failure** is ``probe_status="failed"`` with ``season_status=null`` and no
   counts at all, which is not. Both writers emit the same key set in the same order, so a reader
   can always reach ``probe_status`` without a ``KeyError``.

**Polarity is a property of the ENDPOINT, not of a row.** THREE outcomes, not two: a source that
DECLARES the frozen buy-direction contract and returns rows that do not contradict it is CONFIRMED;
a row carrying a marker that is not a recognized buy is a REFUSAL; and a source that declares no
contractual polarity is a REFUSAL as well, however clean its rows look.
 The frozen source is documented as "latest
buy-direction token signals" and its documented rows carry no direction member whatsoever, so the
probe confirms buy semantics when it has READ rows from that source and nothing in them contradicts
the contract. It still fails closed: zero rows confirm nothing, and any row that volunteers a marker
which is not a recognized buy — including one this module cannot read, such as a numeric side code —
unconfirms the whole probe. Mapping ``"1"`` to "buy" would be a guess about an encoding the frozen
contract does not pin, and a wrong guess would invert every trial's stance.

An earlier revision required a per-row marker. That was a defect of the mirror kind: it never
guessed polarity and it never CONFIRMED it either, so every conforming response forced
``no_season`` and Gate B could not publish a season from the frozen source at all.

**No transport lives here.** ``run_matrix_probe`` takes a structural :class:`MarketClient`; the
concrete HTTP client is constructed only by ``scripts/signal_trials/run_preflight.py``, which is
operator-run (Step 5, GATE B). Nothing in this module can issue a network request.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from veridex.signal_trials.challenge_spec import CanonicalSignal, normalize_signal
from veridex.signal_trials.okx_client import Candle, CandleSeries, SignalFilters, SignalPage
from veridex.signal_trials.spot_markout import select_settlement_candle

# The predeclared (chain x bar) preference matrix (§5.1). Precision (`1m`) ranks above nativeness
# (X Layer `196`) because a 1H settlement lag is visible to a finance-aware judge; nativeness breaks
# ties WITHIN a precision tier. Frozen before any observation — that is what makes it a tie-break
# rather than a post-hoc choice.
COMBO_ORDER: tuple[tuple[str, str], ...] = (("196", "1m"), ("501", "1m"), ("196", "1H"), ("501", "1H"))

SeasonStatus = Literal["qualified", "exploratory", "no_season"]
_SEASON_STATUSES: frozenset[str] = frozenset({"qualified", "exploratory", "no_season"})

# FOUR probe statuses, because the output path must ALWAYS describe the invocation that just ran.
# `completed` and `failed` are outcomes of a probe that ran. `refused` and `aborted` are outcomes of
# a run that stopped BEFORE the probe - a non-frozen season policy, and missing credentials. They are
# kept distinct rather than collapsed into one "not run" value because collapsing distinguishable
# states is the defect this task has now hit four times.
ProbeStatus = Literal["completed", "failed", "refused", "aborted"]
PROBE_COMPLETED: ProbeStatus = "completed"
PROBE_FAILED: ProbeStatus = "failed"
PROBE_REFUSED: ProbeStatus = "refused"
PROBE_ABORTED: ProbeStatus = "aborted"

# Statuses that mean NO PROBE RAN. H2.4 must not consume an artifact carrying one.
NOT_RUN_STATUSES: frozenset[str] = frozenset({PROBE_REFUSED, PROBE_ABORTED})

# Where a superseded artifact is moved when a pre-probe refusal replaces it. A single slot, so a
# second refusal overwrites the first - the operator keeps the most recent prior artifact, and the
# name is deterministic so a test can assert it rather than glob for a timestamp.
SUPERSEDED_SUFFIX = ".superseded"

# Runaway guard on cursor pagination. Hitting it RAISES rather than returning what was collected:
# §5.1 requires per-combo counts to be auditable, and a truncated count reported as THE count is a
# false number — the one thing a preflight must not produce.
MAX_PAGES = 100

# Candles per settlement fetch. `before_ms` is deliberately not passed: its wire semantics are not
# pinned by the frozen contract, and guessing a pagination direction would skew every `close_ts`.
# The default window is therefore the latest `CANDLE_LIMIT` candles, so a settlement point outside
# it counts as `no_settlement_candle` — which IS the retention observation §5.1 asks for, not a bug.
CANDLE_LIMIT = 100

# Named rejection reasons. Exported as constants so a caller can branch on them, but every test
# asserts the literal string: a vector derived from the constant cannot notice the constant moving.
REASON_CHAIN_MISMATCH = "chain_mismatch"
REASON_UNPARSEABLE_SIGNAL = "unparseable_signal"
REASON_NULL_TRIGGER_PRICE = "null_trigger_price"
REASON_NON_POSITIVE_TRIGGER_PRICE = "non_positive_trigger_price"
REASON_WALLET_TYPE_FILTER = "wallet_type_filter"
REASON_MIN_ADDRESS_COUNT = "min_address_count"
REASON_MIN_AMOUNT_USD = "min_amount_usd"
REASON_MIN_MARKET_CAP_USD = "min_market_cap_usd"
REASON_SAME_TOKEN_COOLDOWN = "same_token_cooldown"
# H3.2 returns None - UNSCORED - from THREE distinct causes, and PKT-TASK-H2-3-A2 makes keeping them
# apart an unconditional caller obligation. They are different findings for the operator: no candles
# at all is a RETENTION result, an unconfirmed tail is a TIMING result, and a gap past T is a
# LIQUIDITY result. One bucket would answer "nothing settled" and hide which of the three it was.
REASON_NO_CANDLES_RETURNED = "no_candles_returned"
REASON_NO_CONFIRMED_CANDLE = "no_confirmed_candle"
REASON_SETTLEMENT_WINDOW_GAP = "settlement_window_gap"
# The fourth is ours, not H3.2's: a series we refuse to settle on at all. See _with_unique_open_times.
REASON_AMBIGUOUS_SETTLEMENT_CANDLE = "ambiguous_settlement_candle"
REASON_NON_POSITIVE_SETTLEMENT_PRICE = "non_positive_settlement_price"

# The frozen primary source is the OKX "Latest" Signal List, POST /api/v6/dex/market/signal/list,
# documented as "Get latest buy-direction token signals sorted descending by time".
#
# DIRECTION IS A PROPERTY OF THE ENDPOINT, NOT A REPEATED PROPERTY ON EACH ROW. The documented
# return fields are `timestamp`, `chainIndex`, `price`, `walletType`, `triggerWalletCount`,
# `triggerWalletAddress`, `amountUsd`, `soldRatioPercent`, `token.*` and `cursor` - there is no
# `direction`, `side`, `signalType` or `tradeDirection` member at all.
SIGNAL_SOURCE_DIRECTION = "buy"

# The FROZEN season policy (§5.1). One definition, used as the defaults below, as the only values the
# operator entrypoint will accept, and as the `policy` block written into every authoritative
# artifact. A single source of truth is the point: a mutation of any one of these is visible in all
# three places at once, and the artifact can no longer disagree with the run that produced it.
#
# §5.1 fixes qualification at >= 40 trials and EXPLICITLY FORBIDS lowering thresholds after observing
# outcomes; it fixes the same-token cooldown at 4h and the sole ranking horizon at 1h.
FROZEN_MIN_TRIALS = 40
FROZEN_COOLDOWN_MS = 14_400_000
FROZEN_HORIZON_MS = 3_600_000

# Keys a direction marker may arrive under IF a source volunteers one, and the values read as a buy.
# These exist to DETECT A CONTRADICTION of the endpoint contract, never to satisfy it: no documented
# row carries any of them. Textual only - see the module docstring on why numeric side codes are
# excluded from the buy set.
_DIRECTION_KEYS: tuple[str, ...] = ("direction", "side", "signalType", "tradeDirection")
_BUY_MARKERS: frozenset[str] = frozenset({"buy", "b", "long"})


class PreflightError(ValueError):
    """A preflight input or artifact would misrepresent the season.

    ``ValueError`` so callers already treating bad input as ``ValueError`` keep working, but its own
    type so "the matrix is malformed" is never conflated with "the market had nothing to report".
    """


class MarketClient(Protocol):
    """The read surface ``run_matrix_probe`` needs — structural, so no concrete client is imported.

    ``signal_source_direction`` is the SOURCE'S DECLARATION of its own contractual polarity, and it
    is what makes confirmation possible at all. Polarity is a property of the endpoint, so the only
    party that can attest it is whoever bound the client to an endpoint. A source that declares
    nothing — or declares something other than the frozen buy contract — CANNOT have its polarity
    confirmed, and the probe fails closed on it. That is the third outcome: absence of a marker
    under a DECLARED buy-direction source is confirmation by contract, a contradicting marker is
    refusal, and an UNDECLARED source is refusal too.

    This is the seam that makes it impossible for a test to issue a live request without
    deliberately constructing a transport.
    """

    signal_source_direction: str

    async def list_signals(self, f: SignalFilters, cursor: str | None = None) -> SignalPage: ...

    async def get_candles(
        self,
        chain_index: str,
        token: str,
        bar: str,
        *,
        before_ms: int | None = None,
        limit: int = 100,
    ) -> CandleSeries: ...


@dataclass(frozen=True)
class ComboCount:
    """One cell of the matrix: how many trials a combo would settle, and why the rest did not.

    Field ORDER is load-bearing — the plan's mandated test helper constructs this positionally.
    """

    chain_index: str
    bar: str
    eligible_settleable: int
    rejection_reasons: dict[str, int]


@dataclass(frozen=True)
class MatrixProbeResult:
    """The whole matrix plus the polarity finding.

    ``direction_semantics_confirmed`` is a property of the FEED, not of a combo, which is why it
    sits here and gates every combo at once.
    """

    counts: tuple[ComboCount, ...]
    direction_semantics_confirmed: bool


@dataclass(frozen=True)
class ComboSelection:
    """The season decision. ``chain_index`` and ``bar`` are both ``None`` exactly when no season."""

    chain_index: str | None
    bar: str | None
    season_status: SeasonStatus


def _probe_chains() -> tuple[str, ...]:
    """The distinct chains in ``COMBO_ORDER``, in first-appearance order."""
    return tuple(dict.fromkeys(chain_index for chain_index, _ in COMBO_ORDER))


def _bump(bucket: dict[str, int], reason: str, amount: int = 1) -> None:
    bucket[reason] = bucket.get(reason, 0) + amount


def _index_counts(counts: Sequence[ComboCount]) -> dict[tuple[str, str], ComboCount]:
    """Key the matrix by combo, refusing anything that is not exactly the frozen four.

    Fails CLOSED on every malformation. A partial matrix would let a season be chosen from combos
    that were never probed; a duplicate would make the answer depend on which copy won; a combo
    outside the frozen matrix would settle the season against an unpredeclared market; a negative
    count is not an observation at all. None of these can be repaired by guessing, so none is.
    """
    indexed: dict[tuple[str, str], ComboCount] = {}
    for count in counts:
        key = (count.chain_index, count.bar)
        if key not in COMBO_ORDER:
            raise PreflightError(f"matrix carries an unknown combo {key!r}; the frozen matrix is {list(COMBO_ORDER)}")
        if key in indexed:
            raise PreflightError(f"matrix carries a duplicate entry for combo {key!r}")
        if count.eligible_settleable < 0:
            raise PreflightError(f"combo {key!r} has a negative eligible_settleable count: {count.eligible_settleable}")
        indexed[key] = count

    missing = [combo for combo in COMBO_ORDER if combo not in indexed]
    if missing:
        raise PreflightError(f"matrix is missing {len(missing)} of the frozen combos: {missing}")
    return indexed


def _require_usable_threshold(min_trials: int) -> None:
    if min_trials < 1:
        raise PreflightError(
            f"min_trials must be at least 1, got {min_trials!r}; a threshold that cannot gate would "
            f"manufacture a 'qualified' season, and §5.1 forbids lowering the threshold at all"
        )


def select_combo(result: MatrixProbeResult, *, min_trials: int = FROZEN_MIN_TRIALS) -> ComboSelection:
    """Choose the season's combo and status from a completed matrix probe.

    - ``qualified``: the FIRST combo in ``COMBO_ORDER`` whose count is ``>= min_trials``. Inclusive
      on purpose — the frozen target is "≥ 40", and a ``>`` here would silently move the bar to 41.
    - ``exploratory``: no combo reaches ``min_trials`` but at least one is non-zero → the argmax,
      ties broken by ``COMBO_ORDER``. ``max`` over ``COMBO_ORDER`` (not over ``result.counts``)
      returns the first maximal element in PREDECLARED order, so the tie-break cannot drift onto the
      order the counts happen to be stored in.
    - ``no_season``: all counts zero, OR direction semantics unconfirmed. Both gates run BEFORE the
      qualification scan, so neither can be outvoted by a large count or a small threshold.

    Args:
        result: The probe matrix. Must carry exactly the four frozen combos, in any order.
        min_trials: Qualification threshold, ``>= 1``.

    Returns:
        A ``ComboSelection`` naming a chain and bar, or ``(None, None, "no_season")``.

    Raises:
        PreflightError: If ``min_trials < 1``, or the matrix is partial, duplicated, carries a combo
            outside ``COMBO_ORDER``, or carries a negative count.
    """
    _require_usable_threshold(min_trials)
    indexed = _index_counts(result.counts)

    if not result.direction_semantics_confirmed:
        return ComboSelection(None, None, "no_season")
    if all(indexed[combo].eligible_settleable == 0 for combo in COMBO_ORDER):
        return ComboSelection(None, None, "no_season")

    for chain_index, bar in COMBO_ORDER:
        if indexed[(chain_index, bar)].eligible_settleable >= min_trials:
            return ComboSelection(chain_index, bar, "qualified")

    chain_index, bar = max(COMBO_ORDER, key=lambda combo: indexed[combo].eligible_settleable)
    return ComboSelection(chain_index, bar, "exploratory")


def _contradicts_buy_direction(raw: Mapping[str, Any]) -> bool:
    """Whether ``raw`` EXPLICITLY declares a direction that is not a buy.

    The polarity of the frozen source is fixed by its ENDPOINT CONTRACT, so a row carrying no
    direction member does NOT contradict it — that is the documented shape of every conforming row.
    Requiring a per-row marker was a real defect: it made ``direction_semantics_confirmed`` False for
    every conforming response, which forced ``no_season`` regardless of eligible counts and left
    Gate B unable to publish a season from the frozen primary source at all.

    This still FAILS CLOSED, which is the half worth keeping. If the client is ever generalised to a
    source that volunteers a direction, any row whose marker is not a recognized buy — including one
    this module cannot READ, such as a numeric side code — counts as a contradiction and unconfirms
    the whole probe. EVERY marker present is checked, not merely the first: a row carrying
    ``direction="buy"`` beside ``side="sell"`` has told us we do not understand its schema.

    "Never guess polarity" and "never confirm polarity" are different properties. The first is kept
    here; the second was the defect.
    """
    for key in _DIRECTION_KEYS:
        if key in raw:
            value = raw[key]
            if not (isinstance(value, str) and value.strip().casefold() in _BUY_MARKERS):
                return True
    return False


def _screen(raw: Mapping[str, Any], chain_index: str, filters: SignalFilters) -> CanonicalSignal | str:
    """Apply every chain-level eligibility rule to one wire signal.

    Returns the canonical signal when it survives, or the ``str`` reason naming the FIRST rule it
    broke. A union rather than a ``(signal, reason)`` pair so that "both" and "neither" are not
    representable — a pair would need a fallback for the impossible case, and a fallback that can
    never run is a rejection reason that could silently become wrong.

    The rules are re-applied here even though the request already carried them: the filters are
    enforced by OKX, and counting a row the server should have excluded would inflate a count that
    decides whether a season may claim skill.

    ``minLiquidityUsd`` is deliberately NOT re-checked. It is filter-only (§6) and liquidity is not
    a ``CanonicalSignal`` field, because reading it back would be exactly the leakage
    ``FORBIDDEN_EVIDENCE_FIELDS`` exists to prevent (PKT-DEC-C15). It stays enforced server-side.
    """
    if raw.get("chainIndex") != chain_index:
        return REASON_CHAIN_MISMATCH
    if raw.get("price") is None:
        return REASON_NULL_TRIGGER_PRICE

    try:
        signal = normalize_signal(dict(raw), "rest")
    except (ValueError, TypeError):
        return REASON_UNPARSEABLE_SIGNAL

    # A non-positive entry can never be scored: `spot_markout` rejects it outright, so counting the
    # trial as settleable would promise a settlement the law will refuse.
    if not signal.trigger_price > 0:
        return REASON_NON_POSITIVE_TRIGGER_PRICE
    # `SignalFilters.wallet_type` is the numeric wire code (§5.1 pins `walletType=1`); the canonical
    # value is a sorted comma-joined set of codes, so a multi-category signal qualifies iff the
    # filtered category is among them.
    if filters.wallet_type not in signal.wallet_type.split(","):
        return REASON_WALLET_TYPE_FILTER
    if signal.trigger_wallet_count < filters.min_address_count:
        return REASON_MIN_ADDRESS_COUNT
    if signal.amount_usd < filters.min_amount_usd:
        return REASON_MIN_AMOUNT_USD
    if signal.market_cap_usd < filters.min_market_cap_usd:
        return REASON_MIN_MARKET_CAP_USD
    return signal


def _dedup_by_cooldown(signals: Iterable[CanonicalSignal], cooldown_ms: int) -> tuple[list[CanonicalSignal], int]:
    """Collapse same-token signals inside the cooldown to one trial (§8.6).

    Sorted ascending by ``t0_ms`` FIRST, then swept forward keeping the earliest of each cluster.
    Both halves matter. The feed is "Latest"-first, so sweeping in wire order would keep a different
    member of each cluster and a re-fetch that paginated differently could report a different count.
    Keeping the EARLIEST is the no-look-ahead choice: it is the one a chronological replay reaches
    first, and it is decided without consulting anything that happens later.

    The window is half-open — exactly ``cooldown_ms`` apart is OUTSIDE the cooldown and survives.

    Returns:
        The kept signals and the number dropped.
    """
    ordered = sorted(signals, key=lambda signal: (signal.t0_ms, signal.token_address, signal.trigger_wallet_address))
    kept: list[CanonicalSignal] = []
    last_kept_ms: dict[str, int] = {}
    dropped = 0
    for signal in ordered:
        previous = last_kept_ms.get(signal.token_address)
        if previous is not None and signal.t0_ms - previous < cooldown_ms:
            dropped += 1
            continue
        last_kept_ms[signal.token_address] = signal.t0_ms
        kept.append(signal)
    return kept, dropped


def _with_unique_open_times(series: CandleSeries) -> CandleSeries | None:
    """Guarantee the ``ts_open_ms`` uniqueness H3.2 requires OF ITS CALLERS, or refuse the series.

    H3.2 selects on ``close_ts`` and documents that duplicate ``ts_open_ms`` TIE: ``min`` keeps
    whichever row the wire put first, so when their closes differ **the settlement price follows
    wire order**. Deduping the wire is explicitly not H3.2's job - its docstring says a caller
    needing cross-re-fetch determinism must guarantee uniqueness upstream. A preflight is exactly
    such a caller: a count that changes because a re-fetch paginated differently is not an auditable
    count, and auditable per-combo counts are what §5.1 asks this probe to produce.

    Two cases, and they are NOT the same thing:

    - duplicates that are IDENTICAL carry no ambiguity - whichever row wins, the settlement is the
      same - so they collapse and the series is handed on with unique open times;
    - duplicates that DIFFER in any field make the settlement wire-order dependent. There is no
      non-arbitrary way to choose, so the series is REFUSED rather than settled on a coin flip.
      Picking by position is the exact behaviour this guard exists to prevent.

    Equality is compared over the WHOLE frozen ``Candle``, not over the fields selection happens to
    read today. That is deliberate: it cannot go stale if H3.2's eligibility rule ever consults
    another field.

    No sort is applied. Once open times are unique H3.2 guarantees selection is order-independent,
    so ordering carries no property here - which is precisely why UNIQUENESS is the caller's job and
    ordering is not.

    Returns:
        The series with duplicates collapsed, or ``None`` when it is irreducibly ambiguous.
    """
    by_open_time: dict[int, Candle] = {}
    for candle in series.candles:
        seen = by_open_time.get(candle.ts_open_ms)
        if seen is None:
            by_open_time[candle.ts_open_ms] = candle
        elif seen != candle:
            return None
    if len(by_open_time) == len(series.candles):
        return series
    return CandleSeries(bar=series.bar, bar_ms=series.bar_ms, candles=tuple(by_open_time.values()))


def _unscored_reason(series: CandleSeries) -> str:
    """Name WHICH of H3.2's three UNSCORED causes produced a ``None``.

    The first two are read directly off the series; the third is reached by ELIMINATION. That
    matters: re-deriving H3.2's close-boundary window here would make the two computations agree by
    construction, and a comparison where both sides carry the same defect reports agreement rather
    than correctness.
    """
    if not series.candles:
        return REASON_NO_CANDLES_RETURNED
    if not any(candle.confirmed for candle in series.candles):
        return REASON_NO_CONFIRMED_CANDLE
    return REASON_SETTLEMENT_WINDOW_GAP


async def _collect_signals(client: MarketClient, filters: SignalFilters) -> list[dict[str, Any]]:
    """Paginate one chain's Latest Signal List to exhaustion, following the cursor.

    Raises rather than truncating: see ``MAX_PAGES``.
    """
    collected: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        page = await client.list_signals(filters, cursor)
        collected.extend(dict(row) for row in page.signals)
        cursor = page.next_cursor
        if cursor is None:
            return collected
    raise PreflightError(
        f"pagination for chain {filters.chain_index!r} exceeded MAX_PAGES={MAX_PAGES} with a cursor still "
        f"outstanding; the counts would be a floor rather than a count, so the probe fails instead"
    )


async def run_matrix_probe(
    client: MarketClient,
    filters_by_chain: Mapping[str, SignalFilters],
    *,
    cooldown_ms: int = FROZEN_COOLDOWN_MS,
    horizon_ms: int = FROZEN_HORIZON_MS,
    min_trials: int = FROZEN_MIN_TRIALS,
) -> MatrixProbeResult:
    """Measure the eligible-settleable count for all four frozen combos.

    Per chain: paginate the Latest Signal List to exhaustion, screen every row against the frozen
    eligibility rules, collapse same-token signals inside the cooldown, then — for each of that
    chain's bars — settle each surviving trial through H3.2's close-boundary law. Candle series are
    fetched once per ``(chain, token, bar)`` and reused; the client is deterministic over a probe,
    so caching cannot change a count.

    ``min_trials`` does NOT affect counting: counts are exhaustive within ``MAX_PAGES``, because
    §5.1 requires the per-combo counts to be auditable and an early stop would report a floor as a
    count. It is validated here so an unusable threshold fails BEFORE a 30-minute live probe rather
    than after it, and so the probe and ``select_combo`` can never be handed different thresholds
    without one of them objecting.

    Direction semantics are read from the RAW feed, before filtering. A signal our filters drop
    still tells us what the feed MEANS, and judging polarity only on the survivors would let a
    filtered sell signal pass unnoticed. Zero observations confirm nothing.

    Args:
        client: Structural market read surface. No transport is constructed here.
        filters_by_chain: One ``SignalFilters`` per probed chain, keyed by chain index. Each entry's
            ``chain_index`` must equal its key.
        cooldown_ms: Same-token dedup window. Frozen default 4h.
        horizon_ms: Ranked settlement horizon. Frozen default 1h.
        min_trials: Qualification threshold, validated but not applied to counting.

    Returns:
        A ``MatrixProbeResult`` whose ``counts`` are in ``COMBO_ORDER``.

    Raises:
        PreflightError: For an unusable threshold or window, a missing or mis-keyed filter entry, or
            pagination past ``MAX_PAGES``.
        OKXClientError: Propagated unchanged from the client. A failed probe must never be
            representable as a legitimate zero-count matrix.
    """
    _require_usable_threshold(min_trials)
    if cooldown_ms < 0:
        raise PreflightError(f"cooldown_ms must be non-negative, got {cooldown_ms!r}")
    if horizon_ms < 1:
        raise PreflightError(f"horizon_ms must be positive, got {horizon_ms!r}")

    # Confirmation comes from the SOURCE'S CONTRACT, never from a row. A source that does not
    # declare the frozen buy-direction contract cannot have its polarity confirmed at all, however
    # clean its rows look — the third outcome, and the one that keeps this fail-closed if the client
    # is ever generalised to a source whose polarity is not contractually fixed.
    source_declares_buy = getattr(client, "signal_source_direction", None) == SIGNAL_SOURCE_DIRECTION

    chains = _probe_chains()
    for chain_index in chains:
        filters = filters_by_chain.get(chain_index)
        if filters is None:
            raise PreflightError(
                f"filters_by_chain has no entry for chain {chain_index!r}; the frozen matrix probes {list(chains)}"
            )
        # The sharpest failure available here: probing one chain while recording it under another
        # would settle every trial of the season against the wrong market, with counts that look
        # entirely plausible. Cheap to check, and impossible to notice afterwards.
        if filters.chain_index != chain_index:
            raise PreflightError(
                f"filters registered under chain {chain_index!r} carry chain_index {filters.chain_index!r}; "
                f"probing one chain and recording it as another would settle the season against the wrong market"
            )

    counts: dict[tuple[str, str], int] = dict.fromkeys(COMBO_ORDER, 0)
    reasons: dict[tuple[str, str], dict[str, int]] = {combo: {} for combo in COMBO_ORDER}
    series_cache: dict[tuple[str, str, str], CandleSeries | None] = {}
    observed = 0
    contradicting = 0

    for chain_index in chains:
        bars = tuple(bar for chain, bar in COMBO_ORDER if chain == chain_index)
        raw_signals = await _collect_signals(client, filters_by_chain[chain_index])
        observed += len(raw_signals)
        contradicting += sum(1 for raw in raw_signals if _contradicts_buy_direction(raw))

        eligible: list[CanonicalSignal] = []
        for raw in raw_signals:
            screened = _screen(raw, chain_index, filters_by_chain[chain_index])
            if isinstance(screened, str):
                # A chain-level rejection excludes the signal under BOTH of that chain's bars.
                for bar in bars:
                    _bump(reasons[(chain_index, bar)], screened)
                continue
            eligible.append(screened)

        kept, dropped = _dedup_by_cooldown(eligible, cooldown_ms)
        if dropped:
            for bar in bars:
                _bump(reasons[(chain_index, bar)], REASON_SAME_TOKEN_COOLDOWN, dropped)

        for signal in kept:
            for bar in bars:
                cache_key = (chain_index, signal.token_address, bar)
                if cache_key not in series_cache:
                    fetched = await client.get_candles(chain_index, signal.token_address, bar, limit=CANDLE_LIMIT)
                    series_cache[cache_key] = _with_unique_open_times(fetched)
                series = series_cache[cache_key]
                if series is None:
                    _bump(reasons[(chain_index, bar)], REASON_AMBIGUOUS_SETTLEMENT_CANDLE)
                    continue
                candle = select_settlement_candle(series, t0_ms=signal.t0_ms, horizon_ms=horizon_ms)
                if candle is None:
                    _bump(reasons[(chain_index, bar)], _unscored_reason(series))
                elif not candle.close > 0:
                    _bump(reasons[(chain_index, bar)], REASON_NON_POSITIVE_SETTLEMENT_PRICE)
                else:
                    counts[(chain_index, bar)] += 1

    return MatrixProbeResult(
        counts=tuple(
            ComboCount(chain_index, bar, counts[(chain_index, bar)], dict(reasons[(chain_index, bar)]))
            for chain_index, bar in COMBO_ORDER
        ),
        direction_semantics_confirmed=source_declares_buy and observed > 0 and contradicting == 0,
    )


def _validate_selection(
    sel: ComboSelection, indexed: Mapping[tuple[str, str], ComboCount], direction_confirmed: bool
) -> None:
    """Refuse to emit a selection the matrix beside it does not support.

    The artifact is read by a stage that cannot see the run, so a fabricated or drifted selection is
    undetectable downstream. Everything checkable without re-deciding the season is checked here.
    """
    if sel.season_status not in _SEASON_STATUSES:
        raise PreflightError(
            f"unrecognized season_status {sel.season_status!r}; expected one of {sorted(_SEASON_STATUSES)}"
        )

    if sel.season_status == "no_season":
        if sel.chain_index is not None or sel.bar is not None:
            raise PreflightError(
                f"a no_season selection must name neither chain nor bar, got "
                f"chain_index={sel.chain_index!r} bar={sel.bar!r}"
            )
        # The mirror direction. `no_season` has exactly two routes, so an artifact claiming it over a
        # matrix with neither all-zero counts nor unconfirmed direction is claiming a branch that
        # cannot have produced it — and that reads downstream as a legitimately barren season.
        if direction_confirmed and any(count.eligible_settleable > 0 for count in indexed.values()):
            raise PreflightError(
                "a no_season artifact requires all-zero counts or unconfirmed direction semantics; "
                "this matrix has confirmed direction and a non-zero count, so no_season is unreachable"
            )
        return

    if sel.chain_index is None or sel.bar is None:
        raise PreflightError(
            f"a {sel.season_status!r} selection must name both chain_index and bar, got "
            f"chain_index={sel.chain_index!r} bar={sel.bar!r}"
        )
    if not direction_confirmed:
        raise PreflightError(
            f"a {sel.season_status!r} season cannot be claimed while direction_semantics_confirmed is False"
        )

    key = (sel.chain_index, sel.bar)
    count = indexed.get(key)
    if count is None:
        raise PreflightError(f"selected combo {key!r} is not present in the probe matrix")
    if count.eligible_settleable <= 0:
        raise PreflightError(f"selected combo {key!r} has a zero eligible_settleable count and cannot carry a season")


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Serialize ``payload`` and move it into place in one step.

    Atomic because ABSENCE is a meaningful state: a half-written artifact would be a fourth state
    that reads as a corrupt version of one of the three, and a reader has no way to tell. Either the
    previous artifact survives or the new one lands whole.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _artifact(
    *,
    probe_status: ProbeStatus,
    season_status: str | None,
    chain_index: str | None,
    bar: str | None,
    counts: list[dict[str, Any]],
    rejection_reasons: dict[str, int],
    direction_semantics_confirmed: bool,
    failure_reason: str | None,
) -> dict[str, Any]:
    """The single artifact schema. Both writers go through it, so the key set cannot diverge.

    ``policy`` states the season rules the artifact was produced under. Without it a run under a
    lowered threshold is INDISTINGUISHABLE downstream from a conforming one and can still be labelled
    ``qualified`` — and this artifact is the authority H2.4 consumes.
    """
    return {
        "probe_status": probe_status,
        "season_status": season_status,
        "chain_index": chain_index,
        "bar": bar,
        "counts": counts,
        "rejection_reasons": rejection_reasons,
        "direction_semantics_confirmed": direction_semantics_confirmed,
        "policy": {
            "min_trials": FROZEN_MIN_TRIALS,
            "cooldown_ms": FROZEN_COOLDOWN_MS,
            "horizon_ms": FROZEN_HORIZON_MS,
        },
        "failure_reason": failure_reason,
    }


def write_preflight_result(sel: ComboSelection, result: MatrixProbeResult, path: Path) -> None:
    """Write the machine-readable ``preflight_result.json`` for a COMPLETED probe.

    ``counts`` are serialized in ``COMBO_ORDER`` whatever order they arrived in, each carrying its
    own ``rejection_reasons``. The top-level ``rejection_reasons`` is the AGGREGATE across all four
    combos — the per-combo maps are authoritative, the aggregate is the operator-facing summary. It
    aggregates all four rather than the selected combo alone precisely because a ``no_season``
    artifact has no selected combo, which is when the diagnostic is most needed.

    Args:
        sel: The season decision.
        result: The matrix it was decided from.
        path: Destination. Parent directories are created; the write is atomic.

    Raises:
        PreflightError: If the matrix is malformed, or the selection is not supported by it — an
            unrecognized status, a season claimed over unconfirmed direction, a season with no combo
            or a combo with no season, a combo absent from the matrix, or a combo with a zero count.
            Nothing is written in any of those cases, and any existing artifact is left untouched.
    """
    indexed = _index_counts(result.counts)
    _validate_selection(sel, indexed, result.direction_semantics_confirmed)

    # The artifact declares the FROZEN policy, so it must not carry a selection the frozen policy
    # would not have reached. Re-deriving is cheap and closes the `min_trials` half of the operator
    # override at the writer as well as at the CLI.
    #
    # REACH, stated exactly rather than implied: this catches a threshold deviation, because
    # min_trials changes the DECISION over a fixed matrix. It does NOT catch a cooldown or horizon
    # deviation — those change the COUNTS, and a matrix carries no record of how it was counted.
    # Those are refused at the operator entrypoint, which is the only place they can be supplied.
    frozen = select_combo(result, min_trials=FROZEN_MIN_TRIALS)
    if (frozen.chain_index, frozen.bar, frozen.season_status) != (sel.chain_index, sel.bar, sel.season_status):
        raise PreflightError(
            f"selection {(sel.chain_index, sel.bar, sel.season_status)!r} is not what the frozen policy "
            f"(min_trials={FROZEN_MIN_TRIALS}) produces for this matrix, which is "
            f"{(frozen.chain_index, frozen.bar, frozen.season_status)!r}; the authoritative artifact "
            f"must not represent a different experiment as the frozen season"
        )

    ordered = [indexed[combo] for combo in COMBO_ORDER]
    aggregate: dict[str, int] = {}
    for count in ordered:
        for reason, amount in count.rejection_reasons.items():
            _bump(aggregate, reason, amount)

    _write_atomic(
        path,
        _artifact(
            probe_status=PROBE_COMPLETED,
            season_status=sel.season_status,
            chain_index=sel.chain_index,
            bar=sel.bar,
            counts=[
                {
                    "chain_index": count.chain_index,
                    "bar": count.bar,
                    "eligible_settleable": count.eligible_settleable,
                    "rejection_reasons": dict(count.rejection_reasons),
                }
                for count in ordered
            ],
            rejection_reasons=aggregate,
            direction_semantics_confirmed=result.direction_semantics_confirmed,
            failure_reason=None,
        ),
    )


def supersede_existing_artifact(path: Path) -> Path | None:
    """Move any artifact already at ``path`` aside, atomically, and return where it went.

    A pre-probe refusal must leave nothing CONSUMABLE at the authoritative path, but destroying a
    legitimate earlier result because an operator mistyped a flag would be its own defect. Renaming
    satisfies both: the previous bytes survive under a name H2.4 does not read, and the authoritative
    path is free for a record of the run that actually just happened.

    Returns:
        The path the previous artifact was moved to, or ``None`` if there was nothing there.
    """
    if not path.exists():
        return None
    superseded = path.with_name(path.name + SUPERSEDED_SUFFIX)
    os.replace(path, superseded)
    return superseded


def write_preflight_not_run(probe_status: ProbeStatus, reason: str, path: Path) -> None:
    """Record that a run stopped BEFORE the probe, at the authoritative path.

    This exists because ABSENCE is only honest when the path is genuinely absent. A refused run that
    writes nothing leaves whatever a PREVIOUS run left behind, and H2.4 - which reads the artifact
    and cannot see the terminal - would consume a stale combo as though it were this invocation's
    result. The machine-readable contract exists precisely so correctness does not depend on
    terminal history.

    ``season_status`` is null and ``counts`` is empty for the same reason they are on a failure: no
    observation was made, and four zeroes would be a claim.

    Raises:
        PreflightError: If ``probe_status`` is not a not-run status, or ``reason`` is blank.
    """
    if probe_status not in NOT_RUN_STATUSES:
        raise PreflightError(
            f"write_preflight_not_run is for a run that never probed; got probe_status "
            f"{probe_status!r}, expected one of {sorted(NOT_RUN_STATUSES)}"
        )
    if not reason.strip():
        raise PreflightError("failure_reason must be non-empty: a refusal with no reason is not a record")
    _write_atomic(
        path,
        _artifact(
            probe_status=probe_status,
            season_status=None,
            chain_index=None,
            bar=None,
            counts=[],
            rejection_reasons={},
            direction_semantics_confirmed=False,
            failure_reason=reason,
        ),
    )


def write_preflight_failure(reason: str, path: Path) -> None:
    """Write an artifact recording that the probe did NOT complete.

    Same key set and order as ``write_preflight_result``, so a reader always reaches
    ``probe_status``. ``season_status`` is ``null`` rather than ``"no_season"``: a failed probe made
    no observation, and collapsing it onto the emptiness result is the exact confusion this artifact
    exists to prevent. ``counts`` is empty for the same reason — four zeroes would be a claim.

    Raises:
        PreflightError: If ``reason`` is blank. A failure with no reason is indistinguishable from a
            sloppy success, and nothing is written.
    """
    if not reason.strip():
        raise PreflightError(
            "failure_reason must be non-empty: a failure artifact with no reason is indistinguishable "
            "from a sloppy success"
        )
    _write_atomic(
        path,
        _artifact(
            probe_status=PROBE_FAILED,
            season_status=None,
            chain_index=None,
            bar=None,
            counts=[],
            rejection_reasons={},
            direction_semantics_confirmed=False,
            failure_reason=reason,
        ),
    )
