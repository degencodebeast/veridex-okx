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
import { chromium, type Browser } from '@playwright/test';

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

/**
 * Load the real production document, let React hydrate, and record the two public API paths the
 * client card actually requests. Only those API calls are intercepted; the document and bundles
 * still cross `next start` over HTTP.
 */
const hydratedTrialRequests = async (documentPath: string): Promise<string[]> => {
  if (!browser) {
    const bundled = chromium.executablePath();
    const systemChrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
    browser = await chromium.launch({
      headless: true,
      executablePath: existsSync(bundled) ? bundled : systemChrome,
    });
  }

  const page = await browser.newPage();
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
