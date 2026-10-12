"use client";

import { useState } from 'react';
import { apiFetch } from '@/lib/api';
import {
  canRetry, chipFor, fmtWhen, statusFor,
  type ReplySummary, type SentReply, type Tone,
} from '@/lib/sentReplies';

/**
 * What the desk has already sent in answer to one email, newest first.
 *
 * Every attempt is listed, failed ones included, because the point of the record
 * is that nobody has to open Gmail Sent to find out whether the customer heard
 * back. A failed reply offers Retry (same text, same row) when it had no
 * attachments, and Edit & resend always. A reply whose outcome is unknown offers
 * neither: it may well have arrived, and the honest next step is to look.
 *
 * Inline styles and theme variables, for the same reason as ThreadCard.
 */

const TONE_COLOR: Record<Tone, string> = {
  ok: 'var(--green-soft)',
  bad: 'var(--red)',
  pending: 'var(--blue-text)',
  unknown: 'var(--amber)',
};

/** The "Replied" marker on a collapsed card. Nothing at all when nobody replied. */
export function ReplyChip({ summary }: { summary: ReplySummary | null | undefined }) {
  const chip = chipFor(summary);
  if (!chip) return null;
  return (
    <span
      title={summary?.last_at ? `Last reply attempt ${fmtWhen(summary.last_at)}` : undefined}
      style={{
        fontSize: 11, fontWeight: 600, padding: '2px 8px', borderRadius: 999, whiteSpace: 'nowrap',
        color: TONE_COLOR[chip.tone], border: `1px solid ${TONE_COLOR[chip.tone]}`,
      }}
    >{chip.text}</span>
  );
}

const BODY_PREVIEW = 600;

function HistoryRow({ reply, onChanged, onEditResend }: {
  reply: SentReply;
  onChanged: (row: SentReply, previous: SentReply) => void;
  onEditResend: (row: SentReply) => void;
}) {
  const [retrying, setRetrying] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [showAll, setShowAll] = useState(false);
  const status = statusFor(reply);
  const body = reply.body ?? '';
  const clipped = !showAll && body.length > BODY_PREVIEW;

  async function retry() {
    if (retrying) return;
    setRetrying(true);
    setErrorMsg('');
    try {
      const res = await apiFetch(`/sent-replies/${encodeURIComponent(reply.id)}/retry`, { method: 'POST' });
      if (!res.ok) {
        let detail = `Server error ${res.status}`;
        try { detail = (await res.json()).detail || detail; } catch { /* non-JSON */ }
        throw new Error(detail);
      }
      onChanged(await res.json(), reply);
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Retry failed');
    } finally {
      setRetrying(false);
    }
  }

  const button = {
    fontSize: 12, padding: '4px 10px', borderRadius: 6, cursor: 'pointer',
    background: 'transparent', whiteSpace: 'nowrap' as const,
  };

  return (
    <div style={{
      padding: '10px 12px', borderRadius: 8, background: 'var(--sunken-soft)',
      border: `1px solid ${reply.status === 'failed' ? 'var(--red-line)' : 'var(--border)'}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8, fontSize: 12 }}>
        <span style={{ fontWeight: 700, color: TONE_COLOR[status.tone] }}>{status.text}</span>
        <span style={{ color: 'var(--muted)' }}>
          to {reply.to_addr}
          {reply.cc?.length ? `, cc ${reply.cc.join(', ')}` : ''}
        </span>
        <span style={{ marginLeft: 'auto', color: 'var(--faint)' }}>
          {fmtWhen(reply.sent_at ?? reply.created_at)}
          {reply.sent_by_name ? ` · ${reply.sent_by_name}` : ''}
        </span>
      </div>
      {(reply.status === 'failed' || reply.status === 'unknown') && reply.error && (
        <div style={{ marginTop: 6, fontSize: 12, color: 'var(--red)' }}>{reply.error}</div>
      )}
      <pre style={{
        margin: '8px 0 0', fontSize: 12, color: 'var(--text-soft)', whiteSpace: 'pre-wrap',
        lineHeight: 1.5, fontFamily: 'inherit',
      }}>
        {clipped ? `${body.slice(0, BODY_PREVIEW)}…` : body}
      </pre>
      {body.length > BODY_PREVIEW && (
        <button
          type="button"
          onClick={() => setShowAll(v => !v)}
          style={{ ...button, border: 'none', padding: '2px 0', color: 'var(--blue-text)' }}
        >{showAll ? 'Show less' : 'Show all'}</button>
      )}
      {reply.attachment_names?.length > 0 && (
        <div style={{ marginTop: 6, fontSize: 12, color: 'var(--muted)' }}>
          📎 {reply.attachment_names.join(', ')}
        </div>
      )}
      {status.tone === 'unknown' && (
        <div style={{ marginTop: 6, fontSize: 12, color: 'var(--amber)' }}>
          {reply.status === 'unknown'
            ? 'Gmail gave no clear answer, so this may have arrived. Check Gmail Sent before writing again.'
            : 'The app lost track of this one mid-send. Check Gmail Sent before writing again.'}
        </div>
      )}
      {reply.status === 'failed' && (
        <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
          {canRetry(reply) ? (
            <button
              type="button"
              disabled={retrying}
              onClick={retry}
              style={{ ...button, border: '1px solid var(--blue)', color: 'var(--blue-text)' }}
            >{retrying ? '⏳ Retrying…' : '↻ Retry'}</button>
          ) : (
            <span style={{ fontSize: 12, color: 'var(--faint)' }}>
              Attachments aren&apos;t stored, so this one can only be re-sent with them attached again.
            </span>
          )}
          <button
            type="button"
            disabled={retrying}
            onClick={() => onEditResend(reply)}
            style={{ ...button, border: '1px solid var(--border)', color: 'var(--muted-soft)' }}
          >✎ Edit &amp; resend</button>
          {errorMsg && <span style={{ fontSize: 12, color: 'var(--red)' }}>⚠️ {errorMsg}</span>}
        </div>
      )}
    </div>
  );
}

export default function ReplyHistory({ replies, loading, error, onChanged, onEditResend }: {
  replies: SentReply[];
  loading: boolean;
  error: string;
  onChanged: (row: SentReply, previous: SentReply) => void;
  onEditResend: (row: SentReply) => void;
}) {
  if (!loading && !error && replies.length === 0) return null;
  return (
    <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 10 }}>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 6, fontWeight: 600, letterSpacing: 1 }}>
        REPLIES SENT FROM THE APP
      </div>
      {loading && replies.length === 0 && (
        <div style={{ fontSize: 12, color: 'var(--faint)' }}>Loading replies…</div>
      )}
      {error && <div style={{ fontSize: 12, color: 'var(--red)' }}>⚠️ {error}</div>}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {replies.map(r => (
          <HistoryRow key={r.id} reply={r} onChanged={onChanged} onEditResend={onEditResend} />
        ))}
      </div>
    </div>
  );
}
