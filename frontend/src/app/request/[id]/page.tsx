"use client";

import { useEffect, useState } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { apiFetch } from '@/lib/api';

/* ─── Types ─────────────────────────────────────────────────────── */
interface CustomerEmail {
  provider_msg_id: string;
  sender: string;
  subject: string;
  body: string;
  received_at: string;
}
interface Job {
  reference: string;
  agents_contacted: string[];
  status: string;
  created_at: string;
}
interface Reply {
  id: string;
  rfq_reference: string | null;
  agent_name: string;
  sender: string;
  subject: string;
  body: string;
  received_at: string;
  has_attachments: boolean;
}
interface Attachment {
  id: string;
  file_name: string;
  mime_type: string;
  size_bytes: number | null;
  url: string;
}
interface RequestData {
  customer_email_id: string;
  customer_email: CustomerEmail | null;
  jobs: Job[];
  replies: Reply[];
  agents_contacted: string[];
  // `agents` is how many were contacted, `agents_replied` how many came back,
  // `replies` how many messages arrived — the last is larger on any follow-up.
  counts: { agents: number; replies: number; agents_replied: number };
}

function fmt(iso?: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? '' : d.toLocaleString([], {
    day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

const card: React.CSSProperties = {
  background: '#0d1117', border: '1px solid #1e293b', borderRadius: 10,
  padding: 18, marginBottom: 18,
};

/* ─── One agent reply ───────────────────────────────────────────── */
function ReplyCard({ reply }: { reply: Reply }) {
  const [open, setOpen] = useState(false);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [loadingAtts, setLoadingAtts] = useState(false);
  const [fetched, setFetched] = useState(false);

  async function toggle() {
    const next = !open;
    setOpen(next);
    // Attachments are signed URLs with a short expiry, so fetch them when the
    // reply is actually opened rather than up front for every card.
    if (next && !fetched && reply.has_attachments) {
      setLoadingAtts(true);
      try {
        const res = await apiFetch(`/email-attachments/${reply.id}`);
        if (res.ok) setAttachments((await res.json()).attachments ?? []);
      } catch { /* leave the list empty — the body is still readable */ }
      setLoadingAtts(false);
      setFetched(true);
    }
  }

  return (
    <div style={{ ...card, marginBottom: 12 }}>
      <div
        onClick={toggle}
        style={{ cursor: 'pointer', display: 'flex', alignItems: 'baseline', gap: 10 }}
      >
        <span style={{ color: '#64748b', fontSize: 12 }}>{open ? '▾' : '▸'}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: '#e2e8f0' }}>
            {reply.subject || '(no subject)'}
          </div>
          <div style={{ fontSize: 11, color: '#64748b', marginTop: 3 }}>
            {reply.sender}
            {reply.has_attachments && <span style={{ marginLeft: 8 }}>📎</span>}
          </div>
        </div>
        <span style={{ fontSize: 11, color: '#475569', whiteSpace: 'nowrap' }}>
          {fmt(reply.received_at)}
        </span>
      </div>

      {open && (
        <>
          <div style={{
            marginTop: 12, fontSize: 12, color: '#cbd5e1', whiteSpace: 'pre-wrap',
            lineHeight: 1.6, background: '#080b12', border: '1px solid #1e293b',
            borderRadius: 6, padding: 12, maxHeight: 420, overflowY: 'auto',
          }}>{reply.body || '(no text body — the rates may be in an attachment)'}</div>

          {loadingAtts && (
            <div style={{ marginTop: 8, fontSize: 11, color: '#475569' }}>Loading attachments…</div>
          )}
          {attachments.length > 0 && (
            <div style={{ marginTop: 10, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {attachments.map(att => (
                <a
                  key={att.id}
                  href={att.url || undefined}
                  target="_blank"
                  rel="noreferrer"
                  style={{
                    display: 'inline-flex', alignItems: 'center', gap: 6,
                    padding: '5px 10px', borderRadius: 6, fontSize: 12,
                    border: '1px solid #2a2a3a', background: '#080b12',
                    color: att.url ? '#93c5fd' : '#475569', textDecoration: 'none',
                    pointerEvents: att.url ? undefined : 'none',
                  }}
                >
                  <span>{att.mime_type?.includes('pdf') ? '📄'
                    : att.file_name?.match(/\.(xlsx?|csv)$/i) ? '📊' : '📎'}</span>
                  <span>{att.file_name}</span>
                  {att.size_bytes ? (
                    <span style={{ color: '#475569' }}>· {Math.round(att.size_bytes / 1024)}KB</span>
                  ) : null}
                </a>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

/* ─── Page ──────────────────────────────────────────────────────── */
export default function CustomerRequestPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id ?? '';
  const [data, setData] = useState<RequestData | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [errorMsg, setErrorMsg] = useState('');

  useEffect(() => {
    if (!id) return;
    (async () => {
      setStatus('loading');
      try {
        const res = await apiFetch(`/customer-request/${id}`);
        if (!res.ok) {
          let detail = `Server error ${res.status}`;
          try { detail = (await res.json()).detail || detail; } catch { /* non-JSON */ }
          throw new Error(detail);
        }
        setData(await res.json());
        setStatus('ready');
      } catch (err: unknown) {
        setErrorMsg(err instanceof Error ? err.message : 'Failed to load');
        setStatus('error');
      }
    })();
  }, [id]);

  // Group replies by the agent whose RFQ they answer. agent_name comes from the
  // job the reference belongs to, so it always matches agents_contacted.
  const repliesByAgent: Record<string, Reply[]> = {};
  (data?.replies ?? []).forEach(r => {
    const key = r.agent_name || r.sender || 'Unknown';
    (repliesByAgent[key] ??= []).push(r);
  });
  const awaiting = (data?.agents_contacted ?? []).filter(a => !repliesByAgent[a]);

  // globals.css centres <body> as a fixed, non-scrolling flex panel (built for
  // the dashboard). This is a scrollable document page, so escape that.
  const outer: React.CSSProperties = {
    position: 'fixed', inset: 0, overflowY: 'auto',
    background: '#0a0a14', color: '#e2e8f0', fontFamily: 'system-ui, sans-serif',
  };
  const wrap: React.CSSProperties = {
    maxWidth: 980, margin: '0 auto', padding: '28px 20px', textAlign: 'left',
  };

  return (
    <div style={outer}>
    <div style={wrap}>
      <Link href="/dashboard" style={{ color: '#60a5fa', fontSize: 13, textDecoration: 'none' }}>
        ← Back to inbox
      </Link>
      <h1 style={{ fontSize: 20, fontWeight: 700, margin: '14px 0 20px' }}>📋 Customer Request</h1>

      {status === 'loading' && <div style={{ color: '#94a3b8' }}>Loading…</div>}
      {status === 'error' && <div style={{ color: '#ef4444', fontSize: 13 }}>{errorMsg}</div>}

      {status === 'ready' && data && (
        <>
          {/* Original customer email */}
          {data.customer_email ? (
            <div style={card}>
              <div style={{ fontSize: 11, color: '#64748b', marginBottom: 6 }}>ORIGINAL REQUEST</div>
              <div style={{ fontSize: 13, marginBottom: 4 }}>
                <span style={{ color: '#94a3b8' }}>From: </span>{data.customer_email.sender}
              </div>
              <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 10 }}>{data.customer_email.subject}</div>
              <div style={{
                fontSize: 12, color: '#cbd5e1', whiteSpace: 'pre-wrap', lineHeight: 1.6,
                background: '#080b12', border: '1px solid #1e293b', borderRadius: 6,
                padding: 12, maxHeight: 260, overflowY: 'auto',
              }}>{data.customer_email.body}</div>
            </div>
          ) : (
            <div style={{ ...card, color: '#94a3b8', fontSize: 13 }}>
              Original email not stored — showing agent replies only.
            </div>
          )}

          {/* Summary */}
          <div style={{ display: 'flex', gap: 24, margin: '4px 4px 18px', fontSize: 13 }}>
            <span><strong style={{ color: '#a78bfa' }}>{data.counts.agents}</strong> agents contacted</span>
            {/* Agents first, messages second. An agent who sends a rate and then a
                correction is one response, so "2 replies" alone overstated how
                many quotes there are to compare. */}
            <span><strong style={{ color: '#34d399' }}>{data.counts.agents_replied}</strong> agents replied</span>
            <span><strong style={{ color: '#34d399' }}>{data.counts.replies}</strong> replies received</span>
            <span><strong style={{ color: '#fbbf24' }}>{awaiting.length}</strong> awaiting reply</span>
          </div>

          {/* Agent replies */}
          <div style={{ fontSize: 12, fontWeight: 700, color: '#a78bfa', margin: '0 4px 10px' }}>
            💬 AGENT REPLIES
          </div>
          {data.counts.replies === 0 && (
            <div style={{ ...card, color: '#94a3b8', fontSize: 13 }}>
              No replies linked yet. A reply is linked when the agent keeps the RFQ
              reference in the subject — one that dropped it is still in the inbox,
              just not shown here.
            </div>
          )}
          {Object.entries(repliesByAgent).map(([agent, replies]) => (
            <div key={agent} style={{ marginBottom: 20 }}>
              <div style={{ fontSize: 13, fontWeight: 600, margin: '0 4px 8px', color: '#e2e8f0' }}>
                {agent}
                <span style={{ color: '#64748b', fontWeight: 400 }}>
                  {' '}· {replies.length} {replies.length === 1 ? 'message' : 'messages'}
                </span>
              </div>
              {replies.map(r => <ReplyCard key={r.id} reply={r} />)}
            </div>
          ))}

          {/* Awaiting */}
          {awaiting.length > 0 && (
            <div style={card}>
              <div style={{ fontSize: 12, fontWeight: 700, color: '#fbbf24', marginBottom: 8 }}>⏳ AWAITING REPLY</div>
              <div style={{ fontSize: 12, color: '#cbd5e1' }}>{awaiting.join(', ')}</div>
            </div>
          )}
        </>
      )}
    </div>
    </div>
  );
}
