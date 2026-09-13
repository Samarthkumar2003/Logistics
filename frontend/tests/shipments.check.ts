/**
 * Checks for the shipment read model: what a card leads with, and what it is never
 * allowed to hide.
 *
 * There is no test runner in this frontend, so this is a plain script:
 *
 *     node --experimental-strip-types frontend/tests/shipments.check.ts
 *
 * (Node >= 22. The repo's default node is 18, so use an explicit newer binary.)
 *
 * The Shipments tab used to render one card per `rfq_jobs` row. Twenty-one rows on
 * file are seven pieces of work, so one enquiry to seven agents about one container
 * to Dammam appeared seven times and the operator had to reconstruct the shipment
 * by eye. Cards are shipments now, and the numbers below are the ones a freight
 * desk acts on — so the rules that keep them honest are pinned here rather than
 * left inside JSX.
 */

import assert from 'node:assert/strict';
import {
  agentTypeLabel, agentTypeSummary, agentsRepliedText, failedSendText,
  requestHref, statusBreakdown, statusInfo, totalText,
} from '../src/lib/shipments.ts';

// 1. Every status a job can hold is worded and coloured deliberately. `send_failed`
// in particular: before it was in the map it fell through to the neutral-grey
// fallback and rendered the raw string, which reads as an ordinary state rather
// than as an RFQ that never left.
{
  assert.equal(statusInfo('rfqs_sent').label, 'RFQs Sent');
  assert.equal(statusInfo('quotes_received').label, 'Quotes Received');
  assert.equal(statusInfo('approved').label, 'Approved');
  assert.equal(statusInfo('send_failed').label, 'Send Failed');
  assert.equal(statusInfo('send_failed').color, 'var(--red)');
  assert.ok(statusInfo('sending').label.startsWith('Sending'));
}

// 2. An unknown status is shown as itself, so a status nobody taught this map about
// looks wrong on screen instead of being mapped onto a plausible default.
{
  assert.equal(statusInfo('cancelled').label, 'cancelled');
  assert.equal(statusInfo('cancelled').color, 'var(--muted-soft)');
}

// 3. The breakdown that rides with the headline chip. This is the half of the
// design that keeps the chip honest: `{approved: 1, rfqs_sent: 2}` leads with
// "Approved", which on its own turns two unanswered agents into a finished job.
{
  assert.deepEqual(statusBreakdown({ approved: 1, rfqs_sent: 2 }), [
    { status: 'rfqs_sent', count: 2 },
    { status: 'approved', count: 1 },
  ]);
}

// 4. Lifecycle order, not the order the object happened to be built in, so the
// row reads as the path a job takes.
{
  assert.deepEqual(
    statusBreakdown({ approved: 1, send_failed: 1, sending: 1, quotes_received: 1, rfqs_sent: 1 })
      .map(b => b.status),
    ['sending', 'rfqs_sent', 'quotes_received', 'approved', 'send_failed'],
  );
}

// 5. A status the order does not know about is appended, never dropped — losing a
// count would make the breakdown add up to less than the RFQ count.
{
  assert.deepEqual(statusBreakdown({ cancelled: 2, rfqs_sent: 1 }), [
    { status: 'rfqs_sent', count: 1 },
    { status: 'cancelled', count: 2 },
  ]);
}

// 6. Zero counts are not rendered as chips reading "0".
{
  assert.deepEqual(statusBreakdown({ rfqs_sent: 0, approved: 2 }), [
    { status: 'approved', count: 2 },
  ]);
  assert.deepEqual(statusBreakdown({}), []);
}

// 7. What kind of agent each RFQ went to. The category is joined back from the
// `agents` roster at read time, so empty is a real answer and is worded as unknown
// rather than defaulted — telling an operator they asked a customs broker when
// they asked nobody of the sort is worse than admitting the roster cannot say.
{
  assert.equal(agentTypeLabel('CHA'), 'CHA');
  assert.equal(agentTypeLabel('FREIGHT_FORWARDER'), 'Forwarder');
  assert.equal(agentTypeLabel('CARRIER'), 'Carrier');
  assert.equal(agentTypeLabel(''), 'Unknown type');
  assert.equal(agentTypeLabel('MANUAL'), 'MANUAL', 'a new category shows as itself');
}

// 8. The mix on the card. Seven RFQs to three kinds of agent is the shipment's
// shape at a glance, and it is the fact the row-per-agent view never showed.
{
  assert.equal(
    agentTypeSummary({ CHA: 3, FREIGHT_FORWARDER: 2, CARRIER: 2 }),
    '3 CHAs · 2 forwarders · 2 carriers',
  );
}

// 9. Singular where it is one, because "1 forwarders" on a card is the kind of
// thing that makes a desk distrust the rest of the numbers.
{
  assert.equal(agentTypeSummary({ CHA: 1, FREIGHT_FORWARDER: 1, CARRIER: 1 }),
               '1 CHA · 1 forwarder · 1 carrier');
}

// 10. Fixed order regardless of key order, so two shipments with the same mix read
// identically.
{
  assert.equal(agentTypeSummary({ CARRIER: 2, CHA: 3, FREIGHT_FORWARDER: 2 }),
               '3 CHAs · 2 forwarders · 2 carriers');
}

// 11. Unknown types are counted out loud and sort last. Three of seven RFQs going
// to unrecognised addresses is something to see, not to round down — six of the
// twenty-one RFQs on file are in exactly this state.
{
  assert.equal(agentTypeSummary({ CHA: 2, '': 1 }), '2 CHAs · 1 unknown type');
  assert.equal(agentTypeSummary({ '': 3 }), '3 unknown types');
  assert.equal(agentTypeSummary({}), '');
}

// 12. Agents replied, not messages. One RFQ is one agent, so this counts agents
// who came back; counting messages would report a correction as a second agent
// competing, and whether to keep waiting is decided off exactly this number.
{
  assert.equal(agentsRepliedText({ agents_replied: 0, rfq_count: 7 }), '0 of 7 agents replied');
  assert.equal(agentsRepliedText({ agents_replied: 1, rfq_count: 1 }), '1 of 1 agent replied');
}

// 13. The warning an "Approved" headline is not allowed to hide.
{
  assert.equal(failedSendText({ send_failed: 0 }), '', 'nothing rendered when none failed');
  assert.equal(failedSendText({ send_failed: 1 }), '1 RFQ never sent');
  assert.equal(failedSendText({ send_failed: 3 }), '3 RFQs never sent');
}

// 14. A send with no source email has no enquiry to open, so the caller gets null
// and disables the control rather than linking somewhere that 404s.
{
  assert.equal(requestHref({ customer_email_id: '1a094edaf01deb41' }), '/request/1a094edaf01deb41');
  assert.equal(requestHref({ customer_email_id: null }), null);
}

// 15. A filled grouping window makes the total a floor, and it has to be worded as
// one. Printing it as a count would state a number the server cannot stand behind.
{
  assert.equal(totalText({ total: 7, truncated: false }), '7 shipments');
  assert.equal(totalText({ total: 1, truncated: false }), '1 shipment');
  assert.equal(totalText({ total: 500, truncated: true }), 'at least 500 shipments');
}

console.log('shipments.check.ts: all assertions passed');
