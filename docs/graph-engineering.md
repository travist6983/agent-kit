# Graph engineering — tiers, edges, and gate semantics

A loop recipe is a graph of skills, not a chain of them. This document holds the rules that
decide the graph's shape — which steps run in sequence, which run in parallel, how much a
run is allowed to shrink, and what may never be shrunk — and the rationale behind each rule.
Every rule here was derived by auditing real runs against the skill contracts; where a
measurement justified a rule, the text says "measured on real runs" and omits the number
unless the number *is* the rule.

Read [`loop-engineering.md`](loop-engineering.md) first for the principles and
[`architecture.md`](architecture.md) for the components these rules arrange. Adapter roles
named below (`sandbox`, `issue_tracker`, `vcs_host`) are defined in
[`adapters.md`](adapters.md); the config keys in [`stack-config.md`](stack-config.md).

---

## 1. Why a loop is a graph, not a chain

A recipe typed as a sequence — intake → repro → fix → verify → regression → review → ship —
reads naturally and runs slowly, and the slowness is the least of its problems. Three things
go wrong when a graph is flattened into a chain:

1. **Fake edges.** Steps that share no data run one after another because that is the order
   someone wrote them in. Measured on real runs, the serial verify → regression → review
   stretch was pure waiting: none of the three reads another's output.
2. **A producer grades its own work.** When the verify step is invoked as instructions loaded
   into the context that just wrote the fix, the author marks their own homework with full
   memory of what the code was meant to do. Measured on real runs, that configuration passed
   verification every single time, at high confidence, with masking never suspected — which
   is not what a healthy evaluator looks like. The runs that spawned separate reviewers found
   things; the runs that reviewed themselves found nothing.
3. **Gates that do not gate.** A chain upholds "the fix was verified" only because the recipe
   advances one stage at a time. The moment two stages run concurrently, a failed or missing
   verification can arrive beside two green artifacts and be synthesized into a confident
   `ready`. Measured on real runs, two pull requests had shipped with no verification
   artifact at all, and nothing had noticed.

Treating the recipe as a graph fixes all three with the same edit: remove the fake edges, run
the independent judges as separate agents, and put a checker at the point where their outputs
converge. Correctness and wall-clock improve together.

**Vocabulary.** A *node* is one skill invocation that reads seam files and writes seam files.
An *edge* A → B exists only if B reads a file A wrote. A *diamond* is a fan-out from one node
to several independent nodes that converge on one consumer. A *checker* is a deterministic
node at a convergence point that asks whether the inputs are sane before anything merges
them. A *seam file* is an artifact on disk under `runs/<KEY>/`, shaped by a contract in
`contracts/`.

---

## 2. The fake-edge test

> **Before sequencing two steps, ask: does step 2 read step 1's output? If not, run them in
> parallel.**

Classify every edge from the skill contracts ("Inputs / Outputs" in each `SKILL.md`), never
from intuition. For the defect recipe:

| Edge | Class | Why |
|---|---|---|
| `dev-intake` → `infra-worktree` | real | provisioning takes the branch name intake chose |
| `infra-worktree` → `qa-repro` | real | repro drives the sandbox's app URL and credentials |
| `dev-intake` → `qa-repro` | real | repro's brief is intake's sanitized ticket + area + risk class |
| `qa-repro` → `dev-fix` | real, hard | no red oracle, no fix — `dev-fix` structurally refuses without it |
| `dev-fix` → `qa-verify` | real | verify replays the oracle against the fixed build |
| `dev-fix` → `qa-regression` | real | regression builds, lints and tests the diff |
| `dev-fix` → `dev-code-review` | real | review judges the diff + `fix.json` |
| `qa-verify` → `qa-regression` | **fake** | regression's inputs are the diff, the lint baseline and the oracle; it never reads `verify.json` |
| `qa-verify` → `dev-code-review` | **fake** | `verify.json` is not an input to review |
| `qa-regression` → `dev-code-review` | **fake** | review judges the diff, not the build |
| `qa-regression` → `dev-pre-pr` | real | pre-pr reconciles `regression.json` |
| `dev-code-review` → `dev-pre-pr` | real | pre-pr reads `review.json.verdict` |
| `qa-verify` → `dev-pre-pr` | real — **was missing** | see §5; pre-pr must fail closed on `verify.json` |
| `dev-pre-pr` → `infra-ship` | real | ship gates on the readiness verdict |

Three fake edges, one missing edge. The fake edges were the entire cost of the serial middle
of the loop; the missing edge was the entire reason the chain's safety was an accident.

**Edges kept deliberately, despite looking removable.** Not every sequential edge is waste.
Some stay because of an invariant, not data flow:

| Edge | Why it stays |
|---|---|
| `dev-fix` → `qa-verify` | invariant + real: the judge must be *separate* (blind) and the fix must *exist* |
| `dev-fix` → `dev-code-review` | invariant: separation; the diff is atomic so the sequence is natural |
| `dev-pre-pr` → `infra-ship` | invariant: the R9 pause check and the R6 denylist run in ship's preflight; the readiness verdict must precede it — never parallelize |
| `qa-repro` → `dev-fix` | real, hard: `dev-fix` refuses without the oracle |

The separation-vs-sequence distinction matters: **a blind-judge edge needs the judge to be a
different agent, not a later one.** Two agents running concurrently, neither seeing the
other's reasoning, satisfy blindness perfectly — which is exactly why the diamond is safe.

---

## 3. The proven diamond — `qa-verify ∥ qa-regression ∥ dev-code-review`

All three judges depend only on `dev-fix`. None depends on the others. `dev-pre-pr` genuinely
needs all of them. That is the textbook diamond, and it has been hiding in every defect run.

```
                      [qa-repro]  oracle + verbatim expected/actual
                          │
                          ▼
                      [dev-fix]   fix.json + diff          ← the ONLY writer in this layer
            ┌─────────────┼─────────────────┐
            ▼             ▼                 ▼
     [qa-verify]    [qa-regression]   [dev-code-review]
     BLIND agent    build·lint·unit   BLIND agent
     replay RED→GREEN                 diff + R3 masking judge
            │             │                 │
            └─────────────┼─────────────────┘
                          ▼
                     {{ CHECKER }}   empty · contradictory · off-topic · low-confidence ·
                          │          malformed · judge weaker than producer
                          ▼
                     [dev-pre-pr]    convergence + readiness verdict; fails closed on verify.json
                          │
                          ▼
                     [infra-ship]
```

Both diamond rules hold: the parallel nodes are genuinely independent (verified against the
contracts in §2), and the convergence node genuinely needs all of them.

**It fixes the blind-judge violation for free.** Splitting verify and review into parallel
agent spawns makes them blind *because* they are separate agents. The regression node becomes
an honest gate at the same time — it must run the tests itself instead of transcribing what
the fix step already ran, which costs more compute and buys a real verdict. That trade is
deliberate.

**The spawn rule.** `qa-verify` and `dev-code-review` are spawned as fresh agents, never
invoked as skills into the current context. A skill invocation loads instructions into the
context that called it; it does not create a judge. What the judge may not read: the
producer's own `design_decisions`, `notes`, `confidence` and pending lesson in `fix.json` —
a judge that reads "I'm confident this is the root cause" is anchored before it looks. The
diff and `files_changed` are fair game: facts about the code, not claims about its quality.
`judged_blind` and `model` are contract fields on `verify.json` and `review.json`, so
blindness and judge strength are falsifiable rather than assumed. One invariant remains under
pressure: nothing yet mechanically prevents a recipe from invoking the judge as a skill and
self-reporting `judged_blind: true`; the checker catches a *weak* judge, not a *dishonest*
one.

**The long pole.** Review already fans out into parallel specialists, so the diamond's saving
is bounded by review's width. Do not expect the diamond to save more on features than on
defects; the structural win is the same, the proportion is smaller.

**Only `dev-fix` writes.** The diamond needs no extra worktrees because the three judges are
readers. If a future node writes inside the parallel layer, it needs its own worktree (§10).

---

## 4. Convergence checkers

Running judges in parallel removes the natural checkpoints where a bad output would have been
noticed. A hallucinated, empty or contradictory artifact now arrives beside two good ones, and
`dev-pre-pr` reconciles by **freshness** — "is this newer than the diff?" — never by sanity.
The checker (`dev-loop-defect/scripts/check_convergence.py`) asks the second question. It is
not a reviewer; it catches exactly these and nothing else:

| Failure | What it looks like |
|---|---|
| empty | a node returned nothing usable, `null`, or an empty object |
| contradictory | two artifacts that cannot both be true (`verify.json` vs `regression.json`; `approve` with an unresolved CRITICAL) |
| off-topic | an artifact that names a different ticket |
| low-confidence | below the floor for the run's tier (T0 80 · T1 80 · T2 85 — T0 verifies *less*, so it may not also believe itself more loosely) |
| malformed | unparseable, or a shape the next node cannot read (runs `contracts/validate.py`) |
| weak judge | the judge's model is below the producer's in the strength ordering |

Exit codes: `0` clean · `1` flagged — do not merge, route by `next_action` · `2` uncheckable —
the evidence directory is unreadable; fail closed, never read as clean.

**A flagged input never merges silently. A `null` agent result is a reported failure, never
filtered away.** The dangerous outcome of a bad node is silence, not noise.

**Calibrate against history before trusting a new block.** The first version blocked on
`approve` plus any unresolved HIGH finding. Replayed against real runs it produced false
blockers on a quarter of them — the finding *had* been addressed in a re-fix cycle; the
artifact had never been marked `resolved`. After two false blockers nobody trusts the gate.
It was recalibrated by severity: an unresolved **CRITICAL** under `approve` blocks (those
cannot both be true); an unresolved **HIGH** under `approve` warns and advances; and the
upstream fix was to have review set `resolved: true` on findings a cycle closed. Replayed
again: zero false blockers, and the two genuinely unverified runs caught.

**A gate whose escalate is overridden is not a gate.** Measured on real runs, a regression
step repeatedly returned `blocked-environment / escalate` and the run shipped anyway. That
teaches the loop that gates are advisory, which is the exact failure the safety register
exists to prevent. A `next_action: escalate` from any gate is not overridable by the recipe.
If the gate cannot run in your environment, fix the environment or remove the gate
explicitly — never leave a gate that is routinely ignored.

---

## 5. Verification at every tier

> **A tier may shrink *how much* is verified, never *whether*.**

`dev-pre-pr` fails closed on fix verification, mechanically
(`dev-pre-pr/scripts/verify_gate.py`), because a safety gate expressed as a paragraph is not
a gate:

| `verify.json` | `fix_verified` | Verdict |
|---|---|---|
| `pass`, fresh | `pass` | continue |
| `fail` / `partial` | `fail` | blocked → re-fix |
| `blocked` (oracle exit 2) | `fail` | blocked → **escalate** — no evidence, so a re-fix would be a blind retry |
| `pass` + `masking_suspected` | `fail` | blocked → re-fix (R3: a masked pass is a fail) |
| unreadable / no `verdict` / stale vocabulary | `malformed` | blocked → escalate |
| absent, inside a loop | `missing` | blocked → escalate |
| stale (a changed file is newer) | `fail` | blocked → re-fix |

**A green build never substitutes for it.** A missing `review.json` stays non-blocking in the
standard defect recipe (review is mandatory only where the recipe or tier says so); a missing
`verify.json` is never acceptable at any tier.

**A stale verify is never re-run by pre-pr.** Pre-pr has no edit tool, drives no browser, and
— decisively — a verify it ran itself would not be blind.

**The honest trade.** This gate blocks on *unfalsifiable* evidence, not on bad outcomes. The
two historical runs that shipped without a `verify.json` turned out fine; under this rule they
would have cost two escalations for two good outcomes. With no artifact there is no way to
tell "verified, unrecorded" from "never verified" after the fact, and the invariant makes the
conservative read the required one. The cost is bounded: a run that records its verification
never trips it.

---

## 6. Tier assignment — from evidence, never the title

Triage is part of `dev-intake` (`dev-intake/scripts/assign_tier.py`); it is not a separate
node, because intake already gathers the evidence and already routes. It gains three outputs:
a `tier`, a `models` map, and the context pack (§8).

**The signals** — each obtainable from the ticket and the brain before any heavy work:

| Signal | How |
|---|---|
| Gherkin shape | parse the acceptance-criteria block: scenarios, steps, distinct pages, distinct roles |
| likely layer | ticket nouns → one code search for the named entity: front-end only / back-end only / both / data |
| files likely touched | the same search, deduplicated to a file list |
| flakiness history | `kb read flakiness --area <area>` |
| expectations coverage | `kb search expectations --query <behaviour>` |
| prior attempts | `kb history --ticket <KEY>` |
| defect or feature | the tracker's issue type |

The title is never a signal. Measured on real runs, the title is what a human had been judging
by, and it under-called the run that ended in a long escalation.

**The rules:**

```
   T0  trivial    ALL of:
                    issue type is a bug
                    single layer (front-end only or back-end only)
                    likely files <= 3
                    Gherkin scenarios <= 2
                    touches 1 page AND 1 role
                    area flake_rate < 0.30
                    risk class is standard (not billing / signup / auth / payments)
                    no prior failed attempt on this ticket

   T2  complex    ANY of:
                    issue type is a story or task       (a feature is never T0 — there is no bug to reproduce)
                    likely files >= 8, or layer spans code AND data
                    Gherkin scenarios >= 5, or more than one role
                    risk class is sensitive
                    migration-adjacent, or touches shared / core modules
                    prior attempts >= 1

   T1  standard   everything else — the tier you land in when unsure
```

**Confidence moves a run up, never down.** Triage confidence below 70 starts the run one tier
up regardless of the rules; below 50 starts it at T2. Unknown facts move up.

**Calibration.** Replayed against real runs with each shipped diff as the ground truth for
"how big was it really", the rules classified all but one correctly; the miss was
under-classified, and nothing in its ticket predicted the escalation it needed. That is
precisely why escalate-never-de-escalate (§7) is load-bearing rather than decorative: it is
the *only* recovery path, because triage cannot be made perfect. Once that ticket carries a
prior attempt, the same rules send it to T2, so the loop cannot repeat a strategy that already
failed.

**What a tier changes:**

| | **T0 trivial** | **T1 standard** | **T2 complex** |
|---|---|---|---|
| shape | shortest linear chain + context pack; no fake edges | linear front half + the proven diamond | diamonds with checker nodes; dynamic fan-out width |
| runtime | plain skill chain | skill chain; a workflow only if fan-out ≥ 3 | saved workflow(s) |
| repro | **evidence-light**: static reasoning from Gherkin + code + `app_map`, plus a single-surface browser confirmation; no persona walk | full browser walk + oracle | full walk plus adversarial exploration |
| verify | targeted tests for the Gherkin scenarios + a **blind** judge | targeted tests + adjacent-area checks | **adversarial** verify before synthesis |
| review | blind judge, concurrent with verify (never merged — §11) | review in the diamond | review + masking judge as separate agents |
| models | cheaper model on intake / context / light repro; **strongest on fix**; judge ≥ producer | mostly the strong model | strong model; strongest on fix / design after escalation |
| retry cap (loop total) | 3 → then escalate to T1 | 5 | 6 |

T0 is cheap because it has *fewer and shorter* nodes, not because it thinks less about the
code. The fix step and its judges keep the strong model at every tier.

**Static or dynamic.** T0 and T1 are static graphs: the structure is identical every run and
auditability matters, because this ships code. T2 is a static skeleton with a dynamic fan-out
width — the stages are fixed; only how many parallel investigation nodes run is unknown until
triage reads the scope. Anything that ships to production is static, never dynamic: the risk
register demands an audit of exactly what ran and why.

---

## 7. Escalate, never de-escalate

A run may move up a tier or up a model mid-flight. It never moves down. Downgrades happen only
at triage, by rule.

**Triggers:**

| Trigger | Action |
|---|---|
| a fix attempt fails verify | fix step → next model up, retry within cap, with the observation as new context |
| scope surprise: files touched > 2× the triage estimate | **tier up** and model up |
| cross-layer impact discovered (one layer turns out to need another) | tier up |
| triage confidence < 70 / < 50 | start at T1 / T2 |
| `qa-regression` returns `unrelated-break` | escalate immediately — no retry |
| `dev-code-review` finds R3 masking | fix → stronger model; hard block on ship |
| the same review finding survives 2 fix cycles | stagnation → escalate |
| retries exhausted at the tier cap | escalate |
| checker flags any convergence input | route by `next_action`; never merge |
| `dev-fix` returns `blocked-blast-radius` (R6) | **stop** — forced human gate; never a model escalation |
| R5 budget breach or R9 pause | stop, never retry |

**Model routing.** Triage writes a model per step into the context pack. A step may *request*
escalation by writing `escalation_request: { to, reason }` into its own seam file; a step
never downgrades itself. **The blind judge runs on a model at least as strong as the producer
it grades**, by the strength ordering the checker carries. If the fix step escalates, verify
and review escalate with it. A weaker judge does not produce a weaker verdict — it produces
an invalid one, and the checker blocks on it.

**Escalation reuses completed seam files; it never restarts from scratch.** A T0 → T1
escalation keeps `context.json`, `intake.json` and `repro.json` and re-runs only what the
higher tier requires. The move is recorded in `context.json.tier_history[]`, so the run's own
record shows what it started as.

---

## 8. The context pack

Measured on real runs, a meaningful share of every loop was spent re-deriving what the loop,
or a previous run, already knew: reading a *previous ticket's* artifact to recall a contract
shape, copying a previous oracle as a template, re-issuing the same brain searches in each
skill, re-validating contracts. The context pack carries that once.

`runs/<KEY>/context.json`, written by triage **before** any heavy work
(`contracts/context-pack.json`):

```jsonc
{
  "ticket": "PROJ-123",
  "type": "defect",                  // defect | feature
  "tier": "T0",
  "sanitized_summary": "...",        // R6: ticket text as DATA, imperatives stripped
  "gherkin": [ { "scenario": "...", "given": [], "when": [], "then": [] } ],
  "layer": "BE",                     // FE | BE | both | data
  "likely_files": [ "..." ],
  "app_map_slice": { "routes": {}, "selectors": {} },
  "flakiness": { "area": "...", "quarantined": [], "flake_rate": 0.0 },
  "expectations": [ /* canonical ones covering this behaviour */ ],
  "fix_lessons": [ /* symptom-matched priors */ ],
  "models": { "intake": "...", "fix": "...", "verify": "...", "review": "..." },
  "env": { "app_url": "...", "api_url": "...", "worktree": "...", "e2e_env_file": "..." },
  "repro_evidence": null,            // filled by qa-repro
  "retries": [],                     // every retry: stage, attempt, trigger, evidence, outcome
  "tier_history": []                 // every escalation
}
```

**Rules.** Downstream skills *read* the pack and do not re-derive what it covers. A skill that
needs something absent from the pack adds it **and records that it did**, so the gap shows up
as a pack-design bug rather than as silent per-run waste.

**Safety.** The pack carries the *sanitized* ticket text only. R6 is enforced once, at triage,
instead of hoped for at every downstream skill. This strengthens R6; it does not relax it.

The pack is also the prerequisite for tiering: a tier decision needs exactly the evidence the
pack gathers.

---

## 9. Retry rules

Measured on real runs, the retry budget sat on the wrong stages: reproduction and
verification almost never needed a second attempt, while the review → re-fix cycle fired on
most runs and changed real behaviour on several. The caps follow where iteration actually
happens:

| Tier | `qa-repro` | `qa-verify` | `dev-fix` (from review / regression) | Loop total |
|---|---|---|---|---|
| T0 | 1 | 1 | 2 | 3 → then escalate to T1 |
| T1 | 1 | 2 | 3 | 5 |
| T2 | 2 | 2 | 3 + stagnation check | 6 |

Zero observed repro / verify retries means that machinery is *unexercised*, not proven
unnecessary — ten successful runs cannot say what happens on the eleventh, flaky one. The caps
are lowered, not removed.

**A retry requires transient-failure evidence.** A retry fires only when the failure is
plausibly transient:

- the flaky store matches the failing test or area (`flake_rate ≥ 0.30` over `≥ 5` runs), or
- an infrastructure error (app unreachable, database connection, token failure), or
- a timeout, or
- the replay oracle exited `2` (blocked) — which is *no* evidence, not negative evidence.

**A deterministic failure never triggers a blind retry.** `qa-verify` returning `fail` on a
clean run is information, not noise: it routes to `dev-fix` *with the observation as new
context*, which is a strategy change, not a repeat. `qa-repro` returning `not_reproduced`
takes the reject path (a human confirms the close), never a retry.

**Features have no repro retries, because there is no repro.** `qa-repro` does not run on a
feature; verify against acceptance criteria is capped at 1 (a second failure is a plan
problem, not a flake → human QA); the review → re-implement cycle is capped at 3 with the
stagnation check, because that is the cap that actually binds on features.

**Count the approve-with-findings cycle.** A review that returns `approve` with findings, and
a loop that then spends time fixing them anyway, is work done outside any accounting. Either
the findings justify a re-fix cycle (count it against the cap) or they do not (defer them to a
*Noticed, not changed* section). Silently doing the work off-budget is the worst of the three.

**Log every retry** to `context.json.retries[]` — `{stage, attempt, trigger, evidence,
outcome}`. That record, not instrumentation, is what tells you later whether the caps are
right.

---

## 10. Worktrees — whenever agents could edit concurrently

Provisioning a sandbox is cheap (measured on real runs: a small fraction of a loop). The
policy is about correctness and housekeeping, not speed.

**Required:**

- every T2 run (parallel implementation nodes),
- every run started while **another loop is active** on the same checkout,
- every feature run (long-lived, high collision probability),
- any run whose diamond includes an agent with write access.

**Optional — T0 only, and only if no other run touches that checkout.** Checked mechanically,
never assumed: the `sandbox` adapter's lock-protected registry tracks active sandboxes, so the
test is *registry empty for this checkout → T0 may run in place*.

**Falling back to the main checkout is a logged decision, never a shrug.** It is written to
`context.json.environment.fallback_reason`, and it is **forbidden outright while any other
loop is active**. The failure mode this prevents was observed twice: a run found its sandbox
ports squatted, tore the sandbox down, and quietly completed the whole loop in the developer's
own checkout — restarting the developer's running services on the way. A loop that relocates
silently defeats the isolation the sandbox exists to provide.

**Reuse is allowed only for the same ticket, never another ticket's tree.** One run observed
inside another ticket's worktree pushed two tickets' branches from one tree; harmless only
because the runs were serial. Under parallelism it is corruption. Prefer a fresh worktree
over a clever reuse — correctness beats a few seconds of install.

**Stale trees are the real cost.** A leftover tree holding ports broke runs days apart. The
prune policy (`infra-worktree/scripts/prune.py`) runs at provision time, not on a timer:

1. Registry-driven, not filesystem-driven — the locked registry is the truth.
2. **Port reclamation first**: a port in the offset range held by a process whose working
   directory is a worktree with no registry entry → that tree is dead; kill the process, free
   the port. This step is the whole point.
3. Age out: no registry entry and untouched for more than 24 hours → prune.
4. Branch merged to the default branch → prune unconditionally.
5. Never prune a tree with uncommitted changes; report it and fail loud.

**Credentials are preflighted at provisioning.** The oracle signs in as a real user before it
can grade anything; a dead credential surfaces as exit 2 → `blocked` → escalate, which is
correct behaviour and a dead loop. `infra-worktree/scripts/oracle_preflight.py` catches a
missing file, role or value in a second, static; `--live` is the conclusive check that the
account exists in the environment the sandbox authenticates against. A credential minted for
one environment need not exist in another (see the `auth_provider` adapter).

---

## 11. Seam files on disk are the source of truth

Every hand-off between nodes is a file under `runs/<KEY>/`, shaped by a contract in
`contracts/`, written through `contracts/emit.py` — which builds the artifact from the
contract, validates before writing, and fails loudly. The agent supplies values, never
syntax. Measured on real runs, hand-serializing contract JSON (heredocs, validation
rejections, repair edits, re-reading a previous ticket's file to recall the shape) was a
larger cost than the fake edges, and the emitter removes it at zero risk to any invariant.

Each seam file carries `started_at` / `ended_at` and, for repro and verify, an `attempt`
counter. These make the first real run a direct comparison instead of a transcript
excavation.

**Workflow variables are not a seam.** Workflow resume is same-session only, so a
cross-session escalation *must* rebuild from the seam files. **A workflow cannot prompt**, so
every human gate sits *between* workflows: a tier that needs a human decision ends its
workflow there and a new one starts after. This is why a T2 design is two or more workflows,
not one.

**Review artifacts are overwritten in place**, so the final `approve` can hide an earlier
`request-changes`. Keep the intermediate verdicts in the evidence directory if you need the
history; the retry log in the context pack is the durable record.

---

## 12. Failure modes worth naming

**The one to fear most: a complex defect misclassified as T0, shipped on light verification.**
T0 trims the repro walk to a single browser confirmation. If the real symptom needs a
multi-step or multi-role path, a single-surface confirmation can look green while the defect
is untouched — and T0's lower cap means fewer chances to notice. The guards, in depth order:

1. Verification is never removed, only narrowed — T0 still runs a blind judge and still
   requires `verify.json.verdict == pass`.
2. Scope-surprise auto-escalation: files touched > 2× the estimate → tier up mid-flight.
3. `dev-pre-pr` fails closed on `verify.json` — a prerequisite, not a nicety; without it,
   parallelism makes the misclassification risk strictly worse.
4. Gherkin coverage check: if parsed scenarios exist that the T0 verify did not exercise, the
   checker flags it and the run escalates rather than ships.
5. T0 excludes sensitive areas by rule, regardless of diff size.

**Merging the judges.** It is tempting to make T0 cheaper by folding review into verify — one
judge, one node. Do not. They judge different objects (runtime behaviour vs the diff), and
R3 masking detection lives in both as defence in depth. T0 keeps both; they run concurrently,
which is where the time comes back.

**A before-capture that changes no verdict.** Reverting working code to re-drive the browser
purely for a "before" recording serializes a run behind a checkout of live files and gates
nothing. On features it is removed; the after-recording remains required. On defects the
before recording *is* the evidence the bug was real, so it stays.

---

## Prerequisites, in order

Items 1–3 are correctness work and stand alone; items 4–5 are speed work. If only half gets
built, build the first half.

1. `dev-pre-pr` fails closed on `verify.json` (§5). Without it, parallelism can ship an
   unverified fix.
2. `qa-verify` and `dev-code-review` are separate agents (§3). Self-grading is a live
   correctness defect independent of any speed work.
3. The checker at the convergence point (§4).
4. The context pack, written by `dev-intake` (§8).
5. Tier and model fields plumbed through the recipe router (§6, §7).

If any of this is ever reverted, revert from the bottom up.
