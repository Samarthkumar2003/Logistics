'use client';

/**
 * One paging list of inbox mail, optionally of a single label.
 *
 * Each tab gets its own feed. Customer Requests used to be a client-side filter
 * over whatever inbox rows had been fetched, which is why it read
 * "3 shown · searched 260 of 27599 emails": ~4% of stored mail is a customer
 * request, so twenty inbox rows held about one. The filter now runs in the
 * database and this hook pages it, so the tab shows fifteen requests and a total
 * of how many exist rather than how many arrived.
 *
 * Paging follows the server's `next_offset`, never `offset + limit`. Under a
 * filter the server reads past rows whose stored label no longer matches what
 * gets displayed, so the cursor counts rows scanned; adding the page size instead
 * would re-serve rows already on screen.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  appendPage, mergeFirstPage, nextCursor, serverSaysMore, type PagingState,
} from './inboxPaging';
import { apiFetch } from '@/lib/api';

export interface InboxEmail {
  id: string;
  sender: string;
  subject: string;
  body: string;
  label?: string;
  label_confidence?: number;
  received_at?: string;
  reason?: string;
  cited_reference?: string | null;
}

export interface InboxFeed {
  emails: InboxEmail[];
  /** How many the whole filtered set holds, not how many are on screen. */
  total: number;
  /** First page still in flight, with nothing to show yet. */
  loading: boolean;
  error: string;
  paging: PagingState;
  /** Pull the next page. `auto` marks a load the scroll asked for. */
  loadMore: (auto?: boolean) => void;
  /** Stable identity for the scroll sentinel's observer. */
  loadMoreOnScroll: () => void;
  /** After the cap or a failure: the reader asking, which clears the cap. */
  loadMoreByClick: () => void;
  /** Re-read the first page and merge, for the poll and after a correction. */
  refresh: () => Promise<void>;
}

interface PageResponse {
  emails?: InboxEmail[];
  total?: number;
  next_offset?: number;
  has_more?: boolean;
  detail?: string;
}

export function useInboxFeed(
  label: string, pageSize: number, cap: number,
): InboxFeed {
  const [emails, setEmails] = useState<InboxEmail[]>([]);
  const [total, setTotal] = useState(0);
  const [more, setMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [autoPages, setAutoPages] = useState(0);
  const [error, setError] = useState('');

  // The cursor the next page starts at. A ref because the scroll observer can
  // fire twice before React re-renders, and two fetches at one cursor is
  // duplicated work for rows dedupe then throws away.
  const cursor = useRef(0);
  const inFlight = useRef(false);

  // API-relative, no host: apiFetch prefixes API_BASE.
  const url = useCallback(
    (offset: number) =>
      `/fetch-inbox?limit=${pageSize}&offset=${offset}`
      + (label ? `&label=${label}` : ''),
    [label, pageSize],
  );

  const loadMore = useCallback(async (auto = false) => {
    if (inFlight.current || !more) return;
    const offset = cursor.current;
    inFlight.current = true;
    setLoadingMore(true);
    setError('');
    try {
      const res = await apiFetch(url(offset));
      const d: PageResponse = await res.json();
      if (!res.ok) throw new Error(d?.detail ?? `Server error ${res.status}`);
      setEmails(prev => appendPage(prev, d.emails ?? []));
      // The total moves as ingest commits new mail; a stale one misreports how
      // much is left to read.
      if (typeof d.total === 'number') setTotal(d.total);
      cursor.current = nextCursor(offset, d.next_offset, pageSize);
      setMore(serverSaysMore(d.has_more, offset, d.next_offset));
      if (auto) setAutoPages(n => n + 1);
    } catch (err: unknown) {
      // Left on screen with a retry: scrolling must not quietly stop paging and
      // leave a part-read list looking complete.
      setError(err instanceof Error ? err.message : 'Failed to load more emails');
    } finally {
      inFlight.current = false;
      setLoadingMore(false);
    }
  }, [more, pageSize, url]);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setError('');
    try {
      const res = await apiFetch(url(0));
      const d: PageResponse = await res.json();
      if (!res.ok) throw new Error(d?.detail ?? `Server error ${res.status}`);
      const incoming = d.emails ?? [];
      setEmails(prev => mergeFirstPage(prev, incoming));
      if (typeof d.total === 'number') setTotal(d.total);
      // Only the first page may set the cursor and `more`. Past that the reader
      // is deeper into the list than this response knows about, and adopting its
      // cursor would re-serve page two. An empty first page leaves the cursor
      // where it is, so a tab that is empty now still starts paging properly once
      // mail arrives for it.
      if (cursor.current === 0 && incoming.length > 0) {
        cursor.current = nextCursor(0, d.next_offset, pageSize);
        setMore(serverSaysMore(d.has_more, 0, d.next_offset));
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Cannot reach backend');
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }, [pageSize, url]);

  useEffect(() => { refresh(); }, [refresh]);

  const loadMoreOnScroll = useCallback(() => { loadMore(true); }, [loadMore]);
  const loadMoreByClick = useCallback(() => {
    setAutoPages(0);
    loadMore();
  }, [loadMore]);

  return {
    emails, total, loading, error,
    paging: {
      loaded: emails.length, total, more,
      loading: loading || loadingMore, autoPages, cap, error,
    },
    loadMore, loadMoreOnScroll, loadMoreByClick, refresh,
  };
}
