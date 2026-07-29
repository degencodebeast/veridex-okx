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
import ts from 'typescript';
import { cssDeclaration } from '@/lib/css-source';

const WEB = resolve(__dirname, '../..');
const p = (...seg: string[]) => resolve(WEB, ...seg);
const read = (...seg: string[]) => readFileSync(p(...seg), 'utf8');

// The handoff's narrow breakpoint — CLAUDE-CODE-BRIEF §"At ≤ 760px … every tap target is ≥ 44px",
// restated in PROOFARENA-OKX-DELTA-HANDOFF §7:232. Named once so no assertion can quietly move to a
// different one.
const NARROW = { media: '(max-width: 760px)' } as const;

function namedImportBinding(
  source: ts.SourceFile,
  moduleName: string,
  importedName: string,
): string | null {
  for (const statement of source.statements) {
    if (!ts.isImportDeclaration(statement)
      || !ts.isStringLiteral(statement.moduleSpecifier)
      || statement.moduleSpecifier.text !== moduleName) continue;
    const bindings = statement.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) return null;
    const match = bindings.elements.find((element) =>
      (element.propertyName?.text ?? element.name.text) === importedName,
    );
    return match?.name.text ?? null;
  }
  return null;
}

function defaultLayoutReturn(source: ts.SourceFile): ts.Expression | null {
  const layout = source.statements.find((statement): statement is ts.FunctionDeclaration =>
    ts.isFunctionDeclaration(statement)
      && statement.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.DefaultKeyword)
      === true,
  );
  if (!layout?.body) return null;
  const returns = layout.body.statements.filter(ts.isReturnStatement);
  if (returns.length !== 1 || !returns[0].expression) return null;
  let expression = returns[0].expression;
  while (ts.isParenthesizedExpression(expression)) expression = expression.expression;
  return expression;
}

function tagName(element: ts.JsxElement): string | null {
  const name = element.openingElement.tagName;
  return ts.isIdentifier(name) ? name.text : null;
}

function meaningfulJsxChildren(element: ts.JsxElement): readonly ts.JsxChild[] {
  return element.children.filter((child) => {
    if (ts.isJsxText(child)) return child.text.trim().length > 0;
    // A JSX comment is an expression node with no expression. It is not rendered composition.
    if (ts.isJsxExpression(child)) return child.expression !== undefined;
    return true;
  });
}

function legacyLayoutCompositionErrors(text: string): string[] {
  const source = ts.createSourceFile('app-layout.tsx', text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const appShellBinding = namedImportBinding(
    source,
    '@/components/layout/AppShell',
    'AppShell',
  );
  const providerBinding = namedImportBinding(
    source,
    '@/components/layout/StatusBarContext',
    'StatusBarProvider',
  );
  const errors: string[] = [];
  if (appShellBinding !== 'AppShell') errors.push('missing real AppShell named import');
  if (providerBinding !== 'StatusBarProvider') {
    errors.push('missing real StatusBarProvider named import');
  }

  const returned = defaultLayoutReturn(source);
  if (!returned || !ts.isJsxElement(returned) || tagName(returned) !== providerBinding) {
    errors.push('layout does not return StatusBarProvider as the outer wrapper');
    return errors;
  }
  const providerChildren = meaningfulJsxChildren(returned);
  if (providerChildren.length !== 1
    || !ts.isJsxElement(providerChildren[0])
    || tagName(providerChildren[0]) !== appShellBinding) {
    errors.push('StatusBarProvider does not directly wrap AppShell');
    return errors;
  }
  const shellChildren = meaningfulJsxChildren(providerChildren[0]);
  if (shellChildren.length !== 1
    || !ts.isJsxExpression(shellChildren[0])
    || !shellChildren[0].expression
    || !ts.isIdentifier(shellChildren[0].expression)
    || shellChildren[0].expression.text !== 'children') {
    errors.push('AppShell does not directly wrap the children binding');
  }
  return errors;
}

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
    expect(legacyLayoutCompositionErrors(appLayout)).toEqual([]);
  });

  it.each([
    [
      'comments and strings',
      `
        import type { ReactNode } from 'react';
        // StatusBarProvider and AppShell used to wrap this layout.
        const labels = ['StatusBarProvider', 'AppShell'];
        export default function Layout({ children }: { children: ReactNode }) {
          return <>{children}</>;
        }
      `,
    ],
    [
      'unrelated JSX elements',
      `
        import type { ReactNode } from 'react';
        import { AppShell } from '@/components/layout/AppShell';
        import { StatusBarProvider } from '@/components/layout/StatusBarContext';
        export default function Layout({ children }: { children: ReactNode }) {
          return <main><StatusBarProvider /><AppShell />{children}</main>;
        }
      `,
    ],
    [
      'reversed wrapper nesting',
      `
        import type { ReactNode } from 'react';
        import { AppShell } from '@/components/layout/AppShell';
        import { StatusBarProvider } from '@/components/layout/StatusBarContext';
        export default function Layout({ children }: { children: ReactNode }) {
          return <AppShell><StatusBarProvider>{children}</StatusBarProvider></AppShell>;
        }
      `,
    ],
    [
      'aliased imports plus unbound lookalike JSX names',
      `
        import type { ReactNode } from 'react';
        import { AppShell as LegacyShell } from '@/components/layout/AppShell';
        import {
          StatusBarProvider as LegacyStatus,
        } from '@/components/layout/StatusBarContext';
        export default function Layout({ children }: { children: ReactNode }) {
          return <StatusBarProvider><AppShell>{children}</AppShell></StatusBarProvider>;
        }
      `,
    ],
  ])('cannot be satisfied by %s', (_case, mutatedLayout) => {
    expect(legacyLayoutCompositionErrors(mutatedLayout)).not.toEqual([]);
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

// The metadata CONTENT contract — the exact per-route copy from PROOFARENA-OKX-DELTA-HANDOFF
// §4:107-116 — lives in `route-metadata.test.ts`. What belongs HERE is only the part that is a
// FILESYSTEM fact, for the same reason the rest of this file is filesystem assertions: which layout
// a route's metadata comes from is decided by file location.
//
// The block this replaces asserted `title.toContain('ProofArena')` and a non-empty description
// against the group layout's single metadata object, and passed while `/trials/[trialId]` served the
// season title with no trial id in it. It was a substring guard standing in for an exact-copy
// requirement; it is not weakened here, it is superseded by whole-string assertions next door.
describe('the metadata scope is per ROUTE, which is a fact about file placement', () => {
  it('gives the trial route its own nested layout to generate metadata from', () => {
    // A metadata export cannot vary with a route param from the GROUP layout — that layout is
    // rendered once for both routes and receives no `[trialId]`. Per-trial metadata therefore
    // requires a layout at this exact path, and that requirement is what this asserts. It must be a
    // SERVER component: `generateMetadata` is a server-only export, and the page beside it is
    // `'use client'`.
    const path = 'app/(proofarena)/trials/[trialId]/layout.tsx';
    expect(existsSync(p(path)), 'the trial route has no nested metadata layout').toBe(true);
    const layout = read(path);
    expect(layout).toContain('generateMetadata');
    expect(layout, 'a server layout cannot be a client component').not.toMatch(/^\s*'use client'/m);
  });
});

// ---------------------------------------------------------------------------
// The narrow-viewport contract.
//
// HONEST LIMITATION, stated rather than papered over: jsdom performs no layout and applies no CSS
// module, so NO test in this suite can measure whether descendant text reflows. A viewport-width
// document is insufficient too: root clipping can hide hundreds of pixels outside a descendant's
// own box while `documentElement.scrollWidth` still equals `innerWidth`. The production-build
// browser cases in `route-metadata.transport.test.ts` measure the painted text fragments of both
// publisher-controlled identity surfaces against their own visible boxes. What these assertions ARE
// is the narrower shell source contract.
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

  it('keeps the defensive shell cap without treating it as descendant reflow proof', () => {
    // `max-width: 100%` + `overflow-x: clip` remains a defensive page-level constraint. It is NOT
    // evidence that a descendant is visible; the element-level production-browser assertions named
    // above are load-bearing for that claim.
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
