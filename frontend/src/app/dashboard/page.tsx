"use client";

import { useState, useEffect, useCallback } from 'react';
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
  shipment_weight_kg: number;
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
  const m = raw.match(/^"?([^"<]+)"?\s*</);
  if (m) return m[1].trim();
  return raw.split('@')[0];
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
    rfqs_sent:       { label: 'RFQs Sent',       color: '#60a5fa', bg: '#1e3a5f22', desc: 'Waiting for agents to reply' },
    awaiting_quotes: { label: 'Awaiting Quotes',  color: '#fbbf24', bg: '#78350f22', desc: 'Agents notified, quotes pending' },
    quotes_received: { label: 'Quotes Received',  color: '#34d399', bg: '#065f4622', desc: 'Quotes in — ready to compare' },
    approved:        { label: 'Approved',          color: '#a78bfa', bg: '#4c1d9522', desc: 'Shipment confirmed' },
  };
  return map[status] ?? { label: status, color: '#94a3b8', bg: '#1e293b', desc: '' };
}

function assessInfo(a: string): { label: string; color: string } {
  const map: Record<string, { label: string; color: string }> = {
    within_range:   { label: '✅ Fair price',    color: '#34d399' },
    above_expected: { label: '⚠️ Above average', color: '#fbbf24' },
    below_expected: { label: '🎉 Below average', color: '#60a5fa' },
  };
  return map[a] ?? { label: a || '—', color: '#64748b' };
}

/* ─── Label Pill ─────────────────────────────────────────────── */
function LabelPill({ label, confidence }: { label?: string; confidence?: number }) {
  if (!label) return <span className="pill pill-gray">Unclassified</span>;
  if (label === 'customer_requirement')
    return <span className="pill pill-blue">📦 Customer Request {confidence ? `· ${Math.round(confidence * 100)}%` : ''}</span>;
  if (label === 'quotation_rate_card')
    return <span className="pill pill-green">💰 Rate Card {confidence ? `· ${Math.round(confidence * 100)}%` : ''}</span>;
  return <span className="pill pill-gray">📋 General</span>;
}

/* ─── Email Card ─────────────────────────────────────────────── */
function EmailCard({ email, expanded, onToggle }: {
  email: Email;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div className={`ecard ${expanded ? 'ecard-open' : ''}`} onClick={onToggle}>
      <div className="ecard-top">
        <div className="ecard-avatar">{senderName(email.sender)[0].toUpperCase()}</div>
        <div className="ecard-meta">
          <div className="ecard-sender">{senderName(email.sender)}</div>
          <div className="ecard-addr">{email.sender.match(/<(.+)>/)?.[1] ?? email.sender}</div>
        </div>
        <div className="ecard-right">
          <LabelPill label={email.label} confidence={email.label_confidence} />
        </div>
      </div>
      <div className="ecard-subject">{email.subject}</div>
      {expanded && (
        <div className="ecard-body">
          <pre className="ecard-pre">{email.body?.slice(0, 1200)}{email.body?.length > 1200 ? '\n…' : ''}</pre>
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
  const si = statusInfo(job.status);

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
        <span className="chip">⚖️ {job.shipment_weight_kg.toLocaleString()} kg</span>
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
              {quotes.map(q => {
                const ai = assessInfo(q.ai_assessment);
                return (
                  <div key={q.id} className={`q-card ${q.is_selected ? 'q-selected' : ''}`}>
                    <div className="q-top">
                      <span className="q-agent">{q.agent_name}</span>
                      {q.is_selected && <span className="q-winner">✅ Selected</span>}
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
        <div className="sum-val" style={{ color: '#60a5fa' }}>{requests}</div>
        <div className="sum-lbl">Customer Requests</div>
      </div>
      <div className="sum-divider" />
      <div className="sum-item">
        <div className="sum-val" style={{ color: '#34d399' }}>{rateCards}</div>
        <div className="sum-lbl">Rate Cards</div>
      </div>
      <div className="sum-divider" />
      <div className="sum-item">
        <div className="sum-val" style={{ color: '#fbbf24' }}>{activeJobs}</div>
        <div className="sum-lbl">Active Shipments</div>
      </div>
      <div className="sum-divider" />
      <div className="sum-item">
        <div className="sum-val" style={{ color: '#a78bfa' }}>{quotesIn}</div>
        <div className="sum-lbl">Ready to Approve</div>
      </div>
    </div>
  );
}

/* ─── Main Page ───────────────────────────────────────────────── */
export default function Dashboard() {
  const [tab, setTab] = useState<Tab>('inbox');
  const [emails, setEmails] = useState<Email[]>([]);
  const [jobs, setJobs] = useState<RFQJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [expandedEmail, setExpandedEmail] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const fetchData = useCallback(async (showRefreshing = false) => {
    if (showRefreshing) setRefreshing(true);

    // Jobs load first — unblocks UI immediately
    try {
      const jr = await fetch(`${API_BASE}/jobs`);
      if (jr.ok) setJobs(await jr.json());
      setLastRefresh(new Date());
      setError('');
    } catch {
      setError('Cannot reach the backend. Make sure the server is running.');
    } finally {
      setLoading(false);      // show UI as soon as jobs arrive
      setRefreshing(false);
    }

    // Inbox fetches in background — IMAP is slow, don't block render
    try {
      const ir = await fetch(`${API_BASE}/fetch-inbox?limit=20`);
      if (ir.ok) { const d = await ir.json(); setEmails(d.emails ?? []); }
    } catch { /* non-critical */ }
  }, []);

  useEffect(() => {
    fetchData();
    const t = setInterval(() => fetchData(), 60_000);
    return () => clearInterval(t);
  }, [fetchData]);

  const requests  = emails.filter(e => e.label === 'customer_requirement');
  const rateCards = emails.filter(e => e.label === 'quotation_rate_card');

  function renderEmails(list: Email[], emptyMsg: string) {
    if (list.length === 0) return <div className="empty-state">{emptyMsg}</div>;
    return list.map(e => (
      <EmailCard
        key={e.id}
        email={e}
        expanded={expandedEmail === e.id}
        onToggle={() => setExpandedEmail(expandedEmail === e.id ? null : e.id)}
      />
    ));
  }

  const NAV = [
    { key: 'inbox'     as Tab, icon: '📬', label: 'All Emails',         count: emails.length,    color: '#60a5fa' },
    { key: 'requests'  as Tab, icon: '📦', label: 'Customer Requests',  count: requests.length,  color: '#3b82f6' },
    { key: 'ratecards' as Tab, icon: '💰', label: 'Rate Cards',         count: rateCards.length, color: '#22c55e' },
    { key: 'shipments' as Tab, icon: '🚢', label: 'Shipments & RFQs',   count: jobs.length,      color: '#f59e0b' },
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
                  {emails.length === 0
                    ? <div className="empty-state">⏳ Loading emails from inbox…</div>
                    : <div className="email-list">{renderEmails(emails, 'No emails.')}</div>
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
