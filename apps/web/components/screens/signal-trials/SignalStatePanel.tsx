import type { CommitReceipt, OpenTrial, TrialCard } from '@/lib/contracts';
import styles from './SignalStatePanel.module.css';

export const FIXED_RANKING_HORIZON_MS = 3_600_000;

type RailTrial = OpenTrial | TrialCard;

function outcomeFor(trial: RailTrial) {
  return 'outcome' in trial ? trial.outcome : null;
}

function outcomeText(trial: RailTrial): string {
  if (!('outcome' in trial)) return 'outcome record not served by this read';
  const outcome = trial.outcome;
  if (outcome === null) return 'no outcome record';
  if (outcome.status === 'pending') {
    return 'outcome not known · settles at t0 + 1h against the recorded event boundary';
  }
  if (outcome.status === 'UNSCORED') {
    return 'no completed settlement candle · future, close_ts, markout and Brier are null · nothing interpolated';
  }
  return `entry ${outcome.entry} → close ${outcome.future} · follow ${outcome.followMarkoutBps} / fade ${outcome.fadeMarkoutBps} bps · follow_profitable ${String(outcome.followProfitable)}`;
}

export function SharedEvidenceRail({
  trial,
  agents,
  compact = false,
  testId = 'shared-evidence-rail',
}: {
  trial: RailTrial;
  agents: CommitReceipt[];
  compact?: boolean;
  testId?: string;
}) {
  const outcome = outcomeFor(trial);
  return (
    <section
      className={`${styles.rail} ${compact ? styles.compact : ''}`}
      data-testid={testId}
      data-mode={compact ? 'compact' : 'full'}
    >
      <div className={styles.railHead}>
        <div>
          <p className={styles.label}>SHARED EVIDENCE RAIL</p>
          <p className={styles.sub} data-testid="rail-summary">
            ONE SNAPSHOT · ONE DEADLINE · ONE LAW ·{' '}
            {outcome === null ? 'NO OUTCOME RECORD' : 'ONE OUTCOME RECORD'}
          </p>
        </div>
        {compact ? <span className={styles.meta}>FEATURED TRIAL · {trial.trialId}</span> : null}
      </div>
      <div className={styles.railNodes}>
        <div className={styles.railNode} data-testid="rail-node">
          <span>01</span><strong>OKX SIGNAL SNAPSHOT</strong>
          <small>{trial.evidence.symbol} · chain {trial.evidence.chainIndex}</small>
        </div>
        <div className={styles.railNode} data-testid="rail-node">
          <span>02</span><strong>t0 · EVIDENCE SEALED</strong>
          <small>{trial.t0Ms}</small>
        </div>
        <div className={styles.railNode} data-testid="rail-node">
          <span>03</span><strong>ONE COMMIT DEADLINE</strong>
          <small>{trial.commitDeadlineMs} · decision window</small>
        </div>
        <div className={styles.railNode} data-testid="rail-node">
          <span>04</span><strong>ONE SETTLEMENT LAW</strong>
          <small>fixed 1h ranking horizon</small>
        </div>
      </div>
      <div className={styles.hashChip} data-testid="rail-hash-chip">
        <span>⬢ SEALED EVIDENCE</span>
        <code data-testid="trial-evidence-hash">{trial.evidenceHash}</code>
        <span>ONE RECORD · ONE HASH</span>
      </div>
      <div className={styles.agents}>
        {agents.map((agent) => (
          <div className={styles.agent} data-testid="rail-agent" key={agent.receiptId}>
            <code>{agent.payer}</code>
            <span>p {agent.pFollowProfitable.toFixed(2)}</span>
            <strong>{agent.action}</strong>
          </div>
        ))}
      </div>
      <div className={styles.outcome} data-testid="rail-outcome">
        <strong>ONE EVENT-ANCHORED OUTCOME</strong>
        <span>{outcomeText(trial)}</span>
      </div>
      <p className={styles.note}>
        This trial has one sealed evidence payload, one commit deadline and one settlement law.
        That is the structure of the record, not a finding about any receipt.
      </p>
      <p className={styles.note}>
        Whether a given commitment actually re-derives against this record — that its body
        re-hashes, that it binds the trial it names, and that it arrived before the deadline — is
        not asserted here. The Fair-Play checks below report it per receipt, one independent
        verdict at a time.
      </p>
      <p className={styles.note}>
        A score exists only where a settled outcome does: Brier and chosen markout are derived from
        the settled outcome under the recorded law. A pending commitment has none yet, and an
        UNSCORED one never will.
      </p>
    </section>
  );
}

export function SignalStatePanel({ trial }: { trial: RailTrial }) {
  const e = trial.evidence;
  const fields: Array<[string, string | number]> = [
    ['SYMBOL / NAME', `${e.symbol} / ${e.name}`],
    ['CHAIN INDEX', e.chainIndex],
    ['TOKEN ADDRESS', e.tokenAddress],
    ['TRIGGER PRICE (USD)', e.triggerPrice],
    ['TRIGGER WALLET COUNT', e.triggerWalletCount],
    ['AMOUNT (USD)', e.amountUsd],
    ['MARKET CAP (USD)', e.marketCapUsd],
    ['HOLDERS', e.holders],
    ['TOP-10 HOLDER %', e.top10HolderPercent],
    ['TRIGGER WALLET', e.triggerWalletAddress],
    ['WALLET TYPE', e.walletType],
    ['EVIDENCE HASH', trial.evidenceHash],
  ];
  return (
    <section className={styles.panel} data-testid="signal-state-panel">
      <div className={styles.railHead}>
        <div>
          <p className={styles.label}>SIGNAL STATE AT DECISION</p>
          <p className={styles.sub}>visible_at_decision tier only — every field a recorded t0 snapshot</p>
        </div>
      </div>
      <dl className={styles.fields}>
        {fields.map(([label, value]) => (
          <div className={styles.field} key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <div className={styles.exclusions}>
        <strong>EXCLUDED FROM EVIDENCE</strong>
        <p>exact historical liquidity — not returned by the signal endpoint; the record states only that the trial passed the $20k min-liquidity query filter.</p>
        <p>soldRatioPercent — trigger-time semantics unverified; withheld from agent evidence.</p>
      </div>
    </section>
  );
}

export function EvidenceLawIdentity({ trial }: { trial: RailTrial }) {
  return (
    <section className={styles.panel} data-testid="evidence-law-identity">
      <p className={styles.label}>EVIDENCE &amp; LAW IDENTITY</p>
      <dl className={styles.fields}>
        <div className={styles.field}><dt>TRIAL ID</dt><dd>{trial.trialId}</dd></div>
        <div className={styles.field}><dt>TRIAL MODE</dt><dd>{trial.trialMode}</dd></div>
        <div className={styles.field}><dt>EVIDENCE HASH</dt><dd>{trial.evidenceHash}</dd></div>
        <div className={styles.field}><dt>t0 (SEAL)</dt><dd>{trial.t0Ms}</dd></div>
        <div className={styles.field}><dt>COMMIT DEADLINE</dt><dd>{trial.commitDeadlineMs}</dd></div>
        <div className={styles.field}>
          <dt>RANKING HORIZON</dt><dd>{FIXED_RANKING_HORIZON_MS} ms · fixed 1h horizon</dd>
        </div>
      </dl>
    </section>
  );
}
