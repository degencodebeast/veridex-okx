#!/usr/bin/env python3
"""Settle every live trial whose horizon has passed, and recompute the records that follow.

Run by an operator, repeatedly, against the SAME data directory the API serves:

    python scripts/signal_trials/settle_live_trials.py --data-dir ./data --chain-index 196 --bar 1m

Four steps per trial, in this order, because each one is only meaningful once the previous
succeeded: fetch the settlement candles, apply §7's close-boundary law, record the outcome, then
recompute and record the participant settlements for every FINALIZED commit on that trial. The
agent records are recomputed last and are reported rather than stored — they are a pure function
of the commits and the outcomes, so persisting them would create a fourth artifact that could
disagree with the three that produced it.

**The one thing this script must never do is manufacture an UNSCORED.** §12 reserves UNSCORED for
a genuine absence — no completed settlement candle exists. A fetch that returned the WRONG WINDOW
is not that: it is a fetch that did not answer the question. The two are indistinguishable
downstream, because both arrive as "no eligible candle in this series", and an UNSCORED recorded
from a bad window is a permanent false statement about a trial that settled perfectly well —
recorded terminally, published to the agents who paid to commit, and never revisited.

So :func:`series_covers_settlement` gates the refusal: a trial is only allowed to become UNSCORED
when the fetched series demonstrably SPANS the settlement window. If it does not, the run reports a
coverage gap and records nothing, leaving the trial to a later invocation. A settled trial needs no
such gate — the eligible candle is its own proof that the window was covered.

**The second thing it must never do is settle a trial against a source the trial did not name.**
``--chain-index`` and ``--bar`` are operator claims, and an outcome settled from the wrong chain or
the wrong bar is not a wrong-looking artifact: the candles are real, the law applies cleanly, the
bar label and width form a legal pair, and every Fair-Play check passes over a settlement of a
different market. Two gates close that, and they are checked against two DIFFERENT authorities so
neither can be satisfied by the run's own claims:

* :func:`selected_combo` reads the PUBLISHED SEASON, which is the only artifact recording which
  ``(chain x bar)`` the matrix picked, and :func:`assert_run_matches_selection` aborts before any
  request if this run disagrees. With no season published there is no authority, and the run
  refuses rather than proceeding unchecked.
* Per trial, the chain is DERIVED from the trial's sealed evidence and the operator's value is only
  an assertion checked against it — see :func:`_settle_all`.

What was fetched is then PERSISTED with the settlement (§7) as ``chain_index`` and
``source_endpoint``, so ``outcome_source`` can re-derive the chain against the sealed evidence long
after this process exits. Recording what the fetch used rather than what the trial says is what
makes that check able to catch a wrong-chain settlement at all.

**Credentials.** The three OKX variables are read from the environment, held only inside
:class:`~veridex.signal_trials.okx_client.OKXCredentials` (which redacts its own ``repr``), and
every failure message is passed through :func:`redact` before it is printed. Nothing here logs, and
no credential value is ever rendered — a traceback from httpx can carry a signed URL, which is why
the redaction wraps the whole run rather than individual calls.

The transport and the credential reader are deliberate LOCAL COPIES of the ones in
``run_preflight.py`` rather than an import from it. ``scripts/signal_trials`` is not a package and
both scripts are documented as being run BY PATH, so ``python scripts/signal_trials/…`` puts the
script's own directory on ``sys.path`` and a cross-script import would fail exactly in the
invocation the docstrings tell an operator to use. The copies are small and self-contained; they
are noted here so a change to the credential contract is known to have two sites.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from veridex.signal_trials.live import (
    DECLARED_COST_BPS,
    LiveTrial,
    LiveTrialRepository,
    build_agent_record,
    settle_commit,
    settle_trial,
)
from veridex.signal_trials.okx_client import (
    BAR_MS,
    HISTORICAL_CANDLES_PATH,
    CandleSeries,
    OKXCredentials,
    OKXMarketClient,
)
from veridex.signal_trials.preflight import FROZEN_HORIZON_MS
from veridex.signal_trials.published import read_season
from veridex.signal_trials.receipts import TERMINAL_STATUSES, ReceiptStore

#: Where the live-trial repository lives under the data dir. Must match ``open_live_trial.py`` and
#: ``veridex/api/server.py`` — the store is the data dir itself, the trials sit one level down —
#: or this script settles into a directory nothing serves.
LIVE_SUBDIR = "live"

DEFAULT_BASE_URL = "https://web3.okx.com"
REQUEST_TIMEOUT_SECONDS = 30.0
_CREDENTIAL_VARS: tuple[str, ...] = ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")

#: How many candles to request per trial. The settlement window is ONE bar wide, so this is
#: entirely about reaching back far enough to contain it: 100 bars is 100 minutes at ``1m`` and
#: just over four days at ``1H``. Too small a window is not a silent hazard — it fails
#: :func:`series_covers_settlement` and is reported as a coverage gap rather than as UNSCORED.
DEFAULT_CANDLE_LIMIT = 100


class MissingCredentialError(RuntimeError):
    """A required OKX credential is absent from the environment."""


class SelectionError(RuntimeError):
    """The run's ``(chain x bar)`` is not the one the published season was built under.

    Its own type rather than a bare ``RuntimeError`` so ``main`` can abort on it BEFORE any request
    and distinguish it from a failure during the run, which is a different fact fixed differently.
    """


class HttpxTransport:
    """The concrete OKX HTTP seam, satisfying ``okx_client.Transport`` structurally.

    ``params`` is passed straight through as a dict so httpx serializes it in iteration order — the
    OKX signature covers the query string, so a transport that reordered it would produce a URL the
    ``OK-ACCESS-SIGN`` no longer matches.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None,
        json_body: object | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        response = await self._http.request(method, path, params=params, json=json_body, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"OKX response must be a JSON object, got {type(payload).__name__}")
        return payload


def credentials_from_env(env: Mapping[str, str]) -> OKXCredentials:
    """Read the OKX credentials, naming any that are missing or blank.

    Raises:
        MissingCredentialError: Listing the missing VARIABLE NAMES only, never a value.
    """
    missing = [name for name in _CREDENTIAL_VARS if not env.get(name, "").strip()]
    if missing:
        raise MissingCredentialError(f"missing or blank OKX credentials in the environment: {', '.join(missing)}")
    return OKXCredentials(
        api_key=env["OKX_API_KEY"],
        secret_key=env["OKX_SECRET_KEY"],
        passphrase=env["OKX_PASSPHRASE"],
        base_url=env.get("OKX_BASE_URL", DEFAULT_BASE_URL),
    )


def redact(text: str, secrets: Sequence[str]) -> str:
    """Replace every credential value in ``text``.

    Blank and whitespace-only entries are skipped: replacing the empty string would rewrite every
    character boundary in the message, which destroys the diagnostic while looking like redaction.
    """
    redacted = text
    for secret in secrets:
        if secret.strip():
            redacted = redacted.replace(secret, "***")
    return redacted


def series_covers_settlement(series: CandleSeries, *, t0_ms: int, horizon_ms: int) -> bool:
    """Return whether ``series`` demonstrably spans the settlement window for this trial.

    The window is ``[T, T + bar)`` with ``T = t0 + horizon``. A series covers it when it carries a
    confirmed candle closing at or before ``T`` AND one closing at or after ``T + bar``: the first
    proves the fetch reached back past the window, the second proves it reached forward past it. A
    series that satisfies both and STILL contains no eligible candle is evidence of a genuine gap —
    an illiquid token with no trades in that minute — which is what §12's UNSCORED describes.

    Without this, the commonest operational error produces the worst possible artifact. A window
    that simply did not reach back to ``T`` — the settler ran a day late, the limit was too small,
    the endpoint's retention did not go that far — arrives as an empty eligibility set, which is
    byte-for-byte what a real gap looks like. Recording UNSCORED from it publishes a permanent,
    terminal, false statement about a trial that settled fine.

    A boundary this predicate does NOT cover, stated rather than implied: it proves the fetch
    STRADDLED the window, not that the series is hole-free everywhere in between. That is the right
    scope — a hole exactly at the settlement bar IS the genuine-absence case UNSCORED exists for,
    and demanding a hole-free series would refuse to ever record the one state §12 requires.

    Args:
        series: The fetched candles with their bar provenance.
        t0_ms: The trial's open instant.
        horizon_ms: The settlement horizon.

    Returns:
        ``True`` when absence of an eligible candle can honestly be read as absence.
    """
    settlement_target_ms = t0_ms + horizon_ms
    closes = [candle.ts_open_ms + series.bar_ms for candle in series.candles if candle.confirmed]
    if not closes:
        return False
    return min(closes) <= settlement_target_ms and max(closes) >= settlement_target_ms + series.bar_ms


def selected_combo(data_dir: Path) -> dict[str, str]:
    """Return the ``(chain_index, bar)`` the published season was built under.

    §7 fixes ONE bar per season and the preflight's matrix picks ONE chain to go with it. The
    published season document is the only artifact in this data directory that records which pair
    won, which makes it the authority a settlement run has to agree with — and the whole reason
    this function exists is that ``--bar`` and ``--chain-index`` are otherwise pure operator claims
    that nothing checks. Settling a 1m season's trial on ``--bar 1H`` fetches hourly candles,
    applies the close-boundary law to a completely different candle, and produces a settled outcome
    whose bar label and width form a perfectly legal pair — so every Fair-Play check passes over a
    settlement the season never authorized.

    Raises:
        SelectionError: No season is published, or the published one names no usable combo. This is
            FAIL CLOSED and it is the point: with nothing naming the selected bar there is nothing
            to check ``--bar`` against, and a run that proceeded anyway would write a TERMINAL
            outcome — published to the agents who paid to commit, never revisited — under a bar no
            artifact authorizes. The same reasoning as :func:`series_covers_settlement`: when the
            fetch cannot be shown to have answered the question, record nothing. Publish the season
            (the preflight does it) and re-run.
        ValueError: The published artifacts are unreadable or contradict each other. Raised by
            :func:`~veridex.signal_trials.published.read_season` and deliberately not softened
            here — a corrupt season is not an absent one.
    """
    season = read_season(data_dir)
    if season is None:
        raise SelectionError(
            "no season is published in this data directory, so nothing names the selected "
            "(chain x bar); refusing to record a terminal outcome under an unauthorized bar"
        )
    combo = season.get("combo")
    chain_index = combo.get("chain_index") if isinstance(combo, dict) else None
    bar = combo.get("bar") if isinstance(combo, dict) else None
    if not isinstance(chain_index, str) or not isinstance(bar, str):
        raise SelectionError(
            "the published season carries no usable combo; refusing to settle against a selection it does not name"
        )
    return {"chain_index": chain_index, "bar": bar}


def assert_run_matches_selection(args: argparse.Namespace, selection: Mapping[str, str]) -> None:
    """Refuse a run whose ``(chain x bar)`` is not the published season's.

    Checked BEFORE the credentials are read and long before any request, so a mismatched
    invocation costs one file read and writes nothing.

    Raises:
        SelectionError: ``--chain-index`` or ``--bar`` disagrees with the published selection. Both
            are reported together rather than one at a time — an operator who got one wrong
            usually got both wrong, and two runs to learn two facts is two chances to settle
            something in between.
    """
    wrong = [
        f"--{name.replace('_', '-')} {getattr(args, name)!r} (season selected {selection[name]!r})"
        for name in ("chain_index", "bar")
        if getattr(args, name) != selection[name]
    ]
    if wrong:
        raise SelectionError("this run does not match the published season selection: " + "; ".join(wrong))


def assert_cost_is_recordable(args: argparse.Namespace) -> None:
    """Refuse to RECORD an outcome at any cost but §8.2's declared one.

    ``--cost-bps`` exists for the ``[0, 10, 25, 50]`` diagnostic sweep, which
    :data:`~veridex.signal_trials.live.DECLARED_COST_BPS` says is a display and never the recorded
    outcome. The verifier now holds that line — ``outcome_source`` binds the row's ``cost_bps`` to
    the declared value, because otherwise the row supplies both the input and its own authority
    and a forger can remove the whole cost from a published markout.

    That binding turns the flag into a trap unless the writer honours the same rule: a run at
    ``--cost-bps 50`` would happily record a TERMINAL outcome that can never verify. So the flag is
    still accepted for a ``--dry-run``, which computes and prints and writes nothing, and refused
    for a run that would write. Fail closed at the writer, exactly as the coverage gate does.

    Raises:
        SelectionError: A non-declared cost was supplied for a run that would record.
    """
    if args.cost_bps != DECLARED_COST_BPS and not args.dry_run:
        raise SelectionError(
            f"--cost-bps {args.cost_bps} is not the declared cost {DECLARED_COST_BPS} (section 8.2), and a "
            "recorded outcome may carry no other; re-run with --dry-run for the diagnostic sweep, or "
            "drop the flag to settle"
        )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Settle live signal trials past their horizon and recompute participant records.",
    )
    parser.add_argument(
        "--data-dir", required=True, help="Signal-trials data directory; the API must serve the same one."
    )
    parser.add_argument("--chain-index", required=True, help="The season's chain index, as selected by the preflight.")
    parser.add_argument(
        "--bar", required=True, choices=sorted(BAR_MS), help="The season's bar. One bar per season (§7)."
    )
    parser.add_argument("--trial-id", default=None, help="Settle only this trial. Omit to settle every eligible one.")
    parser.add_argument("--now-ms", type=int, default=None, help="Clock override in epoch ms. Omit for the real clock.")
    parser.add_argument("--limit", type=int, default=DEFAULT_CANDLE_LIMIT, help="Candles to request per trial.")
    parser.add_argument(
        "--cost-bps", type=int, default=DECLARED_COST_BPS, help="Modeled cost; §8.2 fixes the official 25."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would be recorded and write nothing.")
    return parser.parse_args(argv)


def _eligible(
    trials: Sequence[LiveTrial], store: ReceiptStore, *, now_ms: int, trial_id: str | None
) -> list[LiveTrial]:
    """The trials this run should fetch for: past their horizon, and not already terminal.

    Trials whose outcome is already ``settled`` or ``UNSCORED`` are skipped rather than re-fetched,
    because :meth:`ReceiptStore.record_outcome` would refuse the write anyway — skipping makes a
    repeat run cheap and, more importantly, makes it obviously a no-op rather than a sequence of
    refusals an operator has to read past.
    """
    selected = [t for t in trials if trial_id is None or t.trial_id == trial_id]
    eligible = []
    for trial in selected:
        if now_ms < trial.t0_ms + FROZEN_HORIZON_MS:
            continue
        recorded = store.outcome_payload(trial.trial_id)
        if recorded is not None and recorded.get("status") in TERMINAL_STATUSES:
            continue
        eligible.append(trial)
    return eligible


async def _settle_all(
    trials: Sequence[LiveTrial],
    store: ReceiptStore,
    creds: OKXCredentials,
    args: argparse.Namespace,
    *,
    now_ms: int,
) -> list[dict[str, Any]]:
    """Fetch, settle and record each eligible trial. The only network path in this script.

    **The chain is DERIVED from the trial, never taken from the command line**, and the operator's
    ``--chain-index`` is an assertion this loop checks rather than a value it obeys. A trial's
    sealed evidence names the chain its signal was observed on; fetching any other chain returns a
    different token's market, and the close-boundary law settles happily against it.

    The refusal and the derived fetch are two edits rather than one, and that is the honest
    statement of what the second buys. GIVEN the refusal, the two expressions are equal at this
    call site and swapping them changes nothing observable — a mutation run confirms that mutant
    survives. What it buys is that DELETING THE REFUSAL ALONE still cannot redirect the fetch: both
    lines have to change before a wrong chain reaches the network. Same shape as
    :func:`~veridex.signal_trials.live.settle_trial` re-selecting the candle instead of
    back-deriving it.

    A refused trial is skipped and the run continues. One off-season trial in the live directory —
    published under an earlier chain, say — must not stop the trials that ARE settleable from being
    settled, and its refusal is reported in its own summary row.
    """
    results: list[dict[str, Any]] = []
    source_endpoint = f"{creds.base_url}{HISTORICAL_CANDLES_PATH}"
    async with httpx.AsyncClient(base_url=creds.base_url, timeout=REQUEST_TIMEOUT_SECONDS) as http:
        client = OKXMarketClient(HttpxTransport(http), creds)
        for trial in trials:
            if trial.sig.chain_index != args.chain_index:
                results.append(_chain_mismatch(trial, args))
                continue
            series = await client.get_candles(
                trial.sig.chain_index, trial.sig.token_address, args.bar, limit=args.limit
            )
            results.append(_record_one(trial, series, store, args, now_ms=now_ms, source_endpoint=source_endpoint))
    return results


def _chain_mismatch(trial: LiveTrial, args: argparse.Namespace) -> dict[str, Any]:
    """The summary row for a trial whose sealed chain is not the one this run settles.

    Nothing was fetched and nothing was written, and the row says which two values disagreed so an
    operator can tell a wrong ``--chain-index`` from a trial that belongs to another season.
    """
    return {
        "trial_id": trial.trial_id,
        "status": "chain_mismatch",
        "sealed_chain_index": trial.sig.chain_index,
        "run_chain_index": args.chain_index,
        "candles_fetched": 0,
        "observation_lag_ms": None,
        "follow_markout_bps": None,
        "recorded": False,
        "settlements_recorded": 0,
    }


def _record_one(
    trial: LiveTrial,
    series: CandleSeries,
    store: ReceiptStore,
    args: argparse.Namespace,
    *,
    now_ms: int,
    source_endpoint: str,
) -> dict[str, Any]:
    """Apply the law to one fetched series, record what it produced, and summarize the result.

    The coverage gate sits between the law and the WRITE, not before the law: the law is what tells
    us whether a candle was found, and a found candle needs no coverage proof. Only the refusal
    path — the one that would publish a terminal UNSCORED — has to justify itself.

    ``chain_index`` recorded into the provenance is the trial's SEALED one, which is also the one
    :func:`_settle_all` fetched — the caller refuses any trial where those two differ, so recording
    either is recording what happened. It is spelled as the sealed value because that is the fact
    the verifier re-derives against.
    """
    settled = settle_trial(
        trial,
        series,
        now_ms=now_ms,
        chain_index=trial.sig.chain_index,
        source_endpoint=source_endpoint,
        cost_bps=args.cost_bps,
    )
    outcome = settled.outcome
    summary: dict[str, Any] = {
        "trial_id": trial.trial_id,
        "status": outcome.status,
        "candles_fetched": len(series.candles),
        "observation_lag_ms": outcome.observation_lag_ms,
        "follow_markout_bps": outcome.follow_markout_bps,
        "recorded": False,
        "settlements_recorded": 0,
    }
    if outcome.status == "UNSCORED" and not series_covers_settlement(
        series, t0_ms=trial.t0_ms, horizon_ms=FROZEN_HORIZON_MS
    ):
        # NOT recorded, and NOT reported as UNSCORED. The fetch did not answer the question, and a
        # terminal UNSCORED written from it could never be corrected.
        summary["status"] = "coverage_gap"
        return summary
    if args.dry_run:
        return summary

    store.record_outcome(trial.trial_id, settled)
    recorded = store.outcome(trial.trial_id)
    assert recorded is not None, "an outcome that was just recorded must read back"
    for commit in store.finalized():
        if commit.trial_id != trial.trial_id:
            continue
        store.record_settlement(settle_commit(commit, recorded))
        summary["settlements_recorded"] += 1
    summary["recorded"] = True
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """Settle the eligible trials and print a JSON summary. Returns a process exit status.

    Exit codes are distinguishable because they are fixed differently: ``2`` is a bad INVOCATION,
    caught before any request — a missing credential (an operator sets three variables) or a
    ``(chain x bar)`` that is not the published season's (an operator fixes the flags, or publishes
    the season) — and ``1`` is a failure during the run (the reason reaches stderr, redacted).
    Neither writes anything to stdout, so a shell driving this cannot mistake an ABORTED or a
    FAILED run for a settled season.

    The two PER-TRIAL refusals — a COVERAGE GAP and a CHAIN MISMATCH — deliberately exit ``0``,
    which is worth stating because the aborts do not. Exit status here is a claim about the RUN,
    and a run that applied its gates and correctly declined to write is a run that did its job; a
    later invocation settles what remains. The consequence is the part a driver has to know:
    ``&&`` cannot separate a fully-settled season from a partially-settled one, so a shell that
    must not publish a partial season reads ``trials[].status`` for ``coverage_gap`` and
    ``chain_mismatch`` rather than the exit code.
    """
    args = _parse_args(argv)
    now_ms = int(time.time() * 1000) if args.now_ms is None else args.now_ms
    data_dir = Path(args.data_dir)
    store = ReceiptStore(data_dir)
    repository = LiveTrialRepository(data_dir / LIVE_SUBDIR)

    # Before the credentials, because this needs no network and no secret: an invocation that does
    # not match the published season is wrong whether or not the environment is set up, and the
    # operator should learn that from one file read rather than after three variables are exported.
    try:
        selection = selected_combo(data_dir)
        assert_run_matches_selection(args, selection)
        assert_cost_is_recordable(args)
    except (SelectionError, ValueError) as error:
        print(f"aborted before any request: {error}", file=sys.stderr)
        return 2

    try:
        creds = credentials_from_env(os.environ)
    except MissingCredentialError as error:
        print(f"aborted before any request: {error}", file=sys.stderr)
        return 2

    secrets = [creds.api_key, creds.secret_key, creds.passphrase]
    try:
        eligible = _eligible(repository.all_trials(), store, now_ms=now_ms, trial_id=args.trial_id)
        results = asyncio.run(_settle_all(eligible, store, creds, args, now_ms=now_ms))
        payers = sorted({commit.payer for commit in store.finalized()})
        records = [build_agent_record(payer, store).model_dump() for payer in payers]
    except Exception as error:  # noqa: BLE001 - every failure must be reported redacted, not just known ones
        print(f"settlement FAILED: {redact(f'{type(error).__name__}: {error}', secrets)}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "now_ms": now_ms,
                "bar": args.bar,
                # The selection this run was CHECKED against, so the summary states its own
                # authority rather than leaving a reader to trust that one was consulted.
                "season_selection": dict(selection),
                "dry_run": args.dry_run,
                "eligible": len(eligible),
                "trials": results,
                "agent_records": records,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
