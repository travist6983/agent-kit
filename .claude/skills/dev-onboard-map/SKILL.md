---
name: dev-onboard-map
description: >-
  The onboarding loop's assembly step and architect lens: turn the survey and the checked how and
  why cards into one map of a codebase. Trigger for "build the map of this repo", "merge what we
  learned", "where does new code go in this codebase", "who owns this state", or when
  dev-loop-onboard has every card for a round. One agent reads the cards and writes architect.json
  (a plain summary, area names and purposes, merges, and owner and rule claims that span areas);
  assemble_map.py merges it all deterministically, keeps a verified claim only while it is
  unchanged, and writes map.json and map.md; check_map.py refuses uncited claims, broken cites,
  certain inferences and two owners for one thing. Does NOT read the code from scratch or grade.
allowed-tools: Read, Glob, Grep, Bash, Write
---

# dev-onboard-map: one map, one owner for everything in it

The cards are many readers' notes. The map is what the next engineer, and the quiz's examiner,
actually work from, so it has two jobs the cards cannot do alone. It has to be one consistent
model: every piece of state with exactly one owner, every kind of change with one place it
belongs. And it has to be assembled the same way every time, so a relearn changes only what it
was meant to change.

The architect lens comes from pstack's `architect` skill (trace every system a change touches;
give each piece of state one owner; one way to do each task), applied to describing a codebase
instead of designing one.

## What this block does NOT do (atomic boundary)

It does not read the code from scratch: if a card is missing something, that is a gap for the quiz
to find and the router to send back to `dev-onboard-how`. It does not grade, write to the brain, or
edit the repo. It writes `architect.json`, `map.json` and `map.md` and lints the result.

## Success criteria and failure categories (declared first)

- **Ready for the quiz:** `map.json` validates against `contracts/dev-onboard-map.json` (written
  through `contracts/emit.py`) and `check_map.py` exits 0.
- **Lint errors:** two owners for one symbol, an uncited claim, a broken cite, a certain inference,
  a dependency on a missing area, a duplicate id. Fix them in `architect.json` (for the lens's own
  claims and area changes) or send the card back to its lens; never hand-edit `map.json`.
- **Warnings:** an area with no claims, or with how claims but no owner or rule claim. These are
  where the quiz will likely find gaps; say so in the decision log and let the quiz decide.

## Inputs / Outputs

- `REPO`, `SURVEY`, the round's checked cards in `cards/`, and on a relearn the `PRIOR` map.
- Output: `<evidence.root>/onboard/<project>/{architect.json, map.json, map.md}`.

## Procedure

### 1. The architect's lens (one agent, reads the cards, may open cited lines)

Read every card's summary and claims and the survey's areas and edges, then write `architect.json`
in the shape described in `references/architect-lens.md`:

- **summary:** what the system is, in two or three sentences a senior engineer would say out loud:
  the general name for the thing first, then what this codebase uses it for.
- **areas:** a human name and a one-sentence purpose for each; rename an id when the survey's
  path-based one would confuse (`app` holding only `main.py` is better called `entry`); merge areas
  that are one thing split across directories.
- **claims (ids `<area>.a<n>`):** the cross-area facts no single card could see. For each piece of
  state the cards mention, one `owner` claim naming the one module that changes it. For each kind
  of change a newcomer would make (a new endpoint, a new rule, a new table), one `rule` claim naming
  where it goes. Each cites the lines that show it, like any other claim.
- **open_questions:** contradictions between cards you could not settle from the cited lines. Do
  not pick a side without evidence; the quiz and the relearn will.

### 2. Assemble (deterministic)

```bash
python3 .claude/skills/dev-onboard-map/scripts/assemble_map.py \
  --survey "$EVID/survey.json" --cards "$EVID/cards" --architect "$EVID/architect.json" \
  --out "$EVID/map.json" --md "$EVID/map.md" [--prior "$EVID/map.prev.json"]
```

What it does with a prior map: a `fresh` card replaces every claim of its area and lens; other
modes add claims and replace by id; `drops[]` are removed. A claim that comes through unchanged
keeps its `verified_round`; a changed one starts unverified, because the quiz has not tested the
new words. Commands are copied from the survey. Card gaps become open questions tagged with their
area. A tree with no git history gets the all-zero commit, which the staleness check treats as
stale everywhere.

### 3. Lint

```bash
python3 .claude/skills/dev-onboard-map/scripts/check_map.py \
  --repo "$REPO" --map "$EVID/map.json" --survey "$EVID/survey.json"
```

## Key principles

- **One owner per piece of state.** Two `owner` claims for one symbol is an error, not a nuance.
- **Judgment as data.** The agent writes `architect.json`; a script writes the map. A relearn
  changes the map only through a card or the lens, so every change has a source.
- **Verified means tested as written.** Editing a claim's words resets it; the quiz earns it back.
