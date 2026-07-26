import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.mechanisms.evm.exact.server import ExactEvmScheme
from x402.server import x402ResourceServer

from veridex.signal_trials.payments import (
    COMMIT_PATH,
    FAKE_PAYER,
    FAKE_TX_HASH,
    GATED_METHODS,
    X_LAYER_MAINNET,
    FakeFacilitator,
    FakeFacilitatorClientAdapter,
    VerifiedPayment,
    X402Settings,
    build_commit_price,
    build_resource_server,
    load_x402_settings,
)

PROD_ENV = {
    "APP_ENV": "production",
    "X402_ENABLED": "true",
    "PAY_TO_ADDRESS": "0x" + "a" * 40,
}
DEV_ENV = {
    "APP_ENV": "development",
    "X402_ENABLED": "true",
    "PAY_TO_ADDRESS": "0x" + "a" * 40,
}


def test_production_requires_enabled_and_payto():
    with pytest.raises(ValueError, match="X402"):
        load_x402_settings({"APP_ENV": "production", "X402_ENABLED": "false"})


def test_price_config_and_gated_methods():
    s = load_x402_settings({"APP_ENV": "development", "X402_ENABLED": "true", "PAY_TO_ADDRESS": "0x" + "a" * 40})
    p = build_commit_price(s)
    assert p.network == "eip155:196" and set(GATED_METHODS) == {"GET", "POST"}


def test_fake_facilitator_counts_and_failure_mode():
    ok, bad = FakeFacilitator(), FakeFacilitator(fail_settlement=True)
    ok.settle({})
    assert ok.settle_calls == 1 and ok.last_settlement["status"] == "confirmed"
    bad.settle({})
    assert bad.last_settlement["status"] == "failed"


# --- negative controls: the fail-closed behavior this module exists for ---


def test_production_rejects_missing_payto():
    with pytest.raises(ValueError, match="X402"):
        load_x402_settings({"APP_ENV": "production", "X402_ENABLED": "true"})


def test_production_rejects_malformed_payto():
    with pytest.raises(ValueError, match="X402"):
        load_x402_settings({**PROD_ENV, "PAY_TO_ADDRESS": "not-an-address"})


def test_production_settings_with_fake_facilitator_raises():
    settings = load_x402_settings(PROD_ENV)
    with pytest.raises(ValueError, match="X402"):
        build_resource_server(settings, FakeFacilitator())


def test_development_allows_disabled_x402():
    s = load_x402_settings({"APP_ENV": "development", "X402_ENABLED": "false"})
    assert s.enabled is False and s.pay_to == ""


def test_commit_price_carries_pay_to_and_timeout():
    s = load_x402_settings(PROD_ENV)
    p = build_commit_price(s)
    assert p.scheme == "exact" and p.pay_to == "0x" + "a" * 40 and p.max_timeout_seconds == 300
    assert p.price == "$0.01"


# --- positive controls: the configurations build_resource_server must ALLOW ---


def test_non_production_allows_enabled_config_with_fake_facilitator():
    """x402 enabled AND a fake facilitator is an allowed pairing outside production.

    Allowed, not yet usable: the fake is wrapped into an SDK client here, and a
    scheme server still has to be registered before a 402 can be served. Those
    two properties are pinned separately below.
    """
    s = load_x402_settings(DEV_ENV)
    assert s.enabled is True
    server = build_resource_server(s, FakeFacilitator(), is_production=False)
    assert isinstance(server, x402ResourceServer)


def test_build_resource_server_supports_single_argument_call_site():
    server = build_resource_server(load_x402_settings(PROD_ENV))
    assert isinstance(server, x402ResourceServer)


def test_production_rejects_fake_facilitator_on_any_network():
    """Network must not weaken the fake-facilitator refusal.

    Matches the fake-facilitator message specifically: with ``match="X402"`` the
    unsupported-network guard alone satisfied this, so it could not detect the
    weakening it is named for.
    """
    s = X402Settings(enabled=True, pay_to="0x" + "a" * 40, price="$0.01", network="eip155:1952", sync_settle=True)
    with pytest.raises(ValueError, match="FakeFacilitator"):
        build_resource_server(s, FakeFacilitator())


def test_rejected_pay_to_value_is_never_echoed():
    """The reject branch fires when a secret may have been mispasted into the payout field."""
    mispasted_secret = "0x" + "d" * 64
    with pytest.raises(ValueError) as exc:
        load_x402_settings({**PROD_ENV, "PAY_TO_ADDRESS": mispasted_secret})
    message = str(exc.value)
    assert mispasted_secret not in message
    assert "d" * 8 not in message
    assert "66 chars" in message and "production" in message


def test_unsupported_network_is_refused():
    s = X402Settings(enabled=True, pay_to="0x" + "a" * 40, price="$0.01", network="eip155:1952", sync_settle=True)
    with pytest.raises(ValueError, match="X402"):
        build_resource_server(s, facilitator=None)


# --- commit-price validation: an unvalidated price is a gate that charges nothing ---


def test_default_commit_price_is_accepted():
    assert load_x402_settings(PROD_ENV).price == "$0.01"
    assert build_commit_price(load_x402_settings(PROD_ENV)).price == "$0.01"


@pytest.mark.parametrize(
    "price",
    [
        "$0",  # zero: a gate that mounts, challenges, and charges nothing
        "$0.00",
        "0.01",  # no currency marker
        "-1",  # negative
        "$-0.05",
        "1e9",  # exponent notation
        "$1e-3",
        "NaN",  # the SDK parses through float, so these become values, not errors
        "Infinity",
        "$0.0000001",  # seven decimals, beyond the six allowed
        "free",
        "$1,000",
    ],
)
def test_invalid_commit_price_is_refused(price):
    with pytest.raises(ValueError, match="SIGNAL_TRIALS_COMMIT_PRICE"):
        load_x402_settings({**PROD_ENV, "SIGNAL_TRIALS_COMMIT_PRICE": price})


def test_unset_or_blank_commit_price_falls_back_to_the_default():
    """Blank means unset, not invalid — the default is what gets validated."""
    for blank in ("", "   "):
        assert load_x402_settings({**PROD_ENV, "SIGNAL_TRIALS_COMMIT_PRICE": blank}).price == "$0.01"


@pytest.mark.parametrize("price", ["$0", "", "1e9", "$0.0000001", "-1"])
def test_directly_constructed_settings_cannot_bypass_price_validation(price):
    """The loader is not the only way in: build_commit_price re-checks."""
    s = X402Settings(enabled=True, pay_to="0x" + "a" * 40, price=price, network=X_LAYER_MAINNET, sync_settle=True)
    with pytest.raises(ValueError, match="SIGNAL_TRIALS_COMMIT_PRICE"):
        build_commit_price(s)


def test_rejected_commit_price_is_never_echoed():
    with pytest.raises(ValueError) as exc:
        load_x402_settings({**PROD_ENV, "SIGNAL_TRIALS_COMMIT_PRICE": "13.37-totally-bogus"})
    assert "13.37" not in str(exc.value) and "bogus" not in str(exc.value)


def test_disabled_development_config_tolerates_an_unusable_price():
    """No gate mounts, so a junk price is inert and must not block startup."""
    s = load_x402_settings({"APP_ENV": "development", "X402_ENABLED": "false", "SIGNAL_TRIALS_COMMIT_PRICE": "free"})
    assert s.enabled is False and s.price == "free"


# --- the frozen fake's own surface (properties its comments call load-bearing) ---


def test_loader_always_settles_synchronously():
    """Synchronous settlement is what stops an unsettled commit returning 200."""
    assert load_x402_settings(PROD_ENV).sync_settle is True
    assert load_x402_settings({"APP_ENV": "development", "X402_ENABLED": "false"}).sync_settle is True


def test_fake_verify_counts_the_call_and_returns_the_fake_payer():
    fake = FakeFacilitator()
    verified = fake.verify({})
    assert fake.verify_calls == 1
    assert isinstance(verified, VerifiedPayment)
    assert verified.payer == FAKE_PAYER


def test_failed_settlement_carries_no_transaction_hash():
    """A failed settlement must not carry a plausible hash — that would forge a receipt."""
    fake = FakeFacilitator(fail_settlement=True)
    fake.settle({})
    assert fake.last_settlement == {"status": "failed", "tx_hash": ""}
    assert FAKE_TX_HASH not in fake.last_settlement.values()


# --- the SDK adapter: the seam that makes the frozen fake usable by the stock middleware ---


async def test_adapter_delegates_every_call_to_the_underlying_fake():
    """The fake's counters stay the oracle when the adapter is in the path."""
    fake = FakeFacilitator()
    adapter = FakeFacilitatorClientAdapter(fake)

    verified = await adapter.verify({}, None)
    settled = await adapter.settle({}, None)

    assert fake.verify_calls == 1 and fake.settle_calls == 1
    assert verified.is_valid is True and verified.payer == FAKE_PAYER
    assert settled.success is True and settled.transaction == FAKE_TX_HASH
    assert fake.last_settlement == {"status": "confirmed", "tx_hash": FAKE_TX_HASH}


async def test_adapter_surfaces_a_failed_settlement_honestly():
    fake = FakeFacilitator(fail_settlement=True)
    settled = await FakeFacilitatorClientAdapter(fake).settle({}, None)
    assert settled.success is False
    assert settled.transaction == ""
    assert fake.settle_calls == 1 and fake.last_settlement["status"] == "failed"


def test_non_production_wraps_the_fake_so_the_server_can_initialize():
    """An unwrapped fake makes initialize() raise AttributeError on get_supported."""
    server = build_resource_server(load_x402_settings(DEV_ENV), FakeFacilitator(), is_production=False)
    server.initialize()


def test_production_rejects_the_fake_and_anything_wrapping_it():
    """The adapter is not a FakeFacilitator, so it must be refused on its own terms."""

    class UnknownWrapper:
        def __init__(self, fake):
            self.fake = fake

    settings = load_x402_settings(PROD_ENV)
    for facilitator in (
        FakeFacilitator(),
        FakeFacilitatorClientAdapter(FakeFacilitator()),
        UnknownWrapper(FakeFacilitator()),
    ):
        with pytest.raises(ValueError, match="FakeFacilitator"):
            build_resource_server(settings, facilitator)


async def test_stock_402_layer_emits_challenge_on_both_methods():
    """The pairing the plan assigns H1.2: stock middleware + commit price + fake facilitator."""
    settings = load_x402_settings(DEV_ENV)
    server = build_resource_server(settings, FakeFacilitator(), is_production=False)
    server.register(X_LAYER_MAINNET, ExactEvmScheme())

    async def endpoint(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route(COMMIT_PATH, endpoint, methods=list(GATED_METHODS))])
    app.add_middleware(
        PaymentMiddlewareASGI,
        routes={f"{method} {COMMIT_PATH}": {"accepts": [build_commit_price(settings)]} for method in GATED_METHODS},
        server=server,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://stock-402") as client:
        for method in GATED_METHODS:
            response = await client.request(method, COMMIT_PATH)
            assert response.status_code == 402
            assert "payment-required" in {name.lower() for name in response.headers}
