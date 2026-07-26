"""x402 payment configuration for the signal-trials commit route.

Fail-closed by construction: a production ``APP_ENV`` may not run with x402
disabled, without a valid payout address, or against the in-memory
:class:`FakeFacilitator`; and no configuration that can charge — production or
merely enabled — may carry an invalid commit price.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# The x402 SDK (0.1.1) ships no ``py.typed`` marker, so mypy cannot analyze it.
# Ignored at the import site rather than via a ``pyproject.toml`` override so the
# suppression stays scoped to this module and to these two names.
from x402.http import PaymentOption  # type: ignore[import-untyped]
from x402.schemas.responses import (  # type: ignore[import-untyped]
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)
from x402.server import x402ResourceServer  # type: ignore[import-untyped]

COMMIT_PATH = "/signal-trials/commit"
# GET is gated alongside POST: the OKX review probe issues a GET against the
# commit path and must receive a 402, not a 200.
GATED_METHODS = ("GET", "POST")

# X Layer mainnet. The testnet chain id (1952) is unverified, so mainnet is the
# only network this route is configured for.
X_LAYER_MAINNET = "eip155:196"
DEFAULT_COMMIT_PRICE = "$0.01"
COMMIT_MAX_TIMEOUT_SECONDS = 300

# Documented product ceiling for a single commit, enforced before the price ever
# reaches the SDK. The hazard above it is silent rather than loud, and it starts
# far lower than a float overflow would suggest: the advertised amount is built on
# the SDK's DECIMAL path (``parse_amount(parse_money_to_string(price), 6)`` at
# ``mechanisms/evm/exact/server.py:188``), which rounds at 28 significant digits —
# from roughly 1e22 for a six-decimal price. A 400-digit price is advertised as a
# 407-digit atomic amount no EVM ``uint256`` can hold, and that string contains no
# infinity. ``float`` is NOT the cause: ``parse_money_to_decimal`` does return
# ``inf``, but it feeds only ``_money_parsers``, which starts empty and which this
# project never populates, so the ``inf`` is discarded. A ceiling of one million
# dollars keeps the atomic amount at 13 digits: 15 digits inside the rounding
# frontier and 65 inside ``uint256``, while still being a hundred million times
# the default price, so it constrains no real configuration.
MAX_COMMIT_PRICE = Decimal("1000000")

# Deterministic stand-ins returned by FakeFacilitator. Synthetic constants, not
# credentials: no real payer ever has this address and no chain has this hash.
FAKE_PAYER = "0x" + "b" * 40
FAKE_TX_HASH = "0x" + "c" * 64

# Mirrors the fail-closed rule in ``veridex/config.py``: only these EXPLICIT
# values are non-production. Any other value — ``prod``, ``staging``, a typo —
# is treated as production so a misspelling can never downgrade the money path.
_NON_PRODUCTION_APP_ENVS = frozenset({"development", "dev", "test", "local"})
# Networks this route may be served on. Asserted positively in
# ``build_resource_server`` so an unrecognized network fails closed.
_ALLOWED_NETWORKS = frozenset({X_LAYER_MAINNET})
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_EVM_ADDRESS = re.compile(r"\A0x[0-9a-fA-F]{40}\Z")
# Canonical USD price: a leading ``$``, an integer part with no redundant leading
# zero, and at most six fractional places. Exponent notation, ``NaN``,
# ``Infinity``, signs, separators and excess precision all fail to match rather
# than being parsed and inspected. ``[0-9]`` rather than ``\d`` so non-ASCII
# decimal digits are excluded too — "plain base-10" is meant literally.
_COMMIT_PRICE = re.compile(r"\A\$(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?\Z")


@dataclass(frozen=True)
class X402Settings:
    """Resolved x402 configuration for the signal-trials commit route."""

    enabled: bool
    pay_to: str
    price: str
    network: str
    sync_settle: bool


@dataclass(frozen=True)
class VerifiedPayment:
    """The fake's deliberately simplified verify result.

    Not the SDK's type. ``x402.schemas.responses.VerifyResponse`` does exist in
    0.1.1 and also carries ``payer``, but it additionally models validity fields
    the frozen fake does not. :class:`FakeFacilitatorClientAdapter` translates
    this into a real ``VerifyResponse`` at the SDK boundary.
    """

    payer: str


def _redact(value: str) -> str:
    """Describe a possibly-secret value without reproducing any of it.

    The ``PAY_TO_ADDRESS`` rejection fires exactly when an operator may have
    pasted a private key or funding-wallet secret into the payout field, so the
    value must never reach an exception message, a log, or a crash report. The
    length alone separates the cases worth telling apart: empty, truncated, and
    key-shaped (a 0x-prefixed 32-byte key is 66 characters, an address is 42).
    """
    if not value:
        return "<empty>"
    return f"<redacted, {len(value)} chars>"


def _validate_commit_price(price: str) -> None:
    """Refuse any price that is not a positive, canonical, in-range USD amount.

    The SDK parses prices through ``float``, so left unchecked it accepts values
    that quietly break the gate rather than failing: ``"$0"`` mounts a route that
    charges nothing but reports itself paid, ``"-1"`` yields a negative charge,
    ``"NaN"``/``"Infinity"`` become float values instead of errors, and a
    400-digit price is advertised as a 407-digit atomic amount no EVM ``uint256``
    can hold — that one comes from the SDK's Decimal path rounding at 28
    significant digits, not from the float overflow far above it. Matching a
    strict pattern first, then comparing as :class:`~decimal.Decimal`, means none
    of those states is ever constructed.

    The magnitude check compares rather than multiplies. At this ceiling the two
    forms are equivalent — no input distinguishes them — so that is defensive
    style rather than behaviour: ``Decimal(str)`` construction is exact regardless
    of context, but arithmetic is not, and a multiplying form rounds under the
    default 28-digit context. The false pass that habit caught was in a probe's
    own expectation, not in this guard.

    Called from two places on purpose. :func:`load_x402_settings` guards the
    *configuration* an operator supplies; :func:`build_commit_price` guards
    *construction*, since a caller that builds :class:`X402Settings` by hand
    never passes through the loader at all.

    Args:
        price: The configured price string.

    Raises:
        ValueError: ``price`` is not a positive canonical USD amount, or it
            exceeds :data:`MAX_COMMIT_PRICE`.
    """
    # Neither message names the offending value, exactly as _redact keeps a
    # rejected PAY_TO_ADDRESS out of it: a rejected config value should not be
    # copied into an exception, a log, or a crash report.
    if not _COMMIT_PRICE.match(price) or Decimal(price[1:]) <= 0:
        raise ValueError(
            "X402 requires a valid SIGNAL_TRIALS_COMMIT_PRICE: a positive USD amount in plain "
            'decimal notation, no leading zeros, at most six decimal places, for example "$0.01". '
            "The configured value is withheld from this message."
        )
    if Decimal(price[1:]) > MAX_COMMIT_PRICE:
        raise ValueError(
            f"X402 refuses a SIGNAL_TRIALS_COMMIT_PRICE above the ${MAX_COMMIT_PRICE} ceiling, which keeps "
            "the atomic amount exactly representable by the six-decimal asset. "
            "The configured value is withheld from this message."
        )


def load_x402_settings(env: Mapping[str, str]) -> X402Settings:
    """Resolve x402 settings from ``env``, failing closed in production.

    Args:
        env: Environment mapping to read configuration from.

    Returns:
        The resolved settings.

    Raises:
        ValueError: ``APP_ENV`` is production-equivalent and x402 is disabled or
            ``PAY_TO_ADDRESS`` is missing/malformed; or the configuration can
            charge and ``SIGNAL_TRIALS_COMMIT_PRICE`` is not a valid price.
    """
    app_env = env.get("APP_ENV", "development").strip().casefold()
    is_production = app_env not in _NON_PRODUCTION_APP_ENVS
    enabled = env.get("X402_ENABLED", "false").strip().casefold() in _TRUTHY
    pay_to = env.get("PAY_TO_ADDRESS", "").strip()
    price = env.get("SIGNAL_TRIALS_COMMIT_PRICE", "").strip() or DEFAULT_COMMIT_PRICE

    if is_production:
        if not enabled:
            raise ValueError(f"X402 must be enabled (X402_ENABLED=true) when APP_ENV is {app_env!r}")
        if not _EVM_ADDRESS.match(pay_to):
            # ``app_env`` is echoed deliberately — a non-secret label that makes a
            # misspelling-induced production classification diagnosable. The
            # address is not: see :func:`_redact`.
            raise ValueError(f"X402 requires a valid PAY_TO_ADDRESS when APP_ENV is {app_env!r}; got {_redact(pay_to)}")

    # Only when the price can actually be charged. A disabled development config
    # mounts no gate, so a junk price there is inert and must not block startup.
    if enabled or is_production:
        _validate_commit_price(price)

    return X402Settings(
        enabled=enabled,
        pay_to=pay_to,
        price=price,
        network=X_LAYER_MAINNET,
        # Settle before responding so the commit receipt carries a real tx hash;
        # settling asynchronously would let an unsettled commit return 200.
        sync_settle=True,
    )


def build_commit_price(settings: X402Settings) -> PaymentOption:
    """Build the x402 payment option that prices the commit route.

    Raises:
        ValueError: ``settings.price`` is not a positive canonical USD amount.
            Re-checked here rather than trusted from the loader, because a
            directly constructed :class:`X402Settings` never went through it.
    """
    _validate_commit_price(settings.price)
    return PaymentOption(
        scheme="exact",
        pay_to=settings.pay_to,
        price=settings.price,
        network=settings.network,
        max_timeout_seconds=COMMIT_MAX_TIMEOUT_SECONDS,
    )


class FakeFacilitator:
    """In-memory settlement double for offline tests. Moves no money.

    This is a veridex-level double, **not** an SDK ``FacilitatorClient``. Its
    surface is synchronous, single-argument, and returns veridex types, and it
    has no ``get_supported()``, so handing it to ``x402ResourceServer`` directly
    raises ``AttributeError`` the first time the server initializes.
    :class:`FakeFacilitatorClientAdapter` is the seam that makes it usable;
    :func:`build_resource_server` applies that seam outside production and
    refuses the fake and the adapter alike in production.
    """

    def __init__(self, fail_settlement: bool = False) -> None:
        """Create a fake facilitator.

        Args:
            fail_settlement: When true, every settle reports ``"failed"``.
        """
        self.fail_settlement = fail_settlement
        self.verify_calls = 0
        self.settle_calls = 0
        self.last_settlement: dict[str, str] | None = None

    def verify(self, payload: Mapping[str, Any]) -> VerifiedPayment:
        """Accept ``payload`` unconditionally and record the call."""
        self.verify_calls += 1
        return VerifiedPayment(payer=FAKE_PAYER)

    def settle(self, payload: Mapping[str, Any]) -> dict[str, str]:
        """Record a settlement attempt and return its synthetic result."""
        self.settle_calls += 1
        if self.fail_settlement:
            # Empty hash on failure: nothing settled, so there is no transaction
            # to point at. A placeholder here would look like a real receipt.
            self.last_settlement = {"status": "failed", "tx_hash": ""}
        else:
            self.last_settlement = {"status": "confirmed", "tx_hash": FAKE_TX_HASH}
        return self.last_settlement


class FakeFacilitatorClientAdapter:
    """Presents a :class:`FakeFacilitator` as an SDK ``FacilitatorClient``.

    The fake's frozen surface and the SDK protocol (``x402/server_base.py:48-69``)
    disagree on every member: the protocol wants async two-argument ``verify`` and
    ``settle`` returning ``VerifyResponse``/``SettleResponse``, plus a
    ``get_supported()`` the fake lacks entirely. This adapter is the seam between
    them, so the frozen surface stays frozen and the stock middleware can still
    initialize and emit a real 402.

    Every call delegates to the wrapped fake, so ``verify_calls``,
    ``settle_calls`` and ``last_settlement`` remain the test oracle, and
    ``fail_settlement=True`` still surfaces as an unsuccessful settlement.

    Test-only, exactly like the fake it wraps: :func:`build_resource_server`
    refuses it in production.
    """

    def __init__(self, fake: FakeFacilitator) -> None:
        """Wrap ``fake`` in the SDK's facilitator-client shape."""
        self.fake = fake

    def get_supported(self) -> SupportedResponse:
        """Advertise the single payment kind this route serves.

        Synchronous on purpose. ``x402ResourceServer.initialize`` calls this
        *without* awaiting it (``server_base.py:260``), so an async version would
        hand back a coroutine and fail with ``'coroutine' object has no attribute
        'kinds'``.
        """
        return SupportedResponse(kinds=[SupportedKind(x402_version=2, scheme="exact", network=X_LAYER_MAINNET)])

    async def verify(self, payload: Any, requirements: Any) -> VerifyResponse:
        """Delegate to the fake and translate its result into the SDK shape."""
        verified = self.fake.verify(payload)
        return VerifyResponse(is_valid=True, payer=verified.payer)

    async def settle(self, payload: Any, requirements: Any) -> SettleResponse:
        """Delegate to the fake, carrying a failed settlement through honestly."""
        result = self.fake.settle(payload)
        settled = result["status"] == "confirmed"
        return SettleResponse(
            success=settled,
            transaction=result["tx_hash"],
            network=X_LAYER_MAINNET,
            payer=FAKE_PAYER,
            error_reason=None if settled else "settlement_failed",
        )


def _is_test_double(facilitator: object) -> bool:
    """Report whether ``facilitator`` is the fake, its adapter, or a direct holder of a bare fake.

    An ``isinstance(facilitator, FakeFacilitator)`` check alone stopped being
    sufficient the moment an adapter existed: the adapter is not a
    ``FakeFacilitator``, so passing it directly would launder the fake straight
    past the production guard.

    The final clause adds exactly one further shape — an attribute literally
    named ``fake`` holding a **bare** ``FakeFacilitator``. Subclasses of either
    type are covered too, since ``isinstance`` follows inheritance.

    Known residual, accepted for H1.1: the check is name-dependent and one level
    deep. A wrapper that stores the fake under any other name (``_fake``,
    ``inner``, inside a list, captured in a closure) is NOT refused, and neither
    is ``self.fake = FakeFacilitatorClientAdapter(...)`` — the attribute name is
    right but an adapter is not a ``FakeFacilitator``. No production path builds
    such a wrapper; reaching the gap takes authoring a novel class in-repo and
    passing ``is_production=True``.
    """
    if isinstance(facilitator, FakeFacilitator | FakeFacilitatorClientAdapter):
        return True
    return isinstance(getattr(facilitator, "fake", None), FakeFacilitator)


def build_resource_server(
    settings: X402Settings,
    facilitator: Any = None,
    *,
    is_production: bool = True,
) -> x402ResourceServer:
    """Build the x402 resource server for the commit route.

    Production-ness arrives as a parameter because :class:`X402Settings` carries
    exactly five fields and cannot hold ``APP_ENV``. It defaults to ``True`` so a
    caller that forgets the flag gets the strictest treatment; a test that wants a
    :class:`FakeFacilitator` must opt out of production explicitly.

    The guard deliberately does NOT key off ``settings.enabled``. The stock 402
    layer mounts only when ``X402_ENABLED=true`` and injects a
    :class:`FakeFacilitator` in its tests, so refusing "enabled + fake" would
    refuse a pairing the plan requires. Production already implies enabled:
    :func:`load_x402_settings` raises otherwise.

    ``facilitator`` defaults to ``None`` — the SDK's own default — so the
    documented one-argument call site ``build_resource_server(settings)`` works;
    callers serving real traffic inject the real facilitator there.

    Outside production a bare :class:`FakeFacilitator` is wrapped in
    :class:`FakeFacilitatorClientAdapter`, because the fake is not an SDK
    ``FacilitatorClient`` and an unwrapped one makes the server raise
    ``AttributeError`` on its first request.

    Args:
        settings: Resolved x402 configuration.
        facilitator: Facilitator client to settle through. ``None`` leaves the
            server with no facilitator client at all.
        is_production: Whether this server will handle real money.

    Returns:
        An x402 resource server holding ``facilitator`` as its facilitator
        client. It is not yet able to serve a 402: the caller must still
        register a scheme server for the network — ``server.register(...)`` —
        which is H1.2's responsibility, not this factory's.

    Raises:
        ValueError: a :class:`FakeFacilitator`, or anything wrapping one, was
            paired with a production configuration; or ``settings.network`` is
            unsupported.
    """
    if _is_test_double(facilitator):
        if is_production:
            raise ValueError(
                "X402 refuses FakeFacilitator (or any adapter wrapping it) in production; "
                "a fake must never back a real payment gate"
            )
        if isinstance(facilitator, FakeFacilitator):
            facilitator = FakeFacilitatorClientAdapter(facilitator)
    # A positive assertion rather than a conjunct on the guard above: as an
    # AND-term, a network mismatch would have WEAKENED the refusal instead of
    # strengthening it, letting a hand-built non-mainnet config pair with a fake.
    if settings.network not in _ALLOWED_NETWORKS:
        raise ValueError(
            f"X402 refuses unsupported network {settings.network!r}; supported: {sorted(_ALLOWED_NETWORKS)}"
        )
    return x402ResourceServer(facilitator)
