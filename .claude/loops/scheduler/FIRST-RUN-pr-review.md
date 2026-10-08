# First tick — do this by hand, watch it

> This is the hand-run gate for **`com.loops.pr-review`**. The nightly dream has its own:
> **`FIRST-RUN.md`**. Both agents are documented in `README.md`; what every runner promises is in
> `JOB-CONTRACT.md`.

`scripts/pr_review.sh` is the headless wrapper that takes the locks, gates on the brain, states
the posture, watchdogs the session, and cleans up after a kill. Prove it by hand, twice, before any
timer runs it.

**Do not run `install.sh` until two hand ticks come back clean.** An hourly job that has never
completed once is not a timer, it is 24 chances a day to find out something was wrong. And unlike
the dream — which writes only to a brain you can `kb undo` — a live tick puts comments on
**colleagues' pull requests under your own vcs_host identity**. There is no undo for that.

Everything below runs from the repo root:

```bash
cd <this checkout>
```

Everything below is also **dry-run**. Going live is a separate decision with its own gate, in
`README.md` → *Going live, and widening the scope*. Do not combine the two.

---

## 1. Dry run — no lock, no model call, nothing posted

```bash
bash .claude/skills/dev-loop-pr-review/scripts/pr_review.sh --dry-run
```

This is a queue read and nothing else: it takes no lock (not the runner's, not `pr_feed`'s),
makes no model call, posts nothing, writes no brain rows, and appends no log line. It is safe to
run at any time, even beside a real tick.

Expect the queue as JSON on stdout and a few lines of chatter on stderr:

```
pr_review.sh: --dry-run — queue read only (limit=2 scope=own).
pr_review.sh: ledger=<repo>/.claude/loops/pr-review-ledger.json app_root=<app.repo_dir>
pr_review.sh: 3 unreviewed / backend=file
pr_review.sh: --dry-run — no claude call, nothing posted, nothing marked.
```

Read the four things that must be right before the first real tick:

- **`limit=2 scope=own`** — the bounds came out of `loop-limits.json`. If the runner had to fall
  back to its own literals it says so on stderr; that is a warning that the tracked JSON is
  unreadable, not a number to accept.
- **`ledger=` is an absolute path** in this repo. `pr_feed`'s tick lock lives in *this file's
  directory*, so a wrong ledger path silently relocates the lock and two ticks would both see
  it free.
- **`app_root=`** is a real git checkout of the target app on this machine. It is derived from
  stack.config `app.repo_dir` (or the vcs_host provider's `repo_dir`); set one of those rather
  than hand-editing the plist.
- **Every `author` in the queue is you.** That is `scope=own` working — `pr_feed` resolves "you"
  as `LOOP_PR_IDENTITY`, else what the vcs_host provider reports. If a colleague's name appears,
  stop — the scope is wider than the tracked decision.

Each entry carries a `reason`: `never-reviewed` or `new-commits-since-review`. On a fresh ledger
everything is `never-reviewed`, which is why the first tick has a queue at all.

**If `pr_feed` exits non-zero with no JSON on stdout**, that is an infrastructure failure, not a
failure class — almost always `git` missing from `PATH`, or the vcs_host adapter being `none`
(exit 3, `adapter-unavailable:vcs_host`). The runner says so explicitly and prints the PATH it
used. A launchd job with a default `PATH` hits this every single tick, which is what the plist's
`EnvironmentVariables` block exists to prevent.

## 2. Confirm the brain answers

```bash
python3 .claude/skills/qa-kb/scripts/kb.py pause --status
```

Must print `"ok": true` and `"paused": false`. If `ok` is false, stop — `pr_review.sh` exits 20
rather than run, which is the point. Fix the brain first: usually an expired or missing credential
for the configured auth mode (kb.config `auth.mode`). `docs/brain-setup.md` is the setup page.

## 3. Confirm the vcs_host adapter — this is the step `qa-dream` does not have

```bash
# the cheapest thing the adapter knows; exit 3 means the role is absent (provider none)
python3 lib/adapters/cli.py vcs_host default_branch

# the identity a live comment would wear — and what scope 'own' resolves against
python3 .claude/skills/dev-pr-feed/scripts/pr_feed.py unreviewed --limit 1   # the stderr banner names it

# the checkout the review worktrees are cut from
git -C <app.repo_dir> rev-parse --git-dir

# both locks must be free before you start
python3 .claude/skills/dev-pr-feed/scripts/pr_feed.py lock --status     # → "free"
ls -d .claude/loops/pr-review.lock 2>/dev/null || echo "runner lock free"
```

If the adapter fails, every verb fails `auth-expired` rather than guessing whose PRs these are —
and the runner will never attempt to re-authenticate on your behalf, because an interactive login
flow would block forever on a terminal that does not exist. Re-authenticate the way the provider's
docs say (`adapters.vcs_host.config.reauth_hint`, if the provider sets one).

## 4. Record the baseline you will compare against

```bash
python3 .claude/skills/qa-kb/scripts/kb.py receipt --ticket pr-review --since 30d
python3 .claude/skills/dev-pr-feed/scripts/pr_feed.py ledger
git -C <app.repo_dir> worktree list | grep pr-review || echo "no review worktrees"
```

All three should be empty — `"count": 0`, `"entries": {}`, no review worktrees. That is your
before. (Other worktrees beside the checkout are unrelated; only `pr-review/pr-*` under
`LOOP_PR_WORKTREE_ROOT` belongs to this loop.)

## 4b. Prove your shell is not armed for metered billing

Everything up to here is a dry run. The moment you run a real tick **from your own terminal** it
inherits your shell — and `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` **take precedence over a
subscription credential**, which makes the tick bill as metered API usage. (The launchd job is
safe by construction: it gets only the `EnvironmentVariables` its plist declares. The hand runs in
this document are the exposed case.)

```bash
env | grep -c '^ANTHROPIC_\(API_KEY\|AUTH_TOKEN\)='     # 0 is what you want
```

You do not have to remember, because `pr_review.sh` refuses on its own — the **first** gate of a
real run, ahead of the lock, the brain gate and any model call. Prove it now, the same way you
prove the kill switch in step 8:

```bash
ANTHROPIC_API_KEY=whatever bash .claude/skills/dev-loop-pr-review/scripts/pr_review.sh --limit 1
echo $?                                                          # 28
grep 'reason=metered_api_billing' .claude/loops/pr-review.log
```

Expect exit **28**, a refusal that names only the *variable*, **no runner lock left behind**, and
no `event=start` line at all. Presence alone is detected — **no part of the value is ever read,
printed or logged**; check stderr and the log and confirm nothing of a key appears there.
`--dry-run` is deliberately not gated: it makes no model call, so it cannot bill.

If you ever genuinely mean to bill, `LOOP_PR_REVIEW_ALLOW_API_KEY=1` arms it: a loud banner, an
`event=metered_api_billing_armed` line, and every consumption message in the run flips to money
framing. Do not use it for any step in this document.

## 5. The first real tick — one PR, short leash

```bash
bash .claude/skills/dev-loop-pr-review/scripts/pr_review.sh --limit 1 --timeout 900
```

Watch it. It prints its `run_id` (`pr-review-<stamp>-<pid>`) — **copy it**, you need it in
step 6.

Deliberately smaller than the template's arguments: one PR and a 15-minute watchdog. **The first
tick is about proving the machinery, not draining the queue.** Posting is dry-run by default —
do not add `--live`, and do not widen `--scope`.

You should see, on stderr: the runner lock acquired, the shared agent lock acquired, `brain
reachable, paused=false — proceeding`, `posting=dry-run — previews and a report, nothing reaches
the vcs host`, the tick banner, then the assertion and `done — N PR(s) newly marked`.

## 6. What to check when it comes back

| Check | Command | What good looks like |
|---|---|---|
| Exit code | `echo $?` | `0` |
| Report | `ls -t .claude/loops/pr-reviews/ \| head -1` | one `<date>-pr-review-….md` |
| Read it | `cat .claude/loops/pr-reviews/<file>` | opens with `PR sweep — …`; names every skipped PR **and** every withheld finding with its reason; says `Spend: unmeasured` (the session has no meter — a number there would be a guess) |
| Log | `grep event=end .claude/loops/pr-review.log` | `rc=0`, `outcome=reviewed`, `reported=1`, `marked=1`, and a `spend_usd=` **paired with `spend_basis=`** |
| **What that number is** | read `spend_usd=` together with `spend_basis=` | `list-price-estimate-unbilled` means the harness's own client-side estimate under subscription auth, and **nothing was billed** — a consumption reading and a proxy for rate-limit-window burn. `metered-api-billed` would mean the API-key override was armed and it really is money. README → *What the consumption numbers are* |
| **Outcome** | `grep -o 'outcome=[a-z-]*' .claude/loops/pr-review.log` | `reviewed` (or `empty-queue`). Anything else — `auth-expired`, `busy`, `ledger-unreadable` — is the tick correctly reporting that it achieved nothing, and three in a row exit **24** |
| **The guard** | `grep -o 'guard_interventions=[0-9]*' .claude/loops/pr-review.log` | `0`. Anything else means the runner had to override the session — read `pr-review.launchd.err.log`, it prints each one in full |
| What it wrote to the brain | `python3 .claude/skills/qa-kb/scripts/kb.py receipt --ticket pr-review --run-id <run_id> --since 1d` | at least one row — **the ledger is the truth, the report is the claim** |
| The preview | `ls -t pr-*-threads.preview.json \| head` | one per reviewed PR that had findings; byte-faithful to the payload a live post would send. It lands in the working directory unless the recipe passes `--out` |
| The seen-ledger | `python3 .claude/skills/dev-pr-feed/scripts/pr_feed.py ledger` | one entry, with a `head_commit` and `"mode": "dry-run"` |
| **All locks released** | `python3 .claude/skills/dev-pr-feed/scripts/pr_feed.py lock --status`; `ls -d .claude/loops/agent.lock .claude/loops/pr-review.lock 2>/dev/null` | `free`; nothing listed |
| **No leaked worktrees** | `git -C <app.repo_dir> worktree list \| grep pr-review` | nothing |

The last two are the ones to be strict about. A tick that leaves `pr_feed`'s lock held starves
the next tick for up to 45 minutes, and a leaked worktree is what the recipe calls the cardinal
sin. If either is dirty after a clean `rc=0`, the runner's own cleanup did not fire — that is a
bug in the runner, not something to tidy up and forget.

Read the report properly, not just its first line. On someone else's PR the **rebuttal rule**
outranks correctness: a finding that is factually true but ignores reasoning the author already
wrote in a comment, a commit message, or the PR description must be withheld rather than posted.
**A rule that keeps withholding the same finding for the same reason across many PRs is telling
you the rule is wrong.**

Exit codes, briefly: **6** `LOOP_APP_ROOT` is not a checkout · **20** brain unreachable ·
**21** the tick left no trace · **22** watchdog timeout · **23** lock error · **26** the day's
recorded consumption hit the per-day ceiling · **27** that counter is not writable · **28**
metered API billing refused (step 4b) · **29** a model-API **429** — out of the rolling window.
Anything else came from `claude` itself. The full table is in `JOB-CONTRACT.md`.

**Exit 29 is not a failed first run.** It means the *model call* hit an HTTP 429 because the
rolling window — shared with your own interactive usage — was exhausted. The tick marks nothing,
retries nothing, tears down cleanly, and comes back next hour. It deliberately does not count
toward the exit-24 stuck counter. Do **not** confuse it with `outcome=api-unavailable`, which is a
**vcs-host** throttle or 5xx on a board read. If you hit 29 during these hand steps, wait for the
window and re-run the step — nothing is wrong with the runner.

## 7. Second hand tick — prove the ledger suppresses a repeat

Run the exact same command again, immediately:

```bash
bash .claude/skills/dev-loop-pr-review/scripts/pr_review.sh --limit 1 --timeout 900
```

Two things to confirm, and they are the whole point of this step:

- **It does not re-review the PR from step 5.** The seen-ledger is keyed on PR id **plus head
  commit**, so a PR already reviewed at the commit it is sitting on is no longer a candidate. If
  it reviews the same PR again, the ledger did not stick — and an hourly timer would then re-review
  (and, once live, re-comment on) the same revision every hour, forever. This is the PR loop's
  analogue of the dream's watermark check, and it is the single most important thing to prove
  before arming a job that runs 24 times a day.
- **It picks up the next PR instead**, or reports an empty queue in words. Either is a pass.

Then prove the lock, in two terminals:

```bash
# terminal 1
bash .claude/skills/dev-loop-pr-review/scripts/pr_review.sh --limit 1
# terminal 2, while that one is running
bash .claude/skills/dev-loop-pr-review/scripts/pr_review.sh --limit 1
```

The second must exit **0** with `already running since …`, review nothing, and leave exactly one
line in the log:

```bash
grep 'event=skipped reason=already_running' .claude/loops/pr-review.log
```

## 8. Prove the kill switch fails closed

The R9 flag is the control you will actually reach for later, so confirm it works before you
need it:

```bash
python3 .claude/skills/qa-kb/scripts/kb.py pause --on --by "$USER" --reason "first-run gate test"
bash .claude/skills/dev-loop-pr-review/scripts/pr_review.sh --limit 1     # expect exit 0, nothing reviewed
grep 'event=skipped reason=paused' .claude/loops/pr-review.log
python3 .claude/skills/qa-kb/scripts/kb.py pause --off --by "$USER"
python3 .claude/skills/qa-kb/scripts/kb.py pause --status                 # paused:false, before you arm anything
```

The runner checks this **before the model is called**, so a paused tick is nearly free. Note the
gate you have *not* proven here: `dev-pr-feed`'s own pause check is only reached on a `--live`
post — the dry-run branch returns before it. **Do not read a clean dry-run tick as evidence that
the kill switch stops a live post.** That belongs to the going-live gate, not this one.

## 8b. Prove the posting guard refuses `--live` on a dry-run tick

The one gate that stands between "the runner decided dry-run" and "threads on a colleague's pull
request" is mechanical, not a sentence in the prompt. The proof is in the tick you already ran:

```bash
grep -o 'guard_interventions=[0-9]*' .claude/loops/pr-review.log | tail -3
```

It should read `0` on a well-behaved tick. If a session ever does type `--live` on a dry-run tick,
the guard strips it, the JSON that comes back says so, the number above goes up, and
`pr-review.launchd.err.log` prints the refusal in full. **Nothing reaches the vcs host either
way.** What the guard is not: a sandbox. It stops the likely mistake — the recipe's own command
block shows `[--live]`, and a PR description is attacker-controllable text.

## 9. Only now, arm the timer

```bash
bash .claude/loops/scheduler/install.sh --label com.loops.pr-review --check
bash .claude/loops/scheduler/install.sh --label com.loops.pr-review --yes
```

`--check` first, **always**. It renders the template for this machine, re-resolves every binary
**under the job's `PATH`**, imports the brain driver and runs `kb pause --status` under the job's
PATH and HOME, probes the vcs_host adapter the same way, proves `LOOP_APP_ROOT` is a checkout and
the worktree root is writable, reads the posture out of `loop-limits.json` and out of the
rendered arguments, and counts the dry-run entries in the ledger.

**Read the schedule sentence it prints before you type `YES`, and check it against the
template.** No cadence is written down in this document or in `README.md`, on purpose:

```bash
bash .claude/loops/scheduler/install.sh --label com.loops.pr-review --check | grep schedule
grep -A4 StartCalendarInterval .claude/loops/scheduler/com.loops.pr-review.plist.template
```

`--check` also answers whether the plist's own `Label` matches its filename, whether the copy
already armed in `~/Library/LaunchAgents` differs from **what this machine would install** (and
what **its** posting posture is), and whether the template, the runner and the sidecar are
**tracked in git** — arming a job that points at untracked files binds a permanent hourly job to
files one `git clean -fdx` removes.

`RunAtLoad` is false, so installing does not start a tick. Confirm the first scheduled one fired:

```bash
grep event=end .claude/loops/pr-review.log | tail -3
launchctl list | grep com.loops.pr-review
```

Remove it with
`bash .claude/loops/scheduler/install.sh --label com.loops.pr-review --uninstall`.

---

## Two things to know before you arm it

**Your hand ticks marked those PRs as reviewed — in `dry-run` mode.** That is correct behaviour,
and it has a consequence that looks like a broken timer: once the queue is drained, the armed job
will report an empty queue every hour until somebody pushes a new commit. **That is the loop
working**, and `README.md` → *What a normal tick looks like* is the section to re-read when it
starts feeling too quiet. The signal that it is alive is one `event=end rc=0` per hour, not
comments.

**When you later go live, those PRs will not be re-reviewed.** The ledger says they were already
handled at that head commit, so their findings — which only ever reached a preview file — are
never delivered. Re-queue the ones you want:

```bash
python3 .claude/skills/dev-pr-feed/scripts/pr_feed.py ledger --forget <pr-id>
```

Re-queue them one at a time. Do **not** clear the ledger to force a re-review: it is the single
mechanism that stops the loop commenting on the same revision every hour, and the same mechanism
that brings a PR back the moment its author pushes a response to your findings. You will be
reminded twice more — `install.sh --check` counts the `mode: dry-run` entries whenever the job is
armed live, and every live tick prints the list and the `--forget` commands in its banner.

## An operator note: the two agents are serialized, and you should know how

`com.loops.qa-dream` and `com.loops.pr-review` can land on the same minute, and a laptop waking
from sleep fires both at once whatever their minutes say. Both take a **shared cross-agent lock**
(`.claude/loops/lib/agent_lock.sh`) before any real work, so only one loop agent is ever in a
model call, a credential mint, or a brain write. Check what each is armed for before you decide
whether you care — never from prose, always from the templates:

```bash
bash .claude/loops/scheduler/install.sh --check                               # qa-dream
bash .claude/loops/scheduler/install.sh --label com.loops.pr-review --check   # pr-review
```

On a shared slot the dream usually wins (a dream's `--timeout` is **per pass** and it runs more
than one) and this tick logs:

```bash
grep 'reason=agent_lock_held' .claude/loops/pr-review.log
cat .claude/loops/agent.lock/holder     # who is in there right now
```

That skip exits **0** and is not a failure: the PR queue is durable and keyed on PR id + head
commit, so the same queue is picked up an hour later. It deliberately does **not** feed the
exit-24 stuck counter, because a crashed holder's bolt is reclaimed on the TTL that holder itself
recorded, so the skip cannot repeat forever.

**Debugging by hand at the shared slot?** `LOOP_AGENT_LOCK_BYPASS=1` runs anyway and says so
loudly in both the log and stderr. It still *tries* for the bolt first, so a hand run on a free
bolt excludes the timer; and it never releases a lock it did not take.

## If threads were ever posted by hand before the ledger existed

The ledger is per-machine runtime state that is gitignored. If `dev-loop-pr-review` was ever
driven by hand and posted live threads before this machine's ledger existed, nothing records it:
`ledger_read_all()` returns `{}` for a missing file, so a blank ledger reads as *"nothing has ever
been reviewed."*

That is why `pr_review.sh --live` **refuses to run at all** (exit 25) when the ledger file is
absent. Before the first live tick, seed it at the commits already reviewed:

```bash
python3 .claude/skills/dev-pr-feed/scripts/pr_feed.py mark \
        --pr <pr-id> --commit <the sha it was reviewed at> --mode live --verdict reviewed
```

`--first-run` overrides the refusal, and is the right flag only when you genuinely mean *"no pull
request anywhere has a loop thread on it."*
