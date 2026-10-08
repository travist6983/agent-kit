# Loop state — PROJ-310 (dev-loop-hard-defect), end of loop, ready to ship

Synthetic scenario built to contain items that genuinely need the reviewer before merge, mixed with
routine calls. Everything upstream is done and green; what remains is dev-fix's final `fix.json`
(Step 8) and infra-ship's PR body (Step 4) + threads (Step 6).

## Ticket
- **PROJ-310** — Tasks board: the due-date week picker starts weeks on **Sunday** even though the
  workspace is configured to start on **Monday**, so picking "this week" selects the wrong 7 days.
  Reported on the Tasks board only. Release target: **Release 2.5.0**.
- Branch: `fix/PROJ-310-datepicker-week-start`. Routed to hard-defect: the fix is in the shared
  design-system package.

## Root cause (diagnosed, verified)
`packages/ui/src/DatePicker.js` hard-codes `weekStartsOn: 0` (Sunday) when it builds its calendar grid.
It ignores the workspace's `weekStartDay` setting entirely. The Tasks board is just where the mismatch is
most visible — every one of the **14 surfaces** that render `<DatePicker>` (task due dates, project
milestones, invoice due dates, the comment date filter, member start dates, …) draws Sunday-start weeks.

## What the fix changed
- `packages/ui/src/DatePicker.js:33` — reads `workspace.settings.weekStartDay` and falls back to
  **Monday** when a workspace has no setting. This changes the calendar for **all 14 consumers**, not just
  the Tasks board: every workspace with no `weekStartDay` set (about 60% of workspaces per the settings
  table) will see Monday-start weeks everywhere from this release on, where they saw Sunday before. The
  loop judged this correct (the product's own weekly cadence is Monday-based), but the ticket only asked
  about the Tasks board.
- The new behavior ships behind a new feature flag `datepicker-workspace-week-start` (default **off**).
  The loop registered the client default (`web/src/featureFlags.js`) and the backend default-values
  entry. The **feature-flag store key** for the flag does not exist yet, and the loop cannot create it
  (config/infra is on the R6 denylist). Without the key the backend never returns the flag and it is
  silently always false, so QA on staging would see no change at all. A human must create the key in the
  flag store (staging + prod) before this merges.
- Routine calls made along the way: used a derived value for the resolved week start rather than a
  watcher; left `DatePicker`'s public props unchanged (no new prop); added
  `__tests__/DatePicker.weekStart.test.js` (6 cases); removed an unused date-library import the change
  orphaned.

## Plan-synthesis (ran)
- **Chosen:** resolve the week start inside `DatePicker` from the workspace setting (one fix point).
- Runner-up: add an opt-in `weekStartsOn` prop and pass it only from the Tasks board — rejected: the
  other 13 surfaces keep the same wrong week for Monday workspaces, and the bug report would recur per
  surface.
- Also considered: derive the week start from the browser locale — rejected: the workspace setting is the
  source of truth for the product's cadence, and locale would disagree with the board's own week math.

## Review gate (dev-code-review)
- Cycle 1 → `approve`. No findings on the diff. While validating, the reviewer noticed a **pre-existing**
  N+1 in `api/src/tasks/list_tasks_query.py` (one comments query per task; ~40 queries for a large
  board). Outside the diff, not touched.

## Verification
- `qa-verify`: pass (confidence 90) — Tasks board "this week" selects Mon–Sun for a Monday workspace with
  the flag on; unchanged with the flag off. Before/after recordings exist.
- Unit suite 830 green (+6). `qa-regression`: Tasks + Projects + Invoices area tests green (flag forced
  on in the sandbox). `dev-pre-pr`: `ready`.
