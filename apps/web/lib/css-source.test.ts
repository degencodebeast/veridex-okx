// The reader under every CSS-source contract assertion in this repo. It is tested directly because
// its FAILURE MODE IS A FALSE PASS: if it were lenient, the accessibility guards built on it would
// go green against the exact defects they exist to catch. Each case below is one of those defects,
// written as CSS this reader must refuse to answer.
import { describe, it, expect } from 'vitest';
import { cssDeclaration, cssSelectors } from './css-source';

describe('cssDeclaration — what it refuses to be satisfied by', () => {
  it('does not answer a selector query from a DIFFERENT selector', () => {
    // The observed false pass: a 44px assertion that went green because the decorative `.mark`
    // carried the value while every real tap target was 20px.
    const css = '.mark { min-height: 44px; } .retry { padding: 5px 10px; }';
    expect(cssDeclaration(css, '.retry', 'min-height')).toBeNull();
    expect(cssDeclaration(css, '.mark', 'min-height')).toBe('44px');
  });

  it('does not treat a selector as a prefix of another', () => {
    const css = '.retryWide { min-height: 44px; } .retry-alt { min-height: 44px; }';
    expect(cssDeclaration(css, '.retry', 'min-height')).toBeNull();
  });

  it('reads one member of a selector LIST', () => {
    const css = '.navLink, .footerSkill { min-height: 44px; }';
    expect(cssDeclaration(css, '.navLink', 'min-height')).toBe('44px');
    expect(cssDeclaration(css, '.footerSkill', 'min-height')).toBe('44px');
  });

  it('does not answer from a COMMENT, inside the rule or beside it', () => {
    // The second observed false pass. These stylesheets document their own values in prose —
    // SeasonScreen.module.css really does say `≥ 44px at the narrow breakpoint` inside a rule body —
    // so a reader that saw comments would confirm the explanation instead of the rule.
    const css = '.retry { /* min-height: 44px; at narrow */ padding: 5px 10px; }';
    expect(cssDeclaration(css, '.retry', 'min-height')).toBeNull();
    expect(cssDeclaration(css, '.retry', 'padding')).toBe('5px 10px');
  });

  it('does not let a commented-out brace desynchronise block matching', () => {
    const css = '.a { /* } */ color: var(--text-1); } .retry { min-height: 44px; }';
    expect(cssDeclaration(css, '.retry', 'min-height')).toBe('44px');
    expect(cssDeclaration(css, '.a', 'color')).toBe('var(--text-1)');
  });

  it('anchors the property name to the start of a declaration', () => {
    // `height` must not be answered by `min-height`, or a guard could be satisfied by the property
    // next to the one it means.
    const css = '.retry { min-height: 44px; }';
    expect(cssDeclaration(css, '.retry', 'height')).toBeNull();
    expect(cssDeclaration(css, '.retry', 'min-height')).toBe('44px');
  });

  it('distinguishes a custom property from one that merely extends its name', () => {
    // The real pair this protects is `--text-2` / `--text-2-alt` in styles/tokens.css. Their actual
    // values are hex and PAT-001 forbids a raw hex in this directory — even in a fixture string — so
    // the fixture uses stand-in values. Only the property NAMES are under test here.
    const css = ':root { --text-2-alt: alt-value; --text-2: body-value; }';
    expect(cssDeclaration(css, ':root', '--text-2')).toBe('body-value');
    expect(cssDeclaration(css, ':root', '--text-2-alt')).toBe('alt-value');
  });
});

describe('cssDeclaration — @media scoping', () => {
  const css = `
    .retry { padding: 5px 10px; }
    @media (max-width: 760px) { .retry { min-height: 44px; } }
    @media (max-width: 640px) { .retry { letter-spacing: 0.06em; } }
  `;

  it('finds a rule inside the requested breakpoint', () => {
    expect(cssDeclaration(css, '.retry', 'min-height', { media: '(max-width: 760px)' })).toBe('44px');
  });

  it('matches the query whitespace-insensitively but not loosely', () => {
    expect(cssDeclaration(css, '.retry', 'min-height', { media: '(max-width:760px)' })).toBe('44px');
    // A DIFFERENT breakpoint is not the requested one. This is the assertion that makes
    // "44px, but only below 640px" a failure rather than a pass.
    expect(cssDeclaration(css, '.retry', 'min-height', { media: '(max-width: 640px)' })).toBeNull();
  });

  it('does not answer a base-rule query from inside a media block', () => {
    expect(cssDeclaration(css, '.retry', 'min-height')).toBeNull();
    expect(cssDeclaration(css, '.retry', 'padding')).toBe('5px 10px');
  });

  it('does not answer a media query from a base rule', () => {
    expect(cssDeclaration(css, '.retry', 'padding', { media: '(max-width: 760px)' })).toBeNull();
  });
});

describe('cssSelectors — the enumerator the closing guards are built on', () => {
  // Its failure mode is the same false pass as the reader's, arriving from the opposite direction: a
  // guard that asks "is every selector carrying the reserved token classified?" goes green for free
  // if the enumeration comes back short. So the cases below are the ways it could come back short.
  it('splits a selector LIST into its members and keeps attribute selectors intact', () => {
    const css = ".chip[data-state='not_built'], .chip[data-state='loading'] { color: var(--text-3); }";
    expect(cssSelectors(css)).toEqual([
      ".chip[data-state='not_built']",
      ".chip[data-state='loading']",
    ]);
  });

  it('deduplicates a selector declared by more than one rule', () => {
    expect(cssSelectors('.retry { color: a; } .retry { min-height: 44px; }')).toEqual(['.retry']);
  });

  it('does not invent a selector out of a comment', () => {
    // The defect this closes: a stylesheet that DOCUMENTS a class it does not style would otherwise
    // enter the enumeration, and the classification guard would demand a row for a rule that does
    // not exist — training the next reader to ignore it.
    const css = '/* .notQualified { color: var(--text-3); } was removed */ .retry { color: a; }';
    expect(cssSelectors(css)).toEqual(['.retry']);
  });

  it('separates base scope from a breakpoint, in both directions', () => {
    const css = '.a { color: x; } @media (max-width: 760px) { .b { min-height: 44px; } }';
    expect(cssSelectors(css)).toEqual(['.a']);
    expect(cssSelectors(css, { media: '(max-width: 760px)' })).toEqual(['.b']);
    expect(cssSelectors(css, { media: '(max-width: 640px)' })).toEqual([]);
  });

  it('returns the selectors that answer a cssDeclaration query, so the two agree', () => {
    // The pairing the closing guard depends on: every selector this returns must be one
    // `cssDeclaration` can then be asked about. If the two disagreed on selector spelling — a
    // stray space, a kept comma — the filter in the guard would silently match nothing.
    const css = ".a, .b[data-x='y'] { color: var(--text-3); } .c { color: var(--text-2); }";
    const dim = cssSelectors(css).filter((s) => cssDeclaration(css, s, 'color') === 'var(--text-3)');
    expect(dim).toEqual(['.a', ".b[data-x='y']"]);
  });
});

describe('cssDeclaration — the one piece of cascade it resolves', () => {
  it('returns the LAST value declared for the property on that selector', () => {
    const css = '.retry { min-height: 20px; } .retry { min-height: 44px; }';
    expect(cssDeclaration(css, '.retry', 'min-height')).toBe('44px');
  });

  it('returns the last value within a single block too', () => {
    expect(cssDeclaration('.retry { color: var(--text-3); color: var(--text-2); }', '.retry', 'color'))
      .toBe('var(--text-2)');
  });
});
