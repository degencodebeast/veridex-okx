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
import { cssDeclaration } from '@/lib/css-source';

const read = (file: string) => readFileSync(resolve(__dirname, file), 'utf8');

/** The token §12:304 assigns to sentence-case body and honesty-critical disclaimers. */
const BODY = 'var(--text-2)';
/** The two tokens §12:302 reserves for 8–9px uppercase letter-spaced micro-labels ONLY. */
const MICRO_LABEL = ['var(--text-3)', 'var(--text-4)'] as const;

// Every selector on these two routes that colours sentence-case prose, with the clause of §12:302
// that classifies it and the copy it renders. `role` is not decoration: it is the argument for the
// classification, and a reader disputing one of these rows needs to dispute the row's own reason.
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
] as const;

// The other side of the partition. These are the genuine micro-labels in the same two files: short
// uppercase mono, letter-spaced, and each one is what §12:304 reserves the darker tokens FOR. They
// are asserted to STAY on a micro-label token, so that "move everything to the body token" is a
// failing change rather than a passing one.
const MICRO = [
  {
    file: 'TrialMatchCard.module.css',
    selector: '.panelLabel',
    role: '10px mono, letter-spacing 0.1em — SHARED EVIDENCE RAIL / SETTLEMENT / AGENT DECISIONS / '
      + 'FAIR-PLAY CHECKS / COST SENSITIVITY (TrialMatchCard.tsx:406, :470, :620, :788, :873)',
  },
  {
    file: 'TrialMatchCard.module.css',
    selector: '.checkPhase',
    role: '9px mono, letter-spacing 0.06em — the commit-time / outcome-time phase tag '
      + '(TrialMatchCard.tsx:833)',
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
    role: '9px mono, letter-spacing 0.06em — the settlement field labels (TrialMatchCard.tsx:604)',
  },
  {
    file: 'SeasonScreen.module.css',
    selector: '.metaLabel',
    role: '10px mono, letter-spacing 0.1em — SEASON (SeasonScreen.tsx:276)',
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
  it.each(MICRO)('$file $selector stays on a micro-label token', ({ file, selector, role }) => {
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
    const micro = new Set(MICRO.map(({ file, selector }) => cssDeclaration(read(file), selector, 'color')));
    expect(prose.size, `prose renders at ${prose.size} different tokens, not one`).toBe(1);
    for (const label of micro) {
      expect(prose.has(label), 'a micro-label token is being used for prose').toBe(false);
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
