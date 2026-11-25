# Eval 1 — loop state: a fix placed behind an existing flag (PROJ-450)

`qa-repro` reproduced PROJ-450 ("Completing a recurring task late pushes every later due date"): the
replay oracle is RED. Brief (verbatim): expected "the next due date is the previous due date plus the
interval"; actual "the next due date is the completion date plus the interval".

The target repo (Tasks area) already has a feature-flag client, and its guidelines file says every
change to scheduling rules ships behind a flag. The flag `tasks-recurring-due-dates` exists in code
(`web/src/flags.js` lists it) but has never been created in the feature-flag store.

## Diagnosis already done (Steps 1–4)
- Fault site: `web/src/areas/tasks/recurrence.js:27` `nextDue()` adds `t.every` to `done`.
- Only caller: `web/src/areas/tasks/completeTask.js:44`. No bypassers.
- Unit suite for the area: `web/src/areas/tasks/__tests__/recurrence.test.js` (14 tests, green before).

## The change you make (Step 5)
```js
export const nextDue = (t, done, flags) =>
  flags.on("tasks-recurring-due-dates") ? addDays(t.due, t.every) : addDays(done, t.every);
```
`completeTask.js:44` passes `flags`. Nothing else changes: with the flag off, `nextDue` is exactly the
old expression on every path. A new unit test covers both branches; 16/16 pass; the R6 scan is clean.

Write `fix.json` (Step 8) to your outputs directory through `contracts/emit.py --contract
contracts/dev-fix.json`, plus `notes.md`. Do not commit.
