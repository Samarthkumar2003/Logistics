/**
 * Size / Container: picked by the operator, never read off the customer email.
 *
 * The field used to be free text, so every RFQ spelled the same box a different
 * way — "40HC", "40' hc", "2x40 High Cube" — and the vendor draft quoted whatever
 * was typed. The three standard boxes are now fixed strings, and anything else
 * goes through the manual option, which is the only path that can produce text
 * outside that set.
 *
 * The selection is a set, not one value: a shipment is regularly a mix (two 20ft
 * and a 40ft HC), and one enquiry has to name all of it.
 *
 * `containerSizeText` is what reaches the API's `size` field and, through it, the
 * "Container/Size:" line of the vendor draft, so its order is the option order
 * above rather than the order the boxes were clicked — the same shipment must not
 * produce two different drafts.
 */

export const CONTAINER_OPTIONS = ['20ft', '40ft', '40ft HC'] as const;

export type ContainerOption = (typeof CONTAINER_OPTIONS)[number];

export interface ContainerSelection {
  /** Standard boxes ticked, held in option order. */
  picked: ContainerOption[];
  /** Whether the manual entry is in use. */
  manualOn: boolean;
  /** Whatever was typed for manual entry. Kept while unticked so re-ticking
   *  restores it, but it only counts towards the value when `manualOn`. */
  manual: string;
}

export const NO_CONTAINERS: ContainerSelection = {
  picked: [], manualOn: false, manual: '',
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

/**
 * The `size` string the RFQ is sent with — empty when nothing is chosen, which is
 * what drops the "Container/Size:" line from the draft entirely.
 *
 * Manual text is comma-split and trimmed so "40ft, 45ft reefer" arrives as two
 * boxes rather than one long one, and a box already ticked above is not repeated:
 * typing "40ft HC" with 40ft HC ticked must not quote the vendor "40ft HC, 40ft HC".
 */
export function containerSizeText(sel: ContainerSelection): string {
  const manual = sel.manualOn
    ? sel.manual.split(',').map(t => t.trim()).filter(Boolean)
    : [];
  const seen = new Set<string>();
  const kept: string[] = [];
  for (const token of [...sel.picked, ...manual]) {
    const key = token.toLowerCase().replace(/\s+/g, ' ');
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
