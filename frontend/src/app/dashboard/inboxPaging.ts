/**
 * When scrolling is allowed to pull another page of the inbox.
 *
 * The lists that page are the inbox and its two label views (Customer Requests,
 * Rate Cards). Those two filter client-side, so the end of the list can sit on
 * screen with nothing new to show — the matches are further down the inbox. That
 * is the case scrolling has to serve, and also the case that would happily walk
 * tens of thousands of rows if nothing bounded it.
 *
 * Kept out of page.tsx so the rules can be checked without a DOM or an
 * IntersectionObserver; see frontend/tests/inboxPaging.check.ts.
 */

export interface PagingState {
  /** Rows already asked for — the offset the next request would use. */
  offset: number;
  /** Rows the inbox holds, as of the last response. */
  total: number;
  /** A request is in flight. */
  loading: boolean;
  /** Pages pulled by scrolling since the last deliberate click. */
  autoPages: number;
  /** How many pages scrolling may pull before it asks. */
  cap: number;
  /** Last paging failure, empty when the last request succeeded. */
  error: string;
}

/** Whether the inbox holds rows this list has not asked for yet. */
export function hasMore(s: PagingState): boolean {
  // total 0 means nothing has answered yet, not an empty inbox: paging from
  // there would fire a request per scroll event against an unknown total.
  return s.total > 0 && s.offset < s.total;
}

/** Whether reaching the end of the list should fetch, with no click. */
export function shouldAutoLoad(s: PagingState): boolean {
  if (!hasMore(s) || s.loading) return false;
  // A failure stops the automatic pull. Retrying on scroll against a failing
  // endpoint turns one error into a request per frame, and the reader gets no say.
  if (s.error) return false;
  return s.autoPages < s.cap;
}

/** Whether the reader has to ask for the next page themselves. */
export function needsClick(s: PagingState): boolean {
  return hasMore(s) && !s.loading && (!!s.error || s.autoPages >= s.cap);
}

/** Rows not yet requested — the number the "keep loading" control reports. */
export function remaining(s: PagingState): number {
  return Math.max(0, s.total - s.offset);
}
