"""Canonical ChallengeSpec for Signal Trials (H2.2).

The trust boundary between the raw OKX wire and everything downstream. Three responsibilities:

1. **Canonicalization.** ``normalize_signal`` collapses the REST and WS spellings of the same
   signal onto one ``CanonicalSignal``. The two transports disagree on field names
   (``top10HolderPercent`` vs ``top10HolderPercentage``), so a receipt that hashed the raw wire
   dict would produce a different evidence hash for the same market event depending on which
   path observed it. Downstream equality is checked at the HASH level, so canonicalization has
   to be exact rather than merely equivalent.

2. **Leakage tiers.** Some wire fields are not observable at decision time and must never enter
   evidence. ``soldRatioPercent`` is dropped during normalization — it is not a
   ``CanonicalSignal`` field at all, so there is no code path that can carry it into
   ``visible_at_decision`` or the hash. ``FORBIDDEN_EVIDENCE_FIELDS`` plus
   ``assert_no_future_fields`` are the belt-and-braces check for payloads assembled elsewhere.

3. **Loud failure.** Every conversion here raises rather than guessing. A settlement path that
   fails toward "unscored" is recoverable; an evidence hash computed over a silently-coerced or
   silently-defaulted value is a false receipt, and nothing downstream can detect it.

**Explicit coercion, not pydantic's lax mode.** The wire delivers every value as a string
(``"1753400000000"``, ``"0.042"``). ``CanonicalSignal`` declares ``int``/``float``, so
constructing it straight from wire values would lean on pydantic's lax-mode string->number
coercion. This module converts explicitly *before* the model is constructed, so the model only
ever sees values of its declared type and lax mode is never exercised on this path. The reason
is failure attribution: a lax-mode rejection names the canonical field (``t0_ms``), while the
helpers below name the WIRE field (``timestamp``), which is the one an operator has to go look
at. The stricter-than-pydantic edges are deliberate and documented on each helper.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel


class CanonicalSignal(BaseModel, frozen=True):
    """One signal, canonicalized. Every field is observable at decision time (t0).

    Frozen because it is the input to ``evidence_hash``: a signal that could be mutated after
    hashing would let a receipt and its subject drift apart silently.

    ``sold_ratio_percent`` is deliberately absent — see the module docstring. Its absence from
    this model, not a filter somewhere downstream, is what makes the leak structurally
    impossible.
    """

    t0_ms: int
    chain_index: str
    token_address: str
    symbol: str
    name: str
    market_cap_usd: float
    holders: int
    top10_holder_percent: float
    trigger_price: float
    wallet_type: str
    trigger_wallet_count: int
    trigger_wallet_address: str
    amount_usd: float


# Field names that must never appear in anything hashed into evidence. Two categories:
# post-decision observations (`sold_ratio_percent` in both wire spellings), and outright future
# data (`settlement`, `future`, `close`, `clv_bps`, and liquidity, which is only known after the
# fact for the purpose this pipeline uses it).
#
# `_scan_for_leakage` compares EXACT keys, so every spelling a payload can be assembled under has
# to appear here in its own right, and the camelCase twin of a multi-word key is a real spelling:
# `okx_client.py` serializes `minLiquidityUsd`, so camelCase is the live wire convention. While
# `liquidity_usd` and `clv_bps` were listed alone, `liquidityUsd` and `clvBps` walked straight
# through the guard at both the top level and nested (PKT-DEC-C15, extended by its A1 addendum).
# Neither is a new guard: both complete one the plan author had already chosen to apply.
#
# The set is now camel-complete BY CONSTRUCTION rather than by inspection, and that property —
# not the individual entries — is the thing to check when editing it: `liquidity`, `settlement`,
# `future` and `close` are single lowercase words and therefore spelling-invariant, and every
# remaining key is multi-word and carries both of its spellings.
#
# Deliberately absent: `minLiquidityUsd`, which is a REQUEST filter key and never appears in an
# evidence payload, and any generic camel-to-snake normalizer, which would change this guard's
# semantics for every key at once — a frozen-contract change rather than a defect fix.
FORBIDDEN_EVIDENCE_FIELDS: frozenset[str] = frozenset(
    {
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
)

# The transports this module knows how to be handed a payload from. `source` never reaches the
# canonical value (see `normalize_signal`), so this tuple exists purely to reject a typo'd or
# unknown transport tag loudly instead of accepting it as a no-op.
_SOURCES: tuple[str, ...] = ("rest", "ws")


class LeakageError(ValueError):
    """A payload carried a field that is not observable at decision time.

    ``ValueError`` rather than a bare ``Exception`` so it is catchable alongside the coercion
    failures above, but it is its own type so a caller can never conflate "this number was
    malformed" with "this payload leaked the future".
    """


def _scan_for_leakage(node: Any, path: str) -> None:
    """Walk ``node`` depth-first, raising on the first forbidden KEY.

    Recursive rather than a single top-level key check: forbidden data arrives nested at least
    as often as it arrives flat (``{"token": {"liquidity_usd": ...}}``, a list of candles each
    with a ``close``). A guard that only inspected the top level would cover half its failure
    space and read as green over exactly the payload shapes it exists to reject.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if key in FORBIDDEN_EVIDENCE_FIELDS:
                raise LeakageError(
                    f"forbidden field {here!r} is not observable at decision time and must never reach evidence"
                )
            _scan_for_leakage(value, here)
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            _scan_for_leakage(item, f"{path}[{index}]")


def assert_no_future_fields(payload: dict[str, Any]) -> None:
    """Raise ``LeakageError`` if ``payload`` carries any ``FORBIDDEN_EVIDENCE_FIELDS`` key.

    Checks nested dicts and sequences too, not just the top level.
    """
    _scan_for_leakage(payload, "")


def _pick(mapping: dict[str, Any], prefix: str, *aliases: str) -> Any:
    """Return the value of whichever alias is present, loudly.

    Raises when none are present, when the value is ``None``, or when two aliases are present
    with DIFFERENT values. That last case is the dangerous one: the wire contract does not say
    which spelling wins, so silently taking the first would pick a value by declaration order
    and hash it into a receipt with no record that the payload was self-contradictory.
    """
    found = [(name, mapping[name]) for name in aliases if name in mapping]
    if not found:
        wanted = " | ".join(f"{prefix}{name}" for name in aliases)
        raise ValueError(f"missing required field: {wanted}")

    distinct = {repr(value) for _, value in found}
    if len(distinct) > 1:
        conflict = ", ".join(f"{prefix}{name}={value!r}" for name, value in found)
        raise ValueError(f"conflicting aliases for the same field: {conflict}")

    name, value = found[0]
    if value is None:
        raise ValueError(f"field {prefix}{name} is null")
    return value


def _as_str(value: Any, field: str) -> str:
    """Wire string -> ``str``. Rejects every non-string.

    No ``str(value)`` fallback on purpose: ``str(True)`` is ``"True"`` and ``str(1.0)`` is
    ``"1.0"``, both of which would hash cleanly into a receipt while being wrong. Every field
    this is used for is documented as a string on the OKX wire, so a non-string is a contract
    change we want to hear about, not absorb.
    """
    if isinstance(value, str):
        return value
    raise ValueError(f"field {field} must be a string, got {type(value).__name__}: {value!r}")


def _as_int(value: Any, field: str) -> int:
    """Wire value -> ``int``. Accepts ``int`` and integral-looking strings only.

    ``bool`` is rejected explicitly because it is an ``int`` subclass, so ``True`` would
    otherwise sail through as ``1``. ``float`` is rejected because rounding ``900.7`` to a
    holder count is a guess.
    """
    if isinstance(value, bool):
        raise ValueError(f"field {field} must be an integer, got bool: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"field {field} is not an integer: {value!r}") from exc
    raise ValueError(f"field {field} must be an integer, got {type(value).__name__}: {value!r}")


def _as_float(value: Any, field: str) -> float:
    """Wire value -> ``float``. Accepts ``int``, ``float``, and numeric strings.

    ``bool`` rejected for the same reason as in ``_as_int``. ``float("nan")`` and ``float("inf")``
    parse from strings, so they are rejected here too: both survive JSON canonicalization as
    ``NaN``/``Infinity`` (which is not valid JSON for any other reader) and NaN additionally
    breaks the equality comparisons every downstream check relies on.
    """
    if isinstance(value, bool):
        raise ValueError(f"field {field} must be a number, got bool: {value!r}")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError as exc:
            raise ValueError(f"field {field} is not a number: {value!r}") from exc
    else:
        raise ValueError(f"field {field} must be a number, got {type(value).__name__}: {value!r}")

    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"field {field} must be finite, got {value!r}")
    return number


def normalize_signal(raw: dict[str, Any], source: Literal["rest", "ws"]) -> CanonicalSignal:
    """Canonicalize one raw OKX signal dict from either transport.

    ``source`` is validated but deliberately does NOT reach the returned value. That is the
    property the cross-transport hash equality rests on: if the transport tag influenced any
    canonical field, the same market event observed over REST and over WS would produce two
    different evidence hashes, and every downstream identity check would silently split.

    Both alias spellings are therefore accepted regardless of ``source`` — they are a wire
    FORMAT quirk, and binding them to the transport tag would make a mislabeled payload fail as
    "missing field" instead of normalizing correctly.

    ``soldRatioPercent`` / ``soldRatioPercentage`` are never read.
    """
    if source not in _SOURCES:
        raise ValueError(f"unknown source {source!r}; supported sources: {list(_SOURCES)}")

    token = raw.get("token")
    if not isinstance(token, dict):
        raise ValueError(f"missing 'token' object, got {type(token).__name__}")

    return CanonicalSignal(
        t0_ms=_as_int(_pick(raw, "", "timestamp"), "timestamp"),
        chain_index=_as_str(_pick(raw, "", "chainIndex"), "chainIndex"),
        token_address=_as_str(_pick(token, "token.", "tokenAddress"), "token.tokenAddress"),
        symbol=_as_str(_pick(token, "token.", "symbol"), "token.symbol"),
        name=_as_str(_pick(token, "token.", "name"), "token.name"),
        market_cap_usd=_as_float(_pick(token, "token.", "marketCapUsd"), "token.marketCapUsd"),
        holders=_as_int(_pick(token, "token.", "holders"), "token.holders"),
        top10_holder_percent=_as_float(
            _pick(token, "token.", "top10HolderPercent", "top10HolderPercentage"),
            "token.top10HolderPercent",
        ),
        trigger_price=_as_float(_pick(raw, "", "price"), "price"),
        wallet_type=_as_str(_pick(raw, "", "walletType"), "walletType"),
        trigger_wallet_count=_as_int(_pick(raw, "", "triggerWalletCount"), "triggerWalletCount"),
        trigger_wallet_address=_as_str(_pick(raw, "", "triggerWalletAddress"), "triggerWalletAddress"),
        amount_usd=_as_float(_pick(raw, "", "amountUsd"), "amountUsd"),
    )


def visible_at_decision(sig: CanonicalSignal) -> dict[str, Any]:
    """The decision-time-visible view of ``sig`` — the exact payload evidence is hashed over.

    Every ``CanonicalSignal`` field is observable at t0 by construction, so this is the whole
    model. The ``assert_no_future_fields`` call is a self-check, not a filter: it cannot pass
    while dropping something, so if a future edit adds a forbidden field to ``CanonicalSignal``
    this raises here rather than quietly widening what gets hashed.
    """
    visible: dict[str, Any] = sig.model_dump()
    assert_no_future_fields(visible)
    return visible


def evidence_hash(sig: CanonicalSignal) -> str:
    """sha256 hex digest over the canonical JSON encoding of ``visible_at_decision(sig)``.

    ``sort_keys=True`` + the tightest separators make the encoding independent of field
    declaration order and of any whitespace convention, which is what lets a REST-observed and a
    WS-observed instance of the same signal hash byte-identically.
    """
    canonical = json.dumps(visible_at_decision(sig), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
