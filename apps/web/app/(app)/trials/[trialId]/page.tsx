'use client';
import { useParams } from 'next/navigation';
import { TrialMatchCard } from '@/components/screens/signal-trials/TrialMatchCard';

// /trials/[trialId] — the PUBLIC ProofArena Fair-Play match card.
//
// NO AuthGate, for exactly the reason /trials has none: this is the judge-facing fairness artifact
// and the surface the OKX listing points at, so putting it behind a wallet would gate the honesty
// claims themselves. AuthGate is the fail-closed wrapper for OWNER-SCOPED affordances (the Studio
// deploy path); a read-only public match card is not one, and nothing on this route calls an
// authenticated endpoint.
//
// The page is a thin client wrapper on purpose, matching /trials/page.tsx. It does NOT fetch and
// pass a view model down: the card composes THREE independent reads whose failures mean different
// things — the trial, the participant set, and each participant's verdicts — and a page that
// flattened them into one prop would have to pick a single failure state, destroying exactly the
// distinctions the card exists to preserve. The card owns those status machines.
//
// The route segment is read with `useParams`, the idiom the other CLIENT-component dynamic routes
// use — `/agents/[agentId]` and `/instances/[instanceId]`. The async `params` prop is the more
// common shape in this tree overall and is what the server-rendered dynamic routes use, so this is
// a choice between two live conventions rather than the only one. It is the right one here because
// the card is a client component that owns three status machines: a server wrapper could await
// `params` and forward the string (`/proof/maker-ablation/[instanceId]` does exactly that), but it
// would add a boundary without moving any work across it.
export default function TrialPage() {
  const params = useParams<{ trialId: string }>();
  return <TrialMatchCard trialId={params.trialId} />;
}
