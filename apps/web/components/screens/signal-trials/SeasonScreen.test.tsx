// H5.2 — the public season screen at /trials.
//
// EVERY render below is driven through the REAL H5.1 adapter (`getSignalTrialsSeason`) with only
// the FETCH layer stubbed. No test supplies a view model directly. That is deliberate and is the
// whole point of the third render group: the screen must be exercised through the adapter's
// season-404 → /health discriminator rather than around it, or these tests would merely restate
// H5.1's behaviour instead of depending on it.
//
// THE TRAP THIS FILE EXISTS TO SPRING (C26-shaped). `not_built` and `no_season` BOTH answer 404 on
// /signal-trials/season. /health is the ONLY place they differ, and C54 makes `not_built` reachable
// in production for every failed, refused or aborted probe. A test that asserts merely "an empty
// state rendered" cannot separate them and passes against a screen that collapsed the two. So every
// predicate in the discriminator group asserts BOTH a positive (the state that should render) and a
// negative (the OTHER state's distinguishing copy/affordance is ABSENT), in both directions.
//
// THE SECOND AXIS: null is not zero. The frozen fixture carries `agent-flat` (avg_brier 0.0,
// markout 0 — real FLAT/PERFECT values) alongside `agent-unsettled` (both null). A screen that
// rendered `—` for zero, or `0` for null, publishes a fabricated result on a public leaderboard.
// The fixture VARIES in that dimension, so it can pin it (C66).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { SIGNAL_TRIALS_MARKOUT_LABEL } from '@/lib/signal-trials-api';
import type * as W from '@/lib/wire';
import { SeasonScreen } from './SeasonScreen';
import TrialsPage from '@/app/(app)/trials/page';

// Same idiom as lib/signal-trials-api.test.ts: read the frozen contract fixture from the repo root
// at test time so this screen cannot drift from the backend's wire shape.
const FIX = resolve(__dirname, '../../../../../contracts/fixtures');
const seasonWire = JSON.parse(
  readFileSync(resolve(FIX, 'signal_trials_season.json'), 'utf8'),
) as W.SignalTrialsSeasonWire;

beforeEach(() => { vi.restoreAllMocks(); });
afterEach(() => { vi.unstubAllGlobals(); });

// The backend's refusal envelope is `{"error": code}` (signal_trials_router.py `_error`) —
// deliberately NOT HTTPException's `{"detail": ...}`.
function errorResponse(status: number, code: string) {
  return new Response(JSON.stringify({ error: code }), { status });
}
function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

// ONE frozen season-404. Both discriminator renders below are served this BYTE-IDENTICAL response,
// which is what makes "only /health differs" a fact of the test rather than a claim about it.
const SEASON_404 = () => errorResponse(404, 'no_season_published');

type Route = () => Response;
function stubRoutes(routes: { season: Route; health?: Route }) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/signal-trials/season')) return routes.season();
    if (url.includes('/signal-trials/health')) {
      if (!routes.health) throw new Error(`test served no /health route, but the screen fetched ${url}`);
      return routes.health();
    }
    throw new Error(`unexpected fetch in SeasonScreen test: ${url}`);
  }) as unknown as typeof fetch);
}
function fetchedPaths(): string[] {
  return (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]));
}

// A season 200 whose body is the frozen fixture, optionally overridden.
function stubSeason200(over: Partial<W.SignalTrialsSeasonWire> = {}) {
  stubRoutes({ season: () => jsonResponse({ ...seasonWire, ...over }) });
}

// Every row carries qualified=false — what an `exploratory` season looks like on the wire.
const exploratoryRows = seasonWire.rows.map((r) => ({ ...r, qualified: false }));

// Render the screen and wait for its panel to settle out of `loading`. Every non-loading test goes
// through here so no assertion can accidentally run against the skeleton.
async function panel() {
  render(<SeasonScreen />);
  const el = await screen.findByTestId('season-panel');
  await waitFor(() => expect(el).not.toHaveAttribute('data-state', 'loading'));
  return el;
}

// ---------------------------------------------------------------------------
// GROUP A — the `qualified` render (season 200)
// ---------------------------------------------------------------------------
describe('H5.2 qualified season — Brier is the rank key and the metrics are honest', () => {
  it('renders the Brier column FIRST among the metric columns', async () => {
    stubSeason200();
    await panel();
    const headers = screen.getAllByRole('columnheader').map((th) => th.textContent ?? '');
    const brier = headers.findIndex((h) => /avg brier/i.test(h));
    const markout = headers.findIndex((h) => h.includes(SIGNAL_TRIALS_MARKOUT_LABEL));
    const decisions = headers.findIndex((h) => /active decisions/i.test(h));
    expect(brier).toBeGreaterThanOrEqual(0);
    // Acceptance AND discrimination (C52): Brier existing proves nothing about ORDER. Both other
    // metric columns must sit strictly after it, so a screen that moved markout first fails here.
    expect(markout).toBeGreaterThan(brier);
    expect(decisions).toBeGreaterThan(brier);
  });

  it('renders the markout header as the LOWERCASE frozen constant, never a hardcoded uppercase copy', async () => {
    stubSeason200();
    await panel();
    // The one permitted spelling, character-for-character. Casing for display is CSS's job
    // (text-transform), so the DOM text must still be the lowercase constant.
    expect(screen.getByText(SIGNAL_TRIALS_MARKOUT_LABEL)).toBeInTheDocument();
    // Discrimination control: a screen that hardcoded the design's uppercase rendering would put
    // the uppercase string in the DOM and still "show a markout header". This is what catches it.
    expect(document.body.textContent).not.toContain(SIGNAL_TRIALS_MARKOUT_LABEL.toUpperCase());
  });

  it('tags control rows as baseline controls and contestant rows as contestants', async () => {
    stubSeason200();
    await panel();
    const rows = screen.getAllByTestId('season-row');
    const control = rows.find((r) => within(r).queryByText('control-coin-flip'));
    const contestant = rows.find((r) => within(r).queryByText('agent-signal-01'));
    expect(within(control!).getByTestId('season-role')).toHaveTextContent('baseline control');
    // Discrimination: the tag must be row-specific, not blanket copy. A contestant carrying it too
    // would mean the screen labels everything a control.
    expect(within(contestant!).getByTestId('season-role')).toHaveTextContent('contestant');
    expect(within(contestant!).getByTestId('season-role')).not.toHaveTextContent('baseline control');
  });

  it('shows the sample size from the response, and it VARIES with the response', async () => {
    stubSeason200();
    await panel();
    expect(screen.getByTestId('season-sample-size')).toHaveTextContent('61');
    // C66: a fixture invariant under the thing it claims to pin cannot pin it. Re-render with a
    // different sample_size — a hardcoded "61" would survive the first assertion and die here.
    // `cleanup()` unmounts the first instance so the assertion below cannot resolve against it.
    cleanup();
    vi.unstubAllGlobals();
    stubSeason200({ sample_size: 7 });
    await panel();
    expect(screen.getByTestId('season-sample-size')).toHaveTextContent('7');
    expect(screen.getByTestId('season-sample-size')).not.toHaveTextContent('61');
  });

  it('renders NULL metrics as an em dash and ZERO metrics as zero — both directions', async () => {
    stubSeason200();
    await panel();
    const rows = screen.getAllByTestId('season-row');
    const nulled = rows.find((r) => within(r).queryByText('agent-unsettled'))!;
    const flat = rows.find((r) => within(r).queryByText('agent-flat'))!;

    // null ⇒ nothing settled. NEVER 0: a zero Brier is a PERFECT score.
    expect(within(nulled).getByTestId('season-brier')).toHaveTextContent('—');
    expect(within(nulled).getByTestId('season-markout')).toHaveTextContent('—');

    // 0 ⇒ a REAL result. A screen that rendered `—` here would erase a perfect Brier and a flat
    // markout. This is the opposite-direction control for the assertions above.
    expect(within(flat).getByTestId('season-brier')).toHaveTextContent('0.000');
    expect(within(flat).getByTestId('season-brier')).not.toHaveTextContent('—');
    expect(within(flat).getByTestId('season-markout')).toHaveTextContent('0.0');
    expect(within(flat).getByTestId('season-markout')).not.toHaveTextContent('—');
  });

  it('renders rows in the ORDER THE BACKEND SERVED and never re-sorts them', async () => {
    stubSeason200();
    await panel();
    const served = seasonWire.rows.map((r) => r.agent_id);
    const rendered = screen.getAllByTestId('season-agent').map((el) => el.textContent);
    expect(rendered).toEqual(served);

    // Discrimination (C66 — and the fixture DOES vary here): served order is
    // [0.184, 0.0, null, 0.25], so a client-side Brier sort would produce a DIFFERENT order.
    // Without this assertion the test above would pass against a screen that re-sorted a fixture
    // that happened to already be sorted.
    const brierSorted = [...seasonWire.rows]
      .sort((a, b) => (a.avg_brier ?? Infinity) - (b.avg_brier ?? Infinity))
      .map((r) => r.agent_id);
    expect(brierSorted).not.toEqual(served);
    expect(rendered).not.toEqual(brierSorted);
  });

  it('numbers rows by served position (ORD = index + 1), a display ordinal and not a computed rank', async () => {
    stubSeason200();
    await panel();
    expect(screen.getAllByTestId('season-ord').map((el) => el.textContent)).toEqual(['1', '2', '3', '4']);
  });

  it('renders exactly one qualified badge — the row the backend marked qualified', async () => {
    stubSeason200();
    await panel();
    const badges = screen.getAllByTestId('season-qualified-badge');
    expect(badges).toHaveLength(1);
    const row = screen.getAllByTestId('season-row').find((r) => within(r).queryByTestId('season-qualified-badge'))!;
    expect(within(row).getByTestId('season-agent')).toHaveTextContent('agent-signal-01');
  });

  it('renders the served combo without assuming its keys, and the values VARY with the response', async () => {
    stubSeason200();
    await panel();
    const combo = screen.getByTestId('season-combo');
    expect(combo).toHaveTextContent('501');
    expect(combo).toHaveTextContent('1m');
    // `combo` is `dict[str, Any]` on the wire — its keys are NOT frozen fields, so the screen must
    // render what was served rather than a design-assumed shape. Different combo ⇒ different text.
    cleanup();
    vi.unstubAllGlobals();
    stubSeason200({ combo: { chain_index: '196', bar: '5m' } });
    await panel();
    expect(screen.getByTestId('season-combo')).toHaveTextContent('chain_index 196');
    expect(screen.getByTestId('season-combo')).toHaveTextContent('bar 5m');
    // Discriminate on the COMBO ENTRY, not on the bare digits: `season_id` is
    // "season-2026w30-501-1m" and legitimately still contains "501", so a bare-substring negative
    // would fail against correct output.
    expect(screen.getByTestId('season-combo')).not.toHaveTextContent('chain_index 501');
  });
});

// ---------------------------------------------------------------------------
// GROUP B — the `exploratory` render
// ---------------------------------------------------------------------------
describe('H5.2 exploratory season — provisional standings, zero skill claim', () => {
  it('renders the exploratory banner VERBATIM', async () => {
    stubSeason200({ season_status: 'exploratory', rows: exploratoryRows });
    const p = await panel();
    expect(p).toHaveAttribute('data-state', 'exploratory');
    expect(screen.getByText('EXPLORATORY — NO QUALIFIED-SKILL CLAIM')).toBeInTheDocument();
    expect(screen.getByTestId('season-exploratory-banner')).toHaveTextContent(
      'No (chain × bar) combination reached the 40-trial gate.',
    );
  });

  it('renders ZERO qualified badges', async () => {
    stubSeason200({ season_status: 'exploratory', rows: exploratoryRows });
    await panel();
    // Acceptance control lives in Group A ("exactly one qualified badge" on a qualified season),
    // so this zero is a measured absence and not an assertion about an element that never renders.
    expect(screen.queryAllByTestId('season-qualified-badge')).toHaveLength(0);
  });

  it('renders ZERO qualified badges even if a row arrives flagged qualified under an exploratory season', async () => {
    // The honesty guard. An exploratory season states that NO combination cleared the 40-trial
    // gate; a `qualified: true` row served underneath it would be a skill claim the season itself
    // denies. The screen must refuse to publish it rather than relaying the contradiction.
    stubSeason200({ season_status: 'exploratory', rows: seasonWire.rows });
    await panel();
    expect(seasonWire.rows.some((r) => r.qualified)).toBe(true); // the fixture really does vary here
    expect(screen.queryAllByTestId('season-qualified-badge')).toHaveLength(0);
  });

  it('still renders the provisional standings — exploratory is not an empty state', async () => {
    stubSeason200({ season_status: 'exploratory', rows: exploratoryRows });
    await panel();
    expect(screen.getAllByTestId('season-row')).toHaveLength(seasonWire.rows.length);
  });
});

// ---------------------------------------------------------------------------
// GROUP C — THE DISCRIMINATOR. Both 404s, both directions.
// ---------------------------------------------------------------------------
describe('H5.2 the two 404 branches are distinguishable — via the REAL discriminator', () => {
  it('season 404 + health no_season → the dedicated no_season empty state, and NOT the not_built one', async () => {
    stubRoutes({ season: SEASON_404, health: () => jsonResponse({ ok: true, season_state: 'no_season' }) });
    const p = await panel();
    expect(p).toHaveAttribute('data-state', 'no_season');
    expect(screen.getByText('No season published')).toBeInTheDocument();
    expect(screen.getByText('Preflight found insufficient eligible signals for a truthful season.')).toBeInTheDocument();
    // The probe-counts affordance is the `no_season` state's distinguishing action.
    expect(screen.getByTestId('season-probe-counts')).toBeInTheDocument();
    // OPPOSITE DIRECTION: the other 404 branch's copy must be absent. Without this, a screen that
    // rendered BOTH states' copy — or collapsed them into one shared panel — would pass.
    expect(screen.queryByText('Season not built yet')).not.toBeInTheDocument();
    expect(screen.queryByText('The data preflight has not published a season state.')).not.toBeInTheDocument();
  });

  it('season 404 + health not_built → the not_built state, with NO probe-counts affordance', async () => {
    stubRoutes({ season: SEASON_404, health: () => jsonResponse({ ok: true, season_state: 'not_built' }) });
    const p = await panel();
    expect(p).toHaveAttribute('data-state', 'not_built');
    expect(screen.getByText('Season not built yet')).toBeInTheDocument();
    expect(screen.getByText('The data preflight has not published a season state.')).toBeInTheDocument();
    // OPPOSITE DIRECTION: `no_season`'s title, body AND its distinguishing affordance are absent.
    // C54 makes this state reachable in production for every failed/refused/aborted probe, so a
    // screen that showed "Preflight found insufficient eligible signals" here would be asserting a
    // preflight verdict that was never reached.
    expect(screen.queryByText('No season published')).not.toBeInTheDocument();
    expect(screen.queryByText('Preflight found insufficient eligible signals for a truthful season.')).not.toBeInTheDocument();
    expect(screen.queryByTestId('season-probe-counts')).not.toBeInTheDocument();
  });

  it('separates the two on the strength of /health ALONE — the season response is byte-identical', async () => {
    // This is the C26 guard stated as one assertion. Both renders are served the SAME 404 body from
    // the SAME factory; the only difference in the entire test is `season_state`. If the screen
    // collapsed the two branches in EITHER direction, these two states would be equal.
    stubRoutes({ season: SEASON_404, health: () => jsonResponse({ ok: true, season_state: 'no_season' }) });
    const first = (await panel()).getAttribute('data-state');
    const firstBody = document.body.textContent ?? '';

    cleanup();
    vi.unstubAllGlobals();
    stubRoutes({ season: SEASON_404, health: () => jsonResponse({ ok: true, season_state: 'not_built' }) });
    const secondPanel = await panel();
    const second = secondPanel.getAttribute('data-state');

    expect(first).toBe('no_season');
    expect(second).toBe('not_built');
    expect(second).not.toBe(first);
    // And the difference is visible to a HUMAN, not only in an attribute a screenshot cannot show.
    expect(firstBody).toContain('No season published');
    expect(secondPanel.textContent).toContain('Season not built yet');
    expect(secondPanel.textContent).not.toContain('No season published');
  });

  it('actually consults /health after the season 404 rather than guessing a state', async () => {
    stubRoutes({ season: SEASON_404, health: () => jsonResponse({ ok: true, season_state: 'no_season' }) });
    await panel();
    const paths = fetchedPaths();
    expect(paths.some((p) => p.includes('/signal-trials/season'))).toBe(true);
    expect(paths.some((p) => p.includes('/signal-trials/health'))).toBe(true);
  });

  it.each(['no_season', 'not_built'] as const)(
    'fabricates NO rows in the %s empty state',
    async (state) => {
      stubRoutes({ season: SEASON_404, health: () => jsonResponse({ ok: true, season_state: state }) });
      await panel();
      expect(screen.queryAllByTestId('season-row')).toHaveLength(0);
      // Not merely "no row elements" — no agent identity, metric or sample size from any fixture
      // may appear. An empty state populated with reassuring values is the failure this forbids.
      for (const r of seasonWire.rows) {
        expect(document.body.textContent).not.toContain(r.agent_id);
      }
      expect(screen.queryByTestId('season-sample-size')).not.toBeInTheDocument();
      expect(screen.queryByTestId('season-qualified-badge')).not.toBeInTheDocument();
    },
  );
});

// ---------------------------------------------------------------------------
// GROUP D — transport failure and loading. Neither may become a domain claim.
// ---------------------------------------------------------------------------
describe('H5.2 a transport failure is never rendered as a season verdict', () => {
  it('season 500 → unavailable, and explicitly NOT "no season published"', async () => {
    stubRoutes({ season: () => errorResponse(500, 'boom') });
    const p = await panel();
    expect(p).toHaveAttribute('data-state', 'unavailable');
    expect(screen.getByText('Benchmark data unavailable')).toBeInTheDocument();
    expect(screen.getByTestId('season-retry')).toBeInTheDocument();
    // The whole point: a 502 must never be reported as "the preflight ran and declined to build a
    // season". Both empty-state titles must be absent.
    expect(screen.queryByText('No season published')).not.toBeInTheDocument();
    expect(screen.queryByText('Season not built yet')).not.toBeInTheDocument();
    expect(screen.queryAllByTestId('season-row')).toHaveLength(0);
  });

  it('season 404 + an unreadable /health → unavailable, not a guessed 404 branch', async () => {
    stubRoutes({ season: SEASON_404, health: () => errorResponse(500, 'boom') });
    const p = await panel();
    expect(p).toHaveAttribute('data-state', 'unavailable');
    expect(screen.queryByText('No season published')).not.toBeInTheDocument();
    expect(screen.queryByText('Season not built yet')).not.toBeInTheDocument();
  });

  it('renders a loading state carrying no placeholder results', () => {
    // Never resolves — the screen is pinned in its loading state for the whole assertion.
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})) as unknown as typeof fetch);
    render(<SeasonScreen />);
    expect(screen.getByTestId('season-panel')).toHaveAttribute('data-state', 'loading');
    expect(screen.getByText('LOADING · NO PLACEHOLDER RESULTS SHOWN')).toBeInTheDocument();
    expect(screen.queryAllByTestId('season-row')).toHaveLength(0);
    for (const r of seasonWire.rows) {
      expect(document.body.textContent).not.toContain(r.agent_id);
    }
    expect(screen.queryByTestId('season-sample-size')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// GROUP E — the language and honesty guards. This is a PUBLIC artifact.
// ---------------------------------------------------------------------------
describe('H5.2 language and honesty boundary', () => {
  const BANNED = [
    /\bPnL\b/i, /\bprofits?\b/i, /\brealized\b/i, /\bROI\b/i, /\balpha\b/i, /\bedges?\b/i,
    /\bmade money\b/i, /\bfills?\b/i, /\bfilled\b/i, /\bpositions?\b/i, /\bproven\b/i,
    /\bbuy\b/i, /\bsell\b/i, /\brecommend/i,
  ];

  it.each([
    ['qualified', () => stubSeason200()],
    ['exploratory', () => stubSeason200({ season_status: 'exploratory', rows: exploratoryRows })],
  ])('uses no banned trading language in the %s render', async (_name, stub) => {
    stub();
    await panel();
    const text = document.body.textContent ?? '';
    for (const re of BANNED) expect(text).not.toMatch(re);
  });

  it('claims reproducibility over recorded evidence, never tamper-proofing or immutability', async () => {
    stubSeason200();
    await panel();
    const text = document.body.textContent ?? '';
    for (const re of [/tamper.?proof/i, /immutab/i, /cannot be (altered|changed)/i, /unforgeable/i]) {
      expect(text).not.toMatch(re);
    }
  });

  it('never renders a single aggregate "verified" badge over the season', async () => {
    stubSeason200();
    await panel();
    // `verified` exists as a LEGACY Badge variant in lib/badges.ts and must not be reused here: one
    // aggregate badge over a whole season asserts far more than any per-receipt check establishes.
    expect(document.querySelector('[data-variant="verified"]')).toBeNull();
    expect(document.body.textContent).not.toMatch(/\bverified\b/i);
  });

  it('states that the client never re-sorts and never computes rank locally', async () => {
    stubSeason200();
    await panel();
    expect(screen.getByTestId('season-footnotes')).toHaveTextContent(
      'The client never re-sorts and never computes rank locally.',
    );
  });

  it('states that the controls are part of the benchmark, not failed contestants', async () => {
    stubSeason200();
    await panel();
    expect(screen.getByTestId('season-footnotes')).toHaveTextContent(
      'The four controls are part of the benchmark, not failed contestants',
    );
  });
});

// ---------------------------------------------------------------------------
// GROUP F — the /trials route is PUBLIC.
// ---------------------------------------------------------------------------
describe('H5.2 the /trials page', () => {
  it('renders the season screen with NO auth gate', async () => {
    stubSeason200();
    // This is a STRUCTURAL proof, not a source grep. `AuthGate` calls `usePrivy()`, which throws
    // outside a `PrivyProvider` — and this test deliberately renders no provider. So a page that
    // wrapped its content in AuthGate could not reach a rendered season panel at all, and an
    // unauthenticated judge could not reach the public benchmark either.
    render(<TrialsPage />);
    expect(await screen.findByTestId('season-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('auth-login-gate')).not.toBeInTheDocument();
  });

  it('drives the page through the real season endpoints', async () => {
    stubSeason200();
    render(<TrialsPage />);
    await screen.findByTestId('season-panel');
    expect(fetchedPaths().some((p) => p.includes('/signal-trials/season'))).toBe(true);
  });
});
