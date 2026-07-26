import pytest

from veridex.signal_trials import challenge_spec
from veridex.signal_trials.challenge_spec import (
    FORBIDDEN_EVIDENCE_FIELDS,
    LeakageError,
    assert_no_future_fields,
    evidence_hash,
    normalize_signal,
    visible_at_decision,
)

REST = {"timestamp": "1753400000000", "price": "0.042", "chainIndex": "501", "amountUsd": "1500",
        "triggerWalletCount": "3", "walletType": "1", "triggerWalletAddress": "0xa",
        "soldRatioPercent": "12.5",
        "token": {"tokenAddress": "So1", "symbol": "TOK", "name": "Tok", "marketCapUsd": "2000000",
                   "holders": "900", "top10HolderPercent": "31.5"}}
WS = dict(REST); WS["soldRatioPercentage"] = WS.pop("soldRatioPercent")
WS["token"] = dict(REST["token"]); WS["token"]["top10HolderPercentage"] = WS["token"].pop("top10HolderPercent")

def test_rest_and_ws_normalize_to_byte_identical_evidence_hash():
    assert evidence_hash(normalize_signal(REST, "rest")) == evidence_hash(normalize_signal(WS, "ws"))

def test_sold_ratio_never_reaches_evidence():
    ev = visible_at_decision(normalize_signal(REST, "rest"))
    assert "sold_ratio_percent" not in ev and "soldRatioPercent" not in ev

def test_liquidity_and_future_fields_raise():
    with pytest.raises(LeakageError): assert_no_future_fields({"liquidity_usd": 37412.0})
    with pytest.raises(LeakageError): assert_no_future_fields({"close": 1.1})


# --- PKT-DEC-C15 + its A1 addendum: `liquidityUsd` and `clvBps`, the camelCase twins of
# `liquidity_usd` and `clv_bps`. Each alias gets its own rejection test and its own mutation test,
# deliberately not parametrized into one: the two guarantees are independent, and a single case
# covering both could stay green while only one of them was actually load-bearing.
# Additions below this line are not frozen content; the bodies above are, and are untouched.


def test_liquidity_usd_camel_case_refused_top_level_and_nested() -> None:
    """The camelCase spelling must be refused wherever it sits, not only at the top level.

    `okx_client.py` serializes `minLiquidityUsd`, so camelCase is the live wire convention. While
    the set carried only the snake_case spelling, both of these payloads passed the guard. Nested
    is asserted separately from top level because the nested shape (`{"token": {...}}`) is the one
    this pipeline actually assembles, so a guard green only at the top level would read as passing
    over precisely the case it exists to reject.
    """
    with pytest.raises(LeakageError):
        assert_no_future_fields({"liquidityUsd": 500000})
    with pytest.raises(LeakageError):
        assert_no_future_fields({"token": {"liquidityUsd": 500000}})


def test_clv_bps_camel_case_refused_top_level_and_nested() -> None:
    """Same guarantee for CLV, whose snake_case spelling the plan author already chose to guard.

    `clvBps` is not a new guard: `clv_bps` was already in the frozen set, so the camel spelling
    walking through was the guard being half-applied rather than a gap in what it covers. CLV is
    outright post-decision data — it is priced against a settlement the decision cannot have seen.
    """
    with pytest.raises(LeakageError):
        assert_no_future_fields({"clvBps": 42})
    with pytest.raises(LeakageError):
        assert_no_future_fields({"scoring": {"clvBps": 42}})


def test_liquidity_usd_entry_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation drill, in-suite: remove ONLY `liquidityUsd` and prove the guard goes blind to it.

    The rejection test above is what BREAKS if the entry is dropped. This one shows WHY it breaks —
    that the refusal is produced by that specific entry and not by some other rule that happens to
    cover the same payloads. Without it, a green assertion could be riding on a guard that was
    never doing the work.
    """
    monkeypatch.setattr(
        challenge_spec,
        "FORBIDDEN_EVIDENCE_FIELDS",
        FORBIDDEN_EVIDENCE_FIELDS - {"liquidityUsd"},
    )
    # Drop the one entry and the guard accepts both payloads it is required to refuse.
    assert_no_future_fields({"liquidityUsd": 500000})
    assert_no_future_fields({"token": {"liquidityUsd": 500000}})
    # Everything else still bites — including the OTHER new alias, which is what makes this proof
    # independent of the clvBps one rather than the two sharing a single outcome.
    with pytest.raises(LeakageError):
        assert_no_future_fields({"liquidity_usd": 500000})
    with pytest.raises(LeakageError):
        assert_no_future_fields({"clvBps": 42})
    with pytest.raises(LeakageError):
        assert_no_future_fields({"soldRatioPercent": 12.5})


def test_clv_bps_entry_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same drill for `clvBps`, run as its own proof rather than folded into the one above.

    Two aliases landed in one change, so one mutation covering both would leave it possible for a
    single entry to be carrying both assertions. Removing each independently is what establishes
    that each is separately load-bearing.
    """
    monkeypatch.setattr(
        challenge_spec,
        "FORBIDDEN_EVIDENCE_FIELDS",
        FORBIDDEN_EVIDENCE_FIELDS - {"clvBps"},
    )
    assert_no_future_fields({"clvBps": 42})
    assert_no_future_fields({"scoring": {"clvBps": 42}})
    with pytest.raises(LeakageError):
        assert_no_future_fields({"clv_bps": 42})
    with pytest.raises(LeakageError):
        assert_no_future_fields({"liquidityUsd": 500000})
    with pytest.raises(LeakageError):
        assert_no_future_fields({"soldRatioPercent": 12.5})


def test_forbidden_set_retains_every_previously_listed_spelling() -> None:
    """C15 and A1 are ADDITIONS. Nothing already guarded may be lost to them, or to a later one.

    Containment, not equality: this assertion pins the pre-C15 nine and should never need editing,
    so a REMOVAL fails here no matter which later change caused it. The exact-membership test below
    is the other half — this one is deliberately blind to additions so that it keeps meaning the
    same thing over time.
    """
    assert {
        "sold_ratio_percent",
        "soldRatioPercent",
        "soldRatioPercentage",
        "liquidity",
        "liquidity_usd",
        "settlement",
        "future",
        "close",
        "clv_bps",
    } <= FORBIDDEN_EVIDENCE_FIELDS


def test_forbidden_set_is_exactly_the_authorized_eleven() -> None:
    """Widening this frozen guard must be a deliberate, decision-backed edit — so pin it exactly.

    `minLiquidityUsd` is asserted OUT by name: it is a REQUEST filter key (`okx_client.py`), never
    a key in an evidence payload, and it is the most plausible next speculative addition precisely
    because it is where the camelCase evidence came from. A future authorized alias has to edit
    this list, which is the intended cost — an unauthorized one fails here instead of landing quietly.
    """
    authorized = {
        "sold_ratio_percent",
        "soldRatioPercent",
        "soldRatioPercentage",
        "liquidity",
        "liquidity_usd",
        "liquidityUsd",
        "settlement",
        "future",
        "close",
        "clv_bps",
        "clvBps",
    }
    assert authorized == FORBIDDEN_EVIDENCE_FIELDS
    assert len(FORBIDDEN_EVIDENCE_FIELDS) == 11
    assert "minLiquidityUsd" not in FORBIDDEN_EVIDENCE_FIELDS


# --- PKT-MILESTONE-DATA-CODEX-da5b59a MAJOR: `wallet_type` cross-transport canonicalization.
#
# Frozen spec §5.2 (`docs/superpowers/specs/2026-07-23-veridex-signal-trials-design.md:76`) requires
# ONE canonical schema across transports and names this dimension explicitly:
#     `wallet_type` <- REST numeric/named vs WS comma-separated numerics
#
# The frozen fixture holds `walletType` at the string "1" on BOTH sides, so the mandated
# hash-equality test never varied the one dimension the spec says differs between transports. Every
# line ran; the value was simply constant. The pairs below vary it, and each asserts BOTH canonical
# field equality and evidence-hash equality — asserting only the hashes would reproduce the very
# blindness being closed here.
#
# Canonical form (see `_canonical_wallet_type`): comma-separated numeric codes, deduplicated and
# sorted ascending. `walletType=1` is Smart Money per §5.1, so "1" stays "1" and the frozen
# fixture's hash is unchanged — pinned below.


def test_wallet_type_named_rest_and_numeric_ws_share_evidence_identity() -> None:
    """The required cross-transport pair whose RAW `walletType` values differ.

    REST carries the named form, WS the numeric form, for the same logical wallet category. Before
    the fix these produced `'Smart Money'` and `'1'` and therefore two different evidence hashes —
    different receipt identity for one market event across the replay and live-exhibition paths.
    """
    rest = {**REST, "walletType": "Smart Money"}
    ws = {**WS, "walletType": "1"}
    assert rest["walletType"] != ws["walletType"]

    rest_sig = normalize_signal(rest, "rest")
    ws_sig = normalize_signal(ws, "ws")
    assert rest_sig.wallet_type == ws_sig.wallet_type == "1"
    assert evidence_hash(rest_sig) == evidence_hash(ws_sig)


def test_wallet_type_numeric_rest_is_accepted_and_matches_its_string_twin() -> None:
    """A numeric REST `walletType` must enter the pack at all.

    H2.1 carries raw rows through untouched, so an int on the wire reached `_as_str` and raised —
    a valid row could not be normalized. The int and its string twin must also agree, or the same
    row would hash differently depending on the JSON decoder's typing.
    """
    numeric = normalize_signal({**REST, "walletType": 1}, "rest")
    stringy = normalize_signal({**REST, "walletType": "1"}, "rest")
    assert numeric.wallet_type == stringy.wallet_type == "1"
    assert evidence_hash(numeric) == evidence_hash(stringy)


def test_wallet_type_ws_list_is_order_and_duplicate_invariant() -> None:
    """A wallet-type set is unordered, so its serialization must not carry order into the hash.

    WS emits comma-separated numerics. `"2,1"` and `"1,2"` denote the same set; if wire order
    survived into the canonical value, two identical observations would take different receipt
    identities purely from field ordering. Duplicates collapse for the same reason.
    """
    for raw in ("1,2", "2,1", "1,2,1", " 2 , 1 ", "01,2"):
        assert normalize_signal({**WS, "walletType": raw}, "ws").wallet_type == "1,2"

    ordered = normalize_signal({**WS, "walletType": "1,2"}, "ws")
    reversed_ = normalize_signal({**WS, "walletType": "2,1"}, "ws")
    assert evidence_hash(ordered) == evidence_hash(reversed_)


def test_wallet_type_mixed_named_and_numeric_list_crosses_transports() -> None:
    """The named and list forms compose: a named element inside a list resolves like a bare name.

    This is the second cross-transport pair with differing raw values, covering the case where the
    REST named form and the WS numeric-list form describe the same two-category set.
    """
    rest = {**REST, "walletType": "Smart Money,2"}
    ws = {**WS, "walletType": "2,1"}
    assert rest["walletType"] != ws["walletType"]

    rest_sig = normalize_signal(rest, "rest")
    ws_sig = normalize_signal(ws, "ws")
    assert rest_sig.wallet_type == ws_sig.wallet_type == "1,2"
    assert evidence_hash(rest_sig) == evidence_hash(ws_sig)


def test_frozen_fixture_evidence_hash_is_unchanged_by_this_correction() -> None:
    """Over-correction lock: a payload already handled correctly must hash exactly as before.

    This literal was captured from the frozen REST fixture at `da5b59a`, BEFORE any wallet-type
    canonicalization existed. `walletType` there is the string `"1"`, which is already the canonical
    form, so the correction must be a no-op for it. If this value ever moves, the change altered
    evidence identity for previously-correct data — a finding, not a fix.
    """
    assert normalize_signal(REST, "rest").wallet_type == "1"
    assert (
        evidence_hash(normalize_signal(REST, "rest"))
        == "6c803bc40c8bda82825ef62d0c1f01030e1337f6674695435e982f9cbbe5ede0"
    )


def test_wallet_type_refuses_unknown_names_and_malformed_lists() -> None:
    """Canonicalizing must not become a licence to accept anything.

    §5.1 authorizes exactly one name (`walletType=1`, Smart Money). An unrecognized name has no
    known code, so mapping it would be a guess and passing it through would re-open the divergence
    this correction closes — it raises instead. The malformed list forms raise for the same reason:
    silently dropping an empty element would let `"1,,2"` and `"1,2"` share an identity they have
    not earned.
    """
    for bad in ("Whale", "", "   ", "1,", ",1", "1,,2", "1,Whale", "-1", "1.5", "one"):
        with pytest.raises(ValueError):
            normalize_signal({**REST, "walletType": bad}, "rest")

    # bool is an int subclass; True must not slip through as the code 1.
    with pytest.raises(ValueError):
        normalize_signal({**REST, "walletType": True}, "rest")
    with pytest.raises(ValueError):
        normalize_signal({**REST, "walletType": 1.5}, "rest")
