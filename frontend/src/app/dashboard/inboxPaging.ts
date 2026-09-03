/**
 * When scrolling is allowed to pull another page of a list.
 *
 * The lists that page are the inbox and its two label views (Customer Requests,
 * Rate Cards). Those two are now filtered in the database, so a page of fifteen
 * is fifteen customer requests rather than whichever requests happened to be in
 * the last twenty inbox rows. The bound stays: a list that keeps answering
 * "there is more" would otherwise walk the whole 27k-row inbox on one flick of
 * the wheel.
 *
 * `more` comes from the server, and is not derived from `loaded < total`. Under a
 * label filter the server pages by rows *scanned* while the total counts rows
 * *matched*, so those two numbers are not on the same scale — comparing them
 * either stops early or never stops.
 *
 * Kept out of page.tsx so the rules can be checked without a DOM or an
 * IntersectionObserver; see frontend/tests/inboxPaging.check.ts.
 */

export interface PagingState {
  /** Rows on screen. */
  loaded: number;
  /** Rows this list holds in total, as of the last response. */
  total: number;
  /** Whether the server says a further page exists. False until it answers. */
  more: boolean;
  /** A request is in flight. */
  loading: boolean;
  /** Pages pulled by scrolling since the last deliberate click. */
  autoPages: number;
  /** How many pages scrolling may pull before it asks. */
  cap: number;
  /** Last paging failure, empty when the last request succeeded. */
  error: string;
}

/** Whether reaching the end of the list should fetch, with no click. */
export function shouldAutoLoad(s: PagingState): boolean {
  if (!s.more || s.loading) return false;
  // A failure stops the automatic pull. Retrying on scroll against a failing
  // endpoint turns one error into a request per frame, and the reader gets no say.
  if (s.error) return false;
  return s.autoPages < s.cap;
}

/** Whether the reader has to ask for the next page themselves. */
export function needsClick(s: PagingState): boolean {
  return s.more && !s.loading && (!!s.error || s.autoPages >= s.cap);
}

/** Rows not on screen yet — the number the "keep loading" control reports. */
export function remaining(s: PagingState): number {
  return Math.max(0, s.total - s.loaded);
}

/**
 * Where the next page starts.
 *
 * The server's `next_offset` counts rows *scanned*, which under a label filter is
 * more than the rows returned: it reads past mail whose stored label no longer
 * matches what gets displayed. Adding the page size instead would re-serve rows
 * already on screen. A cursor that fails to advance would page the same rows
 * forever, so it is forced forward — the response's own `has_more` is what stops
 * the list, not the cursor.
 */
export function nextCursor(offset: number, reported: number | undefined,
                           pageSize: number): number {
  const next = typeof reported === 'number' ? reported : offset + pageSize;
  return Math.max(next, offset + 1);
}

/** Whether to keep paging, given what the server said. */
export function serverSaysMore(reported: boolean | undefined, offset: number,
                               reportedNext: number | undefined): boolean {
  if (!reported) return false;
  // "More to come" from a cursor that stood still is a contradiction; believe the
  // cursor. Nothing new could arrive from asking again.
  return typeof reportedNext !== 'number' || reportedNext > offset;
}

interface Identified { id: string }

function dedupe<T extends Identified>(rows: T[]): T[] {
  const seen = new Set<string>();
  return rows.filter(r => (seen.has(r.id) ? false : !!seen.add(r.id)));
}

/** A further page, appended. Duplicates are dropped, order is preserved. */
export function appendPage<T extends Identified>(prev: T[], incoming: T[]): T[] {
  return dedupe([...prev, ...incoming]);
}

/**
 * The first page, re-read and merged into what is already on screen.
 *
 * Merged rather than replaced because the poll runs every 60s and must not
 * collapse a list the reader has scrolled several pages into. New mail goes on
 * top, where newest-first ordering puts it; rows already on screen keep their
 * position and take any updated fields.
 */
export function mergeFirstPage<T extends Identified>(prev: T[], incoming: T[]): T[] {
  if (prev.length === 0) return dedupe(incoming);
  const fresh = new Map(incoming.map(r => [r.id, r]));
  const known = new Set(prev.map(r => r.id));
  const updated = prev.map(r => ({ ...r, ...(fresh.get(r.id) ?? {}) }));
  return dedupe([...incoming.filter(r => !known.has(r.id)), ...updated]);
}
