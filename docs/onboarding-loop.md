# The onboarding loop: getting an agent up to speed on a brand new codebase

The defect loop makes the next bug easier. This loop makes the next codebase easier. It drops an
agent into a repository it has never seen, has it build a cited model of that code, proves the
model with a blind exam, relearns only what the exam showed was missing or wrong, and writes the
verified parts back to the shared brain so every later run on that repo starts warm.

It is built the way every loop here is built (`docs/loop-engineering.md`): the evaluator first,
atomic skills plus one thin recipe, every failure classified before anything changes, and
knowledge written back on every run.

It is not the loop's between-runs learning. `qa-dream` consolidates what finished runs taught the
brain, and `dev-harden` turns a correction that keeps coming back into a check. Onboarding is the
first day on a codebase: it builds the model of the code that those two later refine.

## Why a loop and not a long read

Reading a codebase and then summarizing it fails in a predictable way. The summary always sounds
right. It reads as convincing whether it is true or not, and the agent that wrote it is the worst
possible judge of it. Three things fix that:

1. **Every claim says what it rests on.** A claim about behaviour cites file and line. A claim about
   intent cites the record (a commit, a pull request, a decision doc) or says it is an inference.
2. **Someone else checks it.** A writer who read only the code asks questions; an examiner who read
   only the map answers them; a judge grades them; a script checks every cite against the files.
3. **A miss changes the next move.** A question the map could not answer becomes a targeted relearn
   of one area for one reason, not a second full read.

## The loop

```
            ┌──────────────────────────── refresh: only areas whose files changed ◀──────┐
            ▼                                                                             │
 survey ──▶ how ∥ why (one pair per area, fanned out) ──▶ map ──▶ quiz ──▶ router ──▶ write back
 (script)   (cited cards)                                (architect  (blind   │  pass        (brain)
                                                          lens, lint) exam)   │                  │
            ▲                                                                 │ fail             ▼
            └───────── targeted relearn: these areas, this failure class ◀────┘            teach (human)
```

| Stage | Skill | Does | Writes |
|---|---|---|---|
| Frame + orient | `dev-onboard-survey` | A deterministic, zero-model scan: languages, manifests, the repo's own build/test/lint/run commands, entry points, tests, docs and decision records, churn hotspots, and a first cut of areas with sizes. | `survey.json` |
| How | `dev-onboard-how` | One read-only agent per area writes an architectural explanation a senior engineer could act on, as atomic claims: what it is, how a request flows, where things live, who owns which state, gotchas. Every claim cites file and line. | `cards/<area>.how.json` |
| Why | `dev-onboard-why` | One agent per area answers from the record, not the code: the commits and pull requests that introduced and changed the area, decision docs, issue links. It quotes what authors wrote, marks inferences as inferences, and lists what the record does not answer. | `cards/<area>.why.json` |
| Map | `dev-onboard-map` | Merges the cards into one map with the architect's lens: each piece of state has one owner, each task one way to do it, and a rule for where new code of each kind belongs. `check_map.py` refuses an uncited claim, a cite that does not resolve, or a why claim that claims more certainty than its source. | `map.json`, `map.md` |
| Quiz | `qa-onboard-quiz` | The evaluator. Writer (code only) ∥ examiner (map only) ∥ judge (key and answer only), then `grade_quiz.py`. | `quiz/round-<n>/quiz.json` |
| Route | `dev-loop-onboard` | `onboard_router.py` turns each lost question into a next move for its area, enforces the round cap and the R5 consumption cap, and detects stagnation. | `route.json` |
| Write back | `dev-loop-onboard` | `onboard_writeback.py` promotes verified, navigational claims to the brain's `lessons` store (kind `codebase`), records metrics, and keeps the full map as a file. | brain rows, metrics |
| Teach | `dev-onboard-teach` | For a person: explains the codebase plainly from the map, at their pace, and turns any question the map could not answer into a gap the next round relearns. | updated gaps |

How and why for the same area run in parallel, because neither reads the other's output (the
fake-edge test in `docs/graph-engineering.md`). The quiz's three roles are separate fresh-context
`Agent` spawns, never `Skill` calls, because a producer never grades its own work.

## What "learned" means: the exam

The definition of done is falsifiable, and it is declared before any reading starts:

> A map passes when a blind examiner, holding only the map, scores at least
> `onboard_loop.pass_threshold` (0.8) on questions written from the code, no area scores below
> `onboard_loop.area_floor` (0.6), and the question set itself was trustworthy.

Five question kinds cover what understanding a codebase means in practice
(`.claude/skills/qa-onboard-quiz/references/question-kinds.md`):

| Kind | Tests | Why it is in the set |
|---|---|---|
| `locate` | where something lives | the floor; a file index can pass these |
| `trace` | what happens step by step when X | how the code actually runs |
| `predict` | what changes or breaks if X | blast radius: understanding, not recall |
| `why` | why it is built this way | the record, not the code |
| `owner` | which module owns a state or decision; where new code goes | the architect's view |

The grader is a script so the parts that can be checked are checked:

- An answer key whose cite does not resolve (missing file, line out of range, symbol absent from
  the cited lines, a path that climbs out of the repo) is discarded. It never counts against the
  map. Too many of them block the round, because the writer failed, not the map.
- Every file and line the examiner cites must be covered by a map claim it named. A cite nothing
  in the map covers means the examiner read the code, and the round is blocked, because its score
  would be a lie.
- A judge grade that never came back blocks the round. A missing result is a reported failure,
  never a silent pass.

## Classify before relearning

Every lost question carries exactly one class, and the class decides what changes:

| Class | Means | Next move |
|---|---|---|
| `missing` | the map has nothing on it | `dev-onboard-how` on that area, pointed at the question's topic |
| `wrong` | the map says something the code contradicts | `dev-onboard-how` in correction mode on the named claims; the wrong claims are dropped, not patched |
| `vague` | right neighbourhood, no precise cite | `dev-onboard-how` deepen mode: narrower cites, walk the flow |
| `stale` | the code changed since the map's commit | re-survey the area, then `dev-onboard-how` on it |
| `why-gap` | a why question the record was never mined for | `dev-onboard-why` on that area |
| `bad-key` | the writer's key cites nothing real | not the map's fault; a fresh writer next round |
| `outside-map` | the examiner cited code the map does not cover | not the map's fault; a fresh examiner, told again it is blind |

Lost questions carry over unchanged into the next round, alongside new ones. A relearn that only
patches the question it was told about passes the carryover and fails the new questions. The same
question lost twice with the same class is stagnation: the loop stops and escalates with what it
still does not know, instead of reading the same files a third time.

## Bounds

- **Rounds:** `onboard_loop.max_rounds` (3). A map that has not passed by then is handed back with its
  open questions, not quietly accepted.
- **Consumption cap:** the R5 runaway backstop in `loop-limits.json`, compared at every stage gate
  the way the defect loop compares it (read its honesty note before calling it a dollar budget).
- **Read only.** No stage edits the target repo, opens a pull request or posts anywhere. The why
  stage reads commits and pull requests through the `vcs_host` adapter, read operations only.
- **Context:** fan-out is per area, and each agent returns claims, not file dumps (guard the context
  window). A large repo gets more areas, not longer agents.

## What gets written down

| Where | What | Why there |
|---|---|---|
| `<evid_root>/onboard/<project>/` | `survey.json`, the cards, `map.json`, `map.md`, every quiz round, `route.json`, `decisions.tsv` | the run's full record; files on disk are the source of truth between steps |
| brain `lessons` store, kind `codebase` | verified navigational claims only: owners, where-new-code-goes rules, gotchas, commands, where things live | compressed, actionable, recalled by any later run on any machine |
| brain metrics | `onboard_quiz_score` per round, `onboard_rounds_to_pass`, `onboard_bad_key_rate` | so you can tell whether the loop is getting better, not just running |
| `decisions.tsv` | one row per decision: what, why, evidence, result | a reviewer who steps away can audit the run |

A claim is promoted to the brain only after a question that leaned on it was answered correctly
(`verified_round` in the map). Unverified claims stay in the map file and reach the brain as
`proposed`, for a human to bless or reject. Raw transcripts are never stored.

## Refresh

A map records the commit it describes. On the next run the staleness check (`map_staleness.py`)
diffs from that commit, maps the changed files to areas, and relearns only those areas before
quizzing again. A map with no usable commit (built from a tree with no history, or a commit the
checkout lacks) relearns everything, because a partial refresh from an unknown base would be a guess. A repo that
moved a little costs a little.

## Where the ideas come from

The stage skills are written in our own words for this repo's conventions. The shape of several of
them comes from Lauren Tan's pstack skills for Cursor (`cursor/plugins`, `pstack/skills`, MIT
licensed, Copyright (c) 2026 Lauren Tan). Credit where it is due:

| pstack skill | What we took | Where it lands |
|---|---|---|
| `teach` | explain plainly at the person's pace; build diagrams up one part at a time; the smallest complete answer first; keep why's hedges intact | `dev-onboard-teach` |
| `how` | one read-only explorer per question, writing for a senior engineer new to the area | `dev-onboard-how` |
| `why` | answer from the record, cite the authors, mark inferences, say what the record does not answer | `dev-onboard-why` |
| `architect` | trace every system touched; one owner per piece of state; one way to do each task | `dev-onboard-map` |
| `figure-it-out` | a falsifiable definition of done up front; verification harness before the work; hypothesis per unit; VERIFIED / NOT VERIFIED / INCONCLUSIVE | the exam, the router's pass / fail / blocked |
| `blast-radius` | the evidence ladder: said, pointed, walked, ran | the `evidence` field on every claim; `predict` questions |
| `show-me-your-work` | one append-only decision log, one row per decision with evidence | `decisions.tsv` |
| `recall` | rebuild context from the shared record before resuming | refresh mode |
| `reflect` | durable lessons go to structure, not prose | the write-back's verified-only rule |

pstack's `teach` says no quizzes, and this loop is built on one. Both are right: `teach` is about a
person, who should not be tested to prove they listened. The quiz tests an agent's map, which has
no feelings and every reason to overstate itself. `dev-onboard-teach` keeps pstack's rule for people.

## Build order

The loop ships as a stack of small pull requests, evaluator first:

1. `qa-onboard-quiz` + the map and quiz contracts + this doc (the exam exists before anything learns)
2. `dev-onboard-survey` (deterministic orientation)
3. `dev-onboard-how` and `dev-onboard-why` (the cited cards)
4. `dev-onboard-map` + `check_map.py` (assembly and the architect lens)
5. `dev-loop-onboard` (router, write back, staleness, decision log)
6. `dev-onboard-teach` (the human-facing end)
