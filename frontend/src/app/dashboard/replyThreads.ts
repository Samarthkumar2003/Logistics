/**
 * Grouping an RFQ's replies for the Shipment card's reply panel.
 *
 * One card per Gmail thread, not per message. An agent who replies twice is one
 * agent, and rendering a card each read as two different agents having answered
 * — which is exactly the wrong impression, because one job is one agent. Two
 * cards therefore mean two genuinely separate threads.
 *
 * Kept out of page.tsx so it can be checked without a React renderer; see
 * frontend/tests/replyThreads.check.ts.
 */

/** The only fields grouping needs. The panel's Reply type is a superset. */
export interface ThreadMessage {
  id: string;
  thread_id: string;
}

/**
 * The bare address out of a `From` header, lowercased — identity for counting
 * who replied.
 *
 * `Dhaval Shah <ops@carrier.example>` and `ops@carrier.example` are one agent. A
 * display name that changes between replies (signature edits, a phone client)
 * must not read as a second respondent. Mirrors `email_repo.sender_address`.
 */
export function senderAddress(raw: string): string {
  const angled = raw?.match(/<([^>]+)>/);
  return (angled ? angled[1] : raw ?? '').trim().toLowerCase();
}

/**
 * How many distinct agents are represented in these replies.
 *
 * Not `replies.length`. An agent who sends a rate and then a correction is one
 * agent who came back, and two messages counted as two responses say the desk
 * has two quotes to compare when it has one.
 */
export function countAgents(replies: { sender: string }[]): number {
  const seen = new Set<string>();
  for (const r of replies) {
    const addr = senderAddress(r.sender);
    // A message with no parseable sender is still a message, but there is no
    // identity to count, so it is not an agent.
    if (addr) seen.add(addr);
  }
  return seen.size;
}

/**
 * Replies split into threads, preserving the order they arrived in.
 *
 * The API returns newest first, so each group and the groups themselves come out
 * newest first — the caller relies on `group[0]` being the agent's latest word.
 */
export function groupByThread<T extends ThreadMessage>(replies: T[]): T[][] {
  const groups = new Map<string, T[]>();
  for (const r of replies) {
    // A message with no thread id stands alone. Grouping on '' would collapse
    // unrelated mail into a single card and claim it was one conversation.
    const key = r.thread_id ? `thread:${r.thread_id}` : `msg:${r.id}`;
    const group = groups.get(key);
    if (group) group.push(r);
    else groups.set(key, [r]);
  }
  return [...groups.values()];
}
