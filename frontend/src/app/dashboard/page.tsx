"use client";

import { useState, useEffect, useCallback, useRef } from 'react';
import Link from 'next/link';
import './dashboard.css';

const API_BASE = 'http://localhost:8001';

/* ─── Types ─────────────────────────────────────────────────────── */
interface Email {
  id: string;
  sender: string;
  subject: string;
  body: string;
  label?: string;
  label_confidence?: number;
  received_at?: string;
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
}

interface Quotation {
  id: number;
  agent_name: string;
  rate: number;
  currency: string;
  transit_time_days: number;
  rate_label: string;
  validity: string;
  terms: string;
  ai_assessment: string;
  is_selected: boolean;
  received_at: string;
}

type Tab = 'inbox' | 'requests' | 'ratecards' | 'shipments';

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
    awaiting_quotes: { label: 'Awaiting Quotes',  color: 'var(--amber)', bg: 'var(--status-amber-bg)', desc: 'Agents notified, quotes pending' },
    quotes_received: { label: 'Quotes Received',  color: 'var(--green-soft)', bg: 'var(--status-green-bg)', desc: 'Quotes in — ready to compare' },
    approved:        { label: 'Approved',          color: 'var(--purple)', bg: 'var(--status-purple-bg)', desc: 'Shipment confirmed' },
  };
  return map[status] ?? { label: status, color: 'var(--muted-soft)', bg: 'var(--status-neutral-bg)', desc: '' };
}

function assessInfo(a: string): { label: string; color: string } {
  const map: Record<string, { label: string; color: string }> = {
    within_range:   { label: '✅ Fair price',    color: 'var(--green-soft)' },
    above_expected: { label: '⚠️ Above average', color: 'var(--amber)' },
    below_expected: { label: '🎉 Below average', color: 'var(--blue-soft)' },
  };
  return map[a] ?? { label: a || '—', color: 'var(--muted)' };
}

function dedupe(emails: Email[]): Email[] {
  const seen = new Set<string>();
  return emails.filter(e => seen.has(e.id) ? false : !!seen.add(e.id));
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
function EmailCard({ email, expanded, onToggle, onProcessed }: {
  email: Email;
  expanded: boolean;
  onToggle: () => void;
  onProcessed?: () => void;
}) {
  const [correcting, setCorrecting] = useState(false);
  const [correctedLabel, setCorrectedLabel] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [fullBody, setFullBody] = useState<string | null>(null);
  const [loadingBody, setLoadingBody] = useState(false);
  const [processing, setProcessing] = useState(false);
  const [attachments, setAttachments] = useState<{ id: string; file_name: string; mime_type: string; size_bytes: number | null; url: string }[]>([]);
  const [loadingAtts, setLoadingAtts] = useState(false);

  async function submitCorrection(newLabel: string) {
    setSaving(true);
    try {
      await fetch(`${API_BASE}/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email_id: email.id,
          subject: email.subject,
          body: email.body,
          sender: email.sender,
          predicted_label: email.label,
          corrected_label: newLabel,
        }),
      });
      setCorrectedLabel(newLabel);
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
          {correctedLabel ? (
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
          {(displayLabel === 'customer_requirement' || correctedLabel === 'customer_requirement') && (
            <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 10 }}>
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
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ─── Shipment Card ──────────────────────────────────────────── */
function ShipmentCard({ job }: { job: RFQJob }) {
  const [open, setOpen] = useState(false);
  const [quotes, setQuotes] = useState<Quotation[]>([]);
  const [loadingQ, setLoadingQ] = useState(false);
  const [fetched, setFetched] = useState(false);
  const [approving, setApproving] = useState<string | null>(null);
  const [approveResult, setApproveResult] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState(job.status);
  const si = statusInfo(jobStatus);

  async function loadQuotes() {
    if (fetched) { setOpen(o => !o); return; }
    setOpen(true);
    setLoadingQ(true);
    try {
      const r = await fetch(`${API_BASE}/jobs/${job.reference}/quotations`);
      const d = await r.json();
      setQuotes(d ?? []);
      setFetched(true);
    } finally {
      setLoadingQ(false);
    }
  }

  async function approveQuote(agentName: string) {
    setApproving(agentName);
    setApproveResult(null);
    try {
      const r = await fetch(`${API_BASE}/jobs/${job.reference}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ selected_agent: agentName }),
      });
      const d = await r.json();
      if (r.ok) {
        setQuotes(prev => prev.map(q => ({ ...q, is_selected: q.agent_name === agentName })));
        setJobStatus('approved');
        setApproveResult(`✅ Approved ${agentName}`);
      } else {
        setApproveResult(`❌ ${d.detail ?? 'Failed'}`);
      }
    } catch {
      setApproveResult('❌ Network error');
    } finally {
      setApproving(null);
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

      {/* Agents */}
      {job.agents_contacted?.length > 0 && (
        <div className="scard-agents">
          <span className="scard-from-lbl">RFQ sent to:</span>
          {job.agents_contacted.map(a => (
            <span key={a} className="agent-pill">{a}</span>
          ))}
        </div>
      )}

      {/* Status description */}
      <div className="scard-status-desc">{si.desc}</div>

      {/* Created */}
      <div className="scard-date">{fmtDate(job.created_at)}</div>

      {/* Expand quotes */}
      <button className="scard-quotes-btn" onClick={loadQuotes}>
        {open ? '▲ Hide Quotes' : '▼ View Quotes'}
      </button>

      {open && (
        <div className="scard-quotes">
          {loadingQ && <div className="q-loading">Loading quotes…</div>}
          {!loadingQ && quotes.length === 0 && (
            <div className="q-empty">No quotes received yet for this shipment.</div>
          )}
          {!loadingQ && quotes.length > 0 && (
            <div className="q-list">
              {approveResult && (
                <div style={{ fontSize: 13, marginBottom: 8, color: approveResult.startsWith('✅') ? 'var(--green-soft)' : 'var(--red)' }}>
                  {approveResult}
                </div>
              )}
              {quotes.map(q => {
                const ai = assessInfo(q.ai_assessment);
                const isApproving = approving === q.agent_name;
                return (
                  <div key={q.id} className={`q-card ${q.is_selected ? 'q-selected' : ''}`}>
                    <div className="q-top">
                      <span className="q-agent">{q.agent_name}</span>
                      {q.is_selected
                        ? <span className="q-winner">✅ Selected</span>
                        : jobStatus !== 'approved' && (
                          <button
                            disabled={!!approving}
                            onClick={() => approveQuote(q.agent_name)}
                            style={{
                              fontSize: 11, padding: '3px 10px', borderRadius: 6,
                              border: 'none', background: isApproving ? 'var(--input-border)' : 'var(--green-solid)',
                              color: 'var(--on-accent)', cursor: approving ? 'default' : 'pointer', fontWeight: 600,
                            }}
                          >
                            {isApproving ? '⏳…' : 'Approve'}
                          </button>
                        )
                      }
                    </div>
                    <div className="q-rate">
                      {q.currency} {q.rate?.toLocaleString()}
                      {q.rate_label && <span className="q-label-tag">{q.rate_label}</span>}
                    </div>
                    <div className="q-meta">
                      <span>🕐 {q.transit_time_days} days transit</span>
                      {q.validity && <span>📅 Valid: {q.validity}</span>}
                      <span style={{ color: ai.color }}>{ai.label}</span>
                    </div>
                    {q.terms && <div className="q-terms">{q.terms}</div>}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ─── Summary Bar ─────────────────────────────────────────────── */
function SummaryBar({ emails, jobs }: { emails: Email[]; jobs: RFQJob[] }) {
  const requests = emails.filter(e => e.label === 'customer_requirement').length;
  const rateCards = emails.filter(e => e.label === 'quotation_rate_card').length;
  const activeJobs = jobs.filter(j => j.status !== 'approved').length;
  const quotesIn = jobs.filter(j => j.status === 'quotes_received').length;

  return (
    <div className="summary-bar">
      <div className="sum-item">
        <div className="sum-val">{emails.length}</div>
        <div className="sum-lbl">Emails in Inbox</div>
      </div>
      <div className="sum-divider" />
      <div className="sum-item">
        <div className="sum-val" style={{ color: 'var(--blue-soft)' }}>{requests}</div>
        <div className="sum-lbl">Customer Requests</div>
      </div>
      <div className="sum-divider" />
      <div className="sum-item">
        <div className="sum-val" style={{ color: 'var(--green-soft)' }}>{rateCards}</div>
        <div className="sum-lbl">Rate Cards</div>
      </div>
      <div className="sum-divider" />
      <div className="sum-item">
        <div className="sum-val" style={{ color: 'var(--amber)' }}>{activeJobs}</div>
        <div className="sum-lbl">Active Shipments</div>
      </div>
      <div className="sum-divider" />
      <div className="sum-item">
        <div className="sum-val" style={{ color: 'var(--purple)' }}>{quotesIn}</div>
        <div className="sum-lbl">Ready to Approve</div>
      </div>
    </div>
  );
}

/* ─── Main Page ───────────────────────────────────────────────── */
export default function Dashboard() {
  const [theme, toggleTheme] = useTheme();
  const [tab, setTab] = useState<Tab>('inbox');
  const [emails, setEmails] = useState<Email[]>([]);
  const [emailTotal, setEmailTotal] = useState(0);
  const [emailOffset, setEmailOffset] = useState(0);
  // Stale-closure-free mirror of how deep the inbox is scrolled. fetchData has
  // empty deps (stable across the 60s poll) so it can't read live emailOffset;
  // the ref gives it the current depth without re-creating the interval.
  const emailOffsetRef = useRef(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [jobs, setJobs] = useState<RFQJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [inboxError, setInboxError] = useState('');
  const [inboxLoading, setInboxLoading] = useState(true);
  const [error, setError] = useState('');
  const [expandedEmail, setExpandedEmail] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [automationEnabled, setAutomationEnabled] = useState<boolean | null>(null);
  const [togglingAutomation, setTogglingAutomation] = useState(false);

  const PAGE = 20;

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
      const [jr, ar] = await Promise.all([
        fetch(`${API_BASE}/jobs`),
        fetch(`${API_BASE}/automation/status`),
      ]);
      if (jr.ok) setJobs(await jr.json());
      if (ar.ok) {
        const ad = await ar.json();
        setAutomationEnabled(ad.enabled ?? false);
      }
      setLastRefresh(new Date());
      setError('');
    } catch {
      setError('Cannot reach the backend. Make sure the server is running.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }

    // Inbox fetches in background. Re-fetch at the CURRENT scrolled depth (not
    // page 1) so the 60s auto-poll doesn't collapse a "Load more"-expanded list.
    setInboxLoading(true);
    setInboxError('');
    try {
      const ir = await fetch(`${API_BASE}/fetch-inbox?limit=${PAGE}&offset=0`);
      if (ir.ok) {
        const d = await ir.json();
        setEmails(prev => {
          const incoming = d.emails ?? [];
          if (prev.length === 0) return dedupe(incoming);
          const incomingMap = new Map(incoming.map((e: Email) => [e.id, e]));
          const existingIds = new Set(prev.map(e => e.id));
          const updated = prev.map(e => {
            const fresh = incomingMap.get(e.id);
            return fresh ? { ...e, ...fresh } : e;
          });
          const newOnes = incoming.filter((e: Email) => !existingIds.has(e.id));
          return dedupe([...newOnes, ...updated]);
        });
        setEmailTotal(d.total ?? 0);
      } else {
        const d = await ir.json().catch(() => ({}));
        setInboxError(d.detail ?? 'Failed to load inbox');
      }
    } catch {
      setInboxError('Cannot reach backend');
    } finally {
      setInboxLoading(false);
    }
  }, []);

  const loadMoreEmails = useCallback(async () => {
    setLoadingMore(true);
    try {
      const ir = await fetch(`${API_BASE}/fetch-inbox?limit=${PAGE}&offset=${emailOffset}`);
      if (ir.ok) {
        const d = await ir.json();
        setEmails(prev => dedupe([...prev, ...(d.emails ?? [])]));
        setEmailOffset(prev => { const next = prev + PAGE; emailOffsetRef.current = next; return next; });
      }
    } finally {
      setLoadingMore(false);
    }
  }, [emailOffset]);

  useEffect(() => {
    fetchData();
    const t = setInterval(() => fetchData(), 60_000);
    return () => clearInterval(t);
  }, [fetchData]);

  const requests  = emails.filter(e => e.label === 'customer_requirement');
  const rateCards = emails.filter(e => e.label === 'quotation_rate_card');

  function renderEmails(list: Email[], emptyMsg: string, showLoadMore = false) {
    if (list.length === 0) return <div className="empty-state">{emptyMsg}</div>;
    return (
      <>
        {list.map(e => (
          <EmailCard
            key={e.id}
            email={e}
            expanded={expandedEmail === e.id}
            onToggle={() => setExpandedEmail(expandedEmail === e.id ? null : e.id)}
            onProcessed={() => fetchData()}
          />
        ))}
        {showLoadMore && emailOffset < emailTotal && (
          <button
            onClick={loadMoreEmails}
            disabled={loadingMore}
            style={{
              width: '100%', marginTop: 12, padding: '10px 0',
              background: 'transparent', border: '1px solid var(--input-border)',
              borderRadius: 8, color: 'var(--muted-soft)', cursor: 'pointer', fontSize: 13,
            }}
          >
            {loadingMore ? 'Loading…' : `Load more (${emailTotal - emailOffset} remaining)`}
          </button>
        )}
      </>
    );
  }

  const NAV = [
    { key: 'inbox'     as Tab, icon: '📬', label: 'All Emails',         count: emails.length,    color: 'var(--blue-soft)' },
    { key: 'requests'  as Tab, icon: '📦', label: 'Customer Requests',  count: requests.length,  color: 'var(--blue)' },
    { key: 'ratecards' as Tab, icon: '💰', label: 'Rate Cards',         count: rateCards.length, color: 'var(--green)' },
    { key: 'shipments' as Tab, icon: '🚢', label: 'Shipments & RFQs',   count: jobs.length,      color: 'var(--yellow)' },
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
                {n.count}
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
            <SummaryBar emails={emails} jobs={jobs} />

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
                  {inboxLoading && emails.length === 0
                    ? <div className="empty-state">⏳ Loading emails from inbox…</div>
                    : inboxError && emails.length === 0
                      ? <div className="empty-state" style={{color:'var(--red)'}}>⚠️ {inboxError}</div>
                      : <div className="email-list">{renderEmails(emails, 'No emails in inbox.', true)}</div>
                  }
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
                    {renderEmails(requests, 'No customer requests found in the current inbox batch.')}
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
                    {renderEmails(rateCards, 'No rate cards found yet. They appear here once agents reply to your RFQs.')}
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
                      {(['rfqs_sent','quotes_received','approved'] as const).map(s => {
                        const si = statusInfo(s);
                        return <span key={s} className="status-legend" style={{ color: si.color, background: si.bg }}>{si.label}</span>;
                      })}
                    </div>
                  </div>
                  {jobs.length === 0
                    ? <div className="empty-state">No shipments yet. Process a customer email to get started.</div>
                    : <div className="shipments-grid">
                        {[...jobs]
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
