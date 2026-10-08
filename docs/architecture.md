# Architecture — the loops, the brain, the evaluator, the gates

This is the reference for how the framework is shaped and why. It replaces the earlier visual
architecture with prose and ASCII so it can be read in a terminal, diffed, and kept honest.
The principles it rests on are in [`loop-engineering.md`](loop-engineering.md); how the
framework reaches *your* issue tracker, VCS host, CI and analytics is in
[`adapters.md`](adapters.md) and [`stack-config.md`](stack-config.md). Read those three
alongside this one.

> The governing idea: **the model that fixes a bug never changes. What improves is the system
> around it** — triage, reproduction, routing, and the knowledge it accumulates. A weak loop
> fixes this bug. A strong loop makes the next bug easier to triage, reproduce, and route.

Three principles show up in every component below:

1. **Build the evaluator first.** The reproduction *is* the test case. A fix is done when the
   oracle that went red on the broken build goes green on the fixed one — never before.
2. **Improve the workflow, not just the response.** Every run writes reusable knowledge back
   to the shared brain. A selector or route is learned once and propagates everywhere.
3. **Classify the failure before adapting.** A retry that repeats the same action is not
   improvement. The failure class decides what changes on the next attempt.

---

## 1. System map

Atomic skills compose into thin loop recipes. Two kinds of producer feed one shared brain
that every loop consumes. Every external system is reached through an adapter.

```
                 PRODUCERS                              CONSUMERS
   ┌──────────────────────────────┐          ┌──────────────────────────────┐
   │ AI QA                        │          │ every loop                   │
   │   qa-explore · qa-repro      │          │   intake · repro · verify    │
   │   qa-verify · qa-dream       │          │   regression · fix · ship    │
   │ Human QA                     │          │                              │
   │   human-explore (blesses)    │          │                              │
   └──────────────┬───────────────┘          └──────────────▲───────────────┘
                  │ write (via qa-kb only)                  │ read (via qa-kb only)
                  ▼                                         │
   ┌────────────────────────────────────────────────────────┴───────────────┐
   │                      THE KNOWLEDGE BRAIN  (qa-kb seam)                 │
   │   app_map · expectations · flakiness · fix_lessons                     │
   │   + kb_control (pause, posture) · metrics · kb_writes · kb_state       │
   │   Postgres + pgvector, one shared instance, any host                   │
   └────────────────────────────────────────────────────────────────────────┘

   ┌───────────────── ADAPTER LAYER  (.claude/loops/stack.config.json) ─────┐
   │ issue_tracker · vcs_host · ci_cd · deploy · analytics · auth_provider  │
   │ test_management · notifications · sandbox · evidence_store            │
   └────────────────────────────────────────────────────────────────────────┘
```

**Runner-agnostic skills.** A skill never assumes who invoked it — a CI pipeline, an
interactive terminal session, or a scheduler. Only the loop recipe knows the runner, and the
recipe is thin: it sequences atomic skills, owns the retry/escalate counters, checks the
budget and pause gates, and decides when a human steps in. All real work lives in the atoms.

**Thin recipes, atomic skills.** We do not build one giant loop. The same atoms recombine into
different recipes with different gates (§5), and that composition is the whole win. A skill
does one job and explicitly does *not* do its neighbours' jobs, so it can appear in more than
one recipe.

**The adapter layer.** Nothing in a skill names a vendor. "Fetch the ticket" goes through
`issue_tracker`; "open the PR" through `vcs_host`; "what version is deployed" through
`deploy`; "did the error rate drop" through `analytics`. A role whose provider is `none` is
absent: a skill that needs it returns a structured `blocked` with reason
`adapter-unavailable:<role>` — never a crash, never a silent skip, never a guess. Secrets are
never in config; config names the environment variable that holds them.

**The gates.** Four mechanical gates sit on the path to production and are described in §4
and §7: the per-loop budget cap (R5), the blast-radius denylist (R6), the global pause flag
(R9), and post-deploy verification with rollback (R2). Until all four exist and are verified
for your stack, every loop runs **staging-only / dry-run** — everything up to `infra-ship`
opens a PR, nothing deploys to production.

**Where it lives.** Skills under `.claude/skills/`; loop configuration, limits, and the brain
pointer under `.claude/loops/`; ratified artifact schemas under `contracts/`; evidence for
each run under `runs/<KEY>/` (gitignored). The brain is a database the repo points at, never
files in the repo.

---

## 2. The knowledge brain

One brain, four stores, kept deliberately separate. Mechanical and experiential knowledge is
written autonomously; judgment is propose-then-bless.

```
   ┌──────────────┬──────────────────────┬──────────────────┬───────────────────────────┐
   │ store        │ holds                │ dominant query   │ who writes                │
   ├──────────────┼──────────────────────┼──────────────────┼───────────────────────────┤
   │ app_map      │ selectors, routes,   │ keyed / exact    │ AI, autonomously          │
   │              │ area tags            │                  │                           │
   │ expectations │ how a feature SHOULD │ semantic         │ AI PROPOSES;              │
   │              │ behave (the oracle)  │ (pgvector)       │ a human BLESSES           │
   │ flakiness    │ per-test flake rate, │ counters +       │ AI, autonomously          │
   │              │ known-bad strategies │ semantic-ish     │                           │
   │ fix_lessons  │ symptom → root cause │ semantic         │ the recipe, once          │
   │              │ → fix shape          │ (pgvector)       │ qa-verify passes          │
   └──────────────┴──────────────────────┴──────────────────┴───────────────────────────┘
```

**Why the stores differ.** The decision driver is the retrieval pattern, not the write
pattern. `app_map` is answered by an exact key ("the submit selector on the billing page",
"tests tagged billing") — a relational table, vectors add nothing. `expectations` is answered
semantically: before proposing, the agent must find that "bad email shows inline error" and
"invalid email displays validation message" are the *same* rule, which exact match cannot
see. Embeddings can, and that is what prevents duplicate blessings (R10) and conflicting
oracles that poison the evaluator (R1). `flakiness` is counters today with a vector column
when needed. `fix_lessons` is retrieved by symptom, so it embeds too.

**Never merge the stores.** `app_map`, `flakiness` and `fix_lessons` are mechanical or
experiential; `expectations` are judgment. A wrong canonical expectation makes the evaluator
optimize toward a wrong target. Store **compressed, actionable lessons — never raw transcript
piles**. Too much memory pollutes the next attempt.

**Propose → bless.** The AI may *propose* an expectation (from `qa-explore`, `qa-repro`,
`qa-dream`). Only `human-explore` promotes a proposal to canonical, and every bless carries a
human signature. Only canonical expectations grade a repro or verify. This is the one place a
human sits inside the brain's write path, and it is deliberate.

**Provenance and the forget path (R1).** Every row carries `source`, `last_confirmed_at`,
`confidence`, `status`, and the embedding model + dimension, so staleness is measurable in
SQL and re-embedding is possible on a model change. `qa-kb` exposes `forget`/decay alongside
`read · write · search · propose → bless`; every write lands in a provenance ledger
(`kb_writes`) behind `kb receipt` and `kb undo`, so a bad write can be found and reversed
rather than quietly propagating. `qa-dream` is the scheduled sweep that consolidates and
prunes.

**Control flags (R9) and posture.** `kb_control` carries the `global_pause` kill switch and the
`staging_only` posture flag. Both are read **fail-closed**: an unreadable flag is treated as
paused / staging-only. A ship proceeds only on an explicit `paused: false`.

**Metrics (R5 / R8).** Each stage records cost, attempt count, escalation / flake / bless
events and time. The meta-monitor trends them — escalation rate climbing, cost-per-fix
rising, bless-rate with no rejects — and can trip the global pause. `kb_state` holds durable
cross-machine key/value such as the consolidation watermark.

**Concurrency (R4).** Updates to `app_map` pass `--expect-version`; a lost race is a
`version_conflict`, not a silent clobber. There is no general resource lock and none is
claimed; the per-loop sandbox (§5, `infra-worktree`) is the isolation primitive for code.

**Backend.** Postgres + pgvector on any host. The DSN is explicit (never defaulted) and the
pointer `.claude/loops/kb.config.json` is tracked, so a fresh clone, a second machine, or a CI
checkout lands on the *same* brain. A loopback host requires `KB_ALLOW_UNSHARED=1`, because a
lesson only compounds if the next run, on any machine, can read it; a stranded row is a lesson
the loop paid for and never gets back. All access goes through `qa-kb` — the `kb` CLI, never
raw SQL — so the brain can later lift into its own service without rewriting callers.

---

## 3. The evaluator

Three tiers of increasing trust and decreasing speed. The deterministic tiers earn the right
to skip humans.

```
   tier                 what runs                                        signal         scope
   ─────────────────────────────────────────────────────────────────────────────────────────────
   1 · repro / verify   two-video live repro + verify (before / after)   deterministic  the one bug
                        + the replay oracle: replay-oracle.sh driven by
                        playwright-cli — exit 0 green · 1 red · 2 blocked
                        + independent DOM / console / network read
                        + LLM judge (incl. symptom-masking check)
   2 · regression gate  build + lint vs the pre-fix baseline, the area's deterministic  the bug's area +
                        unit tests, then each changed project's full                    blast radius
                        unit suite; every red CLASSIFIED
   3 · human QA         judgment                                         human          pre-release &
                                                                                        escalations only
```

**Two videos, one oracle.** `qa-repro` reproduces the defect live, records the **before**
video, reads the real state independently, and emits a deterministic replay oracle plus the
verbatim expected/actual. The oracle **must exit 1 on the broken build** before `qa-repro`
may emit it — an oracle that is not red proves nothing. `qa-verify` re-runs the same oracle on
the fixed build (**must flip to exit 0**), drives the same steps, reads the DOM / console /
network again, records the **after** video, and runs an LLM judge that also checks the fix is
not a symptom mask. Verify passes only if the replay passes **and** the mechanical assertions
pass **and** the judge raises no flag. Hard assertions for mechanical facts (status codes,
required fields, visible text); the judge only for the genuinely subjective.

**Exit 2 is blocked, not red and not green.** It means the oracle could not run — no sign-in,
app unreachable, missing role. Fix the oracle or the environment; never read it as evidence
either way. Credentials come from the `auth_provider` adapter and are preflighted at
provisioning so a dead credential surfaces in seconds, not after the fix is written.

**Never trust the toast.** The single hardest-won rule: a success toast, a log line, a `200`
or a JSON `"ok": true` is not proof. Every evaluator reads the real state — the DOM, the
database, the network — independently of the app's own claim. A sloppy evaluator that trusts
the app's signal produces a worthless oracle, and the whole loop silently optimizes toward a
lie.

**No spec files.** Nothing in these loops generates, edits, heals or runs e2e spec files. The
oracle is a `playwright-cli` script; a live browser session is the exploration and video
transport.

**The regression gate** (`qa-regression`) is the blast-radius evaluator. A broken build or a
run that executed zero tests is a **blocking** status, never ship-ok. Every red is sorted
into exactly one class (§4), and quarantined flaky tests do not gate (R7).

**A producer never grades its own work.** `qa-verify` and `dev-code-review` run as fresh
agents, never as instructions loaded into the context that wrote the fix. A judge may read
the diff and the files changed — facts about the code — but not the producer's own
confidence, notes or design rationale, which anchor it before it looks. The judge runs on a
model at least as strong as the producer; a weaker judge produces an *invalid* verdict, not a
weaker one. See [`graph-engineering.md`](graph-engineering.md) for the mechanics.

---

## 4. The retry engine

When a gate fails, classify why before doing anything. Broken tooling, a flaky test and a
real regression all look identical at "test failed".

```
   failure class                      what it means                       next move
   ───────────────────────────────────────────────────────────────────────────────────────────
   environment / tooling              runner or its dependencies broke    re-provision or escalate;
                                                                          NO evidence about the fix
   flaky                              flips pass ↔ fail on rerun          record the data point;
                                                                          quarantine at the R7
                                                                          threshold; exclude from
                                                                          the verdict
   same-area regression               behaviour wrong in the bug's area   back to dev-fix, with
                                                                          the failing assertion
                                                                          as new context
   unrelated-area break               the fix reached outside its blast   ESCALATE NOW — no retry
                                      radius
   verify fail / partial              the oracle is still red             back to dev-fix with
                                                                          the observation
   review request-changes / masking   the diff is wrong or hides the      back to dev-fix with
                                      symptom (R3)                        the findings; masking
                                                                          is a hard block on ship
   low fix confidence                 the fix may be right but nobody     escalate, even on green
                                      can say why                         (configurable)
   blocked-blast-radius (R6)          the fix touched a denylisted path   STOP — forced human gate,
                                                                          never a retry or escalation
```

The engine, in pseudocode:

```
   for attempt in 1..CAP(tier, stage):
     result = stage.run(context_pack, brain)
     ev     = evaluator.check(result)            # tier 1 + tier 2
     if ev.passed: finalize()                    # → next stage / ship
     cls = classify(ev)                          # CLASSIFY BEFORE ADAPT
     if cls is deterministic and not transient:  # no blind repeat
         route_by_class(cls)                     # re-fix with new context, or escalate
     elif transient_evidence(cls):               # flaky match · infra error · timeout · oracle exit 2
         log_retry(context_pack); continue
   escalate()                                    # = dev-escalate, with the whole loop memory
```

**Every retry changes something.** A retry fires only with transient-failure evidence: a
flaky-store match (`flake_rate ≥ 0.30` over `≥ 5` runs), an infrastructure error, a timeout,
or an oracle exit 2. A deterministic failure never triggers a blind retry — it routes to a
different next move or escalates. Every retry is logged to `runs/<KEY>/context.json`
(`retries[]`: stage, attempt, trigger, evidence, outcome), which is the record that tells you
later whether the caps are right.

**Caps are tiered, not flat.** Triage assigns a tier (T0 / T1 / T2) from evidence, and the
tier sets the per-stage caps and the total loop cap; the budget sits where iteration actually
happens (review → re-fix), not on stages that rarely need a second attempt. The exact table
is in [`graph-engineering.md`](graph-engineering.md).

**Escalate, never de-escalate.** A run may move up a tier or up a model mid-flight — scope
surprise, cross-layer impact, a failed fix. It never moves down. Downgrades happen only at
triage. Escalation reuses the seam files already on disk; it never restarts from scratch.

**Budget (R5).** The per-loop cap is a number in `.claude/loops/loop-limits.json`
(`r5_budget.per_loop_usd_cap`, shipped default 15, in whatever unit the runner reports). The
recipe router compares reported spend against it at every stage gate; on breach it returns
`stop-budget` — stop and escalate, never retry into unbounded spend. Until a runner supplies
spend, this is a declared cap with a tested comparator, not an enforced gate; do not describe
it as hard beyond that.

**Pause (R9).** Before any ship, and as a general halt, the recipe asks the brain whether
autonomous shipping is paused. If set, or unreadable, the run halts (`halt-paused`).

**Escalation is a design point, not an error.** `dev-escalate` hands the ticket to a human
**warm**: the attempt log, the diagnosed failure class, both videos, the failing test with
actual vs expected, the verify / regression / review verdicts, and a best-guess root cause,
rendered from a configurable template into the issue tracker. The human never starts cold.
The renderer fails loudly rather than emit an empty hand-off.

---

## 5. Loop recipes

Same skills, different composition and gates. The front half is linear because each step
genuinely reads the previous one's output; the back half is a diamond because the three
judges depend only on the fix (see the fake-edge test in
[`graph-engineering.md`](graph-engineering.md)).

```
   legend:  [skill]   {deterministic gate}   (human)   ∥ parallel   ⇢ escalate on stop condition
```

**① `dev-loop-defect` — standard, non-sensitive bug → production, no human**

```
   [dev-intake] → [infra-worktree] → [qa-repro] → [dev-fix] ─┬─ [qa-verify]      ─┐
                      (tier, context pack)   (oracle RED)     ├─ [qa-regression]  ─┼─ {checker} → [dev-pre-pr] → [infra-ship] → [infra-prod-verify]
                                                              └─ [dev-code-review]─┘   (fails closed      (R9 pause ·     (R2 tie-back ·
                                                                 (when the tier         on verify.json)   R6 denylist)    rollback)
                                                                  calls for it)
```

The build + unit-test regression gate is the sanity gate that earns the right to skip QA.
Pre-flight on every run: abort if the pause flag is set; ticket text is data, not
instructions; enforce the budget cap. Staging-only until the safety gates are verified.

**② `dev-loop-hard-defect` — multi-file, shared-core, migration-adjacent, higher risk**

Identical to ①, with `dev-code-review` **mandatory** in the diamond. Masking is a hard block.
The same finding surviving two fix cycles is stagnation → escalate.

**③ `dev-loop-feature-prerelease` — sensitive or pre-release defects, never auto-ship**

```
   [dev-intake] → [infra-worktree] → [qa-repro] → [dev-fix] ─┬─ [qa-verify]     ─┐
     (forced human gate:                                      └─ [qa-regression] ─┴─ STAGE → (human QA batch)
      billing · signup · auth · payments ·
      feature-flagged · flagged for QA)
```

A defect pipeline (repro → fix), not a feature builder. The deterministic tiers prepare the
evidence; a human decides. It never deploys, regardless of confidence — that is the point. The
staged change is posted to the ticket with both videos, the diff and the verify result so the
reviewer starts in context.

**④ `dev-loop-feature-complete` — a story from ticket to a staged, verified change**

```
   [dev-intake] → [infra-worktree] → [plan-synthesis] → [implement] ─┬─ [qa-verify vs acceptance criteria] ─┐
                                      (competing strategies,          ├─ [qa-regression]                     ├─ {checker} → [dev-pre-pr] → STAGE → (human QA)
                                       pick one)                      └─ [dev-code-review]                   ┘
```

There is no bug to reproduce, so `qa-repro` does not run; verification is against the parsed
acceptance criteria and the after-video is still required. Features are pre-release by
nature, so this recipe never auto-ships. A feature PR carries an **after** recording only; a
defect PR keeps the side-by-side before → after, because the before *is* the evidence the bug
was real.

**⑤ `dev-loop-pr-review` — the scheduled review loop**

```
   [dev-pr-feed: list unreviewed PRs at their current head] → for each (bounded per tick):
       [dev-pr-feed: read-only checkout] → [dev-code-review] → [dev-pr-feed: post findings]
                                                                 (dry-run until authorized ·
                                                                  R9 pause · seen-ledger keyed
                                                                  on PR id + head commit)
```

Reviews each PR once per head commit and comes back when the author pushes, so it never
spams and never goes silent. Posting is dry-run until explicitly armed; the number of PRs per
tick and threads per PR are numbers in `loop-limits.json`, not prose.

**What every recipe shares.** The shared engine (`dev-loop-defect/references/recipe-engine.md`,
`scripts/recipe_router.py`, `scripts/check_convergence.py`): the failure-class router, the
tiered caps, the R5 comparator, the R9 check, the blind-judge spawn rule, the convergence
checker, and the rule that `infra-worktree` teardown runs in a `finally`. Recipes own
sequencing and gates and nothing else.

---

## 6. Skill catalog

One line each. Every skill is runner-agnostic, reaches external systems only through the
adapter layer, and is held to the loop-readiness checklist in
[`loop-engineering.md`](loop-engineering.md).

### Developer-side (`dev-*`)

| Skill | Does | Brain |
|---|---|---|
| `dev-intake` | The front door: fetch the ticket through `issue_tracker`, sanitize it (R6), run a sufficiency gate (needs-info rather than guess), judge risk, assign tier + models from evidence, write the context pack, route to exactly one recipe. Sensitive areas never auto-ship. | reads `app_map`, `flakiness`, `expectations`, history |
| `dev-fix` | Produce **one** candidate change that makes an already-reproduced defect's replay pass, at the root cause — enumerate every call site, patch the deepest shared point. Runs the R6 denylist scan before emitting `fix.json`; migrations / secrets / auth / IaC / CI are never edited (`blocked-blast-radius`). Refuses without a red oracle. Also hosts plan-synthesis for features. | reads `fix_lessons` |
| `dev-code-review` | The blind review judge: specialist fan-out over the diff against the repo's guidelines and `conventions.json`, plus the R3 symptom-masking judge. Emits a machine verdict (`approve` / `request-changes`) in `review.json`; findings validated at a confidence floor; marks findings `resolved` once a cycle closes them. | — |
| `dev-pre-pr` | The last gate before a PR: one readiness verdict (`ready` / `warnings` / `blocked`) from the loop's evidence. **Fails closed on `verify.json`** — missing, stale, malformed or not-`pass` all block at every tier. Deterministic standards scan on added lines; reconciles `regression.json` + `review.json` by freshness; re-runs the build when that evidence is stale. Has no edit tool. | — |
| `dev-escalate` | Hand a stuck loop to a human warm, from a configurable template: attempt log, failure class, repro + expected/actual, verdicts, decisions, every recording. Assigns per area, transitions the ticket. Posting is dry-run until the recipe authorizes. | (the ticket is the signal) |
| `dev-pr-feed` | The VCS-host PR surface: list PRs unreviewed at their current head, pull one into a read-only worktree, post findings as review comments, remember what was reviewed. Keyed on PR id + head commit. Fails closed on the R9 pause before any live post. | — |

### QA / evaluator / brain (`qa-*`)

| Skill | Does | Brain |
|---|---|---|
| `qa-kb` | The **only** interface to the brain, via the `kb` CLI. Four stores plus control flags, metrics and the provenance ledger. Enforces provenance, optimistic concurrency, propose → bless, and fail-closed pause semantics. | all |
| `qa-repro` | Reproduce a defect live: assemble the brief from the full ticket, sufficiency gate, drive the exact symptom with a before-video, read real state independently, emit the deterministic replay oracle (must exit 1) + verbatim expected/actual. `reproduced` / `not_reproduced` / `inconclusive`. | writes `app_map`; proposes `expectations` |
| `qa-verify` | The verdict gate, two modes. **fix-verify:** pass = the bug is gone (replay red → green, independent DOM read, after-video, masking judge). **repro-judge:** pass = the bug is present, ruled by a fresh judge that never trusts the repro's conclusion. Always a blind agent. | reads canonical `expectations` |
| `qa-regression` | Build + lint vs the pre-fix baseline, area unit tests, then each changed project's full suite; zero tests run or a broken build is blocking. Classifies every red: environment → flaky (R7 quarantine) → same-area → unrelated-break. | writes `flakiness` (every run, passes included) |
| `qa-explore` | Walk a feature *before* there is a bug, as a range of personas including adversarial and marathon walks; map selectors and routes; note timing sensitivity; propose expectations; draft (never file) a ticket for a likely bug. | writes `app_map`, `flakiness`; proposes `expectations` |
| `qa-personas` | A template pack of realistic tester personas, each with a critical lens; supplies the role and lens for repro / explore / verify walks. Roles come from config. | — |
| `qa-dream` | The scheduled consolidation pass: harvest yesterday's transcripts and loop artifacts deterministically, replay the brain's write outbox and re-embed pending rows, then consolidate compressed lessons so the next run starts warm. | writes `app_map`, `fix_lessons`, `flakiness`, metrics; proposes `expectations` |
| `qa-run-case` | Run one structured test case from a case file against a live environment, read real state independently, and propose a verdict (`PASS` / `FAIL` / `PARTIAL` / `BLOCKED` / `MANUAL`); a human disposes. Never asks a question — marks `BLOCKED` with the reason. | — |
| `qa-diagnose` | Diagnose one failed automated case run — screenshot first, then a two-analyzer swarm — and propose a minimal unified diff. Propose-only by construction (no edit tool). Classifies `app_bug` / `test_bug` / `environment` / `data`. | — |

### Ship / deploy / verify (`infra-*`)

| Skill | Does | Brain |
|---|---|---|
| `infra-worktree` | Thin wrapper on the `sandbox` adapter: an isolated branch + port offset (+ optional DB clone) per loop, a lock-protected registry, a concurrent-sandbox cap, port reclamation and stale-tree pruning, oracle-credential preflight, and crash-safe idempotent teardown — always called in a `finally`. | — |
| `infra-ship` | Format, commit, push and open the PR through `vcs_host` — after the gates. Preflight: R9 pause (fail closed), branch is not protected, the prod-lane refusal from `protected_branch_patterns`, and the R6 denylist scan of the full change set. Embeds before/after media; posts a PR thread only for a must-read item (expected count: zero). Never auto-deploys to prod. | — |
| `infra-prod-verify` | Confirm a shipped fix worked **in prod**: post-deploy smoke plus the ticket's original error signal through `analytics` on the version `deploy` reports. Exactly two outcomes: `prod-confirmed`, or `prod-regressed` → rollback (prefer a flag flip) + escalate with prod evidence. Never retries a fix against prod. | writes metrics |

### Human-in-the-loop

| Skill | Does | Brain |
|---|---|---|
| `human-explore` | A tester drives; live corrections become canonical ground truth. The **only** path that promotes a proposed expectation to canonical, and every bless carries a human signature. Also reviews the propose queue (bless / reject). | writes canonical `expectations` |

### Loop recipes (`*-loop-*`)

| Skill | Does |
|---|---|
| `dev-loop-defect` | Recipe ① (§5). |
| `dev-loop-hard-defect` | Recipe ② — ① plus a mandatory review gate; masking is a hard block; stagnation escalates. |
| `dev-loop-feature-prerelease` | Recipe ③ — sensitive / pre-release defects; stages for human QA; never ships. |
| `dev-loop-feature-complete` | Recipe ④ — story → plan → implement → verify vs acceptance criteria → stage; never auto-ships. |
| `dev-loop-pr-review` | Recipe ⑤ — the scheduled PR-review sweep, bounded per tick, dry-run until armed. |
| `qa-loop-release-ac` | Given a release version, find its PRs through `vcs_host` and the tickets they deliver, reconcile against the tracker's fix version, confirm the build is deployed through `ci_cd`, extract every acceptance scenario, write a case file per browser scenario and execute each through `qa-run-case` (chat ones through `qa-run-chatbot`), and report a per-ticket, per-scenario matrix that names every scenario needing a person instead of skipping it. |

### Named integrations

These are tied to a specific kind of surface (a chat assistant, a pair of environments); every
host, user, environment name and login flow is config or an adapter call.

| Skill | Does |
|---|---|
| `qa-run-chatbot` | Run a conversational / chat / MCP test set against a live environment and grade every case; the conversational sibling of `qa-run-case`. |
| `qa-ui-parity-sweep` | Sweep a list of pages in a baseline environment and a candidate environment side by side and report graded differences (console errors, computed styles, labels, visibility) with coverage per route; hard time budget, read-only against the candidate, a bounded number of one-file draft fixes. |

---

## 7. Safety — the risk register

The honest framing: without these, this is "an autonomous way to ship bugs to prod fast", not
a self-improving system. The deterministic gates prove a fix works in test — they do not
prove it works in prod, that root cause was fixed rather than symptom masked, or that the
system itself is healthy.

| Id | Risk | Severity | Mitigation | Carried by |
|---|---|---|---|---|
| **R1** | **Brain rot — no garbage collection.** We designed how knowledge gets in, never how it gets out or corrected. A bad `app_map` write propagates system-wide; expectations go stale when features change; dead routes accumulate. Slow and invisible — hurts over months. | high | A `forget`/decay path plus a scheduled brain audit (stale-entry decay, conflict resolution, dead-route pruning). Every row carries provenance and `last_confirmed_at` so staleness is measurable; every write is in a ledger that supports `undo`. | `qa-kb` (provenance, forget, receipt/undo) · `qa-dream` (the scheduled sweep) |
| **R2** | **Production verification hole.** The pipeline proves the fix in staging. Nothing confirms it worked in prod after deploy — different data, scale, flags, race conditions. | pre-launch | A post-deploy stage after ship: a smoke check on prod tied back to the ticket's original error signal, confirming the error rate actually dropped. "Ship" is no longer the last step — "ship → confirm in prod" is. | `infra-prod-verify` · `analytics` + `deploy` adapters · every prod recipe |
| **R3** | **Symptom-masking vs root-cause fix.** Two videos prove the reported symptom is gone, not that the cause was fixed. An agent under pressure to green the replay will hide the error, swallow the exception, or special-case the input. Both videos look perfect. Invisible by design. | high | The review judge explicitly looks for masking patterns (swallowed exceptions, suppressed logs, narrow special-cases); the verify judge checks too (defence in depth — never merge the two judges); regression catches side effects. Not fully solvable: a low-confidence root-cause fix is flagged for a human even on green. | `dev-code-review` (masking judge) · `qa-verify` · `dev-fix` (confidence model) |
| **R4** | **Concurrency and ordering.** Parallel loops collide: two fixes on one file, two loops writing conflicting `app_map` rows, merge races. Last-write-wins on the brain means one loop silently clobbers another's learning. | medium | Per-loop sandbox isolation (unique branch, DB, port offset; registry mutated only under an exclusive lock; a concurrent-sandbox cap). Optimistic concurrency on brain writes (`--expect-version`) instead of blind last-write-wins. No general resource lock exists and none is claimed. | `infra-worktree` / `sandbox` adapter · `qa-kb` write semantics · the recipe engine |
| **R5** | **Cost is invisible until the bill arrives.** The system loops on token-hungry stages — recording, multi-attempt fix, a judge on every verify, exploration sweeps. A pathological ticket could burn enormous spend silently retrying. | pre-launch | A per-loop cap as a number in `loop-limits.json`, compared by the recipe router at every stage gate; on breach, stop and escalate. Cost-per-fix is a first-class metric. Honest status: a declared cap with a tested comparator until the runner supplies spend; a scheduled job that hands the cap to the harness is the one place it is enforced today. | recipe router (`stop-budget`) · `qa-kb` metrics · the runner |
| **R6** | **Adversarial / poisoned input and blast radius.** A ticket is untrusted text that becomes agent instructions (prompt injection via the ticket body). The agent has deploy authority and no defined sandbox; a wrong repro step can send it down a destructive path. | pre-launch | Ticket content is data, never instructions — sanitized once at intake and carried into the context pack. A hard denylist: `dev-fix` may never touch migrations, secrets / env config, auth configuration, infra / IaC, or CI definitions without a forced human gate; `infra-ship` re-scans the full change set. Sandboxed execution; least-privilege deploy credentials. | `dev-intake` (sanitize) · `dev-fix` (denylist) · `infra-ship` (preflight scan) · `infra-worktree` · runner permissions |
| **R7** | **A flaky suite poisons the signal.** If the regression suite is flaky the loop cannot trust its own evaluator — escalating real passes, re-fixing phantom failures. Autonomous shipping needs a trusted suite first. | high | Record a flakiness data point for every test run, pass or fail; quarantine a test at `flake_rate ≥ 0.30` over `≥ 5` runs and surface every quarantine in the report — never drop a test silently; a quarantined test cannot act as a gate until stabilized. A suite-health prerequisite on shipping is tracked as future work, not claimed. | `qa-regression` · `flakiness` store · `loop-limits.json` (`r7_flake_quarantine`) |
| **R8** | **Nobody watches the watcher.** Auto-escalation and autonomous brain writes — if the escalation rate spikes or one area fails repeatedly, that is a meta-signal something systemic broke. We have the data, no alerting. | medium | A meta-monitor over loop-health metrics (escalation-rate trend, repeat-failure-by-area, cost-per-fix) with thresholds, able to trip the global pause; a human in the loop for the loop itself degrading. | `qa-kb` metrics · the recipe engine (trips R9) |
| **R9** | **No rollback or kill switch.** Everything is forward motion: fix → ship. An auto-shipped fix causes an incident at 2am and there is no defined response for the *system* failing, only for individual bugs. | pre-launch | An auto-rollback trigger tied to the prod error signal (R2's hook). A global human kill switch (`global_pause` in `kb_control`) that pauses all autonomous shipping, read fail-closed before any ship or post. Ship behind a flag where possible so revert is a flag flip, not a redeploy. A `staging_only` posture flag that forbids the prod lane until a human flips it. | `infra-ship` · `infra-prod-verify` · `dev-pr-feed` · every recipe · `qa-kb` flags |
| **R10** | **Human QA is the scaling bottleneck.** Every sensitive fix, every escalation and every expectation-blessing funnels to humans. As volume grows this queue becomes the constraint, and blessing-fatigue means rubber-stamping, which silently defeats the propose-then-bless guard. | medium | Prioritize the human queue by risk and confidence; batch and summarize blessings; semantic dedup so the same rule is never blessed twice; track bless-rate vs reject-rate — if rejects approach zero, humans have stopped scrutinizing (alert on it). | `human-explore` · `dev-escalate` · `qa-kb` (dedup, metrics) |

**Pre-launch gate — required before the first autonomous production ship:**

```
   R2  prod verification + error-signal tie-back   without it: you ship "fixes" you never confirm in prod
   R6  blast-radius limits + input sanitization    without it: a ticket can drive the agent into destructive actions
   R9  rollback + global kill switch               without it: no defined response when the system itself fails
   R5  budget caps + cost telemetry                 without it: a pathological ticket silently burns enormous spend
```

R1 (brain rot) and R7 (flaky suite) are not launch-blockers but must be scheduled early —
both degrade trust slowly and invisibly. R3 is never fully closed; it is bounded.

---

## 8. Build order — evaluator first

```
   1  qa-kb                       the brain seam: storage-agnostic interface, search, provenance +
                                  forget (R1), pause flag (R9), metrics (R5/R8)
   2  qa-repro + qa-verify        the evaluator — two videos + the replay oracle, before any fixing
   3  qa-regression               build / lint + unit-test gate, flaky quarantine (R7)
   4  dev-fix                     the fix engine, with the blast-radius denylist (R6) and
                                  root-cause-over-symptom (R3)
   5  dev-intake · dev-escalate · infra-ship · dev-code-review
                                  input sanitization (R6), warm hand-off, the ship preflight (R9),
                                  the masking judge (R3)
   6  infra-prod-verify           post-deploy confirmation + rollback (R2 / R9) —
                                  required before the first autonomous prod ship
   7  qa-explore · human-explore  the learning producers
   8  the recipe engine           tiered caps, budget breaker (R5), concurrency (R4),
                                  meta-monitor (R8), convergence checker
   9  dev-loop-*                  the thin recipes, last
```

Why this order: a wrong brain interface poisons every downstream skill, so it is built first
and alone; the evaluator comes before the fixer because a fix with no oracle cannot be judged;
safety gates come before recipes because no recipe may run unattended near prod without them;
recipes come last because they are only composition.

**Done = self-improving.** The system is finished when it: knows what success looks like per
stage; evaluates each attempt (oracle + tests + judge); confirms the fix in prod, not just
staging (R2); stores useful lessons and forgets stale ones (R1); changes strategy by failure
class; never repeats a failed path blindly; knows when to stop, escalate, roll back and halt
globally (R9); is bounded by budget (R5); is safe against poisoned input and blast radius
(R6); and is measurable — brain growth, escalation / flake / bless trends, cost-per-fix.

---

## See also

- [`loop-engineering.md`](loop-engineering.md) — the principles and the loop-readiness checklist.
- [`graph-engineering.md`](graph-engineering.md) — why a loop is a graph, the fake-edge test, tiering, the gates.
- [`adapters.md`](adapters.md) — the adapter contract per role and how to add a provider.
- [`stack-config.md`](stack-config.md) — every key of `.claude/loops/stack.config.json`.
- `contracts/` — the ratified seam-file schemas and `validate.py` / `emit.py`.
- `.claude/loops/CAPABILITIES.md` — the skill catalog and contracts ledger.
