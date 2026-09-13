/**
 * The shipment read model, shared by the dashboard grid and the detail page.
 *
 * `rfq_jobs` is one row per agent. `GET /shipments` groups those rows back into
 * the customer enquiry they came from, because that is the unit an operator makes
 * a decision about: seven agents asked about one container to Dammam is one
 * shipment, not seven.
 *
 * Everything here is pure and has no React in it, so the rules that matter — which
 * status a card leads with, how an agent's type is worded, when a failed send has
 * to be shown — are checked by `frontend/tests/shipments.check.ts` rather than by
 * reading the JSX.
 */

/** One RFQ inside a shipment: one agent, one reference, one status. */
export interface ShipmentRfq {
  reference: string;
  status: string;
  agent_name: string;
  agent_email: string;
  /** Empty when the roster cannot say. Never render this as a default category. */
  agent_category: string;
  reply_count: number;
  replied: boolean;
  created_at: string;
}

export interface Shipment {
  /** Null for a send with no source email, which therefore has no detail page. */
  customer_email_id: string | null;
  customer_thread_id: string | null;
  customer_email_sender: string;
  customer_email_subject: string;
  shipment_origin: string;
  shipment_destination: string;
  shipment_mode: string;
  shipment_commodity: string;
  shipment_weight_kg: number | null;
  shipment_size: string;
  /** The best status in the shipment. Lossy: always render `statuses` with it. */
  status: string;
  statuses: Record<string, number>;
  /** RFQs that never left. Its own field so it cannot hide behind the headline. */
  send_failed: number;
  rfq_count: number;
  agents_replied: number;
  reply_count: number;
  awaiting: number;
  agents_contacted: string[];
  agent_types: Record<string, number>;
  rfqs: ShipmentRfq[];
  first_sent_at: string;
  last_sent_at: string;
}

export interface ShipmentPage {
  shipments: Shipment[];
  total: number;
  /** True when the server's grouping window filled, making `total` a floor. */
  truncated: boolean;
  has_more: boolean;
}

/* ─── Status chips ───────────────────────────────────────────────── */

export interface StatusStyle {
  label: string;
  color: string;
  bg: string;
  desc: string;
}

/**
 * How one RFQ status is worded and coloured.
 *
 * Shared rather than duplicated per page: the dashboard grid and the detail page
 * show the same statuses, and two copies of this map is how "Send Failed" ends up
 * red in one place and neutral grey in the other.
 */
export function statusInfo(status: string): StatusStyle {
  const map: Record<string, StatusStyle> = {
    rfqs_sent: {
      label: 'RFQs Sent', color: 'var(--blue-soft)', bg: 'var(--status-blue-bg)',
      desc: 'Waiting for agents to reply',
    },
    // The row exists but the mail has not been handed to the provider yet — the
    // backend writes it before sending so a reply can never arrive against a
    // reference with no job. Normally lasts seconds; the label says "sending"
    // rather than anything reassuring because a job still here minutes later
    // means the send died mid-flight and needs a human.
    sending: {
      label: 'Sending…', color: 'var(--amber)', bg: 'var(--status-amber-bg)',
      desc: 'Handing the RFQ to the mail provider',
    },
    quotes_received: {
      label: 'Quotes Received', color: 'var(--green-soft)', bg: 'var(--status-green-bg)',
      desc: 'Quotes in — ready to compare',
    },
    approved: {
      label: 'Approved', color: 'var(--purple)', bg: 'var(--status-purple-bg)',
      desc: 'Shipment confirmed',
    },
    // The RFQ never left. Written by _record_outcome when the sender did not
    // confirm a send, so this job is NOT waiting on an agent — nobody was
    // contacted. Without this entry the fallback below rendered the raw string
    // 'send_failed' in neutral grey, which reads as an ordinary state.
    send_failed: {
      label: 'Send Failed', color: 'var(--red)', bg: 'var(--red-tint)',
      desc: 'RFQ did not send — no agent was contacted',
    },
  };
  return map[status] ?? {
    label: status, color: 'var(--muted-soft)', bg: 'var(--status-neutral-bg)', desc: '',
  };
}

/** Lifecycle order, so a legend or breakdown reads as the path a job takes. */
export const STATUS_ORDER = [
  'sending', 'rfqs_sent', 'quotes_received', 'approved', 'send_failed',
] as const;

/**
 * The full status breakdown, in lifecycle order, for a shipment's chip row.
 *
 * This is the half of the design that keeps the headline honest. A shipment at
 * `{approved: 1, rfqs_sent: 2}` leads with "Approved", which on its own turns two
 * unanswered agents into a finished job. Rendering this next to it is what stops
 * that. Statuses the order does not know about are appended rather than dropped.
 */
export function statusBreakdown(
  statuses: Record<string, number>,
): { status: string; count: number }[] {
  const known = STATUS_ORDER
    .filter(s => (statuses[s] ?? 0) > 0)
    .map(s => ({ status: s as string, count: statuses[s] }));
  const extra = Object.keys(statuses)
    .filter(s => !(STATUS_ORDER as readonly string[]).includes(s) && statuses[s] > 0)
    .sort()
    .map(s => ({ status: s, count: statuses[s] }));
  return [...known, ...extra];
}

/* ─── Agent type ─────────────────────────────────────────────────── */

/** Roster categories, in the order a shipment is usually built up. */
export const AGENT_TYPE_ORDER = ['CHA', 'FREIGHT_FORWARDER', 'CARRIER'] as const;

const TYPE_WORDS: Record<string, { label: string; one: string; many: string }> = {
  CHA: { label: 'CHA', one: 'CHA', many: 'CHAs' },
  FREIGHT_FORWARDER: { label: 'Forwarder', one: 'forwarder', many: 'forwarders' },
  CARRIER: { label: 'Carrier', one: 'carrier', many: 'carriers' },
};

/**
 * What kind of agent this is, for a chip or a table cell.
 *
 * The category is joined back from the `agents` roster at read time, so an empty
 * string is a real answer: that mailbox is not in the roster. It is worded as
 * "Unknown type" rather than defaulted to a category, because guessing here would
 * tell an operator they asked a customs broker when they asked nobody of the sort.
 */
export function agentTypeLabel(category: string): string {
  if (!category) return 'Unknown type';
  // A category nobody has taught this map about shows as itself, so a new roster
  // value looks unfamiliar on screen rather than silently reading as an old one.
  return TYPE_WORDS[category]?.label ?? category;
}

/**
 * The mix of agents a shipment went to: "3 CHAs · 2 forwarders · 2 carriers".
 *
 * Unknown types sort last and are counted out loud rather than omitted — a
 * shipment where three of seven RFQs went to unrecognised addresses is something
 * the desk should see, not something to round down.
 */
export function agentTypeSummary(counts: Record<string, number>): string {
  const known = AGENT_TYPE_ORDER.filter(c => (counts[c] ?? 0) > 0);
  const other = Object.keys(counts)
    .filter(c => c && !(AGENT_TYPE_ORDER as readonly string[]).includes(c) && counts[c] > 0)
    .sort();

  const parts = [...known, ...other].map(c => {
    const n = counts[c];
    const word = TYPE_WORDS[c];
    if (!word) return `${n} ${c}`;
    return `${n} ${n === 1 ? word.one : word.many}`;
  });

  const unknown = counts[''] ?? 0;
  if (unknown > 0) parts.push(`${unknown} unknown type${unknown === 1 ? '' : 's'}`);
  return parts.join(' · ');
}

/* ─── Card wording ───────────────────────────────────────────────── */

/**
 * "2 of 7 agents replied". Agents, not messages.
 *
 * One RFQ is one agent, so this counts references that got a reply. Counting
 * messages instead would report an agent's correction as a second agent
 * competing, and whether to keep waiting is decided off exactly this number.
 */
export function agentsRepliedText(s: Pick<Shipment, 'agents_replied' | 'rfq_count'>): string {
  return `${s.agents_replied} of ${s.rfq_count} agent${s.rfq_count === 1 ? '' : 's'} replied`;
}

/**
 * The warning an "Approved" headline is not allowed to hide: RFQs that never left.
 * Empty string when there are none, so the caller renders nothing.
 */
export function failedSendText(s: Pick<Shipment, 'send_failed'>): string {
  if (!s.send_failed) return '';
  return `${s.send_failed} RFQ${s.send_failed === 1 ? '' : 's'} never sent`;
}

/**
 * Where the shipment's detail page lives, or null when it has none.
 *
 * Null for a send with no source email: there is no enquiry to open, so the
 * caller must disable the control rather than link somewhere that 404s.
 */
export function requestHref(s: Pick<Shipment, 'customer_email_id'>): string | null {
  return s.customer_email_id ? `/request/${s.customer_email_id}` : null;
}

/** How the total is worded. A filled grouping window makes it a floor, not a count. */
export function totalText(page: Pick<ShipmentPage, 'total' | 'truncated'>): string {
  const noun = `shipment${page.total === 1 ? '' : 's'}`;
  return page.truncated ? `at least ${page.total} ${noun}` : `${page.total} ${noun}`;
}
