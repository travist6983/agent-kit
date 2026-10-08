# The scheduled-job contract

What every unattended loop agent in this directory promises, what the scheduler gives it, and
what you have to supply to add one. `install.sh` checks the mechanical parts of this at arm
time; the rest is what the runners (`dream.sh`, `pr_review.sh`) already do and what a new
runner must copy.

## The shape

A job is three tracked files and one convention:

| Piece | File | Role |
|---|---|---|
| Decisions | `scheduler/<label>.plist.template` | Schedule, arguments, priority class, log paths. Everything that is a *decision* lives here and nowhere else — no doc restates a cadence or an argument list. |
| Runner | `.claude/skills/<skill>/scripts/<runner>.sh` | The headless wrapper the plist runs: locks, gates, watchdog, receipt, cleanup, exit codes. |
| Sidecar (optional) | `scheduler/<label>.job.sh` | The facts about *this* job that cannot be read out of the plist, and extra preflight checks. A missing sidecar is the generic behaviour. |
| Label | `com.loops.<name>` | The plist's `Label` key **must** equal the filename stem. launchd registers the job under the key inside the file; `install.sh` fails the preflight when they disagree. |

`install.sh --label <label>` renders the template, preflights the result, and installs it.
`bash install.sh --check` is always safe: it copies nothing and bootstraps nothing.

## Template placeholders

`install.sh` fills these and refuses to install while any `{{…}}` remains:

| Placeholder | Resolved from | Verified as |
|---|---|---|
| `{{REPO_DIR}}` | this checkout (the installer's own location) | the directory that holds `.claude/` and `lib/` |
| `{{HOME}}` | the installing user's real `$HOME` | a directory |
| `{{CLAUDE_BIN}}` | `LOOP_INSTALL_CLAUDE_BIN`, else stack.config `runner.claude_bin` (a bare name is resolved once, on the operator's PATH, and pinned absolute) | an executable file |
| `{{CONFIG_DIR}}` | `LOOP_INSTALL_CONFIG_DIR`, else `runner.config_dir`, else `$CLAUDE_CONFIG_DIR`, else `~/.claude` | a directory |
| `{{PATH}}` | `LOOP_INSTALL_PATH`, else derived from where claude / python3 / git resolve plus the system directories | every required binary re-resolves **under it** |

Everything that is *not* a placeholder is a decision and is tracked verbatim.

## The environment a job receives

A launchd job inherits **nothing** from your shell: no `.zshrc`, no `/etc/paths`, a bare
default `PATH`. The plist therefore declares the whole environment, and the preflight re-resolves
everything under exactly that environment:

| Variable | Why it is load-bearing |
|---|---|
| `PATH` | Must resolve `python3`, `git`, `bash`, the claude binary, whatever the configured adapters shell out to, and the CLI the brain's auth mode uses (kb.config `auth.mode` `token_command` → the first word of `auth.command`). `kb.py` swallows a `FileNotFoundError` from that CLI, so a wrong `PATH` is a **silent** brain failure at the scheduled hour. |
| `HOME` | A Python driver installed in a user site-dir, a credential cache the brain's auth mode or a vcs_host provider reads, the secret env file next to `kb.config.json`, and the `CLAUDE_CONFIG_DIR` default all hang off it. |
| `LOOP_CLAUDE_BIN` | Pins the exact binary so a later `PATH` change cannot silently swap which `claude` runs. Neither runner ever trusts a bare `claude`. |
| `CLAUDE_CONFIG_DIR` | The Claude Code config dir the headless session reads. |
| `LANG` | `en_US.UTF-8`, so reports and log lines are not mangled. |

What a job must **never** receive: `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`. Either takes
precedence over a subscription credential and turns the run into metered billing;
`pr_review.sh` refuses a tick that sees one (exit 28) unless `LOOP_PR_REVIEW_ALLOW_API_KEY=1`
says so on purpose. A plist declares its own environment, so the scheduled run is safe by
construction; the exposed case is a hand run from a shell that exports one.

Per-job overrides a plist *may* add to `EnvironmentVariables` (every one is `${VAR:-default}`
in the runner): `LOOP_APP_ROOT`, `LOOP_PR_WORKTREE_ROOT`, `LOOP_PR_LEDGER_FILE`,
`LOOP_PR_REVIEW_STATE`, `LOOP_AGENT_LOCK_DIR`, `LOOP_DREAM_MODEL`, `LOOP_PR_REVIEW_MODEL`. The
defaults are derived from `stack.config.json` (`app.repo_dir`, `project.name`, `models.*`), so
a plist normally declares none of them.

## What the runner promises

1. **Refuses to run metered by accident** (pr-review): the API-key check is the first gate,
   ahead of the lock, the brain gate and any token. Presence only is detected; no key material
   is ever read, printed or logged.
2. **Takes its own single-instance lock**, then the **shared cross-agent lock**
   (`lib/agent_lock.sh`), before any real work. The loser **skips, exit 0** — never waits —
   because every queue is durable (the dream watermark; the PR seen-ledger keyed on PR id +
   head commit). A missing lock library is a refusal (exit 23), never an unserialized run.
   `LOOP_AGENT_LOCK_BYPASS=1` is the deliberate hand-run escape and is logged loudly.
3. **Gates on the brain, fail closed**: `kb pause --status` must return `ok:true` (exit 20
   otherwise). An explicit `paused:true` — the R9 kill switch — exits 0 having done nothing.
4. **Reads its bounds from `loop-limits.json`**, not from its own literals: the per-run
   consumption ceiling (`r5_budget`), and for the PR sweep `prs_per_tick`, `author_scope`,
   `max_threads_per_pr`, `per_day_usd_cap`. A fallback literal is used only when the file is
   unreadable, and the runner says so on stderr.
5. **Pins the model** (stack.config `models.*`) so a settings change cannot silently alter what
   a run consumes or judges, and passes `--disallowedTools` for every human-blocking or
   out-of-scope tool, `AskUserQuestion` first: a headless run has no human, so an ask is a
   deadlock, not a question.
6. **Watchdogs the model call**: TERM at `--timeout`, KILL after the grace period, exit 22. A
   `--timeout` under 60s is refused; `--timeout 0` must never silently disable the watchdog.
7. **Leaves a receipt or fails**: a brain `metrics` row for its run id, or a report that says in
   words what happened. Neither → exit 21. Doing nothing is never a failure; leaving no trace is.
8. **Classifies a model-API 429 as "out of window", exit 29**, not as a fault: it does not
   bump any stuck counter and the queue is untouched.
9. **Cleans up on every exit path** — `EXIT`, `INT`, `TERM`, a watchdog kill: reaps the child,
   releases only the locks it can prove are its own, tears down only the worktrees it created,
   and writes an `event=end` line.
10. **Never ships code, never asks a question, never blesses an expectation.**

## Exit codes

Both shipped runners use the same numbers for the same meanings. A new runner reuses them.

| Code | Meaning |
|---|---|
| `0` | success · already-running skip · the shared bolt is held by another agent · R9 paused |
| `2` | usage error — the plist's arguments are wrong |
| `3` | no real claude binary found (`LOOP_CLAUDE_BIN` / `runner.claude_bin`) |
| `4` | the pre-model step failed (*dream*: harvest; *pr-review*: `--dry-run` queue read) |
| `5` | cannot `cd` to the repo (`WorkingDirectory` moved) |
| `6` | *pr-review* — `LOOP_APP_ROOT` is not a git checkout; no model call was spent |
| `20` | brain unreachable — `kb pause --status` did not return `ok:true` |
| `21` | left no trace — no metrics row for this run id *and* no report saying why |
| `22` | watchdog timeout — the model call was TERMed/KILLed |
| `23` | lock error — could not acquire or reclaim a lock directory, or `lib/agent_lock.sh` is missing |
| `24` | *pr-review* — STUCK: three consecutive ticks achieved nothing for the same reason |
| `25` | *pr-review* — a `--live` tick refused before consuming anything (no seen-ledger without `--first-run`; a widened scope inherited from the JSON instead of re-affirmed with `--scope`) |
| `26` | *pr-review* — today's recorded consumption reached `per_day_usd_cap` |
| `27` | *pr-review* — the consumption counter file is not writable |
| `28` | *pr-review* — metered API billing refused (`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` set) |
| `29` | model-API rate limit (HTTP 429) — out of window, **not a fault** |
| `130` / `143` | INT / TERM — stopped by a person or the OS; an `event=end` line is still written |
| other | the claude exit code, passed through |

## Logs and artifacts — the naming convention

| File | Written by | Contents |
|---|---|---|
| `.claude/loops/<job>.launchd.out.log` / `.err.log` | launchd (`StandardOutPath` / `StandardErrorPath`) | the full transcript |
| `.claude/loops/<job>.log` | the runner | one line per event: `… run_id=<id> event=start …` / `event=end rc=<n> duration_s=<s> outcome=<class> …` |
| `.claude/loops/<artifact dir>/<date>-<run_id>.md` | the runner | the product of one run (`dreams/`, `pr-reviews/`) |

`install.sh` derives the summary log from the transcript path (`<job>.launchd.out.log` →
`<job>.log`), so keep the convention. Every runtime file above is gitignored; the templates,
runners and sidecars are tracked.

Every `event=end` line carries `outcome=`, and a consumption figure carries `spend_basis=` on
the same line (`list-price-estimate-unbilled` under the subscription posture,
`metered-api-billed` under an API key). The field names are stable on purpose: a runbook greps
for them.

## The sidecar hooks

`install.sh` sources `<label>.job.sh` after it has rendered and read the plist, with these
available: `$SRC` (the rendered plist), `$TEMPLATE`, `$SCRIPT`, `$JOB_PATH`, `$JOB_HOME`,
`$LOOPS_DIR`, `$REPO`, `$HERE`, `$LABEL`, `read_plist <key>`, `job_command <plist>`,
`stack_cfg <dotted.key> [default]`, `ok` / `warn` / `bad` / `say`.

| Hook | Kind | Purpose |
|---|---|---|
| `JOB_ARTIFACT_DIR` | variable | where the job drops per-run reports (printed in the epilogue) |
| `JOB_EXTRA_BINS` | variable | binaries beyond `python3 git bash` (+ the brain's auth CLI) that must resolve under the job PATH |
| `JOB_KICKSTART_WARNING` | variable | what `launchctl kickstart` actually does, said at the moment it is offered |
| `JOB_GATE_NOTE` | variable | an extra paragraph for the typed-YES gate |
| `job_preflight` | function | extra checks; return non-zero to fail the preflight |
| `job_epilogue` | function | extra post-install notes |

A sidecar states facts the plist cannot; anything the plist already states is read back from
it. It never restates a cadence or an argument list.

## Adding a job

1. Write the runner against the promises above. Copy `dream.sh`'s lock, gate, watchdog and
   receipt blocks rather than inventing a second style.
2. Register its script name for the shared lock: either pass it as the `<process-name>`
   argument of `agent_lock_acquire` (always) **and**, so a lock written by an older build still
   recognises it, add it to `LOOP_AGENT_LOCK_KNOWN_PROCS` in the plist's `EnvironmentVariables`.
3. Write `scheduler/com.loops.<name>.plist.template` from one of the shipped ones. Keep
   `RunAtLoad` false, `/bin/bash` at index 0 and the runner at index 1, one
   `StartCalendarInterval` entry, no double hyphen inside any XML comment.
4. Add a sidecar only for facts the plist cannot carry.
5. Hand-run the runner twice, clean, before `install.sh --yes` (the FIRST-RUN documents are
   the pattern). Arming a job that has never completed once is 24 chances a day to find out
   something was wrong.
6. Add the regression test under `.claude/loops/tests/` that proves the lock, the gate and the
   receipt against a fake `claude` and a fake `kb.py`.
