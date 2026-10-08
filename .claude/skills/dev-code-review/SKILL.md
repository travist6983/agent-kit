---
name: dev-code-review
description: >-
  The extra review gate inside a defect/feature LOOP: judge a dev-fix diff against the target repo's
  guidelines + conventions ruleset AND for R3 symptom-masking, returning a machine verdict (approve /
  request-changes) in review.json that the recipe routes on. Trigger for "review the candidate fix",
  "code-review the loop's diff", "is this fix masking the symptom", or as the review step
  dev-loop-hard-defect runs before ship. Scope: loop-produced fix diffs needing a machine-actionable
  verdict — for a general "review this diff / review my changes", use the built-in code-review skill
  instead. Runs the 6-specialist split (frontend, backend, data, security, tests, perf) plus the
  masking judge; findings validated at confidence ≥ 80. Does NOT fix or ship.
allowed-tools: Read, Glob, Grep, Bash, Agent, ToolSearch
---

# dev-code-review — high-signal review + the masking judge, as a machine verdict

`dev-code-review` is the recipe's quality gate before a hard defect ships. It is a high-signal
multi-specialist review (six stack-agnostic specialists, false-positive suppression, per-finding
validation at confidence ≥ 80, fingerprint dedup) **plus** the one thing an autonomous loop needs
that a human review assumes: an explicit check that the fix isn't **masking** the symptom (R3), and
a **machine verdict** (`approve` / `request-changes`) the recipe can act on. Read
`references/specialists.md` for the six specialists and `references/masking-specialist.md` for the
R3 judge — the masking judge is the loop-specific addition.

## What this block does NOT do (atomic boundary)
It does **not** fix (`dev-fix`), verify at runtime (`qa-verify`), or ship (`infra-ship`). It reviews the
diff and returns a verdict + findings.

## Inputs / Outputs
**Inputs:**
- the candidate diff — `git diff <base>` in the sandbox, where `<base>` is `--base` or
  `project.default_branch` from `.claude/loops/stack.config.json` (never a literal branch name);
- `fix.json` (the failure class the fix targets), at `<EVID_ROOT>/<KEY>/fix.json`;
- the constraints the diff is judged against — **two config-named sources, read at the start of
  every review, never assumed:**
  1. the target repo's guidelines file: `stack.repo_guidelines_file` (default `CLAUDE.md`), resolved
     relative to `app.repo_dir`;
  2. the conventions ruleset: `stack.conventions_file` (default `.claude/loops/conventions.json`) —
     `rules[]` (the standards rules, each with an id), `shared_core_globs` (files where a change is
     higher-risk by definition), `blast_radius_denylist_globs` (files a loop fix may never touch —
     a diff that touches one is a CRITICAL finding regardless of content), and `domain_glossary`
     (the app's nouns, so a finding can speak the adopter's language).

  A missing guidelines file is not an error — note `guidelines: none` in `summary` and judge on
  the conventions rules + the universal rules in `references/specialists.md`. A missing or
  unparsable conventions file **is** a blocker for the convention dimension: review proceeds on
  the other dimensions and `summary` records `conventions: unavailable` so the readiness gate can
  see the gap.

**Output:** `review.json`, written to **`<EVID_ROOT>/<KEY>/review.json`** (EVID_ROOT = `evidence.root`
from stack.config, default `runs/`, overridable with `LOOPS_CFG__EVIDENCE__ROOT`; `<KEY>` matches
`project.ticket_key_pattern`, e.g. `PROJ-123`; contract: `contracts/dev-code-review.json`)
`{ verdict: approve | request-changes, findings: [...], masking: bool, critical, high, confidence }`,
plus the human-readable `review.md` beside it. Write it through `contracts/emit.py` so the artifact is
validated before it lands. Record `judged_blind`, `attempt`, `retry_reason`, `timing` and `model`
(the reviewer runs on `models.verifier` from stack.config unless the recipe says otherwise) so
retries and judge strength are auditable after the fact.

## Procedure (six specialists; add the masking pass + verdict)
### 1. HIGH-SIGNAL philosophy
Only flag issues that are clearly real (won't compile / wrong result / unambiguous violation of a
guidelines-file rule or a `conventions.rules[]` entry, cited by id / exploitable security /
scale-failing perf / masking). **If not certain, don't flag.** False positives erode trust and waste
loop cycles.

### 2. Specialists (parallel, validated)
Run the six specialists in `references/specialists.md` — **frontend, backend, data, security,
tests, perf** — over the diff (chunk at 50 files), **plus** the **R3 symptom-masking specialist**
(`references/masking-specialist.md`). A specialist whose surface the diff doesn't touch (no
frontend files changed, no data-layer files changed) returns an empty list, not a guess. Each
returns strict JSON findings at confidence ≥ 80, `dimension` set to its name. Then **validate**
each finding (re-read the code at file:line, reject pre-existing or < 80) — the second
false-positive filter. A rejected pre-existing issue that is genuinely serious (a missing
authorization check, a data-loss path) still deserves a human's eyes — just not on this change:
write it as one bullet under a `## Noticed, not changed` heading in the markdown report,
`<EVID_ROOT>/<KEY>/review.md`. The ship skill reads that heading into the PR description
(`--review-report`) when it opens the PR through the `vcs_host` adapter, and the recipe's run
report drafts an issue for a serious one through the `issue_tracker` adapter. It never becomes a
review thread on the PR; nothing the reviewer does to this diff resolves it. (A hazard the diff
*newly exposes* is not pre-existing — it is a finding about this diff.)

### 3. The masking judge (the loop gate)
The masking specialist judges: does the green depend on a swallowed exception / suppressed log / narrow
special-case / disabled check / symptom-surface-only edit? The discriminating question is **"is the
underlying state actually correct now, or just less visible?"** Any confirmed `SYMPTOM_MASKING`
(≥ 80) sets `masking: true`.

### 4. Verdict (machine-actionable)
- Any CRITICAL finding, or `masking: true` → **`request-changes`** (the recipe sends it back to `dev-fix`
  with the findings as new context).
- HIGH findings → `request-changes` unless the recipe's policy accepts them with a note (in
  `review.json` `summary` and the report). An accepted HIGH is a known risk in this diff going to
  merge: it reaches the PR only as an `infra-ship` `pr-candidates.json` entry with the matching
  `must_read_reason`, posted (if at all) through the `vcs_host` adapter — never as a free-form PR
  comment.
- Clean → **`approve`**.

**Set `resolved: true` on every finding a re-fix cycle actually closed.** `review.json` is
overwritten in place, so a later `approve` otherwise erases the fact that an earlier round said
`request-changes` — and an audit reading only the artifacts would count a loop that iterated as
one that passed first time. Without the field, an `approve` still listing a HIGH that a re-fix *did*
address is indistinguishable from an `approve` that shipped the HIGH; nothing in the artifact can
tell the two apart.

The convergence checker reads this field:

| review.json | Checker |
|---|---|
| `approve` + unresolved **CRITICAL** | **blocks** — the two cannot both be true |
| `approve` + unresolved **HIGH**, no `resolved` | **warns** (`unverifiable-claim`) — probably bookkeeping, but unfalsifiable |
| `approve` + findings marked `resolved: true` | clean |

A finding you deliberately accepted as a known risk is **not** `resolved` — mark it accepted in
`summary` and carry it to the PR through the `must_read_reason` funnel. `resolved` means *fixed
in this diff*, and it is the difference between a claim and a receipt.

### 5. Record + route
Write `review.json` + the markdown report (`review.md`, beside it). **False positives go to `qa-kb`**
(known-FP provenance), not a flat `false-positives.json`, so suppression is shared + decayed with the
brain.

## Success criteria & failure categories (declared up front)
**Success:** a defensible verdict — every reported finding is real (validated ≥ 80), masking is caught,
and the verdict is machine-actionable.
**Failure categories (spec):** constraint violation; scope creep; missing test; risky pattern; **+ masking**.

## Knowledge brain (qa-kb)
Reads `app_map`/`expectations` for context; **records false positives** (so they're suppressed next time);
may write a `review` metric. Writes nothing canonical.

## Key principles
1. **High signal only.** A false finding costs a wasted loop cycle — when unsure, don't flag.
2. **Catch the mask.** A green replay + a swallowed exception is `request-changes`, not approve (R3).
3. **Machine verdict.** `approve`/`request-changes` so the recipe can act without a human.
4. **Defense in depth.** This is the diff-time masking check; `qa-verify` is the runtime one — both run.
5. **Constraints come from config.** The guidelines file and the conventions ruleset are the only
   sources of "this repo's rules"; the reviewer never carries a framework's rules in its head.
6. **Atomic.** Review + verdict. No fixing, no shipping.
