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
// The route segment is read with `useParams` (the client-component idiom used by every other
// dynamic route in this tree) rather than the async `params` prop, because the card is a client
// component and this wrapper adds nothing a server component could.
export default function TrialPage() {
  const params = useParams<{ trialId: string }>();
  return <TrialMatchCard trialId={params.trialId} />;
}
