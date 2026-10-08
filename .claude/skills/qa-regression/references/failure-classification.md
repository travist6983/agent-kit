# qa-regression — failure classification (the decision that drives the loop) + R7 quarantine

A red regression test is not one thing. The loop's next move depends entirely on **why** it's red —
that's principle 3 (classify before adapting). Misclassifying wastes a whole retry cycle. Classify
every failure into exactly one of these, in this order:

## 0. Environment / tooling failure (rule out BEFORE everything — broken tooling is not a bug)
**Tell:** the reds are wholesale and mechanical — the test runner fails to start (missing
dependencies, a missing toolchain or SDK, an install/restore failure, a config the runner can't load),
or every test fails with the same infrastructure error before reaching its own assertions (module
resolution, a missing test database, a port already in use). A run that produced **no report file**
where `test_report.glob` says one should be belongs here too, until shown otherwise.
**Check:** does the runner execute a known-trivial test in the same project? If not, nothing that
follows is evidence about the fix.
**Do:** `status: blocked-environment`, `next_action: escalate` — the recipe re-provisions
(`infra-worktree` owns the sandbox through the `sandbox` adapter) or hands to a human. Do **NOT**
write flakiness data points for these reds, and do **NOT** call it `unrelated-break` (it isn't blast
radius).
**Verdict contribution:** terminal — no other class is evaluable this run.

## 1. Flaky (rule it out FIRST among real reds — don't let a known flake fail the gate)
**Tell:** the test passes on rerun, or it's a test `qa-kb` already tracks with a high flake rate /
`quarantined = true`.
**Check:** rerun the failed test(s) up to 3×. If it flips pass↔fail, it's flaky.
**Do:** `kb write flakiness --spec <test> --area <area> --failed` (record the data point — this is the
same per-test write step 4 makes for every executed test, not an extra one); if its `flake_rate`
reaches the R7 threshold — `flake_rate_threshold` over `min_runs` recorded runs, both from
`r7_flake_quarantine` in `.claude/loops/loop-limits.json` — `kb quarantine flakiness --spec <test> --on`
so it stops blocking the gate — and surface the quarantine in the report (never silently drop a test).
**Verdict contribution:** a confirmed flake is NOT a gate failure; exclude it and re-evaluate.

## 2. True same-area regression → back to `dev-fix`
**Tell:** a test **covering the same area as the fix** fails on an **assertion** (a value/behavior is
wrong) — the fix changed behavior it shouldn't have, within its own area.
**Do:** classify `same-area-regression`, feed the failing assertion (expected vs actual) back as new
context. The recipe returns to `dev-fix` to refine — this is a real regression the fix caused.

## 3. Unrelated-area break → ESCALATE immediately (blast-radius signal)
**Tell:** a test in a **different area** than the fix fails. The fix reached beyond its area — almost
always a change under the target repo's `conventions.shared_core_globs` (shared components, shared
services, cross-cutting models) whose blast radius hit other consumers (R6). `resolve_projects.py`
lists those paths as `shared_core_hits`, so you usually know the culprit before the suite finishes.
This is the most dangerous class.
**Do:** classify `unrelated-break`, high severity. The recipe escalates **immediately, no retry** — an
unrelated break means the fix's blast radius is wrong, and retrying the same fix won't help. Name the
broken area + test so the human sees the blast radius.

## Order matters
Rule out **environment** first (broken tooling explains every red at once — re-fix/escalate on it and
you burn a cycle on a non-bug), then **flaky** (so you don't escalate a flake), then **same-area**
(re-fix), then **unrelated** (escalate). If multiple tests fail with different classes, report each —
and the worst class wins the gate verdict (`unrelated-break` > `same-area` > `flaky` > green;
`blocked-environment` preempts them all).

## Selecting the area tests (not only the area)
Run the **area-relevant** unit tests first — speed + signal. Derive the area from the fix's changed
files / the `brief.json` surface / `app_map` area tags, and recognize test files by
`conventions.test_path_regex`. If that selection resolves to **zero** test files, the status is
`blocked-slice-empty` (never green, never ship-ok). Then always run the **full** unit suite of every
changed project (`projects[].unit_test`, else `stack.unit_test_command`) — that's what catches the
`unrelated-break` class (an area-only run can't see blast radius). `area_slice_run` lists the files
the report proves executed, not the files you meant to run.
