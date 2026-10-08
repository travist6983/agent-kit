---
name: dev-loop-defect
description: >-
  Recipe ①: the full autonomous defect loop for a standard, non-sensitive bug — intake → provision →
  repro → fix → verify → regression → pre-pr → ship → prod-verify, with the shared retry/escalate
  router, the R5 budget cap (.claude/loops/loop-limits.json), and the R9 pause check. Trigger for
  "run the defect loop on PROJ-123", "auto-fix this bug end to end", "run my assigned defects through
  the defect loop", or when dev-intake routes here. THIN orchestrator — sequencing + gates only; all
  real work lives in the atomic skills. The ticket comes through the issue_tracker adapter, the
  sandbox through the sandbox adapter, the pull request through the vcs_host adapter. Staging-only /
  dry-run while the `staging_only` posture flag is set, i.e. until the R2/R5/R6/R9 safety gates are
  verified.
allowed-tools: Read, Write, Bash, Agent, Skill, ToolSearch
---

# dev-loop-defect — the standard-defect loop (Recipe ①)

This is a **thin** orchestrator. It sequences the atomic skills and decides the gates; it never does the
work itself. The retry/escalate/budget/locks/pause engine is shared — read `references/recipe-engine.md`,
and the decision logic is `scripts/recipe_router.py` (tested by `.claude/loops/tests/test_recipe_router.sh`).

Every external system is reached through an adapter role declared in `.claude/loops/stack.config.json`
(`lib/adapters/README.md`): the ticket through `issue_tracker`, the sandbox through `sandbox`, the oracle's
sign-in through `auth_provider`, the pull request through `vcs_host`, prod confirmation through `analytics`
+ `deploy`. A role whose provider is `none` makes the stage that needs it emit a structured `blocked` with
reason `adapter-unavailable:<role>` — the router escalates it; nothing guesses.

## The sequence
```
dev-intake ─→ infra-worktree PROVISION ─→ qa-repro ─→ dev-fix ─┐
                                                               │
      ┌────────────────────────────────────────────────────────┘
      ├── Agent: qa-verify        (BLIND)  → verify.json      ┐
      ├── Agent: qa-regression             → regression.json  ├─ CONCURRENT
      └── Agent: dev-code-review  (BLIND)  → review.json      ┘
                              ↓
                    CHECKER (check_convergence.py)
                              ↓
          dev-pre-pr → infra-ship → infra-prod-verify
                    FINALLY: infra-worktree TEARDOWN
```

**Three edges that used to be here are gone, because nothing flowed along them.**
`qa-regression`'s inputs are the diff + the lint baseline + qa-repro's oracle — it drives
no browser and never reads `verify.json`. `dev-code-review`'s inputs are "the candidate diff
(`git diff <project.default_branch>`), the `fix.json`" — neither `verify.json` nor `regression.json`. So
`verify→regression`, `verify→review` and `regression→review` were ordering, not dependency.
Measured on real runs, the serial stretch was pure waiting. The edge-by-edge reasoning is
`docs/graph-engineering.md`.

**`qa-verify` and `dev-code-review` are spawned with `Agent`, never `Skill`.** `Skill` loads
instructions into the *current* context — so the agent that wrote the fix would be grading it.
Measured on real runs, that configuration passed verification every single time, which is what a
producer grading itself looks like. See *Blind judging* in `references/recipe-engine.md` for the
exact spawn prompt and what the judge may not read.

**Only `dev-fix` writes.** The three parallel nodes are readers, so they share one sandbox
safely. A writing node added to that layer would need its own sandbox (R4).
The environment is a first-class step: after intake, `infra-worktree provision` (a thin wrapper on the
`sandbox` adapter) returns the sandbox every spine skill needs — `dir`, `branch`, `urls.app` /
`urls.api`, `next_steps[]`, `started:false`. Run `app.start_command` from `stack.config.json` (or the
sandbox's `next_steps`) in `dir`, then prove reachability on `app.health_path` before any browser step.
The teardown runs **unconditionally in a finally** — crash, escalation, or success — it's idempotent by
design.
After **each** stage, call the router with the stage result + counters + budget + flags, and act on its
verdict (`advance` / `re-fix` / `re-repro` / `reject` / `escalate` / `rollback` / `stop-budget` /
`halt-paused`):
```bash
python3 scripts/recipe_router.py --stage <stage> --result <result> --attempt <n> --max 3 \
  --confidence <c> --budget-spent <x> --budget-cap <cap> --paused <bool> --sensitive <bool>
```

## How a run goes (the loop, driven by the router)
1. **`dev-intake`** (already chose this recipe) → carry the `area`, `risk_class`, `branch_name`, and
   **`prior_attempts`** (intake's `kb history --ticket <KEY>` read; call it here if intake didn't).
   Carry `prior_attempts` into every router call — a ticket this loop already failed on must not
   re-run the identical strategy (classify-before-adapt, applied *across runs*).
   Then **`infra-worktree provision`** (an isolated sandbox: own branch, own port slot, optionally its own
   database copy when `adapters.sandbox.config.db_clone_command` is set; at `sandbox-cap-reached`
   queue/escalate, never force a second environment). The oracle signs in through the `auth_provider`
   adapter — the recipe hands qa-repro/dev-fix/qa-verify the sandbox's `urls.app` and the role name, never a
   credential (`recipe-engine.md`, *Environment*).
2. **`qa-repro`** → `reproduced` advances. `not_reproduced` (a real negative — the repro RAN and the
   bug did not occur) → router says **`reject`**: draft the not-repro close comment for a human to
   confirm; never auto-fix, never blind-retry. `inconclusive` (couldn't determine — tooling/env/needs-info,
   including a `replay-oracle.sh` that exits 2) → router says **`re-repro`** (retry, cap 3) — **never
   reject on inconclusive**. The retired hyphen forms (`not-reproduced`/`needs-info`) are
   contract-invalid; the router escalates them as stale vocabulary.
3. **`dev-fix`** → `fixed` advances; `blocked-blast-radius`/`could-not-fix` → escalate.
4. **`qa-verify` ∥ `qa-regression` ∥ `dev-code-review` — spawned together, in ONE batch.**
   Wait for all three, then run the **checker** before `dev-pre-pr`:
   ```bash
   python3 scripts/check_convergence.py --evid "$EVID_ROOT/$KEY" --key "$KEY" \
     --tier "$TIER" --producer-model "$FIX_MODEL"
   # 0 clean · 1 FLAGGED (route by next_action, never merge) · 2 uncheckable → escalate
   ```
   It catches what the parallel layer hides: an empty or `null` artifact, two artifacts that
   contradict each other, an artifact naming a different ticket, confidence under the tier
   floor, malformed JSON, and a judge weaker than the producer it graded (strength order from
   `models.rank` in `stack.config.json`). **A `null` result is a reported failure, never something
   to filter away.**

   Then the stage verdicts, as before:

   **`qa-verify`** → re-runs qa-repro's replay oracle (`bash <EVID_ROOT>/<KEY>/replay-oracle.sh`, driven by
   `browser.oracle_driver` from `stack.config.json`) against the fixed build: exit 0 GREEN · 1 RED · 2
   BLOCKED. `pass` advances (low-confidence on a sensitive area → escalate even if green); `fail`/`partial`
   → re-fix with the observation as new context (until cap); `blocked` (oracle exit 2, app down, no oracle)
   → escalate, never re-fix — it is no evidence about the fix. **On `pass` — and only on `pass` — the recipe
   writes `fix.json.pending_fix_lesson` to `fix_lessons`.** `dev-fix` deliberately doesn't: it can't know
   its fix worked until this stage rules. See *Who writes the fix lesson* in `references/recipe-engine.md`.
   **`dev-code-review`** (blind) → `approve` advances; `request-changes`, or any R3 masking
   finding, → re-fix with the findings as context. **An `approve` carrying unresolved
   findings is not a free pass**: either those findings justify a re-fix cycle that counts
   against the cap, or they belong in *Noticed, not changed*. A measured run did that work off-budget
   and the router never saw it — the worst of the three options.

5. **`qa-regression`** → `green` advances; `same-area-regression` / `blocked-build` → re-fix;
   `unrelated-break` → **escalate immediately**; `blocked-environment` / `blocked-slice-empty` → escalate;
   quarantined flakes don't block (R7).
6. **`infra-ship`** → preflight checks the **R9 kill-switch** first (halt if paused). Opens the pull
   request through the `vcs_host` adapter with the story in the description. A review-comment thread
   (`vcs_host.post_review_comment`) goes up **only** for a must-read item — the reviewer must act on it
   before merge — through `infra-ship`'s capped `select_pr_threads.py`; most PRs get none. Caveats,
   pre-existing issues and the rest of `fix.json.design_decisions` never become threads
   (`infra-ship/references/pr-thread-bar.md`). After it returns, write the thread counts from
   `ship.json` as metrics — `kb metric --name pr_threads_posted --value <thread_selection.threads>`,
   `pr_threads_before_merging`, and `pr_threads_outside_funnel` from `thread_audit` (each with
   `--unit threads --loop-id <loop> --ticket <KEY>`) — so a noisy trend shows in the brain before a
   reviewer has to complain. `ship.json` `abort: unclassified-must-read` routes like any ship abort.
7. **`infra-prod-verify`** → `prod-confirmed` closes the loop; `prod-regressed` → **rollback + escalate**
   (R2/R9; the rollback instructions come from `deploy.rollback_hint`, a human-only action);
   `inconclusive` → escalate (also the outcome when `analytics` is `none` — autonomous prod-confirmed
   needs an error-signal source). *(The prod stage stays disabled while `staging_only` is set.)*

Throughout: **R5 budget breaker** — the cap lives in the file `limits_file` points at
(`.claude/loops/loop-limits.json`, `r5_budget.per_loop_usd_cap`; the router reads it when `--budget-cap`
isn't passed) and the RUNNER supplies `--budget-spent` (its own per-run usage accounting). Honesty: with no
runner-supplied spend it's a declared cap with a tested comparator, not an enforced gate — never call it
more. **R4 concurrency** is two real mechanisms, not "locks": per-loop sandbox isolation (the `sandbox`
adapter's lock-protected slot registry + unique port offsets) and kb optimistic concurrency
(`--expect-version` MANDATORY on app_map updates). **R8 metrics** each stage, and **every escalation →
`dev-escalate`** with full memory.

## The run report — what a human reads first
Open it with anything that needs a person, before the stage-by-stage story: `ship.json`
`thread_audit` failures (`noise_thread`, `over_cap_on_pr`), any `ticket-not-fully-fixed` remainder
(the ticket stays open), and **Noticed, not changed** — every `fix.json.noticed_not_changed` item plus
the *Noticed, not changed* bullets of `review.md` (and the funnel's `noticed_overflow`), with a
**drafted ticket** (title, file:line, impact — shaped for `issue_tracker.create_ticket`, never filed) for
each security or data-loss one. The loop drafts; a human files. This is where serious pre-existing issues
land now that they never become review threads. Every recipe on this engine ends with this report.

## Stop conditions
One defect per run. Escalate (don't guess) on: inconclusive-at-cap, can't-fix-at-cap, verify-fail-at-cap,
unrelated-break, budget breach, global pause, a required adapter role that is `none`. `not_reproduced`
takes the reject path (human confirms the close), not escalation. Never retry a fix against prod (confirm
or roll back).

## Knowledge brain (qa-kb)

**Invoking `kb`.** Every `kb <verb>` below is the `qa-kb` CLI wrapper
(`.claude/skills/qa-kb/scripts/kb`), which must be **on your PATH** (one symlink per machine —
`docs/brain-setup.md`). It is **not** there in a fresh checkout: an unresolved `kb` exits **127**
(`kb: command not found`) and the brain write is simply *lost*, so fall back to the script instead of
skipping the call — **absolutely**, never cwd-relative, because a loop's cwd is normally a
sandbox worktree, not this repo:

```bash
# <agents-repo> = the checkout of THIS framework the file was loaded from; set both in ONE shell.
KB="${KB_BIN:-<agents-repo>/.claude/skills/qa-kb/scripts/kb.py}"
python3 "$KB" <verb> ...        # ≡ the bare `kb <verb> ...` calls below
```

The atomic skills write most of the knowledge (app_map, proposed expectations, flakiness). The recipe
writes loop-health **metrics** (R5/R8), checks the **pause** flag (R9), reads **`kb history --ticket`**
at intake — and owns exactly one knowledge write: the **`fix_lesson`**, landed from
`fix.json.pending_fix_lesson` **after `qa-verify` returns `pass`**, because only the recipe sees both the
lesson and the verdict.
**Write-back happens every run** (loop-engineering principle #2) — strongly encouraged, not gated — and the
final report ends with a `🧠 brain write-back` summary **rendered from `kb receipt --ticket <KEY>`**, not
from what the agent remembers writing. See *Knowledge write-back* in `references/recipe-engine.md`. Lean
heavy on `app_map` navigation while the brain is young; the user should never have to ask for write-back.

## The pre-PR gate
- **`dev-pre-pr` before ship.** The readiness gate: it reconciles this loop's `regression.json` (and
  `review.json`, where the recipe ran one) **by freshness**, runs the deterministic conventions scan
  (`stack.conventions_file` → `.claude/loops/conventions.json`) on the added lines, rolls up the blockers,
  and emits one verdict. Router: `ready`/`warnings` → advance; `blocked` → back to `dev-fix` with the
  blockers as context; `scan-unavailable` → escalate (fail closed). It never re-runs a build a fresh
  artifact already answered — and never trusts one older than the diff.
- **It also FAILS CLOSED on `verify.json`** (`scripts/verify_gate.py`). Missing, stale,
  malformed, not-`pass`, or `pass` with `masking_suspected` → `blocked`. Without this gate, the
  "fix verification always runs" invariant would be upheld only by the recipe advancing one stage at a
  time — which stops being true the moment the three judges run concurrently. **This gate is what
  makes the parallel layer safe to have at all**, so it is a prerequisite for the diamond above,
  not an addition to it.

## Key principles
1. **Thin.** Sequence + gates only; the atomic skills hold all the real work.
2. **Every retry changes strategy.** The router routes by failure class — no blind repeats.
3. **Safety gates are hard.** Pause halts; budget stops; unrelated-break escalates; prod-regression rolls back.
4. **Escalate warm.** Every stop hands a human the full memory via `dev-escalate`.
5. **Staging-only while the posture flag is set.** No autonomous prod deploy until R2/R5/R6/R9 are
   verified and a human clears `staging_only` in the brain.
6. **A producer never grades itself.** `qa-verify` and `dev-code-review` are `Agent` spawns with
   fresh contexts, never `Skill` calls. This is an invariant, not a performance choice — and
   splitting them is also what makes the parallel layer possible, so correctness and wall-clock
   come from the same edit.
7. **Sequence only where data flows.** Before ordering two steps, confirm the second reads the
   first's output. Three edges here did not, and cost minutes every run.
8. **Every convergence point has a checker.** Parallelism removes the checkpoints where a bad
   output would have been noticed; the checker puts one back.
9. **Vendors live in adapters.** The recipe names roles (`issue_tracker`, `sandbox`, `vcs_host`,
   `auth_provider`, `analytics`, `deploy`), never a product; swapping one is a `stack.config.json` edit.
