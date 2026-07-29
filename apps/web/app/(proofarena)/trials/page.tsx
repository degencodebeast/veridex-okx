'use client';
import { SeasonScreen } from '@/components/screens/signal-trials/SeasonScreen';

// /trials — the PUBLIC ProofArena season standings.
//
// NO AuthGate. This is the judge-facing benchmark surface and the artifact the OKX listing points
// at; putting it behind a wallet would gate the honesty claims themselves. AuthGate is the
// fail-closed wrapper for OWNER-SCOPED affordances (the Studio deploy path) — read-only public
// standings are not one, and nothing on this route calls an authenticated endpoint.
//
// The page is a thin client wrapper on purpose. Unlike the leaderboard route, it does NOT fetch and
// pass rows down: what /signal-trials/season returns is a discriminated verdict, not a list, and
// both `not_built` and `no_season` answer 404 there. Resolving that through /health is
// `getSignalTrialsSeason()`'s job and the screen owns the resulting status machine, so flattening
// it here would destroy the distinction before the screen could render it.
export default function TrialsPage() {
  return <SeasonScreen />;
}
