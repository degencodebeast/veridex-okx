// The transport-failure retry control's tap target, asserted across BOTH of its stylesheets.
//
// WHY ONE FILE FOR TWO COMPONENTS. There is only one retry control in the product as a judge
// experiences it — the button offered when a signal-trials fetch fails — but it is authored twice,
// byte-identically, in SeasonScreen.module.css (/trials) and TrialMatchCard.module.css
// (/trials/[trialId]). Those are adjacent routes a judge moves between. Fixing one ships ONE control
// at TWO sizes, which reads as a rendering bug rather than as the accessibility fix it was. So the
// cross-file equivalence is asserted here, in one place, rather than left implicit in two files.
//
// THE REQUIREMENT. CLAUDE-CODE-BRIEF §"At ≤ 760px … every tap target is ≥ 44px", restated at
// PROOFARENA-OKX-DELTA-HANDOFF §7:232 ("every tap target ≥ 44px (buttons in the prototype are 46px)")
// and §7:302. Both controls shipped at 10px type with `padding: 5px 10px` and no minimum, measuring
// ~25px at 390px.
//
// THE BREAKPOINT IS PART OF THE REQUIREMENT. TrialMatchCard.module.css had no 760px block at all —
// its only `@media` was 640px — so the honest fix adds one rather than reusing the breakpoint that
// happened to be there. A 44px minimum that only applies below 640px leaves the control undersized
// across a 120px band of real phone widths, so `cssDeclaration` is asked for the 760px block
// specifically and returns null for anything else.
//
// HONEST LIMITATION. jsdom applies no CSS module and performs no layout, so nothing here measures a
// rendered box; these are source contracts. The rendered evidence is a browser measurement at 390px,
// recorded in the implementation report, and it is what actually establishes that these declarations
// produce ≥ 44px controls.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { cssDeclaration } from '@/lib/css-source';

const NARROW = { media: '(max-width: 760px)' } as const;
const AA_MIN = '44px';

const read = (file: string) => readFileSync(resolve(__dirname, file), 'utf8');

// Each site: the stylesheet, and the testid of the button that wears `.retry` in the component that
// owns it — recorded so a reader can get from a failing assertion to the rendered control.
const SITES = [
  { file: 'SeasonScreen.module.css', route: '/trials', testId: 'season-retry' },
  { file: 'TrialMatchCard.module.css', route: '/trials/[trialId]', testId: 'trial-retry' },
] as const;

describe('both transport-failure retry controls meet the 44px narrow minimum', () => {
  it.each(SITES)('$route — .retry declares the minimum at the narrow breakpoint', ({ file, route }) => {
    expect(
      cssDeclaration(read(file), '.retry', 'min-height', NARROW),
      `${file}: .retry has no ${AA_MIN} minimum at ${NARROW.media} — the retry on ${route} is ~25px`,
    ).toBe(AA_MIN);
  });

  it('applies the minimum at the SAME breakpoint in both files', () => {
    // The anti-divergence assertion, and the reason this file is not two files. It fails if one
    // route's retry is fixed at 760px and the other's at 640px — which would leave the two controls
    // different sizes on any phone between those widths.
    const [season, card] = SITES.map(({ file }) =>
      cssDeclaration(read(file), '.retry', 'min-height', NARROW),
    );
    expect(season).toBe(card);
    expect(season).toBe(AA_MIN);
  });

  it('does not settle for the 640px breakpoint that TrialMatchCard already had', () => {
    // Stated positively as its own case because it is the specific shortcut available here: the card
    // stylesheet has a 640px block, so dropping the rule in there is the path of least resistance and
    // is wrong at 641–760px.
    for (const { file } of SITES) {
      const at640Only =
        cssDeclaration(read(file), '.retry', 'min-height', { media: '(max-width: 640px)' }) === AA_MIN
        && cssDeclaration(read(file), '.retry', 'min-height', NARROW) === null;
      expect(at640Only, `${file}: the minimum applies only below 640px`).toBe(false);
    }
  });

  it('keeps the control a real button box, not a min-height on a bare inline element', () => {
    // `min-height` does nothing on a non-replaced inline box. These are <button>s, which are not
    // inline, so the declaration takes effect — but the rule states its own box explicitly (the
    // `.headAction` idiom two rules up in SeasonScreen.module.css does the same) so the guarantee
    // does not rest on a UA default, and so the 10px label is centred in the taller box rather than
    // sitting at its top edge.
    for (const { file } of SITES) {
      expect(cssDeclaration(read(file), '.retry', 'display'), file).toBe('inline-flex');
      expect(cssDeclaration(read(file), '.retry', 'align-items'), file).toBe('center');
    }
  });
});
