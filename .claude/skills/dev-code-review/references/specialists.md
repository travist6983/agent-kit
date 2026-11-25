# dev-code-review — the six stack-agnostic specialists

The review fans out to six specialists, each a fresh `Agent` spawn over the same diff chunk (chunk
at 50 files), each returning strict JSON findings. None of them knows a framework. **What "this
repo's rules" are comes from two config-named files, read first and quoted by id in every
convention finding:**

- `stack.repo_guidelines_file` (default `CLAUDE.md`, relative to `app.repo_dir`) — the prose
  guidelines the target repo keeps for its own contributors;
- `stack.conventions_file` (default `.claude/loops/conventions.json`) — `rules[]` (each with an
  `id`), `shared_core_globs`, `blast_radius_denylist_globs`, `domain_glossary`, and the optional
  `review_brief` (the per-area reviewer brief, below; read only through `scripts/review_brief.py plan`).

A specialist that would need a framework fact not present in those files does not invent it. It
either finds the fact in the repo (an existing pattern, three sibling files doing it the same way)
or it does not flag.

## The reviewer brief (optional)

When the conventions file has a `review_brief`, `scripts/review_brief.py plan --repo <sandbox>
--base <base>` runs before the fan-out and decides, without a model, what each specialist gets:

- **The files.** A specialist reads only `review_files`: the changed files minus the ones a
  `skip_paths` glob matched. A file in `skip_refused` (hit by the R6 denylist, the adopter list or
  the framework floor, or by `shared_core_globs`) is in `review_files` and is reviewed as normal; a
  denylisted one is still CRITICAL on sight. The masking judge reads the whole diff regardless.
- **The owner's words.** For each area in the plan whose `files` the specialist is reading, it gets
  the area's `failure_looks_like` lines (what a real bug looks like there, in the owner's words) and
  `not_worth_flagging` lines. Use them to look harder where the owner says failure lives.
- **What a brief can never do.** It can add scrutiny, cap nits and skip paths. It cannot lower a
  severity, waive a critical or high, change the verdict rule, or skip a refused path, and it does
  not relax the evidence rule: every finding still carries `file` and an integer `line`.
- **`unavailable`** (plan exit 2): review everything, no cap, and say `brief: unavailable` in
  `summary`.

## Review depth

`scripts/review_depth.py plan --repo <sandbox> --base <base> [--evid <EVID_ROOT>/<KEY>]` runs before
the fan-out and decides, without a model, how many specialist spawns run. It never decides which
dimensions are looked at: every level covers all six lenses, and security is never off.

| Level | Passes the plan returns |
|---|---|
| `quick` | `combined` (all six specialists, `files: review`) |
| `standard` | `frontend`, `backend`, `data`, `security`, `tests`, `perf` (one each, `files: review`) |
| `deep` | the six + `security-risk-paths` (`security`, `files: risk`) |

`files: review` is the reviewer brief's `review_files`; `files: risk` is the plan's `risk_files`, or
every changed file when the risk is not path-attributed (an unknown, intake `sensitive`, a
conventions edit, `--min-depth`).

The `reasons` vocabulary is closed; the level is the highest any reason demands and is only ever
raised:

| Reason | Level | Meaning |
|---|---|---|
| `small-diff` | quick | the only reason: within loop-limits `review_depth`, no risk, no unknown |
| `size-over-quick` | standard | more files or changed lines than the quick limits |
| `changed-lines-unknown` | standard | a binary or unreadable file, a change with no changed line (mode-only, an empty new file), or a `--changed-file` override |
| `empty-diff` | standard | nothing changed |
| `in-loop-review` | standard | `--evid` given: a loop fix behind a review gate is never quick |
| `min-depth` / `prior-record` | raised | the caller's `--min-depth`, or a deeper level already recorded |
| `blast-radius` | deep | the R6 denylist, adopter list or framework floor, either side of a rename |
| `shared-core` | deep | a `shared_core_globs` match |
| `sensitive-area` | deep | intake.json `sensitive: true` |
| `conventions-changed` | deep | the diff edits the conventions file (it may not strip its own denylist) |
| `r6-scan-unavailable`, `conventions-unreadable`, `limits-invalid`, `seam-unreadable`, `diff-unavailable` | deep, `unavailable` | an unknown: plan exit 2, the deep plan still printed |

`r6-scan-unavailable` also fires when a changed path holds a newline or control character (the R6
scan reads one path per line, so such a name could split a denylisted path into harmless pieces) or
when the scan names a path that is no changed file: a hit is never dropped. `seam-unreadable` also
fires when `--evid` is not a directory or intake.json's `sensitive` is not a boolean. `record`
refuses (exit 2, draft untouched) a draft whose existing `depth` block names no valid level.

**The combined pass (quick).** ONE fresh `Agent` gets all six specialist briefs below, the shared
rules, the files and the brief's area lines, exactly as six spawns would. It reviews through each
lens in turn and returns one strict JSON list in the same finding shape; every finding carries the
`dimension` of the lens that found it (never `combined`), so the ballot, the validators and the
quorum treat it like any other candidate. A lens with nothing to say returns nothing; it is not
skipped.

**The `security-risk-paths` pass (deep).** One more fresh `Agent` with the security brief only. It
reads each risk file WHOLE, not just its hunks, plus the callers of anything the diff changes in it,
because a two-line change to an auth config or a shared module is dangerous for what it reaches, not
for its size. Findings still anchor on a changed line (`file` + integer `line` in the diff) with
`dimension: security`; a hazard it sees only outside the diff is a `## Noticed, not changed` bullet,
not a finding. A denylisted file is CRITICAL on sight here as everywhere.

## Shared rules for every specialist

- **High signal only.** Flag what is clearly real: won't compile, wrong result, an unambiguous
  violation of a cited rule, exploitable security, scale-failing performance. Not style, not taste,
  not "I would have done it differently". When unsure, return nothing.
- **Only the diff.** A problem the diff did not introduce is *pre-existing*; it is not a finding
  (the validators send serious ones to `## Noticed, not changed`, via `tally`'s `noticed_candidates`). A hazard the diff newly exposes
  — a new caller of an unsafe function, a new path around an existing guard — **is** a finding.
- **Confidence at or above the floor**, integer, honest. The floor is `loop-limits.json`
  `review_validation.confidence_floor` (80 as shipped), delivered to you from the `ballot` output, not
  carried in your head. 80 means "I re-read the surrounding code and I would bet on it"; a hunch is 40
  and is not emitted. Your number only gets a candidate onto the ballot: the validators form their own.
- **Fingerprint** = `sha1(dimension + file + normalized issue text)`. `finding_votes.py ballot` dedups
  across specialists and across chunks mechanically (the same `finding_id`, or the same file +
  integer line + `bug_class` other than `other`): two specialists reporting the same line keep the
  higher-severity one.
- **Empty is a valid answer.** A specialist whose surface the diff doesn't touch returns `[]`.
- **Finding shape** (the `findings[]` item of `contracts/dev-code-review.json`):
  `{ dimension, severity: critical|high|medium|low, confidence, file, line, bug_class, instances?,
  issue, rule_id?, suggestion?, fingerprint }`. `rule_id` is required when the finding is a convention
  violation.
- **Flag the class, list the instances.** The same mistake in three places is ONE finding whose
  `bug_class` names the class and whose `instances` lists the other two `{file, line}` pairs, so
  the fixer fixes every instance instead of the one line a comment anchors to. The dedup
  fingerprint above is per review; the cross-round fingerprint `review_rounds.py` records is
  file + `bug_class` (line excluded), so keep a still-open finding's file and class stable across
  re-reviews.

## The bug_class vocabulary (contract v1.4.0, closed)

Closed on purpose: two blind reviewers in different rounds must pick the same word for the
stagnation fingerprint to hold. Pick the most specific class that fits; `other` only as a last
resort (it falls back to a wording-sensitive fingerprint).

| bug_class | One line |
|---|---|
| `null-or-undefined` | a value that can be null/undefined/missing is used without a guard |
| `unhandled-error` | an error path is not handled: a throw, rejection or error return escapes or is ignored |
| `swallowed-error` | an error is caught and dropped, hiding a failure the caller needed to see |
| `race-or-ordering` | correctness depends on timing or ordering that is not guaranteed |
| `boundary-or-off-by-one` | a limit, index, range or empty/one/many edge is wrong |
| `wrong-logic` | the code computes the wrong result on an ordinary input (inverted condition, wrong operator, wrong field) |
| `missing-validation` | input the guidelines or the contract require to be checked is not checked |
| `injection` | untrusted input reaches a query, shell, template or path unescaped |
| `authz-or-authn-gap` | an authentication or authorization check is missing, weakened or bypassable |
| `secret-or-data-exposure` | a secret, credential or another tenant's/member's data is written, logged or returned |
| `resource-leak` | a handle, connection, listener, timer or subscription is never released |
| `performance` | work that fails at scale: N+1, unbounded fetch, per-item work that was batched, blocking I/O on a hot path |
| `state-duplication` | the same state now has two owners or two sources of truth that can drift |
| `api-contract-break` | a response, signature or schema changes shape without its callers being updated |
| `missing-or-weak-test` | the fixed path has no test that would catch the defect, or a test was loosened/skipped |
| `convention-violation` | a cited `conventions.rules[]` or guidelines-file rule is broken (`rule_id` required) |
| `symptom-masking` | the diff greens the replay by hiding the symptom (R3; the masking specialist) |
| `accessibility` | an interactive element lacks an accessible name, role or keyboard path the guidelines require |
| `other` | none of the above fits; say why in `issue` |

## The six

| dimension | Surface | Flags (examples, not an exhaustive list) |
|---|---|---|
| **frontend** | UI code: components, templates, styles, client state, routing, accessibility | render that cannot work (unbound reference, wrong prop type that will throw); state mutated outside the pattern the repo uses everywhere else; an interactive element with no accessible name / keyboard path when the repo's guidelines require one; a hand-rolled control where the guidelines name a shared one |
| **backend** | Server code: handlers, services, validation, error handling, background jobs | a request path that returns success on an unhandled branch; validation the guidelines require and the handler skips; a new endpoint with no authorization check where siblings have one; a contract change (response shape) with no caller update |
| **data** | Persistence: queries, schema, migrations, caching, serialization | a query that can return the wrong rows (missing tenant / workspace scope, wrong join); a migration that loses data or is not reversible; N+1 introduced in a loop; a cache key that omits a dimension the value depends on. A diff touching a `blast_radius_denylist_globs` path is CRITICAL on sight (R6) |
| **security** | Trust boundaries, auth, secrets, injection, data exposure | user input reaching a query / shell / template unescaped; an authorization check removed or weakened; a secret-looking literal; a new field in a response that leaks another Member's or Workspace's data; a log line that writes credentials or tokens |
| **tests** | Test coverage and test honesty | the fixed path has no test that would have caught the original defect; a test skipped, focused (`.only`-style) or deleted; an assertion loosened so the test cannot fail; a test that asserts the mock, not the behavior |
| **perf** | Scale and latency | work moved into a per-item loop that was batched; an unbounded fetch where a page existed; blocking I/O on a hot path; a new synchronous call to an external system in a request handler. Flag only what fails at scale, not micro-optimizations |

The **tests** specialist has one loop-specific extra: when `fix.json` names a failure class, it
checks that a test now exists for *that* class, not just "some test changed".

## The validators (independent, fresh Agent spawns, a quorum)

A producer never grades its own work, and that holds inside the reviewer too: the specialists
produce candidates, fresh validators vote on them, and the orchestrating reviewer only counts,
through `scripts/finding_votes.py`. The numbers are `loop-limits.json` `review_validation`
(`validators` 3, `quorum` 2, `confidence_floor` 80 as shipped; argued, not measured; tuned by an
eval replay, `eval-set.md`).

**The orchestrator** merges every specialist's output into
`<EVID_ROOT>/<KEY>/review-validation/candidates.json` (`{"findings": [...]}`) **without judging a
candidate itself**: it does not drop, merge, re-rate or reword one. Then:

1. `python3 scripts/finding_votes.py ballot --dir <that dir>`: ids, mechanical dedup, the floor,
   and `ballot.json` with the specialist's confidence, suggestion, fingerprint and harden stripped.
   Status `no-candidates` = no candidate reached the floor: spawn no validator, go to step 3.
2. Spawn `validators` fresh `Agent` validators **in ONE message** (parallel). Each receives only
   `ballot.json`, the sandbox path, the base and this section, and writes `votes/v<i>.json`
   (`{"validator": "v<i>", "votes": [{finding_id, verdict, confidence, reason, serious?}]}`).
3. `python3 scripts/finding_votes.py tally --dir <that dir> --review <draft review.json>`. It writes
   the kept findings, `critical`, `high`, `masking` and `validation` into the draft. A finding is
   kept only when the specialist's confidence and at least `quorum` confirms are at or above the
   floor. Never edit those fields by hand afterwards.

**Each validator**, for every finding on the ballot:
1. Re-reads the code at `file:line` in the sandbox and confirms the line is in the diff's added or
   changed hunks (`git diff <base> -U0`). Not in the diff → verdict `pre-existing`, with
   `serious: true` when it deserves a human's eyes (a missing authorization check, a data-loss path).
2. Confirms the claim is true as written. A convention finding needs a `rule_id` that exists in
   `conventions.rules[]` or a quotable line of the guidelines file; otherwise `reject`.
3. Gives its OWN integer confidence (0-100). `confirm` below the floor does not count.

**A validator may not read:** other validators' vote files, `candidates.json`, the specialist's
confidence or suggestion, the `fix.json` fields `design_decisions` / `notes` / `confidence`, or
`evals/cases`. The diff and the code are fair game.

**A validator that fails or returns nothing** is a missing ballot: `tally` reports it under
`missing_validators` and never counts it as a confirm; a malformed file or a duplicate validator id
is not a returned ballot either, and a malformed vote inside one is an abstain. Re-spawn a
validator only for a transient reason (an infra error, a timeout). `tally` exit 2
(`validation-unavailable`: fewer than `quorum` usable ballots) means **no review.json is written**,
and the skill reports `blocked: validation-unavailable` to the recipe. Unvalidated findings are
never passed through.

**What happens to a rejected finding.** It is recorded nowhere as a finding: `validation` carries
counts only (`candidates`, `kept`, `rejected`, `split`). The working dir is evidence of this run,
not a suppression store; nothing reads it to drop a later finding. A serious pre-existing candidate
that at least `quorum` validators marked `pre-existing` with `serious: true` comes back in
`tally`'s `noticed_candidates`: write it as one bullet under `## Noticed, not changed` in
`review.md`. False positives that slip through are measured later from what humans did with the
posted threads (`dev-pr-feed dispositions`).

Do not trim low findings to fit a brief's nit cap. Report every kept low finding;
`review_brief.py finalize` withholds the ones over `max_nits` (lowest confidence first) and records
them as `withheld`, so the choice is mechanical and auditable, not a specialist's.

What survives is `findings[]`; `tally` fills `critical` / `high` / `masking`; the verdict rule in
`SKILL.md` §4 derives from them. `finding_votes.py check` re-derives all of it (exit 0 / 1 / 2).
On a re-review, an earlier finding carried with `resolved: true` needs no votes (`tally` keeps it);
every open finding does.
