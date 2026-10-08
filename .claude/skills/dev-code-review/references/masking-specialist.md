# dev-code-review — the R3 symptom-masking specialist (the loop-specific reviewer)

The six stack-agnostic specialists (frontend, backend, data, security, tests, perf — see
`specialists.md`) are the ordinary review. `dev-code-review` adds a **7th specialist** that the
autonomous loop specifically needs: the **symptom-masking judge (R3)**. A loop's `dev-fix` is graded by
a replay going green — so the dangerous failure mode is a change that makes the replay green by
**hiding** the symptom instead of fixing the cause. This specialist exists to catch that at the diff,
before it ships. (It pairs with `qa-verify`'s runtime masking detector — defense in depth.)

## What it judges
Read the `dev-fix` diff (and the failure class the fix was meant to address, from `fix.json`). Flag
**request-changes** when the green depends on any of these:

| Mask pattern | What it looks like (any language) |
|---|---|
| **Swallowed exception** | an exception handler (try/catch, rescue, except, a promise or async rejection handler) that returns success, an empty handler, or a broad handler wrapped around the call that was failing |
| **Suppressed signal** | logging / error surfacing that was *exposing* the bug is removed or silenced |
| **Narrow special-case** | a guard keyed to the exact repro — a specific id, a hard-coded value, a flag — rather than the general cause |
| **Disabled check** | a test or assertion skipped or deleted, or a type / nullability constraint loosened or cast away to dodge the error |
| **Symptom-surface-only** | the change edits the *symptom's visibility* (e.g. the toast, the message, the badge) while the underlying state stays wrong |

For each hit: quote the exact hunk, explain **why it masks rather than fixes**, and set verdict
`request-changes` with category `SYMPTOM_MASKING`.

Worked example (neutral domain): the defect is "an Invoice for an archived Project still shows as
*Due*". A diff that hides the *Due* badge when `project.archived` is a **symptom-surface-only** mask;
a diff that wraps the invoice-status query in a handler returning `paid` on error is a **swallowed
exception**; a diff that short-circuits `if invoice.id == 4417` is a **narrow special-case**. The fix
that transitions the Invoice when the Project is archived — and adds the test that proves it —
is the real one.

## What is NOT masking (don't false-flag)
- An exception handler that **handles** the error correctly (logs + recovers + the state is right) is fine.
- A genuinely narrow bug whose correct fix *is* a specific guard (e.g. a real null check at the cause)
  is a real fix — the test is whether it addresses the **cause** or just the **visibility**.
- Removing a *noisy debug* log unrelated to the bug is fine.

The discriminating question: **after this change, is the underlying state/behavior actually correct, or
just less visible?** If the diff makes the wrong thing quieter instead of right, it's masking.

## Output
A finding object like the other specialists (`dimension: "masking"`), plus it drives the overall
**verdict**: any confirmed `SYMPTOM_MASKING` (confidence ≥ 80) forces overall `request-changes` and
lowers the fix's confidence — even if every other specialist is clean and the replay is green.

## False positives → qa-kb, not a flat file
A standalone review tool typically suppresses known false positives from a flat JSON file in the
repo. `dev-code-review` routes them through **`qa-kb`** (the `flakiness`/known-FP provenance store)
so the suppression is shared across machines, decayed, and audited with the rest of the brain — not
a stray JSON only one checkout can see.
