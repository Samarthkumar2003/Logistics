/**
 * One RFQ draft per kind of vendor.
 *
 * The page used to hold a single edited subject/body that every recipient
 * received. Now each category gets its own editable copy, so the operator can
 * reword the enquiry for a customs agent without touching what the carriers are
 * sent. One model call still produces one draft; it is copied into every panel,
 * and panels diverge only where the operator edits them.
 *
 * The rule that matters, and the reason this is a module with tests rather than
 * a few useState calls: ONCE PANELS ARE SEEDED, EVERY CATEGORY SENDS ITS PANEL
 * TEXT VERBATIM. An unedited panel sends the model draft the operator looked at.
 * It must never fall through to a fresh model call at send time, because that
 * substitutes text nobody reviewed for text somebody did, on the one path that
 * reaches a real freight vendor.
 *
 * The failure this design creates - and `syncDrafts` exists to close - is
 * ordering. Draft with two forwarders selected, then tick a CHA agent, and that
 * agent belongs to a category the map was never seeded for. With a single shared
 * draft that could not happen; with three keyed drafts it happens the first time
 * somebody remembers a recipient late.
 */

import { CATEGORY_KEYS, type CategoryKey } from './categories';

/** What actually gets posted for one category. */
export interface DraftText {
  subject: string;
  body: string;
}

export interface CategoryDraft {
  subject: string;
  body: string;
  /** Text differs from the seed. Shown in the panel header so an operator who
   *  edited one of three panels can see at a glance that the other two are
   *  going out as the model wrote them, on purpose, rather than being stale. */
  edited: boolean;
  /** Category became present after seeding. Auto-filled from the seed and
   *  flagged, because the operator has not read this panel in context yet. */
  isNew: boolean;
}

export type DraftMap = Partial<Record<CategoryKey, CategoryDraft>>;

/** A chosen recipient. Category is required: it is the routing key. */
export interface CategorisedRecipient {
  agent_name: string;
  email: string;
  category: CategoryKey;
}

function blank(text: DraftText | null): DraftText {
  return { subject: text?.subject ?? '', body: text?.body ?? '' };
}

function differs(draft: DraftText, seed: DraftText | null): boolean {
  const base = blank(seed);
  return draft.subject !== base.subject || draft.body !== base.body;
}

/** Categories with at least one recipient, always in CATEGORY_KEYS order so
 *  every list and panel stack on the page reads the same way. */
export function presentCategories(recipients: CategorisedRecipient[]): CategoryKey[] {
  const held = new Set(recipients.map(r => r.category));
  return CATEGORY_KEYS.filter(k => held.has(k));
}

/** Who this panel's text is addressed to, in the order the page gathered them. */
export function recipientsFor(
  recipients: CategorisedRecipient[], category: CategoryKey,
): CategorisedRecipient[] {
  return recipients.filter(r => r.category === category);
}

/** First seeding: the one model draft, copied into every present category. */
export function seedDrafts(present: CategoryKey[], seed: DraftText): DraftMap {
  const next: DraftMap = {};
  for (const key of present) {
    next[key] = { ...blank(seed), edited: false, isNew: false };
  }
  return next;
}

/**
 * Re-align an existing map with the current recipient list.
 *
 * This is the guard on the ordering failure described at the top of the file. A
 * category that gains its first recipient after seeding is given the seed text
 * and marked `isNew`, so it is never posted empty and never silently falls back
 * to a model call. A category that loses its last recipient is dropped, so no
 * panel is shown for text that would reach nobody - but an edited draft is kept
 * if the category is still present, because re-ticking an agent should not cost
 * the operator their wording.
 *
 * Returns `prev` unchanged when nothing moved, so this is safe to call on every
 * render without churning state.
 */
export function syncDrafts(
  prev: DraftMap, present: CategoryKey[], seed: DraftText | null,
): DraftMap {
  const wanted = new Set(present);
  const next: DraftMap = {};
  let changed = false;

  for (const key of present) {
    const existing = prev[key];
    if (existing) {
      next[key] = existing;
      continue;
    }
    // Newly present. With no seed there is nothing to copy, which happens only
    // when the operator has not drafted yet - then there is no map to sync.
    next[key] = { ...blank(seed), edited: false, isNew: true };
    changed = true;
  }
  for (const key of Object.keys(prev) as CategoryKey[]) {
    if (!wanted.has(key)) changed = true;
  }
  return changed ? next : prev;
}

/** Apply an edit and re-derive `edited` by comparing against the seed, so
 *  hand-reverting a change honestly clears the flag rather than leaving a panel
 *  labelled as edited forever. */
export function editDraft(
  prev: DraftMap, category: CategoryKey, patch: Partial<DraftText>,
  seed: DraftText | null,
): DraftMap {
  const current = prev[category];
  if (!current) return prev;
  const merged: DraftText = {
    subject: patch.subject ?? current.subject,
    body: patch.body ?? current.body,
  };
  return {
    ...prev,
    [category]: { ...merged, edited: differs(merged, seed), isNew: false },
  };
}

/** Put one panel back to the model draft. The explicit way to discard an edit,
 *  which is why `redraft` below never does it silently. */
export function resetDraft(
  prev: DraftMap, category: CategoryKey, seed: DraftText | null,
): DraftMap {
  if (!prev[category]) return prev;
  return {
    ...prev,
    [category]: { ...blank(seed), edited: false, isNew: false },
  };
}

/** Clear the `isNew` marker once the operator has actually looked at the panel. */
export function acknowledgeDraft(prev: DraftMap, category: CategoryKey): DraftMap {
  const current = prev[category];
  if (!current || !current.isNew) return prev;
  return { ...prev, [category]: { ...current, isNew: false } };
}

export interface RedraftResult {
  next: DraftMap;
  /** Panels overwritten with the new model draft. */
  replaced: CategoryKey[];
  /** Panels left alone because the operator had edited them. */
  kept: CategoryKey[];
}

/**
 * Fetch-a-new-draft, without throwing away work.
 *
 * With a single draft, re-drafting replaced the one thing on screen and the
 * operator could see exactly what they lost. With three panels and one edited,
 * the same behaviour discards an edit that is scrolled out of view. So an edited
 * panel is kept and reported; `resetDraft` is the explicit way to drop one.
 */
export function redraft(
  prev: DraftMap, present: CategoryKey[], seed: DraftText,
): RedraftResult {
  const next: DraftMap = {};
  const replaced: CategoryKey[] = [];
  const kept: CategoryKey[] = [];

  for (const key of present) {
    const existing = prev[key];
    if (existing?.edited) {
      next[key] = { ...existing, isNew: false };
      kept.push(key);
      continue;
    }
    next[key] = { ...blank(seed), edited: false, isNew: false };
    replaced.push(key);
  }
  return { next, replaced, kept };
}

/**
 * Present categories with no sendable text.
 *
 * Checked before the request leaves the browser, and again on the server, which
 * refuses the whole send rather than model-drafting the gap. A blank panel is an
 * operator error worth stopping for; quietly generating replacement text would
 * send a vendor an enquiry nobody read.
 */
export function missingDrafts(map: DraftMap, present: CategoryKey[]): CategoryKey[] {
  return present.filter(key => {
    const draft = map[key];
    return !draft || !draft.subject.trim() || !draft.body.trim();
  });
}

/** Panels the operator has not looked at since they appeared. Not fatal - the
 *  send is allowed - but worth saying out loud before it goes to a vendor. */
export function unreviewedDrafts(map: DraftMap, present: CategoryKey[]): CategoryKey[] {
  return present.filter(key => map[key]?.isNew === true);
}

/**
 * The payload. Present categories only, and only the two fields the server
 * needs: `edited` and `isNew` are how the page explains itself to the operator
 * and are none of the server's business. It sends whatever text it is given.
 */
export function toWireDrafts(
  map: DraftMap, present: CategoryKey[],
): Record<string, DraftText> {
  const wire: Record<string, DraftText> = {};
  for (const key of present) {
    const draft = map[key];
    if (!draft) continue;
    wire[key] = { subject: draft.subject, body: draft.body };
  }
  return wire;
}

/** What a re-draft did, in words, or null when there is nothing worth saying.
 *
 *  Exists because the destructive half of a re-draft is invisible: the panels it
 *  replaced may well be scrolled out of view, and the ones it spared need to be
 *  reported or the operator will assume the new text is everywhere.
 */
export function describeRedraft(
  result: RedraftResult, label: (key: CategoryKey) => string,
): string | null {
  const { replaced, kept } = result;
  if (kept.length === 0) return null;
  const keptNames = kept.map(label).join(', ');
  if (replaced.length === 0) {
    return `Nothing replaced - ${keptNames} kept your edits. Use Reset to draft to discard them.`;
  }
  return `${replaced.length} panel${replaced.length === 1 ? '' : 's'} replaced. `
    + `${keptNames} kept your edits - use Reset to draft to discard them.`;
}
