"use client";

import { useState, useEffect } from 'react';
import Link from 'next/link';
import {
  ManualNote, ManualRecipient, dedupeByEmail, describeSplit, mergeManualRecipients,
  splitManualTokens, tokenizeEmails,
} from './manualRecipients';
import {
  CONTAINER_OPTIONS, ContainerSelection, MAX_QUANTITY, MIN_QUANTITY, NO_CONTAINERS,
  containerSizeText, containerSummary, hasContainers, quantityOf, setManualText,
  setQuantity, toggleContainer, toggleManual,
} from './containerSize';
import { apiFetch } from '@/lib/api';

/* ─── Types ─────────────────────────────────────────────────────── */
interface Agent {
  id: number;
  agent_name: string;
  location: string;
  country: string;
  email: string;
  contact_person: string;
  specialty: string;
  category: string;
}

interface SourceEmail {
  id: string;
  sender: string;
  subject: string;
  body: string;
}

interface DraftPreview {
  reference: string;
  vendor_name: string;
  subject: string;
  body: string;
  note: string;
}

interface RFQJob {
  reference: string;
  agent_name: string;
  email: string;
  status: string;
}

interface SendRFQResponse {
  jobs: RFQJob[];
  total_sent: number;
}

interface PendingAttachment {
  filename: string;
  content_type: string;
  size_bytes: number;
  data_base64: string;
}

// Matches backend MAX_ATTACHMENT_TOTAL_BYTES (backend/app/routes/rfq.py) —
// kept in sync by eye since the limit is enforced server-side regardless.
const MAX_ATTACHMENT_TOTAL_BYTES = 15 * 1024 * 1024;

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      // "data:<mime>;base64,<data>" — only the payload after the comma.
      const result = reader.result as string;
      resolve(result.slice(result.indexOf(',') + 1));
    };
    reader.onerror = () => reject(reader.error ?? new Error('Failed to read file'));
    reader.readAsDataURL(file);
  });
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}


const CATEGORIES = [
  { key: 'CHA', label: 'CHA (Origin / Customs)' },
  { key: 'FREIGHT_FORWARDER', label: 'Freight Forwarders (Destination)' },
  { key: 'CARRIER', label: 'Carriers (Shipping Lines)' },
];

const inputStyle: React.CSSProperties = {
  width: '100%', padding: '9px 12px', fontSize: 13, fontFamily: 'monospace',
  background: 'var(--surface)', border: '1px solid var(--input-border)', borderRadius: 6,
  color: 'var(--text)', outline: 'none', boxSizing: 'border-box',
};

const labelStyle: React.CSSProperties = {
  fontSize: 11, fontWeight: 700, color: 'var(--muted-soft)', textTransform: 'uppercase',
  letterSpacing: '0.05em', marginBottom: 5, display: 'block',
};

/* ─── Multi-select dropdown ─────────────────────────────────────── */
function AgentMultiSelect({ label, agents, selected, onToggle }: {
  label: string;
  agents: Agent[];
  selected: Set<number>;
  onToggle: (id: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const count = agents.filter(a => selected.has(a.id)).length;

  return (
    <div style={{ position: 'relative' }}>
      <span style={labelStyle}>{label}</span>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        style={{
          ...inputStyle, textAlign: 'left', cursor: 'pointer',
          color: count > 0 ? 'var(--blue-soft)' : 'var(--muted)',
          border: `1px solid ${count > 0 ? 'var(--blue)' : 'var(--input-border)'}`,
        }}
      >
        {count > 0 ? `${count} selected` : `Select agents... (${agents.length} available)`} {open ? '▲' : '▼'}
      </button>
      {open && (
        <div style={{
          position: 'absolute', zIndex: 10, top: '100%', left: 0, right: 0, marginTop: 4,
          maxHeight: 220, overflowY: 'auto', background: 'var(--surface)',
          border: '1px solid var(--blue)', borderRadius: 6, padding: 4,
        }}>
          {agents.length === 0 && (
            <div style={{ padding: 8, fontSize: 12, color: 'var(--muted)' }}>No agents in this category</div>
          )}
          {agents.map(a => (
            <label key={a.id} style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px',
              fontSize: 12, color: 'var(--text)', cursor: 'pointer', borderRadius: 4,
              background: selected.has(a.id) ? 'var(--blue-tint-2)' : 'transparent',
            }}>
              <input
                type="checkbox"
                checked={selected.has(a.id)}
                onChange={() => onToggle(a.id)}
                style={{ accentColor: 'var(--blue)' }}
              />
              <span>
                {a.agent_name}
                <span style={{ color: 'var(--muted)', fontSize: 10 }}> — {a.location || a.country} · {a.email}</span>
              </span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

/* ─── Size / Container multi-select ─────────────────────────────── */
function ContainerMultiSelect({ selection, onChange }: {
  selection: ContainerSelection;
  onChange: (next: ContainerSelection) => void;
}) {
  const [open, setOpen] = useState(false);
  const chosen = hasContainers(selection);

  const row: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px',
    fontSize: 12, color: 'var(--text)', cursor: 'pointer', borderRadius: 4,
  };

  return (
    <div style={{ position: 'relative' }}>
      <span style={labelStyle}>Size / Container</span>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        style={{
          ...inputStyle, textAlign: 'left', cursor: 'pointer',
          color: chosen ? 'var(--blue-soft)' : 'var(--muted)',
          border: `1px solid ${chosen ? 'var(--blue)' : 'var(--input-border)'}`,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}
      >
        {containerSummary(selection)} {open ? '▲' : '▼'}
      </button>
      {open && (
        <div style={{
          position: 'absolute', zIndex: 10, top: '100%', left: 0, right: 0, marginTop: 4,
          background: 'var(--surface)', border: '1px solid var(--blue)', borderRadius: 6, padding: 4,
        }}>
          {CONTAINER_OPTIONS.map(o => {
            const on = selection.picked.includes(o);
            return (
              // The count sits outside the <label> on purpose: inside it, every
              // click that lands on the spinner would also toggle the tick.
              <div
                key={o}
                style={{ ...row, background: on ? 'var(--blue-tint-2)' : 'transparent', cursor: 'default' }}
              >
                <label style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={() => onChange(toggleContainer(selection, o))}
                    style={{ accentColor: 'var(--blue)' }}
                  />
                  <span>{o}</span>
                </label>
                {on && (
                  <>
                    <span style={{ fontSize: 10, color: 'var(--muted)' }}>QTY</span>
                    <input
                      type="number"
                      min={MIN_QUANTITY}
                      max={MAX_QUANTITY}
                      value={quantityOf(selection, o)}
                      onChange={e => onChange(setQuantity(selection, o, e.target.value))}
                      // Typed and pasted values walk past min/max, so the value is
                      // clamped again on the way out of the field.
                      onBlur={e => onChange(setQuantity(selection, o, e.target.value))}
                      aria-label={`Quantity of ${o}`}
                      style={{ ...inputStyle, width: 62, padding: '4px 6px', textAlign: 'center' }}
                    />
                  </>
                )}
              </div>
            );
          })}
          <label style={{
            ...row, background: selection.manualOn ? 'var(--blue-tint-2)' : 'transparent',
            borderTop: '1px solid var(--border)', borderRadius: 0, marginTop: 2,
          }}>
            <input
              type="checkbox"
              checked={selection.manualOn}
              onChange={() => onChange(toggleManual(selection))}
              style={{ accentColor: 'var(--blue)' }}
            />
            <span>Manual entry<span style={{ color: 'var(--muted)', fontSize: 10 }}> — anything not listed</span></span>
          </label>
          {selection.manualOn && (
            <input
              style={{ ...inputStyle, marginTop: 4 }}
              value={selection.manual}
              onChange={e => onChange(setManualText(selection, e.target.value))}
              placeholder="e.g. 2 x 45ft reefer — separate several with commas"
              autoFocus
            />
          )}
        </div>
      )}
    </div>
  );
}

/* ─── Page ──────────────────────────────────────────────────────── */
export default function SendRequestPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [sourceEmail, setSourceEmail] = useState<SourceEmail | null>(null);

  // Form fields
  // Size / Container is chosen from the fixed list (plus a manual option) and is
  // deliberately not prefilled from the customer email — see the extraction step
  // in init() below.
  const [containers, setContainers] = useState<ContainerSelection>(NO_CONTAINERS);
  const size = containerSizeText(containers);
  const [originPort, setOriginPort] = useState('');
  const [destPort, setDestPort] = useState('');
  const [commodity, setCommodity] = useState('');
  const [mode, setMode] = useState('sea_freight');
  const [weightKg, setWeightKg] = useState('');

  const [phase, setPhase] = useState<'loading' | 'ready' | 'sending' | 'sent' | 'error'>('loading');
  const [extracting, setExtracting] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [result, setResult] = useState<SendRFQResponse | null>(null);
  const [preview, setPreview] = useState<DraftPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  // The user-editable draft, seeded from the preview. This exact text is what
  // gets sent (one shared draft for all selected agents).
  const [editedSubject, setEditedSubject] = useState('');
  const [editedBody, setEditedBody] = useState('');
  // True when the editor was opened without an AI draft (generation failed, e.g.
  // LLM quota) — the user composes by hand. Send works identically.
  const [manualDraft, setManualDraft] = useState(false);

  // Ad-hoc recipients typed in by hand — agents not in the DB list. Merged with
  // the checkbox-selected agents at preview/send time. Keyed by lowercased email.
  const [manualAgents, setManualAgents] = useState<ManualRecipient[]>([]);
  const [manualEmailInput, setManualEmailInput] = useState('');
  const [manualNameInput, setManualNameInput] = useState('');
  const [manualNote, setManualNote] = useState<ManualNote | null>(null);

  // Files attached to the RFQ — same set goes to every selected agent.
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState('');
  const attachmentsTotalBytes = attachments.reduce((n, a) => n + a.size_bytes, 0);

  async function addAttachments(files: FileList | null) {
    if (!files || files.length === 0) return;
    setAttachmentError('');
    const incoming = Array.from(files);
    const incomingBytes = incoming.reduce((n, f) => n + f.size, 0);
    if (attachmentsTotalBytes + incomingBytes > MAX_ATTACHMENT_TOTAL_BYTES) {
      setAttachmentError(`Attachments would total ${formatBytes(attachmentsTotalBytes + incomingBytes)} — limit is ${formatBytes(MAX_ATTACHMENT_TOTAL_BYTES)}`);
      return;
    }
    try {
      const encoded = await Promise.all(incoming.map(async f => ({
        filename: f.name,
        content_type: f.type || 'application/octet-stream',
        size_bytes: f.size,
        data_base64: await fileToBase64(f),
      })));
      setAttachments(prev => [...prev, ...encoded]);
    } catch {
      setAttachmentError('Failed to read one or more files');
    }
  }

  function removeAttachment(filename: string) {
    setAttachments(prev => prev.filter(a => a.filename !== filename));
  }

  // Starter template built from the form, used when composing manually.
  function manualStarter() {
    const modeLabel = mode.replace('_', ' ');
    const subject = `Request for Quotation - ${modeLabel} ${originPort.trim()} to ${destPort.trim()}`.trim();
    const details = [
      `Origin: ${originPort.trim() || '-'}`,
      `Destination: ${destPort.trim() || '-'}`,
      `Mode: ${modeLabel}`,
      commodity.trim() ? `Commodity: ${commodity.trim()}` : null,
      size.trim() ? `Size: ${size.trim()}` : null,
      weightKg.trim() ? `Weight: ${weightKg.trim()} kg` : null,
    ].filter(Boolean);
    const body = [
      'Dear Team,', '',
      'We have the following shipment enquiry and would appreciate your best rate and transit time:', '',
      ...details, '',
      'Please share your best quotation at the earliest.', '', 'Best regards,',
    ].join('\n');
    return { subject, body };
  }

  function openManualDraft() {
    const { subject, body } = manualStarter();
    setEditedSubject(subject);
    setEditedBody(body);
    setManualDraft(true);
    setPreview({ reference: '', vendor_name: '', subject, body, note: '' });
  }

  useEffect(() => {
    async function init() {
      // 1. Load agents list
      try {
        const res = await apiFetch(`/agents`);
        if (!res.ok) {
          // Backend errors are JSON {detail: "..."} — surface the real reason
          let detail = `Server error ${res.status}`;
          try { detail = (await res.json()).detail || detail; } catch { /* non-JSON body */ }
          throw new Error(detail);
        }
        const data = await res.json();
        setAgents(data.agents || []);
      } catch (err: unknown) {
        setErrorMsg(err instanceof Error ? err.message : 'Failed to load agents');
        setPhase('error');
        return;
      }
      setPhase('ready');

      // 2. Prefill from the customer email via the intake agent
      let email: SourceEmail | null = null;
      try {
        const raw = sessionStorage.getItem('sendRequestEmail');
        if (raw) email = JSON.parse(raw);
      } catch { /* no prefill available */ }
      if (!email || !email.body) return;
      setSourceEmail(email);
      setExtracting(true);
      try {
        const res = await apiFetch(`/extract-details`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sender: email.sender, subject: email.subject, body: email.body }),
        });
        if (res.ok) {
          const data = await res.json();
          const s = data.shipment || {};
          // Size / Container is not taken from the email, deliberately, even if a
          // later extraction learns to guess one: a wrong box quotes the customer
          // for freight they never asked for, and the operator picks it below.
          setOriginPort(s.origin || '');
          setDestPort(s.destination || '');
          setCommodity(s.commodity || '');
          if (s.mode) setMode(s.mode);
          if (s.weight_kg !== null && s.weight_kg !== undefined) setWeightKg(String(s.weight_kg));
        }
      } catch { /* extraction failed — user fills the form manually */ }
      setExtracting(false);
    }
    init();
  }, []);

  function toggleAgent(id: number) {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function addManualEmail() {
    const tokens = tokenizeEmails(manualEmailInput);
    if (tokens.length === 0) return;

    // Classify first, then apply. This used to run *inside* the setManualAgents
    // updater, which broke it twice over: the tallies were read back before React
    // had run the updater, so no message ever reached the user; and the updater
    // pushed into arrays declared outside itself, so its development double-invoke
    // added every address twice, producing duplicate chips on one duplicate key.
    const split = splitManualTokens(tokens, agents, manualAgents, manualNameInput);

    if (split.added.length > 0) {
      // Pure updater — re-filters against `prev`, so running it twice is a no-op.
      setManualAgents(prev => mergeManualRecipients(prev, split.added));
    }
    // An address already in the agents table is what the checkbox list is for, so
    // select it. Skipping the token silently — the old behaviour — is why typing a
    // known agent's address looked like the Add button doing nothing at all, and it
    // got steadily more likely as the table grew past a hundred rows.
    if (split.onRoster.length > 0) {
      setSelected(prev => new Set([...prev, ...split.onRoster.map(a => a.id)]));
    }

    // Keep any invalid tokens in the box so the user can fix them; clear the rest.
    setManualEmailInput(split.invalid.join(', '));
    if (split.added.length > 0 || split.onRoster.length > 0) setManualNameInput('');
    setManualNote(describeSplit(split));
    setErrorMsg('');
  }

  function removeManualEmail(email: string) {
    setManualAgents(prev => prev.filter(m => m.email !== email));
  }

  // Recipients = checkbox-selected DB agents + hand-entered emails, one per address.
  // The dedup is not decoration. These are two independent states that can name the
  // same person, the count below is what the send button promises, and this list is
  // what gets posted — so any address appearing twice here is a second enquiry to a
  // vendor under a second reference, sent without anything on screen showing it.
  const selectedAgents = agents.filter(a => selected.has(a.id));
  const recipients = dedupeByEmail([
    ...selectedAgents.map(a => ({ agent_name: a.agent_name, email: a.email })),
    ...manualAgents,
  ]);
  const totalRecipients = recipients.length;

  async function handlePreview() {
    setErrorMsg('');
    if (!originPort.trim() || !destPort.trim()) { setErrorMsg('Origin and destination ports are required'); return; }

    // Preview addresses the first recipient (selected agent or hand-entered
    // email), or a placeholder if none picked yet.
    const sampleAgent = recipients.length > 0
      ? { agent_name: recipients[0].agent_name, email: recipients[0].email }
      : null;

    setPreviewing(true);
    try {
      const res = await apiFetch(`/preview-rfq`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          origin_port: originPort,
          destination_port: destPort,
          size,
          commodity,
          mode,
          weight_kg: weightKg.trim() ? parseFloat(weightKg) : null,
          agent: sampleAgent,
        }),
      });
      if (!res.ok) {
        let detail = `Server error ${res.status}`;
        try { detail = (await res.json()).detail || detail; } catch { /* non-JSON body */ }
        throw new Error(detail);
      }
      const data = await res.json();
      setManualDraft(false);
      setPreview(data);
      setEditedSubject(data.subject || '');
      setEditedBody(data.body || '');
    } catch (err: unknown) {
      // Draft generation failed (commonly LLM quota). Don't dead-end — open a
      // manual draft seeded from the form so the user can still compose & send.
      const msg = err instanceof Error ? err.message : 'Preview failed';
      setErrorMsg(`AI draft unavailable (${msg}) — compose the draft manually below.`);
      openManualDraft();
    }
    setPreviewing(false);
  }

  async function handleSend() {
    setErrorMsg('');
    if (recipients.length === 0) { setErrorMsg('Select at least one agent or add an email'); return; }
    if (!originPort.trim() || !destPort.trim()) { setErrorMsg('Origin and destination ports are required'); return; }

    setPhase('sending');
    try {
      const res = await apiFetch(`/send-rfq`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          origin_port: originPort,
          destination_port: destPort,
          size,
          commodity,
          mode,
          weight_kg: weightKg.trim() ? parseFloat(weightKg) : null,
          agents: recipients,
          customer_sender: sourceEmail?.sender || '',
          customer_subject: sourceEmail?.subject || '',
          customer_body: sourceEmail?.body || '',
          // Links every RFQ from this request back to the customer email (Phase 1).
          customer_email_id: sourceEmail?.id || '',
          // Send the edited draft verbatim when one has been previewed/edited.
          // Omitted otherwise, so the backend falls back to per-agent generation.
          ...(preview ? { edited_subject: editedSubject, edited_body: editedBody } : {}),
          attachments: attachments.map(({ filename, content_type, data_base64 }) => ({ filename, content_type, data_base64 })),
        }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || `Server error ${res.status}`);
      }
      setResult(await res.json());
      setPhase('sent');
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Send failed');
      setPhase('ready');
    }
  }

  const byCategory = (key: string) => agents.filter(a => a.category === key);

  return (
    // Colours below are tokens from app/theme.css, not literals, so this page
    // follows the theme the dashboard toggle stored. `themed` is what brings the
    // tokens into scope — without it every var() resolves to nothing.
    <div className="themed" style={{
      // globals.css pins `body { height:100vh; overflow:hidden }` for the fixed
      // dashboard, which clips this tall form and removes the page scrollbar.
      // Own the scroll here: fill the viewport and scroll internally.
      height: '100vh', overflowY: 'auto',
      // body is a centring flex container, so without this the root is only as
      // wide as its content and body's own background shows down both sides.
      width: '100%',
      background: 'var(--bg)', color: 'var(--text)',
      fontFamily: 'monospace', padding: '32px 24px',
    }}>
      <div style={{ maxWidth: 720, margin: '0 auto' }}>
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 24 }}>
          <Link href="/" style={{ color: 'var(--muted)', textDecoration: 'none', fontSize: 13 }}>← Inbox</Link>
          <h1 style={{ fontSize: 20, margin: 0 }}>✉️ Send RFQ Request</h1>
        </div>

        {phase === 'loading' && <div style={{ color: 'var(--muted-soft)', fontSize: 13 }}>Loading agents...</div>}

        {/* FAIL PAGE — shown when the dashboard cannot load at all */}
        {phase === 'error' && (
          <div style={{
            background: 'var(--surface)', border: '1px solid var(--red-line)', borderRadius: 10,
            padding: '36px 28px', textAlign: 'center',
          }}>
            <div style={{ fontSize: 40, marginBottom: 12 }}>🚧</div>
            <div style={{ fontSize: 17, fontWeight: 700, color: 'var(--red)', marginBottom: 8 }}>
              Could not open the Send Request dashboard
            </div>
            <div style={{
              fontSize: 12, color: 'var(--muted-soft)', marginBottom: 6, maxWidth: 480,
              margin: '0 auto 18px', lineHeight: 1.6, wordBreak: 'break-word',
            }}>
              {errorMsg || 'An unexpected error occurred while loading the agents list.'}
            </div>
            <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
              <button
                onClick={() => window.location.reload()}
                style={{
                  padding: '9px 20px', fontSize: 13, fontWeight: 600,
                  background: 'var(--blue)', color: 'var(--on-accent)', border: 'none',
                  borderRadius: 6, cursor: 'pointer',
                }}
              >
                ↻ Retry
              </button>
              <Link href="/" style={{
                padding: '9px 20px', fontSize: 13, fontWeight: 600,
                background: 'transparent', color: 'var(--muted-soft)',
                border: '1px solid var(--border-strong)', borderRadius: 6, textDecoration: 'none',
              }}>
                ← Back to Inbox
              </Link>
            </div>
          </div>
        )}

        {/* SENT — show RFQ reference + per-agent results */}
        {phase === 'sent' && result && (
          <div style={{ background: 'var(--surface)', border: '1px solid var(--green)', borderRadius: 8, padding: 20 }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--green-soft)', marginBottom: 10 }}>
              ✅ {result.total_sent} RFQ{result.total_sent === 1 ? '' : 's'} sent — one unique reference per agent
            </div>
            {result.jobs.map((j, i) => (
              <div key={i} style={{
                display: 'flex', alignItems: 'center', gap: 10, fontSize: 12, padding: '6px 0',
                borderBottom: i < result.jobs.length - 1 ? '1px solid var(--border)' : 'none',
              }}>
                <span>{j.status === 'sent' ? '📤' : '⚠️'}</span>
                <span style={{ color: 'var(--blue-soft)', fontWeight: 700, fontFamily: 'monospace' }}>{j.reference}</span>
                <span style={{ color: 'var(--text)' }}>{j.agent_name}</span>
                <span style={{ marginLeft: 'auto', color: j.status === 'sent' ? 'var(--green-soft)' : 'var(--red)' }}>{j.status}</span>
              </div>
            ))}
            <Link href="/" style={{
              display: 'inline-block', marginTop: 16, padding: '8px 16px', fontSize: 12, fontWeight: 600,
              background: 'var(--blue)', color: 'var(--on-accent)', borderRadius: 6, textDecoration: 'none',
            }}>Back to Inbox</Link>
          </div>
        )}

        {(phase === 'ready' || phase === 'sending') && (
          <>
            {/* Source email banner */}
            {sourceEmail && (
              <div style={{
                background: 'var(--surface)', border: '1px solid var(--input-border)', borderRadius: 8,
                padding: '10px 14px', marginBottom: 20, fontSize: 12, color: 'var(--muted-soft)',
              }}>
                📦 From: <span style={{ color: 'var(--text)' }}>{sourceEmail.sender}</span>
                {' — '}<span style={{ color: 'var(--text)' }}>{sourceEmail.subject}</span>
                {extracting && <span style={{ color: 'var(--amber)' }}> · extracting details...</span>}
              </div>
            )}

            {/* Shipment fields */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 20 }}>
              <div>
                <span style={labelStyle}>Sending Port (Origin) *</span>
                <input style={inputStyle} value={originPort} onChange={e => setOriginPort(e.target.value)} placeholder="e.g. Mundra" />
              </div>
              <div>
                <span style={labelStyle}>Receiving Port (Destination) *</span>
                <input style={inputStyle} value={destPort} onChange={e => setDestPort(e.target.value)} placeholder="e.g. Tema" />
              </div>
              <ContainerMultiSelect selection={containers} onChange={setContainers} />
              <div>
                <span style={labelStyle}>Commodity</span>
                <input style={inputStyle} value={commodity} onChange={e => setCommodity(e.target.value)} placeholder="e.g. general cargo" />
              </div>
              <div>
                <span style={labelStyle}>Mode</span>
                <select style={inputStyle} value={mode} onChange={e => setMode(e.target.value)}>
                  <option value="sea_freight">Sea Freight</option>
                  <option value="air_freight">Air Freight</option>
                  <option value="road">Road</option>
                </select>
              </div>
              <div>
                <span style={labelStyle}>Weight (kg, optional)</span>
                <input style={inputStyle} value={weightKg} onChange={e => setWeightKg(e.target.value)} placeholder="leave blank if unknown" type="number" />
              </div>
            </div>

            {/* Agent multi-selects */}
            <div style={{ display: 'grid', gap: 16, marginBottom: 16 }}>
              {CATEGORIES.map(c => (
                <AgentMultiSelect
                  key={c.key}
                  label={c.label}
                  agents={byCategory(c.key)}
                  selected={selected}
                  onToggle={toggleAgent}
                />
              ))}
            </div>

            {/* Manual email entry — add recipients not in the agent list */}
            <div style={{ marginBottom: 24 }}>
              <span style={labelStyle}>Add emails manually</span>
              <div style={{ display: 'flex', gap: 8 }}>
                <input
                  style={{ ...inputStyle, flex: 1 }}
                  value={manualNameInput}
                  onChange={e => setManualNameInput(e.target.value)}
                  placeholder="Name (optional)"
                />
                <input
                  style={{ ...inputStyle, flex: 2 }}
                  value={manualEmailInput}
                  onChange={e => { setManualEmailInput(e.target.value); setManualNote(null); }}
                  onKeyDown={e => {
                    if (e.key === 'Enter') { e.preventDefault(); addManualEmail(); }
                  }}
                  placeholder="email@example.com — press Enter to add"
                  type="email"
                />
                <button
                  type="button"
                  onClick={addManualEmail}
                  style={{
                    ...inputStyle, width: 'auto', padding: '9px 16px', cursor: 'pointer',
                    color: 'var(--blue-soft)', border: '1px solid var(--blue)', whiteSpace: 'nowrap',
                  }}
                >+ Add</button>
              </div>
              {manualNote && (
                <div style={{
                  marginTop: 8, fontSize: 11, lineHeight: 1.5,
                  color: manualNote.tone === 'error' ? 'var(--red)' : 'var(--amber)',
                }}>{manualNote.text}</div>
              )}
              {manualAgents.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
                  {manualAgents.map(m => (
                    <span key={m.email} style={{
                      display: 'inline-flex', alignItems: 'center', gap: 6,
                      background: 'var(--blue-tint-2)', border: '1px solid var(--blue)', borderRadius: 14,
                      padding: '4px 6px 4px 10px', fontSize: 11, color: 'var(--text)',
                    }}>
                      {m.agent_name} · {m.email}
                      <button
                        type="button"
                        onClick={() => removeManualEmail(m.email)}
                        aria-label={`Remove ${m.email}`}
                        style={{
                          background: 'none', border: 'none', color: 'var(--blue-text)', cursor: 'pointer',
                          fontSize: 14, lineHeight: 1, padding: '0 2px',
                        }}
                      >×</button>
                    </span>
                  ))}
                </div>
              )}
            </div>

            {/* Attachments — same files go to every selected agent */}
            <div style={{ marginBottom: 24 }}>
              <span style={labelStyle}>Attachments (optional)</span>
              <label style={{
                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
                padding: '14px', border: '1px dashed var(--border-strong)', borderRadius: 6,
                cursor: 'pointer', color: 'var(--muted)', fontSize: 12,
              }}>
                📎 Click to attach PDF, photos, CSV, etc. — {formatBytes(attachmentsTotalBytes)} of {formatBytes(MAX_ATTACHMENT_TOTAL_BYTES)} used
                <input
                  type="file"
                  multiple
                  onChange={e => { addAttachments(e.target.files); e.target.value = ''; }}
                  style={{ display: 'none' }}
                />
              </label>
              {attachmentError && (
                <div style={{ marginTop: 8, fontSize: 11, color: 'var(--red)' }}>{attachmentError}</div>
              )}
              {attachments.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
                  {attachments.map(a => (
                    <span key={a.filename} style={{
                      display: 'inline-flex', alignItems: 'center', gap: 6,
                      background: 'var(--border)', border: '1px solid var(--border-strong)', borderRadius: 14,
                      padding: '4px 6px 4px 10px', fontSize: 11, color: 'var(--text)',
                    }}>
                      📄 {a.filename} <span style={{ color: 'var(--muted)' }}>· {formatBytes(a.size_bytes)}</span>
                      <button
                        type="button"
                        onClick={() => removeAttachment(a.filename)}
                        aria-label={`Remove ${a.filename}`}
                        style={{
                          background: 'none', border: 'none', color: 'var(--blue-text)', cursor: 'pointer',
                          fontSize: 14, lineHeight: 1, padding: '0 2px',
                        }}
                      >×</button>
                    </span>
                  ))}
                </div>
              )}
            </div>

            {/* Draft preview panel */}
            {preview && (
              <div style={{
                background: 'var(--surface)', border: '1px solid var(--purple)', borderRadius: 8,
                padding: 16, marginBottom: 20,
              }}>
                <div style={{ display: 'flex', alignItems: 'center', marginBottom: 10 }}>
                  <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--purple)' }}>
                    {manualDraft ? '✍️ COMPOSE DRAFT' : '✏️ EDIT DRAFT'} — sent to all {totalRecipients} agent{totalRecipients === 1 ? '' : 's'}
                  </span>
                  <button
                    onClick={() => { setPreview(null); setManualDraft(false); }}
                    style={{
                      marginLeft: 'auto', background: 'transparent', border: 'none',
                      color: 'var(--muted)', fontSize: 16, cursor: 'pointer', padding: '0 4px',
                    }}
                  >×</button>
                </div>
                <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 10 }}>
                  Edit freely. Each agent gets its own unique RFQ reference added to the subject
                  automatically, so replies still match the right agent.
                </div>
                <label style={{ fontSize: 11, color: 'var(--muted-soft)', display: 'block', marginBottom: 4 }}>Subject</label>
                <input
                  value={editedSubject}
                  onChange={e => setEditedSubject(e.target.value)}
                  style={{
                    width: '100%', fontSize: 12, color: 'var(--text)', marginBottom: 12,
                    background: 'var(--sunken)', border: '1px solid var(--border)', borderRadius: 6,
                    padding: '8px 10px', boxSizing: 'border-box',
                  }}
                />
                <label style={{ fontSize: 11, color: 'var(--muted-soft)', display: 'block', marginBottom: 4 }}>Body</label>
                <textarea
                  value={editedBody}
                  onChange={e => setEditedBody(e.target.value)}
                  rows={12}
                  style={{
                    width: '100%', fontSize: 12, color: 'var(--text-soft)', lineHeight: 1.6,
                    whiteSpace: 'pre-wrap', background: 'var(--sunken)', border: '1px solid var(--border)',
                    borderRadius: 6, padding: 12, boxSizing: 'border-box', resize: 'vertical',
                    fontFamily: 'inherit',
                  }}
                />
              </div>
            )}

            {errorMsg && <div style={{ color: 'var(--red)', fontSize: 12, marginBottom: 12 }}>{errorMsg}</div>}

            <div style={{ display: 'flex', gap: 10 }}>
              <button
                onClick={handlePreview}
                disabled={previewing || phase === 'sending'}
                style={{
                  flex: '0 0 auto', padding: '14px 20px', fontSize: 13, fontWeight: 600,
                  background: 'transparent', color: previewing ? 'var(--muted)' : 'var(--purple)',
                  border: '1px solid var(--purple)', borderRadius: 8,
                  cursor: previewing ? 'default' : 'pointer',
                }}
              >
                {previewing ? '⏳ Drafting...' : (preview ? '🔄 Re-draft' : '👁 Draft & edit')}
              </button>
              {!preview && (
                <button
                  onClick={openManualDraft}
                  disabled={previewing || phase === 'sending'}
                  style={{
                    flex: '0 0 auto', padding: '14px 20px', fontSize: 13, fontWeight: 600,
                    background: 'transparent', color: 'var(--muted-soft)',
                    border: '1px solid var(--border-strong)', borderRadius: 8, cursor: 'pointer',
                  }}
                >✍️ Write manually</button>
              )}
              <button
                onClick={handleSend}
                disabled={phase === 'sending'}
                style={{
                  flex: 1, padding: 14, fontSize: 14, fontWeight: 700,
                  background: phase === 'sending' ? 'var(--border)' : 'var(--blue)',
                  color: phase === 'sending' ? 'var(--muted)' : 'var(--on-accent)',
                  border: 'none', borderRadius: 8, cursor: phase === 'sending' ? 'default' : 'pointer',
                }}
              >
                {phase === 'sending'
                  ? (preview ? 'Sending edited draft...' : 'Generating drafts & sending...')
                  : `${preview ? 'Send edited draft' : 'Send RFQ'} to ${totalRecipients} agent${totalRecipients === 1 ? '' : 's'}`}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
