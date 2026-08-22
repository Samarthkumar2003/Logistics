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
