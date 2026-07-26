"""x402 payment configuration for the signal-trials commit route.

Fail-closed by construction: a production ``APP_ENV`` may not run with x402
disabled, without a valid payout address, or against the in-memory
:class:`FakeFacilitator`; and no configuration that can charge — production or
merely enabled — may carry an invalid commit price.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# The x402 SDK (0.1.1) ships no ``py.typed`` marker, so mypy cannot analyze it.
# Ignored at the import site rather than via a ``pyproject.toml`` override so the
# suppression stays scoped to this module and to these two names.
from x402.http import PaymentOption  # type: ignore[import-untyped]
from x402.http.constants import (  # type: ignore[import-untyped]
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
)
from x402.http.utils import (  # type: ignore[import-untyped]
    decode_payment_signature_header,
    encode_payment_required_header,
    encode_payment_response_header,
)
from x402.schemas.responses import (  # type: ignore[import-untyped]
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)
from x402.server import x402ResourceServer  # type: ignore[import-untyped]

from veridex.api.signal_trials_schemas import CommitRequest
from veridex.signal_trials.live import LiveTrial, LiveTrialRepository, handle_commit
from veridex.signal_trials.receipts import INDETERMINATE_STATES, ReceiptStore

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

# Distinguishes "no verify_payer configured" from an explicitly configured ``None`` or ``""``.
# A plain ``None`` default could not tell the two apart, and telling them apart is the point:
# ``VerifyResponse.payer`` is typed ``str | None``, so a facilitator returning no payer at all
# is a state the fail-closed guard has to be drivable into.
_USE_FAKE_PAYER: Any = object()

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

    Left unchecked, the SDK mishandles these four in two different ways, and the
    difference matters to anyone reasoning about it later.

    Two break the gate *quietly*: ``"$0"`` mounts a route that charges nothing but
    reports itself paid, and ``"-1"`` becomes an atomic ``-1000000``. A third is
    quiet and much larger — a 400-digit price is advertised as a 407-digit atomic
    amount no EVM ``uint256`` can hold, from the SDK's Decimal path rounding at 28
    significant digits, not from the float overflow far above it.

    ``"NaN"`` and ``"Infinity"`` do NOT break it quietly: they raise on that same
    Decimal path, at ``parse_amount`` (``ValueError`` and ``OverflowError``). They
    are still refused here because a startup refusal beats raising per-request
    while the route is already serving 402s.

    Matching a strict pattern first, then comparing as
    :class:`~decimal.Decimal`, means none of those states is ever constructed.

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


def load_x402_settings(env: Mapping[str, str], *, is_production: bool | None = None) -> X402Settings:
    """Resolve x402 settings from ``env``, failing closed in production.

    ``is_production`` is an EXPLICIT PARAMETER rather than something a caller has to smuggle in
    through ``env``. When a caller has already resolved production-ness from somewhere other
    than ``APP_ENV`` — a ``Settings`` object, a deployment flag — it passes the answer here, and
    the mapping keeps reporting the operator's ACTUAL ``APP_ENV``.

    That distinction is the whole reason the parameter exists, and it is worth being precise
    about. The previous shape was for the caller to synthesize ``{**env, "APP_ENV":
    "production"}``. It reaches the right verdict and it is LOSSY: it overwrites the value the
    diagnostics below quote, so an operator who typed ``APP_ENV=prodction`` — classified as
    production by the fail-closed rule, which is correct — was told their environment was
    ``'production'``. The one message able to explain WHY a development-looking deployment
    started enforcing production rules instead confirmed a value nobody had set. The echo at the
    ``PAY_TO_ADDRESS`` rejection exists specifically to make that misspelling diagnosable, and
    synthesis silently removed the only information it carried.

    ``None`` means "derive it from ``APP_ENV``", which is itself fail-closed: every value
    outside :data:`_NON_PRODUCTION_APP_ENVS` — ``prod``, ``staging``, a typo — reads as
    production, so a misspelling can never downgrade the money path.

    Args:
        env: Environment mapping to read configuration from.
        is_production: The resolved production decision, or ``None`` to derive it from
            ``env["APP_ENV"]``.

    Returns:
        The resolved settings.

    Raises:
        ValueError: The configuration is production and x402 is disabled or ``PAY_TO_ADDRESS``
            is missing/malformed; or the configuration can charge and
            ``SIGNAL_TRIALS_COMMIT_PRICE`` is not a valid price.
    """
    # Reported verbatim in the diagnostics below, never overwritten by the caller's verdict.
    app_env = env.get("APP_ENV", "development").strip().casefold()
    if is_production is None:
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

    def __init__(
        self,
        fail_settlement: bool = False,
        *,
        settle_delay_ms: int = 0,
        raise_transport_error: bool = False,
        verify_payer: Any = _USE_FAKE_PAYER,
        verify_is_valid: bool = True,
    ) -> None:
        """Create a fake facilitator.

        The H1.1 surface is preserved EXACTLY: ``fail_settlement`` stays the sole positional
        argument, and ``verify``, ``settle``, ``verify_calls``, ``settle_calls`` and
        ``last_settlement`` are unchanged in name, signature and behaviour. The three additions
        below are keyword-only with defaults that reproduce the H1.1 behaviour byte for byte, so
        every existing call site and every existing assertion is unaffected. They exist because
        H4.1's frozen test block drives three states the two-outcome fake could not represent —
        a slow settlement, a settlement whose OUTCOME IS UNKNOWN, and a facilitator that
        verifies a payment without naming a payer.

        Args:
            fail_settlement: When true, every settle reports ``"failed"``. This is a DEFINITIVE
                returned failure: the facilitator answered, and the answer was no.
            settle_delay_ms: How long the SDK-facing adapter waits before delegating, so two
                concurrent commits genuinely overlap instead of running back to back. Awaited in
                the adapter rather than slept here, because this method is synchronous by
                freeze and a blocking sleep would serialize the event loop — which would make a
                concurrency test pass while exercising no concurrency at all.
            raise_transport_error: When true, settle records the attempt and then RAISES. This
                is categorically different from ``fail_settlement``: the request was sent and no
                answer came back, so whether money moved is unknown and unknowable from here.
            verify_payer: The payer the adapter reports at the SDK boundary. Defaults to
                :data:`FAKE_PAYER`; an explicit ``None`` or ``""`` drives the fail-closed guard.
            verify_is_valid: The ``is_valid`` flag the adapter reports. ``False`` models the
                facilitator REFUSING the payment while still naming a payer, which the SDK's
                response shape permits — so a caller that read ``payer`` without consulting
                ``is_valid`` would treat a rejected payment as a verified one.
        """
        self.fail_settlement = fail_settlement
        self.verify_calls = 0
        self.settle_calls = 0
        self.last_settlement: dict[str, str] | None = None
        self.settle_delay_ms = settle_delay_ms
        self.raise_transport_error = raise_transport_error
        self.verify_payer = FAKE_PAYER if verify_payer is _USE_FAKE_PAYER else verify_payer
        self.verify_is_valid = verify_is_valid
        #: The requirements the LAST settlement was requested against. Recorded by the adapter,
        #: because the frozen single-argument ``settle`` below never sees them — and they carry
        #: ``pay_to``, which decides where the money goes and is therefore worth an oracle.
        self.last_settled_requirements: Any = None

    def verify(self, payload: Mapping[str, Any]) -> VerifiedPayment:
        """Accept ``payload`` unconditionally and record the call."""
        self.verify_calls += 1
        return VerifiedPayment(payer=FAKE_PAYER)

    def settle(self, payload: Mapping[str, Any]) -> dict[str, str]:
        """Record a settlement attempt and return its synthetic result.

        The counter is incremented BEFORE the transport error is raised, and that order is the
        honest model of the failure: the attempt was made, the request may have reached the
        facilitator, and the caller has no way to learn whether it settled. A fake that raised
        without counting would describe a request that was never sent, which is the one case
        this state is not.

        Raises:
            ConnectionError: ``raise_transport_error`` is set. Not caught by the store's own
                error handling anywhere; the wrapper is required to read it as INDETERMINATE.
        """
        self.settle_calls += 1
        if self.raise_transport_error:
            self.last_settlement = None
            raise ConnectionError("simulated facilitator transport failure after the request was sent")
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
        """Delegate to the fake and translate its result into the SDK shape.

        The fake's own ``verify`` still returns ``VerifiedPayment(payer=FAKE_PAYER)``, whose
        ``payer`` is typed ``str`` and stays non-optional. The payer that reaches the SDK
        response is ``fake.verify_payer``, which may be ``None`` or ``""`` — and it is CORRECT
        for that to be possible only here, because this is the boundary where the SDK's
        ``str | None`` type lives. Making the frozen veridex type carry the optionality would
        push a payment-layer concern into a value object that has no business with it.
        """
        self.fake.verify(payload)
        return VerifyResponse(is_valid=self.fake.verify_is_valid, payer=self.fake.verify_payer)

    async def settle(self, payload: Any, requirements: Any) -> SettleResponse:
        """Delegate to the fake, carrying a failed or indeterminate settlement through honestly.

        The requirements are recorded on the FAKE, not held here, so the fake stays the single
        test oracle exactly as ``PKT-DEC-C11`` requirement 4 intends. They are recorded BEFORE
        delegating, so a settlement that raises still leaves evidence of what it was asked to do
        — which is the case where that evidence matters most.
        """
        self.fake.last_settled_requirements = requirements
        if self.fake.settle_delay_ms:
            await asyncio.sleep(self.fake.settle_delay_ms / 1000)
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


def _settle_outcome_is_definitive_failure(settled: SettleResponse) -> bool:
    """Report whether ``settled`` is a PROVEN settlement failure.

    A separate named predicate because the whole design turns on this one question, and the
    answer is only ever yes when the facilitator RETURNED and said no. An exception never
    reaches here: it is caught by the caller and treated as indeterminate, because a raised
    settle is absence of evidence rather than evidence of absence. Reading a timeout as a
    failure is how a real payment loses its record.
    """
    return not settled.success


class SignalTrialsPaymentASGI:
    """ASGI wrapper that makes the commit route settlement-atomic.

    Mount it AROUND the composed application — the analogue of ``guard.app``, never of a guard
    object that merely delegates to it. Middleware attached to a delegating wrapper is not in
    the request path at all, so it would gate nothing while appearing to be installed.

    **The SDK's verify and settle functions are called DIRECTLY, and no resource-server hook is
    ever registered.** That is not stylistic. ``x402ResourceServerBase._settle_payment_core``
    runs its after-settle hooks INSIDE the ``try`` block that guards the facilitator call, so an
    exception raised by an ``on_after_settle`` hook lands in the ``except`` that then invokes the
    settle-FAILURE hooks. A journal-or-finalize step implemented as an after-settle hook could
    therefore trigger the failure path — deleting records — after a settlement that really
    happened. Orchestrating in this class instead removes the hazard by construction rather than
    by being careful inside a hook.

    The order of operations is fixed by what each step proves, not by convenience:

    ==== =============================== ==================================================
    step action                          why it is where it is
    ==== =============================== ==================================================
    1    402 challenge when unpaid       GET and POST alike, so the review probe meets the
                                         paywall rather than a 200
    2    VERIFY, then ``payer``          fail-closed on ``None``/``""``: an empty payer would
                                         key every commit to one shared decision slot
    3    ACQUIRE the slot                a single atomic ``O_CREAT | O_EXCL``, so two
                                         concurrent commits cannot both proceed
    4    VALIDATE the request            a 4xx here releases the slot: settlement was
                                         provably never reached
    5    STAGE                           the payload exists before anything points at it
    6    MARK ``settle_attempted``       fsynced BEFORE settle, so a crash past this point
                                         is never misread as "never settled"
    7    SETTLE                          three outcomes, not two
    8    JOURNAL then FINALIZE           the proof is durable before the bookkeeping
    ==== =============================== ==================================================

    Step 7 is the reason the class exists:

    * **returned failure** -> delete the staged row, RELEASE the slot, ``402``. Zero records of
      any kind, which is what frozen spec section 12 requires of a failed payment.
    * **raised or timed out** -> change NOTHING. The slot stays ``settle_attempted`` for the
      reconciler to quarantine, and the answer is ``502 payment_indeterminate``. Never a
      deletion, because this is not a proven failure.
    * **returned success** -> journal, then finalize. After this point no failure path exists:
      a journal or finalize error still answers ``200``, because the money has moved and telling
      the payer otherwise would be a lie the store can already repair.
    """

    def __init__(
        self,
        app: Any,
        *,
        server: x402ResourceServer,
        settings: X402Settings,
        store: ReceiptStore,
        live_trials: LiveTrialRepository,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        """Wrap ``app``, gating its commit route.

        Args:
            app: The composed ASGI application to wrap.
            server: An x402 resource server with a facilitator bound and a scheme registered.
            settings: Resolved, validated x402 configuration.
            store: The two-phase commit store.
            live_trials: The published open-trial repository, used to resolve a commit's trial.
            now_ms: Clock returning epoch milliseconds. Injected so a deadline is testable
                without waiting; defaults to the real clock.

        Raises:
            ValueError: ``settings.sync_settle`` is false. CONSUMED here rather than forwarded:
                the entire design depends on settlement completing before the ``200`` is
                emitted, because the receipt carries a real transaction hash and the slot
                reaches ``finalized`` only after money moved. Settling asynchronously would let
                an unsettled commit be published as a paid one, so construction fails closed
                instead of mounting a gate that can lie.
        """
        if not settings.sync_settle:
            raise ValueError(
                "SignalTrialsPaymentASGI requires sync_settle=True: the commit receipt carries a "
                "real transaction hash and the decision slot is finalized only after settlement, "
                "so an asynchronous settle would publish an unsettled commit as paid"
            )
        self.app = app
        self.server = server
        self.settings = settings
        self.store = store
        self.live_trials = live_trials
        self._now_ms = now_ms if now_ms is not None else (lambda: int(time.time() * 1000))
        self._requirements: list[Any] | None = None

    # ------------------------------------------------------------------ ASGI plumbing

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Gate the commit route; pass everything else through untouched.

        Evidence reads are free by frozen spec section 11, so anything that is not the gated
        commit route reaches the wrapped app with its ``receive`` channel UNCONSUMED. The body is
        buffered only on the path this class answers itself, which is also the only path where
        it never delegates — so a buffered request is never replayed and a passed-through one is
        never touched.
        """
        if scope.get("type") != "http" or scope.get("path") != COMMIT_PATH:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        if method not in GATED_METHODS:
            await self.app(scope, receive, send)
            return
        await self._handle_commit_request(scope, receive, send, method=method)

    @staticmethod
    def _header(scope: Any, name: str) -> str | None:
        """Return a request header by lowercase name.

        ASGI delivers headers as a list of raw ``(bytes, bytes)`` pairs with names already
        lowercased by the server, but the comparison lowercases again rather than trusting that:
        ``PAYMENT-SIGNATURE`` is the one header whose absence means "unpaid", so a case mismatch
        would silently turn a paid request into a 402 challenge.
        """
        target = name.lower().encode("latin-1")
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        for key, value in headers:
            if key.lower() == target:
                return value.decode("latin-1")
        return None

    @staticmethod
    async def _read_body(receive: Any) -> bytes:
        """Buffer the full request body from the ASGI receive channel."""
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    @staticmethod
    async def _send_json(send: Any, status: int, payload: dict[str, Any], extra: dict[str, str] | None = None) -> None:
        """Emit a JSON response with ``status`` and any extra headers."""
        body = json.dumps(payload).encode("utf-8")
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("latin-1"))]
        for key, value in (extra or {}).items():
            headers.append((key.encode("latin-1"), value.encode("latin-1")))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    # ------------------------------------------------------------------ x402 challenge

    def _payment_requirements(self) -> list[Any]:
        """Build the route's payment requirements once, initializing the server on first use.

        Lazy for the same reason the stock middleware is lazy: ``initialize`` interrogates the
        facilitator, and doing that at import or mount time would make composing an app depend
        on a facilitator being reachable. Cached because the requirements are a pure function of
        the settings, which are frozen.
        """
        if self._requirements is None:
            self.server.initialize()
            self._requirements = list(self.server.build_payment_requirements(build_commit_price(self.settings)))
        return self._requirements

    async def _send_challenge(self, send: Any, error: str) -> None:
        """Emit the 402 challenge, carrying the requirements in ``PAYMENT-REQUIRED``.

        The body names only a stable machine-readable code. Nothing the caller supplied is
        echoed — not the trial id, not the body — because this is the one response an
        unauthenticated caller can provoke at will, and it is where an echoed value would end up
        in somebody else's logs.
        """
        requirements = self._payment_requirements()
        challenge = self.server.create_payment_required_response(requirements, error=error)
        await self._send_json(
            send,
            402,
            {"error": error},
            {PAYMENT_REQUIRED_HEADER: encode_payment_required_header(challenge)},
        )

    # ------------------------------------------------------------------ the money path

    async def _handle_commit_request(self, scope: Any, receive: Any, send: Any, *, method: str) -> None:
        """Run the eight-step commit path for one gated request."""
        header = self._header(scope, PAYMENT_SIGNATURE_HEADER)
        if not header:
            # Step 1. Identical on GET and POST: the OKX review probe issues a GET.
            await self._send_challenge(send, "payment_required")
            return

        # A paid GET is refused BEFORE verification, so nothing is charged for it. A commitment
        # is a body, and GET is gated only so the unpaid probe meets a challenge.
        if method != "POST":
            await self._send_json(send, 405, {"error": "commit_requires_post"})
            return

        try:
            payload = decode_payment_signature_header(header)
        except Exception:
            # Deliberately broad: every malformation of an attacker-supplied header — bad
            # base64, bad JSON, a payload failing model validation — is the same answer, and
            # enumerating the SDK's decode failures would leave the un-enumerated one as a 500.
            await self._send_challenge(send, "invalid_payment")
            return

        requirements = self._matching_requirements(payload)
        if requirements is None:
            # The payload settles a DIFFERENT set of requirements than this route advertised — a
            # different amount, asset or payee. Refused before verify, and nothing is written.
            await self._send_challenge(send, "invalid_payment")
            return

        # Step 2. Direct SDK verify. No hooks are registered, so this runs the facilitator call
        # and nothing else.
        verified = await self.server.verify_payment(payload, requirements)
        payer = verified.payer if verified.is_valid else None
        if not payer:
            # FAIL CLOSED on None and on "". The SDK types ``payer`` as ``str | None``, and an
            # empty payer would become a decision-slot key SHARED by every anonymous commit —
            # so the first commitment would idempotently answer for all of them. No state is
            # written on this path, which is what makes a refused payment leave no trace.
            await self._send_challenge(send, "invalid_payment")
            return

        raw_body = await self._read_body(receive)
        try:
            request = CommitRequest.model_validate_json(raw_body)
        except Exception:
            # Before any slot exists, so there is nothing to release. Broad for the same reason
            # as the header decode: one answer for every shape of malformed request body.
            await self._send_json(send, 422, {"error": "invalid_commit_request"})
            return

        now_ms = self._now_ms()

        # Resolve BEFORE acquiring, and refuse an unresolvable trial before any slot exists. The
        # ordering is what makes the slot key trustworthy: past this point ``trial`` is not None,
        # so ``trial.trial_id`` is available for every slot operation and the payer's spelling of
        # the id is never used as a key.
        trial = self.live_trials.get(request.trial_id)
        if trial is None:
            await self._send_json(send, 404, {"error": "trial_not_found"})
            return

        # Step 3. Atomic slot acquisition, keyed on the RESOLVED id.
        #
        # ``request.trial_id`` is the payer's spelling and MUST NOT key anything. Resolution is
        # permitted to canonicalize — a case-insensitive filesystem does it today, an alias table
        # or an id migration could do it tomorrow — and when it does, keying acquisition on the
        # request while staging, the attempt marker and finalization key on the resolved id leaves
        # TWO slots for ONE payment. The orphan sits ``in_flight`` with no attempt marker, which
        # is exactly the state the reconciler is required to release as "provably never reached
        # settle"; once released, the next retry finds no slot and settles a second time. One
        # payment, one slot, and the id comes from the trial — not from the request.
        created, state = self.store.acquire_slot(payer, trial.trial_id, now_ms=now_ms)
        if not created:
            await self._answer_existing_slot(send, state, request=request, trial=trial, payer=payer, now_ms=now_ms)
            return

        # Step 4. Validate. Any refusal releases the slot: settlement was never reached, so no
        # payment can exist and holding the slot would lock the payer out of retrying.
        outcome = handle_commit(request, trial=trial, payer=payer, now_ms=now_ms, store=self.store)
        if outcome.status != 200:
            self.store.release_slot(payer, trial.trial_id)
            await self._send_json(send, outcome.status, {"error": outcome.error})
            return
        if outcome.receipt_id is not None:
            # A finalized record already answers for this commitment, so return the ORIGINAL
            # receipt and settle nothing.
            #
            # NOTHING IS RELEASED HERE, and that is the whole point of the branch. It fires only
            # when a finalized record exists — only ever AFTER a successful settlement — and
            # ``release_slot`` names that circumstance unlawful by name: releasing after a
            # successful settlement reopens the slot to a fresh-signature retry and IS the double
            # charge. An earlier revision released here, which in the only case the line could
            # execute deleted the very pointer this branch was answering from, turning a permanent
            # protection into a single-use one.
            #
            # Nothing leaks by not releasing. If a slot was created at some other key — which is
            # the only way this branch is reachable at all — it holds ``in_flight`` with no attempt
            # marker, and that is precisely what the reconciler releases as "provably never reached
            # settle".
            #
            # With acquisition keyed on the resolved id this should not be reachable: the slot was
            # created ``in_flight`` a moment ago and ``finalized_for`` consults that same key. It is
            # honoured rather than assumed anyway. A revision before that argued the branch away as
            # "structural rather than lucky" and was wrong — the argument rested on
            # ``get(x).trial_id == x``, a property of the resolver, not of this function.
            await self._send_receipt(send, outcome.receipt_id, settled=None)
            return

        await self._stage_settle_finalize(send, payload, requirements, request=request, trial=trial, payer=payer)

    def _matching_requirements(self, payload: Any) -> Any:
        """Return the advertised requirements this payload fulfils, or ``None``.

        Delegated to the SDK's own comparison so the definition of "matching" is the protocol's
        rather than this module's opinion of it. Without this check a payer could present a
        valid signature over cheaper requirements and be served.
        """
        return self.server.find_matching_requirements(self._payment_requirements(), payload)

    async def _answer_existing_slot(
        self,
        send: Any,
        state: str,
        *,
        request: CommitRequest,
        trial: LiveTrial,
        payer: str,
        now_ms: int,
    ) -> None:
        """Answer a request whose decision slot is already held, WITHOUT ever settling.

        No branch here reaches a settle call, and that is the property that makes a
        post-quarantine retry unable to double-charge. Each state gets its own code and reason
        so an agent can tell "try again shortly" from "a human has to look at this" from "you
        already committed".

        ``trial`` is non-optional: the caller refuses an unresolvable trial with a 404 before any
        slot is acquired, so reaching this function at all means the trial resolved.
        """
        if state in INDETERMINATE_STATES:
            # An earlier payment for this slot MAY have settled and there is no way to ask.
            # 409 rather than the 502 used at the moment of the indeterminate settle: by now the
            # state is a known, recorded condition of the slot rather than a failure in flight.
            await self._send_json(send, 409, {"error": "payment_indeterminate"})
            return
        if state == "in_flight":
            await self._send_json(send, 409, {"error": "commit_in_flight"})
            return
        # finalized: the idempotency pointer. handle_commit compares canonical bodies and
        # returns either the ORIGINAL receipt or a 409, and settles nothing either way.
        outcome = handle_commit(request, trial=trial, payer=payer, now_ms=now_ms, store=self.store)
        if outcome.status == 200 and outcome.receipt_id is not None:
            await self._send_receipt(send, outcome.receipt_id, settled=None)
            return
        if outcome.status == 200:
            # The slot reads ``finalized`` but no finalized record answers for it, so
            # ``finalized_for`` returned None and the validator fell through to its later checks
            # with nothing to report. Reachable only from an inconsistent store — a partial
            # restore, or a hand-created slot with no receipt id.
            #
            # A NAMED code, never ``outcome.error``, which is None here: the lane's envelope is
            # ``{"error": "<code>"}`` and agents match on that code, so forwarding null would
            # publish a response no caller can dispatch on. Still a 409 and still no settle: the
            # slot says this commitment was already finalized, and an unreadable record is not a
            # licence to charge again.
            await self._send_json(send, 409, {"error": "receipt_record_missing"})
            return
        await self._send_json(send, outcome.status, {"error": outcome.error})

    async def _stage_settle_finalize(
        self,
        send: Any,
        payload: Any,
        requirements: Any,
        *,
        request: CommitRequest,
        trial: LiveTrial,
        payer: str,
    ) -> None:
        """Steps 5 to 8: stage, mark, settle, journal, finalize."""
        staging_id = hashlib.sha256(payload.model_dump_json(by_alias=True, exclude_none=True).encode()).hexdigest()

        # Step 5. The payload lands before anything points at it.
        self.store.stage(
            staging_id=staging_id,
            trial_id=trial.trial_id,
            payer=payer,
            body=request,
            staged_at_ms=self._now_ms(),
            commit_deadline_ms=trial.commit_deadline_ms,
            trial_mode=trial.trial_mode,
        )
        # Step 6. Durable, directory-synced, and BEFORE the settle call.
        self.store.mark_settle_attempted(staging_id)

        # Step 7. Direct SDK settle, three outcomes.
        try:
            settled = await self.server.settle_payment(payload, requirements)
        except Exception:
            # INDETERMINATE. The request may have been sent and the money may have moved, so
            # NOTHING is deleted and NOTHING is released: the slot stays settle_attempted and the
            # reconciler quarantines it. Broad on purpose — a transport error, a timeout and an
            # SDK bug are indistinguishable from here, and all three mean "unknown".
            await self._send_json(send, 502, {"error": "payment_indeterminate"})
            return

        if _settle_outcome_is_definitive_failure(settled):
            # PROVEN failure: the facilitator answered no. This is one of exactly two places a
            # slot may be released, and the only one after staging.
            self.store.delete_staged(staging_id)
            self.store.release_slot(payer, trial.trial_id)
            await self._send_json(send, 402, {"error": "payment_failed"})
            return

        # Step 8. Past this point the money has moved, so no failure path exists.
        if not self._journal_with_one_retry(staging_id, payer=payer, tx_hash=settled.transaction):
            await self._send_settled_receipt_pending(send, settled, trial=trial, payer=payer)
            return
        try:
            receipt_id = self.store.finalize_from_journal(staging_id)
        except Exception:
            # The journal entry survives, so the reconciler completes this exact receipt id. The
            # payer is told the truth: settled, receipt materializing.
            await self._send_settled_receipt_pending(send, settled, trial=trial, payer=payer)
            return
        await self._send_receipt(send, receipt_id, settled=settled)

    def _journal_with_one_retry(self, staging_id: str, *, payer: str, tx_hash: str) -> bool:
        """Write the journal entry, retrying once. Report whether it landed.

        Retried because the write is the durable proof of a settlement that already happened and
        a transient I/O failure should not cost it. Retried ONCE rather than indefinitely because
        the payer is waiting and the fallback is already safe: the slot stays
        ``settle_attempted``, the reconciler quarantines it, and an operator resolves it against
        the facilitator. A ``BaseException`` — process death — is deliberately not caught, so a
        crash here surfaces as a crash instead of a 200 with no journal behind it.
        """
        for attempt in (1, 2):
            try:
                self.store.journal(staging_id, payer=payer, tx_hash=tx_hash)
            except Exception:
                if attempt == 2:
                    return False
            else:
                return True
        return False

    async def _send_settled_receipt_pending(
        self, send: Any, settled: SettleResponse, *, trial: LiveTrial, payer: str
    ) -> None:
        """Answer 200 for a settlement that succeeded but whose record is not yet materialized.

        Never a failure code, because the money moved: reporting failure after settlement is the
        one dishonesty this whole module is built to avoid. ``receipt_id`` is ``null`` rather
        than invented, and the status says exactly which state this is, so a caller can tell it
        from a finalized commit instead of discovering the difference later.
        """
        await self._send_json(
            send,
            200,
            {
                "receipt_id": None,
                "status": "settled_receipt_pending",
                "trial_id": trial.trial_id,
                "payer": payer,
                "payment_tx_hash": settled.transaction,
            },
            {PAYMENT_RESPONSE_HEADER: encode_payment_response_header(settled)},
        )

    async def _send_receipt(self, send: Any, receipt_id: str, *, settled: SettleResponse | None) -> None:
        """Emit the finalized commit receipt, with ``PAYMENT-RESPONSE`` when this call settled.

        The header is attached only when THIS request settled a payment. An idempotent replay
        did not settle anything, and attaching a settlement response to it would advertise a
        second payment that never happened.

        Raises:
            KeyError: The slot pointed at a receipt with no record behind it. Refused rather
                than answered with a stub, because a receipt id the payer can quote must
                resolve to a real record.
        """
        record = self.store.record(receipt_id)
        if record is None:
            raise KeyError(f"decision slot points at receipt {receipt_id!r} with no finalized record")
        extra = {PAYMENT_RESPONSE_HEADER: encode_payment_response_header(settled)} if settled is not None else None
        await self._send_json(
            send,
            200,
            {
                "receipt_id": record.receipt_id,
                "status": "finalized",
                "trial_id": record.trial_id,
                "payer": record.payer,
                "p_follow_profitable": record.p_follow_profitable,
                "methodology_version": record.methodology_version,
                "body_hash": record.body_hash,
                "payment_tx_hash": record.payment_tx_hash,
                "committed_at_ms": record.committed_at_ms,
            },
            extra,
        )
