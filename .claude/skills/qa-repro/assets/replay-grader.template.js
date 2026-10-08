// replay-grader.js — <KEY>. Run by replay-oracle.sh through `playwright-cli run-code --filename`.
// Must be ONE function expression: no import/require, no `process` (run-code has neither), and the
// function's return value is the verdict. `pass` means the CORRECT behavior holds: false with the bug
// present (RED), true once fixed (GREEN). Read state independently: the DOM after a reload, an API
// response, a count. Never "a success toast appeared". Locate by role/label/CSS, never a snapshot
// ref (e12) — refs are renumbered on every run. If it throws, the oracle exits 2 (BLOCKED).
// Replace the placeholder return: `pass: null` is BLOCKED, so an unwritten grader can never pass as RED.
async page => {
  await page.waitForLoadState('networkidle').catch(() => {});
  // const rows = await page.getByRole('row', { name: /<the removed item>/ }).count();
  // return { pass: rows === 0, observed: `rows matching after reload: ${rows}` };
  return { pass: null, observed: 'grader not written yet' };
}
