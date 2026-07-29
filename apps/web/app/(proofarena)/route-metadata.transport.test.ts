// The TRANSPORT contract for `/trials/[trialId]`'s title: what a real client actually receives.
//
// WHY THIS FILE EXISTS SEPARATELY FROM `route-metadata.test.ts`, and why it pays for a `next build`.
//
// `route-metadata.test.ts` imports `generateMetadata` and calls it with a JavaScript string. That
// makes it a test of the TEMPLATE: it proves the composed title carries whatever value the function
// is handed. It cannot prove anything about what value Next HANDS IT, because the test constructs
// that value itself — so it stayed green through a defect where the served title read
// `release%2B1` while the test fed it the already-decoded `release+1` and passed.
//
// The route segment is the input this layer does not control, so the only test that can pin it is
// one that does not construct it: build the app, serve it, request the URL, read the `<title>` out
// of the returned document. Everything below crosses `next build` + `next start` + HTTP for that
// reason, and asserts on rendered bytes rather than on a return value.
//
// MEASURED, on this tree, before the fix (built + served, `curl` against `next start`):
//   /trials/trial-0k9f2c     200  <title>trial-0k9f2c — Fair-Play trial · ProofArena</title>
//   /trials/trial.release-1  200  <title>trial.release-1 — Fair-Play trial · ProofArena</title>
//   /trials/release+1        200  <title>release%2B1 — Fair-Play trial · ProofArena</title>      WRONG
//   /trials/%C3%A9preuve-1   200  <title>%C3%A9preuve-1 — Fair-Play trial · ProofArena</title>   WRONG
// The segment arrives PERCENT-ENCODED. The two WRONG rows are what this file was written to fail on.
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createServer } from 'node:net';
import { resolve } from 'node:path';
import { chromium, type Browser, type Page, type Route } from '@playwright/test';

const WEB = resolve(__dirname, '../..');
const NEXT = resolve(WEB, 'node_modules/.bin/next');

// The copy this route must serve, transcribed from PROOFARENA-OKX-DELTA-HANDOFF §4 :113-116
// (`{trial_id} — Fair-Play trial · ProofArena`). Kept as a template for the same reason
// `route-metadata.test.ts` keeps one: a hardcoded finished title cannot tell a substitution from a
// constant. U+2014 EM DASH and U+00B7 MIDDLE DOT are pinned by codepoint over in that file.
const trialTitle = (trialId: string) => `${trialId} — Fair-Play trial · ProofArena`;

/** A port the OS has just confirmed is free, so a parallel worker cannot collide with us. */
const freePort = () =>
  new Promise<number>((ok, err) => {
    const s = createServer();
    s.once('error', err);
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address() as { port: number };
      s.close(() => ok(port));
    });
  });

let server: ChildProcess | undefined;
let browser: Browser | undefined;
let origin = '';

/**
 * The document Next serves for `path`, requested VERBATIM.
 *
 * `path` is spliced into the request without re-encoding on purpose: these cases are about how the
 * percent-encoding on the wire survives the trip to `generateMetadata`, so normalising it here
 * would destroy the thing under test.
 */
const get = async (path: string) => {
  const res = await fetch(`${origin}${path}`, { redirect: 'manual' });
  return { status: res.status, html: await res.text() };
};

/** The text inside the document's first `<title>`, or `undefined` if it serves none. */
const titleOf = (html: string) => /<title[^>]*>([^<]*)<\/title>/.exec(html)?.[1];

beforeAll(async () => {
  const built = spawnSync(NEXT, ['build'], { cwd: WEB, encoding: 'utf8' });
  expect(
    built.status,
    `next build failed — the transport cases below cannot mean anything without it:\n${built.stdout}\n${built.stderr}`,
  ).toBe(0);

  const port = await freePort();
  origin = `http://127.0.0.1:${port}`;
  server = spawn(NEXT, ['start', '-p', String(port), '-H', '127.0.0.1'], { cwd: WEB, stdio: 'ignore' });

  const deadline = Date.now() + 60_000;
  for (;;) {
    try {
      await fetch(`${origin}/trials`);
      return;
    } catch {
      if (Date.now() > deadline) throw new Error(`next start never answered on ${origin}`);
      await new Promise((r) => setTimeout(r, 250));
    }
  }
}, 600_000);

afterAll(async () => {
  await browser?.close();
  server?.kill('SIGTERM');
});

const getBrowser = async (): Promise<Browser> => {
  if (!browser) {
    const bundled = chromium.executablePath();
    const systemChrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
    browser = await chromium.launch({
      headless: true,
      executablePath: existsSync(bundled) ? bundled : systemChrome,
    });
  }
  return browser;
};

/**
 * Load the real production document, let React hydrate, and record the two public API paths the
 * client card actually requests. Only those API calls are intercepted; the document and bundles
 * still cross `next start` over HTTP.
 */
const hydratedTrialRequests = async (documentPath: string): Promise<string[]> => {
  const page = await (await getBrowser()).newPage();
  const observed: string[] = [];
  await page.route('**/signal-trials/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    observed.push(path);
    if (path.endsWith('/receipts')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    } else {
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: '{"error":"trial_not_found"}',
      });
    }
  });

  await page.goto(`${origin}${documentPath}`, { waitUntil: 'domcontentloaded' });
  const deadline = Date.now() + 10_000;
  while (observed.length < 2 && Date.now() < deadline) {
    await page.waitForTimeout(50);
  }
  await page.close();
  return observed.sort();
};

const navigationEvidence = {
  t0_ms: 1700000000000,
  chain_index: '501',
  token_address: '0x1111111111111111111111111111111111111111',
  symbol: 'AAA',
  name: 'Asset A',
  market_cap_usd: 1234567.5,
  holders: 842,
  top10_holder_percent: 31.5,
  trigger_price: 0.0041732,
  wallet_type: 'smart money',
  trigger_wallet_count: 3,
  trigger_wallet_address: '0x2222222222222222222222222222222222222222',
  amount_usd: 25000,
};

const navigationSeason = {
  season_id: 'season-link-transport',
  season_status: 'qualified',
  combo: { chain_index: '501', bar: '1m' },
  sample_size: 61,
  rows: [
    {
      agent_id: 'agent-signal-01',
      qualified: true,
      avg_brier: 0.184,
      capped_avg_markout_bps: 12,
      active_decisions: 44,
      active_coverage: 0.91,
      unscored: 2,
      is_control: false,
    },
  ],
};

const navigationTrial = (trialId: string) => ({
  trial_id: trialId,
  trial_mode: 'live',
  t0_ms: navigationEvidence.t0_ms,
  commit_deadline_ms: navigationEvidence.t0_ms + 300000,
  evidence: navigationEvidence,
  evidence_hash: '3f1c8a5e0b47d29c6ea1b3f85d0c47921e6ab8d35c0f4172e9b6d84a3c15f072',
  outcome: null,
});

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

async function openSeasonWithTrial(trialId: string, width: number) {
  const page = await (await getBrowser()).newPage({ viewport: { width, height: 1000 } });
  const observed: string[] = [];
  const trial = navigationTrial(trialId);
  await page.route('**/signal-trials/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    observed.push(path);
    if (path === '/signal-trials/season') return json(route, navigationSeason);
    if (path === '/signal-trials/open-trial') {
      const { outcome: _omitted, ...open } = trial;
      return json(route, open);
    }
    if (path.endsWith('/receipts')) return json(route, []);
    if (path.startsWith('/signal-trials/trials/')) return json(route, trial);
    return json(route, { error: 'not_found' }, 404);
  });
  await page.goto(`${origin}/trials`, { waitUntil: 'networkidle' });
  await page.locator('[data-testid="season-featured-trial"]').waitFor();
  return { page, observed };
}

async function waitForTitle(page: Page, title: string) {
  const deadline = Date.now() + 10_000;
  while (await page.title() !== title) {
    if (Date.now() > deadline) throw new Error(`title never became ${JSON.stringify(title)}`);
    await page.waitForTimeout(50);
  }
}

async function assertAllSeasonLinksAndClick(
  trialId: string,
  width: number,
  clicked: 'header' | 'row' | 'card',
) {
  const encoded = encodeURIComponent(trialId);
  const expectedHref = `/trials/${encoded}`;
  const { page, observed } = await openSeasonWithTrial(trialId, width);

  const header = page.locator('[data-testid="season-featured-trial"]');
  const row = page.locator('[data-testid="season-row-link"]').first();
  const card = page.locator('[data-testid="season-cards"] a').first();
  expect(await header.getAttribute('href'), 'header featured action').toBe(expectedHref);
  expect(await row.getAttribute('href'), 'desktop standings row').toBe(expectedHref);
  expect(await card.getAttribute('href'), 'narrow standings card').toBe(expectedHref);

  observed.length = 0;
  const target = clicked === 'header' ? header : clicked === 'row' ? row : card;
  await target.click();
  await waitForTitle(page, `${trialId} — Fair-Play trial · ProofArena`);

  const expectedRequests = [
    `/signal-trials/trials/${encoded}`,
    `/signal-trials/trials/${encoded}/receipts`,
  ].sort();
  const deadline = Date.now() + 10_000;
  while (observed.filter((path) => path.startsWith('/signal-trials/trials/')).length < 2) {
    if (Date.now() > deadline) break;
    await page.waitForTimeout(50);
  }
  expect(
    observed.filter((path) => path.startsWith('/signal-trials/trials/')).sort(),
    `clicked ${clicked}; observed ${JSON.stringify(observed)}`,
  ).toEqual(expectedRequests);
  await page.close();
}

type TextReflowMetrics = {
  text: string;
  fragments: Array<{ left: number; right: number; top: number; bottom: number }>;
  visible: { left: number; right: number; top: number; bottom: number };
  documentWidth: number;
  viewportWidth: number;
};

/**
 * Measure the painted text fragments rather than trusting the document width.
 *
 * A shell-level `overflow-x: clip` can keep `documentElement.scrollWidth` equal to the viewport
 * while hundreds of pixels of an unbroken identity sit outside its own box. Range client rects
 * expose those painted fragments. `visibleBox: 'parent'` is used for the compact inline metadata
 * whose visible allocation is the flex row containing it; the block Match Card heading owns its
 * own visible box.
 */
async function textReflowMetrics(
  locator: ReturnType<Page['locator']>,
  visibleBox: 'self' | 'parent',
): Promise<TextReflowMetrics> {
  return locator.evaluate((element, box) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const fragments = Array.from(range.getClientRects(), (rect) => ({
      left: rect.left,
      right: rect.right,
      top: rect.top,
      bottom: rect.bottom,
    }));
    const visibleElement = box === 'parent' ? element.parentElement : element;
    if (visibleElement === null) throw new Error('identity surface has no visible box');
    const visible = visibleElement.getBoundingClientRect();
    return {
      text: element.textContent ?? '',
      fragments,
      visible: {
        left: visible.left,
        right: visible.right,
        top: visible.top,
        bottom: visible.bottom,
      },
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    };
  }, visibleBox);
}

function expectIdentityReflows(
  metrics: TextReflowMetrics,
  expectedText: string,
  label: string,
) {
  expect(metrics.text, `${label}: the full identity text changed`).toBe(expectedText);
  expect(metrics.fragments.length, `${label}: the long identity did not wrap`).toBeGreaterThan(1);
  for (const [index, fragment] of metrics.fragments.entries()) {
    expect(
      fragment.left,
      `${label}: fragment ${index} starts outside its visible box`,
    ).toBeGreaterThanOrEqual(metrics.visible.left - 0.5);
    expect(
      fragment.right,
      `${label}: fragment ${index} ends outside its visible box`,
    ).toBeLessThanOrEqual(metrics.visible.right + 0.5);
  }
  // Secondary shell-level control only. The fragment assertions above are the load-bearing proof.
  expect(metrics.documentWidth, `${label}: document still overflows`).toBe(metrics.viewportWidth);
}

const MOBILE_REFLOW_ID = `trial%20?#é+.${'z'.repeat(70)}`;

describe('/trials/[trialId] names the id the URL names, through the built transport', () => {
  // THE CONTROL. An id with no percent-encoding in it, which was already correct before the fix.
  // Its job is to prove the harness can PASS: if the two decoding cases below were the only tests
  // here, a broken build, a dead server or a bad `<title>` regex would fail them for reasons that
  // have nothing to do with decoding, and the RED would be worthless as evidence.
  it('serves the exact handoff title for an ordinary id', async () => {
    const { status, html } = await get('/trials/trial-0k9f2c');
    expect(status).toBe(200);
    expect(titleOf(html)).toBe(trialTitle('trial-0k9f2c'));
  });

  it('serves the exact handoff title for an id containing a dot', async () => {
    const { status, html } = await get('/trials/trial.release-1');
    expect(status).toBe(200);
    expect(titleOf(html)).toBe(trialTitle('trial.release-1'));
  });

  // `+` is a legal path character that Next hands to the segment as `%2B`. The publisher can mint
  // this id today — `open_live_trial` (`veridex/signal_trials/live.py:150`) validates nothing and
  // the repository's one check (`live.py:323`) refuses only an empty id, a path separator, `.` and
  // `..` — so serving `release%2B1` names a trial that does not exist.
  it('names an id containing `+`, not its percent-escape', async () => {
    const { status, html } = await get('/trials/release+1');
    expect(status).toBe(200);
    expect(titleOf(html)).toBe(trialTitle('release+1'));
  });

  // The non-ASCII case, requested the only way a client can request it: as UTF-8 percent-escapes.
  // Passing 'épreuve-1' as a JavaScript literal — which is what the previous attempt's test did —
  // skips the wire entirely and is green whether or not this defect exists.
  it('names a non-ASCII id, not its UTF-8 percent-escapes', async () => {
    const { status, html } = await get('/trials/%C3%A9preuve-1');
    expect(status).toBe(200);
    expect(titleOf(html)).toBe(trialTitle('épreuve-1'));
  });

  // A malformed percent sequence is the input that makes an UNGUARDED `decodeURIComponent` throw
  // URIError and 500 a PUBLIC route. MEASURED here: Next's own request handling rejects `/trials/%`
  // with 400 before any route code runs, so the guard's fallback is not reachable from the wire on
  // this version — the point this case pins is the one that matters to a visitor either way, that
  // the malformed URL never becomes a server error. The fallback branch itself is exercised
  // directly in `route-metadata.test.ts`, where it can be reached.
  it('does not turn a malformed percent sequence into a server error', async () => {
    const { status } = await get('/trials/%');
    expect(status).toBeLessThan(500);
  });

  // Decoding must not become injection. `%3Cscript%3E` decodes to `<script>`, and the title is
  // returned to Next as a STRING for Next to escape — so the served bytes must carry the entities,
  // never the raw tag.
  it('lets Next escape a decoded id rather than emitting markup', async () => {
    const { status, html } = await get('/trials/%3Cscript%3Ealert(1)%3C%2Fscript%3E');
    expect(status).toBe(200);
    // Read as ENTITIES, which is what `titleOf` returns for an escaped title: the decoded id is
    // present as text and absent as markup.
    expect(titleOf(html)).toBe(trialTitle('&lt;script&gt;alert(1)&lt;/script&gt;'));
    expect(html).not.toContain('<title><script>');
  });
});

describe('/trials/[trialId] requests the same decoded id after hydration', () => {
  const expectedPair = (encodedId: string) => [
    `/signal-trials/trials/${encodedId}`,
    `/signal-trials/trials/${encodedId}/receipts`,
  ].sort();

  it('keeps the ordinary ASCII control on one encoding boundary', async () => {
    expect(await hydratedTrialRequests('/trials/trial-0k9f2c'))
      .toEqual(expectedPair('trial-0k9f2c'));
  });

  it('decodes `+` once before both client request paths encode it once', async () => {
    expect(await hydratedTrialRequests('/trials/release+1'))
      .toEqual(expectedPair('release%2B1'));
  });

  it('decodes UTF-8 once before both client request paths encode it once', async () => {
    expect(await hydratedTrialRequests('/trials/%C3%A9preuve-1'))
      .toEqual(expectedPair('%C3%A9preuve-1'));
  });

  it('decodes an encoded control once before both client request paths encode it once', async () => {
    expect(await hydratedTrialRequests('/trials/control%09tab'))
      .toEqual(expectedPair('control%09tab'));
  });

  it('decodes markup as text while preserving one encoded request boundary', async () => {
    expect(await hydratedTrialRequests('/trials/%3Cscript%3Ealert(1)%3C%2Fscript%3E'))
      .toEqual(expectedPair('%3Cscript%3Ealert(1)%3C%2Fscript%3E'));
  });
});

describe('/trials preserves the backend trial id through every Match Card link', () => {
  it('passes the ordinary ASCII control through the visible header action', async () => {
    await assertAllSeasonLinksAndClick('trial-0k9f2c', 1440, 'header');
  });

  it('encodes a literal percent before the visible desktop-row click', async () => {
    await assertAllSeasonLinksAndClick('trial%20x', 1440, 'row');
  });

  it('encodes a literal query delimiter before the visible mobile-card click', async () => {
    await assertAllSeasonLinksAndClick('trial?x', 390, 'card');
  });

  it('encodes a literal fragment delimiter before the visible header click', async () => {
    await assertAllSeasonLinksAndClick('trial#x', 390, 'header');
  });

  it('preserves plus, Unicode, dot, and long publisher-permitted ids', async () => {
    for (const id of ['release+1', 'épreuve-1', 'trial.release-1', `trial-${'z'.repeat(80)}`]) {
      await assertAllSeasonLinksAndClick(id, 1440, 'header');
    }
  });
});

describe.each([390, 392])(
  '/trials keeps publisher-controlled identity visible at %dpx',
  (width) => {
    it('reflows the compact featured-trial identity inside its rail', async () => {
      const encoded = encodeURIComponent(MOBILE_REFLOW_ID);
      const { page } = await openSeasonWithTrial(MOBILE_REFLOW_ID, width);
      const expectedHref = `/trials/${encoded}`;
      expect(
        await page.locator('[data-testid="season-featured-trial"]').getAttribute('href'),
        'route encoding control',
      ).toBe(expectedHref);

      const rail = page.locator('[data-testid="season-shared-evidence-rail"]');
      const identity = rail.locator('span').filter({
        hasText: `FEATURED TRIAL · ${MOBILE_REFLOW_ID}`,
      });
      expect(await identity.count(), 'compact identity locator').toBe(1);
      expectIdentityReflows(
        await textReflowMetrics(identity, 'parent'),
        `FEATURED TRIAL · ${MOBILE_REFLOW_ID}`,
        `compact featured-trial identity at ${width}px`,
      );
      await page.close();
    });

    it('reflows the Match Card heading without changing title or API identity', async () => {
      const encoded = encodeURIComponent(MOBILE_REFLOW_ID);
      const { page, observed } = await openSeasonWithTrial(MOBILE_REFLOW_ID, width);
      observed.length = 0;
      await page.locator('[data-testid="season-featured-trial"]').click();
      await waitForTitle(page, trialTitle(MOBILE_REFLOW_ID));
      expect(await page.title(), 'served title identity control').toBe(trialTitle(MOBILE_REFLOW_ID));

      const expectedRequests = [
        `/signal-trials/trials/${encoded}`,
        `/signal-trials/trials/${encoded}/receipts`,
      ].sort();
      const deadline = Date.now() + 10_000;
      while (observed.filter((path) => path.startsWith('/signal-trials/trials/')).length < 2) {
        if (Date.now() > deadline) break;
        await page.waitForTimeout(50);
      }
      expect(
        observed.filter((path) => path.startsWith('/signal-trials/trials/')).sort(),
        'API identity controls',
      ).toEqual(expectedRequests);

      const heading = page.getByRole('heading', { level: 1, name: MOBILE_REFLOW_ID });
      expect(await heading.count(), 'Match Card heading locator').toBe(1);
      expectIdentityReflows(
        await textReflowMetrics(heading, 'self'),
        MOBILE_REFLOW_ID,
        `Match Card heading at ${width}px`,
      );
      await page.close();
    });
  },
);
