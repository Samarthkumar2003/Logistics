/**
 * Checks for inbox paging: when to fetch, where the next page starts, and how a
 * re-read first page merges.
 *
 * There is no test runner in this frontend, so this is a plain script:
 *
 *     node --experimental-strip-types frontend/tests/inboxPaging.check.ts
 *
 * (Node >= 22. The repo's default node is 18, so use an explicit newer binary.)
 *
 * Customer Requests used to be a client-side filter over fetched inbox rows, so
 * the tab showed whichever requests happened to be in the last twenty emails and
 * reported "3 shown · searched 260 of 27599 emails". The filter now runs in the
 * database and the tab pages it fifteen at a time. What that moved into the rules
 * below is the cursor: the server pages by rows *scanned*, which is more than the
 * rows returned, so a client adding the page size re-serves rows already shown.
 */

import assert from 'node:assert/strict';
import {
  appendPage, mergeFirstPage, needsClick, nextCursor, remaining, serverSaysMore,
  shouldAutoLoad,
} from '../src/app/dashboard/inboxPaging.ts';

const state = (over: Partial<Parameters<typeof shouldAutoLoad>[0]> = {}) => ({
  loaded: 15, total: 1054, more: true, loading: false, autoPages: 0, cap: 12,
  error: '', ...over,
});

// 1. Rows remain, so reaching the end fetches with no click.
{
  assert.equal(shouldAutoLoad(state()), true);
  assert.equal(needsClick(state()), false, 'scrolling asks; the reader should not have to');
}

// 2. Nothing left to fetch. Paging stops; the footer says end of list.
{
  assert.equal(shouldAutoLoad(state({ more: false })), false);
  assert.equal(needsClick(state({ more: false })), false);
}

// 3. Before the first response nothing is known, so `more` is false. Paging from
// there would fire a request per scroll event against a list nobody has read.
{
  assert.equal(shouldAutoLoad(state({ loaded: 0, total: 0, more: false })), false);
}

// 4. THE SCALE TRAP: rows on screen can exceed nothing useful about the cursor,
// and a filtered list's total is not comparable to how far it has scanned. Only
// the server's answer decides whether to keep going.
{
  assert.equal(shouldAutoLoad(state({ loaded: 1054, total: 1054, more: true })), true,
    'the server still says more; loaded === total must not stop it');
  assert.equal(shouldAutoLoad(state({ loaded: 2, total: 1054, more: false })), false,
    'a short page is not a promise of another one');
}

// 5. One request at a time. The observer fires again the moment a fetch finishes
// while the end of the list is still on screen, and it re-fires on every scroll
// event in between — without this the same cursor is fetched repeatedly.
{
  assert.equal(shouldAutoLoad(state({ loading: true })), false);
  assert.equal(needsClick(state({ loading: true })), false, 'no button mid-flight');
}

// 6. THE BOUND: after `cap` scroll-driven pages it stops and asks.
{
  assert.equal(shouldAutoLoad(state({ autoPages: 11 })), true, 'one page left under the cap');
  assert.equal(shouldAutoLoad(state({ autoPages: 12 })), false);
  assert.equal(needsClick(state({ autoPages: 12 })), true);
  assert.equal(shouldAutoLoad(state({ autoPages: 40 })), false, 'never resumes on its own');
}

// 7. A failure stops the automatic pull and hands the reader a retry. Retrying on
// scroll against a failing endpoint is a request per frame, with no way to stop it.
{
  assert.equal(shouldAutoLoad(state({ error: 'Server error 500' })), false);
  assert.equal(needsClick(state({ error: 'Server error 500' })), true);
}

// 8. What the control reports is rows not on screen yet, never a negative — the
// total shrinks when mail is deleted between pages.
{
  assert.equal(remaining(state({ loaded: 15, total: 1054 })), 1039);
  assert.equal(remaining(state({ loaded: 1200, total: 1054 })), 0);
}

// 9. THE CURSOR: follow the server, not the page size. Filling a page of fifteen
// took 24 rows of scanning on page two of Customer Requests, because nine had a
// stale stored label. `offset + limit` would have re-served nine rows.
{
  assert.equal(nextCursor(15, 39, 15), 39);
  assert.equal(nextCursor(0, undefined, 15), 15, 'no cursor in the response: fall back');
  assert.equal(nextCursor(15, 15, 15), 16, 'a stalled cursor is forced forward');
  assert.equal(nextCursor(30, 12, 15), 31, 'and never goes backwards');
}

// 10. "More to come" from a cursor that stood still is a contradiction. Asking
// again would return the same rows forever.
{
  assert.equal(serverSaysMore(true, 15, 39), true);
  assert.equal(serverSaysMore(false, 15, 39), false);
  assert.equal(serverSaysMore(true, 15, 15), false, 'believe the cursor, not the flag');
  assert.equal(serverSaysMore(true, 15, undefined), true, 'no cursor: take the flag');
}

// 11. Appending a page drops rows already on screen. Two observer firings at one
// cursor, or a page that overlaps after new mail arrives, must not duplicate rows.
{
  const prev = [{ id: 'a' }, { id: 'b' }];
  assert.deepEqual(appendPage(prev, [{ id: 'b' }, { id: 'c' }]),
    [{ id: 'a' }, { id: 'b' }, { id: 'c' }]);
}

// 12. THE POLL: re-reading the first page every 60s must not collapse a list
// scrolled several pages deep. New mail goes on top; known rows keep their place
// and take updated fields.
{
  const onScreen = [{ id: 'a', label: 'general' }, { id: 'b', label: 'general' },
                    { id: 'c', label: 'general' }];
  const firstPage = [{ id: 'new', label: 'customer_requirement' },
                     { id: 'a', label: 'customer_requirement' }];

  const merged = mergeFirstPage(onScreen, firstPage);
  assert.deepEqual(merged.map(e => e.id), ['new', 'a', 'b', 'c'],
    'new mail on top, nothing dropped from further down');
  assert.equal(merged[1].label, 'customer_requirement', 'a re-read row takes the fresh label');
  assert.deepEqual(mergeFirstPage([], firstPage).map(e => e.id), ['new', 'a'],
    'the very first response is just the page');
}

console.log('inboxPaging.check.ts: all checks passed');
