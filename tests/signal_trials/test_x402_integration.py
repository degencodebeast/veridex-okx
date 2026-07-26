"""Settlement-atomic paid commit — HTTP/x402 integration tests (plan H4.1 step 2).

This is the money path, driven end to end through a real ASGI stack: a real 402 challenge, a
real ``PAYMENT-SIGNATURE`` header decoded by the SDK's own decoder, real requirement matching,
and :class:`~veridex.signal_trials.payments.SignalTrialsPaymentASGI` orchestrating verify,
slot acquisition, staging, settlement, journalling and finalization.

**No network client is constructed anywhere in this module.** Settlement runs through the
frozen :class:`~veridex.signal_trials.payments.FakeFacilitator` behind the ``PKT-DEC-C11``
adapter, in process. A probe here that would issue a live request is a finding, not a test.

**No real or realistic credential appears here.** Every address is a repeated-nibble synthetic
constant that no chain can hold, and :data:`SENTINEL_SECRET` exists solely so
:func:`test_a_refusal_never_echoes_a_configured_value` can assert its ABSENCE from rendered
output.

The three settlement outcomes this file separates — and the reason it is worth the length —
are not three status codes over one code path. They are three *durable states*:

============================  ==================================  ==========================
settle result                 slot ends as                        response
============================  ==================================  ==========================
returned ``success=False``    RELEASED (deleted); staged deleted   402, zero rows of any kind
raised / timed out            ``settle_attempted`` -> quarantined  502 ``payment_indeterminate``
returned ``success=True``     ``finalized(receipt_id)``, retained  200 + ``PAYMENT-RESPONSE``
============================  ==================================  ==========================

The middle row is the whole design. A raised settle is **not a proven failure** — the request
may have reached the facilitator and the money may have moved — so it is the one case that must
never delete anything. Collapsing it into either neighbour is a money bug: fold it into row 1
and a real payment loses its record; fold it into row 3 and an unpaid commit is published.

The block below ``FROZEN MANDATED RED BLOCK`` is reproduced BYTE-IDENTICALLY from the
implementation plan (lines 784-899) under ``PKT-DEC-C8``, including its own ``import pytest``
line, which the plan places inside the block. Those bytes are what the captured RED attests to,
so lint and type gates do not outrank the freeze; the resulting ``I001`` is a tooling
disagreement with frozen text, never an implementation defect (``PKT-DEC-C18`` ruling 2).
Everything above it is this lane's own and is held to the normal gates.
"""

from __future__ import annotations

import inspect
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from fastapi import FastAPI
from x402.http.utils import (  # type: ignore[import-untyped]
    decode_payment_required_header,
    decode_payment_response_header,
    encode_payment_signature_header,
)
from x402.mechanisms.evm.exact.server import ExactEvmScheme  # type: ignore[import-untyped]
from x402.schemas.payments import PaymentPayload  # type: ignore[import-untyped]

from veridex.api.signal_trials_router import register_signal_trials_routes
from veridex.api.signal_trials_schemas import CommitRequest, OpenTrialResponse
from veridex.signal_trials.challenge_spec import (
    CanonicalSignal,
    evidence_hash,
    normalize_signal,
    visible_at_decision,
)
from veridex.signal_trials.live import LiveTrial, LiveTrialRepository, handle_commit, open_live_trial
from veridex.signal_trials.payments import (
    COMMIT_PATH,
    FAKE_PAYER,
    GATED_METHODS,
    X_LAYER_MAINNET,
    FakeFacilitator,
    SignalTrialsPaymentASGI,
    X402Settings,
    build_commit_price,
    build_resource_server,
)
from veridex.signal_trials.receipts import STALE_AFTER_MS, ReceiptStore, receipt_id_for

#: Frozen clock. ``T0 + 1`` is one millisecond into the commit window, so every request in this
#: module is comfortably on time and a deadline failure can only come from the deadline logic.
T0 = 1_700_000_000_000
NOW_MS = T0 + 1

#: The trial every ``_body`` commits to. Passed EXPLICITLY to ``open_live_trial`` rather than
#: recomputing its derived id here: a helper that re-derived the id would agree with a broken
#: derivation by construction (``PKT-DEC-C22``'s "both sides carry the same defect" shape).
LIVE_TRIAL_ID = "trial_x402_integration"

#: Payout addresses. TWO of them, used by different tests on purpose: ``pay_to`` decides where
#: the money goes, and a suite that held one constant could not see an implementation that
#: hard-coded it (``PKT-DEC-C18`` constancy; a Gate 1 mutant hard-coding ``pay_to`` survived all
#: 43 tests). Synthetic repeated-nibble addresses; no chain holds either.
PAY_TO_A = "0x" + "a" * 40
PAY_TO_B = "0x" + "d4" * 20

#: Not a credential and not shaped like one — a tagged marker string. Its only job is to be
#: searched for in rendered output, so a guard that leaks a configured value fails loudly.
SENTINEL_SECRET = "SENTINEL-NEVER-RENDER-0000"


class SimulatedCrash(BaseException):
    """Process death mid-write, deliberately NOT an ``Exception``.

    The wrapper is required to swallow ordinary post-settlement I/O errors and still answer
    200 — never a failure path after money has moved. That ``except Exception`` is exactly
    what must NOT absorb a simulated crash, or the "died before the journal rename" test
    would be silently testing the retry path instead of the crash path. Inheriting
    ``BaseException`` makes the two indistinguishable-by-accident cases structurally
    distinct, rather than distinct by remembering to use a different message.
    """


def _sig(**overrides: Any) -> CanonicalSignal:
    """A canonical signal observed exactly at :data:`T0`. Synthetic constants only."""
    fields: dict[str, Any] = {
        "t0_ms": T0,
        "chain_index": "196",
        "token_address": "0x" + "1" * 40,
        "symbol": "TKN",
        "name": "Token",
        "market_cap_usd": 1_500_000.0,
        "holders": 4_200,
        "top10_holder_percent": 31.5,
        "trigger_price": 0.0125,
        "wallet_type": "smart money",
        "trigger_wallet_count": 3,
        "trigger_wallet_address": "0x" + "2" * 40,
        "amount_usd": 25_000.0,
    }
    return CanonicalSignal(**{**fields, **overrides})


def _body(p: float, **overrides: Any) -> dict[str, Any]:
    """The JSON body of a commit request placing probability ``p`` on the open trial."""
    fields: dict[str, Any] = {
        "trial_id": LIVE_TRIAL_ID,
        "p_follow_profitable": p,
        "methodology_version": "x402-integration-1",
    }
    return {**fields, **overrides}


def _request(p: float, **overrides: Any) -> CommitRequest:
    """The same commit request as a validated model, for calling ``handle_commit`` directly."""
    return CommitRequest(**_body(p, **overrides))


class _FaultInjectingStore(ReceiptStore):
    """A real store with post-settlement write failures injected at chosen seams.

    Subclassed rather than flagged, so PRODUCTION carries no fault-injection switch. The
    overrides sit exactly where the plan's crash windows are: the journal rename, and the
    finalize that consumes the journal entry.

    ``fail_journal_writes`` raises :class:`OSError` — an ordinary I/O failure the wrapper must
    absorb, retry once, and then answer 200 through. ``crash_before_journal`` raises
    :class:`SimulatedCrash`, which the wrapper must NOT absorb. Two different failure classes
    at the same seam, because the required behaviours are opposite.
    """

    def __init__(
        self,
        root: Path,
        *,
        fail_journal_writes: bool = False,
        crash_after_journal: bool = False,
        crash_before_journal: bool = False,
        transient_journal_failures: int = 0,
    ) -> None:
        super().__init__(root)
        self._fail_journal_writes = fail_journal_writes
        self._crash_before_journal = crash_before_journal
        # A BUDGET, not a flag: the plan's recovery path re-runs finalize through reconcile,
        # which must then SUCCEED. A permanent failure would make the recovery assertion
        # unreachable and the test would pass for the wrong reason.
        self._finalize_failures_left = 1 if crash_after_journal else 0
        # A budget for the same reason, one layer up: a PERMANENT journal failure and a
        # transient one are indistinguishable to a caller that never retries, so only a
        # failure that later succeeds can tell whether the retry exists.
        self._journal_failures_left = transient_journal_failures

    def journal(self, staging_id: str, *, payer: str, tx_hash: str) -> None:
        """Fail or die at the journal rename, per the configured fault."""
        if self._crash_before_journal:
            raise SimulatedCrash("simulated process death before the journal rename")
        if self._fail_journal_writes:
            raise OSError("simulated journal write failure")
        if self._journal_failures_left > 0:
            self._journal_failures_left -= 1
            raise OSError("simulated TRANSIENT journal write failure")
        super().journal(staging_id, payer=payer, tx_hash=tx_hash)

    def finalize_from_journal(self, staging_id: str) -> str:
        """Fail the first finalize only, leaving the journal entry for the reconciler."""
        if self._finalize_failures_left > 0:
            self._finalize_failures_left -= 1
            raise OSError("simulated crash after journal, before finalize")
        return super().finalize_from_journal(staging_id)


def _settings(*, pay_to: str = PAY_TO_A, price: str = "$0.01", sync_settle: bool = True) -> X402Settings:
    """Directly constructed settings — the path that never sees the loader."""
    return X402Settings(
        enabled=True,
        pay_to=pay_to,
        price=price,
        network=X_LAYER_MAINNET,
        sync_settle=sync_settle,
    )


def _build_app(
    *,
    facilitator: FakeFacilitator,
    store: ReceiptStore,
    trial: LiveTrial,
    settings: X402Settings,
) -> SignalTrialsPaymentASGI:
    """Compose the gated app the way production must: the wrapper OUTSIDE the composed FastAPI.

    The inner FastAPI is the analogue of ``guard.app``, never of ``guard`` — the wrapper has to
    sit in the request path, and an object that merely delegates to the app is not in it. The
    routes are registered on the inner app, and the wrapper intercepts the gated commit path
    before that app ever sees it.
    """
    repo = LiveTrialRepository(store.root / "live")
    repo.publish(trial)
    inner = FastAPI()
    register_signal_trials_routes(inner, data_dir=None, live_trials=repo, store=store)
    server = build_resource_server(settings, facilitator, is_production=False)
    server.register(X_LAYER_MAINNET, ExactEvmScheme())
    return SignalTrialsPaymentASGI(
        inner,
        server=server,
        settings=settings,
        store=store,
        live_trials=repo,
        now_ms=lambda: NOW_MS,
    )


@pytest.fixture
def x402_app_factory(tmp_path: Path) -> Any:
    """Build ``(app, store, facilitator)`` for one gated commit route.

    Each call gets its own store directory, so two factories inside one test cannot see each
    other's slots. Returns the facilitator that was passed in, unwrapped, because the frozen
    assertions read ``fac.settle_calls`` — the fake's own counters stay the oracle
    (``PKT-DEC-C11`` requirement 4), not the adapter's.
    """

    def make(
        *,
        facilitator: FakeFacilitator,
        fail_journal_writes: bool = False,
        crash_after_journal: bool = False,
        crash_before_journal: bool = False,
        transient_journal_failures: int = 0,
        pay_to: str = PAY_TO_A,
        sync_settle: bool = True,
        trial: LiveTrial | None = None,
    ) -> tuple[SignalTrialsPaymentASGI, ReceiptStore, FakeFacilitator]:
        store = _FaultInjectingStore(
            tmp_path / f"store-{uuid.uuid4().hex}",
            fail_journal_writes=fail_journal_writes,
            crash_after_journal=crash_after_journal,
            crash_before_journal=crash_before_journal,
            transient_journal_failures=transient_journal_failures,
        )
        settings = _settings(pay_to=pay_to, sync_settle=sync_settle)
        resolved = trial if trial is not None else open_live_trial(_sig(), now_ms=T0, trial_id=LIVE_TRIAL_ID)
        app = _build_app(facilitator=facilitator, store=store, trial=resolved, settings=settings)
        return app, store, facilitator

    return make


def _fresh_root() -> Path:
    """A private directory for a store or repository built outside the factory fixture.

    Used by the tests below that need to share ONE store across two differently configured apps —
    the payer- and trial-keying pins — which the per-call factory deliberately cannot do.
    """
    return Path(tempfile.mkdtemp(prefix="signal-trials-"))


def _client(app: Any) -> httpx.AsyncClient:
    """An in-process ASGI client. ``httpx.ASGITransport`` opens no socket."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://commit.invalid")


async def _challenge(client: httpx.AsyncClient, body: dict[str, Any], method: str = "POST") -> httpx.Response:
    """Issue the UNPAID request that must be answered with a 402 challenge."""
    return await client.request(method, COMMIT_PATH, json=body)


def _payment_header(challenge: httpx.Response) -> str:
    """Build a fresh ``PAYMENT-SIGNATURE`` for the requirements the challenge advertised.

    Two things make this a real integration probe rather than a stub. The payload's
    ``accepted`` block is taken from the SERVER's own advertised requirements and encoded with
    the SDK's own encoder, so requirement matching is genuinely exercised — a wrapper that
    skipped the match, or matched against different requirements, fails here. And the nonce is
    fresh on every call, which is what makes the idempotency tests mean what they say: their
    comment is "fresh signature/nonce, identical canonical body", and a reused payload would
    collide on ``staging_id`` and prove idempotency for the wrong reason.
    """
    required = decode_payment_required_header(challenge.headers["payment-required"])
    payload = PaymentPayload(
        payload={
            "signature": "0xfa" + uuid.uuid4().hex,
            "authorization": {"from": FakeFacilitator.__name__, "nonce": uuid.uuid4().hex},
        },
        accepted=required.accepts[0],
    )
    return encode_payment_signature_header(payload)


async def _paid_post(app: Any, body: dict[str, Any]) -> httpx.Response:
    """Do the full client flow: unpaid POST -> 402 -> re-POST with a fresh payment header."""
    async with _client(app) as client:
        challenge = await _challenge(client, body)
        assert challenge.status_code == 402, f"expected a 402 challenge first, got {challenge.status_code}"
        return await client.post(COMMIT_PATH, json=body, headers={"PAYMENT-SIGNATURE": _payment_header(challenge)})


# ----------------------------------------------------------------------------------------
# FROZEN MANDATED RED BLOCK — plan lines 784-899, byte-identical (PKT-DEC-C8).
# Do not reformat, reorder, split or lint-fix. sha256 prefix fc1dba880bc36cd1.
# Its own 'import pytest' line is part of the mandated bytes and stays where the plan put it.
# ----------------------------------------------------------------------------------------
import pytest   # required by the parametrized payer test below; this block is its own module

async def test_settlement_failure_leaves_ZERO_records_of_any_kind(x402_app_factory):
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator(fail_settlement=True))
    r = await _paid_post(app, _body(0.6))
    assert r.status_code == 402 and store.count_all() == 0        # not merely count_finalized: staged row DELETED on failure

async def test_handler_rejection_never_settles(x402_app_factory):
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator())
    r = await _paid_post(app, _body(1.7))                          # invalid p → 422
    assert r.status_code == 422 and store.count_all() == 0 and fac.settle_calls == 0

async def test_success_exactly_one_finalized_with_payer_and_tx(x402_app_factory):
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator())
    r = await _paid_post(app, _body(0.6))
    assert r.status_code == 200 and fac.settle_calls == 1
    (rec,) = store.finalized()
    assert rec.payer.startswith("0x") and rec.payment_tx_hash and store.count_pending() == 0

async def test_identical_paid_retry_returns_original_and_NEVER_settles_twice(x402_app_factory):
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator())
    r1 = await _paid_post(app, _body(0.6))
    r2 = await _paid_post(app, _body(0.6))                         # fresh signature/nonce, identical canonical body
    assert r2.status_code == 200 and r2.json()["receipt_id"] == r1.json()["receipt_id"]
    assert fac.settle_calls == 1 and store.count_finalized() == 1  # sequential anti-double-charge

async def test_CONCURRENT_identical_requests_settle_exactly_once(x402_app_factory):
    import asyncio
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator(settle_delay_ms=100))
    r1, r2 = await asyncio.gather(_paid_post(app, _body(0.6)), _paid_post(app, _body(0.6)))
    codes = sorted([r1.status_code, r2.status_code])
    assert codes in ([200, 200], [200, 409])                       # loser: original receipt after finalize, or 409 commit_in_flight
    assert fac.settle_calls == 1 and store.count_finalized() == 1  # THE concurrent anti-double-charge assertion (reservation)

async def test_CONCURRENT_different_bodies_one_winner_one_409_one_settlement(x402_app_factory):
    import asyncio
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator(settle_delay_ms=100))
    r1, r2 = await asyncio.gather(_paid_post(app, _body(0.6)), _paid_post(app, _body(0.7)))
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    assert fac.settle_calls == 1 and store.count_finalized() == 1

async def test_different_body_409_no_write_no_settle(x402_app_factory):
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator())
    await _paid_post(app, _body(0.6))
    r = await _paid_post(app, _body(0.7))
    assert r.status_code == 409 and store.count_finalized() == 1 and fac.settle_calls == 1

async def test_journal_write_failure_after_settlement_success_never_deletes(x402_app_factory):
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator(), fail_journal_writes=True)
    r = await _paid_post(app, _body(0.6))
    assert r.status_code == 200                                    # never raise / never enter a failure path after on-chain settlement
    assert store.count_finalized() == 0 and store.count_settle_attempted() == 1 and store.count_quarantined() == 0
    store.reconcile()                                              # attempted-without-journal → INDETERMINATE quarantine
    assert store.count_quarantined() == 1 and store.count_all_public() == 0   # never swept, never served

async def test_crash_after_journal_is_recovered_not_lost(x402_app_factory):
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator(), crash_after_journal=True)  # finalize raises once
    r = await _paid_post(app, _body(0.6))
    assert r.status_code == 200
    assert store.count_finalized() == 0 and store.journal_len() == 1
    store.reconcile()
    assert store.count_finalized() == 1 and store.journal_len() == 0

async def test_crash_BEFORE_journal_quarantines_never_sweeps(x402_app_factory):
    # the loss window Codex named: settle succeeded on-chain, process died before the journal rename
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator(), crash_before_journal=True)
    try: await _paid_post(app, _body(0.6))
    except SimulatedCrash: pass
    assert store.count_settle_attempted() == 1 and store.journal_len() == 0
    store.reconcile(now_ms=10**15)
    assert store.count_quarantined() == 1 and store.count_all_public() == 0   # INDETERMINATE — never auto-deleted (a real payment may exist)

async def test_retry_blocked_in_BOTH_indeterminate_states_settles_once(x402_app_factory):
    # THE r4/r5 critical case, both phases: retries must not re-charge while the slot is still
    # settle_attempted (pre-reconciliation) AND after it becomes quarantined.
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator(), fail_journal_writes=True)
    await _paid_post(app, _body(0.6))                  # 200; slot = settle_attempted (journal failed)
    r_pre = await _paid_post(app, _body(0.6))          # retry BEFORE reconciliation
    assert r_pre.status_code == 409 and r_pre.json()["error"] == "payment_indeterminate"
    store.reconcile()                                   # slot → quarantined
    r_same = await _paid_post(app, _body(0.6))
    r_diff = await _paid_post(app, _body(0.7))
    assert r_same.status_code == 409 and r_same.json()["error"] == "payment_indeterminate"
    assert r_diff.status_code == 409
    assert fac.settle_calls == 1                        # aggregate across original + all three retries

async def test_finalized_slot_persists_as_idempotency_pointer(x402_app_factory):
    # release-on-finalize would erase the pointer and allow a fresh-signature re-settle (r5 blocker)
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator())
    r1 = await _paid_post(app, _body(0.6))
    assert r1.status_code == 200
    (rec,) = store.finalized()                          # the slot key comes FROM the finalized record — no free variables
    assert store.slot_state(rec.payer, rec.trial_id) == "finalized"
    r2 = await _paid_post(app, _body(0.6))              # fresh signature, identical body
    assert r2.status_code == 200 and r2.json()["receipt_id"] == r1.json()["receipt_id"]
    assert store.slot_state(rec.payer, rec.trial_id) == "finalized" and fac.settle_calls == 1   # pointer retained after retry

async def test_settle_transport_exception_after_attempt_quarantines_not_deletes(x402_app_factory):
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator(raise_transport_error=True))
    r = await _paid_post(app, _body(0.6))
    assert r.status_code == 502 and r.json()["error"] == "payment_indeterminate"
    store.reconcile()
    assert store.count_quarantined() == 1 and store.count_all_public() == 0   # not a proven failure → never deletion

@pytest.mark.parametrize("bad_payer", [None, ""])   # VerifyResponse.payer is str|None; "" would become a shared slot key
async def test_missing_or_empty_verified_payer_fails_closed_no_state(x402_app_factory, bad_payer):
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator(verify_payer=bad_payer))
    r = await _paid_post(app, _body(0.6))
    assert r.status_code == 402 and store.count_all() == 0 and store.slot_count() == 0 and fac.settle_calls == 0

async def test_crash_orphaned_in_flight_slot_recovered_at_startup(x402_app_factory):
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator())
    store.create_slot("0xb", "t1", state="in_flight", created_at_ms=0)        # finally never ran (simulated crash)
    store.reconcile(now_ms=10**15)                                            # no attempt marker + stale → release
    r = await _paid_post(app, _body(0.6))                                     # slot is usable again
    assert r.status_code == 200 and fac.settle_calls == 1


# ----------------------------------------------------------------------------------------
# END OF FROZEN BLOCK. Everything below is this lane's own and is held to the normal gates.
#
# The frozen block above is complete on the state machine and deliberately silent on four
# properties that a Gate 1 verdict named as survivable gaps. Each test below exists because a
# specific wrong implementation passes every frozen assertion:
#
#   * a wrapper that hard-codes ``pay_to`` -- every frozen vector uses one payout address, so
#     the money's DESTINATION is constant and therefore invisible to them;
#   * a wrapper that FORWARDS ``sync_settle`` without ever consuming it -- a Gate 1 mutant at
#     the forwarding site died while the consumption site survived 155 tests;
#   * a wrapper that accepts a payment payload whose ``accepted`` block does not match the
#     requirements it advertised -- i.e. accepts a payment for a different amount;
#   * a wrapper that registers the SDK settle hooks the plan forbids, which would reintroduce
#     the delete-after-settlement hazard the direct-call design exists to remove.
# ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", GATED_METHODS)
async def test_unpaid_request_is_challenged_identically_on_every_gated_method(x402_app_factory, method):
    """Step 1: no payment header -> 402 challenge, GET and POST alike, nothing written.

    GET is gated with POST because the OKX review probe issues a GET against the commit path
    and must meet the paywall rather than a 200. Parametrized rather than asserted twice so a
    method handled differently is a named failure, not a missing case.
    """
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator())
    async with _client(app) as client:
        response = await _challenge(client, _body(0.6), method=method)
    assert response.status_code == 402
    assert "payment-required" in response.headers
    assert store.count_all() == 0 and store.slot_count() == 0 and fac.verify_calls == 0 and fac.settle_calls == 0


@pytest.mark.parametrize("pay_to", [PAY_TO_A, PAY_TO_B])
async def test_challenge_advertises_the_CONFIGURED_payout_address(x402_app_factory, pay_to):
    """The advertised ``payTo`` is the configured one, not a constant baked into the wrapper.

    Hard-coded literals, not values derived from ``pay_to``: a vector derived from the constant
    under test agrees with a hard-coding implementation by construction and cannot detect it.
    Two addresses because one address cannot distinguish "reads the config" from "returns this".
    """
    app, _store, _fac = x402_app_factory(facilitator=FakeFacilitator(), pay_to=pay_to)
    async with _client(app) as client:
        challenge = await _challenge(client, _body(0.6))
    advertised = decode_payment_required_header(challenge.headers["payment-required"]).accepts[0]
    assert advertised.pay_to == pay_to
    # The OTHER address must be absent, which is the half that catches a hard-coding: asserting
    # only that the advertised value equals this parametrization's own input would also pass an
    # implementation returning a constant on the run where that constant IS the input. A previous
    # revision of this line asserted `advertised.pay_to in (PAY_TO_A, PAY_TO_B)` and that the two
    # differ — both true of every implementation, including a hard-coded one, and therefore
    # nothing.
    other = PAY_TO_B if pay_to == PAY_TO_A else PAY_TO_A
    assert advertised.pay_to != other


@pytest.mark.parametrize("pay_to", [PAY_TO_A, PAY_TO_B])
async def test_SETTLEMENT_is_requested_against_the_configured_payout_address(x402_app_factory, pay_to):
    """Where the money actually GOES, pinned at the settle call rather than at the challenge.

    The challenge test above proves the right address is ADVERTISED. This proves the same
    address reaches ``settle``, which is the call that moves funds. A wrapper that advertised
    the configured address and then settled against a hard-coded one would pass that test and
    fail this one, and it is the failure mode that matters.
    """
    app, _store, fac = x402_app_factory(facilitator=FakeFacilitator(), pay_to=pay_to)
    response = await _paid_post(app, _body(0.6))
    assert response.status_code == 200 and fac.settle_calls == 1
    assert fac.last_settled_requirements is not None, "the fake recorded no settlement requirements"
    assert fac.last_settled_requirements.pay_to == pay_to
    # As at the challenge, the discriminating half: equality with this run's own input would also
    # hold for an implementation returning a constant, on whichever run that constant matches.
    # What rules a hard-coding out is that the OTHER configured address never appears.
    other = PAY_TO_B if pay_to == PAY_TO_A else PAY_TO_A
    assert fac.last_settled_requirements.pay_to != other


async def test_wrapper_REFUSES_an_asynchronous_settlement_configuration(x402_app_factory):
    """``sync_settle`` is CONSUMED here, not forwarded onward.

    The whole design depends on settlement completing before the 200 is emitted: the receipt
    carries a real tx hash, and the slot reaches ``finalized`` only after the money moved.
    ``sync_settle=False`` would break that, so construction fails closed instead of mounting a
    gate that can answer 200 for an unsettled commit. ``match=`` is mandatory on a money-path
    rejection (``PKT-DEC-C25`` ruling 2): a bare ``pytest.raises`` would bank "something
    raised" and could not tell this refusal from any other.
    """
    with pytest.raises(ValueError, match="sync_settle"):
        x402_app_factory(facilitator=FakeFacilitator(), sync_settle=False)


async def test_a_payment_for_DIFFERENT_requirements_is_refused_with_no_state(x402_app_factory):
    """A payload whose ``accepted`` block was not the one advertised buys nothing.

    Without requirement matching, a payer could present a valid signature over a cheaper set
    of requirements — a different amount, asset or payee — and the wrapper would settle it.
    The tampered field here is ``amount``, reduced to ``"1"`` atomic unit.
    """
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator())
    async with _client(app) as client:
        challenge = await _challenge(client, _body(0.6))
        required = decode_payment_required_header(challenge.headers["payment-required"]).accepts[0]
        underpaid = PaymentPayload(
            payload={"signature": "0xfa" + uuid.uuid4().hex, "authorization": {"nonce": uuid.uuid4().hex}},
            accepted=required.model_copy(update={"amount": "1"}),
        )
        response = await client.post(
            COMMIT_PATH,
            json=_body(0.6),
            headers={"PAYMENT-SIGNATURE": encode_payment_signature_header(underpaid)},
        )
    assert response.status_code == 402
    assert store.count_all() == 0 and store.slot_count() == 0 and fac.settle_calls == 0


async def test_no_SDK_settle_hooks_are_registered(x402_app_factory):
    """The delete-after-settlement hazard is absent STRUCTURALLY, not by convention.

    ``x402ResourceServerBase._settle_payment_core`` runs the after-settle hooks INSIDE its own
    ``try``, so an exception from one lands in the ``except`` that then invokes the
    settle-FAILURE hooks — a path that could delete records after a settlement that really
    happened. The plan's answer is to register no hooks at all and orchestrate in the wrapper.
    Asserting the empty hook lists is what makes a later "just add an on_after_settle hook"
    fail a test rather than pass review.
    """
    app, _store, _fac = x402_app_factory(facilitator=FakeFacilitator())
    server = app.server
    assert server._after_settle_hooks == [] and server._on_settle_failure_hooks == []
    assert server._before_settle_hooks == [] and server._after_verify_hooks == []
    assert server._on_verify_failure_hooks == [] and server._before_verify_hooks == []


async def test_success_carries_a_decodable_payment_response_naming_the_same_transaction(x402_app_factory):
    """Step 8: the 200 carries ``PAYMENT-RESPONSE``, and it agrees with the stored receipt.

    Two independent readings of the same settlement — the header the payer receives and the row
    the store kept — must name the same transaction. If they can disagree, one of them is
    lying about what was paid.
    """
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator())
    response = await _paid_post(app, _body(0.6))
    assert response.status_code == 200
    settled = decode_payment_response_header(response.headers["payment-response"])
    (record,) = store.finalized()
    assert settled.success is True
    assert settled.transaction == record.payment_tx_hash != ""
    assert response.json()["payment_tx_hash"] == record.payment_tx_hash


async def test_free_reads_are_not_gated_by_the_wrapper(x402_app_factory):
    """The wrapper intercepts the commit path and nothing else.

    Evidence reads are free by frozen-spec section 11, and a wrapper that gated everything it
    wrapped would put the discovery surface behind a paywall — which would also fail OKX
    review, since the probe reads ``/health`` unpaid.
    """
    app, _store, fac = x402_app_factory(facilitator=FakeFacilitator())
    async with _client(app) as client:
        health = await client.get("/signal-trials/health")
        open_trial = await client.get("/signal-trials/open-trial")
    assert health.status_code == 200 and health.json()["ok"] is True
    assert open_trial.status_code == 200 and open_trial.json()["trial_id"] == LIVE_TRIAL_ID
    assert fac.verify_calls == 0 and fac.settle_calls == 0


async def test_free_reads_serve_ONLY_finalized_rows(x402_app_factory):
    """staged, attempted and quarantined rows are all invisible to the public read path.

    Walked through the public accessor rather than counted out of the finalized directory, so
    this fails if the read path is widened to include a non-finalized state — which is the
    actual regression to fear, and one a directory count would not notice.
    """
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator(), fail_journal_writes=True)
    response = await _paid_post(app, _body(0.6))
    assert response.status_code == 200 and store.count_settle_attempted() == 1
    payer = store.staged_payers()[0]
    assert store.public_records(payer) == [] and store.count_all_public() == 0
    store.reconcile()
    assert store.count_quarantined() == 1
    assert store.public_records(payer) == [] and store.count_all_public() == 0


async def test_reconcile_RELEASES_a_stale_in_flight_slot_and_retains_the_others(x402_app_factory):
    """The release branch, pinned on the slot it actually releases.

    The frozen orphan-slot test creates its slot under a key no request ever uses, so it would
    pass whether or not release happened. This asserts the slot's state directly, before and
    after, and pairs it with a stale ``finalized`` slot that must SURVIVE the same sweep —
    releasing a finalized slot would erase the idempotency pointer and let a fresh signature
    settle a second time.
    """
    _app, store, _fac = x402_app_factory(facilitator=FakeFacilitator())
    store.create_slot("0xstale", "t-stale", state="in_flight", created_at_ms=0)
    store.create_slot("0xkept", "t-kept", state="finalized", created_at_ms=0, receipt_id="rcpt-kept")
    assert store.slot_state("0xstale", "t-stale") == "in_flight"
    store.reconcile(now_ms=10**15)
    assert store.slot_state("0xstale", "t-stale") is None
    assert store.slot_state("0xkept", "t-kept") == "finalized"


async def test_the_default_price_reaches_the_wire_through_the_H1_1_validated_path(x402_app_factory):
    """The default price is advertised as its atomic amount, via ``build_commit_price``.

    Hard-coded expected value rather than one recomputed through the same SDK helper the wrapper
    uses: a comparison where both sides run the same conversion reports agreement, not
    correctness. The ceiling and the malformed-price refusals are H1.1's and are pinned in
    ``test_payments.py``; this asserts only that the wrapper routes the price through that
    already-validated path rather than formatting an amount itself.
    """
    app, _store, _fac = x402_app_factory(facilitator=FakeFacilitator())
    async with _client(app) as client:
        challenge = await _challenge(client, _body(0.6))
    advertised = decode_payment_required_header(challenge.headers["payment-required"]).accepts[0]
    assert advertised.amount == "10000"  # $0.01 at six decimals
    # The option the wrapper actually builds, not a restatement of the fixture's own input: this
    # is the object handed to build_payment_requirements, so its network and timeout are part of
    # what the payer is offered.
    option = build_commit_price(_settings())
    assert option.network == X_LAYER_MAINNET and option.max_timeout_seconds == 300


async def test_a_refusal_never_echoes_a_configured_value(x402_app_factory):
    """No rendered byte reproduces a configured value, on the paths an operator can trigger.

    The commit route's refusals are the ones an unauthenticated caller can provoke at will, so
    they are where a leaked payout address or price would end up in someone else's logs. The
    sentinel is a tagged marker, never a credential; the assertion is on its ABSENCE.
    """
    app, _store, _fac = x402_app_factory(facilitator=FakeFacilitator(), pay_to=PAY_TO_B)
    async with _client(app) as client:
        late = await client.post(COMMIT_PATH, json=_body(0.6, trial_id=SENTINEL_SECRET))
        unknown = await client.get(f"/signal-trials/trials/{SENTINEL_SECRET}")
    for response in (late, unknown):
        assert SENTINEL_SECRET not in response.text, f"a caller-supplied value was echoed back: {response.text}"


async def test_an_unresolvable_trial_is_404_before_any_payment_is_taken(x402_app_factory):
    """An unknown trial is refused at the challenge, so nobody pays for a trial that is absent.

    Ordering, not merely status: taking payment and only then discovering the trial does not
    exist would settle a commit that can never be scored, and the refund path does not exist.
    """
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator())
    response = await _paid_post(app, _body(0.6, trial_id="trial_does_not_exist"))
    assert response.status_code == 404 and response.json()["error"] == "trial_not_found"
    assert store.count_all() == 0 and store.slot_count() == 0 and fac.settle_calls == 0


# ----------------------------------------------------------------------------------------
# CONSTANCY AND CRASH-WINDOW PINS
#
# Written after asking the C18 question of the block above: what do these vectors hold
# CONSTANT? Four things, and each one hides a specific wrong implementation:
#
#   * ONE price ($0.01)  -> a wrapper hard-coding the price passes everything above;
#   * ONE payer          -> a slot key that ignores the payer passes everything above;
#   * ONE trial          -> a slot key that ignores the trial passes everything above;
#   * ONE clock instant  -> a sweep with no staleness bound at all passes everything above.
#
# The last two pins reach properties no in-process assertion can observe from the outside —
# the fsync of the attempt marker, and the ORDER of two writes inside finalization. Both are
# crash-window properties: their whole purpose is to determine what a process finds after it
# dies, so there is no return value to assert on. They are pinned by observing the writes
# themselves, which is the only place the property exists.
# ----------------------------------------------------------------------------------------


async def test_an_identical_replay_AFTER_the_deadline_returns_the_original_receipt(x402_app_factory):
    """A commitment already finalized is reported, not re-judged against a closed window.

    A finalized record can only exist if the deadline check passed when it was made, so
    answering its replay with ``410 commit_window_closed`` would assert that a commitment which
    demonstrably happened never did. Ordering idempotency before the clock is what makes the
    honest answer and the safe answer the same one.
    """
    facilitator = FakeFacilitator()
    store = _FaultInjectingStore(_fresh_root())
    trial = open_live_trial(_sig(), now_ms=T0, trial_id=LIVE_TRIAL_ID)
    app_open = _build_app(facilitator=facilitator, store=store, trial=trial, settings=_settings())
    first = await _paid_post(app_open, _body(0.6))
    assert first.status_code == 200

    # Same store, same trial, a clock PAST the commit deadline.
    late = SignalTrialsPaymentASGI(
        app_open.app,
        server=app_open.server,
        settings=app_open.settings,
        store=store,
        live_trials=app_open.live_trials,
        now_ms=lambda: trial.commit_deadline_ms + 1,
    )
    replay = await _paid_post(late, _body(0.6))
    assert replay.status_code == 200
    assert replay.json()["receipt_id"] == first.json()["receipt_id"]
    assert facilitator.settle_calls == 1

    # A NEW commitment at the same late clock is still refused: the reorder must not have
    # weakened the deadline, only stopped it from answering for an existing record.
    fresh_store = _FaultInjectingStore(_fresh_root())
    fresh_app = SignalTrialsPaymentASGI(
        app_open.app,
        server=app_open.server,
        settings=app_open.settings,
        store=fresh_store,
        live_trials=app_open.live_trials,
        now_ms=lambda: trial.commit_deadline_ms,
    )
    too_late = await _paid_post(fresh_app, _body(0.6))
    assert too_late.status_code == 410 and too_late.json()["error"] == "commit_window_closed"
    assert fresh_store.count_all() == 0


@pytest.mark.parametrize("probability", [0.0, 1.0])
async def test_the_probability_bounds_are_INCLUSIVE(x402_app_factory, probability):
    """``p`` of exactly 0 or exactly 1 is a legitimate commitment.

    Frozen spec section 11 says ``p in [0, 1]``, closed at both ends, and the boundary is where a
    maximally confident forecast lives — precisely the commitment a calibration benchmark most
    wants to score. Every vector above uses an interior probability, so an implementation that
    narrowed the bound to an open interval would pass all of them while refusing the two most
    informative commitments a payer can make.
    """
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator())
    response = await _paid_post(app, _body(probability))
    assert response.status_code == 200, response.text
    (record,) = store.finalized()
    assert record.p_follow_profitable == probability and fac.settle_calls == 1


async def test_the_probability_bound_is_enforced_where_it_is_USED_not_only_where_it_is_parsed():
    """``handle_commit`` refuses an out-of-range probability that bypassed pydantic.

    ``CommitRequest.model_construct`` skips validation, which is exactly how a defensive bound
    gets reached in real code — an internal caller building a model without going through the
    wire. Without this the range check in ``handle_commit`` would be unreachable, and an
    unreachable guard is indistinguishable from an absent one.
    """
    store = ReceiptStore(_fresh_root())
    trial = open_live_trial(_sig(), now_ms=T0, trial_id=LIVE_TRIAL_ID)
    unvalidated = CommitRequest.model_construct(trial_id=LIVE_TRIAL_ID, p_follow_profitable=1.7)
    outcome = handle_commit(unvalidated, trial=trial, payer=PAY_TO_A, now_ms=NOW_MS, store=store)
    assert outcome.status == 422 and outcome.error == "invalid_probability"
    assert store.count_all() == 0 and store.slot_count() == 0


async def test_ONE_payer_may_commit_to_TWO_DIFFERENT_trials(x402_app_factory):
    """The decision slot is keyed on the trial as well as the payer.

    Every vector above uses a single trial, so a slot key that ignored ``trial_id`` would be
    invisible to them — and it would mean a payer's first commitment of the season silently
    blocked every later one, which is the product not working at all.
    """
    facilitator = FakeFacilitator()
    store = _FaultInjectingStore(_fresh_root())
    first_trial = open_live_trial(_sig(), now_ms=T0, trial_id=LIVE_TRIAL_ID)
    second_trial = open_live_trial(_sig(), now_ms=T0, trial_id="trial_x402_second")
    app = _build_app(facilitator=facilitator, store=store, trial=first_trial, settings=_settings())
    app.live_trials.publish(second_trial)

    first = await _paid_post(app, _body(0.6))
    second = await _paid_post(app, _body(0.6, trial_id="trial_x402_second"))
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["receipt_id"] != second.json()["receipt_id"]
    assert facilitator.settle_calls == 2 and store.count_finalized() == 2
    assert {r.trial_id for r in store.finalized()} == {LIVE_TRIAL_ID, "trial_x402_second"}


async def test_TWO_DIFFERENT_payers_may_commit_to_ONE_trial(x402_app_factory):
    """The decision slot is keyed on the payer as well as the trial.

    Every vector above verifies to the same payer, so a slot key that ignored the payer would be
    invisible to them — and it would mean the first agent to commit to a trial locked out every
    other agent, turning a benchmark into a race.
    """
    other_payer = "0x" + "e" * 40
    # A vacuity GUARD, not a property of the code: if these two ever became equal this test would
    # silently degrade into a single-payer test that passes against a payer-blind slot key. It
    # compares two constants on purpose, so a future edit to either one fails here rather than
    # quietly removing the coverage.
    assert other_payer != FAKE_PAYER
    store = _FaultInjectingStore(_fresh_root())
    trial = open_live_trial(_sig(), now_ms=T0, trial_id=LIVE_TRIAL_ID)
    first_fac = FakeFacilitator()
    second_fac = FakeFacilitator(verify_payer=other_payer)
    app_one = _build_app(facilitator=first_fac, store=store, trial=trial, settings=_settings())
    app_two = _build_app(facilitator=second_fac, store=store, trial=trial, settings=_settings())

    first = await _paid_post(app_one, _body(0.6))
    second = await _paid_post(app_two, _body(0.6))
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["receipt_id"] != second.json()["receipt_id"]
    assert {r.payer for r in store.finalized()} == {FAKE_PAYER, other_payer}
    assert first_fac.settle_calls == 1 and second_fac.settle_calls == 1


@pytest.mark.parametrize(("price", "atomic"), [("$0.01", "10000"), ("$0.25", "250000"), ("$3", "3000000")])
async def test_the_advertised_amount_FOLLOWS_the_configured_price(x402_app_factory, price, atomic):
    """The charged amount is read from configuration, not baked in.

    Every other vector in this module prices the commit at ``$0.01``, which is the whole
    weakness: an implementation that ignored ``settings.price`` and emitted ``$0.01`` would pass
    all of them while charging the wrong amount for every configuration but the default. The
    expected atomic values are hard-coded rather than recomputed through the same SDK helper the
    wrapper uses, because a comparison in which both sides run the same conversion reports
    agreement rather than correctness.
    """
    store = _FaultInjectingStore(_fresh_root())
    trial = open_live_trial(_sig(), now_ms=T0, trial_id=LIVE_TRIAL_ID)
    app = _build_app(facilitator=FakeFacilitator(), store=store, trial=trial, settings=_settings(price=price))
    async with _client(app) as client:
        challenge = await _challenge(client, _body(0.6))
    advertised = decode_payment_required_header(challenge.headers["payment-required"]).accepts[0]
    assert advertised.amount == atomic


async def test_a_FRESH_staged_row_is_not_swept_by_a_wall_clock_reconcile(x402_app_factory):
    """The sweep needs a row to be STALE, and "stale" is not "exists".

    Every reconcile above either supplies ``now_ms=10**15`` or operates on a row that a different
    branch claims first, so a staleness bound of zero would be invisible to them — and it would
    delete commitments out from under callers whose requests are still in flight.
    """
    _app, store, _fac = x402_app_factory(facilitator=FakeFacilitator())
    # Staged against the REAL clock, not the module's frozen T0. T0 is a 2023 epoch, so a row
    # stamped with it is genuinely stale to a wall-clock reconcile — asserting it survived would
    # have been asserting the opposite of the truth, and passing only if the sweep were broken.
    staged_at = int(time.time() * 1000)
    store.stage(staging_id="fresh", trial_id=LIVE_TRIAL_ID, payer=FAKE_PAYER, body=_body(0.6), staged_at_ms=staged_at)
    assert store.count_pending() == 1
    store.reconcile()  # real clock: the row is milliseconds old
    assert store.count_pending() == 1, "a row younger than the staleness bound must survive"
    store.reconcile(now_ms=staged_at + 1)
    assert store.count_pending() == 1, "one millisecond is not stale"
    store.reconcile(now_ms=staged_at + STALE_AFTER_MS + 1)
    assert store.count_pending() == 0, "past the bound it is provably never-settled and sweepable"


async def test_the_ATTEMPT_MARKER_is_the_transition_that_syncs_the_directory(x402_app_factory):
    """Only ``settle_attempted`` pays for a directory fsync, and it does pay for it.

    Unobservable from any response, which is exactly why it needs pinning here: ``os.replace``
    publishes the CONTENT atomically but the directory entry can still be lost to a power failure
    until the directory is synced. If the attempt marker were lost that way, a crash past the
    settle call would come back reading "never settled" and the next retry would charge again —
    the one failure mode the marker exists to prevent. Recorded per transition, so this fails
    both if the sync is dropped and if it is applied indiscriminately.
    """
    _app, store, _fac = x402_app_factory(facilitator=FakeFacilitator())
    observed: list[tuple[str, bool]] = []
    original = type(store)._write_atomic

    def spy(path, payload, *, fsync_dir=False):
        if "slots" in str(path):
            observed.append((str(payload.get("state")), fsync_dir))
        original(path, payload, fsync_dir=fsync_dir)

    store._write_atomic = spy  # type: ignore[method-assign]
    store.stage(staging_id="s-sync", trial_id=LIVE_TRIAL_ID, payer=FAKE_PAYER, body=_body(0.6), staged_at_ms=NOW_MS)
    store.mark_settle_attempted("s-sync")
    store.journal("s-sync", payer=FAKE_PAYER, tx_hash="0x" + "c" * 64)
    store.finalize_from_journal("s-sync")

    synced = {state for state, did_sync in observed if did_sync}
    not_synced = {state for state, did_sync in observed if not did_sync}
    assert synced == {"settle_attempted"}, f"expected only the attempt marker to sync, saw {observed}"
    assert "finalized" in not_synced


async def test_finalization_writes_the_PAYLOAD_before_the_POINTER(x402_app_factory):
    """The finalized record exists before any slot points at it.

    The reverse order leaves a window in which a crash produces a slot naming a receipt that was
    never written — a dangling pointer nothing can repair, because the payload it names does not
    exist anywhere. Only the ORDER of the two writes distinguishes the two implementations, and
    the end state is identical, so no assertion on the result can tell them apart.
    """
    _app, store, _fac = x402_app_factory(facilitator=FakeFacilitator())
    order: list[str] = []
    original = type(store)._write_atomic

    def spy(path, payload, *, fsync_dir=False):
        if "finalized" in str(path):
            order.append("payload")
        elif "slots" in str(path) and payload.get("state") == "finalized":
            order.append("pointer")
        original(path, payload, fsync_dir=fsync_dir)

    store._write_atomic = spy  # type: ignore[method-assign]
    store.stage(staging_id="s-order", trial_id=LIVE_TRIAL_ID, payer=FAKE_PAYER, body=_body(0.6), staged_at_ms=NOW_MS)
    store.journal("s-order", payer=FAKE_PAYER, tx_hash="0x" + "c" * 64)
    store.finalize_from_journal("s-order")
    assert order == ["payload", "pointer"], f"payload must precede the pointer, saw {order}"


def test_a_receipt_id_is_DERIVED_from_the_staging_id_not_generated():
    """The same staging id always finalizes to the same receipt id.

    Finalization happens either on the request path or later inside the reconciler, and both must
    land on the SAME receipt id. A generated id would make a crash-recovered receipt a different
    receipt, so the id the payer was already handed would reference a record that never appears.
    """
    assert receipt_id_for("abc") == receipt_id_for("abc")
    assert receipt_id_for("abc") != receipt_id_for("abd")
    assert receipt_id_for("abc").startswith("rcpt_")


def test_an_attempt_marker_without_a_staged_row_is_refused():
    """An attempt marker with no payload behind it is a state nothing can reconcile.

    ``match=`` rather than a bare raises: on the money path a rejection pin has to discriminate
    the IDENTITY of the refusal, or it banks only "something raised" and would accept any other
    error as proof of this one.
    """
    store = ReceiptStore(_fresh_root())
    with pytest.raises(KeyError, match="no staged row"):
        store.mark_settle_attempted("never-staged")
    assert store.slot_count() == 0


def test_a_trial_cannot_be_opened_over_evidence_from_the_FUTURE():
    """Evidence observed after the open instant breaks the commit-before-outcome seal."""
    with pytest.raises(ValueError, match="evidence observed later"):
        open_live_trial(_sig(t0_ms=T0 + 1), now_ms=T0)


def test_republishing_a_trial_id_with_DIFFERENT_terms_is_refused():
    """A published trial's deadline and evidence are fixed once commitments can reference them.

    Re-publishing the IDENTICAL trial stays allowed, because opening the same trial twice is an
    ordinary retry; silently replacing its terms would move the goalposts under commitments
    already placed.
    """
    repo = LiveTrialRepository(_fresh_root() / "live")
    trial = open_live_trial(_sig(), now_ms=T0, trial_id=LIVE_TRIAL_ID)
    repo.publish(trial)
    repo.publish(trial)  # idempotent: identical terms
    assert repo.current() is not None and repo.current().trial_id == LIVE_TRIAL_ID
    with pytest.raises(ValueError, match="already published with different terms"):
        repo.publish(open_live_trial(_sig(), now_ms=T0 + 1, trial_id=LIVE_TRIAL_ID))


# ----------------------------------------------------------------------------------------
# GAP-CLOSING PINS — every one of these was written because a MUTANT SURVIVED the suite above.
#
# Each names the wrong implementation it exists to catch, and each was verified to be
# UNPINNED before it was written (the mutant survived) and pinned after. They are grouped here
# rather than beside their neighbours so the reason they exist is not lost: none of them was
# reasoned into existence, and none of the properties above happens to cover them.
#
#   A4  a proven settlement failure that deletes the staged row but never RELEASES the slot.
#       Invisible above because the frozen assertion is `count_all() == 0`, which counts ROWS,
#       not slots — so a declined payer would be locked out of that trial until the reconciler
#       eventually swept the slot.
#   B5  the `in_flight` refusal. The concurrent vectors above cannot reach it: the winner marks
#       `settle_attempted` BEFORE it sleeps in settle, so the loser observes the indeterminate
#       state instead and the `commit_in_flight` branch never runs.
#   C3  `is_valid` ignored. Every facilitator above verifies successfully, so a wrapper that
#       read `payer` without consulting `is_valid` would accept a payment the facilitator
#       explicitly REFUSED.
#   C4  handle_commit's empty-payer precondition, unreachable through the wrapper because the
#       wrapper fail-closes first — and therefore untested until called directly.
#   D2  the sweep destroying a quarantined commitment's PAYLOAD. Invisible above because both
#       quarantine assertions count slots and public rows, neither of which changes when the
#       staged row is deleted.
#   I4  the journal retry. A permanent failure and a transient one are indistinguishable to a
#       caller that never retries, so only a failure that later SUCCEEDS can detect it.
# ----------------------------------------------------------------------------------------


async def test_a_definitive_settlement_failure_RELEASES_the_slot_so_the_payer_may_retry(x402_app_factory):
    """A proven failure leaves no slot behind, so a declined payer is not locked out.

    Releasing here is safe for the same reason it is required: a returned failure is one of only
    two states in which no payment can exist. Retaining the slot would be indistinguishable to
    the payer from being charged — every retry would answer ``409 commit_in_flight`` until the
    reconciler's staleness window expired, on a trial whose commit window is five minutes.
    """
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator(fail_settlement=True))
    declined = await _paid_post(app, _body(0.6))
    assert declined.status_code == 402 and store.count_all() == 0
    assert store.slot_count() == 0, "a proven failure must leave no decision slot"
    assert store.slot_state(FAKE_PAYER, LIVE_TRIAL_ID) is None

    # And the release is USABLE, not merely absent: the same payer commits successfully after.
    app_ok, _store_ok, fac_ok = x402_app_factory(facilitator=FakeFacilitator())
    retry_store = store
    working = SignalTrialsPaymentASGI(
        app_ok.app,
        server=app_ok.server,
        settings=app_ok.settings,
        store=retry_store,
        live_trials=app_ok.live_trials,
        now_ms=lambda: NOW_MS,
    )
    accepted = await _paid_post(working, _body(0.6))
    assert accepted.status_code == 200 and fac_ok.settle_calls == 1
    assert retry_store.slot_state(FAKE_PAYER, LIVE_TRIAL_ID) == "finalized"


async def test_an_IN_FLIGHT_slot_is_refused_with_commit_in_flight_and_never_settles(x402_app_factory):
    """The ``in_flight`` branch, reached deterministically instead of by racing.

    The concurrent vectors above cannot pin this: the winner transitions to ``settle_attempted``
    before it yields, so the loser lands on the indeterminate branch. Creating the slot directly
    is the only way to observe the state the plan actually specifies for a commit that is still
    in flight — and the reason to keep the two apart is that they mean different things to the
    caller: ``commit_in_flight`` is "retry shortly", ``payment_indeterminate`` is "a human must
    resolve this".
    """
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator())
    store.create_slot(FAKE_PAYER, LIVE_TRIAL_ID, state="in_flight", created_at_ms=NOW_MS)
    response = await _paid_post(app, _body(0.6))
    assert response.status_code == 409
    assert response.json()["error"] == "commit_in_flight"
    assert fac.settle_calls == 0 and store.count_all() == 0
    assert store.slot_state(FAKE_PAYER, LIVE_TRIAL_ID) == "in_flight", "the holder's slot is untouched"


async def test_a_REFUSED_verification_is_not_a_payer_even_when_it_names_one(x402_app_factory):
    """``is_valid=False`` is a refusal, whatever else the response carries.

    ``VerifyResponse`` can report ``is_valid=False`` and STILL carry a ``payer`` — the fields are
    independent in the SDK's shape. So reading ``payer`` without consulting ``is_valid`` accepts
    a payment the facilitator explicitly rejected, and it fails in the worst direction: the
    commitment is staged and settled on the strength of a verification that said no.
    """
    facilitator = FakeFacilitator(verify_is_valid=False, verify_payer=FAKE_PAYER)
    app, store, fac = x402_app_factory(facilitator=facilitator)
    response = await _paid_post(app, _body(0.6))
    assert response.status_code == 402
    assert fac.verify_calls == 1, "the facilitator was asked, and its answer was no"
    assert fac.settle_calls == 0
    assert store.count_all() == 0 and store.slot_count() == 0


async def test_handle_commit_REFUSES_an_empty_payer_as_a_precondition():
    """An empty payer is a caller defect, refused loudly rather than keyed into a shared slot.

    Unreachable through the wrapper, which fail-closes first — which is exactly why it needs a
    direct call: a guard no test can reach is indistinguishable from a guard that is not there,
    and this one is the last line between an anonymous commit and a decision slot shared by
    every anonymous commit. ``match=`` because a bare raises would bank only "something raised".
    """
    store = ReceiptStore(_fresh_root())
    trial = open_live_trial(_sig(), now_ms=T0, trial_id=LIVE_TRIAL_ID)
    with pytest.raises(ValueError, match="verified non-empty payer"):
        handle_commit(_request(0.6), trial=trial, payer="", now_ms=NOW_MS, store=store)
    assert store.count_all() == 0 and store.slot_count() == 0


async def test_a_QUARANTINED_commitments_payload_SURVIVES_the_sweep(x402_app_factory):
    """The staged row behind a quarantined slot is evidence, and the sweep must not destroy it.

    The quarantine assertions above count slots and public rows, so deleting the payload changes
    neither and passes them all. But the payload is the ONLY record of what the payer committed
    to, and a quarantine is resolved by a human comparing that commitment against the
    facilitator's ledger. Sweeping it leaves an operator holding a slot that says "money may have
    moved" with nothing to say what it was for.
    """
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator(), fail_journal_writes=True)
    assert (await _paid_post(app, _body(0.6))).status_code == 200
    (staged_id,) = [p.stem for p in sorted((store.root / "staged").glob("*.json"))]

    store.reconcile(now_ms=10**15)
    assert store.count_quarantined() == 1
    surviving = store.staged_row(staged_id)
    assert surviving is not None, "the sweep destroyed the quarantined commitment's payload"
    assert surviving["body"]["p_follow_profitable"] == 0.6
    assert surviving["payer"] == FAKE_PAYER and surviving["trial_id"] == LIVE_TRIAL_ID
    assert store.count_all_public() == 0

    # Repeated reconciliation is idempotent: quarantine is terminal until an operator acts.
    store.reconcile(now_ms=10**15)
    assert store.count_quarantined() == 1 and store.staged_row(staged_id) is not None


async def test_a_TRANSIENT_journal_failure_is_RETRIED_and_the_commit_finalizes(x402_app_factory):
    """One failed journal write is retried, and the commitment completes normally.

    A permanent failure and a transient one look identical to a caller that never retries — both
    end at ``settled_receipt_pending`` — so only a failure that subsequently SUCCEEDS can tell
    whether the retry exists. It is worth having: without it a single transient I/O error turns a
    completed payment into a quarantine an operator has to resolve by hand.
    """
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator(), transient_journal_failures=1)
    response = await _paid_post(app, _body(0.6))
    assert response.status_code == 200
    assert response.json()["receipt_id"] is not None, "the retry should have completed the commit"
    assert response.json()["status"] == "finalized"
    assert store.count_finalized() == 1 and store.count_quarantined() == 0
    assert store.count_settle_attempted() == 0 and store.journal_len() == 0
    assert fac.settle_calls == 1, "the retry is of the JOURNAL write, never of the settlement"


async def test_a_CORRUPT_finalized_row_is_never_counted_as_servable(x402_app_factory):
    """``count_all_public`` walks the read path, so it cannot count a row it could not serve.

    This is the one behaviour that separates walking the public read path from counting the
    finalized directory. For every intact state the two numbers are identical — which is why the
    directory-counting variant survives the rest of this suite — but they diverge on corruption:
    the glob counts a file it has not read, so it would report a servable commitment that the
    read path refuses. Reporting a row as public when serving it raises is the specific
    dishonesty this store is built to avoid, so the count fails loudly instead.
    """
    app, store, fac = x402_app_factory(facilitator=FakeFacilitator())
    assert (await _paid_post(app, _body(0.6))).status_code == 200
    assert store.count_all_public() == 1

    (finalized,) = sorted((store.root / "finalized").glob("*.json"))
    finalized.write_text("{not json at all", encoding="utf-8")
    with pytest.raises(ValueError, match="not readable JSON"):
        store.count_all_public()
    with pytest.raises(ValueError, match="not readable JSON"):
        store.public_records(FAKE_PAYER)


# ----------------------------------------------------------------------------------------
# C22 CROSS-LANE SURFACE PINS — pin the SURFACE, not the blob.
#
# This lane CONSUMES three modules it does not own, all read-only. Reading another lane's
# public surface is permitted and is not a blocking dependency; only EDITING one would be. What
# C22 requires in exchange is that the dependent lane record the surface it consumes and assert
# it mechanically, because a blob difference is never a finding and a surface difference always
# is.
#
#   veridex/signal_trials/challenge_spec.py   (DATA-owned)
#       CanonicalSignal, normalize_signal, visible_at_decision, evidence_hash
#   veridex/api/signal_trials_schemas.py
#       CommitRequest (the wire contract this lane validates), OpenTrialResponse (constructed
#       by the router)
#   veridex/signal_trials/published.py
#       read_season, read_state — inherited from H1.2, untouched here
#
# Candle and CandleSeries are deliberately NOT pinned here: neither symbol appears anywhere in
# this lane's owned set, so asserting their shape would be an assertion that reads as
# discharging an obligation while testing something this lane never touches. That is the
# vacuous-assertion shape, and the honest answer to "does H4.1 depend on Candle" is no.
#
# SCOPE LIMIT, stated so these pins are not over-credited. Surface identity answers "is the
# DEPENDENT lane affected by the owner's change". It does NOT answer "is the owner correct" —
# a canonicalizer that mapped a wire field to the wrong attribute would leave every surface
# below identical, and that pin belongs in Data's own suite.
#
# And per C17-R2-A1, field ORDER is the property that breaks SILENTLY where names, arity and
# types break loudly. This lane's exposure to order is genuinely LOW and it is worth being
# precise rather than claiming credit: every construction here is by KEYWORD
# (`CanonicalSignal(**fields)`, `OpenTrialResponse(trial_id=...)`), never positional, so a
# reorder cannot silently transpose values the way Law's positional eight-argument construction
# could. Order is asserted anyway — it is cheap, it is what C22 requires, and the reason it is
# safe here is a property of THIS code that a future edit could remove.
# ----------------------------------------------------------------------------------------

#: The surface consumed from DATA-owned ``challenge_spec.py``. Names IN ORDER, with types.
CANONICAL_SIGNAL_SURFACE = (
    ("t0_ms", int),
    ("chain_index", str),
    ("token_address", str),
    ("symbol", str),
    ("name", str),
    ("market_cap_usd", float),
    ("holders", int),
    ("top10_holder_percent", float),
    ("trigger_price", float),
    ("wallet_type", str),
    ("trigger_wallet_count", int),
    ("trigger_wallet_address", str),
    ("amount_usd", float),
)

#: The wire contract this lane validates every paid commit against.
COMMIT_REQUEST_SURFACE = (("trial_id", str), ("p_follow_profitable", float), ("methodology_version", str | None))

#: The discovery payload the router constructs for a known or open trial.
OPEN_TRIAL_RESPONSE_SURFACE = ("trial_id", "trial_mode", "t0_ms", "commit_deadline_ms", "evidence", "evidence_hash")


def test_C22_canonical_signal_surface_is_unchanged():
    """The DATA-owned evidence model this lane binds receipts to, asserted mechanically.

    Arity first, then names in order, then types. Arity is the assertion that actually protects
    this lane: ``LiveTrialRepository._load`` rebuilds a signal with ``CanonicalSignal(**stored)``,
    so a field ADDED without a default makes every previously published trial unloadable, and a
    field REMOVED makes every stored document invalid. Both are loud, and both would otherwise
    surface as a runtime failure in the API rather than as a gate finding.
    """
    fields = CanonicalSignal.model_fields
    assert len(fields) == len(CANONICAL_SIGNAL_SURFACE) == 13
    assert tuple(fields) == tuple(name for name, _t in CANONICAL_SIGNAL_SURFACE)
    assert {n: f.annotation for n, f in fields.items()} == dict(CANONICAL_SIGNAL_SURFACE)
    # Frozen is load-bearing, not incidental: this object is the input to evidence_hash, so a
    # mutable one would let a receipt and the evidence it attests to drift apart silently.
    assert CanonicalSignal.model_config.get("frozen") is True
    # `sold_ratio_percent` is absent BY CONSTRUCTION, in both wire spellings. Its absence from
    # the model, not a filter downstream, is what makes the leak impossible.
    assert "sold_ratio_percent" not in fields and "soldRatioPercent" not in fields


def test_C22_the_three_consumed_callables_exist_with_the_expected_arity():
    """``normalize_signal``, ``visible_at_decision`` and ``evidence_hash``, as this lane calls them.

    Asserted by INSPECTING the signatures rather than by calling them, so this fails on a
    contract change even when a call would still happen to succeed — a parameter renamed from
    ``source`` to something else keeps ``normalize_signal(raw, source=...)`` working nowhere but
    breaks the operator script, which passes it by keyword.
    """
    assert list(inspect.signature(normalize_signal).parameters) == ["raw", "source"]
    assert list(inspect.signature(visible_at_decision).parameters) == ["sig"]
    assert list(inspect.signature(evidence_hash).parameters) == ["sig"]
    # And the two this lane reads through LiveTrial properties agree with each other: the hash
    # must bind the SAME payload that is served, or discovery and verification describe
    # different snapshots.
    trial = open_live_trial(_sig(), now_ms=T0, trial_id=LIVE_TRIAL_ID)
    assert trial.evidence == visible_at_decision(trial.sig)
    assert trial.evidence_hash == evidence_hash(trial.sig)


def test_C22_commit_request_surface_and_its_probability_bound():
    """The wire contract, including the bound this lane RELIES ON pydantic to enforce.

    The bound is part of the surface, not decoration. This lane's 422 on an out-of-range
    probability at the HTTP boundary comes from ``CommitRequest`` rejecting it, so a relaxed
    bound in an unowned file would silently move validation off the wire and onto
    ``handle_commit``'s defensive check alone. Asserted as metadata rather than by feeding a bad
    value, so it fails on the CONSTRAINT changing rather than on the behaviour of one input.
    """
    fields = CommitRequest.model_fields
    assert len(fields) == 3
    assert tuple(fields) == tuple(name for name, _t in COMMIT_REQUEST_SURFACE)
    assert {n: f.annotation for n, f in fields.items()} == dict(COMMIT_REQUEST_SURFACE)
    bounds = {type(m).__name__: m for m in fields["p_follow_profitable"].metadata}
    assert bounds["Ge"].ge == 0 and bounds["Le"].le == 1
    assert fields["methodology_version"].is_required() is False


def test_C22_open_trial_response_surface_is_what_the_router_constructs():
    """Constructed by keyword in ``_open_trial_response``, so its field NAMES are the contract.

    ``trial_mode`` is a ``Literal["live"]``: the router passes the literal string rather than the
    trial's own mode, and that is deliberate — a replay trial must not be constructible as a
    discovery payload at all, so the type refuses it instead of the router remembering to.
    """
    assert tuple(OpenTrialResponse.model_fields) == OPEN_TRIAL_RESPONSE_SURFACE
    assert OpenTrialResponse.model_fields["trial_mode"].annotation == Literal["live"]


# ----------------------------------------------------------------------------------------
# NON-VACUOUS BOUNDARY PINS — the widening direction.
#
# The inclusive-bounds test above proves p=0 and p=1 are ACCEPTED, which kills a bound
# TIGHTENED to an open interval. It says nothing about a bound WIDENED: `<= 1.0` changed to
# `<= 2.0` accepts every value those vectors use, and 1.7 (the only out-of-range value tested
# elsewhere) is so far outside that it cannot distinguish a bound at 1.0 from one at 1.5.
#
# A boundary assertion whose input already sits inside the bound proves nothing about the
# comparison. These use values immediately OUTSIDE it, through `model_construct` so pydantic
# does not answer first — which is the only way to reach handle_commit's own check.
# ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("just_outside", [1.0000001, 1.5, -0.0000001, -0.5])
def test_the_probability_bound_refuses_values_JUST_outside_it(just_outside):
    """A probability a hair outside [0, 1] is refused, in both directions.

    Paired with the inclusive test: together they pin the bound's LOCATION rather than merely
    its existence. Tightening the interval kills the inclusive test; widening it in either
    direction kills this one. Neither test alone constrains where the bound sits.
    """
    store = ReceiptStore(_fresh_root())
    trial = open_live_trial(_sig(), now_ms=T0, trial_id=LIVE_TRIAL_ID)
    unvalidated = CommitRequest.model_construct(trial_id=LIVE_TRIAL_ID, p_follow_profitable=just_outside)
    outcome = handle_commit(unvalidated, trial=trial, payer=PAY_TO_A, now_ms=NOW_MS, store=store)
    assert outcome.status == 422 and outcome.error == "invalid_probability"
    assert store.count_all() == 0 and store.slot_count() == 0
