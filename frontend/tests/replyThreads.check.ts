/**
 * Checks for how the Shipment card groups an RFQ's replies.
 *
 * There is no test runner in this frontend, so this is a plain script:
 *
 *     node --experimental-strip-types frontend/tests/replyThreads.check.ts
 *
 * (Node >= 22. The repo's default node is 18, so use an explicit newer binary.)
 *
 * What it pins is a reported misread. When an agent replied twice, the panel drew
 * one card per message, and two stacked cards under a single RFQ looked like two
 * different agents had answered — the opposite of the truth, since one job is one
 * agent. Grouping by Gmail thread is what makes a second message read as a
 * follow-up instead of a second respondent.
 */

import assert from 'node:assert/strict';
import { countAgents, groupByThread, senderAddress } from '../src/app/dashboard/replyThreads.ts';

const msg = (id: string, thread_id: string) => ({ id, thread_id });

// 1. THE REPORTED MISREAD: two replies in one thread are one card, newest first.
{
  const groups = groupByThread([msg('m2', 't1'), msg('m1', 't1')]);
  assert.equal(groups.length, 1, 'one thread must render as one card');
  assert.deepEqual(groups[0].map(m => m.id), ['m2', 'm1'],
    'order must survive grouping — the caller reads group[0] as the latest message');
}

// 2. Two real threads stay two cards. Grouping must not over-merge: an agent who
// replies in a fresh thread is a separate conversation, not a follow-up.
{
  const groups = groupByThread([msg('m3', 't2'), msg('m2', 't1'), msg('m1', 't1')]);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups.map(g => g.map(m => m.id)), [['m3'], ['m2', 'm1']],
    'groups come out in first-seen order, which is newest-first from the API');
}

// 3. Messages without a thread id stand alone. Grouping them on '' would collapse
// unrelated mail into one card and assert a conversation that does not exist.
{
  const groups = groupByThread([msg('m1', ''), msg('m2', '')]);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups.map(g => g[0].id), ['m1', 'm2']);
}

// 4. A thread id must never collide with the standalone key space.
{
  const groups = groupByThread([msg('x', ''), msg('other', 'x')]);
  assert.equal(groups.length, 2, "a message whose id equals another's thread id is not a sibling");
}

// 5. Degenerate inputs.
{
  assert.deepEqual(groupByThread([]), []);
  assert.deepEqual(groupByThread([msg('m1', 't1')]).map(g => g.length), [1]);
}

// 6. Agent identity comes from the address, never the display name.
{
  assert.equal(senderAddress('Dhaval Shah <ops@carrier.example>'), 'ops@carrier.example');
  assert.equal(senderAddress('OPS@Carrier.Example'), 'ops@carrier.example');
  assert.equal(senderAddress('"Shah, Dhaval" <ops@carrier.example>'), 'ops@carrier.example');
  assert.equal(senderAddress(''), '');
}

// 7. THE PILL'S NUMBER: two messages from one mailbox are one agent. This is the
// count a desk reads as "how many carriers answered", so a follow-up must not
// inflate it — the card said "replied (2)" on an RFQ sent to a single agent.
{
  assert.equal(countAgents([
    { sender: 'Samarth Bhutani <bhutani.samarth@gmail.com>' },
    { sender: 'Samarth Bhutani <bhutani.samarth@gmail.com>' },
  ]), 1);
  assert.equal(countAgents([
    { sender: 'Ops <ops@one.example>' },
    { sender: 'ops@ONE.example' },
  ]), 1, 'same mailbox, different display name and case');
  assert.equal(countAgents([{ sender: 'a@one.example' }, { sender: 'b@two.example' }]), 2);
}

// 8. A message with no parseable sender is not an agent — there is nobody to
// chase — but it must not throw either.
{
  assert.equal(countAgents([{ sender: '' }, { sender: 'a@one.example' }]), 1);
  assert.equal(countAgents([]), 0);
}

console.log('replyThreads.check.ts: all checks passed');
