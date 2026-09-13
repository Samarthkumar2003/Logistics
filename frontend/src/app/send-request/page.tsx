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
import {
  CATEGORY_LABELS, CATEGORY_OPTIONS, CATEGORY_SHORT, type CategoryChoice,
  type CategoryKey, isCategory,
} from './categories';
import {
  type CategoryDraft, type DraftMap, type DraftText, acknowledgeDraft,
  describeRedraft, editDraft,
  missingDrafts, presentCategories, recipientsFor, redraft, resetDraft, seedDrafts,
  syncDrafts, toWireDrafts, unreviewedDrafts,
} from './categoryDrafts';
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

/** A recipient, with the category that decides which draft they are sent. */
interface Recipient {
  agent_name: string;
  email: string;
  category: CategoryKey;
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

/* ─── Recipient chip ────────────────────────────────────────────── */
/** One chosen recipient, with a remove control.
 *
 *  Used for hand-typed addresses and for roster agents pulled in by typing their
 *  address, so both read as one list. Typing a known agent's address used to
 *  answer with a line of text saying it had been selected; a chip is stronger,
 *  because the category dropdowns are collapsed by default and the checkbox it
 *  ticks is therefore off-screen. */
function RecipientChip({ name, email, category, onRemove }: {
  name: string;
  email: string;
  /** Which draft this recipient will be sent. Shown on the chip because the
   *  category dropdowns are collapsed by default, so the checkbox that proves it
   *  is off-screen - and unlike before, the choice changes what they receive. */
  category: CategoryKey;
  onRemove: () => void;
}) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 6,
      background: 'var(--blue-tint-2)', border: '1px solid var(--blue)', borderRadius: 14,
      padding: '4px 6px 4px 10px', fontSize: 11, color: 'var(--text)',
    }}>
      <span style={{
        fontSize: 9, fontWeight: 700, letterSpacing: '0.05em', color: 'var(--blue-text)',
      }}>{CATEGORY_SHORT[category]}</span>
      {name} {'\u00b7'} {email}
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${email}`}
        style={{
          background: 'none', border: 'none', color: 'var(--blue-text)', cursor: 'pointer',
          fontSize: 14, lineHeight: 1, padding: '0 2px',
        }}
      >×</button>
    </span>
  );
}

/* ─── Draft panels ──────────────────────────────────────────────── */
function PanelBadge({ tone, children }: { tone: 'error' | 'warn' | 'info'; children: string }) {
  const colour = tone === 'error' ? 'var(--red)' : tone === 'warn' ? 'var(--amber)' : 'var(--blue-soft)';
  return (
    <span style={{
      fontSize: 9, fontWeight: 700, letterSpacing: '0.06em', color: colour,
      border: `1px solid ${colour}`, borderRadius: 10, padding: '1px 7px',
    }}>{children}</span>
  );
}

/** One category's draft, and who it is addressed to.
 *
 *  Collapsible because three twelve-row textareas bury the send button, and this
 *  page already owns its own scroll for that reason. The header therefore has to
 *  carry everything needed to decide whether to open it: which kind of vendor,
 *  how many, exactly who, and whether the text has been touched.
 *
 *  The recipient list here is deliberately read-only. Selection stays in the
 *  dropdowns above; a second place to remove a recipient is a second place for
 *  the two to disagree about who is being written to.
 */
function DraftPanel({ category, addressees, draft, open, onToggle, onChange, onReset }: {
  category: CategoryKey;
  addressees: Recipient[];
  draft: CategoryDraft;
  open: boolean;
  onToggle: () => void;
  onChange: (patch: Partial<DraftText>) => void;
  onReset: () => void;
}) {
  const blank = !draft.subject.trim() || !draft.body.trim();
  const edge = blank ? 'var(--red-line)' : draft.isNew ? 'var(--amber)' : 'var(--purple)';

  return (
    <div style={{
      background: 'var(--surface)', border: `1px solid ${edge}`,
      borderRadius: 8, marginBottom: 12,
    }}>
      <div onClick={onToggle} style={{ padding: '11px 14px', cursor: 'pointer' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span style={{ color: 'var(--muted)', fontSize: 10 }}>
            {open ? '\u25bc' : '\u25b6'}
          </span>
          <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--purple)' }}>
            {CATEGORY_LABELS[category]}
          </span>
          <span style={{ fontSize: 11, color: 'var(--muted)' }}>
            {'\u00b7'} {addressees.length} recipient{addressees.length === 1 ? '' : 's'}
          </span>
          {blank && <PanelBadge tone="error">BLANK</PanelBadge>}
          {!blank && draft.isNew && <PanelBadge tone="warn">NEW - REVIEW</PanelBadge>}
          {!blank && !draft.isNew && draft.edited && <PanelBadge tone="info">EDITED</PanelBadge>}
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
          {addressees.map(r => (
            <span key={r.email} style={{
              fontSize: 10, color: 'var(--muted-soft)', background: 'var(--sunken)',
              border: '1px solid var(--border)', borderRadius: 10, padding: '2px 8px',
            }}>{r.agent_name} {'\u00b7'} {r.email}</span>
          ))}
        </div>
      </div>

      {open && (
        <div style={{ padding: '0 14px 14px' }}>
          <label style={{ fontSize: 11, color: 'var(--muted-soft)', display: 'block', marginBottom: 4 }}>
            Subject
          </label>
          <input
            value={draft.subject}
            onChange={e => onChange({ subject: e.target.value })}
            style={{
              width: '100%', fontSize: 12, color: 'var(--text)', marginBottom: 12,
              background: 'var(--sunken)', border: '1px solid var(--border)', borderRadius: 6,
              padding: '8px 10px', boxSizing: 'border-box',
            }}
          />
          <label style={{ fontSize: 11, color: 'var(--muted-soft)', display: 'block', marginBottom: 4 }}>
            Body
          </label>
          <textarea
            value={draft.body}
            onChange={e => onChange({ body: e.target.value })}
            rows={9}
            style={{
              width: '100%', fontSize: 12, color: 'var(--text-soft)', lineHeight: 1.6,
              whiteSpace: 'pre-wrap', background: 'var(--sunken)', border: '1px solid var(--border)',
              borderRadius: 6, padding: 12, boxSizing: 'border-box', resize: 'vertical',
              fontFamily: 'inherit',
            }}
          />
          {draft.edited && (
            <button
              type="button"
              onClick={onReset}
              style={{
                marginTop: 8, background: 'transparent', border: '1px solid var(--border-strong)',
                borderRadius: 6, color: 'var(--muted-soft)', fontSize: 11, fontWeight: 600,
                padding: '5px 12px', cursor: 'pointer',
              }}
            >{'\u21ba'} Reset to draft</button>
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
  // Roster agents selected by typing their address into the manual box rather than
  // by ticking a dropdown. Tracked purely so they can be shown as chips: the
  // category dropdowns are collapsed by default, so a checkbox ticking itself
  // inside a closed panel is not visible confirmation of anything.
  const [textSelectedIds, setTextSelectedIds] = useState<Set<number>>(new Set());
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
  // Optional door addresses. Deliberately absent from the extraction in init():
  // a guessed address is worse than a blank one, same reasoning as Size above.
  const [sendingAddress, setSendingAddress] = useState('');
  const [receivingAddress, setReceivingAddress] = useState('');

  const [phase, setPhase] = useState<'loading' | 'ready' | 'sending' | 'sent' | 'error'>('loading');
  const [extracting, setExtracting] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [result, setResult] = useState<SendRFQResponse | null>(null);
  const [previewing, setPreviewing] = useState(false);

  // The one draft the model produced. Kept apart from the panels so a panel can
  // be told from the text it started as: that comparison is what makes "edited"
  // honest, and what lets Reset put a panel back.
  const [seed, setSeed] = useState<DraftText | null>(null);
  // One editable draft per vendor category, seeded from `seed` and diverging only
  // where the operator edits. Never read directly - read `drafts` below, which is
  // this map re-aligned with the current recipient list.
  const [draftsRaw, setDraftsRaw] = useState<DraftMap>({});
  const [openPanels, setOpenPanels] = useState<Set<CategoryKey>>(new Set());
  // The operator's sign-off, so a hand-composed draft can end in the real one
  // rather than have it bolted on server-side. Empty means the profile has no
  // display name, which is a refusal the server will repeat on send.
  const [signature, setSignature] = useState('');
  const [signatureError, setSignatureError] = useState('');
  // What a re-draft did, shown above the panels. A re-draft that spared an edited
  // panel has to say so, or the operator will assume the new wording is everywhere.
  const [draftNote, setDraftNote] = useState('');

  // Ad-hoc recipients typed in by hand, not on the roster. Merged with the
  // checkbox selection at draft and send time. Keyed by lowercased email.
  const [manualAgents, setManualAgents] = useState<ManualRecipient[]>([]);
  const [manualEmailInput, setManualEmailInput] = useState('');
  const [manualNameInput, setManualNameInput] = useState('');
  const [manualNote, setManualNote] = useState<ManualNote | null>(null);
  // Which kind of vendor a hand-typed address is. Mandatory and with no default:
  // it decides which draft they receive, so there is nothing safe to guess.
  const [manualCategory, setManualCategory] = useState<CategoryChoice>('');

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
  //
  // It ends in the operator's real signature, fetched from GET /rfq-signature and
  // laid out exactly as the server's _signed() does: body, blank line, name,
  // company. It used to end on the literal line "Best regards," with nothing
  // after it, and because the backend signs the model's drafts but deliberately
  // never touches operator-supplied text, every hand-composed RFQ went to a
  // freight vendor unsigned. With no signature available the sign-off is left off
  // rather than faked, so the gap is visible; the send is refused anyway.
  function manualStarter(): DraftText {
    const modeLabel = mode.replace('_', ' ');
    const subject = `Request for Quotation - ${modeLabel} ${originPort.trim()} to ${destPort.trim()}`.trim();
    const details = [
      `Origin: ${originPort.trim() || '-'}`,
      `Destination: ${destPort.trim() || '-'}`,
      `Mode: ${modeLabel}`,
      commodity.trim() ? `Commodity: ${commodity.trim()}` : null,
      size.trim() ? `Size: ${size.trim()}` : null,
      weightKg.trim() ? `Weight: ${weightKg.trim()} kg` : null,
      sendingAddress.trim() ? `Pickup (sending) address: ${sendingAddress.trim()}` : null,
      receivingAddress.trim() ? `Delivery (receiving) address: ${receivingAddress.trim()}` : null,
    ].filter(Boolean);
    const body = [
      'Dear Team,', '',
      'We have the following shipment enquiry and would appreciate your best rate and transit time:', '',
      ...details, '',
      'Please share your best quotation at the earliest.',
      ...(signature ? ['', signature] : []),
    ].join('\n');
    return { subject, body };
  }

  /** Why a recipient is needed before there is anything to draft.
   *
   *  The panel stack is derived from the recipient list - one panel per category
   *  that somebody is in - so with nobody selected there is no panel to put text
   *  in. Drafting anyway used to work, because there was a single panel and the
   *  preview addressed a placeholder agent; now it would spend a model call and
   *  put nothing on screen, which reads as the button being broken.
   */
  function needsRecipientFirst(): boolean {
    if (recipients.length > 0) return false;
    setErrorMsg(
      'Pick at least one agent first. The draft is written per kind of agent, so '
      + 'there is nothing to draft until somebody is selected.',
    );
    return true;
  }

  /** Open every present panel on a starter written from the form, with no model
   *  call. Same shape as drafting: one text, copied into each category. */
  function openManualDraft() {
    if (needsRecipientFirst()) return;
    const starter = manualStarter();
    setSeed(starter);
    setDraftsRaw(seedDrafts(present, starter));
    setOpenPanels(new Set(present.slice(0, 1)));
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
      // 2. The sign-off these RFQs will carry. Not fatal to the page: the form is
      // still worth filling in, and the server repeats the refusal on send. But
      // asking now is what surfaces a missing display name on load instead of
      // after the operator has done all the work.
      try {
        const res = await apiFetch(`/rfq-signature`);
        if (res.ok) {
          setSignature(((await res.json()).signature || '').trim());
        } else {
          let detail = `Server error ${res.status}`;
          try { detail = (await res.json()).detail || detail; } catch { /* non-JSON */ }
          setSignatureError(detail);
        }
      } catch {
        setSignatureError('Could not read your operator profile, so drafts cannot be signed.');
      }

      setPhase('ready');

      // 3. Prefill from the customer email via the intake agent
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
    const split = splitManualTokens(
      tokens, agents, manualAgents, manualNameInput, manualCategory,
    );

    if (split.added.length > 0) {
      // Pure updater — re-filters against `prev`, so running it twice is a no-op.
      setManualAgents(prev => mergeManualRecipients(prev, split.added));
    }
    // An address already in the agents table is what the checkbox list is for, so
    // select it. Skipping the token silently — the old behaviour — is why typing a
    // known agent's address looked like the Add button doing nothing at all, and it
    // got steadily more likely as the table grew past a hundred rows.
    if (split.onRoster.length > 0) {
      const ids = split.onRoster.map(a => a.id);
      setSelected(prev => new Set([...prev, ...ids]));
      setTextSelectedIds(prev => new Set([...prev, ...ids]));
    }

    // Keep anything the operator still has to act on in the box - a typo to fix,
    // or an address waiting on a category choice - and clear the rest. Dropping a
    // held-back address would leave a note about an email no longer on screen.
    setManualEmailInput([...split.invalid, ...split.needsCategory].join(', '));
    if (split.added.length > 0 || split.onRoster.length > 0) setManualNameInput('');
    setManualNote(describeSplit(split));
    setErrorMsg('');
  }

  function removeManualEmail(email: string) {
    setManualAgents(prev => prev.filter(m => m.email !== email));
  }

  // Recipients = checkbox-selected DB agents + hand-entered emails, one per
  // address, each carrying the category that decides which draft it is sent.
  //
  // The dedup is not decoration. These are two independent states that can name
  // the same person, the count below is what the send button promises, and this
  // list is what gets posted - so any address appearing twice here is a second
  // enquiry to a vendor under a second reference, sent without anything on screen
  // showing it.
  //
  // The category filter is belt to the braces elsewhere: the dropdowns are built
  // from byCategory, and typing a roster address refuses a row filed under
  // anything else. A recipient with no panel to belong to must not reach the
  // payload by any route.
  const selectedAgents = agents.filter(a => selected.has(a.id) && isCategory(a.category));
  const recipients: Recipient[] = dedupeByEmail([
    ...selectedAgents.map(a => ({
      agent_name: a.agent_name, email: a.email, category: a.category as CategoryKey,
    })),
    ...manualAgents,
  ]) as Recipient[];
  const totalRecipients = recipients.length;

  // Categories that actually have a recipient, and so have a panel. A panel for a
  // category nobody is selected in would invite the operator to write text that
  // reaches no one.
  const present = presentCategories(recipients);
  const hasDrafts = Object.keys(draftsRaw).length > 0;

  // The panels as they should be now, rather than as they were when the model last
  // answered. Re-aligning here instead of in an effect keeps it a pure derivation:
  // syncDrafts hands back its input untouched when nothing moved, and every write
  // below is applied to THIS map - so a panel that appeared because an agent was
  // ticked after drafting is seeded and editable straight away, which is the one
  // ordering this whole design has to survive.
  const drafts = hasDrafts ? syncDrafts(draftsRaw, present, seed) : draftsRaw;
  const blocked = hasDrafts ? missingDrafts(drafts, present) : [];
  const unreviewed = hasDrafts ? unreviewedDrafts(drafts, present) : [];

  function togglePanel(key: CategoryKey) {
    setOpenPanels(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
    // Opening or closing a panel counts as having seen it.
    setDraftsRaw(acknowledgeDraft(drafts, key));
  }

  function patchDraft(key: CategoryKey, patch: Partial<DraftText>) {
    setDraftsRaw(editDraft(drafts, key, patch, seed));
  }

  function handleReset(key: CategoryKey) {
    setDraftsRaw(resetDraft(drafts, key, seed));
  }

  // Roster agents pulled in by typing an address, shown as chips next to the box the
  // operator typed into. Intersected with `selected` rather than trusted on its own,
  // so unticking one in its dropdown also drops the chip and the two controls can
  // never claim different recipient lists.
  const textSelectedAgents = agents.filter(a => textSelectedIds.has(a.id) && selected.has(a.id));

  function removeTextSelected(id: number) {
    setSelected(prev => { const next = new Set(prev); next.delete(id); return next; });
    setTextSelectedIds(prev => { const next = new Set(prev); next.delete(id); return next; });
  }

  async function handlePreview() {
    setErrorMsg('');
    if (!originPort.trim() || !destPort.trim()) { setErrorMsg('Origin and destination ports are required'); return; }
    if (needsRecipientFirst()) return;

    // One model call, whose answer seeds every panel - the three drafts start
    // identical and diverge only where the operator edits one. Three calls would
    // cost three times as much and produce three different texts, which is the
    // opposite of what the panels are for.
    //
    // The prompt is still given one agent for context, so the wording can be
    // addressed to that vendor and then copied to the other categories. That was
    // already true when a single draft went to everyone; it is merely easier to
    // notice now that the operator can see all three.
    const sampleAgent = { agent_name: recipients[0].agent_name, email: recipients[0].email };

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
          sending_address: sendingAddress,
          receiving_address: receivingAddress,
          agent: sampleAgent,
        }),
      });
      if (!res.ok) {
        let detail = `Server error ${res.status}`;
        try { detail = (await res.json()).detail || detail; } catch { /* non-JSON body */ }
        throw new Error(detail);
      }
      const data = await res.json();
      const fresh: DraftText = { subject: data.subject || '', body: data.body || '' };
      setSeed(fresh);
      if (hasDrafts) {
        // Re-draft. Edited panels survive and are named; see describeRedraft.
        const outcome = redraft(drafts, present, fresh);
        setDraftsRaw(outcome.next);
        setDraftNote(describeRedraft(outcome, key => CATEGORY_LABELS[key]) ?? '');
      } else {
        setDraftsRaw(seedDrafts(present, fresh));
        setDraftNote('');
      }
      setOpenPanels(new Set(present.slice(0, 1)));
    } catch (err: unknown) {
      // Draft generation failed (commonly LLM quota). Don't dead-end — open a
      // manual draft seeded from the form so the user can still compose & send.
      const msg = err instanceof Error ? err.message : 'Preview failed';
      setErrorMsg(`AI draft unavailable (${msg}) - compose the drafts manually below.`);
      openManualDraft();
    }
    setPreviewing(false);
  }

  async function handleSend() {
    setErrorMsg('');
    if (recipients.length === 0) { setErrorMsg('Select at least one agent or add an email'); return; }
    if (!originPort.trim() || !destPort.trim()) { setErrorMsg('Origin and destination ports are required'); return; }
    // A recipient whose panel is blank is refused here, and again on the server.
    // The alternative - sending the categories that are filled in and quietly
    // dropping the rest - reports success for everyone else, so there is nothing
    // on screen to tell the operator a whole category went nowhere.
    if (blocked.length > 0) {
      setErrorMsg(
        `Nothing sent. No draft written for ${blocked.map(c => CATEGORY_LABELS[c]).join(', ')} - `
        + `fill those panels in, or remove their recipients.`,
      );
      setOpenPanels(new Set(blocked));
      return;
    }

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
          sending_address: sendingAddress,
          receiving_address: receivingAddress,
          // Each recipient carries its category, which is how the server knows
          // which panel's text to send it.
          agents: recipients,
          customer_sender: sourceEmail?.sender || '',
          customer_subject: sourceEmail?.subject || '',
          customer_body: sourceEmail?.body || '',
          // Links every RFQ from this request back to the customer email (Phase 1).
          customer_email_id: sourceEmail?.id || '',
          // The reviewed panels, keyed by category, sent verbatim. Omitted only
          // when the operator never opened the draft editor at all, which is the
          // one case where the model still writes a draft per agent. A partly
          // filled map is refused rather than topped up, above and on the server.
          ...(hasDrafts ? { drafts: toWireDrafts(drafts, present) } : {}),
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

            {/* No display name on the operator profile means nothing can be signed.
                Said here, on load, rather than discovered after the form is filled
                in: the server refuses to draft or send without it either way. */}
            {signatureError && (
              <div style={{
                background: 'var(--surface)', border: '1px solid var(--red-line)', borderRadius: 8,
                padding: '10px 14px', marginBottom: 20, fontSize: 11, color: 'var(--red)',
                lineHeight: 1.5,
              }}>
                Drafts cannot be signed, so sending will be refused: {signatureError}
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
              {/* Full width: a street address does not fit a half column. maxLength
                  mirrors MAX_ADDRESS_CHARS on the server, so the cap is felt while
                  typing instead of arriving as a 422 after the draft is requested. */}
              <div style={{ gridColumn: '1 / -1' }}>
                <span style={labelStyle}>Sending Address (optional)</span>
                <textarea
                  style={{ ...inputStyle, minHeight: 62, resize: 'vertical', fontFamily: 'inherit' }}
                  value={sendingAddress}
                  onChange={e => setSendingAddress(e.target.value)}
                  maxLength={500}
                  placeholder="Pickup / factory address - leave blank for a port-to-port enquiry"
                />
              </div>
              <div style={{ gridColumn: '1 / -1' }}>
                <span style={labelStyle}>Receiving Address (optional)</span>
                <textarea
                  style={{ ...inputStyle, minHeight: 62, resize: 'vertical', fontFamily: 'inherit' }}
                  value={receivingAddress}
                  onChange={e => setReceivingAddress(e.target.value)}
                  maxLength={500}
                  placeholder="Final delivery address - leave blank for a port-to-port enquiry"
                />
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
              {/* Category first, and required. It is not a label: it decides which
                  of the drafts below this address is sent, so there is no safe
                  default and Add refuses without it. Picking it also files the
                  address under a real category when the send remembers it, which
                  is what stops hand-entered agents disappearing from the three
                  dropdowns the moment they are saved. */}
              <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
                <select
                  value={manualCategory}
                  onChange={e => { setManualCategory(e.target.value as CategoryChoice); setManualNote(null); }}
                  aria-label="Kind of agent"
                  style={{
                    ...inputStyle, flex: 2, cursor: 'pointer',
                    color: manualCategory ? 'var(--blue-soft)' : 'var(--muted)',
                    border: `1px solid ${manualCategory ? 'var(--blue)' : 'var(--input-border)'}`,
                  }}
                >
                  <option value="">What kind of agent is this? (required)</option>
                  {CATEGORY_OPTIONS.map(o => (
                    <option key={o.key} value={o.key}>{o.label}</option>
                  ))}
                </select>
                <input
                  style={{ ...inputStyle, flex: 1 }}
                  value={manualNameInput}
                  onChange={e => setManualNameInput(e.target.value)}
                  placeholder="Name (optional)"
                />
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <input
                  style={{ ...inputStyle, flex: 1 }}
                  value={manualEmailInput}
                  onChange={e => { setManualEmailInput(e.target.value); setManualNote(null); }}
                  onKeyDown={e => {
                    if (e.key === 'Enter') { e.preventDefault(); addManualEmail(); }
                  }}
                  placeholder="email@example.com - press Enter to add"
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
              {(textSelectedAgents.length > 0 || manualAgents.length > 0) && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
                  {textSelectedAgents.map(a => (
                    <RecipientChip
                      key={`roster-${a.id}`}
                      name={a.agent_name}
                      email={a.email}
                      category={a.category as CategoryKey}
                      onRemove={() => removeTextSelected(a.id)}
                    />
                  ))}
                  {manualAgents.map(m => (
                    <RecipientChip
                      key={m.email}
                      name={m.agent_name}
                      email={m.email}
                      category={m.category}
                      onRemove={() => removeManualEmail(m.email)}
                    />
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

            {/* Draft panels - one per category that has a recipient */}
            {hasDrafts && (
              <div style={{ marginBottom: 20 }}>
                <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 10 }}>
                  One draft per kind of agent, all seeded from the same model draft. Editing
                  one leaves the others alone, so an unedited panel sends exactly the text
                  below. Each agent still gets its own unique RFQ reference in the subject,
                  so replies match the right agent.
                </div>
                {draftNote && (
                  <div style={{ fontSize: 11, color: 'var(--amber)', marginBottom: 10 }}>
                    {draftNote}
                  </div>
                )}
                {present.map(key => {
                  const draft = drafts[key];
                  if (!draft) return null;
                  return (
                    <DraftPanel
                      key={key}
                      category={key}
                      addressees={recipientsFor(recipients, key)}
                      draft={draft}
                      open={openPanels.has(key)}
                      onToggle={() => togglePanel(key)}
                      onChange={patch => patchDraft(key, patch)}
                      onReset={() => handleReset(key)}
                    />
                  );
                })}
                {unreviewed.length > 0 && (
                  <div style={{ fontSize: 11, color: 'var(--amber)' }}>
                    Appeared after drafting and not opened yet:{' '}
                    {unreviewed.map(c => CATEGORY_LABELS[c]).join(', ')}
                  </div>
                )}
              </div>
            )}

            {/* Said before the operator presses Send, not only after. */}
            {blocked.length > 0 && (
              <div style={{ fontSize: 11, color: 'var(--red)', marginBottom: 10 }}>
                Nothing will send to {blocked.map(c => CATEGORY_LABELS[c]).join(', ')} until
                those panels have both a subject and a body.
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
                {previewing ? '⏳ Drafting...' : (hasDrafts ? '🔄 Re-draft' : '👁 Draft & edit')}
              </button>
              {!hasDrafts && (
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
                  ? (hasDrafts ? 'Sending reviewed drafts...' : 'Generating drafts & sending...')
                  : `${hasDrafts ? 'Send reviewed drafts' : 'Send RFQ'} to ${totalRecipients} agent${totalRecipients === 1 ? '' : 's'}`}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
