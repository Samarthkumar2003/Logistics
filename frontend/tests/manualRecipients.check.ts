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
 *
 * The roster case is now confirmed visually instead of in prose: the page selects
 * the agent and renders a chip for it, so describeSplit stays silent for it. Checks
 * 1 and 4 pin that silence, because reintroducing the note would tell the operator
 * the same thing twice.
 */

import assert from 'node:assert/strict';
import {
  dedupeByEmail, describeSplit, mergeManualRecipients, splitManualTokens, tokenizeEmails,
} from '../src/app/send-request/manualRecipients.ts';
import type { ManualRecipient, RosterAgent } from '../src/app/send-request/manualRecipients.ts';

const ROSTER: RosterAgent[] = [
  { id: 7, agent_name: 'A S Vasan', email: 'vishal@asvasan.in', category: 'CHA' },
  { id: 64, agent_name: 'Maersk', email: 'quotes@maersk.com', category: 'CARRIER' },
  // Filed under something none of the three panels covers. The agents table has a
  // CHECK constraint making this unreachable in practice; it is here because the
  // consequence if it ever happened is a recipient with no draft to send them.
  { id: 99, agent_name: 'Legacy', email: 'legacy@old.example', category: 'MANUAL' },
];
const NONE: ManualRecipient[] = [];

// 1. THE REPORTED BUG: an address already in the agents table used to be dropped
// with no chip and no message. Now it selects that agent and chips it.
{
  const s = splitManualTokens(['VISHAL@asvasan.in'], ROSTER, NONE, '', 'CHA');
  assert.deepEqual(s.added, []);
  assert.deepEqual(s.onRoster.map(a => a.id), [7], 'must resolve to the agent id, case-insensitively');
  // Deliberately silent: the page selects the agent AND renders a chip for it, so a
  // note here would be a second, weaker copy of confirmation the operator can see.
  assert.equal(describeSplit(s), null, 'a roster hit is chipped, not narrated');
}

// 2. A typo used to clear the box silently. Now it is kept for repair and named.
{
  const s = splitManualTokens(tokenizeEmails('dhaval@'), ROSTER, NONE, '', 'CHA');
  assert.deepEqual(s.invalid, ['dhaval@']);
  assert.deepEqual(s.added, []);
  const note = describeSplit(s)!;
  assert.equal(note.tone, 'error');
  assert.match(note.text, /Not a valid email: dhaval@/);
}

// 3. The ordinary path still works, and a typed name is honoured for a lone address.
{
  const s = splitManualTokens(['dhaval@acme.com'], ROSTER, NONE, '  Dhaval  ', 'CHA');
  assert.deepEqual(s.added, [
    { agent_name: 'Dhaval', email: 'dhaval@acme.com', category: 'CHA' },
  ], 'the chosen category is stamped on at add time, not looked up later');
  assert.equal(describeSplit(s), null, 'a clean add says nothing');
}

// 4. A batch mixes buckets; the typed name is ignored for more than one address.
{
  const s = splitManualTokens(
    tokenizeEmails('a@x.com, quotes@maersk.com; nope@, b@y.com'), ROSTER, NONE, 'Dhaval', 'CARRIER',
  );
  assert.deepEqual(s.added.map(m => m.email), ['a@x.com', 'b@y.com']);
  assert.deepEqual(s.added.map(m => m.agent_name), ['a', 'b'], 'name applies to a single entry only');
  assert.deepEqual(s.onRoster.map(a => a.id), [64]);
  assert.deepEqual(s.invalid, ['nope@']);
  const note = describeSplit(s)!;
  assert.equal(note.tone, 'error', 'an invalid token dominates the tone');
  assert.ok(!note.text.includes('Maersk'), 'a roster hit is chipped, not narrated');
}

// 5. Purity: the old version pushed into arrays declared outside the state updater,
// so React's development double-invoke added everything twice. Same input, same
// output, and repeated calls never accumulate.
{
  const split = () => splitManualTokens(['a@x.com', 'a@x.com'], ROSTER, NONE, '', 'CHA');
  const first = split();
  assert.deepEqual(first.added.map(m => m.email), ['a@x.com'], 'dedup within the batch');
  // The repeat is reported rather than swallowed — the whole point of the fix.
  assert.deepEqual(first.duplicates, ['a@x.com']);
  assert.deepEqual(JSON.stringify(split()), JSON.stringify(first), 'calling twice yields the same split');
}

// 6. Re-adding something already held is reported, not silently ignored.
{
  const held: ManualRecipient[] = [{ agent_name: 'a', email: 'a@x.com', category: 'CHA' }];
  const s = splitManualTokens(['A@X.com'], ROSTER, held, '', 'CHA');
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
  const added: ManualRecipient[] = [
    { agent_name: 'Dhaval', email: 'dhaval@acme.com', category: 'CHA' },
  ];
  const once = mergeManualRecipients([], added);
  assert.deepEqual(once.map(m => m.email), ['dhaval@acme.com']);
  assert.deepEqual(mergeManualRecipients(once, added), once, 'double-invoke must not append twice');
  // Padding and case differ between the text box and the agents table.
  assert.deepEqual(
    mergeManualRecipients(once, [
      { agent_name: 'x', email: ' DHAVAL@Acme.com ', category: 'CARRIER' },
    ]), once,
  );
}

// 9. The count on the send button and the posted list are the same array, so an
// address reachable from both the checkbox list and the text box must appear once.
{
  const merged = dedupeByEmail([
    { agent_name: 'Maersk Line', email: 'quotes@maersk.com', category: 'CARRIER' },
    { agent_name: 'Dhaval', email: 'dhaval@acme.com', category: 'CHA' },
    { agent_name: 'quotes', email: 'QUOTES@maersk.com ', category: 'CHA' },
    { agent_name: 'blank', email: '  ', category: 'CHA' },
  ]);
  assert.deepEqual(merged.map(m => m.email), ['quotes@maersk.com', 'dhaval@acme.com']);
  assert.deepEqual(merged.map(m => m.agent_name), ['Maersk Line', 'Dhaval'],
    'keep the first spelling — the roster name, not the email local part');
}

// 10. THE CATEGORY IS MANDATORY. A valid, new address with no category chosen is
// held back and named, because the category decides which draft it would be sent
// and there is nothing safe to guess. Silently adding it under some default is the
// failure worth preventing: it would reach a vendor with another kind of agent's
// wording, and nothing on screen would say so.
{
  const s = splitManualTokens(['dhaval@acme.com'], ROSTER, NONE, '', '');
  assert.deepEqual(s.added, [], 'nothing is added without a category');
  assert.deepEqual(s.needsCategory, ['dhaval@acme.com']);
  const note = describeSplit(s)!;
  assert.equal(note.tone, 'error', 'a refusal is not an FYI');
  assert.match(note.text, /Pick what kind of agent this is first/);
}

// 11. A roster hit does NOT need the dropdown - it brings its own category from the
// agents table. Ticking the box in a collapsed dropdown is invisible, so this is
// the path that must keep working without a category chosen.
{
  const s = splitManualTokens(['quotes@maersk.com'], ROSTER, NONE, '', '');
  assert.deepEqual(s.onRoster.map(a => a.id), [64]);
  assert.deepEqual(s.needsCategory, [], 'the roster row already knows what it is');
  assert.equal(describeSplit(s), null, 'still chipped, still not narrated');
}

// 12. A roster row filed outside the three categories is refused, not routed. There
// would be no panel carrying its text, so joining the recipient list would mean
// either a silent drop or a send of whatever draft happened to be first.
{
  const s = splitManualTokens(['legacy@old.example'], ROSTER, NONE, '', 'CHA');
  assert.deepEqual(s.onRoster, [], 'MANUAL is not one of the three');
  assert.deepEqual(s.added, [], 'and it is not treated as a new address either');
  assert.deepEqual(s.uncategorisedRoster, ['legacy@old.example']);
  const note = describeSplit(s)!;
  assert.equal(note.tone, 'error');
  assert.match(note.text, /no draft covers them/);
  assert.match(note.text, /fix the category on the agents table/);
}

// 13. A batch can need a category for one address while another is a roster hit.
// Both outcomes have to survive being mixed, or the note would explain one and
// swallow the other.
{
  const s = splitManualTokens(
    tokenizeEmails('new@x.com, quotes@maersk.com'), ROSTER, NONE, '', '',
  );
  assert.deepEqual(s.needsCategory, ['new@x.com']);
  assert.deepEqual(s.onRoster.map(a => a.id), [64]);
  assert.match(describeSplit(s)!.text, /new@x.com/);
}

console.log('all manual-recipient checks passed');
