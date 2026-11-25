# Skill I/O contracts

This directory is the **single source of truth** for the JSON shapes the loop skills emit and the
runner / control plane consume. A skill's `SKILL.md` describes *how* it works; the schema here is
*what it must emit*. If a skill's output and a schema here disagree, the schema wins and the skill is
broken — validate in CI (`contracts/validate.py`) before shipping.

Nothing here names a vendor. Ticket keys match `project.ticket_key_pattern` (the examples use
`PROJ-123`), ids for the test-management system come from `adapters.test_management.config`, evidence
URLs come from the `evidence_store` adapter, and the evidence root is `evidence.root` in
`.claude/loops/stack.config.json`. Swapping a tracker, a VCS host or a CI system changes no contract.

## The contracts

| Contract file | Ver | Emitted by | Written to | Consumed as | Branches on |
|---|---|---|---|---|---|
| `dev-intake.json` | 1.2.0 | `dev-intake` | `<evid_root>/<key>/intake.json` | intake / routing (stored verbatim) | `recipe` |
| `context-pack.json` | 1.0.0 | `dev-intake` (+ `infra-worktree` fills `environment`) | `<evid_root>/<key>/context.json` | read by every downstream skill | — |
| `qa-repro.json` | 1.5.0 | `qa-repro` | `<evid_root>/<key>/repro.json` | `repro_evidence` (stored verbatim) | `status` |
| `qa-verify.repro-judge.json` | 1.2.0 | `qa-verify` mode=`repro-judge` | `<repro_dir>/judge.json` | `judge_evidence` | `verdict` |
| `dev-fix.json` | 1.4.0 | `dev-fix` | `<evid_root>/<key>/fix.json` | router: `status`; ship: `design_decisions` with `must_read: true` | `status` |
| `qa-verify.json` | 1.0.0 | `qa-verify` mode=`fix-verify` (default) | `<evid_root>/<key>/verify.json` | the checker node; `dev-pre-pr` fails closed on it | `verdict` |
| `qa-verify.collateral.json` | 1.0.0 | `qa-verify` fix-verify step 2b | `<repro_dir>/collateral.json` | the `no_collateral` must-have | `status` |
| `qa-regression.json` | 2.1.0 | `qa-regression` | `<evid_root>/<key>/regression.json` | router | `status` + `next_action` |
| `dev-code-review.json` | 1.5.0 | `dev-code-review` | `<evid_root>/<key>/review.json` | router; `dev-harden` reads findings' `harden` blocks; `reviewer_eval.py score` locates findings by `file` + `line`; `review_rounds.py record` fingerprints findings by `file` + `bug_class`; `review_brief.py finalize` / `check` write and re-check `headline` + `brief` | `verdict` |
| `dev-loop-defect.review-rounds.json` | 1.0.0 | `review_rounds.py record` (proposed) | `<evid_root>/<key>/review-rounds.json` | `recipe_router.py --review-rounds` (stagnation); the recipe reads `rounds_to_pass` for the `review_rounds_to_pass` metric | none |
| `dev-loop-defect.meta-monitor.json` | 1.0.0 | `meta_monitor.py` (proposed) | a caller-chosen path (`--out`), or stdout | a person or a runner deciding whether loops are healthy; report-only unless `r8_meta_monitor.trip_mode` is armed | `decision` |
| `dev-code-review.eval-case.json` | 1.0.0 | a human (seed cases: the kit), checked by `reviewer_eval.py check` (proposed) | `.claude/skills/dev-code-review/evals/cases/<id>/case.json` | `reviewer_eval.py` check / score; never the reviewer | `kind` |
| `dev-code-review.eval-score.json` | 1.0.0 | `reviewer_eval.py score` (proposed) | a caller-chosen path (`--out`), or stdout | a person or a later job comparing reviewer versions | `status` |
| `dev-harden.json` | 1.1.0 | `dev-harden` | `<evid_root>/harden/<fingerprint>/harden.json` | `collect_candidates.py` and `collect_misses.py` (decided skip); a human `--apply`; validate.py DEV-HARDEN-L1 (ladder) + DEV-HARDEN-M1 (a review miss says what the reviewer lacked, v1.1.0) | `status` |
| `dev-pre-pr.json` | 1.3.0 | `dev-pre-pr` | `<evid_root>/<key>/pre-pr.json` | router; infra-ship `commit_binding.py check` | `status` |
| `qa-run-case.json` | 1.0.0 | `qa-run-case` (`DRIVER=cli` default · `chrome`) | `<evid_root>/<key>/case-run.json` | grade + drift (stored verbatim); `steps` → per-step rows | `status` |
| `qa-run-chatbot.json` | 1.0.0 | `qa-run-chatbot` | `<evid_root>/<key>/chatbot-run.json` | per-case rows | `status` |
| `qa-diagnose.json` | 1.0.0 | `qa-diagnose` | `<evid_root>/<key>/diagnose.json` | the run record's diagnosis (propose-only) | `failure_layer` |
| `qa-loop-release-ac.json` | 2.0.0 | `qa-loop-release-ac` | `<evid_root>/release-ac/<version>/ac-matrix.json` | QA / release-owner facing | `summary.verdict` |
| `dev-onboard-survey.json` | 1.0.0 | `dev-onboard-survey` | `<evid_root>/onboard/<project>/survey.json` | how / why fan-out; `dev-onboard-map` seeds areas | none (a data artifact) |
| `dev-onboard-card.json` | 1.0.0 | `dev-onboard-how` · `dev-onboard-why` | `<evid_root>/onboard/<project>/cards/<area>.<lens>.json` | `dev-onboard-map` (after `check_card.py`) | `lens` |
| `dev-onboard-map.json` | 1.0.0 | `dev-onboard-map` (proposed) | `<evid_root>/onboard/<project>/map.json` | `qa-onboard-quiz` examiner; write back | none (a data artifact) |
| `qa-onboard-quiz.json` | 1.0.0 | `qa-onboard-quiz` | `<evid_root>/onboard/<project>/quiz/round-<n>/quiz.json` | the onboarding loop's router | `status` + `failure_class` |
| `dev-loop-onboard.json` | 1.0.0 | `dev-loop-onboard` (`onboard_router.py`, proposed) | `<evid_root>/onboard/<project>/quiz/round-<n>/route.json` | the recipe; `mark_verified.py` | `action` |

`status: "ratified"` schemas are locked; `status: "proposed"` ones follow every convention below and
graduate on the repo owner's sign-off. Treat a proposed shape as stable intent, not as locked.

**Path convention (pinned):** every pipeline step writes `<EVID_ROOT>/<KEY>/<step>.json` into **one
shared per-ticket directory**. `EVID_ROOT` is `evidence.root` from stack.config (default `runs/`, repo-root,
gitignored). `dev-escalate --task-dir` receives exactly that directory. There is no per-skill split root.

## Conventions every contract follows

- **`additionalProperties: false`** pins the key set wherever the consumer stores the object verbatim. The
  exceptions are deliberate and documented in the schema (`dev-fix.json` is a free-emitting rich skill, so
  only its machine-read core is pinned).
- **One branch field per artifact.** A consumer branches on exactly one enum (`status` / `verdict` /
  `recipe` / `failure_layer` / `next_action`) and ignores the rest. Everything else is evidence.
- **A tooling failure writes NO artifact.** If a skill cannot run at all (browser unresolvable, app
  unreachable, timeout), it writes nothing; the runner records "skill did not write its artifact" →
  `ok:false`, which the control plane routes as *inconclusive-style* (retry / escalate) — **never** as a
  substantive verdict such as a repro rejection. That `ok:false` is undifferentiated by design.
- **Reject vs. retry is triage-critical.** Every evaluator contract separates a *real negative* from
  *could not tell*: `qa-repro` `not_reproduced` vs `inconclusive`; `repro-judge` `fail` vs `inconclusive`;
  `fix-verify` `fail` vs `blocked`. Only the real negative may reject or re-fix.
- **`media[]` is optional everywhere.** Evidence media is best-effort; an artifact without the key validates.
- **Every artifact may carry `attempt`, `retry_reason` and `timing`.** `contracts/emit.py` stamps them. A retry
  needs a transient-failure reason (flaky-store match, infra error, timeout, oracle exit 2); a deterministic
  failure escalates or changes strategy instead of repeating.
- **Judges run blind.** `qa-verify` and `dev-code-review` artifacts carry `judged_blind` and `model`; a judge
  that produced the thing it grades, or runs on a weaker model than the producer, yields an invalid verdict,
  not merely a weaker one.

## Per-contract notes

### `qa-repro.json` — the reproduction receipt

Required core `{status, surface, symptom_quote, app_map_writes, replay_spec}` plus optional `media`,
`brain_writes`, `attempt`, `retry_reason`, `timing`.

| `status` | Meaning | Triage action |
|---|---|---|
| `reproduced` | Bug confirmed to reproduce | proceed (fix / judge) |
| `not_reproduced` | Ran the repro, bug did **not** occur — a real negative | **may REJECT** the ticket |
| `inconclusive` | Could **not** determine (tooling failed / needs-info / env broke) | **must NOT reject** — retry or escalate |

The skill's finer needs-info vs tooling-failed distinction rides in `brief.json.outcome_detail` (non-contract).
`replay_spec` names the oracle file in the evidence dir — always `"replay-oracle.sh"`, the headless
`browser.oracle_driver` replay (grader `replay-grader.js` beside it) that exits 0 green · 1 red · 2 blocked.
There is no spec file. `app_map_writes` is receipts-checkable (see *Receipts mode* below).

### `qa-verify.repro-judge.json` and `qa-verify.json` — two modes, opposite polarity

`repro-judge` writes `judge.json`; `pass` means **the bug IS present** (independently confirmed). `fix-verify`
writes `verify.json`; `pass` means **the bug is GONE** (the replay went red → green against the fixed build).
The two files never overwrite each other and are never validated against each other's schema. A `fix-verify`
`pass` with `masking_suspected: true` is a fail (R3): `dev-pre-pr` blocks on it and the recipe must not write a
fix lesson from it.

### `dev-intake.json` and `context-pack.json` — the front door

`intake.json` is an 8-key required core (`key, issue_type, recipe, area, components, risk_class, confidence,
branch_name`) plus an honestly-modeled optional tail; `recipe` is the branch field. `context.json` is written
once by intake (sanitized ticket text, tier, model plan, retry caps, brain slices) and completed by
`infra-worktree` (the `environment` block); downstream skills read it instead of re-exploring, and record
anything it lacked in `gaps`.

**The forward-compat flag.** `dev-intake` is a free-emitting LLM skill; `additionalProperties: false` is
enforced and every sample passes, but a future run that invents a new situational key would fail validation.
Two honest reconciliations: **(A, recommended)** tighten `dev-intake/SKILL.md` to emit exactly the modeled
superset (as was done for `qa-repro`), making the schema a real gate; **(B)** relax the optional tail to
`additionalProperties: true` and give up catching stray keys. (Decision pending — not applied here.)

### `dev-fix.json` — the candidate fix

Required core `{status, root_cause, is_root_cause, confidence}`; `status: blocked-blast-radius` is the R6
forced-human-gate outcome. `design_decisions` items are a string, a recorded decision, or a **must-read** item
(`must_read: true` requires an enumerated `must_read_reason` and a `reviewer_must_confirm` ask); only
must-read items can become PR comment threads, capped by `.claude/loops/loop-limits.json`. `dev-fix` only
*reads* the brain: `lessons_consulted` names what it recalled and `pending_fix_lesson` is the lesson the
recipe writes after a green verify — never before.

### `qa-regression.json` — the build + unit-test gate

Required `{status, area, results, next_action}`. **QA-REGRESSION-C1:** `green` / `ship-ok` require
`results.total >= 1` and a non-empty `area_slice_run` — a cross-field rule in `validate.py`, not only prose,
because a zero-test green once shipped. Counts are integers; `'n/a'` fails by construction. The gate drives
no browser and runs no spec files.

### `dev-code-review.json` and the reviewer eval set

`review.json` v1.3.0 adds an OPTIONAL integer `findings[].line`: the new-file line of the added or
changed line the finding is about. It stays optional so every older artifact validates, but a finding
without an integer line (or with a range string, `EXAMPLE-bad-review-line`, which fails) is
unlocatable and can never count as a catch when the reviewer is scored.

`review.json` v1.4.0 adds OPTIONAL `findings[].bug_class` (a closed vocabulary of 19 words, `other`
last; one line per class in `dev-code-review/references/specialists.md`) and `findings[].instances`
(`[{file, line}]`, the other places in the diff where the same class occurs), so one finding covers
the class and every instance gets fixed. `EXAMPLE-bad-review-bug-class` (a free-text class) fails.
`review_rounds.py record` turns each distinct review.json into one round of
`dev-loop-defect.review-rounds.json`, fingerprinting every open finding by normalised `file` +
`bug_class` (line excluded on purpose; normalised issue text when the class is absent or `other`).
`recipe_router.py --review-rounds` escalates with `failure_class: review-stagnation` when one
fingerprint is open in the last `review_convergence.stagnation_fix_cycles` + 1 rounds.

`review.json` v1.5.0 adds OPTIONAL `headline` and `brief`, both written by
`dev-code-review/scripts/review_brief.py finalize`, never by hand. `headline` is the count lead
(`0 findings: nothing needs fixing.` or `N finding(s) (<c> critical, ...).`, open findings only) and
`summary` starts with it. `brief` records the per-area reviewer brief (conventions `review_brief`):
the changed files a `skip_paths` glob skipped, the skips refused because the file is R6-denylisted
or shared core (`skip_refused`, reason `blast-radius` | `shared-core`), the areas that matched, and
how many low findings the nit cap withheld (`findings[].withheld` / `withheld_reason`, now
documented). `validate.py` adds **DEV-REVIEW-H1** (the headline's integer equals the open findings,
not resolved and not withheld, and `summary` starts with the headline) and **DEV-REVIEW-B1** (with
`brief.status` applied, open lows never exceed `brief.max_nits` and no file is both skipped and
refused; with `unavailable`, nothing is skipped or withheld). Both fire only when the fields are
present, so every older review validates. `EXAMPLE-bad-review-brief` fails on both.

`dev-code-review.eval-case.json` is the answer key of one eval case (diff + base tree + optional
fix.json beside it). **DEV-REVIEW-EVAL-K1** (`validate.py`): `kind: bad` needs at least one bug with
`line_end >= line_start`, `kind: clean` names none, and any case whose `source.origin` is not `seed`
carries `source.ref` and `source.confirmed_by`, a person's signature on the location or on "clean".
`dev-code-review.eval-score.json` is the scorer's output: recall, false-flag rate, per-case results.
It is a pure function of the case set, the reviews and `loop-limits.json reviewer_eval`: no timing
stamp, no model field, sorted keys, so it is not written through `emit.py`; the scorer validates it
itself before printing. Compare two scores only at equal `case_set.sha256`. See
`.claude/skills/dev-code-review/references/eval-set.md`.

### `dev-pre-pr.json` — the readiness verdict

Required `{status, next_action, checks, blockers, warnings, summary}`. `sources` is the freshness
provenance for every adopted answer (`fresh | stale | missing | ran-locally`): a `regression.json` older
than a changed source file is `stale` and re-run, never trusted. `checks.fix_verified` must be `pass` (or
`not-applicable`, which a loop run may never use) or the verdict is `blocked` — at every tier. `skipped` is not
`pass`. Heuristic violations block only when `confirmed: true`. The scanner's rules come from
`.claude/loops/conventions.json`; the samples cite its generic default rule ids.

v1.3.0 (additive) adds two optional blocks. `risk` is `scripts/risk_reclass.py`'s output copied verbatim:
`triage_tier` (null = untiered, behaves as T1), `diff_tier`, `tier`, `escalated`, `reasons`, `facts`,
`human_gate {demanded, triggers}` (closed vocabulary `symptom-only-fix | tests-weakened | blast-radius |
sensitive-area`), `human_approvals_required` (integer ≥ 1) and `agent_approval_counts_as_human` (pinned
`false` with `enum: [false]` — an agent approve never fills a human slot). `judged` is
`scripts/commit_binding.py stamp`'s content fingerprint (`head_commit`, `change_fingerprint`, plus
`merge_base`, `dirty`, `files`). `validate.py` adds three cross-field rules: **DEV-PRE-PR-T1** the tier is
never below `triage_tier` or `diff_tier`; **DEV-PRE-PR-T2** `escalated` equals tier > (`triage_tier` or
T1); **DEV-PRE-PR-R1** `ready` is invalid while `escalated` or `human_gate.demanded` is true. Both blocks
are optional in the contract so older artifacts stay valid; inside a loop `infra-ship`'s
`commit_binding.py check` is what requires them (`readiness-unbound`) and proves the commit matches
`judged` (`stale-readiness`).

### `qa-run-case.json`, `qa-run-chatbot.json`, `qa-diagnose.json` — the control plane's QA lane

`case-run.json` grades one case `PASS | FAIL | PARTIAL | BLOCKED | MANUAL` with per-step `PASS | FAIL | INFO
| BLOCKED` and an orthogonal `drift[]` label-drift flag; its `media[]` uses the run vocabulary `gif | mp4 |
screenshot`. `chatbot-run.json` is the conversational twin (worst-of grade, no `MANUAL`, a per-case
`failure_class` for classify-before-adapt). `diagnose.json` is **propose-only**: a unified diff as a string
(possibly empty, with `notes` saying what a human must investigate) and `failure_layer ∈ {app_bug, test_bug,
environment, data}` as the routing key. The skill has no `Edit` tool, so propose-only is structural.

### The release-AC matrix — `qa-loop-release-ac`

`ac-matrix.json` is one row per Gherkin scenario (or explicit AC bullet) in a release's tickets, joined
to the `case-run.json` qa-run-case wrote for it (`rows[].case_dir`) or to qa-run-chatbot's grade. Release
naming comes from `integrations.release_ac.release_name_pattern`; deploy tracks from `deploy.tracks[]`; the
issue tracker and CI through their adapters. Enum vocabulary is vendor-neutral, and `summary.verdict` is
`all-ac-pass` only when every scenario was judged and passed.

## Evidence media — the `media[]` capture-time upload

`qa-repro` and `qa-verify` upload the before/after recordings (GIF **and** MP4) **at capture time** through
the `evidence_store` adapter and put the returned URLs in `media[]`, so the control plane can stream the
evidence the moment it is produced, not only after a PR exists. The single upload helper is
`.claude/skills/qa-repro/scripts/upload_evidence_media.sh`; `qa-verify` and `qa-run-case` call it.

- A URL in `media[]` must be fetchable **with no `Authorization` header**: a signed read URL from a hosted
  provider, or a `file://` URL from the built-in `local` provider.
- Credentials never appear in an artifact or a skill. A provider's config holds the **name** of the env var
  that carries its credential, never a value.
- `media[]` is best-effort: if no provider is configured or an upload fails, the skill omits the key and
  the artifact still validates.
- A browser-based control plane fetching media cross-origin needs the store to allow its origin. That is
  configured once on the store, out of band, by a human — never by a skill.

## How the runner invokes a skill

`run_skill(skill, key, inputs)` → the runner runs `skill` on `key`; the skill writes
`<inputs.evid_root>/<key>/<step>.json`; the runner returns that JSON as `output`. Two runner duties the
contracts depend on:

1. **Clear any stale `<step>.json` before the run**, so a fail-fast (or hang) that writes nothing can never be
   reported `ok:true` off a leftover artifact.
2. **Enforce a subprocess timeout**, so a hang becomes `ok:false` (inconclusive-style) rather than a stall.

Claude Code is the runner by design (`runner.claude_bin`, `runner.config_dir` in stack.config). The model per
step comes from `models.*`; a verifier (`repro-judge`, `fix-verify`, `dev-code-review`) runs on `models.verifier`
and is **never downgraded to save tokens** — a cheap judge that greens a non-reproduction poisons the loop. The
skills declare no model field, so the runner's `--model` is authoritative.

```bash
# Step 0 — intake (no browser). Writes <evid_root>/<key>/intake.json + context.json.
claude -p "intake PROJ-123. EVID_ROOT=./runs/" \
  --model "$MODEL_STRONG" \
  --allowedTools "Read,Glob,Grep,Bash,ToolSearch"

# Step 1 — reproduce. Writes repro.json. The replay oracle runs through Bash, so browser.oracle_driver
# (playwright-cli) must be on PATH; sign-in reads the auth_provider env files.
CHROME_DEVICE_ID="$CHROME_DEVICE_ID" \
claude -p "run qa-repro on PROJ-123. APP_URL=$APP_URL EVID_ROOT=./runs/" \
  --chrome --model "$MODEL_STRONG" \
  --allowedTools "Read,Write,Edit,Glob,Grep,Bash,Agent,ToolSearch,mcp__claude-in-chrome__*"

# Step 2 — independently judge the repro. Writes <repro_dir>/judge.json.
CHROME_DEVICE_ID="$CHROME_DEVICE_ID" \
claude -p "run qa-verify in mode=repro-judge on PROJ-123. REPRO_DIR=./runs/PROJ-123 APP_URL=$APP_URL" \
  --chrome --model "$MODEL_VERIFIER" \
  --allowedTools "Read,Write,Edit,Glob,Grep,Bash,Agent,ToolSearch,mcp__claude-in-chrome__*"

# Agentic case run — CLI driver (default, headless, no --chrome). The case travels inline in a
# runner-written case.json; auth is a 0600 bundle under AUTH_DIR, never in the prompt. EVID_ROOT is ABSOLUTE.
claude -p "run qa-run-case on case-4012-run-9001. CASE_FILE=/abs/evid/case-4012-run-9001/case.json APP_URL=$APP_URL EVID_ROOT=/abs/evid AUTH_DIR=/abs/evid/case-4012-run-9001/auth DRIVER=cli" \
  --model "$MODEL_STRONG" \
  --allowedTools "Read,Write,Edit,Glob,Grep,Bash,Agent,ToolSearch"

# Diagnose a failed run — PROPOSE-ONLY (no Edit in the allow-list; no --chrome).
claude -p "run qa-diagnose on diag-<record_id>. CONTEXT_FILE=/abs/evid/diag-<record_id>/context.json REPO_DIR=/abs/<target-repo> EVID_ROOT=/abs/evid" \
  --model "$MODEL_STRONG" \
  --allowedTools "Read,Write,Glob,Grep,Bash,Agent,ToolSearch"
```

`APP_URL` is `app.url` (or `app.environments.<name>.url`) from stack.config; `MODEL_STRONG` / `MODEL_VERIFIER`
are `models.strong` / `models.verifier`. The ticket itself is fetched through the `issue_tracker` adapter, so
no tracker tool appears in any allow-list; a provider that ships its own tools declares them in its manifest
and the runner merges them in.

### Browser stages: `--chrome` and deterministic instance selection

`--chrome` is required on every stage that drives the live app with `browser.live_driver` (claude-in-chrome);
without it the browser tools are not loaded. Headless runs have no human to answer a prompt, so the browser
instance must be chosen deterministically:

- **Recommended posture: connect exactly one browser instance** on the runner host. Then no hint is needed.
- Otherwise set **`CHROME_DEVICE_ID`** to the exact instance (as an env var on the runner process, or a
  per-request prompt token the runner adds from its inputs — the two are separate). The skills select by id
  **without broadcasting**; they never call a human-blocking pairing or disambiguation prompt in an autonomous run.
  The mechanic lives once in `.claude/skills/qa-repro/references/browser-cookbook.md`.
- **Fail fast, never hang.** If a browser is needed and the instance cannot be resolved (two connected and no
  hint, or a hint that is not connected), the skill exits with a one-line diagnostic naming the connected set
  and writes **no artifact** → `ok:false` → inconclusive-style. A `repro-judge` on a presentation/DOM defect with a
  captured before-video needs no live browser at all, so the ambiguity never arises there.

## Emit an artifact without hand-writing JSON

```bash
python3 contracts/emit.py --contract contracts/qa-verify.json --out runs/PROJ-123/verify.json \
    --set verdict=pass --set confidence=90 --set masking_suspected=false \
    --set-json must_haves='[{"check":"replay","result":"pass"}]' --started-at "$STARTED"
```

`emit.py` builds the object from typed `--set` / `--set-json` (scalars coerced from the schema), stamps
`timing` / `attempt` / `retry_reason`, **validates before writing** (a violation never lands on disk, exit 1 names
the key), and `--dry-run` prints without touching the file.

## Validate any output against its contract

```bash
python3 contracts/validate.py contracts/qa-repro.json              <path>/repro.json
python3 contracts/validate.py contracts/qa-verify.repro-judge.json <path>/judge.json
python3 contracts/validate.py contracts/dev-intake.json            <path>/intake.json
python3 contracts/validate.py contracts/dev-fix.json               <path>/fix.json
python3 contracts/validate.py contracts/qa-regression.json         <path>/regression.json
python3 contracts/validate.py contracts/dev-code-review.json       <path>/review.json
```

Exit 0 = contract-true (prints the byte-for-byte key set); exit 1 = violation (missing/extra keys, nested and
top-level, bad enums, type/range errors); exit 2 = bad usage or an unreadable/malformed input (a clean error line,
not a traceback). The validator is stdlib-only, recurses into object properties and array `items`, supports
`anyOf` and the `null` type, and enforces the QA-REGRESSION-C1 cross-field rule. The full sample matrix runs via
`.claude/loops/tests/test_contracts.sh` (part of `run_all.sh`), which also asserts that
`samples/EXAMPLE-bad-green-zero-tests/` **keeps failing** its contract.

### Receipts mode — is the artifact's brain-write claim TRUE?

A schema check proves an artifact is well-formed; it cannot prove `app_map_writes: [...]` was ever more than
the agent's recollection. The kb CLI ledgers every mutating verb into `kb_writes` inside the write's own
transaction, so the claim is falsifiable:

```bash
python3 contracts/validate.py --receipts PROJ-123 [artifact.json|dir ...] [--evid-root DIR] [--kb PATH] [--since 7d] [--strict] [--json]
```

Reads `app_map_writes` + `brain_writes` (repro.json), `flakiness_writes` (regression.json), `lessons_consulted` +
`pending_fix_lesson` (fix.json) and the `verdict` (verify.json / judge.json), then compares them with
`kb receipt --ticket <KEY>`. Exit 0 no drift · 1 DRIFT (a claimed write the brain never received) · 2 usage ·
3 UNDETERMINED (brain unreachable, kb CLI missing, or `kb_writes` not created yet — deliberately **not** 1: a dead
brain is not evidence that an agent lied). Queued outbox writes explain a gap as a delayed write, not a lie.
`--evid-root` defaults to `evidence.root`.

## Samples

Every sample validates against its contract (or is a named negative that must fail). They are the fixtures
`test_contracts.sh` runs and the shapes a new skill author should copy.

| Sample | Contract | Expectation |
|---|---|---|
| `EXAMPLE-defect-loop/` (`PROJ-123`) — `repro.json`, `repro.with-media.json`, `verify.json`, `verify.with-media.json`, `fix.json`, `regression.json`, `review.json` | qa-repro · qa-verify.repro-judge · dev-fix · qa-regression · dev-code-review | pass — one full defect loop |
| `EXAMPLE-intake-already-addressed/` (`PROJ-124`) | dev-intake | pass — core + the optional tail, `already_addressed.detected: true` |
| `EXAMPLE-defect-minimal/` (`PROJ-125`) | dev-intake · dev-fix | pass — the 8-key core and a compact fix with string decisions |
| `EXAMPLE-inconclusive/`, `EXAMPLE-judge-inconclusive/` | qa-repro · qa-verify.repro-judge | pass — the three-value *could not tell* cases |
| `EXAMPLE-fix-must-read/` | dev-fix | pass — a must-read item beside a recorded decision and a legacy string |
| `EXAMPLE-bad-must-read-reason/`, `EXAMPLE-bad-must-read-no-ask/` | dev-fix | **fail** — off-enum reason / missing reviewer ask |
| `EXAMPLE-blocked-slice-empty/` | qa-regression | pass — zero tests reported honestly as `blocked-slice-empty` |
| `EXAMPLE-bad-green-zero-tests/` | qa-regression | **fail** — the incident shape (QA-REGRESSION-C1) |
| `EXAMPLE-pre-pr-blocked/`, `EXAMPLE-pre-pr-warnings/` | dev-pre-pr | pass — fail-closed on `fix_verified`; heuristics cleared by reading; `warnings` carries a held T1 `risk` + `judged`, `blocked` carries neither (v1.3.0 blocks are optional) |
| `EXAMPLE-pre-pr-risk-escalated/` | dev-pre-pr | pass — triage T0, diff T2, human gate `tests-weakened`, 2 human approvals, `warnings` (v1.3.0) |
| `EXAMPLE-bad-pre-pr-deescalated/`, `EXAMPLE-bad-pre-pr-agent-approval/`, `EXAMPLE-bad-pre-pr-ready-escalated/` | dev-pre-pr | **fail** — tier below triage (DEV-PRE-PR-T1) / agent approval counted as human (enum) / `ready` while escalated (DEV-PRE-PR-R1) |
| `case-4012-run-9001/`, `case-4055-run-9007/` | qa-run-case | pass — a PASS with drift + media, and a BLOCKED auth failure |
| `EXAMPLE-bad-enum/` | qa-run-case | **fail** — off-enum grade and step status |
| `diag-app-bug/`, `diag-inconclusive/` | qa-diagnose | pass — a proposed diff, and the empty-diff give-up path |
| `EXAMPLE-bad-layer/` | qa-diagnose | **fail** — off-enum `failure_layer` |
| `EXAMPLE-harden-rule/`, `EXAMPLE-harden-prose/` | dev-harden | pass — a probed rule, and a guideline that justifies every higher band (DEV-HARDEN-L1) |
| `EXAMPLE-bad-harden-band/`, `EXAMPLE-bad-harden-prose-skip/` | dev-harden | **fail** — off-enum band / a guideline that skips bands without a reason (DEV-HARDEN-L1) |
| `EXAMPLE-harden-miss/`, `EXAMPLE-harden-miss-declined/` | dev-harden | pass — a prod review miss that lacked `context`, ticket drafted; an eval miss that lacked `nothing`, declined with a reason (v1.1.0, DEV-HARDEN-M1) |
| `EXAMPLE-bad-harden-miss-no-lacked/`, `EXAMPLE-bad-harden-miss-nothing-ruled/` | dev-harden | **fail** — a miss placed with `lacked: null` / a rule proposed for a miss that lacked `nothing` (DEV-HARDEN-M1) |
| `EXAMPLE-review-harden/` | dev-code-review | pass — a drafted `harden` block and an integer `line` (v1.3.0) |
| `EXAMPLE-bad-review-line/` | dev-code-review | **fail** — `line` written as a range string |
| `EXAMPLE-review-bug-class/` | dev-code-review | pass — one finding with a `bug_class` and two `instances` (v1.4.0) |
| `EXAMPLE-bad-review-bug-class/` | dev-code-review | **fail** — `bug_class` off the closed vocabulary |
| `EXAMPLE-review-brief/` | dev-code-review | pass — real `review_brief.py finalize` output (v1.5.0): one file skipped, a migration refused as `blast-radius`, one nit withheld, the count lead |
| `EXAMPLE-review-clean-headline/` | dev-code-review | pass — a clean approve whose summary leads with `0 findings: nothing needs fixing.` (v1.5.0) |
| `EXAMPLE-bad-review-brief/` | dev-code-review | **fail** — headline says 0 with two open findings (DEV-REVIEW-H1) and two open lows over `brief.max_nits` 1 (DEV-REVIEW-B1) |
| `EXAMPLE-review-rounds/` | dev-loop-defect.review-rounds | pass — three request-changes rounds sharing one fingerprint (stagnation) |
| `EXAMPLE-bad-review-rounds/` | dev-loop-defect.review-rounds | **fail** — a round verdict off the enum |
| `EXAMPLE-reviewer-eval-bad/`, `EXAMPLE-reviewer-eval-clean/` | dev-code-review.eval-case | pass — a signed re-fix bad case and a signed clean case |
| `EXAMPLE-bad-reviewer-eval-case/` | dev-code-review.eval-case | **fail** — a bad case naming no bug (DEV-REVIEW-EVAL-K1) |
| `EXAMPLE-reviewer-eval/` | dev-code-review.eval-score | pass — real `reviewer_eval.py score` output over the seed set |
| `EXAMPLE-chatbot/` | qa-run-chatbot | pass |

## Versioning

`version` is per-schema semver. **Breaking** the key set, enums or types is a MAJOR bump and requires a
matching change in every consumer that branches on the field. **Adding an enum value that changes triage** is
such a break even when it is syntactically additive (a consumer must learn the new value — coordinate the
bump). Additive, consumer-ignored fields on a verbatim-stored contract are a MINOR bump; `media[]` and the
`attempt` / `retry_reason` / `timing` trio were added that way, proven by keeping the key-free samples green beside
the ones that carry the keys. Any change to `media`'s own item shape is itself a bump.

The MAJOR history that matters to a consumer: `qa-regression.json` 2.0.0 removed the browser-suite slice (status
`drift`, next_action `heal`, the `preflight` key); `qa-loop-release-ac.json` 2.0.0 joined rows to qa-run-case
receipts (`case_dir`) in place of test-management case and test ids, dropped `run_url` and
`summary.unmatched_receipts`, and renamed `fix_version` to `release_name`, with no legacy acceptance.
