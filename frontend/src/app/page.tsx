"use client";

import { useState, useEffect } from 'react';

const API_BASE = 'http://localhost:8001';

const DESKS = {
  intake:  { left: 90,  top: 265 },
  history: { left: 90,  top: 445 },
  rfq:     { left: 380, top: 350 },
  quote:   { left: 610, top: 265 },
  price:   { left: 610, top: 445 },
};

const COLORS: Record<string, { hair: string; shirt: string; pants: string; skin: string }> = {
  intake:  { hair: '#1e3a5f', shirt: '#3b82f6', pants: '#1e293b', skin: '#f5d0a9' },
  history: { hair: '#5c3a1e', shirt: '#f8f8f8', pants: '#6b4423', skin: '#f5d0a9' },
  rfq:     { hair: '#1a1a1a', shirt: '#ef4444', pants: '#1e293b', skin: '#d4a76a' },
  quote:   { hair: '#2d1b4e', shirt: '#a855f7', pants: '#1e293b', skin: '#f5d0a9' },
  price:   { hair: '#1a3a1a', shirt: '#fbbf24', pants: '#3a2510', skin: '#d4a76a' },
};

interface InboxEmail { id: string; sender: string; subject: string; body: string; label?: string; label_confidence?: number; label_method?: string; }
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
  customer_emails: { id: string; subject: string; sender: string; confidence: number; method: string }[];
}
interface AutomationStatus {
  enabled: boolean; schedule: string; next_run: string | null;
  processed_total: number; last_run: AutomationLastRun | null;
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

interface Quotation {
  id: number;
  agent_name: string;
  agent_email: string;
  rate: number;
  currency: string;
  transit_time_days: number;
  validity: string;
  terms: string;
  ai_assessment: string;
  predicted_low: number;
  predicted_high: number;
  received_at: string;
  is_selected: boolean;
}

interface PricePrediction {
  predicted_low: number;
  predicted_high: number;
  confidence: string;
  explanation: string;
}

interface StepData {
  intake: { left: number; top: number };
  history: { left: number; top: number };
  rfq: { left: number; top: number };
  quote: { left: number; top: number };
  price: { left: number; top: number };
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
      intake: D.intake, history: D.history, rfq: D.rfq, quote: D.quote, price: D.price,
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
      intake: D.intake, history: D.history, rfq: D.rfq, quote: D.quote, price: D.price,
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
      intake: { left: 90, top: 355 }, history: D.history, rfq: D.rfq, quote: D.quote, price: D.price,
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
      intake: { left: 90, top: 355 }, history: D.history, rfq: D.rfq, quote: D.quote, price: D.price,
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
      intake: { left: 90, top: 355 }, history: D.history, rfq: D.rfq, quote: D.quote, price: D.price,
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
      intake: D.intake, history: { left: 235, top: 395 }, rfq: D.rfq, quote: D.quote, price: D.price,
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
      intake: D.intake, history: { left: 235, top: 395 }, rfq: D.rfq, quote: D.quote, price: D.price,
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
      intake: D.intake, history: D.history, rfq: D.rfq, quote: { ...D.quote }, price: { ...D.price },
      walking: ['history'], bubbles: { rfq: `${drafts.length} email(s) sent!`, quote: "Waiting for replies...", price: "Standing by..." },
      logs: ["RFQ: Emails sent automatically!", "Quote Parser: Monitoring inbox for replies.", "Price AI: Standing by for analysis."], duration: 5000,
      panel: { agent: 'rfq', title: '📧 RFQs Sent', content: null },
    },
  ];
}

/* ========== OFFICE LAYOUT ========== */
function OfficeLayout({ children }: { children?: React.ReactNode }) {
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
      <div className="f-rug" style={{ left: 52, top: 232, width: 188, height: 252, background: 'linear-gradient(135deg,#c05030,#a03020)' }} />
      <div className="f-rug" style={{ left: 332, top: 280, width: 175, height: 165, background: 'linear-gradient(135deg,#208070,#106050)' }} />
      <div className="f-rug" style={{ left: 568, top: 232, width: 188, height: 252, background: 'linear-gradient(135deg,#503080,#302060)' }} />

      {/* ── Agent desks (v2) ── */}
      {(['intake','history','rfq','quote','price'] as const).map(k => (
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
  const [quotations, setQuotations] = useState<Quotation[]>([]);
  const [prediction, setPrediction] = useState<PricePrediction | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<string>('');
  const [searchQuery, setSearchQuery] = useState('');
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [totalEmails, setTotalEmails] = useState(0);
  const [automationStatus, setAutomationStatus] = useState<AutomationStatus | null>(null);
  const [automationRunning, setAutomationRunning] = useState(false);

  const PAGE_SIZE = 20;

  // Auto-fetch inbox + automation status on load
  useEffect(() => {
    loadInbox();
    fetchAutomationStatus();
  }, []);

  async function fetchAutomationStatus() {
    try {
      const res = await fetch(`${API_BASE}/automation/status`);
      if (res.ok) setAutomationStatus(await res.json());
    } catch { /* non-critical */ }
  }

  async function runAutomationNow() {
    setAutomationRunning(true);
    try {
      const res = await fetch(`${API_BASE}/automation/run-now`, { method: 'POST' });
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      await fetchAutomationStatus();
      loadInbox(true);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Automation run failed');
    } finally {
      setAutomationRunning(false);
    }
  }

  async function toggleAutomation(enabled: boolean) {
    try {
      await fetch(`${API_BASE}/automation/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      setAutomationStatus(prev => prev ? { ...prev, enabled } : prev);
    } catch { /* non-critical */ }
  }

  async function loadInbox(reset = true, overrideSearch?: string) {
    if (reset) {
      setStatus('fetching');
      setErrorMsg('');
      setInbox([]);
    }
    try {
      const offset = reset ? 0 : inbox.length;
      const search = overrideSearch !== undefined ? overrideSearch : searchQuery;
      const searchParam = search ? `&search=${encodeURIComponent(search)}` : '';
      const res = await fetch(`${API_BASE}/fetch-inbox?limit=${PAGE_SIZE}&offset=${offset}${searchParam}`);
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const data = await res.json();
      const newEmails: InboxEmail[] = data.emails || [];
      if (reset) {
        setInbox(newEmails);
      } else {
        setInbox(prev => [...prev, ...newEmails]);
      }
      setTotalEmails(data.total || 0);
      setHasMore(data.has_more || false);
      setStatus('inbox');
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

  async function handleSelectEmail(email: InboxEmail) {
    setSelectedEmail(email);
    setStatus('processing');
    setLogs([]);
    setProcessResult(null);

    try {
      const res = await fetch(`${API_BASE}/process-email`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sender: email.sender, subject: email.subject, body: email.body }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || `Server error ${res.status}`);
      }
      const result = await res.json();

      // Store the process-email result for the "sent" view
      const peResult: ProcessEmailResult = {
        reference: result.reference,
        shipment: result.shipment,
        agents_contacted: result.agents_contacted || [],
        send_results: result.send_results || [],
      };
      setProcessResult(peResult);

      // Build a ProcessResult for the animation flow
      const animResult: ProcessResult = {
        job_id: result.reference || result.job_id || '',
        shipment: result.shipment,
        history_matches: result.history_matches || [],
        drafts: result.drafts || [],
      };
      setApiResult(animResult);
      setStep(0);
      setStatus('running');
      runFlow(animResult, email);
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Processing failed');
      setStatus('error');
    }
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
      const res = await fetch(`${API_BASE}/jobs`);
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
    setQuotations([]);
    setPrediction(null);
    setSelectedAgent('');
    try {
      const res = await fetch(`${API_BASE}/jobs/${job.reference}`);
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const detail: RFQJob = await res.json();
      setSelectedJob(detail);
      setStatus('job_detail');
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to load job detail');
      setStatus('error');
    }
  }

  async function checkQuotations() {
    if (!selectedJob) return;
    try {
      const res = await fetch(`${API_BASE}/jobs/${selectedJob.reference}/check-quotations`, {
        method: 'POST',
      });
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const data = await res.json();
      setQuotations(data.quotations || []);
      setPrediction(data.prediction || null);
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to check quotations');
    }
  }

  async function handleApproveQuotation() {
    if (!selectedJob || !selectedAgent) return;
    try {
      const res = await fetch(`${API_BASE}/jobs/${selectedJob.reference}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ selected_agent: selectedAgent }),
      });
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      await res.json();
      // Refresh job detail after approval
      loadJobDetail(selectedJob);
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Approval failed');
    }
  }

  function handleBackToInbox() {
    setSelectedEmail(null);
    setApiResult(null);
    setStep(0);
    setLogs([]);
    setProcessResult(null);
    setSelectedJob(null);
    setQuotations([]);
    setPrediction(null);
    setSelectedAgent('');
    setSearchQuery('');
    loadInbox(true);
  }

  function handleBackToJobs() {
    setSelectedJob(null);
    setQuotations([]);
    setPrediction(null);
    setSelectedAgent('');
    loadJobs();
  }

  function getStatusBadgeColor(jobStatus: string): string {
    switch (jobStatus) {
      case 'rfqs_sent': return '#3b82f6';
      case 'awaiting_quotes': return '#fbbf24';
      case 'quotes_received': return '#22c55e';
      case 'approved': return '#6b7280';
      default: return '#6b7280';
    }
  }

  function getAssessmentColor(assessment: string): string {
    switch (assessment) {
      case 'within_range': return '#22c55e';
      case 'above_expected': return '#f59e0b';
      case 'below_expected': return '#3b82f6';
      default: return '#6b7280';
    }
  }

  const activeId = status === 'running' && apiResult && selectedEmail
    ? (() => { const f = buildFlow(apiResult, selectedEmail); const c = f[step]; return Object.keys(c.bubbles)[0] || c.walking[0] || ''; })()
    : '';

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
          <SideAgent name="Price AI" role="Rate Predictor" color={COLORS.price.shirt} active={activeId === 'price'} />
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
              {automationStatus.last_run.customer_emails.length > 0 && (
                <div style={{ marginTop: 6 }}>
                  <div style={{ color: '#93c5fd', fontSize: 10, marginBottom: 3 }}>Detected customer emails:</div>
                  {automationStatus.last_run.customer_emails.slice(0, 3).map((e, i) => (
                    <div key={i} style={{ color: '#475569', fontSize: 10, marginBottom: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      · {e.subject || e.sender}
                    </div>
                  ))}
                  {automationStatus.last_run.customer_emails.length > 3 && (
                    <div style={{ color: '#475569', fontSize: 10 }}>+{automationStatus.last_run.customer_emails.length - 3} more</div>
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
          <button onClick={status === 'inbox' ? loadInbox : handleBackToInbox}>
            {status === 'inbox' ? 'Refresh Inbox' : 'Back to Inbox'}
          </button>
        </div>
      </aside>

      {/* OFFICE */}
      <OfficeLayout>
        <PixelChar pos={curStep?.intake ?? DESKS.intake} colors={COLORS.intake} label="INTAKE AI"
          bubble={curStep?.bubbles?.intake} walking={curStep?.walking.includes('intake') ?? false} />
        <PixelChar pos={curStep?.history ?? DESKS.history} colors={COLORS.history} label="HISTORY AI"
          bubble={curStep?.bubbles?.history} walking={curStep?.walking.includes('history') ?? false} />
        <PixelChar pos={curStep?.rfq ?? DESKS.rfq} colors={COLORS.rfq} label="RFQ DRAFTER"
          bubble={curStep?.bubbles?.rfq} walking={curStep?.walking.includes('rfq') ?? false} />
        <PixelChar pos={curStep?.quote ?? DESKS.quote} colors={COLORS.quote} label="QUOTE PARSER"
          bubble={curStep?.bubbles?.quote} walking={curStep?.walking.includes('quote') ?? false} />
        <PixelChar pos={curStep?.price ?? DESKS.price} colors={COLORS.price} label="PRICE AI"
          bubble={curStep?.bubbles?.price} walking={curStep?.walking.includes('price') ?? false} />
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
                      background: email.label === 'customer_requirement' ? '#1e3a5f'
                        : email.label === 'quotation_rate_card' ? '#1a3a1a' : '#2a1a3a',
                      color: email.label === 'customer_requirement' ? '#60a5fa'
                        : email.label === 'quotation_rate_card' ? '#4ade80' : '#a78bfa',
                      border: `1px solid ${email.label === 'customer_requirement' ? '#3b82f6'
                        : email.label === 'quotation_rate_card' ? '#22c55e' : '#7c3aed'}`,
                      textTransform: 'uppercase', letterSpacing: '0.05em',
                    }}>
                      {email.label === 'customer_requirement' ? '📦 Customer Req'
                        : email.label === 'quotation_rate_card' ? '💰 Rate Card'
                        : '📋 General'}
                    </span>
                    <span style={{ fontSize: 9, color: '#475569' }}>
                      {email.label_confidence ? `${Math.round(email.label_confidence * 100)}%` : ''} {email.label_method ? `via ${email.label_method}` : ''}
                    </span>
                    {/* Correction dropdown */}
                    <select
                      style={{ marginLeft: 'auto', fontSize: 9, background: '#0d1117', color: '#64748b', border: '1px solid #2a2a3a', borderRadius: 4, padding: '1px 4px' }}
                      defaultValue=""
                      onChange={async (e) => {
                        const corrected = e.target.value;
                        if (!corrected) return;
                        await fetch(`${API_BASE}/feedback`, {
                          method: 'POST',
                          headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({
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
                  <div style={{ cursor: 'pointer' }} onClick={() => handleSelectEmail(email)}>
                    <div className="detail-label" style={{ fontSize: 11 }}>{email.sender}</div>
                    <div style={{ color: '#e2e8f0', fontSize: 13, margin: '4px 0' }}>{email.subject}</div>
                    <div style={{ color: '#64748b', fontSize: 11 }}>{email.body.substring(0, 80)}...</div>
                    <div style={{ marginTop: 8 }}>
                      <button className="approve-btn" style={{ fontSize: 11, padding: '4px 10px', marginTop: 0 }}>
                        Process this email
                      </button>
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

              {/* Check Quotations Button */}
              <button className="approve-btn" onClick={checkQuotations}>
                Check for Quotations
              </button>

              {/* Price Prediction */}
              {prediction && (
                <div className="detail-block" style={{ marginTop: 14, background: '#0d1117', border: '1px solid #2a2a3a', borderRadius: 8, padding: 14 }}>
                  <div className="detail-label">AI Price Prediction</div>
                  <div style={{ color: '#e2e8f0', fontFamily: 'monospace', fontSize: 13, marginBottom: 6 }}>
                    ${prediction.predicted_low.toFixed(2)} - ${prediction.predicted_high.toFixed(2)}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                    <span style={{ fontSize: 11, color: '#94a3b8' }}>Confidence:</span>
                    <span style={{
                      background: prediction.confidence === 'high' ? '#22c55e' : prediction.confidence === 'medium' ? '#fbbf24' : '#ef4444',
                      color: 'white', padding: '2px 8px', borderRadius: 10,
                      fontSize: '0.65rem', fontWeight: 600,
                    }}>
                      {prediction.confidence}
                    </span>
                  </div>
                  <div style={{ color: '#64748b', fontSize: 11, fontStyle: 'italic' }}>
                    {prediction.explanation}
                  </div>
                </div>
              )}

              {/* Quotations Table */}
              {quotations.length > 0 && (
                <div style={{ marginTop: 14 }}>
                  <div className="detail-label">Quotations ({quotations.length})</div>
                  <table className="detail-table">
                    <thead>
                      <tr>
                        <th></th>
                        <th>Agent</th>
                        <th>Rate</th>
                        <th>Transit</th>
                        <th>Validity</th>
                        <th>Assessment</th>
                      </tr>
                    </thead>
                    <tbody>
                      {quotations.map((q) => (
                        <tr key={q.id}>
                          <td>
                            <input
                              type="radio"
                              name="select-quotation"
                              checked={selectedAgent === q.agent_name}
                              onChange={() => setSelectedAgent(q.agent_name)}
                              style={{ accentColor: '#3b82f6', cursor: 'pointer' }}
                            />
                          </td>
                          <td>{q.agent_name}</td>
                          <td>{q.currency} {q.rate.toFixed(2)}</td>
                          <td>{q.transit_time_days}d</td>
                          <td>{q.validity}</td>
                          <td>
                            <span style={{
                              background: getAssessmentColor(q.ai_assessment),
                              color: 'white', padding: '2px 6px', borderRadius: 10,
                              fontSize: '0.6rem', fontWeight: 600,
                            }}>
                              {q.ai_assessment.replace(/_/g, ' ')}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>

                  {/* Approve Selected */}
                  <button
                    className="approve-btn"
                    onClick={handleApproveQuotation}
                    style={{ opacity: selectedAgent ? 1 : 0.5, cursor: selectedAgent ? 'pointer' : 'not-allowed' }}
                    disabled={!selectedAgent}
                  >
                    Approve Selected
                  </button>
                </div>
              )}

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

function PixelChar({ pos, colors, label, bubble, walking }: {
  pos: { left: number; top: number }; colors: { hair: string; shirt: string; pants: string; skin: string };
  label: string; bubble?: string; walking: boolean;
}) {
  return (
    <div className={`char-wrapper ${walking ? 'walking' : 'idle'}`} style={{ left: pos.left, top: pos.top }}>
      {bubble && <div className="char-bubble">{bubble}</div>}
      <div className="char-body">
        <div className="char-shadow"></div>
        <div className="char-hair" style={{ background: colors.hair }}></div>
        <div className="char-head" style={{ background: colors.skin }}><div className="char-eyes"></div></div>
        <div className="char-torso" style={{ background: colors.shirt }}></div>
        <div className="char-arm-l" style={{ background: colors.shirt }}></div>
        <div className="char-arm-r" style={{ background: colors.shirt }}></div>
        <div className="char-leg-l" style={{ background: colors.pants }}></div>
        <div className="char-leg-r" style={{ background: colors.pants }}></div>
      </div>
      <div className="char-label">{label}</div>
    </div>
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
