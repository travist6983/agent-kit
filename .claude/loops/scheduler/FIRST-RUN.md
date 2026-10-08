# First run — do this by hand, watch it

> This is the hand-run gate for **`com.loops.qa-dream`**. The hourly PR-review agent has its own,
> with different prerequisites: **`FIRST-RUN-pr-review.md`**. Both agents are documented in
> `README.md`; what every runner promises is in `JOB-CONTRACT.md`.

This is the sequence to run before arming the timer on a fresh machine, after a long gap, or
whenever `dream.sh` changes. Confirm where you actually stand before assuming any of it:

```bash
python3 .claude/skills/qa-kb/scripts/kb.py state --key dream.watermark   # found:false = cold start
ls -t .claude/loops/dreams/ | head
```

**Do not run `install.sh` until two hand runs come back clean.** Arming an unattended job that has
never successfully completed once is how you wake up to a brain full of junk you then have to undo.

Everything below runs from the repo root:

```bash
cd <this checkout>
```

---

## 1. Dry run — no lock, no model call, no writes

```bash
bash .claude/skills/qa-dream/scripts/dream.sh --dry-run --since 2d --max-sessions 5
```

Expect: `no brain watermark — using --since 2d`, then one JSON brief on stdout and the harvest
summary on stderr. Nothing is written anywhere.

Read the brief before you spend a token on it. Check:
- `candidates` — are the `env_lessons` / `fix_lessons` real, or shell noise? If the brief is junk,
  the dream will write junk. Narrow the window instead of dreaming on it.
- `sessions_remaining` / `catch_up_needed` — how much backlog there is.
- `watermark_to` — sane, and **not** wall clock.

## 2. Confirm the brain answers

```bash
python3 .claude/skills/qa-kb/scripts/kb.py pause --status
```

Must print `"ok": true` and `"paused": false`. If `ok` is false, stop — `dream.sh` will exit 20
rather than run, which is the point. Fix the brain first: usually an expired or missing credential
for the configured auth mode (kb.config `auth.mode`), or the secret env file not being where
`kb.config.json` says. `docs/brain-setup.md` is the setup page.

## 3. Record the baseline you will compare against

```bash
python3 .claude/skills/qa-kb/scripts/kb.py receipt --ticket qa-dream --since 30d
```

Should be empty on a fresh brain. That is your before.

## 4. The first real run — small window, short leash

```bash
bash .claude/skills/qa-dream/scripts/dream.sh --since 2d --max-sessions 5 --timeout 900
```

Watch it. It prints the `run_id` (`dream-<ts>-<pid>-p1`) — **copy it**, you need it for step 6.

Small deliberately: 2 days and 5 sessions, and a 15-minute watchdog instead of 30. First run is
about proving the machinery, not draining the backlog. Do **not** pass `--catch-up` yet.

If your shell exports `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`, this run bills as metered
API usage — `dream.sh` has no guard for that (the scheduled job is safe because its plist declares
its own environment). Check first:

```bash
env | grep -c '^ANTHROPIC_\(API_KEY\|AUTH_TOKEN\)='   # 0 is what you want
```

## 5. What to check when it comes back

| Check | Command | What good looks like |
|---|---|---|
| Exit code | `echo $?` | `0` |
| Report | `ls .claude/loops/dreams/` | one `<date>-dream-…-p1.md` |
| Read it | `cat .claude/loops/dreams/<file>` | cites real evidence; an honest "nothing to consolidate" is a fine outcome |
| Log | `grep event=end .claude/loops/dream.log` | `rc=0 timed_out=false`, `rows_written` > 0 |
| What it actually wrote | `python3 .claude/skills/qa-kb/scripts/kb.py receipt --ticket qa-dream --run-id <run_id> --since 1d` | matches the report — the ledger is the truth, the report is the claim |
| Watermark advanced | `python3 .claude/skills/qa-kb/scripts/kb.py state --key dream.watermark` | now set, equal to the brief's `watermark_to` |
| Expectations only proposed | see the one-liner below | every expectations verb is `propose` — the dream must never bless |

The bless check — every expectations write in the run must be a `propose`:

```bash
python3 .claude/skills/qa-kb/scripts/kb.py receipt --ticket qa-dream --run-id <run_id> --since 1d \
| python3 -c 'import sys,json; w=json.load(sys.stdin).get("writes_by_store",{}).get("expectations",[]); print(sorted({r["verb"] for r in w}) or "no expectations written")'
```

Anything other than `['propose']` is a bug — stop and undo (step 6).

Non-zero exits mean specific things: **20** brain unreachable, **21** it wrote nothing and did not
say why, **22** watchdog timeout, **23** lock, **29** the model API returned a 429 (out of window,
not a fault — wait and re-run). Anything else came from `claude` itself. The full table is in
`JOB-CONTRACT.md`.

## 6. If you don't like what it wrote

```bash
python3 .claude/skills/qa-kb/scripts/kb.py undo --run-id <run_id>          # plan only, changes nothing
python3 .claude/skills/qa-kb/scripts/kb.py undo --run-id <run_id> --yes    # apply
```

Dry run is the default, so the first command is safe. It is re-runnable, it soft-forgets
expectations rather than destroying a bless decision, and it reverses a fix-lesson bump by
decrementing rather than deleting a row that predates the run. Rows it cannot reverse exactly are
reported `skipped_unreversible` instead of being guessed at.

## 7. Second hand run

Repeat step 4 the next day, same flags. Two things to confirm:

- It is **near-empty** — the watermark from run 1 means run 2 has little to do. If run 2 re-harvests
  the same window, the watermark did not stick, and the timer would re-consolidate the same
  material every time it fires.
- The lock works: start a run, and while it is going, run the same command in a second terminal. The
  second must exit 0 with `already running since …` and harvest nothing.

## 8. Only now, arm the timer

```bash
bash .claude/loops/scheduler/install.sh --check   # render + preflight, installs nothing
bash .claude/loops/scheduler/install.sh --yes     # renders the template, bootstraps it
```

The installer arms both agents and takes `--label`; `com.loops.qa-dream` is the default, so these
two commands need no label.

`--check` first, always. It renders the template for this machine, then re-resolves `python3`,
`git`, `bash` and whatever CLI the brain's auth mode shells out to **under the job's PATH**, not
your shell's — `kb.py` swallows a missing-binary error from that CLI, so a wrong PATH is a silent
auth failure in the middle of the night rather than a loud one. It also imports the Postgres
driver under the job's PATH and HOME and runs `kb pause --status` the way the job will.

Three more things `--check` answers, and each of them is a way to arm the wrong job:

- **the plist's own `Label`.** `--label` only picks which *template* to read; `launchctl
  bootstrap` registers the job under the `Label` **inside** it. A mismatch fails the preflight,
  because otherwise every command the installer prints — including `--uninstall` — targets a
  label that does not exist.
- **the armed copy.** Everything else describes the plist *this machine would install*. If
  `~/Library/LaunchAgents` holds a different one, `--check` says so, prints that copy's own
  schedule and command, and (for the PR sweep) whether **it** carries `--live`. What launchd runs
  is the armed copy, not the intention.
- **whether the files are in git.** A launchd job is a permanent reference to a *path*. Arming one
  that points at untracked files means a `git clean -fdx` turns every later run into `/bin/bash` on
  a missing path: exit 127, and **no line at all** in the summary log.

**Read the schedule sentence it prints before you type `YES`, and check it against the
template:**

```bash
grep -A6 StartCalendarInterval .claude/loops/scheduler/com.loops.qa-dream.plist.template
```

`RunAtLoad` is false, so installing does not start a pass. Check the first scheduled run fired:

```bash
grep event=end .claude/loops/dream.log | tail -3
launchctl list | grep com.loops.qa-dream
```

Remove it with `bash .claude/loops/scheduler/install.sh --uninstall`.

---

## Two things to check before the first run

**Is the embedding provider answering?** When it is not (a 429 from the provider, an exhausted
balance or quota), semantic dedupe and `kb search` on expectations / fix_lessons do not work. The
hash guard covers *exact* duplicates with no embedder at all, so retry is safe — but a
**paraphrase** of an existing lesson will still insert a second row, and a more frequent cadence
means more near-duplicates in the human bless queue (R10). Check rather than assume:

```bash
python3 .claude/skills/qa-kb/scripts/kb.py outbox --status     # pending embeds waiting to retry
```

**Are there rows with no hash?** Rows written before the hash guard are not exact-duplicate
guarded. The backfill is optional and safe — it only fills a derived column, changes no text,
deletes nothing, and is idempotent:

```bash
python3 .claude/skills/qa-kb/scripts/migrate_normalize.py --section hashes              # plan
python3 .claude/skills/qa-kb/scripts/migrate_normalize.py --section hashes --apply --yes
```
