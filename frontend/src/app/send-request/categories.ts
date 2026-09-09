/**
 * The three kinds of freight vendor an RFQ can go to.
 *
 * Split into its own module because this list is no longer only a way to group
 * the dropdowns: a recipient's category now decides WHICH DRAFT TEXT they are
 * sent. That makes the set of valid keys a correctness concern rather than a
 * presentation one, so it lives somewhere a test can import it.
 *
 * These keys must match the `category` column on the agents table. The database
 * has a CHECK constraint pinning the same three values, so a row that does not
 * map to a key here cannot exist. `isCategory` is the guard for everything that
 * arrives from outside anyway - a stale browser tab, a hand-edited request.
 */

export const CATEGORY_KEYS = ['CHA', 'FREIGHT_FORWARDER', 'CARRIER'] as const;

export type CategoryKey = (typeof CATEGORY_KEYS)[number];

/** Empty string is "the operator has not chosen yet" - not a valid category. */
export type CategoryChoice = CategoryKey | '';

export const CATEGORY_LABELS: Record<CategoryKey, string> = {
  CHA: 'CHA (Origin / Customs)',
  FREIGHT_FORWARDER: 'Freight Forwarders (Destination)',
  CARRIER: 'Carriers (Shipping Lines)',
};

/** Shorter form, for places where the full label will not fit. */
export const CATEGORY_SHORT: Record<CategoryKey, string> = {
  CHA: 'CHA',
  FREIGHT_FORWARDER: 'Forwarder',
  CARRIER: 'Carrier',
};

export function isCategory(value: unknown): value is CategoryKey {
  return typeof value === 'string' && (CATEGORY_KEYS as readonly string[]).includes(value);
}

/** For a <select>. Ordered as CATEGORY_KEYS, so every list on the page agrees. */
export const CATEGORY_OPTIONS: { key: CategoryKey; label: string }[] =
  CATEGORY_KEYS.map(key => ({ key, label: CATEGORY_LABELS[key] }));
