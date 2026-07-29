// The ProofArena shell — the chrome for the two public ProofArena routes (`/trials` and
// `/trials/[trialId]`) and NOTHING else.
//
// WHY THIS FILE EXISTS. The brief's scope sentence is "two public routes + the minimal ProofArena
// shell around them". The routes shipped; the shell did not — both routes rendered correct
// ProofArena content inside the legacy Veridex `AppShell`, so a judge landing on the OKX listing's
// own artifact saw a Veridex "V" wordmark, a Competitions/Arena/Markets/Leaderboard/Agents nav, a
// Connect Wallet control and a `FIXTURE · IDLE · EXEC · WS idle · verifier v0` run-status strip
// belonging to a different product. The content was right and the frame was wrong.
//
// THE TWO HALVES OF THAT DEFECT ARE BOTH ASSERTED BELOW, and the second half is the one that makes
// this file more than a copy checklist:
//   * PRESENCE — the identity, integration badges, footer band and product-boundary disclaimer that
//     PROOFARENA-EXACT-COPY.md §1 declares for both routes.
//   * ABSENCE — none of the legacy Veridex chrome. A shell that rendered the ProofArena wordmark
//     ABOVE the Veridex nav would satisfy every presence assertion and still be the shipped bug, so
//     the absence group is the discrimination control for the whole file.
//
// SCOPE NOTE: the legacy chrome is not deleted anywhere. `AppShell`, `TopNav` and `StatusBar` are
// untouched and every other route still renders them — see `app/(proofarena)/route-scope.test.ts`,
// which pins that both ways.
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { NAV_SECTIONS } from '@/lib/nav';
import { ProofArenaShell } from '@/components/layout/ProofArenaShell';

// PROOFARENA-EXACT-COPY.md §1 — frozen, character-for-character. Declared as constants so every
// assertion below reads the SAME spelling the component must ship: a typo here fails the test
// rather than quietly relaxing it.
const WORDMARK = 'PROOFARENA';
const INTEGRATIONS = ['OKX DEX SIGNALS', 'X LAYER · eip155:196'] as const;
const CLOSING_LINE =
  'Smart money can still be exit liquidity. ProofArena tests whether your agent knows the difference.';
const ONBOARDING_LABEL = 'ONE-PROMPT ONBOARDING';
// NOTE the ASCII apostrophe in `agent's`: that is the byte the copy authority and the prototype
// both carry (U+0027, verified against ProofArena.dc.html), not a typographic U+2019. On a frozen
// string the character IS the spec.
const DISCLAIMER =
  'ProofArena does not execute trades, does not sell alpha, and does not provide personalized ' +
  "investment advice. FOLLOW / FADE / ABSTAIN is a display derived from an agent's own " +
  'submitted probability.';

describe('ProofArenaShell — identity', () => {
  it('renders the PROOFARENA wordmark', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    expect(screen.getByTestId('proofarena-wordmark')).toHaveTextContent(WORDMARK);
  });

  it('renders an Arena tab pointing at /trials and marked as the current section', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    const arena = screen.getByRole('link', { name: 'Arena' });
    expect(arena).toHaveAttribute('href', '/trials');
    // Both routes this shell wraps ARE the Arena section, so the tab is current on both. A shell
    // that rendered the tab without marking it would leave a nav with no current item at all.
    expect(arena).toHaveAttribute('aria-current', 'page');
  });

  it('renders the SKILL.md ↗ header link against the real document at /SKILL.md', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    const link = screen.getByTestId('shell-skill-md');
    // Character-for-character INCLUDING the U+2197 arrow. `toHaveTextContent` matches substrings
    // and normalises whitespace, so it would pass on copy the exact-copy sheet forbids.
    expect(link.textContent).toBe('SKILL.md ↗');
    // `apps/web/public/SKILL.md` is served from the image root. The href is load-bearing: a dead
    // link on the honesty surface is worse than a missing one.
    expect(link).toHaveAttribute('href', '/SKILL.md');
  });

  it('renders both integration badges verbatim, header-right', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    const group = screen.getByTestId('proofarena-integrations');
    for (const label of INTEGRATIONS) {
      // Exact node text, not a substring of the group: `X LAYER · eip155:196` carries the chain id
      // that identifies the deployment, and a truncated or reworded badge is a different claim.
      expect(group.textContent).toContain(label);
    }
    expect(screen.getByText(INTEGRATIONS[0])).toBeInTheDocument();
    expect(screen.getByText(INTEGRATIONS[1])).toBeInTheDocument();
  });

  it('renders the page content inside the main landmark', () => {
    render(<ProofArenaShell><p>screen body</p></ProofArenaShell>);
    const main = screen.getByRole('main');
    expect(main).toHaveTextContent('screen body');
  });
});

describe('ProofArenaShell — the footer band and the product boundary', () => {
  it('renders the closing line verbatim', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    expect(screen.getByTestId('proofarena-closing-line').textContent).toBe(CLOSING_LINE);
  });

  it('renders the ONE-PROMPT ONBOARDING label with its READ SKILL.md ↗ action', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    expect(screen.getByText(ONBOARDING_LABEL)).toBeInTheDocument();
    const link = screen.getByTestId('footer-skill-md');
    expect(link.textContent).toBe('READ SKILL.md ↗');
    expect(link).toHaveAttribute('href', '/SKILL.md');
  });

  it('renders the product-boundary disclaimer verbatim', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    // The whole sentence, exactly. This is the line that states what ProofArena is NOT, so a
    // paraphrase is a change in the product claim and not a change in wording.
    expect(screen.getByTestId('proofarena-disclaimer').textContent).toBe(DISCLAIMER);
  });

  it('carries the boundary on EVERY route the shell wraps, not just the season route', () => {
    // The disclaimer is declared "both routes" by PROOFARENA-EXACT-COPY.md §1. Placing it in the
    // shell rather than in one screen is what makes that true without duplicating the string, and
    // this assertion is what pins it: the shell renders it for arbitrary children.
    const { unmount } = render(<ProofArenaShell><p>season</p></ProofArenaShell>);
    expect(screen.getByTestId('proofarena-disclaimer')).toBeInTheDocument();
    unmount();
    render(<ProofArenaShell><p>match card</p></ProofArenaShell>);
    expect(screen.getByTestId('proofarena-disclaimer')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// THE DISCRIMINATION GROUP. Every assertion above passes against a shell that
// renders ProofArena chrome ON TOP OF the Veridex chrome — which is exactly the
// defect. These are the assertions that do not.
// ---------------------------------------------------------------------------
describe('ProofArenaShell — none of the legacy Veridex chrome', () => {
  it('renders no Veridex wordmark and no legacy home link', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    expect(screen.queryByRole('link', { name: 'Veridex home' })).not.toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name: 'Primary' })).not.toBeInTheDocument();
  });

  it('renders none of the five legacy top-level nav sections', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    // Read from the IA single source, so a nav section added to `NAV_SECTIONS` later is covered
    // here automatically instead of silently escaping the guard.
    for (const section of NAV_SECTIONS) {
      // `Arena` is the one label the two navs share — ProofArena's own tab. It is excluded by
      // HREF, not by name: the ProofArena tab points at /trials and the legacy one at /arena, and
      // it is the legacy DESTINATION that must be absent.
      const links = screen.queryAllByRole('link', { name: section.label });
      for (const link of links) {
        expect(link).not.toHaveAttribute('href', section.href);
      }
    }
  });

  it('renders no wallet affordance — both routes are public and nothing here is owner-scoped', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    expect(screen.queryByRole('button', { name: /connect wallet/i })).not.toBeInTheDocument();
  });

  it('renders no run-status strip', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    // The strip is the single loudest wrong signal on these routes: `FIXTURE · IDLE · EXEC ·
    // WS idle · verifier v0` describes a live competition run, and a reproducible-benchmark page
    // has no run. Asserting the testid AND the copy catches both "the component" and "a
    // reimplementation of it".
    expect(screen.queryByTestId('status-bar')).not.toBeInTheDocument();
    const text = document.body.textContent ?? '';
    for (const fragment of ['FIXTURE', 'WS idle', 'verifier v', 'EXEC ·']) {
      expect(text, `legacy status-strip copy "${fragment}" leaked into the ProofArena shell`)
        .not.toContain(fragment);
    }
  });

  it('renders no Direction A/B toggle', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    // The A·TERMINAL / B·SAAS switch is a design-inspection affordance for the legacy screens. It
    // is the same class of thing as the prototype's own inspection toolbar, which the brief
    // forbids shipping.
    const text = document.body.textContent ?? '';
    expect(text).not.toMatch(/terminal/i);
    expect(text).not.toMatch(/saas/i);
  });

  it('renders none of the prototype inspection scaffolding', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    const text = document.body.textContent ?? '';
    // ProofArena.dc.html carries a dashed route/state toggle bar and a fixture label. Both are
    // inspection scaffolding and neither is product UI.
    expect(text).not.toContain('PROTOTYPE INSPECTION');
    expect(text).not.toContain('ILLUSTRATIVE PROTOTYPE FIXTURE');
  });
});

describe('ProofArenaShell — honesty language', () => {
  it('uses no banned trading language outside the disavowal it is required to ship', () => {
    render(<ProofArenaShell><p>body</p></ProofArenaShell>);
    const text = document.body.textContent ?? '';

    // `alpha` is on PROOFARENA-EXACT-COPY.md §2's forbidden list AND inside the §1 disclaimer the
    // same document freezes ("does not sell alpha"). The two are only in conflict if the word is
    // treated as a token rather than as a claim: §2 bans ASSERTING alpha, and the one permitted
    // occurrence is a sentence denying it. So the guard is scoped rather than dropped — the word
    // is allowed exactly once, inside the frozen disavowal, and banned everywhere else.
    const disavowal = 'does not sell alpha';
    expect(text).toContain(disavowal);
    const withoutDisavowal = text.split(disavowal).join(' ');
    expect(withoutDisavowal).not.toMatch(/\balpha\b/i);

    for (const banned of [
      /\bPnL\b/i, /\bprofits?\b/i, /\breali[sz]ed\b/i, /\bROI\b/i, /\bedges?\b/i,
      /\bmade money\b/i, /\bfills?\b/i, /\bfilled\b/i, /\bpositions?\b/i, /\bproven\b/i,
      /\bbuy\b/i, /\bsell\b/i, /tamper/i, /immutab/i,
    ]) {
      expect(withoutDisavowal, `banned ${banned} in the ProofArena shell`).not.toMatch(banned);
    }
  });
});
