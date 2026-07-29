import ast
import asyncio
import base64
import contextlib
import functools
import hashlib
import hmac
import inspect
import io
import json
import math
import subprocess
import sys
import time
from importlib import util as importlib_util
from pathlib import Path

import pytest

from tests.signal_trials.test_challenge_spec import REST as H22_REST
from tests.signal_trials.test_challenge_spec import WS as H22_WS
from veridex.signal_trials import okx_client
from veridex.signal_trials.challenge_spec import evidence_hash, normalize_signal
from veridex.signal_trials.okx_client import (
    BAR_MS,
    CANDLE_FIELD_COUNT,
    CandleSeries,
    OKXAPIError,
    OKXClientError,
    OKXCredentials,
    OKXMarketClient,
    OKXResponseError,
    SignalFilters,
)

# H2.5's new names (`WS_SIGNAL_CHANNEL`, `subscribe_one_signal`) are reached through the MODULE
# object above rather than added to this `from ... import` list, and that is load-bearing rather
# than stylistic. `okx_client.py` is a MODIFY, so a behavioural RED is obtainable here: importing
# the new names directly would raise ImportError at COLLECTION and take all 100+ existing tests in
# this file down with it, which proves only that a name is absent. Through the module object the
# file collects, every pre-existing test still runs and passes, and the new tests fail at RUNTIME
# naming exactly the behaviour that is missing. Same test text before and after the implementation.
# `test_challenge_spec.py` imports its module the same way for its own (monkeypatch) reason.


class RecordingFake:
    def __init__(self, payload): self.payload, self.calls = payload, []
    async def request(self, method, path, *, params, json_body, headers):
        self.calls.append((method, path, params, json_body, headers))
        return self.payload


async def test_signal_list_serializes_all_six_filters_as_strings_and_signs_the_transmitted_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The v6 Signal List declares every request parameter String, including four app-level ints."""

    class ByteRecordingTransport(RecordingFake):
        transmitted_body: bytes | None = None

        async def request(self, method, path, *, params, json_body, headers):
            self.transmitted_body = json.dumps(json_body, separators=(",", ":")).encode()
            return await super().request(
                method,
                path,
                params=params,
                json_body=json_body,
                headers=headers,
            )

    fixed_timestamp = "2026-07-29T00:00:00.000Z"
    monkeypatch.setattr(okx_client, "_timestamp", lambda: fixed_timestamp)
    filters = SignalFilters(
        chain_index="196",
        wallet_type="1",
        min_address_count=7,
        min_amount_usd=1234,
        min_market_cap_usd=567_890,
        min_liquidity_usd=23_456,
    )
    # The wire conversion belongs at the client boundary; application threshold types remain ints.
    assert isinstance(filters.min_address_count, int)
    assert isinstance(filters.min_amount_usd, int)
    assert isinstance(filters.min_market_cap_usd, int)
    assert isinstance(filters.min_liquidity_usd, int)

    transport = ByteRecordingTransport({"code": "0", "data": []})
    creds = OKXCredentials("api-key-sentinel", "secret-sentinel", "passphrase-sentinel")
    await OKXMarketClient(transport, creds).list_signals(filters)

    expected_body = (
        b'[{"chainIndex":"196","walletType":"1","minAddressCount":"7",'
        b'"minAmountUsd":"1234","minMarketCapUsd":"567890","minLiquidityUsd":"23456"}]'
    )
    assert transport.transmitted_body == expected_body

    assert len(transport.calls) == 1
    headers = transport.calls[0][4]
    prehash = fixed_timestamp.encode() + b"POST" + b"/api/v6/dex/market/signal/list" + expected_body
    expected_signature = base64.b64encode(
        hmac.new(b"secret-sentinel", prehash, hashlib.sha256).digest()
    ).decode()
    assert headers["OK-ACCESS-SIGN"] == expected_signature


async def test_signal_list_hmac_covers_the_exact_utf8_bytes_emitted_by_real_httpx_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-ASCII accepted cursor must not make signed JSON differ from real httpx request bytes."""
    import httpx

    fixed_timestamp = "2026-07-29T00:00:00.000Z"
    monkeypatch.setattr(okx_client, "_timestamp", lambda: fixed_timestamp)
    captured: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"code": "0", "data": []})

    seam_path = (
        Path(__file__).resolve().parents[2] / "scripts" / "signal_trials" / "run_preflight.py"
    )
    spec = importlib_util.spec_from_file_location("real_run_preflight_http_seam", seam_path)
    assert spec is not None and spec.loader is not None
    seam = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(seam)

    creds = OKXCredentials("api-key-sentinel", "secret-sentinel", "passphrase-sentinel")
    async with httpx.AsyncClient(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(respond),
    ) as http:
        client = OKXMarketClient(seam.HttpxTransport(http), creds)
        await client.list_signals(SignalFilters(chain_index="196"), cursor="café")

    assert len(captured) == 1
    request = captured[0]
    expected_body = (
        b'[{"chainIndex":"196","walletType":"1","minAddressCount":"2",'
        b'"minAmountUsd":"1000","minMarketCapUsd":"100000","minLiquidityUsd":"20000",'
        b'"cursor":"caf\xc3\xa9"}]'
    )
    assert request.content == expected_body
    prehash = fixed_timestamp.encode() + b"POST" + b"/api/v6/dex/market/signal/list" + request.content
    expected_signature = base64.b64encode(
        hmac.new(b"secret-sentinel", prehash, hashlib.sha256).digest()
    ).decode()
    assert request.headers["OK-ACCESS-SIGN"] == expected_signature


class _StringSubclass(str):
    pass


class _IntSubclass(int):
    pass


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        pytest.param("chain_index", "", id="chain-empty"),
        pytest.param("chain_index", "abc", id="chain-non-decimal"),
        pytest.param("chain_index", "١", id="chain-non-ascii-decimal"),
        pytest.param("chain_index", 196, id="chain-int"),
        pytest.param("chain_index", True, id="chain-bool"),
        pytest.param("chain_index", _StringSubclass("196"), id="chain-str-subclass"),
        pytest.param("wallet_type", "", id="wallet-empty"),
        pytest.param("wallet_type", "4", id="wallet-undocumented"),
        pytest.param("wallet_type", "1,", id="wallet-empty-tail"),
        pytest.param("wallet_type", ",1", id="wallet-empty-head"),
        pytest.param("wallet_type", "1,,2", id="wallet-empty-middle"),
        pytest.param("wallet_type", "1, 2", id="wallet-whitespace"),
        pytest.param("wallet_type", 1, id="wallet-int"),
        pytest.param("wallet_type", _StringSubclass("1"), id="wallet-str-subclass"),
        pytest.param("wallet_type", "SECRET-SENTINEL", id="wallet-value-not-logged"),
        *[
            pytest.param(field, value, id=f"{field}-{case}")
            for field in (
                "min_address_count",
                "min_amount_usd",
                "min_market_cap_usd",
                "min_liquidity_usd",
            )
            for case, value in (
                ("boolean", True),
                ("negative", -1),
                ("float", 1.0),
                ("nan", float("nan")),
                ("infinity", float("inf")),
                ("negative-infinity", float("-inf")),
                ("string", "1"),
                ("none", None),
                ("int-subclass", _IntSubclass(1)),
            )
        ],
        pytest.param("min_amount_usd", 10**5000, id="amount-beyond-int-string-limit"),
        pytest.param("cursor", "", id="cursor-empty"),
        pytest.param("cursor", 1, id="cursor-int"),
        pytest.param("cursor", True, id="cursor-bool"),
        pytest.param("cursor", _StringSubclass("next"), id="cursor-str-subclass"),
    ],
)
async def test_signal_list_refuses_invalid_runtime_input_before_transport(
    field: str,
    invalid_value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every invalid runtime input stops before direct signing and the real HTTP seam."""
    import httpx

    values: dict[str, object] = {
        "chain_index": "196",
        "wallet_type": "1",
        "min_address_count": 2,
        "min_amount_usd": 1000,
        "min_market_cap_usd": 100_000,
        "min_liquidity_usd": 20_000,
    }
    cursor: object | None = None
    if field == "cursor":
        cursor = invalid_value
    else:
        values[field] = invalid_value
    filters = SignalFilters(**values)  # type: ignore[arg-type]

    hmac_calls: list[tuple[str, str, str]] = []
    original_headers = OKXMarketClient._headers

    def observe_headers(
        client: OKXMarketClient,
        method: str,
        request_path: str,
        body: str,
    ) -> dict[str, str]:
        hmac_calls.append((method, request_path, body))
        return original_headers(client, method, request_path, body)

    monkeypatch.setattr(OKXMarketClient, "_headers", observe_headers)

    captured: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"code": "0", "data": []})

    seam_path = (
        Path(__file__).resolve().parents[2] / "scripts" / "signal_trials" / "run_preflight.py"
    )
    spec = importlib_util.spec_from_file_location("invalid_matrix_real_http_seam", seam_path)
    assert spec is not None and spec.loader is not None
    seam = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(seam)

    async with httpx.AsyncClient(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(respond),
    ) as http:
        with pytest.raises(ValueError) as raised:
            await OKXMarketClient(
                seam.HttpxTransport(http),
                OKXCredentials("k", "s", "p"),
            ).list_signals(
                filters,
                cursor=cursor,  # type: ignore[arg-type]
            )

    assert hmac_calls == []
    assert captured == []
    assert "SECRET-SENTINEL" not in str(raised.value)


async def test_signal_list_accepts_zero_thresholds_all_wallet_codes_and_non_ascii_cursor() -> None:
    transport = RecordingFake({"code": "0", "data": []})
    filters = SignalFilters(
        chain_index="0",
        wallet_type="3,1,2",
        min_address_count=0,
        min_amount_usd=0,
        min_market_cap_usd=0,
        min_liquidity_usd=0,
    )

    await OKXMarketClient(transport, OKXCredentials("k", "s", "p")).list_signals(
        filters,
        cursor="café",
    )

    assert transport.calls[0][3] == [
        {
            "chainIndex": "0",
            "walletType": "3,1,2",
            "minAddressCount": "0",
            "minAmountUsd": "0",
            "minMarketCapUsd": "0",
            "minLiquidityUsd": "0",
            "cursor": "café",
        }
    ]


async def test_list_signals_posts_array_body_and_parses_cursor():
    fake = RecordingFake({"code": "0", "data": [{"timestamp": "1753400000000", "price": "0.042",
        "chainIndex": "501", "amountUsd": "1500", "triggerWalletCount": "3", "walletType": "1",
        "triggerWalletAddress": "0xa,0xb,0xc", "soldRatioPercent": "0",
        "token": {"tokenAddress": "So1", "symbol": "TOK", "name": "Tok", "marketCapUsd": "2000000",
                   "holders": "900", "top10HolderPercent": "31.5"}, "cursor": "abc"}]})
    c = OKXMarketClient(fake, OKXCredentials("k", "s", "p"))
    page = await c.list_signals(SignalFilters(chain_index="501"))
    m, path, _, body, headers = fake.calls[0]
    assert m == "POST" and path == "/api/v6/dex/market/signal/list"
    assert isinstance(body, list) and body[0]["chainIndex"] == "501"
    assert "OK-ACCESS-SIGN" in headers and page.next_cursor == "abc" and len(page.signals) == 1

async def test_get_candles_returns_series_with_bar_provenance():
    fake = RecordingFake({"code": "0", "data": [["1753400000000", "1.0", "1.2", "0.9", "1.1", "5", "5.5", "1"]]})
    c = OKXMarketClient(fake, OKXCredentials("k", "s", "p"))
    series = await c.get_candles("501", "So1", "1m")
    assert isinstance(series, CandleSeries) and series.bar == "1m" and series.bar_ms == BAR_MS["1m"]
    assert series.candles[0].ts_open_ms == 1753400000000 and series.candles[0].confirmed is True


# --- MAJOR-1 (Codex milestone review): a non-success envelope must never be representable as a
# legitimate empty page/series. Frozen spec §7 reserves genuine absence for UNSCORED, so an
# auth/parameter/API failure that silently becomes an empty result is a false provenance claim.
# OKX returns application errors inside HTTP 200, so only the envelope `code` can catch these.

ERROR_ENVELOPE = {"code": "51000", "msg": "Invalid parameter", "data": []}


async def test_list_signals_raises_on_non_success_envelope():
    client = OKXMarketClient(RecordingFake(ERROR_ENVELOPE), OKXCredentials("k", "s", "p"))
    with pytest.raises(OKXAPIError) as excinfo:
        await client.list_signals(SignalFilters(chain_index="501"))
    assert excinfo.value.code == "51000"
    assert "Invalid parameter" in str(excinfo.value)


async def test_get_candles_raises_on_non_success_envelope():
    client = OKXMarketClient(RecordingFake(ERROR_ENVELOPE), OKXCredentials("k", "s", "p"))
    with pytest.raises(OKXAPIError) as excinfo:
        await client.get_candles("501", "So1", "1m")
    assert excinfo.value.code == "51000"


@pytest.mark.parametrize("payload", [{"code": "0"}, {"code": "0", "data": None}, {"msg": "no code"}])
async def test_structurally_invalid_envelope_raises(payload: dict[str, object]):
    client = OKXMarketClient(RecordingFake(payload), OKXCredentials("k", "s", "p"))
    with pytest.raises(OKXResponseError):
        await client.get_candles("501", "So1", "1m")


async def test_success_envelope_with_empty_data_stays_a_legitimate_empty_result():
    """Genuine absence must remain representable — it is the lawful input to an UNSCORED trial."""
    client = OKXMarketClient(RecordingFake({"code": "0", "data": []}), OKXCredentials("k", "s", "p"))
    assert await client.get_candles("501", "So1", "1m") == CandleSeries("1m", BAR_MS["1m"], ())
    page = await client.list_signals(SignalFilters(chain_index="501"))
    assert page.signals == () and page.next_cursor is None


# --- MAJOR-2 (Codex milestone review): the frozen wire record is exactly
# [ts,o,h,l,c,vol,volUsd,confirm]. A longer row must not be truncated and a non-sequence must not be
# exploded into characters: the settlement selector keys eligibility on `confirmed`, so a row-shape
# mismatch that survives conversion makes every trial look genuinely unsettleable.


@pytest.mark.parametrize(
    "row",
    [
        ["1", "1", "2", "0.5", "1.5", "10", "11", "12", "1"],  # Codex's nine-field reproduction
        ["1", "1", "2", "0.5", "1.5", "10", "11"],  # seven fields
        "12345678",  # len() == 8 but not a wire row — list() would explode it into characters
        {"ts": "1"},  # mapping, not a sequence
    ],
)
async def test_get_candles_rejects_rows_that_are_not_the_frozen_record(row: object):
    client = OKXMarketClient(RecordingFake({"code": "0", "data": [row]}), OKXCredentials("k", "s", "p"))
    with pytest.raises(OKXResponseError):
        await client.get_candles("501", "So1", "1m")


# --- C18 PINS (frozen-vector-constancy remediation). Every vector above holds four parameters
# CONSTANT: `bar` is "1m" throughout, `confirm` is "1" in every candle row, `cursor`/`before_ms` are
# never passed, and the candles REQUEST itself is never asserted. Constancy is invisible to coverage
# — every line runs, the value is simply never varied — so six materially wrong clients pass all
# twelve tests above: a hard-coded `bar_ms`, an ignored `cursor`, a hard-coded `confirmed`, a swapped
# settlement endpoint, a dropped `before`, and a rewritten `minAmountUsd`. Two of those are
# settlement-critical. These pins vary each held-constant parameter; they add no new behaviour.

CANDLE_CONFIRMED = ["1753400000000", "1.0", "1.2", "0.9", "1.1", "5", "5.5", "1"]
CANDLE_UNCONFIRMED = ["1753400060000", "1.1", "1.3", "1.0", "1.2", "6", "6.6", "0"]

# A SECOND (chain, token) pair. Deliberately NOT the frozen manifest values: every other vector in
# this file uses chain "501" and token "So1", which is exactly the constancy these two exist to
# break. They assert nothing about which chains the season selects — only that the arguments reach
# the request instead of being baked in.
CHAIN_B = "56"
TOKEN_B = "0xB0B"

# Credential SENTINELS for the leakage-path pins. Deliberately self-describing non-secrets, never
# realistic credential values: a test proving that secrets do not leak, which itself contains a
# realistic-looking secret, has leaked one into the repository and into every evidence file that
# quotes the run. These exist to be asserted ABSENT from rendered output, never to be exhibited.
# One distinct sentinel PER FIELD, because the defect being pinned is a TRANSPOSITION — the secret
# key being sent where the passphrase belongs — which identical or near-identical values cannot show.
SENTINEL_API_KEY = "SENTINEL-API-KEY-NOT-A-CREDENTIAL"
SENTINEL_SECRET_KEY = "SENTINEL-SECRET-KEY-NOT-A-CREDENTIAL"
SENTINEL_PASSPHRASE = "SENTINEL-PASSPHRASE-NOT-A-CREDENTIAL"


def _signal_row(cursor: str, token_address: str) -> dict[str, object]:
    """One wire signal row. Carried through untouched — normalization is H2.2, not this module."""
    return {
        "timestamp": "1753400000000",
        "price": "0.042",
        "chainIndex": "501",
        "amountUsd": "1500",
        "triggerWalletCount": "3",
        "walletType": "1",
        "triggerWalletAddress": "0xa,0xb,0xc",
        "soldRatioPercent": "0",
        "token": {
            "tokenAddress": token_address,
            "symbol": "TOK",
            "name": "Tok",
            "marketCapUsd": "2000000",
            "holders": "900",
            "top10HolderPercent": "31.5",
        },
        "cursor": cursor,
    }


async def test_get_candles_takes_bar_ms_from_the_requested_bar_and_not_from_a_constant():
    """PIN 1 — bar provenance at more than one bar.

    `1m` is the only bar any other vector requests, so a client returning a hard-coded 60_000 is
    indistinguishable from one reading `BAR_MS[bar]`. §7 computes `close_ts = ts + bar_ms`, so a
    wrong width silently skews every settlement boundary — and bar provenance is the very property
    this module is named for.
    """
    fake = RecordingFake({"code": "0", "data": [CANDLE_CONFIRMED]})
    client = OKXMarketClient(fake, OKXCredentials("k", "s", "p"))

    series = await client.get_candles("501", "So1", "1H")

    # ORDER IS LOAD-BEARING (PKT-DEC-C20 rule 1). This type check must stay FIRST: a mutant whose
    # failure mode is a `None` return would raise AttributeError on any attribute access ahead of
    # it, which classifies as a FALSE kill and proves nothing. Do not "tidy" it away or reorder it.
    assert isinstance(series, CandleSeries)
    assert series.bar == "1H"
    # Compared to the LITERAL width as well as to the table: a client that also rewrote `BAR_MS`
    # would otherwise agree with itself.
    assert series.bar_ms == 3_600_000
    assert series.bar_ms == BAR_MS["1H"]
    assert BAR_MS["1H"] != BAR_MS["1m"]
    assert len(fake.calls) == 1
    # PKT-DEC-C24: presence asserted BEFORE the subscript. A mutant that drops the key entirely is
    # still a genuine kill either way — the test's own logic reached for it and found it missing —
    # but `AssertionError: 'bar' in {...}` names the CONTRACT, where a bare KeyError names only a
    # missing key and leaves the reader to infer what was required.
    assert "bar" in fake.calls[0][2]
    assert fake.calls[0][2]["bar"] == "1H"


async def test_get_candles_reads_the_confirm_column_of_every_row():
    """PIN 2 — `confirmed` comes from the row, never from an assumption.

    `confirm` is "1" in every other vector and each series holds exactly one candle, so both
    `confirmed=True` hard-coded and a column read once for the whole series pass. The settlement
    selector keys eligibility on `confirmed`; a client that always claims confirmation would settle
    trials against candles OKX may still revise.
    """
    fake = RecordingFake({"code": "0", "data": [CANDLE_CONFIRMED, CANDLE_UNCONFIRMED]})
    client = OKXMarketClient(fake, OKXCredentials("k", "s", "p"))

    series = await client.get_candles("501", "So1", "1m")

    # ORDER IS LOAD-BEARING (PKT-DEC-C20 rule 1) — see the note in the bar-provenance pin above.
    assert isinstance(series, CandleSeries)
    assert len(series.candles) == 2
    assert [c.confirmed for c in series.candles] == [True, False]
    assert [c.ts_open_ms for c in series.candles] == [1753400000000, 1753400060000]


async def test_get_candles_pins_the_settlement_endpoint_and_its_query_params():
    """PIN 3 — the candles REQUEST, which no other vector asserts at all.

    The path is compared to the literal rather than to `HISTORICAL_CANDLES_PATH`, because a client
    that redefined the constant would agree with itself. §7 freezes this endpoint as THE settlement
    source, and `before`/`limit` decide which window is settled against.
    """
    fake = RecordingFake({"code": "0", "data": []})
    client = OKXMarketClient(fake, OKXCredentials("k", "s", "p"))

    await client.get_candles("501", "So1", "1m", before_ms=1753400000000, limit=50)

    # ORDER IS LOAD-BEARING (PKT-DEC-C20 rule 1): a mutant that skips the request entirely would
    # raise IndexError on `fake.calls[0]`, a FALSE kill. Discriminate the call count FIRST.
    assert len(fake.calls) == 1
    method, path, params, json_body, headers = fake.calls[0]
    assert method == "GET"
    assert path == "/api/v6/dex/market/historical-candles"
    assert params == {
        "chainIndex": "501",
        "tokenContractAddress": "So1",
        "bar": "1m",
        "limit": "50",
        "before": "1753400000000",
    }
    assert json_body is None
    assert "OK-ACCESS-SIGN" in headers


async def test_get_candles_omits_before_when_unset_and_sends_the_default_limit():
    """The other branch of the same pin: `before` is conditional, so both of its branches need a
    vector, and `limit` has a default that nothing else exercises."""
    fake = RecordingFake({"code": "0", "data": []})
    client = OKXMarketClient(fake, OKXCredentials("k", "s", "p"))

    await client.get_candles("501", "So1", "1m")

    assert len(fake.calls) == 1  # ORDER IS LOAD-BEARING (PKT-DEC-C20 rule 1)
    assert fake.calls[0][2] == {
        "chainIndex": "501",
        "tokenContractAddress": "So1",
        "bar": "1m",
        "limit": "100",
    }


async def test_list_signals_takes_the_cursor_from_the_last_row_and_round_trips_it():
    """PIN 4 — pagination, across a two-item page.

    Every other vector holds exactly one row and never passes `cursor`, so first-item and last-item
    selection are indistinguishable there, and a client that ignores the argument entirely is
    unobservable — it would silently re-fetch page 1 forever and truncate the season's dataset.
    """
    first, last = _signal_row("CURSOR-FIRST", "So1"), _signal_row("CURSOR-LAST", "So2")
    fake = RecordingFake({"code": "0", "data": [first, last]})
    client = OKXMarketClient(fake, OKXCredentials("k", "s", "p"))

    page = await client.list_signals(SignalFilters(chain_index="501"))

    # ORDER IS LOAD-BEARING (PKT-DEC-C20 rule 1), and this pin has three separate false-kill
    # hazards: a `None` page (AttributeError), a `None` body (TypeError on subscript), and an
    # absent second request (IndexError on `calls[1]`). Each is discriminated BEFORE the assertion
    # it could mask. `next_cursor` is asserted first because it is the targeted property.
    assert page is not None
    assert page.next_cursor == "CURSOR-LAST"
    assert page.signals == (first, last)

    assert len(fake.calls) == 1
    first_body = fake.calls[0][3]
    assert isinstance(first_body, list) and len(first_body) == 1
    assert "cursor" not in first_body[0]

    await client.list_signals(SignalFilters(chain_index="501"), cursor=page.next_cursor)

    assert len(fake.calls) == 2
    method, path, params, body, _ = fake.calls[1]
    assert method == "POST"
    assert path == "/api/v6/dex/market/signal/list"
    assert params is None
    assert isinstance(body, list) and len(body) == 1
    # Whole-body equality: the predeclared MVP filter manifest (§5.1) is otherwise unpinned, and a
    # rewritten threshold changes which signals the season is ever able to see. The BREADTH of this
    # assertion is load-bearing — it is the sole detector for a rewritten `minAmountUsd` as well as
    # for a dropped `cursor`. Narrowing it to just the cursor key would silently unpin the manifest.
    assert body == [
        {
            "chainIndex": "501",
            "walletType": "1",
            "minAddressCount": "2",
            "minAmountUsd": "1000",
            "minMarketCapUsd": "100000",
            "minLiquidityUsd": "20000",
            "cursor": "CURSOR-LAST",
        }
    ]


# --- C21 PINS. Two further constants the C18 sweep MEASURED as unpinned: the (chain, token) pair,
# held at "501"/"So1" by every vector including the C18 pins themselves, and the wire-column-to-field
# mapping, which no vector reads at all. Both are settlement-critical and neither is reachable by any
# existing guard — see the note on PKT-DEC-C17-R2-A1 in the mapping pin below.


async def test_get_candles_sends_the_requested_chain_and_token_rather_than_constants():
    """PIN 5a — chain and token provenance on the settlement request.

    Every other vector in this file requests chain "501" and token "So1", so a client that baked
    either into the query is indistinguishable from one that reads its arguments. This is the
    widest blast radius of the whole constancy class: a wrong endpoint fetches the wrong SOURCE,
    but a wrong chain or token settles every trial against a market the trial was never about.
    """
    fake = RecordingFake({"code": "0", "data": []})
    client = OKXMarketClient(fake, OKXCredentials("k", "s", "p"))

    await client.get_candles("501", "So1", "1m")
    await client.get_candles(CHAIN_B, TOKEN_B, "1m")

    assert len(fake.calls) == 2  # ORDER IS LOAD-BEARING (PKT-DEC-C20 rule 1)
    first_params, second_params = fake.calls[0][2], fake.calls[1][2]
    # PKT-DEC-C24: presence before subscript, so a dropped key fails as a named contract.
    for params in (first_params, second_params):
        assert "chainIndex" in params
        assert "tokenContractAddress" in params
    assert (first_params["chainIndex"], first_params["tokenContractAddress"]) == ("501", "So1")
    assert (second_params["chainIndex"], second_params["tokenContractAddress"]) == (CHAIN_B, TOKEN_B)
    # Stated directly rather than left implicit in the two equalities above: the request must VARY
    # with the arguments. A client hard-coding either value satisfies neither line.
    assert first_params["chainIndex"] != second_params["chainIndex"]
    assert first_params["tokenContractAddress"] != second_params["tokenContractAddress"]


async def test_list_signals_sends_the_requested_chain_rather_than_a_constant():
    """PIN 5b — the same provenance on the signal-list request, which carries its own chainIndex."""
    fake = RecordingFake({"code": "0", "data": []})
    client = OKXMarketClient(fake, OKXCredentials("k", "s", "p"))

    await client.list_signals(SignalFilters(chain_index="501"))
    await client.list_signals(SignalFilters(chain_index=CHAIN_B))

    assert len(fake.calls) == 2  # ORDER IS LOAD-BEARING (PKT-DEC-C20 rule 1)
    first_body, second_body = fake.calls[0][3], fake.calls[1][3]
    assert isinstance(first_body, list) and len(first_body) == 1
    assert isinstance(second_body, list) and len(second_body) == 1
    # PKT-DEC-C24: presence before subscript.
    assert "chainIndex" in first_body[0]
    assert "chainIndex" in second_body[0]
    assert first_body[0]["chainIndex"] == "501"
    assert second_body[0]["chainIndex"] == CHAIN_B
    assert first_body[0]["chainIndex"] != second_body[0]["chainIndex"]


async def test_get_candles_maps_every_wire_column_to_its_own_field():
    """PIN 6 — the wire-column-to-field MAPPING, which nothing else in this repo reaches.

    The frozen record is [ts, o, h, l, c, vol, volUsd, confirm] and the mapping is established by
    the tuple unpack in ``get_candles``. Transposing two columns there is SILENT: ruff and
    ``mypy --strict`` both pass (eight names, eight values, all floats), Law's H3.2 suite reads only
    ``ts_open_ms``/``confirmed`` and asserts on identity, and — measured, not assumed — the
    PKT-DEC-C17-R2-A1 field-order assertion passes too, because ``Candle`` is constructed by KEYWORD.
    That assertion protects Law's POSITIONAL construction against a dataclass reorder, which is a
    different law from this one. Nothing else covers the mapping, so it is pinned here by VALUE.

    Every numeric value below is distinct, so ANY transposition among the six is detected rather
    than only the adjacent ones.
    """
    row = ["1753400000000", "11.0", "22.0", "3.0", "14.0", "55.0", "66.0", "1"]
    fake = RecordingFake({"code": "0", "data": [row]})
    client = OKXMarketClient(fake, OKXCredentials("k", "s", "p"))

    series = await client.get_candles("501", "So1", "1m")

    assert isinstance(series, CandleSeries)  # ORDER IS LOAD-BEARING (PKT-DEC-C20 rule 1)
    assert len(series.candles) == 1
    candle = series.candles[0]
    assert candle.ts_open_ms == 1753400000000
    assert candle.open == 11.0
    assert candle.high == 22.0
    assert candle.low == 3.0
    assert candle.close == 14.0
    assert candle.vol == 55.0
    assert candle.vol_usd == 66.0
    assert candle.confirmed is True
    # Guards the FIXTURE, not the client: if a later edit made two of these values equal, the pin
    # would silently stop detecting a swap between them while every assertion above still passed.
    numeric = [candle.open, candle.high, candle.low, candle.close, candle.vol, candle.vol_usd]
    assert len(set(numeric)) == len(numeric)


# --- C23 PIN. The LEAKAGE PATH. Both behaviours below are currently CORRECT — the H2.1 SPEC review
# verified the redaction by execution — and neither was observable to the suite, which is the whole
# problem: a credential guard nothing tests is one refactor from being silently gone, and the next
# person to touch it will have a green suite telling them it is fine. Asserted by the ABSENCE of a
# sentinel from rendered or transmitted output; no credential-shaped value appears anywhere here.


async def test_auth_headers_carry_the_credential_each_field_is_meant_to_carry():
    """PIN 7a — the secret key SIGNS requests; it must never be TRANSMITTED as a header value.

    Ranked first of the two: this is not a leak into a log or a traceback that someone might later
    read. Populating OK-ACCESS-PASSPHRASE from `secret_key` puts the secret key on the wire, in a
    header, to a third party, on every single request. Each header is asserted to carry the field it
    is supposed to carry, and the secret sentinel is asserted absent from everything transmitted.
    """
    creds = OKXCredentials(SENTINEL_API_KEY, SENTINEL_SECRET_KEY, SENTINEL_PASSPHRASE)
    fake = RecordingFake({"code": "0", "data": []})
    client = OKXMarketClient(fake, creds)

    await client.get_candles("501", "So1", "1m")
    await client.list_signals(SignalFilters(chain_index="501"))

    assert len(fake.calls) == 2  # ORDER IS LOAD-BEARING (PKT-DEC-C20 rule 1)
    for _, _, _, _, headers in fake.calls:
        # PKT-DEC-C24: presence before subscript. Renaming an auth header is still a genuine kill
        # without these two lines — the pin's own logic reaches for the header and finds it absent —
        # but the failure then reads `KeyError: 'OK-ACCESS-KEY'`, which tells a maintainer that
        # something broke rather than that this header is REQUIRED. This is the case that prompted
        # C24; the classification was already right, only the legibility of the kill was poor.
        assert "OK-ACCESS-KEY" in headers
        assert "OK-ACCESS-PASSPHRASE" in headers
        assert headers["OK-ACCESS-KEY"] == SENTINEL_API_KEY
        assert headers["OK-ACCESS-PASSPHRASE"] == SENTINEL_PASSPHRASE
        # The transposition this pin exists to catch: the passphrase header carrying the secret key.
        assert headers["OK-ACCESS-PASSPHRASE"] != SENTINEL_SECRET_KEY
        # Absence, over every transmitted value — not just the header we happened to think of. The
        # signature is an HMAC *derived from* the secret and must not contain it in the clear.
        assert not any(SENTINEL_SECRET_KEY in str(value) for value in headers.values())


def test_credentials_repr_never_renders_the_secret_key_or_passphrase():
    """PIN 7b — the redacting ``__repr__`` is a security control, and controls need tests.

    ``OKXCredentials`` is a frozen dataclass declared ``repr=False`` with an explicit redacting
    ``__repr__``; the generated repr would leak ``secret_key`` and ``passphrase`` into any traceback,
    pytest assertion diff, or structured log that renders the object. Every path below reaches
    ``__repr__``: ``repr()`` directly, ``str()`` and f-string interpolation (no ``__str__``, so both
    fall back to it), and containment in a collection, whose repr calls ``repr`` on its elements —
    the leak route that is easiest to miss, because nothing in the code says "repr".
    """
    creds = OKXCredentials(SENTINEL_API_KEY, SENTINEL_SECRET_KEY, SENTINEL_PASSPHRASE)

    for rendered in (repr(creds), str(creds), f"{creds}", repr([creds]), repr({"c": creds})):
        assert SENTINEL_SECRET_KEY not in rendered
        assert SENTINEL_PASSPHRASE not in rendered
        assert SENTINEL_API_KEY not in rendered
        assert "***" in rendered


# --- MAJOR-1 PIN (QUALITY review of 9a64083; fix at 9564f2d, written by another agent and pinned
# here by one that did not write it). `list_signals` validated no row shape at all, so
# {"code":"0","data":["a","b","c"]} returned SignalPage(signals=('a','b','c')). The dangerous half
# was the cursor read: it already CONTEMPLATED a non-dict last row and chose a silent no-op, so a
# malformed final row produced next_cursor=None — indistinguishable from end-of-pagination. A season
# fetch would stop after page 1 and report a complete dataset. A handled case handled wrongly is
# harder to find than missing handling, because the guard's presence reads as coverage.


def _signal_row_without_cursor(token_address: str) -> dict[str, object]:
    """A well-formed wire row that simply has no ``cursor`` key — the last page, lawfully."""
    row = _signal_row("UNUSED", token_address)
    del row["cursor"]
    return row


@pytest.mark.parametrize(
    "rows",
    [
        ["not-a-signal-object"],  # sole row
        ["not-a-signal-object", _signal_row("C", "So1")],  # FIRST position
        [_signal_row("C", "So1"), "not-a-signal-object"],  # LAST position - the silent vector
        [_signal_row("C", "So1"), 42, _signal_row("C", "So2")],  # MIDDLE position
        [["ts", "price"]],  # a LIST row: iterable and non-empty, but not a wire object
        [None],
        [_signal_row("C", "So1"), None],  # None specifically in last position
    ],
)
async def test_list_signals_rejects_rows_that_are_not_wire_objects(rows: list[object]) -> None:
    """Every position matters, not just position 0.

    A pin that only tested the first element would pass against the exact implementation that was
    silent, because the truncation vector is the LAST row: that is the one the cursor is read from.
    """
    client = OKXMarketClient(RecordingFake({"code": "0", "data": rows}), OKXCredentials("k", "s", "p"))
    with pytest.raises(OKXResponseError):
        await client.list_signals(SignalFilters(chain_index="501"))


async def test_list_signals_raises_rather_than_reporting_a_bad_last_row_as_end_of_pagination():
    """The silent vector, pinned on its own because its failure mode is a WRONG RESULT, not an error.

    The other row positions were merely unvalidated. This one was actively mis-handled: the cursor
    read skipped a non-dict last row and left ``next_cursor=None``, which every caller is entitled to
    read as "no more pages". Truncating a season's dataset while reporting success is a false
    provenance claim of exactly the kind §7 reserves ``UNSCORED`` against.
    """
    rows = [_signal_row("CURSOR-FIRST", "So1"), "not-a-signal-object"]
    client = OKXMarketClient(RecordingFake({"code": "0", "data": rows}), OKXCredentials("k", "s", "p"))

    with pytest.raises(OKXResponseError) as excinfo:
        await client.list_signals(SignalFilters(chain_index="501"))

    # Names the signal row, so this cannot be satisfied by an unrelated failure or confused with the
    # candle-row guard, which raises the same type.
    assert "signal row" in str(excinfo.value)


async def test_list_signals_over_correction_control_absence_is_still_lawful():
    """The control. The fix must reject bad SHAPES without making legitimate ABSENCE raise.

    Two distinct absences are lawful and must stay representable: no rows at all, and rows that
    simply carry no cursor. Both mean "nothing further", which §7 needs in order to express a
    genuine end-of-data rather than a swallowed failure. A guard that raised on either would have
    broken the settlement law in the opposite direction from the defect it fixed.
    """
    empty = OKXMarketClient(RecordingFake({"code": "0", "data": []}), OKXCredentials("k", "s", "p"))
    page = await empty.list_signals(SignalFilters(chain_index="501"))
    assert page.signals == ()
    assert page.next_cursor is None

    last_row = _signal_row_without_cursor("So1")
    assert "cursor" not in last_row
    no_cursor = OKXMarketClient(RecordingFake({"code": "0", "data": [last_row]}), OKXCredentials("k", "s", "p"))
    final_page = await no_cursor.list_signals(SignalFilters(chain_index="501"))
    assert final_page.signals == (last_row,)
    assert final_page.next_cursor is None


# --- H2.6-FINITE (PKT-DEC-C28 ruling 1). A NON-FINITE parsed candle number must be REFUSED here, at
# the wire boundary, rather than entering `Candle` as a price.
#
# `float("1e400")` is not a parse error. It is `inf` — an ordinary-looking decimal that OVERFLOWS —
# and before this guard that `inf` became `Candle.close`. MEASURED at canonical 39cd310, not assumed:
# a series whose p0 close parsed as `+inf` made Law's `compute_ext` return `1`, a CONFIDENT
# "already extended" verdict manufactured from a value that should never have been parsed.
#
# The same measurement refined the claim, and the refinement is recorded rather than smoothed over:
# `-inf` and `NaN` already reach `None` downstream, because `compute_ext` spells its positivity
# check `not price > 0` precisely so `NaN` cannot slip through. `+inf` is the class that gets a
# confident answer. That does NOT narrow this guard to `+inf`: all three are off-contract for a
# candle number, and the reason the guard lands at the parse boundary is exactly that no consumer
# should have to reason about a value it should never have received. One boundary, every consumer.
#
# SCOPE, per C28, is narrow and binding: parsed candle numbers at THIS boundary only. This is NOT a
# general cross-module finiteness law, and it is NOT a positivity law — §5.3's `p0, p1 > 0` is Law's
# rule, and the NEGATIVE-value control below exists to keep that boundary from drifting into this
# module. `Candle`/`CandleSeries` public shape is untouched (C17-R2).

# The documented upstream wire record, column for column, in order, with nothing added (standing
# lesson 94). Source: `.agents/skills/okx-dex-market/references/market-cli-reference.md`, which
# documents the API's raw array as `[ts,o,h,l,c,vol,volUsd,confirm]`. Vectors are built FROM this
# tuple rather than hand-written, so a suite whose fixtures drift from the wire fails rather than
# quietly testing a schema OKX does not serve.
DOCUMENTED_CANDLE_COLUMNS = ("ts", "o", "h", "l", "c", "vol", "volUsd", "confirm")

# The six columns parsed with `float()`, mapped to the `Candle` field each must land in. `ts` is
# parsed with `int()`, which cannot yield a non-finite value (`int("1e400")` raises), and `confirm`
# is compared as a string — so neither is subject to this guard and neither is listed.
NUMERIC_COLUMN_TO_FIELD = {
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "vol": "vol",
    "volUsd": "vol_usd",
}

# The three off-contract classes, in the spellings a wire can actually carry. Both `+inf` spellings
# matter and they fail differently TO A READER: "1e400" contains no hint of infinity at all, while
# "inf"/"Infinity" are literal. A guard written against the literal spellings — a substring check,
# say — would pass the overflow straight through, and the overflow is the vector C28 measured.
NON_FINITE_WIRE_VALUES = ["1e400", "inf", "Infinity", "-1e400", "-inf", "-Infinity", "nan", "NaN"]

# Distinct finite defaults, so a vector that overrides ONE column cannot be mistaken for one that
# overrode another and a transposition stays visible.
_FINITE_CANDLE = {
    "ts": "1753400000000",
    "o": "11.0",
    "h": "22.0",
    "l": "3.0",
    "c": "14.0",
    "vol": "55.0",
    "volUsd": "66.0",
    "confirm": "1",
}


def _candle_row(**overrides: str) -> list[str]:
    """One wire candle row, assembled in DOCUMENTED column order with the named columns replaced.

    The unknown-column check is not defensive noise: a typo'd override would otherwise produce a
    fully finite row, and a rejection test handed a finite row fails as `DID NOT RAISE` — which
    reads as a defect in the guard rather than a defect in the vector.
    """
    unknown = sorted(set(overrides) - set(_FINITE_CANDLE))
    if unknown:
        raise AssertionError(f"unknown candle column(s) {unknown}; documented: {list(_FINITE_CANDLE)}")
    row = {**_FINITE_CANDLE, **overrides}
    return [row[column] for column in DOCUMENTED_CANDLE_COLUMNS]


def _candles_client(rows: list[object]) -> OKXMarketClient:
    """A client whose transport answers with ``rows`` inside a success envelope.

    Returns the CLIENT ALONE. This used to hand back ``(client, fake)`` and all eight call sites
    discarded the fake with ``, _``: a returned value that nothing consumes reads as though it might
    matter, so the next person adding a vector has to check whether it does. None of these vectors
    asserts on the REQUEST — the request surface is pinned by the C18/C21 pins above, which build
    their own fakes precisely because they need to inspect them.
    """
    return OKXMarketClient(RecordingFake({"code": "0", "data": rows}), OKXCredentials("k", "s", "p"))


@pytest.mark.parametrize("column", list(NUMERIC_COLUMN_TO_FIELD))
@pytest.mark.parametrize("raw", NON_FINITE_WIRE_VALUES)
async def test_get_candles_rejects_a_non_finite_value_in_EVERY_numeric_column(column: str, raw: str):
    """The full cross product: WHICH column x WHICH off-contract class. Both dimensions vary.

    Holding either constant is the defect class this lane has paid for repeatedly — a vector that
    always put the bad value in `close`, or always used `nan`, would leave a guard covering one
    column or one class indistinguishable from one covering all six and all three. Each vector here
    carries a FINITE value in the other five numeric columns, so the rejection is attributable to
    the column named in the test id and to no other.
    """
    client = _candles_client([_candle_row(**{column: raw})])

    # PKT-DEC-C25: a rejection pin on a settlement path carries `match=`. Bare `pytest.raises` does
    # not discriminate identity, and `OKXResponseError` is raised by the envelope guard and both
    # row-shape guards inside this very call — any of which would satisfy an unmatched raises.
    with pytest.raises(OKXResponseError, match="must be a finite number") as excinfo:
        await client.get_candles("501", "So1", "1m")

    message = str(excinfo.value)
    # The message must name the column it rejected. Six columns share one guard, and "something in
    # this row was wrong" is not a diagnostic. QUOTED, because an unquoted `vol` is a substring of
    # `volUsd` and would report agreement between the two columns most easily confused.
    assert f"'{column}'" in message
    assert f"Candle.{NUMERIC_COLUMN_TO_FIELD[column]}" in message
    # And the value it saw. Failing loudly means saying what arrived, not only that it was refused.
    assert repr(raw) in message


@pytest.mark.parametrize("position", [0, 1, 2])
async def test_get_candles_rejects_a_non_finite_value_in_ANY_ROW_position(position: int):
    """The guard runs per ROW, not once per series.

    Every other vector in this section holds a single-row series, where a guard applied only to
    `data[0]` is indistinguishable from one applied to every row. A settlement window is fetched a
    hundred rows at a time, so position 0 is the LEAST likely place for a bad row to appear — the
    same reasoning that made the last-row signal vector the silent one in the MAJOR-1 pin above.
    """
    timestamps = ("1753400000000", "1753400060000", "1753400120000")
    rows: list[object] = [_candle_row(ts=ts) for ts in timestamps]
    rows[position] = _candle_row(ts=timestamps[position], c="1e400")

    # Guards the FIXTURE: the other two rows must be finite, or this would pass for the wrong reason.
    assert len(rows) == 3
    assert sum(1 for row in rows if "1e400" in row) == 1

    client = _candles_client(rows)

    with pytest.raises(OKXResponseError, match="must be a finite number") as excinfo:
        await client.get_candles("501", "So1", "1m")

    assert "'c'" in str(excinfo.value)


async def test_the_C28_defect_vector_can_no_longer_become_a_confident_price():
    """The exact vector PKT-DEC-C28 ruling 1 measured, named so the regression stays findable (C26).

    Recorded as an executed premise rather than a prose claim: `float("1e400")` IS `inf`, so this is
    not a malformed number the parser rejects — it is a well-formed decimal that overflows silently.
    Downstream, that `inf` produced `compute_ext -> 1`: a confident "already extended" verdict, not
    an error. This module is where that stops, and it stops by REFUSING the row, not by repairing it.
    """
    assert float("1e400") == float("inf")
    assert not math.isfinite(float("1e400"))

    client = _candles_client([_candle_row(c="1e400")])

    with pytest.raises(OKXResponseError, match="must be a finite number"):
        await client.get_candles("501", "So1", "1m")


def test_the_non_finite_vectors_are_actually_non_finite_and_cover_all_THREE_classes():
    """Guards the FIXTURE, not the client — PKT-DEC-C28 ruling 3.

    Every vector above asserts a REJECTION, and a rejection assertion is satisfied by any input the
    guard happens to refuse. If a later edit made "1e400" into "1e40" the vector would become an
    ordinary finite price and those assertions would silently stop being about finiteness at all.
    PREDICATE: `math.isfinite` over each PARSED vector — not a spelling match, which is what the
    two overflow vectors exist to defeat. EXAMINED: every element of `NON_FINITE_WIRE_VALUES`,
    counted below rather than assumed.
    """
    parsed = [float(raw) for raw in NON_FINITE_WIRE_VALUES]
    assert len(parsed) == len(NON_FINITE_WIRE_VALUES) == 8
    assert [math.isfinite(value) for value in parsed] == [False] * 8

    # The three classes must EACH have a vector, because they fail differently. `+inf` and `-inf` are
    # ordered and compare equal to themselves; `NaN` is neither.
    assert sum(1 for value in parsed if math.isinf(value) and value > 0) == 3
    assert sum(1 for value in parsed if math.isinf(value) and value < 0) == 3
    assert sum(1 for value in parsed if math.isnan(value)) == 2

    # `NaN != NaN`, executed rather than asserted in prose: it is the reason a guard shaped like an
    # equality or a comparison would have missed this class entirely.
    not_a_number = float("nan")
    assert not_a_number != not_a_number

    # At least one vector per SIGN must carry no literal infinity marker, so a substring-shaped guard
    # cannot pass this suite. Named explicitly — dropping them is exactly the silent regression.
    literal_free = [raw for raw in NON_FINITE_WIRE_VALUES if "inf" not in raw.lower() and "nan" not in raw.lower()]
    assert literal_free == ["1e400", "-1e400"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0.0),  # zero — finite, and the first thing an over-eager guard rejects
        ("-1.5", -1.5),  # NEGATIVE and finite: §5.3's positivity rule is LAW's, not this boundary's
        ("1e-308", 1e-308),  # tiny, still finite
        ("1e308", 1e308),  # the largest power of ten that does NOT overflow
        ("0.000000001", 1e-9),
        ("14", 14.0),  # integral spelling, no decimal point
    ],
)
async def test_get_candles_still_accepts_EVERY_finite_value_including_negative_and_extreme(raw: str, expected: float):
    """The over-correction control. A guard that rejected legitimate values would break the
    settlement law in the opposite direction from the defect it fixed.

    The negative vector is the load-bearing one: it pins that this boundary refuses NON-FINITENESS
    and nothing else. Inventing a positivity rule here would change a settlement answer from a
    number to a refusal without authority — which is the CF-1 hazard PKT-DEC-C28 names by name, and
    which the frozen spec assigns to Law's `compute_ext`, not to this module.

    The value is placed in ALL SIX numeric columns at once, so no column is exempted from the
    control the way a single-column vector would exempt five.
    """
    client = _candles_client([_candle_row(**dict.fromkeys(NUMERIC_COLUMN_TO_FIELD, raw))])

    series = await client.get_candles("501", "So1", "1m")

    # ORDER IS LOAD-BEARING (PKT-DEC-C20 rule 1): a `None` return would raise AttributeError below.
    assert isinstance(series, CandleSeries)
    assert len(series.candles) == 1
    candle = series.candles[0]
    assert [candle.open, candle.high, candle.low, candle.close, candle.vol, candle.vol_usd] == [expected] * 6
    assert all(math.isfinite(value) for value in (candle.open, candle.high, candle.low, candle.close))


async def test_the_boundary_is_the_double_OVERFLOW_and_both_sides_of_it_are_exercised():
    """PKT-DEC-C28 ruling 3: an assertion claiming to exercise a boundary must use an input whose
    UNGUARDED value differs from the asserted one.

    `1e308` and `1e309` are one exponent apart and neither is remarkable to read. What separates
    them is measured here, not asserted: `float("1e309")` IS `inf` while `float("1e308")` is an
    ordinary finite double. Measuring it in the test means the pin cannot silently stop straddling
    the boundary if a later edit changes either literal.
    """
    assert math.isfinite(float("1e308"))
    assert math.isinf(float("1e309"))

    accepted = _candles_client([_candle_row(c="1e308")])
    series = await accepted.get_candles("501", "So1", "1m")
    assert isinstance(series, CandleSeries)
    assert series.candles[0].close == 1e308

    rejected = _candles_client([_candle_row(c="1e309")])
    with pytest.raises(OKXResponseError, match="must be a finite number"):
        await rejected.get_candles("501", "So1", "1m")


async def test_the_candle_vector_matches_the_DOCUMENTED_upstream_schema_field_for_field():
    """Standing lesson 94 — at least one vector pinned to the documented upstream schema, in order,
    with nothing added.

    Source: `.agents/skills/okx-dex-market/references/market-cli-reference.md`, which documents the
    raw array as `[ts,o,h,l,c,vol,volUsd,confirm]`. Compared as an EXACT SET as well as an ordered
    tuple: containment would not see an ADDED column, and a synthetic column held constant across
    every vector is precisely the drift this lane has already been bitten by once.
    """
    assert DOCUMENTED_CANDLE_COLUMNS == ("ts", "o", "h", "l", "c", "vol", "volUsd", "confirm")
    assert len(DOCUMENTED_CANDLE_COLUMNS) == CANDLE_FIELD_COUNT == 8
    assert len(set(DOCUMENTED_CANDLE_COLUMNS)) == CANDLE_FIELD_COUNT  # no duplicate column name
    # The vector is built from the documented tuple and carries NOTHING else.
    assert set(_FINITE_CANDLE) == set(DOCUMENTED_CANDLE_COLUMNS)
    assert tuple(_FINITE_CANDLE) == DOCUMENTED_CANDLE_COLUMNS
    # The finiteness guard covers the six numeric columns and exactly those: `ts` and `confirm` are
    # not `float()`-parsed, so exempting them is correct rather than an omission.
    assert set(DOCUMENTED_CANDLE_COLUMNS) - set(NUMERIC_COLUMN_TO_FIELD) == {"ts", "confirm"}

    row = _candle_row()
    assert len(row) == CANDLE_FIELD_COUNT
    client = _candles_client([row])

    series = await client.get_candles("501", "So1", "1m")

    assert isinstance(series, CandleSeries)  # ORDER IS LOAD-BEARING (PKT-DEC-C20 rule 1)
    assert len(series.candles) == 1
    candle = series.candles[0]
    # Every documented numeric column lands in its own field, read back through the SAME mapping the
    # rejection vectors are indexed by — so the two cannot disagree about what `volUsd` means.
    for column, field in NUMERIC_COLUMN_TO_FIELD.items():
        assert getattr(candle, field) == float(_FINITE_CANDLE[column])
    assert candle.ts_open_ms == int(_FINITE_CANDLE["ts"])
    assert candle.confirmed is True


async def test_the_refusal_NAMES_the_column_it_refused_and_names_no_other():
    """The DIAGNOSTIC is a property in its own right, and PKT-DEC-C26 is why it gets its own name.

    The rejection vectors above already assert message content, but their NAME describes
    rejection-per-column, not the naming of the column IN the refusal. Measured, not supposed: a
    mutant that dropped the column from the message, and one that made `vol` and `volUsd` label each
    other, were both killed only by tests whose names do not describe what was mutated — the exact
    shape C26 calls an unpinned property. This pins it under a name that says what it pins.

    `vol` and `volUsd` are the pair that matters: one is a substring of the other, so a refusal
    naming the wrong one still 'contains' the right word. The field form is compared PARENTHESISED
    for the same reason — `Candle.vol` is a substring of `Candle.vol_usd`, `(Candle.vol)` is not.
    """
    messages = {}
    for column in NUMERIC_COLUMN_TO_FIELD:
        client = _candles_client([_candle_row(**{column: "1e400"})])
        with pytest.raises(OKXResponseError, match="must be a finite number") as excinfo:
            await client.get_candles("501", "So1", "1m")
        messages[column] = str(excinfo.value)

    assert len(messages) == 6
    for column, field in NUMERIC_COLUMN_TO_FIELD.items():
        # Names its OWN column and its OWN field...
        assert f"candle column '{column}'" in messages[column]
        assert f"(Candle.{field})" in messages[column]
        # ...and no OTHER column's field. This is the half a substring check cannot do.
        for other_field in set(NUMERIC_COLUMN_TO_FIELD.values()) - {field}:
            assert f"(Candle.{other_field})" not in messages[column]
    # All six refusals are distinguishable from one another. Stated directly rather than left to
    # follow from the assertions above: one generic message for all six columns is the failure mode.
    assert len(set(messages.values())) == 6


def test_the_finite_candle_defaults_are_distinct_AS_PARSED_VALUES():
    """Guards the FIXTURE, not the client — the invariant `_FINITE_CANDLE`'s own comment CLAIMS.

    That comment says the defaults are distinct "so a vector that overrides ONE column cannot be
    mistaken for one that overrode another and a transposition stays visible". Nothing asserted it.
    The model was already in this file: the C21 mapping pin carries exactly this guard, and its
    comment predicts this failure — "if a later edit made two of these values equal, the pin would
    silently stop detecting a swap between them while every assertion above still passed". H2.6
    cloned that pin's six values AND its rationale comment, but not its guard. That is standing
    lesson 130 exactly: cloning copies the body and leaves the tests behind, because attention
    follows novelty.

    PARSED, NOT RAW. The fixture holds strings. A set over the STRINGS would call "11.0" and "11.00"
    distinct while `float()` collapses them to one number — so a string-level invariant would pass
    while the property it claims to protect was already violated. The claim is about VALUES, so the
    assertion parses.

    BOUNDED HONESTLY: no production defect escapes today. The rejection vectors discriminate on the
    non-finite override, not on their finite neighbours, and the C21 mapping pin independently keeps
    its own distinct values. This closes one of three detection layers, not a live hole.
    """
    numeric_defaults = {column: _FINITE_CANDLE[column] for column in NUMERIC_COLUMN_TO_FIELD}
    # EXAMINED count named beside its predicate rather than left implicit. Six NUMERIC columns: `ts`
    # is parsed by `int()` and `confirm` is compared as a string, so neither can take part in a
    # float transposition and neither is in scope for this invariant.
    #
    # This one DOES discriminate: it is `len(NUMERIC_COLUMN_TO_FIELD) == 6` in effect, so adding or
    # dropping a numeric column fires it. Measured: 7 of 40 enumerated fixture states (MINOR-Q2).
    assert len(numeric_defaults) == 6
    # TAUTOLOGY, retained for readability and LABELLED so — same category as the demonstration pair
    # at the end of this function, and labelled for the same reason. The comprehension one line
    # above is keyed BY `NUMERIC_COLUMN_TO_FIELD`, so this equality holds by construction whenever
    # the comprehension completes at all; when it does not complete the failure is a `KeyError`
    # raised before this line is ever reached. Measured over 40 mechanically enumerated fixture
    # states — every single-key deletion from each dict, all 15 pairwise value collapses, the
    # string-distinct collapse, three column additions, six consistent renames — IT FIRED ZERO
    # TIMES, while its neighbours fired 7, 22, 16 and 12 times respectively (MINOR-Q2).
    #
    # WHY LABELLED RATHER THAN MADE TO DISCRIMINATE: the property it LOOKS like it is checking —
    # that `NUMERIC_COLUMN_TO_FIELD`'s keys all exist in `_FINITE_CANDLE` — is already pinned, and
    # genuinely, by `test_the_candle_vector_matches_the_DOCUMENTED_upstream_schema_field_for_field`:
    # `set(_FINITE_CANDLE) == set(DOCUMENTED_CANDLE_COLUMNS)` together with
    # `set(DOCUMENTED_CANDLE_COLUMNS) - set(NUMERIC_COLUMN_TO_FIELD) == {"ts", "confirm"}` gives it
    # transitively, in the test whose actual job is schema pinning. Rewriting this line into a
    # discriminating one would duplicate a working guard in the wrong place to avoid writing a
    # label, which is manufacturing evidence rather than producing it.
    #
    # THE LABEL IS THE POINT, not the assertion. This function is credited for labelling its own
    # non-discriminating assertion (below), so an UNLABELLED tautology three lines earlier taught a
    # reader the opposite of the truth: that :892 was load-bearing and the demonstration pair was
    # not. A labelling convention applied unevenly is worse than none, because it makes the
    # unlabelled thing look verified.
    assert set(numeric_defaults) == set(NUMERIC_COLUMN_TO_FIELD)

    parsed = [float(raw) for raw in numeric_defaults.values()]
    assert len(parsed) == 6
    assert len(set(parsed)) == 6, f"finite candle defaults are not distinct AS VALUES: {numeric_defaults}"

    # DEMONSTRATION, not a guard on the fixture — this pair passes for ANY fixture and is labelled
    # so rather than left to look like a check. It makes the reason the assertion above parses
    # executable instead of a comment, because the next person tempted to "simplify" it into a set
    # over the raw strings reads this first.
    assert "11.0" != "11.00"
    assert float("11.0") == float("11.00")


async def test_UNPARSEABLE_wire_values_still_propagate_their_ORIGINAL_exception_type():
    """PIN OF PRE-EXISTING BEHAVIOUR. THIS IS NOT A RED — it passed the instant it was written.

    Saying so is the point. Presenting a test that never failed as captured RED is the
    vacuous-evidence class this lane has already been burned by, and a docstring is not executable,
    so the MINOR it accompanies has no RED to capture.

    `float()` raises before the finiteness guard is ever consulted, so `"abc"` and `""` surface a
    built-in `ValueError` and `None` a `TypeError`, all through the public `get_candles` path. That
    predates H2.6 and PKT-DEC-C28 does not authorize changing it: this task rejects NON-FINITE
    values, which is a different change from rejecting UNPARSEABLE ones. The pin exists so the
    boundary is a RECORDED DECISION — a later task that does convert these must do it deliberately,
    against a failing test, rather than by tidying.

    EXACT TYPE, NOT `isinstance`. `OKXResponseError` subclasses `ValueError`, so
    `pytest.raises(ValueError)` passes on the finiteness rejection too and would discriminate
    nothing at all. `type(...) is ValueError` is what separates "float() could not parse this" from
    "this module refused it", and that distinction is the entire content of this pin.
    """
    open_index = DOCUMENTED_CANDLE_COLUMNS.index("o")
    for raw, expected in (("abc", ValueError), ("", ValueError), (None, TypeError)):
        row: list[object] = list(_candle_row())
        row[open_index] = raw
        client = _candles_client([row])

        with pytest.raises(expected) as excinfo:
            await client.get_candles("501", "So1", "1m")

        assert type(excinfo.value) is expected
        assert not isinstance(excinfo.value, OKXResponseError)

    # ACCEPTANCE CONTROL (standing lesson 212). A suite that only asserts refusal passes identically
    # when the subject refuses EVERYTHING — including when the harness is broken and refusing the
    # finite baseline too. Same column, same helper, one parseable value: it must go through.
    control = _candles_client([_candle_row(o="11.0")])
    series = await control.get_candles("501", "So1", "1m")
    assert isinstance(series, CandleSeries)
    assert series.candles[0].open == 11.0


# ======================================================================================
# MINOR-Q1 (carried since H2.6 with the trigger "the next Data commit that opens
# okx_client.py" — H2.5 IS that commit). `_finite_candle_number`'s docstring TELLS callers
# that `OKXResponseError` subclasses `ValueError` and instructs test authors to assert the
# EXACT type because of it. NOTHING PINNED THAT BASE: a reviewer rebuilt the class without
# `ValueError` and 95 tests passed — the mutant SURVIVED. The measured `issubclass`-assertion
# count naming `OKXResponseError` across all of `tests/` was 0.
#
# Precedent for the idiom, already in this package: `test_controls.py:183`,
# `assert issubclass(FullPackClimatologyError, ValueError)`, under the rationale that an
# exception's place in the hierarchy is part of its PUBLIC CONTRACT. Same rationale here, and
# a stronger one: a documented instruction to callers that no test enforces is a promise the
# code is free to break silently.
# ======================================================================================


def test_okx_response_error_is_a_value_error_because_the_module_promises_callers_it_is():
    """The ACCEPTANCE half: the documented base is really there.

    `_finite_candle_number`'s `Raises:` section states "``OKXResponseError`` is itself a
    ``ValueError`` subclass, so a caller catching ``ValueError`` catches both". Callers written
    against that sentence — including `test_UNPARSEABLE_wire_values_still_propagate_their_ORIGINAL_
    exception_type` above, whose whole content is the `type(...) is ValueError` / `isinstance`
    distinction — are silently wrong the moment the base is dropped.
    """
    assert issubclass(OKXResponseError, ValueError)
    assert issubclass(OKXResponseError, OKXClientError)


def test_the_value_error_base_DISCRIMINATES_and_is_not_just_true_of_every_error_here():
    """The DISCRIMINATION half: `ValueError` is not simply painted onto the whole hierarchy.

    Without this, the assertion above would pass just as happily against a module where every
    exception descended from `ValueError` — in which case `issubclass(OKXResponseError, ValueError)`
    would be measuring nothing about `OKXResponseError` in particular. `OKXAPIError` is the
    separation: an OKX-side non-success envelope is a REMOTE verdict, not a malformed local value,
    so it deliberately does NOT answer to a caller's `except ValueError`.
    """
    assert not issubclass(OKXAPIError, ValueError)
    assert issubclass(OKXAPIError, OKXClientError)
    assert not issubclass(OKXClientError, ValueError)


# ======================================================================================
# H2.5 — the one-shot WS exhibition path.
#
# THIS IS A WEBSOCKET TASK AND NOTHING BELOW OPENS A REAL CONNECTION. Every WS conversation
# here is `RecordingWS`, an in-memory script of text frames. `socket` is never patched
# (patching `socket.socket` wholesale breaks `ssl`'s subclassing at import), because no test
# here goes anywhere near a socket to begin with.
# ======================================================================================

#: The channel name spelled out INDEPENDENTLY of the module's own `WS_SIGNAL_CHANNEL`, on
#: purpose and for the same reason `test_no_season_branch.py` keeps its own bar-width table: a
#: fake that imported the constant could not disagree with it, and disagreeing is exactly what
#: `test_subscribe_one_signal_refuses_a_push_frame_from_a_DIFFERENT_channel` needs it to do.
WS_CHANNEL_ON_THE_WIRE = "dex-market-new-signal-openapi"

#: A chain index that is NOT the client's default anything, so a request that echoed a constant
#: instead of the argument would be visible (the C21 pin's lesson, applied to the WS op).
WS_CHAIN_INDEX = "501"

#: Outer bound the timeout tests impose on THEMSELVES, so an unbounded subject fails fast instead
#: of hanging the run. Comfortably longer than the transport bounds under test, so it only fires
#: when the subject imposed nothing.
OUTER_SAFETY_NET_S = 3.0


class RecordingWS:
    """A scripted, in-memory WS conversation. Records what was sent and how often it was closed.

    `recv` raises rather than blocking or returning a sentinel when the script runs out: a
    one-shot subscriber that asked for a frame the fake never promised is a defect in the
    subscriber, and a test that hung instead of failing would report it as a timeout.
    """

    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.closed = 0
        self.recv_calls = 0

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):
        self.recv_calls += 1
        if not self.frames:
            raise AssertionError(
                "the subscriber asked for more frames than the fake was scripted with; "
                "a one-shot subscribe must stop at the first signal push"
            )
        return self.frames.pop(0)

    async def close(self):
        self.closed += 1


def _ws_arg(channel=WS_CHANNEL_ON_THE_WIRE, chain_index=WS_CHAIN_INDEX):
    return {"channel": channel, "chainIndex": chain_index}


def _ws_ack(chain_index=WS_CHAIN_INDEX):
    """The subscribe acknowledgement OKX sends before any push — lawfully skipped, never returned."""
    return json.dumps({"event": "subscribe", "arg": _ws_arg(chain_index=chain_index), "connId": "conn-1"})


def _ws_push(signal, channel=WS_CHANNEL_ON_THE_WIRE, chain_index=WS_CHAIN_INDEX):
    """One data push frame carrying one signal on `channel`."""
    return json.dumps({"arg": _ws_arg(channel=channel, chain_index=chain_index), "data": [signal]})


def _ws_client():
    """A client whose REST transport is a tripwire: the WS path must not touch it.

    The payload is deliberately an error envelope, so any accidental REST call raises loudly
    rather than returning something the WS assertions could absorb.
    """
    return OKXMarketClient(RecordingFake({"code": "50000", "msg": "the WS path must not call REST"}),
                           OKXCredentials("k", "s", "p"))


# --- The fixture is part of the predicate --------------------------------------------------
# H2.2's REST/WS twin pair is REUSED rather than retyped, so the two cannot drift apart. But a
# reused fixture still has to be CHECKED for the property the reuse depends on, because a pair
# that turned out to be byte-identical dicts would make the hash-equality test below tautological.


def test_the_reused_H22_twin_fixtures_really_are_a_TWIN_PAIR_and_not_the_same_dict():
    """The REST and WS fixtures must differ in SPELLING while describing the SAME event.

    If they were equal dicts, `test_a_ws_arrival_hashes_IDENTICALLY_to_its_rest_twin` would be
    asserting `h(x) == h(x)` — true for every hash function including a constant one — and would
    bind nothing at all. If they described different events, the equality it asserts would be
    false for a CORRECT implementation. Both halves are checked here, at the fixture, so the
    equality test downstream is known to be a real question before it is answered.
    """
    assert H22_REST != H22_WS, "the twins must differ, or the hash-equality test is tautological"

    # THE LOAD-BEARING DIFFERENCE IS THE NESTED ONE, and it is asserted first because it is the
    # only one that makes the hash-equality question non-trivial.
    # `token.top10HolderPercent` / `top10HolderPercentage` is the alias `normalize_signal` must
    # actually reconcile to produce equal hashes — `_pick` reads exactly this pair.
    assert set(H22_REST["token"]) != set(H22_WS["token"]), (
        "the twins must differ in the NESTED alias spelling; that is the one normalize_signal reads"
    )

    # The top-level key difference is `soldRatioPercent` / `soldRatioPercentage` — which
    # `normalize_signal` documents as NEVER READ. It is asserted, but it is NOT what makes the
    # hash-equality test a real question, and the previous message on this line claimed it was
    # ("the thing normalized"). It is the opposite: the one field normalization ignores. Deleting
    # the nested difference and leaving only this one left the twin guard AND the hash-equality
    # test both passing with the load-bearing alias gone, so this assertion alone would have
    # certified a fixture that binds nothing.
    assert set(H22_REST) != set(H22_WS), "the twins also differ at top level, via the DROPPED sold-ratio alias"

    # ...and the difference must be confined to the alias spellings, not the observed values.
    shared = set(H22_REST) & set(H22_WS) - {"token"}
    assert shared, "the twins share no top-level fields; they cannot be describing one event"
    for key in shared:
        assert H22_REST[key] == H22_WS[key], f"twin fixtures disagree on the VALUE of {key!r}"

    # The nested values must agree too, for the same reason the top-level ones must: the twins
    # describe ONE event. `token` was excluded from the loop above (its key sets differ by design),
    # so without this the nested payload was neither asserted equal nor asserted different.
    shared_token = set(H22_REST["token"]) & set(H22_WS["token"])
    assert shared_token, "the twins' token objects share no fields; they cannot be one event"
    for key in shared_token:
        assert H22_REST["token"][key] == H22_WS["token"][key], f"twins disagree on token.{key}"


# --- subscribe_one_signal: the one-shot subscription protocol ------------------------------


async def test_subscribe_one_signal_returns_the_pushed_signal_and_CLOSES_the_transport():
    """The H2.5 Step-1 contract: one push frame in, the parsed signal dict out, transport closed.

    The close is asserted as hard as the return value. A one-shot exhibition that returned its
    signal but left the socket open would look completely correct in every downstream assertion
    while leaking a connection per demo run.
    """
    ws = RecordingWS([_ws_ack(), _ws_push(H22_WS)])

    signal = await okx_client.subscribe_one_signal(ws, WS_CHAIN_INDEX)

    assert signal == H22_WS
    assert ws.closed == 1
    assert ws.frames == [], "the subscriber must stop at the FIRST push, not drain the socket"


async def test_subscribe_one_signal_sends_the_REQUESTED_channel_and_chain_not_a_constant():
    """The op frame must carry the caller's chain index and the signal channel.

    The C21 pin's lesson: a request built from a hard-coded constant passes every test that only
    inspects the RESPONSE. Here it would silently subscribe the wrong chain and exhibit a signal
    from a market nobody asked about.
    """
    ws = RecordingWS([_ws_push(H22_WS, chain_index="196")])

    await okx_client.subscribe_one_signal(ws, "196")

    assert len(ws.sent) == 1
    op = json.loads(ws.sent[0])
    assert op["op"] == "subscribe"
    assert op["args"] == [{"channel": WS_CHANNEL_ON_THE_WIRE, "chainIndex": "196"}]
    # The module's own constant must be the one that reached the wire, not a coincidence.
    assert okx_client.WS_SIGNAL_CHANNEL == WS_CHANNEL_ON_THE_WIRE


async def test_subscribe_one_signal_raises_the_OKX_ERROR_EVENT_rather_than_waiting_forever():
    """An `event: error` frame is a remote verdict — `OKXAPIError`, carrying its code.

    Not `OKXResponseError`: the frame is perfectly well-formed, OKX is simply refusing. This is
    the same envelope/shape split the REST path draws, and the discrimination test above is what
    makes the two distinguishable to a caller.
    """
    ws = RecordingWS([json.dumps({"event": "error", "code": "60012", "msg": "Invalid request"})])

    with pytest.raises(OKXAPIError) as excinfo:
        await okx_client.subscribe_one_signal(ws, WS_CHAIN_INDEX)

    assert excinfo.value.code == "60012"
    assert ws.closed == 1, "the transport must be closed on the failure path too, not only on success"


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param('{"code":"0","data":[{"timestamp":"1"}]}', id="rest-envelope"),
        pytest.param('{"arg":{"chainIndex":"501"},"data":[{"timestamp":"1"}]}', id="push-without-channel"),
        pytest.param('{"arg":{"channel":"dex-market-new-signal-openapi"}}', id="push-without-data"),
        pytest.param('{"arg":{"channel":"dex-market-new-signal-openapi"},"data":[]}', id="push-with-empty-data"),
        pytest.param('{"arg":{"channel":"dex-market-new-signal-openapi"},"data":["not-an-object"]}', id="row-not-object"),
        pytest.param("[]", id="frame-not-an-object"),
        pytest.param("{{not json", id="frame-not-json"),
    ],
)
async def test_subscribe_one_signal_refuses_anything_that_is_not_a_signal_PUSH_frame(frame):
    """Fail closed on the WS wire exactly as `_rows` does on the REST wire.

    `rest-envelope` is the trust-critical member and the reason this list exists at all: a REST
    response replayed onto the WS path must NOT be accepted, because everything that comes out of
    this method is about to be labelled a WS arrival. See the truth-rule tests below.
    """
    ws = RecordingWS([frame])

    with pytest.raises(OKXResponseError):
        await okx_client.subscribe_one_signal(ws, WS_CHAIN_INDEX)

    assert ws.closed == 1


async def test_a_NON_LIST_data_is_refused_AS_A_DATA_PROBLEM_not_as_a_bad_row():
    """`data` must be a LIST, and that clause is pinned independently of the non-empty one.

    FOUND BY THE CORRECTED LEDGER, not by reading. `_ws_signal`'s guard is a conjunction —
    `not isinstance(data, list) or not data` — and the old per-line ledger called the whole line
    BOUND because one mutant on it was killed. Only the `or not data` half was measured. A mutant
    dropping the `isinstance` half SURVIVED the entire suite.

    It survived because the frame is still refused, just for the wrong reason and one step later:
    a string `data` is truthy, so it passes the mutated guard, `data[0]` takes its first CHARACTER,
    and the row-shape check rejects that. Every existing test asserted only `pytest.raises(
    OKXResponseError)`, which cannot tell "this push had no data list" from "this row was not an
    object". Asserting WHICH refusal fired is what separates them — a diagnostic that names the
    wrong cause sends an operator to the wrong wire contract.
    """
    ws = RecordingWS([json.dumps({"arg": _ws_arg(), "data": "not-a-list"})])

    with pytest.raises(OKXResponseError) as excinfo:
        await okx_client.subscribe_one_signal(ws, WS_CHAIN_INDEX)

    message = str(excinfo.value)
    assert "'data'" in message and "list" in message, f"refused for the wrong reason: {message}"
    assert "JSON object" not in message, "this is a data-list failure, not a row-shape failure"
    assert ws.closed == 1

    # DISCRIMINATION: the row-shape refusal still says its own thing, so the two diagnostics are
    # genuinely distinguishable rather than both matching whatever this test asserts.
    row_ws = RecordingWS([json.dumps({"arg": _ws_arg(), "data": ["not-an-object"]})])
    with pytest.raises(OKXResponseError) as row_error:
        await okx_client.subscribe_one_signal(row_ws, WS_CHAIN_INDEX)
    assert "JSON object" in str(row_error.value)


async def test_subscribe_one_signal_refuses_a_push_frame_from_a_DIFFERENT_channel():
    """A well-formed push on some other channel is not a signal, whatever its `data` looks like.

    Separated from the parametrized refusals above because this one is not a SHAPE failure — the
    frame is a valid push and would deserialize perfectly. Only the channel identity rejects it.
    """
    ws = RecordingWS([_ws_push(H22_WS, channel="dex-market-price-openapi")])

    with pytest.raises(OKXResponseError) as excinfo:
        await okx_client.subscribe_one_signal(ws, WS_CHAIN_INDEX)

    assert "dex-market-price-openapi" in str(excinfo.value)
    assert ws.closed == 1


async def test_subscribe_one_signal_returns_the_FIRST_signal_of_a_multi_signal_push():
    """A push may batch several signals; the one-shot path takes the FIRST of them.

    Added because the mutation drill for this commit found the rule unbound: every other fixture
    here carries exactly one signal in `data`, so `data[0]` and `data[-1]` were the same object and
    a mutant swapping them SURVIVED the whole suite. Which signal is exhibited decides which market
    event the demo is about, so "some signal from the frame" is not the contract.
    """
    first = dict(H22_WS, price="0.011")
    second = dict(H22_WS, price="0.022")
    frame = json.dumps({"arg": _ws_arg(), "data": [first, second]})

    signal = await okx_client.subscribe_one_signal(RecordingWS([frame]), WS_CHAIN_INDEX)

    assert signal == first
    assert signal != second, "the fixtures must differ, or this test cannot tell first from last"


async def test_subscribe_one_signal_refuses_an_UNRECOGNIZED_control_event():
    """An event this module has no rule for is refused, not silently skipped.

    Also added off the back of the drill: with no test feeding an unknown event, a mutant deleting
    this refusal SURVIVED. Skipping the unknown is the dangerous default — a one-shot subscriber
    would sit on a socket that is actively telling it something (an auth failure, a channel
    deprecation) and report it as "no signal has arrived yet".
    """
    ws = RecordingWS([json.dumps({"event": "login", "code": "0"}), _ws_push(H22_WS)])

    with pytest.raises(OKXResponseError) as excinfo:
        await okx_client.subscribe_one_signal(ws, WS_CHAIN_INDEX)

    assert "login" in str(excinfo.value)
    assert ws.closed == 1
    # ...and the refusal is specifically about the UNKNOWN event, not about control frames in
    # general: `subscribe` sits in the same position and is skipped, as the next test shows.
    assert "subscribe" in okx_client._WS_SKIPPABLE_EVENTS


async def test_subscribe_one_signal_skips_heartbeats_and_acks_until_the_FIRST_push():
    """`pong` and the subscribe ack are control traffic and are skipped; the FIRST push wins.

    The acceptance control for the refusal suite above: a method that refused every frame would
    pass all of those tests. This one proves the skip path can actually reach a signal.

    RENAMED, AND THE OLD NAME WAS THE DEFECT. This was
    `..._skips_heartbeats_and_acks_but_NOT_INDEFINITELY`, which asserted a bound nothing here
    measures. `_ws_converse` is a `while True` with no frame budget; the run below terminates
    because `RecordingWS.recv` raises when its four-frame SCRIPT runs out — a property of the
    FAKE, not of the subject. Measured against a transport that returns `"pong"` forever, the
    subscriber reached 50,001 `recv` calls without giving up.

    No budget is added, and that is a deliberate boundary rather than an omission: the receive
    timeout belongs to the connection, which is the only layer that knows what a reasonable wait
    is. `WSTransport`'s docstring now states that a real implementation MUST set one. What is
    fixed here is the NAME — a test asserting a bound nobody measured is the same class of defect
    as the tautology labelled at :892, and this file cannot teach that standard while breaking it.
    """
    ws = RecordingWS(["pong", _ws_ack(), "pong", _ws_push(H22_WS)])

    signal = await okx_client.subscribe_one_signal(ws, WS_CHAIN_INDEX)

    assert signal == H22_WS
    assert ws.recv_calls == 4


async def test_the_client_METHOD_and_the_module_function_are_the_same_subscription():
    """The plan freezes `OKXMarketClient.subscribe_one_signal`; H2.5 also exposes it credential-free.

    The method is retained exactly as the plan specifies. The module-level function exists so the
    exhibition script can subscribe WITHOUT constructing credentials it has no use for — see
    `test_the_exhibition_script_reads_NO_credential_from_the_environment`. This test pins that the
    two are one behaviour rather than two implementations that can drift.
    """
    via_method = await _ws_client().subscribe_one_signal(RecordingWS([_ws_push(H22_WS)]), WS_CHAIN_INDEX)
    via_function = await okx_client.subscribe_one_signal(RecordingWS([_ws_push(H22_WS)]), WS_CHAIN_INDEX)
    assert via_method == via_function == H22_WS


# --- THE HEART OF H2.5: a WS arrival and its REST twin are ONE piece of evidence -------------


async def test_a_ws_arrival_hashes_IDENTICALLY_to_its_rest_twin():
    """A signal that arrived over WS must normalize to the SAME `evidence_hash` as over REST.

    This is what makes the two paths interchangeable as evidence: a receipt sealed against a
    WS-observed signal and one sealed against the same event observed over REST name the same
    thing. If these ever diverged, the live-exhibition path and the replay path would silently
    describe two different markets while claiming to describe one.

    The fixture guard above establishes that this is a real question (the twins differ in
    spelling); the discrimination control below establishes that the hash can still say NO.
    """
    arrived = await okx_client.subscribe_one_signal(RecordingWS([_ws_ack(), _ws_push(H22_WS)]), WS_CHAIN_INDEX)

    assert evidence_hash(normalize_signal(arrived, "ws")) == evidence_hash(normalize_signal(H22_REST, "rest"))


async def test_a_DIFFERENT_ws_signal_hashes_DIFFERENTLY_from_the_rest_twin():
    """DISCRIMINATION control (C52). Equality proves nothing unless inequality is also reachable.

    Without this, the test above passes identically for an `evidence_hash` that ignores its input
    and returns a constant — a hash function under which every signal in the season is the same
    piece of evidence. Each perturbed field is a field the hash is REQUIRED to be sensitive to, so
    each is its own assertion rather than one combined dict.
    """
    rest_hash = evidence_hash(normalize_signal(H22_REST, "rest"))

    for field, value in (("price", "0.043"), ("timestamp", "1753400000001"), ("amountUsd", "1600")):
        perturbed = dict(H22_WS)
        perturbed[field] = value
        arrived = await okx_client.subscribe_one_signal(RecordingWS([_ws_push(perturbed)]), WS_CHAIN_INDEX)
        assert evidence_hash(normalize_signal(arrived, "ws")) != rest_hash, f"the hash ignored {field!r}"

    # ...and a nested field, since `token` is the sub-object the alias normalization rewrites.
    perturbed = dict(H22_WS)
    perturbed["token"] = dict(H22_WS["token"], tokenAddress="So2")
    arrived = await okx_client.subscribe_one_signal(RecordingWS([_ws_push(perturbed)]), WS_CHAIN_INDEX)
    assert evidence_hash(normalize_signal(arrived, "ws")) != rest_hash, "the hash ignored token.tokenAddress"


# ======================================================================================
# `scripts/signal_trials/ws_exhibition.py` — a CREATE.
#
# Loaded BY PATH (the idiom `test_no_season_branch.py` and `test_preflight.py` already use for
# operator scripts), which keeps this file's collection intact: the okx_client half above keeps
# its behavioural RED while these tests fail on the script's ABSENCE.
# ======================================================================================


@functools.lru_cache(maxsize=1)
def _ws_exhibition_module():
    """Load the operator script by path, once, registering it in `sys.modules` before execution.

    Registered first because `@dataclass` resolves its annotations through
    `sys.modules[cls.__module__]`, which is `None` for a module that was never registered.
    """
    script = Path(__file__).resolve().parents[2] / "scripts" / "signal_trials" / "ws_exhibition.py"
    assert script.exists(), f"operator script missing at {script}"
    name = "ws_exhibition_under_test"
    spec = importlib_util.spec_from_file_location(name, script)
    assert spec is not None and spec.loader is not None
    module = importlib_util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RecordingHandoff:
    """Stands in for spawning `open_live_trial.py`. Records the argv and the stdin it was given."""

    def __init__(self, status=0):
        self.status = status
        self.calls = []

    def __call__(self, argv, stdin_text):
        self.calls.append((list(argv), stdin_text))
        return self.status


async def _exhibit(frames, handoff=None, **kwargs):
    """Run one exhibition over a scripted WS conversation, returning (summary, handoff, ws)."""
    module = _ws_exhibition_module()
    ws = RecordingWS(frames)
    handoff = RecordingHandoff() if handoff is None else handoff
    summary = await module.exhibit_one_signal(
        ws, WS_CHAIN_INDEX, data_dir="/tmp/does-not-need-to-exist", handoff=handoff, **kwargs
    )
    return summary, handoff, ws


async def test_the_exhibition_composes_subscribe_then_normalize_then_handoff():
    """H2.5 Step 3: subscribe -> `normalize_signal(raw, "ws")` -> the payments-lane open-trial script.

    The handoff is spawned with the RAW signal on stdin, not the canonical form: `open_live_trial.py`
    normalizes it itself, and handing it a pre-normalized payload would move the leakage boundary
    into this script where no reviewer of that script would look for it.
    """
    summary, handoff, ws = await _exhibit([_ws_ack(), _ws_push(H22_WS)])

    assert ws.closed == 1
    assert len(handoff.calls) == 1
    argv, stdin_text = handoff.calls[0]
    assert json.loads(stdin_text) == H22_WS
    assert argv[0].endswith("open_live_trial.py")
    assert summary.evidence_hash == evidence_hash(normalize_signal(H22_REST, "rest"))


async def test_the_handoff_labels_the_signal_ws_and_there_is_no_argv_that_says_rest():
    """THE TRUTH RULE, at the one place a mislabel could be introduced.

    The plan: "if live WS is unreliable at demo time, the demo says REST-sourced live trial — REST
    IS NEVER LABELED A WS ARRIVAL." The converse obligation is this one: a signal that DID arrive
    over WS is handed off as `ws`, by a constant, on a code path only a consumed WS push frame can
    reach. `"rest"` is asserted absent from the whole argv rather than only from the `--source`
    value, because the label is wrong wherever it appears.
    """
    _, handoff, _ = await _exhibit([_ws_push(H22_WS)])

    argv, _stdin = handoff.calls[0]
    assert "--source" in argv
    assert argv[argv.index("--source") + 1] == "ws"
    assert "rest" not in argv


async def test_the_exhibition_offers_NO_WAY_to_choose_the_source():
    """The label is a recorded fact, not an operator's assertion — so nothing may accept it as input.

    A `--source` flag, or a `source=` parameter, would be exactly the affordance that lets a
    REST-sourced signal be published as a WS arrival at demo time under deadline pressure. Both
    surfaces are checked: the CLI an operator types, and the function another module could call.
    """
    module = _ws_exhibition_module()

    options = {opt for action in module.build_parser()._actions for opt in action.option_strings}
    assert "--source" not in options
    assert not any("source" in opt for opt in options)

    for name in ("exhibit_one_signal", "build_handoff_argv"):
        parameters = inspect.signature(getattr(module, name)).parameters
        assert "source" not in parameters, f"{name} accepts a caller-supplied source"


async def test_the_summary_RECORDS_the_source_and_the_record_cannot_be_reassigned():
    """The rendered summary states where the signal came from, and that statement is not settable.

    `source` is derived, not stored: there is no constructor argument and no assignable attribute
    through which a caller could write `"rest"` into a record produced by the WS path.
    """
    summary, _, _ = await _exhibit([_ws_push(H22_WS)])

    assert summary.source == "ws"
    assert summary.render()["source"] == "ws"
    with pytest.raises((AttributeError, TypeError)):
        summary.source = "rest"


async def test_the_rendered_summary_publishes_NO_evidence_VALUE_without_exception():
    """The printed summary names the evidence fields and hashes them; it publishes NONE of them.

    REPLACES a hand-listed check, and the hand-list was the defect. The previous version asserted
    four specific values absent and one — `t0_ms` — PRESENT, as a declared exception justified by
    three claims none of which had been measured: that it was "trial metadata rather than market
    data", that it was the trial-open instant, and that `open_live_trial.py` printed it for the
    same reason. All three are false. `visible_at_decision` returns the whole model dump, so
    `t0_ms` IS hashed evidence; `live.py` sets the trial's `t0_ms` to `now_ms` and says explicitly
    it is "not from `sig.t0_ms`"; and the two renderings measured ~31.7 billion ms apart in one
    invocation. The rendered dict was listing `"t0_ms"` in `evidence_fields` — naming it evidence —
    while printing its value under a docstring saying evidence values are absent.

    So this walks EVERY field of the signal instead of a curated list. A hand-kept exception list
    is what produced both the original leak and its mislabelled remediation; a general rule cannot
    grow a quiet exception, and a new evidence field is covered the day it is added rather than the
    day someone remembers to extend a literal.
    """
    summary, handoff, _ = await _exhibit([_ws_push(H22_WS)])
    rendered = summary.render()
    evidence = normalize_signal(H22_WS, "ws").model_dump()

    # The KEY SET is pinned exactly, so a future field cannot appear in the rendering without
    # failing here first — including one that would re-introduce an evidence value.
    assert set(rendered) == {
        "source", "evidence_hash", "evidence_fields", "handoff_status", "dry_run", "published",
        # Added deliberately, and this pin is what forced the decision into the open: a nonzero
        # child status does NOT mean nothing was written, so the rendering carries a third state.
        # It is a bool and cannot collide with an evidence value, so it needs no `_scan` exclusion.
        "publication_indeterminate",
    }
    assert rendered["evidence_hash"] == summary.evidence_hash
    assert "trigger_price" in rendered["evidence_fields"], "the field NAMES are the useful part"
    assert set(rendered["evidence_fields"]) == set(evidence), "the names must be the whole signal"

    # TWO KEYS ARE EXCLUDED FROM THE SUBSTRING SCAN, and each is excluded because it carries its
    # OWN assertion above — not because excluding it makes the test pass:
    #   `evidence_hash`   — a 64-char hex digest. A short decimal evidence value can appear inside
    #                       it BY CHANCE, so scanning it makes the test flake on a coincidence
    #                       rather than on a leak. It is asserted equal to `summary.evidence_hash`
    #                       above, and a digest is not a publication of its input — that is exactly
    #                       why it is the thing printed.
    #   `evidence_fields` — the field NAMES, which are legitimately printed and are asserted to be
    #                       EXACTLY the signal's key set above, so they cannot smuggle a value.
    # This exclusion is load-bearing rather than cosmetic: a first draft scanned the whole
    # rendering and failed on `wallet_type="1"`, because the single character "1" occurs inside the
    # NAME `top10_holder_percent`. That was the scan matching a name, not a leak.
    def _scan(payload):
        return json.dumps(
            {k: v for k, v in payload.items() if k not in ("evidence_hash", "evidence_fields")},
            sort_keys=True,
        )

    scanned = _scan(rendered)

    # POPULATION NAMED BESIDE THE PREDICATE: every field of CanonicalSignal, not a chosen subset.
    assert len(evidence) == 13, f"expected the full canonical signal, got {sorted(evidence)}"
    for field, value in evidence.items():
        assert str(value) not in scanned, f"the rendering published evidence value {field}={value!r}"

    # ACCEPTANCE CONTROL: the scan CAN fire, through the SAME `_scan` the assertions above use.
    # Without it, "no value found" is equally consistent with a scan pointed at the wrong string.
    # Every field is poisoned in turn, so the control covers the whole population rather than one
    # convenient member — including `wallet_type`, the one a careless scan gets wrong.
    for field, value in evidence.items():
        assert str(value) in _scan({**rendered, "leaked": value}), f"the scan cannot detect a leaked {field}"

    # The handoff still received the raw signal — the values are not being withheld from the
    # PIPELINE, only from the terminal. Absent this, dropping the payload entirely would pass.
    assert json.loads(handoff.calls[0][1]) == H22_WS


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("InvalidURI", id="bad-ws-url"),
        pytest.param("InvalidHandshake", id="handshake-rejected"),
        pytest.param("ConnectionClosed", id="peer-closed"),
        pytest.param("ModuleNotFoundError", id="websockets-not-installed"),
    ],
)
def test_main_REFUSES_cleanly_when_the_connection_fails(monkeypatch, capsys, failure):
    """`main` promises "refusals go to stderr and exit non-zero". It must keep that for real failures.

    NO SOCKET IS OPENED. A fake `websockets` module is injected into `sys.modules` whose `connect`
    raises before any I/O, so this exercises the connect-failure path without a network.

    This closes a gap I had disclosed as UNREACHABLE. The evidence packet listed `main`'s two
    returns among five decision points "on the real-socket path" and therefore unbindable; the SPEC
    review showed one of them was bindable with a `sys.modules` injection, and it was right. Three
    of the five are genuinely unreachable without real I/O; these are not.

    The old `except (OSError, ValueError, OKXClientError)` caught NONE of these — measured against
    websockets 15.0.1, all three WS errors subclass `WebSocketException` and neither `OSError` nor
    `ValueError` — so the documented refusal degraded to a traceback for every failure the demo
    will actually hit. That scenario, live WS unreliable at demo time, is the exact one the plan's
    truth rule is written for.
    """
    module = _ws_exhibition_module()

    class _FakeWSError(Exception):
        pass

    class _FakeWebsockets:
        def connect(self, *_args, **_kwargs):
            if failure == "ModuleNotFoundError":
                raise ModuleNotFoundError("No module named 'websockets'")
            raise _FakeWSError(f"simulated {failure}")

    monkeypatch.setitem(sys.modules, "websockets", _FakeWebsockets())

    status = module.main(["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"])

    assert status == 1, "a failed connection must not report success"
    captured = capsys.readouterr()
    assert captured.err.startswith("refused: "), f"expected a clean refusal, got {captured.err!r}"
    assert captured.out == "", "nothing may be printed to stdout when no exhibition happened"
    # The TYPE survives into the message, so a genuine defect is still diagnosable rather than
    # being flattened into a bare string by the broadened catch.
    assert ("_FakeWSError" in captured.err) or ("ModuleNotFoundError" in captured.err)


async def test_the_real_transport_BOUNDS_recv_and_does_not_wait_forever():
    """MAJOR-Q2. `_WebsocketsTransport` is the repo's ONLY real `WSTransport`, and it must honour
    the receive-timeout MUST that `WSTransport`'s own docstring states.

    It previously did not: `recv` was a bare `await`. That made the module's stated MUST false at
    the one place it applied, and `_ws_converse` has no frame budget BY DESIGN precisely because
    the transport was supposed to carry the bound. Measured against websockets 15.0.1,
    `ClientConnection.recv` has signature `(self, decode)` — NO timeout parameter — so the bound
    can only be imposed by the caller. `connect()`'s keepalive detects a DEAD peer; a peer that is
    ALIVE and answering Pings while pushing no signal blocks `recv` with nothing to interrupt it.
    On the H6.1 demo path that is a hung terminal in front of an audience.

    NO SOCKET: the connection is a fake whose `recv` never completes.
    """
    module = _ws_exhibition_module()

    class NeverAnswers:
        def __init__(self):
            self.recv_calls = 0

        async def recv(self):
            self.recv_calls += 1
            await asyncio.Event().wait()  # never set: blocks until cancelled by the timeout

        async def send(self, message):
            pass

        async def close(self):
            pass

    connection = NeverAnswers()
    transport = module._WebsocketsTransport(connection, recv_timeout=0.05)

    # THE ASSERTION IS ON ELAPSED TIME, NOT MERELY ON THE EXCEPTION TYPE, and that is forced by
    # what the defect actually is. If the transport imposes no bound, `recv` waits FOREVER — so a
    # bare `pytest.raises(TimeoutError): await transport.recv()` does not fail against the
    # unbounded version, it HANGS, and a hung drill reports nothing at all. Measured: that is
    # exactly what happened, and it is the defect demonstrating itself.
    #
    # So the test carries its own outer safety net and then checks WHICH bound fired. Under the
    # real code the transport's own 0.05s expires; under an unbounded `recv` the 3s net fires and
    # the elapsed-time assertion kills it in three seconds instead of never.
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(transport.recv(), timeout=OUTER_SAFETY_NET_S)
    elapsed = time.monotonic() - started

    assert elapsed < OUTER_SAFETY_NET_S / 2, (
        f"recv took {elapsed:.2f}s: the transport imposed no bound of its own and the test's outer "
        "safety net is what stopped it — which is the unbounded-wait defect, not a passing test"
    )
    assert connection.recv_calls == 1, "the bound must apply to the recv that actually blocked"

    # ACCEPTANCE CONTROL: the bound does not simply reject everything. A connection that ANSWERS
    # returns its frame through the same wrapper, so the timeout discriminates between a quiet
    # socket and a working one rather than failing closed on both.
    class Answers(NeverAnswers):
        async def recv(self):
            return "pong"

    assert await module._WebsocketsTransport(Answers(), recv_timeout=5.0).recv() == "pong"

    # ...and bytes still decode at this edge, which is the adapter's other job.
    class AnswersBytes(NeverAnswers):
        async def recv(self):
            return b"pong"

    assert await module._WebsocketsTransport(AnswersBytes(), recv_timeout=5.0).recv() == "pong"


async def test_a_TimeoutError_from_the_transport_reaches_the_operator_as_a_clean_refusal():
    """The bound is only useful if its expiry becomes the documented `refused:` line.

    A timeout that surfaced as a traceback would trade one bad demo state for another. `main`'s
    broad `except Exception` renders `TimeoutError` correctly, and this pins that end to end
    through the injected connect factory — no socket, no `sys.modules` patching.
    """
    module = _ws_exhibition_module()

    class NeverAnswersConn:
        async def recv(self):
            await asyncio.Event().wait()

        async def send(self, message):
            pass

        async def close(self):
            pass

    class _Ctx:
        async def __aenter__(self):
            return NeverAnswersConn()

        async def __aexit__(self, *exc):
            return False

    monkeypatch_timeout = 0.05
    original = module.RECV_TIMEOUT_S
    module.RECV_TIMEOUT_S = monkeypatch_timeout
    try:
        # Self-bounded for the same reason as the transport test above: with no bound in the
        # subject this awaits forever and HANGS the run rather than failing it. The elapsed-time
        # assertion is what distinguishes "the transport's own bound fired" from "the test's
        # safety net did".
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                module._run(
                    module.build_parser().parse_args(
                        ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"]
                    ),
                    connect_factory=lambda _url: _Ctx(),
                ),
                timeout=OUTER_SAFETY_NET_S,
            )
        elapsed = time.monotonic() - started
    finally:
        module.RECV_TIMEOUT_S = original

    assert elapsed < OUTER_SAFETY_NET_S / 2, (
        f"_run took {elapsed:.2f}s: the transport imposed no bound and the test's net stopped it"
    )


async def test_run_and_main_BOTH_take_an_injected_connect_factory():
    """MINOR-Q3. The connection is injected, like the handoff already was.

    While `_run` resolved `websockets` by a function-local import, the ONLY interception point was
    global `sys.modules` state — so "no real socket" rested on every future test remembering to
    patch it, with no structural backstop, and three decision points here were disclosed as
    "unbindable without real I/O" when they were merely un-injected. `scripts/smoke_public_ws.py`
    already ships this seam; H2.5 had not picked it up.

    `Handoff` was the counter-example proving the point: everything above the injected handoff was
    unit-tested, and `connect` simply had not been given the same treatment.

    RENAMED. This was `..._so_no_test_needs_sys_modules`, which was FALSE IN ITS OWN FILE: two
    tests below still patch `sys.modules`, and they do so deliberately — they are the only things
    that bind `_default_connect` ITSELF, the one function the seam bypasses. A name asserting a
    file-wide absence that its own file refutes is worse than no name, because it tells the next
    reader the pattern is gone when it is load-bearing. The name now says what is measured: BOTH
    entry points take the factory.
    """
    module = _ws_exhibition_module()

    # BOTH entry points, because `main` previously had none — so its two returns were reachable
    # only through the module table, which is exactly what the seam was introduced to end.
    assert "connect_factory" in inspect.signature(module._run).parameters
    assert "connect_factory" in inspect.signature(module.main).parameters

    # Resolved at CALL time, not frozen as a def-time default. The signature default must be None;
    # a frozen `_default_connect` here is the defect this seam had and the other two had before it.
    assert module._run.__kwdefaults__["connect_factory"] is None
    assert module.main.__kwdefaults__["connect_factory"] is None

    # ...and the production path still reaches the real factory when nothing is injected, which is
    # what makes `None` a resolution rather than a hole.
    assert module._default_connect is not None

    calls = []

    class _Ctx:
        async def __aenter__(self):
            return _ScriptedConn([_ws_push(H22_WS)])

        async def __aexit__(self, *exc):
            return False

    def factory(url):
        calls.append(url)
        return _Ctx()

    handoff_calls = []
    module_handoff = module._subprocess_handoff

    def fake_handoff(argv, stdin_text):
        handoff_calls.append((argv, stdin_text))
        return 0

    args = module.build_parser().parse_args(
        ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x", "--dry-run"]
    )
    module._subprocess_handoff = fake_handoff
    try:
        status = await module._run(args, connect_factory=factory)
    finally:
        module._subprocess_handoff = module_handoff

    assert calls == ["wss://example.invalid"], "the factory must receive the operator's URL"
    assert status == 0
    assert handoff_calls and "--dry-run" in handoff_calls[0][0]


class _ScriptedConn:
    """A fake websockets connection: replays text frames, records nothing else. Never a socket."""

    def __init__(self, frames):
        self.frames = list(frames)

    async def send(self, message):
        pass

    async def recv(self):
        if not self.frames:
            raise AssertionError("the subscriber asked for more frames than the script holds")
        return self.frames.pop(0)

    async def close(self):
        pass


class _MainOutcome:
    """Everything one `main` invocation produced, INCLUDING what escaped it.

    `status` and `err` presume `main` RETURNED. Sometimes it must not — Ctrl-C has to stay Ctrl-C —
    so what escaped is evidence too, and a runner that cannot see it cannot tell "swallowed the
    interrupt" from "warned and re-raised". `escaped` is `None` whenever `main` returned normally.

    ONE result type, not two. This was `_MainResult(status, err)` plus a `_MainOutcome` that added
    exactly one field, so a future editor adding a fourth observable had to pick which of the two to
    extend and the other silently became less capable.
    """

    def __init__(self, status, err, escaped=None) -> None:
        self.status = status
        self.err = err
        self.escaped = escaped


def _exhibition_argv(dry_run=False):
    """The operator argv every `main` runner in this file uses. One spelling, four callers."""
    argv = ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"]
    if dry_run:
        argv.append("--dry-run")
    return argv


@contextlib.contextmanager
def _patched_handoff(module, handoff):
    """Swap the module's handoff for the duration, and restore it however the block exits.

    Load-bearing rather than belt-and-braces: `_ws_exhibition_module` is `lru_cache`d, so every
    test in this file shares ONE module object and a leaked patch would follow the suite.
    """
    original = module._subprocess_handoff
    module._subprocess_handoff = handoff
    try:
        yield
    finally:
        module._subprocess_handoff = original


def _capture_main_stderr(module, argv, ctx_factory) -> "_MainOutcome":
    """Run `main` with an INJECTED connect factory and capture its stderr.

    Uses the `connect_factory=` seam rather than `sys.modules`, which is the point of FINDING 2:
    while `main` had no seam, its returns were reachable only by faking the module table, and the
    test that certified the seam's existence was contradicted by two tests in its own file.
    No socket: `ctx_factory` builds an async context manager that never connects.

    Anything that ESCAPES `main` propagates out of here rather than being recorded — use
    `_main_outcome` when the escape is the measurement.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        status = module.main(argv, connect_factory=lambda _url: ctx_factory())
    return _MainOutcome(status, buffer.getvalue())


def test_a_handoff_that_STARTS_and_then_RAISES_warns_that_a_trial_may_exist():
    """CODEX MAJOR-1, half one: the boundary must contain the CALL, not follow it.

    The post-handoff boundary used to begin on the line AFTER `run_handoff(...)`, so an exception
    raised BY the handoff — a broken pipe while writing the child's stdin, which happens after the
    child is already spawned — escaped as an ordinary pre-handoff refusal. The comment naming the
    hazard sat one line below the statement that creates it.

    The handoff here RECORDS THAT IT STARTED before raising, which is what makes this a genuine
    reproduction rather than a plain failure: the child had begun, so a trial may exist.
    """
    module = _ws_exhibition_module()
    argv = ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"]
    started = []

    def _starts_then_raises(_argv, _stdin):
        started.append(_argv)
        raise BrokenPipeError("stdin write failed after child start")

    class _Ctx:
        async def __aenter__(self):
            return _ScriptedConn([_ws_push(H22_WS)])

        async def __aexit__(self, *exc):
            return False

    original = module._subprocess_handoff
    module._subprocess_handoff = _starts_then_raises
    try:
        result = _capture_main_stderr(module, argv, _Ctx)
    finally:
        module._subprocess_handoff = original

    assert started, "the reproduction is only valid if the handoff actually began"
    assert result.status == 1
    assert result.err.startswith("refused AFTER handoff:"), f"got {result.err!r}"
    assert "MAY ALREADY EXIST" in result.err
    assert "check the data dir" in result.err


def test_a_DRY_RUN_handoff_that_raises_is_NOT_reported_as_maybe_published():
    """DISCRIMINATION for the test above: the dry-run exemption is real, not a hole.

    `--dry-run` invokes the child with `--dry-run`, which writes nothing, so a failure there
    genuinely leaves nothing behind and the ordinary refusal is the correct advice. Without this,
    widening the boundary could have been done by warning on EVERY failure — which would make the
    warning meaningless rather than informative.
    """
    module = _ws_exhibition_module()
    argv = ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x", "--dry-run"]

    def _raises(_argv, _stdin):
        raise BrokenPipeError("stdin write failed after child start")

    class _Ctx:
        async def __aenter__(self):
            return _ScriptedConn([_ws_push(H22_WS)])

        async def __aexit__(self, *exc):
            return False

    original = module._subprocess_handoff
    module._subprocess_handoff = _raises
    try:
        result = _capture_main_stderr(module, argv, _Ctx)
    finally:
        module._subprocess_handoff = original

    assert result.status == 1
    assert result.err.startswith("refused: "), f"got {result.err!r}"
    assert "MAY ALREADY EXIST" not in result.err, "a dry run published nothing; do not warn"


class _FailsFirstWrite(io.StringIO):
    """A stderr whose FIRST write raises and whose later writes succeed.

    Codex's reproduction shape. The first write is the indeterminate warning inside `_run`; the
    later ones are `main`'s refusal line. If the warning print is outside a post-handoff boundary,
    the raw exception reaches the generic formatter and the operator gets the PRE-handoff wording
    for a signal whose child has already started AND returned.
    """

    def __init__(self, exc_factory=BrokenPipeError) -> None:
        super().__init__()
        self.failed_first = False
        self._exc_factory = exc_factory

    def write(self, text: str) -> int:
        if not self.failed_first:
            self.failed_first = True
            raise self._exc_factory("first post-handoff write failed")
        return super().write(text)


def test_a_FAILING_warning_write_still_reaches_the_operator_as_post_handoff():
    """CODEX R2 MAJOR-1 — the seventh instance of the class, inside the fix for the fifth.

    The indeterminate warning print sat OUTSIDE every post-handoff boundary. If it raised, the raw
    exception left `_run` and `main`'s GENERIC handler produced `refused: <exception>` — the
    pre-handoff wording — for a child that had started and returned. That is the input that makes
    an operator open a REST fallback and create the duplicate WS/REST trial.

    THE FIX WAS NOT "WRAP THIS PRINT". Twice the correction enclosed everything that existed and
    left what it added outside, so the whole tail of `_run` is now one boundary and a mechanical
    enumeration reports the REGION rather than the known gaps. That enumeration also found a third
    unprotected statement the review did not name — `return exit_status(summary)`.

    NO SOCKET: connection injected, stderr injected, first write fails, later writes succeed.
    """
    module = _ws_exhibition_module()
    argv = ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"]

    class _Ctx:
        async def __aenter__(self):
            return _ScriptedConn([_ws_push(H22_WS)])

        async def __aexit__(self, *exc):
            return False

    buffer = _FailsFirstWrite()
    original = module._subprocess_handoff
    module._subprocess_handoff = lambda _argv, _stdin: 120  # published, then failed to print
    try:
        with contextlib.redirect_stderr(buffer):
            status = module.main(argv, connect_factory=lambda _url: _Ctx())
    finally:
        module._subprocess_handoff = original

    assert buffer.failed_first, "the reproduction is only valid if the first write actually failed"
    err = buffer.getvalue()
    assert status == 1
    assert module.INDETERMINATE_WARNING in err, f"the operator lost the instruction: {err!r}"
    assert "check the data dir" in err
    assert not err.startswith("refused: BrokenPipeError"), (
        "the pre-handoff wording reached the operator for a child that had already returned"
    )
    assert err.startswith("refused AFTER handoff:"), f"got {err!r}"


def test_BOTH_indeterminate_routes_emit_THE_SAME_warning_not_merely_similar_ones():
    """CODEX R2 MINOR-1, and QUALITY found it first — two reviewers, so it is not a taste call.

    `INDETERMINATE_WARNING` exists so the raising route and the nonzero-return route cannot drift.
    It was referenced by ZERO tests: each route asserted its own substrings, so inlining different
    text at one site while keeping the fragments left the suite green and could silently drop the
    actionable "before opening a REST-sourced one" clause.

    This binds the NAMED CONSTANT on both routes, and then asserts the two stderr lines share it
    EXACTLY — substring-per-route is what failed to catch drift, so route-to-route equality is the
    assertion that actually holds them together.
    """
    module = _ws_exhibition_module()
    argv = ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"]

    class _Ctx:
        async def __aenter__(self):
            return _ScriptedConn([_ws_push(H22_WS)])

        async def __aexit__(self, *exc):
            return False

    class _TeardownBoom:
        async def __aenter__(self):
            return _ScriptedConn([_ws_push(H22_WS)])

        async def __aexit__(self, *exc):
            raise RuntimeError("teardown failed after the child was spawned")

    original = module._subprocess_handoff
    try:
        module._subprocess_handoff = lambda _argv, _stdin: 120
        nonzero_route = _capture_main_stderr(module, argv, _Ctx).err
        module._subprocess_handoff = lambda _argv, _stdin: 0
        raising_route = _capture_main_stderr(module, argv, _TeardownBoom).err
    finally:
        module._subprocess_handoff = original

    # The NAMED constant, on both routes — the assertion that was missing entirely.
    assert module.INDETERMINATE_WARNING in nonzero_route
    assert module.INDETERMINATE_WARNING in raising_route

    # ...and it is the SAME text, not two texts that happen to share fragments.
    assert module.INDETERMINATE_WARNING in (
        nonzero_route.split(" -- ", 1)[-1].strip()
    ), "the nonzero route's warning is not the shared constant"
    assert module.INDETERMINATE_WARNING in (
        raising_route.split(" -- ", 1)[-1].strip()
    ), "the raising route's warning is not the shared constant"

    # The routes differ in their PREFIX — they must, or the distinction is lost — and agree on the
    # instruction. Asserting both is what makes this a shared invariant rather than a coincidence.
    assert nonzero_route.startswith("handoff returned") or "AFTER starting" in nonzero_route
    assert raising_route.startswith("refused AFTER handoff:")
    assert "before opening a REST-sourced one" in module.INDETERMINATE_WARNING


def test_a_NONZERO_child_status_is_reported_as_INDETERMINATE_not_as_not_published():
    """CODEX MAJOR-1, half two — the half no exception handler could ever have covered.

    A nonzero return never raises, so no `try` protects it. And the inference behind treating it as
    "not published" is invalid for the actual child: `open_live_trial.py` publishes DURABLY and only
    THEN prints its summary, so a terminal-output failure leaves a real trial on disk while the
    child exits nonzero.

    Reported as `published: false`, that tells an operator to open a REST-sourced fallback for a
    signal that already has a WS-sourced trial — one signal, two live trials, contradictory source
    labels. `published` guarded the false POSITIVE; this is the false NEGATIVE, and the false
    negative is the one that produces the second trial.
    """
    module = _ws_exhibition_module()
    argv = ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"]

    class _Ctx:
        async def __aenter__(self):
            return _ScriptedConn([_ws_push(H22_WS)])

        async def __aexit__(self, *exc):
            return False

    original = module._subprocess_handoff
    module._subprocess_handoff = lambda _argv, _stdin: 120  # published, then failed to print
    try:
        result = _capture_main_stderr(module, argv, _Ctx)
    finally:
        module._subprocess_handoff = original

    assert result.status == 1
    assert "MAY ALREADY EXIST" in result.err, "a nonzero child may still have published"
    assert "check the data dir" in result.err
    assert "AFTER starting" in result.err

    # THREE STATES, not two, and each asserted so none can collapse into another.
    Summary = module.ExhibitionSummary

    def _s(status, dry):
        return Summary(evidence_hash="h", evidence_fields=("a",), handoff_status=status, dry_run=dry)

    assert (_s(0, False).published, _s(0, False).publication_indeterminate) == (True, False)
    assert (_s(120, False).published, _s(120, False).publication_indeterminate) == (False, True)
    assert (_s(0, True).published, _s(0, True).publication_indeterminate) == (False, False)
    assert (_s(120, True).published, _s(120, True).publication_indeterminate) == (False, False)
    assert _s(120, False).render()["publication_indeterminate"] is True


def test_the_REAL_child_publishes_BEFORE_it_prints_which_is_why_nonzero_is_indeterminate(tmp_path):
    """The premise of the finding, verified against the REAL `open_live_trial.py`. OFFLINE.

    This is the fact the whole third state rests on, so it is measured rather than assumed: the
    child writes the trial to disk and only afterwards prints. A local subprocess is spawned — no
    socket, no network, no credential — and its stdout is closed so the print fails after the write
    has already landed.

    If a future edit made the child print BEFORE publishing, `publication_indeterminate` would
    become needless pessimism and this test is where that would be noticed.

    CODEX R3 MINOR-1 — AND THE CONFIGURATION IS THE WHOLE TEST. This previously launched the child
    with a plain pipe for stdout. A pipe is BLOCK-BUFFERED, so `print` returns long before the
    EPIPE is ever observed and the failure surfaces only during the interpreter-exit flush — after
    every statement in the child has already run. Under that regime BOTH statement orders publish,
    so the test selected the one configuration in which its own premise could not bind. Measured
    against this exact child (all four rows below reproduced locally, no network):

        buffered   stdout=CLOSED rc=120 published=True   <- print failure deferred to exit flush
        unbuffered stdout=CLOSED rc=1   published=True   <- print failure AT the print statement

    `-u` is therefore load-bearing rather than tidiness: it is what moves the failure back to the
    print statement, which is what makes "published anyway" evidence about the ORDER. The regime
    control below executes both orders under both regimes and shows that only the unbuffered one
    can tell them apart.
    """
    module = _ws_exhibition_module()
    data_dir = tmp_path / "data"
    argv = module.build_handoff_argv(data_dir=str(data_dir), dry_run=False)

    payload = json.dumps(H22_WS, separators=(",", ":"), sort_keys=True)

    # THE REGIME CONTROL, and it is a DISCRIMINATION rather than an acceptance control: an
    # acceptance control would prove only that a closed stdout can fail the child. What has to be
    # proven is that the harness SEPARATES the two statement orders — because the previous harness
    # did not, and passed anyway. Two synthetic children, identical but for the order of their two
    # statements, run under both regimes. Local subprocesses; no network, no credential.
    write_first = "import pathlib,sys\np=pathlib.Path(sys.argv[1])\np.write_text('published')\nprint('summary')\n"
    print_first = "import pathlib,sys\np=pathlib.Path(sys.argv[1])\nprint('summary')\np.write_text('published')\n"

    def _publishes_with_stdout_closed(source: str, *, unbuffered: bool, tag: str) -> bool:
        child = tmp_path / f"child_{tag}.py"
        child.write_text(source)
        target = tmp_path / f"target_{tag}.txt"
        child_argv = [sys.executable, *(["-u"] if unbuffered else []), str(child), str(target)]
        probe = subprocess.Popen(child_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert probe.stdout is not None
        probe.stdout.close()
        if probe.stderr is not None:
            probe.stderr.read()  # drained so the child cannot block on a full stderr pipe
        assert probe.wait(timeout=30) != 0, f"{tag}: the closed stdout did not fail the child at all"
        return target.exists()

    # UNBUFFERED: the order is visible. A child that prints first never reaches its publish.
    assert _publishes_with_stdout_closed(write_first, unbuffered=True, tag="u_write") is True
    assert _publishes_with_stdout_closed(print_first, unbuffered=True, tag="u_print") is False, (
        "under -u a print-first child must publish NOTHING; without that this harness cannot tell "
        "the two orders apart and the premise below is unmeasured"
    )

    # BUFFERED: the finding itself, executed. Both orders publish, so the assertion further down
    # would hold no matter which order the child used — which is why the launch below passes -u.
    assert _publishes_with_stdout_closed(write_first, unbuffered=False, tag="b_write") is True
    assert _publishes_with_stdout_closed(print_first, unbuffered=False, tag="b_print") is True, (
        "block-buffered stdout defers the print failure to the exit flush, so it cannot bind order"
    )

    # MADE TO DO WHAT IT SAYS, rather than narrowing the docstring to what it did. The previous
    # version used `stdout=DEVNULL` — where writes SUCCEED — and asserted `returncode == 0`, so it
    # documented a failure it never executed and checked the premise by a SOURCE-TEXT INDEX
    # comparison that an ordinary refactor (moving the publish into a helper defined earlier) would
    # survive. Executing the failure is what makes this evidence: the whole third state rests on
    # "a nonzero child may still have published", and here that actually happens.
    #
    # The read end of the child's stdout is closed before it writes, so its print raises EPIPE
    # AFTER the publish has landed. Local subprocess only — no socket, no network, no credential.
    # `-u` for the reason the regime control just measured: with a buffered pipe the print would
    # not fail until the exit flush and this would publish under either statement order.
    proc = subprocess.Popen(
        [sys.executable, "-u", *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stdin is not None
    proc.stdout.close()  # the child's writes to stdout now fail
    try:
        proc.stdin.write(payload)
        proc.stdin.close()
    except BrokenPipeError:  # pragma: no cover - the child died even earlier; asserted below
        pass
    stderr_text = proc.stderr.read() if proc.stderr else ""
    returncode = proc.wait(timeout=30)

    # THE PREMISE, EXECUTED: the trial is on disk even though the child reported failure.
    written = sorted(p.name for p in (data_dir / "live").rglob("*") if p.is_file())
    assert written, f"the child published nothing; rc={returncode} err={stderr_text!r}"
    assert returncode != 0, (
        f"the child succeeded despite a closed stdout (rc={returncode}); this test no longer "
        "demonstrates publish-before-print and the third state needs a different justification"
    )
    # ...AND THE REGIME HELD. 120 is the code Python uses when the failure is the interpreter-exit
    # flush, which is precisely the buffered regime in which both orders publish. Asserting it is
    # absent is how this test states, in the units of its own claim, that the print failed AT the
    # print. Measured here: rc=1 unbuffered, rc=120 buffered.
    assert returncode != 120, (
        f"rc=120 means the print failure was deferred to the exit flush (rc={returncode}); the "
        "child ran block-buffered and this test cannot bind the publish-before-print order"
    )

    # ACCEPTANCE CONTROL: the same child with a WORKING stdout succeeds and publishes, so the
    # nonzero above is caused by the closed pipe rather than by anything else being broken. Same
    # `-u`, so the control differs from the case in exactly one thing: whether stdout is readable.
    ok_dir = tmp_path / "ok"
    ok = subprocess.run(
        [sys.executable, "-u", *module.build_handoff_argv(data_dir=str(ok_dir), dry_run=False)],
        input=payload, text=True, capture_output=True, check=False,
    )
    assert ok.returncode == 0, f"baseline child failed: {ok.stderr!r}"
    assert sorted(p.name for p in (ok_dir / "live").rglob("*") if p.is_file())


def test_a_TIMEOUT_refusal_tells_the_operator_what_to_do(monkeypatch):
    """CODEX MINOR-1. `str(TimeoutError())` is the EMPTY STRING.

    So the generic formatter rendered `refused: TimeoutError: ` with nothing after the colon — a
    refusal naming only that something did not happen. The detail is added where the bound is
    known, and the message names the fallback the truth rule requires.
    """
    module = _ws_exhibition_module()
    argv = ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"]

    # THE CONNECTION RAISES TimeoutError IMMEDIATELY rather than going quiet, and that is a
    # deliberate change from a never-answering fake. `main` is synchronous, so it carries no
    # outer safety net — and under a mutant that removes the transport's bound, a never-answering
    # fake makes this test HANG rather than fail. That cost three orphaned drill runs before I
    # diagnosed it, which is the same lesson the two elapsed-time tests already encode.
    #
    # Nothing is lost: this test is about the MESSAGE, not the bound. The bound is bound by
    # `test_the_real_transport_BOUNDS_recv_and_does_not_wait_forever`, which asserts on elapsed
    # time. Here the transport's `except TimeoutError` is entered the same way either route
    # reaches it, so the detail it adds is measured without waiting for anything.
    class _TimesOutAtOnce:
        async def send(self, message):
            pass

        async def recv(self):
            raise TimeoutError

        async def close(self):
            pass

    class _Ctx:
        async def __aenter__(self):
            return _TimesOutAtOnce()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(module, "RECV_TIMEOUT_S", 0.05)
    result = _capture_main_stderr(module, argv, _Ctx)

    assert result.status == 1
    assert result.err.startswith("refused: TimeoutError:")
    assert result.err.rstrip() != "refused: TimeoutError:", "the detail is empty — the whole finding"
    assert "0.05s" in result.err, "the message must name the bound that expired"
    assert WS_CHANNEL_ON_THE_WIRE in result.err
    assert "rest" in result.err.lower(), "it must name the fallback the truth rule requires"
    # A timeout is PRE-handoff — nothing was spawned — so it must NOT carry the maybe-published
    # warning. Otherwise the warning appears on refusals where it is simply false.
    assert "MAY ALREADY EXIST" not in result.err


def test_main_PRINTS_A_DIFFERENT_SENTENCE_after_the_handoff_than_before_it():
    """THE SENTENCE AN OPERATOR READS — which is the entire deliverable of MINOR-Q5.

    `PostHandoffError` exists because "a single word in a stderr line is the difference". The type
    was bound at three sites; the SENTENCE was bound nowhere. SPEC deleted `main`'s whole
    `except PostHandoffError` branch and got 805 passed, zero failures, identical to baseline — so
    a post-handoff failure would have fallen through to the ordinary `refused:` line and told the
    operator to open a REST-sourced trial over a signal that may already have one.

    It survived because exit status is 1 on BOTH paths and the exception type is unchanged, and
    every assertion in the neighbourhood was on type or status — the two things the mutant
    preserves. The operator-facing string is the only part of this feature a human ever sees, and
    it was the only part nothing measured.

    BOTH HALVES ARE HERE, and the pair is what makes it a discrimination rather than a spelling
    check: a post-handoff failure must say so, and a PRE-handoff failure must NOT.
    """
    module = _ws_exhibition_module()
    argv = ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"]

    class _TeardownBoom:
        async def __aenter__(self):
            return _ScriptedConn([_ws_push(H22_WS)])

        async def __aexit__(self, *exc):
            raise RuntimeError("connection teardown failed after the child was spawned")

    class _ConnectBoom:
        async def __aenter__(self):
            raise RuntimeError("could not connect at all")

        async def __aexit__(self, *exc):
            return False

    original = module._subprocess_handoff
    module._subprocess_handoff = lambda _argv, _stdin: 0
    try:
        after = _capture_main_stderr(module, argv, _TeardownBoom)
        before = _capture_main_stderr(module, argv, _ConnectBoom)
    finally:
        module._subprocess_handoff = original

    # AFTER the handoff: the operator must be told a trial may already exist.
    assert after.status == 1
    assert after.err.startswith("refused AFTER handoff:"), f"got {after.err!r}"
    assert "MAY ALREADY EXIST" in after.err
    assert "check the data dir" in after.err, "the line must say what to DO, not merely that it is different"

    # BEFORE the handoff: the ordinary line, which invites the documented REST fallback. If this
    # also said "AFTER handoff" the new sentence would be noise rather than information.
    assert before.status == 1
    assert before.err.startswith("refused: "), f"got {before.err!r}"
    assert "AFTER handoff" not in before.err
    assert "MAY ALREADY EXIST" not in before.err, (
        "a pre-handoff refusal must NOT warn about an existing trial; nothing was published"
    )


class _Teardown:
    """A connection context whose `__aexit__` does something OTHER than pass the failure along.

    Both modes are non-statement exits — the population a census over the STATEMENTS of `_run` is
    blind to, which is how the eighth instance of this lane's class survived a fix that enumerated
    every statement in the region.

    `mode="raises"` reproduces the REPLACEMENT Python performs when teardown fails while an
    exception is already propagating: the new exception becomes the active one and the original
    survives only in `__context__`. `mode="suppresses"` reproduces the other one — an `__aexit__`
    that returns True, after which no exception propagates at all and the code after the `async
    with` runs with nothing assigned. `mode="passes"` is the ordinary teardown, for the cases where
    the failure being studied is somewhere else.
    """

    MODES = ("raises", "suppresses", "passes", "interrupts")

    def __init__(self, frames, mode, *, connection=None):
        assert mode in self.MODES, f"unknown teardown mode {mode!r}"
        self._frames = frames
        self._mode = mode
        self._connection = connection
        self.calls = 0
        self.saw = None

    async def __aenter__(self):
        # `connection` is for the cases whose failure is in the WS conversation itself rather than
        # in the frames it replays -- a PRE-handoff interrupt, for instance.
        if self._connection is not None:
            return self._connection()
        return _ScriptedConn(list(self._frames))

    async def __aexit__(self, exc_type, exc, tb):
        self.calls += 1
        self.saw = exc_type
        if self._mode == "raises":
            raise RuntimeError("connection teardown also failed")
        if self._mode == "interrupts":
            # A teardown that is itself interrupted -- the `BaseException` twin of `"raises"`.
            raise KeyboardInterrupt
        return self._mode == "suppresses"


#: A frame the channel check refuses, so the handoff is never reached: a PRE-handoff failure.
_REFUSED_FRAME = '{"code":"0","data":[]}'


def _main_over(module, *, ctx, dry_run, handoff):
    """Run `main` over one injected connection and handoff. For rows where `main` RETURNS."""
    with _patched_handoff(module, handoff):
        return _capture_main_stderr(module, _exhibition_argv(dry_run), lambda: ctx)


def _chain_types(error):
    """Every exception reachable from `error` by either chain link, as a set of types."""
    seen, pending, types = set(), [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        types.add(type(current))
        pending.extend(link for link in (current.__cause__, current.__context__) if link is not None)
    return types


def test_a_FAILING_TEARDOWN_cannot_REPLACE_the_post_handoff_instruction():
    """CODEX R3 MAJOR-1 — the eighth instance of the class, and the first that arrives BETWEEN
    statements.

    `exhibit_one_signal` raises `PostHandoffError` after the child has started, so no summary is
    returned. If the connection's `__aexit__` then ALSO raises, Python REPLACES the active
    `PostHandoffError` with the teardown exception. At the arbitration point `summary is None`, so
    the replacement was re-raised unwrapped and `main` printed an ordinary PRE-handoff refusal —
    inviting the REST fallback for a signal whose child may already have published. The
    post-handoff fact was still in `error.__context__`; the code consulted `summary` and the outer
    type, neither of which survives the replacement.

    THE PREVIOUS FIX ENUMERATED STATEMENTS, AND THAT WAS THE WRONG POPULATION. `__aexit__`,
    `finally` and generator close run BETWEEN statements and are invisible to a census over them —
    and `__aexit__` is precisely where an exception is REPLACED rather than passed along.

    THE FAMILY, not a single acceptance case: an acceptance control proves only that the predicate
    CAN fire. The three rows that must NOT warn are what prove it SEPARATES — the dry-run rows in
    particular, where publication was impossible and the ordinary refusal is the true one.

    NO SOCKET: the connection is injected and the handoff is a function; nothing is spawned.
    """
    module = _ws_exhibition_module()
    started = []

    def _starts_then_raises(_argv, _stdin):
        started.append(_argv)
        raise BrokenPipeError("child stdin failed after spawn")

    def _starts_then_succeeds(_argv, _stdin):
        started.append(_argv)
        return 0

    def _never_reached(_argv, _stdin):
        started.append(_argv)
        raise AssertionError("a pre-handoff refusal must never reach the handoff")

    push = [_ws_push(H22_WS)]
    cases = (
        ("post-handoff failure", push, False, _starts_then_raises),
        ("pre-handoff failure", [_REFUSED_FRAME], False, _never_reached),
        ("dry-run handoff failure", push, True, _starts_then_raises),
        ("dry-run handoff success", push, True, _starts_then_succeeds),
    )

    rows = {}
    for label, frames, dry_run, handoff in cases:
        ctx = _Teardown(frames, "raises")
        before = len(started)
        result = _main_over(module, ctx=ctx, dry_run=dry_run, handoff=handoff)
        rows[label] = (
            result.status,
            result.err.startswith("refused AFTER handoff:"),
            module.INDETERMINATE_WARNING in result.err,
        )
        assert ctx.calls == 1, f"{label}: the reproduction requires the failing teardown to have run"
        if label != "pre-handoff failure":
            assert len(started) == before + 1, f"{label}: the handoff must actually have begun"

    # THE TABLE. Only the first row may carry the instruction; the other three are what make it
    # information rather than noise.
    assert rows == {
        "post-handoff failure": (1, True, True),
        "pre-handoff failure": (1, False, False),
        "dry-run handoff failure": (1, False, False),
        "dry-run handoff success": (1, False, False),
    }, rows


async def test_the_replacing_teardown_keeps_BOTH_causes_and_the_teardown_detail():
    """The other half of MAJOR-1: preserving the phase must not cost the diagnosis.

    What `main` prints is the TEARDOWN failure, because that is what actually stopped the run; what
    the operator is TOLD is the post-handoff instruction, because that is what governs their next
    action. Both are required, and the original post-handoff cause has to remain reachable or a
    later reader cannot tell which window the run died in.
    """
    module = _ws_exhibition_module()
    args = module.build_parser().parse_args(
        ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"]
    )

    def _starts_then_raises(_argv, _stdin):
        raise BrokenPipeError("child stdin failed after spawn")

    original = module._subprocess_handoff
    module._subprocess_handoff = _starts_then_raises
    try:
        with pytest.raises(module.PostHandoffError) as excinfo:
            await module._run(args, connect_factory=lambda _url: _Teardown([_ws_push(H22_WS)], "raises"))
    finally:
        module._subprocess_handoff = original

    assert "teardown also failed" in str(excinfo.value), "the teardown cause must stay diagnosable"
    types = _chain_types(excinfo.value)
    assert module.PostHandoffError in types
    assert BrokenPipeError in types, "the ORIGINAL post-handoff cause was lost from the chain"
    assert RuntimeError in types


def test_a_SUPPRESSING_teardown_is_classified_by_PHASE_and_not_by_what_was_assigned():
    """The sibling of MAJOR-1 found by sweeping the PATHS rather than the lines.

    An `__aexit__` that returns True makes the failure vanish: nothing propagates, `summary` was
    never assigned, and execution simply continues past the `async with`. That is the same class as
    the replacement — control left the block without any statement of ours running — so it is
    classified the same way: by the recorded phase, not by what happens to be bound.

    THREE ROWS, and the third is QUALITY R5 MAJOR-1. The `dry-run` x `suppresses` cell existed in
    no family in this file: every `"suppresses"` row was `dry_run=False`, and every dry-run row used
    `"raises"` or `"passes"`. The cell is the intersection of two dimensions each of which was
    covered alone — not a case anyone forgot to think about, but one nobody took the cross-product
    of. It went unguarded through four rounds, and a mutant that made this branch warn "a live trial
    MAY ALREADY EXIST" on a run where publication was IMPOSSIBLE survived all 154 tests.

    The phase is only informative if it can say NO, and it must say NO for two different reasons: a
    swallowed PRE-handoff failure published nothing, and a swallowed dry-run failure could not have.
    """
    module = _ws_exhibition_module()

    def _starts_then_raises(_argv, _stdin):
        raise BrokenPipeError("child stdin failed after spawn")

    def _never_reached(_argv, _stdin):
        raise AssertionError("a pre-handoff refusal must never reach the handoff")

    after_ctx = _Teardown([_ws_push(H22_WS)], "suppresses")
    before_ctx = _Teardown([_REFUSED_FRAME], "suppresses")
    dry_ctx = _Teardown([_ws_push(H22_WS)], "suppresses")
    after = _main_over(module, ctx=after_ctx, dry_run=False, handoff=_starts_then_raises)
    before = _main_over(module, ctx=before_ctx, dry_run=False, handoff=_never_reached)
    dry = _main_over(module, ctx=dry_ctx, dry_run=True, handoff=_starts_then_raises)

    # THE REPRODUCTION VALIDATES ITS OWN PREMISE: each teardown ran, and each SAW the exception it
    # was supposed to swallow. Without the second half a context that never received the failure
    # would still satisfy every assertion below.
    assert (after_ctx.calls, before_ctx.calls, dry_ctx.calls) == (1, 1, 1)
    assert after_ctx.saw is module.PostHandoffError, f"the post-handoff row swallowed {after_ctx.saw}"
    assert before_ctx.saw is OKXResponseError, f"the pre-handoff row swallowed {before_ctx.saw}"
    assert dry_ctx.saw is BrokenPipeError, (
        f"the dry-run row must swallow the RAW failure, unconverted: got {dry_ctx.saw}"
    )

    assert after.status == 1
    assert after.err.startswith("refused AFTER handoff:"), f"got {after.err!r}"
    assert module.INDETERMINATE_WARNING in after.err
    # ...and it names WHAT was suppressed. The branch built its message with no cause at all, so an
    # operator learned only that something had been swallowed.
    assert "BrokenPipeError" in after.err, f"the erased cause never reached the operator: {after.err!r}"

    assert before.status == 1
    assert before.err.startswith("refused: "), f"got {before.err!r}"
    assert module.INDETERMINATE_WARNING not in before.err
    assert "AFTER handoff" not in before.err

    # ...and the cause survives in the CHAIN as well as in the sentence, so a developer reading a
    # traceback gets it too. `__aexit__` returning True clears the handled exception, so this is
    # recovered from a local captured one block earlier rather than from `__context__`.
    async def _suppressed_run():
        with _patched_handoff(module, _starts_then_raises), pytest.raises(module.PostHandoffError) as excinfo:
            await module._run(
                module.build_parser().parse_args(_exhibition_argv()),
                connect_factory=lambda _url: _Teardown([_ws_push(H22_WS)], "suppresses"),
            )
        return excinfo.value

    assert BrokenPipeError in _chain_types(asyncio.run(_suppressed_run())), (
        "the suppressed cause is named in the sentence but lost from the chain"
    )

    # THE MISSING CELL. Publication was impossible, so the ordinary refusal is the true sentence.
    assert dry.status == 1
    assert dry.err.startswith("refused: "), f"got {dry.err!r}"
    assert module.INDETERMINATE_WARNING not in dry.err, (
        f"a dry run cannot have published; warning here erodes the warning that matters: {dry.err!r}"
    )
    assert "AFTER handoff" not in dry.err


def test_a_failure_in_the_EVENT_LOOP_teardown_still_reaches_the_operator_as_post_handoff(monkeypatch):
    """The one replacement no local of `_run` can record, so `main` carries the backstop.

    `asyncio.run` has a `finally` of its own — it cancels pending tasks, shuts async generators
    down and closes the loop. Anything raised there REPLACES a propagating `PostHandoffError`
    exactly as a failing `__aexit__` does, except that it happens ABOVE `_run`, where the phase flag
    is already out of scope. The fact still exists in the chain, so `main` looks there.

    The fake below is the real `asyncio.run` with a raising `finally` wrapped around it, which is
    the actual mechanism rather than a hand-built chain: `_run` really executes, really raises
    `PostHandoffError`, and the teardown really replaces it.
    """
    module = _ws_exhibition_module()
    real_run = asyncio.run
    used = []

    def _run_then_fail_in_teardown(coro):
        used.append(coro)
        try:
            return real_run(coro)
        finally:
            raise RuntimeError("event loop shutdown failed")

    def _starts_then_raises(_argv, _stdin):
        raise BrokenPipeError("child stdin failed after spawn")

    def _never_reached(_argv, _stdin):
        raise AssertionError("a pre-handoff refusal must never reach the handoff")

    monkeypatch.setattr(asyncio, "run", _run_then_fail_in_teardown)
    after = _main_over(
        module, ctx=_Teardown([_ws_push(H22_WS)], "passes"), dry_run=False, handoff=_starts_then_raises
    )
    before = _main_over(module, ctx=_Teardown([_REFUSED_FRAME], "passes"), dry_run=False, handoff=_never_reached)

    assert len(used) == 2, "the reproduction is only valid if the failing teardown actually ran"
    assert after.status == 1
    assert after.err.startswith("refused AFTER handoff:"), f"got {after.err!r}"
    assert module.INDETERMINATE_WARNING in after.err
    assert "event loop shutdown failed" in after.err, "the failure that stopped the run must stay visible"

    # DISCRIMINATION: the same loop-teardown failure over a PRE-handoff refusal is an ordinary one.
    assert before.status == 1
    assert before.err.startswith("refused: "), f"got {before.err!r}"
    assert module.INDETERMINATE_WARNING not in before.err


def test_the_post_handoff_chain_walk_is_COMPLETE_over_both_links():
    """The backstop above is only as good as the walk, so the walk is measured, not assumed.

    `raise X from Y` sets `__cause__`; raising while another exception is being handled sets
    `__context__`. Both occur in the chains this module produces, so a walk over one link is a
    census over half the population — the exact error the statement enumeration made. Cycles are
    reachable through `__context__`, and a walk that hangs is worse than one that misses.
    """
    module = _ws_exhibition_module()
    walk = module._post_handoff_in_chain
    post = module.PostHandoffError(BrokenPipeError("child stdin failed after spawn"))

    assert walk(post) is post, "the exception itself counts"

    via_cause = RuntimeError("teardown")
    via_cause.__cause__ = post
    assert walk(via_cause) is post, "the __cause__ link is not walked"

    via_context = RuntimeError("teardown")
    via_context.__context__ = post
    assert walk(via_context) is post, "the __context__ link is not walked"

    deep = RuntimeError("loop shutdown")
    deep.__context__ = via_cause
    assert walk(deep) is post, "the walk stops before the end of the chain"

    # DISCRIMINATION: a chain with no post-handoff failure in it must not be classified as one, or
    # every refusal becomes a maybe-published warning and the warning stops meaning anything.
    plain = ValueError("could not connect at all")
    plain.__context__ = OSError("dns")
    assert walk(plain) is None

    # ...and a cycle terminates rather than hanging the suite.
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__context__ = b
    b.__context__ = a
    assert walk(a) is None


def _main_outcome(module, *, ctx, dry_run, handoff):
    """Run `main` over an injected connection and handoff, recording a return OR an escape."""
    buffer = io.StringIO()
    status, escaped = None, None
    with _patched_handoff(module, handoff):
        try:
            with contextlib.redirect_stderr(buffer):
                status = module.main(_exhibition_argv(dry_run), connect_factory=lambda _url: ctx)
        except BaseException as error:  # noqa: BLE001 - what ESCAPES main is the measurement
            escaped = error
    return _MainOutcome(status, buffer.getvalue(), escaped)


def _sentence_class(module, err):
    """Classify the operator-facing stderr into the three sentences this program can produce."""
    if err.startswith("refused AFTER handoff:") and module.INDETERMINATE_WARNING in err:
        return "AFTER+warn"
    if err.startswith("refused: "):
        return "ordinary"
    if err == "":
        return "silent"
    return f"other({err!r})"


def test_a_BASE_EXCEPTION_after_the_handoff_still_reaches_the_operator_as_post_handoff():
    """SPEC R4 MAJOR-2 -- the NINTH instance, and the first carried by `BaseException`.

    MAJOR-1 with `KeyboardInterrupt` substituted for `PostHandoffError`. `exhibit_one_signal`
    marked the phase with `except Exception`, and `_run` recorded it with `except
    PostHandoffError`; a SIGINT delivered while the child is running matches NEITHER, so the phase
    was never recorded. If `__aexit__` then raised, the `RuntimeError` REPLACED the interrupt --
    and a `RuntimeError` IS an `Exception`, so it was caught, fell through the arbitration, and the
    chain walk found only a `KeyboardInterrupt`. The operator got `refused: RuntimeError: teardown
    failed` over a child that may already have published.

    I DISCLOSED THE WEAKER FORM OF THIS AND THAT IS THE LESSON. My report called it "a traceback
    and no indeterminate warning", which is true only with a CLEAN `__aexit__`. Composed with the
    failing teardown that this whole round was about, it is not a missing sentence but the
    affirmatively WRONG one -- the pre-handoff refusal, which invites the REST fallback. A
    disclosure is only as good as its worst case, and I had not composed my gap with the failure
    mode already in scope.

    THE POPULATION MOVED AGAIN: statements -> paths -> `await` SUSPENSION POINTS. The R3 census
    enumerated statement-boundary constructs and was correct for `Exception`; `BaseException`
    enters at a suspension point, which that census did not cover.

    THE CLASS, NOT ONE MEMBER. Rows are run for `KeyboardInterrupt` and `SystemExit` both, because
    the claim is about `BaseException` and a guard must be expressed in the units of its claim.

    NO SOCKET: the connection is injected and the handoff is a function; nothing is spawned.
    """
    module = _ws_exhibition_module()
    started = []

    def _interrupted(exc_factory):
        def _handoff(_argv, _stdin):
            started.append(_argv)
            raise exc_factory()

        return _handoff

    def _succeeds(_argv, _stdin):
        started.append(_argv)
        return 0

    def _never_reached(_argv, _stdin):
        started.append(_argv)
        raise AssertionError("a pre-handoff interrupt must never reach the handoff")

    def _interrupted_send(exc_factory):
        """A connection interrupted during the SUBSCRIBE, which is before anything is spawned.

        The interrupt lands on `send` rather than `recv` deliberately. `recv` is wrapped in
        `asyncio.wait_for`, which runs the awaited coroutine in a NESTED TASK, and `Task.__step`
        re-raises `KeyboardInterrupt`/`SystemExit` into the event loop instead of returning them
        through the awaiting frame -- so the interrupt bypasses the `async with` entirely and the
        teardown mode stops meaning anything. That is asyncio's behaviour, not this program's, and
        a row whose outcome is decided by it would be measuring the wrong subject. `send` is on the
        ordinary coroutine path, so the three teardown modes discriminate as designed.
        """

        class _SendInterrupted:
            async def send(self, message):
                raise exc_factory()

            async def recv(self):
                raise AssertionError("the subscribe never completed; recv must not be reached")

            async def close(self):
                pass

        return lambda: _SendInterrupted()

    push = [_ws_push(H22_WS)]
    rows = {}
    # label -> (frames, connection, mode, dry_run, handoff)
    cases = {}
    for exc_name, exc_factory in (("KeyboardInterrupt", KeyboardInterrupt), ("SystemExit", SystemExit)):
        for mode in ("passes", "raises", "suppresses"):
            cases[f"post-handoff {exc_name} + {mode}"] = (push, None, mode, False, _interrupted(exc_factory))
            cases[f"PRE-handoff {exc_name} + {mode}"] = (
                push, _interrupted_send(exc_factory), mode, False, _never_reached,
            )
    # The hole the compound case exposes from the other side: the child SUCCEEDED, so a trial
    # definitely exists, and the interrupt arrives during teardown instead of during the handoff.
    cases["success then KeyboardInterrupt teardown"] = (push, None, "interrupts", False, _succeeds)
    # Publication was impossible, so the ordinary refusal is the TRUE sentence -- ACROSS ALL THREE
    # teardown modes. These were two named rows covering `raises` and `passes`; the missing third
    # was the `dry-run` x `suppresses` cell, which existed in no family in this file and which a
    # surviving mutant found before this row did.
    for mode in ("passes", "raises", "suppresses"):
        cases[f"dry-run KeyboardInterrupt + {mode}"] = (push, None, mode, True, _interrupted(KeyboardInterrupt))

    for label, (frames, connection, mode, dry_run, handoff) in cases.items():
        ctx = _Teardown(frames, mode, connection=connection)
        outcome = _main_outcome(module, ctx=ctx, dry_run=dry_run, handoff=handoff)
        rows[label] = (
            _sentence_class(module, outcome.err),
            type(outcome.escaped).__name__ if outcome.escaped is not None else None,
        )
        assert ctx.calls == 1, f"{label}: the reproduction requires the teardown to have run"

    expected = {}
    for exc_name in ("KeyboardInterrupt", "SystemExit"):
        # A CLEAN teardown lets the interrupt out, so the operator gets the instruction AND the
        # process still dies by interrupt. A failing or suppressing teardown has already destroyed
        # the interrupt before `main` sees anything, so `main` returns 1 as it does for any refusal.
        expected[f"post-handoff {exc_name} + passes"] = ("AFTER+warn", exc_name)
        expected[f"post-handoff {exc_name} + raises"] = ("AFTER+warn", None)
        expected[f"post-handoff {exc_name} + suppresses"] = ("AFTER+warn", None)
        # DISCRIMINATION: nothing was spawned, so no sentence may claim a trial might exist.
        expected[f"PRE-handoff {exc_name} + passes"] = ("silent", exc_name)
        expected[f"PRE-handoff {exc_name} + raises"] = ("ordinary", None)
        expected[f"PRE-handoff {exc_name} + suppresses"] = ("ordinary", None)
    expected["success then KeyboardInterrupt teardown"] = ("AFTER+warn", "KeyboardInterrupt")
    # A clean teardown lets the interrupt out unmarked; a failing or suppressing one has already
    # replaced it with an ordinary `RuntimeError` before `main` sees anything. None of the three may
    # warn: under `--dry-run` the child wrote nothing.
    expected["dry-run KeyboardInterrupt + passes"] = ("silent", "KeyboardInterrupt")
    expected["dry-run KeyboardInterrupt + raises"] = ("ordinary", None)
    expected["dry-run KeyboardInterrupt + suppresses"] = ("ordinary", None)

    assert rows == expected, rows
    assert len(started) == len(cases) - 6, "every non-pre-handoff row must actually have begun the handoff"


def test_CTRL_C_after_the_handoff_warns_and_STILL_terminates_as_an_interrupt():
    """The instruction must not be bought with Ctrl-C's meaning.

    Converting a `KeyboardInterrupt` into an ordinary refusal would emit the right sentence and
    silently take Ctrl-C away -- an operator who interrupts a demo would get exit 1 and a program
    that claims it merely "refused". So `main` prints the instruction and RE-RAISES the original.

    The `refused:`/`refused AFTER handoff:` split exists because the operator's next ACTION
    differs; the interrupt's meaning is a second thing they rely on, and both are kept.
    """
    module = _ws_exhibition_module()

    def _interrupted(_argv, _stdin):
        raise KeyboardInterrupt

    outcome = _main_outcome(
        module, ctx=_Teardown([_ws_push(H22_WS)], "passes"), dry_run=False, handoff=_interrupted
    )

    assert isinstance(outcome.escaped, KeyboardInterrupt), (
        f"the interrupt was swallowed; escaped={outcome.escaped!r} status={outcome.status!r}"
    )
    assert outcome.status is None, "main must not return a status for an interrupt it re-raises"
    assert outcome.err.startswith("refused AFTER handoff:"), f"got {outcome.err!r}"
    assert module.INDETERMINATE_WARNING in outcome.err

    # ...AND THE LINE SAYS SOMETHING. `str(KeyboardInterrupt())` is the EMPTY STRING, so a naive
    # `f"{type(e).__name__}: {e}"` renders `KeyboardInterrupt: ` -- a refusal whose detail is a
    # bare colon, which is the defect `_WebsocketsTransport.recv`'s docstring already records for
    # `TimeoutError`. The detail is the type alone when there is no message.
    detail = outcome.err.split(" -- ", 1)[0]
    assert detail == "refused AFTER handoff: KeyboardInterrupt", f"got {detail!r}"

    # THE SECOND SURFACE, AND IT IS THE ONE AN OPERATOR ACTUALLY MEETS. The assertion above binds
    # `main`'s own rendering. `PostHandoffError.__init__` formats a SECOND copy of the same
    # sentence, and on this path that copy is spliced into the interrupt's `__context__` and
    # re-raised — so the DEFAULT EXCEPTHOOK prints it in the operator's terminal. Both were built
    # by hand from `type(cause).__name__` and `cause`; one was fixed and the other was not, which
    # is this round's defect class rendered in miniature.
    assert str(module.PostHandoffError(KeyboardInterrupt())) == "KeyboardInterrupt"
    assert str(module.PostHandoffError(TimeoutError())) == "TimeoutError"
    marker = module._post_handoff_in_chain(outcome.escaped)
    assert marker is not None and str(marker) == "KeyboardInterrupt", (
        f"the excepthook will print {str(marker)!r} to the operator's terminal"
    )

    # ...AND `main`'s PostHandoffError branch renders through that same string, so the fix reaches
    # it transitively. A message-less cause is what makes the two surfaces distinguishable at all.
    assert str(module.PostHandoffError(RuntimeError("teardown failed"))) == "RuntimeError: teardown failed"


def test_an_INTERRUPT_in_the_TAIL_after_the_child_returned_is_still_post_handoff():
    """The third boundary, which had the same `Exception` width as the other two.

    Everything in `_run`'s tail runs after the child has started AND returned, so a failure there
    is post-handoff by construction — that was settled in round 3. But the boundary enclosing it
    caught `Exception`, so an interrupt landing on the very print that carries the indeterminate
    warning escaped it, exactly as one landing in the handoff did. I found this while choosing
    mutants rather than from the verdict: the two boundaries the verdict named were fixed and this
    one would have been left one clause narrower than its siblings.

    BOTH HALVES, because the tail is also where the dry-run exemption changed. A `--dry-run` tail
    failure published nothing, so it must NOT warn.
    """
    module = _ws_exhibition_module()
    argv = ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"]
    original = module._subprocess_handoff
    module._subprocess_handoff = lambda _argv, _stdin: 120  # published, then failed to print

    # THE INTERRUPT LANDS ON THE WARNING PRINT ITSELF. First write raises, later writes succeed, so
    # `main`'s own refusal line still reaches the operator through the same stream.
    stderr = _FailsFirstWrite(KeyboardInterrupt)
    try:
        with contextlib.redirect_stderr(stderr), pytest.raises(KeyboardInterrupt):
            module.main(argv, connect_factory=lambda _url: _Teardown([_ws_push(H22_WS)], "passes"))
    finally:
        module._subprocess_handoff = original

    assert stderr.failed_first, "the reproduction is only valid if the first write actually failed"
    err = stderr.getvalue()
    assert err.startswith("refused AFTER handoff:"), f"got {err!r}"
    assert module.INDETERMINATE_WARNING in err

    # DISCRIMINATION: the same interrupt, in the same tail, under `--dry-run`. The child was
    # invoked with `--dry-run` and wrote nothing, so the ordinary outcome is the true one.
    dry_stdout = _FailsFirstWrite(KeyboardInterrupt)
    dry_stderr = io.StringIO()
    module._subprocess_handoff = lambda _argv, _stdin: 0
    try:
        with (
            contextlib.redirect_stdout(dry_stdout),
            contextlib.redirect_stderr(dry_stderr),
            pytest.raises(KeyboardInterrupt),
        ):
            module.main([*argv, "--dry-run"], connect_factory=lambda _url: _Teardown([_ws_push(H22_WS)], "passes"))
    finally:
        module._subprocess_handoff = original

    assert dry_stdout.failed_first, "the dry-run half must also have executed a real failure"
    assert module.INDETERMINATE_WARNING not in dry_stderr.getvalue(), (
        f"a dry run published nothing; do not warn: {dry_stderr.getvalue()!r}"
    )


def test_the_post_handoff_marker_SPLICES_into_the_chain_without_replacing_the_exception():
    """Why a `BaseException` is marked rather than converted, measured at the seam.

    An `Exception` can be replaced by `PostHandoffError` because nothing downstream depends on its
    identity. A `KeyboardInterrupt` cannot: replacing it is what swallows Ctrl-C. So the phase is
    SPLICED into the exception's chain, where the existing walk already looks, and the exception
    itself is re-raised unchanged.

    The splice must not cost the context it displaces -- an interrupt raised while another failure
    was being handled still has that failure to explain it -- so the previous `__context__` is
    carried on the marker rather than dropped.
    """
    module = _ws_exhibition_module()

    earlier = ValueError("the failure that was already being handled")
    interrupt = KeyboardInterrupt()
    interrupt.__context__ = earlier

    module._splice_post_handoff(interrupt)

    marker = module._post_handoff_in_chain(interrupt)
    assert isinstance(marker, module.PostHandoffError), "the walk cannot find the spliced phase"
    assert interrupt.__context__ is marker, "the marker must sit in the chain of the ORIGINAL"
    assert marker.__context__ is earlier, "the displaced context was dropped rather than carried"
    assert type(interrupt) is KeyboardInterrupt, "the exception's identity must be untouched"

    # DISCRIMINATION: an unspliced interrupt is not post-handoff, or every interrupt would warn.
    assert module._post_handoff_in_chain(KeyboardInterrupt()) is None

    # IDEMPOTENT, AND THE GUARD IS PRODUCTION-REACHABLE RATHER THAN DEFENSIVE. On the Ctrl-C path
    # the same exception object passes two boundaries that both mark it: `exhibit_one_signal`
    # splices, and `_run`'s arbitration calls `_reraise_as_post_handoff` on the same object. I
    # declared this guard a "predicted survivor" in my own drill on the grounds that a second marker
    # is only traceback noise; that reasoning was right about the HARM and wrong about the STATUS.
    # A documented guard that production exercises and no test binds is one a future simplifier
    # deletes, and it also creates a reference cycle that only `seen` keeps from hanging the walk.
    module._splice_post_handoff(interrupt)
    assert interrupt.__context__ is marker, "a second splice displaced the first marker"
    chain, seen, pending = [], set(), [interrupt]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        pending.extend(link for link in (current.__cause__, current.__context__) if link is not None)
    assert sum(isinstance(e, module.PostHandoffError) for e in chain) == 1, (
        f"the chain carries {sum(isinstance(e, module.PostHandoffError) for e in chain)} markers, not 1"
    )


async def test_a_failure_AFTER_the_handoff_says_so_because_the_operator_acts_differently():
    """MINOR-Q5. `refused:` and `refused AFTER handoff:` are different instructions.

    The module tells an operator that a `refused:` line means "fall back to a REST-sourced trial
    and say so". That is right for a failure BEFORE the handoff, when nothing was published.
    Applied to a failure AFTER it, the same advice opens a SECOND live trial for one signal — one
    labelled `ws`, one labelled `rest`. The window is small (the summary construction, the
    connection teardown, the final print) but the docstring makes a guarantee about what `refused:`
    MEANS, and nothing enforced it.
    """
    module = _ws_exhibition_module()

    class _Boom:
        async def __aenter__(self):
            return _ScriptedConn([_ws_push(H22_WS)])

        async def __aexit__(self, *exc):
            raise RuntimeError("connection teardown failed after the child was spawned")

    def fake_handoff(argv, stdin_text):
        return 0

    original = module._subprocess_handoff
    module._subprocess_handoff = fake_handoff
    try:
        with pytest.raises(module.PostHandoffError) as excinfo:
            await module._run(
                module.build_parser().parse_args(
                    ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"]
                ),
                connect_factory=lambda _url: _Boom(),
            )
    finally:
        module._subprocess_handoff = original

    assert "teardown failed" in str(excinfo.value)

    # THE SECOND WINDOW, and my own drill is why it is here. A mutant that stripped the
    # `PostHandoffError` wrapper from `exhibit_one_signal`'s TAIL survived the whole suite: the
    # test above only ever exercised the `_run` teardown window, so the summary-construction
    # window — which is the FIRST thing to run after the child is spawned — was unmeasured. Two
    # windows, one property; measuring one and claiming the property is the ledger defect again.
    handoff_fired = []

    def _recording_handoff(argv, stdin_text):
        handoff_fired.append(argv)
        return 0

    def _boom(_signal):
        raise RuntimeError("evidence hashing failed after the child was spawned")

    original_hash = module.evidence_hash
    module.evidence_hash = _boom
    try:
        with pytest.raises(module.PostHandoffError) as tail:
            await module.exhibit_one_signal(
                RecordingWS([_ws_push(H22_WS)]),
                WS_CHAIN_INDEX,
                data_dir="/tmp/x",
                handoff=_recording_handoff,
            )
    finally:
        module.evidence_hash = original_hash

    assert handoff_fired, "the guard is only meaningful once the handoff has actually fired"
    assert "evidence hashing failed" in str(tail.value)

    # DISCRIMINATION: a failure BEFORE the handoff must NOT be labelled post-handoff, or the new
    # message is just the old one in different words and tells the operator nothing.
    class _FailsBeforeHandoff:
        async def __aenter__(self):
            return _ScriptedConn(['{"code":"0","data":[]}'])  # refused by the channel check

        async def __aexit__(self, *exc):
            return False

    with pytest.raises(Exception) as before:
        await module._run(
            module.build_parser().parse_args(
                ["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"]
            ),
            connect_factory=lambda _url: _FailsBeforeHandoff(),
        )
    assert not isinstance(before.value, module.PostHandoffError), (
        "a pre-handoff refusal must keep the ordinary message; nothing was published"
    )


def test_main_refusal_control_the_fake_really_does_prevent_a_connection(monkeypatch):
    """ACCEPTANCE CONTROL for the test above: prove the injection is what stops the socket.

    Without this, the refusals above could be produced by argparse, a bad data dir, or anything
    else that fails before `connect` — and the tests would pass while never reaching the code path
    they claim to bind. Asserting `connect` was actually CALLED is what ties them to it.
    """
    module = _ws_exhibition_module()
    calls = []

    class _Recording:
        def connect(self, *args, **kwargs):
            calls.append(args)
            raise RuntimeError("no socket, by construction")

    monkeypatch.setitem(sys.modules, "websockets", _Recording())
    status = module.main(["--ws-url", "wss://example.invalid", "--chain-index", "501", "--data-dir", "/tmp/x"])

    assert status == 1
    assert calls == [("wss://example.invalid",)], "main must reach connect with the operator's URL"


async def test_a_REST_envelope_on_the_ws_wire_produces_NO_record_at_all():
    """The mislabel cannot be reached by feeding the WS path a REST response.

    This is the discrimination partner of the truth-rule test above: it is not enough that the WS
    path labels things `ws`, it must also be impossible for a REST arrival to travel it. The
    handoff must never fire — a refusal that still published would be the exact failure the truth
    rule exists to prevent.
    """
    with pytest.raises(OKXResponseError):
        await _exhibit(['{"code":"0","data":[{"timestamp":"1753400000000"}]}'])

    module = _ws_exhibition_module()
    handoff = RecordingHandoff()
    with pytest.raises(OKXClientError):
        await module.exhibit_one_signal(
            RecordingWS(['{"code":"0","data":[]}']), WS_CHAIN_INDEX, data_dir="/tmp/x", handoff=handoff
        )
    assert handoff.calls == [], "a refused frame must not reach the open-trial handoff"


async def test_DRY_RUN_travels_the_whole_edge_and_publishes_nothing():
    """`--dry-run` end to end THROUGH `exhibit_one_signal`, not at the argv boundary.

    ADDED BECAUSE FOUR SINGLE-TOKEN MUTANTS SURVIVED THE WHOLE SUITE. `--dry-run` was exercised
    only by DIRECT calls to `build_handoff_argv`, so the FORWARDING EDGE from `exhibit_one_signal`
    into it was never travelled. The operationally serious survivor hard-coded `dry_run=False` at
    that call site: an operator types `--dry-run`, `open_live_trial.py` publishes a REAL live
    trial, the printed summary still says `"dry_run": true`, and 797 tests passed.

    That is the honesty failure this whole task exists to prevent, arriving through the one flag
    whose entire purpose is "publish nothing".

    Each assertion below is a separate mutant's grave, and they are not redundant:
      argv carries --dry-run   -> kills the severed forwarding edge (the child would really publish)
      summary.dry_run is True  -> kills a constant on the record (the summary would lie about itself)
      published is False       -> kills dropping `and not self.dry_run` from the honesty guard
      exit_status == 0         -> pins the branch `exit_status`'s docstring claims: a dry run is a
                                  SUCCESS, so a shell driving this must not read it as a failure
    """
    summary, handoff, _ = await _exhibit([_ws_push(H22_WS)], dry_run=True)
    module = _ws_exhibition_module()

    argv, _stdin = handoff.calls[0]
    assert "--dry-run" in argv, "the flag never reached the child; it would publish for real"
    assert summary.dry_run is True
    assert summary.published is False, "a dry run published nothing and must never claim otherwise"
    assert summary.render()["dry_run"] is True
    assert summary.render()["published"] is False
    assert module.exit_status(summary) == 0, "a dry run is a SUCCESS; nothing was meant to publish"


async def test_published_is_TRUE_on_the_ordinary_path_so_the_guard_is_pinned_BOTH_ways():
    """The other direction of `published`, without which the property is half-measured.

    `published` was asserted False exactly once, on the failed-handoff path, and True nowhere at
    all. A one-directional assertion cannot tell a working guard from a CONSTANT: a mutant making
    `published` return a constant `False` survived the entire suite, because nothing ever required
    it to be True. Pinning both directions is what makes the assertion evidence rather than a
    coincidence — the same acceptance/discrimination split this file applies elsewhere.
    """
    summary, _, _ = await _exhibit([_ws_push(H22_WS)])

    assert summary.dry_run is False
    assert summary.handoff_status == 0
    assert summary.published is True, "a real run with a successful handoff DID publish"
    assert summary.render()["published"] is True


async def test_published_is_false_if_EITHER_reason_holds_and_the_two_are_independent():
    """`published` is a conjunction, and a per-line ledger cannot see that.

    The rule "L100 is bound by a killed mutant" was recorded as satisfied while only the
    `handoff_status` clause was measured. A two-clause conjunction hosts TWO independent
    behaviours at one line number, so a per-line accounting reports 100% and means 50%. All four
    combinations are walked here so neither clause can be dropped unnoticed.
    """
    module = _ws_exhibition_module()
    Summary = module.ExhibitionSummary

    def _summary(status, dry):
        return Summary(evidence_hash="h", evidence_fields=("a",), handoff_status=status, dry_run=dry)

    assert _summary(0, False).published is True, "the only combination that publishes"
    assert _summary(1, False).published is False, "the handoff refused"
    assert _summary(0, True).published is False, "a dry run publishes nothing"
    assert _summary(1, True).published is False, "both reasons at once"


async def test_a_nonzero_handoff_status_is_reported_and_not_swallowed():
    """If `open_live_trial.py` refuses the capture, the exhibition must say so.

    A script that printed a summary and exited 0 over a trial that was never published would be
    the demo telling the operator a live trial exists when none does.
    """
    module = _ws_exhibition_module()
    summary, _, _ = await _exhibit([_ws_push(H22_WS)], handoff=RecordingHandoff(status=1))

    assert summary.handoff_status == 1
    assert summary.published is False
    assert module.exit_status(summary) != 0


def _reads_environment(source: str) -> set[str]:
    """Return the environment-reading constructs `source` actually CONTAINS, by parsing it.

    Structural, so a docstring or comment mentioning `os.getenv` contributes nothing: comments are
    not in the tree at all, and a string literal is an `ast.Constant`, never a `Name` or a call.

    Covers the routes a credential read can actually arrive by — `import os` at any scope
    (including inside a function, the idiom `ws_exhibition.py` itself uses for `websockets`),
    `from os import ...`, attribute access such as `os.environ` / `os.getenv`, and a bare `getenv`
    bound by a from-import.
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {f"import {a.name}" for a in node.names if a.name.split(".")[0] == "os"}
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "os":
            found |= {f"from os import {a.name}" for a in node.names}
        elif isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
            found.add(f".{node.attr}")
        elif isinstance(node, ast.Name) and node.id in ("environ", "getenv"):
            found.add(node.id)
    return found


def test_the_exhibition_script_reads_NO_credential_from_the_environment(monkeypatch):
    """Gate A: this script cannot leak a credential because it never reads one.

    `subscribe_one_signal` authenticates nothing — the WS handshake belongs to the transport — so
    the exhibition path has no reason to hold `OKX_API_KEY`, `OKX_SECRET_KEY` or `OKX_PASSPHRASE`.
    Sentinels are planted and their ABSENCE from every rendered surface is asserted; the sentinels
    are obvious non-secrets, never a realistic credential.
    """
    module = _ws_exhibition_module()
    sentinels = {
        "OKX_API_KEY": "SENTINEL-API-KEY-MUST-NOT-APPEAR",
        "OKX_SECRET_KEY": "SENTINEL-SECRET-MUST-NOT-APPEAR",
        "OKX_PASSPHRASE": "SENTINEL-PASSPHRASE-MUST-NOT-APPEAR",
    }
    for name, value in sentinels.items():
        monkeypatch.setenv(name, value)

    # THE PREDICATE IS THE AST, NOT THE TEXT — and the previous version of this comment claimed
    # exactly that while still scanning text. `inspect.getsource` returns every docstring and
    # comment in the file, so `assert "getenv" not in source` accused the script of calling
    # `getenv` on the strength of a COMMENT SAYING IT NEVER DOES. Measured: adding one comment line
    # (`# NOTE: this script never calls os.getenv for a credential.`) produced a file whose
    # `ast.dump` was IDENTICAL — provably no behaviour change — and turned this test red.
    #
    # Twice now the fix changed the words being scanned rather than the class of the scan. Parsing
    # is what actually distinguishes a call from a sentence about a call.
    assert _reads_environment(inspect.getsource(module)) == set(), (
        "the exhibition script reads the environment; it has no use for credentials"
    )

    # `hasattr(module, "os")` is retained but is NOT the guard — a function-local `import os` never
    # binds a module attribute, and this file demonstrates that idiom itself. The AST walk above
    # sees a function-local import; this line only catches the top-level case.
    assert not hasattr(module, "os")

    # ACCEPTANCE CONTROL, which this test previously had none of. A predicate that returns the empty
    # set for everything would pass above and measure nothing. Each of these synthetic modules reads
    # the environment by a DIFFERENT route, including the function-local import the real module's
    # own style makes plausible.
    for label, snippet in (
        ("top-level import + environ", "import os\nKEY = os.environ['OKX_API_KEY']\n"),
        ("top-level import + getenv", "import os\ndef f():\n    return os.getenv('OKX_API_KEY')\n"),
        ("function-local import", "def f():\n    import os\n    return os.environ.get('OKX_API_KEY')\n"),
        ("from-import", "from os import getenv\ndef f():\n    return getenv('OKX_API_KEY')\n"),
    ):
        assert _reads_environment(snippet), f"the predicate cannot detect: {label}"

    # ...and it does NOT fire on prose about the environment, which is the whole point.
    assert _reads_environment("# this module never calls os.getenv\n'''os.environ is not read'''\n") == set()

    rendered = json.dumps(module.build_handoff_argv(data_dir="/tmp/x", dry_run=False))
    for value in sentinels.values():
        assert value not in rendered


def test_the_handoff_argv_targets_the_payments_lane_script_that_actually_exists():
    """The composition is script-level: this argv must name a real `open_live_trial.py`.

    An argv pointing at a path that does not exist would fail only at demo time, in front of the
    thing it exists to demonstrate.
    """
    module = _ws_exhibition_module()

    argv = module.build_handoff_argv(data_dir="/tmp/x", dry_run=True)
    assert Path(argv[0]).exists()
    assert "--dry-run" in argv
    assert "--dry-run" not in module.build_handoff_argv(data_dir="/tmp/x", dry_run=False)
