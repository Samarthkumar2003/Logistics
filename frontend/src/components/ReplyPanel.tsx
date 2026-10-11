"use client";

import { useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api';
import { withReply, type ReplySummary, type SentReply } from '@/lib/sentReplies';
import ReplyComposer, { type ReplySeed } from './ReplyComposer';
import ReplyHistory from './ReplyHistory';

/**
 * The composer plus the history of replies for one email.
 *
 * The host page owns only whether the composer is open (its Reply button sits
 * wherever that page keeps its actions) and, if it shows a chip, the summary.
 * Everything else is here so the dashboard card, the request page and the Office
 * View behave the same.
 *
 * History is fetched on mount unless the host already knows there is none
 * (`summary === null` from the inbox row), which saves a request per expanded card
 * on the many emails nobody has answered.
 */
export default function ReplyPanel({ emailId, sender, subject, open, onOpenChange, summary, onSummary }: {
  emailId: string;
  sender: string;
  subject: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** null: the inbox says nothing was sent. undefined: not known, so ask. */
  summary?: ReplySummary | null;
  onSummary?: (s: ReplySummary) => void;
}) {
  const [replies, setReplies] = useState<SentReply[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [seed, setSeed] = useState<ReplySeed | null>(null);
  // Bumped per Edit & resend, so the composer remounts with a fresh key.
  const [composeId, setComposeId] = useState(0);
  const skipFetch = summary === null;

  useEffect(() => {
    if (skipFetch) return;
    let cancelled = false;
    setLoading(true);
    setError('');
    (async () => {
      try {
        const res = await apiFetch(`/emails/${encodeURIComponent(emailId)}/sent-replies`);
        if (!res.ok) {
          let detail = `Server error ${res.status}`;
          try { detail = (await res.json()).detail || detail; } catch { /* non-JSON */ }
          throw new Error(detail);
        }
        const d = await res.json();
        if (!cancelled) setReplies(d.replies ?? []);
      } catch (err: unknown) {
        if (!cancelled) setError(`Could not load sent replies: ${err instanceof Error ? err.message : 'unknown error'}`);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // Once per email. A later poll flipping summary from null to a value is this
    // panel's own send coming back, which is already in `replies`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [emailId]);

  function sent(row: SentReply) {
    // The server may hand back an earlier attempt (same idempotency key). That one
    // is already listed and already counted: replace it, don't count it again.
    const known = replies.some(r => r.id === row.id);
    setReplies(prev => [row, ...prev.filter(r => r.id !== row.id)]);
    if (!known) onSummary?.(withReply(summary, row));
    setSeed(null);
    onOpenChange(false);
  }

  function changed(row: SentReply, previous: SentReply) {
    setReplies(prev => prev.map(r => (r.id === row.id ? row : r)));
    onSummary?.(withReply(summary, row, previous));
  }

  function editResend(row: SentReply) {
    setSeed({ body: row.body, cc: row.cc ?? [] });
    setComposeId(n => n + 1);
    onOpenChange(true);
  }

  return (
    <>
      {open && (
        <ReplyComposer
          key={composeId}
          emailId={emailId}
          sender={sender}
          subject={subject}
          seed={seed}
          onSent={sent}
          onCancel={() => { setSeed(null); onOpenChange(false); }}
        />
      )}
      <ReplyHistory
        replies={replies}
        loading={loading}
        error={error}
        onChanged={changed}
        onEditResend={editResend}
      />
    </>
  );
}
