# The dream brief — what `harvest.py` emits, and how to read it

`scripts/harvest.py` is the deterministic, zero-LLM half of `qa-dream`. It does all the
scanning, truncating, capping and cross-session merging so the agent never touches a raw
transcript. Read this before consolidating: it tells you what each list *is*, and — just as
important — what the harvest already ruled out.

## Invocation

```
python3 harvest.py --since 7d                    # or --watermark <ISO> (what the skill passes)
                   [--project=-home-me-src-my-app]   # slugs start with '-', so use the =form
                   [--all-projects]
                   [--repo /path] [--tasks-dir /path]
                   [--max-sessions 40] [--max-bytes 180000]
                   [--no-subagents] [--no-brain]
                   [--now <ISO>]                 # pin the clock → byte-reproducible output
                   [--compact] [--out brief.json]
```

With `--out`, the brief goes to the file and a one-line summary to stdout. Without it, the
brief itself goes to stdout. Exit 0 always means a usable brief was produced.

**Defaults, when nothing is passed:** the repo roots are this repo plus the target app
checkout named by `app.repo_dir` in stack.config; the project slugs are every
`<config_dir>/projects/<slug>` that maps to one of them (`<config_dir>` = `runner.config_dir`,
else `$CLAUDE_CONFIG_DIR`, else `~/.claude`), and a session is kept only if its recorded `cwd`
is genuinely under a repo root (so a sibling checkout that shares the path prefix doesn't leak
in on a prefix match). `--project` / `--all-projects` skip that cwd filter — you asked for
those slugs explicitly.

The run-artifact scan defaults to **each repo root's evidence dir plus every live git
worktree's** — `git -C <repo> worktree list --porcelain`, one `<worktree>/<evidence.root>` per
entry (`runs` by default). That is not a nicety: **the loops run inside sandboxes provisioned
by the `sandbox` adapter**, so a scan of the repo roots alone reports `tickets_seen: 0` for
windows in which real loops ran, and `fix_lessons` never fills. The rules:

- **deduped by `os.path.realpath`** — `git worktree list` reports the MAIN worktree too (the
  repo root, a second time) and prints resolved paths, so on macOS `/tmp` and `/var` come
  back as `/private/…`. A string compare would scan the same directory twice;
- **sorted** — git makes no ordering promise and the brief is byte-reproducible;
- **degrade, never crash** — git missing, not a repo, a non-zero exit, or a worktree that has
  been deleted since it was listed: skip that source and carry on with the repo roots;
- **only an existing evidence dir is scanned**, and nothing is ever created;
- `--tasks-dir` **overrides the whole default** (repo roots and worktrees both), as before.

What was actually scanned is in the brief: `artifact_sources[]` (absolute, deduped, sorted)
and `artifact_sources_skipped[]` (`{path, reason}` — `no_runs_dir`, `worktree_missing`,
`git_worktree_list_failed`, `not_a_directory`). Read them before concluding a window was
empty: a `tickets_seen: 0` that reads as "no tickets" when it means "did not look" is the
failure these two lists exist to make visible.

## What it reads

1. **Session transcripts** — `<config_dir>/projects/<slug>/*.jsonl`, plus the per-session
   subagent/workflow transcripts under `<slug>/<sessionId>/**/*.jsonl`. Subagent findings are
   folded into their parent session (`subagent_recaps`, plus their failures/kb-calls/signals),
   so `sessions_scanned` stays a count of real sessions.
2. **Run artifacts** — `<repo>/<evidence.root>/**` **and `<worktree>/<evidence.root>/**`**
   (depth ≤ 3, see the default above): `intake/brief/repro/fix/verify/judge/regression/review/
   pre-pr/ship/results/escalation.json`, the older `fix-metadata.json` /
   `task-metadata.json`, and `pr-followups.md` / `learnings.md` / `decisions.md` /
   `FINAL-REPORT.md` — plus **`pr-body*.md` and `plan.md` as bounded prose evidence**
   (below). A directory whose only artifact is prose still counts as a run directory, and a
   ticket's `source_dir` says which evidence root it came from.
3. **Brain state** — `kb receipt --ticket <KEY>` per ticket, READ-ONLY, to set
   `already_in_brain`.

## What it extracts (and what it deliberately drops)

| Kept | Dropped |
|---|---|
| the human's own turns (`origin.kind == "human"`) | queued/tool-result `user` lines — those are not human input |
| the text half of a human turn | `image` blocks (base64 never enters the brief) |
| Bash commands whose result actually failed | `thinking` blocks and their signatures |
| the recovery command that worked after a failure | whole tool outputs, file contents, diffs |
| `kb` invocations + whether they succeeded | everything on a line type other than user/assistant/ai-title/summary |
| file paths edited (Edit/Write/MultiEdit/NotebookEdit) | any single line over 2 MB |
| the last assistant message of the session (the recap) | anything outside the window |

**Failure detection is deliberately strict.** A tool result counts as failed only on the
harness's own `is_error` flag, an `Exit code <nonzero>` preamble, or a `<tool_use_error>`
envelope. A looser "contains the word error" test false-positives on successful output that
merely *mentions* an error — and a brief built on false failures teaches the brain lies.

## Caps

Reported verbatim in the brief's `caps` object, so a reader can always see what was bounded.
Per session: 8 human turns @200 chars, 6 failed commands @160+160, 8 kb calls @160, 20 file
paths @90, 6 skill signals @200, 8 subagent recaps @200, 1 recap @320. Per artifact field:
400 chars, 8 list items. Per ticket: 2 prose files @2000 chars.

On top of that a **global byte budget** (`--max-bytes`, default 180 000). If the brief is
over, it trims the lowest-value session evidence in a fixed order (files → kb calls → recap →
human turns → subagent recaps → failed commands → …), then thins the prose evidence (600 then
200 chars per file — never to zero, and `dropped.prose_thinned` says it happened), then drops
whole sessions oldest-first, then thins `env_lessons` evidence to one example each.
**`candidates` and `tickets` are never dropped** — they are the product. If it still doesn't
fit, `budget_exceeded: true` and a warning say so plainly: narrow the window rather than
dream on a truncated picture.

## Determinism

Given a fixed window and unchanged inputs, two runs emit byte-identical output — file lists,
session order, the discovered `artifact_sources` (git's worktree order is not guaranteed),
the prose files picked out of a directory listing, and every candidate list are explicitly
sorted, and nothing iterates an unordered set. Pin the window with `--now` (or `--watermark`) to make that exact; with a
free-running clock only `window.now` and `watermark_from` move.

`watermark_to` is **data-derived**: the newest record the scan actually *consumed*, clamped to
the window end — never wall clock. A session still being appended to can carry a line stamped
after our clock; advancing past it would lose that line forever. On an empty window
`watermark_to == watermark_from`, so the watermark never advances over unseen ground.

## Output shape

```
schema, watermark_from, watermark_to, window{since, now}, repos[], projects[],
artifact_sources[], artifact_sources_skipped[{path, reason}],
sessions_scanned, sessions_available, sessions_empty_in_window, sessions_skipped_by_cap,
tickets_seen, candidate_counts{}, caps{}, parse_stats{malformed, huge_lines, unreadable},
filtered{env_lessons_filtered{}, env_lessons_filtered_total,
         prose_chars_dropped{}, prose_chars_dropped_total,
         prose_boilerplate_lines_dropped_total, prose_files_skipped_total}, warnings[],
candidates { app_map[], fix_lessons[], expectations[], flakiness[], env_lessons[],
             skill_followups[], metrics[] },
tickets[], sessions[], truncated, budget_exceeded, dropped{}
```

### The candidate lists

| List | Built from | What to do with it |
|---|---|---|
| `env_lessons` | failed commands, **bucketed by failure signature across all sessions** (`<command>:<error-class>`), each with `occurrences`, `distinct_sessions`, up to 3 examples carrying `recovered_by`. **Pre-filtered** — see below | The highest-value list. Write **one** `app_map` row of `--kind lesson` per bucket, carrying the count — never one per session. A bucket with a `recovered_by` that differs from the failed command is a real "do X, not Y" lesson. |
| `skill_followups` | transcript text matching the pattern set (`doc-wrong`, `documented-command-fails`, `classifier-gap`, `skill-should`, `update-the-skill`, `skill-bug`), deduped, with `voice: human\|assistant`. **Assistant turns may raise only `doc-wrong` and `documented-command-fails`** — a looser rule matches an agent *discussing* skill defects (including its own design notes) and produces a self-referential followup built from the agent's own planning text | Append to `skill-followups.md`. **A human-voiced signal outranks an assistant-voiced one** — a correction from the user is ground truth about the skill. Never edit the skill. |
| `fix_lessons` | tickets with a `root_cause` **and** a passing `verify` | `kb write fix_lessons` — symptom → root cause → fix shape. Skip when `already_in_brain: true`. |
| `expectations` | `brief.json.expected_verbatim` / `fix-metadata.expected_result` | `kb propose expectation` — **PROPOSE ONLY.** `propose_only: true` is on every entry as a reminder. |
| `app_map` | `repro.json.app_map_writes[]` | Selectors/routes a run recorded. Cross-check against `already_in_brain` before rewriting; prefer `kb confirm app_map --id` when the row exists and only needs freshening. |

### The `env_lessons` filter — why the list is short

A brief full of junk produces a brain full of junk, so `env_lessons` carries only failures with
a **named diagnostic class**: `command-not-found`, `no-such-file`, `permission-denied`,
`module-not-found`, `connection-refused`, `input-validation`, `harness-blocked`, `git-fatal`.
Everything else is dropped and **counted** in `filtered.env_lessons_filtered` — a filter whose
rejects are invisible is indistinguishable from one that is quietly eating real signal.

What is deliberately dropped, and why:

| Dropped | Why it is not a lesson |
|---|---|
| `exit-143` / any code > 128 | SIGTERM/SIGINT — a 10-minute timeout or a Ctrl-C. An interruption, not a property of the environment. |
| a bare `exit-1` | For `grep`/`ls`/`find`/`git` it literally means "no match"; for anything else it is an unexplained one-off. |
| `:error` with no exit code and no class | We cannot say what went wrong, so we cannot write a lesson anyone could act on. |
| `python-traceback` | A traceback from inline code written seconds earlier (a `python3 - <<PY` heredoc with a `KeyError`) is a bug in that code, not an environment lesson. An environmental traceback (`ModuleNotFoundError`) classifies as `module-not-found` first and survives. |
| a tool call the human declined | A person exercising judgment. Harvesting it teaches the brain that its own operator is a broken dependency. |

`recovered` is **not** part of the bar. It fires on nearly every failure by construction (the
heuristic pairs the next differing Bash call), so it separates nothing — but
it is still reported, because the recovery command is the most useful thing in the example.

`command_head` is front-only and will answer `?` for a multi-statement script. Guessing a head
out of a compound command yields bucket names that are really a loop variable or a mid-string
fragment posing as a tool. When the head is unknown the **error class carries the meaning**,
and `?:no-such-file` is a more honest bucket than a confidently wrong one.
| `flakiness` | `regression.json.failed_specs[]` classed `flaky`/`environment`, plus `flaky_quarantined[]` | `kb write flakiness` data points. Quarantine decisions stay with `qa-regression`'s R7 threshold — the dream records, it doesn't quarantine. |
| `metrics` | counts over the window itself | `kb metric` — fuel for the R8 meta-monitor. |

### `prose_evidence` — candidate EVIDENCE, never a candidate

A feature loop's root cause and fix shape are written up for a human in `pr-body*.md` and the
`plan.md` it worked from, and **nowhere else** — no `fix.json`, no `verify.json`. That is
exactly `fix_lessons` material, so each ticket carries up to two of those files under
`prose_evidence[]` (`file / chars / chars_dropped / boilerplate_lines_dropped / text`).

It is **evidence, not a candidate**. The harvest never synthesises a `fix_lesson` out of
prose — it stays deterministic and zero-LLM, and a ticket with prose but no verified fix
produces no candidate at all. Read it, judge it, and write the lesson yourself if it earns
one; cite the file in the lesson's evidence.

Bounded hard, because this is the one input that could turn the brief back into a transcript
pile:

| Bound | Rule |
|---|---|
| per file | 2000 chars (`caps.prose_file_chars`), plain truncation with a trailing `…` |
| per ticket | at most 2 files (`caps.prose_files`): ONE pr-body + `plan.md` |
| which pr-body | the **final** variant — `pr-body.final.md` > `pr-body.md` > `pr-body.initial.md` > any other `pr-body*.md`, alphabetically. Drafts of one document are not three sources |
| stripped first | markdown image / badge lines, `<img>`, bare URL lines, `🤖 Generated with`, `Co-Authored-By:`, `Claude-Session:`, and any `data:…;base64,` blob — so the cap is spent on the argument, not the signature |

Everything the bounds refused is **counted**, the same discipline `env_lessons_filtered` runs
on: `filtered.prose_chars_dropped{"<KEY>/<file>": n}`, `prose_chars_dropped_total`,
`prose_boilerplate_lines_dropped_total`, `prose_files_skipped_total` (drafts not taken). A
silent truncation reads as "nothing there" — and a PR body is usually far longer than the cap,
so the dropped count routinely exceeds the kept one. The counts matter.

### `already_in_brain` is three-valued

- `true` — a run already wrote this store for this ticket. **Skip it.**
- `false` — the brain has no write for it. **Yours to write.**
- `null` — **unknown**: the lookup was unavailable (`brain.lookup` says why —
  `verb_unsupported`, `timeout`, a connection error, or `skipped` under `--no-brain`).
  `kb search` before writing, and never treat unknown as absent.

### Per-ticket record

`key, area, dir, source_dir, last_artifact_at, artifacts[], recipe, risk_class, statuses{},
symptom, expected, root_cause, is_root_cause, masking_suspected, files_changed[],
design_decisions[], failure_classes[], failed_specs[], quarantined[], app_map_writes[],
warnings[], blockers[], review_findings[], followups[], task_description, prose_evidence[],
prose_files_skipped, brain{lookup, stores_written[]}`.

`source_dir` is the evidence root the ticket was found under — a repo root, or a sandbox
worktree. It joins back to `artifact_sources[]`, shortened against the repo roots the same
way `dir` is.

### Per-session record

`session_id, project, cwd, title, started_at, ended_at, lines_read, human_turns[],
failed_commands[] (command / error / recovered_by / recovered), kb_calls[] (verb / store /
ok / error), files_edited[], skill_signals[], subagent_recaps[], recap`.

A `kb_calls` entry with `ok: false` is a **lost write** — that's precisely what the outbox
replay and re-embed in step 2 of the skill exist to recover. Read them together.
