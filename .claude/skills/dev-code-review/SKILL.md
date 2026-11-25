---
name: dev-code-review
description: >-
  The extra review gate inside a defect/feature LOOP: judge a dev-fix diff against the target repo's
  guidelines + conventions ruleset AND for R3 symptom-masking, returning a machine verdict (approve /
  request-changes / needs-human) in review.json that the recipe routes on. Trigger for "review the candidate fix",
  "code-review the loop's diff", "is this fix masking the symptom", or as the review step
  dev-loop-hard-defect runs before ship. Scope: loop-produced fix diffs needing a machine-actionable
  verdict — for a general "review this diff / review my changes", use the built-in code-review skill
  instead. Six specialist lenses (frontend, backend, data, security, tests, perf) plus the masking
  judge, the fan-out scaled by a risk-based depth from the diff; findings validated by independent
  validators at a quorum, except masking and critical findings, which only a unanimous reject drops
  and a split sends to a human. Does NOT fix or ship.
allowed-tools: Read, Glob, Grep, Bash, Agent, ToolSearch
---

# dev-code-review — high-signal review + the masking judge, as a machine verdict

`dev-code-review` is the recipe's quality gate before a hard defect ships. It is a high-signal
multi-specialist review (six stack-agnostic specialists that only propose candidates, independent
validators — fresh `Agent` spawns, never this context — that vote on each candidate, a quorum counted
by `scripts/finding_votes.py`, mechanical dedup) **plus** the one thing an autonomous loop needs
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
  3. optionally, in the same conventions file, the code owners' **reviewer brief** `review_brief`
     (what failure looks like per area, a nit cap, paths to skip). Read it ONLY through
     `python3 scripts/review_brief.py plan --repo <sandbox> --base <base>` at the start of every
     review, never by reading the key yourself: the script is what refuses a skip that would cover a
     denylisted or shared-core file. No key (status `absent`) = review as you always have. Exit 2 /
     status `unavailable` = review everything, no cap, and write `brief: unavailable` in `summary`.
  4. the **review depth**, read ONLY through
     `python3 scripts/review_depth.py plan --repo <sandbox> --base <base> [--evid <EVID_ROOT>/<KEY>]`.
     Pass `--evid` whenever a loop fix is being reviewed (`fix.json` exists); omit it for an incoming
     PR. Exit 2, or no plan at all, = run at `deep` with every pass. A caller may raise the depth with
     `--min-depth`; nothing may lower it, and you never pick a level by eye.

  A missing guidelines file is not an error — note `guidelines: none` in `summary` and judge on
  the conventions rules + the universal rules in `references/specialists.md`. A missing or
  unparsable conventions file **is** a blocker for the convention dimension: review proceeds on
  the other dimensions and `summary` records `conventions: unavailable` so the readiness gate can
  see the gap.

**Output:** `review.json`, written to **`<EVID_ROOT>/<KEY>/review.json`** (EVID_ROOT = `evidence.root`
from stack.config, default `runs/`, overridable with `LOOPS_CFG__EVIDENCE__ROOT`; `<KEY>` matches
`project.ticket_key_pattern`, e.g. `PROJ-123`; contract: `contracts/dev-code-review.json`)
`{ verdict: approve | request-changes | needs-human, findings: [...], masking: bool, human_gate, critical, high, confidence }`,
plus the human-readable `review.md` beside it (contract v1.5.0 adds `headline`, the count lead,
and `brief`, the record of the reviewer brief; both are written by `scripts/review_brief.py
finalize`, never by hand; contract v1.6.0 adds `validation`, the record of independent finding
validation, and `findings[].finding_id` / `votes`, all written by `scripts/finding_votes.py tally`,
never by hand; contract v1.7.0 adds `depth`, the review depth and the passes it ran, written by
`scripts/review_depth.py record`, never by hand). **Every finding carries `file` (repo-relative, forward
slashes, no leading `./`) and an integer `line`: the new-file line number of the added or changed
line it is about** (contract v1.3.0). That pair is how a human, the validators and the eval
scorer find what you meant; a range or a string is unlocatable, so a real catch written that way
scores as a miss. **Every finding also carries `bug_class`**, one word from the contract's closed
vocabulary (v1.4.0; one line per class in `references/specialists.md`), and lists the other places in
this diff where the same class occurs under `instances` (`[{file, line}]`) instead of raising one
finding per occurrence: flagging the class is what gets every instance fixed, not just the anchored
line. On a re-review, a finding that is still open keeps its `file` and `bug_class` even when you
would word it differently: file + bug_class is the fingerprint `review_rounds.py` uses to notice that
the same finding survived a fix cycle, so a renamed class hides stagnation. Use `other` only when no
class fits; it falls back to a weaker, wording-sensitive fingerprint. Write it through `contracts/emit.py` so the artifact is
validated before it lands. Record `judged_blind`, `attempt`, `retry_reason`, `timing` and `model`
(the reviewer runs on `models.verifier` from stack.config unless the recipe says otherwise) so
retries and judge strength are auditable after the fact.

## Procedure (six specialists; add the masking pass + verdict)
### 1. HIGH-SIGNAL philosophy
Only flag issues that are clearly real (won't compile / wrong result / unambiguous violation of a
guidelines-file rule or a `conventions.rules[]` entry, cited by id / exploitable security /
scale-failing perf / masking). **If not certain, don't flag.** False positives erode trust and waste
loop cycles.

### 1b. Load the reviewer brief
Run `scripts/review_brief.py plan` (Inputs, item 3) before the fan-out. Hand each specialist only
the plan's `review_files`, and give a specialist the `failure_looks_like` and `not_worth_flagging`
lines of the areas whose files it is reading: that is the owner telling you what failure looks like
there, so look harder for it. A brief can add scrutiny, cap nits and skip paths. It can never lower
a severity, waive a critical or high, change the verdict rule in §4, or skip a path in
`skip_refused`: those files are reviewed as normal, and a denylisted one is still CRITICAL on sight.
Every finding still needs `file` + an integer `line` as evidence. The masking judge (§3) reads the
whole diff whatever the brief skips, because a mask can hide in a "generated" file. Report every
validated low finding; the nit cap is applied mechanically in §5, not by trimming.

### 1c. Pick the depth
Most small diffs come back clean, so spawning six specialists over a one-line copy change buys
nothing; a two-line change to an auth config deserves more than the usual pass. `review_depth.py
plan` (Inputs, item 4) decides from the diff, deterministically, and its `passes` are what §2 spawns:

| Level | When (the script decides) | Specialist spawns |
|---|---|---|
| `quick` | ≤ `quick_max_files` files and ≤ `quick_max_changed_lines` lines (loop-limits `review_depth`), no risk path, no unknown, not an in-loop review | ONE `Agent` spawn handed all six specialist briefs; every finding still carries the `dimension` of the lens that found it |
| `standard` | over the size limit, an unknown line count, an empty diff, or any in-loop review (`--evid`) | the six, one spawn each |
| `deep` | a risk path (R6 denylist via the adopter list or the framework floor, `shared_core_globs`, intake `sensitive`, a change to the conventions file) or any unknown | the six + `security-risk-paths`: one more security spawn reading the risk files whole, plus their callers |

Why the invariants hold at every depth: depth shrinks how much is spawned, never which dimensions
are looked at, so **security is never off** (quick carries the security lens; deep adds a second
security pass where the risk is). The masking judge, the validators and their quorum, the brief's
skip rules, the §4 verdict rule and the evidence rule (`file` + integer `line`) are the same at
every level; a denylisted file is CRITICAL on sight whether the review is quick or deep. The pass
briefs are in `references/specialists.md` "Review depth".

### 2. Specialists propose, independent validators decide (parallel, counted by a script)
A producer never grades its own work, and that holds inside this skill: the context that writes the
verdict does not decide which findings are real. Numbers: `loop-limits.json` `review_validation`
(`validators`, `quorum`, `confidence_floor`; 3 / 2 / 80 as shipped, argued not measured). The full
validator brief is `references/specialists.md` "The validators". The sequence is mechanical:

1. **Specialists** (fresh `Agent` spawns, parallel). Spawn the depth plan's `passes` (§1c) — the six
   lenses in `references/specialists.md`, **frontend, backend, data, security, tests, perf**, as one
   combined spawn at quick, six at standard, six + `security-risk-paths` at deep — over the diff
   (chunk at 50 files), **plus** the **R3 symptom-masking specialist**
   (`references/masking-specialist.md`), at every depth. A specialist whose
   surface the diff doesn't touch returns an empty list, not a guess. Each returns strict JSON
   findings at or above the floor, `dimension` set to its name.
2. **Merge without judging.** Put every specialist finding into
   `<EVID_ROOT>/<KEY>/review-validation/candidates.json` (`{"findings": [...]}`). Do not drop,
   merge, re-rate or reword a candidate: that would be this context grading.
3. `python3 scripts/finding_votes.py ballot --dir <EVID_ROOT>/<KEY>/review-validation` — ids,
   mechanical dedup, the floor, and `ballot.json` with the specialist's confidence stripped.
   `no-candidates` = spawn no validator.
4. **Spawn the `validators` as fresh `Agent` calls in ONE message.** Each gets only `ballot.json`,
   the sandbox path, the base and the validator brief, re-reads the code at file:line, checks the
   line is in the diff, and writes its own `votes/v<i>.json` with its own confidence. It never reads
   another validator's votes, `candidates.json`, the specialist's confidence, or fix.json's
   `design_decisions` / `notes` / `confidence`. You never write a vote file yourself.
5. `python3 scripts/finding_votes.py tally --dir <that dir> --review <draft review.json>` — keeps a
   an ordinary finding only with at least `quorum` confirms at or above the floor, and writes
   `findings`, `critical`, `high`, `masking`, `human_gate` and `validation` into the draft. A
   masking-class or critical finding is different (§3a): only a unanimous reject drops it, and a
   split keeps it with `human_gate: true`. **Exit 2 (`validation-unavailable`:
   fewer than `quorum` usable ballots) = no review.json; report `blocked: validation-unavailable` to
   the recipe.** Re-spawn a missing validator only for a transient reason.
6. Only then: §2b harden blocks, the §4 verdict and the `summary`, on the kept findings; §5
   `review_depth.py record`, then `review_brief.py finalize`; `contracts/emit.py`. `finding_votes.py check` re-derives the tally
   (exit 0 / 1 / 2).

A candidate the validators marked pre-existing and serious (`tally`'s `noticed_candidates`) still
deserves a human's eyes — just not on this change: write it as one bullet under a
`## Noticed, not changed` heading in the markdown report, `<EVID_ROOT>/<KEY>/review.md`. The ship
skill reads that heading into the PR description (`--review-report`) when it opens the PR through
the `vcs_host` adapter, and the recipe's run report drafts an issue for a serious one through the
`issue_tracker` adapter. It never becomes a review thread on the PR; nothing the reviewer does to
this diff resolves it. (A hazard the diff *newly exposes* is not pre-existing — it is a finding
about this diff.)

On a re-review, an earlier finding carried with `resolved: true` needs no votes (`tally` keeps it
and does not count it); every open finding goes through the ballot again.

### 2b. Draft a check for findings a check could catch (harden block)
After `tally`, ask of each kept finding: **would a check have caught this before review?**
A banned call, a banned import, a suppression comment, a hard-coded value: one regex over an added
line decides it. When the answer is yes, attach a `harden` block to the finding (contract
`dev-code-review.json` v1.2.0) so the correction is not made by hand again:
```
"harden": { "band": "static", "why": "<the recurring mistake and the right move, one sentence>",
            "candidate_rule": { id, severity, include_path, pattern, skip_tests, rule, fix },
            "must_trip": "<the offending line from this diff>", "must_pass": "<the compliant line>" }
```
`band` can also be `structure` (a design change would make it impossible), `verification` or
`prose`; only `static` takes a `candidate_rule`. Write the rule's `fix` **to the next agent**: what
to do instead and where the real fix lives, in one or two sentences (format:
`../dev-pre-pr/references/violation-rules.md`). Reuse the same `candidate_rule.id` for the same
mistake across tickets; that id is how `dev-harden` counts recurrence.

You only **draft**. Never write to the conventions file, the harden queue or a guidelines file:
`dev-harden` proves the rule with fixtures and the real scanner between runs, and a human applies
it. List the drafted ids under a `## Harden candidates` heading in `review.md`. Do not attach a
block to a judgment call (naming, design taste) or to a finding the conventions rules already
cover; that rule is already enforced.

### 3. The masking judge (the loop gate)
The masking specialist judges: does the green depend on a swallowed exception / suppressed log / narrow
special-case / disabled check / symptom-surface-only edit? The discriminating question is **"is the
underlying state actually correct now, or just less visible?"** `masking` is set by `tally`, not by
you: a symptom-masking finding every returned ballot confirmed sets `masking: true`.

### 3a. Protected findings: a mask or a critical is never voted down
A masking-class finding (this judge's `symptom-masking` finding) and any finding with severity
`critical` are too costly to lose on a 2-of-3 vote: one validator who sees a swallowed failure may
be the only one who looked hard enough. So `tally` treats them differently (owner decision; it is
not a loop-limits number and nothing turns it off):
- They go on the ballot whatever the specialist's confidence.
- Every returned ballot rejects it → dropped. This is the only way one leaves.
- Every returned ballot confirms it at or above the floor → kept as a plain finding (a mask sets
  `masking: true`, a hard block).
- Anything else (one confirm, a `pre-existing` vote, a confirm below the floor, an abstain or an
  unusable vote) → kept with `human_gate: true` and a `human_gate_reason` naming the split, the
  review's `human_gate` is true, and `masking` stays false because the call is undecided.
A split is a disagreement between judges about the fix, and another fix cycle cannot settle it, so
the verdict goes to a person (§4). Fewer than `quorum` usable ballots is still
`validation-unavailable`: a protected finding never makes an unvalidated review pass through.

### 4. Verdict (machine-actionable)
- Any finding with `human_gate: true` → **`needs-human`**, whatever else the review holds. The
  recipe escalates it to a human (`recipe_router.py` returns `escalate`, failure class
  `review-needs-human`) and never re-fixes on it. validate.py DEV-REVIEW-G1 refuses a gated review
  under any other verdict, and `masking: true` or `human_gate` under `approve`.
- Any CRITICAL finding, or `masking: true` → **`request-changes`** (the recipe sends it back to `dev-fix`
  with the findings as new context).
- HIGH findings → `request-changes` unless the recipe's policy accepts them with a note (in
  `review.json` `summary` and the report). An accepted HIGH is a known risk in this diff going to
  merge: it reaches the PR only as an `infra-ship` `pr-candidates.json` entry with the matching
  `must_read_reason`, posted (if at all) through the `vcs_host` adapter — never as a free-form PR
  comment.
- Clean → **`approve`**.

**`approve` is a machine verdict, never a human approval.** The recipe routes on it; it fills no
required-reviewer slot on the PR, and this skill never approves, votes or merges on a host (the
`vcs_host` adapter has no such operation). How many humans must approve is stated by `dev-pre-pr` in
`pre-pr.json` `risk.human_approvals_required`, from the actual diff, and an agent `approve` never lowers
it. A reviewer's approval covers the commit it read; a later push needs reviewing again.

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
| `needs-human`, or any open `human_gate` finding | **escalates** (`needs-human`; under `approve` it is `contradictory`) |
| `approve` + unresolved **HIGH**, no `resolved` | **warns** (`unverifiable-claim`) — probably bookkeeping, but unfalsifiable |
| `approve` + findings marked `resolved: true` | clean |

A finding you deliberately accepted as a known risk is **not** `resolved` — mark it accepted in
`summary` and carry it to the PR through the `must_read_reason` funnel. `resolved` means *fixed
in this diff*, and it is the difference between a claim and a receipt.

### 5. Record + route
Write the draft `review.json` + the markdown report (`review.md`, beside it). When the plan skipped
or refused anything, list those paths in `review.md` under a `## Not reviewed (brief skip_paths)`
heading (refused ones marked as reviewed anyway), and put a `Depth: <level> (<reasons>)` line
right under the headline. Then run
`python3 scripts/review_depth.py record --review <draft> --repo <sandbox> --base <base> [--evid <EVID_ROOT>/<KEY>]`
(the same flags as the plan; it recomputes the plan, never lowers a level already recorded, and
writes `depth`), then
`python3 scripts/review_brief.py finalize --review <draft> --repo <sandbox> --base <base> --report <EVID_ROOT>/<KEY>/review.md`
and land the result through `contracts/emit.py --from-json`. `finalize` withholds the low findings
over the cap, writes `brief`, and puts the `headline` first in `summary` and on the first line of
`review.md`; it is idempotent. Never hand-write `headline`, `brief`, `depth`, or a nit-cap
`withheld`; `review_depth.py check` re-derives the depth (exit 0 / 1 / 2). A
clean review reads `0 findings: nothing needs fixing.` and says so plainly; do not add filler nits
to look thorough. `scripts/review_brief.py check` re-derives all of it (exit 0 / 1 / 2). A candidate the
validators reject is simply not reported; `validation` counts it and nothing records it as a finding
(the review-validation dir is this run's evidence, not a store). There is **no false-positive suppression
store** — not in the brain and not in a flat file — so never claim to have looked one up or written
one. False positives are measured after the fact instead: `dev-pr-feed dispositions` reads back which
posted threads humans called wrong or intentional, and a confirmed one becomes a `clean` case in
`evals/cases` (`origin: human-review`) that the reviewer is then scored against.

## Success criteria & failure categories (declared up front)
**Success:** a defensible verdict — every reported ordinary finding reached quorum among independent
validators (`validation.status: independent`, or `no-candidates`), no masking or critical finding was
dropped without a unanimous reject, a split on one went to a human (`needs-human`), masking is
caught, and the verdict is machine-actionable.
**Failure class `review-needs-human`:** the validators split on a protected finding; the next step is
a person, never a re-fix or a retry.
**Failure categories (spec):** constraint violation; scope creep; missing test; risky pattern; **+ masking**.

## The eval set — how this reviewer is scored
`evals/cases/` holds past-incident diffs with a confirmed bug location (recall) and known-clean
diffs (harmful false flags); `scripts/reviewer_eval.py` checks, materializes and scores them with
no model call. Read `references/eval-set.md` for the run procedure and how to grow the set. Rescore
after any change to this file, `references/`, the tool list, `models.verifier` or
`review_validation` / `review_depth` in `loop-limits.json`; tune the quorum and the floor by replay, not by argument
(`references/eval-set.md`, "Tuning the quorum and the floor"). The cases are
answer keys: when you review, never read `evals/cases` — review only the diff you were given.

## Knowledge brain (qa-kb)
Reads `app_map`/`expectations` for context; may write a `review` metric, and writes nothing else to
the brain — rejected findings included (there is no false-positive store; see §5). Writes nothing
canonical. A `harden` block is a draft in review.json, not a rule: `dev-harden` reads it later.

## Key principles
1. **High signal only, decided by a quorum.** A false finding costs a wasted loop cycle — when unsure,
   don't flag; and what counts as real is decided by independent validators at a quorum, never by a
   self-check in the context that writes the verdict.
2. **Catch the mask.** A green replay + a swallowed exception is `request-changes`, not approve (R3).
3. **Machine verdict.** `approve`/`request-changes` so the recipe can act without a human, and
   `needs-human` for the one case it must not act alone: a split on a mask or a critical.
4. **Defense in depth.** This is the diff-time masking check; `qa-verify` is the runtime one — both run.
5. **Constraints come from config.** The guidelines file and the conventions ruleset are the only
   sources of "this repo's rules"; the reviewer never carries a framework's rules in its head.
6. **Atomic.** Review + verdict. No fixing, no shipping.
7. **Depth scales the fan-out, not the scrutiny of risky paths.** A small clean diff gets one
   combined pass; a risky path always gets the full six plus a security pass over it.
8. **The count comes first.** The summary and `review.md` lead with how many findings are open, and
   when nothing needs fixing the review says so in those words.
