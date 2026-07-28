// The reader under every CSS-source contract assertion in this repo. It is tested directly because
// its FAILURE MODE IS A FALSE PASS: if it were lenient, the accessibility guards built on it would
// go green against the exact defects they exist to catch. Each case below is one of those defects,
// written as CSS this reader must refuse to answer.
import { describe, it, expect } from 'vitest';
import { cssDeclaration } from './css-source';

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
