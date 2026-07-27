'use client';
import { useCallback, useEffect, useState } from 'react';
import { getSignalTrialsSeason, SIGNAL_TRIALS_MARKOUT_LABEL } from '@/lib/signal-trials-api';
import type { SeasonViewState, SignalTrialsRow, SignalTrialsSeason } from '@/lib/contracts';
import styles from './SeasonScreen.module.css';

// H5.2 — the PUBLIC season standings at /trials. No AuthGate: this is the judge-facing benchmark
// and gating it would put the honesty surface behind a wallet.
//
// WHY THIS SCREEN FETCHES ITSELF rather than taking rows as a prop (the LeaderboardScreen shape):
// the state it must render is not a list, it is a DISCRIMINATED VERDICT, and the discrimination
// happens inside `getSignalTrialsSeason()`. Both `not_built` and `no_season` answer 404 on
// /signal-trials/season — /health is the ONLY place they differ, and C54 makes `not_built`
// reachable in production for every failed, refused or aborted probe. A caller that flattened the
// composed result into rows would throw that distinction away before the screen ever saw it. The
// precedent is `useMakerArenaResult` in the maker lane: a screen that owns its own status machine.
//
// THE HONESTY AXIS THIS FILE PROTECTS, restated because it is a public leaderboard:
//   * null is NOT zero. A zero markout is a real FLAT outcome; a zero Brier is a PERFECT score.
//     Every nullable metric renders `—` on null and its real value on 0. There is no `??` here.
//   * A transport failure is NOT a domain verdict. A 500 renders `unavailable`, never
//     "no season published" — the second would report that a preflight ran and declined when it
//     never ran at all. That is why the 404 branch is resolved through /health and not a catch.
//   * No loading, empty or error state carries a value. Not a name, not a metric, not a count.
//   * The client NEVER re-sorts. The backend ranks; ORD is a display ordinal (index + 1).

// The six things this screen can be showing. Five come from the adapter's `SeasonViewState` plus
// `loading`/`unavailable`, which are transport facts and deliberately NOT part of that union —
// merging them would let a fetch failure typecheck as a season verdict.
type ScreenState = SeasonViewState | 'loading' | 'unavailable';

const STATE_CHIP: Record<ScreenState, string> = {
  loading: 'loading',
  qualified: 'qualified season',
  exploratory: 'exploratory season',
  no_season: 'no season published',
  not_built: 'not built',
  unavailable: 'unavailable',
};

export function SeasonScreen() {
  const [season, setSeason] = useState<SignalTrialsSeason | null>(null);
  const [state, setState] = useState<ScreenState>('loading');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let alive = true;
    setState('loading');
    setSeason(null); // no stale standings survive a retry — see the `unavailable` copy
    getSignalTrialsSeason()
      .then((s) => {
        if (!alive) return;
        setSeason(s);
        setState(s.seasonStatus);
      })
      .catch(() => {
        // Every throw reaching here is a TRANSPORT or contract failure: the adapter resolves both
        // 404 branches itself and only throws on non-404 statuses, an unreadable /health, or an
        // unrecognised season_state. None of those is evidence about a season, so none of them may
        // render as one.
        if (alive) setState('unavailable');
      });
    return () => { alive = false; };
  }, [attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  return (
    <section className={styles.screen} aria-label="ProofArena season standings">
      <header className={styles.head}>
        <div className={styles.headText}>
          <h1 className={styles.title}>ProofArena</h1>
          <p className={styles.lead}>paper markout after modeled costs vs. predeclared baselines</p>
          <p className={styles.leadSub}>
            Official ranking is Brier-first. Markout is a legibility metric, not a trading result.
          </p>
        </div>
        <span className={styles.chip} data-state={state} data-testid="season-status-chip">
          {STATE_CHIP[state]}
        </span>
      </header>

      <div className={styles.statePanel} data-state={state} data-testid="season-panel">
        <SeasonBody state={state} season={season} onRetry={retry} />
      </div>
    </section>
  );
}

function SeasonBody({
  state, season, onRetry,
}: { state: ScreenState; season: SignalTrialsSeason | null; onRetry: () => void }) {
  switch (state) {
    case 'loading':
      return <LoadingState />;
    case 'unavailable':
      return <UnavailableState onRetry={onRetry} />;
    case 'no_season':
      return <NoSeasonState />;
    case 'not_built':
      return <NotBuiltState />;
    case 'qualified':
    case 'exploratory':
      // `season` is non-null on both standings branches — it is what produced the state.
      return season ? <Standings season={season} state={state} /> : <LoadingState />;
  }
}

// ---------------------------------------------------------------------------
// The empty / transport states. NONE of them renders a value.
// ---------------------------------------------------------------------------

function LoadingState() {
  return (
    <>
      <p className={styles.stateSub}>SEASON STANDINGS</p>
      {/* Named so it is unmistakable in a screenshot: the bars below stand for nothing. A skeleton
          filled with plausible agent names and scores is the most persuasive lie this screen could
          tell, so it holds no names, no scores and no counts. */}
      <p className={styles.stateBody}>LOADING · NO PLACEHOLDER RESULTS SHOWN</p>
      <div className={styles.skeletonRow} aria-hidden />
      <div className={styles.skeletonRow} aria-hidden />
    </>
  );
}

function UnavailableState({ onRetry }: { onRetry: () => void }) {
  return (
    <>
      <h2 className={styles.stateTitle}>Benchmark data unavailable</h2>
      <p className={styles.stateBody}>
        The season endpoint did not respond. No cached or partial standings are shown.
      </p>
      <button type="button" className={styles.retry} onClick={onRetry} data-testid="season-retry">
        RETRY →
      </button>
      <p className={styles.stateSub}>
        A transport failure is never rendered as “no season published”.
      </p>
    </>
  );
}

// The 404 branch the PREFLIGHT REACHED: it ran, examined the eligible signals, and declined to
// publish a season. That is a verdict, and it comes with the evidence trail behind it — hence the
// probe-counts affordance, which is this state's distinguishing feature against `not_built`.
function NoSeasonState() {
  return (
    <>
      <h2 className={styles.stateTitle}>No season published</h2>
      <p className={styles.stateBody}>
        Preflight found insufficient eligible signals for a truthful season.
      </p>
      <details className={styles.probeCounts} data-testid="season-probe-counts">
        <summary className={styles.probeSummary}>VIEW PROBE COUNTS →</summary>
        <p className={styles.stateSub}>
          Per-combo eligible-settleable counts and rejection reasons are recorded in the replay-pack
          manifest.
        </p>
      </details>
    </>
  );
}

// The 404 branch the preflight NEVER REACHED. C54 makes this reachable in production for every
// failed, refused or aborted probe, so it is not a theoretical state — and it must never borrow
// `no_season`'s copy, which would assert a preflight verdict that was never produced. It carries
// NO probe-counts affordance for exactly that reason: there are no probe counts, because nothing
// probed. No retry either: a retry would suggest this is a transient failure, and it is not.
function NotBuiltState() {
  return (
    <>
      <h2 className={styles.stateTitle}>Season not built yet</h2>
      <p className={styles.stateBody}>The data preflight has not published a season state.</p>
      <p className={styles.stateSub}>season_state = not_built · no standings exist to show or retry</p>
    </>
  );
}

// ---------------------------------------------------------------------------
// The standings table — `qualified` and `exploratory`.
// ---------------------------------------------------------------------------

function Standings({ season, state }: { season: SignalTrialsSeason; state: 'qualified' | 'exploratory' }) {
  // An exploratory season states that NO (chain × bar) combination cleared the 40-trial gate.
  // A `qualified: true` row served underneath that is a skill claim the season itself denies, so
  // the badge is suppressed at the SEASON level rather than trusted per row. Relaying the
  // contradiction would publish the stronger of two conflicting claims, which is the wrong default
  // on a public artifact.
  const badgesAllowed = state === 'qualified';

  return (
    <>
      <div className={styles.metaRow}>
        <span className={styles.metaLabel}>SEASON</span>
        <span data-testid="season-combo">
          {season.seasonId ?? '—'}
          {season.combo
            ? Object.entries(season.combo).map(([k, v]) => ` · ${k} ${String(v)}`).join('')
            : ''}
        </span>
      </div>
      <p className={styles.stateSub}>
        1h ranking horizon · deduplicated, chronological, 4h same-token cooldown
      </p>

      {state === 'exploratory' ? (
        <p
          className={`${styles.banner} ${styles.bannerWarn}`}
          data-testid="season-exploratory-banner"
        >
          <span className={styles.bannerTitle}>EXPLORATORY — NO QUALIFIED-SKILL CLAIM</span>
          No (chain × bar) combination reached the 40-trial gate. Standings below are provisional:
          every row carries qualified = false, no row earns a qualified badge, and nothing here
          supports a skill claim.
        </p>
      ) : null}

      {/* Sample size renders ONLY where a season document exists to report one. On a 404 the
          adapter returns `sampleSize: null` rather than 0 — "no season document" is a different
          claim from "we sampled zero signals" — and this element is absent there entirely. */}
      {season.sampleSize === null ? null : (
        <p className={styles.stateSub} data-testid="season-sample-size">
          N = {season.sampleSize} settled trials
        </p>
      )}

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th className={styles.upper}>ORD</th>
              <th className={styles.upper}>AGENT</th>
              <th className={styles.upper}>ROLE</th>
              {/* Brier FIRST among the metrics — it is the rank key, and column order on a
                  leaderboard is itself a claim about what is being ranked. */}
              <th className={`${styles.upper} ${styles.r} ${styles.rankKey}`}>AVG BRIER ↓ / RANK KEY</th>
              {/* The frozen label, lowercase in the DOM; `.upper` supplies the design's uppercase
                  presentation. See SeasonScreen.module.css. */}
              <th className={`${styles.upper} ${styles.r} ${styles.secondary}`}>
                {SIGNAL_TRIALS_MARKOUT_LABEL}
              </th>
              <th className={`${styles.upper} ${styles.r}`}>ACTIVE DECISIONS</th>
              <th className={`${styles.upper} ${styles.r}`}>ACTIVE COVERAGE</th>
              <th className={`${styles.upper} ${styles.r}`}>UN-SCORED</th>
              <th className={styles.upper}>QUALIFIED</th>
            </tr>
          </thead>
          <tbody>
            {/* Served order, verbatim. `rankByAvgClv`-style client sorting is deliberately absent:
                the backend ranks and the client renders. ORD is `index + 1` — a display ordinal,
                never a rank this screen computed. */}
            {season.rows.map((row, i) => (
              <SeasonRow key={row.agentId} row={row} ord={i + 1} badgesAllowed={badgesAllowed} />
            ))}
          </tbody>
        </table>

        <div className={styles.tableFoot} data-testid="season-footnotes">
          <p className={styles.footNote}>
            Rows are rendered in the order returned by GET /signal-trials/season. The client never
            re-sorts and never computes rank locally.
          </p>
          <p className={styles.footNote}>
            Ranking is lowest average Brier over 1h-settled trials. Paper markout is a secondary
            legibility metric computed after 25 bps of modeled costs — a modeled quantity over
            recorded bars, never an executed trade.
          </p>
          <p className={styles.footNote}>
            The four controls are part of the benchmark, not failed contestants: they are the bar a
            contestant must clear. Un-scored trials never count toward coverage.
          </p>
          {/* The honesty boundary, stated where the numbers are. These standings are reproducible
              from the recorded evidence — that is a claim about re-derivation, and deliberately
              not a claim about what a malicious storage operator could do. */}
          <p className={styles.footNote}>
            Every row above is reproducible from the evidence recorded for this season. That is
            reproducibility over stored records — nothing here asserts anything stronger about the
            storage itself.
          </p>
        </div>
      </div>
    </>
  );
}

function SeasonRow({
  row, ord, badgesAllowed,
}: { row: SignalTrialsRow; ord: number; badgesAllowed: boolean }) {
  return (
    <tr className={styles.row} data-testid="season-row">
      <td className="mono" data-testid="season-ord">{ord}</td>
      <td data-testid="season-agent">{row.agentId}</td>
      <td
        className={row.isControl ? styles.roleControl : styles.roleContestant}
        data-testid="season-role"
      >
        {row.isControl ? 'baseline control' : 'contestant'}
      </td>
      {/* avgBrier: null ⇒ nothing settled. `0` ⇒ a PERFECT score, and it renders as one. The
          explicit `=== null` is load-bearing — `??` or `||` would erase the perfect score. */}
      <td className={styles.num} data-testid="season-brier">
        {row.avgBrier === null ? <span className={styles.dash}>—</span> : row.avgBrier.toFixed(3)}
      </td>
      {/* cappedAvgMarkoutBps: null ⇒ nothing settled. `0` ⇒ a real FLAT outcome. */}
      <td className={styles.num} data-testid="season-markout">
        {row.cappedAvgMarkoutBps === null
          ? <span className={styles.dash}>—</span>
          : `${row.cappedAvgMarkoutBps > 0 ? '+' : ''}${row.cappedAvgMarkoutBps.toFixed(1)} bps`}
      </td>
      <td className={styles.num}>{row.activeDecisions}</td>
      <td className={styles.num}>{(row.activeCoverage * 100).toFixed(1)}%</td>
      <td className={styles.num}>{row.unscored}</td>
      <td>
        {row.isControl ? (
          // A control is not competing, so it is neither qualified nor unqualified — reporting it
          // as "not qualified" would read as a control that failed the gate.
          <span className={styles.notQualified}>n/a — control</span>
        ) : badgesAllowed && row.qualified ? (
          <span className={styles.qualifiedChip} data-testid="season-qualified-badge">qualified</span>
        ) : (
          <span className={styles.notQualified}>not qualified</span>
        )}
      </td>
    </tr>
  );
}
