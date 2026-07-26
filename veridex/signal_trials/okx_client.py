"""OKX DEX market client for Signal Trials (H2.1).

Two read paths, both transport-injected (no HTTP library here — the concrete transport is
out of scope for this task):

- ``list_signals``  -> ``POST /api/v6/dex/market/signal/list`` (the "Latest" Signal List).
- ``get_candles``   -> ``GET  /api/v6/dex/market/historical-candles`` (the frozen settlement
  source, design spec §7).

Two trust-relevant properties this module is responsible for:

1. **Bar provenance.** The candle wire rows carry no bar field, so ``CandleSeries.bar`` /
   ``bar_ms`` are taken from the REQUEST argument, not inferred from the payload. §7 requires
   ``bar``/``bar_ms`` to be persisted in every receipt, and a season must never mix bars.
2. **Credential secrecy.** ``OKXCredentials`` has a redacting ``__repr__`` so a traceback or a
   log line can never echo the secret key or passphrase. This module performs no logging at all.
3. **Fail closed, never quietly empty.** An error envelope, an off-contract signal row, or an
   off-contract candle row raises (``OKXAPIError`` / ``OKXResponseError``) instead of degrading
   into an empty page or series. §7 permits an empty settlement result only for genuine absence,
   so a swallowed API failure would masquerade as a lawful ``UNSCORED`` — a false provenance
   claim rather than a diagnostic. A candle number that parses to ``+inf``, ``-inf`` or ``NaN``
   is off-contract in exactly this sense and is refused here rather than downstream — see
   ``_finite_candle_number``.

Normalization of the raw signal dicts is task H2.2 and deliberately does NOT happen here —
``SignalPage.signals`` carries the wire dicts untouched.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode

SIGNAL_LIST_PATH = "/api/v6/dex/market/signal/list"
HISTORICAL_CANDLES_PATH = "/api/v6/dex/market/historical-candles"

# OKX signals application errors in the envelope `code`, not in the HTTP status, so an
# `raise_for_status` in the transport cannot stand in for this check.
OKX_SUCCESS_CODE = "0"

# The frozen candle wire record: [ts, o, h, l, c, vol, volUsd, confirm] (implementation-plan.md:51).
CANDLE_FIELD_COUNT = 8

# Bar width in milliseconds. The (chain x bar) preference matrix (§5.1) selects one bar for the
# whole season: `1m` preferred, `1H` the fallback. Anything else is rejected rather than guessed,
# because a wrong bar width silently corrupts the §7 close-boundary law.
BAR_MS: dict[str, int] = {"1m": 60_000, "1H": 3_600_000}


class OKXClientError(Exception):
    """Base for every OKX response this client refuses to convert into a result."""


class OKXAPIError(OKXClientError):
    """OKX returned a non-success envelope ``code``.

    This must never be absorbed into an empty page or series: §7 reserves an empty settlement
    result for genuine absence (``UNSCORED``), so silently swallowing an auth/parameter/API
    failure would turn a diagnostic failure into a false provenance claim.
    """

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"OKX returned non-success code {code!r}: {msg or '<no msg>'}")
        self.code = code
        self.msg = msg


class OKXResponseError(OKXClientError, ValueError):
    """A response did not match the frozen wire contract (bad envelope shape or candle row).

    Also a ``ValueError`` so that callers written against H2.1's documented malformed-row
    behaviour keep working.
    """


class Transport(Protocol):
    """The HTTP seam. Implementations MUST serialize ``params`` in iteration order.

    The OKX signature covers the request path *including* its query string, so a transport that
    reorders or re-encodes ``params`` would produce a body/URL that no longer matches the
    ``OK-ACCESS-SIGN`` computed here.
    """

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None,
        json_body: object | None,
        headers: dict[str, str],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, repr=False)
class OKXCredentials:
    """OKX API credentials.

    ``repr=False`` + the explicit ``__repr__`` below is a security control, not cosmetics: the
    generated dataclass repr would leak ``secret_key`` and ``passphrase`` into any traceback,
    pytest assertion diff, or structured log that renders the object.
    """

    api_key: str
    secret_key: str
    passphrase: str
    base_url: str = "https://web3.okx.com"

    def __repr__(self) -> str:
        return f"OKXCredentials(api_key='***', secret_key='***', passphrase='***', base_url={self.base_url!r})"


@dataclass(frozen=True)
class SignalFilters:
    """Predeclared MVP dataset filters (§5.1). Defaults are the frozen manifest values."""

    chain_index: str
    wallet_type: str = "1"
    min_address_count: int = 2
    min_amount_usd: int = 1000
    min_market_cap_usd: int = 100_000
    min_liquidity_usd: int = 20_000


@dataclass(frozen=True)
class Candle:
    ts_open_ms: int
    open: float
    high: float
    low: float
    close: float
    vol: float
    vol_usd: float
    confirmed: bool


@dataclass(frozen=True)
class CandleSeries:
    """Candles plus their bar provenance.

    ``bar`` / ``bar_ms`` come from the request argument — the wire carries no bar field, and §7's
    close-boundary law needs ``B`` to compute ``close_ts = ts + B``.
    """

    bar: str
    bar_ms: int
    candles: tuple[Candle, ...]


@dataclass(frozen=True)
class SignalPage:
    """A raw page of signals. ``signals`` holds the wire dicts untouched — normalization is H2.2."""

    signals: tuple[dict[str, Any], ...]
    next_cursor: str | None


def _timestamp() -> str:
    """ISO-8601 UTC with millisecond precision, e.g. ``2026-07-25T21:55:42.123Z``."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _finite_candle_number(raw: Any, wire_column: str, field: str) -> float:
    """Parse one numeric candle column, refusing a value that is not a finite number.

    ``float("1e400")`` is **not** a parse error — it is ``inf``, an ordinary-looking decimal that
    overflows silently — and ``float("nan")`` is ``NaN``. Either would enter ``Candle`` as a price
    and be indistinguishable downstream from a measurement. Measured, not supposed: a series whose
    ``p0`` close parsed as ``+inf`` made ``contestants.compute_ext`` return ``1`` — a confident
    "already extended" verdict manufactured from a value that should never have been parsed.

    So this fails **loudly**, exactly as the envelope and row-shape guards above do. Rejecting at
    the parse boundary fixes the root cause once for every consumer instead of asking each one to
    guard a value it should never have received (PKT-DEC-C28 ruling 1). ``+inf``, ``-inf`` and
    ``NaN`` are all off-contract for a candle number; ``NaN`` in particular fails every comparison
    it takes part in, so only an explicit ``math.isfinite`` covers all three.

    Deliberately **not** a positivity check. §5.3's ``p0, p1 > 0`` is Law's rule, and inventing a
    stricter one at this boundary would change a settlement answer without authority — the hazard
    C28 names. A negative finite value passes this guard unchanged.

    Args:
        raw: The wire value for one numeric column, as OKX serialized it.
        wire_column: The documented upstream column name, for the diagnostic.
        field: The ``Candle`` field this column lands in, for the diagnostic.

    Returns:
        The parsed value, guaranteed finite.

    Raises:
        OKXResponseError: ``raw`` parsed successfully but to ``+inf``, ``-inf`` or ``NaN``. This is
            the only exception this function itself raises.
        ValueError: ``float()`` could not parse ``raw`` at all — ``"abc"``, ``""``. Propagates from
            ``float()`` unconverted, exactly as it did before this guard existed. Listed because a
            ``Raises`` section that names only the new failure reads as though it were the only one.
            Note ``OKXResponseError`` is itself a ``ValueError`` subclass, so a caller catching
            ``ValueError`` catches both — and a test that wants to tell them apart must assert the
            EXACT type rather than use ``isinstance``.
        TypeError: ``raw`` was not a type ``float()`` accepts — ``None``. Same provenance.

    The ``ValueError``/``TypeError`` paths PREDATE this guard and are deliberately left alone.
    Converting them to ``OKXResponseError`` would be tidier and is out of scope: PKT-DEC-C28
    authorizes rejecting NON-FINITE values here, which is a different change from rejecting
    UNPARSEABLE ones. Documented rather than silently widened.
    """
    parsed = float(raw)
    if not math.isfinite(parsed):
        raise OKXResponseError(
            f"candle column {wire_column!r} (Candle.{field}) must be a finite number, "
            f"got {raw!r} which parsed as {parsed}"
        )
    return parsed


def _rows(payload: dict[str, Any]) -> list[Any]:
    """Return the envelope's ``data`` list, failing closed on anything that is not a success.

    Only a success ``code`` with a list ``data`` may produce an empty result. Everything else
    raises, because an empty page/series is a *claim* — that OKX had nothing to report — and an
    error envelope is not evidence for that claim.
    """
    if not isinstance(payload, dict):
        raise OKXResponseError(f"OKX response must be a JSON object, got {type(payload).__name__}")
    if "code" not in payload:
        raise OKXResponseError("OKX response is missing the envelope 'code' field")
    code = str(payload["code"])
    if code != OKX_SUCCESS_CODE:
        raise OKXAPIError(code, str(payload.get("msg", "")))
    data = payload.get("data")
    if not isinstance(data, list):
        raise OKXResponseError(f"OKX response 'data' must be a list, got {type(data).__name__}")
    return list(data)


class OKXMarketClient:
    def __init__(self, transport: Transport, creds: OKXCredentials) -> None:
        self._transport = transport
        self._creds = creds

    def _headers(self, method: str, request_path: str, body: str) -> dict[str, str]:
        """Standard OKX HMAC scheme over ``timestamp + METHOD + request_path(+query) + body``."""
        timestamp = _timestamp()
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        signature = base64.b64encode(
            hmac.new(self._creds.secret_key.encode(), message.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "OK-ACCESS-KEY": self._creds.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self._creds.passphrase,
            "Content-Type": "application/json",
        }

    async def list_signals(self, f: SignalFilters, cursor: str | None = None) -> SignalPage:
        """One page of the Latest Signal List. The body is a JSON ARRAY of one filter object."""
        filters: dict[str, Any] = {
            "chainIndex": f.chain_index,
            "walletType": f.wallet_type,
            "minAddressCount": f.min_address_count,
            "minAmountUsd": f.min_amount_usd,
            "minMarketCapUsd": f.min_market_cap_usd,
            "minLiquidityUsd": f.min_liquidity_usd,
        }
        if cursor is not None:
            filters["cursor"] = cursor
        body = [filters]
        headers = self._headers("POST", SIGNAL_LIST_PATH, json.dumps(body, separators=(",", ":")))
        payload = await self._transport.request("POST", SIGNAL_LIST_PATH, params=None, json_body=body, headers=headers)

        rows = _rows(payload)
        # Row-shape guard, mirroring the candle path below: validate before the page can be
        # represented as a result. Two failures ride on this, and the second is the silent one.
        # `SignalPage.signals` is annotated tuple[dict[str, Any], ...] and mypy cannot see a breach
        # because `_rows` returns list[Any]. Worse, the cursor read below used to skip a non-dict
        # last row and leave next_cursor=None — indistinguishable from end-of-pagination, so a
        # season fetch would stop after page 1 and report a complete dataset. Absence of rows and
        # absence of a cursor stay legitimate; only a row of the wrong shape raises.
        for row in rows:
            if not isinstance(row, dict):
                raise OKXResponseError(f"signal row must be a JSON object, got {type(row).__name__}")

        next_cursor: str | None = None
        if rows:
            raw_cursor = rows[-1].get("cursor")
            if raw_cursor is not None:
                next_cursor = str(raw_cursor)
        return SignalPage(signals=tuple(rows), next_cursor=next_cursor)

    async def get_candles(
        self,
        chain_index: str,
        token: str,
        bar: str,
        *,
        before_ms: int | None = None,
        limit: int = 100,
    ) -> CandleSeries:
        """Historical candles for one token — the §7 settlement source.

        Raises ``ValueError`` for a bar this client has no width for; guessing one would silently
        skew every ``close_ts`` derived from the series. Raises ``OKXAPIError`` /
        ``OKXResponseError`` rather than returning a partial or empty series for an error
        envelope, a row that is not the frozen wire record, or a numeric column that parses to a
        non-finite value (``_finite_candle_number``).
        """
        if bar not in BAR_MS:
            raise ValueError(f"unsupported bar {bar!r}; supported bars: {sorted(BAR_MS)}")

        params: dict[str, str] = {
            "chainIndex": chain_index,
            "tokenContractAddress": token,
            "bar": bar,
            "limit": str(limit),
        }
        if before_ms is not None:
            params["before"] = str(before_ms)
        headers = self._headers("GET", f"{HISTORICAL_CANDLES_PATH}?{urlencode(params)}", "")
        payload = await self._transport.request(
            "GET", HISTORICAL_CANDLES_PATH, params=params, json_body=None, headers=headers
        )

        candles: list[Candle] = []
        for row in _rows(payload):
            # Validate the container BEFORE converting: `list("12345678")` would explode a string
            # into eight characters and yield a complete, plausible-looking candle.
            if not isinstance(row, (list, tuple)):
                raise OKXResponseError(f"candle row must be a list or tuple, got {type(row).__name__}")
            # Exact arity, never truncation. Taking the first 8 of a longer row reads some other
            # column as `confirm`, and the settlement selector keys eligibility on `confirmed` —
            # so a silent schema drift would make every trial look genuinely unsettleable.
            if len(row) != CANDLE_FIELD_COUNT:
                raise OKXResponseError(
                    f"candle row must have exactly {CANDLE_FIELD_COUNT} fields "
                    f"[ts,o,h,l,c,vol,volUsd,confirm], got {len(row)}"
                )
            ts, open_, high, low, close, vol, vol_usd, confirm = row
            # Every `float()` column goes through the finiteness guard. `ts` does not: `int()`
            # cannot produce a non-finite value (`int("1e400")` raises), and `confirm` is compared
            # as a string. `Candle`'s public shape is untouched — this changes what may be PUT in
            # the fields, never the fields themselves (C17-R2).
            candles.append(
                Candle(
                    ts_open_ms=int(ts),  # OKX `ts` is the candle OPEN time (§7).
                    open=_finite_candle_number(open_, "o", "open"),
                    high=_finite_candle_number(high, "h", "high"),
                    low=_finite_candle_number(low, "l", "low"),
                    close=_finite_candle_number(close, "c", "close"),
                    vol=_finite_candle_number(vol, "vol", "vol"),
                    vol_usd=_finite_candle_number(vol_usd, "volUsd", "vol_usd"),
                    confirmed=str(confirm) == "1",
                )
            )
        return CandleSeries(bar=bar, bar_ms=BAR_MS[bar], candles=tuple(candles))
