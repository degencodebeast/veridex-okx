import type { Metadata } from 'next';
import type { ReactNode } from 'react';

// The metadata layout for `/trials/[trialId]`, and NOTHING else.
//
// WHY A LAYOUT AND NOT THE PAGE. `generateMetadata` is a server-only export: Next calls it while
// rendering the document head, before any client bundle runs. `page.tsx` in this directory is a
// `'use client'` component that reads its segment with `useParams` — the idiom the other client-side
// dynamic routes in this tree use — so it cannot carry a `generateMetadata` export at all. Nesting a
// SERVER layout here is what lets the route have per-trial metadata without converting the page to a
// server component or moving the three status machines the card owns across a boundary.
//
// The pass-through render is the point: this file adds a metadata scope, not chrome. The shell comes
// from the group layout one level up, so returning `children` untouched leaves the rendered tree and
// the page's client behaviour exactly as they were.
//
// WHY THIS OVERRIDES THE GROUP'S TITLE. Next merges metadata NEAREST-WINS PER FIELD down the layout
// chain, so the two fields declared here replace the group layout's season title and description for
// this route only, and `/trials` keeps them. That per-field merge is the entire mechanism: before
// this file existed, the group's single metadata object was the title for BOTH routes, and the trial
// deep link — the surface the OKX listing points at and the one people actually share — carried the
// season title, no trial id, and no Fair-Play identity.

// PROOFARENA-OKX-DELTA-HANDOFF §4 "Page metadata" :113-116, verbatim.
//
// The title is a template over the route param; the handoff writes it `{trial_id} — Fair-Play trial ·
// ProofArena`. Both separators are specified characters, not decoration: U+2014 EM DASH and U+00B7
// MIDDLE DOT. `app/(proofarena)/route-metadata.test.ts` pins them by codepoint, because an ASCII
// hyphen or a U+2022 BULLET is invisible in a diff.
const TITLE_TAIL = 'Fair-Play trial · ProofArena';
const DESCRIPTION =
  'One sealed evidence hash, one commit deadline, one settlement law. Paper markout after modeled '
  + 'costs. Not a trade recommendation.';

// The shape a trial id has. `veridex/signal_trials/live.py:183` derives ids as `trial_{hex}` from the
// evidence hash and the open instant; the fixtures in this tree use `trial-0k9f2c` and `trial_h43`.
// This gate is strictly WIDER than every id the store mints and strictly NARROWER than what a URL
// path segment can carry, which is exactly where the line belongs — see the non-echo note below.
const TRIAL_ID_SHAPE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

/**
 * The title for `trialId`, naming it only when the segment is shaped like a trial id.
 *
 * THE JUDGEMENT CALL, stated because it is one. `generateMetadata` runs before anything is known
 * about whether the id RESOLVES — the page discovers that from its own fetch, and renders
 * "Trial not found" when it does not. So the title is composed from caller-controlled text, and
 * there are two hazards to hold apart:
 *
 * 1. FABRICATING A TRIAL IDENTITY. The route contract for this API records the trial id as
 *    non-echoing (`veridex/api/signal_trials_router.py:322-323`, `PKT-TASK-H4-4.md:60-61`: the id
 *    "arrives from a URL path segment, so echoing it would reflect caller-controlled text back into
 *    logs and responses"), and the card's `not_found` branch honours that by rendering no id at all.
 *    A `<title>` is the most shareable reflection surface the page has — it is what a link preview
 *    shows — and §4:119 forbids metadata that claims a result. `/trials/ALPHA-BEAT-BASELINE-BY-40BPS`
 *    must not become a link preview asserting that.
 * 2. BLANKING A WORKING PAGE'S TITLE. Every real trial must get the handoff's exact title.
 *
 * A SHAPE GATE separates them without a network call: a segment shaped like an id passes through
 * verbatim, anything else gets a title that names no trial. What this deliberately does NOT do is
 * resolve the trial, and that is the substantive part of the decision rather than an omission —
 * fetching here would put a server→API dependency on a public route and, worse, would introduce a
 * third state the title cannot express. `TrialMatchCard` spends real effort keeping `not_found`
 * (a domain answer) distinct from `unavailable` (a transport failure); a title that fell back to the
 * un-named form when the fetch failed would silently report "no such trial" during an outage, which
 * is precisely the conflation the card refuses to make.
 *
 * The residue, stated plainly: a WELL-FORMED id that does not resolve still appears in the title.
 * That is intended. The title names the route's subject — which trial this page is about — and the
 * page BODY is what states whether it exists, where the non-echo rule is enforced against a real
 * 404. Nothing in the title claims a result, a settlement or a score for it.
 */
function titleFor(trialId: string): string {
  // The un-named form is the specified title with the `{trial_id} — ` prefix dropped, so the two
  // differ ONLY in whether an id is named. It is not a separate string to keep in step.
  return TRIAL_ID_SHAPE.test(trialId) ? `${trialId} — ${TITLE_TAIL}` : TITLE_TAIL;
}

export async function generateMetadata(
  { params }: { params: Promise<{ trialId: string }> },
): Promise<Metadata> {
  // `params` is a promise in Next 15 and the segment arrives percent-DECODED, which is what the
  // shape gate needs to see: `%3Cscript%3E` reaches this function as `<script>` and is refused as
  // the text it actually is, not as the encoding it travelled in.
  const { trialId } = await params;
  return { title: titleFor(trialId), description: DESCRIPTION };
}

export default function TrialMetadataLayout({ children }: { children: ReactNode }) {
  return children;
}
