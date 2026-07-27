// H5.3 — the Trial Match Card at /trials/[trialId]. NEVER CUT, and the most public artifact here.
//
// EVERY render below is driven through the REAL adapters (`getTrial`, `getTrialReceipts`,
// `verifyReceipt`) with only the FETCH layer stubbed — the H5.2 precedent. No test supplies a view
// model directly, so these assertions depend on H5.1/H5.2-A's behaviour rather than restating it.
//
// THE TRAPS THIS FILE EXISTS TO SPRING:
//
// C26 — `pending` and `UNSCORED` carry IDENTICAL null metrics. Only `status` separates them. Every
// settlement predicate below therefore asserts the STATUS plus the ABSENCE of the other label's
// distinguishing copy, in BOTH directions, over outcomes that are byte-identical except for
// `status`. A test asserting only the nulls passes against a card that collapsed the two.
//
// A FOURTH state hides next to those three: `outcome: null` means NOTHING was computed at all,
// which is a WEAKER statement than a recorded `pending`. It gets its own directional pair.
//
// THE ACTION IS ON THE WIRE. The card must RENDER `receipt.action`, never re-derive it from
// `p_follow_profitable`. The fixture below carries a receipt with p = 0.91 and action ABSTAIN —
// a combination no client-side band could produce — so a card that re-derived the bands renders
// FOLLOW there and dies. That is the discrimination control for element 2 (C52).
//
// THE Q1 LIMITATION IS A UI REQUIREMENT. Any unreadable finalized row anywhere in the store fails
// `getTrialReceipts` for EVERY trial, so a 500 from that route implies NOTHING about the trial
// being viewed. The card must never say this trial's data is corrupt, and the rest of the card —
// which comes from `getTrial()`, a DIFFERENT call — must survive intact. Both are asserted.
//
// THREE PARTICIPANT ANSWERS, NONE INTERCHANGEABLE: `200 []` is a real state meaning nobody paid to
// commit; `503 participant_store_unavailable` is NOT an empty set; a throw is neither. Each is
// pinned positively AND against the other two's copy.
//
// C66 — the fixtures VARY along every dimension that is meant to be pinned: three settlement
// states plus a null outcome; four receipt statuses; a real 0 brier and a real 0 markout beside
// null ones; FOLLOW, FADE and ABSTAIN actions; a served check `fail`, a served `pending`, and an
// ABSENT key. What this basis CANNOT express is stated at the bottom of this file.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  SIGNAL_TRIALS_CHECK_KEYS,
  SIGNAL_TRIALS_MARKOUT_LABEL,
  SIGNAL_TRIALS_PARTICIPANT_STORE_UNAVAILABLE,
} from '@/lib/signal-trials-api';
import type * as W from '@/lib/wire';
import { TrialMatchCard, TRIAL_SETTLEMENT_HORIZON_MS } from './TrialMatchCard';
import TrialPage from '@/app/(app)/trials/[trialId]/page';

// next/navigation is globally mocked in vitest.setup.ts WITHOUT `useParams`, which the route page
// needs. Re-mocked here rather than widened globally: the trial id this page reads is the one thing
// the page contributes, so it belongs to this file's fixture surface. `vi.mock` is hoisted above
// the imports above, so the page picks this up despite the source order.
vi.mock('next/navigation', () => ({
  useParams: () => ({ trialId: 'trial-0k9f2c' }),
  usePathname: () => '/trials/trial-0k9f2c',
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

// Same idiom as SeasonScreen.test.tsx: read the frozen contract fixture from the repo root at test
// time so this screen cannot drift from the backend's wire shape.
const FIX = resolve(__dirname, '../../../../../contracts/fixtures');
const trialWire = JSON.parse(
  readFileSync(resolve(FIX, 'signal_trials_trial.json'), 'utf8'),
) as W.TrialWire;

const TRIAL_ID = trialWire.trial_id;

beforeEach(() => { vi.restoreAllMocks(); });
afterEach(() => { vi.unstubAllGlobals(); });

// ---------------------------------------------------------------------------
// wire builders
// ---------------------------------------------------------------------------

function errorResponse(status: number, code: string) {
  return new Response(JSON.stringify({ error: code }), { status });
}
function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

// The three recorded outcomes, IDENTICAL except for `status` and the settled-only metrics.
// `pending` and `UNSCORED` below are byte-identical apart from the one label — which is exactly the
// C26 basis: no assertion over the metrics alone can tell them apart.
const settledOutcome = trialWire.outcome as W.TrialOutcomeWire;
function nullMetricOutcome(status: 'pending' | 'UNSCORED'): W.TrialOutcomeWire {
  return {
    trial_id: TRIAL_ID,
    status,
    entry: settledOutcome.entry, // NON-nullable — survives on every status
    future: null,
    close_ts_ms: null,
    observation_lag_ms: null,
    follow_markout_bps: null,
    fade_markout_bps: null,
    follow_profitable: null,
  };
}

function trial(outcome: W.TrialOutcomeWire | null): W.TrialWire {
  return { ...trialWire, outcome };
}

function receiptWire(over: Partial<W.CommitReceiptWire> = {}): W.CommitReceiptWire {
  return {
    receipt_id: 'rcpt_1',
    trial_id: TRIAL_ID,
    payer: '0xaaa',
    p_follow_profitable: 0.72,
    methodology_version: 'sig_trials@1.0.0',
    action: 'FOLLOW',
    status: 'settled',
    brier: 0.08,
    chosen_markout_bps: 41,
    committed_at_ms: 1_700_000_100_000,
    commit_deadline_ms: 1_700_000_300_000,
    trial_mode: 'live',
    body_hash: 'bh_1',
    payment_tx_hash: 'tx_1',
    ...over,
  };
}

// THE PARTICIPANT SET, varying along every axis the card claims to render (C66):
//   rcpt_1  FOLLOW  settled   brier 0.08, markout  41   — an ordinary settled row
//   rcpt_2  FADE    settled   brier 0,    markout  0     — REAL values: a PERFECT score, a FLAT outcome
//   rcpt_3  ABSTAIN pending   p = 0.91                   — p and action DISAGREE: the re-derivation trap
//   rcpt_4  FOLLOW  UNSCORED  identical nulls to rcpt_3  — C26 at the participant surface
const RECEIPTS: W.CommitReceiptWire[] = [
  receiptWire(),
  receiptWire({
    receipt_id: 'rcpt_2', payer: '0xbbb', action: 'FADE', status: 'settled',
    p_follow_profitable: 0.31, brier: 0, chosen_markout_bps: 0, body_hash: 'bh_2', payment_tx_hash: 'tx_2',
  }),
  receiptWire({
    receipt_id: 'rcpt_3', payer: '0xccc', action: 'ABSTAIN', status: 'pending',
    // 0.91 is far ABOVE the FOLLOW band. A card that re-derived the action from p renders FOLLOW
    // here; the wire says ABSTAIN and the wire is what must render.
    p_follow_profitable: 0.91, brier: null, chosen_markout_bps: null,
    body_hash: 'bh_3', payment_tx_hash: 'tx_3',
  }),
  receiptWire({
    receipt_id: 'rcpt_4', payer: '0xddd', action: 'FOLLOW', status: 'UNSCORED',
    p_follow_profitable: 0.91, brier: null, chosen_markout_bps: null,
    body_hash: 'bh_4', payment_tx_hash: 'tx_4',
  }),
];

// Eight served verdicts: four commit-time `pass`, four outcome-time `pending`.
const ALL_EIGHT: Record<string, W.SignalTrialsCheckStatusWire> = {
  body_hash: 'pass', manifest: 'pass', deadline_respected: 'pass', live_mode: 'pass',
  bar_version: 'pending', law_version: 'pending', evidence_equality: 'pending', outcome_source: 'pending',
};

// EIGHT `pass` — the ONLY input that can produce a forbidden aggregate badge.
//
// The design forbids the aggregate badge BY NAME, but a badge gated on `checks.every(pass)` is
// invisible to every other fixture in this file: `ALL_EIGHT` above is four pass + four pending,
// and every other verify fixture derives from it. A pure absence assertion over those inputs is
// unfalsifiable — the forbidden branch is never reachable, so the guard cannot fail no matter what
// the card does. Built off the frozen constant rather than spelled out, so it cannot drift from
// the eight keys the card actually renders (CF-5).
const ALL_PASS: Record<string, W.SignalTrialsCheckStatusWire> =
  Object.fromEntries(SIGNAL_TRIALS_CHECK_KEYS.map((c) => [c.key, 'pass' as const]));

function verifyWire(
  receiptId: string,
  checks: Record<string, W.SignalTrialsCheckStatusWire> = ALL_EIGHT,
  receipt: W.CommitReceiptWire | null = null,
): W.VerifyReceiptWire {
  return { receipt_id: receiptId, checks, receipt };
}

// ---------------------------------------------------------------------------
// fetch stub
// ---------------------------------------------------------------------------
// Route discrimination is by SUFFIX and it is load-bearing: `/signal-trials/trials/{id}/receipts`
// has `/signal-trials/trials/{id}` as a strict prefix, and `/signal-trials/receipts/{rid}/verify`
// also contains the substring `/receipts`. A naive `includes()` chain routes the card's calls to
// the wrong stub and the whole file becomes meaningless.
type Route = (url: string) => Response;
interface Routes {
  trial?: Route;
  receipts?: Route;
  verify?: Route;
}
function stubRoutes(routes: Routes) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const pick = (r: Route | undefined, name: string) => {
      if (!r) throw new Error(`test served no ${name} route, but the card fetched ${url}`);
      return r(url);
    };
    if (url.endsWith('/verify')) return pick(routes.verify, 'verify');
    if (url.endsWith('/receipts')) return pick(routes.receipts, 'receipts');
    if (url.includes('/signal-trials/trials/')) return pick(routes.trial, 'trial');
    throw new Error(`unexpected fetch in TrialMatchCard test: ${url}`);
  }) as unknown as typeof fetch);
}

const OK_RECEIPTS: Route = () => jsonResponse(RECEIPTS);
const OK_VERIFY: Route = (url) => {
  const id = decodeURIComponent(url.split('/signal-trials/receipts/')[1]?.split('/')[0] ?? '');
  return jsonResponse(verifyWire(id));
};

// Render and wait for the trial panel to settle out of `loading`. Every non-loading assertion goes
// through here so nothing accidentally runs against the skeleton.
async function card(over: Routes = {}) {
  stubRoutes({ trial: () => jsonResponse(trial(settledOutcome)), receipts: OK_RECEIPTS, verify: OK_VERIFY, ...over });
  render(<TrialMatchCard trialId={TRIAL_ID} />);
  const el = await screen.findByTestId('trial-panel');
  await waitFor(() => expect(el).not.toHaveAttribute('data-state', 'loading'));
  return el;
}

// Wait for the participant section to settle too — it is a SEPARATE failure domain from the trial
// section and resolves independently, so a test about participants must wait on its own panel.
async function participants(over: Routes = {}) {
  await card(over);
  const el = await screen.findByTestId('participants-panel');
  await waitFor(() => expect(el).not.toHaveAttribute('data-state', 'loading'));
  return el;
}

// ---------------------------------------------------------------------------
// GROUP A — the evidence hash chip (element 1)
// ---------------------------------------------------------------------------
describe('H5.3 evidence hash chip', () => {
  it('renders TrialCard.evidenceHash verbatim, not a truncation the card invented', async () => {
    await card();
    expect(screen.getByTestId('trial-evidence-hash')).toHaveTextContent(trialWire.evidence_hash);
  });
});

// ---------------------------------------------------------------------------
// GROUP B — the settlement panel, ALL FOUR states (element 3 + C26)
// ---------------------------------------------------------------------------
// Each predicate asserts the STATUS and the ABSENCE of the neighbouring state's distinguishing
// copy. The `pending` and `UNSCORED` bodies are served outcomes that differ in ONE field.
describe('H5.3 settlement panel — pending, settled, UNSCORED and "nothing computed" are four states', () => {
  it('settled: reports the status and the observation lag, and never the pending or UNSCORED copy', async () => {
    await card();
    const panel = screen.getByTestId('settlement-panel');
    expect(panel).toHaveAttribute('data-status', 'settled');
    expect(panel).toHaveTextContent(String(settledOutcome.observation_lag_ms));
    expect(panel.textContent).not.toMatch(/no completed settlement candle/i);
    expect(panel.textContent).not.toMatch(/the result is not known/i);
  });

  it('pending: reports status "pending" and NEVER the UNSCORED title (dies if the two collapse)', async () => {
    await card({ trial: () => jsonResponse(trial(nullMetricOutcome('pending'))) });
    const panel = screen.getByTestId('settlement-panel');
    expect(panel).toHaveAttribute('data-status', 'pending');
    expect(panel.textContent).toMatch(/the result is not known/i);
    // The discrimination control. The metrics are identical to the UNSCORED case below, so this
    // negative is the ONLY thing separating them.
    expect(panel.textContent).not.toMatch(/no completed settlement candle/i);
    expect(panel.textContent).not.toMatch(/no value was interpolated/i);
  });

  it('UNSCORED: reports status "UNSCORED" and NEVER the pending copy (dies if the two collapse)', async () => {
    await card({ trial: () => jsonResponse(trial(nullMetricOutcome('UNSCORED'))) });
    const panel = screen.getByTestId('settlement-panel');
    expect(panel).toHaveAttribute('data-status', 'UNSCORED');
    expect(panel.textContent).toMatch(/UNSCORED — no completed settlement candle/);
    expect(panel.textContent).toMatch(/no value was interpolated/i);
    expect(panel.textContent).not.toMatch(/the result is not known/i);
  });

  // C46 — PIN. This one was GREEN the instant it was written and touches no component code. It is
  // kept deliberately: it is the PROOF OF BASIS for every C26 predicate above. If it ever fails,
  // the `pending`/`UNSCORED` pair stopped being identical and those predicates stopped being
  // discriminating for the reason they claim.
  it('PIN (basis proof): pending and UNSCORED are served BYTE-IDENTICAL metrics — only `status` differs', () => {
    const p = { ...nullMetricOutcome('pending'), status: 'X' };
    const u = { ...nullMetricOutcome('UNSCORED'), status: 'X' };
    // Proves the basis: any predicate that separated the two renders read `status` to do it.
    expect(p).toEqual(u);
  });

  it('outcome: null is "nothing was computed at all" — never rendered as pending or UNSCORED', async () => {
    await card({ trial: () => jsonResponse(trial(null)) });
    const panel = screen.getByTestId('settlement-panel');
    expect(panel).toHaveAttribute('data-status', 'none');
    expect(panel.textContent).toMatch(/no outcome record/i);
    expect(panel.textContent).not.toMatch(/the result is not known/i);
    expect(panel.textContent).not.toMatch(/no completed settlement candle/i);
  });

  it('renders a REAL 0 markout as 0 and a null markout as an em dash — never the other way round', async () => {
    await card({
      trial: () => jsonResponse(trial({ ...settledOutcome, follow_markout_bps: 0, fade_markout_bps: null })),
    });
    // 0 bps is a real FLAT outcome; null is "nothing settled". `??` or `||` erases the first.
    expect(screen.getByTestId('settlement-follow-markout')).toHaveTextContent(/0/);
    expect(screen.getByTestId('settlement-fade-markout')).toHaveTextContent('—');
    expect(screen.getByTestId('settlement-fade-markout').textContent).not.toMatch(/\d/);
  });
});

// ---------------------------------------------------------------------------
// GROUP C — the pending countdown
// ---------------------------------------------------------------------------
describe('H5.3 pending countdown', () => {
  it('counts down to t0 + the published settlement horizon and labels the target as DERIVED', async () => {
    // Date.now is spied rather than faked so the component's interval keeps real semantics.
    vi.spyOn(Date, 'now').mockReturnValue(trialWire.t0_ms + TRIAL_SETTLEMENT_HORIZON_MS - 90_000);
    await card({ trial: () => jsonResponse(trial(nullMetricOutcome('pending'))) });
    const cd = screen.getByTestId('settlement-countdown');
    expect(cd).toHaveTextContent('1m 30s');
    // The target is not a served field. Saying so is the difference between a derived display and
    // a fabricated one.
    expect(screen.getByTestId('settlement-panel').textContent).toMatch(/derived from the published/i);
  });

  it('never renders a negative countdown once the horizon has elapsed, and still says pending', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(trialWire.t0_ms + TRIAL_SETTLEMENT_HORIZON_MS + 600_000);
    await card({ trial: () => jsonResponse(trial(nullMetricOutcome('pending'))) });
    const cd = screen.getByTestId('settlement-countdown');
    expect(cd.textContent).not.toMatch(/-/);
    expect(cd).toHaveTextContent(/elapsed/i);
    // A trial stays pending until T + bar + grace. An elapsed horizon is NOT UNSCORED.
    expect(screen.getByTestId('settlement-panel')).toHaveAttribute('data-status', 'pending');
    expect(screen.getByTestId('settlement-panel').textContent).not.toMatch(/no completed settlement candle/i);
  });
});

// ---------------------------------------------------------------------------
// GROUP D — participants (element 2) and the THREE non-interchangeable answers
// ---------------------------------------------------------------------------
describe('H5.3 participants — the action is on the wire and is never re-derived', () => {
  it('renders every participant with its payer, p_follow_profitable and SERVED action', async () => {
    const panel = await participants();
    expect(panel).toHaveAttribute('data-state', 'list');
    const rows = within(panel).getAllByTestId('participant-row');
    expect(rows).toHaveLength(RECEIPTS.length);
    expect(rows.map((r) => within(r).getByTestId('participant-payer').textContent))
      .toEqual(RECEIPTS.map((r) => r.payer)); // served order, verbatim — the client never re-sorts
    expect(within(rows[0]).getByTestId('participant-p')).toHaveTextContent('0.72');
  });

  it('renders ABSTAIN for a receipt whose p is 0.91 — the wire action, not a client-side band', async () => {
    const panel = await participants();
    const row = within(panel).getAllByTestId('participant-row')[2];
    // THE discrimination control (C52). p = 0.91 is deep in the FOLLOW band, so a card that
    // re-derived the action renders FOLLOW here and this dies.
    expect(within(row).getByTestId('participant-action')).toHaveTextContent('ABSTAIN');
    expect(within(row).getByTestId('participant-action').textContent).not.toMatch(/FOLLOW/);
  });

  it('renders a REAL 0 brier and 0 markout as 0, and null ones as an em dash', async () => {
    const panel = await participants();
    const rows = within(panel).getAllByTestId('participant-row');
    expect(within(rows[1]).getByTestId('participant-brier')).toHaveTextContent(/0/);
    expect(within(rows[1]).getByTestId('participant-markout')).toHaveTextContent(/0/);
    expect(within(rows[2]).getByTestId('participant-brier')).toHaveTextContent('—');
    expect(within(rows[2]).getByTestId('participant-brier').textContent).not.toMatch(/\d/);
  });

  it('keeps a pending participant and an UNSCORED one apart despite identical null metrics', async () => {
    const panel = await participants();
    const rows = within(panel).getAllByTestId('participant-row');
    expect(within(rows[2]).getByTestId('participant-status')).toHaveTextContent('pending');
    expect(within(rows[2]).getByTestId('participant-status').textContent).not.toMatch(/UNSCORED/);
    expect(within(rows[3]).getByTestId('participant-status')).toHaveTextContent('UNSCORED');
    expect(within(rows[3]).getByTestId('participant-status').textContent).not.toMatch(/pending/);
  });

  it('states that every participant shown paid for the commitment (finalized-only visibility)', async () => {
    const panel = await participants();
    expect(panel.textContent).toMatch(/paid/i);
  });
});

describe('H5.3 participants — three answers, none interchangeable', () => {
  it('200 [] is a REAL state: nobody paid to commit — never an error and never a loading state', async () => {
    const panel = await participants({ receipts: () => jsonResponse([]) });
    expect(panel).toHaveAttribute('data-state', 'empty');
    expect(panel.textContent).toMatch(/no .*commitment/i);
    // Discrimination against the OTHER two answers' copy.
    expect(panel.textContent).not.toMatch(/unavailable/i);
    expect(within(panel).queryAllByTestId('participant-row')).toHaveLength(0);
  });

  it('503 participant_store_unavailable is NOT an empty set', async () => {
    const panel = await participants({
      receipts: () => errorResponse(503, SIGNAL_TRIALS_PARTICIPANT_STORE_UNAVAILABLE),
    });
    expect(panel).toHaveAttribute('data-state', 'store_unavailable');
    expect(panel.textContent).toMatch(/unavailable/i);
    // The positive claim `[]` would make must be absent: nothing here supports "nobody committed".
    expect(panel.textContent).not.toMatch(/nobody/i);
    expect(panel.textContent).toContain(SIGNAL_TRIALS_PARTICIPANT_STORE_UNAVAILABLE);
  });

  it('a 500 leaves the REST of the card intact and never blames this trial (Q1)', async () => {
    const panel = await participants({ receipts: () => jsonResponse({ error: 'boom' }, 500) });
    expect(panel).toHaveAttribute('data-state', 'failed');
    // A 500 from that route implies NOTHING about the trial being viewed.
    expect(panel.textContent).not.toMatch(/corrupt/i);
    expect(panel.textContent).not.toMatch(/this trial'?s data/i);
    expect(panel.textContent).toMatch(/says nothing about this trial/i);
    // THE Q1 REQUIREMENT: the rest of the card comes from getTrial(), a different call.
    expect(screen.getByTestId('trial-evidence-hash')).toHaveTextContent(trialWire.evidence_hash);
    expect(screen.getByTestId('settlement-panel')).toHaveAttribute('data-status', 'settled');
    expect(screen.getByTestId('markout-table')).toBeInTheDocument();
  });

  it('a 404 on the receipts route is a fourth answer and is not rendered as an empty set', async () => {
    const panel = await participants({ receipts: () => errorResponse(404, 'trial_not_found') });
    expect(panel).toHaveAttribute('data-state', 'unknown');
    expect(panel.textContent).not.toMatch(/nobody/i);
    expect(within(panel).queryAllByTestId('participant-row')).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// GROUP E — Fair-Play checks, ALL EIGHT keys (element 5, CF-5)
// ---------------------------------------------------------------------------
describe('H5.3 Fair-Play checks', () => {
  it('renders all EIGHT frozen keys per participant, in the frozen order, from the shared constant', async () => {
    const panel = await participants();
    const group = within(panel).getAllByTestId('fairplay-checks')[0];
    const rows = within(group).getAllByTestId('check-row');
    expect(rows).toHaveLength(8);
    // Iterating the CONSTANT is what makes an inline list in the component fail here.
    expect(rows.map((r) => r.getAttribute('data-key')))
      .toEqual(SIGNAL_TRIALS_CHECK_KEYS.map((c) => c.key));
    expect(rows.map((r) => r.getAttribute('data-phase')))
      .toEqual(SIGNAL_TRIALS_CHECK_KEYS.map((c) => c.phase));
  });

  it('relays each served verdict verbatim — a fail is a fail', async () => {
    const panel = await participants({
      verify: (url) => {
        const id = decodeURIComponent(url.split('/signal-trials/receipts/')[1]?.split('/')[0] ?? '');
        return jsonResponse(verifyWire(id, { ...ALL_EIGHT, body_hash: 'fail' }));
      },
    });
    const group = within(panel).getAllByTestId('fairplay-checks')[0];
    const rows = within(group).getAllByTestId('check-row');
    expect(rows[0]).toHaveAttribute('data-status', 'fail');
    expect(rows.map((r) => r.getAttribute('data-status')))
      .toEqual(['fail', 'pass', 'pass', 'pass', 'pending', 'pending', 'pending', 'pending']);
  });

  it('an ABSENT key renders as not-served, NEVER as the real verdict "pending"', async () => {
    const { manifest: _dropped, ...missingOne } = ALL_EIGHT;
    const panel = await participants({
      verify: (url) => {
        const id = decodeURIComponent(url.split('/signal-trials/receipts/')[1]?.split('/')[0] ?? '');
        return jsonResponse(verifyWire(id, missingOne));
      },
    });
    const group = within(panel).getAllByTestId('fairplay-checks')[0];
    const row = within(group).getAllByTestId('check-row')[1];
    expect(row).toHaveAttribute('data-key', 'manifest');
    expect(row).toHaveAttribute('data-status', 'not_served');
    // The discrimination control: `pending` is a verdict the verifier would then never have made.
    expect(row).not.toHaveAttribute('data-status', 'pending');
    expect(row.textContent).not.toMatch(/pending/i);
  });

  it('reads the receipt status ALONGSIDE the checks — identical pending maps, different meanings', async () => {
    const panel = await participants();
    const groups = within(panel).getAllByTestId('fairplay-checks');
    // rcpt_3 is `pending`, rcpt_4 is `UNSCORED`, and BOTH carry the same four pending outcome
    // checks. A check `pending` cannot say which; `receipt.status` can, so it is rendered.
    expect(groups[2]).toHaveAttribute('data-receipt-status', 'pending');
    expect(groups[3]).toHaveAttribute('data-receipt-status', 'UNSCORED');
  });

  it('surfaces served keys outside the frozen eight instead of swallowing the drift (CF-5)', async () => {
    const panel = await participants({
      verify: (url) => {
        const id = decodeURIComponent(url.split('/signal-trials/receipts/')[1]?.split('/')[0] ?? '');
        return jsonResponse(verifyWire(id, { ...ALL_EIGHT, brand_new_check: 'pass' }));
      },
    });
    expect(within(panel).getAllByTestId('checks-unexpected-keys')[0]).toHaveTextContent('brand_new_check');
  });

  it('a verify failure for ONE participant never blanks the others', async () => {
    const panel = await participants({
      verify: (url) => {
        const id = decodeURIComponent(url.split('/signal-trials/receipts/')[1]?.split('/')[0] ?? '');
        return id === 'rcpt_1' ? jsonResponse({ error: 'boom' }, 500) : jsonResponse(verifyWire(id));
      },
    });
    const groups = within(panel).getAllByTestId('fairplay-checks');
    expect(groups[0]).toHaveAttribute('data-checks-state', 'unavailable');
    expect(groups[1]).toHaveAttribute('data-checks-state', 'ok');
    expect(within(groups[1]).getAllByTestId('check-row')).toHaveLength(8);
    // The participant row itself is unaffected — its commitment is still recorded.
    expect(within(within(panel).getAllByTestId('participant-row')[0]).getByTestId('participant-payer'))
      .toHaveTextContent('0xaaa');
  });

  it('publishes NO aggregate verified badge — one pending check is never absorbed into a summary', async () => {
    await participants();
    expect(screen.queryAllByText(/^\s*verified\s*$/i)).toHaveLength(0);
    expect(document.body.textContent).not.toMatch(/all checks pass(ed)?/i);
  });

  // THE CASE THAT CAN ACTUALLY PRODUCE THE FORBIDDEN THING. The test above cannot: with four
  // `pending` verdicts in the fixture, an all-pass-gated badge never renders, so its negatives
  // hold against a card that publishes one. This render serves EIGHT `pass` verdicts, which is the
  // single input under which such a badge would appear — and it is the input the whole file was
  // missing. Verified by mutation: inserting an all-pass-gated `VERIFIED` label into
  // `FairPlayChecks` leaves the test above green and kills this one.
  it('publishes NO aggregate badge even when ALL EIGHT checks pass — the only case that could', async () => {
    const panel = await participants({
      verify: (url) => {
        const id = decodeURIComponent(url.split('/signal-trials/receipts/')[1]?.split('/')[0] ?? '');
        return jsonResponse(verifyWire(id, ALL_PASS));
      },
    });
    // ACCEPTANCE CONTROL (C52). Without this the negatives below could pass because the fixture
    // silently failed to be all-pass, which is the same unfalsifiable guard in a new costume.
    const group = within(panel).getAllByTestId('fairplay-checks')[0];
    expect(within(group).getAllByTestId('check-row').map((r) => r.getAttribute('data-status')))
      .toEqual(Array(SIGNAL_TRIALS_CHECK_KEYS.length).fill('pass'));
    // Each of the eight still reports independently — that is what the absence of a summary means.
    expect(within(group).getAllByTestId('check-row')).toHaveLength(8);
    // THE FORBIDDEN THING. `queryAll` rather than `query`: with four all-pass participants a card
    // that published a badge would render several, and `getByText`-style ambiguity must not be
    // what fails this test.
    expect(screen.queryAllByText(/^\s*verified\s*$/i)).toHaveLength(0);
    expect(document.body.textContent).not.toMatch(/all checks pass(ed)?/i);
    expect(document.body.textContent).not.toMatch(/\b8\s*\/\s*8\b|\ball (eight|8) (checks )?pass/i);
  });
});

// ---------------------------------------------------------------------------
// GROUP F — the markout table (element 4)
// ---------------------------------------------------------------------------
describe('H5.3 markout table', () => {
  it('carries the frozen markout label and the diagnostic tag', async () => {
    await card();
    expect(screen.getByText(SIGNAL_TRIALS_MARKOUT_LABEL)).toBeInTheDocument();
    expect(screen.getByTestId('markout-diagnostic-tag'))
      .toHaveTextContent('diagnostic — does not change ranking');
  });

  it('renders the four declared cost rows with 25 bps as the official rank basis', async () => {
    await card();
    const rows = screen.getAllByTestId('markout-row');
    expect(rows.map((r) => r.getAttribute('data-cost-bps'))).toEqual(['0', '10', '25', '50']);
    const official = rows.filter((r) => r.getAttribute('data-basis') === 'official');
    expect(official).toHaveLength(1);
    expect(official[0]).toHaveAttribute('data-cost-bps', '25');
  });

  it('fills the 25 bps row from the SERVED values and leaves the sweep rows explicitly not-served', async () => {
    await card();
    const rows = screen.getAllByTestId('markout-row');
    const official = rows.find((r) => r.getAttribute('data-cost-bps') === '25')!;
    expect(within(official).getByTestId('markout-follow'))
      .toHaveTextContent(String(settledOutcome.follow_markout_bps));
    expect(within(official).getByTestId('markout-fade'))
      .toHaveTextContent(String(settledOutcome.fade_markout_bps));
    // THE CONTROL AGAINST A DERIVED SWEEP. `TrialOutcomeWire` carries exactly ONE (follow, fade)
    // pair — the official 25 bps basis. No 0/10/50 bps value is on the wire, and computing one on
    // the client would be the frontend re-deriving scoring. These cells must hold NO digits.
    for (const cost of ['0', '10', '50']) {
      const row = rows.find((r) => r.getAttribute('data-cost-bps') === cost)!;
      expect(within(row).getByTestId('markout-follow').textContent).not.toMatch(/\d/);
      expect(within(row).getByTestId('markout-fade').textContent).not.toMatch(/\d/);
    }
  });

  it('shows no sweep at all when nothing settled, and says the 25 bps basis is unchanged', async () => {
    await card({ trial: () => jsonResponse(trial(nullMetricOutcome('pending'))) });
    expect(screen.queryAllByTestId('markout-row')).toHaveLength(0);
    expect(screen.getByTestId('markout-table').textContent)
      .toMatch(/no markout exists at any cost assumption/i);
  });
});

// ---------------------------------------------------------------------------
// GROUP G — the trial section's own failure domains
// ---------------------------------------------------------------------------
describe('H5.3 trial section states', () => {
  it('404 renders "trial not found" with no retry — a 404 is not a transport failure', async () => {
    stubRoutes({ trial: () => errorResponse(404, 'trial_not_found'), receipts: OK_RECEIPTS, verify: OK_VERIFY });
    render(<TrialMatchCard trialId={TRIAL_ID} />);
    const panel = await screen.findByTestId('trial-panel');
    await waitFor(() => expect(panel).toHaveAttribute('data-state', 'not_found'));
    expect(panel.textContent).toMatch(/no trial exists for this id/i);
    expect(screen.queryByTestId('trial-retry')).toBeNull();
    // Non-echo: the addendum forbids reflecting the requested id back from a 404.
    expect(panel.textContent).not.toContain(TRIAL_ID);
  });

  it('a 500 renders "trial data unavailable" WITH a retry, and never as "not found"', async () => {
    stubRoutes({ trial: () => jsonResponse({ error: 'boom' }, 500), receipts: OK_RECEIPTS, verify: OK_VERIFY });
    render(<TrialMatchCard trialId={TRIAL_ID} />);
    const panel = await screen.findByTestId('trial-panel');
    await waitFor(() => expect(panel).toHaveAttribute('data-state', 'unavailable'));
    expect(panel.textContent).toMatch(/did not respond/i);
    expect(screen.getByTestId('trial-retry')).toBeInTheDocument();
    expect(panel.textContent).not.toMatch(/no trial exists/i);
  });

  it('an ABSENT outcome key surfaces as a LOUD NAMED contract failure, never an empty card', async () => {
    // Reachable against this tree TODAY: /trials/{id} still serves OpenTrialResponse (no `outcome`
    // key) until H4.3 merges. The adapter throws naming the field; the card must SHOW that.
    const { outcome: _absent, ...noOutcome } = trialWire;
    stubRoutes({ trial: () => jsonResponse(noOutcome), receipts: OK_RECEIPTS, verify: OK_VERIFY });
    render(<TrialMatchCard trialId={TRIAL_ID} />);
    const panel = await screen.findByTestId('trial-panel');
    await waitFor(() => expect(panel).toHaveAttribute('data-state', 'contract_violation'));
    expect(screen.getByTestId('trial-error-detail').textContent).toMatch(/outcome/);
    // Discrimination: a card that swallowed the throw would land in `unavailable` or render an
    // empty settlement panel. Neither may happen.
    expect(screen.queryByTestId('settlement-panel')).toBeNull();
    expect(panel.textContent).not.toMatch(/did not respond/i);
  });
});

// ---------------------------------------------------------------------------
// GROUP H — the language guard. This is the most public artifact in the build.
// ---------------------------------------------------------------------------
describe('H5.3 language guard', () => {
  const FORBIDDEN: RegExp[] = [
    /\bpnl\b/i, /\bprofit(s|ed)?\b/i, /\bmade money\b/i, /\breali[sz]ed\b/i,
    /\bfill(s|ed)?\b/i, /\bposition(s)?\b/i, /\balpha\b/i, /\bedge\b/i, /\bproven\b/i,
    /tamper/i, /immutab/i, /\bguarantee/i,
  ];

  it('never uses trading-result or tamper-proofing language in any settlement state', async () => {
    for (const outcome of [settledOutcome, nullMetricOutcome('pending'), nullMetricOutcome('UNSCORED'), null]) {
      stubRoutes({ trial: () => jsonResponse(trial(outcome)), receipts: OK_RECEIPTS, verify: OK_VERIFY });
      const { unmount } = render(<TrialMatchCard trialId={TRIAL_ID} />);
      const panel = await screen.findByTestId('trial-panel');
      await waitFor(() => expect(panel).not.toHaveAttribute('data-state', 'loading'));
      const text = document.body.textContent ?? '';
      // ACCEPTANCE CONTROL (C52). Without this the test is a pure absence assertion and is green
      // against an EMPTY component — a pin, not a predicate. Requiring the honest label to be
      // PRESENT in every settlement state is what makes the negatives below mean something.
      expect(text, `markout label missing in state ${outcome?.status ?? 'none'}`)
        .toContain(SIGNAL_TRIALS_MARKOUT_LABEL);
      for (const bad of FORBIDDEN) {
        expect(text, `forbidden ${bad} in state ${outcome?.status ?? 'none'}`).not.toMatch(bad);
      }
      unmount();
    }
  });

  it('describes the checks as reproducibility over recorded evidence, and claims nothing stronger', async () => {
    await participants();
    const text = document.body.textContent ?? '';
    expect(text).toMatch(/reproducib/i);
    // The declared limit: never proof against a malicious storage operator.
    expect(text).not.toMatch(/tamper|immutab|cannot be (altered|changed)/i);
    // THE FORBIDDEN LIST, RUN WHERE THE CHECK DESCRIPTIONS ARE GUARANTEED TO BE ON SCREEN.
    //
    // The test above scans a `card()` render, which awaits only the TRIAL panel; whether the
    // participants panel — and therefore the eight rendered check descriptions — has resolved by
    // then is a promise-ordering accident. Measured: it usually has, so that test does usually
    // cover this copy. "Usually" is not a guard. This assertion awaits the participants panel
    // explicitly, so the eight descriptions are always in `text`.
    //
    // Worth stating plainly: a QUALITY review, not a test, is what caught three of those
    // descriptions overstating what the verifier establishes. The language guard would not have
    // caught them either — none of the overstatements used a forbidden WORD — so this closes the
    // reachability gap, not the semantic one. Nothing here can check a sentence against
    // receipts.py; that remains a human obligation, recorded in CHECK_DESCRIPTION's own comments.
    for (const bad of FORBIDDEN) {
      expect(text, `forbidden ${bad} in the participant/checks copy`).not.toMatch(bad);
    }
  });

  it('carries the verbatim paper-benchmark fence', async () => {
    await card();
    expect(screen.getByTestId('trial-fence'))
      .toHaveTextContent('PAPER BENCHMARK — NOT A TRADE RECOMMENDATION');
  });

  it('never claims a qualified status on a live exhibition trial', async () => {
    await participants();
    expect(document.body.textContent).not.toMatch(/\bqualified\b/i);
  });
});

// ---------------------------------------------------------------------------
// GROUP I — the route page
// ---------------------------------------------------------------------------
describe('H5.3 /trials/[trialId] route page', () => {
  it('renders the card for the route param and is PUBLIC — no AuthGate anywhere in the tree', async () => {
    stubRoutes({ trial: () => jsonResponse(trial(settledOutcome)), receipts: OK_RECEIPTS, verify: OK_VERIFY });
    render(<TrialPage />);
    const panel = await screen.findByTestId('trial-panel');
    await waitFor(() => expect(panel).not.toHaveAttribute('data-state', 'loading'));
    expect(screen.getByTestId('trial-evidence-hash')).toHaveTextContent(trialWire.evidence_hash);
    // AuthGate renders a connect-wallet affordance; a public benchmark surface must show none.
    expect(screen.queryByText(/connect wallet/i)).toBeNull();
  });

  it('fetches the trial id taken from the route, not a hardcoded one', async () => {
    stubRoutes({ trial: () => jsonResponse(trial(settledOutcome)), receipts: OK_RECEIPTS, verify: OK_VERIFY });
    render(<TrialPage />);
    await screen.findByTestId('trial-evidence-hash');
    const urls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.endsWith(`/signal-trials/trials/${TRIAL_ID}`))).toBe(true);
    expect(urls.some((u) => u.endsWith(`/signal-trials/trials/${TRIAL_ID}/receipts`))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// WHAT THIS BASIS CANNOT EXPRESS (C66, stated rather than implied)
// ---------------------------------------------------------------------------
// 1. It cannot pin the SWEEP VALUES at 0/10/50 bps, because no such value exists on the frozen
//    wire — `TrialOutcomeWire` carries exactly one (follow, fade) pair, the official 25 bps basis.
//    The tests above pin only that those cells carry NO number. If the backend ever serves a real
//    sweep, this file must be revised before the cells are filled.
// 2. It cannot pin the SETTLEMENT TARGET T against a served field. The 1h horizon is the published
//    settlement law, not a wire value, so the countdown is a DERIVED display and is labelled as
//    one. A backend that later serves a settlement timestamp would make this derivation wrong and
//    no test here would notice.
// 3. It cannot distinguish a receipts 500 caused by THIS trial from one caused by an unreadable
//    row belonging to another trial — that is the Q1 limitation itself, and it is precisely why
//    the copy asserts nothing about which. The test pins the silence, not the cause.
// 4. It exercises the checks through `verifyReceipt` per participant. It cannot detect a backend
//    that serves the eight keys with correct names but wrong SEMANTICS; these are relay tests.
// 5. SOME NEGATIVES HERE ARE LEXICAL REGRESSION GUARDS, NOT DISCRIMINATION CONTROLS, and the
//    difference is worth naming. The forbidden-language list, `\bqualified\b`, `connect wallet`,
//    `corrupt` / `this trial's data`, and `nobody` assert the absence of strings that appear
//    NOWHERE in the card today, so no fixture can make them fail — they guard a future edit that
//    types one of those words, which is their whole job. What separates them from the
//    aggregate-badge gap is that the badge was a STRUCTURE gated on a data condition (all eight
//    `pass`) that no fixture produced; that condition is now served by `ALL_PASS` above. Where a
//    lexical negative pairs with a claim the card really makes, it carries an acceptance control:
//    the language guard requires the frozen markout label present in all four settlement states,
//    and the Q1 negatives pair with the positive "says nothing about this trial".
