/**
 * Checks for the per-category draft panels.
 *
 *     node --experimental-strip-types frontend/tests/categoryDrafts.check.ts
 *
 * The page used to hold one edited subject/body that every recipient received, so
 * the draft could not disagree with the recipient list: whoever was selected at
 * send time got that text. Three keyed drafts introduce state that CAN disagree,
 * and the disagreement is reached by an ordinary sequence of clicks - draft with
 * two forwarders selected, then remember a CHA agent and tick it.
 *
 * Checks 4 and 5 are the ones that matter. They pin that a category which becomes
 * present after seeding is given text and flagged, and that a panel left blank is
 * reported rather than posted, because the server refuses a partial bundle and the
 * operator should learn that from the form, not from a failed send.
 */

import assert from 'node:assert/strict';
import type { CategoryKey } from '../src/app/send-request/categories.ts';
import {
  acknowledgeDraft, describeRedraft, editDraft, missingDrafts, presentCategories,
  recipientsFor, redraft, resetDraft, seedDrafts, syncDrafts, toWireDrafts,
  unreviewedDrafts,
} from '../src/app/send-request/categoryDrafts.ts';
import type {
  CategorisedRecipient, DraftText,
} from '../src/app/send-request/categoryDrafts.ts';

const SEED: DraftText = { subject: 'RFQ subject', body: 'RFQ body' };

const cha: CategorisedRecipient = { agent_name: 'Jeena', email: 'a@x.com', category: 'CHA' };
const fwd: CategorisedRecipient = { agent_name: 'KN', email: 'b@x.com', category: 'FREIGHT_FORWARDER' };
const fwd2: CategorisedRecipient = { agent_name: 'DBS', email: 'c@x.com', category: 'FREIGHT_FORWARDER' };
const car: CategorisedRecipient = { agent_name: 'Maersk', email: 'd@x.com', category: 'CARRIER' };

// 1. Present categories follow CATEGORY_KEYS order, not selection order, and each
// appears once however many agents it holds.
assert.deepEqual(presentCategories([car, fwd, cha, fwd2]),
  ['CHA', 'FREIGHT_FORWARDER', 'CARRIER']);
assert.deepEqual(presentCategories([]), []);

// 2. A panel knows exactly who it is addressed to. This is what the panel header
// lists, and it is the operator's only confirmation of where the text is going.
assert.deepEqual(recipientsFor([cha, fwd, fwd2], 'FREIGHT_FORWARDER').map(r => r.email),
  ['b@x.com', 'c@x.com']);
assert.deepEqual(recipientsFor([cha], 'CARRIER'), []);

// 3. One model draft, copied into every present category. "Same draft to all" is
// the default, and divergence only ever comes from an edit.
{
  const map = seedDrafts(['CHA', 'CARRIER'], SEED);
  assert.deepEqual(Object.keys(map).sort(), ['CARRIER', 'CHA']);
  assert.equal(map.CHA!.body, SEED.body);
  assert.equal(map.CARRIER!.body, SEED.body);
  assert.equal(map.CHA!.edited, false);
  assert.equal(map.CHA!.isNew, false);
  assert.equal(map.FREIGHT_FORWARDER, undefined, 'no panel for an unselected category');
}

// 4. THE ORDERING FAILURE THIS DESIGN CREATES. Draft with forwarders selected, then
// tick a CHA agent: without this, CHA has no text at all and either posts blank or
// falls through to a model call nobody asked for. It is given the seed and flagged.
{
  const seeded = seedDrafts(['FREIGHT_FORWARDER'], SEED);
  const synced = syncDrafts(seeded, ['CHA', 'FREIGHT_FORWARDER'], SEED);
  assert.equal(synced.CHA!.body, SEED.body, 'the late category must not be empty');
  assert.equal(synced.CHA!.isNew, true, 'and must be flagged as unread');
  assert.equal(synced.FREIGHT_FORWARDER!.isNew, false, 'the original is not re-flagged');
  assert.deepEqual(unreviewedDrafts(synced, ['CHA', 'FREIGHT_FORWARDER']), ['CHA']);
  assert.deepEqual(missingDrafts(synced, ['CHA', 'FREIGHT_FORWARDER']), [],
    'seeded, so nothing is blocking the send');
}

// 4b. Deselecting the last agent of a category drops its panel, so no text is shown
// for a category nobody would receive it. An edit to a category still present is
// kept, because re-ticking an agent must not cost the operator their wording.
{
  const both = editDraft(seedDrafts(['CHA', 'CARRIER'], SEED), 'CHA',
    { body: 'my words' }, SEED);
  const synced = syncDrafts(both, ['CHA'], SEED);
  assert.deepEqual(Object.keys(synced), ['CHA']);
  assert.equal(synced.CHA!.body, 'my words', 'an edit survives an unrelated deselect');
}

// 4c. Returns its input untouched when nothing moved. The page derives the synced
// map on every render, so a new object each time would be a render loop.
{
  const map = seedDrafts(['CHA'], SEED);
  assert.equal(syncDrafts(map, ['CHA'], SEED), map, 'same object when unchanged');
}

// 5. A blank panel is reported, not posted. The server refuses a partial bundle, so
// this is what lets the operator find out from the form instead of a failed send.
{
  const map = seedDrafts(['CHA', 'CARRIER'], SEED);
  assert.deepEqual(missingDrafts(map, ['CHA', 'CARRIER']), []);
  assert.deepEqual(missingDrafts(editDraft(map, 'CHA', { body: '' }, SEED),
    ['CHA', 'CARRIER']), ['CHA'], 'an emptied body blocks');
  assert.deepEqual(missingDrafts(editDraft(map, 'CHA', { body: '   \n ' }, SEED),
    ['CHA', 'CARRIER']), ['CHA'], 'whitespace is not a draft');
  assert.deepEqual(missingDrafts(editDraft(map, 'CHA', { subject: '  ' }, SEED),
    ['CHA', 'CARRIER']), ['CHA'], 'a subject is required too');
  assert.deepEqual(missingDrafts({}, ['CHA']), ['CHA'], 'an absent panel blocks');
}

// 6. "Edited" is derived by comparing against the seed, so hand-reverting a change
// honestly clears the badge rather than leaving the panel labelled forever.
{
  let map = seedDrafts(['CHA'], SEED);
  map = editDraft(map, 'CHA', { body: 'changed' }, SEED);
  assert.equal(map.CHA!.edited, true);
  map = editDraft(map, 'CHA', { body: SEED.body }, SEED);
  assert.equal(map.CHA!.edited, false, 'reverted by hand is not edited');
}

// 6b. Editing a newly-appeared panel counts as having read it.
{
  const synced = syncDrafts(seedDrafts(['CARRIER'], SEED), ['CHA', 'CARRIER'], SEED);
  assert.equal(editDraft(synced, 'CHA', { body: 'x' }, SEED).CHA!.isNew, false);
  assert.equal(acknowledgeDraft(synced, 'CHA').CHA!.isNew, false, 'so does opening it');
}

// 6c. Reset puts one panel back without touching the others.
{
  let map = seedDrafts(['CHA', 'CARRIER'], SEED);
  map = editDraft(map, 'CHA', { body: 'mine' }, SEED);
  map = editDraft(map, 'CARRIER', { body: 'theirs' }, SEED);
  const reset = resetDraft(map, 'CHA', SEED);
  assert.equal(reset.CHA!.body, SEED.body);
  assert.equal(reset.CHA!.edited, false);
  assert.equal(reset.CARRIER!.body, 'theirs', 'reset is per panel');
}

// 7. Re-draft replaces untouched panels and spares edited ones. With one draft on
// screen the operator could see what a re-draft cost them; with three panels the
// one they edited may be scrolled out of view, so it is kept and reported.
{
  const fresh: DraftText = { subject: 'New subject', body: 'New body' };
  let map = seedDrafts(['CHA', 'FREIGHT_FORWARDER', 'CARRIER'], SEED);
  map = editDraft(map, 'CHA', { body: 'hand written for customs' }, SEED);

  const out = redraft(map, ['CHA', 'FREIGHT_FORWARDER', 'CARRIER'], fresh);
  assert.deepEqual(out.kept, ['CHA']);
  assert.deepEqual(out.replaced, ['FREIGHT_FORWARDER', 'CARRIER']);
  assert.equal(out.next.CHA!.body, 'hand written for customs', 'the edit survives');
  assert.equal(out.next.CARRIER!.body, fresh.body);
  assert.equal(out.next.CARRIER!.edited, false, 'freshly seeded is not edited');

  // And it says so, naming the panel it spared and how to discard it.
  const said = describeRedraft(out, k => k)!;
  assert.match(said, /2 panels replaced/);
  assert.match(said, /CHA kept your edits/);
  assert.match(said, /Reset to draft/);
}

// 7b. Nothing to report when no edit was at risk. A note after every re-draft would
// train the operator to ignore the one that matters.
{
  const out = redraft(seedDrafts(['CHA'], SEED), ['CHA'], SEED);
  assert.deepEqual(out.kept, []);
  assert.equal(describeRedraft(out, k => k), null);
}

// 7c. Every panel edited means nothing is replaced, which still has to be said or
// the operator will believe the new wording landed somewhere.
{
  let map = seedDrafts(['CHA'], SEED);
  map = editDraft(map, 'CHA', { body: 'mine' }, SEED);
  const out = redraft(map, ['CHA'], { subject: 'x', body: 'y' });
  assert.deepEqual(out.replaced, []);
  assert.match(describeRedraft(out, k => k)!, /Nothing replaced/);
}

// 8. The payload carries present categories only, and only the two fields the
// server needs. `edited` and `isNew` are how the page explains itself to the
// operator; posting them would invite the server to start making decisions on them.
{
  const map = syncDrafts(seedDrafts(['CHA'], SEED), ['CHA', 'CARRIER'], SEED);
  const wire = toWireDrafts(map, ['CHA']);
  assert.deepEqual(Object.keys(wire), ['CHA']);
  assert.deepEqual(Object.keys(wire.CHA).sort(), ['body', 'subject']);
  assert.deepEqual(wire.CHA, { subject: SEED.subject, body: SEED.body });
}

// 8b. A stale key cannot leak into the payload: the categories asked for are the
// ones derived from the recipient list, so a panel for a deselected category is
// simply not read.
{
  const map = seedDrafts(['CHA', 'CARRIER'], SEED);
  assert.deepEqual(Object.keys(toWireDrafts(map, ['CARRIER'])), ['CARRIER']);
}

console.log('all category-draft checks passed');
