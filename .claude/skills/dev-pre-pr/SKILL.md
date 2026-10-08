---
name: dev-pre-pr
description: >-
  The last gate before a branch becomes a pull request: turn the loop's scattered evidence into ONE
  machine-readable readiness verdict (ready | warnings | blocked) in pre-pr.json plus a developer-facing
  pre-pr-report.md. Trigger for "pre-pr", "run the pre-PR check", "is this branch ready for a PR", "am I
  clear to open the PR", "readiness check", or as the gate every dev-loop recipe runs between
  dev-code-review and infra-ship. FAILS CLOSED on fix verification: verify.json must be present, fresh
  and `pass` or the verdict is blocked — missing, stale, malformed or failed all block at every tier,
  and a green build never substitutes for it. Runs the DETERMINISTIC conventions scan
  (scripts/standards_check.sh — added lines only, the target repo's ruleset from
  .claude/loops/conventions.json, zero tokens), confirms heuristic findings by reading the file, and
  reconciles regression.json + review.json instead of re-running them — but re-runs the build itself
  when that evidence is missing or stale, because a readiness verdict built on a stale build is a lie.
  Complements, never replaces: qa-regression owns build/lint/test, dev-code-review owns the diff
  judgment. Has no Edit tool — it reports and blocks, it never fixes or ships.
allowed-tools: Read, Write, Glob, Grep, Bash, ToolSearch
---

# dev-pre-pr — one verdict, from evidence that is actually fresh

Everything upstream of this skill produces a *fragment* of readiness: `qa-regression` knows whether it
compiles, `dev-code-review` knows whether the diff is sound, `infra-ship`'s preflight knows whether
shipping is allowed. Nobody answers the question a human asks before opening a PR — **"is this branch
ready?"** — and nobody checks the target repo's mechanical house rules at all. `dev-pre-pr` is that
answer: it consolidates the fragments, fills the two real gaps (the deterministic conventions scan and
the blocker roll-up), and emits a verdict the recipe router can branch on.

Its discipline is **consolidate, don't duplicate**. Re-running a five-minute build that `qa-regression`
already ran is waste; *trusting* that build after three more commits landed is worse. So it reconciles
by freshness, and says in the artifact where every check's answer came from.

## What this block does NOT do (atomic boundary)
It does not fix (`dev-fix`), own the build/lint/unit-test gate (`qa-regression`), judge the
diff for correctness or masking (`dev-code-review`), or commit/push/open the PR (`infra-ship`). It has
**no `Edit` tool** — reporting-only is structurally enforced, the same way `qa-diagnose` is propose-only.

## Where its configuration comes from
Nothing in this skill is hard-coded to a stack. Every value below is read from
`.claude/loops/stack.config.json` (through `lib/adapters/config.py`; any key has an env override
`LOOPS_CFG__<PATH>`) or from the conventions file it points at:

| value | source |
|---|---|
| base branch for the diff | `project.default_branch` (every command below takes `--base`; it defaults to this) |
| ticket key shape | `project.ticket_key_pattern` (expands `{{ticket_key_pattern}}` inside rules) |
| evidence root (`<EVID_ROOT>`) | `evidence.root` (default `runs/`) |
| the rules the scanner applies | `stack.conventions_file` → `.claude/loops/conventions.json`, its `rules` array |
| which commands a changed path implies | `conventions.projects[]` (`name`, `path_globs`, `build`, `lint`, `unit_test`); a path matching no project falls back to `stack.build_command` / `stack.lint_command` / `stack.unit_test_command` |
| the reader-judged rules | `conventions.guidelines_file` (default `CLAUDE.md` of the target repo) |
| the target repo | `app.repo_dir`, or the sandbox the recipe provisioned |

No adapter role is needed: git is used locally, and the brain is reached through `qa-kb`.

## Inputs / Outputs
**Inputs:** the branch + its diff vs the base; and, when running inside a loop, `<EVID_ROOT>/<KEY>/`'s
`regression.json`, `review.json` and **`verify.json`**. `regression.json`/`review.json` are optional —
with neither, the skill degrades to a standalone developer check that runs the validations itself.
**`verify.json` is NOT optional inside a loop** — see the next section.
**Outputs:** `pre-pr.json` at **`<EVID_ROOT>/<KEY>/pre-pr.json`** (contract: `contracts/dev-pre-pr.json`)
`{ status, next_action, branch, checks, sources, violations, blockers, warnings, counts, summary }`,
plus **`pre-pr-report.md`** next to it — the human half, because a JSON blob is not something a
developer reads at the end of the day before opening a PR.

## Procedure

### 1. Gather context (cheap — do this first)
```bash
BASE="$(python3 -c 'import sys; sys.path.insert(0, "."); from lib.adapters.config import load_config; print(load_config().require("project.default_branch"))')"
git branch --show-current
git diff --stat $(git merge-base "$BASE" HEAD)
git diff --name-only $(git merge-base "$BASE" HEAD)
```
Classify the changed paths by `conventions.projects[]` — the first project whose `path_globs` match a
path owns it; anything unmatched is **other** (repo-wide commands). The classification decides which
validations even apply — running a backend test suite for a stylesheet-only branch is a five-minute
answer to a question nobody asked. Use the merge-base, not the base tip: `git diff <base>` reports
commits that landed on the base branch since you branched as if you deleted them.

### 2a. The fix-verification gate — fail closed, at every tier

**Run this before anything else, because it is the only check that can block on its own.**

The loop's whole purpose is a fix that works. `qa-verify` is what proves it. A recipe that advances
one stage at a time upholds that by construction — and stops upholding it the moment `qa-verify`,
`qa-regression` and `dev-code-review` run **in parallel**, because a failed or crashed verify can
then reach this gate alongside two green artifacts.

**Do not hand-judge this.** Run the deterministic gate and adopt its answer verbatim:

```bash
python3 scripts/verify_gate.py --verify <EVID_ROOT>/<KEY>/verify.json \
        --changed-from-git --repo <repo-dir>          # --base defaults to project.default_branch
# exit 0 → continue · exit 1 → BLOCKED, use its next_action · exit 2 → bad usage
# stdout: {fix_verified, source, status, next_action, reason}
```

Copy `fix_verified` into `checks.fix_verified` and `source` into `sources.verify`, and put
`reason` in the report's Blockers section. The script fails closed on every ambiguity —
unreadable JSON, a missing `verdict`, stale vocabulary, a git command that cannot answer —
so there is no path where a gap reads as clean. It is covered by
`.claude/loops/tests/test_verify_gate.sh`.

What it enforces, for reference:

| `verify.json` | `checks.fix_verified` | `sources.verify` | Verdict |
|---|---|---|---|
| `verdict: pass` **and** fresh | `pass` | `fresh` | continue to step 2b |
| `verdict: fail` / `partial` | `fail` | `fresh` | **`blocked` → `re-fix`** |
| `verdict: blocked` | `fail` | `fresh` | **`blocked` → `escalate`** (the oracle gave no evidence about the fix) |
| present, fresh, but unreadable / contract-invalid / no `verdict` key | `malformed` | `malformed` | **`blocked` → `escalate`** |
| absent, inside a loop | `missing` | `missing` | **`blocked` → `escalate`** |
| present but **stale** (a changed file is newer) | `fail` | `stale` | **`blocked` → `re-fix`** (re-verify) |
| `verdict: pass` **but `masking_suspected: true`** | `fail` | `fresh` | **`blocked` → `re-fix`** (R3 — a masked pass is a fail) |
| no fix to verify — docs-only branch, or a standalone developer run outside any loop | `not-applicable` | `not-applicable` | continue |

**`not-applicable` is the only non-`pass` value that does not block, and a loop run may
never use it.** If `<EVID_ROOT>/<KEY>/` exists at all, you are inside a loop.

**A stale `verify.json` is never re-run here.** Unlike `regression.json`, this skill cannot
regenerate it: it has no `Edit` tool, it drives no browser, and — decisively — **a verify
this skill ran itself would not be blind**, which defeats the point. Stale routes back for a
re-verify; it never degrades to `ran-locally`.

**Never infer verification from anything else.** A green `regression.json` proves it
compiles and the unit tests pass. It does not prove the reported bug is gone. Only
`verify.json` does, and `masking_suspected: true` means the bug may be hidden rather than
fixed — treat it as `fail`.

### 2b. Reconcile the rest of the loop's evidence — by freshness, never by presence
For each of `regression.json` and `review.json`, decide **fresh · stale · missing** and record it in
`sources`:
```bash
# stale if ANY changed source file is newer than the artifact
find $(git diff --name-only $(git merge-base "$BASE" HEAD)) -newer <EVID_ROOT>/<KEY>/regression.json 2>/dev/null
```
- **fresh** → adopt its result. `regression.json.status` supplies build/lint/test; `review.json.verdict`
  and its findings supply the diff judgment. Do not re-run either.
- **stale or missing** → run the check yourself, and say so in `sources` (`ran-locally` / `missing`).
  Run **exactly** the commands the conventions file declares for each touched project
  (`projects[].build` / `.lint` / `.unit_test`, else the repo-wide `stack.*_command`) — the same
  commands `qa-regression` runs, so the two can never disagree:
  ```bash
  # for each project whose path_globs matched a changed file:
  #   <project.build>  &&  <project.lint>  &&  <project.unit_test>     (skip a null command, record it as skipped)
  ```
  **The configured flags are not optional.** Those commands are the CI-parity commands — the exact
  flags CI uses, including any warnings-as-errors setting. A looser local build passes with warnings
  the pipeline promotes to errors — green here, red in CI, PR check fails. Fix warnings at the source;
  never suppress them. A project with no configured command for a check is `skipped`, which is **not**
  a pass; a command that cannot run (toolchain missing) is `unknown`, which is a blocker.
- A **missing** `review.json` is not a blocker on its own (the standard defect recipe has no review
  stage) — record it and move on. A missing *build* result is: with nothing proving it compiles, the
  honest verdict is `blocked`, not `ready`.

### 3. Run the deterministic conventions scan
```bash
bash scripts/standards_check.sh --repo <repo-dir>          # --base / --rules default from stack.config
```
The `rules` array of the target repo's conventions file (`.claude/loops/conventions.json` unless
`stack.conventions_file` says otherwise), matched against **added lines only**, zero model calls.
Exit `3` = at least one non-heuristic critical/high; `2` = could not scan (fail closed — treat as
`blocked`, never as clean). Read `references/violation-rules.md` for the rule format, the
`suppress_marker` escape hatch, and the rules that are deliberately left to a reader.

Then judge the **Half 2** rules — the ones in `conventions.guidelines_file` that no regex decides
honestly (layer boundaries, tenant scoping, soft-delete bypass, design-token substitution, and
whatever else the target repo wrote down) — over the changed hunks. Report them in the same shape as
the scanner's findings, `source: reader`, so the report has one voice.

### 4. Confirm the heuristic findings before they block
A finding marked `heuristic: true` is a lead, not a verdict — the scanner deliberately refuses to block
on it. **Open the file and answer the rule's `confirm_question`** (it rides along in the scanner's
output; `references/violation-rules.md` explains the shipped ones). A confirmed CRITICAL heuristic —
the shipped `no-secret-literals`, or a tenant-isolation rule if the target repo has one — is the most
serious thing this skill can find and blocks as CRITICAL; a cleared one goes in the report as *checked,
fine*, so the next run doesn't re-litigate it. Everything you didn't confirm stays a warning, labeled
unconfirmed. **Never promote a heuristic to a blocker without reading the file** — a false blocker
costs a whole loop cycle, and after two of them nobody trusts the gate.

### 5. Roll up the blockers
A blocker is anything that must change before a human should see this as a PR:
- **`checks.fix_verified` anything but `pass` or `not-applicable` (step 2a) — the first
  blocker to check and the one that can never be downgraded to a warning,**
- build / lint-above-baseline / test failure, or a build result that could not be established (step 2b),
- a confirmed CRITICAL or HIGH conventions violation (steps 3–4),
- an unresolved CRITICAL or HIGH finding in `review.json` (an `approve` verdict clears these),
- evidence that could not be established at all (scan exit 2, unreadable artifacts).

Everything real but non-blocking — MEDIUM/LOW violations, unconfirmed heuristics, a missing review stage
— is a **warning**. Warnings inform; they never stop the loop, or the gate becomes noise the recipe
learns to route around.

### 6. Verdict + report
| status | Meaning | `next_action` |
|---|---|---|
| `ready` | validations pass, zero blockers, zero warnings | `ship-ok` |
| `warnings` | validations pass, zero blockers, warnings present | `ship-ok` |
| `blocked` | any blocker, or evidence that couldn't be established | `re-fix` (or `escalate`) |

`warnings` advances deliberately: a MEDIUM violation that can't block would otherwise spin the loop
forever, so it rides along in the report and the PR body where a human sees it. Use `escalate` rather
than `re-fix` only when re-fixing can't help — the scan couldn't run, the repo is in a bad state, or the
same blocker already survived a fix cycle.

Write `pre-pr.json` (validate: `python3 contracts/validate.py`), then `pre-pr-report.md`. `checks`
carries one `build` / `lint` / `tests` entry per touched project, keyed `<project>_build`,
`<project>_lint`, `<project>_tests` (plain `build` / `lint` / `tests` when the repo declares no
projects), plus `standards_scan`, `code_review` and `fix_verified`:

```markdown
# Pre-PR Readiness — <branch>
**Status:** <READY | WARNINGS | BLOCKED> · **Files:** N (<count per project>, O other) · **Lines:** +A / -R

## Validations
| Check | Result | Source |
|---|---|---|
| **Fix verified** | **PASS / FAIL / MISSING / MALFORMED / N-A** | **verify.json (fresh \| stale)** |
| <project> build / lint | PASS/FAIL/SKIPPED/UNKNOWN | regression.json (fresh) \| ran locally \| n/a |
| <project> tests (<the configured command>) | PASS/FAIL/SKIPPED/UNKNOWN | ... |
| Conventions scan | N violations (C/H/M/L) | standards_check.sh |
| Code review | approve / request-changes / not run | review.json (fresh) |

Put **Fix verified** first in the table. It is the row a reader should see before any other,
and a `FAIL`/`MISSING` there makes every row under it irrelevant.

## Blockers
<file:line — rule — what to do instead, or "None">

## Warnings
<same shape, including unconfirmed heuristics, or "None">

## Confirmed clear
<heuristics you checked and cleared — so the next run doesn't re-open them>

## Next
<the one thing to do: fix the blockers / open the PR / escalate>
```
Print the same summary to the terminal. If the recipe invoked you, its router consumes `pre-pr.json`;
if a human did, the report *is* the deliverable.

### 7. Write back to the brain
Record a `pre-pr` metric via `qa-kb` (status, blocker count, wall time — R5/R8 loop health). A violation
you judged and **dismissed** as a false positive goes to `qa-kb` as a known-FP with its provenance, the
same path `dev-code-review` uses — so suppression is shared and decayed with the brain instead of
accumulating in a stray JSON. Writes nothing canonical.

## Success criteria & failure categories (declared up front)
**Success:** a verdict a human would agree with on inspection — every blocker is real and reproducible
from the report, every check names where its answer came from, and nothing was declared PASS on evidence
older than the code.
**Failure categories:**
- `stale-evidence` — adopted a `regression.json`/`review.json` older than the diff. The specific failure
  this skill exists to prevent; treat a PASS you can't date as `missing`.
- `false-blocker` — blocked on a pre-existing violation, or on an unconfirmed heuristic.
- `missed-blocker` — greened a branch that CI then failed (almost always a local build that did not use
  the CI-parity command from the conventions file).
- `scan-unavailable` — `standards_check.sh` exited 2 and the run continued as if clean. Fail closed.
- `duplicated-work` — re-ran a build that a fresh `regression.json` already answered.
- `unverified-ship` — emitted `ready`/`warnings` with `fix_verified` anything but `pass` or
  `not-applicable`. **The most serious failure this skill has**: it means a fix nobody proved
  works reached `infra-ship` with a green readiness verdict. Strictly worse than
  `missed-blocker`, because the loop's entire justification is the verified fix.
- `verification-by-proxy` — inferred that the fix works from a green `regression.json`, a
  clean conventions scan, or an `approve` review. None of those look at the reported bug.

## Knowledge brain (qa-kb)
**Writes** a `pre-pr` metric (R5/R8) and known-FP provenance. **Reads** nothing canonical, proposes
nothing. Judgment about *behavior* lives in `expectations`, and this skill has no opinion about behavior.

## Key principles
1. **Consolidate, don't duplicate.** Reconcile the evidence the loop already produced; only run what
   nothing has answered — or what has gone stale.
2. **Date every PASS.** A check whose freshness you can't establish is `missing`, not passing. This is
   the whole reason the skill exists.
3. **Added lines only.** Never blame the author for pre-existing code — that is how a standards report
   loses its readers.
4. **Heuristics are leads.** Read the file before a heuristic blocks anything; an unconfirmed one is a
   warning, always.
5. **CI parity or it doesn't count.** The commands in the conventions file are the ones CI runs; a
   looser local green is not a green.
6. **Report, never fix.** No `Edit` tool. The verdict routes to `dev-fix` or to a human — this gate does
   not quietly repair what it found.
7. **No verified fix, no `ready`.** `verify.json` is the one input this gate may never shrug off.
   Missing, stale, malformed or not-`pass` all block, at every tier, with no override. This is the
   mechanical form of the invariant that a tier may shrink *how much* is verified but never *whether*
   — and it is what makes the parallel verify/regression/review layer safe to run at all.
