"""Two-phase journaled commit store — lifecycle unit tests (plan H4.1 step 1).

These exercise the store and the commit handler as PLAIN OBJECTS, with no ASGI stack, no
payment header and no facilitator. The HTTP/x402 half lives in ``test_x402_integration.py``;
splitting them is what lets a state-machine failure be read off a stack trace instead of
being inferred from a status code.

The four assertions this file exists to defend, each of which is a *money* property:

* **A pending row is invisible.** ``stage`` writes a row that no free read may serve. If a
  staged row were public, an unpaid commit would be indistinguishable from a paid one.
* **Journal-then-finalize is recoverable.** The journal is the durable record that settlement
  HAPPENED; finalization is bookkeeping over it. A crash between the two must lose nothing,
  so ``reconcile`` completes it rather than re-settling.
* **Sweeping is only ever safe for a row that PROVABLY never reached the settle call.** The
  attempt marker is what separates "no payment can exist" from "a payment may exist", and the
  two get opposite treatment: swept versus quarantined.
* **An attempted row with no journal entry is INDETERMINATE, never a failure.** The facilitator
  may have settled it; the status is queryable only by a tx hash this state does not have. So
  it is quarantined and retained, never deleted and never served.

The block below ``FROZEN MANDATED RED BLOCK`` is reproduced BYTE-IDENTICALLY from the
implementation plan (lines 745-780) under ``PKT-DEC-C8``: those bytes are what the captured
RED attests to, so lint and type gates do not outrank the freeze. Everything above it — this
docstring, the imports, and the three helpers — is this lane's own and is held to the normal
gates.
"""

from __future__ import annotations

from typing import Any

import pytest

from veridex.api.signal_trials_schemas import CommitRequest
from veridex.signal_trials.challenge_spec import CanonicalSignal
from veridex.signal_trials.live import DECISION_WINDOW_MS, LiveTrial, handle_commit, open_live_trial
from veridex.signal_trials.receipts import ReceiptStore

#: The wall clock the fixtures freeze on. A fixed epoch rather than ``time.time()`` so a
#: deadline assertion is reproducible and a failure is not a function of when it ran.
T0 = 1_700_000_000_000


def _sig(**overrides: Any) -> CanonicalSignal:
    """A canonical signal observed exactly at :data:`T0`.

    Every field is a synthetic constant. ``trigger_wallet_address`` is a repeated-nibble
    address that no chain can hold, so nothing here is or resembles a real credential.
    """
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


def _req(p: float, **overrides: Any) -> CommitRequest:
    """A commit request carrying probability ``p``.

    The ``trial_id`` is deliberately NOT tied to the trial the assertions pass in.
    :func:`~veridex.signal_trials.live.handle_commit` takes the RESOLVED trial as an
    argument — resolution is the caller's job — so it never re-derives the trial from the
    body, and a helper that pinned the two together would hide that division of labour.
    """
    fields: dict[str, Any] = {
        "trial_id": "trial_lifecycle",
        "p_follow_profitable": p,
        "methodology_version": "unit-test-1",
    }
    return CommitRequest(**{**fields, **overrides})


@pytest.fixture
def store(tmp_path: Any) -> ReceiptStore:
    """A store rooted in this test's own ``tmp_path``, so no two tests share state."""
    return ReceiptStore(tmp_path)


@pytest.fixture
def live_trial() -> LiveTrial:
    """An open live trial whose commit window closes exactly one decision window after T0."""
    trial = open_live_trial(_sig(), now_ms=T0)
    # Asserted in the fixture, not in a test: every deadline assertion below reads as a
    # statement about handle_commit, and it would silently become a statement about
    # open_live_trial's arithmetic instead if this drifted.
    assert trial.commit_deadline_ms == T0 + DECISION_WINDOW_MS == T0 + 300_000
    return trial


# ----------------------------------------------------------------------------------------
# FROZEN MANDATED RED BLOCK — plan lines 745-780, byte-identical (PKT-DEC-C8).
# Do not reformat, reorder, split or lint-fix. sha256 prefix 69557c782dbf5783.
# ----------------------------------------------------------------------------------------
def test_replay_trial_paid_commit_rejected(store):
    replay = LiveTrial(trial_id="r1", sig=_sig(), trial_mode="replay", t0_ms=0, commit_deadline_ms=10**15)
    out = handle_commit(_req(0.6), trial=replay, payer="0xb", now_ms=1, store=store)
    assert out.status == 409 and out.error == "replay_trials_read_only" and store.count_all() == 0

def test_absent_trial_404(store):
    assert handle_commit(_req(0.6), trial=None, payer="0xb", now_ms=1, store=store).status == 404

def test_commit_after_deadline_rejected(live_trial, store):
    out = handle_commit(_req(0.6), trial=live_trial, payer="0xb", now_ms=live_trial.commit_deadline_ms, store=store)
    assert out.status == 410 and store.count_all() == 0

def test_stage_then_finalize_roundtrip(live_trial, store):
    staged = store.stage(staging_id="s1", trial_id=live_trial.trial_id, payer="0xb", body=_req(0.6))
    assert store.count_finalized() == 0 and store.public_records("0xb") == []   # pending invisible
    store.journal("s1", payer="0xb", tx_hash="0x" + "c"*64)
    store.finalize_from_journal("s1")
    assert store.count_finalized() == 1 and store.count_pending() == 0

def test_reconciler_completes_journaled_crash(live_trial, store):
    store.stage(staging_id="s2", trial_id=live_trial.trial_id, payer="0xb", body=_req(0.6))
    store.journal("s2", payer="0xb", tx_hash="0x" + "d"*64)
    # simulate crash BEFORE finalize; reconciler must complete exactly one finalized record
    store.reconcile()
    assert store.count_finalized() == 1 and store.count_pending() == 0 and store.journal_len() == 0

def test_reconciler_sweeps_only_rows_that_never_reached_settle(store):
    store.stage(staging_id="s3", trial_id="t", payer="0xb", body=_req(0.6), staged_at_ms=0)   # NO attempt marker
    store.reconcile(now_ms=10**15)
    assert store.count_all() == 0   # provably never settled → swept

def test_reconciler_quarantines_attempted_without_journal(store):
    store.stage(staging_id="s4", trial_id="t", payer="0xb", body=_req(0.6), staged_at_ms=0)
    store.mark_settle_attempted("s4")           # reached the settle call; tx hash unknown
    store.reconcile(now_ms=10**15)
    assert store.count_quarantined() == 1 and store.count_all_public() == 0   # INDETERMINATE, never deleted
