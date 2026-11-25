# dev-code-review — the eval set: how this reviewer is scored

## Why

A reviewer that is never scored drifts silently. The set in `evals/cases/` turns past incidents into
an eval: **bad cases** are diffs with a confirmed bug at a confirmed location, so they measure
**recall** (did the reviewer find it); **clean cases** are diffs a human confirmed have nothing to
flag, so they measure the **harmful false-flag rate** (did the reviewer block a good fix). A false
flag costs a wasted loop cycle and erodes trust as surely as a miss ships a bug, so both are counted.

Rescore after any change to `SKILL.md`, anything under `references/`, the reviewer's tool list, or
`models.verifier` in stack.config. Compare a score with the previous one before and after the change.

## The case format

One directory per case, `evals/cases/<id>/`:

| File | What it is |
|---|---|
| `case.json` | The answer key, contract `contracts/dev-code-review.eval-case.json`. Never shown to the reviewer. |
| `change.diff` | The candidate diff, produced by a real `git diff` (never written by hand). |
| `base/` | The files the diff modifies, as they were before it. New files need no base copy. |
| `fix.json` | Optional dev-fix receipt (`contracts/dev-fix.json`). The reviewer reads it, as in a loop. A masked fix's receipt claims a root-cause fix, because that is what a masked fix claims. |

`case.json` fields:

| Field | Meaning |
|---|---|
| `id` | Equals the directory name. |
| `kind` | `bad` (scored for recall) or `clean` (scored for false flags). |
| `title` | One line for humans. |
| `ticket` | A key in `project.ticket_key_pattern` form, e.g. `PROJ-123`; the evidence key when materialized. |
| `bugs[]` | `bad` only, at least one: `file` (repo-relative), `line_start`, `line_end` (new-file lines, inclusive), `class` (one of the reviewer's dimensions: frontend, backend, data, security, tests, perf, masking), optional `pattern` and `note`. |
| `source` | `origin` (`seed`, `prod-regressed`, `escalation`, `re-fix`, `human-review`), `ref` (the run key or PR ref it came from), `added` (YYYY-MM-DD), `confirmed_by` (the person who confirmed the location, or that the diff is clean). |

**A confirmed location** is the line range a human agrees is the defect: the line the follow-up fix
or rollback changed, not the line where the symptom surfaced. **It must sit on lines the diff adds**,
because the reviewer only judges the diff (a pre-existing bug is out of its scope by design);
`reviewer_eval.py check` refuses a range that does not. Rule `DEV-REVIEW-EVAL-K1` (validate.py): a
bad case names at least one bug, a clean case none, and any non-seed case carries `source.ref` and
`source.confirmed_by`.

## How a run works

```bash
EV=.claude/skills/dev-code-review/scripts/reviewer_eval.py
python3 $EV check                                         # the set is well-formed
python3 $EV materialize --case <id> --out <scratch>/<id>  # once per case, OUTSIDE evals/cases
#   -> <scratch>/<id>/repo   git repo: branch main = base/, branch eval-<id> = base + change.diff
#   -> <scratch>/<id>/runs/<ticket>/fix.json
# run dev-code-review as a FRESH Agent per case: repo <scratch>/<id>/repo, --base main,
#   evidence root <scratch>/<id>/runs, key <ticket>; copy its review.json to <reviews>/<id>/review.json
python3 $EV score --reviews <reviews> [--out score.json]
```

The reviewer runs exactly as it would in a loop: blind, on the materialized tree, with no access to
`evals/cases`. Running it is the step that spends model calls, and it is outside the scorer. The
scorer itself is zero-LLM, offline and deterministic. `--cases DIR` (repeatable) replaces the default
set, so an adopter passes the kit's directory plus their own.

## How scoring decides

- **A bug is caught** when some finding has the same `file` (a leading `./` is ignored), an integer
  `line` within `line_tolerance` lines of `[line_start, line_end]`, and, for a `masking`-class bug,
  the review sets `masking: true`. The finding's `dimension` need not equal the bug's `class` (a
  tenant-scope bug is fairly data or security); class feeds only the per-class breakdown.
- A bad case is `caught` (all bugs), `partial` (some) or `missed` (none). A catch on a review whose
  verdict is `approve` is reported as `caught-but-approved` and still counts as caught.
- Findings that match no confirmed bug are `unmatched_findings`: reported, **never penalised**, since
  the answer key lists confirmed bugs, not every possible one. Findings with no integer `line` are
  `unlocatable_findings` and can never count as a catch.
- **A clean case is false-flagged** when the verdict is `request-changes`, or `masking` is true, or
  any `critical`/`high` finding is not `resolved: true`. Low and medium findings on a clean case are
  counted as `noise_findings` and are not gated.
- **recall** = caught bugs / all confirmed bugs; **false_flag_rate** = false-flagged clean cases /
  clean cases with a valid review.
- **A case with no `review.json`, or one that fails `contracts/dev-code-review.json`, is never
  dropped**: its bugs count as missed and the score is `incomplete`.
- Exit codes: `0` pass (at or above both bars) · `1` fail (below either) · `2` incomplete or bad input.
- The output (`contracts/dev-code-review.eval-score.json`) carries no timestamp and no model field,
  so two runs over the same inputs are byte-identical. **Compare two scores only when
  `case_set.sha256` matches**: a different set is a different exam.

## The bar

`min_recall`, `max_false_flag_rate` and `line_tolerance` live in `.claude/loops/loop-limits.json`
under `reviewer_eval`; the scorer reads them and has no default of its own. They are argued, not
measured: the seed set is small and synthetic, so the first real baseline should replace them.
Nothing schedules the eval and no runner or ship gate reads the score yet; it is a declared bar a
person or a later job runs.

## Growing the set

The seed cases prove the harness; real cases make the score mean something. Find candidates with:

```bash
python3 $EV candidates --evidence-root <evidence.root>
```

It lists run keys whose `prod-verify.json` says `prod-regressed`, that have an `escalate.json`, or
that went through a re-fix cycle (`attempt > 1`, or findings marked `resolved: true`), and that no
case already names in `source.ref`. It writes nothing and promotes nothing. For each candidate a
human decides:

- **Prod-regressed and the review approved** → a `bad` case. The confirmed location is the line the
  follow-up fix or the rollback identified. `origin: prod-regressed`.
- **Escalation or re-fix cycle** → a `bad` case when a human confirms the reviewer missed or
  mis-located the bug (`origin: escalation` or `re-fix`). When the human confirms the first-round diff
  was actually fine and the reviewer blocked it, that diff is a `clean` case.
- **A false positive the brain already suppresses** is a good `clean` case: it proves the
  suppression, and it keeps working if the brain entry decays.

Every non-seed case needs `source.ref` and `source.confirmed_by`. Minimise the diff to the files that
matter and regenerate it with a real `git diff` over a minimal `base/`. Sanitise: a case holding an
adopter's real code lives in the adopter's own repo or overlay and is passed with `--cases`; only
neutral-domain cases (Invoices, Projects, Members, Workspaces; `PROJ-123`-style keys; `example.com`)
are committed to the kit. Run `check` after every addition.

## What this is not

It does not run the reviewer, schedule anything, write to the brain, record a metric, or gate a ship.
It is the exam and the marking scheme; sitting the exam is a separate step.
