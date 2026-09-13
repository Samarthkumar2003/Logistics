/**
 * Checks for the vendor-category list.
 *
 *     node --experimental-strip-types frontend/tests/categories.check.ts
 *
 * Thin, but not pointless. This list stopped being presentation the moment a
 * recipient's category began deciding which draft they are sent, and it is now
 * duplicated in three places that must agree: here, CATEGORY_KEYS in
 * backend/app/routes/rfq.py, and the CHECK constraint on agents.category. A key
 * added on one side only is a recipient with no draft panel, or a 422 on a
 * perfectly ordinary send.
 */

import assert from 'node:assert/strict';
import {
  CATEGORY_KEYS, CATEGORY_LABELS, CATEGORY_OPTIONS, CATEGORY_SHORT, isCategory,
} from '../src/app/send-request/categories.ts';

// 1. Exactly three, in a fixed order. The order is what makes every list and panel
// stack on the page read the same way, so it is asserted rather than assumed.
assert.deepEqual([...CATEGORY_KEYS], ['CHA', 'FREIGHT_FORWARDER', 'CARRIER']);

// 2. Every key is presentable, both long and short. A missing label renders as
// "undefined" in a panel header telling the operator who a draft is going to.
for (const key of CATEGORY_KEYS) {
  assert.ok(CATEGORY_LABELS[key], `no long label for ${key}`);
  assert.ok(CATEGORY_SHORT[key], `no short label for ${key}`);
}

// 3. "Shipping line" is the CARRIER key. Worth pinning because the operator's
// vocabulary and the column value differ, and inventing a SHIPPING_LINE key would
// break the database constraint.
assert.match(CATEGORY_LABELS.CARRIER, /Shipping Lines/);

// 4. The guard. Everything arriving from outside - a stale tab, the agents table,
// a hand-edited request - goes through this rather than being trusted.
assert.ok(isCategory('CHA'));
assert.ok(isCategory('CARRIER'));
for (const bad of ['MANUAL', '', '   ', 'cha', 'Carrier', 'OTHER', null, undefined, 7, {}]) {
  assert.equal(isCategory(bad), false, `${JSON.stringify(bad)} must not pass as a category`);
}

// 5. The dropdown options are the keys, in the same order, with their long labels.
assert.deepEqual(CATEGORY_OPTIONS.map(o => o.key), [...CATEGORY_KEYS]);
assert.deepEqual(
  CATEGORY_OPTIONS.map(o => o.label),
  CATEGORY_KEYS.map(k => CATEGORY_LABELS[k]),
);

console.log('all category checks passed');
