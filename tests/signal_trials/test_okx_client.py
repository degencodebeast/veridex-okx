import functools
import inspect
import json
import math
import sys
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
    assert set(H22_REST) != set(H22_WS), "the twins must differ in KEY SPELLING, which is the thing normalized"
    # ...and the difference must be confined to the alias spellings, not the observed values.
    shared = set(H22_REST) & set(H22_WS) - {"token"}
    assert shared, "the twins share no top-level fields; they cannot be describing one event"
    for key in shared:
        assert H22_REST[key] == H22_WS[key], f"twin fixtures disagree on the VALUE of {key!r}"


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


async def test_subscribe_one_signal_skips_heartbeats_and_acks_but_not_indefinitely():
    """`pong` and the subscribe ack are control traffic and are skipped; the FIRST push wins.

    The acceptance control for the refusal suite above: a method that refused every frame would
    pass all of those tests. This one proves the skip path can actually reach a signal.
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


async def test_the_rendered_summary_carries_evidence_FIELD_NAMES_but_not_evidence_VALUES():
    """The printed summary names the evidence fields and hashes them; it does not publish them.

    `open_live_trial.py` renders on exactly this principle and this script's docstring claims the
    same one, but the mechanical decision-point sweep for this commit found `render()` unbound: no
    mutant and no assertion reached it, so the claim was documentation only. The demo transcript is
    not where the payload gets published — the free read is — and the hash is what an operator
    actually compares against a receipt.
    """
    summary, _, _ = await _exhibit([_ws_push(H22_WS)])
    rendered = json.dumps(summary.render(), sort_keys=True)

    assert summary.evidence_hash in rendered
    assert "trigger_price" in rendered, "the FIELD NAMES are the useful part and must be present"

    # ACCEPTANCE CONTROL. Every value below really is in the fixture, so a rendering that leaked
    # values WOULD contain it — without this the absence assertions could be passing because the
    # values were never in the signal in the first place.
    assert (H22_WS["price"], H22_WS["amountUsd"], H22_WS["triggerWalletAddress"]) == ("0.042", "1500", "0xa")
    assert H22_WS["token"]["tokenAddress"] == "So1"
    for value in ("0.042", "1500", "So1", "0xa"):
        assert value not in rendered, f"the rendered summary published the evidence value {value!r}"

    # `t0_ms` is the DECLARED exception and is asserted PRESENT rather than quietly omitted from
    # the list above. It is trial metadata (when the trial opens, which an operator needs to check
    # a deadline) and `open_live_trial.py` prints it for the same reason. Stating it here means the
    # rule is "market data never, metadata deliberately" rather than an unexplained hole.
    assert str(H22_WS["timestamp"]) in rendered


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

    # The predicate is the MECHANISM, not the spelling. An earlier draft asserted that the variable
    # NAMES never appear in the source, which flagged the module docstring's own explanation that it
    # does not read them — a predicate that fires on documentation is measuring the wrong thing.
    # What "reads no credential" actually means is that no environment read exists at all.
    source = inspect.getsource(module)
    assert not hasattr(module, "os"), "the exhibition script imported `os`; it has no environment to read"
    for reader in ("environ", "getenv"):
        assert reader not in source, f"the exhibition script calls {reader}; it has no use for credentials"

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
