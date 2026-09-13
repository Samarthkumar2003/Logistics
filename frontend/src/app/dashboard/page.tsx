"use client";

import { useState, useEffect, useCallback, useRef } from 'react';
import Link from 'next/link';
import { needsClick, remaining, shouldAutoLoad } from './inboxPaging';
import { useInboxFeed, type InboxFeed } from './useInboxFeed';
import { apiFetch, fetchCurrentUser, logout } from '@/lib/api';
import {
  agentTypeSummary, agentsRepliedText, failedSendText, requestHref,
  statusBreakdown, statusInfo, totalText,
  type Shipment, type ShipmentPage,
} from '@/lib/shipments';
import './dashboard.css';

/** Rows a page of the unfiltered inbox holds. */
const INBOX_PAGE = 20;
/** Rows a page of one label holds — fifteen customer requests, not fifteen
 *  inbox rows of which one is a request. */
const LABEL_PAGE = 15;
/** Shipments the grid asks for. A shipment is an enquiry, not a row, so fifty is a
 *  lot of work on screen — twenty-one RFQ rows on file are seven shipments. The
 *  server caps this at 100 and reports `has_more`, which the pane says out loud
 *  rather than silently showing a prefix. */
const SHIPMENT_PAGE = 50;
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

/* ─── Signed-in operator ─────────────────────────────────────── */
/** Who is signed in, and the way out.
 *
 *  Worth the row of pixels because this desk sends mail as a named person: an
 *  operator who does not notice they are on a colleague's session sends RFQs
 *  under that colleague's identity. */
function SignedInAs() {
  const [email, setEmail] = useState('');

  useEffect(() => {
    // Silent on failure. apiFetch has already redirected if the session is
    // actually gone, and an API blip must not blank the dashboard over a caption.
    // Sign out works regardless — it only clears local storage.
    fetchCurrentUser().then(u => setEmail(u.email)).catch(() => {});
  }, []);

  return (
    <div style={{ marginTop: 8, borderTop: '1px solid var(--border)', paddingTop: 8 }}>
      {/* title so a long address is readable without widening the sidebar */}
      <div
        title={email}
        style={{
          fontSize: 11, color: 'var(--muted)', marginBottom: 6,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}
      >
        {email || ' '}
      </div>
      <button
        onClick={logout}
        style={{
          width: '100%', padding: '7px 0', borderRadius: 8,
          border: '1px solid var(--input-border)', background: 'transparent',
          color: 'var(--muted-soft)', fontSize: 12, cursor: 'pointer',
        }}
      >
        Sign out
      </button>
    </div>
  );
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
  const [approved, setApproved] = useState(false);
  const [correctionError, setCorrectionError] = useState('');
  const [saving, setSaving] = useState(false);
  const [fullBody, setFullBody] = useState<string | null>(null);
  const [loadingBody, setLoadingBody] = useState(false);
  const [processing, setProcessing] = useState(false);
  const [attachments, setAttachments] = useState<{ id: string; file_name: string; mime_type: string; size_bytes: number | null; url: string }[]>([]);
  const [loadingAtts, setLoadingAtts] = useState(false);

  async function submitFeedback(newLabel: string, isApprove: boolean) {
    setSaving(true);
    setCorrectionError('');
    try {
      const res = await apiFetch(`/feedback`, {
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
      if (isApprove) setApproved(true);
      else setCorrectedLabel(newLabel);
      // `POST /feedback` writes the label cache as well as the audit row, so the
      // server's answer for this email has genuinely changed — the tab counts and
      // the two label-filtered lists are now stale. The local `correctedLabel`
      // above only fixes this one card. This prop was passed in from the
      // dashboard and never called, so a correction sat stale until the 60s poll.
      onProcessed?.();
    } catch (err: unknown) {
      setCorrectionError(err instanceof Error ? err.message : 'Feedback failed');
    } finally {
      setSaving(false);
      setCorrecting(false);
    }
  }

  function submitCorrection(newLabel: string) {
    return submitFeedback(newLabel, false);
  }

  function approveLabel() {
    return submitFeedback(email.label ?? 'unknown', true);
  }

  async function handleToggle() {
    onToggle();
    if (!expanded && fullBody === null) {
      setLoadingBody(true);
      setLoadingAtts(true);
      try {
        const [bodyRes, attRes] = await Promise.all([
          apiFetch(`/email-body/${email.id}`),
          apiFetch(`/email-attachments/${email.id}`),
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
        const r = await apiFetch(`/email-body/${email.id}`);
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
          ) : approved ? (
            <span style={{ fontSize: 11, color: 'var(--green-soft)' }}>✓ Approved</span>
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
            <>
              <button
                disabled={saving}
                title="Confirm this label is correct"
                style={{ fontSize: 11, padding: '3px 8px', borderRadius: 6, border: '1px solid var(--green-soft)', background: 'transparent', color: 'var(--green-soft)', cursor: 'pointer', whiteSpace: 'nowrap' }}
                onClick={e => { e.stopPropagation(); approveLabel(); }}
              >
                ✓ Approve
              </button>
              <button
                disabled={saving}
                title="Mark this label wrong and pick the right one"
                style={{ fontSize: 11, padding: '3px 8px', borderRadius: 6, border: '1px solid var(--red)', background: 'transparent', color: 'var(--red)', cursor: 'pointer', whiteSpace: 'nowrap' }}
                onClick={e => { e.stopPropagation(); setCorrecting(true); }}
              >
                ✗ Disapprove
              </button>
            </>
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
/* ─── Shipment Card ──────────────────────────────────────────── */
/**
 * One customer enquiry and every RFQ it produced.
 *
 * Cards used to be one per `rfq_jobs` row — one per *agent* — so an enquiry sent to
 * seven agents filled the grid with seven near-identical cards and the operator had
 * to reconstruct the shipment by eye. Twenty-one rows on file are seven pieces of
 * work.
 *
 * There is also nothing to decide on a single agent's card, which is why reading
 * replies and awarding the shipment both moved to the detail page: awarding means
 * choosing between agents, and that is a comparison you cannot make from one
 * agent's card.
 */
function ShipmentCard({ shipment }: { shipment: Shipment }) {
  const si = statusInfo(shipment.status);
  const href = requestHref(shipment);
  const failed = failedSendText(shipment);
  const types = agentTypeSummary(shipment.agent_types);
  const breakdown = statusBreakdown(shipment.statuses);

  return (
    <div className="scard">
      {/* Header: how big the shipment is, and the one status it leads with */}
      <div className="scard-hdr">
        <div className="scard-ref">
          {shipment.rfq_count} RFQ{shipment.rfq_count === 1 ? '' : 's'}
        </div>
        <span className="scard-status-pill" style={{ color: si.color, background: si.bg }}>
          {si.label}
        </span>
      </div>

      <div className="scard-route">
        <span className="scard-port">{shipment.shipment_origin || '—'}</span>
        <span className="scard-arrow">→</span>
        <span className="scard-port">{shipment.shipment_destination || '—'}</span>
      </div>

      <div className="scard-chips">
        <span className="chip">{modeLabel(shipment.shipment_mode)}</span>
        {shipment.shipment_commodity && <span className="chip">📦 {shipment.shipment_commodity}</span>}
        {shipment.shipment_size && <span className="chip">🧱 {shipment.shipment_size}</span>}
        {shipment.shipment_weight_kg != null && (
          <span className="chip">⚖️ {shipment.shipment_weight_kg.toLocaleString()} kg</span>
        )}
      </div>

      <div className="scard-from">
        <span className="scard-from-lbl">From:</span>
        <span>{senderName(shipment.customer_email_sender)}</span>
        <span className="scard-from-subj">&quot;{shipment.customer_email_subject}&quot;</span>
      </div>

      {/* What kind of agents were asked. Only visible once the enquiry is one card:
          on a per-agent card there was no mix to show. */}
      {types && (
        <div className="scard-agents">
          <span className="scard-from-lbl">Sent to:</span>
          <span style={{ fontSize: 12, color: 'var(--muted-soft)' }}>{types}</span>
        </div>
      )}

      {/* The desk's own question: of everyone we asked, how many came back. */}
      <div style={{ fontSize: 12 }}>
        <strong style={{
          color: shipment.agents_replied > 0 ? 'var(--green-soft)' : 'var(--amber)',
        }}>
          {agentsRepliedText(shipment)}
        </strong>
        {shipment.awaiting > 0 && (
          <span style={{ color: 'var(--amber)' }}> · {shipment.awaiting} awaiting</span>
        )}
        {shipment.reply_count > shipment.agents_replied && (
          <span
            title="Some agents wrote more than once — the detail page shows the latest on top"
            style={{ color: 'var(--faint)' }}
          > · {shipment.reply_count} messages</span>
        )}
      </div>

      {/* The breakdown the headline chip cannot carry on its own. Shown only when
          the RFQs disagree — with all seven at `rfqs_sent` the chip already said
          it — but a shipment at {approved: 1, rfqs_sent: 2} must never render as
          just "Approved", which turns two unanswered agents into a finished job. */}
      {breakdown.length > 1 && (
        <div className="scard-chips">
          {breakdown.map(b => {
            const bi = statusInfo(b.status);
            return (
              <span
                key={b.status}
                className="scard-status-pill"
                style={{ color: bi.color, background: bi.bg }}
              >
                {b.count} {bi.label}
              </span>
            );
          })}
        </div>
      )}

      {/* The one thing an "Approved" headline is not allowed to hide. */}
      {failed && (
        <div style={{ fontSize: 12, color: 'var(--red)', fontWeight: 600 }}>⚠️ {failed}</div>
      )}

      <div className="scard-date">{fmtDate(shipment.first_sent_at)}</div>

      {href ? (
        <Link
          href={href}
          className="scard-quotes-btn"
          style={{ display: 'block', textAlign: 'center', textDecoration: 'none' }}
        >
          Open shipment →
        </Link>
      ) : (
        // A send with no source email has no enquiry to open. Disabled and said
        // out loud, rather than a link that 404s.
        <button
          className="scard-quotes-btn"
          disabled
          title="This RFQ was not raised from a customer email, so there is no request page for it"
          style={{ cursor: 'default', opacity: 0.55 }}
        >
          No customer email to open
        </button>
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
  const [shipmentPage, setShipmentPage] = useState<ShipmentPage>(
    { shipments: [], total: 0, truncated: false, has_more: false });
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
      const r = await apiFetch(`/automation/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !automationEnabled }),
      });
      if (r.ok) setAutomationEnabled(!automationEnabled);
    } finally {
      setTogglingAutomation(false);
    }
  }

  const fetchData = useCallback(async () => {
    // Jobs + automation status load first — unblocks UI immediately
    try {
      const [sr, ar, ur] = await Promise.all([
        apiFetch(`/shipments?limit=${SHIPMENT_PAGE}`),
        apiFetch(`/automation/status`),
        apiFetch(`/rate-cards/unlinked?limit=50`),
      ]);
      if (sr.ok) setShipmentPage(await sr.json());
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
    }
  }, []);

  /**
   * Everything on screen, re-read. The feeds merge their first page rather than
   * replacing it, so a list scrolled several pages deep stays where it is.
   *
   * The three email lists live in `useInboxFeed`, not in this component's state,
   * so they are only re-read by calling each feed's own `refresh`. "Refresh Now"
   * used to call `fetchData` alone — which reloads shipments, automation status and
   * unlinked rate cards, none of which the Inbox, Customer Requests or Rate Cards
   * tab displays. The button moved the "Updated Ns ago" line and changed nothing
   * else on screen, so it read as broken. It goes through here now.
   *
   * `allSettled`, not `all`: one list failing must not abandon the other three.
   * Each feed keeps its own error line, and `fetchData` sets the shared banner.
   */
  const refreshAll = useCallback(async (showRefreshing = false) => {
    // Held until every list has landed, not just the first — releasing the button
    // while three feeds are still in flight invites a second click that the
    // feeds' in-flight guard would silently drop.
    if (showRefreshing) setRefreshing(true);
    try {
      await Promise.allSettled([
        fetchData(),
        refreshInbox(),
        refreshRequests(),
        refreshRateCards(),
      ]);
    } finally {
      if (showRefreshing) setRefreshing(false);
    }
  }, [fetchData, refreshInbox, refreshRequests, refreshRateCards]);

  // Jobs, automation status and the unlinked list, once. The feeds load their own
  // first page on mount, so nothing here fetches inbox rows.
  useEffect(() => { fetchData(); }, [fetchData]);

  useEffect(() => {
    // Wrapped rather than passed directly: the poll must not show the button's
    // spinner, and `setInterval` would hand the callback its own arguments.
    const t = setInterval(() => { void refreshAll(); }, 60_000);
    return () => clearInterval(t);
  }, [refreshAll]);

  // Summary for the Shipments pane, totalled over the shipments on screen.
  //
  // The server has already excluded any enquiry that never sent an RFQ, and has
  // already worked out per shipment who replied and who is still awaited. These are
  // sums of its numbers rather than a second attempt at deriving them: the previous
  // version computed `awaiting` as contacted-minus-replied, which counted a failed
  // send and an awarded RFQ as agents still to hear from.
  const shipments = shipmentPage.shipments;
  const rfqsSent = shipments.reduce((n, s) => n + s.rfq_count, 0);
  const agentsReplied = shipments.reduce((n, s) => n + s.agents_replied, 0);
  const agentsAwaiting = shipments.reduce((n, s) => n + s.awaiting, 0);
  const failedSends = shipments.reduce((n, s) => n + s.send_failed, 0);

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
    { key: 'shipments' as Tab, icon: '🚢', label: 'Shipments & RFQs',   count: shipmentPage.total,    color: 'var(--yellow)' },
  ];

  // `themed` opts this subtree into the palette in app/theme.css.
  return (
    <div className="dash-app themed">

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
          <button
            className="btn-refresh"
            onClick={() => { void refreshAll(true); }}
            disabled={refreshing}
          >
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
          <SignedInAs />
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
                        One card per customer enquiry, with every RFQ it produced.
                        Open a shipment to compare agents, read their replies and award it.
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

                  {shipments.length > 0 && (
                    <div style={{ fontSize: 12, color: 'var(--muted-soft)', marginBottom: 10 }}>
                      <strong>{totalText(shipmentPage)}</strong>
                      {' · '}
                      <strong>{rfqsSent}</strong>
                      {' RFQs · '}
                      {/* Agents, not messages — a follow-up is not a second response. */}
                      <strong style={{ color: 'var(--green-soft)' }}>{agentsReplied}</strong>
                      {' replied'}
                      {agentsAwaiting > 0 && (
                        <span style={{ color: 'var(--amber)' }}>
                          {' · '}{agentsAwaiting} awaiting
                        </span>
                      )}
                      {failedSends > 0 && (
                        <span style={{ color: 'var(--red)' }}>
                          {' · '}{failedSends} never sent
                        </span>
                      )}
                      {shipmentPage.has_more && (
                        /* Said out loud rather than showing a prefix as if it were
                           everything. There is no paging control on this pane yet. */
                        <span style={{ color: 'var(--faint)' }}>
                          {' · showing the '}{shipments.length}{' most recent'}
                        </span>
                      )}
                    </div>
                  )}

                  {shipments.length === 0
                    ? <div className="empty-state">No RFQs sent yet. Process a customer email and send an RFQ to get started.</div>
                    : <div className="shipments-grid">
                        {shipments.map(s => (
                          <ShipmentCard
                            /* An orphan send has no customer_email_id, so the
                               reference of its first RFQ is the only stable key. */
                            key={s.customer_email_id ?? s.rfqs[0]?.reference ?? s.first_sent_at}
                            shipment={s}
                          />
                        ))}
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
