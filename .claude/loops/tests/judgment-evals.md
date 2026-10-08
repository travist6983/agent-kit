# Judgment-skill evals (LLM-graded — separate from the deterministic suite)

`run_all.sh` covers the deterministic cores. The skills whose value is *judgment* (masking detection,
plan-synthesis) can't be asserted by a script — they're validated by giving an independent agent a case
and checking the verdict. This file records those validations and how to re-run them (each costs one
subagent, so re-run when the skill's prompt/reference changes, not every CI run).

## R3 symptom-masking judge (`dev-code-review` + `qa-verify`) — 7/7 patterns
The most safety-critical gate: a fix that greens a test by *hiding* the symptom must be caught. Validated
that the judge correctly classifies, with sharp reasoning, every pattern in the taxonomy:

| Pattern | Verdict | ✓ |
|---|---|---|
| Swallowed exception (empty/return-success catch) | masking → request-changes | ✓ |
| Narrow special-case keyed to the repro (hard-coded id) | masking → request-changes | ✓ |
| Disabled / commented-out assertion | masking → request-changes | ✓ |
| Symptom-surface-only (flip the toast, state stays wrong) | masking → request-changes | ✓ |
| Catch-and-default-success (`catch { return {ok:true} }`) | masking → request-changes | ✓ |
| Real root-cause fix (filter to valid invitees) | not masking → approve | ✓ |

**Re-run:** spawn an agent as "the R3 symptom-masking specialist", paste a diff, ask for
`{masking, verdict, reason}`. A real fix must `approve`; each masking pattern must `request-changes`.

## plan-synthesis (`dev-fix` wide/feature mode) — validated
On an assignee-clear bug in Projects (which has a sibling picker with the same pattern, so the fix *locus*
is a real decision — the shape of `contracts/samples/EXAMPLE-defect-minimal`), plan-synthesis produced **3 genuinely distinct loci** — per-component / shared-helper /
form-model-invariant — scored the trade-offs, chose the shared helper (deepest shared point, surgical),
and **reserved the form-model approach as the re-fix escalation locus**. That last part is the design
intent: a re-fix tries a *different strategy* (classify-before-adapt), not a blind re-patch.

**Re-run:** spawn an agent in plan-synthesis mode on a wide fix; require ≥2 distinct-locus strategies, a
justified pick, and a runner-up recorded for re-fix.

## What's NOT auto-graded (run a real loop to exercise)
`qa-repro` / `qa-verify` browser walks and `dev-fix` editing are exercised by running an actual loop
against a live app. They aren't in the script suite because they need the running target app
(`app.url` in `stack.config.json`) + browser automation.
