"""Commit-time receipt verifier — unit and endpoint tests (plan H4.2 step 1).

The verifier answers one question about a finalized receipt: **do the facts it serves still
re-derive the commitments it was sealed with?** Four checks, each ``pass`` or ``fail``:

``body_hash``
    The payer's canonical commit body, reconstructed from the fields the receipt SERVES,
    re-hashes to the hash that was taken over it at staging time.
``manifest``
    The receipt's commit-time binding facts re-hash to the manifest hash sealed at
    finalization.
``deadline_respected``
    The recorded commit instant is strictly before the recorded deadline.
``live_mode``
    The recorded trial mode is ``live`` — frozen spec section 11 makes paid commits live-only.

Two properties are what this file exists to defend, and both are honesty properties:

* **A tamper is a 200 carrying a ``fail``**, never a 500 and never a silent pass. A verifier
  that raised on a tampered receipt would make tampering indistinguishable from an outage; one
  that passed it would make it indistinguishable from an intact receipt.
* **A pending staging row is NOT a receipt.** It is a commitment that was received, not one
  that was paid for, so ``verify_receipt`` raises ``KeyError`` and the route answers 404.

**Every discriminating test asserts the WHOLE four-key verdict**, not the one key it perturbed,
**with one exception named below**. A single-key assertion cannot tell "this check noticed" from
"every check fails on every mutation", and a four-key equality that passes because everything is
``"pass"`` cannot tell which check produced which verdict. Each mutation below therefore pins all
four values, so a check that stopped discriminating shows up as a diff rather than as a
still-green suite.

The exception is the coupling tripwire in
``test_the_body_derivation_covers_every_field_a_commit_request_can_carry``, which asserts only the
key it is pinning. That loop walks all three :data:`COMMIT_BODY_FIELDS`, and ``committed_trial_id``
is bound by BOTH ``body_hash`` and ``manifest`` while the other two are bound by ``body_hash``
alone — so no single whole-verdict expectation is correct for all three iterations. Whole-verdict
discrimination for those same fields is covered by the parametrised tests above it. The claim in
this docstring was originally written as an unqualified universal and was falsified by that one
line; it is stated with its exception now because a docstring that overstates its own suite is the
same defect class the suite exists to catch.

Several tests mutate a field the manifest binds and then RESEAL the manifest hash over the
mutated row — a forger who fixed up the seal. That is the only way to prove
``deadline_respected`` and ``live_mode`` are independent checks rather than shadows of the
manifest hash.

The block below ``FROZEN MANDATED RED BLOCK`` is reproduced BYTE-IDENTICALLY from the
implementation plan (lines 912-924) under ``PKT-DEC-C8``: those bytes are what the captured RED
attests to, so lint and type gates do not outrank the freeze. It is fenced with ``fmt: off`` so
the formatter leaves it alone while the rest of the file stays formatter-clean; the fence lines
sit OUTSIDE the frozen bytes and change none of them. sha256 of the 13 frozen lines:
``18f6a050c1b7531ea7a1d1917d7abe505b9d12b230d98616de156c1c1fe74d52``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from veridex.api.signal_trials_router import register_signal_trials_routes
from veridex.api.signal_trials_schemas import CommitRequest
from veridex.chain.anchor import run_manifest_hash
from veridex.signal_trials import live as live_module
from veridex.signal_trials.challenge_spec import CanonicalSignal
from veridex.signal_trials.live import LiveTrial, open_live_trial
from veridex.signal_trials.receipts import (
    _FINALIZED_DIRNAME,
    COMMIT_BODY_FIELDS,
    LIVE_TRIAL_MODE,
    VERIFY_COMMIT_CHECKS,
    CommitRecord,
    ReceiptStore,
    commit_manifest,
    verify_receipt,
)

#: The wall clock the fixtures freeze on. A fixed epoch rather than ``time.time()`` so a
#: deadline verdict is reproducible and a failure is not a function of when it ran.
T0 = 1_700_000_000_000

#: The RESOLVED trial id. Named explicitly rather than derived, so a deadline or body assertion
#: below is a statement about the verifier and not about ``open_live_trial``'s id rule.
RESOLVED_TRIAL_ID = "trial_h42"

#: The PAYER'S spelling of the same trial id, deliberately different in case only.
#:
#: ``payments.py`` documents that resolution is permitted to canonicalize an id — a
#: case-insensitive filesystem does it today — so the id the payer signed over and the id the
#: record is keyed on are two different values. The receipt's ``body_hash`` was taken over the
#: PAYER'S spelling, so a verifier that re-derived it from the resolved id would report ``fail``
#: on every honest receipt the moment resolution canonicalized anything. Making the two differ
#: in the base fixture is what keeps that from passing by coincidence.
PAYER_TRIAL_SPELLING = "TRIAL_H42"

PAYER = "0xb"

#: A synthetic transaction hash. Repeated nibbles, so it is not and cannot resemble a real one.
TX_HASH = "0x" + "c" * 64

#: Absent, in :meth:`_TamperableStore.tamper`. A sentinel rather than ``None`` because ``None``
#: is itself a value worth writing — an absent deadline and a null deadline are different rows.
_ABSENT = object()


def _sig(**overrides: Any) -> CanonicalSignal:
    """A canonical signal observed exactly at :data:`T0`.

    Every field is a synthetic constant. ``trigger_wallet_address`` is a repeated-nibble address
    that no chain can hold, so nothing here is or resembles a real credential.
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
    """A commit request carrying probability ``p``, in the PAYER'S spelling of the trial id.

    The default ``trial_id`` is :data:`PAYER_TRIAL_SPELLING`, not the resolved id: the request is
    what the payer sent, and the record it becomes is keyed on what resolution returned.
    """
    fields: dict[str, Any] = {
        "trial_id": PAYER_TRIAL_SPELLING,
        "p_follow_profitable": p,
        "methodology_version": "unit-test-1",
    }
    return CommitRequest(**{**fields, **overrides})


class _TamperableStore(ReceiptStore):
    """TEST-ONLY raw writer over the finalized directory.

    Verification is only meaningful against a row that something CHANGED after it was sealed, and
    nothing in the production store can change one — a finalized record is written once and
    :class:`CommitRecord` is frozen. So the mutation has to come from outside the production API,
    and it lives here rather than on :class:`ReceiptStore` on purpose: a ``tamper`` method on the
    real store would be a supported way to rewrite a paid receipt.
    ``test_tamper_is_not_a_production_capability`` and
    ``test_no_module_under_veridex_can_tamper_a_receipt`` hold that line.
    """

    def tamper(self, receipt_id: str, *, field: str, value: Any = _ABSENT, reseal: bool = False) -> None:
        """Overwrite (or, with no ``value``, delete) one raw field of a finalized row.

        Args:
            receipt_id: The row to rewrite.
            field: The stored key to overwrite or delete.
            value: The replacement. Omitted means DELETE the key, which is how an unsealed or
                legacy row is reproduced.
            reseal: Recompute ``manifest_hash`` over the mutated row using the PRODUCTION
                manifest function. This models a forger who repaired the seal, and it is what
                lets a test attribute a ``fail`` to ``deadline_respected`` or ``live_mode``
                rather than to the manifest hash noticing first.
        """
        path = Path(self.root) / _FINALIZED_DIRNAME / f"{receipt_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if value is _ABSENT:
            payload.pop(field, None)
        else:
            payload[field] = value
        if reseal:
            payload["manifest_hash"] = run_manifest_hash(commit_manifest(payload))
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def corrupt(self, receipt_id: str, *, raw: str) -> None:
        """Replace a finalized row's bytes wholesale, including with bytes that are not JSON.

        A weaker write than any :meth:`tamper`, and reachable by the same adversary — anyone able
        to write to the finalized directory, which is the precondition every tamper test already
        assumes. It exists because :meth:`tamper` cannot express it: that method round-trips
        through ``json.loads``, so it can only ever produce a well-formed object, and the row
        states that matter here are the ones that are not objects at all.
        """
        path = Path(self.root) / _FINALIZED_DIRNAME / f"{receipt_id}.json"
        path.write_text(raw, encoding="utf-8")


@pytest.fixture
def store(tmp_path: Path) -> _TamperableStore:
    """A store rooted in this test's own ``tmp_path``, so no two tests share state."""
    return _TamperableStore(tmp_path)


@pytest.fixture
def live_trial() -> LiveTrial:
    """An open live trial whose commit window closes one decision window after :data:`T0`."""
    trial = open_live_trial(_sig(), now_ms=T0, trial_id=RESOLVED_TRIAL_ID)
    # Asserted in the fixture, not in a test: every deadline verdict below reads as a statement
    # about the verifier, and it would silently become a statement about open_live_trial's
    # arithmetic instead if this drifted.
    assert trial.commit_deadline_ms == T0 + live_module.DECISION_WINDOW_MS == T0 + 300_000
    assert trial.trial_mode == LIVE_TRIAL_MODE
    return trial


def _commit(store: _TamperableStore, trial: LiveTrial, *, staging_id: str = "s1", p: float = 0.6) -> CommitRecord:
    """Drive one commitment through the REAL two-phase path and return its finalized record.

    Stage, journal, finalize — the same three writes the payment wrapper performs, in the same
    order, so the row under verification is sealed by production code rather than hand-built. A
    hand-built row would let the verifier agree with a fixture instead of with the writer.
    """
    store.stage(
        staging_id=staging_id,
        trial_id=trial.trial_id,
        payer=PAYER,
        body=_req(p),
        staged_at_ms=T0 + 1_000,
        commit_deadline_ms=trial.commit_deadline_ms,
        trial_mode=trial.trial_mode,
    )
    store.journal(staging_id, payer=PAYER, tx_hash=TX_HASH)
    record = store.record(store.finalize_from_journal(staging_id))
    assert record is not None
    return record


@pytest.fixture
def committed_receipt(store: _TamperableStore, live_trial: LiveTrial) -> CommitRecord:
    """One finalized, intact receipt, sealed through the production write path."""
    return _commit(store, live_trial)


def _verdict(receipt: CommitRecord, store: ReceiptStore) -> dict[str, str]:
    """The whole four-key verdict, as a plain dict, for equality assertions."""
    return dict(verify_receipt(receipt.receipt_id, store).checks)


def _all_pass(**overrides: str) -> dict[str, str]:
    """The clean verdict, with named checks overridden.

    Every discriminating assertion is written against this, so the assertion states BOTH what
    changed and that nothing else did.
    """
    return {**dict.fromkeys(VERIFY_COMMIT_CHECKS, "pass"), **overrides}


def _client_for(app: FastAPI) -> AsyncClient:
    """An in-process client over ``app``; no socket is opened."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://sig")


def _server_client_for(app: FastAPI) -> AsyncClient:
    """A client that reports what a REAL SERVER would answer, rather than re-raising.

    ``ASGITransport`` defaults to ``raise_app_exceptions=True``, which surfaces an unhandled
    exception to the test as the exception itself. That is convenient for debugging and useless
    for the question these tests ask: a deployed server does not re-raise, it runs Starlette's
    ``ServerErrorMiddleware`` and answers ``500``. Asserting "never a 500" against a transport
    that cannot produce a 500 would be asserting nothing — which is exactly how this branch went
    uncovered in the first place.
    """
    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://sig")


def _verify_app(store: ReceiptStore | None) -> FastAPI:
    """A bare app carrying only the signal-trials routes, over ``store``."""
    app = FastAPI()
    register_signal_trials_routes(app, store=store)
    return app


def _verify_path(receipt_id: str) -> str:
    return f"/signal-trials/receipts/{receipt_id}/verify"


# ----------------------------------------------------------------------------------------
# FROZEN MANDATED RED BLOCK — plan lines 912-924, byte-identical (PKT-DEC-C8).
# Do not reformat, reorder, split or lint-fix. sha256 18f6a050c1b7531ea7a1d1917d7abe505b9d12b230d98616de156c1c1fe74d52.
# ----------------------------------------------------------------------------------------
# fmt: off
def test_verify_clean_receipt_commit_checks_pass(committed_receipt, store):
    rep = verify_receipt(committed_receipt.receipt_id, store)
    assert {k: v for k, v in rep.checks.items()} == {"body_hash": "pass", "manifest": "pass",
                                                     "deadline_respected": "pass", "live_mode": "pass"}

def test_tampered_probability_fails_body_hash(committed_receipt, store):
    store.tamper(committed_receipt.receipt_id, field="p_follow_profitable", value=0.99)   # test-only raw write
    assert verify_receipt(committed_receipt.receipt_id, store).checks["body_hash"] == "fail"

def test_pending_staging_id_is_not_a_receipt(store, live_trial):
    store.stage(staging_id="sX", trial_id=live_trial.trial_id, payer="0xb", body=_req(0.6))
    import pytest
    with pytest.raises(KeyError): verify_receipt("sX", store)     # router maps to 404
# fmt: on
# ----------------------------------------------------------------------------------------
# END FROZEN MANDATED RED BLOCK
# ----------------------------------------------------------------------------------------


# --- the report's own shape: four commit checks, and NOT H4.3's outcome checks ---


def test_the_report_carries_exactly_the_four_commit_checks(committed_receipt, store):
    """Outcome checks belong to H4.3. A ``pending`` placeholder for them here would advertise a
    settlement verdict that has not been computed."""
    report = verify_receipt(committed_receipt.receipt_id, store)
    assert tuple(report.checks) == VERIFY_COMMIT_CHECKS == ("body_hash", "manifest", "deadline_respected", "live_mode")
    assert report.receipt_id == committed_receipt.receipt_id
    assert set(report.checks.values()) <= {"pass", "fail"}


def test_verification_is_repeatable_and_writes_nothing(committed_receipt, store):
    """Verification is a free read. Two calls agree, and neither changes the tree."""
    before = sorted(p.name for p in (Path(store.root) / _FINALIZED_DIRNAME).glob("*.json"))
    first = _verdict(committed_receipt, store)
    second = _verdict(committed_receipt, store)
    assert first == second == _all_pass()
    assert sorted(p.name for p in (Path(store.root) / _FINALIZED_DIRNAME).glob("*.json")) == before
    assert store.count_pending() == 0 and store.count_finalized() == 1


# --- body_hash: reachable, and attributable to body_hash ---


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("p_follow_profitable", 0.99),
        ("p_follow_profitable", 0.6000000000000001),  # a lie small enough to look like rounding
        ("methodology_version", "not-what-was-signed"),
        ("methodology_version", None),
    ],
)
def test_a_tampered_commitment_fails_body_hash_AND_NOTHING_ELSE(committed_receipt, store, field, value):
    """The probability, the methodology and the stored hash are what ``body_hash`` covers.

    ``manifest`` must keep passing here, and that is the point of asserting all four: the
    manifest binds the body by REFERENCE (it carries ``body_hash``, not the probability), so a
    rewritten probability is ``body_hash``'s finding alone. If the manifest also failed, the two
    checks would be one check reported twice.
    """
    store.tamper(committed_receipt.receipt_id, field=field, value=value)
    assert _verdict(committed_receipt, store) == _all_pass(body_hash="fail")


def test_a_dropped_probability_fails_body_hash_rather_than_raising(committed_receipt, store):
    """An absent field is a verdict, not a crash. ``fail`` is the only honest reading: the
    receipt cannot show what it committed to."""
    store.tamper(committed_receipt.receipt_id, field="p_follow_profitable")
    assert _verdict(committed_receipt, store) == _all_pass(body_hash="fail")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("committed_trial_id", "TRIAL_SOMETHING_ELSE"),  # inside the signed body AND the manifest
        ("body_hash", "0" * 64),  # the body's hash IS one of the manifest's bound facts
    ],
)
def test_a_field_BOTH_checks_cover_is_reported_by_both(committed_receipt, store, field, value):
    """Two of the nine bound facts are also body-hash inputs, so rewriting either is legitimately
    two findings. Asserted rather than avoided: the overlap is a property of the design — the
    manifest binds the body BY REFERENCE, so it must notice the reference being swapped — and
    recording it here is what keeps a future reader from reading it as a leak between checks."""
    store.tamper(committed_receipt.receipt_id, field=field, value=value)
    assert _verdict(committed_receipt, store) == _all_pass(body_hash="fail", manifest="fail")


def test_body_hash_passes_when_resolution_canonicalized_nothing(store, live_trial):
    """The base fixture makes the payer's spelling differ from the resolved id. This pins the
    other half: when they are identical, ``body_hash`` still passes — the check reads the payer's
    spelling because that is what was signed, not because the two happen to differ."""
    store.stage(
        staging_id="s_same",
        trial_id=live_trial.trial_id,
        payer=PAYER,
        body=_req(0.6, trial_id=live_trial.trial_id),
        staged_at_ms=T0 + 1_000,
        commit_deadline_ms=live_trial.commit_deadline_ms,
        trial_mode=live_trial.trial_mode,
    )
    store.journal("s_same", payer=PAYER, tx_hash=TX_HASH)
    record = store.record(store.finalize_from_journal("s_same"))
    assert record is not None
    assert _verdict(record, store) == _all_pass()


def test_the_body_derivation_covers_every_field_a_commit_request_can_carry(committed_receipt, store):
    """The re-derivation is coupled to :class:`CommitRequest`'s shape, and this is the tripwire.

    ``canonical_body_hash`` at staging time hashes the WHOLE dumped request. If the request grows
    a field and the derivation does not, every receipt starts failing ``body_hash`` — loud and
    fail-closed rather than silent, but still wrong. Pinning the two key sets together makes the
    obligation impossible to miss while editing either one.
    """
    assert set(COMMIT_BODY_FIELDS) == set(CommitRequest.model_fields)
    # And the coupling is live: the derivation actually reads each mapped record field.
    for field, source in COMMIT_BODY_FIELDS.items():
        store.tamper(committed_receipt.receipt_id, field=source, value=f"perturbed-{field}")
        assert _verdict(committed_receipt, store)["body_hash"] == "fail"
        store.tamper(committed_receipt.receipt_id, field=source, value=getattr(committed_receipt, field, None))


# --- manifest: reachable, and attributable to the manifest ---


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payment_tx_hash", "0x" + "9" * 64),  # a different payment claimed for the same commit
        ("payment_tx_hash", None),
        ("payer", "0xattacker"),
        ("receipt_id", "rcpt_" + "0" * 32),
        ("manifest_hash", "0" * 64),
        ("trial_id", "trial_somebody_elses"),
    ],
)
def test_a_rewritten_binding_fact_fails_the_manifest_AND_NOTHING_ELSE(committed_receipt, store, field, value):
    """The payment, the payer, the receipt id and the resolved trial are what the manifest binds.

    None of them is inside the signed body, so ``body_hash`` must keep passing — which is what
    makes ``manifest`` a check of its own rather than a second reading of ``body_hash``.
    """
    store.tamper(committed_receipt.receipt_id, field=field, value=value)
    assert _verdict(committed_receipt, store) == _all_pass(manifest="fail")


def test_an_unsealed_receipt_fails_the_manifest_rather_than_passing_vacuously(committed_receipt, store):
    """A row with NO manifest hash cannot be verified against one, and the fail-closed reading is
    the honest one: absence of a seal is not evidence of an intact receipt. This is also what a
    row written before the verifier existed looks like."""
    store.tamper(committed_receipt.receipt_id, field="manifest_hash")
    assert _verdict(committed_receipt, store) == _all_pass(manifest="fail")


# --- deadline_respected: reachable, and attributable to deadline_respected ALONE ---
#
# Its two inputs are both inside the manifest, so an unrepaired mutation fails the manifest too.
# Every test here RESEALS, which strips the manifest's shadow and leaves the deadline check as
# the only thing that can notice. A verifier that treated the deadline as "whatever the sealed
# manifest says" would pass all of these.


def test_a_resealed_late_commit_fails_deadline_respected_ALONE(committed_receipt, store, live_trial):
    """A commitment stamped after its own window closed, with the seal repaired over the lie."""
    store.tamper(
        committed_receipt.receipt_id,
        field="committed_at_ms",
        value=live_trial.commit_deadline_ms + 1,
        reseal=True,
    )
    assert _verdict(committed_receipt, store) == _all_pass(deadline_respected="fail")


def test_the_deadline_boundary_instant_is_LATE(committed_receipt, store, live_trial):
    """Frozen spec section 11: ``received_at >= commit_deadline`` is late. The boundary instant
    itself fails, and the instant before it passes — asserted as a pair, because a check with the
    comparison inverted would still pass a one-sided test."""
    store.tamper(
        committed_receipt.receipt_id, field="committed_at_ms", value=live_trial.commit_deadline_ms, reseal=True
    )
    assert _verdict(committed_receipt, store) == _all_pass(deadline_respected="fail")
    store.tamper(
        committed_receipt.receipt_id, field="committed_at_ms", value=live_trial.commit_deadline_ms - 1, reseal=True
    )
    assert _verdict(committed_receipt, store) == _all_pass()


@pytest.mark.parametrize("value", [None, "1700000000000", 1.7e12, True])
def test_a_resealed_unusable_deadline_fails_deadline_respected_ALONE(committed_receipt, store, value):
    """A missing, stringly-typed, floating or boolean stamp is not a timestamp. Each one fails
    closed rather than being coerced into a comparison that would report ``pass`` on a receipt
    whose timeliness nothing can establish."""
    store.tamper(committed_receipt.receipt_id, field="commit_deadline_ms", value=value, reseal=True)
    assert _verdict(committed_receipt, store) == _all_pass(deadline_respected="fail")


@pytest.mark.parametrize("value", [True, False])
def test_a_resealed_BOOLEAN_commit_instant_fails_rather_than_counting_as_1970(committed_receipt, store, value):
    """``bool`` is a subclass of ``int``, and this is the case where that matters.

    ``True`` as a commit instant is the millisecond 1, which IS strictly before the deadline — an
    ``isinstance(value, int)`` guard would compare it happily and report ``deadline_respected:
    pass`` on a receipt whose stamp says ``true``. The check must reject the type, not compare the
    value, so both booleans fail regardless of which side of the deadline they would land on.
    """
    store.tamper(committed_receipt.receipt_id, field="committed_at_ms", value=value, reseal=True)
    assert _verdict(committed_receipt, store) == _all_pass(deadline_respected="fail")


def test_a_resealed_absent_deadline_fails_deadline_respected_ALONE(committed_receipt, store):
    store.tamper(committed_receipt.receipt_id, field="commit_deadline_ms", reseal=True)
    assert _verdict(committed_receipt, store) == _all_pass(deadline_respected="fail")


def test_a_resealed_absent_commit_instant_fails_deadline_respected_ALONE(committed_receipt, store):
    store.tamper(committed_receipt.receipt_id, field="committed_at_ms", reseal=True)
    assert _verdict(committed_receipt, store) == _all_pass(deadline_respected="fail")


# --- live_mode: reachable, and attributable to live_mode ALONE (resealed, same reasoning) ---


@pytest.mark.parametrize("value", ["replay", "LIVE", "", None, "live "])
def test_a_resealed_non_live_mode_fails_live_mode_ALONE(committed_receipt, store, value):
    """Frozen spec section 11 restricts paid commits to live trials: a replay outcome is publicly
    knowable, so a paid "prediction" of one is not a prediction. Case and whitespace variants are
    included because a receipt is only live if it says exactly ``live``."""
    store.tamper(committed_receipt.receipt_id, field="trial_mode", value=value, reseal=True)
    assert _verdict(committed_receipt, store) == _all_pass(live_mode="fail")


def test_a_resealed_absent_mode_fails_live_mode_ALONE(committed_receipt, store):
    store.tamper(committed_receipt.receipt_id, field="trial_mode", reseal=True)
    assert _verdict(committed_receipt, store) == _all_pass(live_mode="fail")


def test_the_live_mode_constant_matches_the_trial_module(committed_receipt, store):
    """``receipts`` cannot import ``live`` — ``live`` imports ``receipts`` — so the mode literal
    is spelled twice. This is the assertion that keeps the two copies equal."""
    assert LIVE_TRIAL_MODE == live_module.LIVE_MODE == "live"


# --- no check masks another ---


def test_every_check_can_fail_at_once(committed_receipt, store, live_trial):
    """Four independent findings on one row. A verifier that returned early on the first failure
    — or that reported one compound verdict under four names — cannot produce this."""
    receipt_id = committed_receipt.receipt_id
    store.tamper(receipt_id, field="p_follow_profitable", value=0.99)
    store.tamper(receipt_id, field="payment_tx_hash", value="0x" + "9" * 64)
    store.tamper(receipt_id, field="committed_at_ms", value=live_trial.commit_deadline_ms + 1)
    store.tamper(receipt_id, field="trial_mode", value="replay")
    assert _verdict(committed_receipt, store) == dict.fromkeys(VERIFY_COMMIT_CHECKS, "fail")


def test_two_receipts_are_verified_independently(store, live_trial):
    """One tampered row must not contaminate an intact one. Without this, a verifier that read
    the wrong file — or cached the first verdict — would look correct in every single-row test."""
    first = _commit(store, live_trial, staging_id="s_a", p=0.6)
    second = _commit(store, live_trial, staging_id="s_b", p=0.7)
    assert first.receipt_id != second.receipt_id
    store.tamper(first.receipt_id, field="p_follow_profitable", value=0.99)
    assert _verdict(first, store) == _all_pass(body_hash="fail")
    assert _verdict(second, store) == _all_pass()


# --- what is NOT a receipt ---


@pytest.mark.parametrize("receipt_id", ["", "rcpt_unknown", "s1", "trial_h42"])
def test_an_id_with_no_finalized_row_behind_it_raises(store, receipt_id):
    with pytest.raises(KeyError):
        verify_receipt(receipt_id, store)


def test_a_quarantined_slot_is_not_a_receipt(store, live_trial):
    """A settlement was attempted and its outcome is unknown. There is no receipt to verify, and
    inventing a verdict for one would publish a commitment that may never have been paid for."""
    store.stage(staging_id="s_q", trial_id=live_trial.trial_id, payer=PAYER, body=_req(0.6), staged_at_ms=0)
    store.mark_settle_attempted("s_q")
    store.reconcile(now_ms=10**15)
    assert store.count_quarantined() == 1
    with pytest.raises(KeyError):
        verify_receipt("s_q", store)


def test_a_traversing_id_cannot_read_a_PENDING_row_as_a_receipt(store, live_trial):
    """The sharpest form of the traversal: ``../staged/<id>`` lands exactly on a staged row.

    Without the path guard the verifier would load an UNPAID commitment, find a ``body_hash`` in
    it, and publish a verdict — turning "received, not paid for" into "verified receipt" through
    nothing but a URL. The guard is what makes the receipt directory the only readable one.
    """
    store.stage(staging_id="s_pending", trial_id=live_trial.trial_id, payer=PAYER, body=_req(0.6))
    assert (Path(store.root) / "staged" / "s_pending.json").is_file()  # the row the id would reach
    with pytest.raises(KeyError):
        verify_receipt("../staged/s_pending", store)


@pytest.mark.parametrize("receipt_id", ["../leak", "../../leak", "..", ".", "a/b", "..\\..\\x", "/etc/hosts"])
def test_a_receipt_id_that_could_escape_the_finalized_tree_is_not_a_receipt(store, receipt_id, tmp_path):
    """The id arrives from a URL path segment, so this is where a traversal attempt stops.

    A readable JSON file is planted at every spot an UNGUARDED resolution would land — the store
    root and its parent for the ``..`` forms, plus the literal ``..json`` name a bare ``..``
    produces — so a missing guard fails this test rather than being masked by a bare filesystem.
    """
    planted = '{"receipt_id": "planted", "body_hash": "0", "trial_mode": "live"}'
    for spot in (
        tmp_path / "leak.json",
        tmp_path.parent / "leak.json",
        tmp_path / _FINALIZED_DIRNAME / "...json",
        tmp_path / _FINALIZED_DIRNAME / "..json",
    ):
        spot.write_text(planted, encoding="utf-8")
    with pytest.raises(KeyError):
        verify_receipt(receipt_id, store)


# --- tampering is a test capability, never a production one ---


def test_tamper_is_not_a_production_capability():
    """Both raw writes the tests need exist on the TEST subclass only. On ``ReceiptStore`` either
    would be a supported way to rewrite or destroy a paid receipt after it was sealed."""
    for method in ("tamper", "corrupt"):
        assert not hasattr(ReceiptStore, method)
        assert method in vars(_TamperableStore)


def test_no_module_under_veridex_can_tamper_a_receipt():
    """Nothing in the production tree may CALL or DEFINE either raw write.

    Matched on the call and definition syntax, not on the words: "tamper-evident",
    "tamper-resistant" and "corrupt" are all over the production docstrings — ``replay_catalog.py``
    alone says "(tampered / corrupt / unverified)" — and every one of them is the opposite of a
    violation. A word-based predicate would report those as offenders and would have to be
    weakened until it caught nothing.
    """
    root = Path(__file__).resolve().parents[2] / "veridex"
    markers = (".tamper(", "def tamper", ".corrupt(", "def corrupt")
    offenders = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if any(marker in path.read_text(encoding="utf-8") for marker in markers)
    )
    assert offenders == []


# --- the endpoint: an honest fail is a 200 carrying a fail ---


async def test_the_verify_endpoint_serves_the_clean_verdict(committed_receipt, store):
    async with _client_for(_verify_app(store)) as client:
        response = await client.get(_verify_path(committed_receipt.receipt_id))
    assert response.status_code == 200
    assert response.json() == {"receipt_id": committed_receipt.receipt_id, "checks": _all_pass()}


async def test_the_verify_endpoint_returns_200_CARRYING_the_fail(committed_receipt, store):
    """The honesty surface of the whole task. A tamper is not an error condition of the API — the
    API's answer IS the tamper report — so a 500 or a swallowed ``pass`` would both be lies, in
    opposite directions."""
    store.tamper(committed_receipt.receipt_id, field="p_follow_profitable", value=0.99)
    async with _client_for(_verify_app(store)) as client:
        response = await client.get(_verify_path(committed_receipt.receipt_id))
    assert response.status_code == 200
    assert response.json()["checks"] == _all_pass(body_hash="fail")


async def test_the_verify_endpoint_maps_a_pending_staging_id_to_404(store, live_trial):
    """A pending row is a commitment that was RECEIVED, not one that was PAID for. Serving a
    verdict for it would make an unpaid commit indistinguishable from a paid one."""
    store.stage(staging_id="s_pending", trial_id=live_trial.trial_id, payer=PAYER, body=_req(0.6))
    async with _client_for(_verify_app(store)) as client:
        response = await client.get(_verify_path("s_pending"))
    assert response.status_code == 404
    assert response.json() == {"error": "receipt_not_found"}


@pytest.mark.parametrize(
    "receipt_id",
    [
        "rcpt-unknown",
        # PERCENT-ENCODED dot-dot survives client normalization and arrives at the handler AS
        # "..", so the store's path guard is the only thing between it and ``finalized/../..json``.
        # A raw ".." would be collapsed by the client and never reach the route at all — see the
        # framework-404 test below for that half.
        "%2E%2E",
    ],
)
async def test_the_verify_endpoint_refuses_an_unresolvable_id_with_the_LANE_envelope(store, receipt_id):
    """These ids reach the handler, so the refusal is the lane's own ``{"error": ...}`` contract —
    one code for every not-a-receipt, naming nothing about which one applied.

    The envelope IS the evidence that the handler ran: only this route produces
    ``{"error": ...}``, while a request that never matched the route yields FastAPI's
    ``{"detail": ...}``. An earlier revision also carried a parametrised expected-decoded-value
    and asserted it, which was a tautology — the value came from the parameter list, so the
    assertion could not fail and verified nothing about what the handler received. It is gone
    rather than reworded; a false mechanism is worse than none.
    """
    async with _client_for(_verify_app(store)) as client:
        response = await client.get(_verify_path(receipt_id))
    assert response.status_code == 404
    assert response.json() == {"error": "receipt_not_found"}


@pytest.mark.parametrize("receipt_id", ["..", "a%2Fb", "..%2F..%2Fetc%2Fpasswd"])
async def test_an_id_the_ROUTE_cannot_carry_is_404_from_the_framework(store, receipt_id):
    """A raw ``..`` is collapsed by the client and a decoded ``/`` splits into two path segments, so
    neither ever matches this single-segment route. The answer is still 404 — FastAPI's own
    ``{"detail": ...}`` rather than the lane's code — and recording that here is the point: the two
    envelopes are not interchangeable, and a caller that matches on ``error`` must not be told a
    404 always carries one."""
    async with _client_for(_verify_app(store)) as client:
        response = await client.get(_verify_path(receipt_id))
    assert response.status_code == 404
    assert "checks" not in response.json()


# --- an UNREADABLE row is a verdict, not an outage (SPEC F1) ---
#
# The corruption an attacker reaches with a simpler write than any tamper in this file: destroy the
# bytes. Before the fix each of these raised ValueError out of verify_receipt and the route answered
# 500 — the exact failure the route's docstring says must never happen, on the branch the packet
# calls the honesty surface. The suite could not see it: the endpoint tests all used a transport that
# re-raises instead of producing the 500 a deployed server produces, and nothing ever wrote a
# non-JSON row.

#: The three row states that are not a JSON object. ``[1,2,3]`` is the one that matters most: it
#: parses cleanly, so a verifier guarding only against a decode error would still crash on it.
_UNREADABLE_ROWS = [
    pytest.param("{not json", id="unparseable"),
    pytest.param("[1,2,3]", id="valid_json_but_not_an_object"),
    pytest.param("", id="empty_file"),
    pytest.param('"a string"', id="valid_json_scalar"),
]


@pytest.mark.parametrize("raw", _UNREADABLE_ROWS)
def test_an_unreadable_row_verifies_as_four_fails_and_does_not_raise(committed_receipt, store, raw):
    """A row that exists and re-derives nothing is what ``fail`` means.

    Not an exception, because verification failing is the answer this function exists to give. Not
    a ``KeyError`` either — that maps to 404, and ``_read_json``'s own docstring fixes the principle
    that corruption is never reported as absence. A destroyed receipt must not be indistinguishable
    from one that never existed.
    """
    store.corrupt(committed_receipt.receipt_id, raw=raw)
    assert _verdict(committed_receipt, store) == dict.fromkeys(VERIFY_COMMIT_CHECKS, "fail")


@pytest.mark.parametrize("raw", _UNREADABLE_ROWS)
async def test_the_verify_endpoint_answers_200_CARRYING_fails_for_an_unreadable_row(committed_receipt, store, raw):
    """The honesty surface, on the branch that falsified it. Asserted through a transport that
    would actually report a 500, so this test can fail the way the defect failed."""
    store.corrupt(committed_receipt.receipt_id, raw=raw)
    async with _server_client_for(_verify_app(store)) as client:
        response = await client.get(_verify_path(committed_receipt.receipt_id))
    assert response.status_code == 200
    assert response.json() == {
        "receipt_id": committed_receipt.receipt_id,
        "checks": dict.fromkeys(VERIFY_COMMIT_CHECKS, "fail"),
    }


async def test_an_unreadable_row_is_not_reported_as_ABSENCE(committed_receipt, store):
    """Corruption and non-existence are different facts and must not share an answer.

    A 404 here would tell a caller its receipt never existed, when what happened is that the row
    was destroyed — the difference between "you were never paid for this" and "your receipt no
    longer verifies", which are acted on differently.
    """
    store.corrupt(committed_receipt.receipt_id, raw="{not json")
    async with _server_client_for(_verify_app(store)) as client:
        corrupt_response = await client.get(_verify_path(committed_receipt.receipt_id))
        absent_response = await client.get(_verify_path("rcpt_" + "0" * 32))
    assert corrupt_response.status_code == 200
    assert absent_response.status_code == 404
    assert corrupt_response.json() != absent_response.json()


async def test_an_INFRASTRUCTURE_fault_is_still_a_500_and_not_a_verdict(committed_receipt, store, monkeypatch):
    """Only corruption becomes a verdict. A disk or permissions fault stays an error.

    The distinction is the whole justification for catching ``ValueError`` narrowly instead of
    ``Exception``: an unreadable ROW is a statement about the receipt, while an unreadable DISK is
    a statement about the service, and reporting the second as four ``fail``s would tell every
    caller their receipts are invalid during an outage. Without this test the narrow catch and a
    broad one are observationally identical, which is exactly the kind of untested distinction
    that let the original 500 through.
    """

    def _unreadable_disk(receipt_id: str) -> dict[str, Any] | None:
        raise OSError("simulated unreadable disk")

    monkeypatch.setattr(store, "finalized_payload", _unreadable_disk)
    with pytest.raises(OSError, match="simulated unreadable disk"):
        verify_receipt(committed_receipt.receipt_id, store)
    async with _server_client_for(_verify_app(store)) as client:
        response = await client.get(_verify_path(committed_receipt.receipt_id))
    assert response.status_code == 500


def test_the_unreadable_row_tolerance_is_SCOPED_to_the_verifier(committed_receipt, store):
    """The tolerance lives in ``verify_receipt`` only — ``_read_json`` and ``finalized_payload`` still
    raise, and ``record()`` still propagates.

    Deliberate, and this test is what stops a later edit from "simplifying" it. Softening the read
    itself would return ``None`` for a corrupt row, which the payment path reads as "the slot points
    at a receipt with no record behind it" — a different and wrong diagnosis, on the money path.
    """
    store.corrupt(committed_receipt.receipt_id, raw="{not json")
    with pytest.raises(ValueError):
        store.finalized_payload(committed_receipt.receipt_id)
    with pytest.raises(ValueError):
        store.record(committed_receipt.receipt_id)


# --- a row that is SYNTACTICALLY valid JSON but nested past the recursion budget ---
#
# The previous block's rows are all rejected by the DECODER as malformed. These are not: every row
# below is a well-formed JSON object. What defeats them is depth — CPython raises ``RecursionError``
# rather than ``JSONDecodeError``, and ``RecursionError`` is a ``RuntimeError``, so the
# ``except ValueError`` that catches every other unreadable row does not see it. Before the fix the
# direct call raised and the route answered 500.
#
# MEASURED on this head (CPython 3.11.15, recursion limit 1000, C ``_json`` accelerator present),
# sweeping depths 940-1059 over both nesting shapes through the real reader, with every
# ``RecursionError`` attributed to the frame that raised it:
#
#   depth <= 990   the row parses; all four checks report their own real result
#   depth >= 991   ``RecursionError`` out of ``ReceiptStore._read_json``
#   no depth       reaches ``run_manifest_hash`` / ``canonical_body_hash``
#
# The second boundary is REAL but is not reachable from disk bytes here, and the reason is worth
# recording: the decode sits two frames DEEPER than the re-hash (``verify_receipt`` ->
# ``finalized_payload`` -> ``_read_json`` -> ``json.loads`` versus ``verify_receipt`` ->
# ``run_manifest_hash`` -> ``json.dumps``), so it exhausts the budget first and a row deep enough to
# defeat a re-hash is always deep enough to defeat the read. Two frames is the whole margin —
# calling ``verify_receipt`` from two frames further down moves the transition by two levels, which
# is exactly what happens under the ASGI stack. Both boundaries are therefore guarded and both are
# tested, the post-parse one through the read seam rather than through bytes, because on this build
# no bytes can reach it. See ``test_a_value_that_defeats_ONE_canonical_rehash_fails_only_THAT_check``.

#: Past any reader on this interpreter: the recursion limit is 1000, so nothing parses this.
_NEST_BEYOND_ANY_READER = 2_000

#: Deep, and comfortably WITHIN the budget at any plausible stack depth. The discrimination
#: control: proves the repair did not turn "deeply nested" into a blanket four-``fail``.
_NEST_WITHIN_BUDGET = 500

#: Depths straddling the measured transition. Which side of it a given depth lands on shifts with
#: the caller's stack, so these are pinned to the invariant that holds on BOTH sides rather than to
#: a verdict that only holds on one — a test asserting the 991 boundary itself would be asserting
#: this machine's frame count.
_BOUNDARY_DEPTHS = [985, 988, 990, 991, 992, 995, 1_000, 1_005]

#: Both JSON container shapes. Swept because the decoder has a separate parse routine for each and
#: a guard that covered only arrays would leave objects escaping.
_DEEP_ROW_SHAPES = [pytest.param("array", id="deep_array"), pytest.param("object", id="deep_object")]

#: A field bound by exactly ONE canonical re-hash, and the check that re-hash produces. ``payer`` is
#: in :data:`COMMIT_MANIFEST_FIELDS` and not in :data:`COMMIT_BODY_FIELDS`; ``p_follow_profitable``
#: is the reverse, because the manifest binds the commitment by reference through ``body_hash``.
#: That separation is what makes "only the affected check fails" a decidable claim rather than a
#: hope — each row here names a check that MUST flip and three that must NOT.
_ONE_REHASH_DEFEATED = [
    pytest.param("payer", "manifest", id="manifest_only"),
    pytest.param("p_follow_profitable", "body_hash", id="body_hash_only"),
]


def _nested_text(depth: int, shape: str) -> str:
    """Return JSON TEXT nested ``depth`` levels, built by string repetition.

    Text rather than ``json.dumps`` of a nested object because the encoder recurses too: dumping a
    992-deep value raises ``RecursionError`` in the TEST, before the row is ever written. The
    fixture has to be able to write rows the interpreter cannot round-trip.
    """
    opener, closer = ("[", "]") if shape == "array" else ('{"a": ', "}")
    return opener * depth + '"x"' + closer * depth


def _nest_row_field(store: _TamperableStore, receipt_id: str, *, field: str, depth: int, shape: str) -> None:
    """Replace one field of a finalized row with ``depth`` levels of nesting, as raw bytes.

    Not :meth:`_TamperableStore.tamper`, for the reason in :func:`_nested_text` — that method
    round-trips the whole row through ``json.dumps`` and cannot express these depths at all.
    """
    path = Path(store.root) / _FINALIZED_DIRNAME / f"{receipt_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop(field, None)
    head = json.dumps(payload, sort_keys=True)[:-1]
    store.corrupt(receipt_id, raw=f"{head}, {json.dumps(field)}: {_nested_text(depth, shape)}}}")


def _nested_value(depth: int) -> Any:
    """Return a Python value nested ``depth`` levels, built ITERATIVELY.

    A loop rather than a recursive helper, so constructing the fixture cannot itself be what raises.
    """
    value: Any = "x"
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize("shape", _DEEP_ROW_SHAPES)
def test_a_row_nested_beyond_any_reader_verifies_as_four_fails(committed_receipt, store, shape):
    """Well-formed JSON that no reader on this interpreter can parse is still an unreadable row.

    The bytes are a valid JSON object — a decoder with an unlimited budget would accept them — so
    this is not the malformed case the previous block covers. Nothing in it can be re-derived, which
    is what four ``fail``s mean, and the depth is what makes it so rather than the syntax.
    """
    _nest_row_field(store, committed_receipt.receipt_id, field="payer", depth=_NEST_BEYOND_ANY_READER, shape=shape)
    assert _verdict(committed_receipt, store) == dict.fromkeys(VERIFY_COMMIT_CHECKS, "fail")


@pytest.mark.parametrize("shape", _DEEP_ROW_SHAPES)
async def test_the_verify_endpoint_answers_200_CARRYING_fails_for_a_row_nested_beyond_any_reader(
    committed_receipt, store, shape
):
    """The public surface, on the path that produced the reported 500.

    Through the transport that reports what a deployed server reports, because the defect WAS a 500
    and a transport that re-raises cannot tell one from an escaping exception.
    """
    _nest_row_field(store, committed_receipt.receipt_id, field="payer", depth=_NEST_BEYOND_ANY_READER, shape=shape)
    async with _server_client_for(_verify_app(store)) as client:
        response = await client.get(_verify_path(committed_receipt.receipt_id))
    assert response.status_code == 200
    assert response.json() == {
        "receipt_id": committed_receipt.receipt_id,
        "checks": dict.fromkeys(VERIFY_COMMIT_CHECKS, "fail"),
    }


@pytest.mark.parametrize("shape", _DEEP_ROW_SHAPES)
def test_a_deeply_nested_but_READABLE_row_keeps_independent_check_attribution(committed_receipt, store, shape):
    """Depth alone is not a verdict. A row that READS still gets four independently derived checks.

    The discrimination control for the repair: "unreadable" has to keep meaning "this reader could
    not get at it", not "this looked alarming". A guard that answered four ``fail``s for any nested
    value would pass every test above this one and would be strictly less honest than the code it
    replaced — it would report ``body_hash``, ``deadline_respected`` and ``live_mode`` as failed
    when all three were re-derived successfully. Only ``manifest`` may move here, and only because
    ``payer`` was rewritten.
    """
    _nest_row_field(store, committed_receipt.receipt_id, field="payer", depth=_NEST_WITHIN_BUDGET, shape=shape)
    assert _verdict(committed_receipt, store) == _all_pass(manifest="fail")


@pytest.mark.parametrize("depth", _BOUNDARY_DEPTHS)
def test_no_nesting_depth_across_the_reader_boundary_makes_the_verifier_raise(committed_receipt, store, depth):
    """Sweep the transition. Every depth answers with a verdict; none escapes as an exception.

    Which of the two honest verdicts applies depends on whether this depth happens to fit in the
    budget left by the caller's stack, and that is a property of the machine rather than of the
    receipt — so the expectation is the pair, and the claim under test is that the set contains no
    third member. This is the shape of the original defect: it lived in a handful of depths nobody
    had a reason to pick, on the far side of a boundary that moves.
    """
    _nest_row_field(store, committed_receipt.receipt_id, field="payer", depth=depth, shape="array")
    assert _verdict(committed_receipt, store) in (
        dict.fromkeys(VERIFY_COMMIT_CHECKS, "fail"),
        _all_pass(manifest="fail"),
    )


@pytest.mark.parametrize(("field", "defeated"), _ONE_REHASH_DEFEATED)
def test_a_value_that_defeats_ONE_canonical_rehash_fails_only_THAT_check(
    committed_receipt, store, monkeypatch, field, defeated
):
    """A row that PARSES, carrying a value one re-hash cannot serialize: only that check fails.

    The second boundary. Collapsing this to four ``fail``s would discard true information — three
    of these checks completed and returned a real answer, and reporting them as failed would tell a
    holder their receipt's probability had been altered when it had not.

    Delivered through the read seam rather than through bytes on disk, and the reason is measured
    rather than assumed: see the block comment above — on this build the decode exhausts the budget
    two frames before the re-hash does, so no on-disk depth reaches this code. The DATA is real
    (genuinely nested, defeating the real production hash function on its own); only the delivery is
    injected. The existing infrastructure-fault test uses the same seam for the same reason.

    The single equality carries both controls. Acceptance: ``defeated`` must flip to ``fail``, so a
    guard that never fires is caught. Discrimination: the other three must stay ``pass``, so a guard
    that fires too widely is caught — and the two parametrisations swap which check is which, so
    neither can be satisfied by a verdict that is simply hard-coded.
    """
    intact = store.finalized_payload(committed_receipt.receipt_id)
    assert intact is not None
    deep = {**intact, field: _nested_value(_NEST_BEYOND_ANY_READER)}
    monkeypatch.setattr(store, "finalized_payload", lambda _receipt_id: deep)
    assert _verdict(committed_receipt, store) == _all_pass(**{defeated: "fail"})


@pytest.mark.parametrize(("field", "defeated"), _ONE_REHASH_DEFEATED)
async def test_the_verify_endpoint_reports_only_THAT_check_failed_for_an_unhashable_value(
    committed_receipt, store, monkeypatch, field, defeated
):
    """The same attribution, published. A 500 here would erase three checks that succeeded."""
    intact = store.finalized_payload(committed_receipt.receipt_id)
    assert intact is not None
    deep = {**intact, field: _nested_value(_NEST_BEYOND_ANY_READER)}
    monkeypatch.setattr(store, "finalized_payload", lambda _receipt_id: deep)
    async with _server_client_for(_verify_app(store)) as client:
        response = await client.get(_verify_path(committed_receipt.receipt_id))
    assert response.status_code == 200
    assert response.json() == {
        "receipt_id": committed_receipt.receipt_id,
        "checks": _all_pass(**{defeated: "fail"}),
    }


def test_an_OSError_from_the_READ_is_not_normalized_into_a_verdict(committed_receipt, store, monkeypatch):
    """The read boundary absorbs a recursion, never a disk fault.

    ``_read_json`` now converts ``RecursionError`` into the ``ValueError`` the verifier absorbs. The
    conversion is scoped to the parse; the ``read_text`` beside it is not covered, and this test is
    what says so. Widening it to the whole read would make an unmounted volume report every receipt
    as invalid — a false verdict standing in for an outage, in the one function whose entire purpose
    is to keep those two apart.
    """

    def _unreadable_disk(*_args: Any, **_kwargs: Any) -> str:
        raise OSError("simulated unreadable disk")

    monkeypatch.setattr(Path, "read_text", _unreadable_disk)
    with pytest.raises(OSError, match="simulated unreadable disk"):
        store.finalized_payload(committed_receipt.receipt_id)
    with pytest.raises(OSError, match="simulated unreadable disk"):
        verify_receipt(committed_receipt.receipt_id, store)


def test_an_OSError_from_a_REHASH_is_not_swallowed_by_the_recursion_guard(committed_receipt, store, monkeypatch):
    """The re-derivation guard absorbs ``RecursionError`` and nothing else.

    Without this the guard and a bare ``except Exception`` are observationally identical, and the
    broad version would report ``manifest: fail`` for any fault inside the hash — a receipt declared
    unverifiable because the service broke while checking it. That is the same laundering the route
    refuses to do, moved one level down where it is harder to see.
    """

    def _boom(_manifest: dict[str, Any]) -> str:
        raise OSError("simulated fault inside the hash")

    monkeypatch.setattr("veridex.signal_trials.receipts.run_manifest_hash", _boom)
    with pytest.raises(OSError, match="simulated fault inside the hash"):
        verify_receipt(committed_receipt.receipt_id, store)


def test_the_deep_row_tolerance_is_SCOPED_to_the_verifier(committed_receipt, store):
    """The money path still refuses a row it cannot parse, rather than reading it as absent.

    The companion to ``test_the_unreadable_row_tolerance_is_SCOPED_to_the_verifier`` for the depth
    case. ``ValueError`` and not ``RecursionError``: normalizing at the read is what lets every
    existing caller keep its single ``except ValueError`` for corruption, and a ``None`` here would
    tell the payment path that a paid slot points at nothing.
    """
    _nest_row_field(store, committed_receipt.receipt_id, field="payer", depth=_NEST_BEYOND_ANY_READER, shape="array")
    with pytest.raises(ValueError):
        store.finalized_payload(committed_receipt.receipt_id)
    with pytest.raises(ValueError):
        store.record(committed_receipt.receipt_id)


async def test_the_verify_endpoint_answers_404_when_no_store_is_configured():
    """No commit store means no receipt can exist, which is the same honest 404 this route
    already served at H1.2 — an unmounted deployment's answer does not change."""
    async with _client_for(_verify_app(None)) as client:
        response = await client.get(_verify_path("rcpt_anything"))
    assert response.status_code == 404
    assert response.json() == {"error": "receipt_not_found"}
