/**
 * Checks for the "Add emails manually" decision on the Send RFQ page.
 *
 * There is no test runner in this frontend, so this is a plain script:
 *
 *     node --experimental-strip-types frontend/tests/manualRecipients.check.ts
 *
 * (Node >= 22. The repo's default node is 18, so use an explicit newer binary.)
 *
 * What it pins is one reported bug and its cause. The Add button appeared to stop
 * working: an address that already belonged to a row in the agents table was
 * skipped, the input box was cleared anyway, and nothing was said — so as the
 * agents table grew past a hundred rows, "add this person" silently did nothing
 * more and more often. The second half of the cause was that the old code did this
 * work inside a setState updater while pushing into arrays declared outside it, so
 * the tallies were read back before React had run the updater (no message could
 * ever appear) and the development double-invoke added every address twice.
 */

import assert from 'node:assert/strict';
import {
  dedupeByEmail, describeSplit, mergeManualRecipients, splitManualTokens, tokenizeEmails,
} from '../src/app/send-request/manualRecipients.ts';
import type { ManualRecipient, RosterAgent } from '../src/app/send-request/manualRecipients.ts';

const ROSTER: RosterAgent[] = [
  { id: 7, agent_name: 'A S Vasan', email: 'vishal@asvasan.in' },
  { id: 64, agent_name: 'Maersk', email: 'quotes@maersk.com' },
];
const NONE: ManualRecipient[] = [];

// 1. THE REPORTED BUG: an address already in the agents table used to be dropped
// with no chip and no message. Now it selects that agent and says so.
{
  const s = splitManualTokens(['VISHAL@asvasan.in'], ROSTER, NONE, '');
  assert.deepEqual(s.added, []);
  assert.deepEqual(s.onRoster.map(a => a.id), [7], 'must resolve to the agent id, case-insensitively');
  const note = describeSplit(s)!;
  assert.equal(note.tone, 'info');
  assert.match(note.text, /Already in the agent list — selected A S Vasan/);
}

// 2. A typo used to clear the box silently. Now it is kept for repair and named.
{
  const s = splitManualTokens(tokenizeEmails('dhaval@'), ROSTER, NONE, '');
  assert.deepEqual(s.invalid, ['dhaval@']);
  assert.deepEqual(s.added, []);
  const note = describeSplit(s)!;
  assert.equal(note.tone, 'error');
  assert.match(note.text, /Not a valid email: dhaval@/);
}

// 3. The ordinary path still works, and a typed name is honoured for a lone address.
{
  const s = splitManualTokens(['dhaval@acme.com'], ROSTER, NONE, '  Dhaval  ');
  assert.deepEqual(s.added, [{ agent_name: 'Dhaval', email: 'dhaval@acme.com' }]);
  assert.equal(describeSplit(s), null, 'a clean add says nothing');
}

// 4. A batch mixes buckets; the typed name is ignored for more than one address.
{
  const s = splitManualTokens(
    tokenizeEmails('a@x.com, quotes@maersk.com; nope@, b@y.com'), ROSTER, NONE, 'Dhaval',
  );
  assert.deepEqual(s.added.map(m => m.email), ['a@x.com', 'b@y.com']);
  assert.deepEqual(s.added.map(m => m.agent_name), ['a', 'b'], 'name applies to a single entry only');
  assert.deepEqual(s.onRoster.map(a => a.id), [64]);
  assert.deepEqual(s.invalid, ['nope@']);
  const note = describeSplit(s)!;
  assert.equal(note.tone, 'error', 'an invalid token dominates the tone');
}

// 5. Purity: the old version pushed into arrays declared outside the state updater,
// so React's development double-invoke added everything twice. Same input, same
// output, and repeated calls never accumulate.
{
  const split = () => splitManualTokens(['a@x.com', 'a@x.com'], ROSTER, NONE, '');
  const first = split();
  assert.deepEqual(first.added.map(m => m.email), ['a@x.com'], 'dedup within the batch');
  // The repeat is reported rather than swallowed — the whole point of the fix.
  assert.deepEqual(first.duplicates, ['a@x.com']);
  assert.deepEqual(JSON.stringify(split()), JSON.stringify(first), 'calling twice yields the same split');
}

// 6. Re-adding something already held is reported, not silently ignored.
{
  const held: ManualRecipient[] = [{ agent_name: 'a', email: 'a@x.com' }];
  const s = splitManualTokens(['A@X.com'], ROSTER, held, '');
  assert.deepEqual(s.added, []);
  assert.deepEqual(s.duplicates, ['A@X.com']);
  assert.match(describeSplit(s)!.text, /Already added: A@X.com/);
  assert.equal(describeSplit(s)!.tone, 'info');
}

// 7. Empty / whitespace-only input is a no-op at the tokenizer.
assert.deepEqual(tokenizeEmails('   \n , ; '), []);

// 8. THE SECOND REPORTED BUG: adding one agent by hand read "Send RFQ to 2 agents"
// and sent that vendor two RFQs. The merge updater is what React double-invokes, so
// applying it twice has to be indistinguishable from applying it once.
{
  const added: ManualRecipient[] = [{ agent_name: 'Dhaval', email: 'dhaval@acme.com' }];
  const once = mergeManualRecipients([], added);
  assert.deepEqual(once.map(m => m.email), ['dhaval@acme.com']);
  assert.deepEqual(mergeManualRecipients(once, added), once, 'double-invoke must not append twice');
  // Padding and case differ between the text box and the agents table.
  assert.deepEqual(
    mergeManualRecipients(once, [{ agent_name: 'x', email: ' DHAVAL@Acme.com ' }]), once,
  );
}

// 9. The count on the send button and the posted list are the same array, so an
// address reachable from both the checkbox list and the text box must appear once.
{
  const merged = dedupeByEmail([
    { agent_name: 'Maersk Line', email: 'quotes@maersk.com' },
    { agent_name: 'Dhaval', email: 'dhaval@acme.com' },
    { agent_name: 'quotes', email: 'QUOTES@maersk.com ' },
    { agent_name: 'blank', email: '  ' },
  ]);
  assert.deepEqual(merged.map(m => m.email), ['quotes@maersk.com', 'dhaval@acme.com']);
  assert.deepEqual(merged.map(m => m.agent_name), ['Maersk Line', 'Dhaval'],
    'keep the first spelling — the roster name, not the email local part');
}

console.log('all manual-recipient checks passed');
