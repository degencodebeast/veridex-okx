import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, resolve, basename } from 'node:path';

const ROOT = resolve(__dirname, '..');
const SCAN_DIRS = ['app', 'components', 'hooks', 'lib', 'styles'];
const HEX = /#[0-9a-fA-F]{3,8}\b/;
const EXEMPT = new Set(['tokens.css']);

function walk(dir: string): string[] {
  let out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out = out.concat(walk(full));
    else if (/\.(css|ts|tsx)$/.test(entry)) out.push(full);
  }
  return out;
}

describe('token conformance (PAT-001: no raw hex outside the token source)', () => {
  it('declares the required Direction A color tokens', () => {
    const tokens = readFileSync(join(ROOT, 'styles/tokens.css'), 'utf8');
    for (const t of ['--bg:', '--panel:', '--border:', '--text-1:', '--accent:', '--warning:', '--positive:', '--negative:']) {
      expect(tokens, `missing ${t}`).toContain(t);
    }
    expect(tokens).toContain('#070A0E');
    expect(tokens).toContain('#3B82F6');
  });

  it('holds the text tokens at the values the handoff assigns to their roles', () => {
    // PROOFARENA-OKX-DELTA-HANDOFF §7:302-304 assigns text values by ROLE: `--text-2` is
    // sentence-case body and HONESTY-CRITICAL DISCLAIMERS, while `--text-3`/`--text-4` are reserved
    // for 8–9px uppercase micro-labels because they do not clear AA at body sizes. The
    // product-boundary disclaimer shipped at `--text-3` (3.42:1 on `--bg`) and is now at `--text-2`
    // (7.49:1); see app/(proofarena)/route-scope.test.ts for the per-selector assertion.
    //
    // This pin belongs HERE and not with that assertion: this file is the one PAT-001 exempts, so it
    // is the only place a test may name a hex. Without it, `--text-2` could be retinted to a failing
    // value and the selector assertion would still pass.
    const tokens = readFileSync(join(ROOT, 'styles/tokens.css'), 'utf8');
    // The dark theme lives in `:root`; the light theme is `[data-direction='b']`, so anchor on the
    // block rather than searching the file, or this reads whichever value appears last.
    const dark = tokens.slice(tokens.indexOf(':root'), tokens.indexOf('[data-direction'));
    expect(dark).toContain('--text-2: #93A0B4');
    expect(dark).toContain('--text-3: #5D6675');
  });

  it('contains no raw hex color outside tokens.css', () => {
    const offenders: string[] = [];
    for (const dir of SCAN_DIRS) {
      const abs = join(ROOT, dir);
      if (!existsSync(abs)) continue; // scan dir not created yet (built in a later plan)
      for (const file of walk(abs)) {
        if (EXEMPT.has(basename(file))) continue;
        const text = readFileSync(file, 'utf8');
        text.split('\n').forEach((line, i) => {
          if (HEX.test(line)) offenders.push(`${file}:${i + 1}  ${line.trim()}`);
        });
      }
    }
    expect(offenders, `raw hex found:\n${offenders.join('\n')}`).toEqual([]);
  });
});
