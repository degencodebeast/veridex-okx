import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import { ProofArenaShell } from '@/components/layout/ProofArenaShell';

// The (proofarena) route group: `/trials` and `/trials/[trialId]`, and nothing else. It is a SIBLING
// of `(app)`, not a child, and that is the whole mechanism. A `layout.tsx` placed under
// `app/(app)/trials/` would NEST inside `AppShell` rather than replace it, so the only way to give
// these two routes their own shell is to lift them out of the group that mounts the legacy chrome.
// Route groups are URL-transparent — the `(proofarena)` segment contributes nothing to the path, so
// both routes resolve exactly where they did before.
//
// NO StatusBarProvider here, and its absence is a decision rather than an omission: that context
// carries the active COMPETITION run (fixture, execution mode, WS sequence) which the status strip
// reads, and neither ProofArena screen consumes it. A benchmark computed over recorded evidence has
// no live run to report, so providing the context would only make it possible to render a strip
// describing one.
//
// Both routes remain PUBLIC: no AuthGate anywhere in this group, and the shell renders no wallet
// affordance. Gating the honesty surface behind a wallet would gate the claims themselves.

// ITEM 9 — metadata scoped to these two routes. Next merges metadata nearest-wins per field, so
// exporting it here overrides the root layout's Veridex title for this group ONLY; `/`, the
// marketing routes and all of `(app)` keep the root values untouched.
//
// The shipped root title (`Veridex — TxLINE Agent Proof Arena`) and description are not wrong — they
// are just about a different product surface. The description in particular claimed Solana
// anchoring, and nothing on these two routes anchors anything: they read OKX DEX signals on X Layer
// and report reproducibility over recorded evidence. Carrying that sentence into a link preview of
// /trials would state an anchoring property this benchmark has never established.
export const metadata: Metadata = {
  title: 'ProofArena — reproducible benchmarks for financial agents',
  description:
    'Same sealed evidence. Different agent probabilities. One scoring law. ProofArena ranks agents '
    + 'by average Brier over recorded OKX DEX signals on X Layer, against four predeclared '
    + 'baseline controls, and every standing is re-derivable from the evidence recorded for the '
    + 'season.',
};

export default function ProofArenaGroupLayout({ children }: { children: ReactNode }) {
  return <ProofArenaShell>{children}</ProofArenaShell>;
}
