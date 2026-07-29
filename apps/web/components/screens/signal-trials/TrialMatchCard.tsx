'use client';
import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { ApiError } from '@/lib/api';
import {
  SIGNAL_TRIALS_CHECK_KEYS,
  SIGNAL_TRIALS_MARKOUT_LABEL,
  SignalTrialsUnavailableError,
  getTrial,
  getTrialReceipts,
  verifyReceipt,
} from '@/lib/signal-trials-api';
import type { CommitReceipt, TrialCard, TrialOutcome, VerifyChecks } from '@/lib/contracts';
import {
  EvidenceLawIdentity,
  SharedEvidenceRail,
  SignalStatePanel,
} from './SignalStatePanel';
import { TrialsSplitScreen } from './TrialsSplitScreen';
import styles from './TrialMatchCard.module.css';

// H5.3 — the Trial Match Card at /trials/[trialId]. PUBLIC (no AuthGate): this is the judge-facing
// fairness artifact, and gating it would put the honesty claims themselves behind a wallet.
//
// THREE INDEPENDENT FAILURE DOMAINS, and keeping them independent is the point of this file:
//
//   1. THE TRIAL          GET /signal-trials/trials/{id}           → the evidence hash, the
//                                                                    settlement panel, the markouts
//   2. THE PARTICIPANTS   GET /signal-trials/trials/{id}/receipts  → the per-agent decisions
//   3. THE CHECKS         GET /signal-trials/receipts/{rid}/verify → the eight Fair-Play verdicts
//
// They are DIFFERENT CALLS and one failing says nothing about the others. This matters most for
// (2): the carried Q1 limitation means `store.finalized()` reads EVERY finalized row before
// filtering by trial_id, so a single unreadable row belonging to some OTHER trial fails this
// request — measured at 200 → 500 with the requested trial's own set fully intact. A 500 there
// therefore implies NOTHING about the trial being viewed. This card must never say "this trial's
// data is corrupt", and the rest of the card, which comes from (1), stays on screen.
//
// THE ACTION IS ALREADY ON THE WIRE. `CommitReceipt.action` is FOLLOW / FADE / ABSTAIN as the
// backend derived and recorded it. This card RELAYS it and never re-derives the bands from
// `p_follow_profitable` — the p ≥ 0.60 / ≤ 0.40 line below is explanatory copy about how the
// backend derived the recorded action, not a computation performed here (CON-003: the frontend
// never reimplements law, scoring or verification).
//
// FOUR SETTLEMENT STATES, NOT THREE. `pending`, `settled` and `UNSCORED` are the recorded ones,
// and `outcome === null` is a fourth: it says nothing was computed AT ALL, which is a WEAKER
// statement than a recorded `pending`. `pending` and `UNSCORED` carry IDENTICAL null metrics
// (C26) — `status` is the only thing that separates them, so `status` is what this file branches
// on, never the nulls.
//
// WHAT THE EIGHT CHECKS ESTABLISH, stated precisely because this is the most public artifact in
// the build: they are REPRODUCIBILITY CHECKS OVER RECORDED EVIDENCE. They re-derive a receipt's
// commit-time and settlement-time claims from the stored artifacts. A single-field edit to a
// published outcome can no longer change or erase a score while verification reports green. That
// is the whole claim. No copy in this file asserts tamper-proofing or immutability, and there is
// deliberately NO aggregate badge: one pending or failed check must never be absorbed into a green
// summary.
//
// null IS NOT ZERO, everywhere below. A zero markout is a real FLAT outcome and a zero Brier is a
// PERFECT score, so every nullable metric is tested with `=== null` and there is no `??` or `||`
// fallback on any of them.

// The published settlement horizon: a trial settles at t0 + 1h against one completed 1m candle.
//
// THIS IS A LAW CONSTANT, NOT A WIRE FIELD. `TrialOutcomeWire` serves `close_ts_ms` only once a
// settlement exists, so a PENDING trial has no served target and the countdown below is a DERIVED
// display. It is labelled as derived wherever it renders — the alternative, presenting a computed
// target as though the backend had stated it, is exactly the class of fabrication this screen
// exists to avoid. Exported so the test pins the same constant the card counts against.
export const TRIAL_SETTLEMENT_HORIZON_MS = 3_600_000;

// The official cost basis, and the declared diagnostic sweep around it. The API carries the 25-bps
// pair used by ranking. The binding handoff separately classifies 0 / 10 / 50 as presentation
// derivations: gross = served FOLLOW + 25; follow(c) = gross - c; fade(c) = -gross - c. Those rows
// never replace or reinterpret the official pair, and the sweep never changes ranking.
const OFFICIAL_COST_BPS = 25;
const COST_SWEEP_BPS = [0, 10, 25, 50] as const;

// The frozen check keys are the SINGLE source (CF-5) — the backend publishes `checks` keys as
// unconstrained `str`, so a second spelling could drift with nothing able to notice. This map is
// keyed off that constant's own key type, which makes a missing or misspelled entry a COMPILE
// error rather than a silently absent description.
type FrozenCheckKey = (typeof SIGNAL_TRIALS_CHECK_KEYS)[number]['key'];

// What each check ASSERTS. Deliberately state-independent: the verdict is rendered separately, so
// no description here implies its own outcome.
//
// THIS MAP IS RENDERED COPY, not commentary — it goes into every check row on the public route,
// so each sentence is a public claim about what verification establishes and is written against
// `veridex/signal_trials/receipts.py`, not against the check's NAME. Two of these names are
// specifically over-readable and the backend author wrote paragraphs disclaiming the stronger
// reading; those paragraphs are cited inline below so a later editor cannot re-introduce the
// overstatement by reasoning from the name. Where a check binds membership in a frozen
// vocabulary, this says membership — it is a weaker claim than correspondence with the live
// trial, and it is the true one.
const CHECK_DESCRIPTION: Record<FrozenCheckKey, string> = {
  body_hash: 'The canonical commit body re-hashes to the sealed value recorded at commit time.',
  manifest: 'The receipt binds to the published run manifest hash it claims, including the resolved trial id.',
  deadline_respected: 'The commitment was received strictly before the commit deadline.',
  // receipts.py:1965 — `payload.get("trial_mode") == LIVE_TRIAL_MODE`, a comparison against a
  // module constant. The trial is NEVER consulted, and receipts.py:1882-1884 states why that is
  // deliberate: this and `deadline_respected` "read facts the receipt CARRIES rather than
  // consulting the live trial, and that is what makes a historical receipt verifiable at all."
  live_mode: 'The mode recorded on the receipt is exactly live — paid commitments are live-only. It reads the receipt, never the trial.',
  // receipts.py:1843 — `BAR_MS.get(bar) == bar_ms`, pure membership in a frozen table.
  // receipts.py:157-162 disclaims the selection reading under the heading "What it does NOT bind,
  // stated so the name cannot be over-read": "It checks membership, not selection." Selection is
  // enforced at the WRITER instead, because the only artifact naming the selected bar is the
  // published season and a season is republished.
  bar_version: 'The recorded bar label and its width are one of the frozen pairs this law settles on — membership, not a claim about which pair this season selected.',
  // receipts.py:1844 — `row.get("law_version") == SETTLEMENT_LAW_VERSION`. It reads the OUTCOME
  // row, not the receipt, and compares against what THIS BUILD implements (:172-174: "A record
  // settled under an older law is not wrong, but it is not re-derivable HERE").
  law_version: 'The scoring law version recorded on the outcome row is the one this build implements.',
  // receipts.py:1849-1851 plus `_evidence_reproduces` (:801-806, which asserts
  // `visible_at_decision(signal) == evidence` as well as the hash). Three properties of the
  // OUTCOME ROW. receipts.py:181-185 disclaims the receipt-level reading: "it does not tie the
  // RECEIPT to the trial" — that join is `manifest`'s finding, not this one's — so this sentence
  // must not attach the binding to agents or to receipts.
  evidence_equality: 'The outcome row’s sealed evidence re-hashes to its own recorded hash, carries nothing beyond the visible_at_decision tier, and is filed under the trial it names.',
  // receipts.py:1852 → `_outcome_source_reproduces` (:986). THREE obligations under one name
  // (:186-203), and the middle one is the strongest thing the verifier does: leaving the law's
  // outputs unchecked "is what let a forged follow_profitable verify with all eight checks
  // passing". Describing this as a boundary re-derivation sells it short.
  outcome_source: 'Three obligations: the close boundary, the law’s outputs (entry, both markout legs and follow_profitable) and the fetch source all re-derive from the recorded candle.',
};

const CHECK_STATUS_PRESENTATION = {
  pass: { glyph: '✓', word: 'PASS' },
  fail: { glyph: '✕', word: 'FAIL' },
  pending: { glyph: '◷', word: 'PENDING' },
  not_served: { glyph: '—', word: 'not served' },
} as const;

// ---------------------------------------------------------------------------
// state machines — one per failure domain, deliberately not merged
// ---------------------------------------------------------------------------

type TrialState =
  | { kind: 'loading' }
  | { kind: 'ok'; trial: TrialCard }
  | { kind: 'not_found' }
  // A NON-CONFORMING response, distinct from an outage. Reachable against this tree TODAY:
  // GET /signal-trials/trials/{id} still serves an `OpenTrialResponse` with no `outcome` key until
  // H4.3 merges, and the adapter throws NAMING the field. That must surface as a loud named
  // failure, not as an empty card and not as "the endpoint did not respond".
  | { kind: 'contract_violation'; detail: string }
  | { kind: 'unavailable' };

type ChecksState =
  | { kind: 'ok'; verify: VerifyChecks }
  | { kind: 'not_found' }
  | { kind: 'unavailable' };

interface ParticipantEntry {
  receipt: CommitReceipt;
  checks: ChecksState;
}

type ParticipantsState =
  | { kind: 'loading' }
  // `[]` is carried inside `list` and separated at render: it is a REAL, positive state meaning
  // nobody has paid to commit on this trial. It is neither an error nor a loading state.
  | { kind: 'list'; entries: ParticipantEntry[] }
  | { kind: 'unknown' }
  | { kind: 'store_unavailable'; code: string }
  | { kind: 'failed' };

// A named contract violation is a plain `Error` carrying the offending field; a transport or
// protocol failure is an `ApiError` (or a `TypeError` from a failed fetch). Separating them is
// what lets the two render differently — "the response was non-conforming" and "the endpoint did
// not respond" are different claims, and merging them sends a debugger to the wrong place.
function classifyTrialError(err: unknown): TrialState {
  if (err instanceof ApiError) return { kind: 'unavailable' };
  if (err instanceof TypeError) return { kind: 'unavailable' };
  if (err instanceof Error) return { kind: 'contract_violation', detail: err.message };
  return { kind: 'unavailable' };
}

// ---------------------------------------------------------------------------
// formatters — `=== null` everywhere, never `??`
// ---------------------------------------------------------------------------

const DASH = '—';

function bps(v: number | null): string {
  // 0 is a real FLAT outcome and renders as one; null is "nothing settled" and renders as a dash.
  if (v === null) return DASH;
  return `${v > 0 ? '+' : ''}${v.toFixed(1)} bps`;
}

function brierText(v: number | null): string {
  // 0 is a PERFECT score and renders as one.
  return v === null ? DASH : v.toFixed(3);
}

function num(v: number | null): string {
  return v === null ? DASH : String(v);
}

// Remaining time to a derived target. Never negative: once the horizon has elapsed the trial is
// still PENDING (it stays pending until T + bar + the fetch grace and is never marked UNSCORED
// early), so the honest reading is "elapsed, awaiting settlement" and not a negative clock.
function remaining(ms: number): string {
  if (ms <= 0) return 'horizon elapsed · awaiting a completed settlement candle';
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}h ${m}m ${s}s` : `${m}m ${s}s`;
}

// ---------------------------------------------------------------------------
// the card
// ---------------------------------------------------------------------------

export function TrialMatchCard({ trialId }: { trialId: string }) {
  const [trial, setTrial] = useState<TrialState>({ kind: 'loading' });
  const [people, setPeople] = useState<ParticipantsState>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let alive = true;
    setTrial({ kind: 'loading' });
    setPeople({ kind: 'loading' });

    getTrial(trialId)
      .then((t) => {
        if (!alive) return;
        setTrial(t === null ? { kind: 'not_found' } : { kind: 'ok', trial: t });
      })
      .catch((err: unknown) => { if (alive) setTrial(classifyTrialError(err)); });

    // The participant domain resolves as ONE unit — the receipts, then each receipt's eight
    // verdicts — so the section never flickers through a half-populated state in which some
    // participants appear to have no checks. `allSettled` is load-bearing: one participant's
    // verify failing must not blank the other participants' verdicts.
    getTrialReceipts(trialId)
      .then(async (receipts) => {
        if (!alive) return;
        if (receipts === null) { setPeople({ kind: 'unknown' }); return; }
        const settled = await Promise.allSettled(receipts.map((r) => verifyReceipt(r.receiptId)));
        if (!alive) return;
        setPeople({
          kind: 'list',
          entries: receipts.map((receipt, i) => {
            const outcome = settled[i];
            if (outcome.status === 'rejected') return { receipt, checks: { kind: 'unavailable' } };
            // A 404 from verify resolves to `null` — the receipt id is unknown to the verify
            // route. That is a different statement from "verification could not be run".
            return {
              receipt,
              checks: outcome.value === null
                ? { kind: 'not_found' }
                : { kind: 'ok', verify: outcome.value },
            };
          }),
        });
      })
      .catch((err: unknown) => {
        if (!alive) return;
        // The NAMED 503 is the one refusal this card may report as a diagnosis, because it is the
        // one the backend actually stated. Everything else — the Q1 500 included — is an
        // undiagnosed failure and is reported as one.
        setPeople(
          err instanceof SignalTrialsUnavailableError
            ? { kind: 'store_unavailable', code: err.code }
            : { kind: 'failed' },
        );
      });

    return () => { alive = false; };
  }, [trialId, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  return (
    <section className={styles.screen} aria-label="ProofArena trial match card">
      <div className={styles.panel} data-testid="trial-panel" data-state={trial.kind}>
        <TrialBody state={trial} onRetry={retry} />
      </div>
      {/* The participant and cost panels hang off the TRIAL succeeding, because both are framed by
          the trial's own evidence. Their INTERNAL failures are independent and handled below. */}
      {trial.kind === 'ok' ? (
        <div className={styles.trialComposition}>
          <div className={styles.trialPrimary}>
            <SharedEvidenceRail
              trial={trial.trial}
              agents={people.kind === 'list' ? people.entries.map((entry) => entry.receipt) : []}
            />
            <SignalStatePanel trial={trial.trial} />
            {people.kind === 'list' && people.entries.length > 0 ? (
              <TrialsSplitScreen
                trial={trial.trial}
                receipts={people.entries.map((entry) => entry.receipt)}
              />
            ) : null}
            <ParticipantsSection state={people} />
          </div>
          <aside className={styles.trialSecondary}>
            <EvidenceLawIdentity trial={trial.trial} />
            <MarkoutSection outcome={trial.trial.outcome} />
          </aside>
        </div>
      ) : null}
    </section>
  );
}

function TrialBody({ state, onRetry }: { state: TrialState; onRetry: () => void }) {
  switch (state.kind) {
    case 'loading':
      return (
        <>
          {/* Named so it is unmistakable in a screenshot: nothing below stands for a value. A
              skeleton populated with plausible hashes and markouts is the most persuasive lie this
              card could tell, so it holds none. */}
          <p className={styles.stateSub}>LOADING TRIAL · NO PLACEHOLDER RESULTS SHOWN</p>
          <div className={styles.skeletonRow} aria-hidden />
          <div className={styles.skeletonRow} aria-hidden />
        </>
      );
    case 'not_found':
      // NON-ECHO, per the route contract addendum: the id arrives from a URL path segment and is
      // never reflected back out of a 404. So this branch renders no id at all — not in the copy
      // and not in a heading. There is also no RETRY: a 404 is a domain answer, not an outage, and
      // offering a retry would frame a settled fact as a transient failure.
      return (
        <>
          <h1 className={styles.stateTitle}>Trial not found</h1>
          <p className={styles.stateBody}>No trial exists for this id. Nothing was published under it.</p>
          <Link className={styles.back} href="/trials">← BACK TO ARENA</Link>
          {/* PROOFARENA-EXACT-COPY.md §4 `not found`. The behaviour above was already correct; this
              line is what makes it LEGIBLE. An absent retry button is invisible — a judge cannot
              distinguish a deliberate refusal from a forgotten affordance by looking at nothing —
              so the reason is stated rather than merely enacted. It appears on this state ONLY:
              `unavailable` IS a transport failure and DOES offer a retry, so borrowing the line
              there would state the exact opposite of the truth about that state. */}
          <p className={styles.stateSub} data-testid="trial-not-found-sub">
            404 is not a transport failure — retry is not offered.
          </p>
        </>
      );
    case 'unavailable':
      return (
        <>
          <h1 className={styles.stateTitle}>Trial data unavailable</h1>
          <p className={styles.stateBody}>
            The trial endpoint did not respond. No partial trial state, evidence, or outcome is shown.
          </p>
          <button type="button" className={styles.retry} onClick={onRetry} data-testid="trial-retry">
            RETRY →
          </button>
        </>
      );
    case 'contract_violation':
      // A response that does not conform to the frozen contract. Shown LOUDLY and in full: the
      // adapter names the offending field, and swallowing that into a generic outage message would
      // send a reader looking for a network problem that does not exist.
      return (
        <>
          <h1 className={styles.stateTitle}>Trial response did not match the frozen contract</h1>
          <p className={styles.stateBody}>
            The response was served, but it does not conform to the published schema, so nothing on
            it can be rendered as a trial. No field was substituted or inferred.
          </p>
          <pre className={styles.errorDetail} data-testid="trial-error-detail">{state.detail}</pre>
        </>
      );
    case 'ok':
      return <TrialHead trial={state.trial} />;
  }
}

// ---------------------------------------------------------------------------
// identity + the sealed evidence hash (element 1) + the settlement panel (element 3)
// ---------------------------------------------------------------------------

function TrialHead({ trial }: { trial: TrialCard }) {
  const status = trial.outcome === null ? 'none' : trial.outcome.status;
  return (
    <>
      <header className={styles.head}>
        <div className={styles.headText}>
          <Link className={styles.back} href="/trials">← ARENA</Link>
          <h1 className={styles.title}>{trial.trialId}</h1>
          <p className={styles.lead}>
            live exhibition — proves the OKX signal feed is connected. Exhibition is never skill
            evidence; comparative claims come only from the sealed replay season.
          </p>
        </div>
        <div className={styles.headChips}>
          <span className={styles.chip}>● live exhibition</span>
          <span className={styles.chip} data-status={status} data-testid="trial-status-chip">{status}</span>
        </div>
      </header>

      {/* VERBATIM (design handoff §4). Uppercase in the DOM because that is the frozen string. */}
      <p className={styles.fence} data-testid="trial-fence">
        PAPER BENCHMARK — NOT A TRADE RECOMMENDATION
      </p>

      <SettlementPanel outcome={trial.outcome} t0Ms={trial.t0Ms} />
    </>
  );
}

function SettlementPanel({ outcome, t0Ms }: { outcome: TrialOutcome | null; t0Ms: number }) {
  // `status` is the ONLY discriminator between `pending` and `UNSCORED` — every metric is null on
  // both — and `outcome === null` is a fourth state saying nothing was computed at all.
  const status = outcome === null ? 'none' : outcome.status;
  return (
    <div className={styles.settlement} data-testid="settlement-panel" data-status={status}>
      <p className={styles.panelLabel}>SETTLEMENT</p>
      {outcome === null ? <NoOutcomeBody /> : null}
      {outcome !== null && outcome.status === 'settled' ? <SettledBody o={outcome} t0Ms={t0Ms} /> : null}
      {outcome !== null && outcome.status === 'pending' ? <PendingBody o={outcome} t0Ms={t0Ms} /> : null}
      {outcome !== null && outcome.status === 'UNSCORED' ? <UnscoredBody o={outcome} /> : null}
    </div>
  );
}

// `outcome: null` — the response carried the key and stated `null`. That is a WEAKER claim than a
// recorded `pending`: nothing was computed at all, not "the result is not known yet". It must
// never borrow either recorded state's copy.
function NoOutcomeBody() {
  return (
    <>
      <h2 className={styles.stateTitle}>No outcome record exists for this trial</h2>
      <p className={styles.stateBody}>
        The trial endpoint served no outcome at all — not a recorded result, and not a recorded
        absence of one. Nothing about settlement can be stated from it.
      </p>
    </>
  );
}

function SettledBody({ o, t0Ms }: { o: TrialOutcome; t0Ms: number }) {
  return (
    <>
      <dl className={styles.grid}>
        <Field label="EVENT-ANCHORED ENTRY" value={String(o.entry)} />
        <Field label="SETTLEMENT CANDLE CLOSE" value={num(o.future)} />
        <Field label="CLOSE TIMESTAMP" value={num(o.closeTsMs)} />
        <Field
          label="OBSERVATION LAG"
          value={o.observationLagMs === null ? DASH : `${o.observationLagMs} ms`}
        />
        <Field label="FOLLOW MARKOUT" value={bps(o.followMarkoutBps)} testId="settlement-follow-markout" />
        <Field label="FADE MARKOUT" value={bps(o.fadeMarkoutBps)} testId="settlement-fade-markout" />
        <Field
          label="FOLLOW_PROFITABLE"
          // A boolean is not a number: `false` is a recorded verdict and `null` is its absence.
          value={o.followProfitable === null ? DASH : String(o.followProfitable)}
        />
        <Field
          label="SETTLEMENT TARGET T"
          value={`${t0Ms + TRIAL_SETTLEMENT_HORIZON_MS} · derived from fixed 1h horizon`}
        />
      </dl>
      <p className={styles.official}>OFFICIAL RESULT · {OFFICIAL_COST_BPS} BPS MODELED COSTS</p>
      <p className={styles.footNote}>
        These are paper markouts against an event-anchored historical candle close. They are modeled
        over recorded bars after modeled costs — never an executed trade and never a trading result.
        Both FOLLOW and FADE pay the modeled cost.
      </p>
    </>
  );
}

// PENDING. The design's body copy enumerates "no entry, close, markout or Brier value exists yet";
// `entry` is NON-nullable on the frozen wire and IS served here, so that enumeration is amended
// rather than shipped — a card that printed the served entry beside a sentence denying one exists
// would be contradicting its own data. Everything else in the sentence holds.
function PendingBody({ o, t0Ms }: { o: TrialOutcome; t0Ms: number }) {
  const target = t0Ms + TRIAL_SETTLEMENT_HORIZON_MS;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <>
      <dl className={styles.grid}>
        <Field label="COMMIT WINDOW" value="CLOSED" />
        <Field label="EVENT-ANCHORED ENTRY" value={String(o.entry)} />
        <Field label="REMAINING HORIZON" value={remaining(target - now)} testId="settlement-countdown" />
        <Field label="SETTLEMENT TARGET T" value={`${target} · derived from fixed 1h horizon`} />
      </dl>
      <p className={styles.stateBody}>
        The result is not known. No close, markout, or Brier value exists yet. Commit-time checks
        are already evaluated; outcome-time checks stay pending until a completed event-anchored
        candle exists.
      </p>
      {/* The derivation is disclosed where it is used. The endpoint serves no settlement timestamp
          on a pending trial, so this target is computed from the published 1h law — stating that
          is the difference between a derived display and a fabricated one. */}
      <p className={styles.stateSub}>
        Settlement target {target} is derived from the published 1h settlement law, not served by
        the trial endpoint. A trial stays pending until that target plus one bar and the fetch
        grace; it is never marked UNSCORED early.
      </p>
    </>
  );
}

// UNSCORED. The title and body are VERBATIM strings.
//
// CASING: the DOM carries the TASK PACKET's exact spelling and the design handoff's uppercase
// presentation is applied in CSS (`.verbatimUpper`). This is the precedent SeasonScreen set for
// the frozen markout label, and it resolves the two authorities without putting a second spelling
// in the tree — the packet ranks above the design handoff, and the design still gets its rendering.
function UnscoredBody({ o }: { o: TrialOutcome }) {
  return (
    <>
      <h2 className={`${styles.stateTitle} ${styles.verbatimUpper}`}>
        UNSCORED — no completed settlement candle
      </h2>
      <p className={styles.stateBody}>
        The required event-anchored candle was unavailable after the scoring window and grace
        period. No value was interpolated.
      </p>
      <dl className={styles.grid}>
        <Field label="EVENT-ANCHORED ENTRY" value={String(o.entry)} />
        <Field label="SETTLEMENT CANDLE CLOSE" value={num(o.future)} />
        <Field label="CLOSE TIMESTAMP" value={num(o.closeTsMs)} />
        <Field
          label="OBSERVATION LAG"
          value={o.observationLagMs === null ? DASH : `${o.observationLagMs} ms`}
        />
        <Field
          label="FOLLOW / FADE MARKOUT"
          value={`${bps(o.followMarkoutBps)} · ${bps(o.fadeMarkoutBps)}`}
        />
        <Field label="TERMINAL ELIGIBILITY" value="T + bar + 600,000 ms grace elapsed" />
      </dl>
      <p className={styles.footNote}>
        The agent commitments and the evidence receipt above are preserved and remain verifiable. No
        fallback price was substituted, no bar was skipped, and this trial does not count toward any
        agent&apos;s active coverage.
      </p>
    </>
  );
}

function Field({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return (
    <div className={styles.field}>
      <dt className={styles.fieldLabel}>{label}</dt>
      <dd className={styles.fieldValue} data-testid={testId}>{value}</dd>
    </div>
  );
}

// ---------------------------------------------------------------------------
// participants (element 2) + Fair-Play checks (element 5)
// ---------------------------------------------------------------------------

function ParticipantsSection({ state }: { state: ParticipantsState }) {
  // `[]` gets its OWN attribute — collapsing it into `list` would let an empty table stand in for
  // the positive claim that nobody paid to commit.
  const attr = state.kind === 'list' ? (state.entries.length === 0 ? 'empty' : 'list') : state.kind;
  return (
    <div className={styles.panel} data-testid="participants-panel" data-state={attr}>
      <div className={styles.panelHead}>
        <p className={styles.panelLabel}>AGENT DECISIONS</p>
        <span className={styles.participantContext} data-testid="participants-context">
          paid commits · live-only · 300s window
        </span>
      </div>
      <p className={styles.panelSub}>one probability per agent · action is derived, never submitted</p>
      <ParticipantsBody state={state} />
    </div>
  );
}

function ParticipantsBody({ state }: { state: ParticipantsState }) {
  switch (state.kind) {
    case 'loading':
      return <p className={styles.stateSub}>LOADING PARTICIPANTS · NO PLACEHOLDER RESULTS SHOWN</p>;

    case 'unknown':
      // 404 on the receipts route: no participant set is reachable for this id. Emphatically not
      // `[]`, which would be a positive claim that no agent committed.
      return (
        <p className={styles.stateBody}>
          No participant set is served for this id. That is not a statement about which agents
          committed.
        </p>
      );

    case 'store_unavailable':
      // The one refusal the backend NAMED, so it is the one this card may diagnose. `[]` would
      // assert that no agent paid to commit, and a deployment with no store mounted has no basis
      // for that claim.
      return (
        <>
          <h2 className={styles.stateTitle}>Participant set unavailable</h2>
          <p className={styles.stateBody}>
            No participant store is mounted, so this trial&apos;s participant set cannot be served.
            An empty list would be a positive claim that no agent paid to commit, and nothing here
            supports it.
          </p>
          <p className={styles.stateSub}>{state.code}</p>
        </>
      );

    case 'failed':
      // THE Q1 SURFACE. The receipts route reads every finalized row before filtering by trial_id,
      // so an unreadable record belonging to ANY trial fails this request. A failure here is
      // therefore evidence about the store, not about this trial, and the copy says only that.
      return (
        <>
          <h2 className={styles.stateTitle}>Participant set could not be served</h2>
          <p className={styles.stateBody}>
            The participant route did not return a set. This says nothing about this trial: the
            route reads the whole finalized store before selecting a trial, so an unreadable record
            belonging to any trial refuses the request. The evidence, settlement and cost panels
            come from a different endpoint and are unaffected.
          </p>
        </>
      );

    case 'list':
      if (state.entries.length === 0) {
        return (
          <>
            <h2 className={styles.stateTitle}>No commitment was recorded for this trial</h2>
            <p className={styles.stateBody}>
              The participant route served an empty set: no agent paid to commit on this trial
              before the deadline. Nothing is inferred for the missing side.
            </p>
          </>
        );
      }
      return (
        <>
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th className={styles.upper}>AGENT</th>
                  <th className={styles.upper}>ROLE</th>
                  <th className={`${styles.upper} ${styles.r}`}>p_follow_profitable</th>
                  <th className={styles.upper}>DERIVED ACTION</th>
                  <th className={`${styles.upper} ${styles.r}`}>BRIER</th>
                  {/* The frozen label, lowercase in the DOM; `.upper` supplies the design's
                      uppercase presentation. Hardcoding an uppercase copy would put a second,
                      drifting spelling of the frozen label into the tree. */}
                  <th className={`${styles.upper} ${styles.r} ${styles.secondary}`}>
                    chosen {SIGNAL_TRIALS_MARKOUT_LABEL}
                  </th>
                  <th className={styles.upper}>STATUS</th>
                </tr>
              </thead>
              <tbody>
                {/* Served order, verbatim — the backend guarantees ascending receipt_id and it is
                    the backend's guarantee to keep. Re-sorting here would repair a drifted order
                    and destroy the only evidence that it drifted. */}
                {state.entries.map((e) => (
                  <ParticipantRow key={e.receipt.receiptId} entry={e} />
                ))}
              </tbody>
            </table>
          </div>
          <p className={styles.footNote}>
            FOLLOW at p ≥ 0.60 · FADE at p ≤ 0.40 · ABSTAIN otherwise. The action shown is the one
            recorded on each receipt, relayed as served — this card never re-derives it. No
            rationale, model message, confidence explanation, or portfolio recommendation is
            recorded or displayed.
          </p>
          <p className={styles.footNote}>
            This route serves finalized commitments only, so every agent listed above paid for the
            commitment shown.
          </p>
          {state.entries.map((e) => (
            <FairPlayChecks key={e.receipt.receiptId} entry={e} />
          ))}
          <p className={styles.footNote}>
            Every check reports independently as pass / fail / pending. There is no single aggregate
            badge — one pending or failed check is never absorbed into a green summary.
          </p>
          {/* Was "They certify that the benchmark was produced correctly" — an unconditional
              aggregate success claim sitting six lines under this file's own rule that no failed
              check is absorbed into a green summary. What the checks do is REPORT; what they
              report is scoped to the one receipt each verdict sits under. */}
          <p className={styles.footNote}>
            These are reproducibility checks over recorded evidence: they re-derive each
            receipt&apos;s commit-time and settlement-time claims from the stored artifacts and
            report independently whether those claims re-derive. A verdict is a finding about the
            receipt it sits under and nothing else — it establishes nothing about any other
            receipt, and it says nothing about how a submitted probability scored.
          </p>
        </>
      );
  }
}

function ParticipantRow({ entry }: { entry: ParticipantEntry }) {
  const r = entry.receipt;
  return (
    <tr className={styles.row} data-testid="participant-row">
      <td
        className="mono"
        data-testid="participant-payer"
        data-label="AGENT"
        aria-label="AGENT"
      >
        {r.payer}
      </td>
      <td data-testid="participant-role" data-label="ROLE" aria-label="ROLE">
        external payer
      </td>
      <td
        className={styles.num}
        data-testid="participant-p"
        data-label="p_follow_profitable"
        aria-label="p_follow_profitable"
      >
        {r.pFollowProfitable.toFixed(2)}
      </td>
      {/* RELAYED, NOT RE-DERIVED. The backend derived this action at commit time and recorded it;
          re-deriving the bands here would let a client-side rule silently overrule the record. */}
      <td data-testid="participant-action" data-label="DERIVED ACTION" aria-label="DERIVED ACTION">
        <span className={styles.actionChip} data-action={r.action}>{r.action}</span>
      </td>
      <td
        className={styles.num}
        data-testid="participant-brier"
        data-label="BRIER"
        aria-label="BRIER"
      >
        {brierText(r.brier)}
      </td>
      <td
        className={styles.num}
        data-testid="participant-markout"
        data-label={`chosen ${SIGNAL_TRIALS_MARKOUT_LABEL}`}
        aria-label={`chosen ${SIGNAL_TRIALS_MARKOUT_LABEL}`}
      >
        {bps(r.chosenMarkoutBps)}
      </td>
      {/* C26 at the participant surface: `pending` and `UNSCORED` rows carry identical null
          metrics, so this cell is the only thing that separates them. */}
      <td data-testid="participant-status" data-label="STATUS" aria-label="STATUS">
        {r.status}
      </td>
    </tr>
  );
}

function FairPlayChecks({ entry }: { entry: ParticipantEntry }) {
  const { receipt, checks } = entry;
  const [descriptionsExpanded, setDescriptionsExpanded] = useState(true);
  const verify = checks.kind === 'ok' ? checks.verify : null;
  // The verify route can serve a receipt whose status disagrees with the one the participant route
  // served. Surfaced rather than silently preferring one: they are two published reads of the same
  // record, and a disagreement between them is information.
  const disagreement =
    verify !== null && verify.receiptStatus !== null && verify.receiptStatus !== receipt.status
      ? verify.receiptStatus
      : null;
  const tally = verify === null
    ? null
    : (['pass', 'pending', 'fail', null] as const)
      .map((status) => {
        const count = verify.checks.filter((check) => check.status === status).length;
        return count === 0 ? null : `${count} ${status === null ? 'not served' : status}`;
      })
      .filter((part): part is string => part !== null)
      .join(' · ');

  return (
    <div
      className={styles.checks}
      data-testid="fairplay-checks"
      data-checks-state={checks.kind}
      // Read ALONGSIDE the verdicts: a check reading `pending` does NOT distinguish "not settled
      // yet" from "settled UNSCORED and never will be". Only the receipt status does.
      data-receipt-status={receipt.status}
    >
      <div className={styles.panelHead}>
        <p className={styles.panelLabel}>FAIR-PLAY CHECKS · {receipt.payer}</p>
        {verify === null ? null : (
          <button
            type="button"
            className={styles.checkToggle}
            aria-expanded={descriptionsExpanded}
            onClick={() => setDescriptionsExpanded((expanded) => !expanded)}
          >
            {descriptionsExpanded
              ? 'Collapse Fair-Play check descriptions'
              : 'Expand Fair-Play check descriptions'}
          </button>
        )}
      </div>
      {/* Was "was this benchmark produced correctly?". Interrogative, so it asserted nothing — but
          it puts the aggregate phrasing on screen beside eight independent verdicts, and a reader
          skimming a screenshot does not parse a question mark. Scoped to this receipt instead. */}
      <p className={styles.panelSub}>
        does this receipt&apos;s record re-derive? · receipt status {receipt.status}
        {receipt.status === 'pending'
          ? ' — outcome-time checks cannot be evaluated until a candle exists'
          : ''}
        {receipt.status === 'UNSCORED'
          ? ' — outcome-time checks will never leave pending for this receipt'
          : ''}
      </p>

      {checks.kind === 'unavailable' ? (
        <p className={styles.stateBody}>
          Verification could not be run for this receipt. The commitment itself is still recorded,
          and the other participants&apos; verdicts are unaffected.
        </p>
      ) : null}
      {checks.kind === 'not_found' ? (
        <p className={styles.stateBody}>
          The verify route does not recognise this receipt id. No verdict is inferred for it.
        </p>
      ) : null}

      {verify !== null ? (
        <>
          <div className={styles.checkTierHead}>
            <span>COMMIT-TIME</span>
            <span>OUTCOME-TIME</span>
            <strong data-testid="fairplay-tally">{tally}</strong>
          </div>
          <ul className={styles.checkList}>
            {/* The frozen eight, always in the frozen order, always driven off the SINGLE exported
                constant (CF-5). The adapter already guarantees length 8 and the order; iterating
                its output rather than a local list is what keeps a second spelling out of the
                tree. */}
            {verify.checks.map((c) => {
              const status = c.status === null ? 'not_served' : c.status;
              const presentation = CHECK_STATUS_PRESENTATION[status];
              return (
                <li
                  key={c.key}
                  className={styles.checkRow}
                  data-testid="check-row"
                  data-key={c.key}
                  data-phase={c.phase}
                  // `null` means the backend did not serve this key. It is NOT the verdict
                  // `pending`, which the verifier would then never have made.
                  data-status={status}
                >
                  <code className={styles.checkKey}>{c.key}</code>
                  <span className={styles.checkPhase}>{c.phase}-time</span>
                  {descriptionsExpanded ? (
                    <span className={styles.checkDesc} data-testid="check-description">
                      {CHECK_DESCRIPTION[c.key as FrozenCheckKey]}
                    </span>
                  ) : null}
                  <span
                    className={styles.checkStatus}
                    data-status={status}
                    data-testid="check-status"
                  >
                    <span aria-hidden="true" data-testid="check-status-glyph">
                      {presentation.glyph}
                    </span>{' '}
                    {presentation.word}
                  </span>
                </li>
              );
            })}
          </ul>
          {verify.unexpectedKeys.length > 0 ? (
            // CF-5 drift is invisible precisely because the keys are unconstrained `str`, so a
            // name we do not recognise has to be reportable rather than swallowed.
            <p className={styles.stateSub} data-testid="checks-unexpected-keys">
              keys served outside the frozen eight: {verify.unexpectedKeys.join(', ')}
            </p>
          ) : null}
          <p className={styles.footNote}>
            Every check reports independently as pass / fail / pending. There is no single
            aggregate verified badge for this receipt.
          </p>
          {disagreement !== null ? (
            <p className={styles.stateSub}>
              the verify route reports status {disagreement} for this receipt, which differs from
              the participant route&apos;s {receipt.status}
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// the cost-sensitivity table (element 4)
// ---------------------------------------------------------------------------

function MarkoutSection({ outcome }: { outcome: TrialOutcome | null }) {
  const followOfficial = outcome?.status === 'settled' ? outcome.followMarkoutBps : null;
  const fadeOfficial = outcome?.status === 'settled' ? outcome.fadeMarkoutBps : null;
  const settled = followOfficial !== null && fadeOfficial !== null;
  const gross = followOfficial === null ? null : followOfficial + OFFICIAL_COST_BPS;
  const rows = COST_SWEEP_BPS.map((cost) => ({
    cost,
    official: cost === OFFICIAL_COST_BPS,
    follow: cost === OFFICIAL_COST_BPS || gross === null ? followOfficial : gross - cost,
    fade: cost === OFFICIAL_COST_BPS || gross === null ? fadeOfficial : -gross - cost,
  }));
  const officialFormulaFade = gross === null ? null : -gross - OFFICIAL_COST_BPS;
  const officialPairIsInconsistent = settled
    && officialFormulaFade !== null
    && Math.abs(fadeOfficial - officialFormulaFade) > 1e-9;

  return (
    <details className={`${styles.panel} ${styles.disclosure}`} open={settled} data-testid="markout-table">
      <summary className={styles.disclosureSummary}>
        <span className={styles.panelLabel}>
          COST SENSITIVITY · <span className={styles.frozenLabel}>{SIGNAL_TRIALS_MARKOUT_LABEL}</span>
        </span>
        <span className={styles.disclosureState}>
          {settled ? 'SETTLED EVIDENCE · SWEEP AVAILABLE' : 'NO SETTLED MARKOUT · NO SWEEP'}
        </span>
      </summary>

      {!settled ? (
        <p className={styles.stateBody}>
          No markout exists at any cost assumption for this trial, so no sweep is shown. The
          official {OFFICIAL_COST_BPS} bps basis is unchanged.
        </p>
      ) : (
        <>
          <p className={styles.panelSub}>official rank basis: {OFFICIAL_COST_BPS} bps modeled costs</p>
          {/* VERBATIM. The DOM carries the task packet's exact spelling; `.verbatimUpper` supplies
              the design handoff's uppercase presentation. Same resolution as UNSCORED above. */}
          <p className={`${styles.tag} ${styles.verbatimUpper}`} data-testid="markout-diagnostic-tag">
            diagnostic — does not change ranking
          </p>
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th className={styles.upper}>DECLARED COST</th>
                  <th className={`${styles.upper} ${styles.r}`}>FOLLOW MARKOUT (BPS)</th>
                  <th className={`${styles.upper} ${styles.r}`}>FADE MARKOUT (BPS)</th>
                  <th className={styles.upper}>BASIS</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(({ cost, official, follow, fade }) => (
                  <tr
                    key={cost}
                    className={styles.row}
                    data-testid="markout-row"
                    data-cost-bps={cost}
                    data-basis={official ? 'official' : 'diagnostic'}
                  >
                    <td
                      className={styles.num}
                      data-label="DECLARED COST"
                      aria-label="DECLARED COST"
                    >
                      {cost} bps
                    </td>
                    <td
                      className={styles.num}
                      data-testid="markout-follow"
                      data-label="FOLLOW MARKOUT (BPS)"
                      aria-label="FOLLOW MARKOUT (BPS)"
                    >
                      {bps(follow)}
                    </td>
                    <td
                      className={styles.num}
                      data-testid="markout-fade"
                      data-label="FADE MARKOUT (BPS)"
                      aria-label="FADE MARKOUT (BPS)"
                    >
                      {bps(fade)}
                    </td>
                    <td data-label="BASIS" aria-label="BASIS">
                      {official ? 'official rank basis' : 'diagnostic'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {officialPairIsInconsistent ? (
            <p className={styles.fence} data-testid="markout-formula-warning">
              The served 25 bps pair is inconsistent with the declared diagnostic formula. The
              official values remain displayed verbatim.
            </p>
          ) : null}
          <p className={styles.footNote}>
            The {OFFICIAL_COST_BPS} bps row is the only basis the season ranks on and is displayed
            from the served official pair. The 0, 10 and 50 bps rows are diagnostic presentation
            derivations from gross = served FOLLOW + {OFFICIAL_COST_BPS}; they do not alter the
            official evidence.
          </p>
          <p className={styles.footNote}>
            The sweep exists to answer one question: does the ordering survive the cost assumption?
            It never re-ranks the season.
          </p>
        </>
      )}
    </details>
  );
}
