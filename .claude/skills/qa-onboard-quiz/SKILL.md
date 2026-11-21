---
name: qa-onboard-quiz
description: >-
  The onboarding loop's evaluator: a blind exam of a codebase map. Trigger for "quiz the map",
  "test what we learned about this repo", "did the agent really learn this codebase", "grade
  round N of the onboarding loop", or when dev-loop-onboard reaches its quiz step. Three fresh-context
  agents that never see each other's inputs: a writer reads the CODE and writes cited questions
  with answer keys (locate, trace, predict, why, owner), an examiner reads ONLY the map and
  answers, a judge grades key against answer. grade_quiz.py then checks every cite against the
  real files, discards bad keys, blocks a round whose examiner peeked at the code, classifies
  each miss (missing, wrong, vague, stale, why-gap) and writes quiz.json (pass | fail |
  blocked). Does NOT learn, fix the map, or decide what to relearn; the router reads its output.
allowed-tools: Read, Write, Glob, Grep, Bash, Agent
---

# qa-onboard-quiz: prove the map, not the mapper

The onboarding loop is only as good as its exam. An agent that read a codebase will always say it
understood it, and a summary that sounds right reads the same whether it is true or not. So this
skill never asks the learner how it did. It asks a stranger who only has the map to answer
questions a second stranger wrote from the code, and a third stranger grades them. Then a script
checks every file and line anyone cited.

Read `references/question-kinds.md` before writing a question set. It holds the five kinds, how
many of each, and what makes a key good.

## What this block does NOT do (atomic boundary)

It does not learn, read the record, edit the map, or pick what to relearn. It does not quiz a human
(that is `dev-onboard-teach`, which never quizzes). It produces one graded `quiz.json` per round and
stops. The recipe's router (`dev-loop-onboard`) reads it and decides what changes.

## Success criteria and failure categories (declared first)

- **Pass:** mean score >= `onboard_loop.pass_threshold` (loop-limits.json, default 0.8), no area with
  two or more scored questions below `onboard_loop.area_floor` (default 0.6), the question set was
  trustworthy, and all three roles ran blind.
- **Fail (a real negative):** the map could not answer enough. Every lost question carries one class:
  `missing` (nothing in the map), `wrong` (the map contradicts the code), `vague` (partial, no
  precise cite), `stale` (the code moved since the map commit), `why-gap` (the record was never
  mined). The router turns each class into a different next move.
- **Blocked (could not tell):** `no-questions`, `missing-input`, `missing-grade` (a judge result
  that never came back is a reported failure, never filtered away), `bad-key-rate` (too many keys
  cite nothing real, so the writer failed, not the map), `examiner-not-blind`. Blocked never routes
  to relearning.

## Inputs / Outputs

- `REPO`: the codebase checkout, at the map's commit.
- `MAP`: `<evid_root>/onboard/<project>/map.json` (contract `contracts/dev-onboard-map.json`).
- `ROUND`: the round number, from the recipe. Carryover questions from the last round come in as
  `carryover.json` (the router writes it); include them unchanged so a fix is re-tested.
- Optional `CHANGED_FILES`: paths changed since the map commit (from the staleness check).
- Output: `<evid_root>/onboard/<project>/quiz/round-<n>/{questions,answers,grades,quiz}.json`.
  `quiz.json` validates against `contracts/qa-onboard-quiz.json`; `grade_quiz.py` writes it through
  `contracts/emit.py`, so a contract violation never lands on disk.

## Procedure

### 1. Writer (fresh agent, reads the code, never the map)

Spawn an `Agent` with read access to `REPO` and no path to the map. Hand it the area list from
`map.json` (ids, names, paths only, never claims) and `references/question-kinds.md`. It writes
`questions.json`: `{"questions": [{id, kind, area, question, key: {text, cites: [{path,
line_start, line_end, symbol}]}}]}`. Each key cites the lines that prove it and names a symbol that
must sit in those lines. Questions come from the code a newcomer would actually need, not trivia.

### 2. Examiner (fresh agent, reads only the map)

Spawn a second `Agent`. Give it `map.json` and `questions.json` with every `key` stripped, and tell
it plainly that it may not open the repo. For each question it answers from the map and names the
claim ids it used: `{"answers": [{id, text, cites, from_claims, not_in_map}]}`. If the map has
nothing, it says `not_in_map: true` instead of guessing. A guess that happens to be right teaches the
loop nothing and hides a gap. The grader enforces blindness: every file-and-line the examiner cites
must be covered by a claim it named, so an examiner that read the code blocks the round.

### 3. Judge (fresh agent, reads key and answer, nothing else)

Spawn a third `Agent` with the questions, their keys and the answers. It grades each answered
question `correct`, `partial` or `wrong` with a one-line reason: `{"grades": [{id, grade, reason}]}`.
Partial means the answer is in the right place but would not let a newcomer act. The judge runs on a
model at least as strong as the examiner.

### 4. Grade (deterministic)

```bash
python3 .claude/skills/qa-onboard-quiz/scripts/grade_quiz.py \
  --repo "$REPO" --map "$MAP" --round "$ROUND" \
  --questions "$RD/questions.json" --answers "$RD/answers.json" --grades "$RD/grades.json" \
  --out "$RD/quiz.json" --judged-blind [--changed-files "$CHANGED"] \
  [--writer-model M --examiner-model M --judge-model M]
```

Pass `--judged-blind` only when steps 1 to 3 really were three separate fresh agents. The script
prints one JSON line with `status`, `score` and `blocked_reason`.

### 5. Feed the brain

Record the round as a metric so the loop is measurable over time:
`kb metric --name onboard_quiz_score --value <score> --loop-id dev-loop-onboard --source qa-onboard-quiz`.
A metric write that fails is reported, never skipped. The quiz writes no knowledge rows; promoting
verified claims is the recipe's write-back step.

## Key principles

- **A producer never grades its own work.** The agent that wrote the map never writes, answers or
  judges its quiz.
- **The key is checked before the answer.** A wrong key would punish a right map, so a key that does
  not resolve is discarded and counted against the writer.
- **Not knowing is a finding.** `not_in_map` is the most useful answer an examiner can give.
- **Read the real state.** Cites are checked against the files on disk, never against what an agent
  says the file contains.
