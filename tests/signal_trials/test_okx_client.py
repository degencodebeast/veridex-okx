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
