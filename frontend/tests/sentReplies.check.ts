/**
 * Checks for how replies sent from the app read on screen.
 *
 *     node --experimental-strip-types frontend/tests/sentReplies.check.ts
 *
 * Live email is armed, so the wording here is not cosmetic. A failed reply must
 * never look sent, and a reply whose outcome nobody knows must never look failed,
 * because "failed" invites a resend and the customer then gets it twice.
 */

import assert from 'node:assert/strict';
import {
  addressOf, canRetry, chipFor, parseCc, replySubject, starterBody, statusFor, withReply,
  STALE_SENDING_MS,
  type ReplySummary, type SentReply,
} from '../src/lib/sentReplies.ts';

const at = '2026-10-11T09:02:00Z';

// 1. Nothing sent, no chip. One or more sent, a Replied chip with the count.
{
  assert.equal(chipFor(null), null);
  assert.equal(chipFor({ sent: 0, failed: 0, last_at: null, last_status: '' }), null);
  const one = chipFor({ sent: 1, failed: 0, last_at: at, last_status: 'sent' });
  assert.ok(one && one.tone === 'ok' && one.text.startsWith('↩ Replied') && !one.text.includes('×'));
  const two = chipFor({ sent: 2, failed: 0, last_at: at, last_status: 'sent' });
  assert.ok(two && two.text.startsWith('↩ Replied 2×'));
}

// 2. The latest attempt decides. A failure after good replies is still a failure.
{
  const c = chipFor({ sent: 2, failed: 1, last_at: at, last_status: 'failed' });
  assert.deepEqual(c, { text: '⚠ Reply failed', tone: 'bad' });
  assert.equal(chipFor({ sent: 0, failed: 0, last_at: at, last_status: 'sending' })?.tone, 'pending');
}

// 3. A `sending` row that is old is "unknown", never "failed".
{
  const now = Date.parse(at);
  assert.equal(statusFor({ status: 'sending', created_at: at }, now + 1000).tone, 'pending');
  const stale = statusFor({ status: 'sending', created_at: at }, now + STALE_SENDING_MS + 1);
  assert.equal(stale.tone, 'unknown');
  assert.ok(!stale.text.toLowerCase().includes('not sent'));
  assert.equal(statusFor({ status: 'sent', created_at: at }).text, '✓ Sent');
  assert.equal(statusFor({ status: 'failed', created_at: at }).tone, 'bad');
}

// 4. Retry only for a failed reply with nothing attached (bytes are not stored).
{
  assert.equal(canRetry({ status: 'failed', attachment_names: [] }), true);
  assert.equal(canRetry({ status: 'failed', attachment_names: ['BL.pdf'] }), false);
  assert.equal(canRetry({ status: 'sent', attachment_names: [] }), false);
  assert.equal(canRetry({ status: 'sending', attachment_names: [] }), false);
}

// 5. Cc parsing accepts the separators people actually type.
{
  assert.deepEqual(parseCc(' a@x.example, b@y.example;c@z.example  d@w.example '),
    ['a@x.example', 'b@y.example', 'c@z.example', 'd@w.example']);
  assert.deepEqual(parseCc('  '), []);
}

// 6. The starter ends in the real signature, or in nothing. Never a placeholder.
{
  assert.equal(starterBody('Priya Shah\nHarbourline Freight'), '\n\nRegards,\nPriya Shah\nHarbourline Freight');
  assert.equal(starterBody('  '), '');
}

// 7. The chip updates locally from the row the server returned, and a retry that
// succeeds moves one failure to one success rather than adding a reply.
{
  const row = (status: string): SentReply => ({
    id: 'r1', email_id: 'm', to_addr: 'c@x.example', cc: [], subject: 'Re: s', body: 'b',
    attachment_names: [], status, error: null, sent_by_name: 'P', created_at: at, sent_at: null,
  });
  let s: ReplySummary = withReply(null, row('failed'));
  assert.deepEqual([s.sent, s.failed, s.last_status], [0, 1, 'failed']);
  s = withReply(s, row('sent'), row('failed'));
  assert.deepEqual([s.sent, s.failed, s.last_status], [1, 0, 'sent']);
}

// 8. Subject and address, as the composer shows them.
{
  assert.equal(replySubject('Rates for Mundra'), 'Re: Rates for Mundra');
  assert.equal(replySubject('RE: Rates'), 'RE: Rates');
  assert.equal(replySubject('  re : Rates'), 're : Rates');
  assert.equal(replySubject(''), 'Re:');
  assert.equal(replySubject('   '), 'Re:');
  assert.equal(addressOf('"Asha K" <asha@cust.example>'), 'asha@cust.example');
  assert.equal(addressOf('asha@cust.example'), 'asha@cust.example');
}

// 9. An ambiguous provider failure reads as unknown, on the card and the row, and
// is never retryable.
{
  assert.equal(chipFor({ sent: 0, failed: 0, last_at: at, last_status: 'unknown' })?.tone, 'unknown');
  assert.equal(statusFor({ status: 'unknown', created_at: at }).tone, 'unknown');
  assert.equal(canRetry({ status: 'unknown', attachment_names: [] }), false);
}

console.log('sentReplies: ok');
