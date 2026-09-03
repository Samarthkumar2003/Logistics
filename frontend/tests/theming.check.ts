/**
 * Checks for the light/dark theme wiring: which pages follow the toggle, and
 * whether the palette they read from is actually complete.
 *
 * There is no test runner in this frontend, so this is a plain script:
 *
 *     node --experimental-strip-types frontend/tests/theming.check.ts
 *
 * The bug this exists for: /send-request and /request/[id] stayed dark with light
 * mode on. Two independent causes, and a fix for either alone leaves the pages
 * broken, so both are pinned below.
 *
 *   1. Every token lived in dashboard/dashboard.css, scoped to
 *      `body:has(.dash-app)` and imported only by dashboard/page.tsx — so the
 *      other routes never loaded the palette at all.
 *   2. Both pages wrote dark hex literals (#0a0a14, #0d1117, …) into their inline
 *      style objects, so they would have ignored the palette even with it loaded.
 *
 * These are source-text assertions rather than DOM ones because the failure is
 * structural: a literal hex or a missing token cannot respond to a data-theme
 * change no matter what renders it.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel: string) => readFileSync(join(SRC, rel), 'utf8');

const themeCss = read('app/theme.css');
const layout = read('app/layout.tsx');
const dashboardCss = read('app/dashboard/dashboard.css');

/** Pages that opt into the palette and must therefore carry `themed`. */
const THEMED_PAGES = [
  'app/dashboard/page.tsx',
  'app/send-request/page.tsx',
  'app/request/[id]/page.tsx',
];

/**
 * The body of a CSS rule, by exact selector text. Brace-matched rather than
 * regexed to the first `}` so a nested block could not truncate it.
 */
function ruleBody(css: string, selector: string): string {
  const at = css.indexOf(selector + ' {');
  assert.notEqual(at, -1, `theme.css has no rule for \`${selector}\``);
  const open = css.indexOf('{', at);
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === '{') depth++;
    else if (css[i] === '}' && --depth === 0) return css.slice(open + 1, i);
  }
  throw new Error(`unbalanced braces after \`${selector}\``);
}

const declaredIn = (body: string) =>
  new Set([...body.matchAll(/^\s*(--[a-z0-9-]+)\s*:/gim)].map(m => m[1]));

const referencedIn = (text: string) =>
  new Set([...text.matchAll(/var\(\s*(--[a-z0-9-]+)/g)].map(m => m[1]));

// 1. The palette is loaded app-wide, not per-route. This is cause (1): tokens
// reachable only from the route that imports them is how two pages went dark.
{
  assert.match(layout, /import\s+["']\.\/theme\.css["']/,
    'app/layout.tsx must import ./theme.css so every route has the palette');
  assert.doesNotMatch(dashboardCss, /^\s*--[a-z0-9-]+\s*:/im,
    'dashboard.css must not redeclare tokens — theme.css is the only source, or the two drift');
}

// 2. Light defines exactly what dark defines.
//
// The sharp edge in a two-block palette: an undefined var() in the light block
// does not fall back to a light value, it inherits the dark one. One missing
// token is a black card on a white page, and nothing errors.
{
  const dark = declaredIn(ruleBody(themeCss, 'body:has(.themed)'));
  const light = declaredIn(ruleBody(themeCss, ':root[data-theme="light"] body:has(.themed)'));

  assert.ok(dark.size > 50, `expected a full palette, found ${dark.size} dark tokens`);

  const missing = [...dark].filter(t => !light.has(t));
  const extra = [...light].filter(t => !dark.has(t));
  assert.deepEqual(missing, [], `tokens missing from the light block (they would stay dark): ${missing}`);
  assert.deepEqual(extra, [], `tokens only in the light block (undefined in dark): ${extra}`);
}

// 3. Every token anyone reads is a token someone declares. A typo'd var() name
// renders as nothing at all — no border, or transparent text.
{
  const declared = declaredIn(ruleBody(themeCss, 'body:has(.themed)'));
  // globals.css owns these three on :root for the office view, and the dashboard
  // stylesheet's own rules resolve them from the themed block instead.
  const fromGlobals = new Set(['--accent', '--accent-bright', '--floor', '--floor-plank',
    '--wall-face', '--wall-top', '--sidebar-bg', '--panel-bg', '--card-bg']);

  for (const rel of [...THEMED_PAGES, 'app/dashboard/dashboard.css']) {
    const source = rel.endsWith('.css') ? dashboardCss : read(rel);
    for (const token of referencedIn(source)) {
      if (fromGlobals.has(token)) continue;
      assert.ok(declared.has(token), `${rel} reads ${token}, which theme.css never declares`);
    }
  }
}

// 4. THE REGRESSION. Cause (2): a hex literal in an inline style object cannot
// follow a theme. Reverting either page to literals fails here.
{
  for (const rel of ['app/send-request/page.tsx', 'app/request/[id]/page.tsx']) {
    const source = read(rel);
    const hex = [...source.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map(m => m[0]);
    assert.deepEqual(hex, [], `${rel} hardcodes ${hex.length} colour literal(s) — use var(--token): ${hex}`);
    assert.doesNotMatch(source, /color:\s*['"]white['"]/,
      `${rel} should say var(--on-accent) rather than a bare white`);
  }
}

// 5. Reading tokens is not enough — the page must also bring them into scope.
// `themed` is what the selector keys on, so a page full of correct var() calls
// and no class renders unstyled.
{
  for (const rel of THEMED_PAGES) {
    assert.match(read(rel), /className="[^"]*\bthemed\b/,
      `${rel} reads theme tokens but never carries the \`themed\` class that scopes them`);
  }
}

// 6. The office view stays out of it, deliberately. globals.css declares
// --border/--blue/--green on :root for that page; pulling the dashboard palette
// over it would repaint its borders and accents.
{
  assert.doesNotMatch(read('app/page.tsx'), /className="[^"]*\bthemed\b/,
    'the office view must not be themed — globals.css owns its :root tokens');
}

console.log('theming.check.ts — all assertions passed');
