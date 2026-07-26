import math

import pytest

from veridex.signal_trials.okx_client import (
    BAR_MS,
    CANDLE_FIELD_COUNT,
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


def _candles_client(rows: list[object]) -> tuple[OKXMarketClient, RecordingFake]:
    fake = RecordingFake({"code": "0", "data": rows})
    return OKXMarketClient(fake, OKXCredentials("k", "s", "p")), fake


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
    client, _ = _candles_client([_candle_row(**{column: raw})])

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

    client, _ = _candles_client(rows)

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

    client, _ = _candles_client([_candle_row(c="1e400")])

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
    client, _ = _candles_client([_candle_row(**dict.fromkeys(NUMERIC_COLUMN_TO_FIELD, raw))])

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

    accepted, _ = _candles_client([_candle_row(c="1e308")])
    series = await accepted.get_candles("501", "So1", "1m")
    assert isinstance(series, CandleSeries)
    assert series.candles[0].close == 1e308

    rejected, _ = _candles_client([_candle_row(c="1e309")])
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
    client, _ = _candles_client([row])

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
        client, _ = _candles_client([_candle_row(**{column: "1e400"})])
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
