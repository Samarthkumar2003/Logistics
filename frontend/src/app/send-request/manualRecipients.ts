/**
 * Hand-typed RFQ recipients.
 *
 * Split out of the page because this is the whole of the "Add emails manually"
 * decision and it is worth testing on its own. The bug it exists to prevent was
 * not a wrong decision but a silent one: every rejection path — a typo, an
 * address that already belonged to an agent — cleared the input box and produced
 * no message, so the Add button looked broken. The rule here is that every token
 * lands in exactly one bucket and no bucket is discarded in silence: three are
 * reported as text, and `onRoster` is confirmed by the recipient chip it produces.
 */

import { isCategory, type CategoryChoice, type CategoryKey } from './categories';

/** An ad-hoc recipient typed in by hand, not backed by a row in the agents table.
 *
 *  `category` is required and has no default. It is not a label: it decides which
 *  of the three draft panels this address is sent, so guessing one would mean a
 *  customs agent receiving the text written for a shipping line. */
export interface ManualRecipient {
  agent_name: string;
  email: string;
  category: CategoryKey;
}

/** The part of an agent row this decision needs. `Agent` satisfies it structurally. */
export interface RosterAgent {
  id: number;
  agent_name: string;
  email: string;
  /** Raw from the agents table, so typed loosely and validated here rather than
   *  trusted. A row outside the three real categories has no draft panel to
   *  belong to, and is refused below instead of being quietly routed. */
  category: string;
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
  /** Valid, new addresses held back because no category was chosen. There is no
   *  sensible default: the category picks the draft text they receive. */
  needsCategory: string[];
  /** On the roster, but filed under something other than the three categories,
   *  so no panel would carry their text. Refused and named, rather than guessed
   *  at - the agents table has a CHECK constraint making this unreachable, and
   *  if it ever fires the fix is in the data, not here. */
  uncategorisedRoster: string[];
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
  category: CategoryChoice,
): TokenSplit {
  const roster = new Map(agents.map(a => [a.email.toLowerCase(), a]));
  const held = new Set(manual.map(m => m.email.toLowerCase()));
  const split: TokenSplit = {
    added: [], onRoster: [], duplicates: [], invalid: [],
    needsCategory: [], uncategorisedRoster: [],
  };

  for (const email of tokens) {
    const lower = email.toLowerCase();
    if (!EMAIL_RE.test(email)) { split.invalid.push(email); continue; }
    const existing = roster.get(lower);
    if (existing) {
      // A roster hit carries its own category from the agents table, so the
      // dropdown is not consulted for it and may legitimately be empty.
      if (isCategory(existing.category)) split.onRoster.push(existing);
      else split.uncategorisedRoster.push(email);
      continue;
    }
    if (held.has(lower)) { split.duplicates.push(email); continue; }
    // Checked after the duplicate test on purpose: telling the operator to pick a
    // category for an address they have already added would be the less useful of
    // the two messages.
    if (!isCategory(category)) { split.needsCategory.push(email); continue; }
    held.add(lower);                      // dedup within this batch as well
    split.added.push({
      // A typed name applies only when a single address was entered.
      agent_name: (tokens.length === 1 && typedName.trim())
        ? typedName.trim()
        : email.split('@')[0],
      email,
      category,
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

/** Say something for the outcomes the operator cannot otherwise see.
 *
 *  Silence was the original defect: a typo and an address that already belonged
 *  to an agent both cleared the box and left no trace, which reads as "Add is
 *  broken". Note what is deliberately NOT reported here: `onRoster`. That case
 *  is shown instead as a chip in the recipient row, because a message the
 *  operator has to read is weaker confirmation than seeing the recipient appear.
 *  Do not restore a note for it without removing the chip. */
export function describeSplit(split: TokenSplit): ManualNote | null {
  const notes: string[] = [];
  if (split.invalid.length > 0) notes.push(`Not a valid email: ${split.invalid.join(', ')}`);
  if (split.duplicates.length > 0) notes.push(`Already added: ${split.duplicates.join(', ')}`);
  // Both of these are refusals, so both must speak. Silence here would read as
  // the Add button doing nothing, which is the exact defect this file exists for.
  if (split.needsCategory.length > 0) {
    notes.push(`Pick what kind of agent this is first: ${split.needsCategory.join(', ')}`);
  }
  if (split.uncategorisedRoster.length > 0) {
    notes.push(
      `Not filed as a CHA, forwarder or carrier, so no draft covers them: ` +
      `${split.uncategorisedRoster.join(', ')} - fix the category on the agents table`,
    );
  }
  if (notes.length === 0) return null;
  const isError = split.invalid.length > 0
    || split.needsCategory.length > 0
    || split.uncategorisedRoster.length > 0;
  return { text: notes.join(' · '), tone: isError ? 'error' : 'info' };
}
