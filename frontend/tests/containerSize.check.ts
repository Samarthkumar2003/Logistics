/**
 * Checks for the Size / Container picker: what the operator ticks, and what string
 * the vendor draft ends up quoting.
 *
 * There is no test runner in this frontend, so this is a plain script:
 *
 *     node --experimental-strip-types frontend/tests/containerSize.check.ts
 *
 * (Node >= 22. The repo's default node is 18, so use an explicit newer binary.)
 *
 * The field was free text, and its value goes straight into the "Container/Size:"
 * line of the RFQ sent to vendors — so "40HC", "40' hc" and "2x40 High Cube" were
 * all asking for the same box in three ways. The rules below are what keeps the
 * three standard sizes spelled one way, and keep the manual option the only route
 * to anything else.
 */

import assert from 'node:assert/strict';
import {
  CONTAINER_OPTIONS, NO_CONTAINERS, containerSizeText, containerSummary,
  hasContainers, setManualText, toggleContainer, toggleManual,
} from '../src/app/send-request/containerSize.ts';

// 1. Nothing picked sends nothing. An empty `size` is what drops the
// "Container/Size:" line from the draft, rather than printing a blank one.
{
  assert.equal(containerSizeText(NO_CONTAINERS), '');
  assert.equal(hasContainers(NO_CONTAINERS), false);
  assert.equal(containerSummary(NO_CONTAINERS), 'Select size / container...');
}

// 2. The four options the operator gets: three fixed sizes plus manual entry.
{
  assert.deepEqual([...CONTAINER_OPTIONS], ['20ft', '40ft', '40ft HC']);
}

// 3. Each standard size, on its own, is quoted verbatim.
{
  for (const o of CONTAINER_OPTIONS) {
    assert.equal(containerSizeText(toggleContainer(NO_CONTAINERS, o)), o);
  }
}

// 4. MULTI-SELECT: a shipment is regularly a mix, and one enquiry must name all of
// it. The order is the option order, not the click order — the same set of boxes
// has to produce the same draft however it was ticked.
{
  const clicked = toggleContainer(toggleContainer(NO_CONTAINERS, '40ft HC'), '20ft');
  assert.equal(containerSizeText(clicked), '20ft, 40ft HC');
  const other = toggleContainer(toggleContainer(NO_CONTAINERS, '20ft'), '40ft HC');
  assert.equal(containerSizeText(other), containerSizeText(clicked));
}

// 5. Ticking twice unticks. Nothing else in the selection moves.
{
  const on = toggleContainer(toggleContainer(NO_CONTAINERS, '20ft'), '40ft');
  const off = toggleContainer(on, '20ft');
  assert.equal(containerSizeText(off), '40ft');
}

// 6. MANUAL ENTRY: the escape hatch for anything off the list.
{
  const sel = setManualText(toggleManual(NO_CONTAINERS), '45ft reefer');
  assert.equal(containerSizeText(sel), '45ft reefer');
  assert.equal(containerSummary(sel), '45ft reefer');
}

// 7. Manual text mixes with the ticked boxes, standard sizes first.
{
  let sel = toggleContainer(NO_CONTAINERS, '40ft');
  sel = setManualText(toggleManual(sel), '45ft reefer');
  assert.equal(containerSizeText(sel), '40ft, 45ft reefer');
}

// 8. Several typed sizes are separate boxes, not one long string, and stray
// whitespace and empty commas are dropped.
{
  const sel = setManualText(toggleManual(NO_CONTAINERS), ' 45ft reefer ,, 20ft open top ');
  assert.equal(containerSizeText(sel), '45ft reefer, 20ft open top');
}

// 9. NO DOUBLE QUOTE: typing a size that is already ticked must not ask the vendor
// for it twice, however it was capitalised or spaced.
{
  let sel = toggleContainer(NO_CONTAINERS, '40ft HC');
  sel = setManualText(toggleManual(sel), "40FT  hc, 45ft reefer");
  assert.equal(containerSizeText(sel), '40ft HC, 45ft reefer');
}

// 10. Ticked manual entry with nothing typed yet contributes nothing, and the
// closed dropdown says what it is waiting for instead of looking untouched.
{
  const sel = toggleManual(NO_CONTAINERS);
  assert.equal(containerSizeText(sel), '');
  assert.equal(hasContainers(sel), false);
  assert.equal(containerSummary(sel), 'Manual entry — type the size');
}

// 11. Unticking manual entry drops its text from the value but keeps it in state,
// so an accidental untick does not lose what was typed.
{
  const typed = setManualText(toggleManual(NO_CONTAINERS), '45ft reefer');
  const off = toggleManual(typed);
  assert.equal(containerSizeText(off), '');
  assert.equal(off.manual, '45ft reefer');
  assert.equal(containerSizeText(toggleManual(off)), '45ft reefer');
}

// 12. Every helper returns a new selection — React re-renders on identity, and a
// mutated selection would leave the dropdown showing the previous choice.
{
  const before = toggleContainer(NO_CONTAINERS, '20ft');
  const after = toggleContainer(before, '40ft');
  assert.notEqual(before, after);
  assert.equal(containerSizeText(before), '20ft', 'the earlier selection is untouched');
  assert.deepEqual(NO_CONTAINERS, { picked: [], manualOn: false, manual: '' });
}

console.log('containerSize.check.ts: all checks passed');
