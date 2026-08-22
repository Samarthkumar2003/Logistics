/**
 * Checks for scroll-driven inbox paging.
 *
 * There is no test runner in this frontend, so this is a plain script:
 *
 *     node --experimental-strip-types frontend/tests/inboxPaging.check.ts
 *
 * (Node >= 22. The repo's default node is 18, so use an explicit newer binary.)
 *
 * What it pins is the reported behaviour and the bound that keeps it safe.
 * Customer Requests is a label filter over the inbox, so it used to show only the
 * requests inside the first twenty rows and offered nothing to load the rest —
 * the tab read as "these are all your requests". Reaching the end of the list now
 * fetches, without a click; the cap is what stops a filtered tab with no matches
 * from walking the whole inbox on one flick of the wheel.
 */

import assert from 'node:assert/strict';
import {
  hasMore, needsClick, remaining, shouldAutoLoad,
} from '../src/app/dashboard/inboxPaging.ts';

const state = (over: Partial<Parameters<typeof shouldAutoLoad>[0]> = {}) => ({
  offset: 20, total: 100, loading: false, autoPages: 0, cap: 12, error: '', ...over,
});

// 1. THE REPORTED CASE: rows remain, so reaching the end fetches with no click.
{
  assert.equal(shouldAutoLoad(state()), true);
  assert.equal(needsClick(state()), false, 'scrolling asks; the reader should not have to');
}

// 2. Nothing left to fetch. Paging stops; the footer says end of inbox.
{
  assert.equal(hasMore(state({ offset: 100, total: 100 })), false);
  assert.equal(shouldAutoLoad(state({ offset: 100, total: 100 })), false);
  assert.equal(needsClick(state({ offset: 100, total: 100 })), false);
}

// 3. Before the first response, total is 0 — unknown, not empty. Paging from
// there would fire a request per scroll event against a total nobody has yet.
{
  assert.equal(hasMore(state({ offset: 0, total: 0 })), false);
  assert.equal(shouldAutoLoad(state({ offset: 0, total: 0 })), false);
}

// 4. One request at a time. The observer fires again the moment a fetch finishes
// while the end of the list is still on screen, and it re-fires on every scroll
// event in between — without this the same offset is fetched repeatedly.
{
  assert.equal(shouldAutoLoad(state({ loading: true })), false);
  assert.equal(needsClick(state({ loading: true })), false, 'no button mid-flight');
}

// 5. THE BOUND: after `cap` scroll-driven pages it stops and asks. A filtered tab
// whose matches are all further down would otherwise page the entire inbox.
{
  assert.equal(shouldAutoLoad(state({ autoPages: 11 })), true, 'one page left under the cap');
  assert.equal(shouldAutoLoad(state({ autoPages: 12 })), false);
  assert.equal(needsClick(state({ autoPages: 12 })), true);
  assert.equal(shouldAutoLoad(state({ autoPages: 40 })), false, 'never resumes on its own');
}

// 6. A failure stops the automatic pull and hands the reader a retry. Retrying on
// scroll against a failing endpoint is a request per frame, with no way to stop it.
{
  assert.equal(shouldAutoLoad(state({ error: 'Server error 500' })), false);
  assert.equal(needsClick(state({ error: 'Server error 500' })), true);
}

// 7. What the control reports is rows not yet requested, never a negative — the
// total shrinks when mail is deleted between pages.
{
  assert.equal(remaining(state({ offset: 20, total: 100 })), 80);
  assert.equal(remaining(state({ offset: 120, total: 100 })), 0);
}

console.log('inboxPaging.check.ts: all checks passed');
