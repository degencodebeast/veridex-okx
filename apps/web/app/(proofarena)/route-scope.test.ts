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
import { cssDeclaration } from '@/lib/css-source';

const WEB = resolve(__dirname, '../..');
const p = (...seg: string[]) => resolve(WEB, ...seg);
const read = (...seg: string[]) => readFileSync(p(...seg), 'utf8');

// The handoff's narrow breakpoint — CLAUDE-CODE-BRIEF §"At ≤ 760px … every tap target is ≥ 44px",
// restated in PROOFARENA-OKX-DELTA-HANDOFF §7:232. Named once so no assertion can quietly move to a
// different one.
const NARROW = { media: '(max-width: 760px)' } as const;

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
    //
    // ASSERTED PER SELECTOR, deliberately. The previous form of this test was
    // `expect(css()).toMatch(/min-height:\s*44px/)`, which is satisfied by 44px on ANY rule in the
    // file — including a decorative one — and so passed elsewhere in this repo while the real tap
    // targets were 20px. Naming the two selectors makes the assertion fail when either control
    // regresses, which is the only version of it worth having.
    for (const selector of ['.navLink', '.footerSkill']) {
      expect(
        cssDeclaration(css(), selector, 'min-height', NARROW),
        `${selector} is not ≥ 44px at ${NARROW.media}`,
      ).toBe('44px');
    }
  });
});

// ---------------------------------------------------------------------------
// The product-boundary disclaimer's contrast.
//
// PROOFARENA-OKX-DELTA-HANDOFF §7 fixes the token per ROLE, not per taste. Quoted by token name
// rather than by literal value, because PAT-001 makes `styles/tokens.css` the only file in the
// scanned tree permitted to carry a raw hex — including inside a comment:
//
//   :302 — every sentence-case string (descriptor, thesis, footnotes, check descriptions, exclusion
//          notes, empty/error bodies, AND the product-boundary disclaimer) renders at one of the two
//          body-copy values, `--text-2` or the slightly dimmer descriptive-mono value; the two
//          darkest values, `--text-3` and `--text-4`, are "reserved for 8–9px uppercase
//          letter-spaced micro-labels ONLY".
//   :304 — `--text-3`/`--text-4` = short uppercase mono labels and timestamps. `--text-2` =
//          "sentence-case sans body and honesty-critical disclaimers", and: "DO NOT PUT A
//          MULTI-SENTENCE STRING BELOW" the descriptive-mono value.
//
// The disclaimer is multi-sentence AND honesty-critical, so it is the `--text-2` case twice over. It
// shipped at `--text-3`, which measures 3.42:1 against the page background it actually renders on —
// below the 4.5:1 AA floor for 11px text. `--text-2` measures 7.49:1 there.
//
// Note the backdrop: the disclaimer is a SIBLING of `.footerBand`, not a child, so it sits on `--bg`
// rather than on `--panel`. That makes both ratios marginally better than the panel-based estimate
// in the review that raised this, and changes neither verdict.
//
// The companion assertion that `--text-2` still HOLDS the value §7 names lives in
// __tests__/token-conformance.test.ts, beside the other token-value pins — that is the file PAT-001
// exempts, and the only lawful home for an assertion that must name a hex.
// ---------------------------------------------------------------------------
describe('the product-boundary disclaimer renders at the honesty-critical body token', () => {
  const css = () => read('components/layout/ProofArenaShell.module.css');

  it('colours the disclaimer with the sentence-case body token', () => {
    expect(cssDeclaration(css(), '.disclaimer', 'color')).toBe('var(--text-2)');
  });

  it('does NOT colour it with a micro-label token', () => {
    // Stated as its own assertion because it is the specific defect: `--text-3`/`--text-4` are
    // reserved by §7:304 for 8–9px uppercase micro-labels, and this is an 11px multi-sentence
    // string. A future edit that reaches for either token fails here with the reason attached.
    const color = cssDeclaration(css(), '.disclaimer', 'color');
    expect(color, 'the disclaimer is not a micro-label').not.toBe('var(--text-3)');
    expect(color, 'the disclaimer is not a micro-label').not.toBe('var(--text-4)');
  });

  it('uses the SAME token as the adjacent sentence-case footer copy', () => {
    // `.closingLine` is the other sentence-case string in this footer and was already correct. The
    // two are one band of prose; a fix that made only the disclaimer right would leave the footer
    // rendering one register of copy at two contrasts.
    expect(cssDeclaration(css(), '.disclaimer', 'color'))
      .toBe(cssDeclaration(css(), '.closingLine', 'color'));
  });

  it('does not reach the right token by retinting it', () => {
    // The token indirection is what makes the assertion above readable; it is also a way to satisfy
    // it while regressing the rendered colour, by pointing `--text-2` at a darker value. The
    // VALUE pin lives in __tests__/token-conformance.test.ts (PAT-001 exempts that file and forbids
    // a hex here). What is checkable in this file is the relationship: the body token and the
    // micro-label tokens must remain three DIFFERENT values in the dark theme, so `--text-2` cannot
    // have been quietly collapsed onto either reserved one.
    const tokens = read('styles/tokens.css');
    const body = cssDeclaration(tokens, ':root', '--text-2');
    expect(body).not.toBeNull();
    expect(body).not.toBe(cssDeclaration(tokens, ':root', '--text-3'));
    expect(body).not.toBe(cssDeclaration(tokens, ':root', '--text-4'));
  });
});
