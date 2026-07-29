// The EXACT-COPY contract for the two ProofArena routes' page metadata.
//
// WHY THIS FILE EXISTS AS ITS OWN FILE, and why it is worth reading before editing it.
//
// PROOFARENA-OKX-DELTA-HANDOFF §4 ("Page metadata", :107-116) specifies metadata PER ROUTE, and it
// specifies it as copy — two distinct titles and two distinct descriptions, given as literal strings.
// The version this file replaces asserted only `title.toContain('ProofArena')` and
// `description.length > 0` against a SINGLE metadata object exported by the group layout. Every one
// of those assertions passed while `/trials/[trialId]` served the season title, carrying no trial id
// and no Fair-Play identity — a well-built test of the wrong requirement. A guard that a substring
// match can satisfy cannot detect a metadata object that is merely plausible, so nothing here
// matches on substrings: every assertion in the exact-copy blocks compares a WHOLE string.
//
// The strings below are transcribed from :107-116. They are the authority; this file is a copy of it,
// so if the two ever disagree the handoff wins and these constants are what changes.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// ---------------------------------------------------------------------------
// The copy, transcribed from PROOFARENA-OKX-DELTA-HANDOFF :107-116.
//
// The handoff renders the descriptions wrapped across source lines; a wrapped line break is
// typographic, not part of the string, so each is collapsed to single spaces here.
//
// PUNCTUATION IS PART OF THE COPY. Both titles carry U+2014 EM DASH and the trial title carries
// U+00B7 MIDDLE DOT. Those two characters have near-identical-looking neighbours that an editor,
// a paste through a smart-quote filter, or a well-meaning "fix the dashes" pass will substitute:
// U+2013 EN DASH, an ASCII hyphen, U+2022 BULLET, U+2027 HYPHENATION POINT. Read as glyphs they are
// indistinguishable in a diff, so `assertsCodepoints` below pins them by number.
// ---------------------------------------------------------------------------
const SEASON_TITLE = 'ProofArena — Reproducible benchmarks for financial agents';
const SEASON_DESCRIPTION =
  'Same sealed evidence. Different agent probabilities. One deterministic settlement and scoring '
  + 'law. Brier-first standings with predeclared negative controls. Paper benchmark — not a trade '
  + 'recommendation.';

// The trial title is a TEMPLATE, and it is written here as a function rather than as a string for a
// reason that is the whole point of the changing-id control below: a test that hardcodes one
// finished title cannot tell a real substitution from a constant that happens to contain that id.
const trialTitle = (trialId: string) => `${trialId} — Fair-Play trial · ProofArena`;
const TRIAL_DESCRIPTION =
  'One sealed evidence hash, one commit deadline, one settlement law. Paper markout after modeled '
  + 'costs. Not a trade recommendation.';

/** The codepoints of `s`, as hex, for the characters that are not plain ASCII. */
const nonAsciiCodepoints = (s: string) =>
  [...s].filter((c) => c.codePointAt(0)! > 0x7f).map((c) => c.codePointAt(0)!.toString(16));

const WEB = resolve(__dirname, '../..');
const read = (...seg: string[]) => readFileSync(resolve(WEB, ...seg), 'utf8');

type MetadataShape = { title?: unknown; description?: unknown; openGraph?: unknown };
type TrialLayoutModule = {
  generateMetadata?: (arg: { params: Promise<{ trialId: string }> }) => Promise<MetadataShape>;
};

const groupMetadata = async () =>
  ((await import('./layout')) as { metadata?: MetadataShape }).metadata ?? {};

const trialMetadata = async (trialId: string) => {
  const mod = (await import('./trials/[trialId]/layout')) as TrialLayoutModule;
  expect(
    typeof mod.generateMetadata,
    'the [trialId] layout must export generateMetadata — a static `metadata` object cannot vary '
    + 'with the route param',
  ).toBe('function');
  return mod.generateMetadata!({ params: Promise.resolve({ trialId }) });
};

describe('/trials serves the season metadata exactly as §4:107-111 specifies it', () => {
  it('titles the route with the whole season title and nothing else', async () => {
    // WHOLE-STRING equality, deliberately. The assertion this replaces was
    // `expect(title).toContain('ProofArena')`, which is satisfied by any title with the word in it
    // — including the one the trial route was wrongly serving.
    expect(String((await groupMetadata()).title)).toBe(SEASON_TITLE);
  });

  it('describes the route with the whole season description', async () => {
    expect(String((await groupMetadata()).description)).toBe(SEASON_DESCRIPTION);
  });

  it('spells the title punctuation with the exact codepoints', () => {
    // U+2014 EM DASH, once. An ASCII hyphen or U+2013 EN DASH reads the same in a diff.
    expect(nonAsciiCodepoints(SEASON_TITLE)).toEqual(['2014']);
    expect(nonAsciiCodepoints(SEASON_DESCRIPTION)).toEqual(['2014']);
  });

  it('does not carry the root product metadata onto this route', async () => {
    // The discrimination that was already right and stays: the shipped root metadata is
    // `Veridex — TxLINE Agent Proof Arena` and describes Solana anchoring. Both are true of the
    // legacy product; neither is true of these two routes, which read OKX DEX signals on X Layer
    // and anchor nothing.
    const { title, description } = await groupMetadata();
    for (const value of [String(title), String(description)]) {
      expect(value).not.toContain('Veridex');
      expect(value).not.toMatch(/solana/i);
    }
  });
});

describe('/trials/[trialId] serves trial-specific metadata exactly as §4:113-116 specifies it', () => {
  it('names the trial id in the title, composed as the handoff composes it', async () => {
    expect(String((await trialMetadata('trial-0k9f2c')).title)).toBe(
      trialTitle('trial-0k9f2c'),
    );
  });

  it('describes the route with the whole trial description', async () => {
    expect(String((await trialMetadata('trial-0k9f2c')).description)).toBe(TRIAL_DESCRIPTION);
  });

  it('spells the title punctuation with the exact codepoints', () => {
    // U+2014 EM DASH then U+00B7 MIDDLE DOT, in that order. `·` is the character most at risk here:
    // U+2022 BULLET and U+00B7 are one pixel apart at 11px and the shell's own micro-labels use
    // this same separator, so a copy-paste from the wrong one is a live failure mode.
    expect(nonAsciiCodepoints(trialTitle('id'))).toEqual(['2014', 'b7']);
    expect(nonAsciiCodepoints(TRIAL_DESCRIPTION)).toEqual([]);
  });

  // -------------------------------------------------------------------------
  // THE CHANGING-TRIAL-ID CONTROL.
  //
  // This is the assertion the defect got past, so it is the one worth stating twice. Asserting a
  // single hardcoded id proves nothing about substitution: a `generateMetadata` that ignored its
  // params entirely and returned the finished string `trial-0k9f2c — Fair-Play trial · ProofArena`
  // would satisfy the first test in this block. Only driving TWO different ids and requiring each
  // title to carry its own and NOT the other can distinguish a substitution from a constant.
  // -------------------------------------------------------------------------
  it('varies the title with the route param — two ids, two titles', async () => {
    const [a, b] = ['trial-0k9f2c', 'trial_h43'];
    const titleA = String((await trialMetadata(a)).title);
    const titleB = String((await trialMetadata(b)).title);

    expect(titleA).toBe(trialTitle(a));
    expect(titleB).toBe(trialTitle(b));
    expect(titleA).not.toBe(titleB);
    // Stated explicitly rather than left implied by the inequality above: a constant fails here
    // with the reason attached instead of as an opaque "expected A not to be A".
    expect(titleB, 'the title still carries the FIRST id — it is not reading the param').not.toContain(a);
    expect(titleA).not.toContain(b);
  });

  it('accepts every id shape the store actually mints', async () => {
    // `veridex/signal_trials/live.py:183` derives ids as `trial_{hex}`; the fixtures in this tree
    // use `trial-0k9f2c` and `trial-zzz999`. All must pass through verbatim — a shape gate that
    // rejected a real id would blank the title on a working page.
    for (const id of ['trial_a1b2c3d4e5f6', 'trial-0k9f2c', 'trial-zzz999', 'season001', 'A']) {
      expect(String((await trialMetadata(id)).title)).toBe(trialTitle(id));
    }
  });

  it('names every id the PUBLISHER can mint, not a narrower frontend guess at the shape', async () => {
    // MEASURED against the publisher, not assumed. `open_live_trial`
    // (`veridex/signal_trials/live.py:150`) takes an operator-supplied `trial_id` and validates
    // NOTHING, and the repository's one check (`live.py:323`) refuses only an empty id, a path
    // separator, `.` and `..`. So each id below is one a real operator can publish TODAY: a dot, a
    // plus, an accented letter, and 65 characters. Each must reach the title verbatim — dropping
    // one leaves a legitimately published trial serving a title that names no trial.
    for (const id of ['trial.release-1', 'release+1', 'épreuve-1', `trial-${'z'.repeat(59)}`]) {
      expect(String((await trialMetadata(id)).title)).toBe(trialTitle(id));
    }
  });

  // -------------------------------------------------------------------------
  // THERE IS NO FRONTEND ID GRAMMAR, and this block exists so that its absence is a STATED
  // contract rather than something a later edit can reinstate by accident.
  //
  // An earlier version of this layer gated the title behind `/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/`
  // and served a title naming no trial when a segment missed it. That gate was narrower than the
  // publisher (see the test above), so its only measurable effect was to drop the id from the
  // title of a real trial.
  //
  // The division of responsibility that replaces it: the TITLE names the route's SUBJECT and
  // asserts nothing about it — no result, no settlement, no score, which is all §4:119 asks of
  // metadata. The page BODY states whether the trial exists, and the router's non-echo rule
  // (`veridex/api/signal_trials_router.py:322-323`) governs REFUSAL BODIES, not this template.
  // -------------------------------------------------------------------------
  it('names whatever subject the route names, with no shape judgement of its own', async () => {
    for (const segment of [
      'ALPHA BEAT THE BASELINE BY 40 BPS',
      '<script>alert(1)</script>',
      'trial-0k9f2c?verdict=settled',
    ]) {
      expect(String((await trialMetadata(segment)).title)).toBe(trialTitle(segment));
    }
  });

  it('still states the settlement law whatever the segment is', async () => {
    // The description is fixed [COPY] about the law, not a claim about any particular trial, so it
    // is true of the route whatever the segment is. Blanking it would withhold the disclaimer
    // ("Not a trade recommendation") on exactly the requests most likely to be adversarial.
    expect(String((await trialMetadata('not an id')).description)).toBe(TRIAL_DESCRIPTION);
  });
});

describe('the metadata scope boundary', () => {
  it('claims no result in an og:image, on either route — §4:119', () => {
    // ":119 — No `og:image` claiming results". The safe form of that requirement is to declare no
    // openGraph image at all, so this asserts absence in the SOURCE of both layouts rather than on
    // the resolved object: Next fills `openGraph` in from other fields, so an assertion on the
    // resolved metadata could not tell a declared image from a derived one.
    for (const f of ['app/(proofarena)/layout.tsx', 'app/(proofarena)/trials/[trialId]/layout.tsx']) {
      const src = read(f);
      expect(src, `${f} declares an og:image`).not.toMatch(/\bopenGraph\b/);
      expect(src).not.toMatch(/og:image/);
    }
  });

  it('leaves the ROOT metadata alone — it is every other route\'s title', () => {
    // The root layout serves the marketing landing and all of `(app)`. Rewriting it would rename
    // the whole product from inside a two-route task.
    expect(read('app/layout.tsx')).toContain('Veridex — TxLINE Agent Proof Arena');
  });

  it('overrides the season title on the trial route ONLY by nesting, not by editing the group', async () => {
    // Next merges metadata nearest-wins PER FIELD, which is the mechanism the whole fix rests on:
    // the nested `[trialId]` layout replaces title and description for that route while `/trials`
    // keeps the group values. Asserting both objects in one test pins the relationship rather than
    // the two halves separately — the defect was precisely that ONE object served both routes.
    const season = String((await groupMetadata()).title);
    const trial = String((await trialMetadata('trial-0k9f2c')).title);
    expect(season).toBe(SEASON_TITLE);
    expect(trial).not.toBe(season);
  });
});
