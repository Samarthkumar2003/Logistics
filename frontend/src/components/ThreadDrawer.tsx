"use client";

import { useEffect, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { apiFetch } from '@/lib/api';
import {
  displayName, fmtMailDate, fmtSize, preview, splitQuoted,
  type MailMessage, type MailThread,
} from '@/lib/mailbox';

/**
 * The whole conversation an email belongs to, both directions, in a side panel.
 *
 * Read live from Gmail (GET /emails/{id}/thread), so it includes what the desk
 * sent from Gmail itself and what the app sent, neither of which is in the store.
 * Oldest at the top like a mail client; the newest message and the one the panel
 * was opened from start expanded, the rest collapsed to one line.
 *
 * Inline styles and theme variables, because it opens on pages that do not load
 * dashboard.css. Portalled into <body> (where theme.css puts the variables) so
 * no ancestor's stacking context or transform can trap a fixed panel. Clicks are
 * still stopped at the root: it is opened from clickable cards, and React
 * bubbles events through portals along the component tree.
 */

const TAG: Record<MailMessage['direction'], { text: string; color: string }> = {
  sent: { text: '↗ Sent', color: 'var(--blue-text)' },
  received: { text: '↘ Received', color: 'var(--green-soft)' },
};

const small: CSSProperties = { fontSize: 12, color: 'var(--muted)' };

function Message({ m, open, onToggle }: { m: MailMessage; open: boolean; onToggle: () => void }) {
  const [showQuoted, setShowQuoted] = useState(false);
  const { main, quoted } = splitQuoted(m.body);
  const tag = TAG[m.direction] ?? TAG.received;
  return (
    <div style={{
      borderRadius: 8, border: '1px solid var(--border)',
      background: m.direction === 'sent' ? 'var(--blue-tint)' : 'var(--surface)',
    }}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        style={{
          width: '100%', textAlign: 'left', padding: '10px 12px', border: 'none',
          background: 'transparent', color: 'var(--text)', cursor: 'pointer',
          display: 'flex', alignItems: 'baseline', gap: 8, fontFamily: 'inherit',
        }}
      >
        <span style={{ fontSize: 11, fontWeight: 700, color: tag.color, whiteSpace: 'nowrap' }}>{tag.text}</span>
        <span style={{ fontSize: 13, fontWeight: 600, whiteSpace: 'nowrap' }}>{displayName(m.from) || '(unknown sender)'}</span>
        {!open && (
          <span style={{ ...small, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {preview(m.body) || '(no text)'}
          </span>
        )}
        <span style={{ ...small, marginLeft: 'auto', whiteSpace: 'nowrap' }}>
          {m.attachments.length > 0 ? '📎 ' : ''}{fmtMailDate(m.date)}
        </span>
      </button>
      {open && (
        <div style={{ padding: '0 12px 12px' }}>
          <div style={{ ...small, marginBottom: 8, lineHeight: 1.5 }}>
            <div><b>From</b> {m.from}</div>
            {m.to && <div><b>To</b> {m.to}</div>}
            {m.cc && <div><b>Cc</b> {m.cc}</div>}
          </div>
          <pre style={{
            margin: 0, fontSize: 13, color: 'var(--text-soft)', whiteSpace: 'pre-wrap',
            lineHeight: 1.6, fontFamily: 'inherit', wordBreak: 'break-word',
          }}>
            {main || '(no text in this message; it may be attachments only)'}
          </pre>
          {quoted && (
            <>
              <button
                type="button"
                onClick={() => setShowQuoted(v => !v)}
                style={{
                  marginTop: 6, padding: '1px 8px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
                  border: '1px solid var(--border)', background: 'transparent', color: 'var(--muted)',
                }}
              >{showQuoted ? 'Hide quoted text' : '··· Show quoted text'}</button>
              {showQuoted && (
                <pre style={{
                  margin: '6px 0 0', fontSize: 12, color: 'var(--faint)', whiteSpace: 'pre-wrap',
                  lineHeight: 1.5, fontFamily: 'inherit', wordBreak: 'break-word',
                }}>{quoted}</pre>
              )}
            </>
          )}
          {m.body_clipped && (
            <div style={{ ...small, marginTop: 6 }}>Long message, shortened here. Open it in Gmail for the rest.</div>
          )}
          {m.attachments.length > 0 && (
            <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {m.attachments.map((a, i) => (
                <span key={`${i}-${a.filename}`} style={{
                  fontSize: 12, padding: '3px 8px', borderRadius: 6,
                  background: 'var(--sunken-soft)', color: 'var(--muted-soft)',
                }}>📎 {a.filename}{a.size_bytes ? ` · ${fmtSize(a.size_bytes)}` : ''}</span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function ThreadDrawer({ emailId, onClose }: { emailId: string; onClose: () => void }) {
  const [thread, setThread] = useState<MailThread | null>(null);
  const [errorMsg, setErrorMsg] = useState('');
  const [open, setOpen] = useState<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await apiFetch(`/emails/${encodeURIComponent(emailId)}/thread`);
        if (!res.ok) {
          let detail = `Server error ${res.status}`;
          try { detail = (await res.json()).detail || detail; } catch { /* non-JSON */ }
          throw new Error(detail);
        }
        const t: MailThread = await res.json();
        if (cancelled) return;
        const last = t.messages[t.messages.length - 1]?.id;
        setOpen(new Set([emailId, last].filter(Boolean) as string[]));
        setThread(t);
      } catch (err: unknown) {
        if (!cancelled) setErrorMsg(err instanceof Error ? err.message : 'Could not load the thread');
      }
    })();
    return () => { cancelled = true; };
  }, [emailId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const toggle = (id: string) => setOpen(prev => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const msgs = thread?.messages ?? [];
  const subject = msgs.find(m => m.subject)?.subject || '(no subject)';
  const allOpen = msgs.length > 0 && msgs.every(m => open.has(m.id));

  return createPortal(
    <div
      onClick={e => { e.stopPropagation(); onClose(); }}
      style={{ position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.45)' }}
    >
      <aside
        role="dialog"
        aria-label="Email thread"
        onClick={e => e.stopPropagation()}
        style={{
          position: 'absolute', top: 0, right: 0, bottom: 0, width: 'min(760px, 100vw)',
          background: 'var(--bg)', color: 'var(--text)', borderLeft: '1px solid var(--border)',
          boxShadow: 'var(--shadow)', display: 'flex', flexDirection: 'column',
        }}
      >
        <header style={{
          padding: '14px 16px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', gap: 10,
        }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 15, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              🧵 {subject}
            </div>
            {thread && (
              <div style={small}>
                {msgs.length} message{msgs.length === 1 ? '' : 's'}
                {' · '}{msgs.filter(m => m.direction === 'sent').length} sent
                {' · '}{msgs.filter(m => m.direction === 'received').length} received
              </div>
            )}
          </div>
          {msgs.length > 1 && (
            <button
              type="button"
              onClick={() => setOpen(allOpen ? new Set() : new Set(msgs.map(m => m.id)))}
              style={{
                fontSize: 12, padding: '4px 10px', borderRadius: 6, cursor: 'pointer',
                border: '1px solid var(--border)', background: 'transparent', color: 'var(--muted)',
              }}
            >{allOpen ? 'Collapse all' : 'Expand all'}</button>
          )}
          <button
            type="button"
            aria-label="Close thread"
            onClick={onClose}
            style={{ border: 'none', background: 'transparent', color: 'var(--muted)', fontSize: 18, cursor: 'pointer' }}
          >✕</button>
        </header>
        <div style={{ flex: 1, overflowY: 'auto', padding: 16, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {!thread && !errorMsg && <div style={small}>Loading thread from Gmail…</div>}
          {errorMsg && <div role="alert" style={{ fontSize: 13, color: 'var(--red)' }}>⚠️ {errorMsg}</div>}
          {thread && thread.omitted > 0 && (
            <div style={small}>{thread.omitted} older message{thread.omitted === 1 ? '' : 's'} not shown. Open the thread in Gmail to see them.</div>
          )}
          {msgs.map(m => (
            <Message key={m.id} m={m} open={open.has(m.id)} onToggle={() => toggle(m.id)} />
          ))}
        </div>
      </aside>
    </div>,
    document.body,
  );
}

/** The button that opens it. Same look wherever it sits. */
export function ThreadButton({ onClick, compact }: { onClick: () => void; compact?: boolean }) {
  return (
    <button
      type="button"
      onClick={e => { e.stopPropagation(); onClick(); }}
      style={{
        padding: compact ? '4px 10px' : '7px 16px', borderRadius: 8, cursor: 'pointer',
        border: '1px solid var(--border)', background: 'transparent', color: 'var(--muted-soft)',
        fontSize: compact ? 12 : 13, fontWeight: 600, whiteSpace: 'nowrap',
      }}
    >🧵 View thread</button>
  );
}
