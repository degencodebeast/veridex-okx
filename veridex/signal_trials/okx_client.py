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

Normalization of the raw signal dicts is task H2.2 and deliberately does NOT happen here —
``SignalPage.signals`` carries the wire dicts untouched.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode

SIGNAL_LIST_PATH = "/api/v6/dex/market/signal/list"
HISTORICAL_CANDLES_PATH = "/api/v6/dex/market/historical-candles"

# Bar width in milliseconds. The (chain x bar) preference matrix (§5.1) selects one bar for the
# whole season: `1m` preferred, `1H` the fallback. Anything else is rejected rather than guessed,
# because a wrong bar width silently corrupts the §7 close-boundary law.
BAR_MS: dict[str, int] = {"1m": 60_000, "1H": 3_600_000}


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
        return (
            "OKXCredentials(api_key='***', secret_key='***', passphrase='***', "
            f"base_url={self.base_url!r})"
        )


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


def _rows(payload: dict[str, Any]) -> list[Any]:
    data = payload.get("data")
    return list(data) if isinstance(data, list) else []


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
        payload = await self._transport.request(
            "POST", SIGNAL_LIST_PATH, params=None, json_body=body, headers=headers
        )

        rows = _rows(payload)
        next_cursor: str | None = None
        if rows and isinstance(rows[-1], dict):
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
        skew every ``close_ts`` derived from the series.
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
            fields = list(row)
            if len(fields) < 8:
                raise ValueError(f"malformed candle row: expected 8 fields, got {len(fields)}")
            ts, open_, high, low, close, vol, vol_usd, confirm = fields[:8]
            candles.append(
                Candle(
                    ts_open_ms=int(ts),  # OKX `ts` is the candle OPEN time (§7).
                    open=float(open_),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    vol=float(vol),
                    vol_usd=float(vol_usd),
                    confirmed=str(confirm) == "1",
                )
            )
        return CandleSeries(bar=bar, bar_ms=BAR_MS[bar], candles=tuple(candles))
