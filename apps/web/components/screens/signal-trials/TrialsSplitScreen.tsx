import type { CommitReceipt, TrialCard } from '@/lib/contracts';
import styles from './TrialsSplitScreen.module.css';

const DASH = '—';

function choosePair(receipts: CommitReceipt[]): CommitReceipt[] {
  const ordered = [...receipts].sort((a, b) => a.payer.localeCompare(b.payer));
  if (ordered.length < 2) return ordered;
  let pair: [CommitReceipt, CommitReceipt] = [ordered[0], ordered[1]];
  let delta = Math.abs(pair[0].pFollowProfitable - pair[1].pFollowProfitable);
  for (let left = 0; left < ordered.length - 1; left += 1) {
    for (let right = left + 1; right < ordered.length; right += 1) {
      const candidate = Math.abs(
        ordered[left].pFollowProfitable - ordered[right].pFollowProfitable,
      );
      if (candidate > delta) {
        pair = [ordered[left], ordered[right]];
        delta = candidate;
      }
    }
  }
  return pair;
}

function metric(receipt: CommitReceipt, value: number | null, digits: number): string {
  if (receipt.status !== 'settled' || value === null) return DASH;
  return value.toFixed(digits);
}

function Side({ receipt }: { receipt: CommitReceipt }) {
  return (
    <article className={`${styles.splitSide} split-side`} data-testid="split-agent">
      <code className={styles.payer}>{receipt.payer}</code>
      <dl className={styles.metrics}>
        <div><dt>p_follow_profitable</dt><dd>{receipt.pFollowProfitable.toFixed(2)}</dd></div>
        <div><dt>DERIVED ACTION</dt><dd>{receipt.action}</dd></div>
        <div><dt>BRIER</dt><dd>{metric(receipt, receipt.brier, 3)}</dd></div>
        <div><dt>CHOSEN PAPER MARKOUT (BPS)</dt><dd>{metric(receipt, receipt.chosenMarkoutBps, 1)}</dd></div>
        <div><dt>STATUS</dt><dd>{receipt.status}</dd></div>
      </dl>
    </article>
  );
}

function AbsentSide() {
  return (
    <article className={`${styles.splitSide} split-side`} data-testid="split-agent-absent">
      <h3>NO COMMITMENT RECORDED</h3>
      <p>
        No second agent committed to this trial before the deadline. The shared evidence, deadline,
        and law are unchanged; nothing is inferred for the missing side.
      </p>
      <p className={styles.absentValues}>p_follow_profitable — · action — · brier —</p>
    </article>
  );
}

function SharedBand({ trial, delta }: { trial: TrialCard; delta: number | null }) {
  return (
    <div className={styles.splitEvidence} data-testid="split-evidence">
      <p className={styles.label}>SHARED EVIDENCE</p>
      <div className={styles.hash} data-testid="split-evidence-hash">
        <span>⬢ ONE HASH</span><code>{trial.evidenceHash}</code>
      </div>
      <p>SAME DEADLINE · {trial.commitDeadlineMs}</p>
      <p>SAME LAW · FIXED 1H HORIZON</p>
      {delta === null ? null : <p className={styles.delta}>DISAGREEMENT · Δ {delta.toFixed(2)}</p>}
    </div>
  );
}

function sharedOutcome(trial: TrialCard): string {
  const o = trial.outcome;
  if (o === null) return 'no outcome record';
  if (o.status === 'pending') return 'pending · outcome not known';
  if (o.status === 'UNSCORED') {
    return 'UNSCORED · no completed settlement candle · future, close_ts, markout and Brier are null · nothing interpolated';
  }
  return `settled · entry ${o.entry} → close ${o.future} · follow_profitable ${String(o.followProfitable)}`;
}

export function TrialsSplitScreen({
  trial,
  receipts,
}: {
  trial: TrialCard;
  receipts: CommitReceipt[];
}) {
  const pair = choosePair(receipts);
  const delta = pair.length === 2
    ? Math.abs(pair[0].pFollowProfitable - pair[1].pFollowProfitable)
    : null;
  return (
    <section
      className={styles.panel}
      data-testid="trials-split"
      data-state={receipts.length === 0 ? 'empty' : receipts.length === 1 ? 'one' : 'pair'}
    >
      <div className={styles.head}>
        <div>
          <p className={styles.label}>SAME-EVIDENCE COMPARISON</p>
          <p className={styles.sub}>two agents · one snapshot · one hash · one t0 · one deadline · one law</p>
        </div>
        <span>NO INDEPENDENT EVIDENCE PER SIDE</span>
      </div>
      <div className={styles.splitGrid}>
        {pair[0] ? <Side receipt={pair[0]} /> : null}
        <SharedBand trial={trial} delta={delta} />
        {pair[1] ? <Side receipt={pair[1]} /> : receipts.length === 1 ? <AbsentSide /> : null}
      </div>
      <p className={styles.caption}>
        p_follow_profitable → action derived by the server at p ≥ 0.60 / ≤ 0.40; this client relays
        the recorded action.
      </p>
      <div className={styles.outcome} data-testid="split-outcome">
        <strong>ONE SHARED OUTCOME BENEATH BOTH AGENTS</strong>
        <span>{sharedOutcome(trial)}</span>
      </div>
    </section>
  );
}
