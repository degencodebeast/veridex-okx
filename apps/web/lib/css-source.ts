// Read a single declaration out of a CSS SOURCE file, scoped to one selector.
//
// WHY THIS EXISTS. jsdom applies no CSS module and performs no layout, so no test in this suite can
// measure a rendered box or a computed colour. The fallback is to assert against the stylesheet
// source — but a bare `expect(css).toMatch(/min-height:\s*44px/)` asserts almost nothing. Two false
// passes have already been observed in this repo:
//
//   1. A 44px assertion that passed while every real tap target was 20px, because the DECORATIVE
//      `.mark` carried `min-height: 44px`.
//   2. A guard satisfied by a COMMENT that merely contained the name it was searching for. These
//      stylesheets carry prose inside rule bodies — `.headAction` in SeasonScreen.module.css
//      contains the literal text `≥ 44px at the narrow breakpoint` — so a text search over raw CSS
//      can be satisfied by the explanation of a rule instead of the rule.
//
// So this reader (a) strips comments before parsing anything, which removes that entire class of
// false pass, and (b) answers per selector and per property rather than per file.
//
// WHAT IT DOES NOT DO, stated rather than implied: it resolves the cascade only in the trivial case
// this codebase needs — later rule wins among rules with the SAME selector text in ONE file. It
// does not compare specificity across different selectors, expand shorthands (`padding` does not
// answer a `padding-top` query), follow `var()` indirection, or know anything about inheritance or
// layout. A source assertion built on it pins AUTHORED INTENT; the rendered outcome still has to be
// measured in a browser.

/** Remove `/* … *\/` comments so no assertion can be satisfied by prose, and so a brace inside a
 *  comment cannot desynchronise block matching. */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '');
}

/** The body of the `{…}` block whose opening brace is at `open`, matched by brace depth. */
function blockBody(source: string, open: number): { body: string; end: number } {
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return { body: source.slice(open + 1, i), end: i };
    }
  }
  throw new Error('unbalanced braces in CSS source');
}

const normalizeQuery = (q: string) => q.replace(/\s+/g, '');

/**
 * Narrow `source` to the rules that apply in one place.
 *
 * With `media`, returns the concatenated bodies of every `@media` block whose query matches
 * (whitespace-insensitively) — so a rule found there is genuinely inside that breakpoint. Without
 * `media`, returns the source with every `@media` block REMOVED, so a base-rule query can never be
 * answered by a rule that only applies at some viewport width. Either way the result contains no
 * nested at-rule, which is what lets the flat rule scan below be correct.
 */
function scopeTo(source: string, media?: string): string {
  const atMedia = /@media\b([^{]*)\{/g;
  let out = '';
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = atMedia.exec(source)) !== null) {
    const open = match.index + match[0].length - 1;
    const { body, end } = blockBody(source, open);
    if (media === undefined) {
      out += source.slice(cursor, match.index);
    } else if (normalizeQuery(match[1]) === normalizeQuery(media)) {
      out += `\n${body}\n`;
    }
    cursor = end + 1;
    atMedia.lastIndex = cursor;
  }
  if (media === undefined) out += source.slice(cursor);
  return out;
}

/** Every declaration block belonging to a rule whose selector list contains `selector` exactly. */
function bodiesFor(scoped: string, selector: string): string[] {
  const bodies: string[] = [];
  const rule = /([^{}]+)\{([^{}]*)\}/g;
  let match: RegExpExecArray | null;
  while ((match = rule.exec(scoped)) !== null) {
    const selectors = match[1].split(',').map((s) => s.trim());
    if (selectors.includes(selector)) bodies.push(match[2]);
  }
  return bodies;
}

/**
 * The winning value of `property` on `selector`, or `null` if that selector never declares it.
 *
 * `selector` must match a comma-separated entry of the rule's selector list EXACTLY, which is the
 * point: `cssDeclaration(css, '.retry', 'min-height', …)` cannot be satisfied by a `min-height` on
 * any other class, and returns `null` — a failing assertion — if the rule is missing entirely.
 */
export function cssDeclaration(
  source: string,
  selector: string,
  property: string,
  options: { media?: string } = {},
): string | null {
  const scoped = scopeTo(stripComments(source), options.media);
  // `(?:^|;)` anchors the property name to the start of a declaration, so a `height` query is not
  // answered by `min-height`, and `--text-2` is not answered by `--text-2-alt`.
  const decl = new RegExp(`(?:^|;)\\s*${property.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*:\\s*([^;]+)`, 'g');
  let winner: string | null = null;
  for (const body of bodiesFor(scoped, selector)) {
    let match: RegExpExecArray | null;
    while ((match = decl.exec(body)) !== null) winner = match[1].trim();
    decl.lastIndex = 0;
  }
  return winner;
}
