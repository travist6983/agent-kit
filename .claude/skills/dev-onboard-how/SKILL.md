---
name: dev-onboard-how
description: >-
  The onboarding loop's how lens: explain one area of a codebase from the code, as cited claims.
  Trigger for "how does this part of the repo work", "explain the invoices module", "trace what
  happens when X", "relearn the members area", or when dev-loop-onboard fans out over the survey's
  areas or routes a missing, wrong, vague or stale question back to an area. One read-only agent
  per area writes a card of atomic claims (what, how, where, owner, rule, gotcha, command), each
  citing file, lines and a symbol, then check_card.py proves every cite against the repo. Modes:
  fresh, missing, correct, deepen. Does NOT answer why (dev-onboard-why), merge cards
  (dev-onboard-map) or grade anything (qa-onboard-quiz).
allowed-tools: Read, Glob, Grep, Bash, Agent, Write
---

# dev-onboard-how: what the code does, with the line that proves it

An explanation of code is only useful to the next agent if it can be checked. "The service owns
state changes" is a sentence; "InvoiceService owns every state change, `app/invoices/service.py`
8-23, `class InvoiceService`" is a claim a script can verify and a quiz can lean on. This skill
turns one area into claims of the second kind.

The shape comes from pstack's `how` skill (one read-only explorer per question, written for a
senior engineer new to the area) and `blast-radius` (the evidence ladder), adapted to write
structured, checkable cards instead of prose.

## What this block does NOT do (atomic boundary)

It does not say why the code is the way it is: intent comes from the record, and that is
`dev-onboard-why`'s job, run in parallel. It does not edit the repo, merge cards into the map, or
grade itself. It writes one card per area and checks it.

## Success criteria and failure categories (declared first)

- **Pass:** `cards/<area>.how.json` validates against `contracts/dev-onboard-card.json` and
  `check_card.py --expect-lens how` exits 0: every claim cites real lines with a symbol in them, no
  claim is merely asserted, no why claims.
- **Retry once:** the card fails the check. Hand the reader its own error list (each names the
  claim id) and re-run as `attempt: 2` with `retry_reason`. A claim it cannot cite is dropped, never
  kept uncited.
- **Failed:** a second failure. The area goes into the run's decision log as unlearned, with the
  errors, and the quiz will show the gap. A card is never patched by the caller to make it pass.

## Inputs / Outputs

- `REPO` (read only), `SURVEY` (`survey.json`), `AREA` (an id from the survey), `MODE`
  (`fresh` | `missing` | `correct` | `deepen`), and for a relearn `FOCUS`: the lost questions'
  topics, or the claim ids graded wrong. The router writes these.
- Output: `<evidence.root>/onboard/<project>/cards/<area>.how.json`. On a relearn the card holds the
  new claims and, in correct mode, `drops[]` for map claims that the code contradicts.

## Procedure

### 1. Fan out (the caller)

One `Agent` per area, all in one message so they run in parallel. Each gets the brief in
`references/how-brief.md` filled with its area's paths, size, `depends_on`, tests, the survey's
commands and entry points, and the mode and focus. No agent gets another area's card or the map's
why claims: a reader primed with an answer stops reading the code.

### 2. Read (each agent)

The agent reads its area the way a senior engineer would on day one: entry points and public
surface first, then the main flows end to end, then the state and who changes it, then the traps.
It follows calls into other areas only far enough to name the edge. It writes claims as it goes,
each one sentence or two, each with the narrowest cite that proves it, and a `symbol` that must
appear in those lines. `evidence` says how far it got: `pointed` (here is the line), `walked` (the
flow was traced step by step), `ran` (a declared test or command exercised it, in a scratch copy,
never in the user's checkout).

What a good card covers, roughly in this order:

| kind | asks |
|---|---|
| `what` | what is this area, in the general name for the thing |
| `where` | where the pieces live; where to look first |
| `how` | the main flows, step by step, as a newcomer would trace them |
| `owner` | which module owns each piece of state and each decision |
| `rule` | where new code of each kind belongs (one way to do each task) |
| `gotcha` | what a newcomer would get wrong |
| `command` | how to build, test or run just this area, from the survey's commands |

### 3. Check

```bash
python3 .claude/skills/dev-onboard-how/scripts/check_card.py \
  --repo "$REPO" --card "$EVID/cards/$AREA.how.json" --expect-area "$AREA" --expect-lens how
```

One JSON line: `{ok, errors, warnings}`. Errors go back to the same reader once (see failure
categories). Warnings (a cite with no symbol) are logged, not fatal.

### Relearn modes

- **missing:** the map had nothing on `FOCUS`. Read for those topics; add claims; touch nothing
  else.
- **correct:** the claims in `FOCUS` were graded wrong. Re-read the code they cite. Put each wrong
  one in `drops[]` with what the code actually says, and write the correct claim with a new id.
  Never edit a wrong claim in place: the map removes it and the quiz re-tests the new one.
- **deepen:** answers were vague. Replace broad cites with the exact lines, and walk the flow
  (`walked`) instead of pointing at a whole function.
- **stale:** the router re-surveys first, then runs `fresh` on the changed area.

## Key principles

- **Narrowest cite that proves it.** A claim citing a whole file proves nothing.
- **Not knowing goes in `gaps`.** A reader that could not work something out says so; a guess
  dressed as a claim is what the quiz exists to catch, and it will.
- **Read only.** No edits to the repo, ever. Running a declared test to raise evidence to `ran`
  happens in a scratch copy.
