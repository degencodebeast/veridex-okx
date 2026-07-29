import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { cssDeclaration } from '@/lib/css-source';

const seasonCss = readFileSync(resolve(__dirname, 'SeasonScreen.module.css'), 'utf8');
const trialCss = readFileSync(resolve(__dirname, 'TrialMatchCard.module.css'), 'utf8');
const shellCss = readFileSync(
  resolve(__dirname, '../../layout/ProofArenaShell.module.css'),
  'utf8',
);
const tokensCss = readFileSync(resolve(__dirname, '../../../styles/tokens.css'), 'utf8');
const railCssPath = resolve(__dirname, 'SignalStatePanel.module.css');
const splitCssPath = resolve(__dirname, 'TrialsSplitScreen.module.css');
const phone = '(max-width: 760px)';
const tablet = '(min-width: 900px) and (max-width: 1180px)';
const desktop = '(min-width: 1181px)';

function optionalCss(path: string): string {
  try { return readFileSync(path, 'utf8'); } catch { return ''; }
}

describe('SPEC-R1 selector-specific responsive source contract', () => {
  it('switches the season from desktop table to narrow cards rather than clipping overflow', () => {
    expect(cssDeclaration(seasonCss, '.tableWrap', 'display', { media: phone })).toBe('none');
    expect(cssDeclaration(seasonCss, '.seasonCards', 'display', { media: phone })).toBe('grid');
    expect(cssDeclaration(seasonCss, '.seasonCardAction', 'min-height', { media: phone })).toBe('44px');
    expect(cssDeclaration(seasonCss, '.headAction', 'min-height', { media: phone })).toBe('44px');
  });

  it('uses a vertical one-hash rail on phones, a 2×2 rail at intermediate widths, and four nodes on desktop', () => {
    const css = optionalCss(railCssPath);
    expect(cssDeclaration(css, '.railNodes', 'grid-template-columns', { media: phone })).toBe('1fr');
    expect(cssDeclaration(css, '.railNodes', 'grid-template-columns', { media: tablet }))
      .toBe('repeat(2, minmax(0, 1fr))');
    expect(cssDeclaration(css, '.railNodes', 'grid-template-columns', { media: desktop }))
      .toBe('repeat(4, minmax(0, 1fr))');
    expect(cssDeclaration(css, '.railNodes', 'border-left', { media: phone })).not.toBeNull();
  });

  it('stacks the comparison around its shared evidence band on phones', () => {
    const css = optionalCss(splitCssPath);
    expect(cssDeclaration(css, '.splitGrid', 'grid-template-columns', { media: phone })).toBe('1fr');
    expect(cssDeclaration(css, '.splitEvidence', 'order', { media: phone })).toBe('2');
    expect(cssDeclaration(css, '.splitSide:last-child', 'order', { media: phone })).toBe('3');
  });

  it('keeps intermediate trial composition one-column and desktop composition two-column', () => {
    expect(cssDeclaration(trialCss, '.trialComposition', 'grid-template-columns', { media: tablet }))
      .toBe('1fr');
    expect(cssDeclaration(trialCss, '.trialComposition', 'grid-template-columns', { media: desktop }))
      .toBe('minmax(0, 1.25fr) minmax(320px, 0.75fr)');
    expect(cssDeclaration(trialCss, '.back', 'min-height', { media: phone })).toBe('44px');
  });

  it('disables rail and split motion when reduced motion is requested', () => {
    const reduced = '(prefers-reduced-motion: reduce)';
    expect(cssDeclaration(optionalCss(railCssPath), '.railNode', 'transition', { media: reduced })).toBe('none');
    expect(cssDeclaration(optionalCss(splitCssPath), '.splitSide', 'transition', { media: reduced })).toBe('none');
  });
});

describe('ProofArena interaction and motion CSS contract', () => {
  it('gives every route-scoped keyboard target the exact deterministic focus ring', () => {
    for (const selector of [
      '.shell a:focus-visible',
      '.shell button:focus-visible',
      '.shell summary:focus-visible',
      ".shell [role='link']:focus-visible",
    ]) {
      expect(cssDeclaration(shellCss, selector, 'outline')).toBe('2px solid var(--accent)');
      expect(cssDeclaration(shellCss, selector, 'outline-offset')).toBe('2px');
    }
    const focusBlue = ['#3B', '82F6'].join('');
    expect(cssDeclaration(tokensCss, ':root', '--accent')).toBe(focusBlue);
  });

  it('runs one bounded railDraw and railFade entrance', () => {
    const css = optionalCss(railCssPath);
    expect(css).toMatch(/@keyframes\s+railDraw\s*\{/);
    expect(css).toMatch(/@keyframes\s+railFade\s*\{/);
    expect(cssDeclaration(css, '.rail', 'animation')).toBe('railFade 250ms ease-out both');
    expect(cssDeclaration(css, '.rail::before', 'animation')).toBe(
      'railDraw 250ms ease-out both',
    );
  });

  it('disables both rail animations under reduced motion', () => {
    const css = optionalCss(railCssPath);
    const reduced = '(prefers-reduced-motion: reduce)';
    expect(cssDeclaration(css, '.rail', 'animation', { media: reduced })).toBe('none');
    expect(cssDeclaration(css, '.rail::before', 'animation', { media: reduced })).toBe('none');
  });
});
