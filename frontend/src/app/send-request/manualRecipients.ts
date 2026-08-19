/**
 * Hand-typed RFQ recipients.
 *
 * Split out of the page because this is the whole of the "Add emails manually"
 * decision and it is worth testing on its own. The bug it exists to prevent was
 * not a wrong decision but a silent one: every rejection path — a typo, an
 * address that already belonged to an agent — cleared the input box and produced
 * no message, so the Add button looked broken. The rule here is that every token
 * lands in exactly one bucket and every non-empty bucket gets said out loud.
 */

/** An ad-hoc recipient typed in by hand, not backed by a row in the agents table. */
export interface ManualRecipient {
  agent_name: string;
  email: string;
}

/** The part of an agent row this decision needs. `Agent` satisfies it structurally. */
export interface RosterAgent {
  id: number;
  agent_name: string;
  email: string;
}

/** Feedback for the manual-entry box, shown next to it rather than at the foot of
 *  the form — a message the user has to scroll to find is a message they don't see. */
export interface ManualNote {
  text: string;
  tone: 'error' | 'info';
}

/** What a batch of hand-typed addresses resolves to. */
export interface TokenSplit {
  added: ManualRecipient[];
  onRoster: RosterAgent[];   // already in the agents table — tick the checkbox instead
  duplicates: string[];      // already entered by hand
  invalid: string[];
}

// Basic RFC-ish email shape check — enough to catch typos, not to be a parser.
export const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Accept one or many at once — split on newline / comma / semicolon / space so
 *  Enter-per-email, paste-a-list, and "a@x.com, b@y.com" all work. */
export function tokenizeEmails(raw: string): string[] {
  return raw.split(/[\s,;]+/).map(t => t.trim()).filter(Boolean);
}

/** Sort typed tokens into the four things they can be. Pure: no state is touched,
 *  so the result can be inspected before anything is committed. */
export function splitManualTokens(
  tokens: string[], agents: RosterAgent[], manual: ManualRecipient[], typedName: string,
): TokenSplit {
  const roster = new Map(agents.map(a => [a.email.toLowerCase(), a]));
  const held = new Set(manual.map(m => m.email.toLowerCase()));
  const split: TokenSplit = { added: [], onRoster: [], duplicates: [], invalid: [] };

  for (const email of tokens) {
    const lower = email.toLowerCase();
    if (!EMAIL_RE.test(email)) { split.invalid.push(email); continue; }
    const existing = roster.get(lower);
    if (existing) { split.onRoster.push(existing); continue; }
    if (held.has(lower)) { split.duplicates.push(email); continue; }
    held.add(lower);                      // dedup within this batch as well
    split.added.push({
      // A typed name applies only when a single address was entered.
      agent_name: (tokens.length === 1 && typedName.trim())
        ? typedName.trim()
        : email.split('@')[0],
      email,
    });
  }
  return split;
}

/** Append `added` to `prev`, skipping anything already held.
 *
 *  Named and pure so it can be handed to setState and applied twice with the same
 *  result. That is not hypothetical tidiness: the original code built its additions
 *  by pushing into an array declared outside the updater, so React's development
 *  double-invoke ran the loop twice and appended every address twice. The two copies
 *  shared `key={m.email}`, so React rendered one chip while state held two entries —
 *  the UI showed one recipient and the send went to that address twice.
 */
export function mergeManualRecipients(
  prev: ManualRecipient[], added: ManualRecipient[],
): ManualRecipient[] {
  const held = new Set(prev.map(m => m.email.trim().toLowerCase()));
  return [...prev, ...added.filter(m => !held.has(m.email.trim().toLowerCase()))];
}

/** One address, one RFQ. The checkbox list and the hand-typed list are separate
 *  states that can name the same person, and the count shown on the send button has
 *  to be the same list that gets sent — a recipient the operator cannot see is a
 *  duplicate enquiry to a vendor under two references, which cannot be taken back. */
export function dedupeByEmail(recipients: ManualRecipient[]): ManualRecipient[] {
  const seen = new Set<string>();
  const unique: ManualRecipient[] = [];
  for (const r of recipients) {
    const key = r.email.trim().toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    unique.push(r);
  }
  return unique;
}

/** Say something for every outcome. Silence was the actual defect: a typo and an
 *  address that happened to match an agent both cleared the box and left no trace,
 *  which reads as "Add is broken". */
export function describeSplit(split: TokenSplit): ManualNote | null {
  const notes: string[] = [];
  if (split.invalid.length > 0) notes.push(`Not a valid email: ${split.invalid.join(', ')}`);
  if (split.onRoster.length > 0) {
    notes.push(`Already in the agent list — selected ${split.onRoster.map(a => a.agent_name).join(', ')}`);
  }
  if (split.duplicates.length > 0) notes.push(`Already added: ${split.duplicates.join(', ')}`);
  if (notes.length === 0) return null;
  return { text: notes.join(' · '), tone: split.invalid.length > 0 ? 'error' : 'info' };
}
