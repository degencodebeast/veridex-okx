import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import { decodeTrialRouteSegment } from './route-segment';

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

// THE SEGMENT ARRIVES PERCENT-ENCODED, so it is decoded here — exactly once, and guarded.
//
// MEASURED on this tree, built and served (`next build` then `next start`, titles read off the
// wire), NOT inherited from a comment: `/trials/release+1` served `release%2B1 — …` and
// `/trials/%C3%A9preuve-1` served `%C3%A9preuve-1 — …`. Both are ids the publisher can mint (see
// the note below), so the un-decoded title named a trial that does not exist. An earlier revision
// of this file asserted the opposite — that the segment arrives decoded — and cited `release+1` as
// its example; the served bytes disprove it, and `route-metadata.transport.test.ts` now pins the
// served bytes rather than a return value so the claim cannot silently go stale again.
//
// WHY THE GUARD IS LOAD-BEARING AND MUST NOT BE "SIMPLIFIED" AWAY. `decodeURIComponent` throws
// URIError on a malformed percent sequence (`%`, `%zz`, a truncated `%C3`). Unguarded, that throw
// becomes a 500 on a PUBLIC route — strictly worse than the defect being fixed. On Next 15.5 those
// URLs are rejected with 400 by the server's own request handling before this function runs, so
// today the fallback is reachable only from a caller that is not the wire; it stays because the
// only thing standing between a malformed segment and a 500 must not be a version-specific
// behaviour of the layer above. Falling back to the RAW segment keeps the title naming the route's
// subject instead of blanking it.
//
// The return value is a STRING handed to Next's metadata API. Next escapes it into the document
// head — `<script>` comes back as `&lt;script&gt;` — so decoding widens what the title can SAY,
// never what it can DO. Nothing here builds markup. The same guarded helper is used by the
// hydrated page so the title and API requests cannot disagree about the route subject.

export async function generateMetadata(
  { params }: { params: Promise<{ trialId: string }> },
): Promise<Metadata> {
  // `params` is a promise in Next 15. `trialId` is the RAW, still-encoded segment.
  const { trialId } = await params;

  // THE ID IS NOT VALIDATED HERE, and that is the decision rather than an omission.
  //
  // There is no id grammar for this layer to check against. `open_live_trial`
  // (`veridex/signal_trials/live.py:150`) takes an operator-supplied `trial_id` and validates
  // NOTHING, and the repository's one check (`live.py:323`) refuses only an empty id, a path
  // separator, `.` and `..`. So `trial.release-1`, `release+1`, `épreuve-1` and ids past 64
  // characters are all REAL, PUBLISHABLE trials. Any shape gate written here would be a second,
  // narrower contract than the publisher's, and its only observable effect would be to drop the id
  // from the title of a legitimately published trial — which is the defect this file used to have.
  //
  // METADATA IS PRESENTATION, NOT A SECOND VALIDATOR. The title names the route's SUBJECT — which
  // trial this page is about — and claims nothing about it: no result, no settlement, no score.
  // The page BODY owns the distinction between a real trial, `trial_not_found`, and a transport
  // failure (`TrialMatchCard` keeps those apart deliberately), and the router's non-echo rule
  // (`veridex/api/signal_trials_router.py:321-322`) governs REFUSAL BODIES, not this template.
  // Nothing here does I/O: resolving the id would put a server→API dependency on a public route.
  return {
    title: `${decodeTrialRouteSegment(trialId)} — ${TITLE_TAIL}`,
    description: DESCRIPTION,
  };
}

export default function TrialMetadataLayout({ children }: { children: ReactNode }) {
  return children;
}
