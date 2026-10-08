---
name: dev-loop-pr-review
description: >-
  Recipe ⑤: the scheduled PR-review loop. Sweeps the VCS host (through the vcs_host adapter) for
  pull requests nobody has reviewed at their current head commit, pulls each into a read-only
  worktree, runs dev-code-review over the PR diff, and puts the findings on the PR — dry-run until
  authorized. Trigger for "review any new PRs", "check the PR board and review what's new", "run
  the PR review loop", "sweep the PR board", "are there PRs waiting on review", or as the hourly
  job behind /loop. THIN orchestrator — sequencing, gates, and the per-tick bound only; the board
  mechanics live in dev-pr-feed and the judgment in dev-code-review. Reviews each PR once per head
  commit and comes back when the author pushes, so it never spams and never goes silent.
allowed-tools: Read, Glob, Grep, Bash, Skill, Agent, ToolSearch
---

# dev-loop-pr-review — the scheduled PR-review loop (Recipe ⑤)

A **thin** orchestrator over two skills that already do the work:

```
dev-pr-feed unreviewed
  └─ for each PR (bounded):
       dev-pr-feed checkout  →  dev-code-review (the PR diff)  →  dev-pr-feed post  →  dev-pr-feed mark
       FINALLY: dev-pr-feed teardown
```

This recipe owns exactly four things: **how many PRs per tick**, **what to do with each failure
class**, **the R5 consumption cap**, and **the R9 posture**. Everything else belongs to the atoms —
that separation is what lets the same feed serve a one-off "review PR <id>" without the loop.

Every call to the VCS host — listing PRs, checking one out, reading its threads, posting a comment
— goes through `dev-pr-feed`, which speaks to the `vcs_host` adapter declared in
`.claude/loops/stack.config.json`. This recipe never names a host, a CLI, or a credential. A stack
whose `vcs_host` provider is `none` gets a structured `blocked` (`adapter-unavailable:vcs_host`)
from the first feed call, and the tick ends there.

## Before anything: the two bounds

Read `.claude/loops/loop-limits.json`. Two numbers govern a tick:

- **Author scope — default `own`.** `dev-pr-feed` returns only the runner's own PRs unless
  `LOOP_PR_AUTHOR_SCOPE` says otherwise (`all`, or `others`). This default is deliberate: a live
  post carries the runner's **own identity on the VCS host**, so comments on a colleague's PR
  appear to come from that person. Widening the scope is a human decision, not something a cron
  job should inherit — see `pr_review_loop.author_scope` in `loop-limits.json`.

  **The loops' own PRs are skipped.** `infra-ship` labels every PR it opens with the loop label
  (`dev-pr-feed` reads the label name from the `vcs_host` adapter config), and `dev-pr-feed
  unreviewed` filters those out, reporting them under `loop_prs_skipped` rather than queueing
  them. Without that, scope `own` would point this loop straight at the defect loop's own output
  — several threads per push onto PRs `infra-ship` deliberately caps at a handful (usually 0). Set
  `LOOP_PR_INCLUDE_LOOP_PRS=1` to put them back in the queue; the explicit `checkout` / `post` /
  `mark --pr N` verbs are unaffected either way.
- **PRs per tick** (`pr_review_loop.prs_per_tick`). A first run against a board of a dozen open
  PRs is not a reason to review a dozen at once; that's a lot of consumption and, once posting
  goes live, a wall of bot comments landing on colleagues simultaneously. The queue drains over a
  few ticks and stays drained.
- **R5 consumption cap** (`r5_budget.per_loop_usd_cap`). Check between PRs, not just at the
  start. On breach: stop the sweep, report which PRs are still queued, do not start another
  review. Be honest about it **twice over**, because there are two separate things this cap does
  not deliver:
  - **Mechanism.** It is a declared cap with a tested comparator until a runner supplies real
    figures — `loop-limits.json` says so and this recipe must not claim more. (The one runner that
    *does* enforce it is `pr_review.sh`, which hands the number to `claude --max-budget-usd` so
    the harness ends the session itself.)
  - **Units.** The dollars are **notional** unless `runner.billing_posture` in `stack.config.json`
    is `metered`. The figure is the harness's own `total_cost_usd`, a client-side list-price
    *estimate* from token counts — and under a **subscription** posture nothing is billed for a
    tick at all. Treat the cap as a runaway-consumption backstop and a proxy for
    rate-limit-window burn; never write "spend", "cost $x", or "budget" in a report as if money
    changed hands. It bounds real dollars only when a metered API key authenticates the session,
    which `pr_review.sh` refuses outright (exit 28) under a subscription posture unless
    `LOOP_PR_REVIEW_ALLOW_API_KEY=1` arms it deliberately. The full derivation is
    `pr_review_loop.per_day_usd_cap_rule` in `loop-limits.json`.

  The resource that genuinely runs out under a subscription posture is the **rolling rate-limit
  window, shared with the operator's own interactive usage** — nothing in this system meters it,
  and exhausting it ends the session (see `anthropic-rate-limited` below).

## The sweep

### 0. Take the tick lock — first, always

```bash
python3 $F lock          # exits 4 with status "busy" if another tick holds it
```

**Release it in the same `finally` as the teardowns** — `python3 $F lock --release`. The lock is a
record on disk, not a process handle, precisely because a tick spans many separate commands over
15+ minutes; that also means nothing releases it for you if you forget. A crashed tick can't block
the loop forever (a lock past its TTL is stale and gets taken), but a forgotten release still
costs a wasted tick.

If it reports `busy`, **stop this tick immediately** and say so in one line. Do not wait, do not
queue behind it. The next tick is an hour out and the queue is durable, so a skipped tick costs
nothing — while two sweeps racing costs real damage: duplicate comments on a PR, and two processes
minting host credentials at once, contending on the same credential cache and turning a
one-second check into a multi-minute hang that starves the next tick too.

A review of a large PR can take 15+ minutes. On an hourly schedule that is fine; the moment
someone shortens the interval, overlap is the default, not the exception.

### 1. Get the queue

```bash
F=.claude/skills/dev-pr-feed/scripts/pr_feed.py
python3 $F unreviewed --limit <prs_per_tick>
```

An empty queue is the **normal, healthy outcome** for most ticks and is a success, not a no-op to
work around. Report it in one line and stop — do not go looking for PRs to review by relaxing the
filter, and do not review something already marked.

Under the headless runner an empty queue never reaches this skill: `pr_review.sh` answers it without
a model call. With `--health` it also spends that idle tick on `pr_feed health` — every in-scope PR
with merge conflicts or a red CI build (read through the `ci_cd` adapter) goes into the report and a
`pr-health` metric. That sweep is **report-only**, loop PRs included: this loop never rebases,
pushes, or re-runs a build.

If this returns `ledger-unreadable`, **stop the whole tick**. That class means we cannot tell what
we've already reviewed, and the wrong move — reviewing everything — puts duplicate comments on
real colleagues' pull requests. Doing nothing for an hour costs nothing.

### 2. Per PR: check out, review, post, mark

```bash
python3 $F checkout --pr <ID>     # → worktree + diff_base + diff_command
```

Then run **`dev-code-review`** over that PR's diff. **Read
`references/reviewing-incoming-prs.md` before this step** — reviewing a person's pull request
differs from judging our own loop's diff in ways that decide what gets posted. In short:

- **Diff the PR, not your local default branch.** Use the `diff_command` the checkout returns
  (`git -C <worktree> diff <diff_base>...HEAD`). The local default branch drifts, and a stale base
  produces findings about code the PR never touched — the fastest way to lose a reviewer's trust.
- **The rebuttal rule.** Before reporting anything, look for the author's answer to it in a code
  comment, the commit message, or the PR description. If they already reasoned about it and your
  finding doesn't engage with that reasoning, **don't post it** — record it withheld instead. A
  true finding aimed at someone who already answered it costs more trust than staying quiet.
- **There is no `fix.json` and no repro oracle**, so the R3 masking judge has nothing to
  mask-check against and must not manufacture a verdict. The six specialists and the target repo's
  own rules — the guidelines file named by `stack.repo_guidelines_file` plus
  `.claude/loops/conventions.json` — apply in full.
- **Verify every `file:line` anchor** against the worktree, and verify any count you state as
  fact. The anchor becomes the comment's position; a wrong one taints the finding attached to it.

The bar is `dev-code-review`'s own: **high-signal only, confidence ≥ 80, validated by re-reading
the code at `file:line`**. On someone else's PR this matters even more — the comment lands on a
colleague's work under the runner's name. When unsure, don't flag. **An approve with no findings is
a good outcome** — report what you checked and cleared, which is more useful to the author than a
padded list.

```bash
python3 $F post --pr <ID> --findings <review.json> --reviewed-commit <head_commit> [--live]
python3 $F mark --pr <ID> --commit <head_commit> --verdict <v> --findings-count <n> --mode dry-run|live
```

A review of a large PR takes minutes, and PRs merge in that window — expect a PR to complete
partway through its own review now and then. That is a normal race, not a failure: `post` returns
`pr-closed`, the findings are dropped, and the sweep continues.

**`mark` is not optional, and its `--mode` must match what actually happened.** An unmarked review
is re-run and re-posted next tick. A review marked `live` that was only a dry run makes findings
look delivered when nobody saw them.

**Do not mark a PR whose findings could not be delivered.** If the review produced findings and
the post was refused — `paused`, `post-rejected` — leave the PR unmarked so the next tick picks it
up and actually delivers them. Marking here would be the worst outcome available: the loop reports
a clean sweep, the ledger says reviewed, and real findings are silently lost. A review that found
**nothing** is different — there was nothing to deliver, so mark it `dry-run` and move on.

### 3. Teardown, always

```bash
python3 $F teardown --pr <ID>
```

In a `finally` — after success, after a failure, after an escalation. A leaked worktree is the
cardinal sin, same as `infra-worktree`.

## Posting posture

**Dry-run by default.** Commenting on colleagues' pull requests is outward-facing, so `--live` is
a deliberate choice the invoker makes, per run. `dev-pr-feed` independently checks the R9 pause
flag and **fails closed** before any live post, so a live flag is a request, not a guarantee.

A dry-run tick is genuinely useful: it produces the previews and the report, and the ledger records
`mode: dry-run` so those PRs can be re-queued (`ledger --forget <pr>`) once you go live.

## Failure-class routing (classify before adapting)

The whole point of the classes is that a retry changes strategy. A blind repeat is a bug.

| Class | Route |
|---|---|
| `ledger-unreadable` | **Stop the tick.** Never widen to "review everything" |
| `auth-expired` | Stop and tell the human to re-authenticate the `vcs_host` adapter — no retry will fix it |
| `api-unavailable` | **VCS host** side: a 5xx, throttle or network blip on a board read. End the tick quietly; the next one picks up the same queue |
| `anthropic-rate-limited` | **Model provider** side, and a different thing entirely: the account is out of its rolling window, so the *model call itself* ends on an HTTP 429. Nothing for this recipe to route — the session is gone; the **runner** detects it after the fact (`api_error_status: 429` on the result object) and exits **29** with `outcome=anthropic-rate-limited`, marking nothing and retrying nothing. **Not a broken loop**, and deliberately not counted toward the consecutive-stuck counter |
| `checkout-failed` | Skip this PR, continue the sweep, report it |
| `stale-iteration` | Skip; the PR moved, so the next tick reviews it at the new commit |
| `post-rejected` | Stop posting for the tick — a permissions problem, not a code problem |
| `pr-closed` | The PR merged or was abandoned while we reviewed it. **Drop the findings** — never comment on a closed PR. Not an error; report it and move on |
| `paused` | R9 on: finish in dry-run, post nothing |
| `adapter-unavailable:vcs_host` | The stack declares no VCS host. **Stop the tick** with that reason in the report; there is nothing to sweep |
| review error | Skip the PR, mark nothing, continue |

Nothing here retries the same PR within a tick. The queue is naturally self-healing: an unmarked
PR is simply still unreviewed an hour later, which is the correct behavior and needs no counter.

## Stop conditions

Per-tick PR cap reached · empty queue · R5 consumption-cap breach · `ledger-unreadable` · `auth-expired` ·
`post-rejected` · `adapter-unavailable:vcs_host`. This loop **never fixes, never pushes, never
ships** — it reviews and comments. A PR needing a code change is the author's to make.

## Success criteria

- Every PR in the queue is reviewed at most once per head commit — no duplicate comments, ever.
- A PR whose author pushes new commits comes **back** into the queue.
- Findings that reach a PR are high-signal enough that a human wouldn't ask for them to stop.
- The developer's own checkout of the target app (`app.repo_dir`) is untouched and no worktree leaks.
- A tick with nothing to do is one line and costs almost nothing.

## Knowledge brain (qa-kb)

Writes a `pr-review` metric per tick (R5/R8: PRs swept, reviewed, findings, consumption) so you can tell
whether the loop is getting better rather than just running. Reads the R9 pause flag. The
seen-ledger is runtime state, not knowledge — see `dev-pr-feed/references/seen-ledger.md`. False
positives go to `qa-kb` through `dev-code-review`'s existing known-FP path, so a finding a human
waves off is suppressed next time. That write-back is the point: the loop should get quieter and
sharper over weeks, not repeat the same rejected finding every hour.

## Running it hourly

The recipe is runner-agnostic — `/loop`, a scheduler job (launchd, cron, systemd), a CI pipeline, or
any control plane all invoke it identically. **Only the runner knows the schedule**, so never
restate a cadence here or in a report; print it from whatever armed it.

```
/loop 1h Run dev-loop-pr-review over the PR board.
```

There is also a headless runner — `.claude/skills/dev-loop-pr-review/scripts/pr_review.sh`, armed
by the scheduler under the label `com.loops.pr-review`. It wraps this recipe in the safety
envelope a scheduled job needs and the recipe deliberately does not own: a single-instance runner
lock, the R9 brain gate before a token is spent, a metered-API-key refusal (exit 28) under a
subscription posture, the per-day consumption gate (exits 26/27), a queue gate that answers an
empty queue or an expired host credential itself with no model call (so on most ticks this recipe
never runs), a watchdog, crash-safe teardown of `pr_feed`'s tick lock and this tick's worktrees, a
posting guard that strips `--live` on a dry-run tick, and a post-run assertion that the tick left a
receipt. It overrides one line of the Report template (see below). Its operator docs are
`.claude/loops/scheduler/README.md`, and the schedule it is armed with is printed by:

```bash
bash .claude/loops/scheduler/install.sh --label com.loops.pr-review --check
```

## Report

End every tick with:

```
PR sweep — <n> candidates · <n> reviewed · <n> already current · <n> skipped (<class>)
  PR <id>  request-changes  2 findings (1 critical)  [dry-run → preview.json]
  PR <id>  approve          0 findings
Queued for next tick: <id>, <id>
Spend: $x.xx / $<per_loop_usd_cap>
```

**About that last line** — it is kept in this shape because `pr_review.sh` quotes it verbatim in
order to override it, and a template the runner describes must not drift out from under it. Two
things are true about it and both belong in any report you write:

- **A session has no meter**, so under the headless runner the instruction is to write exactly
  `Spend: unmeasured (the runner records the measured figure)`. A guess wearing the costume of a
  measurement is worse in a health report than no number. Under `/loop` or a hand run, where a
  session cost really is visible, write it — and say what it is.
- **It is not money** under a subscription posture. The number is a client-side list-price
  estimate, and nothing is billed for the tick (see *the two bounds* above). Never present it as
  a bill unless `runner.billing_posture` is `metered`.

Name the skipped PRs and their failure class, **and name every withheld finding with its reason**.
A sweep that silently drops a PR — or quietly swallows a finding it decided not to post — is the
failure mode that looks identical to a healthy one. The withheld list is also where you find out a
rule is wrong: the same finding withheld for the same reason across many PRs means the rule needs
changing, not the PRs.
