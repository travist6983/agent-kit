---
name: qa-dream
description: >-
  The nightly consolidation pass: read yesterday's raw transcripts + loop artifacts + current
  brain state and write back COMPRESSED lessons, so the next run starts warm even when the
  in-run write was skipped, blocked, or never designed. Trigger for "run the dream", "dream
  on the last week", "consolidate what we learned", "the brain missed a bunch of runs — catch
  it up", or as the scheduled batch job behind scripts/dream.sh. Also the brain's RETRY loop:
  it replays the kb outbox and re-embeds pending rows before consolidating, so an embedder
  429 or a dead DB costs a delay, never a lesson. Harvest is deterministic and zero-LLM; the
  agent only consolidates. Writes app_map/fix_lessons/flakiness/metrics autonomously and
  PROPOSES expectations — never blesses. Does NOT fix, ship, edit skills, or touch the
  target app's working tree.
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
---

# qa-dream — consolidate the day, so the next day starts warm

Every loop is *supposed* to write what it learned back to `qa-kb`. In practice writes get
skipped (the agent forgot), blocked (the embedder 429s, the DB is unreachable), or were never
designed at all — the everyday interactive lane isn't a loop and has no write step. `qa-dream` is the
backstop: a periodic batch pass that reads what actually happened — raw session transcripts,
run artifacts under the evidence root (`evidence.root` in stack.config, `runs/` by
default), and the brain's own write ledger — and lands the lessons the runs didn't.

It is the **third learning producer**, next to `qa-explore` (AI walks a feature) and
`human-explore` (a human corrects). Those two learn from a *deliberate* session. `qa-dream`
learns from **every** session, after the fact, whether or not anyone meant it to.

Two properties make it safe to run unattended:

- **The harvest is deterministic and zero-LLM.** `scripts/harvest.py` does all the scanning,
  truncating, capping and cross-session merging with no model in the loop. Same inputs → same
  brief, byte for byte. The agent never reads a raw transcript; it reads a bounded brief.
- **Every dreamed row is traceable and forgettable.** Everything written carries
  `--source qa-dream` plus dream metadata (date, session ids, evidence paths), so a bad dream
  is one `kb forget --source`-scoped cleanup away, and a human can always ask *where did this
  come from*.

## What this block does NOT do (atomic boundary)

It does **not** fix code, ship, verify, reproduce, run a suite, or **edit any skill**. Skill
defects it finds are *appended to a tracked queue* (`.claude/loops/skill-followups.md`,
created on the first append) for a human or `skill-creator` to act on — the dream never
rewrites a `SKILL.md`. It **never** writes to the
target app's working tree (`app.repo_dir` — read-only there, always). It **never** blesses an
expectation.

## Success criteria & failure categories (declared up front — principle 1)

**Success** — all of:
1. `kb pause --status` was checked **first** and returned an explicit `paused:false`.
2. `kb outbox --replay` and `kb reembed` ran **before** consolidation, and their results are
   in the report (this pass is the brain's retry loop, not just its writer).
3. Every candidate in the brief is accounted for: **consolidated**, **skipped as
   `already_in_brain`**, or **explicitly declined with a reason** — nothing silently dropped.
4. Near-duplicate candidates across sessions are **merged into one lesson with an occurrence
   count**, not written N times. (Three sessions hitting the same trap is one lesson.)
5. Every written row carries `--source qa-dream` + dream metadata, and every lesson cites
   evidence that exists (a session id, an artifact path).
6. `expectations` were **proposed**, never blessed. `app_map` / `fix_lessons` / `flakiness` /
   `metrics` written autonomously.
7. A dated report exists at `.claude/loops/dreams/<date>-<run_id>.md` (the runner
   hands you the exact path as `REPORT=`), and the watermark was advanced **only** because the
   pass was clean.
8. The pass either **landed at least one brain row** or the report **says in words** that it
   had nothing to consolidate. `dream.sh` asserts exactly this afterwards (exit 21) — a run
   that writes nothing and does not say why is a failure, not a quiet success.

**Failure categories (emit the class, not just "failed"):**

| Class | Meaning | Next move |
|---|---|---|
| `paused` | R9 kill switch on, or pause unverifiable | **Stop. Write nothing.** Report and exit clean. Fail closed — "couldn't verify" is never "not paused". |
| `empty-window` | brief has no candidates | Write the report saying so, advance the watermark, stop. Honest nothing beats invented something. |
| `brain-unreachable` | `kb` can't connect — **or `kb` itself never resolved** (exit 127: a *skipped* write, not an unreachable brain; re-run through the resolved path in *Invoking `kb`* before classing the pass) | Stop before any write; the outbox already holds what earlier runs queued. Do **not** advance the watermark. |
| `embedder-down` | the embedding provider returns 429 / quota exhausted | Expected, not fatal: `fix_lessons` and `propose expectation` still store with `embed_status:pending`. Record how many are pending; the next dream's `kb reembed` finishes the job. Never `--embedder stub`. |
| `no-evidence` | a candidate has no citable session/artifact | **Decline it.** Never write a lesson you can't cite. |
| `duplicate-flood` | proposing near-duplicates of existing rows | `kb search` first; reference the existing row instead (R10). |
| `over-consolidation` | one lesson merged from genuinely different traps | Split it. A lesson that fits everything guides nothing. |
| `budget-exceeded` | brief hit `--max-bytes` and evidence was trimmed | Narrow the window and re-run rather than dream on a truncated picture. |

**Measurable (is it getting better?):** `dream_rows_written`, `dream_candidates_declined`,
`dream_outbox_replayed`, `dream_reembedded`, `dream_pending_embeds`,
`dream_already_in_brain_ratio`. A healthy trend is the **ratio rising** — the loops are
writing more of their own knowledge in-run and the dream is doing less catch-up. A dream that
keeps landing lots of rows is telling you a loop's write step is broken.

## Inputs / Outputs

**Inputs:** a window (`--since 7d`, or the durable watermark in `kb state --key
dream.watermark`), optionally a `BRIEF_FILE` already harvested by `scripts/dream.sh`.
**Outputs:**
- brain rows (the real product) — `app_map`, `fix_lessons`, `flakiness`, `metrics`, plus
  **proposed** `expectations`;
- `.claude/loops/dreams/<date>-<run_id>.md` — the dated report (tracked in git).
  **One file per pass, never overwritten** — a second dream on the same day (or a `--catch-up`
  pass) gets its own file, so no pass can erase an earlier pass's memory. `dream.sh` computes
  the path and passes it as `REPORT=`; use it verbatim;
- appended entries in `.claude/loops/skill-followups.md` (tracked in git; created on the
  first append, in the entry format under step 6);
- an advanced `dream.watermark` — **only** on a clean pass.

## Procedure

**Invoking `kb`.** Every `kb <verb>` below is the `qa-kb` CLI wrapper
(`.claude/skills/qa-kb/scripts/kb`), which must be **on your PATH** (one symlink per machine —
`docs/brain-setup.md`). It is **not** there in a fresh checkout: an unresolved `kb` exits **127**
(`kb: command not found`) and the brain write is simply *lost*, so fall back to the script instead of
skipping the call — **absolutely**, never cwd-relative, because a loop's cwd is normally an
`infra-worktree` sandbox, not this repo:

```bash
# <agents-repo> = the checkout of this framework THIS file was loaded from; set both in ONE shell.
KB="${KB_BIN:-<agents-repo>/.claude/skills/qa-kb/scripts/kb.py}"
python3 "$KB" <verb> ...        # ≡ the bare `kb <verb> ...` calls below
```

Either route reads the tracked `.claude/loops/kb.config.json` and lands on the **one shared
brain** it names (an explicit Postgres + pgvector DSN, any host, any configured auth mode) —
there is no flat-file or local fallback, and a DSN that is missing or points at loopback without
`KB_ALLOW_UNSHARED=1` is refused, never silently written.
Under `dream.sh` this pass is **headless — no human is there to improvise on a 127**. The runner
`cd`s to the framework checkout before invoking you, so the repo-relative
`.claude/skills/qa-kb/scripts/kb.py` does resolve for *this* pass — but resolve `$KB` as above
anyway, and treat a `kb: command not found` as a **failed pass** (report it, leave the watermark
alone), never as a write that quietly didn't happen.

### 1. Check the kill switch FIRST — fail closed

```bash
kb pause --status
```
Proceed **only** on an explicit `"paused": false`. `true`, `null`, `ok:false`, a non-JSON
response, or a timeout all mean **stop** — dreaming writes to the brain, so R9 governs it
exactly like a ship. Report class `paused` and exit 0 without writing anything.

### 2. Drain the retry queue BEFORE consolidating

The dream is also the brain's retry loop. Anything the day's runs failed to land is sitting
in the outbox or embedded-pending; clear it before adding more, so the report counts the
day's knowledge once.

```bash
kb outbox --status
kb outbox --replay            # re-runs each queued argv; drops the line on success
kb reembed --store all        # embeds rows stuck at embed_status='pending'
```
`kb reembed` reports `similar_to` for a row whose vector now reveals a near-duplicate.
**Report only — never auto-merge.** Note the pair in the report; a human (or `human-explore`)
decides. If the embedder is still 429ing, `reembed` will report the pendings unchanged —
that's the `embedder-down` class, not a failure. **Never** fall back to `--embedder stub`.

### 3. Harvest the window (deterministic, zero-LLM)

If `dream.sh` handed you a `BRIEF_FILE`, **use it — do not re-harvest.** Otherwise:

```bash
python3 .claude/skills/qa-dream/scripts/harvest.py \
  --watermark "<ts from kb state --key dream.watermark>" \
  --out /tmp/dream-brief.json
# no watermark yet? use --since 7d
```
Read `references/harvest-brief.md` for the brief's shape and what each candidate list means.
The brief already: bounded every string, capped every category, merged failures across
sessions into `env_lessons` with occurrence counts, and marked each candidate
`already_in_brain: true | false | null` from `kb receipt`.

It has also already **thrown most failures away**. `env_lessons` carries only failures with a
named diagnostic class (`command-not-found`, `no-such-file`, `harness-blocked`,
`input-validation`, `git-fatal`, `permission-denied`, `module-not-found`,
`connection-refused`); timeouts, bare `exit-1`s, heredoc tracebacks and tool calls the human
declined are dropped and counted in `filtered.env_lessons_filtered`. So a short `env_lessons`
list is the filter working, not a thin week — and `filtered.env_lessons_filtered_total` is the
number to quote in the report, not to go re-mine. Read `references/harvest-brief.md` §"The
`env_lessons` filter" before deciding the brief missed something.

**`already_in_brain` is three-valued.** `true` → skip, a run already wrote it. `false` → this
is yours to write. `null` → **unknown** (the brain lookup was unavailable): `kb search` before
writing, and never treat unknown as absent.

If the brief has no candidates: skip to step 7, write the report, advance the watermark, stop
(class `empty-window`).

### 4. Consolidate — compress, merge, decline

For each candidate, produce **one compressed, actionable lesson** — the shape the *next* run
can use, not a summary of what happened.

- **Merge across sessions.** This is the whole value. `env_lessons` arrive pre-bucketed by
  failure signature with `occurrences` and `distinct_sessions` — write ONE row carrying the
  count, not one per session. Same for a symptom seen on two tickets.
- **Search before writing.** `kb search app_map --query "<symptom>"`,
  `kb search fix_lessons --query "<symptom>"`, `kb search expectations --query "<behavior>"`.
  A close match means *reference or update it*, not propose a twin (R10). (Semantic search
  needs the embedder; when it's down, fall back to the documented degraded path —
  `kb read fix_lessons --area X` / `kb read expectations --area X` — and say so in the report.)
- **Decline honestly.** No citable evidence → decline, class `no-evidence`. Genuinely
  different traps in one bucket → split rather than write a lesson that fits everything.
- **Areas are normalized** (`Billing/Invoices` → `billing-invoices`) — the brief already did it;
  keep it.

### 5. Write — autonomous for the mechanical stores, PROPOSE-ONLY for expectations

Every write carries the provenance that makes it forgettable:

```bash
META='{"dream_date":"<date>","sessions":["<id>",...],"evidence":["<path>",...],"occurrences":<n>}'

# environment/tooling traps and routes → app_map (mechanical, autonomous)
kb write app_map --kind lesson --area <area> --key "<stable.key>" \
  --value "{\"lesson\":\"…\",\"signature\":\"…\"}" --source qa-dream --metadata "$META"

# symptom → root cause → fix shape (experience, not an oracle — no bless needed)
kb write fix_lessons --area <area> --symptom "…" --root-cause "…" --fix-shape "…" \
  --source qa-dream --metadata "$META"

# per-test flake data points / quarantine notes. `--spec` is the brain's test-id field
# (`<test file> > <test name>`, the convention the target repo supplies) — not an RSpec or
# Playwright spec file; the dream never reads or writes test files.
kb write flakiness --spec "<test file> > <test name>" --area <area> [--failed] --source qa-dream

# loop-health counters (R5/R8 fuel)
kb metric --name dream_rows_written --value <n> --unit count --source qa-dream

# behaviour that SHOULD hold → PROPOSED ONLY. A human blesses via human-explore.
kb propose expectation --area <area> --statement "…" --source qa-dream --metadata "$META"
```

**Never `kb bless`.** A wrong canonical expectation makes the evaluator optimise toward a wrong
target (R1) — and a dream's inference is exactly the kind of judgment that needs a human. If a
row already exists and only needs freshening, prefer `kb confirm app_map --id N` over rewriting
it; on any update pass `--expect-version` (R4) so a concurrent loop can't be clobbered.

A write that comes back `embed_status: "pending"` with `warning: embedder_unavailable` is a
**success** — the row is durably stored, exit 0. Count it as pending and move on.

### 6. Queue the skill defects (never edit a skill)

Append every `skill_followups` candidate to `.claude/loops/skill-followups.md` — one entry
per distinct defect. The file is a plain Markdown queue; create it with the header below if it
does not exist, and never rewrite earlier entries except to bump their count or flip their
status. Merge into an existing `open` entry with the same skill + pattern class (bump
`Occurrences`, add the new session id) rather than adding a near-duplicate. A `human`-voiced
signal outranks an `assistant`-voiced one. **Do not edit the skill.** A human or
`skill-creator` acts on the queue and sets `Status` to `done` or `wontfix`.

```markdown
# Skill follow-ups — defects the loops discovered, queued for a human / skill-creator
<!-- one entry per distinct defect; appended by qa-dream (never edits a skill) -->

## <skill-name> · <pattern-class> · <dream_date>
- **Signal:** <one line: what the skill says vs. what actually happened>
- **Evidence:** "<verbatim quote>" — session <id>[, <id>] · <artifact path, if any>
- **Occurrences:** <n> · **Voice:** human | assistant · **Status:** open
- **Suggested fix:** <one line, or "none — needs a human">
```

`<pattern-class>` is the harvest's label (`doc-wrong`, `documented-command-fails`,
`classifier-gap`, `skill-should`, `update-the-skill`, `skill-bug`); `<dream_date>` is the
pass's `DREAM_DATE`; `<skill-name>` is the skill the evidence names, or `unknown`.

### 7. Report, then advance the watermark

Write the report at **exactly the `REPORT=` path the runner handed you** —
`.claude/loops/dreams/<date>-<run_id>.md`. One file per pass; never reuse or
overwrite another pass's file.

**If this pass had nothing to land, the report must SAY SO in words** — a `**Status:**` of
`empty-window` / `paused` / `brain-unreachable`, or the phrase "nothing to consolidate".
`dream.sh` asserts after the run that the pass either wrote ≥1 ledger row or said this, and
exits 21 otherwise. An honest nothing is a pass; a silent nothing is a failure.

```markdown
# Dream — <date> · <run_id>

**Window:** <watermark_from> → <watermark_to> · **Sessions:** N · **Tickets:** N
**Status:** consolidated | empty-window | paused | brain-unreachable | embedder-down

## Retry pass (ran before consolidation)
- outbox: N queued, N replayed, N still queued
- reembed: N re-embedded, N still pending · near-duplicates surfaced (report only): …

## Written
| Store | Rows | Merged from | Notes |
| app_map | 3 | 11 failures / 6 sessions | … |
…

## Proposed (awaiting a human bless — human-explore)
- expectations: N  (never blessed here)

## Declined (and why)
- <candidate> — no-evidence / already_in_brain / over-consolidation risk

## Skill follow-ups queued
- <pattern> — <one line> → skill-followups.md

## Honest gaps
- <what this pass could NOT consolidate, and why>
```

Then, **only if the pass was clean** (pause false, no `brain-unreachable`, nothing silently
dropped):

```bash
kb state --key dream.watermark --set '{"ts":"<watermark_to from the brief>","by":"qa-dream"}'
```
Use the brief's `watermark_to` — never wall clock, and never further than the ground this
pass actually covered. It is derived two ways, and the brief's `catch_up_needed` tells you
which one you got:

| Pass | `watermark_to` is | Why |
|---|---|---|
| **uncapped** (`sessions_remaining == 0`) | the newest record actually *consumed* | a line appended during the scan carries a later stamp, so it can't be skipped |
| **capped** (`sessions_remaining > 0`) | the newest **scanned file's mtime**, clamped below the oldest *deferred* file's mtime | every record in a scanned file was read — including files that yielded nothing — so the next pass moves on to the backlog instead of re-reading this batch forever |

A file's mtime is the newest instant it can contain and is `stat`ed *before* the read, so
anything appended mid-scan is stamped later and survives to the next pass. Never substitute
wall clock or the newest deferred file for either value: that is precisely the failure the
rule exists to prevent — a capped run advancing past sessions it never opened, which
transcript rotation then makes unrecoverable.

On any failure class, leave the watermark alone: the next pass re-covers the window, and
every write is idempotent-safe (app_map/flakiness upsert, a duplicate fix_lesson bumps
`occurrences` — by hash now, so it holds with the embedder down too).

## Bounds & stop conditions

- **R9 pause** — checked first, fail closed (step 1). `dream.sh` checks it *too*, before it
  spends a token, and skips the whole pass on anything but an explicit `paused:false`.
- **Brief size** — `--max-bytes` (default 180 KB). Over budget → the brief says
  `budget_exceeded`; narrow the window rather than dream on a truncated picture.
- **Session cap** — `--max-sessions` (default 40). A capped run is a **partial catch-up, not a
  skip**: the brief reports `sessions_remaining` / `catch_up_needed`, and `dream.sh --catch-up`
  re-runs (bounded by `--max-passes`, default 3) until the backlog drains.
- **Single instance** — `dream.sh` holds an atomic lock (`.claude/loops/dream.lock/`)
  for the whole pass, so a hand-run dream and the scheduled one can never consolidate the same
  window twice. A second invocation exits 0 with "already running".
- **Wall clock** — the runner's watchdog TERMs the model call at `--timeout` (default 1800s),
  KILLs it `--grace` (15s) later, and exits 22. A killed pass leaves the watermark alone.
- **Cost** — one harvest + one consolidation pass per window (per catch-up pass). No retry loop
  inside the dream: a failed pass leaves the watermark alone and the *next* scheduled run
  retries. `dream.sh` pins `--model` and passes `--max-budget-usd` from the R5 number in
  `.claude/loops/loop-limits.json`, so the cap the job uses is the decided one.
- **Escalation** — the dream has no autonomous escalation path and needs none: the
  `skill-followups.md` queue and the proposed-expectation queue *are* the warm hand-off, and the dated report carries the
  full pass memory a human needs to pick it up cold.

## Knowledge brain (qa-kb)

**Reads** `kb pause`, `kb state`, `kb receipt`, `kb search` (all four stores).
**Writes** `app_map`, `fix_lessons`, `flakiness`, `metrics` autonomously; **proposes**
`expectations`. **Repairs** via `kb outbox --replay` + `kb reembed`. Never merges the stores,
never blesses, never runs `kb decay --apply` or `kb forget` (a human owns pruning, R1).

## Running it

```bash
# harvest + print the brief. No lock, no claude call, no brain writes, no log. Safe any
# time — including alongside a real run.
bash .claude/skills/qa-dream/scripts/dream.sh --since 7d --dry-run

# the real pass (reads the watermark from the brain, invokes this skill headlessly)
bash .claude/skills/qa-dream/scripts/dream.sh --since 7d

# drain a session-cap backlog without hand-holding (up to 3 passes)
bash .claude/skills/qa-dream/scripts/dream.sh --since 7d --catch-up
```

### Flags the runner adds for unattended safety

| Flag | Default | What it buys |
|---|---|---|
| *(always on)* | — | **Single-instance lock**: atomic `mkdir .claude/loops/dream.lock/` with the PID inside. A second invocation exits **0** with "already running since …". A lock whose PID is dead (or was recycled by a non-`dream.sh` process) is reclaimed and logged. Released by a `trap` on `EXIT INT TERM`, so a Ctrl-C or a scheduler stop never strands it. |
| *(always on)* | — | **Brain-health gate**: `kb pause --status` must return `ok:true` **before** the model call (exit 20 otherwise), and `paused:false` to proceed at all. A silent success that writes nothing is the failure mode this removes. |
| *(always on)* | — | **Post-run assertion**: `kb receipt --ticket qa-dream --run-id <this run> --since 1d` must show ≥1 row, or the report must say it had nothing to consolidate. Exit 21 otherwise. `LOOP_RUN_ID`/`LOOP_TICKET` are exported for the child so every `kb` write is ledgered against this run — **do not pass `--run-id` yourself**. |
| `--timeout S` | `1800` | Watchdog: run the model call in the background, poll, `TERM` at S seconds, `KILL` after the grace, report `timed_out:true`, exit 22. (A portable background-and-poll — it needs no `timeout`/`flock` binary — and it signals the whole process **group**.) |
| `--grace S` | `15` | Seconds between the `TERM` and the `KILL`. |
| `--model M` | `$LOOP_DREAM_MODEL`, else `models.strong` from stack.config | Pinned so a `settings.json` change cannot silently alter what a nightly run costs. The dream is a judgment pass — do not downgrade it to save tokens. |
| `--budget-usd N` | `r5_budget.per_loop_usd_cap` from the `limits_file` in stack.config (`loop-limits.json`) | Passed as `--max-budget-usd`. `0` omits the flag. |
| `--log PATH` | `.claude/loops/dream.log` | One `event=start` + one `event=end` line per pass: `rc`, `duration_s`, `rows_written`, `timed_out`, `report`, `sessions_remaining`. `grep 'event=end'` is the run history. |
| `--catch-up` / `--max-passes N` | off / `3` | Re-run while the brief says `catch_up_needed`, so a capped window drains. **Stops early** if the brief carries a `catch-up cannot advance:` warning — the watermark cannot clear the oldest transcript that pass scanned (cap of 1, or a whole batch sharing one second), so another pass would re-read the identical batch and drain nothing. Logs `event=catch_up_stalled`; the fix is a larger `--max-sessions`, not more passes. |
| `--projects-root P` · `--repo R` · `--tasks-dir D` · `--now TS` | harvest's defaults | Forwarded verbatim to `harvest.py`: aim the pass at another checkout or projects dir, or pin the clock so the brief is byte-reproducible. `--now` + a fixture is how the suites drive a real `dream.sh` without reading your live transcripts. |
| `--ticket T` | `qa-dream` | The ledger `ticket` the run's writes are tagged with (what the assertion queries). |
| `--no-assert` | off | Skip the post-run assertion. `--no-brain` implies it. |
| `--no-strict-mcp` | off | By default the child runs `--strict-mcp-config` + `--no-chrome`: **no MCP servers, no browser.** The dream needs neither. |

**Tool restriction.** The child gets `--disallowedTools` for every human-blocking or
out-of-scope tool — `AskUserQuestion` first (a headless run has no human, so an ask is a
deadlock, not a question), plus `WebFetch`/`WebSearch`, the messaging/notification tools, the
Cron tools (the dream must never reschedule itself) and the worktree tools (it never touches
the app tree). By default the child also runs `--strict-mcp-config`, so no MCP server loads;
if you pass `--no-strict-mcp`, add every write-capable MCP tool your stack loads (the
vcs_host / ci_cd / issue_tracker servers) to `$LOOP_DREAM_DISALLOWED_TOOLS`. `Bash`, `Read`,
`Write`, `Edit`, `Glob`, `Grep` are deliberately left alone — the dream needs all of them.

**Exit codes.** `0` success · dry-run · already-running · paused. `2` usage · `3` no real
`claude` · `4` harvest failed · `5` bad repo path · **`20` brain unreachable** · **`21` wrote
nothing and didn't say why** · **`22` watchdog timeout** · `23` lock error · anything else is
`claude`'s own code. A non-zero pass never advances the watermark, so the next run re-covers
the window.

`dream.sh` resolves the `claude` binary itself (`$LOOP_CLAUDE_BIN` → `runner.claude_bin` from
stack.config → `command -v claude`), requires a real executable (a shell alias or function
does not survive into a script), and fails loudly with exit 3 if none exists. It runs the
child with `CLAUDE_CONFIG_DIR` set to `runner.config_dir` from stack.config (else the
inherited `$CLAUDE_CONFIG_DIR`, else `~/.claude`) — the same directory `harvest.py` reads the
transcripts from.

### Scheduling

`dream.sh` is one command line, so **any scheduler that can run one command line** can own
it — cron, systemd timers, a CI schedule, launchd. The definitions are **written but
deliberately not installed**, under `.claude/loops/scheduler/`:
`scheduler/JOB-CONTRACT.md` carries the job contract (exit codes, lock, env, logs);
`scheduler/cron.example` is the portable entry; `com.loops.qa-dream.plist.template` is the
launchd reference, rendered by `install.sh`. A systemd timer is a straight port of the cron
line. Where the host sleeps (a laptop), prefer a trigger that fires the missed slot on the
next wake (launchd `StartCalendarInterval`, systemd `Persistent=true`) — a plain interval or
`cron` silently skips a sleep window; the watermark makes the late run a complete one.
**Arming it is the operator's call**: `install.sh` refuses to install non-interactively
without `--yes`, and nothing runs at load. Read `JOB-CONTRACT.md` for the scheduler PATH trap:
any CLI the brain's auth mode shells out to (a cloud token helper under `token_command`) must be on the job's PATH, or the auth failure is *silent*. The watermark makes
overlapping windows a no-op and the lock makes concurrent ones impossible, so a missed night
self-heals; change the cadence in the scheduler definition, not here.

## Files

- `scripts/harvest.py` — the deterministic zero-LLM scanner (transcripts + artifacts + brain).
- `scripts/dream.sh` — the headless runner: lock, brain gate, watchdog, assertion, log
  (`--dry-run` for harvest-only).
- `references/harvest-brief.md` — the brief's shape, the caps, and what each candidate means.
- `.claude/loops/dreams/` — dated reports, one per pass (tracked).
- `.claude/loops/dream.log` — the run log (one line per event).
- `.claude/loops/scheduler/` — the (uninstalled) scheduler definitions: `JOB-CONTRACT.md`,
  `cron.example`, `com.loops.qa-dream.plist.template` + `install.sh`.
- `.claude/loops/skill-followups.md` — the run-discovered skill-defect queue (tracked;
  created by the first dream that has something to queue).

## Key principles

1. **Nothing valuable is lost.** A write that was skipped, blocked, or never designed still
   lands — later, from the evidence, with provenance.
2. **The dream is the retry loop.** Outbox replay and re-embed run *before* consolidation.
3. **Compress and merge, never pile.** Three sessions, one trap, one lesson with a count.
4. **Propose, never bless.** Expectations are judgment; the dream is inference.
5. **Cite or decline.** No evidence, no lesson.
6. **Traceable and forgettable.** `--source qa-dream` + dream metadata on every row.
7. **Honest nothing.** An empty window gets a report saying so — never an invented lesson.
