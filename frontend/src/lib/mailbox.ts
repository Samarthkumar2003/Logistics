/**
 * The Sent folder and thread views: the shapes the API returns and the small
 * rules for showing them. No React and no `@/` imports, so
 * tests/mailbox.check.ts runs it under plain Node (same arrangement as
 * lib/sentReplies.ts).
 */

export interface MailAttachment {
  filename: string;
  mime_type: string;
  size_bytes: number | null;
}

/** One message in a thread, or one opened Sent row. */
export interface MailMessage {
  id: string;
  thread_id: string;
  from: string;
  to: string;
  cc: string;
  subject: string;
  date: string | null;
  direction: 'sent' | 'received';
  body: string;
  body_clipped: boolean;
  attachments: MailAttachment[];
}

export interface MailThread {
  thread_id: string;
  /** Older messages left off a very long thread. */
  omitted: number;
  messages: MailMessage[];
}

/** One row of the Sent folder (metadata only; the body loads on open). */
export interface SentItem {
  id: string;
  thread_id: string;
  to: string;
  cc: string;
  subject: string;
  snippet: string;
  date: string | null;
  app_reply: boolean;
  rfq_reference: string | null;
}

export interface SentPage {
  items: SentItem[];
  next_page_token: string;
}

/** `"Asha K" <asha@x>` gives Asha K; a bare address gives its local part.
 *  For a list (`a@x, b@y`) the first name plus a count. */
export function displayName(raw: string): string {
  const list = splitAddresses(raw);
  if (list.length === 0) return '';
  const one = (s: string) => {
    const m = s.match(/^\s*"?([^"<]+?)"?\s*</);
    if (m && m[1].trim()) return m[1].trim();
    const addr = (s.match(/<([^>]+)>/)?.[1] ?? s).trim();
    return addr.split('@')[0] || addr;
  };
  return list.length > 1 ? `${one(list[0])} +${list.length - 1}` : one(list[0]);
}

/** Address list split on commas that are not inside a quoted display name. */
export function splitAddresses(raw: string): string[] {
  const out: string[] = [];
  let cur = '';
  let quoted = false;
  for (const ch of raw ?? '') {
    if (ch === '"') quoted = !quoted;
    if (ch === ',' && !quoted) {
      if (cur.trim()) out.push(cur.trim());
      cur = '';
    } else cur += ch;
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}

/** Where the quoted history starts in a reply body, so the view can fold it.
 *
 * Recognises the common shapes: Gmail's "On <date>, <name> wrote:" (often wrapped
 * over two lines), Outlook's "From: ... / Sent: ..." header block and its
 * underscore rule, "-----Original Message-----", and a run of "> " lines.
 * Anything unrecognised is left unfolded: showing too much beats hiding text.
 */
export function splitQuoted(body: string): { main: string; quoted: string } {
  const lines = (body ?? '').split('\n');
  const starts = (i: number): boolean => {
    const l = lines[i].trim();
    if (/^>/.test(l)) return true;
    if (/^-{2,}\s*original message\s*-{2,}$/i.test(l)) return true;
    if (/^_{10,}$/.test(l)) return true;
    if (/^on\s.+/i.test(l)) {
      const joined = [l, (lines[i + 1] ?? '').trim(), (lines[i + 2] ?? '').trim()].join(' ');
      if (/^on\s.{4,300}?\swrote:/i.test(joined)) return true;
    }
    if (/^from:\s.+/i.test(l)) {
      const next = lines.slice(i + 1, i + 4).map(s => s.trim());
      if (next.some(s => /^(sent|date):\s/i.test(s))) return true;
    }
    return false;
  };
  for (let i = 0; i < lines.length; i++) {
    if (starts(i)) {
      const main = lines.slice(0, i).join('\n').trimEnd();
      // A message that is nothing but quote (a bare forward) shows as it is.
      if (!main.trim()) return { main: body ?? '', quoted: '' };
      return { main, quoted: lines.slice(i).join('\n') };
    }
  }
  return { main: body ?? '', quoted: '' };
}

/** First line of a body, for a collapsed message. */
export function preview(body: string, max = 140): string {
  const first = (splitQuoted(body).main || '').split('\n').map(s => s.trim()).find(Boolean) ?? '';
  return first.length > max ? `${first.slice(0, max - 1)}…` : first;
}

export function fmtMailDate(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleString([], { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

export function fmtSize(n: number | null | undefined): string {
  if (!n) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
