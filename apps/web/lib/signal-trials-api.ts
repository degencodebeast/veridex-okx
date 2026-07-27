// Signal Trials read client. Binds the frozen wire family (lib/wire.ts, mirrored in
// contracts/veridex_api.contract.ts) and maps it to the screen view-model (lib/contracts.ts).
// Same posture as lib/api.ts: the frontend NEVER reimplements law/scoring/verification (CON-003) —
// verify is an authoritative backend recompute and this client only relays its verdicts.
//
// WHAT THE VERIFY ENDPOINT ACTUALLY ESTABLISHES, stated precisely because this is a public
// artifact: the eight checks are REPRODUCIBILITY CHECKS OVER RECORDED EVIDENCE. They re-derive a
// receipt's commit-time and settlement-time claims from the stored artifacts. What that buys is
// that a single-field edit to a published outcome can no longer change or erase a score while
// verification reports green. It is NOT proof against a malicious storage operator, and no copy
// in this module may say otherwise.
//
// THE AXIS THIS MODULE PROTECTS: every nullable wire field is REQUIRED with no default, and every
// adapter below passes null through verbatim. A `??` or `||` fallback on any of them would publish
// a fabricated result on a public leaderboard — a zero markout is a real FLAT outcome and a zero
// Brier is a PERFECT score.
//
// ONE POSTURE FOR NON-CONFORMANCE, applied uniformly: when a response omits a field the frozen
// contract requires, this module throws an error NAMING the missing field. It never normalises the
// absence into a value, because "the backend omitted this" and "the backend reported nothing was
// computed" are different claims and merging them fabricates the second. It also never lets the
// violation surface as an anonymous `TypeError`, which names whichever field the code happened to
// dereference first rather than the one actually missing. The three sites are the absent `outcome`
// key (`adaptTrial`), the absent `checks` map (`adaptVerifyReceipt`) and an unrecognised
// `season_state` (`getSeasonHealth`). Unrecognised check KEYS are the deliberate exception: they
// are surfaced in `unexpectedKeys` rather than thrown, because an extra key invalidates nothing
// that was served alongside it.
import { ApiError } from '@/lib/api';
import type * as W from '@/lib/wire';
import type {
  AgentRecord, CommitReceipt, OpenTrial, SeasonViewState, SignalTrialsHealth, SignalTrialsRow,
  SignalTrialsSeason, TrialCard, TrialOutcome, VerifyChecks,
} from '@/lib/contracts';

// ---------------------------------------------------------------------------
// CF-5 — the eight check keys.
// ---------------------------------------------------------------------------
// The backend publishes `checks` as `dict[str, Literal["pass","fail","pending"]]`, so the KEYS are
// unconstrained `str` on the wire. That makes this declaration a SECOND site that can drift from
// the frozen tuples with nothing able to notice — hence ONE exported constant, declared here and
// never re-spelled at a use site. Order is the frozen order: the four commit-time checks, then the
// four outcome checks.
//
// A `pending` verdict does NOT distinguish "not settled yet" from "settled UNSCORED and never will
// be". That distinction lives in `receipt.status` and must be read alongside these.
export const SIGNAL_TRIALS_CHECK_KEYS = [
  { key: 'body_hash', phase: 'commit' },
  { key: 'manifest', phase: 'commit' },
  { key: 'deadline_respected', phase: 'commit' },
  { key: 'live_mode', phase: 'commit' },
  { key: 'bar_version', phase: 'outcome' },
  { key: 'law_version', phase: 'outcome' },
  { key: 'evidence_equality', phase: 'outcome' },
  { key: 'outcome_source', phase: 'outcome' },
] as const;

// The one permitted spelling of the markout label. It is "paper" because these are modeled
// markouts over recorded bars after modeled costs — never a realized fill, position or PnL.
export const SIGNAL_TRIALS_MARKOUT_LABEL = 'paper markout (bps, after modeled costs)';

const SIGNAL_TRIALS_PREFIX = '/signal-trials';

// Centralized path map — the binding points. A route change is a one-line edit.
export const SIGNAL_TRIALS_PATHS = {
  health: () => `${SIGNAL_TRIALS_PREFIX}/health`,
  season: () => `${SIGNAL_TRIALS_PREFIX}/season`,
  openTrial: () => `${SIGNAL_TRIALS_PREFIX}/open-trial`,
  trial: (trialId: string) => `${SIGNAL_TRIALS_PREFIX}/trials/${encodeURIComponent(trialId)}`,
  agent: (payer: string) => `${SIGNAL_TRIALS_PREFIX}/agents/${encodeURIComponent(payer)}`,
  verifyReceipt: (receiptId: string) =>
    `${SIGNAL_TRIALS_PREFIX}/receipts/${encodeURIComponent(receiptId)}/verify`,
} as const;

// The four published states as an exhaustive runtime lookup — needed because /health is UNTYPED
// and can therefore serve any string at all.
//
// Typing it `Record<SeasonViewState, true>` makes the compiler enforce BOTH directions: a state
// added to `SeasonViewState` without an entry here is a missing-property error, and an entry here
// that is not a `SeasonViewState` is an excess-property error. So the runtime validator below can
// never quietly fall out of step with the type the screens branch on.
const SEASON_VIEW_STATES: Record<SeasonViewState, true> = {
  not_built: true,
  no_season: true,
  qualified: true,
  exploratory: true,
};

function isSeasonViewState(value: string): value is SeasonViewState {
  return Object.prototype.hasOwnProperty.call(SEASON_VIEW_STATES, value);
}

// ---------------------------------------------------------------------------
// transport
// ---------------------------------------------------------------------------
// Resolve a request URL against the configured API base, read at CALL TIME (Next inlines
// NEXT_PUBLIC_* into the client bundle, and it is a live env read on the server) so SSR fetches
// resolve to an ABSOLUTE URL. Fail-closed on the server: a missing base is a boot error because a
// relative URL cannot be fetched in Node — never silently hit the wrong origin.
//
// This mirrors lib/api.ts's `resolveApiUrl` deliberately rather than importing it: that helper is
// module-private, and lib/api.ts is outside H5.1's owned-files list, so exporting it would be a
// scope deviation. The call-time env read is the honesty-relevant part and is preserved exactly.
function resolveSignalTrialsUrl(path: string): string {
  const base = process.env.NEXT_PUBLIC_API_BASE ?? '';
  if (base) return `${base}${path}`;
  if (typeof window === 'undefined') {
    throw new Error(
      'NEXT_PUBLIC_API_BASE is required for server-side rendering: set it to the absolute API origin ' +
        '(e.g. https://api.example.com). A relative URL cannot be fetched during SSR.',
    );
  }
  return path;
}

// These routes are public and read-only, so a plain accept-JSON GET is correct — no bearer.
async function signalTrialsGet(path: string): Promise<Response> {
  return fetch(resolveSignalTrialsUrl(path), { headers: { accept: 'application/json' } });
}

async function getJson<T>(path: string): Promise<T> {
  const res = await signalTrialsGet(path);
  if (!res.ok) throw new ApiError(res.status, `GET ${path} failed: ${res.status}`);
  return (await res.json()) as T;
}

// 404 is a legitimate DOMAIN state on every Signal Trials resource route, and the backend says so
// under a named code (`no_open_trial`, `trial_not_found`, `agent_not_found`, `receipt_not_found`).
// Every OTHER non-ok status throws, so a 5xx can never be mistaken for "this does not exist".
async function getJsonOrNullOn404<T>(path: string): Promise<T | null> {
  const res = await signalTrialsGet(path);
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `GET ${path} failed: ${res.status}`);
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// adapters — wire → view. Nulls are passed through, never coerced.
// ---------------------------------------------------------------------------
export function adaptSignalTrialsRow(w: W.SignalTrialsRowWire): SignalTrialsRow {
  return {
    agentId: w.agent_id,
    qualified: w.qualified,
    avgBrier: w.avg_brier,                       // null ⇒ nothing settled; 0 ⇒ a PERFECT score
    cappedAvgMarkoutBps: w.capped_avg_markout_bps, // null ⇒ nothing settled; 0 ⇒ a FLAT outcome
    activeDecisions: w.active_decisions,
    activeCoverage: w.active_coverage,
    unscored: w.unscored,
    isControl: w.is_control,
  };
}

export function adaptSignalTrialsSeason(w: W.SignalTrialsSeasonWire): SignalTrialsSeason {
  return {
    seasonStatus: w.season_status,
    seasonId: w.season_id,
    combo: w.combo,
    sampleSize: w.sample_size,
    rows: w.rows.map(adaptSignalTrialsRow), // served order preserved — the backend ranks, not us
  };
}

export function adaptOpenTrial(w: W.OpenTrialWire): OpenTrial {
  return {
    trialId: w.trial_id,
    trialMode: w.trial_mode,
    t0Ms: w.t0_ms,
    commitDeadlineMs: w.commit_deadline_ms,
    evidence: w.evidence,
    evidenceHash: w.evidence_hash,
  };
}

export function adaptTrialOutcome(w: W.TrialOutcomeWire): TrialOutcome {
  return {
    trialId: w.trial_id,
    status: w.status, // preserved verbatim — the ONLY thing separating `pending` from `UNSCORED`
    entry: w.entry,
    future: w.future,
    closeTsMs: w.close_ts_ms,
    observationLagMs: w.observation_lag_ms,
    followMarkoutBps: w.follow_markout_bps,
    fadeMarkoutBps: w.fade_markout_bps,
    followProfitable: w.follow_profitable,
  };
}

export function adaptTrial(w: W.TrialWire): TrialCard {
  // An ABSENT `outcome` key is a CONTRACT VIOLATION, and it is a different claim from
  // `outcome: null`. `null` is the contract's real, meaningful statement that nothing was computed
  // at all; an absent key means the response never answered the question. Relaxing the check below
  // to `== null` would merge the two and report a non-conforming response as a computed absence,
  // which is exactly the kind of fabricated claim this module exists to prevent.
  //
  // This is not a defensive hypothetical. Until H4.3 merges, GET /signal-trials/trials/{id} serves
  // an `OpenTrialResponse` — six fields, no `outcome` key — so this branch is reachable against the
  // backend in this tree today. Without the guard, `adaptTrialOutcome(undefined)` throws a
  // TypeError naming `trial_id`, a field that is present and correct, sending a debugger to the
  // wrong field entirely. Named here instead, matching `getSeasonHealth`'s fail-closed posture.
  if (w.outcome === undefined) {
    throw new Error(
      `GET ${SIGNAL_TRIALS_PATHS.trial(w.trial_id)} omitted the required 'outcome' key. The frozen ` +
        'contract requires it to be present; `null` states that nothing was computed, whereas an ' +
        'absent key is a non-conforming response and is not treated as an absent outcome.',
    );
  }
  return {
    trialId: w.trial_id,
    trialMode: w.trial_mode,
    t0Ms: w.t0_ms,
    commitDeadlineMs: w.commit_deadline_ms,
    evidence: w.evidence,
    evidenceHash: w.evidence_hash,
    // A recorded null outcome stays null: it claims nothing was computed at all, which is a
    // WEAKER statement than a recorded `pending`, and the two must not be merged.
    outcome: w.outcome === null ? null : adaptTrialOutcome(w.outcome),
  };
}

export function adaptCommitReceipt(w: W.CommitReceiptWire): CommitReceipt {
  return {
    receiptId: w.receipt_id,
    trialId: w.trial_id,
    payer: w.payer,
    pFollowProfitable: w.p_follow_profitable,
    methodologyVersion: w.methodology_version,
    action: w.action,
    status: w.status, // C26: `pending` and `UNSCORED` share identical null metrics — never collapse
    brier: w.brier,
    chosenMarkoutBps: w.chosen_markout_bps,
    committedAtMs: w.committed_at_ms,
    commitDeadlineMs: w.commit_deadline_ms,
    trialMode: w.trial_mode,
    bodyHash: w.body_hash,
    paymentTxHash: w.payment_tx_hash,
  };
}

export function adaptAgentRecord(w: W.AgentRecordWire): AgentRecord {
  return {
    payer: w.payer,
    commits: w.commits,
    settled: w.settled,
    pending: w.pending,
    unscored: w.unscored,
    avgBrier: w.avg_brier,
    cappedAvgMarkoutBps: w.capped_avg_markout_bps,
    qualified: w.qualified, // relayed as served; §8.4 makes this false on live records
  };
}

export function adaptVerifyReceipt(w: W.VerifyReceiptWire): VerifyChecks {
  // Same posture as the absent-`outcome` guard above: `checks` is required and non-nullable, so an
  // absent map is a contract violation and is named rather than normalised. Substituting `{}` would
  // render eight `null` verdicts — indistinguishable from a backend that legitimately served an
  // empty map — and so would report "no verdicts served" as though that were the answer. An empty
  // map that WAS served is data, not a violation, and still yields eight nulls below.
  if (w.checks === undefined || w.checks === null) {
    throw new Error(
      `GET ${SIGNAL_TRIALS_PATHS.verifyReceipt(w.receipt_id)} omitted the required 'checks' map. ` +
        'The frozen contract requires it to be present; a missing map is a non-conforming response ' +
        'and is not treated as an absence of verdicts.',
    );
  }
  const served: Record<string, W.SignalTrialsCheckStatusWire> = w.checks;
  const frozen = new Set<string>(SIGNAL_TRIALS_CHECK_KEYS.map((c) => c.key));
  return {
    receiptId: w.receipt_id,
    // Always the frozen eight, always in the frozen order. A key the backend did not serve is
    // `null` — NOT `pending`, which is a real verdict the verifier would then never have made.
    checks: SIGNAL_TRIALS_CHECK_KEYS.map(({ key, phase }) => ({
      key,
      phase,
      status: Object.prototype.hasOwnProperty.call(served, key) ? served[key] : null,
    })),
    // Surfaced rather than swallowed: CF-5's drift is invisible precisely because the keys are
    // unconstrained, so a name we do not recognise has to be reportable.
    unexpectedKeys: Object.keys(served).filter((k) => !frozen.has(k)),
    receipt: w.receipt === null ? null : adaptCommitReceipt(w.receipt),
    // Lifted so a caller cannot render the checks without the one field that separates a
    // not-settled-yet `pending` from a settled-`UNSCORED`-and-never-will-be.
    receiptStatus: w.receipt === null ? null : w.receipt.status,
  };
}

// ---------------------------------------------------------------------------
// fetchers
// ---------------------------------------------------------------------------

// GET /signal-trials/health. The route is UNTYPED (`dict[str, Any]`), so `season_state` is
// validated here: an unrecognised value FAILS CLOSED rather than being coerced into one of the
// four. On a public leaderboard a visible error is strictly better than a confidently-rendered
// wrong state, and coercion is exactly how a fifth backend state would ship as a silent lie.
export async function getSeasonHealth(): Promise<SignalTrialsHealth> {
  const w = await getJson<W.SignalTrialsHealthWire>(SIGNAL_TRIALS_PATHS.health());
  if (!isSeasonViewState(w.season_state)) {
    throw new Error(
      `GET ${SIGNAL_TRIALS_PATHS.health()} returned an unrecognised season_state ` +
        `${JSON.stringify(w.season_state)}; expected one of ${Object.keys(SEASON_VIEW_STATES).join(', ')}`,
    );
  }
  return { ok: w.ok, seasonState: w.season_state };
}

// GET /signal-trials/season, composed with /health.
//
// Both `not_built` and `no_season` answer 404 here, so the season response ALONE cannot tell them
// apart — /health is the only place they differ, and C54 makes `not_built` reachable in production
// for every failed/refused/aborted probe. So a 404 is resolved by asking health, and the document
// fields come back null rather than zero-filled.
//
// Any other non-ok status throws. A transport failure must never collapse into `no_season`: that
// would report "the preflight ran and declined to build a season" on the strength of a 502.
export async function getSignalTrialsSeason(): Promise<SignalTrialsSeason> {
  const path = SIGNAL_TRIALS_PATHS.season();
  const res = await signalTrialsGet(path);
  if (res.ok) return adaptSignalTrialsSeason((await res.json()) as W.SignalTrialsSeasonWire);
  if (res.status !== 404) throw new ApiError(res.status, `GET ${path} failed: ${res.status}`);

  // 404 `no_season_published` — the honest empty state. Which of the two it is comes from health,
  // and if health cannot be read either, that throws too rather than guessing a state.
  const health = await getSeasonHealth();
  return {
    seasonStatus: health.seasonState,
    seasonId: null,
    combo: null,
    sampleSize: null, // NOT 0 — "no season document" is not "we sampled zero signals"
    rows: [],
  };
}

// GET /signal-trials/open-trial → the open live trial, or `null` on 404 `no_open_trial` (an honest
// "nothing is open right now", not an error). That 404 is this route's ONLY refusal — it either
// serves the trial or returns that one code — so every other status throws and a server or
// transport failure can never be read as "nothing is open". (The router's two `503`s,
// `trials_not_open` and `payment_gate_not_configured`, are raised by `_commit_unavailable()` on
// /signal-trials/commit, which this read client never calls.)
export async function getOpenTrial(): Promise<OpenTrial | null> {
  const w = await getJsonOrNullOn404<W.OpenTrialWire>(SIGNAL_TRIALS_PATHS.openTrial());
  return w === null ? null : adaptOpenTrial(w);
}

// GET /signal-trials/trials/{id} → the trial card, or `null` on 404 `trial_not_found`.
export async function getTrial(trialId: string): Promise<TrialCard | null> {
  const w = await getJsonOrNullOn404<W.TrialWire>(SIGNAL_TRIALS_PATHS.trial(trialId));
  return w === null ? null : adaptTrial(w);
}

// GET /signal-trials/agents/{payer} → the participant record, or `null` on 404 `agent_not_found`.
// The backend 404s zero finalized commits rather than serving a zero record, because an all-zero
// record asserts that this payer participated and scored nothing — a different claim from having
// no record at all, and one that would let any address be quoted as a participant. `null` here
// preserves that distinction instead of re-introducing the zero row on the client.
export async function getAgentRecord(payer: string): Promise<AgentRecord | null> {
  const w = await getJsonOrNullOn404<W.AgentRecordWire>(SIGNAL_TRIALS_PATHS.agent(payer));
  return w === null ? null : adaptAgentRecord(w);
}

// GET /signal-trials/receipts/{id}/verify → eight verdicts, or `null` on 404 `receipt_not_found`.
// A failed check is a 200 carrying a `fail`, never a 500 — reporting a tampered receipt as a
// server error would make tampering indistinguishable from an outage. An unreadable row answers
// 200 with `receipt: null` and eight verdicts, which is why a null receipt is not an error here.
export async function verifyReceipt(receiptId: string): Promise<VerifyChecks | null> {
  const w = await getJsonOrNullOn404<W.VerifyReceiptWire>(SIGNAL_TRIALS_PATHS.verifyReceipt(receiptId));
  return w === null ? null : adaptVerifyReceipt(w);
}
