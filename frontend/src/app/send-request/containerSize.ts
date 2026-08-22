/**
 * Size / Container: picked by the operator, never read off the customer email.
 *
 * The field used to be free text, so every RFQ spelled the same box a different
 * way — "40HC", "40' hc", "2x40 High Cube" — and the vendor draft quoted whatever
 * was typed. The three standard boxes are now fixed strings, and anything else
 * goes through the manual option, which is the only path that can produce text
 * outside that set.
 *
 * The selection is a set with a count per box, not one value: a shipment is
 * regularly a mix (two 20ft and a 40ft HC), and one enquiry has to name all of it
 * with the quantities, because a rate for one box is not a rate for four.
 *
 * `containerSizeText` is what reaches the API's `size` field and, through it, the
 * "Container/Size:" line of the vendor draft, so its order is the option order
 * above rather than the order the boxes were clicked — the same shipment must not
 * produce two different drafts. Every box carries its count, "1 x 40ft HC"
 * included: an unqualified size in a mixed list is the kind of ambiguity that
 * comes back as a quote for the wrong load.
 */

export const CONTAINER_OPTIONS = ['20ft', '40ft', '40ft HC'] as const;

export type ContainerOption = (typeof CONTAINER_OPTIONS)[number];

/** One box is the smallest enquiry; the upper bound keeps a slipped keypress from
 *  asking a vendor to rate 2000 containers. Both bounds are enforced here rather
 *  than by the input's min/max, which a typed or pasted value walks straight past. */
export const MIN_QUANTITY = 1;
export const MAX_QUANTITY = 99;

export interface ContainerSelection {
  /** Standard boxes ticked, held in option order. */
  picked: ContainerOption[];
  /** How many of each box. Kept for every option, ticked or not, so unticking and
   *  re-ticking does not silently reset a count back to one. */
  quantities: Record<ContainerOption, number>;
  /** Whether the manual entry is in use. */
  manualOn: boolean;
  /** Whatever was typed for manual entry. Kept while unticked so re-ticking
   *  restores it, but it only counts towards the value when `manualOn`. */
  manual: string;
}

export const NO_CONTAINERS: ContainerSelection = {
  picked: [],
  quantities: { '20ft': 1, '40ft': 1, '40ft HC': 1 },
  manualOn: false,
  manual: '',
};

/** Tick or untick one standard box. */
export function toggleContainer(
  sel: ContainerSelection, option: ContainerOption,
): ContainerSelection {
  const on = sel.picked.includes(option);
  // Rebuilt from the option list rather than pushed, so the value is a function
  // of which boxes are ticked and not of the order they were ticked in.
  const picked = CONTAINER_OPTIONS.filter(
    o => (o === option ? !on : sel.picked.includes(o)),
  );
  return { ...sel, picked };
}

/** Turn the manual entry on or off. The text itself is left alone. */
export function toggleManual(sel: ContainerSelection): ContainerSelection {
  return { ...sel, manualOn: !sel.manualOn };
}

export function setManualText(sel: ContainerSelection, manual: string): ContainerSelection {
  return { ...sel, manual };
}

/** How many of one box, whether or not it is ticked. */
export function quantityOf(sel: ContainerSelection, option: ContainerOption): number {
  return clampQuantity(sel.quantities[option]);
}

/**
 * A typed quantity, held to 1–99.
 *
 * The spinner is a text field: it sees empty strings mid-edit (select-all then
 * type), pasted junk, and leading zeroes. Anything unreadable falls back to one
 * box rather than to zero or NaN, because an unreadable count must not become a
 * missing count in the enquiry, and out-of-range values are clamped instead of
 * rejected so a stuck keypress cannot leave the field unusable.
 */
export function clampQuantity(raw: string | number): number {
  const n = typeof raw === 'number' ? raw : parseInt(raw.trim(), 10);
  if (!Number.isFinite(n)) return MIN_QUANTITY;
  return Math.min(MAX_QUANTITY, Math.max(MIN_QUANTITY, Math.trunc(n)));
}

/** Set the count for one box. Ticking is separate: a count on its own asks for
 *  nothing until the box is ticked. */
export function setQuantity(
  sel: ContainerSelection, option: ContainerOption, raw: string | number,
): ContainerSelection {
  return { ...sel, quantities: { ...sel.quantities, [option]: clampQuantity(raw) } };
}

/** The size a token names, with any leading count stripped — "2 x 40ft HC" and
 *  "40FT  hc" are the same box, and the enquiry must not list it twice. */
function sizeKey(token: string): string {
  return token
    .replace(/^\s*\d{1,3}\s*(?:x|×|\*)\s*/i, '')
    .trim().toLowerCase().replace(/\s+/g, ' ');
}

/**
 * The `size` string the RFQ is sent with — empty when nothing is chosen, which is
 * what drops the "Container/Size:" line from the draft entirely.
 *
 * Every ticked box is written "<count> x <size>", counts included at one, so the
 * vendor is never left to read a quantity out of a bare list.
 *
 * Manual text is comma-split and trimmed so "40ft, 45ft reefer" arrives as two
 * boxes rather than one long one, and a box already ticked above is not repeated:
 * typing "40ft HC" with 40ft HC ticked must not quote the vendor twice for it. The
 * ticked box wins that clash, since its count is the one on screen next to it.
 */
export function containerSizeText(sel: ContainerSelection): string {
  const manual = sel.manualOn
    ? sel.manual.split(',').map(t => t.trim()).filter(Boolean)
    : [];
  const picked = sel.picked.map(o => `${quantityOf(sel, o)} x ${o}`);
  const seen = new Set<string>();
  const kept: string[] = [];
  for (const token of [...picked, ...manual]) {
    const key = sizeKey(token);
    if (seen.has(key)) continue;
    seen.add(key);
    kept.push(token);
  }
  return kept.join(', ');
}

/** What the closed dropdown reads. */
export function containerSummary(sel: ContainerSelection): string {
  const text = containerSizeText(sel);
  if (text) return text;
  // Ticked "Other" and typed nothing yet: say so, rather than looking untouched.
  if (sel.manualOn) return 'Manual entry — type the size';
  return 'Select size / container...';
}

/** Whether anything at all has been chosen — drives the dropdown's highlight. */
export function hasContainers(sel: ContainerSelection): boolean {
  return containerSizeText(sel) !== '';
}
