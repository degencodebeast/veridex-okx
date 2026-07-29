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
import re
import socket
from dataclasses import dataclass, fields, replace
from importlib import util as importlib_util
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from veridex.api import signal_trials_router
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
    DECLARED_COST_BPS,
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
from veridex.signal_trials.okx_client import BAR_MS, HISTORICAL_CANDLES_PATH, Candle, CandleSeries
from veridex.signal_trials.preflight import FROZEN_HORIZON_MS
from veridex.signal_trials.published import write_season, write_state
from veridex.signal_trials.receipts import (
    _FINALIZED_DIRNAME,
    _OUTCOMES_DIRNAME,
    OUTCOME_FIELDS,
    OUTCOME_PROVENANCE_FIELDS,
    SETTLED_ONLY_FIELDS,
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
from veridex.signal_trials.receipts import (
    DECLARED_COST_BPS as RECEIPTS_DECLARED_COST_BPS,
)
from veridex.signal_trials.spot_markout import SpotMarkoutError, spot_markout

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

#: The chain the fixture signal is SEALED on, and the endpoint a settlement is recorded as fetched
#: from. Named constants rather than inline literals because ``outcome_source`` compares the
#: recorded chain against the evidence's: a test that spelled one of them twice could agree with a
#: broken derivation by construction, and the cross-chain tests below need the mismatch to be the
#: one thing that differs.
CHAIN_INDEX = "196"
OTHER_CHAIN_INDEX = "501"
SOURCE_ENDPOINT = f"https://web3.okx.com{HISTORICAL_CANDLES_PATH}"

_ABSENT = object()


# ------------------------------------------------------------------------------ builders


def _sig(**overrides: Any) -> CanonicalSignal:
    """A canonical signal observed exactly at :data:`T0`.

    Every field is a synthetic constant; ``trigger_wallet_address`` is a repeated-nibble address
    that no chain can hold, so nothing here is or resembles a real credential.
    """
    fields_: dict[str, Any] = {
        "t0_ms": T0,
        "chain_index": CHAIN_INDEX,
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


def _settle(trial: LiveTrial, series: CandleSeries, **kwargs: Any) -> SettledTrial:
    """:func:`settle_trial` with this fixture's SEALED chain and a well-formed source endpoint.

    A helper rather than eleven repetitions of the same two keywords, and it is safe to hide them
    HERE because nothing in this file's coverage of them depends on the default: the settlement
    source is exercised by tests that call :func:`settle_trial` directly with explicit values, and
    by the operator-script tests that drive the real caller. What this helper must never become is
    the only place those keywords appear.
    """
    kwargs.setdefault("chain_index", CHAIN_INDEX)
    kwargs.setdefault("source_endpoint", SOURCE_ENDPOINT)
    return settle_trial(trial, series, **kwargs)


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
    settled = _settle(_trial(trial_id=trial_id), series, now_ms=T + FROZEN_HORIZON_MS)
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
        _settle(unscored_trial, _empty_series(), now_ms=T + 60_000 + FETCH_GRACE_MS),
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
    store.record_outcome(TRIAL_ID, _settle(_trial(), _empty_series(), now_ms=T0 + 60_000))
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
        store.record_outcome(TRIAL_ID, _settle(_trial(), _empty_series(), now_ms=T0 + 60_000))


def test_an_UNSCORED_outcome_is_terminal_too(store: _TamperableStore) -> None:
    """UNSCORED is an ANSWER, not an absence of one, so it is as immutable as a settled outcome.

    A window that reopened would let a late-arriving candle rewrite a trial agents already saw
    reported as unscored — which is exactly the "never interpolate" rule applied to time.
    """
    store.record_outcome(TRIAL_ID, _settle(_trial(), _empty_series(), now_ms=T + 60_000 + FETCH_GRACE_MS))
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
    store.record_outcome(committed_receipt.trial_id, _settle(_trial(), _empty_series(), now_ms=T0 + 60_000))
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
        committed_receipt.trial_id, _settle(_trial(), _empty_series(), now_ms=T + 60_000 + FETCH_GRACE_MS)
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


# ==================================================================================================
# CODEX CRITICAL — a SCORE-CHANGING tamper must not verify.
#
# Before this block, the four outcome checks verified the bar label and width, the law-version
# string, the evidence payload and hash, and the close-time arithmetic — and NEVER re-derived the
# outcome that law governs. `entry`, `follow_markout_bps`, `fade_markout_bps` and
# `follow_profitable` occurred ZERO times in the check construction. Flipping `follow_profitable`
# alone moved a published `avg_brier` from 0.03999999999999998 to 0.6400000000000001 with ALL EIGHT
# CHECKS PASSING: a public receipt saying every Fair-Play check passed over a materially rewritten
# score. That is the product's central claim being false, so these tests assert BOTH halves — the
# check result, and that the forged score cannot remain verified.
# ==================================================================================================


#: The score-bearing fields, each with a forged value and the published number it moves. Every one
#: is a primary input to ``settle_commit``/``build_agent_record``, so none of these is a cosmetic
#: rewrite of a displayed field: each changes what an agent's record asserts.
_SCORE_BEARING_TAMPERS: list[tuple[str, Any]] = [
    # The verdict itself. A FOLLOW committer at p=0.8 goes from a near-perfect Brier to a terrible
    # one, and the FADE committer at p=0.3 goes the other way, off one boolean.
    ("follow_profitable", False),
    # The sealed entry price. Re-derives against the evidence's ``trigger_price``, so a forged entry
    # is caught even though it moves no average on its own — it is the basis every markout is from.
    ("entry", 0.0100),
    # The two legs. Each is the chosen markout for whichever stance took it, and the capped average
    # is computed from exactly these.
    ("follow_markout_bps", FOLLOW_BPS + 4_000),
    ("fade_markout_bps", FADE_BPS + 4_800),
]


@pytest.mark.parametrize(
    ("field", "forged"), _SCORE_BEARING_TAMPERS, ids=[field for field, _ in _SCORE_BEARING_TAMPERS]
)
def test_a_forged_SCORE_BEARING_field_fails_outcome_source(
    settled_receipt: CommitRecord, store: _TamperableStore, field: str, forged: Any
) -> None:
    """Each score-bearing field, forged on its own, is CAUGHT — and the other seven checks still answer.

    One mutant per field rather than one compound tamper, because a check that re-derived only the
    verdict would pass three of these and a check that re-derived only the legs would pass two. The
    whole eight-key verdict is asserted so the row states both what broke and that nothing else did:
    a fix that collapsed the report to eight ``fail``s would satisfy "the tamper was caught" while
    destroying the attribution H4.2 spent four rounds establishing.
    """
    store.tamper(settled_receipt.receipt_id, field=f"outcome.{field}", value=forged)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(outcome_source="fail")


def test_a_forged_verdict_CANNOT_remain_verified_while_the_published_score_moves(
    store: _TamperableStore, series_1m: CandleSeries
) -> None:
    """The CRITICAL finding end to end: the score moves, and the checks say so.

    Two payers take OPPOSITE legs on one trial, so flipping ``follow_profitable`` moves BOTH agent
    records — in opposite directions — and the assertion is not about one payer's arithmetic. The
    forged-score half is asserted first and independently: without it, a test could pass because the
    tamper changed nothing, and "the checks caught a tamper that had no effect" is not the property
    this benchmark rests on.
    """
    follow = _commit(store, _trial(), staging_id="s_follow", p=0.8, payer=PAYER)
    _commit(store, _trial(), staging_id="s_fade", p=0.3, payer=OTHER_PAYER)
    store.record_outcome(TRIAL_ID, _settled_outcome_from(series_1m))

    intact = (build_agent_record(PAYER, store).avg_brier, build_agent_record(OTHER_PAYER, store).avg_brier)
    assert _verdict(follow.receipt_id, store) == _clean(), "the intact receipt must verify before anything is forged"

    store.tamper(follow.receipt_id, field="outcome.follow_profitable", value=False)
    forged = (build_agent_record(PAYER, store).avg_brier, build_agent_record(OTHER_PAYER, store).avg_brier)

    # THE FORGERY IS REAL: both published records moved, and in opposite directions.
    assert forged[0] == pytest.approx((0.8 - 0) ** 2) and intact[0] == pytest.approx((0.8 - 1) ** 2)
    assert forged[1] == pytest.approx((0.3 - 0) ** 2) and intact[1] == pytest.approx((0.3 - 1) ** 2)
    # AND IT IS CAUGHT. This is the assertion the product's central claim is.
    assert _verdict(follow.receipt_id, store) == _clean(outcome_source="fail")


def test_the_markout_legs_are_RECOMPUTED_rather_than_compared_to_each_other(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """DISCRIMINATION against a check that only asserted the law's internal symmetry.

    ``follow + fade == -2 * cost_bps`` holds for the honest row and is tempting to check, but it is
    satisfied by INFINITELY many forged pairs — here both legs are shifted by the same amount in
    opposite directions, so the symmetry is preserved exactly while both published markouts are
    wrong. Only recomputing from ``(entry, future, cost_bps)`` rejects it.
    """
    store.tamper(settled_receipt.receipt_id, field="outcome.follow_markout_bps", value=FOLLOW_BPS + 1_000)
    store.tamper(settled_receipt.receipt_id, field="outcome.fade_markout_bps", value=FADE_BPS - 1_000)
    row = store.outcome_payload(TRIAL_ID)
    assert row is not None
    assert row["follow_markout_bps"] + row["fade_markout_bps"] == -2 * COST_BPS, "the symmetry must still hold"
    assert _verdict(settled_receipt.receipt_id, store) == _clean(outcome_source="fail")


def test_the_entry_is_bound_to_the_SEALED_EVIDENCE_and_not_to_itself(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """Moving the evidence's ``trigger_price`` to MATCH a forged entry does not rescue the row.

    The direction that matters. A forger who noticed the entry was checked would try to move the
    other side of the comparison — and the evidence is hash-sealed, so ``evidence_equality``
    reports that, while ``outcome_source`` reports that the markouts no longer re-derive from the
    new price. Two findings, which is the honest report: two artifacts were altered.
    """
    store.tamper(settled_receipt.receipt_id, field="outcome.entry", value=0.0100)
    store.tamper(
        settled_receipt.receipt_id, field="outcome.evidence", value={**_sig().model_dump(), "trigger_price": 0.0100}
    )
    assert _verdict(settled_receipt.receipt_id, store) == _clean(evidence_equality="fail", outcome_source="fail")


@pytest.mark.parametrize("forged", [1, "true", None], ids=["int-one", "string", "null"])
def test_the_verdict_must_be_a_BOOLEAN_and_not_merely_truthy(
    settled_receipt: CommitRecord, store: _TamperableStore, forged: Any
) -> None:
    """``follow_profitable`` is compared with ``is``, so a truthy stand-in is a ``fail``.

    ``1 == True`` in Python, so an ``==`` comparison would wave the first row through — and the
    integer ``1`` is a row the writer could never have produced. The stored artifact and the value
    the law returns have to be the same thing, not merely equal.
    """
    store.tamper(settled_receipt.receipt_id, field="outcome.follow_profitable", value=forged)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(outcome_source="fail")


def test_a_price_stored_as_a_STRING_does_not_re_derive(settled_receipt: CommitRecord, store: _TamperableStore) -> None:
    """The coercion boundary. ``float("0.0125")`` succeeds; the check must still refuse it.

    A verifier that coerced could not tell a price stored as a number from one stored as text, and
    the two are different artifacts. Same reasoning ``_exact_int`` already applies to the stamps.
    """
    store.tamper(settled_receipt.receipt_id, field="outcome.entry", value=str(ENTRY))
    assert _verdict(settled_receipt.receipt_id, store) == _clean(outcome_source="fail")


def test_a_SELF_CONSISTENT_forgery_off_a_different_entry_is_still_caught(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """The tamper that ONLY the evidence binding can catch, and the reason that binding exists.

    Every other entry tamper in this file is caught by the leg recomputation as a side effect —
    change ``entry`` alone and the stored legs stop matching. So a check that recomputed the legs
    and never compared ``entry`` to the evidence would pass all of them, which a mutation run
    proved: deleting the binding left the suite green.

    This is the row that discriminates. The entry is rewritten AND both legs and the verdict are
    recomputed from the new entry, so the row is internally perfect and every relation among its
    own fields holds. It fails only because ``entry`` is anchored to the hash-sealed
    ``trigger_price``, which the forger did not touch.

    It is also exactly the forgery the ``future`` limit above CANNOT catch, and the contrast is the
    point: ``entry`` has an independent record and ``future`` does not, so one is closed and the
    other is documented.
    """
    forged_entry = 0.0100
    markout = spot_markout(forged_entry, FUTURE, COST_BPS)
    for field, value in (
        ("entry", forged_entry),
        ("follow_markout_bps", markout.follow_markout_bps),
        ("fade_markout_bps", markout.fade_markout_bps),
        ("follow_profitable", markout.follow_profitable),
    ):
        store.tamper(settled_receipt.receipt_id, field=f"outcome.{field}", value=value)

    row = store.outcome_payload(TRIAL_ID)
    assert row is not None
    # The forgery is INTERNALLY CONSISTENT: recomputing from its own entry reproduces its own legs.
    recomputed = spot_markout(row["entry"], row["future"], row["cost_bps"])
    assert (recomputed.follow_markout_bps, recomputed.fade_markout_bps) == (
        row["follow_markout_bps"],
        row["fade_markout_bps"],
    )
    # And it is still caught, because the entry is not its own authority.
    assert row["entry"] != row["evidence"]["trigger_price"]
    assert _verdict(settled_receipt.receipt_id, store) == _clean(outcome_source="fail")


def test_the_LIMIT_of_the_law_re_derivation_is_pinned_rather_than_implied(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """``future`` has no independent record, so a SELF-CONSISTENT rewrite still re-derives. Stated.

    This test asserts a GAP, deliberately, because an unstated gap is the thing that gets
    over-claimed. The settlement candle's close price is not attested by any second artifact on
    disk, so an adversary who rewrites ``future`` AND recomputes both legs and the verdict from it
    produces a row that re-derives perfectly — and ``outcome_source`` passes.

    What IS closed is the cheap forgery: changing a result without changing its inputs, which is
    every row in the table above. Closing this one needs a signed candle from the venue, which §7
    does not provide. If a later change makes this row fail, the gap has been closed and this test
    should be REPLACED by one asserting that — not deleted quietly.
    """
    forged_future = 0.0140
    markout = spot_markout(ENTRY, forged_future, COST_BPS)
    for field, value in (
        ("future", forged_future),
        ("follow_markout_bps", markout.follow_markout_bps),
        ("fade_markout_bps", markout.fade_markout_bps),
        ("follow_profitable", markout.follow_profitable),
    ):
        store.tamper(settled_receipt.receipt_id, field=f"outcome.{field}", value=value)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(), (
        "a self-consistent rewrite of the unattested close price is the DOCUMENTED limit of this check"
    )


# ==================================================================================================
# SPEC N1 — cost_bps was a law INPUT with an available referent and no predicate binding it.
#
# `_law_outputs_reproduce` recomputes both markout legs using cost_bps READ FROM THE ROW IT IS
# VERIFYING. A re-derivation whose inputs all come from the artifact under test proves only that
# the artifact agrees with itself. Measured at b92ac7a: drop cost_bps to 0, restate the legs, and
# all eight checks pass while a published capped_avg_markout_bps moves 375 -> 400 — the entire
# declared cost removed from an agent's record.
#
# This is NOT the declared `future` boundary, and the distinction is the finding. `future` has no
# independent record anywhere, which is what makes declaring it legitimate. cost_bps HAS one in
# this build: DECLARED_COST_BPS = 25, section 8.2. A field with an available referent is bindable
# and therefore must be bound.
# ==================================================================================================


def test_the_declared_cost_constant_matches_the_trial_module() -> None:
    """``receipts`` cannot import ``live`` — ``live`` imports ``receipts`` — so 25 is spelled twice.

    Same coupling, and same risk, as the ``LIVE_TRIAL_MODE`` pin in ``test_receipts.py``. If §8.2's
    cost is ever revised on one side only, every honest recorded outcome starts reporting
    ``outcome_source: fail`` — fail-closed, but wrong, and this is what makes that a caught edit
    rather than a mystery.
    """
    assert RECEIPTS_DECLARED_COST_BPS == DECLARED_COST_BPS == 25


def test_a_SELF_CONSISTENT_forgery_off_a_free_cost_is_caught(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """N1 exactly: the row is internally perfect and inflates a published score by the whole cost.

    Both legs are restated from the forged cost, so every relation among the row's own fields
    holds — recomputing from its own ``cost_bps`` reproduces its own legs. It fails only because
    ``cost_bps`` is anchored to §8.2's declared value rather than to itself.

    The published consequence is asserted, not assumed: the FOLLOW leg an agent's
    ``capped_avg_markout_bps`` is computed from moves by exactly the declared cost.
    """
    free = spot_markout(ENTRY, FUTURE, 0)
    for field, value in (
        ("cost_bps", 0),
        ("follow_markout_bps", free.follow_markout_bps),
        ("fade_markout_bps", free.fade_markout_bps),
        ("follow_profitable", free.follow_profitable),
    ):
        store.tamper(settled_receipt.receipt_id, field=f"outcome.{field}", value=value)

    row = store.outcome_payload(TRIAL_ID)
    assert row is not None
    # INTERNALLY CONSISTENT: the forged row re-derives its own legs from its own cost.
    recomputed = spot_markout(row["entry"], row["future"], row["cost_bps"])
    assert (recomputed.follow_markout_bps, recomputed.fade_markout_bps) == (
        row["follow_markout_bps"],
        row["fade_markout_bps"],
    )
    # AND THE FORGERY IS REAL: the follow leg is inflated by the entire declared cost.
    assert row["follow_markout_bps"] == FOLLOW_BPS + COST_BPS == 400
    assert build_agent_record(PAYER, store).capped_avg_markout_bps == 400
    # AND IT IS CAUGHT.
    assert _verdict(settled_receipt.receipt_id, store) == _clean(outcome_source="fail")


@pytest.mark.parametrize(
    "forged", [0, 10, 50, -25, "25", None], ids=["zero", "ten", "fifty", "negative", "string", "null"]
)
def test_a_recorded_outcome_may_carry_NO_COST_but_the_declared_one(
    settled_receipt: CommitRecord, store: _TamperableStore, forged: Any
) -> None:
    """Every non-declared cost is refused, including the other three sweep values.

    ``[0, 10, 25, 50]`` is a diagnostic DISPLAY and never a recorded outcome, which is what makes
    the binding to a single value correct rather than over-strict. The negative row would also be
    refused by the law's own guard; it is here so the check does not depend on that happening.
    """
    store.tamper(settled_receipt.receipt_id, field="outcome.cost_bps", value=forged)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(outcome_source="fail")


# ==================================================================================================
# SPEC N2 — a status forgery ERASES a score and reports the benign `pending`.
#
# `_outcome_checks` gates on status BEFORE ANY PREDICATE RUNS, so status is the one field that
# switches the checks off. A single-field settled -> UNSCORED edit moved settled 1 -> 0 and
# avg_brier 0.04 -> None with four `pending` and no fail. Erasure rather than fabrication, and the
# cheapest forgery on this surface.
#
# The fix is the asymmetry the forged row leaves behind: an honest non-settled row is all-None on
# every settled-only field, so a row claiming UNSCORED while still carrying future=0.013,
# follow_markout_bps=375 and follow_profitable=True is not merely unverified — it is INCONSISTENT.
# ==================================================================================================


@pytest.mark.parametrize(
    "forged_status",
    ["UNSCORED", "pending", "not_a_status", "", None, 1],
    ids=["unscored", "pending", "unknown-string", "empty", "null", "int"],
)
def test_a_status_forgery_that_leaves_settled_VALUES_behind_is_caught(
    settled_receipt: CommitRecord, store: _TamperableStore, forged_status: Any
) -> None:
    """One field, and the score is gone. The row's own metrics are what report it.

    The first two rows are the real forgery — both are legal statuses, so nothing downstream
    objects — and the rest are values outside the frozen triple, which are not states this law
    produces and are refused for that reason alone.

    ``outcome_source`` fails ALONE and the other three stay ``pending``. That is attribution, not
    an omission: ``bar_version``, ``law_version`` and ``evidence_equality`` are gated on a settled
    outcome and computed nothing here, so failing them would assert the bar and the law were wrong
    on the evidence of a field neither of them reads.
    """
    store.tamper(settled_receipt.receipt_id, field="outcome.status", value=forged_status)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(
        bar_version="pending", law_version="pending", evidence_equality="pending", outcome_source="fail"
    )


def test_the_status_forgery_ERASES_a_published_score_and_the_checks_say_so(
    store: _TamperableStore, series_1m: CandleSeries
) -> None:
    """N2 end to end: the erasure is real, and it is reported.

    Asserted in both halves for the same reason the CRITICAL test is: a check that caught a tamper
    which changed nothing would not be worth having. The record loses its settled row and its
    Brier entirely — this forgery does not move a score, it deletes one — and an agent whose bad
    prediction simply vanishes from their record is the cheapest possible way to look calibrated.
    """
    receipt = _commit(store, _trial(), staging_id="s_erase", p=0.8)
    store.record_outcome(TRIAL_ID, _settled_outcome_from(series_1m))

    intact = build_agent_record(PAYER, store)
    assert (intact.settled, intact.pending) == (1, 0)
    assert intact.avg_brier == pytest.approx((0.8 - 1) ** 2)
    assert _verdict(receipt.receipt_id, store) == _clean()

    store.tamper(receipt.receipt_id, field="outcome.status", value="UNSCORED")

    erased = build_agent_record(PAYER, store)
    assert (erased.settled, erased.unscored) == (0, 1), "the score was not actually erased"
    assert erased.avg_brier is None and erased.capped_avg_markout_bps is None
    assert _verdict(receipt.receipt_id, store) == _clean(
        bar_version="pending", law_version="pending", evidence_equality="pending", outcome_source="fail"
    )


@pytest.mark.parametrize("field", SETTLED_ONLY_FIELDS)
def test_EACH_settled_only_field_left_behind_is_enough_to_report_the_forgery(
    settled_receipt: CommitRecord, store: _TamperableStore, field: str
) -> None:
    """A forger who blanks all but ONE of the settled-only fields is still caught by that one.

    The row is flipped to UNSCORED and every settled-only field is nulled EXCEPT ``field``, so each
    parametrization asserts that this field alone carries the signature. Without this, a shape
    check that only read ``future`` would pass the whole table above.
    """
    store.tamper(settled_receipt.receipt_id, field="outcome.status", value="UNSCORED")
    for other in SETTLED_ONLY_FIELDS:
        if other != field:
            store.tamper(settled_receipt.receipt_id, field=f"outcome.{other}", value=None)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(
        bar_version="pending", law_version="pending", evidence_equality="pending", outcome_source="fail"
    )


@pytest.mark.parametrize(
    "forged_status", ["not_a_status", "", None, 1, "SETTLED"], ids=["unknown", "empty", "null", "int", "wrong-case"]
)
def test_a_status_OUTSIDE_THE_FROZEN_TRIPLE_is_refused_even_when_the_shape_is_clean(
    settled_receipt: CommitRecord, store: _TamperableStore, forged_status: Any
) -> None:
    """The frozen-triple check, bound independently of the shape check.

    Every row in the table above leaves settled VALUES behind, so the shape check alone catches all
    of them and the membership test is never the thing being exercised — deleting it left the suite
    green, which a mutation run found. Here the forger blanks every settled-only field first, so the
    shape is impeccable and the STATUS is the only thing wrong with the row.

    ``"SETTLED"`` is the row that matters most: it is one keystroke from a legal value, reads as
    correct to a human, and is not a state this law produces.
    """
    for field in SETTLED_ONLY_FIELDS:
        store.tamper(settled_receipt.receipt_id, field=f"outcome.{field}", value=None)
    store.tamper(settled_receipt.receipt_id, field="outcome.status", value=forged_status)

    row = store.outcome_payload(TRIAL_ID)
    assert row is not None
    assert all(row.get(field) is None for field in SETTLED_ONLY_FIELDS), "the shape must be clean"
    assert _verdict(settled_receipt.receipt_id, store) == _clean(
        bar_version="pending", law_version="pending", evidence_equality="pending", outcome_source="fail"
    )


@pytest.mark.parametrize("status", ["pending", "UNSCORED"], ids=["pending", "unscored"])
def test_an_HONEST_non_settled_outcome_still_reports_four_pending(
    committed_receipt: CommitRecord, store: _TamperableStore, status: str
) -> None:
    """ACCEPTANCE CONTROL for the whole N2 block, and the row that stops it over-reaching.

    Every test above asserts a ``fail`` on a non-settled row; a shape check that failed EVERY
    non-settled row would satisfy all of them while telling every honest agent whose trial has not
    settled yet that their receipt does not verify. These outcomes come from the production
    settlement path, so they carry whatever shape the law actually produces rather than one this
    test asserted into existence.
    """
    trial = _trial()
    now_ms = T0 + 60_000 if status == "pending" else T + 60_000 + FETCH_GRACE_MS
    settled = _settle(trial, _empty_series(), now_ms=now_ms)
    assert settled.outcome.status == status
    store.record_outcome(committed_receipt.trial_id, settled)
    assert _verdict(committed_receipt.receipt_id, store) == _unsettled()


def test_the_LIMIT_of_the_status_check_is_pinned_rather_than_implied(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """A FULLY consistent erasure is indistinguishable from an honest UNSCORED, and that is declared.

    A forger who flips the status AND blanks every settled-only field produces a row byte-identical
    to one the settler would have written for a trial that genuinely never settled. Nothing in the
    outcome row is bound to the market, so no predicate here can separate them.

    Declared in the same spirit as the ``future`` boundary above: what is closed is the single-field
    edit, and what remains costs the forger the entire settlement record rather than one word. If a
    later change makes this row fail, the gap has been closed and this test should be REPLACED by
    one asserting that — not deleted quietly.
    """
    store.tamper(settled_receipt.receipt_id, field="outcome.status", value="UNSCORED")
    for field in SETTLED_ONLY_FIELDS:
        store.tamper(settled_receipt.receipt_id, field=f"outcome.{field}", value=None)
    assert _verdict(settled_receipt.receipt_id, store) == _unsettled(), (
        "a fully consistent erasure is the DOCUMENTED limit of the status check"
    )


# ==================================================================================================
# FOUND BY THE C63 COVERAGE MATRIX — horizon_ms was the THIRD field of N1's class.
#
# Not reported by SPEC. The field-driven matrix built for N1/N2 found it on its first run: a
# boundary input the row supplies about itself, with an available referent (FROZEN_HORIZON_MS,
# section 8.3) that `settle_trial` hard-codes rather than accepts. `bar` and `t0_ms` came out of the
# same run and are DECLARED rather than bound — see the tests below for why each is different.
# ==================================================================================================


@pytest.mark.parametrize(
    "forged", [1_800_000, 7_200_000, 0, "3600000", None], ids=["half", "double", "zero", "string", "null"]
)
def test_a_forged_HORIZON_does_not_verify(settled_receipt: CommitRecord, store: _TamperableStore, forged: Any) -> None:
    """The horizon is §8.3's frozen 1h, and a recorded outcome may claim no other."""
    store.tamper(settled_receipt.receipt_id, field="outcome.horizon_ms", value=forged)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(outcome_source="fail")


def test_a_SELF_CONSISTENT_forgery_off_a_different_horizon_is_caught(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """The discriminating row: the lag is restated, so every other boundary relation still holds.

    ``lag == close_ts - (t0 + horizon)`` is satisfied for ANY ``(t0, horizon)`` pair summing to the
    same target, so a forger who halves the horizon and restates the lag leaves the arithmetic
    perfect. It fails only because the horizon is anchored to the frozen constant — which is what
    a lone-field probe cannot show, since a lone horizon edit breaks the lag and is caught for the
    wrong reason.
    """
    row = store.outcome_payload(TRIAL_ID)
    assert row is not None
    forged_horizon = FROZEN_HORIZON_MS // 2
    # The candle is slid too. Restating only the LAG leaves it outside the half-open window, which
    # the boundary refuses for its own reason -- and a test that stopped there would pass with the
    # horizon binding deleted. A mutation run confirmed exactly that.
    target = row["t0_ms"] + forged_horizon
    for field, value in (
        ("horizon_ms", forged_horizon),
        ("settlement_ts_open_ms", target - row["bar_ms"]),
        ("close_ts_ms", target),
        ("observation_lag_ms", 0),
    ):
        store.tamper(settled_receipt.receipt_id, field=f"outcome.{field}", value=value)

    forged_row = store.outcome_payload(TRIAL_ID)
    assert forged_row is not None
    # INTERNALLY CONSISTENT: every boundary relation holds against the claimed horizon.
    assert forged_row["close_ts_ms"] == forged_row["settlement_ts_open_ms"] + forged_row["bar_ms"]
    assert forged_row["observation_lag_ms"] == forged_row["close_ts_ms"] - (
        forged_row["t0_ms"] + forged_row["horizon_ms"]
    )
    assert 0 <= forged_row["observation_lag_ms"] < forged_row["bar_ms"]
    # It fails ONLY because the horizon is anchored to the frozen constant.
    assert _verdict(settled_receipt.receipt_id, store) == _clean(outcome_source="fail")


def test_the_DECLARED_bar_limit_is_pinned_rather_than_implied(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """``bar_version`` checks pair MEMBERSHIP, not matrix SELECTION, and that gap is declared.

    Moving BOTH halves to the other legal pair and restating the boundary produces a row that
    passes. It is declared rather than closed because the only artifact naming the selected bar is
    the published season, and a season is REPUBLISHED — a verifier bound to it would start failing
    every earlier season's receipts, which is the same defect as consulting the live trial store.
    The selection is enforced at the writer instead, where it is decidable and permanent.

    No score moves here, and that is part of the disposition: no markout leg reads the bar. If a
    later change closes this, REPLACE this test rather than deleting it.
    """
    row = store.outcome_payload(TRIAL_ID)
    assert row is not None
    target = row["t0_ms"] + row["horizon_ms"]
    for field, value in (
        ("bar", "1H"),
        ("bar_ms", BAR_MS["1H"]),
        ("settlement_ts_open_ms", target - BAR_MS["1H"]),
        ("close_ts_ms", target),
        ("observation_lag_ms", 0),
    ):
        store.tamper(settled_receipt.receipt_id, field=f"outcome.{field}", value=value)

    assert _verdict(settled_receipt.receipt_id, store) == _clean(), (
        "a consistent move to the other frozen pair is the DECLARED limit of bar_version"
    )
    # The score is untouched, which is why this is a provenance claim and not a scoring one.
    assert build_agent_record(PAYER, store).avg_brier == pytest.approx((0.8 - 1) ** 2)


def test_the_DECLARED_t0_limit_is_pinned_rather_than_implied(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """``t0_ms`` has no referent this verifier may reach, and the row states which two it rules out.

    Not the evidence's ``t0_ms``: ``open_live_trial`` permits the signal's observation time and the
    trial's open time to differ, so comparing them would fail honest rows. Not the live trial store:
    a check that needed it could only verify recent commitments.

    The fixture is built with the two times EQUAL, so this test would pass by accident if the
    verifier did compare them — the assertion below pins that they are equal in the fixture, making
    the limit a real statement rather than an artefact of the data.

    The forger must also SLIDE THE CANDLE, and that is a genuine part of the disposition rather
    than a detail of the fixture. Moving ``t0`` alone pushes the lag outside the half-open window
    and IS caught — for the window's reason, not for ``t0``'s — so what passes is a forger who
    restates the whole settlement narrative: a different open time, a different settlement candle,
    a consistent boundary. That is the cost, and it is why the gap is narrow enough to declare.
    """
    row = store.outcome_payload(TRIAL_ID)
    assert row is not None
    assert row["t0_ms"] == row["evidence"]["t0_ms"], "the fixture must have the two times equal"

    shifted = row["t0_ms"] - 600_000
    target = shifted + row["horizon_ms"]
    for field, value in (
        ("t0_ms", shifted),
        ("settlement_ts_open_ms", target - row["bar_ms"]),
        ("close_ts_ms", target),
        ("observation_lag_ms", 0),
    ):
        store.tamper(settled_receipt.receipt_id, field=f"outcome.{field}", value=value)

    assert _verdict(settled_receipt.receipt_id, store) == _clean(), (
        "a fully restated t0 and candle is the DECLARED limit of the close-boundary check"
    )


def test_moving_t0_ALONE_is_still_caught_by_the_window(settled_receipt: CommitRecord, store: _TamperableStore) -> None:
    """DISCRIMINATION for the declared limit above: the cheap version of that forgery IS reported.

    Declaring a limit is only honest if the limit is as narrow as claimed. Shifting ``t0`` by ten
    minutes without moving the candle leaves a lag of ten minutes on a one-minute bar, which the
    half-open window refuses. So the declared gap costs the forger the settlement candle too.
    """
    row = store.outcome_payload(TRIAL_ID)
    assert row is not None
    shifted = row["t0_ms"] - 600_000
    store.tamper(settled_receipt.receipt_id, field="outcome.t0_ms", value=shifted)
    store.tamper(
        settled_receipt.receipt_id,
        field="outcome.observation_lag_ms",
        value=row["close_ts_ms"] - (shifted + row["horizon_ms"]),
    )
    assert _verdict(settled_receipt.receipt_id, store) == _clean(outcome_source="fail")


def test_the_recorded_SETTLEMENT_SOURCE_is_verified_against_the_sealed_evidence(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """§7's persisted source is checked, not merely stored.

    A settlement fetched from the wrong chain is refused at write time by the operator script, but
    a row already on disk is past every write-time gate — only this comparison can report it. The
    sealed evidence names the chain the signal was observed on, which is what makes the recorded
    chain checkable at all rather than a self-consistent copy.
    """
    store.tamper(settled_receipt.receipt_id, field="outcome.chain_index", value=OTHER_CHAIN_INDEX)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(outcome_source="fail")


@pytest.mark.parametrize(
    "forged",
    ["https://web3.okx.com/api/v6/dex/market/candles", "", None, 196],
    ids=["different-endpoint", "empty", "null", "not-a-string"],
)
def test_a_settlement_from_an_UNRECOGNIZED_endpoint_does_not_verify(
    settled_receipt: CommitRecord, store: _TamperableStore, forged: Any
) -> None:
    """The recorded endpoint must be the one candles path this build settles from.

    ``candles`` and ``historical-candles`` are different OKX endpoints serving different data, and
    the first row is the realistic confusion. The host half is NOT constrained — nothing on disk can
    attest it — so this is the same kind of claim ``bar_version`` makes: a value this law could have
    produced.
    """
    store.tamper(settled_receipt.receipt_id, field="outcome.source_endpoint", value=forged)
    assert _verdict(settled_receipt.receipt_id, store) == _clean(outcome_source="fail")


def test_an_intact_settlement_recorded_through_the_REAL_settler_verifies_clean(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """ACCEPTANCE CONTROL for every discriminating row above.

    Each test in this block asserts a ``fail``, and a check hard-wired to ``fail`` would satisfy all
    of them. This is the row that makes them mean something: the same fixture, untampered, verifying
    eight ``pass`` — and the outcome was produced by ``settle_trial`` rather than hand-built, so the
    writer and the verifier are being held to each other rather than to a fixture that agreed with
    one of them by construction.
    """
    assert _verdict(settled_receipt.receipt_id, store) == _clean()


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
        # §7's persisted SOURCE. Absent until H4.3's remediation, which is how a settlement fetched
        # from the wrong chain could be recorded with nothing on disk able to notice.
        "chain_index",
        "source_endpoint",
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
    store.record_outcome(TRIAL_ID, _settle(trial, _empty_series(), now_ms=now_ms))
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


# ==================================================================================================
# CODEX MAJOR — the 500 H4.2 spent four rounds removing, reintroduced by H4.3's receipt envelope.
#
# `_commit_receipt_response` caught bounded row failures only through `store.outcome()`;
# `settle_commit()` and `CommitReceiptResponse(...)` sat OUTSIDE the guard. So a row the verifier
# evaluated perfectly well — eight attributable verdicts ready to publish — crashed while the newly
# embedded receipt was rendered, and the route answered 500. That is the exact tampering-versus-
# outage conflation the route's docstring says it exists to refuse.
#
# The `NaN` row is the one that proves a guard around the two reads was never going to be enough:
# pydantic ACCEPTS a non-finite float, and the failure lands in the JSON renderer AFTER the handler
# has returned, where no `try` in this module can reach it. It is refused at the store read instead.
# ==================================================================================================


@pytest.mark.parametrize(
    ("field", "forged", "failing"),
    [
        # Non-finite probabilities. `json.loads` accepts all three literals and `float()` builds
        # them happily; the response renderer is RFC-compliant and will not emit them.
        ("p_follow_profitable", float("nan"), ("body_hash",)),
        ("p_follow_profitable", float("inf"), ("body_hash",)),
        ("p_follow_profitable", float("-inf"), ("body_hash",)),
        # Wrong-SHAPED nullable and string fields. These pass through `_record_from` uncoerced by
        # design — `null` is a tamper signal the model is meant to serve — so the model is the first
        # thing that sees them, and it raises.
        ("commit_deadline_ms", "not-an-int", ("manifest", "deadline_respected")),
        ("commit_deadline_ms", [1, 2, 3], ("manifest", "deadline_respected")),
        ("trial_mode", [1, 2, 3], ("manifest", "live_mode")),
        ("committed_at_ms", {"nested": 1}, ("manifest", "deadline_respected")),
    ],
    ids=["nan", "inf", "-inf", "deadline-string", "deadline-list", "mode-list", "committed-object"],
)
async def test_a_tampered_finalized_ROW_is_a_200_with_a_null_receipt_and_never_a_500(
    settled_receipt: CommitRecord, store: _TamperableStore, field: str, forged: Any, failing: tuple[str, ...]
) -> None:
    """Every one of these was a 500 before the guard was widened. Each is now a published verdict.

    The failing checks are asserted BY NAME rather than merely "some check failed": the point of
    answering 200 is that the eight verdicts are worth publishing, so a response carrying eight
    passes and a null receipt would be a worse lie than the 500 it replaced.
    """
    store.tamper(settled_receipt.receipt_id, field=field, value=forged)
    async with _client(_app(store=store), raise_app_exceptions=False) as client:
        response = await client.get(f"/signal-trials/receipts/{settled_receipt.receipt_id}/verify")

    assert response.status_code == 200, "a tampered row must be a finding, not an outage"
    body = response.json()
    assert body["receipt"] is None
    assert body["checks"] == _clean(**dict.fromkeys(failing, "fail"))


async def test_an_outcome_that_claims_SETTLED_with_no_verdict_is_a_200_and_not_a_500(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """The ``settle_commit()`` half of the widened boundary, which the row tampers above never reach.

    A settled outcome carrying no ``follow_profitable`` makes ``settle_commit`` raise rather than
    invent an outcome indicator — correct, and it was escaping to the client as a 500. The receipt
    is null and ``outcome_source`` reports the missing verdict, which is the finding.
    """
    store.tamper(settled_receipt.receipt_id, field="outcome.follow_profitable", value=None)
    async with _client(_app(store=store), raise_app_exceptions=False) as client:
        response = await client.get(f"/signal-trials/receipts/{settled_receipt.receipt_id}/verify")

    assert response.status_code == 200
    body = response.json()
    assert body["receipt"] is None
    assert body["checks"] == _clean(outcome_source="fail")


async def test_an_OSError_from_the_disk_is_STILL_a_500_and_not_an_honest_looking_verdict(
    settled_receipt: CommitRecord, store: _TamperableStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DISCRIMINATION CONTROL for every row above, and the constraint that bounds the fix.

    Widening WHAT is guarded must not widen WHAT IS CAUGHT. A fix that normalized every failure to
    ``receipt: null`` would satisfy all seven rows above while laundering an outage into a
    verdict — telling a receipt holder their receipt is fine when the service could not read it.
    That is strictly worse than the bug being fixed, so the same fault must still be a 500.

    Injected at ``Path.read_text`` rather than by ``chmod``, so it is a real ``OSError`` from the
    read and does not depend on the test running as a non-root user.
    """
    real_read_text = Path.read_text

    def exploding_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.suffix == ".json" and _FINALIZED_DIRNAME in self.parts:
            raise OSError("simulated unreadable disk")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", exploding_read_text)
    async with _client(_app(store=store), raise_app_exceptions=False) as client:
        response = await client.get(f"/signal-trials/receipts/{settled_receipt.receipt_id}/verify")
    assert response.status_code == 500, "an infrastructure fault must not be reported as a receipt verdict"


async def test_an_OSError_from_INSIDE_the_widened_region_is_still_a_500(
    settled_receipt: CommitRecord, store: _TamperableStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control that actually measures the guard's BREADTH, and the one the disk test cannot.

    A fault injected at the disk reaches ``verify_receipt`` first — which absorbs only
    ``ValueError`` — so the 500 above is produced BEFORE the receipt renderer is ever called. That
    makes it a fine test of the verifier and a vacuous one of this guard: widening the catch here
    to ``except Exception`` leaves it green, which a mutation run confirmed.

    So the fault is injected where ONLY the widened region runs. ``settle_commit`` is inside the
    guard now and was outside it before, and an ``OSError`` from there is the service being broken.
    If this ever returns 200 with a null receipt, the guard has started laundering outages into
    honest-looking verdicts — the failure mode that is strictly worse than the bug it replaced.
    """

    def exploding_settle_commit(record: Any, outcome: Any) -> Any:
        raise OSError("simulated infrastructure fault from inside the guarded region")

    monkeypatch.setattr(signal_trials_router, "settle_commit", exploding_settle_commit)
    async with _client(_app(store=store), raise_app_exceptions=False) as client:
        response = await client.get(f"/signal-trials/receipts/{settled_receipt.receipt_id}/verify")
    assert response.status_code == 500, "the widened guard swallowed an infrastructure fault"


def test_a_non_finite_probability_is_refused_where_every_caller_reads_a_corrupt_row(
    settled_receipt: CommitRecord, store: _TamperableStore
) -> None:
    """The store read is where ``NaN`` becomes a ``ValueError``, and the route only inherits that.

    Asserted at the store rather than only through the endpoint because the endpoint cannot
    distinguish "the row was refused" from "the model rejected it", and the refusal has to be at
    the boundary every caller already treats as corruption — otherwise the next consumer of
    ``record()`` reintroduces the same crash somewhere this test is not watching.
    """
    store.tamper(settled_receipt.receipt_id, field="p_follow_profitable", value=float("nan"))
    with pytest.raises(ValueError, match="non-finite"):
        store.record(settled_receipt.receipt_id)


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
    settled = _settle(_trial(), _empty_series(), now_ms=T0 + 60_000)
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


# ==================================================================================================
# QUALITY F2 — the operator script's controls are WIRED, not merely present.
#
# ``settle_live_trials.py`` is +337 lines on the operator path, and before this block the only thing
# any test in this repository touched was the pure predicate ``series_covers_settlement``. Four
# mutants of the script therefore survived the whole 856-test suite: the coverage gate deleted from
# ``_record_one``, ``redact()`` deleted from the failure path, ``return 2`` on a missing credential
# softened to ``return 0``, and ``_eligible``'s horizon-and-terminal filtering deleted.
#
# A GUARD THAT IS TESTED BUT NOT WIRED IS NOT A GUARD, and this program has already written that
# lesson down — about the SISTER script, at ``test_preflight.py:2158``, where deleting the
# ``redact()`` call at ``run_preflight.py:218`` left 463 tests green. ``settle_live_trials.py:33-38``
# declares its own ``redact`` and credential reader a deliberate LOCAL COPY of that script's,
# "noted here so a change to the credential contract is known to have two sites". The copy was made
# and the pin was not. Both sites carry one now.
#
# GATE A — no test below can reach a live endpoint, and this is enforced three ways rather than
# assumed: ``httpx.AsyncClient`` and ``OKXMarketClient`` are both replaced in the module namespace,
# ``socket.socket.connect``/``connect_ex`` are tripwires, and every credential is a SENTINEL string
# that no exchange issued. The tripwire derives from ``BaseException`` deliberately — ``main()``
# wraps the entire run in ``except Exception``, so a tripwire raising an ordinary exception would be
# CAUGHT BY THE CODE IT GUARDS and rendered as a tidy "settlement FAILED": the safety mechanism
# fires and the suite stays green. ``test_preflight.py:2094`` records that as a measurement, not a
# worry, and it is the same handler shape here.
# ==================================================================================================


class _LiveConnectionAttempted(BaseException):
    """Raised if anything in these tests tries to open a real connection. See GATE A above."""


def _refuse_connection(*args: object, **kwargs: object) -> None:
    raise _LiveConnectionAttempted("TRIPWIRE: a test attempted a real network connection")


class _NoNetworkAsyncClient:
    """Stands in for ``httpx.AsyncClient`` so the script's ``async with`` has something to hold.

    It carries no ``request`` method at all, so a run that routed a call through the transport
    instead of through the stubbed market client fails with ``AttributeError`` here rather than
    reaching an endpoint.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> _NoNetworkAsyncClient:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _StubMarketClient:
    """Serves one prepared series for every trial, and records what it was asked for.

    The call log is what makes the "nothing was written" assertions non-vacuous: without it, a run
    that never reached the fetch at all would satisfy them for entirely the wrong reason.
    """

    def __init__(self, series: CandleSeries) -> None:
        self._series = series
        self.calls: list[tuple[str, str, str]] = []

    async def get_candles(self, chain_index: str, token: str, bar: str, *, limit: int = 100) -> CandleSeries:
        self.calls.append((chain_index, token, bar))
        return self._series


def _publish_selection(root: Path, *, chain_index: str = CHAIN_INDEX, bar: str = "1m") -> None:
    """Publish the season document naming the matrix-selected ``(chain x bar)`` for ``root``.

    Every ``main()`` test needs one, because the script now REFUSES to record a terminal outcome
    under a bar no published artifact authorizes. That refusal is the point of the gate, so the
    prerequisite is spelled here rather than hidden in a fixture: a test that wants the run to
    proceed has to say which selection it is proceeding under.
    """
    write_season(
        root,
        {
            "season_id": "season_h43",
            "season_status": "exploratory",
            "combo": {"chain_index": chain_index, "bar": bar},
            "sample_size": 1,
            "rows": [],
        },
    )
    write_state(root, "exploratory", {})


def _set_sentinel_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials that are self-evidently not credentials, and are greppable if one ever escapes."""
    monkeypatch.setenv("OKX_API_KEY", "SENTINEL-KEY-DO-NOT-LEAK")
    monkeypatch.setenv("OKX_SECRET_KEY", "SENTINEL-SECRET-DO-NOT-LEAK")
    monkeypatch.setenv("OKX_PASSPHRASE", "SENTINEL-PASS-DO-NOT-LEAK")


@pytest.fixture
def settle_operator(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The settle script loaded BY PATH with every network seam closed and the credential env cleared.

    Cleared rather than left alone so an operator's real environment cannot leak into a test run,
    and so the credential-abort test asserts against a known-empty environment.
    """
    module = _settle_script()
    monkeypatch.setattr(module.httpx, "AsyncClient", _NoNetworkAsyncClient)
    monkeypatch.setattr(socket.socket, "connect", _refuse_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse_connection)
    for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE", "OKX_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    return module


def _covering_series() -> CandleSeries:
    """A well-formed fetch that STRADDLES the settlement window and still holds no eligible candle.

    Confirmed closes at ``T - bar`` and ``T + bar``: the first proves the fetch reached back past
    the window, the second that it reached forward past it, and neither lands inside ``[T, T + bar)``.
    This is the genuine absence §12 reserves UNSCORED for — an illiquid token with no trades in that
    minute — as opposed to a fetch that simply did not answer the question.
    """
    return CandleSeries(
        bar="1m", bar_ms=60_000, candles=tuple(_candle(ts_open_ms=T + o - 60_000) for o in (-60_000, 60_000))
    )


@pytest.mark.parametrize(
    ("series_factory", "expected_status", "expected_recorded", "expected_on_disk"),
    [
        (_empty_series, "coverage_gap", False, None),
        (_covering_series, "UNSCORED", True, "UNSCORED"),
    ],
    ids=["unproven-fetch-records-NOTHING", "proven-gap-records-UNSCORED"],
)
def test_the_coverage_gate_is_WIRED_into_the_write_path(
    settle_operator: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    series_factory: Any,
    expected_status: str,
    expected_recorded: bool,
    expected_on_disk: str | None,
) -> None:
    """The script's headline safety property, asserted where it is DECIDED rather than where it is computed.

    The module docstring's strongest claim is that "the one thing this script must never do is
    manufacture an UNSCORED", and ``series_covers_settlement`` is what enforces it. That predicate
    has three direct tests above; its WIRING had none, so deleting the gate from ``_record_one``
    left all 856 tests green — and the artifact that mutant produces is a terminal, published,
    permanently false statement about a trial that settled perfectly well.

    The two rows are an acceptance/discrimination pair over the ONE thing that differs between them.
    Both series are past the UNSCORED boundary and both make the law return UNSCORED; only one of
    them PROVES the window was covered. So a gate that always refused would fail the second row and
    a gate that never refused would fail the first, and neither row can pass by the run having
    quietly done nothing — ``client.calls`` pins that the fetch was actually reached.
    """
    data_dir = tmp_path
    trial = _trial()
    LiveTrialRepository(data_dir / settle_operator.LIVE_SUBDIR).publish(trial)
    _publish_selection(data_dir)
    _set_sentinel_credentials(monkeypatch)

    client = _StubMarketClient(series_factory())
    monkeypatch.setattr(settle_operator, "OKXMarketClient", lambda transport, creds: client)

    now_ms = T + 60_000 + FETCH_GRACE_MS
    exit_code = settle_operator.main(
        ["--data-dir", str(data_dir), "--chain-index", "196", "--bar", "1m", "--now-ms", str(now_ms)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    # ACCEPTANCE CONTROL: the run reached the network path for exactly this trial. Without it, every
    # assertion below is also satisfied by a run that never got that far.
    assert client.calls == [("196", trial.sig.token_address, "1m")]

    payload = json.loads(captured.out)
    assert payload["eligible"] == 1
    (summary,) = payload["trials"]
    assert summary["status"] == expected_status
    assert summary["recorded"] is expected_recorded

    # The only assertion that matters to an agent who paid: what is TERMINALLY on disk.
    outcome = ReceiptStore(data_dir).outcome(TRIAL_ID)
    if expected_on_disk is None:
        assert outcome is None, "an unproven fetch wrote a terminal outcome; the coverage gate is not wired"
    else:
        assert outcome is not None and outcome.status == expected_on_disk


def test_main_REDACTS_credentials_out_of_the_failure_report(
    settle_operator: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE credential-surface pin for this script, and it exists because the sister script needed one.

    Deleting the ``redact()`` call from this script's failure path left all 856 tests green, exactly
    as deleting it from ``run_preflight.py:218`` once left 463 green. ``redact`` itself is a local
    copy, tested in isolation three times over on the other script and wired nowhere here.

    The failure is injected at ``_settle_all`` because that is the vector the module docstring names:
    "a traceback from httpx can carry a signed URL". The message below is shaped like one — a query
    string carrying two of the three credential values — so this asserts against the real hazard and
    not against a string that merely happens to contain the sentinel.
    """
    _publish_selection(tmp_path)
    _set_sentinel_credentials(monkeypatch)
    LiveTrialRepository(tmp_path / settle_operator.LIVE_SUBDIR).publish(_trial())

    async def exploding_settle_all(trials: Any, store: Any, creds: Any, args: Any, *, now_ms: int) -> Any:
        raise RuntimeError(
            "HTTPStatusError for GET /api/v5/wallet/token/candles"
            "?apiKey=SENTINEL-KEY-DO-NOT-LEAK&sign=SENTINEL-SECRET-DO-NOT-LEAK"
        )

    monkeypatch.setattr(settle_operator, "_settle_all", exploding_settle_all)
    exit_code = settle_operator.main(
        ["--data-dir", str(tmp_path), "--chain-index", "196", "--bar", "1m", "--now-ms", str(T)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == "", "a failed run must print no summary a shell could read as a settled season"
    assert "SENTINEL" not in captured.err, "a credential value reached the operator's terminal"
    assert "settlement FAILED" in captured.err
    # STANDING LESSON 62: a guard firing is not the guard under test. `except Exception` catches
    # everything, so exit 1 plus "settlement FAILED" is reachable by any error at all — including a
    # bug in this test's own setup. Identify the exception that was actually injected.
    assert "RuntimeError" in captured.err, f"a different guard fired: {captured.err}"
    assert "***" in captured.err, "the reason must be REDACTED, not merely emptied"
    assert "HTTPStatusError for GET" in captured.err, "redaction must not destroy the diagnostic"


def test_main_ABORTS_with_a_status_a_shell_cannot_read_as_a_settled_season(
    settle_operator: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit ``2`` is a contract, not an implementation detail, and softening it left 856 tests green.

    ``main()``'s docstring fixes the two failure statuses differently on purpose so an operator can
    tell a missing variable from a failed run. The mutant that returns ``0`` here is not a cosmetic
    one: exit 0 is exactly what a shell running ``settle.py && publish.py`` reads as a settled
    season, so the softened script publishes a season it never settled.

    The selection is published first so this reaches the CREDENTIAL abort rather than the earlier
    selection abort. Both exit ``2`` — they are the same class of fault, a bad invocation caught
    before any request — and this test is about the credential one, so it has to get past the other.
    """
    _publish_selection(tmp_path)
    exit_code = settle_operator.main(
        ["--data-dir", str(tmp_path), "--chain-index", "196", "--bar", "1m", "--now-ms", str(T)]
    )
    captured = capsys.readouterr()

    assert exit_code == 2, "a credential abort must not be readable as a successful run"
    assert captured.out == "", "an aborted run must print no summary"
    assert "aborted before any request" in captured.err
    # Exit 2 alone cannot say WHICH guard produced it, and an operator who has set two of three
    # variables needs to be told which one is missing rather than that something is.
    for variable in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"):
        assert variable in captured.err, f"the abort did not name {variable}"


def test_only_trials_PAST_their_horizon_and_NOT_already_terminal_are_settled(
    settle_operator: Any, store: _TamperableStore
) -> None:
    """``_eligible`` is what decides which trials a run touches at all, and it had no test.

    Deleting both filters left 856 tests green. The consequence is not merely wasted requests: a
    re-fetch of an already-terminal trial produces a run whose every line is a refusal from
    ``record_outcome``, which is precisely the output the docstring says the filter exists to spare
    an operator from reading past.

    ``trial_pending`` is the discrimination control and it is the one that matters. A filter written
    against "has an outcome recorded" rather than against TERMINAL_STATUSES passes every other row
    here and silently strands every pending trial — permanently, because a pending trial's outcome
    only becomes settleable on a LATER run than the one that recorded it.
    """
    now_ms = T + 60_000 + FETCH_GRACE_MS
    past = _trial(trial_id="trial_past")
    settled_already = _trial(trial_id="trial_settled")
    unscored_already = _trial(trial_id="trial_unscored")
    still_pending = _trial(trial_id="trial_pending")
    not_yet = replace(_trial(trial_id="trial_not_yet"), t0_ms=now_ms - FROZEN_HORIZON_MS + 1)

    store.record_outcome("trial_settled", _settled_outcome_from(_series(), trial_id="trial_settled"))
    store.record_outcome("trial_unscored", _settle(unscored_already, _empty_series(), now_ms=now_ms))
    store.record_outcome("trial_pending", _settle(still_pending, _empty_series(), now_ms=T0 + 60_000))
    pending_payload = store.outcome_payload("trial_pending")
    assert pending_payload is not None and pending_payload["status"] == "pending", "the control row is not pending"

    trials = [past, settled_already, unscored_already, still_pending, not_yet]
    eligible = settle_operator._eligible(trials, store, now_ms=now_ms, trial_id=None)
    assert [trial.trial_id for trial in eligible] == ["trial_past", "trial_pending"]

    # The `--trial-id` selector narrows the same set rather than bypassing either filter.
    assert settle_operator._eligible(trials, store, now_ms=now_ms, trial_id="trial_pending") == [still_pending]
    assert settle_operator._eligible(trials, store, now_ms=now_ms, trial_id="trial_settled") == []


def _crash_after_terminal_outcome(
    settle_operator: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    terminal_status: str,
    trial_id: str = TRIAL_ID,
    receipt_id: str = "rcpt_terminal_crash",
) -> tuple[LiveTrial, CommitRecord, bytes]:
    """Create the real outcome-written/settlement-missing crash state.

    The trial and finalized commitment both use their production writers.  The only injected
    failure is the first participant-settlement write, after :func:`_record_one` has durably
    recorded the terminal outcome.  That is the exact non-transactional boundary this regression
    exists to recover, not a hand-authored approximation of it.
    """
    trial = _trial(trial_id=trial_id)
    LiveTrialRepository(tmp_path / settle_operator.LIVE_SUBDIR).publish(trial)
    store = ReceiptStore(tmp_path)
    commit = _commit(store, trial, staging_id=f"staging_{receipt_id}")
    series = _series() if terminal_status == "settled" else _covering_series()
    now_ms = T + 60_000 + FETCH_GRACE_MS
    args = SimpleNamespace(cost_bps=DECLARED_COST_BPS, dry_run=False)
    calls = 0

    def crash_on_first_settlement(settlement: ParticipantSettlement) -> None:
        nonlocal calls
        calls += 1
        raise OSError("injected crash after the terminal outcome write")

    with monkeypatch.context() as crash:
        crash.setattr(store, "record_settlement", crash_on_first_settlement)
        with pytest.raises(OSError, match="injected crash"):
            settle_operator._record_one(
                trial,
                series,
                store,
                args,
                now_ms=now_ms,
                source_endpoint=SOURCE_ENDPOINT,
            )

    assert calls == 1, "the injected crash did not occur at the participant-settlement boundary"
    outcome = store.outcome(trial_id)
    assert outcome is not None and outcome.status == terminal_status
    assert store.settlement(commit.receipt_id) is None
    outcome_path = tmp_path / _OUTCOMES_DIRNAME / f"{trial_id}.json"
    return trial, commit, outcome_path.read_bytes()


def _run_terminal_retry(
    settle_operator: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    trial_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the exact-trial operator path with a provider-call tripwire."""
    _publish_selection(tmp_path)
    _set_sentinel_credentials(monkeypatch)

    class _ProviderCallForbidden:
        def __init__(self, transport: Any, creds: Any) -> None:
            raise AssertionError("a terminal reconciliation entered the provider client path")

        async def get_candles(self, *args: Any, **kwargs: Any) -> CandleSeries:
            raise AssertionError("a terminal reconciliation attempted to refetch settlement candles")

    monkeypatch.setattr(settle_operator, "OKXMarketClient", _ProviderCallForbidden)
    argv = [
        "--data-dir",
        str(tmp_path),
        "--chain-index",
        CHAIN_INDEX,
        "--bar",
        "1m",
        "--trial-id",
        trial_id,
        "--now-ms",
        str(T + 60_000 + FETCH_GRACE_MS),
    ]
    if dry_run:
        argv.append("--dry-run")
    exit_code = settle_operator.main(argv)
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    assert captured.err == ""
    return cast(dict[str, Any], json.loads(captured.out))


@pytest.mark.parametrize("terminal_status", ["settled", "UNSCORED"])
def test_a_later_exact_trial_run_reconciles_the_crash_window_once_without_refetching_or_rewriting(
    settle_operator: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    terminal_status: str,
) -> None:
    """RED: terminal history must not strand the paid participant artifact forever.

    This drives the real crash ordering, then invokes ``main`` twice.  The first retry must derive
    the missing participant settlement from the immutable finalized commitment and stored terminal
    outcome; the second must be a complete no-op.  Outcome byte identity makes "repair by
    recomputing/re-recording the event" distinguishable from the required participant-only repair.
    """
    _, commit, original_outcome_bytes = _crash_after_terminal_outcome(
        settle_operator,
        tmp_path,
        monkeypatch,
        terminal_status=terminal_status,
    )
    original_record_settlement = ReceiptStore.record_settlement
    settlement_writes: list[str] = []

    def counted_record_settlement(self: ReceiptStore, settlement: ParticipantSettlement) -> None:
        settlement_writes.append(settlement.receipt_id)
        original_record_settlement(self, settlement)

    monkeypatch.setattr(ReceiptStore, "record_settlement", counted_record_settlement)
    first = _run_terminal_retry(
        settle_operator,
        tmp_path,
        monkeypatch,
        capsys,
        trial_id=TRIAL_ID,
    )

    assert first["eligible"] == 1
    assert len(first["trials"]) == 1
    assert first["trials"][0] == {
        "trial_id": TRIAL_ID,
        "status": terminal_status,
        "candles_fetched": 0,
        "observation_lag_ms": first["trials"][0]["observation_lag_ms"],
        "follow_markout_bps": first["trials"][0]["follow_markout_bps"],
        "recorded": True,
        "settlements_recorded": 1,
        "settlements_missing": 1,
        "reconciliation": "recorded",
    }
    assert settlement_writes == [commit.receipt_id]
    assert ReceiptStore(tmp_path).settlement(commit.receipt_id) is not None
    outcome_path = tmp_path / _OUTCOMES_DIRNAME / f"{TRIAL_ID}.json"
    assert outcome_path.read_bytes() == original_outcome_bytes

    second = _run_terminal_retry(
        settle_operator,
        tmp_path,
        monkeypatch,
        capsys,
        trial_id=TRIAL_ID,
    )
    assert second["eligible"] == 0
    assert second["trials"] == []
    assert settlement_writes == [commit.receipt_id], "a fully reconciled terminal trial was rewritten"
    assert outcome_path.read_bytes() == original_outcome_bytes


def test_terminal_reconciliation_dry_run_reports_but_writes_nothing_and_exact_id_excludes_other_rows(
    settle_operator: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dry-run and exact-id scope apply to the repair path, including non-final commitments."""
    _, target_commit, target_outcome_bytes = _crash_after_terminal_outcome(
        settle_operator,
        tmp_path,
        monkeypatch,
        terminal_status="settled",
    )
    other_id = "trial_terminal_other"
    other_trial, other_commit, other_outcome_bytes = _crash_after_terminal_outcome(
        settle_operator,
        tmp_path,
        monkeypatch,
        terminal_status="UNSCORED",
        trial_id=other_id,
        receipt_id="rcpt_terminal_other",
    )
    store = ReceiptStore(tmp_path)
    store.stage(
        staging_id="staged_not_paid",
        trial_id=TRIAL_ID,
        payer="0xd",
        body=_req(0.41),
        staged_at_ms=T0 + 2_000,
        commit_deadline_ms=other_trial.commit_deadline_ms,
        trial_mode="live",
    )
    pending_before = store.count_pending()

    dry = _run_terminal_retry(
        settle_operator,
        tmp_path,
        monkeypatch,
        capsys,
        trial_id=TRIAL_ID,
        dry_run=True,
    )
    assert dry["eligible"] == 1
    assert dry["trials"][0]["reconciliation"] == "would_record"
    assert dry["trials"][0]["recorded"] is False
    assert dry["trials"][0]["settlements_missing"] == 1
    assert dry["trials"][0]["settlements_recorded"] == 0
    assert store.settlement(target_commit.receipt_id) is None
    assert store.settlement(other_commit.receipt_id) is None
    assert store.count_pending() == pending_before

    live = _run_terminal_retry(
        settle_operator,
        tmp_path,
        monkeypatch,
        capsys,
        trial_id=TRIAL_ID,
    )
    assert live["eligible"] == 1
    assert store.settlement(target_commit.receipt_id) is not None
    assert store.settlement(other_commit.receipt_id) is None
    assert store.count_pending() == pending_before
    assert (tmp_path / _OUTCOMES_DIRNAME / f"{TRIAL_ID}.json").read_bytes() == target_outcome_bytes
    assert (tmp_path / _OUTCOMES_DIRNAME / f"{other_id}.json").read_bytes() == other_outcome_bytes


def test_a_fully_reconciled_terminal_trial_never_calls_the_write_once_store_again(
    settle_operator: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An existing settlement is not offered back to the idempotent writer as a pseudo-repair."""
    _, commit, _ = _crash_after_terminal_outcome(
        settle_operator,
        tmp_path,
        monkeypatch,
        terminal_status="settled",
    )
    store = ReceiptStore(tmp_path)
    outcome = store.outcome(TRIAL_ID)
    assert outcome is not None
    store.record_settlement(settle_commit(commit, outcome))

    def rewrite_forbidden(self: ReceiptStore, settlement: ParticipantSettlement) -> None:
        raise AssertionError("existing participant settlement was offered for rewrite")

    monkeypatch.setattr(ReceiptStore, "record_settlement", rewrite_forbidden)
    result = _run_terminal_retry(
        settle_operator,
        tmp_path,
        monkeypatch,
        capsys,
        trial_id=TRIAL_ID,
    )
    assert result["eligible"] == 0
    assert result["trials"] == []


def test_the_published_check_names_match_the_frozen_tuples(
    store: _TamperableStore, committed_receipt: CommitRecord
) -> None:
    """QUALITY F6. ``checks`` freezes its VALUES in the annotation and cannot freeze its KEYS.

    ``dict[str, Literal["pass", "fail", "pending"]]`` publishes unconstrained keys, so H5.1's mirror
    receives ``Record<string, …>`` and would have to re-declare the eight names out of band — a
    second site that can drift from :data:`VERIFY_COMMIT_CHECKS` / :data:`VERIFY_OUTCOME_CHECKS`
    with nothing able to notice, in the one surface whose whole purpose is that the frontend keeps
    no second copy of anything.

    A ``Literal``-keyed annotation was the obvious fix and is not available: ``signal_trials_schemas``
    imports nothing from ``signal_trials`` and CANNOT, because ``live.py`` imports ``CommitRequest``
    from it and the dependency runs one way only. A hand-written ``Literal`` would therefore be a
    copy of the eight names living at the same distance from the tuples as the docstring list, with
    the identical drift exposure — and it would additionally turn a name drift into a 500 on the
    verify route, whose stated design is that a finding about a receipt is never an outage.

    So the published key list is prose, and this test is what makes it a contract instead of a
    comment. It asserts the exact names IN ORDER, so a name added on the receipts side and not here
    fails, and a name listed here that no longer exists fails too.
    """
    published = VerifyReceiptResponse.__doc__
    assert published is not None
    marker = "**The key set is exactly these eight names, in this order:**"
    _, _, tail = published.partition(marker)
    assert tail, "the published key list is gone — a mirror has nothing authoritative to read"
    listed = tuple(re.findall(r"``([a-z_]+)``", tail.split(".", 1)[0]))
    assert listed == (*VERIFY_COMMIT_CHECKS, *VERIFY_OUTCOME_CHECKS)

    # And the list describes what the VERIFIER actually serves, not merely what the tuples declare.
    # Without this the test pins two declarations to each other and neither to a response, which is
    # the shape of pin that stays green while the thing it is about drifts.
    assert listed == tuple(_verdict(committed_receipt.receipt_id, store))


# ==================================================================================================
# CODEX MAJOR — the operator could settle against the wrong chain or bar and every check passed.
#
# `settle_live_trials.py` fetched with the operator-supplied `--chain-index` without deriving or
# validating it against `trial.sig.chain_index`, and `bar_version` only ever asked whether the
# recorded (label, width) formed ONE ALLOWED PAIR — never whether it was the MATRIX-SELECTED pair.
# Codex sealed a trial on chain 196, ran with `--chain-index 501`, and got a settled outcome with
# all four outcome checks PASS. The candles are real, the law applies cleanly, and the settlement
# is of a completely different token's market.
#
# GATE A applies to every test below: no live endpoint is reachable. See the block above.
# ==================================================================================================


def _settle_run(
    settle_operator: Any,
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
    *,
    trial: LiveTrial,
    chain_index: str,
    bar: str,
    publish: bool = True,
    season_chain: str = CHAIN_INDEX,
    season_bar: str = "1m",
    extra: tuple[str, ...] | list[str] = (),
) -> tuple[int, _StubMarketClient]:
    """Publish ``trial``, run the operator once, and return its exit status and the fetch log.

    The client is returned rather than asserted on here because "what was fetched" is the whole
    question in this block: a refusal that happened AFTER the fetch is a different and much weaker
    property than one that happened before it.
    """
    LiveTrialRepository(data_dir / settle_operator.LIVE_SUBDIR).publish(trial)
    if publish:
        _publish_selection(data_dir, chain_index=season_chain, bar=season_bar)
    _set_sentinel_credentials(monkeypatch)
    client = _StubMarketClient(_series(bar=bar, bar_ms=BAR_MS[bar]))
    monkeypatch.setattr(settle_operator, "OKXMarketClient", lambda transport, creds: client)
    exit_code = settle_operator.main(
        [
            "--data-dir",
            str(data_dir),
            "--chain-index",
            chain_index,
            "--bar",
            bar,
            "--now-ms",
            str(T + FROZEN_HORIZON_MS),
            *extra,
        ]
    )
    return exit_code, client


@pytest.mark.parametrize(
    ("chain_index", "bar", "named"),
    [
        (OTHER_CHAIN_INDEX, "1m", "--chain-index"),
        (CHAIN_INDEX, "1H", "--bar"),
    ],
    ids=["cross-chain", "wrong-selected-bar"],
)
def test_a_run_that_does_not_match_the_PUBLISHED_SELECTION_is_refused_before_any_fetch(
    settle_operator: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    chain_index: str,
    bar: str,
    named: str,
) -> None:
    """Both of Codex's reproductions, refused — and refused BEFORE the network, not after.

    The season is published naming ``(196, 1m)``; each row invokes the settler against something
    else. Before this gate both rows produced a settled outcome with all four outcome checks
    passing, which is a false all-pass report over a settlement of the wrong market.

    ``client.calls == []`` is the load-bearing assertion. A gate that refused only at the write
    would still have made the request, and on a real endpoint that is a rate-limited call against
    the wrong chain; more importantly, a refusal after the fact leaves the wrong series in memory
    for any later edit to record.
    """
    exit_code, client = _settle_run(
        settle_operator, monkeypatch, tmp_path, trial=_trial(), chain_index=chain_index, bar=bar
    )
    captured = capsys.readouterr()

    assert exit_code == 2, "a mismatched selection must not be readable as a settled season"
    assert client.calls == [], "the run reached the network before checking its own selection"
    assert captured.out == "", "an aborted run must print no summary a shell could read as success"
    assert "aborted before any request" in captured.err
    assert named in captured.err, f"the abort did not name which flag disagreed: {captured.err}"
    assert ReceiptStore(tmp_path).outcome(TRIAL_ID) is None, "a refused run wrote a terminal outcome"


def test_a_run_with_NO_PUBLISHED_SEASON_refuses_rather_than_settling_unchecked(
    settle_operator: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """FAIL CLOSED. With nothing naming the selected bar there is nothing to check ``--bar`` against.

    The alternative — proceed when no season is published — leaves the wrong-bar hole open in
    exactly the state where it is least visible, and what it produces is a TERMINAL outcome
    published to the agents who paid to commit and never revisited. Same reasoning as
    ``series_covers_settlement``: when the run cannot be shown to have answered the question, it
    records nothing.

    This is a real operational prerequisite and not a technicality: the preflight publishes the
    season, so an operator who has run the preflight has one.
    """
    exit_code, client = _settle_run(
        settle_operator, monkeypatch, tmp_path, trial=_trial(), chain_index=CHAIN_INDEX, bar="1m", publish=False
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert client.calls == []
    assert "no season is published" in captured.err
    assert ReceiptStore(tmp_path).outcome(TRIAL_ID) is None


def test_a_trial_sealed_on_ANOTHER_CHAIN_is_skipped_and_the_run_continues(
    settle_operator: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The PER-TRIAL gate, which the run-level one cannot cover.

    Here the operator's flags match the published season exactly — the run-level gate passes — and
    the TRIAL is the thing that belongs to another chain. Fetching it would return a different
    token's market for a trial that names this one.

    The run continues and exits 0, which is deliberate and is the same claim ``coverage_gap``
    makes: exit status describes the RUN, and a run that applied its gates and declined to write
    did its job. The refusal is reported in ``trials[].status`` where a driver can read it, and it
    names BOTH chains so an operator can tell a wrong flag from an off-season trial.
    """
    off_season = _trial(trial_id="trial_offseason", chain_index=OTHER_CHAIN_INDEX)
    exit_code, client = _settle_run(
        settle_operator, monkeypatch, tmp_path, trial=off_season, chain_index=CHAIN_INDEX, bar="1m"
    )
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert client.calls == [], "an off-season trial must not be fetched at all"
    summary = json.loads(captured.out)
    (row,) = summary["trials"]
    assert row["status"] == "chain_mismatch" and row["recorded"] is False
    assert row["sealed_chain_index"] == OTHER_CHAIN_INDEX and row["run_chain_index"] == CHAIN_INDEX
    assert ReceiptStore(tmp_path).outcome("trial_offseason") is None


def test_a_matching_run_FETCHES_THE_SEALED_CHAIN_and_persists_its_source(
    settle_operator: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ACCEPTANCE CONTROL, and the positive half of §7's persisted provenance.

    Every test above asserts a refusal, and a settler that refused everything would satisfy all of
    them. This is the row that makes them discriminating: the same harness, a matching selection,
    and a settlement that is actually recorded.

    It then asserts what was WRITTEN, which is the part the verifier depends on. The recorded chain
    is the sealed one and the recorded endpoint is the candles path — so ``outcome_source`` has
    something real to compare against rather than a field that was never populated.
    """
    exit_code, client = _settle_run(
        settle_operator, monkeypatch, tmp_path, trial=_trial(), chain_index=CHAIN_INDEX, bar="1m"
    )
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert client.calls == [(CHAIN_INDEX, _trial().sig.token_address, "1m")]
    summary = json.loads(captured.out)
    assert summary["season_selection"] == {"chain_index": CHAIN_INDEX, "bar": "1m"}
    (row,) = summary["trials"]
    assert row["status"] == "settled" and row["recorded"] is True

    stored = ReceiptStore(tmp_path).outcome_payload(TRIAL_ID)
    assert stored is not None
    assert stored["chain_index"] == CHAIN_INDEX
    assert stored["source_endpoint"].endswith(HISTORICAL_CANDLES_PATH)


def test_the_persisted_chain_is_what_was_FETCHED_and_not_a_copy_of_the_evidence(
    settle_operator: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property that makes ``outcome_source``'s chain comparison able to catch anything.

    If the settler recorded ``trial.sig.chain_index`` regardless of what it fetched, the recorded
    chain would agree with the sealed evidence BY CONSTRUCTION and the verifier's comparison would
    pass for every settlement, including one pulled from the wrong chain. So this drives the real
    caller and asserts the recorded value equals the value the client was CALLED with — two
    independently observed facts, not one value compared to itself.
    """
    _, client = _settle_run(settle_operator, monkeypatch, tmp_path, trial=_trial(), chain_index=CHAIN_INDEX, bar="1m")
    fetched_chain = client.calls[0][0]
    stored = ReceiptStore(tmp_path).outcome_payload(TRIAL_ID)
    assert stored is not None and stored["chain_index"] == fetched_chain


def test_settle_trial_records_the_chain_it_was_TOLD_and_never_the_evidence_s(store: _TamperableStore) -> None:
    """The single property that makes ``outcome_source``'s chain comparison capable of catching anything.

    ``settle_trial`` has the trial in hand, so recording ``trial.sig.chain_index`` would be the
    obvious shortcut — and it would make the recorded chain agree with the sealed evidence BY
    CONSTRUCTION, for every settlement, including one fetched from the wrong chain. The verifier's
    comparison would then be a value against a copy of itself, which passes for anything.

    The caller's tests cannot see this, and a mutation run proved it: the operator script refuses a
    mismatch before it fetches, so at ITS call site the two values are always equal and swapping
    them changes nothing. This asserts the contract where it lives, by passing a chain the trial
    does NOT name and requiring the parameter to win.
    """
    trial = _trial()
    assert trial.sig.chain_index == CHAIN_INDEX, "the fixture must not already carry the chain under test"
    settled = settle_trial(
        trial,
        _series(),
        now_ms=T + FROZEN_HORIZON_MS,
        chain_index=OTHER_CHAIN_INDEX,
        source_endpoint=SOURCE_ENDPOINT,
    )
    assert settled.provenance.chain_index == OTHER_CHAIN_INDEX

    # AND the verifier reports it, which is the consequence that matters: a settlement recorded as
    # fetched from a chain the evidence does not name does not verify.
    receipt = _commit(store, trial, staging_id="s_wrongchain")
    store.record_outcome(TRIAL_ID, settled)
    assert _verdict(receipt.receipt_id, store) == _clean(outcome_source="fail")


def test_settle_trial_REFUSES_to_record_a_settlement_that_states_no_source() -> None:
    """The recording contract, pinned at the function rather than only through its caller.

    ``chain_index`` and ``source_endpoint`` are required keywords for the same reason ``series``
    is: a settlement whose source is unknown cannot be verified and must not be written. A default
    would let a caller record an unexamined claim about its own fetch by saying nothing, and the
    only test that would notice is one that reads the recorded value — which is a test about the
    caller, not about the contract.
    """
    with pytest.raises(TypeError, match="chain_index"):
        settle_trial(_trial(), _series(), now_ms=T + FROZEN_HORIZON_MS)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="source_endpoint"):
        settle_trial(_trial(), _series(), now_ms=T + FROZEN_HORIZON_MS, chain_index=CHAIN_INDEX)  # type: ignore[call-arg]


def test_a_run_at_a_NON_DECLARED_COST_is_refused_before_it_can_record(
    settle_operator: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The writer half of N1's binding, and the reason the flag is not now a trap.

    ``outcome_source`` binds a recorded ``cost_bps`` to §8.2's declared 25. Without a matching
    refusal at the writer, ``--cost-bps 50`` would record a TERMINAL outcome that can never verify
    — a permanently unverifiable artifact produced by a documented flag, which is a worse failure
    than the one the binding fixes.
    """
    exit_code, client = _settle_run(
        settle_operator,
        monkeypatch,
        tmp_path,
        trial=_trial(),
        chain_index=CHAIN_INDEX,
        bar="1m",
        extra=["--cost-bps", "50"],
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert client.calls == [], "the run reached the network before checking a cost it could not record"
    assert "--cost-bps 50 is not the declared cost 25" in captured.err
    assert ReceiptStore(tmp_path).outcome(TRIAL_ID) is None


def test_the_diagnostic_SWEEP_still_runs_under_dry_run(
    settle_operator: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """DISCRIMINATION for the refusal above: the flag is refused for a WRITE, not for a display.

    ``[0, 10, 25, 50]`` is a diagnostic sweep the constant explicitly preserves, and a gate that
    refused every non-declared cost outright would delete it. ``--dry-run`` computes and prints and
    writes nothing, so it is the state where a non-declared cost is harmless — and the assertion
    that nothing was recorded is what makes that claim rather than assumes it.
    """
    exit_code, client = _settle_run(
        settle_operator,
        monkeypatch,
        tmp_path,
        trial=_trial(),
        chain_index=CHAIN_INDEX,
        bar="1m",
        extra=["--cost-bps", "50", "--dry-run"],
    )
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert client.calls == [(CHAIN_INDEX, _trial().sig.token_address, "1m")], "the sweep must still fetch"
    summary = json.loads(captured.out)
    (row,) = summary["trials"]
    assert row["status"] == "settled" and row["recorded"] is False
    assert ReceiptStore(tmp_path).outcome(TRIAL_ID) is None, "a dry run wrote a terminal outcome"


def test_a_run_at_the_DECLARED_cost_records_and_the_row_verifies(
    settle_operator: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ACCEPTANCE CONTROL: the settler and the verifier agree on the declared cost end to end.

    The two constants are pinned to each other by a unit test, but that pin is a statement about
    two literals. This is the statement about the SYSTEM: a row the real operator wrote is a row
    the real verifier accepts, so the binding cannot be satisfied by a writer and a reader that
    happen to disagree with the same number.
    """
    store = _TamperableStore(tmp_path)
    receipt = _commit(store, _trial(), staging_id="s_declared")
    exit_code, _ = _settle_run(
        settle_operator,
        monkeypatch,
        tmp_path,
        trial=_trial(),
        chain_index=CHAIN_INDEX,
        bar="1m",
        extra=["--cost-bps", str(DECLARED_COST_BPS)],
    )
    assert exit_code == 0, capsys.readouterr().err

    stored = store.outcome_payload(TRIAL_ID)
    assert stored is not None and stored["cost_bps"] == DECLARED_COST_BPS
    # The whole point: a row the REAL operator wrote passes the REAL verifier's cost binding.
    assert _verdict(receipt.receipt_id, store) == _clean()


# ==================================================================================================
# H4.4 — the participant JOIN route: `GET /signal-trials/trials/{trial_id}/receipts` (PKT-DEC-C65).
#
# The gap C65 adjudicated: no frozen model and no route joined a trial to the agents who committed
# against it, so nothing could reach a receipt id without already holding one. The join is served as
# an array of the ALREADY-FROZEN `CommitReceiptResponse` — no ninth model, and still no participants
# array on `TrialResponse`, whose separation from participant data is deliberate and stays.
#
# The route is a free read, and its correctness boundary is security-relevant in two directions.
# Both are pinned below. Reading the wrong source method publishes commitments nobody paid for:
# `ReceiptStore._iter_slots()` sees staged, in-flight, settle-attempted and quarantined rows, and it
# is the read C65 rejected. Silently dropping an unreadable row publishes a participant set smaller
# and cleaner than the artifacts support, which is the route-family rule H4.3 established.
# ==================================================================================================


def _receipts_path(trial_id: str) -> str:
    """The participant-join path for ``trial_id``."""
    return f"/signal-trials/trials/{trial_id}/receipts"


async def test_the_receipts_route_serves_both_finalized_commitments_in_receipt_id_ORDER(
    store: _TamperableStore,
) -> None:
    """AC1. Two paid commitments on one trial, as two whole frozen receipts, deterministically ordered.

    The staging ids are chosen so the fixture DISCRIMINATES rather than agreeing by construction:
    ``s_one`` is written FIRST and by :data:`PAYER`, and its receipt id sorts SECOND. Receipt-id
    order is therefore the reverse of both the write order and the payer order here, so a route that
    served either of those instead would fail this assertion. The guard on the two ids says so out
    loud, because a future change to :func:`~veridex.signal_trials.receipts.receipt_id_for` could
    quietly collapse the three orders into one and leave this test passing for no reason.

    The two probabilities differ for the same reason: this is a PER-PARTICIPANT join, and a route
    that rendered one receipt twice would satisfy an assertion on the ids alone.
    """
    trial = _trial()
    first = _commit(store, trial, staging_id="s_one", p=0.8)
    second = _commit(store, trial, staging_id="s_two", p=0.3, payer=OTHER_PAYER)
    assert first.receipt_id > second.receipt_id, "the fixture no longer discriminates receipt-id order"

    async with _client(_app(store=store, live_trials=_OneTrialRepo(trial))) as client:
        response = await client.get(_receipts_path(TRIAL_ID))

    assert response.status_code == 200
    body = response.json()
    assert [row["receipt_id"] for row in body] == [second.receipt_id, first.receipt_id]
    assert [row["payer"] for row in body] == [OTHER_PAYER, PAYER]
    assert [row["p_follow_profitable"] for row in body] == [0.3, 0.8]
    # Every element is the WHOLE frozen model. A trimmed projection would leave H5.3 unable to reach
    # `GET /receipts/{id}/verify`, which is the entire reason the array element is this model.
    assert all(set(row) == set(CommitReceiptResponse.model_fields) for row in body)


async def test_a_known_trial_with_no_finalized_commitments_is_200_AND_AN_EMPTY_ARRAY(
    store: _TamperableStore,
) -> None:
    """AC2. "Nobody committed" is a successful empty answer — the store is mounted and readable.

    Paired with the absent-store test below, which must NOT answer this way. ``[]`` asserts that
    nobody committed, and a deployment that cannot read its participant store does not know that.
    """
    async with _client(_app(store=store, live_trials=_OneTrialRepo(_trial()))) as client:
        response = await client.get(_receipts_path(TRIAL_ID))

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize(
    ("requested", "marker"),
    [
        pytest.param("trial_h43_never_opened", "never_opened", id="unknown"),
        # DOUBLE-encoded on purpose. A single `%2F` survives on the wire — `raw_path` keeps it — and
        # is decoded SERVER-SIDE when ASGI builds `scope["path"]`, which is what Starlette routes on.
        # So `..%2F..%2Fsecrets` becomes extra path segments during normalization and never reaches
        # this route at all — see the companion test below. `%252F` keeps the traversal attempt inside
        # ONE segment, which is the only form that actually exercises this handler's refusal.
        pytest.param("..%252F..%252Fsecrets", "secrets", id="traversal"),
        # Slash-free for the same reason: the closing `</script>` carried a `%2F`.
        pytest.param("%3Cscript%3Ealert(1)", "script", id="markup"),
    ],
)
async def test_an_unknown_or_malformed_trial_id_is_the_existing_NON_ECHOING_404(
    tmp_path: Path, requested: str, marker: str
) -> None:
    """AC3. One refusal code for both, and the caller's own text is never reflected back at them.

    The id arrives from a URL path segment, so echoing it would put caller-controlled bytes into the
    response and the access log. The body is asserted by EQUALITY, which is the strongest form of
    that claim — nothing caller-controlled can be present in a body that is exactly two known
    tokens — and the marker assertion states it again against the raw text, so a route that echoed
    into a second field or a header-shaped envelope fails here rather than passing on the JSON.

    Driven through the REAL :class:`~veridex.signal_trials.live.LiveTrialRepository` rather than the
    test double for REALISM ONLY. **This test does not exercise the traversal guard, and an earlier
    version of this docstring claimed it did.** Measured: ``_trial_path`` raises only for an empty id
    or one containing a separator or equal to ``.``/``..``, and after a single decode none of these
    three payloads contains a separator — so all three are refused by ordinary FILE ABSENCE, exactly
    as the double would have refused them.

    It is stronger than that, and worth stating so nobody re-adds the claim: through this route the
    guard is UNOBSERVABLE. ``repo.get()`` catches the guard's ``ValueError`` and answers ``None``,
    which is the same answer it gives for a merely absent trial, so both arrive here as one 404. A
    test asserting through this route cannot distinguish guard-refusal from absence at all. Covering
    the guard needs a unit test against ``_trial_path``, not a request.

    The published trial in the same client is the ACCEPTANCE CONTROL — it proves this route can
    answer 200 at all, so the 404s are a statement about the ids and not about a route that is
    permanently absent or permanently refusing.
    """
    trial = _trial()
    repo = LiveTrialRepository(tmp_path / "live")
    repo.publish(trial)

    async with _client(_app(store=_TamperableStore(tmp_path / "store"), live_trials=repo)) as client:
        served = await client.get(_receipts_path(TRIAL_ID))
        response = await client.get(_receipts_path(requested))

    assert served.status_code == 200, "the acceptance control failed: this route never answers 200"
    assert response.status_code == 404
    assert response.json() == {"error": "trial_not_found"}
    assert marker not in response.text


async def test_a_SLASH_BEARING_id_never_reaches_this_route_and_still_does_not_echo(tmp_path: Path) -> None:
    """AC3, the boundary the parametrized test above CANNOT reach, recorded rather than hidden.

    A single ``%2F`` is NOT decoded by the client — ``raw_path`` carries it intact onto the wire. It
    is decoded SERVER-SIDE, when ASGI constructs ``scope["path"]``, and Starlette routes on that
    normalized value: ``..%2F..%2Fsecrets`` becomes additional path segments and matches no route at
    all. The refusal therefore comes from the FRAMEWORK, not from this handler, and its body is a
    different shape: ``{"detail": "Not Found"}`` rather than ``{"error": "trial_not_found"}``.

    The attribution matters and an earlier version of this docstring got it wrong, blaming the client.
    Saying "the client did it" implies no client could ever reach this boundary, when the real
    mechanism is server-side path normalization — which is security-relevant precisely because
    proxies and servers differ in whether they decode ``%2F`` before routing. **Here the deciding
    party is the ASGI server**: uvicorn unquotes ``raw_path`` into ``scope["path"]``
    (``h11_impl.py:202``, ``httptools_impl.py:260``) and Starlette matches on that and never on
    ``raw_path``, so a slash-bearing id cannot reach this handler however the deployment is fronted —
    only a RE-ENCODING front, or an ASGI layer that routes on ``raw_path``, would change that.

    A previous version of this paragraph said the opposite: that a front which does NOT normalize
    would deliver the id to the handler. That is inverted. A non-normalizing proxy forwards ``%2F``
    intact, uvicorn then unquotes it into extra path segments, and no route matches — which is exactly
    why the ONE form that does reach the handler is the double-encoded ``%252F`` used above.

    That is worth pinning for two reasons. The SECURITY property still holds — the framework's 404
    does not echo the caller's bytes either, which is the claim AC3 actually makes — but a frontend
    branching on an ``error`` key would not recognise this body, so the difference is a real contract
    fact rather than a curiosity. And the original version of the parametrized test asserted this
    route's body against these payloads, which could never have passed: it was measuring the client's
    URL handling and reporting it as a defect in the route.
    """
    trial = _trial()
    repo = LiveTrialRepository(tmp_path / "live")
    repo.publish(trial)

    async with _client(_app(store=_TamperableStore(tmp_path / "store"), live_trials=repo)) as client:
        served = await client.get(_receipts_path(TRIAL_ID))
        framework = await client.get(_receipts_path("..%2F..%2Fsecrets"))

    assert served.status_code == 200, "the acceptance control failed: this route never answers 200"
    assert framework.status_code == 404
    assert framework.json() == {"detail": "Not Found"}, "the framework's 404 shape changed"
    assert "secrets" not in framework.text, "the framework echoed caller-controlled bytes"


async def test_no_STAGED_IN_FLIGHT_SETTLE_ATTEMPTED_or_QUARANTINED_row_is_ever_served(
    store: _TamperableStore,
) -> None:
    """AC4. The finalized-only gate, measured against a store that actually holds all four states.

    This is the test that would have caught the read C65 rejected. Every non-finalized row here is
    CONSTRUCTED, and the slot iterator is asserted to see all of the ones that take a slot, so the
    exclusions are proven rather than inherited from a store that had nothing to exclude. A route
    built on ``_iter_slots()`` would serve four participants where one was paid for.

    Order matters in the fixture and is not incidental. :meth:`ReceiptStore.reconcile` is what turns
    an attempted slot into a quarantined one, and the same call would RELEASE a stale in-flight slot
    and SWEEP a stale staged row — so those two are created after it, or the controls they provide
    would be deleted before the assertion ran.
    """
    trial = _trial()
    # Quarantined: a settle was attempted and no journal entry proves how it ended. Never served,
    # and never deleted, because the facilitator may be holding a real payment.
    store.stage(staging_id="s_quarantined", trial_id=TRIAL_ID, payer="0xq", body=_req(0.5), staged_at_ms=T0)
    store.mark_settle_attempted("s_quarantined")
    store.reconcile(now_ms=10**15)
    # Staged: received, never paid for.
    store.stage(staging_id="s_staged", trial_id=TRIAL_ID, payer="0xs", body=_req(0.6), staged_at_ms=T0)
    # In flight: the slot is taken and nothing has been written behind it.
    store.acquire_slot("0xi", TRIAL_ID, now_ms=T0)
    # Settle-attempted: past the durable marker, outcome not yet known.
    store.stage(staging_id="s_attempted", trial_id=TRIAL_ID, payer="0xa", body=_req(0.4), staged_at_ms=T0)
    store.mark_settle_attempted("s_attempted")
    paid = _commit(store, trial, staging_id="s_paid", p=0.8)

    # The controls exist. Without these four assertions the test below could pass against a store
    # holding nothing but the paid row.
    assert store.count_quarantined() == 1
    assert store.count_settle_attempted() == 1
    assert store.slot_state("0xi", TRIAL_ID) == "in_flight"
    assert store.count_pending() == 3
    # DISCRIMINATION: the private read path C65 rejected sees FOUR slot rows where the public gate
    # serves one, so "only one is served" is a property of the SOURCE METHOD and not of a store with
    # nothing else in it. Four and not five: the STAGED row never takes a slot, so `_iter_slots()`
    # cannot see it — which is why `count_pending()` above is the control that proves it exists.
    # Sorted order, and PAYER is "0xb", so the finalized row lands SECOND. (The first version of this
    # list was written in authoring order with the finalized row last and compared against sorted(),
    # so it failed for its own ordering rather than for anything about the route.)
    assert sorted((row["payer"], row["state"]) for row in store._iter_slots()) == [
        ("0xa", "settle_attempted"),
        (PAYER, "finalized"),
        ("0xi", "in_flight"),
        ("0xq", "quarantined"),
    ]

    async with _client(_app(store=store, live_trials=_OneTrialRepo(trial))) as client:
        response = await client.get(_receipts_path(TRIAL_ID))

    assert response.status_code == 200
    assert [row["receipt_id"] for row in response.json()] == [paid.receipt_id]
    assert [row["payer"] for row in response.json()] == [PAYER]


async def test_a_finalized_commitment_on_ANOTHER_trial_is_never_served(store: _TamperableStore) -> None:
    """AC5. The join filters by trial, and each trial's request returns only its own participant.

    Both receipts are served SOMEWHERE, each under its own trial, which is what makes this a filter
    test rather than a test that one of the two rows is unreadable: a route that returned an empty
    array for everything would satisfy "the other trial's receipt is absent" perfectly.
    """
    trial = _trial()
    other = _trial(trial_id=OTHER_TRIAL_ID)
    mine = _commit(store, trial, staging_id="s_mine", p=0.8)
    theirs = _commit(store, other, staging_id="s_theirs", p=0.3, payer=OTHER_PAYER)

    async with _client(_app(store=store, live_trials=_OneTrialRepo(trial))) as client:
        response = await client.get(_receipts_path(TRIAL_ID))
    async with _client(_app(store=store, live_trials=_OneTrialRepo(other))) as client:
        other_response = await client.get(_receipts_path(OTHER_TRIAL_ID))

    assert [row["receipt_id"] for row in response.json()] == [mine.receipt_id]
    assert [row["receipt_id"] for row in other_response.json()] == [theirs.receipt_id]


class _CanonicalizingRepo:
    """A repository that RESOLVES an id rather than requiring the caller's exact spelling.

    Not an exotic double. It is what :class:`~veridex.signal_trials.live.LiveTrialRepository` already
    does on a case-insensitive filesystem, which is macOS APFS by default and every Windows volume:
    ``get("TRIAL_H43")`` opens ``trials/trial_h43.json`` and returns a trial whose ``trial_id`` is the
    document's ``trial_h43``. MEASURED against the real repository on this host — the request pair
    ``/trials/TRIAL_H43`` and ``/trials/TRIAL_H43/receipts`` answered ``200`` with the resolved trial
    and ``200 []`` respectively, while the same pair under ``trial_h43`` served the participant.

    The double is used instead of that filesystem so the property is pinned identically on every
    platform: a test that depended on the host's case-folding would pass on Linux CI and fail on a
    developer's macOS, or the reverse, which is worse than no test.
    """

    def __init__(self, trial: LiveTrial) -> None:
        self._trial = trial

    def current(self) -> LiveTrial | None:
        return self._trial

    def get(self, trial_id: str) -> LiveTrial | None:
        return self._trial if trial_id.lower() == self._trial.trial_id.lower() else None


async def test_a_RESOLVED_trial_id_serves_its_participants_and_never_an_empty_set(
    store: _TamperableStore,
) -> None:
    """The join must filter by the id the repository RESOLVED, never by the caller's raw spelling.

    Reading the raw path segment answers ``200 []`` here — the one answer AC2 and AC7 exist to
    forbid, because an empty array is the positive claim that nobody committed to this trial, and a
    trial with a paid participant makes that claim false. It arrives with no corruption and no
    tampering: one request for a case-variant of a real id, on a filesystem that resolves it.

    The two spellings in one test are the DISCRIMINATION CONTROL. Filtering by ``trial.trial_id``
    passes both; filtering by the raw segment passes the exact spelling and returns ``[]`` for the
    variant, so the pair separates a resolved read from an unresolved one where either alone cannot.

    The sibling ``/trials/{id}`` assertion is what makes this a CONSISTENCY claim rather than a
    preference. That route already reads ``trial.trial_id`` for its outcome, so before the fix the two
    routes disagreed about the same URL: one served the trial and its settled outcome, the other said
    nobody had committed to it. A frontend joining them would have rendered a settled trial with an
    empty participant list and had nothing on the wire to tell it that was a defect.
    """
    trial = _trial()
    committed = _commit(store, trial, staging_id="s_resolved", p=0.8)
    store.record_outcome(TRIAL_ID, _settled_outcome_from(_series()))
    variant = TRIAL_ID.upper()
    assert variant != TRIAL_ID, "the fixture no longer varies the spelling"

    async with _client(_app(store=store, live_trials=_CanonicalizingRepo(trial))) as client:
        exact = await client.get(_receipts_path(TRIAL_ID))
        resolved = await client.get(_receipts_path(variant))
        sibling = await client.get(f"/signal-trials/trials/{variant}")

    assert exact.status_code == 200
    assert [row["receipt_id"] for row in exact.json()] == [committed.receipt_id]
    assert resolved.status_code == 200
    assert resolved.json() != [], "a case-variant of a known trial id claimed that nobody committed"
    assert [row["receipt_id"] for row in resolved.json()] == [committed.receipt_id]
    # Both routes resolved the SAME variant spelling to the same trial, so a consumer cannot receive a
    # settled outcome from one and an empty participant set from the other.
    assert sibling.status_code == 200
    assert sibling.json()["trial_id"] == TRIAL_ID
    assert sibling.json()["outcome"]["status"] == "settled"
    assert [row["trial_id"] for row in resolved.json()] == [TRIAL_ID]


@pytest.mark.parametrize(
    ("recorded", "status", "brier", "markout"),
    [
        pytest.param("none", "pending", None, None, id="no-outcome-recorded"),
        pytest.param("pending", "pending", None, None, id="recorded-pending"),
        pytest.param("unscored", "UNSCORED", None, None, id="recorded-UNSCORED"),
        pytest.param("settled", "settled", pytest.approx((0.8 - 1) ** 2), FOLLOW_BPS, id="settled"),
    ],
)
async def test_every_participant_STATE_renders_with_the_frozen_nullability(
    store: _TamperableStore, recorded: str, status: str, brier: Any, markout: int | None
) -> None:
    """AC6. ``pending``, ``settled`` and ``UNSCORED`` all render, and the metrics are null only where
    there is no result.

    The settled row is the DISCRIMINATION CONTROL for the other three: without it, a route that
    hard-coded both metrics to ``null`` would pass every case here. With it, the same route fails,
    because a settled commitment carries a real Brier and a real chosen leg.

    ``no-outcome-recorded`` and ``recorded-pending`` are two different backend states that this model
    deliberately COLLAPSES to one wire value, and both are exercised so the collapse is a pinned
    property rather than an accident of whichever one the tests happened to use.

    Every key is asserted present, so a metric is served as an explicit ``null`` rather than omitted:
    a consumer reading a missing key cannot tell "no result" from "a field I do not know about".
    """
    trial = _trial()
    _commit(store, trial, staging_id="s_state", p=0.8)
    if recorded == "pending":
        store.record_outcome(TRIAL_ID, _settle(trial, _empty_series(), now_ms=T0 + 60_000))
    elif recorded == "unscored":
        store.record_outcome(TRIAL_ID, _settle(trial, _empty_series(), now_ms=T + 60_000 + FETCH_GRACE_MS))
    elif recorded == "settled":
        store.record_outcome(TRIAL_ID, _settled_outcome_from(_series()))

    async with _client(_app(store=store, live_trials=_OneTrialRepo(trial))) as client:
        response = await client.get(_receipts_path(TRIAL_ID))

    assert response.status_code == 200
    (row,) = response.json()
    assert row["status"] == status
    assert row["brier"] == brier
    assert row["chosen_markout_bps"] == markout
    assert set(row) == set(CommitReceiptResponse.model_fields)
    # Derived from the payer's own probability at commit time, so it does not wait on the market and
    # is present in every state — including the two that carry no metrics at all.
    assert row["action"] == "FOLLOW"


def test_every_field_of_the_frozen_receipt_model_is_REQUIRED_with_no_default() -> None:
    """AC6, the schema half. **PIN, not a RED** (C46): this passed the instant it was written.

    Recorded as a pin because it constrains a model this task must not touch. A nullable field that
    acquired a default would let a renderer omit it and have pydantic fill the null back in, which is
    exactly how "no result" and "this route did not serve the field" become indistinguishable on the
    wire — and the array element is now consumed by a second route, so there are two renderers that
    could drift into relying on it.
    """
    assert [name for name, field in CommitReceiptResponse.model_fields.items() if not field.is_required()] == []


async def test_an_ABSENT_store_refuses_rather_than_claiming_that_nobody_committed(tmp_path: Path) -> None:
    """AC7. No participant store is an honest refusal, and specifically NOT ``200 []``.

    ``[]`` is a positive claim — nobody committed to this trial — and a deployment with no store
    mounted has no basis for it. The trial itself is known here, so 404 would be a second false
    claim; 503 with its own code names the missing precondition, which is how the commit route
    already distinguishes "no trials" from "no payment gate".

    The mounted-but-empty request is the DISCRIMINATION CONTROL: the same trial, the same route, and
    the answer differs. Without it, a route that always refused would pass.
    """
    trial = _trial()
    async with _client(_app(store=None, live_trials=_OneTrialRepo(trial))) as client:
        absent = await client.get(_receipts_path(TRIAL_ID))
    async with _client(_app(store=_TamperableStore(tmp_path), live_trials=_OneTrialRepo(trial))) as client:
        mounted = await client.get(_receipts_path(TRIAL_ID))

    assert absent.status_code == 503
    assert absent.json() == {"error": "participant_store_unavailable"}
    assert mounted.status_code == 200
    assert mounted.json() == []

    # The CHECK ORDER is load-bearing and was unpinned until the SPEC reviewer asked for it. An
    # UNKNOWN trial on a store-less deployment must still answer 404, because "this trial does not
    # exist" is the more specific true statement and 503 would blame the wrong precondition. Hoisting
    # the store check above the trial check would silently flip every such request from 404 to 503,
    # and nothing else here would notice: the assertions above only ask about a KNOWN trial, and the
    # AC3 tests always mount a store.
    async with _client(_app(store=None, live_trials=_OneTrialRepo(trial))) as client:
        unknown_and_storeless = await client.get(_receipts_path("trial_that_was_never_opened"))
    assert unknown_and_storeless.status_code == 404
    assert unknown_and_storeless.json() == {"error": "trial_not_found"}


async def test_a_CORRUPT_finalized_row_FAILS_the_request_instead_of_shrinking_the_participant_set(
    store: _TamperableStore,
) -> None:
    """AC8, the receipt half. A row nothing can read must not be quietly dropped from the array.

    Serving the survivor alone would publish a complete-looking participant set that is missing a
    paid commitment — a smaller, cleaner record than the artifacts support, and indistinguishable to
    the caller from a trial that only ever had one participant. This route has no verdict to publish
    about the damage; that is the verify route's job. So it refuses the whole answer.

    The intact request first is the ACCEPTANCE CONTROL: two receipts are genuinely servable here, so
    the 500 is caused by the corruption and not by a route that fails on two rows.
    """
    trial = _trial()
    intact = _commit(store, trial, staging_id="s_intact", p=0.8)
    broken = _commit(store, trial, staging_id="s_broken", p=0.3, payer=OTHER_PAYER)

    async with _client(_app(store=store, live_trials=_OneTrialRepo(trial)), raise_app_exceptions=False) as client:
        before = await client.get(_receipts_path(TRIAL_ID))
        (Path(store.root) / _FINALIZED_DIRNAME / f"{broken.receipt_id}.json").write_text("{not json", encoding="utf-8")
        after = await client.get(_receipts_path(TRIAL_ID))

    assert before.status_code == 200 and len(before.json()) == 2
    assert after.status_code == 500
    assert intact.receipt_id not in after.text, "the surviving receipt was served as if it were the whole set"


async def test_a_CORRUPT_outcome_row_FAILS_the_request_and_is_not_served_as_pending(
    store: _TamperableStore,
) -> None:
    """AC8, the outcome half — and the sharper of the two, because a plausible wrong answer exists.

    A destroyed outcome could be rendered as ``pending`` with null metrics, which is well-formed,
    reasonable-looking and false: ``pending`` says the trial has not settled, when in fact it settled
    and the record was destroyed. The trial route already refuses that trade and this one matches it.

    The settled request first is the ACCEPTANCE CONTROL, and it is what makes ``pending`` the wrong
    answer rather than an untested one: the same row rendered ``settled`` moments earlier.
    """
    trial = _trial()
    _commit(store, trial, staging_id="s_outcome", p=0.8)
    store.record_outcome(TRIAL_ID, _settled_outcome_from(_series()))

    async with _client(_app(store=store, live_trials=_OneTrialRepo(trial)), raise_app_exceptions=False) as client:
        before = await client.get(_receipts_path(TRIAL_ID))
        store.corrupt_outcome(TRIAL_ID, raw="{not json")
        after = await client.get(_receipts_path(TRIAL_ID))

    assert before.status_code == 200 and before.json()[0]["status"] == "settled"
    assert after.status_code == 500
    assert "pending" not in after.text, "a destroyed settlement was rendered as an unsettled one"
