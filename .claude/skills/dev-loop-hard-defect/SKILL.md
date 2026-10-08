---
name: dev-loop-hard-defect
description: >-
  Recipe ②: dev-loop-defect plus a dev-code-review gate before ship, for HARD defects — multi-file,
  shared/core-module, migration-adjacent, or otherwise higher-risk (dev-intake routes them here).
  Trigger for "run the hard-defect loop on PROJ-123", "this one's risky — review the fix before
  shipping", or when dev-intake picks it. Sequence: intake → provision → repro → fix → verify →
  regression → code-review (masking = hard block; the same finding surviving 2 fix cycles =
  stagnation → escalate) → pre-pr → ship → prod-verify. THIN orchestrator on the shared engine; no
  prod deploy while the `staging_only` posture flag is set, i.e. until the safety gates are verified.
allowed-tools: Read, Write, Bash, Agent, Skill, ToolSearch
---

# dev-loop-hard-defect — the defect loop with a review gate (Recipe ②)

Identical to `dev-loop-defect` except for one inserted gate. Shares the engine: see
`../dev-loop-defect/references/recipe-engine.md` and `../dev-loop-defect/scripts/recipe_router.py`
(tested) — the router already handles the `code-review` stage.

Every external system is reached through the adapter roles in `.claude/loops/stack.config.json`
(`lib/adapters/README.md`). This recipe never names a vendor: the ticket comes from the
`issue_tracker` role, the sandbox from `sandbox`, the PR from `vcs_host`, the prod error signal from
`analytics`, the deployed version from `deploy`. A role configured as `none` makes the step that needs
it return a structured `blocked` with reason `adapter-unavailable:<role>` — the router escalates it;
nothing crashes, skips silently, or guesses.

## The sequence (delta in **bold**)
```
dev-intake → infra-worktree PROVISION → qa-repro → dev-fix ─┐
   ┌────────────────────────────────────────────────────────┘
   ├── Agent: qa-verify              (BLIND)
   ├── Agent: qa-regression
   └── Agent: **dev-code-review**    (BLIND, full specialist fan-out + masking judge)
                     ↓  CHECKER (check_convergence.py)
        dev-pre-pr → infra-ship → infra-prod-verify
              FINALLY: infra-worktree TEARDOWN
```
Same parallel layer and checker as Recipe ① — see *The parallel layer* and *Blind judging* in
`../dev-loop-defect/references/recipe-engine.md`. Both recipes run a review; the delta is the
**depth** of the review and the stagnation rule below.

Same environment rule as Recipe ①: `infra-worktree` is a thin wrapper on the `sandbox` adapter. Its
provision returns the normalized `Sandbox` — `dir`, `branch`, `ports`, `urls{app, api}`, `db`,
`started: false`, `next_steps[]`. The recipe runs `next_steps` (install, migrate, `app.start_command`
in `dir`) and proves reachability on `urls.app` + `app.health_path` before any browser step. Teardown
runs unconditionally in a `finally` — crash, escalation, or success — and is idempotent.

The last step is the same as Recipe ①: `infra-prod-verify` reads the original error signal through the
`analytics` adapter and the deployed version through the `deploy` adapter. With either role at `none`
its verdict is `inconclusive`, which the router escalates with a prod-confirm handoff; autonomous
`prod-confirmed` needs a real analytics adapter.

## What's different from Recipe ①
- **A deeper `dev-code-review`, and it is a hard gate.** Both recipes run review *concurrently*
  with verify and regression — it does not wait for a green regression, because it never reads
  `regression.json` (its inputs are the diff and `fix.json`). What Recipe ② adds is **depth**: the
  full specialist fan-out plus the masking judge, rather than a single reviewer. The review judges
  the diff against the target repo's own rules — `stack.repo_guidelines_file` and
  `stack.conventions_file` from `stack.config.json` — never against a built-in stack.
  The router maps its verdict: `approve` → advance; `request-changes` (including any **R3 masking**
  finding) → back to `dev-fix` with the findings as new context (until cap → escalate). This is
  defense in depth on top of `qa-verify`'s runtime masking detector — and the two are kept
  **separate deliberately**. Merging them to save a node would violate the blind-judge invariant:
  they judge different objects (runtime behaviour vs. the diff), and the checker flags them
  contradicting each other, which is only possible while they are two.
  The reviewers run concurrently, so the extra depth costs reviewer count, not wall clock.
- **Stagnation check** (shared with feature-complete): if the *same* review finding survives **2**
  fix cycles, stop and escalate — repeated identical findings mean the loop isn't converging, and a
  third cycle that repeats the action is not a retry, it is a loop.
- **The review gate adds no PR comments.** Findings the re-fix resolved are in the diff, not new
  design decisions. Pre-existing issues the review noticed go to the description's *Noticed, not
  changed* and the run report — never a thread. Risk earns a review, not more threads: this recipe
  posts under the same `infra-ship` thread bar as Recipe ① (`loop-limits.json` `defect_pr_threads`,
  expected count zero). A simple bug routed here only because it touched a shared core module
  (`conventions.json` `shared_core_globs`) earns the deeper review, not a thread per decision.
- Everything else — repro/fix/verify/regression, the budget breaker (R5, cap in
  `.claude/loops/loop-limits.json` — `limits_file` in `stack.config.json` — with runner-supplied
  spend), pause (R9), concurrency (R4 = sandbox isolation + kb `--expect-version`, not file locks),
  warm escalation through `dev-escalate` → the `issue_tracker` adapter, and the no-prod-deploy
  posture while the brain's `staging_only` flag is set (until R2/R5/R6/R9 are verified) — is exactly
  the shared engine.

## The pre-PR gate
- **`dev-pre-pr` before ship.** The readiness gate: it reconciles this loop's `regression.json` (and
  `review.json`, where the recipe ran one) **by freshness**, runs the deterministic conventions scan
  (`.claude/loops/conventions.json`, the ruleset the target repo supplies) on the added lines, rolls
  up the blockers, and emits one verdict. Router: `ready`/`warnings` → advance; `blocked` → back to
  `dev-fix` with the blockers as context; `scan-unavailable` → escalate (fail closed).
  It never re-runs a build a fresh artifact already answered — and never trusts one older than the diff.
- **It fails closed on `verify.json`**, as in Recipe ①: missing, stale, malformed or not-`pass` all
  block. This is what makes the concurrent judge layer safe to have at all.

## Key principles
1. **Risk earns a review.** Hard defects get the diff-level gate a standard defect skips.
2. **Masking is a hard block.** `request-changes` with masking → re-fix, never ship.
3. **Converge or escalate.** A finding that survives 2 cycles stops the loop (stagnation).
4. **Thin.** Same engine + atomic skills as Recipe ①; only the review gate is added.
