# Loop state — PROJ-320 (dev-loop-hard-defect), end of loop, ready to ship

Synthetic scenario: most of the ticket is fixed, one part cannot be. Everything upstream is done and
green; what remains is dev-fix's final `fix.json` (Step 8) and infra-ship's PR body (Step 4) + threads
(Step 6).

## Ticket
- **PROJ-320** — Invoice totals are wrong in three places: (1) the on-screen **Total** column skips line
  items whose amount is 0; (2) the monthly summary tooltip shows **last month's** total; (3) the **CSV
  export** totals don't match the screen. Release target: **Release 2.5.0**.
- Branch: `fix/PROJ-320-invoice-totals`.

## Root cause (diagnosed, verified)
- Symptoms (1) and (2) share one cause: `web/src/areas/invoices/hooks/useInvoiceTotals.js` filters line
  items with a falsy amount (`if (!item.amount) continue`), which drops zero-amount rows, and it caches
  the monthly roll-up per component instance without invalidating when the month changes.
- Symptom (3) has a different cause: the export (`api/src/invoices/export_invoices_query.py:88`) reads
  the denormalized `invoice_month.total_value` column, which a legacy import job populated with the same
  zero-amount bug years ago. Correcting it needs a **data migration + backfill** of historical rows —
  migrations are on the R6 denylist, so the loop cannot write it.

## What the fix changed
- `useInvoiceTotals.js:41` — filter on `item.amount === null` instead of falsy; invalidate the roll-up
  cache on month change. Fixes (1) and (2) everywhere the hook is used (the Invoices page + the project
  billing section).
- `export_invoices_query.py` — untouched. **Symptom (3) is NOT fixed by this PR.**

## Plan-synthesis (ran)
- **Chosen:** fix the shared hook (one fix point, both symptoms).
- Runner-up: patch the Invoices page, the project billing section and the tooltip separately — rejected:
  three copies of the same filter to keep in step, and the next consumer would inherit the bug.

## Review gate (dev-code-review) — 2 cycles
- Cycle 1 → `request-changes`: HIGH — `item.amount === null` throws when a line-item object lacks the
  `amount` key entirely (older API payloads). **Fixed** in re-fix cycle 1 (`item.amount == null`, plus a
  unit case).
- Cycle 2 → `approve`.

## Verification
- `qa-verify`: pass for symptoms (1) and (2) (confidence 86) — zero-amount items now count; the tooltip
  follows the selected month. Before/after recordings exist. Symptom (3) reproduces unchanged, as expected.
- Unit suite 826 green (+4). `qa-regression`: Invoices + Projects area tests green. `dev-pre-pr`: `ready`.
