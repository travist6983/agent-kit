# The recipe engine — retry / escalate / budget / locks / pause (shared by all dev-loop-* recipes)

**Environment (all recipes):** after `dev-intake`, `infra-worktree provision` — a thin wrapper on the
`sandbox` adapter declared in `.claude/loops/stack.config.json` — opens an isolated sandbox and returns
`{dir, branch, slot, ports, urls{app, api}, db, started:false, next_steps[]}`. Ports are the app's
configured port plus `slot × port_stride`; a database copy exists only when
`adapters.sandbox.config.db_clone_command` is set. The app is **not** started: run `app.start_command`
in `dir` (or the sandbox's `next_steps`) and prove reachability on `app.health_path` before any browser
step. `infra-worktree teardown` runs UNCONDITIONALLY in a finally — crash, escalation, or success
(idempotent, crash-safe). At `sandbox-cap-reached` (exit 2; `max_concurrent` slots are all taken) the
recipe queues or escalates — never a second forced environment. With `sandbox: none` loops serialize
under the registry lock, and a main-checkout fallback is a logged decision taken only when no other loop
is active — never while one is. **Oracle credentials:** the replay oracle signs in through the
`auth_provider` adapter (`credentials(role)`; the role names and login selectors live in
`adapters.auth_provider.config`). The recipe hands `qa-repro`, `dev-fix` and `qa-verify` the sandbox's
`urls.app` and the role name — never a password, never a machine-specific path. With the `env-file`
provider the credentials come from the tracked `env_file` + gitignored `local_env_file` pair named in that
config. Without credentials a signed-in oracle exits 2 and the loop escalates; a missing variable is named
by variable, never by value.

Recipes are **thin**: they sequence the atomic skills and own the counters + gates. All the real work
lives in the atomic skills. This engine is what every `dev-loop-*` recipe shares. The decision logic is
in `scripts/recipe_router.py` (tested) — the recipe calls it after each stage to decide what's next.

**Adapter roles, fail closed.** Every external system is a role in `stack.config.json`: `issue_tracker`
(the ticket, comments, the drafted close), `sandbox` (the environment), `auth_provider` (oracle sign-in),
`vcs_host` (the pull request and review threads), `analytics` + `deploy` (prod confirmation and rollback
instructions), `notifications` (escalation posts), `evidence_store` (media uploads). A role whose provider
is `none` makes the skill that needs it emit a structured `blocked` with reason
`adapter-unavailable:<role>`; the router escalates that like any other block. Nothing guesses a value, and
nothing skips the stage silently.

## Pre-check: prior attempts (run BEFORE the loop, feed the router)
**Invoking `kb`.** Every `kb <verb>` below is the `qa-kb` CLI wrapper
(`.claude/skills/qa-kb/scripts/kb`), which must be **on your PATH** (one symlink per machine —
`docs/brain-setup.md`). It is **not** there in a fresh checkout: an unresolved `kb` exits **127**
(`kb: command not found`) and the brain write is simply *lost*, so fall back to the script instead of
skipping the call — **absolutely**, never cwd-relative, because a loop's cwd is normally a
sandbox worktree, not this repo:

```bash
# <agents-repo> = the checkout of THIS framework the file was loaded from; set both in ONE shell.
KB="${KB_BIN:-<agents-repo>/.claude/skills/qa-kb/scripts/kb.py}"
python3 "$KB" <verb> ...        # ≡ the bare `kb <verb> ...` calls below
```

Either route reads the tracked `.claude/loops/kb.config.json` and lands on the **one shared brain** —
Postgres + pgvector on whatever host the config names (`database_url`, or `KB_DATABASE_URL`), with the
auth mode the config selects. There is no flat-file or local fallback: the DSN must be explicit, and a
loopback host is refused unless `KB_ALLOW_UNSHARED=1` says offline work is intended. A refused brain is
a loud failure, never a silent write somewhere only one machine can see.

The router is not stateless on a ticket the loop has already tried. Right after `dev-intake`, read the
brain's memory of this key and carry it through the run:
```bash
kb history --ticket <KEY>   # {attempts:[…metrics rows…], writes_by_store:{…}, escalations:N,
                            #  last_outcome:…, areas:[…]}  — safe on a cold ticket: empty, ok:true
```
Pass it into the routing decision as context: a ticket with prior `escalations` or a `last_outcome` of
`verify-fail-at-cap` must not silently re-run the identical strategy — that is the cardinal rule below
applied *across runs*, not just across attempts. A cold ticket returns empty structures and changes
nothing. `dev-intake` reads the same call and carries it as `prior_attempts`; the recipe does not need
to read it twice if intake already handed it over.

## Pre-check: already-addressed (run BEFORE the loop)
If `intake.json.already_addressed.detected` is true (the ticket carries a fix note / linked PR), do NOT
start a fix loop. Run `qa-repro` to confirm: if the bug does **not** reproduce, the fix is present →
close as **already-fixed** (cite the PR); write nothing. Only if it **still reproduces** (the PR isn't in
this checkout) does the normal loop proceed. This stops the loop re-fixing solved work — a ticket can
carry a merged fix PR while its tracker status is still an open one, and the status alone proves nothing.

## The cardinal rule: every retry CHANGES strategy (classify before adapting — principle 3)
A retry that repeats the same action is a bug, not a retry. The router routes by **failure class**:
- **same-area regression** → `dev-fix` again, with the failing assertion fed in as new context.
- **unrelated-area break** → **escalate immediately, no retry** (the fix exceeded its blast radius — a
  scary signal that retrying won't fix).
- **verify fail / partial** → `dev-fix` with the new observation.
- **code-review request-changes (incl. masking)** → `dev-fix` with the findings.

## Blind judging — a producer NEVER grades its own work

**This is an invariant, not an optimisation, and it is the one a flattened loop quietly
breaks.** Measured on real runs (`docs/graph-engineering.md`), a recipe that invoked
`qa-verify` inline — the same agent context that wrote the fix replaying the oracle and ruling
on it — returned `pass` every single time, at high confidence, with `masking_suspected: false`
every single time. That is what a producer grading itself looks like. The runs that *also*
reviewed themselves, with zero sub-agent spawns, were exactly the runs that returned **0 review
findings**; every run that spawned real reviewers found something.

**`Skill` does not create a context.** It loads instructions into the *current* one. So
`Skill(qa-verify)` right after writing the fix is not a judge — it is the author marking
their own homework with full memory of what they meant the code to do.

### The rule

> `qa-verify` and `dev-code-review` are invoked with **`Agent`**, never `Skill`, in every
> recipe and at every tier. Each gets a fresh context that has **never seen** the fix being
> written, the reasoning behind it, or the producer's own confidence.

```
# WRONG — the author grades itself
Skill(qa-verify)

# RIGHT — a blind judge
Agent(subagent_type: "general-purpose", description: "qa-verify (blind) <KEY>", prompt: """
  Invoke the qa-verify skill on <KEY>. You are a BLIND judge: you did not write this fix
  and you must not assume it works.
  Read ONLY these files — do not ask for or infer the producer's reasoning:
    <EVID_ROOT>/<KEY>/repro.json        the oracle + verbatim expected behaviour
    <EVID_ROOT>/<KEY>/context.json      the shared context pack
    <sandbox dir>/                       the code as it now stands
  You may NOT read fix.json's `design_decisions`, `notes`, or `confidence`.
  Write <EVID_ROOT>/<KEY>/verify.json. Your verdict is yours alone.
""")
```

**What the judge may not see.** `fix.json.design_decisions`, `.notes`, `.confidence` and
`.pending_fix_lesson` are the producer's self-assessment. A judge that reads "I'm confident
this is the root cause" is anchored before it looks. It *may* read `fix.json.files_changed`
and the diff — those are facts about the code, not claims about its quality.

**`dev-code-review` already does this on most runs** and fans out to parallel specialists.
That machinery works; `qa-verify` simply has to be given the same treatment.

### Judge strength

The judge runs on a model **at least as strong** as the producer it grades. The strength order
is `models.rank` in `stack.config.json` (weakest → strongest; `models.cheap` / `models.strong` /
`models.verifier` name the defaults). If `dev-fix` escalated to the strongest model, verify and
review escalate too. A weaker judge does not produce a weaker verdict — it produces an *invalid*
one. The checker node blocks on this.

## The parallel layer — verify ∥ regression ∥ review (the diamond)

`qa-verify`, `qa-regression` and `dev-code-review` each depend **only** on `dev-fix`, and
none reads another's output. Verified against the contracts:

- `qa-regression` Inputs = the diff + the lint baseline + qa-repro's oracle. It drives no
  browser and **never reads `verify.json`**.
- `dev-code-review` Inputs = "the candidate diff (`git diff <project.default_branch>`), the
  `fix.json`". Neither `verify.json` nor `regression.json` is an input.

So the three edges `verify→regression`, `verify→review` and `regression→review` are **fake**
— nothing flows along them. Run the three **concurrently**, in one `Agent` batch:

```
        dev-fix  (fix.json + fix.diff)
           ├── Agent: qa-verify       (BLIND)   → verify.json
           ├── Agent: qa-regression             → regression.json
           └── Agent: dev-code-review (BLIND)   → review.json
                         ↓  all three
                   CHECKER  (scripts/check_convergence.py)
                         ↓
                   dev-pre-pr  → infra-ship
```

Measured on real runs the saving is minutes per run, and blindness comes free with the split.
`qa-regression` gets *slower* as its own agent (it must run the tests rather than transcribe
`dev-fix`'s summary), which is a deliberate trade: more compute, less wall-clock, and an
honest gate.

**Only `dev-fix` writes.** The three judges are readers, so they need no extra sandbox. If
you ever add a *writing* node to this layer, it needs its own sandbox — see R4.

**Ordering is separation, not sequence.** Two agents running at the same time, neither
seeing the other's reasoning, satisfies blindness perfectly. Sequence was never the point.

## The checker — every convergence point has one

The parallel layer removed the natural checkpoints where a bad output would have been
noticed, so a bad artifact could now flow into `dev-pre-pr` beside two good ones. Before
`dev-pre-pr`, run:

```bash
python3 <agents-repo>/.claude/skills/dev-loop-defect/scripts/check_convergence.py \
  --evid <EVID_ROOT>/<KEY> --key <KEY> [--producer-model <model id>]
# exit 0 clean · 1 FLAGGED (do not merge) · 2 could not check (fail closed)
```

It catches the five failure modes a diamond hides: empty/null outputs, artifacts that
contradict each other, off-topic artifacts (wrong ticket), confidence below the floor, and
malformed JSON — plus a judge weaker than its producer, ranked by `models.rank`. **A flagged
input never merges silently, and a `null` result from an agent is a reported failure — never
something to filter away.** On exit 1 route by its `next_action`; on exit 2 escalate.

## Caps (per retryable stage) — rebalanced by tier

Measured on real runs, `qa-repro` and `qa-verify` almost never needed a failure-retry; the
stage that actually iterates is `dev-code-review` → `dev-fix` (one or two cycles on most
runs, and on a third of them the cycle changed real behaviour). The caps follow the
evidence rather than guarding stages that have never needed it:

| Tier | `qa-repro` | `qa-verify` | `dev-fix` (from review/regression) | Loop total |
|---|---|---|---|---|
| T0 | 1 | 1 | 2 | 3 → then escalate to T1 |
| T1 | 1 | 2 | 3 | 5 |
| T2 | 2 | 2 | 3 + stagnation check | 6 |

`context.json.tier` supplies the tier; pass the stage's cap as `recipe_router.py --max M`.
With no tier (a pre-tiering run) the old default of 3 still applies.

**A retry needs transient-failure evidence.** Fire one only when the failure is plausibly
transient: a flaky-store match (`flake_rate ≥ 0.30` over `≥ 5` runs — `r7_flake_quarantine` in
`loop-limits.json`), an infrastructure error, a timeout, or `replay-oracle.sh` exit 2. **A
deterministic failure never triggers a blind retry** — it escalates, or routes to `dev-fix`
*with the observation as new context*, which is a strategy change, not a repeat.

Log every retry to `context.json.retries[]`:
`{stage, attempt, trigger, evidence, outcome}`. That record is what tells us in a month
whether these caps are right.

**Also count the approve-with-findings cycle.** A measured run got `approve` with several
findings and then spent minutes fixing them outside any router accounting. Either those
findings justify a re-fix cycle (count it against the cap) or they do not (defer them to
*Noticed, not changed*). Doing the work off-budget is the worst of the three.

## R5 — budget circuit breaker
The cap is a NUMBER, not prose: the file `limits_file` in `stack.config.json` points at
(`.claude/loops/loop-limits.json`), key `r5_budget.per_loop_usd_cap` (the router reads it when
`--budget-cap` isn't passed — explicit `--budget-cap 0` disables). Spend is supplied by the RUNNER (a
scheduler or CI job's per-run API usage accounting; a Claude Code session's reported cost interactively)
via `--budget-spent` at every stage gate. On breach → `stop-budget`: stop and escalate, **never retry
into unbounded spend**. HONESTY: until a runner supplies spend, this is a declared cap with a tested
comparator — not an enforced hard gate; don't describe it as one. Under a subscription
`runner.billing_posture` the figure is a list-price-equivalent consumption number, not money.

## R9 — global pause
Before any ship (and as a general halt), check `qa-kb` `kb pause --status`. If set → `halt-paused`.
`infra-ship`'s preflight also fails closed on this.

## R4 — concurrency (two real mechanisms, no lock primitive)
There is NO general file/resource lock and none is claimed (loop-limits.json `r4_concurrency`). What
actually prevents parallel-loop clobbering: (1) **sandbox isolation** — every loop runs in its own
sandbox from the `sandbox` adapter (own branch, own port slot, own database copy when configured; the slot
registry mutates only under an exclusive lock, race-tested), so file paths are never shared; (2)
**optimistic concurrency on the shared brain** — `app_map` updates MUST pass `--expect-version`; a
`version_conflict` means re-read + resolve, never clobber.

## R8 — meta-monitor
Each stage records a `qa-kb` metric (cost, attempt count, escalation/flake/bless, time). The
meta-monitor trends these (escalation rate climbing, cost-per-fix rising) and can trip the global pause.

## Knowledge write-back — every run (strongly encouraged, NOT a hard gate)
This is loop-engineering principle #2: every run writes reusable knowledge back so the next task is
easier. Do it at the natural stages of **every** run, through the `kb` CLI — the user should never have to
ask for it (a run that fixes a real bug and writes nothing is a real miss). It is **encouraged, not
gated**: a missing write does **not** fail the loop — but it must be **visible**, never silent.
- **`app_map`** (write autonomously, `qa-repro`/`qa-explore`): component/file, route, selectors, nav path,
  useful APIs, framework entry points. **Lean heavy here while the brain is young** — this is how the loop
  learns to drive the app, and early defects should each teach it a lot of navigation.
- **`expectations`** (`qa-repro`/`qa-explore` **propose**, never bless): the correct behavior of the
  surface; `search` first to dedup.
- **`fix_lessons`** — **the RECIPE writes this, not `dev-fix`.** See *Who writes the fix lesson* below.
- **`metrics`** (the recipe): loop_outcome, fix_cycles, review findings, etc. (R8 history) — plus
  `pr_threads_posted` / `pr_threads_before_merging` / `pr_threads_outside_funnel` from `ship.json`
  after every ship, so review-thread volume is measured rather than noticed by an annoyed reviewer.
- **`flakiness`** — one data point per test run (`qa-regression`, passes included); **`pause`** only the
  kill-switch.

### Who writes the fix lesson (and when)
`dev-fix` cannot know whether its fix worked — that verdict is `qa-verify`'s and arrives one stage later.
So `dev-fix` only *carries* the lesson: `fix.json.pending_fix_lesson`
`{symptom, root_cause, fix_shape, signature_tag, area}` plus `fix.json.lessons_consulted[]`. **The recipe
lands it, and only on a real pass:**
```bash
# AFTER qa-verify returns verdict:pass — never before, never on partial/fail/blocked
kb write fix_lessons --area "<pending_fix_lesson.area>" \
  --symptom "<pending_fix_lesson.symptom>" --root-cause "<pending_fix_lesson.root_cause>" \
  --fix-shape "<pending_fix_lesson.fix_shape>" --signature-tag <pending_fix_lesson.signature_tag> \
  --source dev-fix --ticket <KEY>
```
Skip the write when `pending_fix_lesson` is `null`, when `qa-verify` returned anything but `pass`, or when
the fix carries `masking_suspected: true` — a lesson learned from a mask actively poisons future recall
(R3). On a multi-cycle run, write **once**, from the fix that finally passed; a near-duplicate symptom
bumps `occurrences` rather than cloning (R10). The `--source` stays `dev-fix` (that's the knowledge's
origin); the recipe is merely the gate that knows the outcome.

**The final report's `🧠 brain write-back` line comes from the ledger, not from recollection.** Read it:
```bash
kb receipt --ticket <KEY>    # every kb_writes row this run made, grouped by store
```
and render the summary from that (e.g. `app_map ×4 · expectation proposed (#7) · fix_lesson (#1) ·
metrics ×4`), or `— none (and why)` if a run genuinely had nothing to teach. Receipts are written
automatically inside each write's own transaction, so this is a *falsifiable* record — an agent that
believes it wrote four rows and shows a receipt of one has found a real bug, not a formatting problem.
An empty brain line on a real fix is the signal that write-back was skipped — surface it, don't hide it.
If the receipt shows queued or pending writes (`kb outbox --status`, `embed_status: "pending"`), say so
in the same line: those recover via `kb outbox --replay` / `kb reembed`, or on the next `qa-dream` pass.

## Escalation carries full memory
Every escalation → `dev-escalate` with the whole loop memory (attempt log, failure class, recordings,
failing test, root-cause guess). It reaches the human through the `issue_tracker` and `notifications`
adapters — with the default file tracker that is a comment appended to the ticket plus a notification
file. The human resumes warm. The step is two commands, and the recipe is the only caller allowed to
pass `--authorize`:

```bash
python3 .claude/skills/dev-escalate/scripts/render_escalation.py --task-dir "$TASK_DIR" > "$TASK_DIR/escalate.json"
python3 .claude/skills/dev-escalate/scripts/post_escalation.py --task-dir "$TASK_DIR" --reason "<stop reason>" [--authorize]
```

Without `--authorize` the post is a dry run that records what would have been delivered.

## Safety envelope (while `staging_only` is set)
Prod shipping is disabled until R2/R5/R6/R9 are verified. Recipes run **staging-only / dry-run** — up to
`infra-ship`'s PR assembly, **no prod deploy** — until a human clears the `staging_only` flag in the brain
for one low-risk area. Which environments exist, and their URLs, is `app.environments` in
`stack.config.json`; which branches deploy on push is `deploy.prod_lane_branch_patterns`.
