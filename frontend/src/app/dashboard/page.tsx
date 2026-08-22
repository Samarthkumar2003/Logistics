"use client";

import { useState, useEffect, useCallback, useRef } from 'react';
import Link from 'next/link';
import { needsClick, remaining, shouldAutoLoad } from './inboxPaging';
import { countAgents, groupByThread } from './replyThreads';
import { useInboxFeed, type InboxFeed } from './useInboxFeed';
import './dashboard.css';

const API_BASE = 'http://localhost:8001';

/** Rows a page of the unfiltered inbox holds. */
const INBOX_PAGE = 20;
/** Rows a page of one label holds — fifteen customer requests, not fifteen
 *  inbox rows of which one is a request. */
const LABEL_PAGE = 15;
/** How many pages scrolling may pull before it asks. Twelve is enough to fill
 *  any screen; few enough that a list which keeps claiming "there is more" stops
 *  and says so instead of paging through 27k rows. */
const AUTO_PAGE_LIMIT = 12;

/* ─── Types ─────────────────────────────────────────────────────── */
interface Email {
  id: string;
  sender: string;
  subject: string;
  body: string;
  label?: string;
  label_confidence?: number;
  received_at?: string;
  // Only set on the unlinked rate-card list — why this reply never attached.
  reason?: string;
  cited_reference?: string | null;
}

function formatReceived(iso?: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleString([], { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

interface RFQJob {
  id: number;
  reference: string;
  customer_email_sender: string;
  customer_email_subject: string;
  customer_email_body: string;
  shipment_origin: string;
  shipment_destination: string;
  shipment_mode: string;
  shipment_weight_kg: number | null;
  shipment_commodity: string;
  status: string;
  agents_contacted: string[];
  created_at: string;
  customer_email_id: string | null;
  // Two different numbers. `agents_replied` is how many agents came back — the
  // one a desk acts on. `reply_count` is how many messages arrived, and is
  // larger whenever an agent follows up with a correction.
  reply_count: number;
  agents_replied: number;
}

interface Reply {
  id: string;
  sender: string;
  subject: string;
  body: string;
  received_at: string;
  has_attachments: boolean;
  // The Gmail thread this message belongs to, and whether it cited the RFQ
  // reference itself. A follow-up that dropped the token is still shown — the
  // thread places it — but it is labelled rather than passed off as attributed.
  thread_id: string;
  linked: boolean;
}

type Tab = 'inbox' | 'requests' | 'ratecards' | 'unlinked' | 'shipments';

/* ─── Helpers ─────────────────────────────────────────────────── */
function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d === 1) return 'yesterday';
  return `${d} days ago`;
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-GB', {
    day: '2-digit', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

function senderName(raw: string): string {
  if (!raw) return '?';
  const m = raw.match(/^"?([^"<]+)"?\s*</);
  const name = m ? m[1].trim() : raw.split('@')[0];
  return name || '?';
}

function modeLabel(mode: string): string {
  const map: Record<string, string> = {
    sea_freight: '🚢 Sea Freight',
    air_freight: '✈️ Air Freight',
    road: '🚛 Road',
  };
  return map[mode] ?? mode;
}

function statusInfo(status: string): { label: string; color: string; bg: string; desc: string } {
  const map: Record<string, { label: string; color: string; bg: string; desc: string }> = {
    rfqs_sent:       { label: 'RFQs Sent',       color: 'var(--blue-soft)', bg: 'var(--status-blue-bg)', desc: 'Waiting for agents to reply' },
    // The row exists but the mail has not been handed to the provider yet — the
    // backend writes it before sending so a reply can never arrive against a
    // reference with no job. Normally lasts seconds; the label says "sending"
    // rather than anything reassuring because a job still here minutes later
    // means the send died mid-flight and needs a human.
    sending:         { label: 'Sending…',          color: 'var(--amber)', bg: 'var(--status-amber-bg)', desc: 'Handing the RFQ to the mail provider' },
    quotes_received: { label: 'Quotes Received',  color: 'var(--green-soft)', bg: 'var(--status-green-bg)', desc: 'Quotes in — ready to compare' },
    approved:        { label: 'Approved',          color: 'var(--purple)', bg: 'var(--status-purple-bg)', desc: 'Shipment confirmed' },
    // The RFQ never left. Written by _record_outcome when the sender did not
    // confirm a send, so this job is NOT waiting on an agent — nobody was
    // contacted. Without this entry the fallback below rendered the raw string
    // 'send_failed' in neutral grey, which reads as an ordinary state.
    send_failed:     { label: 'Send Failed',       color: 'var(--red)', bg: 'var(--red-tint)', desc: 'RFQ did not send — no agent was contacted' },
  };
  return map[status] ?? { label: status, color: 'var(--muted-soft)', bg: 'var(--status-neutral-bg)', desc: '' };
}

/* ─── Theme ──────────────────────────────────────────────────── */
type Theme = 'dark' | 'light';
const THEME_KEY = 'dashboard-theme';

function useTheme(): [Theme, () => void] {
  // Server render and first client render both assume the default so the
  // markup matches; the effect then adopts whatever the layout script set.
  const [theme, setTheme] = useState<Theme>('dark');

  useEffect(() => {
    if (document.documentElement.dataset.theme === 'light') setTheme('light');
  }, []);

  // The <html> attribute is the source of truth (the layout script sets it
  // before paint); state only mirrors it so the button label can re-render.
  const toggle = useCallback(() => {
    const next: Theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch { /* storage blocked — theme reverts on next load */ }
    setTheme(next);
  }, []);

  return [theme, toggle];
}

/* ─── Label Pill ─────────────────────────────────────────────── */
function LabelPill({ label, confidence }: { label?: string; confidence?: number }) {
  if (!label) return <span className="pill pill-gray">Unclassified</span>;
  // Classification never succeeded (LLM quota/outage). Show it as pending rather
  // than letting the 'general' fallback masquerade as a real verdict.
  if (label === 'pending')
    return <span className="pill pill-amber">⏳ Pending classification</span>;
  if (label === 'customer_requirement')
    return <span className="pill pill-blue">📦 Customer Request {confidence ? `· ${Math.round(confidence * 100)}%` : ''}</span>;
  if (label === 'quotation_rate_card')
    return <span className="pill pill-green">💰 Rate Card {confidence ? `· ${Math.round(confidence * 100)}%` : ''}</span>;
  return <span className="pill pill-gray">📋 General</span>;
}

/* ─── Helpers ─────────────────────────────────────────────────── */
function decodeEntities(str: string): string {
  return str
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, ' ');
}

/* ─── Email Card ─────────────────────────────────────────────── */
function EmailCard({ email, expanded, onToggle, onProcessed, note }: {
  email: Email;
  expanded: boolean;
  onToggle: () => void;
  onProcessed?: () => void;
  note?: string;
}) {
  const [correcting, setCorrecting] = useState(false);
  const [correctedLabel, setCorrectedLabel] = useState<string | null>(null);
  const [correctionError, setCorrectionError] = useState('');
  const [saving, setSaving] = useState(false);
  const [fullBody, setFullBody] = useState<string | null>(null);
  const [loadingBody, setLoadingBody] = useState(false);
  const [processing, setProcessing] = useState(false);
  const [attachments, setAttachments] = useState<{ id: string; file_name: string; mime_type: string; size_bytes: number | null; url: string }[]>([]);
  const [loadingAtts, setLoadingAtts] = useState(false);

  async function submitCorrection(newLabel: string) {
    setSaving(true);
    setCorrectionError('');
    try {
      const res = await fetch(`${API_BASE}/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email_id: email.id,
          email_subject: email.subject,
          // The list ships body:"" by design; the backend fills it in from
          // email_id rather than trusting the client for something it was
          // never given.
          email_body: fullBody ?? '',
          email_sender: email.sender,
          predicted_label: email.label ?? 'unknown',
          corrected_label: newLabel,
          confidence: email.label_confidence ?? 0,
        }),
      });
      if (!res.ok) {
        let detail = `Server error ${res.status}`;
        try { detail = (await res.json()).detail || detail; } catch { /* non-JSON */ }
        throw new Error(detail);
      }
      // Only claim success once the server agreed. This used to be
      // unconditional, so a rejected correction still rendered "✓ Corrected".
      setCorrectedLabel(newLabel);
    } catch (err: unknown) {
      setCorrectionError(err instanceof Error ? err.message : 'Correction failed');
    } finally {
      setSaving(false);
      setCorrecting(false);
    }
  }

  async function handleToggle() {
    onToggle();
    if (!expanded && fullBody === null) {
      setLoadingBody(true);
      setLoadingAtts(true);
      try {
        const [bodyRes, attRes] = await Promise.all([
          fetch(`${API_BASE}/email-body/${email.id}`),
          fetch(`${API_BASE}/email-attachments/${email.id}`),
        ]);
        if (bodyRes.ok) {
          const d = await bodyRes.json();
          setFullBody(decodeEntities(d.body ?? ''));
        }
        if (attRes.ok) {
          const d = await attRes.json();
          setAttachments(d.attachments ?? []);
        }
      } catch {
        setFullBody(null);
      } finally {
        setLoadingBody(false);
        setLoadingAtts(false);
      }
    }
  }

  async function openSendRequest() {
    // Open the Send Request dashboard prefilled with this email.
    // RFQ generation/sending happens there — one unique RFQ-ID per agent.
    setProcessing(true);
    let body = fullBody ?? email.body ?? '';
    if (!body) {
      try {
        const r = await fetch(`${API_BASE}/email-body/${email.id}`);
        if (r.ok) body = decodeEntities((await r.json()).body ?? '');
      } catch { /* proceed with empty body — form stays blank */ }
    }
    try {
      sessionStorage.setItem('sendRequestEmail', JSON.stringify({
        id: email.id, sender: email.sender, subject: email.subject, body,
      }));
    } catch { /* sessionStorage unavailable — form opens blank */ }
    window.location.assign('/send-request');
  }

  const displayLabel = correctedLabel ?? email.label;
  const bodyText = fullBody ?? decodeEntities(email.body ?? '');

  return (
    <div className={`ecard ${expanded ? 'ecard-open' : ''}`} onClick={handleToggle}>
      <div className="ecard-top">
        <div className="ecard-avatar">{senderName(email.sender)[0].toUpperCase()}</div>
        <div className="ecard-meta">
          <div className="ecard-sender">{senderName(email.sender)}</div>
          <div className="ecard-addr">
            {email.sender.match(/<(.+)>/)?.[1] ?? email.sender}
            {email.received_at && (
              <span style={{ marginLeft: 8, color: 'var(--faint)' }}>🕐 {formatReceived(email.received_at)}</span>
            )}
          </div>
        </div>
        <div className="ecard-right" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <LabelPill label={displayLabel} confidence={correctedLabel ? undefined : email.label_confidence} />
          {correctionError ? (
            <span style={{ fontSize: 11, color: 'var(--red)' }} title={correctionError}>
              ⚠️ Not saved
            </span>
          ) : correctedLabel ? (
            <span style={{ fontSize: 11, color: 'var(--green-soft)' }}>✓ Corrected</span>
          ) : correcting ? (
            <select
              autoFocus
              disabled={saving}
              defaultValue=""
              style={{ fontSize: 12, borderRadius: 6, padding: '3px 6px', background: 'var(--input-bg)', color: 'var(--text)', border: '1px solid var(--input-border)', cursor: 'pointer' }}
              onClick={e => e.stopPropagation()}
              onChange={e => { if (e.target.value) submitCorrection(e.target.value); }}
            >
              <option value="" disabled>Select correct label…</option>
              <option value="customer_requirement">📦 Customer Request</option>
              <option value="quotation_rate_card">💰 Rate Card</option>
              <option value="general">📋 General</option>
            </select>
          ) : (
            <button
              style={{ fontSize: 11, padding: '3px 8px', borderRadius: 6, border: '1px solid var(--input-border)', background: 'transparent', color: 'var(--muted-soft)', cursor: 'pointer', whiteSpace: 'nowrap' }}
              onClick={e => { e.stopPropagation(); setCorrecting(true); }}
            >
              Correct label
            </button>
          )}
        </div>
      </div>
      <div className="ecard-subject">{decodeEntities(email.subject)}</div>
      {note && (
        <div style={{
          marginTop: 6, fontSize: 11, color: 'var(--amber)',
          display: 'flex', alignItems: 'center', gap: 6,
        }}>
          <span>⚠️</span><span>{note}</span>
        </div>
      )}
      {expanded && (
        <div className="ecard-body" onClick={e => e.stopPropagation()}>
          {loadingBody
            ? <div style={{ color: 'var(--muted)', fontSize: 13, padding: '8px 0' }}>Loading full message…</div>
            : <pre className="ecard-pre">{bodyText?.slice(0, 3000)}{bodyText?.length > 3000 ? '\n…' : ''}</pre>
          }
          {attachments.length > 0 && (
            <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 10 }}>
              <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 6, fontWeight: 600, letterSpacing: 1 }}>ATTACHMENTS</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {attachments.map(att => (
                  <a
                    key={att.id}
                    href={att.url || undefined}
                    target="_blank"
                    rel="noreferrer"
                    style={{
                      display: 'inline-flex', alignItems: 'center', gap: 6,
                      padding: '5px 10px', borderRadius: 6,
                      border: `1px solid ${att.url ? 'var(--input-border)' : 'var(--border)'}`,
                      background: 'var(--sunken-soft)', color: att.url ? 'var(--muted-soft)' : 'var(--faint)',
                      fontSize: 12, textDecoration: 'none',
                      cursor: att.url ? 'pointer' : 'default',
                      pointerEvents: att.url ? undefined : 'none',
                    }}
                    onClick={e => e.stopPropagation()}
                  >
                    <span>{att.mime_type.includes('pdf') ? '📄' : att.mime_type.includes('sheet') || att.file_name.endsWith('.xlsx') || att.file_name.endsWith('.xls') ? '📊' : '📎'}</span>
                    <span>{att.file_name}</span>
                    {att.size_bytes && <span style={{ color: 'var(--faint)' }}>· {Math.round(att.size_bytes / 1024)}KB</span>}
                  </a>
                ))}
              </div>
            </div>
          )}
          {loadingAtts && attachments.length === 0 && (
            <div style={{ marginTop: 8, fontSize: 12, color: 'var(--faint)' }}>Loading attachments…</div>
          )}
          <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 10 }}>
            {/* Process is available on every email — the operator decides, not the classifier. */}
            <button
              disabled={processing || loadingBody}
              style={{
                padding: '7px 16px', borderRadius: 8, border: 'none',
                background: processing ? 'var(--input-border)' : 'var(--blue)', color: 'var(--on-accent)',
                fontSize: 13, fontWeight: 600, cursor: processing ? 'default' : 'pointer',
              }}
              onClick={e => { e.stopPropagation(); openSendRequest(); }}
            >
              {processing ? '⏳ Opening…' : '🚀 Process this email → Send RFQs'}
            </button>
            {/* Rate-card view is only meaningful for a customer request (it maps to an RFQ). */}
            {(displayLabel === 'customer_requirement' || correctedLabel === 'customer_requirement') && (
              <a
                href={`/request/${email.id}`}
                onClick={e => e.stopPropagation()}
                style={{
                  padding: '7px 16px', borderRadius: 8, border: '1px solid var(--border)',
                  color: 'var(--muted)', fontSize: 13, fontWeight: 600, textDecoration: 'none',
                }}
              >📋 View rate cards</a>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* ─── Reply thread ─────────────────────────────────────────────── */
function MessageBody({ body }: { body: string }) {
  return (
    <pre style={{
      fontSize: 12, color: 'var(--muted-soft)', whiteSpace: 'pre-wrap',
      lineHeight: 1.6, margin: 0, maxHeight: 220, overflowY: 'auto',
    }}>{body?.slice(0, 2000)}{(body?.length ?? 0) > 2000 ? '\n…' : ''}</pre>
  );
}

/* Same thread, but this message did not quote the RFQ reference itself. Shown
   for context; it is not attribution. */
function ThreadOnlyChip() {
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

function ThreadCard({ messages }: { messages: Reply[] }) {
  const [showEarlier, setShowEarlier] = useState(false);
  // Newest first from the API, so the head is the agent's latest word — and when
  // they replied twice, the correction rather than the version it supersedes.
  const [latest, ...earlier] = messages;
  // groupByThread never yields an empty group, but a card is not worth a white
  // screen if that ever stops being true.
  if (!latest) return null;

  return (
    <div className="q-card">
      <div className="q-top">
        <span className="q-agent">{latest.subject || '(no subject)'}</span>
        <span style={{ fontSize: 11, color: 'var(--faint)' }}>
          {formatReceived(latest.received_at)}
        </span>
      </div>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 6 }}>
        {latest.sender}{latest.has_attachments && <span style={{ marginLeft: 6 }}>📎</span>}
        {earlier.length > 0 && (
          <span style={{ marginLeft: 8, color: 'var(--faint)' }}>
            · latest of {messages.length} messages
          </span>
        )}
        {!latest.linked && <ThreadOnlyChip />}
      </div>

      <MessageBody body={latest.body} />

      {earlier.length > 0 && (
        <>
          <button
            onClick={() => setShowEarlier(s => !s)}
            style={{
              marginTop: 8, padding: 0, border: 'none', background: 'none',
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
              marginTop: 8, paddingLeft: 10, borderLeft: '2px solid var(--border)',
            }}>
              <div style={{ fontSize: 11, color: 'var(--faint)', marginBottom: 4 }}>
                {formatReceived(m.received_at)}
                {m.has_attachments && <span style={{ marginLeft: 6 }}>📎</span>}
                {!m.linked && <ThreadOnlyChip />}
              </div>
              <MessageBody body={m.body} />
            </div>
          ))}
        </>
      )}
    </div>
  );
}

/* ─── Shipment Card ──────────────────────────────────────────── */
function ShipmentCard({ job }: { job: RFQJob }) {
  const [open, setOpen] = useState(false);
  const [replies, setReplies] = useState<Reply[]>([]);
  const [loadingR, setLoadingR] = useState(false);
  const [replyError, setReplyError] = useState('');
  // The counts the pill shows: agents who answered, and messages they sent. Both
  // start from /jobs, then track whatever the panel actually holds, so opening
  // the panel can never disagree with the badge.
  const [replyCount, setReplyCount] = useState(job.reply_count ?? 0);
  const [agentsReplied, setAgentsReplied] = useState(job.agents_replied ?? 0);
  const [approving, setApproving] = useState(false);
  const [approveResult, setApproveResult] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState(job.status);
  const si = statusInfo(jobStatus);
  const agentName = job.agents_contacted?.[0] ?? 'this agent';

  async function loadReplies() {
    if (open) { setOpen(false); return; }
    setOpen(true);
    // Always refetch on open, never serve the first load again. Replies arrive
    // while the dashboard sits on screen — ingest every 5 min, then the scan —
    // so a cached panel showed the state of the thread at whatever moment it was
    // first expanded and gave no hint that a newer message existed. Collapse and
    // re-expand is the refresh gesture; keeping the old rows visible until the
    // new ones land means it never blanks out.
    setLoadingR(true);
    setReplyError('');
    try {
      const r = await fetch(`${API_BASE}/jobs/${job.reference}/replies`);
      // A 500 returns {detail}, not an array — guard before trusting the shape.
      const d = await r.json();
      if (!r.ok) throw new Error(d?.detail ?? `Server error ${r.status}`);
      const list: Reply[] = Array.isArray(d) ? d : [];
      setReplies(list);
      setReplyCount(list.length);
      setAgentsReplied(countAgents(list));
    } catch (err: unknown) {
      setReplyError(err instanceof Error ? err.message : 'Failed to load replies');
    } finally {
      setLoadingR(false);
    }
  }

  // One job is one agent, so approving needs no selection — the reference is
  // the winner. Only an acceptance goes out; nobody else is emailed.
  async function approveJob() {
    setApproving(true);
    setApproveResult(null);
    try {
      const r = await fetch(`${API_BASE}/jobs/${job.reference}/approve`, { method: 'POST' });
      const d = await r.json();
      if (r.ok) {
        setJobStatus('approved');
        setApproveResult(
          d.acceptance_status === 'sent'
            ? `✅ Approved ${d.agent_name} — acceptance sent`
            : `⚠️ Approved ${d.agent_name}, but the acceptance email ${d.acceptance_status}`
        );
      } else {
        setApproveResult(`❌ ${d.detail ?? 'Failed'}`);
      }
    } catch {
      setApproveResult('❌ Network error');
    } finally {
      setApproving(false);
    }
  }

  return (
    <div className="scard">
      {/* Header row */}
      <div className="scard-hdr">
        <div className="scard-ref">{job.reference}</div>
        <span className="scard-status-pill" style={{ color: si.color, background: si.bg }}>
          {si.label}
        </span>
      </div>

      {/* Route */}
      <div className="scard-route">
        <span className="scard-port">{job.shipment_origin}</span>
        <span className="scard-arrow">→</span>
        <span className="scard-port">{job.shipment_destination}</span>
      </div>

      {/* Details row */}
      <div className="scard-chips">
        <span className="chip">{modeLabel(job.shipment_mode)}</span>
        <span className="chip">📦 {job.shipment_commodity}</span>
        {job.shipment_weight_kg != null && (
          <span className="chip">⚖️ {job.shipment_weight_kg.toLocaleString()} kg</span>
        )}
      </div>

      {/* From */}
      <div className="scard-from">
        <span className="scard-from-lbl">From:</span>
        <span>{senderName(job.customer_email_sender)}</span>
        <span className="scard-from-subj">"{job.customer_email_subject}"</span>
      </div>

      {/* Agent, and whether they have come back */}
      {job.agents_contacted?.length > 0 && (
        <div className="scard-agents">
          <span className="scard-from-lbl">RFQ sent to:</span>
          {job.agents_contacted.map(a => (
            <span key={a} className="agent-pill">{a}</span>
          ))}
          <span style={{
            fontSize: 11, fontWeight: 600, marginLeft: 4,
            color: replyCount > 0 ? 'var(--green-soft)' : 'var(--amber)',
          }}>
            {/* Agents, not messages. `(2)` on a card whose RFQ went to one agent
                read as two carriers answering; it was that agent writing twice. */}
            {replyCount > 0
              ? `✓ ${agentsReplied || 1} agent${(agentsReplied || 1) > 1 ? 's' : ''} replied`
              : '⏳ awaiting reply'}
          </span>
          {replyCount > agentsReplied && agentsReplied > 0 && (
            <span
              title="The agent wrote more than once — the panel shows the latest on top"
              style={{ fontSize: 11, color: 'var(--faint)' }}
            >
              · {replyCount} messages
            </span>
          )}
        </div>
      )}

      {/* Status description */}
      <div className="scard-status-desc">{si.desc}</div>

      {/* Created */}
      <div className="scard-date">{fmtDate(job.created_at)}</div>

      {/* Expand replies */}
      <button className="scard-quotes-btn" onClick={loadReplies}>
        {open ? '▲ Hide Replies' : '▼ View Replies'}
      </button>

      {open && (
        <div className="scard-quotes">
          {approveResult && (
            <div style={{ fontSize: 13, marginBottom: 8, color: approveResult.startsWith('✅') ? 'var(--green-soft)' : 'var(--red)' }}>
              {approveResult}
            </div>
          )}
          {loadingR && <div className="q-loading">Loading replies…</div>}
          {replyError && <div className="q-empty" style={{ color: 'var(--red)' }}>⚠️ {replyError}</div>}
          {!loadingR && !replyError && replies.length === 0 && (
            <div className="q-empty">
              No reply from {agentName} yet. A reply is linked when the agent keeps
              the RFQ reference in the subject.
            </div>
          )}
          {replies.length > 0 && (
            <div className="q-list">
              {groupByThread(replies).map(thread => (
                <ThreadCard key={thread[0].thread_id || thread[0].id} messages={thread} />
              ))}
              <a
                href={`/request/${job.customer_email_id ?? ''}`}
                style={{ fontSize: 12, color: 'var(--blue-soft)', textDecoration: 'none' }}
              >
                Open full request →
              </a>
            </div>
          )}

          {jobStatus !== 'approved' && (
            <button
              disabled={approving}
              onClick={approveJob}
              style={{
                marginTop: 12, fontSize: 12, padding: '6px 14px', borderRadius: 6,
                border: 'none', background: approving ? 'var(--input-border)' : 'var(--green-solid)',
                color: 'var(--on-accent)', cursor: approving ? 'default' : 'pointer', fontWeight: 600,
              }}
            >
              {approving ? '⏳ Sending…' : `Award to ${agentName}`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/* ─── Summary Bar ─────────────────────────────────────────────── */
/**
 * Both numbers are totals from the server, not rows on screen. They used to count
 * the fetched page, so the strip reported "20 emails in inbox · 1 customer
 * request" on an inbox of 27,599 holding 1,054 requests.
 */
function SummaryBar({ inboxTotal, requestTotal }: { inboxTotal: number; requestTotal: number }) {
  return (
    <div className="summary-bar">
      <div className="sum-item">
        <div className="sum-val">{inboxTotal.toLocaleString()}</div>
        <div className="sum-lbl">Emails in Inbox</div>
      </div>
      <div className="sum-divider" />
      <div className="sum-item">
        <div className="sum-val" style={{ color: 'var(--blue-soft)' }}>{requestTotal.toLocaleString()}</div>
        <div className="sum-lbl">Customer Requests</div>
      </div>
    </div>
  );
}

/* ─── Scroll-driven paging ─────────────────────────────────────── */
/**
 * A one-pixel marker at the end of a list. Reaching it is the request for more.
 *
 * `rootMargin` starts the fetch before the last row is actually on screen, so
 * the rows are usually there by the time the reader gets to them. The observer is
 * rebuilt whenever `paused` changes: a fetch that finishes while the marker is
 * still in view produces no new intersection event, and a fresh observer fires
 * immediately on an element already inside the viewport — which is what keeps a
 * filtered tab pulling pages until it has something to show.
 */
function LoadMoreSentinel({ onReach, paused }: { onReach: () => void; paused: boolean }) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node || paused) return;
    const io = new IntersectionObserver(
      entries => { if (entries.some(e => e.isIntersecting)) onReach(); },
      { rootMargin: '400px' },
    );
    io.observe(node);
    return () => io.disconnect();
  }, [onReach, paused]);

  return <div ref={ref} style={{ height: 1 }} aria-hidden />;
}

/* ─── Main Page ───────────────────────────────────────────────── */
export default function Dashboard() {
  const [theme, toggleTheme] = useTheme();
  const [tab, setTab] = useState<Tab>('inbox');
  const [unlinked, setUnlinked] = useState<Email[]>([]);
  const [jobs, setJobs] = useState<RFQJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [expandedEmail, setExpandedEmail] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [automationEnabled, setAutomationEnabled] = useState<boolean | null>(null);
  const [togglingAutomation, setTogglingAutomation] = useState(false);

  // One feed per tab, each paging its own filtered list on the server. The label
  // tabs take fifteen a page: a page of a *label* is what the reader counts, and
  // twenty inbox rows used to hold about one customer request.
  const inboxFeed = useInboxFeed('', INBOX_PAGE, AUTO_PAGE_LIMIT);
  const requestFeed = useInboxFeed('customer_requirement', LABEL_PAGE, AUTO_PAGE_LIMIT);
  const rateCardFeed = useInboxFeed('quotation_rate_card', LABEL_PAGE, AUTO_PAGE_LIMIT);
  // Pulled out so the poll's dependency is the function, not the whole feed
  // object — the object is new on every render, which would rebuild the interval.
  const { refresh: refreshInbox } = inboxFeed;
  const { refresh: refreshRequests } = requestFeed;
  const { refresh: refreshRateCards } = rateCardFeed;

  async function toggleAutomation() {
    if (automationEnabled === null) return;
    setTogglingAutomation(true);
    try {
      const r = await fetch(`${API_BASE}/automation/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !automationEnabled }),
      });
      if (r.ok) setAutomationEnabled(!automationEnabled);
    } finally {
      setTogglingAutomation(false);
    }
  }

  const fetchData = useCallback(async (showRefreshing = false) => {
    if (showRefreshing) setRefreshing(true);

    // Jobs + automation status load first — unblocks UI immediately
    try {
      const [jr, ar, ur] = await Promise.all([
        fetch(`${API_BASE}/jobs`),
        fetch(`${API_BASE}/automation/status`),
        fetch(`${API_BASE}/rate-cards/unlinked?limit=50`),
      ]);
      if (jr.ok) setJobs(await jr.json());
      if (ar.ok) {
        const ad = await ar.json();
        setAutomationEnabled(ad.enabled ?? false);
      }
      if (ur.ok) setUnlinked((await ur.json()).emails ?? []);
      setLastRefresh(new Date());
      setError('');
    } catch {
      setError('Cannot reach the backend. Make sure the server is running.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  /** Everything on screen, re-read. The feeds merge their first page rather than
   *  replacing it, so a list scrolled several pages deep stays where it is. */
  const refreshAll = useCallback(() => {
    fetchData();
    refreshInbox();
    refreshRequests();
    refreshRateCards();
  }, [fetchData, refreshInbox, refreshRequests, refreshRateCards]);

  // Jobs, automation status and the unlinked list, once. The feeds load their own
  // first page on mount, so nothing here fetches inbox rows.
  useEffect(() => { fetchData(); }, [fetchData]);

  useEffect(() => {
    const t = setInterval(refreshAll, 60_000);
    return () => clearInterval(t);
  }, [refreshAll]);

  // Shipments & RFQs shows only jobs we actually sent an RFQ for — i.e. at least
  // one agent was contacted. Excludes any job row that never reached a send.
  const sentJobs  = jobs.filter(j => (j.agents_contacted?.length ?? 0) > 0);
  // Summary for the Shipments pane. One job is one agent, so an agent who has
  // answered is a job with at least one distinct replying sender — counting
  // messages here would report a follow-up as another agent responding.
  const agentsContacted = sentJobs.reduce((n, j) => n + (j.agents_contacted?.length ?? 0), 0);
  const agentsReplied   = sentJobs.reduce((n, j) => n + (j.agents_replied ?? 0), 0);
  const agentsAwaiting  = Math.max(0, agentsContacted - agentsReplied);

  /** A plain, unpaged list of emails — the unlinked rate cards, which come from
   *  their own endpoint and arrive whole. */
  function renderEmails(list: Email[], emptyMsg: string) {
    if (list.length === 0) return <div className="empty-state">{emptyMsg}</div>;
    return list.map(e => (
      <EmailCard
        key={e.id}
        email={e}
        expanded={expandedEmail === e.id}
        onToggle={() => setExpandedEmail(expandedEmail === e.id ? null : e.id)}
        onProcessed={refreshAll}
        note={e.reason}
      />
    ));
  }

  /** A paging list. `noun` is what the footer counts — "customer requests", not
   *  "emails", because the list is a page of that label and the total is how many
   *  of them exist. */
  function renderFeed(feed: InboxFeed, noun: string, emptyMsg: string) {
    // Nothing has answered yet. "No customer requests" would be a claim about an
    // inbox nobody has read.
    if (feed.emails.length === 0 && feed.loading) {
      return <div className="empty-state">⏳ Loading {noun}…</div>;
    }
    if (feed.emails.length === 0 && feed.error) {
      return <div className="empty-state" style={{ color: 'var(--red)' }}>⚠️ {feed.error}</div>;
    }
    if (feed.emails.length === 0 && !feed.paging.more) {
      return <div className="empty-state">{emptyMsg}</div>;
    }
    return (
      <>
        {feed.emails.map(e => (
          <EmailCard
            key={e.id}
            email={e}
            expanded={expandedEmail === e.id}
            onToggle={() => setExpandedEmail(expandedEmail === e.id ? null : e.id)}
            onProcessed={refreshAll}
            note={e.reason}
          />
        ))}
        <div style={{ marginTop: 12, textAlign: 'center', fontSize: 12, color: 'var(--faint)' }}>
          {`${feed.emails.length.toLocaleString()} of ${feed.total.toLocaleString()} ${noun}`}
          {feed.error && (
            <div style={{ color: 'var(--red)', marginTop: 6 }}>⚠️ {feed.error}</div>
          )}
          {feed.paging.loading && <div style={{ marginTop: 6 }}>Loading more…</div>}
          {/* Past the cap, or after a failure, scrolling stops asking and the
              reader does — an automatic retry loop against a failing endpoint
              is the one thing worse than a button. */}
          {needsClick(feed.paging) && (
            <button
              onClick={feed.loadMoreByClick}
              style={{
                marginTop: 8, padding: '8px 16px', background: 'transparent',
                border: '1px solid var(--input-border)', borderRadius: 8,
                color: 'var(--muted-soft)', cursor: 'pointer', fontSize: 13,
              }}
            >
              {feed.error ? 'Retry' : `Keep loading (${remaining(feed.paging).toLocaleString()} remaining)`}
            </button>
          )}
          {!feed.paging.more && feed.total > 0 && (
            <div style={{ marginTop: 6 }}>End of list</div>
          )}
          <LoadMoreSentinel
            onReach={feed.loadMoreOnScroll}
            paused={!shouldAutoLoad(feed.paging)}
          />
        </div>
      </>
    );
  }

  // Badges count what each list holds in total, not the rows fetched so far. They
  // used to count fetched rows, so Customer Requests read "1" on 1,054 of them.
  const NAV = [
    { key: 'inbox'     as Tab, icon: '📬', label: 'All Emails',         count: inboxFeed.total,    color: 'var(--blue-soft)' },
    { key: 'requests'  as Tab, icon: '📦', label: 'Customer Requests',  count: requestFeed.total,  color: 'var(--blue)' },
    { key: 'ratecards' as Tab, icon: '💰', label: 'Rate Cards',         count: rateCardFeed.total, color: 'var(--green)' },
    { key: 'unlinked'  as Tab, icon: '🔗', label: 'Needs Linking',      count: unlinked.length,    color: 'var(--amber)' },
    { key: 'shipments' as Tab, icon: '🚢', label: 'Shipments & RFQs',   count: sentJobs.length,    color: 'var(--yellow)' },
  ];

  return (
    <div className="dash-app">

      {/* ── Sidebar ── */}
      <aside className="sidebar">
        <div className="sidebar-brand">
          <span className="brand-icon">🚢</span>
          <div>
            <div className="brand-name">Logistics Copilot</div>
            <div className="brand-sub">Operations Dashboard</div>
          </div>
        </div>

        <nav className="sidebar-nav">
          {NAV.map(n => (
            <button
              key={n.key}
              className={`nav-item ${tab === n.key ? 'nav-active' : ''}`}
              style={{ '--nc': n.color } as React.CSSProperties}
              onClick={() => setTab(n.key)}
            >
              <span className="nav-icon">{n.icon}</span>
              <span className="nav-label">{n.label}</span>
              <span className="nav-badge" style={{ background: tab === n.key ? n.color : undefined }}>
                {n.count.toLocaleString()}
              </span>
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          {error && <div className="err-banner">{error}</div>}
          <div className="refresh-time">
            {lastRefresh ? `Updated ${timeAgo(lastRefresh.toISOString())}` : 'Loading…'}
          </div>
          <button className="btn-refresh" onClick={() => fetchData(true)} disabled={refreshing}>
            {refreshing ? '⏳ Refreshing…' : '↻ Refresh Now'}
          </button>
          <button
            onClick={toggleAutomation}
            disabled={togglingAutomation || automationEnabled === null}
            style={{
              width: '100%', padding: '9px 0', borderRadius: 8,
              border: `1px solid ${automationEnabled ? 'var(--green-solid)' : 'var(--input-border)'}`,
              background: automationEnabled ? 'var(--status-green-bg)' : 'var(--status-neutral-bg)',
              color: automationEnabled ? 'var(--green-soft)' : 'var(--muted-soft)',
              fontSize: 13, fontWeight: 600, cursor: 'pointer',
              marginTop: 4,
            }}
          >
            {togglingAutomation ? '⏳…' : automationEnabled ? '🤖 Automation ON' : '⏸ Automation OFF'}
          </button>
          <button
            className="btn-theme"
            onClick={toggleTheme}
            aria-pressed={theme === 'light'}
            title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          >
            <span className="theme-icon">{theme === 'dark' ? '☀️' : '🌙'}</span>
            <span className="theme-label">{theme === 'dark' ? 'Light mode' : 'Dark mode'}</span>
          </button>
          <Link href="/" className="btn-office">🏢 Office View</Link>
        </div>
      </aside>

      {/* ── Main ── */}
      <div className="main-area">

        {loading ? (
          <div className="full-loading">
            <div className="big-spinner" />
            <p>Loading your dashboard…</p>
          </div>
        ) : (
          <>
            {/* ── Summary strip ── */}
            <SummaryBar inboxTotal={inboxFeed.total} requestTotal={requestFeed.total} />

            {/* ── Content ── */}
            <div className="content-wrap">

              {/* ALL EMAILS */}
              {tab === 'inbox' && (
                <div className="pane">
                  <div className="pane-header">
                    <div>
                      <h2 className="pane-title">📬 All Emails in Inbox</h2>
                      <p className="pane-desc">Every email received — click any row to read the full message.</p>
                    </div>
                    <div className="pane-legend">
                      <span className="pill pill-blue">📦 Customer Request</span>
                      <span className="pill pill-green">💰 Rate Card</span>
                      <span className="pill pill-gray">📋 General</span>
                    </div>
                  </div>
                  <div className="email-list">
                    {renderFeed(inboxFeed, 'emails', 'No emails in inbox.')}
                  </div>
                </div>
              )}

              {/* CUSTOMER REQUESTS */}
              {tab === 'requests' && (
                <div className="pane">
                  <div className="pane-header">
                    <div>
                      <h2 className="pane-title">📦 Customer Requests</h2>
                      <p className="pane-desc">
                        Emails from customers asking for a freight quote.
                        The system auto-detects these and sends RFQs to agents on your behalf.
                      </p>
                    </div>
                  </div>
                  <div className="email-list">
                    {/* Fifteen requests a page, filtered in the database. This
                        used to be a client-side filter over fetched inbox rows,
                        so the tab showed the one or two requests that happened to
                        be in the last twenty emails. */}
                    {renderFeed(
                      requestFeed, 'customer requests',
                      'No customer requests yet.',
                    )}
                  </div>
                </div>
              )}

              {/* RATE CARDS */}
              {tab === 'ratecards' && (
                <div className="pane">
                  <div className="pane-header">
                    <div>
                      <h2 className="pane-title">💰 Rate Cards from Agents</h2>
                      <p className="pane-desc">
                        Pricing replies from freight agents — auto-parsed and matched to shipment jobs.
                      </p>
                    </div>
                  </div>
                  <div className="email-list">
                    {renderFeed(
                      rateCardFeed, 'rate cards',
                      'No rate cards yet. They appear here once agents reply to your RFQs.',
                    )}
                  </div>
                </div>
              )}

              {/* NEEDS LINKING */}
              {tab === 'unlinked' && (
                <div className="pane">
                  <div className="pane-header">
                    <div>
                      <h2 className="pane-title">🔗 Rate Cards That Need Linking</h2>
                      <p className="pane-desc">
                        Agent replies we could not attach to an RFQ, because the
                        reply did not quote its <code>RFQ-…</code> reference back.
                        Nothing is lost — these are normal inbox emails — but they
                        will not appear on the customer request page until they are
                        linked.
                      </p>
                    </div>
                  </div>
                  <div className="email-list">
                    {renderEmails(
                      unlinked,
                      'Nothing to link — every rate card reached its RFQ. 🎉',
                    )}
                  </div>
                </div>
              )}

              {/* SHIPMENTS */}
              {tab === 'shipments' && (
                <div className="pane">
                  <div className="pane-header">
                    <div>
                      <h2 className="pane-title">🚢 Shipments & RFQs</h2>
                      <p className="pane-desc">
                        Every shipment processed — route, agents contacted, and quotes received.
                        Click <strong>View Quotes</strong> on any card to see agent prices.
                      </p>
                    </div>
                    <div className="pane-legend">
                      {/* Lifecycle order, so the legend reads as the path a job takes. */}
                      {(['sending','rfqs_sent','quotes_received','approved','send_failed'] as const).map(s => {
                        const si = statusInfo(s);
                        return <span key={s} className="status-legend" style={{ color: si.color, background: si.bg }}>{si.label}</span>;
                      })}
                    </div>
                  </div>
                  {sentJobs.length > 0 && (
                    /* The desk's own question: of everyone we asked, how many
                       came back. Agents, not messages — a follow-up is not a
                       second response. */
                    <div style={{ fontSize: 12, color: 'var(--muted-soft)', marginBottom: 10 }}>
                      <strong style={{ color: 'var(--green-soft)' }}>{agentsReplied}</strong>
                      {' of '}
                      <strong>{agentsContacted}</strong>
                      {' agents replied'}
                      {agentsAwaiting > 0 && (
                        <span style={{ color: 'var(--amber)' }}>
                          {' · '}{agentsAwaiting} still awaiting
                        </span>
                      )}
                    </div>
                  )}
                  {sentJobs.length === 0
                    ? <div className="empty-state">No RFQs sent yet. Process a customer email and send an RFQ to get started.</div>
                    : <div className="shipments-grid">
                        {[...sentJobs]
                          .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
                          .map(j => <ShipmentCard key={j.reference} job={j} />)
                        }
                      </div>
                  }
                </div>
              )}

            </div>
          </>
        )}
      </div>
    </div>
  );
}
