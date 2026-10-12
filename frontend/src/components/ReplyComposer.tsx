"use client";

import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { apiFetch } from '@/lib/api';
import {
  addressOf, draftKey, newIdempotencyKey, parseCc, replySubject, starterBody,
  type SentReply,
} from '@/lib/sentReplies';

/**
 * Write one reply to an email's sender and send it.
 *
 * Live email is armed in production, so the composer says so in plain words next
 * to the button, and the button stays disabled until the operator has written
 * something beyond the signature.
 *
 * The draft (text, Cc, idempotency key) lives in sessionStorage, so collapsing the
 * card or switching tab keeps it. The key travels with the draft on purpose: if a
 * send dies on the network after the server got it, pressing Send again reuses the
 * key and the server returns the first attempt instead of mailing the customer twice.
 * Attachments are not kept in the draft; they are re-read from disk if needed.
 *
 * Inline styles and theme variables only, like ThreadCard, because this also
 * renders on pages that do not load dashboard.css.
 */

// Matches MAX_ATTACHMENT_TOTAL_BYTES in backend/app/routes/rfq.py, which the reply
// route shares. The server enforces it regardless.
const MAX_ATTACHMENT_TOTAL_BYTES = 15 * 1024 * 1024;

interface PendingAttachment {
  filename: string;
  content_type: string;
  size_bytes: number;
  data_base64: string;
}

interface Draft {
  body: string;
  cc: string;
  key: string;
}

/** What "Edit & resend" starts from: an earlier attempt's text and Cc. */
export interface ReplySeed {
  body: string;
  cc: string[];
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      // "data:<mime>;base64,<data>": only the payload after the comma.
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

function readDraft(emailId: string): Draft | null {
  try {
    const raw = sessionStorage.getItem(draftKey(emailId));
    if (!raw) return null;
    const d = JSON.parse(raw);
    if (typeof d?.body === 'string' && typeof d?.key === 'string') {
      return { body: d.body, cc: typeof d.cc === 'string' ? d.cc : '', key: d.key };
    }
  } catch { /* storage blocked or junk: start fresh */ }
  return null;
}

function writeDraft(emailId: string, d: Draft): void {
  try { sessionStorage.setItem(draftKey(emailId), JSON.stringify(d)); } catch { /* blocked */ }
}

function clearDraft(emailId: string): void {
  try { sessionStorage.removeItem(draftKey(emailId)); } catch { /* blocked */ }
}

const label: CSSProperties = {
  fontSize: 11, color: 'var(--muted)', fontWeight: 600, letterSpacing: 1, minWidth: 54,
};
const field: CSSProperties = {
  flex: 1, fontSize: 13, padding: '6px 8px', borderRadius: 6,
  background: 'var(--input-bg)', color: 'var(--text)', border: '1px solid var(--input-border)',
  fontFamily: 'inherit',
};

export default function ReplyComposer({ emailId, sender, subject, seed, onSent, onCancel }: {
  emailId: string;
  /** The original From header; the reply goes to its address. */
  sender: string;
  subject: string;
  /** Set for "Edit & resend". Overrides any saved draft. */
  seed?: ReplySeed | null;
  /** The stored attempt the server returned, sent or failed. */
  onSent: (reply: SentReply) => void;
  onCancel: () => void;
}) {
  const [body, setBody] = useState('');
  const [cc, setCc] = useState('');
  const [signature, setSignature] = useState('');
  const [signatureError, setSignatureError] = useState('');
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState('');
  const [sending, setSending] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [ready, setReady] = useState(false);
  const keyRef = useRef('');
  const fileInput = useRef<HTMLInputElement>(null);

  const to = addressOf(sender);
  const attachmentsTotalBytes = attachments.reduce((n, a) => n + a.size_bytes, 0);

  // Start from, in order: the edit-and-resend seed, a saved draft, or the signature.
  useEffect(() => {
    let cancelled = false;
    const draft = seed ? null : readDraft(emailId);
    if (seed) {
      keyRef.current = newIdempotencyKey();
      setBody(seed.body);
      setCc(seed.cc.join(', '));
    } else if (draft) {
      keyRef.current = draft.key;
      setBody(draft.body);
      setCc(draft.cc);
    } else {
      keyRef.current = newIdempotencyKey();
    }
    setReady(true);

    // Asked for even when a draft exists: a missing display name is refused on
    // send, and saying so now beats saying so after the operator wrote the reply.
    (async () => {
      try {
        const res = await apiFetch(`/rfq-signature`);
        if (cancelled) return;
        if (res.ok) {
          const sig = ((await res.json()).signature || '').trim();
          if (cancelled) return;
          setSignature(sig);
          if (!seed && !draft) setBody(prev => (prev ? prev : starterBody(sig)));
        } else {
          let detail = `Server error ${res.status}`;
          try { detail = (await res.json()).detail || detail; } catch { /* non-JSON */ }
          if (!cancelled) setSignatureError(detail);
        }
      } catch {
        if (!cancelled) setSignatureError('Could not read your operator profile, so this reply cannot be signed.');
      }
    })();
    return () => { cancelled = true; };
  }, [emailId, seed]);

  // Keep the draft as it is typed. Not before the effect above has run, or the
  // empty first render would overwrite the saved draft it is about to load.
  useEffect(() => {
    if (!ready || !keyRef.current) return;
    writeDraft(emailId, { body, cc, key: keyRef.current });
  }, [ready, emailId, body, cc]);

  async function addAttachments(files: FileList | null) {
    if (!files || files.length === 0) return;
    setAttachmentError('');
    const incoming = Array.from(files);
    const incomingBytes = incoming.reduce((n, f) => n + f.size, 0);
    if (attachmentsTotalBytes + incomingBytes > MAX_ATTACHMENT_TOTAL_BYTES) {
      setAttachmentError(`Attachments would total ${formatBytes(attachmentsTotalBytes + incomingBytes)}; the limit is ${formatBytes(MAX_ATTACHMENT_TOTAL_BYTES)}`);
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

  function discard() {
    clearDraft(emailId);
    onCancel();
  }

  async function send() {
    if (sending) return;
    setSending(true);
    setErrorMsg('');
    try {
      const res = await apiFetch(`/emails/${encodeURIComponent(emailId)}/reply`, {
        method: 'POST',
        body: JSON.stringify({
          body,
          cc: parseCc(cc),
          attachments: attachments.map(({ filename, content_type, data_base64 }) => ({ filename, content_type, data_base64 })),
          idempotency_key: keyRef.current,
        }),
      });
      if (!res.ok) {
        let detail = `Server error ${res.status}`;
        try { detail = (await res.json()).detail || detail; } catch { /* non-JSON */ }
        throw new Error(detail);
      }
      const row: SentReply = await res.json();
      // A row came back, so this key is spent whether the provider took the mail
      // or not. A failed one is retried from the history, not from here.
      clearDraft(emailId);
      onSent(row);
    } catch (err: unknown) {
      // Draft and key are kept: if the request did reach the server, sending again
      // returns that attempt rather than a second email.
      setErrorMsg(err instanceof Error ? err.message : 'Reply failed');
    } finally {
      setSending(false);
    }
  }

  const untouched = !body.trim() || body.trim() === starterBody(signature).trim();
  const sendDisabled = sending || untouched || !!signatureError || !to.includes('@');
  const ccList = parseCc(cc);

  return (
    <div
      onClick={e => e.stopPropagation()}
      style={{
        marginTop: 12, padding: 12, borderRadius: 10,
        border: '1px solid var(--blue-line)', background: 'var(--surface)',
        display: 'flex', flexDirection: 'column', gap: 8,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={label}>TO</span>
        <span style={{ fontSize: 13, color: 'var(--text)' }}>{to || '(no address)'}</span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={label}>CC</span>
        <input
          value={cc}
          onChange={e => setCc(e.target.value)}
          placeholder="Optional. Separate addresses with commas"
          disabled={sending}
          style={field}
        />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={label}>SUBJECT</span>
        <span style={{ fontSize: 13, color: 'var(--text-soft)' }}>{replySubject(subject)}</span>
      </div>
      <textarea
        value={body}
        onChange={e => setBody(e.target.value)}
        disabled={sending}
        rows={8}
        autoFocus
        placeholder="Write your reply"
        style={{ ...field, lineHeight: 1.5, resize: 'vertical', minHeight: 140 }}
      />
      <div style={{ fontSize: 11, color: 'var(--faint)' }}>
        The original message is quoted below your text automatically.
      </div>

      <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 6 }}>
        <input
          ref={fileInput}
          type="file"
          multiple
          style={{ display: 'none' }}
          onChange={e => { addAttachments(e.target.files); e.target.value = ''; }}
        />
        <button
          type="button"
          disabled={sending}
          onClick={() => fileInput.current?.click()}
          style={{
            fontSize: 12, padding: '4px 10px', borderRadius: 6, cursor: 'pointer',
            border: '1px dashed var(--input-border)', background: 'transparent', color: 'var(--muted-soft)',
          }}
        >📎 Attach</button>
        {attachments.map(a => (
          <span key={a.filename} style={{
            display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12,
            padding: '3px 8px', borderRadius: 6, background: 'var(--sunken-soft)', color: 'var(--muted-soft)',
          }}>
            {a.filename} · {formatBytes(a.size_bytes)}
            <button
              type="button"
              aria-label={`Remove ${a.filename}`}
              disabled={sending}
              onClick={() => setAttachments(prev => prev.filter(x => x.filename !== a.filename))}
              style={{ border: 'none', background: 'transparent', color: 'var(--faint)', cursor: 'pointer', padding: 0 }}
            >✕</button>
          </span>
        ))}
      </div>
      {attachmentError && <div style={{ fontSize: 12, color: 'var(--red)' }}>{attachmentError}</div>}

      <div style={{
        fontSize: 12, padding: '6px 10px', borderRadius: 6,
        color: 'var(--amber-text)', background: 'var(--amber-tint)', border: '1px solid var(--amber-line)',
      }}>
        Live email: pressing Send mails {to || 'the sender'}
        {ccList.length > 0 ? ` and ${ccList.length} Cc` : ''} straight away, signed as you.
      </div>
      {signatureError && (
        <div style={{ fontSize: 12, color: 'var(--red)' }}>⚠️ {signatureError}</div>
      )}
      {errorMsg && (
        <div role="alert" style={{ fontSize: 12, color: 'var(--red)' }}>⚠️ Not sent: {errorMsg}</div>
      )}

      <div style={{ display: 'flex', gap: 8 }}>
        <button
          type="button"
          disabled={sendDisabled}
          onClick={send}
          title={!to.includes('@') ? 'This email has no reply address' : untouched ? 'Write something before sending' : undefined}
          style={{
            padding: '7px 16px', borderRadius: 8, border: 'none', fontSize: 13, fontWeight: 600,
            background: sendDisabled ? 'var(--input-border)' : 'var(--blue)', color: 'var(--on-accent)',
            cursor: sendDisabled ? 'default' : 'pointer',
          }}
        >{sending ? '⏳ Sending…' : '↩ Send reply'}</button>
        <button
          type="button"
          disabled={sending}
          onClick={discard}
          style={{
            padding: '7px 16px', borderRadius: 8, border: '1px solid var(--border)', fontSize: 13,
            background: 'transparent', color: 'var(--muted)', cursor: 'pointer',
          }}
        >Discard</button>
      </div>
    </div>
  );
}
