// Sentence-case prose on the two ProofArena routes renders at the body-copy token, not at a
// micro-label token.
//
// THE AUTHORITY, quoted by token name rather than by literal value because PAT-001 makes
// `styles/tokens.css` the only file in the scanned tree permitted to carry a raw hex — including
// inside a comment. PROOFARENA-OKX-DELTA-HANDOFF §12:302-304:
//
//   :302 — "every sentence-case string — descriptor, thesis, FOOTNOTES, CHECK DESCRIPTIONS,
//          exclusion notes, EMPTY/ERROR BODIES, the product-boundary disclaimer — renders at
//          [the descriptive-mono value] (≈5.4:1 on panel) or [the body value] (≈6.4:1), at 10px mono
//          minimum. [The two darkest values] are reserved for 8–9px uppercase letter-spaced
//          micro-labels ONLY" — and it cites Direction A's own `Veridex.dc.html` proof-check
//          description rows using the descriptive-mono value "for exactly this class of copy".
//   :304 — "Token rule for implementation: `--text-3`/`--text-4` = short uppercase mono labels and
//          timestamps. `--text-2` = sentence-case sans body and honesty-critical disclaimers. DO NOT
//          PUT A MULTI-SENTENCE STRING BELOW [the descriptive-mono value]."
//
// This app's token set has no descriptive-mono value between the body token and the micro-label
// tokens, so `--text-2` is the only token that satisfies :302 for prose. The product-boundary
// disclaimer took the same route in 633bcc7; its assertion lives in
// app/(proofarena)/route-scope.test.ts and is the precedent this file follows.
//
// THIS IS A SEMANTIC CLASSIFICATION, NOT A FIND-AND-REPLACE. `.panelLabel` in
// TrialMatchCard.module.css is ALSO on `--text-3` and MUST STAY THERE: 10px mono with
// `letter-spacing: 0.1em`, rendering `SHARED EVIDENCE RAIL`, `SETTLEMENT`, `AGENT DECISIONS`,
// `FAIR-PLAY CHECKS · {payer}` and `COST SENSITIVITY · …` — precisely the "short uppercase mono
// labels" :304 reserves the token for. So the guard below asserts a PARTITION over named selectors,
// and the micro-label side is asserted as positively as the prose side. A guard that counted
// `--text-3` occurrences in these files, or that a change to `.panelLabel` could satisfy, would
// assert the opposite of the rule it is here to hold.
//
// WHY PER SELECTOR, restating what `lib/css-source.ts` was built for: a bare
// `expect(css).not.toMatch(/--text-3/)` is both too weak and wrong — wrong because these files
// legitimately keep the token on table headers, field labels, phase labels, dash cells and dashed
// status chips, and too weak because a text search over raw CSS can be satisfied by a comment. Two
// false passes of exactly those shapes have already been observed in this repo (see
// lib/css-source.ts). `cssDeclaration` answers per selector, per property and per breakpoint, and
// strips comments before parsing.
//
// HONEST LIMITATION. jsdom applies no CSS module and computes no colour, so nothing here measures a
// rendered contrast ratio; these are source contracts over authored intent. The rendered evidence is
// a Chrome measurement of each surface, recorded in the implementation report, and it is what
// actually establishes that these declarations clear AA.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { cssDeclaration, cssSelectors } from '@/lib/css-source';

const read = (file: string) => readFileSync(resolve(__dirname, file), 'utf8');

/** The token §12:304 assigns to sentence-case body and honesty-critical disclaimers. */
const BODY = 'var(--text-2)';
/** The two tokens §12:302 reserves for 8–9px uppercase letter-spaced micro-labels ONLY. */
const MICRO_LABEL = ['var(--text-3)', 'var(--text-4)'] as const;

// Every selector on these two routes that colours sentence-case prose, with the clause of §12:302
// that classifies it and the copy it renders. `role` is not decoration: it is the argument for the
// classification, and a reader disputing one of these rows needs to dispute the row's own reason.
//
// THE DISCRIMINATOR IS THE RENDERED CONTENT AT THE CALL SITES, NOT THE STYLESHEET. Four earlier
// passes undercounted this cluster, and the last classifier that tried to automate it looked for
// `text-transform: uppercase` in the CSS — which flags `.hashLabel`, `.fieldLabel` and `.checkPhase`
// as violations even though the first two render strings that are already uppercase IN THE JSX
// (`⬢ SEALED EVIDENCE`, `EVENT-ANCHORED ENTRY`), and misses `.chip[data-state='not_built']`, whose
// lowercase `not built` IS uppercased by CSS. Neither the stylesheet's `text-transform` nor the
// literal's casing decides this on its own: the rendered string and its COPY CLASS do. Every row in
// the three lists below therefore cites the call site it was classified from.
const PROSE = [
  {
    file: 'SeasonScreen.module.css',
    selector: '.leadSub',
    role: '§12:302 honesty-critical, and §12:304 multi-sentence — "Official ranking is Brier-first. '
      + 'Markout is a legibility metric, not a trading result." (SeasonScreen.tsx:105)',
  },
  {
    file: 'SeasonScreen.module.css',
    selector: '.footNote',
    role: '§12:302 names "footnotes" verbatim — the four standings footnotes, each multi-sentence '
      + '(SeasonScreen.tsx:345-361)',
  },
  {
    file: 'SeasonScreen.module.css',
    selector: '.stateSub',
    role: '§12:302 names "empty/error bodies" — the transport-failure and no-season explanations '
      + '(SeasonScreen.tsx:200, :219, :256, :288)',
  },
  {
    file: 'SeasonScreen.module.css',
    selector: '.metaRow',
    role: 'MIXED, and therefore prose. The row colours BOTH registers: `.metaLabel` (SEASON) '
      + 're-declares the micro-label token and keeps it, but the unclassed `season-combo` span '
      + 'beside it INHERITS from here, and it renders the season id, the humanized combo and the '
      + 'exploratory suffix "· below the 40-trial gate" — a lowercase honesty qualifier stating '
      + 'that the combo did not reach the gate (SeasonScreen.tsx:275-287, handoff §6:189). Under '
      + 'the one-way reading a mixed carrier moves; the label inside it stays dim, so the register '
      + 'distinction survives the move',
  },
  {
    file: 'SeasonScreen.module.css',
    selector: '.notQualified',
    role: '§12:302 names "exclusion notes" verbatim, and these are the handoff\'s own: it lists '
      + '`qualified` / `not qualified` / `n/a — control` at §5:145 and explains at §9:257 why a '
      + 'control renders `n/a — control` rather than "not qualified". Renders 11px, lowercase, '
      + 'letter-spacing normal — it fails ALL THREE criteria of the reservation, and it renders in '
      + 'the DEFAULT qualified state on every non-qualified contestant row and every control row '
      + '(SeasonScreen.tsx:403, :407)',
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: '.panelSub',
    role: '§12:302 sentence-case strings — the per-panel explanatory line, including "does this '
      + "receipt's record re-derive? · receipt status …\" (TrialMatchCard.tsx:419, :621, :792, :876)",
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: '.stateSub',
    role: '§12:302 "empty/error bodies" and §12:304 multi-sentence — the pending-settlement '
      + 'derivation disclosure and the 404-is-not-a-transport-failure line (TrialMatchCard.tsx:313, '
      + ':550, :844, :849)',
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: '.footNote',
    role: '§12:302 names "footnotes" verbatim — `max-width: 88ch` of multi-sentence prose, including '
      + 'the paper-markout and no-aggregate-badge lines (TrialMatchCard.tsx:432-454, :514, :592, '
      + ':715-742, :926-932)',
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: '.checkDesc',
    role: '§12:302 names "check descriptions" verbatim and cites Direction A\'s own proof-check '
      + 'description rows as the precedent — the eight CHECK_DESCRIPTION sentences '
      + '(TrialMatchCard.tsx:834)',
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: '.tag',
    role: '§12:302 "the product-boundary disclaimer" / §12:304 honesty-critical disclaimer — the '
      + 'single call site renders "diagnostic — does not change ranking" (TrialMatchCard.tsx:879), '
      + 'the one line that stops a judge reading the cost sweep as a ranking. `.verbatimUpper` '
      + 'renders it uppercase, so it meets the reservation\'s TYPOGRAPHIC form; it is moved on '
      + 'CONTENT, because a claim-scoping disclaimer is the protected class and the reservation is a '
      + 'floor for that class rather than a ceiling for the permitted one. Handoff §3:38 separately '
      + 'assigns the diagnostic tag the warning amber — a design-fidelity delta recorded, not taken '
      + 'here',
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: ".checkStatus[data-status='not_served']",
    role: 'An absence-of-verdict disclosure, the same copy class as `.notQualified` above. Every '
      + 'REAL verdict in this pill family is `c.status.toUpperCase()` — PASS / FAIL / PENDING — and '
      + 'this variant is the one member that renders the lowercase authored string "not served" '
      + '(TrialMatchCard.tsx:835-836), because the backend served no key and the verifier therefore '
      + 'made no verdict. It is not a data code and not an uppercase label. What keeps it from '
      + 'reading as a verdict is its NEUTRAL hue against three coloured verdicts and its lowercase '
      + 'spelling — measured, not the dashed border, which is `--border` at 1.14:1 on the box behind '
      + 'it — and both survive a dark-neutral-to-light-neutral move',
  },
] as const;

// The other side of the partition, stated EXHAUSTIVELY. Between them, MICRO and PERMITTED below
// name every remaining `--text-3` selector in these two files, so the classification is closed
// rather than sampled: a `grep -- --text-3` over either file yields nothing that is not either in
// PROSE above (moved) or in one of these two lists (adjudicated to stay, with its call site).
//
// MICRO is the reservation's own class — short uppercase letter-spaced mono, whether the uppercase
// comes from the literal or from `text-transform`. Each row is asserted to STAY on a micro-label
// token, so "move everything to the body token" is a failing change rather than a passing one.
const MICRO = [
  {
    file: 'TrialMatchCard.module.css',
    selector: '.panelLabel',
    role: '10px mono, letter-spacing 0.1em — SHARED EVIDENCE RAIL / SETTLEMENT / AGENT DECISIONS / '
      + 'FAIR-PLAY CHECKS / COST SENSITIVITY (TrialMatchCard.tsx:406, :470, :620, :788, :873)',
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: '.hashLabel',
    role: '9px mono, letter-spacing 0.08em — ⬢ SEALED EVIDENCE / ONE RECORD · ONE HASH '
      + '(TrialMatchCard.tsx:424, :430)',
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: '.fieldLabel',
    role: '9px mono, letter-spacing 0.06em — every `<Field label>` on the settlement grid is an '
      + 'uppercase literal: EVENT-ANCHORED ENTRY, SETTLEMENT CANDLE CLOSE, CLOSE TIMESTAMP, '
      + 'OBSERVATION LAG, FOLLOW / FADE MARKOUT, FOLLOW_PROFITABLE, COMMIT WINDOW, REMAINING '
      + 'HORIZON (TrialMatchCard.tsx:498-507, :538-540, :576-585 via :604)',
  },
  {
    file: 'SeasonScreen.module.css',
    selector: '.metaLabel',
    role: '10px mono, letter-spacing 0.1em — SEASON (SeasonScreen.tsx:276)',
  },
  {
    file: 'SeasonScreen.module.css',
    selector: '.table th',
    role: '9px mono, letter-spacing 0.06em — the standings column headers, every one uppercase '
      + 'either as a literal (ORD, AGENT, ROLE, ACTIVE DECISIONS, UN-SCORED, QUALIFIED) or via '
      + '`.upper` over the frozen lowercase markout label (SeasonScreen.tsx:317-332)',
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: '.table th',
    role: '9px mono, letter-spacing 0.06em — the participants and cost-sweep column headers, same '
      + 'uppercase-literal-or-`.upper` construction (TrialMatchCard.tsx:692-702, :888-895)',
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: '.table td::before',
    role: 'The same participant and cost-sweep column labels repeated at narrow widths after the '
      + 'real table headers are hidden: 9px, letter-spacing 0.06em, uppercase presentation, with '
      + 'content sourced from each cell data-label (TrialMatchCard.tsx participant and markout rows)',
  },
  {
    file: 'SeasonScreen.module.css',
    selector: '.secondary',
    role: 'THE STYLESHEET CANNOT BE READ ALONE HERE EITHER: its only call site is the markout '
      + '<th> (SeasonScreen.tsx:325), where `.table th` (0,1,1) outranks `.secondary` (0,1,0) and '
      + 'supplies the colour — so this declaration is INERT, and it declares the same token the '
      + 'winning rule does. Uppercase micro-label content either way',
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: '.secondary',
    role: 'Same construction and same inertness — only call site is the markout <th> '
      + '(TrialMatchCard.tsx:699), outranked by `.table th`',
  },
  {
    file: 'SeasonScreen.module.css',
    selector: ".chip[data-state='not_built']",
    role: 'A status pill, and the CASING IS THE CHECK: `STATE_CHIP.not_built` is the lowercase '
      + 'literal `not built` (SeasonScreen.tsx:38), but `.chip` carries `text-transform: uppercase`, '
      + 'so it RENDERS `NOT BUILT` at 9px mono / letter-spacing 0.08em (SeasonScreen.tsx:111-113)',
  },
  {
    file: 'SeasonScreen.module.css',
    selector: ".chip[data-state='loading']",
    role: 'Same rule, same pill — renders `LOADING` (`STATE_CHIP.loading`, SeasonScreen.tsx:34)',
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: ".chip[data-status='none']",
    role: 'The absent-outcome status pill — `status` is the literal `none` when `trial.outcome === '
      + 'null` (TrialMatchCard.tsx:354) and `.chip` uppercases it to `NONE` at 9px mono / '
      + 'letter-spacing 0.08em (TrialMatchCard.tsx:368)',
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: ".actionChip[data-action='ABSTAIN']",
    role: 'Uppercase in the DATA, not by CSS: `TrialAction` is `FOLLOW | FADE | ABSTAIN` '
      + '(lib/contracts.ts:504) and the chip relays `r.action` verbatim at 9px mono / '
      + 'letter-spacing 0.06em (TrialMatchCard.tsx:757). A decision word, not a disclosure',
  },
] as const;

// PERMITTED is the honest remainder: neither an uppercase micro-label nor a member of any class
// §12:302 protects. Naming them as their own list rather than filing them under MICRO is the point —
// calling `commit-time` or an em dash a "short uppercase mono label" would be a false justification,
// and the next reviewer would be right to reject it. They stay dim because the authority does not
// reach them, which is a different argument from being what it reserves the token for.
const PERMITTED = [
  {
    file: 'TrialMatchCard.module.css',
    selector: '.checkPhase',
    role: 'A phase CODE, lowercase because the contract spells it lowercase: renders '
      + '`{c.phase}-time` → `commit-time` / `outcome-time` from `phase: \'commit\' | \'outcome\'` '
      + '(lib/contracts.ts:599, lib/signal-trials-api.ts:47-54, TrialMatchCard.tsx:833). Not prose, '
      + 'not a footnote, not a disclosure — and it is the dim tag the `.checkDesc` sentence beside '
      + 'it is read AGAINST, so brightening it would collapse the one register pair in the check row',
  },
  {
    file: 'SeasonScreen.module.css',
    selector: '.dash',
    role: 'A single em dash standing in for a null metric (SeasonScreen.tsx:388, :393). §12:302 '
      + 'governs "every sentence-case STRING" and §12:304 forbids "a MULTI-SENTENCE STRING" below '
      + 'the body token; neither reaches one glyph, and the dim colour is what separates "no value" '
      + 'from the `--text-1` values in the same column. Recorded as a legibility observation in the '
      + 'implementation report rather than changed under an authority that does not cover it',
  },
] as const;

describe('sentence-case prose on the ProofArena routes clears the body-copy token', () => {
  it.each(PROSE)('$file $selector renders at the body token', ({ file, selector, role }) => {
    expect(
      cssDeclaration(read(file), selector, 'color'),
      `${file} ${selector} must be ${BODY} — ${role}`,
    ).toBe(BODY);
  });

  it.each(PROSE)('$file $selector is NOT on a reserved micro-label token', ({ file, selector, role }) => {
    // Stated as its own case per selector because it is the specific defect, and because the
    // positive assertion above would also be satisfied by a third token that happened to be
    // brighter. §12:302 reserves these two values for 8–9px uppercase letter-spaced micro-labels
    // ONLY; every selector here is sentence-case prose at 10–11px. A future edit that reaches for
    // either token fails here with the reason attached.
    const color = cssDeclaration(read(file), selector, 'color');
    for (const reserved of MICRO_LABEL) {
      expect(color, `${file} ${selector} is not a micro-label — ${role}`).not.toBe(reserved);
    }
  });
});

describe('the genuine micro-labels keep the token §12:304 reserves for them', () => {
  it.each([...MICRO, ...PERMITTED])('$file $selector stays on a micro-label token', ({ file, selector, role }) => {
    // The negative control, and the reason this task is a classification rather than a
    // find-and-replace of the token in two files. If these moved too, the routes would lose the
    // register distinction between a label and the prose under it — and a guard that only checked
    // the prose side would call that a pass.
    expect(
      MICRO_LABEL as readonly string[],
      `${file} ${selector} must stay on a micro-label token — ${role}`,
    ).toContain(cssDeclaration(read(file), selector, 'color'));
  });

  it('keeps the two registers on DIFFERENT tokens', () => {
    // The partition stated as one assertion. It fails if the prose token and the label token are
    // ever collapsed onto each other — from either direction, and without needing to know which
    // token each side ended up on.
    const prose = new Set(PROSE.map(({ file, selector }) => cssDeclaration(read(file), selector, 'color')));
    const micro = new Set(
      [...MICRO, ...PERMITTED].map(({ file, selector }) => cssDeclaration(read(file), selector, 'color')),
    );
    expect(prose.size, `prose renders at ${prose.size} different tokens, not one`).toBe(1);
    for (const label of micro) {
      expect(prose.has(label), 'a micro-label token is being used for prose').toBe(false);
    }
  });
});

describe('the classification is CLOSED, not sampled', () => {
  // THIS IS THE GUARD THAT IS MEANT TO MAKE A FIFTH INCREMENT UNNECESSARY. Every assertion above
  // checks a selector someone wrote down, so on its own the suite can only ever be as complete as
  // the last person's enumeration — and four consecutive passes proved that is not complete enough
  // (1 member, then 5, then 7, then 9). This one inverts the direction: it reads the reserved token
  // OUT of the stylesheets and fails on any selector carrying it that no list above classifies. A
  // future edit that reaches for `--text-3` in either file cannot land silently; it has to be filed
  // as MICRO, as PERMITTED, or fixed.
  const FILES = ['SeasonScreen.module.css', 'TrialMatchCard.module.css'] as const;
  const classified = new Set(
    [...MICRO, ...PERMITTED].map(({ file, selector }) => `${file} ${selector}`),
  );

  it.each(FILES)('%s has no unclassified selector on a reserved token', (file) => {
    const css = read(file);
    const dim = cssSelectors(css).filter((selector) => {
      const color = cssDeclaration(css, selector, 'color');
      return color !== null && (MICRO_LABEL as readonly string[]).includes(color);
    });
    // Positive first: if this ever came back empty the filter would have silently stopped working,
    // and an empty set trivially satisfies the subset check below.
    expect(dim.length, `no selector in ${file} carries a reserved token — the filter is broken`)
      .toBeGreaterThan(0);
    expect(
      dim.filter((selector) => !classified.has(`${file} ${selector}`)),
      `${file} carries the reserved token on selectors no list in this file classifies. Each one is `
        + 'either a micro-label (add it to MICRO with its call site), outside §12:302 entirely (add '
        + 'it to PERMITTED with the reason), or prose that must move to the body token.',
    ).toEqual([]);
  });

  it.each(FILES)('%s classifies every selector it lists, in the file it lists it for', (file) => {
    // The other way this could rot: a row whose selector no longer exists, or was copied to the
    // wrong file. Either leaves a classification that looks maintained and checks nothing — and
    // `cssDeclaration` returns `null` for a missing selector, which the `toContain` assertion above
    // would report as a token mismatch rather than as a stale row.
    for (const { file: rowFile, selector } of [...MICRO, ...PERMITTED, ...PROSE]) {
      if (rowFile !== file) continue;
      expect(
        cssSelectors(read(file)),
        `${file} no longer contains ${selector} — the row classifying it is stale`,
      ).toContain(selector);
    }
  });

  it.each(FILES)('%s recolours nothing at a breakpoint, so the base scope is the whole story', (file) => {
    // Every assertion in this file reads the BASE rule. That is only a complete account of the
    // rendered colour if no `@media` block in these stylesheets sets one — otherwise a phone could
    // get a token the desktop guard never sees. Asserted rather than assumed, and asserted over the
    // breakpoints the files actually declare.
    const css = read(file);
    for (const media of ['(max-width: 760px)', '(max-width: 640px)']) {
      for (const selector of cssSelectors(css, { media })) {
        expect(
          cssDeclaration(css, selector, 'color', { media }),
          `${file} ${media} ${selector} sets a colour — the base-rule guards no longer cover it`,
        ).toBeNull();
      }
    }
  });
});

describe('the prose token is not reached by a technicality', () => {
  it('reads the BASE rule, not a rule that only applies at some viewport width', () => {
    // `.checkDesc` appears twice in TrialMatchCard.module.css: the base rule at :146 and the 640px
    // grid reflow at :183 (`.checkPhase, .checkDesc { grid-column: 1 / -1; }`). Confirming the
    // reader answers from the base rule is the difference between asserting the colour every
    // viewport gets and asserting one a phone gets. `cssDeclaration` with no `media` REMOVES every
    // `@media` block first, so this cannot be satisfied by a narrow-only override.
    const css = read('TrialMatchCard.module.css');
    expect(cssDeclaration(css, '.checkDesc', 'color')).toBe(BODY);
    expect(
      cssDeclaration(css, '.checkDesc', 'color', { media: '(max-width: 640px)' }),
      'the 640px block must not carry a colour — the base rule is the one that must be right',
    ).toBeNull();
  });

  it('does not reach the right token by retinting it', () => {
    // The token indirection is what makes the assertions above readable; it is also a way to satisfy
    // them while regressing the rendered colour, by pointing `--text-2` at a darker value. The
    // VALUE pin lives in __tests__/token-conformance.test.ts — PAT-001 exempts that file and forbids
    // a hex here. What is checkable in this file is the relationship: the body token and the two
    // reserved tokens must remain three DIFFERENT values in the dark theme.
    const tokens = readFileSync(resolve(__dirname, '../../../styles/tokens.css'), 'utf8');
    const body = cssDeclaration(tokens, ':root', '--text-2');
    expect(body).not.toBeNull();
    expect(body).not.toBe(cssDeclaration(tokens, ':root', '--text-3'));
    expect(body).not.toBe(cssDeclaration(tokens, ':root', '--text-4'));
  });
});
