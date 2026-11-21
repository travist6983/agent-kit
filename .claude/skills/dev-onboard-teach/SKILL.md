---
name: dev-onboard-teach
description: >-
  The onboarding loop's human end: explain a codebase to a person from its map, plainly and at
  their pace, and turn every question the map cannot answer into a gap the next round relearns.
  Trigger for "walk me through this codebase", "teach me how this repo works", "explain the
  invoices area", "I'm new here, where do I start", or after dev-loop-onboard writes a map someone
  will work in. lesson_plan.py orders the map from where work enters, one area at a time, with a
  diagram that grows a box at a time and the standing of every claim (verified by the quiz,
  unverified, or an inference); record_gap.py keeps unanswered questions and turns them into
  relearn jobs. Answers only from the map and the code it cites; never quizzes the person unasked; says
  plainly when the map does not know. Does NOT build or grade the map.
allowed-tools: Read, Glob, Grep, Bash
---

# dev-onboard-teach: explain the map to a person

The rest of the loop exists so this conversation can be trusted. The map's claims are cited, the
quiz has tested what it could, and the standing of every claim is recorded. The teacher's job is
to pass that on without adding anything the map does not hold and without losing any of its
hedges.

The teaching rules adapt Lauren Tan's pstack `teach` skill (`cursor/plugins`, `pstack/`, MIT),
written in our own words: the smallest complete answer first, at the person's pace, a diagram
built up a piece at a time, and no tests of the person. The loop quizzes its map because a map
has every reason to overstate itself; a person who is learning does not need to prove it.

## Inputs / Outputs

- `MAP`: `<evid_root>/onboard/<project>/map.json` from `dev-onboard-map` (contract
  `contracts/dev-onboard-map.json`), and `REPO` read only, for showing the cited lines.
- Optional `FOCUS`: one area id, when the person asked about one part.
- Output: the conversation itself, plus `<evid_root>/onboard/<project>/teach-gaps.json` for the
  questions the map could not answer. Nothing else is written; the map is never edited here.

## How a session goes

1. **Plan, without a model.**
   ```bash
   python3 .claude/skills/dev-onboard-teach/scripts/lesson_plan.py \
     --map "$EVID/map.json" [--focus "$AREA"] --out "$EVID/teach-plan.json"
   ```
   `order` is where to start: an area nothing calls, then what it calls, all the way down, then
   the next. Each section's claims go plain to deep (what, where, how, owner, rule, gotcha,
   command, why). `diagram_steps[k]` is the diagram after the k-th area arrives.
2. **Open with the whole thing in three sentences.** The map's `summary`, what the person can run
   (`commands`), and the first area. Ask whether to go on in order or jump to what they need.
3. **One area at a time.** Show the diagram step for it, then its claims in order, each in your
   own plain words with its cite, so they can open the line. Stop after each area and let them
   steer: go on, go deeper, or ask something.
4. **Carry the standing.** A `verified` claim can be said plainly. An `unverified` one is said as
   what the map says and has not been tested yet. An `inference-likely` or `inference-guessing`
   claim keeps its hedge: "the record does not say; the likeliest reason is...". Never round an
   inference up to a fact, and never call the map complete when `open_questions` is not empty.
5. **A question the map cannot answer is a finding.** Say so in one line. Open the cited code if
   that settles it, and say you read the code, not the map. Either way record it:
   ```bash
   python3 .claude/skills/dev-onboard-teach/scripts/record_gap.py --gaps "$EVID/teach-gaps.json" \
     --question "<their words>" --area "<area id, if known>" --lens how|why
   ```
   Use `--lens why` for a why question, so it goes to the record, not the code.
6. **Close with what is open.** Name the gaps recorded this session and the map's own open
   questions. The next `dev-loop-onboard` run turns them into relearn jobs:
   ```bash
   python3 .claude/skills/dev-onboard-teach/scripts/record_gap.py --gaps "$EVID/teach-gaps.json" \
     --jobs --map "$EVID/map.json" --out "$EVID/teach-jobs.json"
   ```
   Each job has the same shape as a router job; an `unplaced` gap needs an area from the survey
   first. When a later round's reader answers a gap, close it with `--close <id> --by-round <n>`.

## Key principles

- **The map is the source.** Answer from its claims and the lines they cite. A fact from memory or
  a guess is not taught; it is recorded as a gap.
- **Smallest complete answer first.** One sentence that is true and whole, then detail only when
  asked. A person can always ask for more; they cannot un-read a wall of text.
- **Their pace, their route.** Stop after each area. A question beats the plan.
- **No quizzing the person unasked.** Check understanding by asking what they want next. If they ask
  to be tested, that is their call: a couple of questions answered by verified claims, with the cite.
- **Hedges survive.** What the map marks as unverified or inferred stays that way in every sentence.
- **Read only.** Nothing in the repo or the map changes during a lesson.
