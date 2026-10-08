---
name: dev-loop-feature-complete
description: >-
  Recipe ④: take a feature/story from ticket to a staged, verified change. Trigger when
  dev-intake routes a feature-type ticket here (build/add/implement intent), or "implement
  PROJ-123 end to end", "build out this story and verify it". Sequence: intake → provision →
  plan-synthesis (competing strategies, pick one) → implement → verify against the acceptance
  criteria → regression → code-review → pre-pr → STAGE for human QA. Features are pre-release by
  nature, so it never auto-ships. THIN orchestrator on the shared engine; the ticket comes
  through the issue_tracker adapter and the staged pull request opens through the vcs_host
  adapter.
allowed-tools: Read, Write, Bash, Agent, Skill, ToolSearch
---

# dev-loop-feature-complete — the feature loop (Recipe ④)

The second major use case: a story, not a defect. It runs on the shared engine
(`../dev-loop-defect/references/recipe-engine.md`, `../dev-loop-defect/scripts/recipe_router.py`).
The difference from the defect loops is the front half — features need a **plan** and are verified
against **acceptance criteria**, not a single replay — and the back half stages for human QA instead
of shipping.

This is a **thin** orchestrator: it sequences the atomic skills and decides the gates; it never does
the work itself. Everything stack-specific (how the ticket is fetched, how a sandbox is provisioned,
how the pull request opens) lives in the atomic skills and the adapters they call, configured in
`.claude/loops/stack.config.json`. This recipe names roles, never vendors.

## The sequence
```
dev-intake(story) → infra-worktree PROVISION (FINALLY: TEARDOWN at the end)
                  → PLAN-SYNTHESIS → implement ─┐
   ┌──────────────────────────────────────────── ┘
   ├── Agent: qa-verify (BLIND, vs acceptance criteria)
   ├── Agent: qa-regression
   └── Agent: dev-code-review (BLIND, fan-out)
                 ↓  CHECKER (check_convergence.py)
                  → dev-pre-pr
                  → infra-ship: PUBLISHED pull request for human QA (the AFTER video in the BODY)
                  → human-explore (bless AC expectations) → STOP   # never auto-merge, never auto-prod
```
Features are **always T2**: there is no bug to reproduce and the solution space is open
(`dev-intake`'s tiering treats a feature-type ticket as a T2 trigger on its own). `qa-repro` does not
run on a feature — there is no `repro.json` in a feature run, and nothing downstream may require one.

**Same parallel layer and checker as Recipes ①/②.** Reviewer fan-out is natural on a feature — the
diff is wide and draws many more findings than a defect fix — so `dev-code-review` runs as a fan-out
of `Agent` spawns. The two pieces a feature run must not skip are a **blind** `qa-verify` (an `Agent`
with a fresh context, never a `Skill` call into the context that wrote the code) and the convergence
checker over the three artifacts. Every seam file lives in `<evidence.root>/<KEY>/`
(`runs/PROJ-123/` by default) and is written through `contracts/emit.py`.

**There is no before-capture in this recipe.** The **after video stays** — `qa-verify` records it on
the target surface and `infra-ship` embeds it in the pull-request body. See below.

## What's feature-specific
- **Plan-synthesis** (the `dev-fix` plan-synthesis mode): generate 2–3 competing implementation
  strategies, score the trade-offs, **pick one before coding**. The evaluator and the brain
  (`qa-kb` `fix_lessons` / `app_map`) make the pick where a human gate would otherwise sit; the
  runner-up stays in `fix.json.plan_synthesis` as re-fix memory.
- **Baseline protection**: capture the build/lint/type-check baseline up front — the commands are the
  target repo's own (`stack.build_command`, `stack.lint_command`, and any per-project commands in
  `conventions.json`) — and never exceed it. A feature that adds warnings has regressed.
- **Verify against acceptance criteria**, not a single replay: each AC becomes a checked expectation
  (propose via `qa-kb`; `human-explore` blesses the canonical ones — the loop never does). The
  confidence-≥80 review rubric and the review lenses carry over via `dev-code-review`. `qa-verify`
  records the **after**-video (`<KEY>-fixed.gif` + `.mp4`) on the target surface at `app.url` (or the
  sandbox's url), per `qa-repro/references/recording.md`, signed in through the `auth_provider`
  adapter with a role from its configured `roles`.
- **No before-capture. The after video is the deliverable.** A before-capture would revert the
  working tree to `project.default_branch`, re-drive the browser, and restore — to produce a same-view
  A/B whose only consumer is the pull-request description. It changes no verdict, and it serialises
  the whole run behind a checkout of live files (restarting the developer's running app to do it).
  Evidence that changes no verdict is decoration, so it is not part of this recipe.
  **`qa-verify`'s after-video still runs and is still required.** A feature pull request carries the
  after on the target surface and no before; if a reviewer needs the contrast, the description says
  what the surface looked like before in one line.
- **Stagnation check**: a review finding surviving **2** re-fix cycles → stop + escalate. This is the
  cap that actually binds on features: the review→fix cycle is the stage that iterates, and a
  finding that two fixes could not clear is a plan problem, not a patch problem.
- **Repro/verify retries are not a feature concern.** `qa-repro` never runs; `qa-verify`'s retry
  cap on a feature is **1** (pass `recipe_router.py --max 1` at that gate) — a second failure is a
  plan problem, not a flake, and belongs with a human. A human may still *order* a re-verify; a
  requested round is not a loop retry, and that path stays open.
- **Stage as a pull request for human QA, never an auto-ship.** Features are pre-release by nature,
  so the loop stages via **`infra-ship`** (opened **published** for review — never a draft; this
  recipe does not pass `--draft`) and STOPS — it **never auto-merges and never deploys to prod**.
  `infra-ship` opens it through the `vcs_host` adapter; with the default `git-local` provider the
  stage is the pushed branch plus the pull-request body written under `<evidence.root>/prs/`, and a
  `none` provider blocks the stage with `adapter-unavailable:vcs_host` rather than guessing. The pull
  request is the human-QA surface and carries: the **`qa-verify` after-video in the BODY**
  (`infra-ship` Step 5 — a feature PR has no before half, so the media block is the after alone) and,
  in the description, one sentence on the approach taken and why. **Plan-synthesis runs on every
  feature**, but its runner-up stays in `fix.json.plan_synthesis` — it is not a review comment. A
  review thread goes up only for a must-read item under `infra-ship`'s thread bar
  (`infra-ship/references/pr-thread-bar.md`); on a feature the usual one is a genuine **product call**
  the story didn't settle and users would notice (hide vs disable). A default the story implies, or
  one that matches a sibling setting, is not a fork — state it in the description. Most feature pull
  requests get none. `human-explore` then blesses the AC expectations the verify step proposed.

> ⚠️ **A gate whose `escalate` is routinely ignored teaches the loop that gates are advisory.**
> A `regression.json` that ends `blocked-environment / next_action: escalate` **stops the run** —
> it never ships a pull request anyway. The regression gate is build + lint + unit tests on the
> target repo's own commands (`stack.*_command`); it runs no browser suite, so a blocked
> environment means the evidence the stage exists to produce was never produced. Treat it as the
> failure class it is (environment, transient at most once) and route through the router, not
> around it.

The retry/escalate engine (caps, budget R5, pause R9, locks R4, warm escalation) is the shared one.
After **each** stage, call `recipe_router.py` with the stage result, the attempt counter, the stage's
cap (`--max`), the spend (`--budget-spent`, compared against `loop-limits.json` `r5_budget`) and the
`global_pause` flag read through `qa-kb`, and act on its verdict.

## The pre-PR gate
- **`dev-pre-pr` before ship.** The readiness gate: it reconciles this loop's `regression.json` (and
  `review.json`, where the recipe ran one) **by freshness**, runs the deterministic conventions scan
  (`.claude/loops/conventions.json`, the ruleset the target repo supplies) on the added lines, rolls
  up the blockers, and emits one verdict. Router: `ready`/`warnings` → advance; `blocked` → back to
  `dev-fix` with the blockers as context; `scan-unavailable` → escalate (fail closed). It never
  re-runs a build a fresh artifact already answered — and never trusts one older than the diff.
  It fails closed on `verify.json`: missing, stale, malformed or not-`pass` all block, at every tier.

## Key principles
1. **Plan before code.** Competing strategies + a pick beat one-attempt-iterated for wide solution spaces.
2. **Acceptance criteria are the oracle.** Verify each AC as an expectation; a human blesses canon.
3. **Stage as a pull request, don't auto-ship.** Features go through human QA via a published pull
   request a human merges (`infra-ship` through the `vcs_host` adapter, carrying the qa-verify
   after-video; a review thread only for a must-read item, usually none); the loop stops there —
   never auto-merge, never auto-prod.
4. **Thin.** Shared engine + atomic skills; only the plan and the AC-verify are feature-specific.
5. **Evidence must change a verdict, or it is decoration.** A before-capture would produce a nicer
   pull request and nothing else, while serialising the run behind a checkout of live files. The
   after-video stays because it is what a human QAs against.
