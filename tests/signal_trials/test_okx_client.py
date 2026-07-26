import pytest

from veridex.signal_trials.okx_client import (
    BAR_MS,
    CandleSeries,
    OKXAPIError,
    OKXCredentials,
    OKXMarketClient,
    OKXResponseError,
    SignalFilters,
)


class RecordingFake:
    def __init__(self, payload): self.payload, self.calls = payload, []
    async def request(self, method, path, *, params, json_body, headers):
        self.calls.append((method, path, params, json_body, headers)); return self.payload

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
            "minAddressCount": 2,
            "minAmountUsd": 1000,
            "minMarketCapUsd": 100_000,
            "minLiquidityUsd": 20_000,
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
