/**
 * Checks for the Sent folder and thread view helpers.
 *
 *     node --experimental-strip-types frontend/tests/mailbox.check.ts
 */

import assert from 'node:assert/strict';
import { displayName, preview, splitAddresses, splitQuoted } from '../src/lib/mailbox.ts';

// 1. Names out of headers, including a comma inside a quoted name.
{
  assert.equal(displayName('"Kumar, Ravi" <ravi@x.example>'), 'Kumar, Ravi');
  assert.equal(displayName('ops@sunrise.example'), 'ops');
  assert.equal(displayName('a@x.example, "B" <b@y.example>, c@z.example'), 'a +2');
  assert.deepEqual(splitAddresses('"Kumar, Ravi" <r@x>, b@y'), ['"Kumar, Ravi" <r@x>', 'b@y']);
  assert.equal(displayName(''), '');
}

// 2. Gmail quote, wrapped over two lines.
{
  const b = 'Rate is 120/kg.\n\nOn Fri, 17 Oct 2026 at 14:29, Samarth Bhutani <\nbhutani.samarth@gmail.com> wrote:\n> need a quote';
  const s = splitQuoted(b);
  assert.equal(s.main, 'Rate is 120/kg.');
  assert.ok(s.quoted.startsWith('On Fri'));
}

// 3. Outlook header block, and the original-message rule.
{
  const o = 'Noted.\n\nFrom: Ravi <r@x>\nSent: Friday, 17 October 2026 10:00\nTo: desk\nSubject: rates\n\nold text';
  assert.equal(splitQuoted(o).main, 'Noted.');
  assert.equal(splitQuoted('Ok\n-----Original Message-----\nold').main, 'Ok');
}

// 4. Nothing recognisable: nothing folded. A bare quote: shown whole.
{
  assert.deepEqual(splitQuoted('Just text\nOn time delivery matters.'), { main: 'Just text\nOn time delivery matters.', quoted: '' });
  assert.equal(splitQuoted('> all quote').main, '> all quote');
}

// 5. Preview is the first line of the new text, not of the quote.
{
  assert.equal(preview('\n\nThanks, confirmed.\nMore\nOn Mon, 1 Jan, X <x@y> wrote:\n> q'), 'Thanks, confirmed.');
}

console.log('mailbox: ok');
