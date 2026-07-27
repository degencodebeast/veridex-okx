"""Outcome settlement, participant settlement, agent records and the staged verifier (plan H4.3).

Four things land here, and the split between the first two is the whole point of the task:

``TrialOutcome`` / :func:`~veridex.signal_trials.live.settle_trial_outcome`
    EVENT level. What the market did, once, for the whole trial. **No participant data.**
``ParticipantSettlement`` / :func:`~veridex.signal_trials.live.settle_commit`
    PARTICIPANT level. Joins ONE finalized commit to ONE outcome. Two payers who committed
    different probabilities against the same event get two different Briers, which is exactly
    what a per-event record could not express.
``AgentRecord`` / :func:`~veridex.signal_trials.live.build_agent_record`
    The aggregate over a payer's FINALIZED commits only, recomputed from primary artifacts.
:data:`~veridex.signal_trials.receipts.VERIFY_OUTCOME_CHECKS`
    The four settlement-time checks, added to the same report the four commit-time checks
    already live in. The report is now THREE-valued.

Three honesty properties are what this file exists to defend, and each one has a named trap:

* **``pending`` and ``UNSCORED`` are different facts and BOTH carry ``None`` metrics.**
  ``pending`` means the answer is not knowable yet; ``UNSCORED`` means the window closed without
  one. A test that asserts only ``brier is None`` cannot tell them apart — that is the H2.4
  defect class, where three branches sharing a null combination all passed with their guard
  deleted. **Every test below that touches either state asserts the ``status``**, and
  ``test_pending_and_UNSCORED_differ_ONLY_in_status_not_in_their_metrics`` pins the two side by
  side so the distinction cannot quietly collapse into one branch.

* **The UNSCORED boundary is ``T + bar_ms + FETCH_GRACE_MS``, never ``T``.** The candle a trial
  settles against closes in ``[T, T + bar)``, so it cannot even EXIST as completed before its own
  close; declaring UNSCORED at ``T + 1ms`` would report "no answer" about a bar that had not
  finished forming. The boundary is tested at BOTH frozen bars because a boundary that forgot
  ``bar_ms`` still passes the 1m case (60s of grace is swallowed by the 600s fetch grace) and
  fails only the 1h one.

* **An outcome check reports ``pending``, not ``fail``, until a settled outcome exists.**
  Collapsing the report to two values would publish "this receipt fails verification" about a
  trial whose horizon has simply not been reached, which is the same lie as an unscored trial
  shown as a loss.

**Every discriminating verify test asserts the WHOLE EIGHT-KEY verdict**, via :func:`_clean`, so a
check that stopped discriminating shows up as a diff rather than as a still-green suite; a
single-key assertion cannot tell "this check noticed" from "every check fails on every mutation".

The block below ``FROZEN MANDATED RED BLOCK`` is reproduced BYTE-IDENTICALLY from the
implementation plan (lines 972-1016) under ``PKT-DEC-C8``: those bytes are what the captured RED
attests to, so lint and type gates do not outrank the freeze. It is fenced with ``fmt: off`` so the
formatter leaves it alone; the fence lines sit OUTSIDE the frozen bytes and change none of them.
sha256 of the 45 frozen lines: ``d5d13d38792cb85a018505c855b00fddf95e770b0a65ce243ee5198b0884422d``.

The frozen block reads ``trial.bar`` off the two bar fixtures. :class:`_BarredTrial` is what makes
that literal true without touching :class:`~veridex.signal_trials.live.LiveTrial`: a test-local
subclass carrying the bar LABEL the block names. Nothing in the settlement path reads it — the bar
width arrives on the ``CandleSeries``, which is where §7 puts the provenance — so the subclass adds
a fixture attribute and no behaviour.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from importlib import util as importlib_util
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from veridex.api.signal_trials_router import register_signal_trials_routes
from veridex.api.signal_trials_schemas import (
    AgentRecordResponse,
    CommitReceiptResponse,
    CommitRequest,
    TrialOutcomeModel,
    TrialResponse,
    VerifyReceiptResponse,
)
from veridex.signal_trials.challenge_spec import CanonicalSignal
from veridex.signal_trials.live import (
    FETCH_GRACE_MS,
    MARKOUT_CAP_BPS,
    AgentRecord,
    LiveTrial,
    LiveTrialRepository,
    build_agent_record,
    commit_action,
    open_live_trial,
    settle_commit,
    settle_trial,
    settle_trial_outcome,
    unscored_boundary_ms,
    unsettled_commit,
)
from veridex.signal_trials.okx_client import BAR_MS, Candle, CandleSeries
from veridex.signal_trials.preflight import FROZEN_HORIZON_MS
from veridex.signal_trials.receipts import (
    _FINALIZED_DIRNAME,
    _OUTCOMES_DIRNAME,
    OUTCOME_FIELDS,
    OUTCOME_PROVENANCE_FIELDS,
    SETTLEMENT_FIELDS,
    SETTLEMENT_LAW_VERSION,
    VERIFY_COMMIT_CHECKS,
    VERIFY_OUTCOME_CHECKS,
    CommitRecord,
    OutcomeProvenance,
    ParticipantSettlement,
    ReceiptStore,
    SettledTrial,
    TrialOutcome,
    verify_receipt,
)
from veridex.signal_trials.spot_markout import SpotMarkoutError

#: The wall clock every fixture freezes on. A fixed epoch rather than ``time.time()`` so a
#: boundary verdict is reproducible and a failure is not a function of when it ran.
T0 = 1_700_000_000_000

#: The settlement target: ``T = t0 + horizon``, with the frozen 1h ranking horizon (§8.3).
T = T0 + FROZEN_HORIZON_MS

#: The RESOLVED trial id, and the PAYER'S differing spelling of it. The two differ in case for the
#: same reason ``test_receipts.py`` makes them differ: resolution is permitted to canonicalize, the
#: body hash was taken over the payer's spelling, and a fixture where they coincide would let a
#: verifier that re-derived from the wrong one pass by luck.
TRIAL_ID = "trial_h43"
PAYER_TRIAL_SPELLING = "TRIAL_H43"

#: A second trial, used wherever "this outcome belongs to a DIFFERENT event" has to be expressible.
OTHER_TRIAL_ID = "trial_h43_other"

PAYER = "0xb"
OTHER_PAYER = "0xc"

#: A synthetic transaction hash. Repeated nibbles, so it is not and cannot resemble a real one.
TX_HASH = "0x" + "d" * 64

#: The sealed entry (``trigger_price`` at t0) and a settlement close that clears the modeled cost.
#: ``(0.0130 - 0.0125) / 0.0125 * 1e4 == 400`` bps gross, so at the official 25 bps the follow leg
#: is ``+375`` and the fade leg ``-425``: ``follow_profitable`` is True and the two legs are far
#: enough apart that a sign error cannot land on the other one's value.
ENTRY = 0.0125
FUTURE = 0.0130
GROSS_BPS = 400
COST_BPS = 25
FOLLOW_BPS = GROSS_BPS - COST_BPS
FADE_BPS = -GROSS_BPS - COST_BPS

_ABSENT = object()


# ------------------------------------------------------------------------------ builders


def _sig(**overrides: Any) -> CanonicalSignal:
    """A canonical signal observed exactly at :data:`T0`.

    Every field is a synthetic constant; ``trigger_wallet_address`` is a repeated-nibble address
    that no chain can hold, so nothing here is or resembles a real credential.
    """
    fields_: dict[str, Any] = {
        "t0_ms": T0,
        "chain_index": "196",
        "token_address": "0x" + "1" * 40,
        "symbol": "TKN",
        "name": "Token",
        "market_cap_usd": 1_500_000.0,
        "holders": 4_200,
        "top10_holder_percent": 31.5,
        "trigger_price": ENTRY,
        "wallet_type": "smart money",
        "trigger_wallet_count": 3,
        "trigger_wallet_address": "0x" + "2" * 40,
        "amount_usd": 25_000.0,
    }
    return CanonicalSignal(**{**fields_, **overrides})


@dataclass(frozen=True)
class _BarredTrial(LiveTrial):
    """A trial carrying the bar LABEL the frozen RED block reads off it.

    Test-local by design. The settlement path takes its bar width from the ``CandleSeries`` —
    §7 puts the provenance on the series because the wire carries no bar field — so this
    attribute is read by the frozen block and by nothing in production. Defining it here rather
    than widening :class:`~veridex.signal_trials.live.LiveTrial` keeps the published trial
    document's shape untouched.
    """

    bar: str = "1m"


def _trial(*, trial_id: str = TRIAL_ID, bar: str = "1m", **sig_overrides: Any) -> _BarredTrial:
    """A live trial opened through the PRODUCTION entrypoint, tagged with a bar label.

    Built by :func:`~veridex.signal_trials.live.open_live_trial` rather than by hand so every
    deadline and evidence-hash assertion below is a statement about the settlement path and not
    about a fixture that agreed with it by construction.
    """
    base = open_live_trial(_sig(**sig_overrides), now_ms=T0, trial_id=trial_id)
    return _BarredTrial(**{field.name: getattr(base, field.name) for field in fields(base)}, bar=bar)


def _candle(*, ts_open_ms: int, close: float = FUTURE, confirmed: bool = True) -> Candle:
    """One candle. Only ``ts_open_ms``, ``close`` and ``confirmed`` matter to settlement."""
    return Candle(
        ts_open_ms=ts_open_ms,
        open=ENTRY,
        high=max(ENTRY, close),
        low=min(ENTRY, close),
        close=close,
        vol=1_000.0,
        vol_usd=10_000.0,
        confirmed=confirmed,
    )


def _series(
    *,
    bar: str = "1m",
    bar_ms: int = 60_000,
    lag_ms: int = 0,
    close: float = FUTURE,
    confirmed: bool = True,
) -> CandleSeries:
    """A one-candle series whose candle closes ``lag_ms`` after :data:`T`.

    ``lag_ms=0`` is the exact-boundary case §7 declares valid (``close_ts == T``). The candle's
    open is derived from the intended CLOSE — ``ts_open = T + lag - bar_ms`` — so a test states
    the lag it means rather than an open time a reader has to re-derive the lag from.
    """
    return CandleSeries(
        bar=bar, bar_ms=bar_ms, candles=(_candle(ts_open_ms=T + lag_ms - bar_ms, close=close, confirmed=confirmed),)
    )


def _empty_series(*, bar: str = "1m", bar_ms: int = 60_000) -> CandleSeries:
    """A series that carries bar provenance and no candles — the honest "we looked, nothing" shape."""
    return CandleSeries(bar=bar, bar_ms=bar_ms, candles=())


def _req(p: float, **overrides: Any) -> CommitRequest:
    """A commit request carrying probability ``p``, in the PAYER'S spelling of the trial id."""
    fields_: dict[str, Any] = {
        "trial_id": PAYER_TRIAL_SPELLING,
        "p_follow_profitable": p,
        "methodology_version": "unit-test-1",
    }
    return CommitRequest(**{**fields_, **overrides})


def _finalized(
    *, p: float, trial_id: str = TRIAL_ID, payer: str = PAYER, receipt_id: str = "rcpt_fixture"
) -> CommitRecord:
    """A finalized commit record, constructed directly.

    Direct construction is correct HERE and would be wrong in the verify tests below, and the
    difference is what each side is asking. :func:`~veridex.signal_trials.live.settle_commit` is a
    pure function of a record's VALUES, so a hand-built record exercises it exactly as a stored one
    would. Verification asks whether a STORED row re-derives its own seals, which only the real
    two-phase write path can produce — those tests use :func:`_commit`.
    """
    return CommitRecord(
        receipt_id=receipt_id,
        trial_id=trial_id,
        payer=payer,
        p_follow_profitable=p,
        methodology_version="unit-test-1",
        body_hash="0" * 64,
        payment_tx_hash=TX_HASH,
        committed_at_ms=T0 + 1_000,
        commit_deadline_ms=T0 + 300_000,
        trial_mode="live",
    )


def _settled_outcome_from(series: CandleSeries, *, trial_id: str = TRIAL_ID) -> SettledTrial:
    """The recordable settlement of :data:`TRIAL_ID` against ``series``.

    Returns the outcome TOGETHER WITH its provenance, because those are two different claims: the
    outcome says what the market did, the provenance says which bar, which law and which candle it
    was derived from — and the four outcome checks re-derive the second from the first. Asserted
    settled in the helper so a test that depends on a settled outcome cannot silently receive a
    pending one and pass for the wrong reason.
    """
    settled = settle_trial(_trial(trial_id=trial_id), series, now_ms=T + FROZEN_HORIZON_MS)
    assert settled.outcome.status == "settled", "the settled-outcome helper did not produce a settled outcome"
    return settled


def _outcome(status: str, **overrides: Any) -> TrialOutcome:
    """A :class:`TrialOutcome` in ``status``, with settled metrics only where they belong.

    The non-settled shape is spelled out field by field rather than defaulted, because "every
    metric is ``None``" is the property that makes ``pending`` and ``UNSCORED`` indistinguishable
    by their metrics alone — it has to be visible in the fixture, not hidden behind a default.
    """
    settled = status == "settled"
    base: dict[str, Any] = {
        "trial_id": TRIAL_ID,
        "status": status,
        "entry": ENTRY,
        "future": FUTURE if settled else None,
        "close_ts_ms": T if settled else None,
        "observation_lag_ms": 0 if settled else None,
        "follow_markout_bps": FOLLOW_BPS if settled else None,
        "fade_markout_bps": FADE_BPS if settled else None,
        "follow_profitable": True if settled else None,
    }
    return TrialOutcome(**{**base, **overrides})


# ------------------------------------------------------------------------------ store


class _TamperableStore(ReceiptStore):
    """TEST-ONLY raw writer over the finalized and outcome directories.

    Verification is only meaningful against a row that something CHANGED after it was sealed, and
    nothing in the production store can change one. The mutation therefore lives outside the
    production API, exactly as ``test_receipts.py`` argues: a ``tamper`` method on the real store
    would be a supported way to rewrite a paid receipt or a settled outcome.

    ``field`` selects the row: an ``outcome.`` prefix rewrites the OUTCOME row of the receipt's
    trial, anything else rewrites the finalized RECEIPT row. One method rather than two because
    every caller is making the same statement — "an adversary with write access changed this
    stored field" — and the prefix is what names which artifact they reached.
    """

    def tamper(self, receipt_id: str, *, field: str, value: Any = _ABSENT) -> None:
        """Overwrite (or, with no ``value``, delete) one raw field of a stored row."""
        if field.startswith("outcome."):
            receipt = json.loads(
                (Path(self.root) / _FINALIZED_DIRNAME / f"{receipt_id}.json").read_text(encoding="utf-8")
            )
            path = Path(self.root) / _OUTCOMES_DIRNAME / f"{receipt['trial_id']}.json"
            key = field[len("outcome.") :]
        else:
            path = Path(self.root) / _FINALIZED_DIRNAME / f"{receipt_id}.json"
            key = field
        payload = json.loads(path.read_text(encoding="utf-8"))
        if value is _ABSENT:
            payload.pop(key, None)
        else:
            payload[key] = value
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def corrupt_outcome(self, trial_id: str, *, raw: str) -> None:
        """Replace an outcome row's bytes wholesale, including with bytes that are not JSON."""
        (Path(self.root) / _OUTCOMES_DIRNAME / f"{trial_id}.json").write_text(raw, encoding="utf-8")


def _commit(
    store: ReceiptStore,
    trial: LiveTrial,
    *,
    staging_id: str = "s1",
    p: float = 0.8,
    payer: str = PAYER,
) -> CommitRecord:
    """Drive one commitment through the REAL two-phase path and return its finalized record.

    Stage, journal, finalize — the same three writes the payment wrapper performs, in the same
    order, so a row under verification is sealed by production code rather than hand-built.
    """
    store.stage(
        staging_id=staging_id,
        trial_id=trial.trial_id,
        payer=payer,
        body=_req(p, trial_id=PAYER_TRIAL_SPELLING if trial.trial_id == TRIAL_ID else trial.trial_id),
        staged_at_ms=T0 + 1_000,
        commit_deadline_ms=trial.commit_deadline_ms,
        trial_mode=trial.trial_mode,
    )
    store.journal(staging_id, payer=payer, tx_hash=TX_HASH)
    record = store.record(store.finalize_from_journal(staging_id))
    assert record is not None
    return record


# ------------------------------------------------------------------------------ verdict helpers


def _verdict(receipt_id: str, store: ReceiptStore) -> dict[str, str]:
    """The whole eight-key verdict, as a plain dict, for equality assertions."""
    return dict(verify_receipt(receipt_id, store).checks)


def _clean(**overrides: str) -> dict[str, str]:
    """The verdict for an intact receipt over a SETTLED outcome, with named checks overridden.

    Every discriminating assertion is written against this, so the assertion states BOTH what
    changed and that nothing else did.
    """
    return {
        **dict.fromkeys(VERIFY_COMMIT_CHECKS, "pass"),
        **dict.fromkeys(VERIFY_OUTCOME_CHECKS, "pass"),
        **overrides,
    }


def _unsettled(**overrides: str) -> dict[str, str]:
    """The verdict for an intact receipt with NO settled outcome behind it.

    The four outcome checks are ``pending`` — not ``fail`` — because nothing has been shown to be
    wrong; there is simply no settlement to re-derive yet.
    """
    return {
        **dict.fromkeys(VERIFY_COMMIT_CHECKS, "pass"),
        **dict.fromkeys(VERIFY_OUTCOME_CHECKS, "pending"),
        **overrides,
    }


# ------------------------------------------------------------------------------ fixtures


@pytest.fixture
def store(tmp_path: Path) -> _TamperableStore:
    """A store rooted in this test's own ``tmp_path``, so no two tests share state."""
    return _TamperableStore(tmp_path)


@pytest.fixture
def live_trial() -> _BarredTrial:
    """An open live trial on the 1m bar."""
    return _trial()


@pytest.fixture
def live_trial_1m() -> _BarredTrial:
    return _trial(bar="1m")


@pytest.fixture
def live_trial_1h() -> _BarredTrial:
    return _trial(bar="1H")


@pytest.fixture
def series_1m() -> CandleSeries:
    """A 1m series whose single confirmed candle closes exactly at :data:`T`."""
    return _series()


@pytest.fixture
def settled_outcome(series_1m: CandleSeries) -> TrialOutcome:
    """A settled outcome with ``follow_profitable is True``, produced by the settlement path."""
    outcome = settle_trial_outcome(_trial(), series_1m, now_ms=T + FROZEN_HORIZON_MS)
    assert outcome.status == "settled" and outcome.follow_profitable is True
    return outcome


@pytest.fixture
def pending_outcome() -> TrialOutcome:
    """A pending outcome: the horizon has not been reached and no candle was found."""
    outcome = settle_trial_outcome(_trial(), _empty_series(), now_ms=T0 + 60_000)
    assert outcome.status == "pending"
    return outcome


@pytest.fixture
def unscored_outcome() -> TrialOutcome:
    """An UNSCORED outcome: the settlement window and its fetch grace both expired, empty-handed."""
    outcome = settle_trial_outcome(_trial(), _empty_series(), now_ms=T + 60_000 + FETCH_GRACE_MS)
    assert outcome.status == "UNSCORED"
    return outcome


@pytest.fixture
def committed_receipt(store: _TamperableStore, live_trial: _BarredTrial) -> CommitRecord:
    """One finalized, intact receipt on :data:`TRIAL_ID`, sealed through the production path."""
    return _commit(store, live_trial)


@pytest.fixture
def settled_receipt(store: _TamperableStore, committed_receipt: CommitRecord, series_1m: CandleSeries) -> CommitRecord:
    """A finalized receipt whose trial ALSO has a settled outcome recorded against it."""
    store.record_outcome(committed_receipt.trial_id, _settled_outcome_from(series_1m))
    return committed_receipt


@pytest.fixture
def store_with_mixed(store: _TamperableStore) -> _TamperableStore:
    """A store holding every row shape an agent record must tell apart.

    For :data:`PAYER`: a finalized commit on a SETTLED trial, one on an UNSCORED trial, one on a
    trial with no outcome recorded at all (pending), and a STAGED row that was never paid for.
    For :data:`OTHER_PAYER`: a finalized commit on the settled trial. The record for
    :data:`PAYER` must count three commits — not four, and not five.
    """
    settled_trial = _trial(trial_id=TRIAL_ID)
    unscored_trial = _trial(trial_id=OTHER_TRIAL_ID)
    open_trial = _trial(trial_id="trial_h43_open")

    _commit(store, settled_trial, staging_id="s_settled", p=0.8)
    _commit(store, unscored_trial, staging_id="s_unscored", p=0.3)
    _commit(store, open_trial, staging_id="s_open", p=0.5)
    _commit(store, settled_trial, staging_id="s_other", p=0.9, payer=OTHER_PAYER)
    # Received, never paid for. A free read must never serve it and an agent record must never
    # count it: a staged row is a commitment, not a commitment that was PAID.
    store.stage(staging_id="s_staged", trial_id="trial_h43_staged", payer=PAYER, body=_req(0.7), staged_at_ms=T0)

    store.record_outcome(TRIAL_ID, _settled_outcome_from(_series(), trial_id=TRIAL_ID))
    store.record_outcome(
        OTHER_TRIAL_ID,
        settle_trial(unscored_trial, _empty_series(), now_ms=T + 60_000 + FETCH_GRACE_MS),
    )
    return store


# ------------------------------------------------------------------------------ app helpers


def _app(store: ReceiptStore | None = None, live_trials: Any = None) -> FastAPI:
    """A bare app carrying only the signal-trials routes."""
    app = FastAPI()
    register_signal_trials_routes(app, store=store, live_trials=live_trials)
    return app


def _client(app: FastAPI, *, raise_app_exceptions: bool = True) -> AsyncClient:
    """An in-process client over ``app``; no socket is opened."""
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions), base_url="http://sig"
    )


def _settle_script() -> Any:
    """Load ``scripts/signal_trials/settle_live_trials.py`` BY PATH.

    By path rather than by import because ``scripts/signal_trials`` is not a package — which is
    also why the script keeps local copies of the transport and credential reader instead of
    importing them from ``run_preflight.py``. ``test_preflight.py`` loads its script the same way.
    """
    script = Path(__file__).resolve().parents[2] / "scripts" / "signal_trials" / "settle_live_trials.py"
    spec = importlib_util.spec_from_file_location("settle_live_trials_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _OneTrialRepo:
    """The narrowest live-trial repository the routes need: one known trial, nothing open."""

    def __init__(self, trial: LiveTrial) -> None:
        self._trial = trial

    def current(self) -> LiveTrial | None:
        return self._trial

    def get(self, trial_id: str) -> LiveTrial | None:
        return self._trial if trial_id == self._trial.trial_id else None


# ==========================================================================================
# FROZEN MANDATED RED BLOCK — plan lines 972-1016, byte-identical (PKT-DEC-C8).
# Do not reformat, reorder, split or lint-fix. sha256
# d5d13d38792cb85a018505c855b00fddf95e770b0a65ce243ee5198b0884422d.
# ==========================================================================================
# fmt: off
def test_outcome_pending_before_horizon(live_trial):
    o = settle_trial_outcome(live_trial, None, now_ms=live_trial.t0_ms + 60_000)
    assert o.status == "pending" and o.future is None

def test_outcome_still_pending_just_after_T_without_candle(live_trial_1m, live_trial_1h):
    # the valid candle may not close until T + bar (Codex r3 #3) — never UNSCORED at T+1ms
    for trial, bar_ms in ((live_trial_1m, 60_000), (live_trial_1h, 3_600_000)):
        T = trial.t0_ms + 3_600_000
        empty = CandleSeries(bar=trial.bar, bar_ms=bar_ms, candles=())
        assert settle_trial_outcome(trial, empty, now_ms=T + 1).status == "pending"
        assert settle_trial_outcome(trial, empty, now_ms=T + bar_ms + FETCH_GRACE_MS - 1).status == "pending"
        assert settle_trial_outcome(trial, empty, now_ms=T + bar_ms + FETCH_GRACE_MS).status == "UNSCORED"

def test_outcome_settled_with_candle(live_trial, series_1m):
    o = settle_trial_outcome(live_trial, series_1m, now_ms=live_trial.t0_ms + 3_700_000)
    assert o.status == "settled" and o.follow_profitable is not None and o.observation_lag_ms < 60_000

def test_two_probabilities_one_outcome(settled_outcome):
    a = settle_commit(_finalized(p=0.8), settled_outcome)   # follow_profitable == True in fixture
    b = settle_commit(_finalized(p=0.3), settled_outcome)
    assert a.action == "FOLLOW" and b.action == "FADE"
    assert a.brier == pytest.approx((0.8 - 1) ** 2) and b.brier == pytest.approx((0.3 - 1) ** 2)
    assert a.chosen_markout_bps == settled_outcome.follow_markout_bps and b.chosen_markout_bps == settled_outcome.fade_markout_bps

def test_pending_and_unscored_propagate(pending_outcome, unscored_outcome):
    assert settle_commit(_finalized(p=0.6), pending_outcome).brier is None
    assert settle_commit(_finalized(p=0.6), unscored_outcome).status == "UNSCORED"

def test_agent_record_aggregates_finalized_only_and_recomputes(store_with_mixed):
    rec1 = build_agent_record("0xb", store_with_mixed)
    rec2 = build_agent_record("0xb", store_with_mixed)
    assert rec1 == rec2 and rec1.qualified is False and rec1.pending >= 0   # idempotent; no live skill claim

def test_full_verify_pending_then_settled(committed_receipt, store, series_1m):
    rep = verify_receipt(committed_receipt.receipt_id, store)
    assert all(rep.checks[k] == "pass" for k in VERIFY_COMMIT_CHECKS)
    assert all(rep.checks[k] == "pending" for k in VERIFY_OUTCOME_CHECKS)   # honest pre-horizon state
    store.record_outcome(committed_receipt.trial_id, _settled_outcome_from(series_1m))
    rep2 = verify_receipt(committed_receipt.receipt_id, store)
    assert all(rep2.checks[k] == "pass" for k in VERIFY_COMMIT_CHECKS + VERIFY_OUTCOME_CHECKS)

def test_outcome_source_tamper_fails_only_that_check(settled_receipt, store):
    store.tamper(settled_receipt.receipt_id, field="outcome.close_ts_ms", value=1)
    rep = verify_receipt(settled_receipt.receipt_id, store)
    assert rep.checks["outcome_source"] == "fail" and rep.checks["body_hash"] == "pass"
# fmt: on
# ==========================================================================================
# END FROZEN MANDATED RED BLOCK
# ==========================================================================================


# ------------------------------------------------------------------------------ the boundary


@pytest.mark.parametrize("bar_ms", sorted(BAR_MS.values()))
def test_the_unscored_boundary_is_T_plus_bar_plus_grace(bar_ms: int) -> None:
    """ACCEPTANCE: the boundary is exactly ``T + bar_ms + FETCH_GRACE_MS`` at both frozen bars.

    Stated as the arithmetic rather than as a literal so the test says WHY the number is that
    number: the settlement candle closes in ``[T, T + bar)``, so no candle can be shown missing
    until a full bar past ``T`` has elapsed, and the fetch grace covers the interval in which a
    closed candle is not yet retrievable.
    """
    assert unscored_boundary_ms(T0, FROZEN_HORIZON_MS, bar_ms) == T0 + FROZEN_HORIZON_MS + bar_ms + FETCH_GRACE_MS


def test_the_boundary_MOVES_with_the_bar_width() -> None:
    """DISCRIMINATION: a boundary that ignored ``bar_ms`` would be the same for both frozen bars.

    This is the control the parametrised acceptance test above cannot be: an implementation that
    returned ``T + FETCH_GRACE_MS`` satisfies neither, but one that returned
    ``T + 60_000 + FETCH_GRACE_MS`` for every bar passes the 1m case and only this comparison
    catches it.
    """
    narrow = unscored_boundary_ms(T0, FROZEN_HORIZON_MS, BAR_MS["1m"])
    wide = unscored_boundary_ms(T0, FROZEN_HORIZON_MS, BAR_MS["1H"])
    assert wide - narrow == BAR_MS["1H"] - BAR_MS["1m"] == 3_540_000


def test_a_1h_trial_is_still_pending_where_a_1m_trial_is_already_UNSCORED() -> None:
    """The bar-blind boundary bug, stated as the behaviour it would produce.

    At ``T + 60_000 + FETCH_GRACE_MS`` the 1m trial's window has closed and the 1h trial's has
    not, because the 1h settlement candle may not have closed yet. An implementation that used a
    single fixed bar width would report the same status for both.
    """
    at = T + BAR_MS["1m"] + FETCH_GRACE_MS
    assert settle_trial_outcome(_trial(), _empty_series(bar="1m", bar_ms=BAR_MS["1m"]), now_ms=at).status == "UNSCORED"
    assert settle_trial_outcome(_trial(), _empty_series(bar="1H", bar_ms=BAR_MS["1H"]), now_ms=at).status == "pending"


def test_pending_and_UNSCORED_differ_ONLY_in_status_not_in_their_metrics() -> None:
    """The H2.4 trap, pinned: both states carry identical ``None`` metrics.

    Every metric field is equal between the two outcomes, so ANY assertion written over the
    metrics alone — ``future is None``, ``brier is None``, ``follow_profitable is None`` — passes
    for both and therefore distinguishes neither. ``status`` is the only field that separates
    them, which is why every test in this file that touches either state asserts it.
    """
    pending = settle_trial_outcome(_trial(), _empty_series(), now_ms=T0 + 60_000)
    unscored = settle_trial_outcome(_trial(), _empty_series(), now_ms=T + 60_000 + FETCH_GRACE_MS)
    assert pending.status == "pending" and unscored.status == "UNSCORED"
    assert replace(pending, status="x") == replace(unscored, status="x"), (
        "pending and UNSCORED are identical once status is removed — a metrics-only assertion cannot tell them apart"
    )


def test_the_instant_BEFORE_the_boundary_is_pending_and_the_boundary_itself_is_UNSCORED() -> None:
    """The boundary is inclusive at the top: ``now >= boundary`` is UNSCORED, ``boundary - 1`` is not."""
    boundary = unscored_boundary_ms(T0, FROZEN_HORIZON_MS, BAR_MS["1m"])
    assert settle_trial_outcome(_trial(), _empty_series(), now_ms=boundary - 1).status == "pending"
    assert settle_trial_outcome(_trial(), _empty_series(), now_ms=boundary).status == "UNSCORED"


def test_a_series_that_was_never_fetched_uses_the_WIDEST_frozen_bar() -> None:
    """``series=None`` carries no bar provenance, so the boundary is taken at the widest frozen bar.

    The plan's contract says "no candle AND ``now_ms >= unscored_boundary`` -> UNSCORED" without
    saying which ``bar_ms`` applies when no series — and therefore no bar — was supplied. This is
    the resolution, and it is chosen in the only direction that cannot declare UNSCORED early:
    under any narrower bar the window closes SOONER, so the widest frozen bar is the last instant
    at which a valid candle could still be forming. It never yields UNSCORED while a candle might
    still close, and UNSCORED stays reachable rather than becoming an unreachable state.
    """
    widest = unscored_boundary_ms(T0, FROZEN_HORIZON_MS, max(BAR_MS.values()))
    assert settle_trial_outcome(_trial(), None, now_ms=widest - 1).status == "pending"
    assert settle_trial_outcome(_trial(), None, now_ms=widest).status == "UNSCORED"
    # DISCRIMINATION: the 1m boundary is long past at this instant, and is NOT what was used.
    assert unscored_boundary_ms(T0, FROZEN_HORIZON_MS, BAR_MS["1m"]) < widest - 1


# ------------------------------------------------------------------------------ settling an event


def test_a_settled_outcome_carries_the_close_boundary_arithmetic(live_trial: _BarredTrial) -> None:
    """``close_ts = ts_open + bar_ms``, ``observation_lag = close_ts - T``, and the markout legs."""
    outcome = settle_trial_outcome(live_trial, _series(lag_ms=0), now_ms=T + 1)
    assert outcome.status == "settled"
    assert outcome.entry == ENTRY and outcome.future == FUTURE
    assert outcome.close_ts_ms == T and outcome.observation_lag_ms == 0
    assert outcome.follow_markout_bps == FOLLOW_BPS and outcome.fade_markout_bps == FADE_BPS
    assert outcome.follow_profitable is True


def test_a_sub_bar_lag_is_reported_rather_than_rounded_away(live_trial: _BarredTrial) -> None:
    """A candle closing 30s after ``T`` settles the trial and reports the 30s lag.

    §8.8 requires observation lag to be DISPLAYED everywhere; an implementation that reported
    zero would make a late settlement look instantaneous.
    """
    outcome = settle_trial_outcome(live_trial, _series(lag_ms=30_000), now_ms=T + 60_000)
    assert outcome.status == "settled" and outcome.observation_lag_ms == 30_000
    assert outcome.close_ts_ms == T + 30_000


def test_a_candle_settles_the_trial_even_before_the_unscored_boundary(live_trial: _BarredTrial) -> None:
    """A found candle settles at ANY clock, because its existence does not depend on the clock.

    The boundary decides when ABSENCE becomes UNSCORED. It has no bearing on a candle that was
    already found, and gating settlement on it would leave a settleable trial reported as pending.
    """
    outcome = settle_trial_outcome(live_trial, _series(), now_ms=T)
    assert outcome.status == "settled"


def test_an_unconfirmed_candle_does_not_settle_the_trial(live_trial: _BarredTrial) -> None:
    """§7: an in-progress bar's close is a moving number, so it is never eligible.

    DISCRIMINATION for the settled path: the same series with ``confirmed=True`` settles, so this
    asserts the confirm flag is read rather than that this series is unusable for some other reason.
    """
    unconfirmed = _series(confirmed=False)
    assert settle_trial_outcome(live_trial, unconfirmed, now_ms=T + 1).status == "pending"
    assert settle_trial_outcome(live_trial, _series(confirmed=True), now_ms=T + 1).status == "settled"


def test_a_candle_a_full_bar_late_does_not_settle_the_trial(live_trial: _BarredTrial) -> None:
    """§7's half-open window: ``0 <= close_ts - T < bar_ms``. A full bar of lag is a different bar."""
    assert settle_trial_outcome(live_trial, _series(lag_ms=60_000), now_ms=T + 120_000).status == "pending"
    assert settle_trial_outcome(live_trial, _series(lag_ms=59_999), now_ms=T + 120_000).status == "settled"


def test_a_non_positive_sealed_entry_is_refused_outright() -> None:
    """A trial whose sealed entry is not a positive spot price yields NO honest outcome.

    Not even UNSCORED, which asserts something specific — a well-formed trial whose settlement
    candle is missing. A trial with a zero entry is malformed at t0, before any settlement question
    arises, and reporting it as UNSCORED would file a data-integrity fault under a market outcome.
    """
    with pytest.raises(SpotMarkoutError):
        settle_trial_outcome(_trial(trigger_price=0.0), _empty_series(), now_ms=T0)


# ------------------------------------------------------------------------------ the participant join


@pytest.mark.parametrize(
    ("p", "action"),
    [
        (1.0, "FOLLOW"),
        (0.60, "FOLLOW"),
        (0.5999999, "ABSTAIN"),
        (0.5, "ABSTAIN"),
        (0.4000001, "ABSTAIN"),
        (0.40, "FADE"),
        (0.0, "FADE"),
    ],
)
def test_the_action_bands_are_the_frozen_ones(p: float, action: str) -> None:
    """§8.1: ``>= 0.60`` FOLLOW, ``<= 0.40`` FADE, strictly between them ABSTAIN.

    Both band edges are INCLUSIVE and both are tested at the edge and one step inside it, because
    a ``>``/``>=`` slip moves exactly one value and only a test standing on that value sees it.
    """
    assert commit_action(p) == action


def test_the_join_is_per_participant_not_per_event(settled_outcome: TrialOutcome) -> None:
    """Two payers, one event, two different records — including two different chosen legs.

    The frozen block asserts the Briers differ. This asserts the rest of the participant row does
    too: the FOLLOW payer is credited the follow leg and the FADE payer the fade leg, and the two
    legs have opposite signs, so a join that returned the event's numbers unchanged would give
    both payers the same markout.
    """
    follower = settle_commit(_finalized(p=0.8, receipt_id="rcpt_follow"), settled_outcome)
    fader = settle_commit(_finalized(p=0.3, receipt_id="rcpt_fade"), settled_outcome)
    assert follower.receipt_id == "rcpt_follow" and fader.receipt_id == "rcpt_fade"
    assert follower.chosen_markout_bps == FOLLOW_BPS and fader.chosen_markout_bps == FADE_BPS
    assert follower.chosen_markout_bps > 0 > fader.chosen_markout_bps
    assert follower.status == fader.status == "settled"


def test_an_abstaining_commit_is_scored_but_takes_no_position(settled_outcome: TrialOutcome) -> None:
    """§8.2: ABSTAIN's markout is exactly 0, and the trial is STILL Brier-scored.

    Every eligible trial is Brier-scored including neutral (§8.2, "no excluded sample"), so a
    ``None`` brier here would drop the abstainers out of the calibration sample.
    """
    abstainer = settle_commit(_finalized(p=0.5), settled_outcome)
    assert abstainer.action == "ABSTAIN"
    assert abstainer.chosen_markout_bps == 0
    assert abstainer.brier == pytest.approx(0.25)


def test_a_participant_settlement_propagates_the_event_status_with_null_metrics(
    pending_outcome: TrialOutcome, unscored_outcome: TrialOutcome
) -> None:
    """The H2.4 trap at the PARTICIPANT level: assert the status, not only the nulls.

    Both rows carry ``brier is None`` and ``chosen_markout_bps is None``, so the frozen block's
    ``brier is None`` assertion is satisfied by either state. The statuses are what separate them.
    """
    pending = settle_commit(_finalized(p=0.6), pending_outcome)
    unscored = settle_commit(_finalized(p=0.6), unscored_outcome)
    assert pending.status == "pending" and unscored.status == "UNSCORED"
    assert pending.brier is None and unscored.brier is None
    assert pending.chosen_markout_bps is None and unscored.chosen_markout_bps is None
    # The action is still derived: it comes from the payer's own probability, which is known the
    # moment the commit is finalized and does not wait on the market.
    assert pending.action == unscored.action == "FOLLOW"


def test_a_commit_cannot_be_joined_to_another_trials_outcome(settled_outcome: TrialOutcome) -> None:
    """A cross-trial join is a defect, not a result, so it raises rather than scoring.

    Returning a settlement here would score a payer's probability against an event they never
    committed to, and nothing downstream could detect it — the row would look ordinary.
    """
    with pytest.raises(ValueError, match="different trial"):
        settle_commit(_finalized(p=0.8, trial_id=OTHER_TRIAL_ID), settled_outcome)


def test_a_settled_outcome_missing_its_verdict_is_refused() -> None:
    """A ``settled`` outcome with no ``follow_profitable`` cannot be scored, and says so.

    Structurally unreachable from :func:`settle_trial_outcome`, and reachable by a hand-built or
    a corrupted record. Brier-scoring against a missing verdict would have to invent an outcome
    indicator, which is interpolation under another name.
    """
    with pytest.raises(ValueError, match="follow_profitable"):
        settle_commit(_finalized(p=0.8), _outcome("settled", follow_profitable=None))


def test_an_unsettled_commit_is_pending_rather_than_absent() -> None:
    """A finalized commit whose trial has NO recorded outcome is ``pending``, with its action known."""
    settlement = unsettled_commit(_finalized(p=0.8))
    assert settlement.status == "pending" and settlement.action == "FOLLOW"
    assert settlement.brier is None and settlement.chosen_markout_bps is None


# ------------------------------------------------------------------------------ agent records


def test_an_agent_record_counts_every_status_exactly(store_with_mixed: _TamperableStore) -> None:
    """The tallies, stated one by one. ``pending >= 0`` in the frozen block cannot fail.

    Three finalized commits for this payer: one settled, one UNSCORED, one on a trial with no
    recorded outcome (pending). The staged row and the other payer's commit are absent, and the
    counts summing to ``commits`` is what makes "absent" checkable rather than assumed.
    """
    record = build_agent_record(PAYER, store_with_mixed)
    assert record.payer == PAYER
    assert record.commits == 3
    assert record.settled == 1 and record.unscored == 1 and record.pending == 1
    assert record.settled + record.unscored + record.pending == record.commits


def test_an_agent_record_excludes_staged_rows_and_other_payers(store_with_mixed: _TamperableStore) -> None:
    """DISCRIMINATION for the aggregation predicate: the rows it must NOT count exist.

    Both exclusions are known-present negatives — a staged row for this payer and a finalized row
    for another — so a ``build_agent_record`` that scanned every row, or ignored the payer, counts
    more than three. An empty store would make this test pass for the wrong reason.
    """
    assert store_with_mixed.count_pending() == 1, "the staged row must be present for this to discriminate"
    assert len(store_with_mixed.public_records(OTHER_PAYER)) == 1
    assert build_agent_record(PAYER, store_with_mixed).commits == 3
    assert build_agent_record(OTHER_PAYER, store_with_mixed).commits == 1


def test_an_agent_records_averages_cover_the_SETTLED_rows_only(store_with_mixed: _TamperableStore) -> None:
    """One settled commit at ``p=0.8`` against ``follow_profitable=True``: Brier ``0.04``, markout ``+375``.

    The UNSCORED and pending rows contribute to neither average. Averaging them in as zeros would
    reward an agent for trials that were never scored, which §8.6 forbids in the coverage
    direction and which is the same error here.
    """
    record = build_agent_record(PAYER, store_with_mixed)
    assert record.avg_brier == pytest.approx((0.8 - 1) ** 2)
    assert record.capped_avg_markout_bps == FOLLOW_BPS


def test_an_agent_with_no_settled_trials_has_no_score(store: _TamperableStore) -> None:
    """Nullable averages, not zeros: a zero Brier is a PERFECT score, and this agent has none.

    §8.5's row model makes both nullable for the same reason, and this is the live-record twin of
    that rule.
    """
    _commit(store, _trial(), staging_id="s_only", p=0.8)
    record = build_agent_record(PAYER, store)
    assert record.commits == 1 and record.settled == 0 and record.pending == 1
    assert record.avg_brier is None and record.capped_avg_markout_bps is None


def test_an_unknown_payer_has_an_empty_record_rather_than_an_error(store: _TamperableStore) -> None:
    """Zero commits is a fact about a payer, not a failure; the ROUTE is what turns it into a 404."""
    record = build_agent_record("0xnobody", store)
    assert record.commits == 0 and record.settled == 0 and record.pending == 0 and record.unscored == 0
    assert record.avg_brier is None and record.capped_avg_markout_bps is None and record.qualified is False


def test_a_per_event_markout_is_capped_at_the_frozen_500_bps(store: _TamperableStore) -> None:
    """§8.3: ``±500`` bps per event, so one memecoin move cannot dominate a record.

    ACCEPTANCE: a ``+10_000`` bps event is carried into the average as ``+500``.
    DISCRIMINATION: the UNCAPPED value is on the record's own outcome, and differs — so a
    ``capped_avg`` that forgot to cap reports 9_975 and this test sees it.
    """
    trial = _trial()
    _commit(store, trial, staging_id="s_big", p=0.8)
    # +100% on the entry: 10_000 bps gross, 9_975 after the 25 bps cost.
    store.record_outcome(TRIAL_ID, _settled_outcome_from(_series(close=ENTRY * 2.0)))
    outcome = store.outcome(TRIAL_ID)
    assert outcome is not None and outcome.follow_markout_bps == 10_000 - COST_BPS
    assert build_agent_record(PAYER, store).capped_avg_markout_bps == MARKOUT_CAP_BPS


def test_the_cap_is_symmetric_on_the_losing_side(store: _TamperableStore) -> None:
    """The floor is ``-500``. A cap applied only above zero would let one collapse dominate."""
    trial = _trial()
    _commit(store, trial, staging_id="s_crash", p=0.8)
    store.record_outcome(TRIAL_ID, _settled_outcome_from(_series(close=ENTRY * 0.5)))
    outcome = store.outcome(TRIAL_ID)
    assert outcome is not None and outcome.follow_markout_bps == -5_000 - COST_BPS
    assert build_agent_record(PAYER, store).capped_avg_markout_bps == -MARKOUT_CAP_BPS


def test_a_markout_inside_the_cap_is_carried_UNCHANGED(store: _TamperableStore) -> None:
    """DISCRIMINATION for the cap: an implementation that clamped everything to ``±500`` fails here."""
    _commit(store, _trial(), staging_id="s_small", p=0.8)
    store.record_outcome(TRIAL_ID, _settled_outcome_from(_series()))
    assert build_agent_record(PAYER, store).capped_avg_markout_bps == FOLLOW_BPS
    assert abs(FOLLOW_BPS) < MARKOUT_CAP_BPS, "this fixture only discriminates while it sits inside the cap"


def test_qualified_is_False_even_when_every_commit_settled(store: _TamperableStore) -> None:
    """MVP live records NEVER claim skill — not a placeholder, a claim boundary (§3, §8.4).

    A live record is a handful of trials with no season, no controls and no chronological replay
    behind it, so there is nothing that could make ``qualified`` true; §8.4 gates it on a
    ``qualified`` SEASON, which a live exhibition is not.
    """
    _commit(store, _trial(), staging_id="s_win", p=0.8)
    store.record_outcome(TRIAL_ID, _settled_outcome_from(_series()))
    record = build_agent_record(PAYER, store)
    assert record.settled == 1 and record.avg_brier is not None
    assert record.qualified is False


def test_an_agent_record_is_recomputable_from_the_stored_artifacts(store_with_mixed: _TamperableStore) -> None:
    """Idempotent AND derived: a second store over the same tree produces an equal record.

    Stronger than calling the function twice, which a cached record would also satisfy. This
    rebuilds the store, so the record is proven to be a function of what is on disk.
    """
    first = build_agent_record(PAYER, store_with_mixed)
    second = build_agent_record(PAYER, ReceiptStore(store_with_mixed.root))
    assert first == second
    assert isinstance(first, AgentRecord)


# ------------------------------------------------------------------------------ outcome persistence


def test_a_recorded_outcome_round_trips_through_the_store(store: _TamperableStore, series_1m: CandleSeries) -> None:
    """ACCEPTANCE for the persistence path: what was recorded is what is read back."""
    settled = _settled_outcome_from(series_1m)
    store.record_outcome(TRIAL_ID, settled)
    assert store.outcome(TRIAL_ID) == settled.outcome


def test_an_absent_outcome_reads_as_None_rather_than_as_a_pending_one(store: _TamperableStore) -> None:
    """DISCRIMINATION: "nothing recorded" and "a pending outcome recorded" are different rows.

    A store that fabricated a pending outcome for an unknown trial would make the two
    indistinguishable, and an agent record could then count trials that do not exist.
    """
    assert store.outcome("trial_nothing_here") is None


def test_recording_the_identical_outcome_twice_is_idempotent(store: _TamperableStore, series_1m: CandleSeries) -> None:
    """A settler that runs twice over the same trial must not be a conflict."""
    settled = _settled_outcome_from(series_1m)
    store.record_outcome(TRIAL_ID, settled)
    store.record_outcome(TRIAL_ID, settled)
    assert store.outcome(TRIAL_ID) == settled.outcome


def test_a_pending_outcome_may_be_SUPERSEDED_by_its_settled_self(
    store: _TamperableStore, series_1m: CandleSeries
) -> None:
    """``pending`` is not terminal: the settler's whole job is to replace it once a candle exists."""
    store.record_outcome(TRIAL_ID, settle_trial(_trial(), _empty_series(), now_ms=T0 + 60_000))
    assert (before := store.outcome(TRIAL_ID)) is not None and before.status == "pending"
    store.record_outcome(TRIAL_ID, _settled_outcome_from(series_1m))
    assert (after := store.outcome(TRIAL_ID)) is not None and after.status == "settled"


@pytest.mark.parametrize("terminal_close", [FUTURE, ENTRY * 3])
def test_a_TERMINAL_outcome_cannot_be_rewritten(store: _TamperableStore, terminal_close: float) -> None:
    """A settled or UNSCORED outcome is history. Rewriting it moves the result under a paid record.

    Both directions are refused: a settled outcome cannot be re-settled at a different price, and
    it cannot be demoted back to pending. The agents who committed against this trial were told
    what happened; changing the answer afterwards is the one thing a benchmark record cannot do.
    """
    store.record_outcome(TRIAL_ID, _settled_outcome_from(_series()))
    with pytest.raises(ValueError, match="already settled"):
        store.record_outcome(TRIAL_ID, _settled_outcome_from(_series(close=terminal_close * 1.5)))
    with pytest.raises(ValueError, match="already settled"):
        store.record_outcome(TRIAL_ID, settle_trial(_trial(), _empty_series(), now_ms=T0 + 60_000))


def test_an_UNSCORED_outcome_is_terminal_too(store: _TamperableStore) -> None:
    """UNSCORED is an ANSWER, not an absence of one, so it is as immutable as a settled outcome.

    A window that reopened would let a late-arriving candle rewrite a trial agents already saw
    reported as unscored — which is exactly the "never interpolate" rule applied to time.
    """
    store.record_outcome(TRIAL_ID, settle_trial(_trial(), _empty_series(), now_ms=T + 60_000 + FETCH_GRACE_MS))
    with pytest.raises(ValueError, match="already settled"):
        store.record_outcome(TRIAL_ID, _settled_outcome_from(_series()))


def test_recording_an_outcome_under_a_DIFFERENT_trial_id_is_refused(store: _TamperableStore) -> None:
    """The key and the payload carry the same fact, so they are never allowed to disagree.

    Filing trial A's outcome under trial B would join every one of B's participants to A's market
    move, and every downstream artifact would look ordinary.
    """
    with pytest.raises(ValueError, match="trial_id"):
        store.record_outcome(OTHER_TRIAL_ID, _settled_outcome_from(_series(), trial_id=TRIAL_ID))


@pytest.mark.parametrize("bad_id", ["", ".", "..", "../escape", "a/b", "a\\b"])
def test_an_outcome_id_that_could_ESCAPE_the_store_names_no_outcome(store: _TamperableStore, bad_id: str) -> None:
    """Trial ids arrive from a URL path segment, so this is where a traversal attempt stops."""
    assert store.outcome(bad_id) is None
    assert store.outcome_payload(bad_id) is None


def test_a_participant_settlement_is_an_immutable_append(
    store: _TamperableStore, settled_outcome: TrialOutcome
) -> None:
    """Keyed by receipt id, written once. A re-record of the SAME settlement is a no-op; a
    different one for the same receipt is refused, because a participant's scored record is the
    artifact they paid for.
    """
    settlement = settle_commit(_finalized(p=0.8), settled_outcome)
    store.record_settlement(settlement)
    store.record_settlement(settlement)
    assert store.settlement(settlement.receipt_id) == settlement
    with pytest.raises(ValueError, match="already recorded"):
        store.record_settlement(settle_commit(_finalized(p=0.3), settled_outcome))


# ------------------------------------------------------------------------------ the eight checks


def test_the_report_carries_the_eight_frozen_checks_in_order(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """The key set and its ORDER are part of the frozen contract H5.1 mirrors."""
    report = verify_receipt(settled_receipt.receipt_id, store)
    assert tuple(report.checks) == VERIFY_COMMIT_CHECKS + VERIFY_OUTCOME_CHECKS
    assert VERIFY_OUTCOME_CHECKS == ("bar_version", "law_version", "evidence_equality", "outcome_source")
    assert _verdict(settled_receipt.receipt_id, store) == _clean()


def test_the_outcome_checks_are_pending_while_the_OUTCOME_ITSELF_is_pending(
    committed_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """A recorded PENDING outcome is not a settlement, so there is still nothing to re-derive.

    ``pending``, never ``fail``: the trial's horizon has not produced a candle yet, and reporting
    ``fail`` would tell a receipt holder their receipt does not verify because the market has not
    moved on.
    """
    store.record_outcome(committed_receipt.trial_id, settle_trial(_trial(), _empty_series(), now_ms=T0 + 60_000))
    assert _verdict(committed_receipt.receipt_id, store) == _unsettled()


def test_the_outcome_checks_are_pending_for_an_UNSCORED_trial_and_the_STATUS_says_why(
    committed_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """UNSCORED reports ``pending`` checks, and the participant STATUS is what distinguishes it.

    The frozen check triple is ``pass|fail|pending`` and cannot express UNSCORED. ``pending`` here
    means "no outcome verdict is available"; it does NOT distinguish "not yet" from "never", and
    that boundary is deliberate rather than overlooked — the distinction is carried by the
    settlement ``status`` served beside the checks, which this test asserts in the same breath so
    the two can never drift apart. ``fail`` was the alternative and is worse: nothing about an
    UNSCORED trial has been shown to be wrong.
    """
    store.record_outcome(
        committed_receipt.trial_id, settle_trial(_trial(), _empty_series(), now_ms=T + 60_000 + FETCH_GRACE_MS)
    )
    assert _verdict(committed_receipt.receipt_id, store) == _unsettled()
    outcome = store.outcome(committed_receipt.trial_id)
    assert outcome is not None
    assert settle_commit(committed_receipt, outcome).status == "UNSCORED"


def test_a_receipt_whose_trial_has_no_outcome_at_all_is_pending(
    committed_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """No outcome row on disk at all — the state every receipt starts in."""
    assert store.outcome_payload(committed_receipt.trial_id) is None
    assert _verdict(committed_receipt.receipt_id, store) == _unsettled()


@pytest.mark.parametrize(
    ("field", "value", "failing"),
    [
        # bar_version: the recorded bar and its width must be a frozen §5.1 pair.
        ("outcome.bar", "5m", "bar_version"),
        ("outcome.bar", 60_000, "bar_version"),
        # law_version: the outcome must have been produced under the law this build implements.
        ("outcome.law_version", "spot_markout_v0", "law_version"),
        ("outcome.law_version", None, "law_version"),
        # evidence_equality: the sealed evidence must re-derive its own hash.
        ("outcome.evidence_hash", "0" * 64, "evidence_equality"),
        ("outcome.trial_id", OTHER_TRIAL_ID, "evidence_equality"),
        # outcome_source: close_ts must re-derive from the recorded candle open plus the bar.
        ("outcome.close_ts_ms", 1, "outcome_source"),
        ("outcome.settlement_ts_open_ms", 1, "outcome_source"),
        ("outcome.observation_lag_ms", 1, "outcome_source"),
    ],
)
def test_each_outcome_check_fails_ONLY_on_its_own_tamper(
    settled_receipt: CommitRecord, store: _TamperableStore, field: str, value: Any, failing: str
) -> None:
    """One tamper, one failing check, and the WHOLE eight-key verdict asserted.

    This is the attribution property H4.2 established for the commit checks, extended to the four
    outcome checks: a verifier that collapsed to a single verdict would report all eight as
    ``fail`` here and discard seven answers it successfully computed. Each row's expectation names
    both what broke and that nothing else did.
    """
    store.tamper(settled_receipt.receipt_id, field=field, value=value)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(**{failing: "fail"})


def test_tampering_the_bar_WIDTH_fails_both_checks_that_read_it(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """``bar_ms`` is an input to TWO checks, and both are expected to notice.

    Stated separately from the one-check table above because it is a genuinely different claim:
    attribution means each check reports what IT found, not that every tamper lands on exactly one
    check. ``bar_ms`` is read by ``bar_version`` (is it the frozen pair?) and by ``outcome_source``
    (does ``close_ts`` re-derive from it?), so a tamper there corrupts two derivations and two
    ``fail``s is the honest report — one would mean a check stopped reading its own input.
    """
    store.tamper(settled_receipt.receipt_id, field="outcome.bar_ms", value=120_000)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(bar_version="fail", outcome_source="fail")


def test_rewriting_the_sealed_EVIDENCE_fails_evidence_equality(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """The evidence, not only its hash: the check re-derives the hash FROM the stored payload.

    A check that compared the stored hash against a stored copy of itself would pass here. This
    rewrites the evidence and leaves the hash alone, which is the direction a forger would take —
    changing what a trial claims to have been opened over while the digest still looks sealed.
    """
    store.tamper(settled_receipt.receipt_id, field="outcome.evidence", value={**_sig().model_dump(), "holders": 1})
    assert _verdict(settled_receipt.receipt_id, store) == _clean(evidence_equality="fail")


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"unexpected_field": "injected"}, "a key the model does not declare"),
        ({"sold_ratio_percent": 42.0}, "a FORBIDDEN post-decision field"),
        ({"holders": "4200"}, "a value that only survives because the model coerces it"),
    ],
)
def test_an_edit_the_HASH_CANNOT_SEE_still_fails_evidence_equality(
    settled_receipt: CommitRecord, store: _TamperableStore, overrides: dict[str, Any], why: str
) -> None:
    """The DISCRIMINATION control for the check's second half. The digest is intact in all three.

    Re-hashing alone cannot see any of these, because the hash is taken over the model's DUMP
    rather than over the stored bytes: pydantic ignores keys it does not declare, so an injected
    field never reaches the digest, and it coerces ``"4200"`` to ``4200``, so a retyped value
    hashes to exactly what the honest one did. Every row here therefore re-hashes to the SEALED
    value and a hash-only check reports ``pass`` on a payload that no longer says what it said.

    What catches them is the round-trip: ``visible_at_decision`` of the rebuilt signal must equal
    the stored payload byte for byte. That comparison is the reason the check is two halves rather
    than one, and this test is what makes the second half load-bearing — without it the mutation
    drill scores a hash-only implementation as a survivor, which is exactly what it did before this
    test existed.
    """
    tampered = {**_sig().model_dump(), **overrides}
    store.tamper(settled_receipt.receipt_id, field="outcome.evidence", value=tampered)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(evidence_equality="fail")
    # ACCEPTANCE control, in the same test: the SEALED hash is still the honest one, so the
    # re-hash half genuinely passes here and the verdict above is the round-trip half's finding.
    row = store.outcome_payload(settled_receipt.trial_id)
    assert row is not None and row["evidence_hash"] == _trial().evidence_hash


def test_repointing_a_RECEIPT_at_another_trial_is_the_MANIFESTS_finding_not_the_outcome_checks(
    committed_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """Where the receipt-to-trial binding actually lives, stated as a BOUNDARY rather than implied.

    Rewriting the receipt's resolved trial id to a trial with its own intact settled outcome makes
    ``manifest`` fail — the resolved id is one of the facts it binds — and leaves all four outcome
    checks passing, because the outcome they examined IS intact and IS the one recorded for the id
    the row now claims.

    That is the honest report and it names a real limit: **at H4.3 nothing on the outcome side
    binds a receipt to its trial, because the finalized receipt row carries no evidence hash of its
    own.** Adding one would mean changing what ``stage`` writes, which is H4.1's sealed shape and
    outside this task. So the binding is ``manifest``'s alone, and this test exists to state that
    rather than to let a future reader infer from ``evidence_equality``'s name that it covers the
    join. What ``evidence_equality`` does bind is the outcome row's OWN consistency — see the
    parametrised table above, whose ``outcome.trial_id`` row is the tamper it can see.
    """
    store.record_outcome(OTHER_TRIAL_ID, _settled_outcome_from(_series(), trial_id=OTHER_TRIAL_ID))
    store.tamper(committed_receipt.receipt_id, field="trial_id", value=OTHER_TRIAL_ID)
    assert _verdict(committed_receipt.receipt_id, store) == _clean(manifest="fail")


@pytest.mark.parametrize("raw", ["not json at all", '["a list is not a row"]', '{"trial_id": '])
def test_a_CORRUPT_outcome_row_fails_the_four_outcome_checks_and_leaves_the_commit_checks_alone(
    settled_receipt: CommitRecord, store: _TamperableStore, raw: str
) -> None:
    """A row that EXISTS and re-derives nothing is a ``fail``, not a ``pending`` and not a 500.

    The same reading ``verify_receipt`` already applies to an unreadable RECEIPT row, applied to
    the outcome: ``pending`` would say "no settlement has been recorded", which is false — one was
    recorded and then destroyed. The four commit checks still report what they found, because the
    receipt itself is intact.
    """
    store.corrupt_outcome(settled_receipt.trial_id, raw=raw)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(**dict.fromkeys(VERIFY_OUTCOME_CHECKS, "fail"))


def test_a_commit_tamper_does_not_disturb_the_outcome_checks(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """Attribution in the other direction: a rewritten probability is not a settlement fault."""
    store.tamper(settled_receipt.receipt_id, field="p_follow_profitable", value=0.99)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(body_hash="fail")


def test_an_UNREADABLE_receipt_row_reports_four_fails_and_four_pendings(
    committed_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """The receipt's own bytes are destroyed, so its trial — and therefore its outcome — is unknown.

    The commit checks fail because every one of their inputs is unreadable. The outcome checks
    report ``pending`` because nothing about the SETTLEMENT was examined at all: the row that
    would name the trial is gone, so claiming its bar or law is wrong would be a finding nothing
    supports. The four ``fail``s beside them are what carry the verdict.
    """
    path = Path(store.root) / _FINALIZED_DIRNAME / f"{committed_receipt.receipt_id}.json"
    path.write_text("{not json", encoding="utf-8")
    assert _verdict(committed_receipt.receipt_id, store) == {
        **dict.fromkeys(VERIFY_COMMIT_CHECKS, "fail"),
        **dict.fromkeys(VERIFY_OUTCOME_CHECKS, "pending"),
    }


# ------------------------------------------------------------------------------ frozen wire models


def test_the_outcome_model_mirrors_the_dataclass_FIELD_FOR_FIELD() -> None:
    """The pydantic mirror and the dataclass cannot drift, and nothing but a test can hold that.

    ``schemas.py`` is imported BY ``live.py``, so the mirror cannot import the dataclass and the
    duplication is structural rather than lazy. H5.1 mirrors this model exactly, so a field added
    on one side and not the other is a frontend contract break that would otherwise surface as a
    missing key in a browser.
    """
    assert tuple(TrialOutcomeModel.model_fields) == tuple(field.name for field in fields(TrialOutcome))
    assert tuple(TrialOutcomeModel.model_fields) == OUTCOME_FIELDS


def test_the_stored_outcome_row_covers_the_outcome_and_its_provenance() -> None:
    """The row's field lists are the coupling between the writer and the verifier.

    ``OUTCOME_FIELDS`` is what a reader rebuilds a :class:`TrialOutcome` from and
    ``OUTCOME_PROVENANCE_FIELDS`` is what the four outcome checks re-derive over. The two must not
    overlap: a field claimed by both would be written twice and could be repaired by whichever
    write happened last.
    """
    assert set(OUTCOME_FIELDS).isdisjoint(OUTCOME_PROVENANCE_FIELDS)
    assert tuple(field.name for field in fields(OutcomeProvenance)) == OUTCOME_PROVENANCE_FIELDS
    # Named literally as well, so a field REMOVED from both the dataclass and the tuple — which the
    # comparison above would still call consistent — has to be a deliberate edit here too.
    assert set(OUTCOME_PROVENANCE_FIELDS) == {
        "bar",
        "bar_ms",
        "t0_ms",
        "horizon_ms",
        "cost_bps",
        "settlement_ts_open_ms",
        "evidence",
        "evidence_hash",
        "law_version",
    }


def test_the_stored_settlement_row_covers_every_participant_field() -> None:
    """Same coupling for the participant row: the tuple IS the dataclass's field list.

    A field added to :class:`ParticipantSettlement` and not to ``SETTLEMENT_FIELDS`` would be
    dropped on write and read back as missing, which surfaces as an unrelated ``KeyError`` far
    from the edit that caused it.
    """
    assert tuple(field.name for field in fields(ParticipantSettlement)) == SETTLEMENT_FIELDS


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (
            TrialResponse,
            ("trial_id", "trial_mode", "t0_ms", "commit_deadline_ms", "evidence", "evidence_hash", "outcome"),
        ),
        (
            CommitReceiptResponse,
            (
                "receipt_id",
                "trial_id",
                "payer",
                "p_follow_profitable",
                "methodology_version",
                "action",
                "status",
                "brier",
                "chosen_markout_bps",
                "committed_at_ms",
                "commit_deadline_ms",
                "trial_mode",
                "body_hash",
                "payment_tx_hash",
            ),
        ),
        (
            AgentRecordResponse,
            ("payer", "commits", "settled", "pending", "unscored", "avg_brier", "capped_avg_markout_bps", "qualified"),
        ),
        (VerifyReceiptResponse, ("receipt_id", "checks", "receipt")),
    ],
)
def test_the_frozen_response_models_carry_exactly_their_frozen_fields(model: Any, expected: tuple[str, ...]) -> None:
    """These four shapes freeze at H4.3 and H5.1 mirrors them EXACTLY.

    Pinned as an exact tuple, order included, because "the frontend mirrors this" is a promise
    about a specific set of keys — an additive field is as much a contract change as a removal
    once another lane has typed against it.
    """
    assert tuple(model.model_fields) == expected


def test_the_agent_record_and_its_wire_model_agree() -> None:
    """The aggregate and the response are the same eight facts, so neither can grow alone."""
    assert tuple(AgentRecord.model_fields) == tuple(AgentRecordResponse.model_fields)


# ------------------------------------------------------------------------------ routes


async def test_the_trial_route_carries_the_settled_outcome(store: _TamperableStore, series_1m: CandleSeries) -> None:
    """``GET /trials/{id}`` serves the evidence, its hash, and the outcome once one exists."""
    trial = _trial()
    store.record_outcome(TRIAL_ID, _settled_outcome_from(series_1m))
    async with _client(_app(store=store, live_trials=_OneTrialRepo(trial))) as client:
        response = await client.get(f"/signal-trials/trials/{TRIAL_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["evidence_hash"] == trial.evidence_hash
    assert body["outcome"]["status"] == "settled"
    assert body["outcome"]["follow_markout_bps"] == FOLLOW_BPS
    assert set(body) == set(TrialResponse.model_fields)


async def test_the_trial_route_reports_a_MISSING_outcome_as_null_not_as_a_pending_one(store: _TamperableStore) -> None:
    """No outcome recorded is ``null``, which is a different claim from a recorded ``pending``.

    A route that synthesized a pending outcome would publish a settlement state nothing computed —
    the exact placeholder H1.2 refused to serve.
    """
    async with _client(_app(store=store, live_trials=_OneTrialRepo(_trial()))) as client:
        response = await client.get(f"/signal-trials/trials/{TRIAL_ID}")
    assert response.status_code == 200 and response.json()["outcome"] is None


@pytest.mark.parametrize(
    ("now_ms", "status"),
    [
        pytest.param(T0 + 60_000, "pending", id="pending"),
        pytest.param(T + 60_000 + FETCH_GRACE_MS, "UNSCORED", id="UNSCORED"),
    ],
)
async def test_the_trial_route_serves_a_RECORDED_non_settled_outcome_with_every_metric_null(
    store: _TamperableStore, now_ms: int, status: str
) -> None:
    """A recorded ``pending`` and a recorded ``UNSCORED`` both render over the wire, metrics null.

    This is the state BETWEEN the two the other route tests cover — a settled outcome, and no
    outcome at all — and it is the common one in production: ``settle_live_trials`` records
    ``pending`` on every trial it visits before one settles, so ``GET /trials/{id}`` on a visited
    but unsettled trial is an ordinary read, not an edge case.

    It is also the only place :class:`TrialOutcomeModel`'s NULLABILITY is exercised. The mirror test
    compares field NAMES, so it is blind to a metric that stopped being ``| None``; nothing else
    serves an outcome carrying ``None`` through the model. A metric narrowed to a non-optional type
    with a default would pass every other test in this file and then answer 500 on this read, and a
    metric narrowed WITHOUT a default would publish a zero where the backend meant "no result" —
    which is the H5.1 contract break the freeze exists to prevent, since a zero markout is a real
    flat outcome and a zero Brier is a perfect score.

    Asserts the STATUS as well as the nulls, per ``PKT-DEC-C26``: both non-settled states carry
    identical ``None`` metrics, so an assertion on the nulls alone cannot tell them apart and would
    pass just as happily against a route that had collapsed the two labels into one.
    """
    trial = _trial()
    store.record_outcome(TRIAL_ID, settle_trial(trial, _empty_series(), now_ms=now_ms))
    async with _client(_app(store=store, live_trials=_OneTrialRepo(trial))) as client:
        response = await client.get(f"/signal-trials/trials/{TRIAL_ID}")
    assert response.status_code == 200
    outcome = response.json()["outcome"]
    # Not ``null``: a RECORDED non-settled outcome is a stronger claim than an absent one, and the
    # test above already covers the absent case — without this, both could be served as ``null``.
    assert outcome is not None
    assert outcome["status"] == status
    # Every key present, so a metric is served as an explicit ``null`` rather than omitted: a
    # consumer reading a missing key cannot tell "no result" from "field I do not know about".
    assert set(outcome) == set(TrialOutcomeModel.model_fields)
    metrics = ("future", "close_ts_ms", "observation_lag_ms", "follow_markout_bps", "fade_markout_bps")
    assert {key: outcome[key] for key in metrics} == dict.fromkeys(metrics)
    assert outcome["follow_profitable"] is None
    # The entry is NOT null in any status — it is sealed at ``t0``, long before the settlement
    # candle. Asserted so the nulls above are a statement about the metrics and not about an
    # outcome object that came back empty.
    assert outcome["entry"] == ENTRY


async def test_the_agent_route_serves_a_record_and_404s_without_one(store: _TamperableStore) -> None:
    """A payer with no FINALIZED commit has no participant record; one with a commit has one."""
    async with _client(_app(store=store)) as client:
        assert (await client.get(f"/signal-trials/agents/{PAYER}")).status_code == 404
        _commit(store, _trial(), staging_id="s_agent", p=0.8)
        response = await client.get(f"/signal-trials/agents/{PAYER}")
    assert response.status_code == 200
    body = response.json()
    assert body["commits"] == 1 and body["qualified"] is False and body["avg_brier"] is None
    assert set(body) == set(AgentRecordResponse.model_fields)


async def test_the_agent_route_never_counts_a_STAGED_row(store: _TamperableStore) -> None:
    """A commitment that was received and not paid for must not appear as a public record."""
    store.stage(staging_id="s_unpaid", trial_id=TRIAL_ID, payer=PAYER, body=_req(0.6), staged_at_ms=T0)
    async with _client(_app(store=store)) as client:
        assert (await client.get(f"/signal-trials/agents/{PAYER}")).status_code == 404


async def test_the_verify_route_carries_eight_checks_and_the_receipt(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """The frozen verify envelope: the id, the eight-key check map, and the participant receipt."""
    async with _client(_app(store=store)) as client:
        response = await client.get(f"/signal-trials/receipts/{settled_receipt.receipt_id}/verify")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == set(VerifyReceiptResponse.model_fields)
    assert body["checks"] == _clean()
    assert body["receipt"]["status"] == "settled" and body["receipt"]["action"] == "FOLLOW"
    assert body["receipt"]["brier"] == pytest.approx((0.8 - 1) ** 2)


async def test_the_verify_route_answers_200_CARRYING_fails_for_a_tampered_outcome(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """A tampered settlement is a finding, not an outage, so the route publishes it as a 200."""
    store.tamper(settled_receipt.receipt_id, field="outcome.close_ts_ms", value=1)
    async with _client(_app(store=store), raise_app_exceptions=False) as client:
        response = await client.get(f"/signal-trials/receipts/{settled_receipt.receipt_id}/verify")
    assert response.status_code == 200
    assert response.json()["checks"] == _clean(outcome_source="fail")


async def test_the_verify_route_serves_a_NULL_receipt_when_the_row_cannot_be_read(
    committed_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """An unreadable row still verifies — as fails — and carries no receipt rather than a guessed one.

    Rendering a partial receipt out of a destroyed row would publish fields nobody can re-derive,
    and answering 500 would report a destroyed receipt as a broken service.
    """
    (Path(store.root) / _FINALIZED_DIRNAME / f"{committed_receipt.receipt_id}.json").write_text(
        "{not json", encoding="utf-8"
    )
    async with _client(_app(store=store), raise_app_exceptions=False) as client:
        response = await client.get(f"/signal-trials/receipts/{committed_receipt.receipt_id}/verify")
    assert response.status_code == 200
    body = response.json()
    assert body["receipt"] is None
    assert body["checks"] == {
        **dict.fromkeys(VERIFY_COMMIT_CHECKS, "fail"),
        **dict.fromkeys(VERIFY_OUTCOME_CHECKS, "pending"),
    }


async def test_the_verify_route_still_404s_for_a_pending_staging_id(store: _TamperableStore) -> None:
    """H4.2's rule survives H4.3: a staged row is not a receipt, whatever the settlement state."""
    store.stage(staging_id="sX", trial_id=TRIAL_ID, payer=PAYER, body=_req(0.6), staged_at_ms=T0)
    async with _client(_app(store=store)) as client:
        response = await client.get("/signal-trials/receipts/sX/verify")
    assert response.status_code == 404 and response.json() == {"error": "receipt_not_found"}


# ------------------------------------------------------------------------------ law provenance


def test_the_recorded_law_version_is_the_one_this_build_implements(
    store: _TamperableStore, series_1m: CandleSeries
) -> None:
    """§7 requires the law version to be persisted with every settlement.

    Asserted against the constant rather than a literal, so bumping the law in one place turns
    this green again ONLY once every recorded outcome is re-derived under it — which is the point
    of versioning it at all.
    """
    settled = _settled_outcome_from(series_1m)
    assert settled.provenance.law_version == SETTLEMENT_LAW_VERSION
    store.record_outcome(TRIAL_ID, settled)
    row = store.outcome_payload(TRIAL_ID)
    assert row is not None and row["law_version"] == SETTLEMENT_LAW_VERSION


def test_the_recorded_provenance_names_the_bar_the_series_carried(series_1m: CandleSeries) -> None:
    """The bar is taken from the SERIES (request provenance, §7), never inferred from the candles."""
    settled = _settled_outcome_from(series_1m)
    assert settled.provenance.bar == series_1m.bar and settled.provenance.bar_ms == series_1m.bar_ms
    assert settled.provenance.settlement_ts_open_ms == T - series_1m.bar_ms
    assert settled.provenance.horizon_ms == FROZEN_HORIZON_MS and settled.provenance.t0_ms == T0


def test_an_unsettled_provenance_records_no_settlement_candle() -> None:
    """There was no candle, so ``settlement_ts_open_ms`` is ``None`` — never a placeholder zero.

    A zero here would be re-derived by ``outcome_source`` as a close at ``bar_ms``, i.e. 1970, and
    the check would report a tamper on an honestly unsettled trial.
    """
    settled = settle_trial(_trial(), _empty_series(), now_ms=T0 + 60_000)
    assert settled.outcome.status == "pending"
    assert settled.provenance.settlement_ts_open_ms is None
    assert settled.provenance.bar_ms == 60_000


def test_the_declared_cost_is_the_frozen_25_bps_and_is_recorded(series_1m: CandleSeries) -> None:
    """§8.2: the official rank uses ``declared_cost_bps = 25``. The sweep is a display, never this."""
    settled = _settled_outcome_from(series_1m)
    assert settled.provenance.cost_bps == COST_BPS == 25
    assert settled.outcome.follow_markout_bps == GROSS_BPS - COST_BPS


def test_a_diagnostic_cost_sweep_does_not_change_the_recorded_official_outcome(live_trial: _BarredTrial) -> None:
    """The sweep is expressible and is NOT the default: ``cost_bps`` is an argument, 25 is the law."""
    swept = settle_trial_outcome(live_trial, _series(), now_ms=T + 1, cost_bps=50)
    official = settle_trial_outcome(live_trial, _series(), now_ms=T + 1)
    assert swept.follow_markout_bps == GROSS_BPS - 50
    assert official.follow_markout_bps == GROSS_BPS - COST_BPS
    assert swept.follow_markout_bps != official.follow_markout_bps


def test_all_trials_returns_every_published_trial_CHRONOLOGICALLY(tmp_path: Path) -> None:
    """The settler reads every trial, not the one at the open pointer.

    By the time a trial is settleable a newer one has usually replaced it at the pointer, so a
    settler driven by ``current()`` would leave every superseded trial permanently unsettled. The
    ordering is asserted because two runs over the same directory must do the same work in the same
    order, and the filesystem orders by the id digest rather than by time.
    """
    repository = LiveTrialRepository(tmp_path / "live")
    later = open_live_trial(_sig(t0_ms=T0 + 5_000), now_ms=T0 + 5_000, trial_id="trial_late")
    earlier = open_live_trial(_sig(), now_ms=T0, trial_id="trial_early")
    repository.publish(later)
    repository.publish(earlier)
    assert [trial.trial_id for trial in repository.all_trials()] == ["trial_early", "trial_late"]
    # DISCRIMINATION: the pointer names the LAST publish, so a settler reading it would see one
    # trial and miss the other entirely.
    current = repository.current()
    assert current is not None and current.trial_id == "trial_early"


@pytest.mark.parametrize(
    ("lag_offsets", "covers"),
    [
        # A candle at T-bar and one at T+bar: the fetch straddles the window.
        ((-60_000, 60_000), True),
        # Exactly the two boundary candles the predicate requires, and nothing more.
        ((0, 60_000), True),
        # The whole fetch sits BEFORE the window — the settler ran against a stale page.
        ((-180_000, -120_000), False),
        # The whole fetch sits AFTER it — the window fell off the far end of the page.
        ((120_000, 180_000), False),
        # Reached back far enough but not forward: nothing proves the window is in the past.
        ((-120_000, 0), False),
    ],
)
def test_the_coverage_gate_requires_the_fetch_to_STRADDLE_the_settlement_window(
    lag_offsets: tuple[int, int], covers: bool
) -> None:
    """ACCEPTANCE and DISCRIMINATION for the predicate that guards every UNSCORED this script writes.

    The window is ``[T, T + bar)``. Coverage means one confirmed close at or before ``T`` and one at
    or after ``T + bar``. The three ``False`` rows are the operational errors that would otherwise
    produce a false UNSCORED, and each is a known-present negative: the series is non-empty and
    perfectly well-formed in every one, so a predicate that merely checked "did we get candles"
    passes all three.
    """
    settle_script = _settle_script()
    candles = tuple(_candle(ts_open_ms=T + offset - 60_000) for offset in lag_offsets)
    series = CandleSeries(bar="1m", bar_ms=60_000, candles=candles)
    assert settle_script.series_covers_settlement(series, t0_ms=T0, horizon_ms=FROZEN_HORIZON_MS) is covers


def test_an_UNCONFIRMED_candle_does_not_prove_coverage() -> None:
    """An in-progress bar is not evidence that the window is in the past.

    DISCRIMINATION against the same series with the flag set: coverage flips to ``True``, so this
    asserts the confirm flag is read rather than that these timestamps are unusable.
    """
    settle_script = _settle_script()
    offsets = (-60_000, 60_000)
    unconfirmed = CandleSeries(
        bar="1m", bar_ms=60_000, candles=tuple(_candle(ts_open_ms=T + o - 60_000, confirmed=False) for o in offsets)
    )
    confirmed = CandleSeries(
        bar="1m", bar_ms=60_000, candles=tuple(_candle(ts_open_ms=T + o - 60_000) for o in offsets)
    )
    assert settle_script.series_covers_settlement(unconfirmed, t0_ms=T0, horizon_ms=FROZEN_HORIZON_MS) is False
    assert settle_script.series_covers_settlement(confirmed, t0_ms=T0, horizon_ms=FROZEN_HORIZON_MS) is True


def test_an_EMPTY_fetch_never_proves_coverage() -> None:
    """The commonest bad fetch. An empty series and a genuine gap are the same bytes downstream,
    and only this predicate separates them: an empty page proves nothing about the window, so it
    can never authorize the terminal UNSCORED that a genuine gap earns."""
    settle_script = _settle_script()
    assert settle_script.series_covers_settlement(_empty_series(), t0_ms=T0, horizon_ms=FROZEN_HORIZON_MS) is False
    # And the law over that same empty series DOES reach UNSCORED once the boundary passes — which
    # is exactly the outcome the gate is there to withhold from an unproven fetch.
    assert settle_script.series_covers_settlement(_empty_series(), t0_ms=T0, horizon_ms=FROZEN_HORIZON_MS) is False
    assert settle_trial_outcome(_trial(), _empty_series(), now_ms=T + 60_000 + FETCH_GRACE_MS).status == "UNSCORED"


def test_a_settlement_is_recomputable_from_the_finalized_commit_and_the_recorded_outcome(
    store: _TamperableStore, series_1m: CandleSeries
) -> None:
    """The persisted participant row is a CACHE of a recomputable join, not a second source of truth.

    An agent record built from the primary artifacts must equal the settlement that was recorded
    beside them; if it did not, a rewritten settlement row could inflate a record while every
    primary artifact still verified.
    """
    record = _commit(store, _trial(), staging_id="s_recompute", p=0.8)
    store.record_outcome(TRIAL_ID, _settled_outcome_from(series_1m))
    outcome = store.outcome(TRIAL_ID)
    assert outcome is not None
    recomputed = settle_commit(record, outcome)
    store.record_settlement(recomputed)
    assert store.settlement(record.receipt_id) == recomputed
    assert isinstance(recomputed, ParticipantSettlement)
