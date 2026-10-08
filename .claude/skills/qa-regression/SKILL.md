---
name: qa-regression
description: >-
  The post-fix sanity gate: build + lint against the pre-fix baseline, run the unit tests covering
  the fixed AREA plus the full unit suite of every changed project, then CLASSIFY every red so the
  loop adapts correctly. Trigger for "run the regression", "did the fix break anything", "run the
  area tests", or as the regression step of a defect loop. Commands come from the target repo's
  conventions file (`projects[]`) and stack.config (`stack.*`), never from memory; counts come from
  the runner's own report (`test_report.format`: junit | json). A broken build or a run that
  executed zero tests is a BLOCKING status, never ship-ok. Classification order: environment → flaky
  (R7 quarantine at the loop-limits threshold) → same-area regression (→ re-fix) → unrelated-break
  (→ escalate immediately, blast radius). Emits regression.json per contract. Does NOT fix, ship, or
  own retries.
allowed-tools: Read, Write, Glob, Grep, Bash, Agent, ToolSearch
---

# qa-regression — build, area tests + classify, so the loop adapts correctly

A green replay (qa-verify) proves the bug is fixed; it does **not** prove the fix broke nothing else.
`qa-regression` is that second question. Its real value isn't running tests — it's **classifying a
failure correctly**, because the class decides the loop's next move (re-fix vs escalate). A
misclassified failure burns a whole retry cycle, so the classification (`references/failure-classification.md`)
is the heart of this skill.

## What this block does NOT do (atomic boundary)
It does **not** fix (`dev-fix`), ship (`infra-ship`), run the deep pattern/standards review
(`dev-code-review`), or own the retry counters (the recipe). It builds, runs the tests, classifies,
and returns a verdict. It drives **no browser**: behavior in the running app is `qa-verify`'s job
(the `replay-oracle.sh` from `qa-repro`, driven by the configured `browser.oracle_driver`), and this
gate never writes, runs, or reads browser e2e spec files.

## Where every value comes from (nothing is hard-coded to a stack)
| what | source |
|---|---|
| which commands a changed path implies | `conventions.projects[]` — `{name, path_globs, build, lint, unit_test, test_report}` in the file `stack.conventions_file` names (default `.claude/loops/conventions.json`) |
| the repo-wide fallback for a path no project claims (`other`) | `stack.build_command` / `stack.lint_command` / `stack.unit_test_command` / `stack.test_report` in `.claude/loops/stack.config.json` |
| how to read the runner's counts | `test_report: {format: junit \| json, glob}` — per project, else `stack.test_report` (`references/test-report-formats.md`) |
| which changed paths are tests | `conventions.test_path_regex` |
| the classic unrelated-break tell | `conventions.shared_core_globs` |
| the R7 quarantine threshold | `r7_flake_quarantine.flake_rate_threshold` + `.min_runs` in `.claude/loops/loop-limits.json` (`limits_file`) |
| the merge base | `project.default_branch` (every command below takes `--base` defaulting to it) |
| the evidence root | `evidence.root` (default `runs/`) → `<EVID_ROOT>/<KEY>/` |
| the app checkout the commands run in | `app.repo_dir` (or the sandbox dir the recipe passed) |

`scripts/resolve_projects.py` does the path → project → commands mapping (the same first-match rule
`dev-pre-pr` applies, so the two gates can never disagree); `scripts/parse_test_report.py` turns the
report the runner wrote into counts, a per-test list and the flakiness data points. Any key has a
`LOOPS_CFG__*` env override; a key this skill needs that is missing from the schema is reported, never
invented.

## Inputs / Outputs
**Inputs:** the applied fix (working tree on the bug branch) + the `qa-repro` `brief.json` (the surface
→ the area) + `qa-kb` (`app_map` area tags → which test files; `flakiness` → known flakes/quarantines).
**Output:** `regression.json`, written to **`<EVID_ROOT>/<KEY>/regression.json`** (contract:
`contracts/qa-regression.json` — validate before returning):
```
{ status: green | same-area-regression | unrelated-break | flaky | blocked-build
        | blocked-environment | blocked-slice-empty,
  area, area_slice_run: [...], smoke_slice_run: [...],
  results: {passed, failed, total}, failed_specs: [{spec, class, evidence}],
  flaky_quarantined: [...], baseline: {lint_before, lint_after, baseline_missing?}, next_action }
```
`next_action` ∈ { ship-ok | re-fix | escalate } — what the recipe should do.
`area_slice_run` = the test files that cover the fixed area; `smoke_slice_run` = the broader suite
runs (the full unit suite of each changed project, named `<project>: <unit_test command>`) that catch
blast radius.

**HARD RULE (QA-REGRESSION-C1): a run that executed zero tests is a BLOCKING status — never ship-ok.**
`status: green` / `next_action: ship-ok` require `area_slice_run` non-empty AND `results.total ≥ 1`.
`contracts/validate.py` enforces this mechanically on every `regression.json`. "There are no tests for
this area" is a claim this gate is forbidden to make on its own. The contract makes the shape
`area_slice_run: []`, `total: "n/a"`, `ship-ok` unwritable, because it is exactly the false claim a gate
makes when it stops looking.

## Procedure
### 0. Resolve what changed and what it owes
```bash
# <framework-repo> = the checkout of this framework THIS file was loaded from; <app> = the checkout
# under test (app.repo_dir, or the sandbox dir the recipe passed). --base defaults to project.default_branch.
python3 <framework-repo>/.claude/skills/qa-regression/scripts/resolve_projects.py \
  --cwd <app> [--base "$BASE"] > "$EVID/projects.json"
```
It diffs against the **merge-base** of the base branch and HEAD (plus untracked files) — not the base
tip, or commits that landed on the base since you branched read as deletions — and lists every matched
project with its `build` / `lint` / `unit_test` / `test_report`, the `other` bucket (repo-wide
`stack.*` commands) for unclaimed paths, `shared_core_hits` (paths under `conventions.shared_core_globs`)
and `area_hints` (the changed directories, deepest first). A path list on stdin (`--stdin`) or as
arguments replaces the git diff when the recipe already has one.

### 1. Build/lint baseline gate (the same command set `dev-pre-pr` re-runs)
A regression run is meaningless if it doesn't compile. For each matched project run `build` then
`lint` (skip a `null` command and record it as skipped — skipped is not green), and compare lint to
the **baseline captured before the fix**: lint errors must **never exceed baseline**. The baseline's
producer is **`dev-fix`**: before applying its diff it writes `<EVID_ROOT>/<KEY>/lint-baseline.json`
`{command, errors, warnings, captured_at, head}` — read `lint_before` from that file. If the file is
missing, set `baseline: {lint_before: "missing", baseline_missing: true}` and say so — **never invent
a baseline**; the never-exceed rule is simply unenforceable that run and the report must show it.
```bash
# for each project in projects.json (and `other` when present):
#   <project.build>   &&   <project.lint>          # exactly as configured — these are the CI-parity commands
```
**The configured flags are not optional.** `projects[].build` / `.unit_test` must be the exact commands
CI runs — same configuration, same warnings-as-errors setting — because a looser local build passes
with warnings the CI check promotes to errors: green locally, red in CI, PR check fails. If the
configured command is looser than CI, that is a conventions-file bug to report, not a reason to add
flags from memory. Fix warnings at the source, never suppress them. Build fails (including any warning
the configured flags promote to an error), or lint exceeds baseline → `status: blocked-build`,
`next_action: re-fix`. Stop here.

### 2. Run the area tests, then the cross-area net
Derive the area from the fix's changed files (`area_hints`) / `brief.json` surface / `app_map` area tags,
then pick the unit tests that cover it: files matching `conventions.test_path_regex` under or importing
the changed area. Run them with the project's `unit_test` command, appending the selected test paths as
positional arguments (`<unit_test> <files>`) — the one form the conventions schema supports today. A
runner that does not take positional paths (one that needs a `--filter`-style expression) gets **no
flag from memory**: run the project's full `unit_test` once, exactly as configured, and derive
`area_slice_run` from the report with `--area-regex` — the files that actually executed are the slice,
not the files you intended. Either way, then run the **full** `unit_test` of every changed project (and
`other`'s `stack.unit_test_command` when unclaimed paths changed) — that wider run is what catches
`unrelated-break`; an area-only run is blind to blast radius. (When the full run already served as the
area run, it counts once: do not run it twice.)

**Zero tests is a block, not a pass.** If the area selection resolves to no test files → `status:
blocked-slice-empty`, `next_action: escalate`, and name what you searched in `detail` (the regex, the
directories, the `app_map` tags). If the runner itself cannot start (missing dependencies, a missing
toolchain or SDK, an install/restore failure) → `status: blocked-environment`. Neither is evidence
about the fix.

**Counts matter** (verify-fix): record passed/failed/total from the runner's own report — never "looks
fine", never a number scraped from a terminal summary that may be stale across differently-filtered
runs. Every run writes its report where `test_report.glob` says; parse it:
```bash
# --cwd <app> = the same checkout the test command ran in; the report glob resolves relative to it
# (default app.repo_dir, which is NOT the sandbox dir — pass it every time the recipe provisioned one).
python3 <framework-repo>/.claude/skills/qa-regression/scripts/parse_test_report.py \
  --cwd <app> --project <name> --area-regex '<area path regex>' --area-tag <area> > "$EVID/report-<name>.json"
# exit 0 = tests ran · 1 = ZERO tests executed (blocking) · 2 = no report file (do not trust any count) · 3 = malformed report
```
A runner that produced no report, or a report with zero executed tests, is `blocked-environment` or
`blocked-slice-empty` — decide which from *why*, never `green`. This gate never reads a terminal
summary line at all — only the parsed report — so a runner whose summary line repeats a stale count
across differently-filtered runs cannot mislead it.

### 3. Classify every failure — `references/failure-classification.md`
In order: **environment** (the runner or its dependencies broke mid-run — every test red with the
same infrastructure error → `status: blocked-environment`, nothing else is evaluable; broken tooling
is NOT a blast-radius break) → **flaky** (rerun ≤3×; if it flips, class it flaky and quarantine at
the R7 threshold — `flake_rate_threshold` over `min_runs` from `.claude/loops/loop-limits.json`
— exclude from the verdict; the `flakiness` data points themselves are written in step 4 for *every*
test, not only the ones that flipped) → **same-area regression** (assertion fails in the fixed area →
`next_action: re-fix`, feed the failing assertion back) → **unrelated-area break** (a test in a
*different* area fails → `next_action: escalate`, immediately, no retry — blast-radius signal; a
`shared_core_hits` entry in step 0 is the usual explanation). Worst class wins the verdict.

### 4. Feed the brain (qa-kb) + emit

**Invoking `kb`.** Every `kb <verb>` below is the `qa-kb` CLI (`docs/brain-setup.md` covers putting it
on your PATH and reaching the shared brain). Where `kb` is not on PATH (exit 127), call the script by
its absolute path — never cwd-relative, because a loop's cwd is normally a sandbox checkout — and do
not skip the write:

```bash
KB="${KB_BIN:-<framework-repo>/.claude/skills/qa-kb/scripts/kb.py}"   # <framework-repo> = this checkout
python3 "$KB" <verb> ...        # ≡ the bare `kb <verb> ...` calls below
```

Either route reads `.claude/loops/kb.config.json` and lands on the one shared brain it declares; a
refused write (no DSN, or a loopback host without `KB_ALLOW_UNSHARED=1`) is reported, never silently
dropped.

- `flakiness`: **write one data point for EVERY test that ran — passes included**, `--failed` only on
  the reds, `--area <area>` always. `parse_test_report.py` emits exactly these argument lists as
  `kb_flakiness_args`, one per executed test:
  ```bash
  kb write flakiness --spec "<test file > test name>" --area <area> --source qa-regression [--failed]
  ```
  A rate needs a denominator: recording only the flips (or only the failures) makes `flake_rate` a
  failure log, and the R7 threshold reads as ~1.0 for any test that ever went red once. Then
  `quarantine` a test when its `flake_rate` ≥ `flake_rate_threshold` over ≥ `min_runs` recorded runs
  (the R7 threshold, defined in `.claude/loops/loop-limits.json`) and **surface the quarantine in the
  report** — never silently drop a test. Record the count you wrote as `flakiness_writes`.
  Two boundaries: this step is the **single** place a run's flakiness counters are written (the
  per-class `kb write flakiness` in `references/failure-classification.md` §1 is *this* data point, not
  an extra one — never double-count a test run); and a **`blocked-environment`** run writes **none**,
  because broken tooling is no evidence about any test (§0).
- `app_map`: if the area → test-file mapping you derived isn't in the brain yet, write it as an
  `area_tag` so the next run of this area starts from it.
- Write `regression.json` (through `contracts/emit.py`, which validates first) and return. The recipe
  acts on `next_action`.

## Success criteria & failure categories (declared up front)
**Success:** the build held, the right tests ran, counts recorded from the report, every red classified
into exactly one class, the verdict's `next_action` is correct, R7 quarantines surfaced.
**Failure categories (of the gate itself):**
- `misclassified` — the cardinal sin; sending a flake to `escalate` or a blast-radius break to `re-fix` wastes a cycle.
- `slice-too-narrow` — ran only the fixed area, missed an `unrelated-break` (always run the full suite
  of each changed project). The zero-test extreme is mechanically blocked: `validate.py` refuses a
  green / ship-ok `regression.json` with `total < 1` or an empty `area_slice_run`.
- `flake-counted-as-fail` — failed the gate on a known flake (rule flaky out first).
- `baseline-drift-ignored` — let lint creep above baseline.

## Knowledge brain (qa-kb)
Reads `app_map` (area tags → test selection) and `flakiness` (known flakes/quarantines). Writes
`flakiness` autonomously — **one counter data point per test run, pass or fail** (that's what makes
`flake_rate` a rate) plus the R7 quarantine. Never blesses expectations.

## Key principles
1. **Classification is the product.** The class (`flaky`/`same-area`/`unrelated`) decides the loop's next move.
2. **Zero tests run = blocked, never ship-ok.** A green verdict needs at least one executed test in the area.
3. **Rule out environment, then flaky first**, so you never re-fix broken tooling or escalate a flake.
4. **Always run the full suite of each changed project** — blast radius is invisible to an area-only run.
5. **Counts matter; baseline holds.** Record passed/failed/total from the report; lint never exceeds the pre-fix baseline.
6. **Commands are config, not memory.** Run what `conventions.projects[]` / `stack.*` declare, with CI's flags.
7. **Atomic.** Build + test + classify + return. No fixing or shipping here.
