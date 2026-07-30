import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { render } from '@testing-library/react';
import { LandingScreen } from '@/components/screens/LandingScreen';
import { NAV_SECTIONS } from '@/lib/nav';

// COPY-LINT (T-2 honesty core). Veridex executes DRY-RUN / paper on recorded replay — no live money,
// no real venue orders, no live on-chain execution (proofs anchor on devnet; settlement is design-ahead,
// Phase 2D). The public-facing copy must never claim otherwise. This lint bans live-money /
// venue-execution / on-chain-execution overclaims on the LANDING page AND every primary-nav route, so
// no judge-facing surface can drift back into an overclaim. It is a deliberate tripwire: honest copy
// must simply avoid these literal phrases (e.g. say "dry-run fills", not "venue fills").
const OVERCLAIMS: { pattern: RegExp; why: string }[] = [
  { pattern: /real orders?/i, why: 'execution is dry-run / paper — no live orders are ever placed' },
  { pattern: /venue fills?/i, why: 'no venue execution — receipts are dry-run, never real venue fills' },
  { pattern: /live on solana/i, why: 'nothing executes live on Solana; proofs anchor on devnet, settlement is design-ahead (Phase 2D)' },
  { pattern: /real execution/i, why: 'execution is dry-run (no live money) — never real execution' },
];

// CLASS-level overclaim guards (T-2 remediation). The 4-phrase denylist above only catches
// phrasings we already thought of; these guard whole CLASSES of contradiction against the backend's
// authoritative defaults. The competition finalize path sets anchor_status "not_anchored" BY DEFAULT
// (a run is NOT anchored unless an external anchor actually happened), competition data is
// RECORDED/REPLAYED (not source=live), and the hackathon scope has NO on-chain payout / no Squads
// custody yet. So the landing DOM must never make an UNCONDITIONAL / universal / present-tense claim
// that every run is anchored, that each proof commits to a real tx, that agents get paid on-chain
// today, or that the arena is a live market. Honest CONDITIONAL / design-ahead copy (anchored WHEN
// externally anchored, otherwise not-anchored; recorded/replayed windows; payout design-ahead on
// devnet) must pass — that is exactly the RED→GREEN boundary these patterns encode.
const CLASS_OVERCLAIMS: { pattern: RegExp; why: string }[] = [
  { pattern: /every\s+(run|proof)\s+is\s+(an?\s+)?(on-chain[\s-]?)?anchored/i, why: 'universal anchor claim — anchor_status defaults to "not_anchored"; a run is anchored only WHEN externally anchored' },
  { pattern: /each\s+proof\s+commits\s+to\s+a\s+real\s+solana/i, why: 'universal real-tx claim — a proof commits to a real Solana tx only WHEN the run is externally anchored, else it is honestly not-anchored' },
  { pattern: /proofs?\s+anchored\s+on\s+solana/i, why: 'universal anchored-on-Solana claim — anchoring is conditional (default not_anchored), never every proof' },
  { pattern: /get\s+paid\s+on-chain/i, why: 'unhedged payout promise — there is no on-chain payout yet; settlement is design-ahead on devnet (Phase 2D)' },
  { pattern: /\blive\s+arena\b/i, why: 'unqualified "live arena" — competition runs on recorded/replayed market windows, not a live market' },
  { pattern: /\blive\s+market\b/i, why: 'unqualified "live market" — competition data is recorded/replayed, not source=live' },
];

// Every primary-nav route → its screen source. The coverage test below asserts this map covers
// EXACTLY the live nav (a new nav route with no entry here fails — no judge-nav route survives uncovered).
const NAV_ROUTE_SCREEN: Record<string, string> = {
  '/competitions': 'components/screens/CompetitionsScreen.tsx',
  '/arena': 'components/screens/ArenaEmptyState.tsx',
  '/trials': 'components/screens/signal-trials/SeasonScreen.tsx',
  '/markets': 'components/screens/MarketsScreen.tsx',
  '/leaderboard': 'components/screens/LeaderboardScreen.tsx',
  '/agents': 'components/screens/AgentsScreen.tsx',
};
const LANDING_SOURCE = 'components/screens/LandingScreen.tsx';
const readSource = (rel: string) => readFileSync(join(process.cwd(), rel), 'utf8');

function stubMatchMedia(reduced: boolean) {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: reduced && /reduce/.test(q), media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(),
    removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
}
beforeEach(() => stubMatchMedia(false));

describe('Landing / nav copy-honesty (T-2 — no live-money / venue / on-chain-execution overclaims)', () => {
  it('the RENDERED landing page carries none of the banned overclaims', () => {
    render(<LandingScreen />);
    const text = document.body.textContent ?? '';
    for (const { pattern, why } of OVERCLAIMS) {
      expect(text, `Landing overclaims — ${why} (matched ${pattern})`).not.toMatch(pattern);
    }
  });

  it('the RENDERED landing page carries no UNCONDITIONAL anchor / live-market / on-chain-payout class overclaim', () => {
    render(<LandingScreen />);
    const text = document.body.textContent ?? '';
    for (const { pattern, why } of CLASS_OVERCLAIMS) {
      expect(text, `Landing class-overclaim — ${why} (matched ${pattern})`).not.toMatch(pattern);
    }
  });

  it('every primary-nav route is covered by the copy-lint (no judge-nav route survives uncovered)', () => {
    const navHrefs = NAV_SECTIONS.map((s) => s.href).sort();
    expect(Object.keys(NAV_ROUTE_SCREEN).sort()).toEqual(navHrefs);
  });

  it.each([['LANDING', LANDING_SOURCE], ...Object.entries(NAV_ROUTE_SCREEN)])(
    '%s source carries none of the banned overclaims',
    (_route, sourcePath) => {
      const src = readSource(sourcePath);
      for (const { pattern, why } of OVERCLAIMS) {
        expect(src, `${sourcePath} overclaims — ${why} (matched ${pattern})`).not.toMatch(pattern);
      }
    },
  );
});
