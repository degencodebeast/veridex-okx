import base64
import json
from decimal import Decimal

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
    MAX_COMMIT_PRICE,
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
UINT256_MAX = 2**256 - 1
AT_CEILING = f"${MAX_COMMIT_PRICE}"
JUST_ABOVE_CEILING = f"${MAX_COMMIT_PRICE + Decimal('0.000001')}"
# The shape that produced a 407-digit atomic amount from the stock middleware.
OVERSIZED_PRICE = "$" + "9" * 400


def _settings_priced(price):
    """A directly constructed X402Settings — the path that never sees the loader."""
    return X402Settings(enabled=True, pay_to="0x" + "a" * 40, price=price, network=X_LAYER_MAINNET, sync_settle=True)


def _must_not_raise(build):
    """Run ``build`` and convert any refusal into a PINNED assertion failure.

    A test whose subject simply raises fails by exception, which PKT-DEC-C23 classifies
    as detection-by-crash rather than a banked kill. Turning the refusal into an
    AssertionError is what makes "this configuration must be accepted" a property the
    suite owns rather than one it happens to notice.

    Duplicated from ``test_router.py`` rather than imported: that file belongs to another
    lane, and importing across two independently owned suites couples them.
    """
    try:
        return build()
    except Exception as error:  # noqa: BLE001 - re-raised as an assertion below
        raise AssertionError(
            f"expected this configuration to be accepted, got {type(error).__name__}: {error}"
        ) from error


def _expected_atomic(price):
    """Atomic units by exact integer arithmetic; Decimal arithmetic would round at this scale."""
    whole, _, frac = price.lstrip("$").partition(".")
    return int(whole + frac.ljust(6, "0"))


async def _advertised_atomic_amount(settings):
    """Atomic amount the stock 402 challenge advertises, via the in-process C11 adapter.

    No network client is constructed anywhere in this path.
    """
    server = build_resource_server(settings, FakeFacilitator(), is_production=False)
    server.register(X_LAYER_MAINNET, ExactEvmScheme())

    async def endpoint(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route(COMMIT_PATH, endpoint, methods=list(GATED_METHODS))])
    app.add_middleware(
        PaymentMiddlewareASGI,
        routes={f"{m} {COMMIT_PATH}": {"accepts": [build_commit_price(settings)]} for m in GATED_METHODS},
        server=server,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://boundary") as client:
        response = await client.request("GET", COMMIT_PATH)
    assert response.status_code == 402
    challenge = json.loads(base64.b64decode(response.headers["payment-required"]))
    return challenge["accepts"][0]["amount"]


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


@pytest.mark.parametrize("payout", ["0x" + "f0" * 20, "0x" + "9c" * 20])
def test_commit_price_routes_to_the_configured_payout_address(payout):
    """``pay_to`` decides WHERE THE MONEY GOES, so it must come from the configuration.

    Every other test in this file holds the payout at one accepted literal, and a
    ``build_commit_price`` that ignored ``settings.pay_to`` and returned a hard-coded
    address would satisfy all of them — an implementation routing every payment to a
    fixed address would ship green. Two distinct addresses, neither of them that
    literal, is the smallest change that makes such an implementation observable.

    One assertion by design: there is no second, redundant assertion here that a later
    edit could re-order ahead of it (PKT-DEC-C20 rule 1 / CF-5a).
    """
    settings = load_x402_settings({**PROD_ENV, "PAY_TO_ADDRESS": payout})
    assert build_commit_price(settings).pay_to == payout


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


def test_enabled_development_config_still_validates_the_price():
    """Enabled-and-not-production can charge, and is exactly what the stock 402 layer runs."""
    with pytest.raises(ValueError, match="SIGNAL_TRIALS_COMMIT_PRICE"):
        load_x402_settings({**DEV_ENV, "SIGNAL_TRIALS_COMMIT_PRICE": "free"})


def test_disabled_development_config_tolerates_an_unusable_price():
    """No gate mounts, so a junk price is inert and must not block startup."""
    s = load_x402_settings({"APP_ENV": "development", "X402_ENABLED": "false", "SIGNAL_TRIALS_COMMIT_PRICE": "free"})
    assert s.enabled is False and s.price == "free"


# --- magnitude bound: an amount the EVM gate cannot represent must never be advertised ---


@pytest.mark.parametrize("price", ["$00.01", "$01", "$000000.01", "$0000"])
def test_redundant_leading_zeros_are_refused(price):
    with pytest.raises(ValueError, match="SIGNAL_TRIALS_COMMIT_PRICE"):
        load_x402_settings({**PROD_ENV, "SIGNAL_TRIALS_COMMIT_PRICE": price})


def test_canonical_leading_zero_before_the_point_is_still_accepted():
    """Rejecting redundant zeros must not reject the default price."""
    assert load_x402_settings({**PROD_ENV, "SIGNAL_TRIALS_COMMIT_PRICE": "$0.01"}).price == "$0.01"


def test_the_ceiling_is_inclusive_on_both_paths():
    """The ACCEPT side of the boundary, banked as an assertion rather than as the absence of a crash.

    Without this, flipping ``>`` to ``>=`` is noticed only because production code raises
    through a test that never reaches an assertion.
    """
    settings = _must_not_raise(lambda: load_x402_settings({**PROD_ENV, "SIGNAL_TRIALS_COMMIT_PRICE": AT_CEILING}))
    assert settings.price == AT_CEILING
    assert _must_not_raise(lambda: build_commit_price(settings)).price == AT_CEILING
    assert _must_not_raise(lambda: build_commit_price(_settings_priced(AT_CEILING))).price == AT_CEILING


@pytest.mark.parametrize("price", ["$٥", "$1٥", "$1.٥", "$۵", "$１"])
def test_non_ascii_decimal_digits_are_refused(price):
    r"""``\d`` matches any Unicode decimal digit, and ``Decimal("1٥")`` reads as 15.

    The grammar uses ``[0-9]`` so these never reach the money path. Without this vector,
    reverting that character class is invisible to the whole suite.

    Asserted explicitly rather than with ``pytest.raises``: a context manager that reports
    ``DID NOT RAISE`` fails without any assertion of this test running, which is the
    detection-by-crash shape PKT-DEC-C23 declines to bank. ``$1٥`` and ``$1.٥`` are the
    vectors that discriminate — a leading non-ASCII digit is refused either way, because
    ``\d`` sits only in the trailing and fractional positions.
    """
    refusal = None
    try:
        load_x402_settings({**PROD_ENV, "SIGNAL_TRIALS_COMMIT_PRICE": price})
    except ValueError as error:
        refusal = error
    assert refusal is not None, f"non-ASCII digits must be refused; Decimal reads {price!r} as a number"
    assert "SIGNAL_TRIALS_COMMIT_PRICE" in str(refusal)


@pytest.mark.parametrize("price", [JUST_ABOVE_CEILING, "$1000001", OVERSIZED_PRICE])
def test_price_above_the_ceiling_is_refused_by_the_loader(price):
    with pytest.raises(ValueError, match="ceiling"):
        load_x402_settings({**PROD_ENV, "SIGNAL_TRIALS_COMMIT_PRICE": price})


@pytest.mark.parametrize("price", [JUST_ABOVE_CEILING, "$1000001", OVERSIZED_PRICE])
def test_price_above_the_ceiling_is_refused_at_construction(price):
    """The loader is not the only way in; the ceiling binds on the construction path too."""
    with pytest.raises(ValueError, match="ceiling"):
        build_commit_price(_settings_priced(price))


def test_rejected_oversized_price_is_never_echoed():
    """C6's redaction rule binds here too: a rejected value stays out of the message."""
    for raises in (
        lambda: load_x402_settings({**PROD_ENV, "SIGNAL_TRIALS_COMMIT_PRICE": OVERSIZED_PRICE}),
        lambda: build_commit_price(_settings_priced(OVERSIZED_PRICE)),
        lambda: load_x402_settings({**PROD_ENV, "SIGNAL_TRIALS_COMMIT_PRICE": JUST_ABOVE_CEILING}),
    ):
        with pytest.raises(ValueError) as exc:
            raises()
        message = str(exc.value)
        assert "9999" not in message
        assert OVERSIZED_PRICE not in message and JUST_ABOVE_CEILING.lstrip("$") not in message


@pytest.mark.parametrize("price", ["$0.000001", "$0.01", "$1", "$999999.999999", AT_CEILING])
async def test_accepted_boundary_prices_advertise_a_representable_atomic_amount(price):
    """Every price the validator accepts must survive the SDK as an exact uint256 amount.

    Guards the defect directly: the 400-digit price reached this middleware and was
    advertised as a 407-digit atomic amount that no EVM uint256 can hold.
    """
    settings = _must_not_raise(lambda: load_x402_settings({**DEV_ENV, "SIGNAL_TRIALS_COMMIT_PRICE": price}))
    amount = await _advertised_atomic_amount(settings)

    assert amount.isdigit(), f"non-integer atomic amount advertised: {amount[:40]}"
    assert int(amount) == _expected_atomic(price), "atomic amount was silently rounded"
    assert 0 < int(amount) <= UINT256_MAX


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
