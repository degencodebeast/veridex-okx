// H5.1 adapter tests. The contract is the SCHEMA_FREEZE packet (8 models, 60 fields, 0
// defaults) frozen at 2d303d4 — NOT the design handoff, which is fifth in the acceptance
// order and whose design-only assumptions are not fields.
//
// THE AXIS THIS FILE EXISTS TO PROTECT: every nullable field is REQUIRED-with-no-default.
// Rendering `0` for `null` publishes a fabricated result on a public leaderboard, because
// a zero markout is a real FLAT outcome and a zero Brier is a PERFECT score. So the null
// assertions below use toBeNull() and never a truthiness check — `expect(x).toBeFalsy()`
// would pass for both `null` and `0` and would therefore protect nothing.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  SIGNAL_TRIALS_CHECK_KEYS,
  SIGNAL_TRIALS_MARKOUT_LABEL,
  SIGNAL_TRIALS_PARTICIPANT_STORE_UNAVAILABLE,
  SIGNAL_TRIALS_PATHS,
  SignalTrialsUnavailableError,
  adaptSignalTrialsSeason,
  adaptTrial,
  adaptCommitReceipt,
  adaptAgentRecord,
  adaptVerifyReceipt,
  getSignalTrialsSeason,
  getSeasonHealth,
  getOpenTrial,
  getTrial,
  getTrialReceipts,
  getAgentRecord,
  verifyReceipt,
} from '@/lib/signal-trials-api';
import { ApiError } from '@/lib/api';
import type * as W from '@/lib/wire';

// The frozen contract fixtures live at the repo root (outside apps/web), same idiom as
// lib/wire.test.ts: read + parse at test time so the wire types cannot drift from the backend.
const FIX = resolve(__dirname, '../../../contracts/fixtures');
function fixture<T>(name: string): T {
  return JSON.parse(readFileSync(resolve(FIX, name), 'utf8')) as T;
}

const seasonWire = fixture<W.SignalTrialsSeasonWire>('signal_trials_season.json');
const trialWire = fixture<W.TrialWire>('signal_trials_trial.json');

beforeEach(() => { vi.restoreAllMocks(); });
afterEach(() => { vi.unstubAllGlobals(); });

function stubFetch(impl: typeof fetch) {
  vi.stubGlobal('fetch', vi.fn(impl) as unknown as typeof fetch);
}
function calls() {
  return (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
}
// The backend's refusal envelope is `{"error": code}` — deliberately NOT HTTPException's
// `{"detail": ...}` (signal_trials_router.py `_error`). Fixtures mirror it exactly.
function errorResponse(status: number, code: string) {
  return new Response(JSON.stringify({ error: code }), { status });
}

// ---------------------------------------------------------------------------
// CF-5 — the eight check keys are published as unconstrained `str`, so this
// frontend declaration is a SECOND site that can drift from the frozen tuples
// with nothing able to notice. Pin it hard.
// ---------------------------------------------------------------------------
describe('CF-5: the eight check keys are one exported constant', () => {
  it('holds exactly the eight frozen keys in the frozen order, commit-time then outcome', () => {
    expect(SIGNAL_TRIALS_CHECK_KEYS.map((c) => c.key)).toEqual([
      'body_hash', 'manifest', 'deadline_respected', 'live_mode',
      'bar_version', 'law_version', 'evidence_equality', 'outcome_source',
    ]);
  });

  it('tags the first four commit-time and the last four outcome', () => {
    expect(SIGNAL_TRIALS_CHECK_KEYS.map((c) => c.phase)).toEqual([
      'commit', 'commit', 'commit', 'commit',
      'outcome', 'outcome', 'outcome', 'outcome',
    ]);
  });
});

// ---------------------------------------------------------------------------
// Language guard — this is a public artifact.
// ---------------------------------------------------------------------------
describe('language guard', () => {
  it('the markout label is exactly the frozen string', () => {
    expect(SIGNAL_TRIALS_MARKOUT_LABEL).toBe('paper markout (bps, after modeled costs)');
  });

  // The regex below is the specification, not the name: it bans EIGHT words — pnl, profit,
  // made money, realized, fill, alpha, edge, proven. Naming a subset in the title is how a
  // maintainer ends up tripping a guard whose failure message does not explain itself.
  it('contains none of the banned outcome-claim words', () => {
    expect(SIGNAL_TRIALS_MARKOUT_LABEL).not.toMatch(/pnl|profit|made money|realized|fill|alpha|edge|proven/i);
  });
});

// ---------------------------------------------------------------------------
// Season adapter — fixture wire → view model.
// ---------------------------------------------------------------------------
describe('adaptSignalTrialsSeason', () => {
  it('propagates seasonStatus and sample size from the served season document', () => {
    const s = adaptSignalTrialsSeason(seasonWire);
    expect(s.seasonStatus).toBe(seasonWire.season_status);
    expect(s.seasonId).toBe(seasonWire.season_id);
    expect(s.sampleSize).toBe(seasonWire.sample_size);
    expect(s.rows).toHaveLength(seasonWire.rows.length);
  });

  it('flags control rows and keeps them in the served order', () => {
    const s = adaptSignalTrialsSeason(seasonWire);
    expect(s.rows.map((r) => r.isControl)).toEqual(seasonWire.rows.map((r) => r.is_control));
    expect(s.rows.some((r) => r.isControl)).toBe(true);   // the fixture carries a control row
    expect(s.rows.some((r) => !r.isControl)).toBe(true);  // ...and a non-control row (C52)
  });

  it('preserves a null avgBrier as null and NEVER as 0 (a zero Brier is a PERFECT score)', () => {
    const s = adaptSignalTrialsSeason(seasonWire);
    const unsettled = s.rows.find((r) => r.agentId === 'agent-unsettled');
    expect(unsettled).toBeDefined();
    expect(unsettled!.avgBrier).toBeNull();
    expect(unsettled!.cappedAvgMarkoutBps).toBeNull();
  });

  it('preserves a real 0 markout as 0 (a zero markout is a real FLAT outcome)', () => {
    const s = adaptSignalTrialsSeason(seasonWire);
    const flat = s.rows.find((r) => r.agentId === 'agent-flat');
    expect(flat).toBeDefined();
    expect(flat!.cappedAvgMarkoutBps).toBe(0);
    expect(flat!.avgBrier).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// getSignalTrialsSeason() composes /season with /health.
// Both `not_built` and `no_season` answer 404 on /season; /health is the ONLY
// place they differ. A transport failure must NEVER collapse into `no_season`.
// ---------------------------------------------------------------------------
describe('getSignalTrialsSeason composes season + health', () => {
  it('returns the served season on a 200 without needing health', async () => {
    stubFetch(async (input) => {
      const url = String(input);
      if (url.includes(SIGNAL_TRIALS_PATHS.season())) {
        return new Response(JSON.stringify(seasonWire), { status: 200 });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    const s = await getSignalTrialsSeason();
    expect(s.seasonStatus).toBe(seasonWire.season_status);
    expect(s.rows.length).toBeGreaterThan(0);
    expect(calls()).toHaveLength(1); // health is not consulted when the season is served
  });

  it('404 + health no_season → seasonStatus "no_season" with NO fabricated rows', async () => {
    stubFetch(async (input) => {
      const url = String(input);
      if (url.includes('/season')) return errorResponse(404, 'no_season_published');
      if (url.includes('/health')) {
        return new Response(JSON.stringify({ ok: true, season_state: 'no_season' }), { status: 200 });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    const s = await getSignalTrialsSeason();
    expect(s.seasonStatus).toBe('no_season');
    expect(s.rows).toEqual([]);
    // A 404 means no season document exists. `sampleSize: 0` would assert "we sampled zero
    // signals", a different and false claim — so it must be null, not 0.
    expect(s.sampleSize).toBeNull();
    expect(s.seasonId).toBeNull();
  });

  it('404 + health not_built → seasonStatus "not_built", DISTINCT from no_season', async () => {
    stubFetch(async (input) => {
      const url = String(input);
      if (url.includes('/season')) return errorResponse(404, 'no_season_published');
      if (url.includes('/health')) {
        return new Response(JSON.stringify({ ok: true, season_state: 'not_built' }), { status: 200 });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    const s = await getSignalTrialsSeason();
    expect(s.seasonStatus).toBe('not_built');
    expect(s.seasonStatus).not.toBe('no_season'); // the two 404 branches are NOT collapsed
    expect(s.rows).toEqual([]);
  });

  it('a 500 on /season THROWS and never collapses into a season state', async () => {
    stubFetch(async (input) => {
      const url = String(input);
      if (url.includes('/season')) return new Response('boom', { status: 500 });
      throw new Error(`health must not be consulted on a transport failure: ${url}`);
    });
    await expect(getSignalTrialsSeason()).rejects.toBeInstanceOf(ApiError);
    await expect(getSignalTrialsSeason()).rejects.toMatchObject({ status: 500 });
  });

  it('a 500 on /health after a season 404 THROWS rather than guessing a state', async () => {
    stubFetch(async (input) => {
      const url = String(input);
      if (url.includes('/season')) return errorResponse(404, 'no_season_published');
      return new Response('boom', { status: 500 });
    });
    await expect(getSignalTrialsSeason()).rejects.toBeInstanceOf(ApiError);
  });
});

// ---------------------------------------------------------------------------
// /health is UNTYPED (dict[str, Any]) with EXACTLY two fields. `read_state`'s
// `detail` is projected away at the route and must not be modelled.
// ---------------------------------------------------------------------------
describe('getSeasonHealth', () => {
  it.each(['not_built', 'no_season', 'qualified', 'exploratory'] as const)(
    'passes through the %s season state verbatim',
    async (state) => {
      stubFetch(async () => new Response(JSON.stringify({ ok: true, season_state: state }), { status: 200 }));
      const h = await getSeasonHealth();
      expect(h).toEqual({ ok: true, seasonState: state });
    },
  );

  it('fails closed on an unrecognised season_state rather than rendering a wrong state', async () => {
    stubFetch(async () => new Response(JSON.stringify({ ok: true, season_state: 'brand_new' }), { status: 200 }));
    await expect(getSeasonHealth()).rejects.toThrow(/brand_new/);
  });
});

// ---------------------------------------------------------------------------
// Trial adapter. TrialResponse has SEVEN fields and NO participants list —
// `ParticipantSettlement` does not exist at the frozen head. It is a design
// note, not a field, and must not be invented.
// ---------------------------------------------------------------------------
describe('adaptTrial', () => {
  it('maps the seven frozen fields and carries NO participants array', () => {
    const t = adaptTrial(trialWire);
    expect(t.trialId).toBe(trialWire.trial_id);
    expect(t.trialMode).toBe('live');
    expect(t.t0Ms).toBe(trialWire.t0_ms);
    expect(t.commitDeadlineMs).toBe(trialWire.commit_deadline_ms);
    expect(t.evidenceHash).toBe(trialWire.evidence_hash);
    expect(t.evidence).toEqual(trialWire.evidence);
    expect('participants' in t).toBe(false);
  });

  it('preserves a settled outcome including a real 0 markout', () => {
    const t = adaptTrial(trialWire);
    expect(t.outcome).not.toBeNull();
    expect(t.outcome!.status).toBe('settled');
    expect(t.outcome!.followMarkoutBps).toBe(trialWire.outcome!.follow_markout_bps);
    expect(t.outcome!.observationLagMs).toBe(trialWire.outcome!.observation_lag_ms);
    expect(t.outcome!.followProfitable).toBe(trialWire.outcome!.follow_profitable);
  });

  it('a null outcome stays null — a WEAKER statement than a recorded pending', () => {
    const t = adaptTrial({ ...trialWire, outcome: null });
    expect(t.outcome).toBeNull();
  });

  // An ABSENT `outcome` key and `outcome: null` are DIFFERENT claims and must not be merged.
  // `null` is the contract's real statement that nothing was computed; an absent key means the
  // response did not answer the question at all, which is a contract violation. This is not
  // hypothetical: at this branch base GET /signal-trials/trials/{id} still returns an
  // OpenTrialResponse — six fields, no `outcome` key — so this is the shape the backend in this
  // very tree serves today. Both controls are asserted (C52): absent throws, null does not.
  it('an ABSENT outcome key throws a NAMED error, not an anonymous TypeError', () => {
    const sixKeyShape = {
      trial_id: 'trial-0k9f2c',
      trial_mode: 'live',
      t0_ms: 1_700_000_000_000,
      commit_deadline_ms: 1_700_000_300_000,
      evidence: { symbol: 'AAA' },
      evidence_hash: 'eh',
    } as unknown as W.TrialWire;
    expect(Object.keys(sixKeyShape)).toHaveLength(6);
    expect('outcome' in sixKeyShape).toBe(false);

    // The message must name `outcome` — the field that is actually missing. The pre-fix failure
    // was a TypeError naming `trial_id`, a field that is present and correct, which points a
    // debugger at the wrong field entirely.
    expect(() => adaptTrial(sixKeyShape)).toThrow(/outcome/);
    expect(() => adaptTrial(sixKeyShape)).not.toThrow(TypeError);
  });

  it('getTrial surfaces the same named error on a 200 carrying the six-key shape', async () => {
    stubFetch(async () => new Response(
      JSON.stringify({
        trial_id: 'trial-0k9f2c', trial_mode: 'live', t0_ms: 1, commit_deadline_ms: 2,
        evidence: {}, evidence_hash: 'eh',
      }),
      { status: 200 },
    ));
    await expect(getTrial('trial-0k9f2c')).rejects.toThrow(/outcome/);
  });

  it('a pending outcome is a RECORDED row with null metrics, not an absent outcome', () => {
    const pending: W.TrialOutcomeWire = {
      trial_id: trialWire.trial_id,
      status: 'pending',
      entry: 100.5,
      future: null,
      close_ts_ms: null,
      observation_lag_ms: null,
      follow_markout_bps: null,
      fade_markout_bps: null,
      follow_profitable: null,
    };
    const t = adaptTrial({ ...trialWire, outcome: pending });
    expect(t.outcome).not.toBeNull();          // recorded — distinct from outcome: null
    expect(t.outcome!.status).toBe('pending'); // C26: assert the STATUS, not only the nulls
    expect(t.outcome!.entry).toBe(100.5);      // entry is NON-nullable and survives
    expect(t.outcome!.followMarkoutBps).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// C26 — `pending` and `UNSCORED` carry IDENTICAL null metrics. A test asserting
// only the nulls CANNOT separate them and passes against an adapter that
// collapsed the labels. These two cases assert the STATUS, so a collapse in
// EITHER direction kills exactly one of them (C52: both controls).
// ---------------------------------------------------------------------------
function receiptWire(status: W.TrialStatusWire): W.CommitReceiptWire {
  return {
    receipt_id: 'rcpt_c26',
    trial_id: 'trial_c26',
    payer: '0xpayer',
    p_follow_profitable: 0.62,
    methodology_version: null,
    action: 'FOLLOW',
    status,
    // IDENTICAL null metrics on both labels — this is precisely why the status must be asserted.
    brier: null,
    chosen_markout_bps: null,
    committed_at_ms: 1_700_000_000_000,
    commit_deadline_ms: 1_700_000_300_000,
    trial_mode: 'live',
    body_hash: 'bh_c26',
    payment_tx_hash: 'tx_c26',
  };
}

describe('C26: pending and UNSCORED are never collapsed', () => {
  it('a pending receipt keeps status "pending" (dies if the adapter hardcodes UNSCORED)', () => {
    const r = adaptCommitReceipt(receiptWire('pending'));
    expect(r.status).toBe('pending');
    expect(r.status).not.toBe('UNSCORED');
    expect(r.brier).toBeNull();
    expect(r.chosenMarkoutBps).toBeNull();
  });

  it('an UNSCORED receipt keeps status "UNSCORED" (dies if the adapter hardcodes pending)', () => {
    const r = adaptCommitReceipt(receiptWire('UNSCORED'));
    expect(r.status).toBe('UNSCORED');
    expect(r.status).not.toBe('pending');
    expect(r.brier).toBeNull();
    expect(r.chosenMarkoutBps).toBeNull();
  });

  it('a settled receipt keeps a real 0 brier and 0 markout as 0, never as null', () => {
    const r = adaptCommitReceipt({ ...receiptWire('settled'), brier: 0, chosen_markout_bps: 0 });
    expect(r.status).toBe('settled');
    expect(r.brier).toBe(0);
    expect(r.chosenMarkoutBps).toBe(0);
  });

  it('preserves a null methodologyVersion, commitDeadlineMs and trialMode', () => {
    const r = adaptCommitReceipt({
      ...receiptWire('pending'), methodology_version: null, commit_deadline_ms: null, trial_mode: null,
    });
    expect(r.methodologyVersion).toBeNull();
    expect(r.commitDeadlineMs).toBeNull();
    expect(r.trialMode).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Agent record. `qualified` is always false on live records (§8.4).
// ---------------------------------------------------------------------------
describe('adaptAgentRecord', () => {
  it('maps the eight fields and preserves null aggregates as null', () => {
    const w: W.AgentRecordWire = {
      payer: '0xpayer', commits: 3, settled: 0, pending: 2, unscored: 1,
      avg_brier: null, capped_avg_markout_bps: null, qualified: false,
    };
    const a = adaptAgentRecord(w);
    expect(a).toEqual({
      payer: '0xpayer', commits: 3, settled: 0, pending: 2, unscored: 1,
      avgBrier: null, cappedAvgMarkoutBps: null, qualified: false,
    });
  });

  it('keeps a real 0 avgBrier as 0', () => {
    const w: W.AgentRecordWire = {
      payer: '0xp', commits: 1, settled: 1, pending: 0, unscored: 0,
      avg_brier: 0, capped_avg_markout_bps: 0, qualified: false,
    };
    const a = adaptAgentRecord(w);
    expect(a.avgBrier).toBe(0);
    expect(a.cappedAvgMarkoutBps).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Verify adapter — surfaces all EIGHT checks including the `pending` state.
// `pending` does NOT distinguish "not settled yet" from "settled UNSCORED and
// never will be"; that lives in receipt.status, which must be read alongside.
// ---------------------------------------------------------------------------
const ALL_EIGHT: Record<string, 'pass' | 'fail' | 'pending'> = {
  body_hash: 'pass', manifest: 'pass', deadline_respected: 'pass', live_mode: 'pass',
  bar_version: 'pending', law_version: 'pending', evidence_equality: 'pending', outcome_source: 'pending',
};

describe('adaptVerifyReceipt', () => {
  it('surfaces all EIGHT checks in the frozen order with their verdicts verbatim', () => {
    const v = adaptVerifyReceipt({ receipt_id: 'r1', checks: { ...ALL_EIGHT }, receipt: receiptWire('pending') });
    expect(v.checks).toHaveLength(8);
    expect(v.checks.map((c) => c.key)).toEqual(SIGNAL_TRIALS_CHECK_KEYS.map((c) => c.key));
    expect(v.checks.map((c) => c.status)).toEqual([
      'pass', 'pass', 'pass', 'pass', 'pending', 'pending', 'pending', 'pending',
    ]);
  });

  it('preserves a fail as a fail — a failed check is a 200 carrying a fail, never an error', () => {
    const v = adaptVerifyReceipt({
      receipt_id: 'r1', checks: { ...ALL_EIGHT, body_hash: 'fail' }, receipt: receiptWire('UNSCORED'),
    });
    expect(v.checks.find((c) => c.key === 'body_hash')!.status).toBe('fail');
  });

  it('an ABSENT key is null, never silently rendered as the real verdict "pending"', () => {
    const { manifest: _dropped, ...missingOne } = ALL_EIGHT;
    const v = adaptVerifyReceipt({ receipt_id: 'r1', checks: missingOne, receipt: null });
    expect(v.checks).toHaveLength(8);
    expect(v.checks.find((c) => c.key === 'manifest')!.status).toBeNull();
    expect(v.checks.find((c) => c.key === 'body_hash')!.status).toBe('pass');
  });

  it('surfaces served keys outside the frozen eight instead of swallowing the drift (CF-5)', () => {
    const v = adaptVerifyReceipt({
      receipt_id: 'r1', checks: { ...ALL_EIGHT, brand_new_check: 'pass' }, receipt: null,
    });
    expect(v.unexpectedKeys).toEqual(['brand_new_check']);
    expect(v.checks).toHaveLength(8); // the frozen eight are still reported in order
  });

  it('has no unexpected keys on an exactly-conforming response', () => {
    const v = adaptVerifyReceipt({ receipt_id: 'r1', checks: { ...ALL_EIGHT }, receipt: null });
    expect(v.unexpectedKeys).toEqual([]);
  });

  // Same posture as the absent-`outcome` guard: `checks` is required and non-nullable, so an
  // absent map is a contract violation, and it is named rather than normalised. Silently
  // substituting `{}` would render eight `null` verdicts — indistinguishable from a backend that
  // legitimately served an empty map, i.e. it would report "no verdicts" as if that were an answer.
  it('an ABSENT checks map throws a NAMED error rather than rendering eight null verdicts', () => {
    const noChecks = { receipt_id: 'r1', receipt: null } as unknown as W.VerifyReceiptWire;
    expect(() => adaptVerifyReceipt(noChecks)).toThrow(/checks/);
    expect(() => adaptVerifyReceipt(noChecks)).not.toThrow(TypeError);
  });

  it('an EMPTY served checks map is legitimate and yields eight null verdicts, not a throw', () => {
    // The control for the test above: `{}` is a map the backend actually served, so it is data,
    // not a violation. Eight nulls is the honest rendering of "served, but no verdict for any key".
    const v = adaptVerifyReceipt({ receipt_id: 'r1', checks: {}, receipt: null });
    expect(v.checks).toHaveLength(8);
    expect(v.checks.every((c) => c.status === null)).toBe(true);
    expect(v.unexpectedKeys).toEqual([]);
  });

  // C26 at the verify surface: identical `pending` check maps, separated only by receipt.status.
  it('exposes receiptStatus "pending" alongside identical pending checks', () => {
    const v = adaptVerifyReceipt({ receipt_id: 'r1', checks: { ...ALL_EIGHT }, receipt: receiptWire('pending') });
    expect(v.receiptStatus).toBe('pending');
    expect(v.receiptStatus).not.toBe('UNSCORED');
  });

  it('exposes receiptStatus "UNSCORED" alongside the SAME pending checks', () => {
    const v = adaptVerifyReceipt({ receipt_id: 'r1', checks: { ...ALL_EIGHT }, receipt: receiptWire('UNSCORED') });
    expect(v.receiptStatus).toBe('UNSCORED');
    expect(v.receiptStatus).not.toBe('pending');
  });

  it('a null receipt (unreadable bytes) still carries eight verdicts, with a null receiptStatus', () => {
    const unreadable: Record<string, 'pass' | 'fail' | 'pending'> = {
      body_hash: 'fail', manifest: 'fail', deadline_respected: 'fail', live_mode: 'fail',
      bar_version: 'pending', law_version: 'pending', evidence_equality: 'pending', outcome_source: 'pending',
    };
    const v = adaptVerifyReceipt({ receipt_id: 'r1', checks: unreadable, receipt: null });
    expect(v.receipt).toBeNull();
    expect(v.receiptStatus).toBeNull(); // absent, NOT coerced to a status
    expect(v.checks.filter((c) => c.status === 'fail')).toHaveLength(4);
    expect(v.checks.filter((c) => c.status === 'pending')).toHaveLength(4);
  });
});

// ---------------------------------------------------------------------------
// Fetchers: 404 is a legitimate DOMAIN state where the backend says so; every
// other non-ok throws ApiError so no screen renders a fabricated value.
// ---------------------------------------------------------------------------
describe('resource fetchers treat the backend 404 codes as domain states', () => {
  it('getOpenTrial → null on 404 no_open_trial', async () => {
    stubFetch(async () => errorResponse(404, 'no_open_trial'));
    await expect(getOpenTrial()).resolves.toBeNull();
  });

  it('getOpenTrial → the open trial on 200', async () => {
    const open: W.OpenTrialWire = {
      trial_id: 't1', trial_mode: 'live', t0_ms: 1, commit_deadline_ms: 2,
      evidence: { a: 1 }, evidence_hash: 'eh',
    };
    stubFetch(async () => new Response(JSON.stringify(open), { status: 200 }));
    const t = await getOpenTrial();
    expect(t).toEqual({
      trialId: 't1', trialMode: 'live', t0Ms: 1, commitDeadlineMs: 2,
      evidence: { a: 1 }, evidenceHash: 'eh',
    });
  });

  it('getOpenTrial THROWS on any non-404 rather than reporting "no open trial"', async () => {
    // 404 `no_open_trial` is this route's ONLY refusal, so every other status is a real failure and
    // has to surface. A 5xx rendered as "nothing is open" would hide an outage behind an
    // honest-looking empty state.
    stubFetch(async () => new Response('boom', { status: 500 }));
    await expect(getOpenTrial()).rejects.toBeInstanceOf(ApiError);
  });

  it('getTrial → null on 404 trial_not_found, and hits the exact route', async () => {
    stubFetch(async () => errorResponse(404, 'trial_not_found'));
    await expect(getTrial('t_9')).resolves.toBeNull();
    expect(String(calls()[0][0])).toMatch(/\/signal-trials\/trials\/t_9$/);
  });

  it('getAgentRecord → null on 404 agent_not_found (zero commits is NOT a zero record)', async () => {
    stubFetch(async () => errorResponse(404, 'agent_not_found'));
    await expect(getAgentRecord('0xnobody')).resolves.toBeNull();
  });

  it('getAgentRecord url-encodes the payer', async () => {
    stubFetch(async () => errorResponse(404, 'agent_not_found'));
    await getAgentRecord('0x a/b');
    expect(String(calls()[0][0])).toContain(encodeURIComponent('0x a/b'));
  });

  it('verifyReceipt → null on 404 receipt_not_found', async () => {
    stubFetch(async () => errorResponse(404, 'receipt_not_found'));
    await expect(verifyReceipt('r_9')).resolves.toBeNull();
    expect(String(calls()[0][0])).toMatch(/\/signal-trials\/receipts\/r_9\/verify$/);
  });

  it('verifyReceipt → eight verdicts on a 200 whose receipt is null', async () => {
    stubFetch(async () => new Response(
      JSON.stringify({ receipt_id: 'r_9', checks: { ...ALL_EIGHT }, receipt: null }), { status: 200 },
    ));
    const v = await verifyReceipt('r_9');
    expect(v).not.toBeNull();
    expect(v!.checks).toHaveLength(8);
    expect(v!.receipt).toBeNull();
  });
});

// ===========================================================================
// H5.2-A — GET /signal-trials/trials/{trial_id}/receipts
//
// The authority is PKT-ROUTE-CONTRACT-ADDENDUM-TRIAL-RECEIPTS (sha256 e03ef362…), NOT any
// message. SCHEMA_FREEZE is NOT amended: the array element is the already-frozen
// `CommitReceiptResponse`, so `CommitReceiptWire` is the element and there is no new type.
//
// THE AXIS THIS BLOCK EXISTS TO PROTECT — three distinct answers that a careless consumer
// collapses into one "no rows shown" rendering:
//
//   200 []  nobody paid to commit on this trial   a POSITIVE claim, and a real state
//   404     this trial is unknown                 no basis for the positive claim
//   503     no participant store is mounted       no basis for the positive claim
//
// This is the C26 shape. A test asserting only "no rows shown" passes against a client that
// collapsed any pair of them, which is why every case below asserts the DISCRIMINATOR (the
// resolved value's identity, or the thrown error's identity) and never merely an absence of rows.
// ===========================================================================

// Distinct receipt ids over the shared C26 receipt fixture. `receiptWire` above is the one
// spelling of a `CommitReceiptWire`; re-spelling it here would be the exact drift this task's
// packet warns about.
function receiptAt(receiptId: string, status: W.TrialStatusWire = 'pending'): W.CommitReceiptWire {
  return { ...receiptWire(status), receipt_id: receiptId };
}
function jsonResponse(body: unknown, status: number) {
  return new Response(JSON.stringify(body), { status });
}

describe('SIGNAL_TRIALS_PATHS.trialReceipts', () => {
  it('builds the exact contract path', () => {
    expect(SIGNAL_TRIALS_PATHS.trialReceipts('t_9')).toBe('/signal-trials/trials/t_9/receipts');
  });

  // The other six builders url-encode every path parameter; this one must too. The interop
  // consequence of the encoding is asserted in the 404 block below.
  it('url-encodes the trial id rather than splicing it in raw', () => {
    expect(SIGNAL_TRIALS_PATHS.trialReceipts('a/b')).toBe('/signal-trials/trials/a%2Fb/receipts');
    expect(SIGNAL_TRIALS_PATHS.trialReceipts('a/b')).not.toContain('/a/b/');
  });
});

// ---------------------------------------------------------------------------
// FACT 1 — no new wire type, and ONE adapter for it.
// ---------------------------------------------------------------------------
describe('getTrialReceipts adapts elements through the existing adaptCommitReceipt', () => {
  it('produces exactly what adaptCommitReceipt produces for the same wire object', async () => {
    const w = receiptAt('rcpt_001', 'settled');
    stubFetch(async () => jsonResponse([w], 200));
    const rows = await getTrialReceipts('t_9');
    expect(rows).not.toBeNull();
    expect(rows).toHaveLength(1);
    // A SECOND adapter that diverged in any of the 14 fields dies here.
    //
    // C66 — what this basis CANNOT express: it pins the MAPPING, not the call site. A second
    // adapter that is field-for-field identical to `adaptCommitReceipt` would pass. No fixture
    // can distinguish two identical mappings; only reading the module can, and the packet
    // requires reuse for that reason.
    expect(rows![0]).toEqual(adaptCommitReceipt(w));
  });

  it('hits the receipts route and not the trial route', async () => {
    stubFetch(async () => jsonResponse([], 200));
    await getTrialReceipts('t_9');
    expect(String(calls()[0][0])).toMatch(/\/signal-trials\/trials\/t_9\/receipts$/);
  });

  // Frozen nullability, unchanged: every one of the 14 fields is required with no default, and a
  // `??` on any of them publishes a fabricated result on a public leaderboard.
  it('preserves a null brier and null markout as null (never 0)', async () => {
    stubFetch(async () => jsonResponse([receiptAt('rcpt_001', 'pending')], 200));
    const rows = await getTrialReceipts('t_9');
    expect(rows![0].brier).toBeNull();
    expect(rows![0].chosenMarkoutBps).toBeNull();
    expect(rows![0].status).toBe('pending'); // C26: assert the status, not only the nulls
  });

  it('preserves a real 0 brier and 0 markout as 0 (PERFECT score, FLAT outcome)', async () => {
    const settled = { ...receiptAt('rcpt_002', 'settled'), brier: 0, chosen_markout_bps: 0 };
    stubFetch(async () => jsonResponse([settled], 200));
    const rows = await getTrialReceipts('t_9');
    expect(rows![0].brier).toBe(0);
    expect(rows![0].chosenMarkoutBps).toBe(0);
  });

  // `receipt_id` is the join key to the already-frozen verify route, and is what gives H5.3 the
  // eight Fair-Play checks per participant. Losing it silently would leave the card unverifiable.
  it('carries receiptId per element — the join key to the verify route', async () => {
    stubFetch(async () => jsonResponse([receiptAt('rcpt_a'), receiptAt('rcpt_b')], 200));
    const rows = await getTrialReceipts('t_9');
    expect(rows!.map((r) => r.receiptId)).toEqual(['rcpt_a', 'rcpt_b']);
  });
});

// ---------------------------------------------------------------------------
// FACT 2 — ordering is the BACKEND's guarantee (ascending receipt_id) and the
// client must relay it, never re-sort.
// ---------------------------------------------------------------------------
describe('getTrialReceipts never re-sorts client-side', () => {
  it('relays a contract-ordered response in the served order', async () => {
    const served = ['rcpt_001', 'rcpt_002', 'rcpt_003'];
    stubFetch(async () => jsonResponse(served.map((id) => receiptAt(id)), 200));
    const rows = await getTrialReceipts('t_9');
    expect(rows!.map((r) => r.receiptId)).toEqual(served);
  });

  // THE DISCRIMINATION CONTROL, and the reason the fixture above cannot stand alone (C66):
  // a client that sorted ascending would produce output IDENTICAL to the served order in the
  // test above, so that test cannot see a `.sort()` at all. Serving a NON-ascending array is
  // what makes the re-sort observable. The property under test is "this client does not
  // reorder", which belongs to the client and is testable regardless of what the backend
  // actually serves — the addendum's ascending guarantee is the backend's to keep.
  it('relays a NON-ascending response verbatim (dies the moment a .sort() is added)', async () => {
    const served = ['rcpt_009', 'rcpt_001', 'rcpt_005'];
    stubFetch(async () => jsonResponse(served.map((id) => receiptAt(id)), 200));
    const rows = await getTrialReceipts('t_9');
    expect(rows!.map((r) => r.receiptId)).toEqual(served);
    expect(rows!.map((r) => r.receiptId)).not.toEqual(['rcpt_001', 'rcpt_005', 'rcpt_009']);
  });
});

// ---------------------------------------------------------------------------
// FACTS 3, 4, 5 and the 404 — the four answers, each asserted against the
// other three. C52: every case is somebody else's discrimination control.
// ---------------------------------------------------------------------------
describe('getTrialReceipts: 200 [] is a REAL state, distinct from 404, 503 and 500', () => {
  // FACT 3. `[]` asserts that nobody has paid to commit on this trial. It is not an error and
  // not a loading state, so it must RESOLVE — and resolve to an array, not to null.
  it('200 [] resolves to an empty ARRAY — nobody paid to commit on this trial', async () => {
    stubFetch(async () => jsonResponse([], 200));
    const rows = await getTrialReceipts('t_9');
    expect(Array.isArray(rows)).toBe(true);
    expect(rows).toEqual([]);
    expect(rows).not.toBeNull(); // the discriminator against the 404 branch below
  });

  // FACT 4, acceptance control. A 503 must never reach a renderer as an empty set: `[]` is a
  // positive claim that nobody committed, and a deployment with no store mounted has no basis
  // for it. Throwing is what makes rendering it as `[]` impossible.
  it('503 participant_store_unavailable THROWS and never resolves to [] or null', async () => {
    stubFetch(async () => jsonResponse({ error: 'participant_store_unavailable' }, 503));
    await expect(getTrialReceipts('t_9')).rejects.toBeInstanceOf(SignalTrialsUnavailableError);
    await expect(getTrialReceipts('t_9')).rejects.toMatchObject({
      status: 503,
      code: 'participant_store_unavailable',
    });
  });

  // The exact wire string, pinned as a literal. The addendum records a naming caveat (QUALITY Q3,
  // carried not closed): the module elsewhere calls this object the *commit store*. If the backend
  // ever renames the code to `commit_store_unavailable`, the addendum must be revised FIRST — and
  // this assertion is the frontend's half of that gate.
  it('matches the wire code string exactly', () => {
    expect(SIGNAL_TRIALS_PARTICIPANT_STORE_UNAVAILABLE).toBe('participant_store_unavailable');
  });

  // FACT 4, discrimination control on the STRING. A 503 carrying a code we do not recognise is
  // still a refusal and still throws — but it is NOT the named contract state, because the
  // backend did not tell us the participant store is unmounted. Reporting it as the named state
  // would be claiming to know something we were never told; this dies if the match is loosened
  // to "any 503".
  it('a 503 carrying a DIFFERENT code is not reported as the named store-unavailable state', async () => {
    stubFetch(async () => jsonResponse({ error: 'commit_store_unavailable' }, 503));
    await expect(getTrialReceipts('t_9')).rejects.toBeInstanceOf(ApiError);
    await expect(getTrialReceipts('t_9')).rejects.not.toBeInstanceOf(SignalTrialsUnavailableError);
  });

  // A 503 with no parseable body at all — an intermediary, not the route. Still a refusal, still
  // never an empty set, still not the named state.
  it('a 503 with an unparseable body still throws rather than degrading to []', async () => {
    stubFetch(async () => new Response('<html>gateway</html>', { status: 503 }));
    await expect(getTrialReceipts('t_9')).rejects.toBeInstanceOf(ApiError);
    await expect(getTrialReceipts('t_9')).rejects.not.toBeInstanceOf(SignalTrialsUnavailableError);
  });

  // FACT 4, the reverse direction. A collapse either way kills exactly one test: the case above
  // dies if 503 degrades into `[]`, and this one dies if `200 []` is escalated into the
  // unavailability state.
  it('a 200 [] is NOT reported as the store-unavailable state', async () => {
    stubFetch(async () => jsonResponse([], 200));
    await expect(getTrialReceipts('t_9')).resolves.toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// FACT 5 — the CARRIED Q1 limitation, disclosed and NOT closed.
// ---------------------------------------------------------------------------
describe('getTrialReceipts: a 500 implies NOTHING about the trial that was asked for', () => {
  // Measured by H4.4 QUALITY: `store.finalized()` reads every finalized row and only then filters
  // by `trial_id`, so ONE unreadable row belonging to a DIFFERENT trial turns this request into a
  // 500 with the requested trial's own set fully intact. Availability is coupled store-wide.
  // The behaviour is a conservative refusal and never a false publication — which is why it
  // throws here rather than resolving to anything.
  it('500 THROWS and never degrades into [] or null', async () => {
    stubFetch(async () => new Response('boom', { status: 500 }));
    await expect(getTrialReceipts('t_9')).rejects.toBeInstanceOf(ApiError);
    await expect(getTrialReceipts('t_9')).rejects.toMatchObject({ status: 500 });
  });

  // ...and it is not the store-unavailable state either: a 500 is an unreadable row somewhere in
  // a store that IS mounted, which is a different claim from no store being mounted at all.
  it('500 is not reported as the store-unavailable state', async () => {
    stubFetch(async () => new Response('boom', { status: 500 }));
    await expect(getTrialReceipts('t_9')).rejects.not.toBeInstanceOf(SignalTrialsUnavailableError);
  });

  // The surfaced message must not assert a trial-specific fault. The corrupt row may belong to
  // any other trial in the store, so "this trial's data is corrupt" is a claim nothing supports.
  //
  // The message is read off the caught error DELIBERATELY. The obvious spelling —
  // `rejects.toThrow(expect.not.stringMatching(/corrupt/i))` — is VACUOUS: `toThrow` matches an
  // asymmetric matcher against the Error OBJECT, an Error is not a string, so `stringMatching` is
  // always false and `.not` therefore always passes. That spelling was written here first and
  // survived a mutation that put "this trial's data is corrupt" into the message verbatim.
  //
  // C66 — what this basis CANNOT express: a string guard cannot stop a SCREEN from composing that
  // sentence around a caught error. It pins only what this module itself says. The screen-level
  // obligation lands on H5.3 and is not testable from here.
  it('the surfaced message makes no claim that THIS trial is corrupt', async () => {
    stubFetch(async () => new Response('boom', { status: 500 }));
    const banned = /corrupt|damaged|tampered|unreadable/i;
    const err: unknown = await getTrialReceipts('t_9').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as Error).message).not.toMatch(banned);
    // C52, and the reason the assertion above is not vacuous: the predicate demonstrably fires on
    // the sentence it exists to forbid.
    expect("this trial's data is corrupt").toMatch(banned);
  });
});

// ---------------------------------------------------------------------------
// 404 — TWO different body shapes, one meaning.
// ---------------------------------------------------------------------------
describe('getTrialReceipts: an unknown trial is null, never an empty participant set', () => {
  // `null` rather than `[]` is load-bearing and is the same distinction the 503 draws: `[]` is a
  // positive claim that nobody committed on this trial, and a trial that does not exist has no
  // more basis for that claim than an unmounted store does.
  it('404 trial_not_found resolves to null, NOT to []', async () => {
    stubFetch(async () => errorResponse(404, 'trial_not_found'));
    const rows = await getTrialReceipts('t_9');
    expect(rows).toBeNull();
    expect(rows).not.toEqual([]); // the discriminator against the 200 [] branch
  });

  // The framework's 404 shape. Measured during H4.4: `encodeURIComponent` turns a `/` in a trial
  // id into `%2F`; uvicorn then UNQUOTES `raw_path` into `scope["path"]` (h11_impl.py:202,
  // httptools_impl.py:260) and Starlette routes on `scope["path"]`, never on `raw_path` — so the
  // id becomes extra path segments, matches NO route, and yields the framework's
  // `{"detail": "Not Found"}` instead of the route's `{"error": "trial_not_found"}`.
  //
  // This fetcher branches on the STATUS and never on an `error` key, so both shapes mean the same
  // thing to it. That is deliberate: a consumer branching on `error` would fail to recognise the
  // framework's shape and fall through to a throw, reporting an outage for what is really an
  // unaddressable trial id. "No such trial is reachable" is the true statement in both cases.
  it('the framework 404 {"detail":"Not Found"} resolves to null exactly as the route 404 does', async () => {
    stubFetch(async () => jsonResponse({ detail: 'Not Found' }, 404));
    await expect(getTrialReceipts('a/b')).resolves.toBeNull();
  });

  it('sends %2F for a slash-bearing trial id, which is what produces the framework 404', async () => {
    stubFetch(async () => jsonResponse({ detail: 'Not Found' }, 404));
    await getTrialReceipts('a/b');
    expect(String(calls()[0][0])).toContain('a%2Fb');
  });
});

// ---------------------------------------------------------------------------
// Non-conformance posture, applied uniformly: name the missing shape, never
// normalise it, and never let it surface as an anonymous TypeError.
// ---------------------------------------------------------------------------
describe('getTrialReceipts fails closed on a non-array 200', () => {
  // The contract body IS the bare array. An enveloped `{"receipts": [...]}` is a non-conforming
  // response, and `.map` on it would throw an anonymous TypeError naming `map` — a message that
  // sends a debugger to the client rather than to the response shape.
  it('a 200 carrying an enveloped object throws a NAMED error, not an anonymous TypeError', async () => {
    stubFetch(async () => jsonResponse({ receipts: [receiptAt('rcpt_001')] }, 200));
    await expect(getTrialReceipts('t_9')).rejects.toThrow(/array/i);
    await expect(getTrialReceipts('t_9')).rejects.not.toBeInstanceOf(TypeError);
  });

  // The acceptance control (C52): a bare array — including the empty one — is conforming and
  // must not trip the guard.
  it('a bare array 200 does not trip the guard', async () => {
    stubFetch(async () => jsonResponse([], 200));
    await expect(getTrialReceipts('t_9')).resolves.toEqual([]);
  });
});

// ===========================================================================
// H5.5 — the public `SKILL.md`, and the image that must actually carry it.
// ===========================================================================
//
// WHY THESE TESTS LIVE IN THE ADAPTER'S TEST FILE and not beside the document: the endpoint list
// below is DERIVED from `SIGNAL_TRIALS_PATHS`, the same constant every fetcher above binds to. A
// hand-retyped list would be a second spelling of the routes with nothing able to notice a drift
// between them — precisely the CF-5 failure the check-keys constant exists to prevent. Deriving it
// here means a route rename in `lib/signal-trials-api.ts` breaks the DOCUMENT'S test, which is the
// only mechanism that keeps the public onboarding artifact honest about the API it describes.
//
// C64 IS THE REASON THE DOCKERFILE IS ASSERTED AT ALL. `apps/web/public/SKILL.md` existing in the
// source tree is NOT acceptance: `pnpm test`, `pnpm build` and `docker build` all pass while the
// deployed `/SKILL.md` 404s, because the runner stage copies `.next` and never `public`. Every gate
// this task has is a SOURCE-TREE gate; the 404 appears only when something requests the file from
// the running image, and the first thing to do that is the H6.0 Step 0 smoke at the eligibility
// clock. So the second describe below asserts the COPY is active, and — per C66 — proves the
// assertion discriminates by re-commenting the line and watching it fail.

const SKILL_MD = readFileSync(resolve(__dirname, '../public/SKILL.md'), 'utf8');
const DOCKERFILE = readFileSync(resolve(__dirname, '../Dockerfile'), 'utf8');

// A URI-safe stand-in for a path parameter. `SIGNAL_TRIALS_PATHS` runs `encodeURIComponent` over
// its argument, so `{id}` would arrive as `%7Bid%7D`; an alphanumeric sentinel passes through
// untouched and is then swapped for the placeholder spelling the document uses.
const PATH_PARAM = 'PARAM';

// THE SEVEN ENDPOINTS `SKILL.md` MUST NAME — and the count is the third one this task was given.
//
// The frozen plan (L1061) says the test asserts the file "names the FIVE endpoints". Integration
// enumerated the same plan's Step-1 prose and found SIX distinct paths, ruling that all six are
// named because omitting a real endpoint to match a miscount is the worse error.
//
// Deriving the six from `SIGNAL_TRIALS_PATHS` rather than retyping them then surfaced a SEVENTH:
// `trialReceipts`, added by the route-contract addendum AFTER the plan froze. So "the packet's six"
// and "the client's path map" were never the same set. That discrepancy was escalated rather than
// silently included or omitted, and it changed the answer — the lane controller ruled the seventh
// IN, on a deployment-unit argument worth recording because it turns on a fact the count alone
// cannot settle: `TrialMatchCard.tsx:228` CALLS `getTrialReceipts`, so H5.3 — a NEVER CUT screen —
// has a hard runtime dependency on this route. Canonical must therefore advance to include it
// before H6.0 deploys, so the route WILL be in the deployed image and documenting it is correct.
// Documenting a route that was NOT going to ship would have been C64's own failure turned on
// SKILL.md: a judge following the document into a 404.
//
// SIX of the seven are derived from `SIGNAL_TRIALS_PATHS`. `POST /signal-trials/commit` is a
// LITERAL and cannot be derived: the path map belongs to a READ client that never calls the paid
// route (see `getOpenTrial`'s note on the two 503s it therefore never sees), so no entry exists to
// derive from. That is a disclosed gap in the binding, not an oversight — the commit path is the
// one route here whose spelling this test cannot keep in step with the client automatically.
const SKILL_MD_ENDPOINTS: readonly { readonly method: string; readonly path: string }[] = [
  { method: 'GET', path: SIGNAL_TRIALS_PATHS.openTrial() },
  { method: 'GET', path: SIGNAL_TRIALS_PATHS.season() },
  { method: 'POST', path: '/signal-trials/commit' },
  { method: 'GET', path: SIGNAL_TRIALS_PATHS.trial(PATH_PARAM).replace(PATH_PARAM, '{id}') },
  { method: 'GET', path: SIGNAL_TRIALS_PATHS.trialReceipts(PATH_PARAM).replace(PATH_PARAM, '{id}') },
  { method: 'GET', path: SIGNAL_TRIALS_PATHS.agent(PATH_PARAM).replace(PATH_PARAM, '{payer}') },
  { method: 'GET', path: SIGNAL_TRIALS_PATHS.verifyReceipt(PATH_PARAM).replace(PATH_PARAM, '{id}') },
];

function endpointLine(e: { method: string; path: string }) {
  return `${e.method} ${e.path}`;
}

describe('public SKILL.md names the API surface it claims to document', () => {
  it('exists and carries content — the file the deploy smoke will request', () => {
    expect(SKILL_MD.trim().length).toBeGreaterThan(0);
  });

  // SIX, not five. One assertion per endpoint so a drop is attributable to the endpoint dropped
  // rather than to "the list changed".
  it.each(SKILL_MD_ENDPOINTS.map((e) => [endpointLine(e)] as const))(
    'names %s',
    (line) => {
      expect(SKILL_MD).toContain(line);
    },
  );

  // C66 — the discrimination control for the predicate above, and the direct answer to "would this
  // fail if an endpoint were dropped?". Each endpoint's line is DELETED from a copy of the document
  // and the same predicate is re-run: it must fail. A `toContain` over a document that happens to
  // mention a path proves nothing on its own; this is what makes the six load-bearing.
  it.each(SKILL_MD_ENDPOINTS.map((e) => [endpointLine(e)] as const))(
    'a document with %s removed FAILS the same predicate',
    (line) => {
      const without = SKILL_MD.split(line).join('');
      expect(without).not.toEqual(SKILL_MD); // the mutation demonstrably landed
      expect(without).not.toContain(line);
    },
  );

  // Plan Step 1's commit terms. Split per term for the same attributability reason.
  it('states the $0.01 price', () => {
    expect(SKILL_MD).toContain('$0.01');
  });

  it('states the 300-second decision window', () => {
    expect(SKILL_MD).toMatch(/300\s*seconds/);
  });

  it('states the commit body fields by their wire names', () => {
    expect(SKILL_MD).toContain('trial_id');
    expect(SKILL_MD).toContain('p_follow_profitable');
  });

  it('states that paid commits are live-only', () => {
    expect(SKILL_MD).toMatch(/live[- ]only/i);
  });

  // The honest empty state on discovery. A caller that reads a 404 here as an outage will retry a
  // quiet period forever, which is why the code is named in the document rather than implied.
  it('names the no_open_trial 404 as an honest state', () => {
    expect(SKILL_MD).toContain('no_open_trial');
  });
});

// ---------------------------------------------------------------------------
// The language guard. This is the most externally-read artifact in the build.
// ---------------------------------------------------------------------------

// The §3.8 listing sentence, VERBATIM from `authority/frozen-spec.md`. It is quoted here rather
// than paraphrased because H6.0 Step 4 submits this exact copy to the marketplace listing.
const SECTION_3_8 =
  'Veridex provides reproducible agent benchmarking and auditable calibration records from frozen ' +
  'market evidence. It does not execute trades, provide personalized investment advice, or claim ' +
  'proven alpha.';

// C44: the word list is the SEARCH SPACE and the banned trading SENSE is the discriminator.
const BANNED_TRADING_WORDS = /\b(verified|edges?|fills?|filled|PnL|proven|profit\w*|ROI|positions?|alpha)\b/gi;

// The two exemptions, each an ordinary-language or wire-name use rather than a trading claim.
// Stripped BEFORE the predicate runs so the predicate itself stays blunt and unarguable.
//
//   * `p_follow_profitable` / `follow_profitable` — REQUIRED wire field names. A document that
//     cannot spell the field a caller must POST is unusable, and the field name asserts nothing
//     about anyone having made money.
//   * the §3.8 sentence — every banned word in it ("proven alpha") appears under NEGATION. It is
//     the frozen listing copy and is the strongest honesty statement in the file, so a grep that
//     failed the document for containing its own disclaimer would be the instrument misfiring.
function strippedOfExemptions(text: string): string {
  return text.split('p_follow_profitable').join('').split('follow_profitable').join('').split(SECTION_3_8).join('');
}

describe('SKILL.md language guard — paper-markout only, no trading claim', () => {
  it('carries the §3.8 listing sentence verbatim', () => {
    expect(SKILL_MD).toContain(SECTION_3_8);
  });

  // C46 — LABELLED PIN. This was GREEN against the empty placeholder that produced the captured
  // RED, because an empty document contains no banned word. Its RED is UNOBTAINABLE in the units of
  // the claim: the only document that fails it is one that already carries the violation, and
  // writing a violation in order to watch the test catch it would then have to be un-written. Its
  // discrimination is supplied instead by the two paired controls below, which run the SAME
  // predicate over text that must trip it. It DID catch a real hit during authoring: an earlier
  // draft read "never filled in", and `filled` is in the search space. Per C44 that was an
  // ordinary-English homonym rather than the banned trading sense — a false positive of the
  // instrument — and it was resolved by rewording to "never invented" rather than by adding a third
  // exemption, so the predicate stays blunt for the next editor.
  it('contains no banned trading word outside the two disclosed exemptions', () => {
    expect(strippedOfExemptions(SKILL_MD).match(BANNED_TRADING_WORDS)).toBeNull();
  });

  // C52/C46 — a discrimination CONTROL, green by construction and by design. The predicate above is
  // worth nothing unless this demonstrates it fires on the sentence it exists to forbid.
  it('the banned-word predicate fires on a real trading claim', () => {
    expect(strippedOfExemptions('this agent made a 12% profit on a filled position')).toMatch(
      BANNED_TRADING_WORDS,
    );
  });

  // C52/C46 — the second control, also green by construction. The exemption is SCOPED, not a hole:
  // the field NAME is stripped, a profit CLAIM standing next to it still trips the predicate.
  it('the exemption does not license a profit claim that merely sits next to the field name', () => {
    expect(strippedOfExemptions('p_follow_profitable — how much profit you made')).toMatch(
      BANNED_TRADING_WORDS,
    );
  });

  it('uses the one permitted markout label, spelled from the adapter constant', () => {
    expect(SKILL_MD).toContain(SIGNAL_TRIALS_MARKOUT_LABEL);
  });

  it('states that qualified is false on live records', () => {
    expect(SKILL_MD).toMatch(/`qualified`[^.]*`false`[^.]*live/i);
  });
});

// The binding from the H4.3 milestone Codex: the eight checks are REPRODUCIBILITY CHECKS OVER
// RECORDED EVIDENCE. They re-derive a receipt's claims from the stored artifacts. That is NOT proof
// against a malicious storage operator, and no copy in this program may say otherwise — least of
// all the file a judge reads first.
const STORAGE_OVERCLAIM =
  /\btamper[-\s]?(proof|evident|resistant)\b|\bimmutab\w*|\bcannot be (altered|changed|edited|tampered)\b/i;

describe('SKILL.md describes verification as reproducibility, never as tamper-proofing', () => {
  // `\s+` rather than a literal space, and `[^.]` rather than `.`, so the assertion survives the
  // document being re-wrapped. A copy test that breaks when a paragraph reflows trains an editor to
  // weaken the test instead of fixing the copy, which is the opposite of what it is for.
  it('makes the reproducibility-over-recorded-evidence claim explicitly', () => {
    expect(SKILL_MD).toMatch(/reproducibility[^.]*recorded\s+evidence/i);
  });

  // C46 — LABELLED PIN, same shape as the banned-word assertion above and for the same reason: it
  // was green against the empty placeholder, and its RED is unobtainable without first writing the
  // overclaim it forbids. The three controls beneath it carry the discrimination.
  it('makes no tamper-proofing or immutability claim', () => {
    expect(SKILL_MD).not.toMatch(STORAGE_OVERCLAIM);
  });

  // C52/C46 — controls, green by construction: the overclaim predicate demonstrably fires on each
  // of the three claims the H4.3 milestone Codex forbids this program from making.
  it.each([
    'receipts are tamper-proof',
    'the record is immutable once written',
    'a published outcome cannot be altered',
  ])('the overclaim predicate fires on %s', (claim) => {
    expect(claim).toMatch(STORAGE_OVERCLAIM);
  });
});

// ---------------------------------------------------------------------------
// C64 — the file must reach the IMAGE, not merely the source tree.
// ---------------------------------------------------------------------------

// An ACTIVE `COPY` of `public` into the runner stage. Anchored to the start of a line and allowing
// only whitespace before `COPY`, so a `#` in front of it does NOT match — which is the entire point
// of the assertion. `[^#\n]*` after COPY keeps a trailing-comment form from matching a commented
// line that happens to contain the word COPY later on.
const ACTIVE_PUBLIC_COPY = /^[ \t]*COPY[^#\n]*\/app\/public\s+\.\/public[ \t]*$/m;

describe('C64: the Dockerfile actually ships public/ — a source-tree file is not acceptance', () => {
  // The defect this exists for: `pnpm test`, `pnpm build` and `docker build` all pass, the
  // container starts, `/readyz` is green — and `https://proofarena.xyz/SKILL.md` 404s, because
  // `next start` serves `public` from the image and the runner stage never copied it.
  it('carries an ACTIVE, uncommented COPY of public into the runner stage', () => {
    expect(DOCKERFILE).toMatch(ACTIVE_PUBLIC_COPY);
  });

  // C66, and the direct answer to "would this fail if someone re-commented the COPY?". The real
  // Dockerfile text is mutated — the matched line is commented back out — and the same predicate is
  // re-run against the mutant. The inequality assertion proves the mutant actually loaded (C41):
  // without it, a regex that silently matched nothing would pass this test by doing nothing.
  it('FAILS when that COPY is commented back out', () => {
    const recommented = DOCKERFILE.replace(ACTIVE_PUBLIC_COPY, (line) => `# ${line.trim()}`);
    expect(recommented).not.toEqual(DOCKERFILE);
    expect(recommented).not.toMatch(ACTIVE_PUBLIC_COPY);
  });

  // C52/C46 — a control, green by construction. The predicate must not be satisfiable by the
  // COMMENT that stood in this file before H5.5. Reconstructing that exact prior line and asserting
  // it fails is what separates "an active COPY exists" from "the word COPY appears near public".
  it('is not satisfied by the pre-H5.5 commented placeholder', () => {
    expect('# COPY --from=build --chown=node:node /app/public ./public').not.toMatch(ACTIVE_PUBLIC_COPY);
  });

  // The pairing C64 makes an acceptance criterion, asserted as one fact: the COPY without the file
  // breaks the build on a missing directory, and the file without the COPY ships a 404 at the
  // eligibility gate. Neither half is allowed to land alone.
  it('ships the COPY and the file together', () => {
    expect(DOCKERFILE).toMatch(ACTIVE_PUBLIC_COPY);
    expect(SKILL_MD.trim().length).toBeGreaterThan(0);
  });
});
