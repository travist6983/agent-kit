---
name: dev-loop-onboard
description: >-
  Recipe: the onboarding loop. Gets an agent up to speed on a codebase it has never seen and
  proves it: survey, then one how and one why reader per area in parallel, then the map with the
  architect lens, then a blind quiz, then a router that relearns only the areas and failure classes
  the quiz exposed, then a write back of verified claims to the shared brain. Trigger for "onboard
  onto this repo", "learn this codebase", "get up to speed on this repo", "build a map of this code we
  can trust", "refresh the map of this repo", or before a loop works in a repository the brain has no
  map of. THIN orchestrator: sequencing and gates only; the work lives in dev-onboard-survey,
  dev-onboard-how, dev-onboard-why, dev-onboard-map and qa-onboard-quiz. Read only on the target
  repo: it never edits code, opens a pull request or posts anywhere. Bounded by onboard_loop in
  loop-limits.json, the R5 consumption cap and the R9 pause. Not qa-dream and not dev-harden.
allowed-tools: Read, Write, Bash, Agent, Skill, ToolSearch
---

# dev-loop-onboard: the onboarding loop

A thin orchestrator. It sequences the onboarding skills and enforces the gates; it never reads code
or writes claims itself. The whole design, and why each piece is there, is `docs/onboarding-loop.md`.
The decisions are scripts, each tested by `.claude/loops/tests/test_onboard_loop.sh`:

| Script | Decides | Writes |
|---|---|---|
| `scripts/onboard_router.py` | the one next action after a quiz round, and the relearn jobs | `route.json`, `carryover.json` |
| `scripts/mark_verified.py` | which claims a round verified | `verified_round` on `map.json` |
| `scripts/map_staleness.py` | which areas moved since the map's commit | `stale.json`, `changed.txt` |
| `scripts/onboard_writeback.py` | what reaches the brain, and as what status | a plan; the brain on `--apply` |
| `scripts/decide.py` | nothing; it records | one row in `decisions.tsv` |

## The sequence

```
 [refresh: map_staleness] ─▶ dev-onboard-survey ─▶ dev-onboard-how ∥ dev-onboard-why (per area)
                                                              │
     ┌────────────────────────────────────────────────────────┘
     ▼
 dev-onboard-map ─▶ qa-onboard-quiz ─▶ onboard_router ─▶ mark_verified
                         ▲                   │
                         │   relearn/requiz  │   write-back / stop-*
                         └───────────────────┤
                                             ▼
                                   onboard_writeback ─▶ run report
```

All files live under one run directory, `EVID="<evid_root>/onboard/<project>"` (`evidence.root`
from `stack.config.json`, default `runs`). Files on disk are the source of truth between steps.

## How a run goes

0. **Gates first.** `kb pause --status` (R9). A set or unreadable pause halts before any agent is
   spawned. Record the start time; it is `--started-at` for every script.
1. **Refresh or fresh.** If `$EVID/map.json` exists, run
   ```bash
   python3 scripts/map_staleness.py --repo "$REPO" --map "$EVID/map.json" \
     --out "$EVID/stale.json" --changed-out "$EVID/changed.txt"
   ```
   `fresh` with nothing changed: skip to the quiz to re-check the map, or stop if the brain already
   holds it at this commit. `stale`: relearn only `stale_areas` (steps 2 to 4 for those areas, with
   the old map as `--prior`). `unknown` or any `unassigned` path: run the full survey and every area.
2. **Survey.** `dev-onboard-survey` (a script, no model). Log the area cut with `decide.py`.
3. **How ∥ why.** For every area, spawn one `dev-onboard-how` agent and one `dev-onboard-why` agent
   **in one batch**, as `Agent` calls, never `Skill`: neither reads the other's output, and a fresh
   context per reader keeps one area's guesses out of the next. Each card must pass `check_card.py`
   before it counts; a card that fails twice is logged and the area goes into the map as a gap.
4. **Map.** `dev-onboard-map`: the architect agent writes `architect.json`, then `assemble_map.py`
   and `check_map.py`. A map with lint errors never reaches the quiz. On a later round, pass the last
   map as `--prior` so verified claims survive and wrong ones are dropped.
5. **Quiz.** `qa-onboard-quiz`, round `n`: writer (code only), examiner (map only), judge (key and
   answer only), each a fresh `Agent`, then `grade_quiz.py`. Round 2 and later include
   `round-<n>/carryover.json` unchanged, and pass `--changed-files "$EVID/changed.txt"` on a refresh.
6. **Route.**
   ```bash
   RD="$EVID/quiz/round-$N"
   python3 scripts/onboard_router.py --quiz "$RD/quiz.json" --out "$RD/route.json" \
     --prior "$EVID/quiz/round-$((N-1))/quiz.json" --questions "$RD/questions.json" \
     --carryover-out "$EVID/quiz/round-$((N+1))/carryover.json" \
     --budget-spent "$SPENT" --paused "$PAUSED" --started-at "$T0"
   python3 scripts/mark_verified.py --map "$EVID/map.json" --route "$RD/route.json"
   ```
   Then act on `action`, and on nothing else:

   | action | do |
   |---|---|
   | `relearn` | run each job in `jobs` (step 3 for that area and lens, in the job's `mode`, with `topics` as the focus and `drop_claims` for a correct-mode card; a `resurvey` job re-runs the survey first), then steps 4 to 6 with `n+1` |
   | `requiz` | the map is unchanged; quiz again with `n+1`, replacing the roles `requiz` names |
   | `write-back` | step 7 |
   | `stop-rounds`, `stop-stagnant` | step 7, then the report names every question still lost |
   | `stop-budget`, `halt-paused`, `escalate` | no more agents; step 7 in dry run only, then the report |

   A relearn reader is told the lost question's topic, never its answer key: a reader that sees
   the key would only teach the map that one answer, and the next round's new questions would show it.
7. **Write back.** Always plan; apply only when the run is allowed to write to the brain.
   ```bash
   python3 scripts/onboard_writeback.py --map "$EVID/map.json" --quiz-root "$EVID/quiz" \
     --project "$PROJECT" --loop-id dev-loop-onboard --run-id "$RUN" --out "$EVID/writeback.json"
   # then the same command with --apply
   ```
   Verified navigational claims (owner, rule, gotcha, command, where) land as `active` lessons of
   kind `codebase`; unverified ones land as `proposed` only when the map passed, for a person to
   bless with `kb bless lessons`. Metrics go in every run. The receipt in the report is rendered
   from what `--apply` returned, not from what was planned.

Log every decision that changes the run's path (an area cut, a dropped card, a route action, a
skipped write) with `scripts/decide.py --log "$EVID/decisions.tsv"`.

## The run report

Lead with what needs a person: questions still lost and their areas (from the last `route.json`),
`proposed` lessons waiting to be blessed, open questions from the map, and any `escalate` reason.
Then the score by round, the areas relearned and why, and the path to `map.md`. A map that did not
pass says so in its first line; it is a partial map, and its verified claims are still good.

## Stop conditions

- Pass: score at or above `pass_threshold`, no area under `area_floor`, a trustworthy question set.
- `max_rounds` reached, or a question lost twice for the same reason (stagnation): stop and hand
  back what is still unknown. A third read of the same files is not the fix.
- Blocked twice for the same reason, or blocked on missing inputs: escalate.
- R5 cap, R9 pause: stop at once.

## Key principles

1. **Thin.** Sequence and gates; the skills do the work and the scripts make the decisions.
2. **The exam decides.** Nothing is "learned" until a blind examiner answered from it.
3. **Every miss is classified before anything changes**, and the class picks the next move.
   bad-key and outside-map are the quiz's faults, so they change a role and never the map.
4. **A producer never grades itself.** Readers, writer, examiner and judge are separate `Agent`
   spawns with fresh contexts.
5. **Read only on the target repo.** The why reader uses `vcs_host` read operations; nothing writes.
6. **Only verified knowledge goes in as active.** Everything else is proposed or stays in the file.
