/**
 * Replies the desk sends from the app: the shapes the API returns, and the small
 * rules for how they read on screen.
 *
 * Kept free of React and of `@/` imports so tests/sentReplies.check.ts can run it
 * under plain Node, the same arrangement as lib/shipments.ts.
 *
 * Not to be confused with lib/replyThreads.ts, which is about INBOUND replies
 * from freight agents. These go the other way.
 */

/** One reply as stored in `sent_replies`. */
export interface SentReply {
  id: string;
  email_id: string;
  to_addr: string;
  cc: string[];
  subject: string;
  body: string;
  attachment_names: string[];
  /** unknown: the provider errored in a way that does not prove the mail did not
   *  go (a timeout, a 5xx). Treated like a stale `sending`: look, don't resend. */
  status: 'sending' | 'sent' | 'failed' | 'unknown' | string;
  error: string | null;
  sent_by_name: string;
  created_at: string | null;
  sent_at: string | null;
}

/** What an inbox row carries about its replies: `replies` on /fetch-inbox rows. */
export interface ReplySummary {
  sent: number;
  failed: number;
  last_at: string | null;
  last_status: string;
}

/** A `sending` row older than this is not "in progress" any more; the process that
 *  owned it is gone, and nobody can say whether the mail left. */
export const STALE_SENDING_MS = 2 * 60 * 1000;

export type Tone = 'ok' | 'bad' | 'pending' | 'unknown';

export function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleString([], { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function fmtClock(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/**
 * The chip on a collapsed inbox card, or null for an email nobody has answered.
 *
 * The latest attempt decides the tone. A failed reply after two good ones still
 * reads as a failure, because the latest thing the desk tried to say did not
 * arrive, and that is the one that needs a person.
 */
export function chipFor(s: ReplySummary | null | undefined): { text: string; tone: Tone } | null {
  if (!s || (s.sent === 0 && s.failed === 0 && !s.last_status)) return null;
  if (s.last_status === 'failed') return { text: '⚠ Reply failed', tone: 'bad' };
  if (s.last_status === 'sending') return { text: '↩ Sending reply…', tone: 'pending' };
  if (s.last_status === 'unknown') return { text: '? Reply outcome unknown', tone: 'unknown' };
  if (s.sent === 0) return null;
  const count = s.sent > 1 ? ` ${s.sent}×` : '';
  const at = fmtClock(s.last_at);
  return { text: `↩ Replied${count}${at ? ` · ${at}` : ''}`, tone: 'ok' };
}

/** How one stored reply's outcome is worded. */
export function statusFor(r: Pick<SentReply, 'status' | 'created_at'>, now: number = Date.now()): { text: string; tone: Tone } {
  if (r.status === 'sent') return { text: '✓ Sent', tone: 'ok' };
  if (r.status === 'failed') return { text: '⚠ Not sent', tone: 'bad' };
  if (r.status === 'unknown') return { text: '? Outcome unknown, check Gmail Sent', tone: 'unknown' };
  const started = r.created_at ? new Date(r.created_at).getTime() : NaN;
  if (r.status === 'sending' && !isNaN(started) && now - started > STALE_SENDING_MS) {
    // Deliberately not "failed": it may well have gone out. Resending blind is how
    // a customer gets the same reply twice.
    return { text: '? Outcome unknown, check Gmail Sent', tone: 'unknown' };
  }
  return { text: 'Sending…', tone: 'pending' };
}

/** Retry resends the stored text as-is. Attachment bytes are not stored, so a reply
 *  that had any can only be edited and re-sent with the files attached again. */
export function canRetry(r: Pick<SentReply, 'status' | 'attachment_names'>): boolean {
  return r.status === 'failed' && (r.attachment_names ?? []).length === 0;
}

/** "Re: " plus the original, never "Re: Re: ". Mirrors reply_subject on the server,
 *  which is the one that counts; this is only what the composer shows. */
export function replySubject(subject: string): string {
  const s = (subject ?? '').trim();
  if (!s) return 'Re: (no subject)';
  return /^\s*re\s*:/i.test(s) ? s : `Re: ${s}`;
}

/** The bare address out of a From header: `"Name" <a@b.example>` gives a@b.example. */
export function addressOf(raw: string): string {
  const m = (raw ?? '').match(/<([^>]+)>/);
  return (m ? m[1] : raw ?? '').trim();
}

/** Cc as typed: commas, semicolons or whitespace between addresses. The server
 *  validates each one and refuses a bad entry rather than dropping it. */
export function parseCc(text: string): string[] {
  return text.split(/[,;\s]+/).map(s => s.trim()).filter(Boolean);
}

/** The text a new reply starts with: room to type, then the operator's sign-off.
 *  Same layout as the RFQ starter in send-request: body, blank line, signature. */
export function starterBody(signature: string): string {
  const sig = signature.trim();
  return sig ? `\n\nRegards,\n${sig}` : '';
}

/** sessionStorage key for an unsent draft, so collapsing a card or switching tabs
 *  does not throw away what the operator typed. */
export function draftKey(emailId: string): string {
  return `reply-draft:${emailId}`;
}

/** One per composed reply. The server sends at most once per key. */
export function newIdempotencyKey(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/** Fold a reply the browser just got back into the card's summary, so the chip
 *  updates without waiting for the next inbox poll. */
export function withReply(s: ReplySummary | null | undefined, r: SentReply, previous?: SentReply): ReplySummary {
  const base: ReplySummary = s ? { ...s } : { sent: 0, failed: 0, last_at: null, last_status: '' };
  // A retry replaces an earlier failed attempt on the same row: uncount it first.
  if (previous?.status === 'failed') base.failed = Math.max(0, base.failed - 1);
  if (r.status === 'sent') base.sent += 1;
  else if (r.status === 'failed') base.failed += 1;
  base.last_at = r.created_at ?? new Date().toISOString();
  base.last_status = r.status;
  return base;
}
