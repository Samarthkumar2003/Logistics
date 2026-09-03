"use client";

import { useState, useEffect, useRef } from 'react';
import Link from 'next/link';
import { apiFetch } from '@/lib/api';

const DESKS = {
  intake:  { left: 90,  top: 265 },
  history: { left: 90,  top: 445 },
  rfq:     { left: 380, top: 350 },
  quote:   { left: 610, top: 265 },
};

const COLORS: Record<string, { hair: string; shirt: string; pants: string; skin: string }> = {
  intake:  { hair: '#1e3a5f', shirt: '#3b82f6', pants: '#1e293b', skin: '#f5d0a9' },
  history: { hair: '#5c3a1e', shirt: '#f8f8f8', pants: '#6b4423', skin: '#f5d0a9' },
  rfq:     { hair: '#1a1a1a', shirt: '#ef4444', pants: '#1e293b', skin: '#d4a76a' },
  quote:   { hair: '#2d1b4e', shirt: '#a855f7', pants: '#1e293b', skin: '#f5d0a9' },
};


interface InboxEmail { id: string; sender: string; subject: string; body: string; label?: string; label_confidence?: number; label_method?: string; received_at?: string; }

function formatReceived(iso?: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleString([], { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

/* Label badge styling, keyed by label.
 *
 * `pending` matters: it means the classifier never managed to label the email,
 * not that the email is unremarkable. It used to fall through to the `general`
 * branch of a chain of ternaries here, which is exactly the mislabel the
 * pending state exists to prevent — an unclassified enquiry that looks like a
 * confident "general" is an RFQ nobody sends and nobody notices. */
const LABEL_STYLE: Record<string, { bg: string; fg: string; border: string; text: string }> = {
  customer_requirement: { bg: '#1e3a5f', fg: '#60a5fa', border: '#3b82f6', text: '📦 Customer Req' },
  quotation_rate_card:  { bg: '#1a3a1a', fg: '#4ade80', border: '#22c55e', text: '💰 Rate Card' },
  pending:              { bg: '#3a2a0a', fg: '#fbbf24', border: '#f59e0b', text: '⏳ Pending' },
  general:              { bg: '#2a1a3a', fg: '#a78bfa', border: '#7c3aed', text: '📋 General' },
};

function labelStyle(label?: string) {
  return LABEL_STYLE[label ?? ''] ?? LABEL_STYLE.general;
}
interface ShipmentDetails { origin: string; destination: string; weight_kg: number; commodity: string; mode: string; }
interface HistoryMatch { commodity: string; agent_used: string; rate_paid: number; transit_time_days: number; similarity: number; }
interface DraftEmail { vendor_name: string; subject: string; body: string; vendor_email?: string; }
interface ProcessResult { job_id: string; shipment: ShipmentDetails; history_matches: HistoryMatch[]; drafts: DraftEmail[]; }

interface AgentContacted { agent_name: string; email: string; source: string; }
interface SendResult { vendor_name: string; status: string; }

interface AutomationLastRun {
  run_at: string; emails_scanned: number; new_emails: number;
  customer_requirements: number; quotation_rate_cards: number;
  general: number; errors: number; duration_seconds: number;
  customer_emails?: { id: string; subject: string; sender: string; confidence: number; method: string }[];
}
interface AutomationStatus {
  enabled: boolean; schedule: string; next_run: string | null;
  processed_total: number; last_run: AutomationLastRun | null;
}
interface IngestStatus {
  running: boolean;
}
interface ProcessEmailResult {
  reference: string;
  shipment: ShipmentDetails;
  agents_contacted: AgentContacted[];
  send_results: SendResult[];
}

interface RFQJob {
  reference: string;
  shipment_origin: string;
  shipment_destination: string;
  shipment_mode: string;
  shipment_commodity: string;
  status: string;
  agents_contacted: string[];
  created_at: string;
}



interface StepData {
  intake: { left: number; top: number };
  history: { left: number; top: number };
  rfq: { left: number; top: number };
  quote: { left: number; top: number };
  walking: string[];
  bubbles: Record<string, string>;
  logs: string[];
  duration: number;
  panel: { agent: string; title: string; content: React.ReactNode };
}

function buildFlow(result: ProcessResult, email: InboxEmail): StepData[] {
  const { shipment, history_matches, drafts } = result;
  const D = DESKS;
  return [
    {
      intake: D.intake, history: D.history, rfq: D.rfq, quote: D.quote,
      walking: [], bubbles: { intake: "New email detected!" },
      logs: [`Intake: Reading email from ${email.sender}`], duration: 4000,
      panel: { agent: 'intake', title: '📥 Incoming Email', content: (
        <div className="detail-block">
          <div className="detail-label">From: {email.sender}</div>
          <div className="detail-label" style={{ marginTop: 4 }}>Subject: {email.subject}</div>
          <pre className="detail-code" style={{ marginTop: 8 }}>{email.body}</pre>
        </div>
      )},
    },
    {
      intake: D.intake, history: D.history, rfq: D.rfq, quote: D.quote,
      walking: [], bubbles: { intake: "Parsed into structured JSON!" },
      logs: ["Intake: Extracted structured data via gpt-4o-mini."], duration: 4000,
      panel: { agent: 'intake', title: '🧠 Extracted Data', content: (
        <div>
          <div className="detail-block">
            <div className="detail-label">Input Email (truncated)</div>
            <pre className="detail-code" style={{ maxHeight: '60px', overflow: 'hidden' }}>{email.body.substring(0, 120)}...</pre>
          </div>
          <div className="detail-label" style={{ marginTop: 12 }}>OpenAI Structured Output</div>
          <pre className="detail-code json">{JSON.stringify(shipment, null, 2)}</pre>
        </div>
      )},
    },
    {
      intake: { left: 90, top: 355 }, history: D.history, rfq: D.rfq, quote: D.quote,
      walking: ['intake'], bubbles: {},
      logs: ["Intake: Walking to History desk..."], duration: 2800,
      panel: { agent: 'intake', title: '🚶 Delivering Data', content: (
        <div className="detail-block">
          <div className="detail-label">Carrying Payload</div>
          <pre className="detail-code json">{JSON.stringify(shipment, null, 2)}</pre>
          <div className="detail-note">Walking to History Agent desk...</div>
        </div>
      )},
    },
    {
      intake: { left: 90, top: 355 }, history: D.history, rfq: D.rfq, quote: D.quote,
      walking: [], bubbles: { intake: `Route: ${shipment.origin} → ${shipment.destination}`, history: "Searching pgvector..." },
      logs: ["History: Running hybrid SQL + vector search..."], duration: 5000,
      panel: { agent: 'history', title: '🔍 Searching Database', content: (
        <div>
          <div className="detail-block">
            <div className="detail-label">Search Query</div>
            <pre className="detail-code">{`SELECT * FROM shipments\nWHERE origin = '${shipment.origin}'\n  AND destination = '${shipment.destination}'\n  AND mode = '${shipment.mode}'\nORDER BY cargo_embedding <=> query_vec\nLIMIT 5;`}</pre>
          </div>
          <div className="detail-note">Running cosine similarity on commodity embeddings...</div>
        </div>
      )},
    },
    {
      intake: { left: 90, top: 355 }, history: D.history, rfq: D.rfq, quote: D.quote,
      walking: [], bubbles: { history: `Found ${history_matches.length} match(es)!` },
      logs: [`History: ${history_matches.length} semantic match(es) found.`], duration: 4000,
      panel: { agent: 'history', title: '✅ Search Results', content: (
        <div>
          <div className="detail-label">Top Matches (Cosine Similarity)</div>
          {history_matches.length > 0 ? (
            <table className="detail-table">
              <thead><tr><th>Commodity</th><th>Agent</th><th>Rate</th><th>Transit</th><th>Score</th></tr></thead>
              <tbody>
                {history_matches.map((r, i) => (
                  <tr key={i}>
                    <td>{r.commodity}</td><td>{r.agent_used}</td>
                    <td>${r.rate_paid}</td><td>{r.transit_time_days}d</td>
                    <td>{r.similarity.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="detail-note">No historical matches. Using fallback vendor.</div>}
        </div>
      )},
    },
    {
      intake: D.intake, history: { left: 235, top: 395 }, rfq: D.rfq, quote: D.quote,
      walking: ['intake', 'history'], bubbles: {},
      logs: ["History: Walking to RFQ desk with results..."], duration: 2800,
      panel: { agent: 'history', title: '🚶 Delivering Results', content: (
        <div className="detail-block">
          <div className="detail-label">Payload to RFQ Agent</div>
          <pre className="detail-code json">{JSON.stringify({ vendors: history_matches.map(m => m.agent_used), shipment }, null, 2)}</pre>
        </div>
      )},
    },
    {
      intake: D.intake, history: { left: 235, top: 395 }, rfq: D.rfq, quote: D.quote,
      walking: [], bubbles: { history: `Draft for ${drafts.length} vendor(s).`, rfq: "Running gpt-4o..." },
      logs: ["RFQ: Generating vendor emails via gpt-4o..."], duration: 5000,
      panel: { agent: 'rfq', title: '✍️ Drafting Emails', content: (
        <div>
          {drafts.map((d, i) => (
            <div key={i} className="detail-block" style={{ marginTop: i > 0 ? 12 : 0 }}>
              <div className="detail-label">Draft #{i + 1} — {d.vendor_name}</div>
              <pre className="detail-code email">{d.body}</pre>
            </div>
          ))}
        </div>
      )},
    },
    {
      intake: D.intake, history: D.history, rfq: D.rfq, quote: { ...D.quote },
      walking: ['history'], bubbles: { rfq: `${drafts.length} email(s) sent!`, quote: "Waiting for replies..." },
      logs: ["RFQ: Emails sent automatically!", "Quote Parser: Monitoring inbox for replies."], duration: 5000,
      panel: { agent: 'rfq', title: '📧 RFQs Sent', content: null },
    },
  ];
}

/* ========== OFFICE LAYOUT ========== */
function OfficeLayout({ children, matrixTrigger }: { children?: React.ReactNode; matrixTrigger?: number }) {
  return (
    <div className="office">
      {/* Walls */}
      <div className="w-top" /><div className="w-top-edge" />
      <div className="w-left" /><div className="w-right" /><div className="w-bottom" />

      {/* Windows + clock on top wall */}
      <div className="f-window" style={{ left: 330, top: 8, width: 68, height: 34 }} />
      <div className="f-window" style={{ left: 510, top: 8, width: 68, height: 34 }} />
      <div className="f-clock" style={{ left: 460, top: 13 }} />

      {/* CEO cabin glass partitions */}
      <div className="f-glass-v" style={{ left: 258, top: 53, height: 162 }} />
      <div className="f-glass-h" style={{ left: 30, top: 213, width: 228 }} />

      {/* Meeting room glass partitions */}
      <div className="f-glass-v" style={{ left: 572, top: 53, height: 162 }} />
      <div className="f-glass-h" style={{ left: 572, top: 213, width: 273 }} />

      {/* Zone divider between top cabins and main floor */}
      <div className="zone-divider" style={{ top: 220 }} />

      {/* ── CEO Cabin ── */}
      <div className="f-exec-desk" style={{ left: 50, top: 68 }}>
        <div className="e-screen" /><div className="e-papers" /><div className="e-mug" />
      </div>
      <div className="f-exec-chair" style={{ left: 76, top: 148 }} />
      <div className="f-filing" style={{ left: 228, top: 65 }} />
      <div className="f-plant" style={{ left: 36, top: 68 }}><div className="leaf" /><div className="pot" /></div>
      <div className="zone-label" style={{ left: 38, top: 200 }}>CEO</div>

      {/* ── Reception ── */}
      <div className="f-reception" style={{ left: 305, top: 68, width: 145, height: 56 }} />
      <div className="f-couch" style={{ left: 318, top: 148, width: 100, height: 26 }} />
      <div className="f-plant" style={{ left: 268, top: 65 }}><div className="leaf" /><div className="pot" /></div>
      <div className="zone-label" style={{ left: 330, top: 200 }}>RECEPTION</div>

      {/* ── Bookshelf on wall between cabin and meeting room ── */}
      <div className="f-shelf-v2" style={{ left: 278, top: 57, width: 22, height: 38 }}>
        <div className="books">
          {['#ef4444','#3b82f6','#fbbf24'].map((c,i) => (
            <div key={i} className="book" style={{ height: `${[70,90,60][i]}%`, background: c }} />
          ))}
        </div>
      </div>

      {/* ── Meeting Room ── */}
      <div className="f-meeting-table" style={{ left: 600, top: 75 }} />
      {[0,1,2,3].map(i => <div key={`mt${i}`} className="f-m-chair" style={{ left: 608+i*44, top: 62 }} />)}
      {[0,1,2,3].map(i => <div key={`mb${i}`} className="f-m-chair" style={{ left: 608+i*44, top: 170 }} />)}
      <div className="f-m-chair" style={{ left: 818, top: 90 }} />
      <div className="f-m-chair" style={{ left: 818, top: 128 }} />
      <div className="f-whiteboard" style={{ left: 825, top: 58, width: 16, height: 65 }} />
      <div className="zone-label" style={{ left: 618, top: 200 }}>MEETING ROOM</div>

      {/* ── Area rugs ── */}
      <div className="f-rug" style={{ left: 52, top: 232, width: 188, height: 252, background: 'linear-gradient(135deg,rgba(192,80,48,0.35),rgba(160,48,32,0.35))' }} />
      <div className="f-rug" style={{ left: 332, top: 280, width: 175, height: 165, background: 'linear-gradient(135deg,rgba(32,128,112,0.35),rgba(16,96,80,0.35))' }} />
      <div className="f-rug" style={{ left: 568, top: 232, width: 188, height: 252, background: 'linear-gradient(135deg,rgba(80,48,128,0.35),rgba(48,32,96,0.35))' }} />

      {/* ── Agent desks (v2) ── */}
      {(['intake','history','rfq','quote'] as const).map(k => (
        <div key={k}>
          <div className="f-desk-v2" style={{ left: DESKS[k].left-15, top: DESKS[k].top+55 }}>
            <div className="d-monitor" /><div className="d-papers" /><div className="d-mug" />
          </div>
          <div className="f-chair-v2" style={{ left: DESKS[k].left+18, top: DESKS[k].top+120 }} />
        </div>
      ))}

      {/* ── Break area (bottom-left) ── */}
      <div className="f-counter" style={{ left: 35, top: 576, width: 148, height: 32 }} />
      <div className="f-coffee" style={{ left: 44, top: 579 }} />
      <div className="f-water" style={{ left: 88, top: 571 }} />
      <div className="zone-label" style={{ left: 38, top: 622 }}>BREAK</div>

      {/* ── Print/copy area (bottom-center) ── */}
      <div className="f-counter" style={{ left: 368, top: 576, width: 80, height: 26 }} />
      <div className="f-printer" style={{ left: 376, top: 579 }} />
      <div className="zone-label" style={{ left: 378, top: 620 }}>COPY</div>

      {/* ── Storage area (bottom-right) ── */}
      {[0,1,2,3].map(i => <div key={`fl${i}`} className="f-filing" style={{ left: 690+i*37, top: 566 }} />)}
      <div className="zone-label" style={{ left: 692, top: 625 }}>STORAGE</div>

      {/* ── Decorative plants ── */}
      <div className="f-plant-lg" style={{ left: 36, top: 534 }}>
        <div className="lg-leaf-b" /><div className="lg-leaf-t" /><div className="lg-pot" />
      </div>
      <div className="f-plant-lg" style={{ left: 820, top: 534 }}>
        <div className="lg-leaf-b" /><div className="lg-leaf-t" /><div className="lg-pot" />
      </div>
      <div className="f-plant" style={{ left: 550, top: 550 }}><div className="leaf" /><div className="pot" /></div>

      {children}

      {/* Matrix rain overlay — on top of everything */}
      <MatrixEffect trigger={matrixTrigger} />
    </div>
  );
}

/* ========== MAIN ========== */
type AppStatus = 'inbox' | 'fetching' | 'processing' | 'running' | 'sent' | 'jobs' | 'job_detail' | 'error';

export default function Office() {
  const [status, setStatus] = useState<AppStatus>('inbox');
  const [inbox, setInbox] = useState<InboxEmail[]>([]);
  const [selectedEmail, setSelectedEmail] = useState<InboxEmail | null>(null);
  const [apiResult, setApiResult] = useState<ProcessResult | null>(null);
  const [step, setStep] = useState(0);
  const [logs, setLogs] = useState<string[]>([]);
  const [errorMsg, setErrorMsg] = useState('');
  const [processResult, setProcessResult] = useState<ProcessEmailResult | null>(null);
  const [jobs, setJobs] = useState<RFQJob[]>([]);
  const [selectedJob, setSelectedJob] = useState<RFQJob | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [totalEmails, setTotalEmails] = useState(0);
  const [automationStatus, setAutomationStatus] = useState<AutomationStatus | null>(null);
  const [automationRunning, setAutomationRunning] = useState(false);
  const [ingestRunning, setIngestRunning] = useState(false);

  const PAGE_SIZE = 20;
  // Must match the `le=` ceiling on GET /fetch-inbox (backend/app/routes/inbox.py).
  // A restore deeper than this asks for a page the server now refuses with a 422,
  // which would blank the view on refresh; clamped, the user gets the newest 200
  // back with has_more set and pages on from there.
  const MAX_RESTORE = 200;
  // Persists how deep the inbox was scrolled so a browser refresh restores the
  // same emails (offset would otherwise reset to 0 and drop loaded pages).
  const INBOX_DEPTH_KEY = 'inboxLoadedCount';
  const INBOX_SEARCH_KEY = 'inboxSearch';

  // Mirror of inbox for stale-closure-free reads inside async loadInbox appends
  const inboxRef = useRef<InboxEmail[]>([]);
  useEffect(() => { inboxRef.current = inbox; }, [inbox]);

  // Auto-fetch inbox + automation status on load
  useEffect(() => {
    let savedCount = 0;
    let savedSearch = '';
    try {
      savedCount = parseInt(sessionStorage.getItem(INBOX_DEPTH_KEY) || '0', 10) || 0;
      savedSearch = sessionStorage.getItem(INBOX_SEARCH_KEY) || '';
    } catch { /* sessionStorage unavailable — fall back to first page */ }
    console.debug('[inbox] mount restore — savedCount', savedCount, 'savedSearch', JSON.stringify(savedSearch), '→ restore?', savedCount > PAGE_SIZE);
    if (savedSearch) setSearchQuery(savedSearch);
    // Restore previous depth in one request; otherwise load the first page.
    loadInbox(true, savedSearch || undefined, savedCount > PAGE_SIZE ? savedCount : undefined);
    fetchAutomationStatus();
  }, []);

  async function fetchAutomationStatus() {
    try {
      const res = await apiFetch(`/automation/status`);
      if (res.ok) setAutomationStatus(await res.json());
    } catch { /* non-critical */ }
  }

  async function runAutomationNow() {
    setAutomationRunning(true);
    try {
      // 202 = the scan was started in the background; 409 = one is already
      // running. Neither returns results, so poll the status endpoint instead
      // of treating the response body as a finished run.
      const res = await apiFetch(`/automation/run-now`, { method: 'POST' });
      if (!res.ok) {
        let detail = `Server error ${res.status}`;
        try { detail = (await res.json()).detail || detail; } catch { /* non-JSON */ }
        throw new Error(detail);
      }
      // Give the scan a moment to record its first results, then refresh.
      await new Promise(r => setTimeout(r, 3000));
      await fetchAutomationStatus();
      loadInbox(true, undefined, undefined, true);  // preserve scrolled depth
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Automation run failed');
    } finally {
      setAutomationRunning(false);
    }
  }

  /** Poll until the sweep finishes, capped so a stuck run can't hang the button. */
  async function waitForIngest(maxWaitMs = 60000) {
    const deadline = Date.now() + maxWaitMs;
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 2000));
      try {
        const res = await apiFetch(`/ingest/status`);
        if (!res.ok) return;
        const body: IngestStatus = await res.json();
        if (!body.running) return;
      } catch {
        return;  // status unreachable — stop waiting; the reload below still runs
      }
    }
  }

  async function refreshInbox() {
    // "Refresh" has to mean "go and get new mail". The inbox endpoints read
    // Supabase only, so on its own this button could never surface a message the
    // 5-minute scheduler had not already pulled — it re-rendered the same rows.
    setIngestRunning(true);
    try {
      const res = await apiFetch(`/ingest/run-now`, { method: 'POST' });
      // 409 = a sweep is already in flight. For this button that is success, not
      // an error: new mail is on its way, so wait on it like our own run.
      if (!res.ok && res.status !== 409) {
        let detail = `Server error ${res.status}`;
        try { detail = (await res.json()).detail || detail; } catch { /* non-JSON */ }
        throw new Error(detail);
      }
      // Show what is already stored first, so the list is never blank while the
      // sweep runs, then again once it has committed its rows.
      await loadInbox(true, undefined, undefined, true);
      await waitForIngest();
      await loadInbox(true, undefined, undefined, true);
    } catch (err) {
      // Both, or the message renders nowhere: errorMsg is only displayed under
      // status 'error'.
      setErrorMsg(err instanceof Error ? err.message : 'Refresh failed');
      setStatus('error');
    } finally {
      setIngestRunning(false);
    }
  }

  async function toggleAutomation(enabled: boolean) {
    try {
      await apiFetch(`/automation/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      setAutomationStatus(prev => prev ? { ...prev, enabled } : prev);
    } catch { /* non-critical */ }
  }

  async function loadInbox(reset = true, overrideSearch?: string, restoreCount?: number, preserveDepth = false) {
    // preserveDepth: a refresh/poll that should re-fetch the SAME depth the user
    // already scrolled to, not snap back to page 1. We derive the depth from the
    // current list and skip the empty-list wipe so the view doesn't flash.
    const effectiveRestore =
      preserveDepth && inboxRef.current.length > PAGE_SIZE
        ? inboxRef.current.length
        : restoreCount;
    if (reset && !preserveDepth) {
      setStatus('fetching');
      setErrorMsg('');
      setInbox([]);
    }
    try {
      // On a reset we either restore the previous scroll depth (one big request)
      // or load a single page. On "load more" we page from the current length.
      const restoreDepth =
        effectiveRestore && effectiveRestore > PAGE_SIZE
          ? Math.min(effectiveRestore, MAX_RESTORE)
          : PAGE_SIZE;
      const limit = reset ? restoreDepth : PAGE_SIZE;
      const offset = reset ? 0 : inbox.length;
      const search = overrideSearch !== undefined ? overrideSearch : searchQuery;
      const searchParam = search ? `&search=${encodeURIComponent(search)}` : '';
      const res = await apiFetch(`/fetch-inbox?limit=${limit}&offset=${offset}${searchParam}`);
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const data = await res.json();
      const newEmails: InboxEmail[] = data.emails || [];
      // Dedup by Message-ID across pages: each backend fetch dedups itself, but
      // IMAP ordering can shift between offset calls so the same id may appear
      // on two pages. Map keeps first occurrence, drops repeats.
      // Use the freshest list (inboxRef) to avoid a stale-closure race on append.
      const combined = reset ? newEmails : [...inboxRef.current, ...newEmails];
      const merged = Array.from(new Map(combined.map(e => [e.id, e])).values());
      setInbox(merged);
      setTotalEmails(data.total || 0);
      setHasMore(data.has_more || false);
      setStatus('inbox');
      // Persist depth + search OUTSIDE the state updater (updaters must be pure;
      // Strict Mode double-invokes them). This is the saved scroll depth.
      try {
        sessionStorage.setItem(INBOX_DEPTH_KEY, String(merged.length));
        sessionStorage.setItem(INBOX_SEARCH_KEY, search || '');
        console.debug('[inbox] saved depth', merged.length, 'search', JSON.stringify(search || ''));
      } catch { /* sessionStorage unavailable — refresh will reset to page 1 */ }
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to fetch inbox');
      setStatus('error');
    }
  }

  async function loadMore() {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    await loadInbox(false);
    setLoadingMore(false);
  }

  function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    loadInbox(true);
  }

  async function openSendRequest(email: InboxEmail) {
    // Load full body on demand (inbox list ships body: '')
    let body = email.body;
    if (!body) {
      try {
        const res = await apiFetch(`/email-body/${email.id}`);
        if (res.ok) body = (await res.json()).body || '';
      } catch { /* proceed with empty body — form stays blank */ }
    }
    try {
      sessionStorage.setItem('sendRequestEmail', JSON.stringify({
        id: email.id, sender: email.sender, subject: email.subject, body,
      }));
    } catch { /* sessionStorage unavailable — form opens blank */ }
    window.location.assign('/send-request');
  }

  async function handleSelectEmail(email: InboxEmail) {
    // Always route to the gated Send Request flow — never auto-process/auto-send.
    // (The old /process-email auto-send path has been removed.)
    return openSendRequest(email);
  }

  function runFlow(result: ProcessResult, email: InboxEmail) {
    const flow = buildFlow(result, email);
    const advance = (s: number) => {
      setStep(s);
      setLogs(prev => [...prev, ...flow[s].logs]);
      if (s < flow.length - 1) {
        setTimeout(() => advance(s + 1), flow[s].duration);
      } else {
        // Last step: auto-advance to 'sent' after its duration
        setTimeout(() => {
          setStatus('sent');
        }, flow[s].duration);
      }
    };
    advance(0);
  }

  async function loadJobs() {
    try {
      const res = await apiFetch(`/jobs`);
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const data: RFQJob[] = await res.json();
      setJobs(data);
      setStatus('jobs');
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to load jobs');
      setStatus('error');
    }
  }

  async function loadJobDetail(job: RFQJob) {
    setSelectedJob(job);
    try {
      const res = await apiFetch(`/jobs/${job.reference}`);
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const detail: RFQJob = await res.json();
      setSelectedJob(detail);
      setStatus('job_detail');
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to load job detail');
      setStatus('error');
    }
  }

  function handleBackToInbox() {
    setSelectedEmail(null);
    setApiResult(null);
    setStep(0);
    setLogs([]);
    setProcessResult(null);
    setSelectedJob(null);
    setSearchQuery('');
    loadInbox(true);
  }

  function handleBackToJobs() {
    setSelectedJob(null);
    loadJobs();
  }

  function getStatusBadgeColor(jobStatus: string): string {
    switch (jobStatus) {
      case 'rfqs_sent': return '#3b82f6';
      // Row written before the mail leaves; in flight, not yet delivered.
      case 'sending': return '#fbbf24';
      case 'quotes_received': return '#22c55e';
      case 'approved': return '#6b7280';
      // Not grey: the RFQ never left, so this job is not waiting on an agent.
      // Sharing 'approved' grey made a failed send read as a finished one.
      case 'send_failed': return '#dc2626';
      default: return '#6b7280';
    }
  }

  const activeId = status === 'running' && apiResult && selectedEmail
    ? (() => { const f = buildFlow(apiResult, selectedEmail); const c = f[step]; return Object.keys(c.bubbles)[0] || c.walking[0] || ''; })()
    : '';

  const [matrixTrigger, setMatrixTrigger] = useState(0);
  const prevActiveId = useRef('');
  useEffect(() => {
    if (activeId && activeId !== prevActiveId.current) {
      setMatrixTrigger(t => t + 1);
    }
    prevActiveId.current = activeId;
  }, [activeId]);

  const curStep = status === 'running' && apiResult && selectedEmail ? buildFlow(apiResult, selectedEmail)[step] : null;
  const flow = apiResult && selectedEmail ? buildFlow(apiResult, selectedEmail) : [];
  const isLastStep = status === 'running' && step === flow.length - 1;

  return (
    <div className="app">
      {/* SIDEBAR */}
      <aside className="sidebar">
        <div className="sidebar-head">
          <div className="live"></div>
          <h2>Bit Office</h2>
        </div>
        <div className="agents-panel">
          <SideAgent name="Intake AI" role="Email Parser" color={COLORS.intake.shirt} active={activeId === 'intake'} />
          <SideAgent name="History AI" role="Semantic Search" color={COLORS.history.shirt} active={activeId === 'history'} />
          <SideAgent name="RFQ Drafter" role="Email Writer" color={COLORS.rfq.shirt} active={activeId === 'rfq'} />
          <SideAgent name="Quote Parser" role="Quotation Reader" color={COLORS.quote.shirt} active={activeId === 'quote'} />
        </div>
        <div className="log-section">
          <h3>Activity Log</h3>
          {logs.length === 0
            ? <div className="log-entry"><div className="ts">--:--:--</div>Waiting...</div>
            : logs.map((l, i) => (
              <div key={i} className="log-entry">
                <div className="ts">{new Date().toLocaleTimeString()}</div>{l}
              </div>
            ))}
        </div>
        {/* AUTOMATION PANEL */}
        <div style={{ margin: '12px 0 0', padding: '10px 12px', background: '#0f172a', borderRadius: 8, border: '1px solid #1e293b' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <span style={{ color: '#94a3b8', fontSize: 11, fontWeight: 700, letterSpacing: 1 }}>AUTO SCAN</span>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
              <span style={{ fontSize: 10, color: automationStatus?.enabled ? '#4ade80' : '#6b7280' }}>
                {automationStatus?.enabled ? 'ON' : 'OFF'}
              </span>
              <input type="checkbox" checked={automationStatus?.enabled ?? true}
                onChange={e => toggleAutomation(e.target.checked)}
                style={{ cursor: 'pointer' }} />
            </label>
          </div>
          <div style={{ fontSize: 10, color: '#475569', marginBottom: 6 }}>
            {automationStatus?.schedule ?? 'Daily at 07:00 UTC'}
          </div>
          {automationStatus?.last_run && (
            <div style={{ fontSize: 10, color: '#64748b', marginBottom: 6, lineHeight: 1.6 }}>
              <div>Last: {new Date(automationStatus.last_run.run_at).toLocaleString()}</div>
              <div style={{ display: 'flex', gap: 8, marginTop: 3 }}>
                <span style={{ color: '#60a5fa' }}>📦 {automationStatus.last_run.customer_requirements}</span>
                <span style={{ color: '#4ade80' }}>💰 {automationStatus.last_run.quotation_rate_cards}</span>
                <span style={{ color: '#94a3b8' }}>✉ {automationStatus.last_run.new_emails} new</span>
              </div>
              {(automationStatus.last_run.customer_emails?.length ?? 0) > 0 && (
                <div style={{ marginTop: 6 }}>
                  <div style={{ color: '#93c5fd', fontSize: 10, marginBottom: 3 }}>Detected customer emails:</div>
                  {automationStatus.last_run.customer_emails!.slice(0, 3).map((e, i) => (
                    <div key={i} style={{ color: '#475569', fontSize: 10, marginBottom: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      · {e.subject || e.sender}
                    </div>
                  ))}
                  {automationStatus.last_run.customer_emails!.length > 3 && (
                    <div style={{ color: '#475569', fontSize: 10 }}>+{automationStatus.last_run.customer_emails!.length - 3} more</div>
                  )}
                </div>
              )}
            </div>
          )}
          <button onClick={runAutomationNow} disabled={automationRunning}
            style={{ width: '100%', padding: '5px 0', fontSize: 11, background: automationRunning ? '#1e293b' : '#1e3a5f', color: automationRunning ? '#475569' : '#93c5fd', border: '1px solid #3b82f6', borderRadius: 4, cursor: automationRunning ? 'not-allowed' : 'pointer' }}>
            {automationRunning ? 'Scanning...' : 'Run Now'}
          </button>
          <div style={{ fontSize: 10, color: '#334155', marginTop: 6 }}>
            Next: {automationStatus?.next_run ? new Date(automationStatus.next_run).toLocaleString() : '—'}
          </div>
        </div>

        <div className="sidebar-footer">
          <button onClick={status === 'inbox' ? refreshInbox : handleBackToInbox}
            disabled={status === 'inbox' && ingestRunning}>
            {status === 'inbox'
              ? (ingestRunning ? 'Fetching mail...' : 'Refresh Inbox')
              : 'Back to Inbox'}
          </button>
          <Link href="/dashboard" style={{
            display: 'block', marginTop: 8, padding: '10px',
            fontFamily: "'Press Start 2P', cursive", fontSize: '0.5rem',
            textAlign: 'center', textDecoration: 'none',
            background: 'rgba(99,102,241,0.12)', color: '#818cf8',
            border: '1px solid rgba(99,102,241,0.3)', letterSpacing: 1,
            transition: 'all 0.2s',
          }}>
            📊 Dashboard
          </Link>
        </div>
      </aside>

      {/* OFFICE */}
      <OfficeLayout matrixTrigger={matrixTrigger}>
        <PixelChar pos={curStep?.intake ?? DESKS.intake} colors={COLORS.intake} label="INTAKE AI"
          bubble={curStep?.bubbles?.intake} walking={curStep?.walking.includes('intake') ?? false}
          active={activeId === 'intake'} />
        <PixelChar pos={curStep?.history ?? DESKS.history} colors={COLORS.history} label="HISTORY AI"
          bubble={curStep?.bubbles?.history} walking={curStep?.walking.includes('history') ?? false}
          active={activeId === 'history'} />
        <PixelChar pos={curStep?.rfq ?? DESKS.rfq} colors={COLORS.rfq} label="RFQ DRAFTER"
          bubble={curStep?.bubbles?.rfq} walking={curStep?.walking.includes('rfq') ?? false}
          active={activeId === 'rfq'} />
        <PixelChar pos={curStep?.quote ?? DESKS.quote} colors={COLORS.quote} label="QUOTE PARSER"
          bubble={curStep?.bubbles?.quote} walking={curStep?.walking.includes('quote') ?? false}
          active={activeId === 'quote'} />
      </OfficeLayout>

      {/* RIGHT PANEL */}
      <aside className="detail-panel">
        {/* INBOX VIEW */}
        {(status === 'inbox' || status === 'fetching' || status === 'error') && (
          <>
            <div className="detail-header">
              <div className="detail-agent-dot" style={{ background: '#3b82f6' }}></div>
              <h3>📬 Inbox {totalEmails > 0 && <span style={{ fontSize: 11, color: '#64748b', fontWeight: 400 }}>({inbox.length}/{totalEmails})</span>}</h3>
            </div>
            <div className="detail-content">
              {/* Search bar */}
              <form onSubmit={handleSearch} style={{ display: 'flex', gap: 6, marginBottom: 12 }}>
                <input
                  type="text"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder="Search emails..."
                  style={{
                    flex: 1, padding: '7px 10px', fontSize: 12, fontFamily: 'monospace',
                    background: '#0d1117', border: '1px solid #2a2a3a', borderRadius: 5,
                    color: '#e2e8f0', outline: 'none',
                  }}
                />
                <button type="submit" style={{
                  padding: '7px 12px', fontSize: 11, fontWeight: 600,
                  background: '#3b82f6', color: 'white', border: 'none', borderRadius: 5, cursor: 'pointer',
                }}>Search</button>
                {searchQuery && (
                  <button type="button" onClick={() => { setSearchQuery(''); loadInbox(true, ''); }} style={{
                    padding: '7px 8px', fontSize: 11, background: 'transparent',
                    color: '#94a3b8', border: '1px solid #334155', borderRadius: 5, cursor: 'pointer',
                  }}>X</button>
                )}
              </form>

              {status === 'fetching' && <div style={{ color: '#94a3b8', fontFamily: 'monospace', fontSize: 12 }}>Loading inbox...</div>}
              {status === 'error' && <div style={{ color: '#ef4444', fontSize: 12 }}>Error: {errorMsg}</div>}
              {status === 'inbox' && inbox.length === 0 && (
                <div style={{ color: '#94a3b8', fontSize: 12 }}>No emails found{searchQuery ? ` for "${searchQuery}"` : ''}.</div>
              )}
              {status === 'inbox' && inbox.map(email => (
                <div key={email.id} className="detail-block" style={{ marginBottom: 10 }}>
                  {/* Label badge row */}
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                    <span style={{
                      fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 10,
                      background: labelStyle(email.label).bg,
                      color: labelStyle(email.label).fg,
                      border: `1px solid ${labelStyle(email.label).border}`,
                      textTransform: 'uppercase', letterSpacing: '0.05em',
                    }}>
                      {labelStyle(email.label).text}
                    </span>
                    <span style={{ fontSize: 9, color: '#475569' }}>
                      {/* No confidence or method for pending — there was no
                          successful call to attribute one to. */}
                      {email.label === 'pending'
                        ? 'retrying shortly'
                        : `${email.label_confidence ? `${Math.round(email.label_confidence * 100)}%` : ''} ${email.label_method ? `via ${email.label_method}` : ''}`}
                    </span>
                    {/* Correction dropdown */}
                    <select
                      style={{ marginLeft: 'auto', fontSize: 9, background: '#0d1117', color: '#64748b', border: '1px solid #2a2a3a', borderRadius: 4, padding: '1px 4px' }}
                      defaultValue=""
                      onChange={async (e) => {
                        const corrected = e.target.value;
                        if (!corrected) return;
                        await apiFetch(`/feedback`, {
                          method: 'POST',
                          headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({
                            email_id: email.id,
                            email_subject: email.subject,
                            email_body: email.body,
                            email_sender: email.sender,
                            predicted_label: email.label || 'general',
                            corrected_label: corrected,
                            confidence: email.label_confidence || 0,
                          }),
                        });
                        e.target.value = '';
                      }}
                      onClick={(e) => e.stopPropagation()}
                    >
                      <option value="">Correct label...</option>
                      <option value="customer_requirement">Customer Req</option>
                      <option value="quotation_rate_card">Rate Card</option>
                      <option value="general">General</option>
                    </select>
                  </div>
                  <div style={{ cursor: 'pointer' }} onClick={() => email.label === 'customer_requirement' ? openSendRequest(email) : handleSelectEmail(email)}>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                      <div className="detail-label" style={{ fontSize: 11, flex: 1, minWidth: 0 }}>{email.sender}</div>
                      {email.received_at && (
                        <span style={{ fontSize: 10, color: '#475569', whiteSpace: 'nowrap' }}>
                          🕐 {formatReceived(email.received_at)}
                        </span>
                      )}
                    </div>
                    <div style={{ color: '#e2e8f0', fontSize: 13, margin: '4px 0' }}>{email.subject}</div>
                    <div style={{ color: '#64748b', fontSize: 11 }}>{email.body.substring(0, 80)}...</div>
                    <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
                      {email.label === 'customer_requirement' ? (
                        <button
                          onClick={(e) => { e.stopPropagation(); e.preventDefault(); openSendRequest(email); }}
                          style={{
                            fontSize: 11, padding: '4px 10px', fontWeight: 600,
                            background: '#3b82f6', color: 'white', border: '1px solid #3b82f6',
                            borderRadius: 5, cursor: 'pointer', marginTop: 0,
                          }}
                        >
                          ✉️ Send RFQs
                        </button>
                      ) : (
                        <button className="approve-btn" style={{ fontSize: 11, padding: '4px 10px', marginTop: 0 }}>
                          Process this email
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              ))}
              {/* Load more button */}
              {status === 'inbox' && hasMore && (
                <button onClick={loadMore} disabled={loadingMore} style={{
                  width: '100%', padding: 10, marginTop: 8, fontSize: 12, fontWeight: 600,
                  background: loadingMore ? '#1e293b' : '#0d1117', color: '#94a3b8',
                  border: '1px solid #2a2a3a', borderRadius: 6, cursor: loadingMore ? 'default' : 'pointer',
                }}>
                  {loadingMore ? 'Loading...' : `Load more (${totalEmails - inbox.length} remaining)`}
                </button>
              )}
            </div>
          </>
        )}

        {/* PROCESSING SPINNER */}
        {status === 'processing' && (
          <>
            <div className="detail-header">
              <div className="detail-agent-dot" style={{ background: '#fbbf24' }}></div>
              <h3>Running Pipeline...</h3>
            </div>
            <div className="detail-content" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <div style={{ textAlign: 'center', color: '#94a3b8', fontFamily: 'monospace' }}>
                <div style={{ fontSize: 28, marginBottom: 12 }}>⚙️</div>
                <div>Intake → History → RFQ</div>
              </div>
            </div>
          </>
        )}

        {/* RUNNING: show step panel */}
        {status === 'running' && curStep && (
          <>
            <div className="detail-header">
              <div className="detail-agent-dot" style={{ background: COLORS[curStep.panel.agent]?.shirt || '#666' }}></div>
              <h3>{isLastStep ? '📧 RFQs Sent' : curStep.panel.title}</h3>
            </div>
            <div className="detail-content">
              {isLastStep && apiResult ? (
                <div style={{ textAlign: 'center', paddingTop: 40 }}>
                  <div style={{ fontSize: 24, marginBottom: 12 }}>📧</div>
                  <div style={{ color: '#22c55e', fontFamily: 'monospace', marginBottom: 8 }}>RFQs sent automatically!</div>
                  <div style={{ color: '#94a3b8', fontFamily: 'monospace', fontSize: 11 }}>Finalizing...</div>
                </div>
              ) : curStep.panel.content}
            </div>
            <div className="detail-step-indicator">Step {step + 1} / {flow.length}</div>
          </>
        )}

        {/* SENT: RFQs sent successfully */}
        {status === 'sent' && processResult && (
          <>
            <div className="detail-header">
              <div className="detail-agent-dot" style={{ background: '#22c55e' }}></div>
              <h3>RFQs Sent Successfully</h3>
            </div>
            <div className="detail-content">
              <div className="detail-block approval">
                <div className="detail-label">Reference</div>
                <div style={{ color: '#e2e8f0', fontFamily: 'monospace', fontSize: 16, fontWeight: 700, marginBottom: 8 }}>
                  {processResult.reference}
                </div>
              </div>

              <div className="detail-block" style={{ marginTop: 12 }}>
                <div className="detail-label">Agents Contacted</div>
                {processResult.send_results.map((sr, i) => (
                  <div key={i} style={{
                    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                    padding: '6px 0', borderBottom: '1px solid #1e293b', fontSize: '0.75rem', color: '#94a3b8',
                  }}>
                    <span>{sr.vendor_name}</span>
                    <span style={{ color: sr.status === 'sent' ? '#22c55e' : '#ef4444' }}>
                      {sr.status === 'sent' ? '✓ Sent' : '✗ Failed'}
                    </span>
                  </div>
                ))}
                {processResult.agents_contacted.length > 0 && processResult.send_results.length === 0 && (
                  processResult.agents_contacted.map((ac, i) => (
                    <div key={i} style={{
                      display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                      padding: '6px 0', borderBottom: '1px solid #1e293b', fontSize: '0.75rem', color: '#94a3b8',
                    }}>
                      <span>{ac.agent_name}</span>
                      <span style={{ color: '#94a3b8' }}>{ac.email}</span>
                    </div>
                  ))
                )}
              </div>

              <button className="approve-btn" onClick={loadJobs}>View All Jobs</button>
              <button className="review-btn" onClick={handleBackToInbox}>Back to Inbox</button>
            </div>
          </>
        )}

        {/* JOBS DASHBOARD */}
        {status === 'jobs' && (
          <>
            <div className="detail-header">
              <div className="detail-agent-dot" style={{ background: '#3b82f6' }}></div>
              <h3>RFQ Jobs</h3>
            </div>
            <div className="detail-content">
              {jobs.length === 0 && (
                <div style={{ color: '#94a3b8', fontSize: 12 }}>No jobs found.</div>
              )}
              {jobs.map((job) => (
                <div key={job.reference} className="detail-block" style={{
                  cursor: 'pointer', marginBottom: 10, background: '#0d1117',
                  border: '1px solid #2a2a3a', borderRadius: 8, padding: 14,
                }} onClick={() => loadJobDetail(job)}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                    <div style={{ color: '#e2e8f0', fontFamily: 'monospace', fontSize: 13, fontWeight: 600 }}>
                      {job.reference}
                    </div>
                    <span style={{
                      background: getStatusBadgeColor(job.status),
                      color: 'white', padding: '2px 8px', borderRadius: 10,
                      fontSize: '0.65rem', fontWeight: 600,
                    }}>
                      {job.status.replace(/_/g, ' ')}
                    </span>
                  </div>
                  <div style={{ color: '#94a3b8', fontSize: 12 }}>
                    {job.shipment_origin} → {job.shipment_destination}
                  </div>
                  <div style={{ color: '#64748b', fontSize: 11, marginTop: 4 }}>
                    {new Date(job.created_at).toLocaleDateString()}
                  </div>
                </div>
              ))}
              <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                <button className="approve-btn" style={{ flex: 1 }} onClick={loadJobs}>Refresh</button>
                <button className="review-btn" style={{ flex: 1 }} onClick={handleBackToInbox}>Back to Inbox</button>
              </div>
            </div>
          </>
        )}

        {/* JOB DETAIL */}
        {status === 'job_detail' && selectedJob && (
          <>
            <div className="detail-header">
              <div className="detail-agent-dot" style={{ background: '#3b82f6' }}></div>
              <h3>{selectedJob.reference}</h3>
            </div>
            <div className="detail-content">
              {/* Shipment Summary */}
              <div className="detail-block approval">
                <div className="detail-label">Shipment</div>
                <div className="approval-row"><span>Route</span><span>{selectedJob.shipment_origin} → {selectedJob.shipment_destination}</span></div>
                <div className="approval-row"><span>Mode</span><span>{selectedJob.shipment_mode}</span></div>
                <div className="approval-row"><span>Commodity</span><span>{selectedJob.shipment_commodity}</span></div>
                <div className="approval-row"><span>Status</span>
                  <span style={{
                    background: getStatusBadgeColor(selectedJob.status),
                    color: 'white', padding: '2px 8px', borderRadius: 10,
                    fontSize: '0.65rem', fontWeight: 600,
                  }}>
                    {selectedJob.status.replace(/_/g, ' ')}
                  </span>
                </div>
              </div>

              {/* Agent replies live in the dashboard — this legacy view shows
                  the job summary only. Rates are no longer parsed, so there is
                  no table to render here. */}
              <Link href="/dashboard" className="approve-btn" style={{
                display: 'block', textAlign: 'center', textDecoration: 'none',
              }}>
                View replies in dashboard →
              </Link>

              {errorMsg && status === 'job_detail' && (
                <div style={{ color: '#ef4444', fontSize: 12, marginTop: 8 }}>{errorMsg}</div>
              )}

              <button className="review-btn" onClick={handleBackToJobs}>Back to Jobs</button>
            </div>
          </>
        )}
      </aside>
    </div>
  );
}

/* ========== SPRITE CONSTANTS ========== */
// 12-col × 22-row chibi RPG sprite at 4× scale = 48×88 canvas
// Matches pixel-agents reference: big head, chunky body, detailed face
const S  = 4;        // canvas px per art px
const CW = 12 * S;   // 48
const CH = 22 * S;   // 88

// Key: ' '=transparent  H=hair  D=hairDark  K=skin  E=eyeWhite  P=pupil  U=blush
//      N=neck  T=shirt  A=armShade  W=beltLight  L=beltDark  X=pants  Z=pantsShad  O=shoe
type SpriteFrame = string[];

function makeFrames(
  _hair: string, _skin: string, _shirt: string, _pants: string
): { idle: SpriteFrame; walk: SpriteFrame[] } {
  void [_hair, _skin, _shirt, _pants];

  // 14 rows = big head (0-8) + neck/torso/belt (9-13)
  const upper: SpriteFrame = [
    '   HHHHHHHH   ', //  0 hair crown
    '  DHHHHHHHHD  ', //  1 hair band (D=darker)
    '  KKKKKKKKKK  ', //  2 forehead
    ' KKKKKKKKKKKK ', //  3 upper face
    ' KKEPKKKPEKKK ', //  4 eyes (E=white, P=pupil)
    ' KKKKKKKKKKKK ', //  5 mid face
    ' KKUKKKKKKUKK ', //  6 blush cheeks (U=pink)
    ' KKKKKKKKKKKK ', //  7 lower face/smile
    '  KKKKKKKKKK  ', //  8 chin
    '   NTTTTTTN   ', //  9 neck+collar
    '  ATTTTTTTTAA ', // 10 shoulders
    '  ATTTTTTTTAA ', // 11 torso
    '  ATTTTTTTTAA ', // 12 lower torso
    '  WLLLLLLLW   ', // 13 belt
  ];

  // 8 rows = pants + shoes (two leg columns)
  const idle: string[] = [
    '   XXX  XXX   ', // 14
    '   XXX  XXX   ', // 15
    '   XXX  XXX   ', // 16
    '   ZXX  XXZ   ', // 17 inner-leg shadow
    '   OOO  OOO   ', // 18 shoe top
    '  OOOOO OOOOO ', // 19 shoe toe
    '  OOOOO OOOOO ', // 20
    '               ', // 21 ground clearance
  ];
  const walk1: string[] = [
    '    XXXXXX    ', // 14 legs crossing
    '  XXXXXX XXX  ', // 15
    '  ZZXXX  XXX  ', // 16
    '  ZZXXX  ZXZ  ', // 17
    '  OOOO   OOO  ', // 18
    ' OOOOO   OOOO ', // 19
    ' OOOOO   OOOO ', // 20
    '               ', // 21
  ];
  const walk3: string[] = [
    '    XXXXXX    ',
    '  XXX XXXXXX  ',
    '  XXX  XXXZZ  ',
    '  ZXZ  XXXZZ  ',
    '  OOO   OOOO  ',
    ' OOOO   OOOOO ',
    ' OOOO   OOOOO ',
    '               ',
  ];

  const mk = (legs: string[]): SpriteFrame => [...upper, ...legs];
  return {
    idle: mk(idle),
    walk: [mk(walk1), mk(idle), mk(walk3), mk(idle)],
  };
}

function drawSpriteFrame(
  ctx: CanvasRenderingContext2D, frame: SpriteFrame,
  hairC: string, skinC: string, shirtC: string, pantsC: string,
  bobY: number, active: boolean
) {
  // Build per-character palette
  const hex2rgb = (h: string) => {
    const n = parseInt(h.replace('#',''), 16);
    return [(n>>16)&255, (n>>8)&255, n&255] as [number,number,number];
  };
  const darken = (h: string, a: number) => {
    const [r,g,b] = hex2rgb(h);
    return `rgb(${Math.max(0,r-a)},${Math.max(0,g-a)},${Math.max(0,b-a)})`;
  };

  const pal: Record<string, string> = {
    H: hairC,
    D: darken(hairC, 45),
    K: skinC,
    E: '#f5f0ee',
    P: '#1a0e0e',
    U: '#f0a8a8',
    N: skinC,
    T: shirtC,
    A: darken(shirtC, 30),
    W: '#e8dfc0',
    L: '#2e1e0e',
    X: pantsC,
    Z: darken(pantsC, 35),
    O: '#1c1410',
  };

  ctx.clearRect(0, 0, CW, CH);

  // Warm yellow glow on active agent (matches wood floor aesthetic)
  if (active) {
    ctx.save();
    ctx.shadowBlur = 28;
    ctx.shadowColor = 'rgba(255,210,60,0.9)';
    ctx.fillStyle = 'rgba(255,200,50,0.12)';
    ctx.beginPath();
    ctx.ellipse(CW / 2, CH - 8 + bobY, CW * 0.6, 14, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  // Drop shadow on floor
  ctx.save();
  ctx.fillStyle = 'rgba(0,0,0,0.40)';
  ctx.beginPath();
  ctx.ellipse(CW / 2, CH - 2 + bobY, 20, 6, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();

  // Draw sprite pixels
  for (let row = 0; row < frame.length; row++) {
    const line = frame[row];
    for (let col = 0; col < 12; col++) {
      const ch = (line[col] ?? ' ');
      if (ch === ' ') continue;
      const color = pal[ch];
      if (!color) continue;
      ctx.fillStyle = color;
      ctx.fillRect(col * S, row * S + bobY, S, S);
    }
  }

  // 1-px highlight on top of each art-pixel block
  ctx.save();
  ctx.globalAlpha = 0.20;
  ctx.fillStyle = '#ffffff';
  for (let row = 0; row < frame.length; row++) {
    const line = frame[row];
    for (let col = 0; col < 12; col++) {
      if ((line[col] ?? ' ') === ' ') continue;
      ctx.fillRect(col * S, row * S + bobY, S, 1);
    }
  }
  ctx.restore();
}

/* ========== PIXEL CHARACTER (canvas-based) ========== */
function PixelChar({ pos, colors, label, bubble, walking, active }: {
  pos: { left: number; top: number };
  colors: { hair: string; shirt: string; pants: string; skin: string };
  label: string; bubble?: string; walking: boolean; active: boolean;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const frameRef = useRef(0);
  const bobRef = useRef(0);
  const bobDirRef = useRef(1);

  useEffect(() => {
    const frames = makeFrames(colors.hair, colors.skin, colors.shirt, colors.pants);
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d')!;
    if (!ctx) return;

    let rafId: number;
    let lastTime = 0;
    let elapsed = 0;

    function tick(now: number) {
      const dt = now - lastTime;
      lastTime = now;
      elapsed += dt;

      if (walking) {
        // Rhythmic bob tied to frame
        bobRef.current = Math.sin(now / 120) * 1.5;
      } else {
        // Idle slow breathe
        bobRef.current = Math.sin(now / 1100) * 0.7;
      }

      // Frame advance every 130ms while walking
      if (elapsed >= 130) {
        elapsed = 0;
        if (walking) {
          frameRef.current = (frameRef.current + 1) % 4;
        } else {
          frameRef.current = 0;
        }
      }

      const frame = walking ? frames.walk[frameRef.current] : frames.idle;
      drawSpriteFrame(ctx, frame, colors.hair, colors.skin, colors.shirt, colors.pants,
        Math.round(bobRef.current), active);
      rafId = requestAnimationFrame(tick);
    }

    rafId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafId);
  }, [walking, active, colors.hair, colors.shirt, colors.pants, colors.skin]);

  return (
    <div
      className="char-wrapper"
      style={{ left: pos.left, top: pos.top }}
    >
      {/* ToolOverlay card — appears above character when active */}
      {active && bubble && (
        <div className="tool-overlay-card">
          <div className="toc-name">{label}</div>
          <div className="toc-activity">{bubble}</div>
        </div>
      )}
      {/* fallback bubble when not active but bubble exists */}
      {!active && bubble && <div className="char-bubble">{bubble}</div>}
      <canvas ref={canvasRef} width={CW} height={CH} style={{ imageRendering: 'pixelated', display: 'block' }} />
      <div className="char-label" style={{ borderColor: active ? colors.shirt : 'rgba(96,48,255,0.4)', color: active ? colors.shirt : undefined }}>
        {label}
      </div>
    </div>
  );
}

/* ========== MATRIX RAIN EFFECT ========== */
function MatrixEffect({ trigger }: { trigger: number | undefined }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const runningRef = useRef(false);

  useEffect(() => {
    if (!trigger) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    if (runningRef.current) return;
    runningRef.current = true;

    const W = canvas.offsetWidth || canvas.width;
    const H = canvas.offsetHeight || canvas.height;
    canvas.width = W;
    canvas.height = H;

    const fontSize = 14;
    const cols = Math.floor(W / fontSize);
    const drops: number[] = Array(cols).fill(0).map(() => Math.random() * -H / fontSize);
    const speeds: number[] = Array(cols).fill(0).map(() => 0.5 + Math.random() * 1.5);
    // Purple-tinted matrix rain matching accent (#6030ff)
    const chars = 'アイウエオカキクケコサシスセソタナニヌネノ01ABCDEF▓▒░█';

    const totalMs = 1400;
    const fadeInMs = 350;
    const fadeOutMs = 400;
    const start = performance.now();

    let rafId: number;
    function draw(now: number) {
      const elapsed = now - start;
      if (elapsed > totalMs) {
        ctx!.clearRect(0, 0, W, H);
        runningRef.current = false;
        return;
      }

      let alpha = 1;
      if (elapsed < fadeInMs) alpha = elapsed / fadeInMs;
      else if (elapsed > totalMs - fadeOutMs) alpha = (totalMs - elapsed) / fadeOutMs;

      ctx!.fillStyle = `rgba(0,0,0,${0.14 * alpha})`;
      ctx!.fillRect(0, 0, W, H);

      ctx!.font = `bold ${fontSize}px monospace`;
      for (let i = 0; i < cols; i++) {
        const ch = chars[Math.floor(Math.random() * chars.length)];
        const bright = Math.random() > 0.88;
        const r = bright ? 200 : Math.floor(80 + Math.random() * 60);
        const g = bright ? 160 : Math.floor(30 + Math.random() * 40);
        const b = bright ? 255 : Math.floor(180 + Math.random() * 75);
        ctx!.fillStyle = bright
          ? `rgba(${r},${g},${b},${alpha})`
          : `rgba(${r},${g},${b},${alpha * 0.7})`;
        ctx!.fillText(ch, i * fontSize, drops[i] * fontSize);
        drops[i] += speeds[i];
        if (drops[i] * fontSize > H && Math.random() > 0.96) drops[i] = 0;
      }
      rafId = requestAnimationFrame(draw);
    }

    rafId = requestAnimationFrame(draw);
    return () => { cancelAnimationFrame(rafId); runningRef.current = false; };
  }, [trigger]);

  return (
    <canvas
      ref={canvasRef}
      style={{
        position: 'absolute', inset: 0, width: '100%', height: '100%',
        pointerEvents: 'none', zIndex: 50,
      }}
    />
  );
}

function SideAgent({ name, role, color, active }: { name: string; role: string; color: string; active: boolean }) {
  return (
    <div className={`agent-card ${active ? 'active' : ''}`}>
      <div className="agent-swatch" style={{ background: color }}></div>
      <div className="info"><div className="name">{name}</div><div className="role">{role}</div></div>
      <div className={`dot ${active ? 'on' : ''}`}></div>
    </div>
  );
}
