// The scope guard for the ProofArena shell.
//
// WHY A FILESYSTEM TEST. Next.js App Router composition is decided by file location, not by any
// import a component test could observe: a `layout.tsx` added UNDER `app/(app)/trials/` nests
// INSIDE `AppShell` instead of replacing it, so the only way to give these two routes a different
// shell is to move them out of the `(app)` route group. Route groups are URL-transparent, so
// `/trials` and `/trials/[trialId]` are unchanged by the move. That makes the layout of the `app/`
// tree a real, checkable contract, and this file checks it.
//
// It is also the file that pins the SCOPE BOUNDARY in the direction that matters most: the legacy
// chrome is not deleted, and every other route still renders it. A change that "fixed" these two
// routes by gutting `AppShell` would pass every assertion in ProofArenaShell.test.tsx and fail
// here.
import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const WEB = resolve(__dirname, '../..');
const p = (...seg: string[]) => resolve(WEB, ...seg);
const read = (...seg: string[]) => readFileSync(p(...seg), 'utf8');

describe('the ProofArena routes live outside the legacy (app) shell group', () => {
  it('serves both routes from the (proofarena) group', () => {
    expect(existsSync(p('app/(proofarena)/trials/page.tsx'))).toBe(true);
    expect(existsSync(p('app/(proofarena)/trials/[trialId]/page.tsx'))).toBe(true);
    expect(existsSync(p('app/(proofarena)/layout.tsx'))).toBe(true);
  });

  it('leaves NO trials route behind in the (app) group', () => {
    // The discrimination control for the move. Both directories existing at once is not a partial
    // fix — Next.js raises a duplicate-route error, and before that it means the legacy-shelled
    // copy is still the one some build resolves.
    expect(existsSync(p('app/(app)/trials'))).toBe(false);
  });

  it('wraps the group in the ProofArena shell and not in the legacy AppShell', () => {
    const layout = read('app/(proofarena)/layout.tsx');
    expect(layout).toContain('ProofArenaShell');
    // Named-import check rather than a bare substring: the word "AppShell" is a substring of
    // nothing else here, but asserting the import path is what actually distinguishes "does not
    // use it" from "mentions it in a comment".
    expect(layout).not.toMatch(/from\s+['"]@?[^'"]*components\/layout\/AppShell['"]/);
    // Import AND element, not the bare word: the layout DOCUMENTS why it does not provide the
    // status-bar context, and a substring check would read that explanation as the thing it
    // explains — a false positive that would push the reasoning out of the file to satisfy a test.
    expect(layout).not.toMatch(/from\s+['"]@?[^'"]*layout\/StatusBarContext['"]/);
    expect(layout).not.toMatch(/<StatusBarProvider\b/);
    expect(layout).not.toMatch(/<AppShell\b/);
  });
});

describe('the scope boundary — the legacy chrome is untouched and still used', () => {
  it('keeps AppShell, TopNav and StatusBar in the tree', () => {
    for (const f of [
      'components/layout/AppShell.tsx',
      'components/layout/TopNav.tsx',
      'components/layout/StatusBar.tsx',
      'components/layout/StatusBarContext.tsx',
    ]) {
      expect(existsSync(p(f)), `${f} was deleted — the other routes still render it`).toBe(true);
    }
  });

  it('still renders every OTHER route inside the legacy AppShell', () => {
    const appLayout = read('app/(app)/layout.tsx');
    expect(appLayout).toContain('AppShell');
    expect(appLayout).toContain('StatusBarProvider');
  });

  it('leaves the other product routes in the (app) group', () => {
    // Named explicitly because the brief names them: these must not be collaterally moved,
    // restyled, or re-shelled by a change scoped to /trials.
    for (const route of ['competitions', 'arena', 'markets', 'leaderboard', 'agents']) {
      expect(existsSync(p('app/(app)', route, 'page.tsx')), `/${route} left the (app) group`)
        .toBe(true);
      expect(existsSync(p('app/(proofarena)', route)), `/${route} was pulled into (proofarena)`)
        .toBe(false);
    }
  });
});

describe('page metadata is ProofArena for these two routes only', () => {
  it('declares ProofArena metadata on the route group', async () => {
    const mod = (await import('./layout')) as { metadata?: { title?: unknown; description?: unknown } };
    const title = String(mod.metadata?.title ?? '');
    const description = String(mod.metadata?.description ?? '');

    expect(title).toContain('ProofArena');
    expect(description.length).toBeGreaterThan(0);

    // Discrimination: the shipped metadata said `Veridex — TxLINE Agent Proof Arena` and described
    // Solana anchoring. Both are true of the legacy product and neither is true of these two
    // routes, which read OKX DEX signals on X Layer and anchor nothing.
    expect(title).not.toContain('Veridex');
    expect(description).not.toContain('Veridex');
    expect(description).not.toMatch(/solana/i);
    expect(title).not.toMatch(/solana/i);
  });

  it('leaves the ROOT metadata alone — it is every other route\'s title', () => {
    // The root layout serves the marketing landing and all of `(app)`. Rewriting it would rename
    // the whole product from inside a two-route task.
    const root = read('app/layout.tsx');
    expect(root).toContain('Veridex — TxLINE Agent Proof Arena');
  });
});

// ---------------------------------------------------------------------------
// The narrow-viewport contract.
//
// HONEST LIMITATION, stated rather than papered over: jsdom performs no layout and applies no CSS
// module, so NO test in this suite can measure a document width. The real evidence for the 390px
// overflow is a browser measurement of `documentElement.scrollWidth`, recorded outside the suite.
// What these assertions ARE is a source contract: they fail if the wrapping rules that make the
// shell reflowable are removed, which is the regression a future edit would actually introduce.
// ---------------------------------------------------------------------------
describe('the shell cannot force a min-content width wider than a phone', () => {
  const css = () => read('components/layout/ProofArenaShell.module.css');

  it('lets the header wrap', () => {
    // The legacy header could not: a single nowrap flex row carrying five nav links, a wallet
    // control and a status strip has a min-content width far wider than 390px, which is why the
    // document measured 558px there and Connect Wallet sat at x=483.
    expect(css()).toMatch(/\.header\b[^}]*flex-wrap:\s*wrap/s);
  });

  it('lets the integration badge group wrap', () => {
    expect(css()).toMatch(/\.integrations\b[^}]*flex-wrap:\s*wrap/s);
  });

  it('lets the footer band wrap', () => {
    expect(css()).toMatch(/\.footerBand\b[^}]*flex-wrap:\s*wrap/s);
  });

  it('caps the shell at the viewport and clips nothing horizontally', () => {
    // `max-width: 100%` + `overflow-x: clip` on the shell root is the belt to the wrapping
    // braces: if any DESCENDANT (a table, a hash chip) is wider than the viewport, the page body
    // still must not scroll sideways — the offending element scrolls inside its own container.
    expect(css()).toMatch(/\.shell\b[^}]*max-width:\s*100%/s);
    expect(css()).toMatch(/\.shell\b[^}]*overflow-x:\s*clip/s);
  });

  it('gives every shell tap target at least 44px', () => {
    // The handoff's §7 breakpoint rule. Both header links and the footer action are real tap
    // targets on a phone.
    expect(css()).toMatch(/min-height:\s*44px/);
  });
});
