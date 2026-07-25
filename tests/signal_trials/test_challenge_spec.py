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
