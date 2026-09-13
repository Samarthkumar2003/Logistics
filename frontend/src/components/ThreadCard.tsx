"use client";

import { useState } from 'react';
import { apiFetch } from '@/lib/api';
import type { Reply } from '@/lib/replyThreads';

/**
 * One agent's conversation about one RFQ.
 *
 * A card per Gmail thread, not per message. An agent who replies twice is one
 * agent, and a card each read as two agents having answered — exactly the wrong
 * impression when one RFQ is one agent. The latest message is on top because when
 * an agent writes twice the second message is a correction, and the correction is
 * the one the desk has to act on.
 *
 * Styled inline rather than with classes on purpose: `dashboard.css` is imported by
 * the dashboard route only, so a card that depended on it would render unstyled on
 * the shipment detail page, which is now the only place replies are read.
 */

export interface Attachment {
  id: string;
  file_name: string;
  mime_type: string;
  size_bytes: number | null;
  url: string;
}

function fmt(iso?: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? '' : d.toLocaleString([], {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

/** Same thread as a linked reply, but this message never quoted the RFQ reference
 *  itself. Shown for context; it is not attribution. */
export function ThreadOnlyChip() {
  return (
    <span
      title="Same thread as a linked reply, but this message did not quote the RFQ reference"
      style={{
        marginLeft: 8, padding: '1px 6px', borderRadius: 4, fontSize: 10,
        fontWeight: 700, color: 'var(--amber)', border: '1px solid var(--amber)',
      }}
    >THREAD ONLY</span>
  );
}

function MessageBody({ body }: { body: string }) {
  return (
    <pre style={{
      fontSize: 12, color: 'var(--text-soft)', whiteSpace: 'pre-wrap',
      lineHeight: 1.6, margin: 0, maxHeight: 260, overflowY: 'auto',
      fontFamily: 'inherit',
    }}>
      {body?.slice(0, 4000) || '(no text body — the rates may be in an attachment)'}
      {(body?.length ?? 0) > 4000 ? '\n…' : ''}
    </pre>
  );
}

/**
 * A message's attachments, fetched when asked for.
 *
 * Never on mount. These are signed URLs with a short expiry, so fetching them for
 * every message in every thread up front both spends a request per message and
 * mints links that can expire before anyone clicks one.
 */
function Attachments({ messageId }: { messageId: string }) {
  const [items, setItems] = useState<Attachment[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function load() {
    setLoading(true);
    setError('');
    try {
      const res = await apiFetch(`/email-attachments/${messageId}`);
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      setItems((await res.json()).attachments ?? []);
    } catch {
      // The body is still readable, so this is a note rather than a failure state.
      setError('Could not load attachments');
    } finally {
      setLoading(false);
    }
  }

  if (items === null) {
    return (
      <div style={{ marginTop: 8 }}>
        <button
          onClick={load}
          disabled={loading}
          style={{
            padding: 0, border: 'none', background: 'none', fontSize: 11,
            color: 'var(--blue-soft)', cursor: loading ? 'default' : 'pointer',
          }}
        >
          {loading ? 'Loading attachments…' : '📎 Show attachments'}
        </button>
        {error && <span style={{ marginLeft: 8, fontSize: 11, color: 'var(--red)' }}>{error}</span>}
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div style={{ marginTop: 8, fontSize: 11, color: 'var(--faint)' }}>
        No stored attachments — the mail was flagged as having some, but none were kept.
      </div>
    );
  }

  return (
    <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      {items.map(att => (
        <a
          key={att.id}
          href={att.url || undefined}
          target="_blank"
          rel="noreferrer"
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 6,
            padding: '5px 10px', borderRadius: 6, fontSize: 12,
            border: '1px solid var(--input-border)', background: 'var(--sunken)',
            color: att.url ? 'var(--blue-text)' : 'var(--faint)', textDecoration: 'none',
            pointerEvents: att.url ? undefined : 'none',
          }}
        >
          <span>{att.mime_type?.includes('pdf') ? '📄'
            : att.file_name?.match(/\.(xlsx?|csv)$/i) ? '📊' : '📎'}</span>
          <span>{att.file_name}</span>
          {att.size_bytes ? (
            <span style={{ color: 'var(--faint)' }}>· {Math.round(att.size_bytes / 1024)}KB</span>
          ) : null}
        </a>
      ))}
    </div>
  );
}

export function ThreadCard({ messages }: { messages: Reply[] }) {
  const [showEarlier, setShowEarlier] = useState(false);
  // Newest first from the API, so the head is the agent's latest word.
  const [latest, ...earlier] = messages;
  // groupByThread never yields an empty group, but a card is not worth a white
  // screen if that ever stops being true.
  if (!latest) return null;

  return (
    <div style={{
      background: 'var(--sunken)', border: '1px solid var(--border)',
      borderRadius: 10, padding: 14,
    }}>
      <div style={{
        display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10,
      }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>
          {latest.subject || '(no subject)'}
        </span>
        <span style={{ fontSize: 11, color: 'var(--faint)', whiteSpace: 'nowrap' }}>
          {fmt(latest.received_at)}
        </span>
      </div>

      <div style={{ fontSize: 11, color: 'var(--muted)', margin: '4px 0 10px' }}>
        {latest.sender}
        {earlier.length > 0 && (
          <span style={{ marginLeft: 8, color: 'var(--faint)' }}>
            · latest of {messages.length} messages
          </span>
        )}
        {!latest.linked && <ThreadOnlyChip />}
      </div>

      <MessageBody body={latest.body} />
      {latest.has_attachments && <Attachments messageId={latest.id} />}

      {earlier.length > 0 && (
        <>
          <button
            onClick={() => setShowEarlier(s => !s)}
            style={{
              marginTop: 10, padding: 0, border: 'none', background: 'none',
              color: 'var(--blue-soft)', fontSize: 11, cursor: 'pointer',
            }}
          >
            {showEarlier
              ? '▲ Hide earlier messages'
              : `▼ ${earlier.length} earlier message${earlier.length > 1 ? 's' : ''} in this thread`}
          </button>
          {showEarlier && earlier.map(m => (
            // Indented inside the same card, so the thread reads as one
            // conversation with history rather than as separate replies.
            <div key={m.id} style={{
              marginTop: 10, paddingLeft: 10, borderLeft: '2px solid var(--border)',
            }}>
              <div style={{ fontSize: 11, color: 'var(--faint)', marginBottom: 4 }}>
                {fmt(m.received_at)}
                {!m.linked && <ThreadOnlyChip />}
              </div>
              <MessageBody body={m.body} />
              {m.has_attachments && <Attachments messageId={m.id} />}
            </div>
          ))}
        </>
      )}
    </div>
  );
}
