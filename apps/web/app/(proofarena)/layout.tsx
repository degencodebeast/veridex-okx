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

// The SEASON metadata — `/trials` and nothing else.
//
// Next merges metadata nearest-wins per field, so exporting it here overrides the root layout's
// Veridex title for this group; `/`, the marketing routes and all of `(app)` keep the root values
// untouched. The same per-field merge is what lets `trials/[trialId]/layout.tsx` override THESE two
// fields for the trial route, which is why this object is now the season's alone. It previously
// served both routes, and a deep-linked trial therefore announced itself with the season title.
//
// The shipped root title (`Veridex — TxLINE Agent Proof Arena`) and description are not wrong — they
// are just about a different product surface. The description in particular claimed Solana
// anchoring, and nothing on these two routes anchors anything: they read OKX DEX signals on X Layer
// and report reproducibility over recorded evidence. Carrying that sentence into a link preview of
// /trials would state an anchoring property this benchmark has never established.
//
// PROOFARENA-OKX-DELTA-HANDOFF §4 "Page metadata" :107-111, verbatim — the handoff gives these as
// literal copy, not as a description of the intent, so they are transcribed rather than paraphrased.
// The handoff wraps the description across three source lines; those breaks are typographic and
// collapse to single spaces. The dash is U+2014 EM DASH, pinned by codepoint in
// route-metadata.test.ts.
//
// An EARLIER PASS wrote this object from a paraphrase of :107-116 and it drifted twice — a lowercase
// `reproducible`, and a description that was about the right subject but was not the specified
// sentence. Neither survives an exact-copy assertion, which is why the test now makes one.
export const metadata: Metadata = {
  title: 'ProofArena — Reproducible benchmarks for financial agents',
  description:
    'Same sealed evidence. Different agent probabilities. One deterministic settlement and scoring '
    + 'law. Brier-first standings with predeclared negative controls. Paper benchmark — not a trade '
    + 'recommendation.',
};

export default function ProofArenaGroupLayout({ children }: { children: ReactNode }) {
  return <ProofArenaShell>{children}</ProofArenaShell>;
}
