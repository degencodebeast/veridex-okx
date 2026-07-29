import type { ReactNode } from 'react';
import Link from 'next/link';
import styles from './ProofArenaShell.module.css';

// The chrome for the two PUBLIC ProofArena routes — `/trials` and `/trials/[trialId]` — and for
// nothing else. It is mounted by `app/(proofarena)/layout.tsx`, which is a SIBLING route group to
// `(app)`; that is what lets these two routes stop inheriting `AppShell` without any other route
// changing. Route groups are URL-transparent, so both paths are exactly what they were.
//
// WHY A SECOND SHELL RATHER THAN A FLAG ON THE FIRST. `AppShell` is a product frame with a claim
// attached: a five-section Veridex nav, a wallet control, and a `FIXTURE · IDLE · EXEC · WS idle ·
// verifier v0` run-status strip that describes a LIVE COMPETITION RUN. A reproducible benchmark has
// no run, no fixture, and no wallet session, so on these two routes every field in that strip is
// either blank or answering a question the page never asked. Threading a `variant` prop through
// `AppShell` would put that decision inside a component four other route groups depend on; a
// sibling group puts it in a file only these two routes can reach.
//
// NOTHING IS DELETED. `AppShell`, `TopNav` and `StatusBar` are untouched and every other route
// still renders them — pinned in both directions by `app/(proofarena)/route-scope.test.ts`.
//
// THIS IS A SERVER COMPONENT, deliberately. It holds no state and reads no pathname: both routes it
// wraps ARE the Arena section, so the tab's current-ness is a static fact about the group rather
// than something to derive from `usePathname()`. The screens inside it are the client components.

// PROOFARENA-EXACT-COPY.md §1 — frozen strings, rendered from constants rather than inline JSX text
// for two reasons that are both about the bytes. First, `agent's` carries an ASCII apostrophe
// (U+0027) in the copy authority and in ProofArena.dc.html, NOT a typographic U+2019, and a
// constant makes that visible and greppable instead of leaving it to an editor's autocorrect.
// Second, JSX text is subject to whitespace collapsing across line breaks; a string is not.
const CLOSING_LINE =
  'Smart money can still be exit liquidity. ProofArena tests whether your agent knows the difference.';

// The product boundary. It states what ProofArena is NOT, which is why it ships on BOTH routes
// (PROOFARENA-EXACT-COPY.md §1 says so explicitly) and why it lives here rather than in either
// screen: one shell, one copy of the sentence, no drift between the two routes.
//
// ON THE WORD `alpha`. §2 of the same document lists `alpha` as forbidden UI language, and this
// sentence contains it. That is a conflict only if the ban is read as a token filter: what §2
// forbids is CLAIMING alpha, and the single occurrence here is a clause denying that ProofArena
// sells any. The screen-level language guards keep banning the word outright, and the shell test
// permits it in this disavowal and nowhere else.
const PRODUCT_BOUNDARY =
  'ProofArena does not execute trades, does not sell alpha, and does not provide personalized ' +
  "investment advice. FOLLOW / FADE / ABSTAIN is a display derived from an agent's own " +
  'submitted probability.';

// `public/SKILL.md` is served from the image root. A plain <a> rather than <Link>: this is a static
// document, not a route, so there is nothing for the router to prefetch or client-navigate to.
const SKILL_HREF = '/SKILL.md';

export function ProofArenaShell({ children }: { children: ReactNode }) {
  return (
    <div className={styles.shell}>
      <header className={styles.header} data-testid="proofarena-header">
        <div className={styles.brand}>
          {/* Decorative: the wordmark beside it is the accessible name, so announcing the glyph
              would read the brand twice. */}
          <span className={styles.mark} aria-hidden>◆</span>
          <span className={styles.wordmark} data-testid="proofarena-wordmark">PROOFARENA</span>
        </div>

        <nav className={styles.nav} aria-label="ProofArena">
          {/* One tab, and it is honest that there is one. The prototype's shell has exactly this:
              the benchmark surface plus the document that explains it. Linking any legacy Veridex
              section from here is out of scope AND would import that product's navigation claim. */}
          <Link
            className={`${styles.navLink} ${styles.navActive}`}
            href="/trials"
            aria-current="page"
          >
            Arena
          </Link>
          <a
            className={styles.navLink}
            href={SKILL_HREF}
            target="_blank"
            rel="noreferrer"
            data-testid="shell-skill-md"
          >SKILL.md ↗</a>
        </nav>

        {/* The integration identity: which venue's signals the benchmark reads, and which chain it
            runs on. `eip155:196` is the CAIP-2 id, carried in full because the chain id is the part
            that makes the claim checkable. */}
        <div className={styles.integrations} data-testid="proofarena-integrations">
          <span className={styles.integration}>OKX DEX SIGNALS</span>
          <span className={styles.integration}>X LAYER · eip155:196</span>
        </div>
      </header>

      <div className={styles.column}>
        <main className={styles.main}>{children}</main>

        <footer className={styles.footer} data-testid="proofarena-footer">
          <div className={styles.footerBand}>
            <p className={styles.closingLine} data-testid="proofarena-closing-line">{CLOSING_LINE}</p>
            <div className={styles.footerActions}>
              <span className={styles.onboarding}>ONE-PROMPT ONBOARDING</span>
              <a
                className={styles.footerSkill}
                href={SKILL_HREF}
                target="_blank"
                rel="noreferrer"
                data-testid="footer-skill-md"
              >READ SKILL.md ↗</a>
            </div>
          </div>
          <p className={styles.disclaimer} data-testid="proofarena-disclaimer">{PRODUCT_BOUNDARY}</p>
        </footer>
      </div>
    </div>
  );
}
