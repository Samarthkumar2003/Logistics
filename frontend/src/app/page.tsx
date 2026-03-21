"use client";

import { useState, useEffect } from 'react';

const API_BASE = 'http://localhost:8001';

const DESKS = {
  intake:  { left: 140, top: 130 },
  history: { left: 140, top: 380 },
  rfq:     { left: 500, top: 260 },
};

const COLORS: Record<string, { hair: string; shirt: string; pants: string; skin: string }> = {
  intake:  { hair: '#1e3a5f', shirt: '#3b82f6', pants: '#1e293b', skin: '#f5d0a9' },
  history: { hair: '#5c3a1e', shirt: '#f8f8f8', pants: '#6b4423', skin: '#f5d0a9' },
  rfq:     { hair: '#1a1a1a', shirt: '#ef4444', pants: '#1e293b', skin: '#d4a76a' },
};

interface InboxEmail { id: string; sender: string; subject: string; body: string; }
interface ShipmentDetails { origin: string; destination: string; weight_kg: number; commodity: string; mode: string; }
interface HistoryMatch { commodity: string; agent_used: string; rate_paid: number; transit_time_days: number; similarity: number; }
interface DraftEmail { vendor_name: string; subject: string; body: string; }
interface ProcessResult { job_id: string; shipment: ShipmentDetails; history_matches: HistoryMatch[]; drafts: DraftEmail[]; }

interface StepData {
  intake: { left: number; top: number };
  history: { left: number; top: number };
  rfq: { left: number; top: number };
  walking: string[];
  bubbles: Record<string, string>;
  logs: string[];
  duration: number;
  panel: { agent: string; title: string; content: React.ReactNode };
}

function buildFlow(result: ProcessResult, email: InboxEmail): StepData[] {
  const { shipment, history_matches, drafts } = result;
  return [
    {
      intake: DESKS.intake, history: DESKS.history, rfq: DESKS.rfq,
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
      intake: DESKS.intake, history: DESKS.history, rfq: DESKS.rfq,
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
      intake: { left: 260, top: 380 }, history: DESKS.history, rfq: DESKS.rfq,
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
      intake: { left: 260, top: 380 }, history: DESKS.history, rfq: DESKS.rfq,
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
      intake: { left: 260, top: 380 }, history: DESKS.history, rfq: DESKS.rfq,
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
      intake: DESKS.intake, history: { left: 390, top: 260 }, rfq: DESKS.rfq,
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
      intake: DESKS.intake, history: { left: 390, top: 260 }, rfq: DESKS.rfq,
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
      intake: DESKS.intake, history: DESKS.history, rfq: DESKS.rfq,
      walking: ['history'], bubbles: { rfq: `${drafts.length} email(s) ready!` },
      logs: ["RFQ: Emails drafted.", "System: Awaiting approval."], duration: 99999,
      panel: { agent: 'rfq', title: '📧 Ready for Approval', content: null },
    },
  ];
}

/* ========== OFFICE LAYOUT ========== */
function OfficeLayout({ children }: { children?: React.ReactNode }) {
  return (
    <div className="office">
      <div className="w-top"></div><div className="w-top-edge"></div>
      <div className="w-left"></div><div className="w-right"></div><div className="w-bottom"></div>
      <div className="f-desk" style={{ left: DESKS.intake.left - 15, top: DESKS.intake.top + 55 }}><div className="screen"></div></div>
      <div className="f-desk" style={{ left: DESKS.history.left - 15, top: DESKS.history.top + 55 }}><div className="screen"></div></div>
      <div className="f-desk" style={{ left: DESKS.rfq.left - 15, top: DESKS.rfq.top + 55 }}><div className="screen"></div></div>
      <div className="f-chair" style={{ left: DESKS.intake.left + 20, top: DESKS.intake.top + 110 }}></div>
      <div className="f-chair" style={{ left: DESKS.history.left + 20, top: DESKS.history.top + 110 }}></div>
      <div className="f-chair" style={{ left: DESKS.rfq.left + 20, top: DESKS.rfq.top + 110 }}></div>
      <div className="f-bookshelf" style={{ left: 300, top: 56 }}>
        <div className="books">
          {['#ef4444','#3b82f6','#fbbf24','#22c55e','#a855f7'].map((c, i) => (
            <div key={i} className="book" style={{ height: `${[60,80,50,70,90][i]}%`, background: c }}></div>
          ))}
        </div>
      </div>
      <div className="f-plant" style={{ left: 55, top: 65 }}><div className="leaf"></div><div className="pot"></div></div>
      <div className="f-plant" style={{ left: 420, top: 500 }}><div className="leaf"></div><div className="pot"></div></div>
      {children}
    </div>
  );
}

/* ========== MAIN ========== */
type AppStatus = 'inbox' | 'fetching' | 'processing' | 'running' | 'done' | 'error';

export default function Office() {
  const [status, setStatus] = useState<AppStatus>('inbox');
  const [inbox, setInbox] = useState<InboxEmail[]>([]);
  const [selectedEmail, setSelectedEmail] = useState<InboxEmail | null>(null);
  const [apiResult, setApiResult] = useState<ProcessResult | null>(null);
  const [step, setStep] = useState(0);
  const [logs, setLogs] = useState<string[]>([]);
  const [errorMsg, setErrorMsg] = useState('');
  const [approveMsg, setApproveMsg] = useState('');

  // Auto-fetch inbox on load
  useEffect(() => {
    loadInbox();
  }, []);

  async function loadInbox() {
    setStatus('fetching');
    setErrorMsg('');
    try {
      const res = await fetch(`${API_BASE}/fetch-inbox?limit=5`);
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const emails: InboxEmail[] = await res.json();
      setInbox(emails);
      setStatus('inbox');
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to fetch inbox');
      setStatus('error');
    }
  }

  async function handleSelectEmail(email: InboxEmail) {
    setSelectedEmail(email);
    setStatus('processing');
    setLogs([]);
    setApproveMsg('');

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
      const result: ProcessResult = await res.json();
      setApiResult(result);
      setStep(0);
      setStatus('running');
      runFlow(result, email);
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
      }
    };
    advance(0);
  }

  async function handleApprove() {
    if (!apiResult) return;
    try {
      const res = await fetch(`${API_BASE}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: apiResult.job_id }),
      });
      const data = await res.json();
      setApproveMsg(`${data.approved_vendors?.length ?? 0} email(s) approved!`);
      setStatus('done');
    } catch {
      setApproveMsg('Approval failed.');
    }
  }

  function handleBack() {
    setStatus('inbox');
    setSelectedEmail(null);
    setApiResult(null);
    setStep(0);
    setLogs([]);
    setApproveMsg('');
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
        <div className="sidebar-footer">
          <button onClick={status === 'inbox' ? loadInbox : handleBack}>
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
      </OfficeLayout>

      {/* RIGHT PANEL */}
      <aside className="detail-panel">
        {/* INBOX VIEW */}
        {(status === 'inbox' || status === 'fetching' || status === 'error') && (
          <>
            <div className="detail-header">
              <div className="detail-agent-dot" style={{ background: '#3b82f6' }}></div>
              <h3>📬 Customer Inbox</h3>
            </div>
            <div className="detail-content">
              {status === 'fetching' && <div style={{ color: '#94a3b8', fontFamily: 'monospace', fontSize: 12 }}>Loading inbox...</div>}
              {status === 'error' && <div style={{ color: '#ef4444', fontSize: 12 }}>Error: {errorMsg}</div>}
              {status === 'inbox' && inbox.length === 0 && (
                <div style={{ color: '#94a3b8', fontSize: 12 }}>No emails found in inbox.</div>
              )}
              {status === 'inbox' && inbox.map(email => (
                <div key={email.id} className="detail-block" style={{ cursor: 'pointer', marginBottom: 10 }}
                  onClick={() => handleSelectEmail(email)}>
                  <div className="detail-label" style={{ fontSize: 11 }}>{email.sender}</div>
                  <div style={{ color: '#e2e8f0', fontSize: 13, margin: '4px 0' }}>{email.subject}</div>
                  <div style={{ color: '#64748b', fontSize: 11 }}>{email.body.substring(0, 80)}...</div>
                  <div style={{ marginTop: 8 }}>
                    <button className="approve-btn" style={{ fontSize: 11, padding: '4px 10px', marginTop: 0 }}>
                      Process this email
                    </button>
                  </div>
                </div>
              ))}
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
              <h3>{isLastStep ? '📧 Ready for Approval' : curStep.panel.title}</h3>
            </div>
            <div className="detail-content">
              {isLastStep && apiResult ? (
                <div className="detail-block approval">
                  <div className="detail-label">Summary</div>
                  <div className="approval-row"><span>Vendor Emails</span><span className="badge">{apiResult.drafts.length} Drafts</span></div>
                  <div className="approval-row"><span>Route</span><span>{apiResult.shipment.origin} → {apiResult.shipment.destination}</span></div>
                  <div className="approval-row"><span>Weight</span><span>{apiResult.shipment.weight_kg} kg</span></div>
                  <div className="approval-row"><span>Mode</span><span>{apiResult.shipment.mode.replace('_', ' ')}</span></div>
                  {apiResult.history_matches[0] && (
                    <div className="approval-row"><span>Top Vendor</span><span className="badge green">{apiResult.history_matches[0].agent_used}</span></div>
                  )}
                  {approveMsg
                    ? <div style={{ color: '#22c55e', fontSize: 12, marginTop: 12, textAlign: 'center' }}>{approveMsg}</div>
                    : <>
                        <button className="approve-btn" onClick={handleApprove}>Approve & Send All</button>
                        <button className="review-btn" onClick={handleBack}>Back to Inbox</button>
                      </>
                  }
                </div>
              ) : curStep.panel.content}
            </div>
            <div className="detail-step-indicator">Step {step + 1} / {flow.length}</div>
          </>
        )}

        {/* DONE */}
        {status === 'done' && (
          <>
            <div className="detail-header">
              <div className="detail-agent-dot" style={{ background: '#22c55e' }}></div>
              <h3>Done!</h3>
            </div>
            <div className="detail-content" style={{ textAlign: 'center', paddingTop: 40 }}>
              <div style={{ fontSize: 24, marginBottom: 12 }}>✅</div>
              <div style={{ color: '#22c55e', fontFamily: 'monospace', marginBottom: 20 }}>{approveMsg}</div>
              <button className="approve-btn" onClick={handleBack}>Back to Inbox</button>
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
