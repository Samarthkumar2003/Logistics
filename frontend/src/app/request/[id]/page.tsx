"use client";

import { useEffect, useState } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { apiFetch } from '@/lib/api';
import { groupByThread, type Reply } from '@/lib/replyThreads';
import { agentTypeLabel, statusInfo } from '@/lib/shipments';
import { ThreadCard } from '@/components/ThreadCard';

/* ─── Types ─────────────────────────────────────────────────────── */
interface CustomerEmail {
  provider_msg_id: string;
  sender: string;
  subject: string;
  body: string;
  received_at: string;
}
/** One RFQ: one agent, one reference, one status. */
interface Job {
  reference: string;
  agents_contacted: string[];
  agent_name: string;
  agent_email: string;
  /** Empty when the roster cannot say — rendered as unknown, never defaulted. */
  agent_category: string;
  status: string;
  reply_count: number;
  replied: boolean;
  created_at: string;
}
interface ShipmentFacts {
  origin: string;
  destination: string;
  mode: string;
  weight_kg: number | null;
  commodity: string;
  size: string;
}
interface RequestData {
  customer_email_id: string;
  customer_email: CustomerEmail | null;
  shipment: ShipmentFacts | null;
  jobs: Job[];
  replies: Reply[];
  agents_contacted: string[];
  // `agents` is how many were asked, `agents_replied` how many came back,
  // `replies` how many linked messages arrived, `thread_only` how many context
  // messages the thread pulled in that answered nothing.
  counts: { agents: number; replies: number; thread_only: number; agents_replied: number };
}

function fmt(iso?: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? '' : d.toLocaleString([], {
    day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function modeLabel(mode: string): string {
  const map: Record<string, string> = {
    sea_freight: '🚢 Sea Freight',
    air_freight: '✈️ Air Freight',
    road: '🚛 Road',
  };
  return map[mode] ?? mode;
}

const card: React.CSSProperties = {
  background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 10,
  padding: 18, marginBottom: 18,
};

const chip: React.CSSProperties = {
  fontSize: 12, padding: '4px 10px', borderRadius: 8,
  background: 'var(--overlay-2)', color: 'var(--muted-soft)',
  border: '1px solid var(--border)',
};

const cell: React.CSSProperties = {
  padding: '10px 8px', fontSize: 12, textAlign: 'left', verticalAlign: 'top',
  borderBottom: '1px solid var(--border)',
};

/**
 * What the Replies column says about one RFQ.
 *
 * "awaiting" is reserved for RFQs genuinely still waiting on somebody. An awarded
 * RFQ with no reply linked is not waiting — the shipment went to that agent — and
 * a failed send has nobody to wait for. Both used to read "awaiting", which put
 * the word on the winning row of a finished shipment.
 */
function replyCellText(job: Job): string {
  if (job.replied) return `${job.reply_count} message${job.reply_count === 1 ? '' : 's'}`;
  if (job.status === 'send_failed') return 'never sent';
  if (job.status === 'approved') return 'no reply linked';
  return 'awaiting';
}

function replyCellColour(job: Job): string {
  if (job.replied) return 'var(--green-soft)';
  if (job.status === 'send_failed') return 'var(--red)';
  // Decided, so neither a warning nor good news.
  if (job.status === 'approved') return 'var(--faint)';
  return 'var(--amber)';
}

/* ─── The agents we asked ───────────────────────────────────────── */
/**
 * Every agent on the shipment, side by side, and the only place it can be awarded.
 *
 * Awarding used to sit on an individual RFQ's card, which is the one view where the
 * choice cannot be made: there was nothing to compare it against. It belongs here.
 *
 * That move creates a hazard the old layout could not: seven Award buttons on one
 * screen. `approve` sends a real acceptance email and has no guard of its own
 * against a shipment being awarded twice, so two agents could each be told they
 * won. Hence both defences below — a confirmation naming the agent, and the other
 * rows locked once there is a winner.
 */
function AgentTable({ jobs, onAwarded }: { jobs: Job[]; onAwarded: (reference: string) => void }) {
  const [awarding, setAwarding] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);

  const winner = jobs.find(j => j.status === 'approved');

  async function award(job: Job) {
    const ok = window.confirm(
      `Award this shipment to ${job.agent_name || job.reference}?\n\n` +
      `An acceptance email is sent to ${job.agent_email || 'the agent'} immediately. ` +
      `This cannot be unsent.`
    );
    if (!ok) return;

    setAwarding(job.reference);
    setResult(null);
    try {
      const r = await apiFetch(`/jobs/${job.reference}/approve`, { method: 'POST' });
      const d = await r.json();
      if (r.ok) {
        onAwarded(job.reference);
        setResult({
          ok: true,
          message: d.acceptance_status === 'sent'
            ? `Awarded to ${d.agent_name} — acceptance sent`
            : `Awarded to ${d.agent_name}, but the acceptance email ${d.acceptance_status}`,
        });
      } else {
        // The backend leaves the job unchanged when the acceptance does not send,
        // so this is the whole outcome: nothing was recorded and nobody was told.
        setResult({ ok: false, message: d.detail ?? `Failed (${r.status})` });
      }
    } catch {
      setResult({ ok: false, message: 'Network error — nothing was sent' });
    } finally {
      setAwarding(null);
    }
  }

  return (
    <div style={card}>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 10 }}>
        AGENTS ASKED · {jobs.length}
      </div>

      {result && (
        <div style={{
          fontSize: 12, marginBottom: 10,
          color: result.ok ? 'var(--green-soft)' : 'var(--red)',
        }}>
          {result.ok ? '✅ ' : '❌ '}{result.message}
        </div>
      )}

      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {['Agent', 'Type', 'RFQ', 'Status', 'Sent', 'Replies', ''].map(h => (
                <th key={h} style={{
                  ...cell, fontSize: 10, textTransform: 'uppercase',
                  letterSpacing: 0.5, color: 'var(--dim)', fontWeight: 700,
                }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {jobs.map(job => {
              const si = statusInfo(job.status);
              const isWinner = job.status === 'approved';
              return (
                <tr key={job.reference}>
                  <td style={{ ...cell, fontWeight: 600, color: 'var(--text)' }}>
                    {job.agent_name || '(unnamed)'}
                    {job.agent_email && (
                      <div style={{ fontWeight: 400, color: 'var(--faint)', fontSize: 11 }}>
                        {job.agent_email}
                      </div>
                    )}
                  </td>
                  <td style={cell}>
                    <span style={{
                      ...chip,
                      // Unknown is muted rather than coloured like a real category:
                      // the roster cannot say what this agent is, and the cell
                      // should not imply otherwise.
                      color: job.agent_category ? 'var(--purple)' : 'var(--faint)',
                      background: job.agent_category ? 'var(--purple-tint)' : 'var(--overlay-2)',
                      borderColor: job.agent_category ? 'var(--purple-line)' : 'var(--border)',
                    }}>
                      {agentTypeLabel(job.agent_category)}
                    </span>
                  </td>
                  <td style={{ ...cell, fontFamily: 'monospace', fontSize: 11, color: 'var(--indigo)' }}>
                    {job.reference}
                  </td>
                  <td style={cell}>
                    <span style={{
                      fontSize: 11, fontWeight: 700, padding: '3px 9px', borderRadius: 20,
                      color: si.color, background: si.bg, whiteSpace: 'nowrap',
                    }}>{si.label}</span>
                  </td>
                  <td style={{ ...cell, color: 'var(--muted)', whiteSpace: 'nowrap' }}>
                    {fmt(job.created_at)}
                  </td>
                  <td style={{ ...cell, color: replyCellColour(job) }}>
                    {replyCellText(job)}
                  </td>
                  <td style={{ ...cell, textAlign: 'right' }}>
                    {isWinner ? (
                      <span style={{ fontSize: 11, color: 'var(--purple)', fontWeight: 700 }}>
                        🏆 Awarded
                      </span>
                    ) : winner ? (
                      // Locked, not hidden: the row still has to be readable for
                      // comparison, but a second acceptance is not one click away.
                      <button
                        disabled
                        title={`This shipment is already awarded to ${winner.agent_name}. Awarding again would tell a second agent they won.`}
                        style={{
                          fontSize: 11, padding: '5px 10px', borderRadius: 6,
                          border: '1px solid var(--border)', background: 'var(--overlay-1)',
                          color: 'var(--faint)', cursor: 'not-allowed', whiteSpace: 'nowrap',
                        }}
                      >Awarded elsewhere</button>
                    ) : (
                      <button
                        disabled={awarding !== null}
                        onClick={() => award(job)}
                        style={{
                          fontSize: 11, fontWeight: 600, padding: '5px 12px', borderRadius: 6,
                          border: 'none', whiteSpace: 'nowrap',
                          background: awarding ? 'var(--input-border)' : 'var(--green-solid)',
                          color: 'var(--on-accent)',
                          cursor: awarding ? 'default' : 'pointer',
                        }}
                      >{awarding === job.reference ? '⏳ Sending…' : 'Award'}</button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
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

  /** Reflect an award locally rather than refetching the whole page. */
  function markAwarded(reference: string) {
    setData(d => d && {
      ...d,
      jobs: d.jobs.map(j => j.reference === reference ? { ...j, status: 'approved' } : j),
    });
  }

  // Replies grouped by the agent whose RFQ they answer. `agent_name` comes from
  // that RFQ's job, so it always matches a row in the table above — including for
  // a follow-up that dropped the reference, which the backend credits by thread.
  const repliesByAgent: Record<string, Reply[]> = {};
  (data?.replies ?? []).forEach(r => {
    const key = r.agent_name || r.sender || 'Unknown';
    (repliesByAgent[key] ??= []).push(r);
  });
  const awaiting = (data?.jobs ?? [])
    .filter(j => !j.replied && j.status !== 'send_failed' && j.status !== 'approved')
    .map(j => j.agent_name || j.reference);

  // globals.css centres <body> as a fixed, non-scrolling flex panel (built for
  // the dashboard). This is a scrollable document page, so escape that.
  const outer: React.CSSProperties = {
    position: 'fixed', inset: 0, overflowY: 'auto',
    background: 'var(--bg)', color: 'var(--text)', fontFamily: 'system-ui, sans-serif',
  };
  // Colours below are tokens from app/theme.css, not literals, so this page
  // follows the theme the dashboard toggle stored. `themed` is what brings the
  // tokens into scope — without it every var() resolves to nothing.
  const wrap: React.CSSProperties = {
    maxWidth: 1080, margin: '0 auto', padding: '28px 20px', textAlign: 'left',
  };

  const ship = data?.shipment;

  return (
    <div className="themed" style={outer}>
    <div style={wrap}>
      <Link href="/dashboard" style={{ color: 'var(--blue-soft)', fontSize: 13, textDecoration: 'none' }}>
        ← Back to shipments
      </Link>

      {/* The route is the title. "Customer Request" told the reader nothing they
          could not see from the URL. */}
      <h1 style={{ fontSize: 20, fontWeight: 700, margin: '14px 0 10px' }}>
        {ship?.origin || ship?.destination
          ? <>🚢 {ship?.origin || '—'} <span style={{ color: 'var(--muted)' }}>→</span> {ship?.destination || '—'}</>
          : '📋 Customer Request'}
      </h1>

      {ship && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 20 }}>
          {ship.mode && <span style={chip}>{modeLabel(ship.mode)}</span>}
          {ship.commodity && <span style={chip}>📦 {ship.commodity}</span>}
          {ship.size && <span style={chip}>🧱 {ship.size}</span>}
          {ship.weight_kg != null && <span style={chip}>⚖️ {ship.weight_kg.toLocaleString()} kg</span>}
        </div>
      )}

      {status === 'loading' && <div style={{ color: 'var(--muted-soft)' }}>Loading…</div>}
      {status === 'error' && <div style={{ color: 'var(--red)', fontSize: 13 }}>{errorMsg}</div>}

      {status === 'ready' && data && (
        <>
          {/* Original customer email */}
          {data.customer_email ? (
            <div style={card}>
              <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 6 }}>ORIGINAL REQUEST</div>
              <div style={{ fontSize: 13, marginBottom: 4 }}>
                <span style={{ color: 'var(--muted-soft)' }}>From: </span>{data.customer_email.sender}
              </div>
              <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 10 }}>{data.customer_email.subject}</div>
              <div style={{
                fontSize: 12, color: 'var(--text-soft)', whiteSpace: 'pre-wrap', lineHeight: 1.6,
                background: 'var(--sunken)', border: '1px solid var(--border)', borderRadius: 6,
                padding: 12, maxHeight: 260, overflowY: 'auto',
              }}>{data.customer_email.body}</div>
            </div>
          ) : (
            <div style={{ ...card, color: 'var(--muted-soft)', fontSize: 13 }}>
              Original email not stored — showing agent replies only.
            </div>
          )}

          {/* Summary */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 24, margin: '4px 4px 18px', fontSize: 13 }}>
            <span><strong style={{ color: 'var(--purple)' }}>{data.counts.agents}</strong> agents asked</span>
            {/* Agents first, messages second. An agent who sends a rate and then a
                correction is one response, so "2 replies" alone overstated how
                many quotes there are to compare. */}
            <span><strong style={{ color: 'var(--green-soft)' }}>{data.counts.agents_replied}</strong> agents replied</span>
            <span><strong style={{ color: 'var(--green-soft)' }}>{data.counts.replies}</strong> replies received</span>
            <span><strong style={{ color: 'var(--amber)' }}>{awaiting.length}</strong> awaiting reply</span>
          </div>

          {/* Every agent side by side — and the only place this can be awarded */}
          {data.jobs.length > 0 && <AgentTable jobs={data.jobs} onAwarded={markAwarded} />}

          {/* Agent replies */}
          <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--purple)', margin: '24px 4px 10px' }}>
            💬 AGENT REPLIES
            {data.counts.thread_only > 0 && (
              <span style={{ color: 'var(--faint)', fontWeight: 400 }}>
                {' '}· {data.counts.thread_only} context message
                {data.counts.thread_only === 1 ? '' : 's'} pulled in by thread
              </span>
            )}
          </div>
          {data.replies.length === 0 && (
            <div style={{ ...card, color: 'var(--muted-soft)', fontSize: 13 }}>
              No replies linked yet. A reply is linked when the agent keeps the RFQ
              reference in the subject — one that dropped it is still in the inbox,
              just not shown here.
            </div>
          )}
          {Object.entries(repliesByAgent).map(([agent, replies]) => (
            <div key={agent} style={{ marginBottom: 20 }}>
              <div style={{ fontSize: 13, fontWeight: 600, margin: '0 4px 8px', color: 'var(--text)' }}>
                {agent}
                <span style={{ color: 'var(--muted)', fontWeight: 400 }}>
                  {' '}· {replies.length} {replies.length === 1 ? 'message' : 'messages'}
                </span>
              </div>
              {/* One card per Gmail thread, not per message: an agent who wrote
                  twice is one conversation, and the latest message is on top
                  because the second one is usually a correction. */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {groupByThread(replies).map(thread => (
                  <ThreadCard key={thread[0].thread_id || thread[0].id} messages={thread} />
                ))}
              </div>
            </div>
          ))}

          {/* Awaiting */}
          {awaiting.length > 0 && (
            <div style={card}>
              <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--amber)', marginBottom: 8 }}>⏳ AWAITING REPLY</div>
              <div style={{ fontSize: 12, color: 'var(--text-soft)' }}>{awaiting.join(', ')}</div>
            </div>
          )}
        </>
      )}
    </div>
    </div>
  );
}
