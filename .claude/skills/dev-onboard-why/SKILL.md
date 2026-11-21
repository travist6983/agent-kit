---
name: dev-onboard-why
description: >-
  The onboarding loop's why lens: answer why one area of a codebase is built the way it is, from
  the record, not the code. Trigger for "why is it done this way", "what was the reasoning
  behind X", "mine the history of this module", or when dev-loop-onboard fans out over the
  survey's areas or routes a why-gap question back. record_bundle.py gathers the commits that
  touched the area (the introducing one always kept), the pull requests they name and the
  decision docs that point at it; one agent reads that bundle, opens pull requests through
  vcs_host read operations, and writes a card of why claims that cite the record or are marked
  as inference. check_card.py proves the refs. Does NOT read code for behaviour (dev-onboard-how).
allowed-tools: Read, Glob, Grep, Bash, Agent, Write
---

# dev-onboard-why: the reasons, in the authors' words or marked as a guess

Code shows what a system does. It almost never shows why. The why lives in commit messages, pull
request descriptions and decision docs, and it is the part a newcomer most often gets wrong: they
"clean up" a constraint that was there on purpose. This skill reads the record for one area and
writes down only what the record supports, with a ref, and marks everything else as inference with
an honest confidence.

The shape comes from pstack's `why` skill (answer from the record, cite the authors, separate what
they said from what you infer, and say what the record does not answer), adapted to write
checkable cards.

## What this block does NOT do (atomic boundary)

It does not explain behaviour (that is `dev-onboard-how`, run in parallel), assign blame, or judge
whether a decision was right. It names no people: claims cite commits, pull requests and docs.
It writes one card per area and checks it.

## Success criteria and failure categories (declared first)

- **Pass:** `cards/<area>.why.json` validates and `check_card.py --expect-lens why` exits 0: every
  claim is kind `why`; a claim with `source: record` cites a `commit:`, `pr:` or `doc:` ref that
  exists; an inference carries `likely` or `guessing`, never `certain`.
- **Thin record (not a failure):** few or no commits and docs touch the area, or the clone is
  shallow. The card says so in `gaps` and the bundle's notes go in the decision log. A card of only
  gaps is honest and passes.
- **Retry once / failed:** as for `dev-onboard-how`: one hand-back with the error list, then the area
  is logged as unlearned.

## Inputs / Outputs

- `REPO` (read only), `SURVEY`, `AREA`, `MODE` (`fresh`, or `missing` for a why-gap with `FOCUS`
  holding the lost questions).
- Output: `<evidence.root>/onboard/<project>/cards/<area>.why.json`, plus the bundle at
  `cards/<area>.record.json`.

## Procedure

### 1. Gather the record (deterministic)

```bash
python3 .claude/skills/dev-onboard-why/scripts/record_bundle.py \
  --repo "$REPO" --area "$AREA" --paths <area paths> \
  --decisions <survey decisions> --out "$EVID/cards/$AREA.record.json" [--limit 40]
```

It lists the commits that touched the area (newest first, capped, plus the commit that first added
each path, because that is where intent is most often stated), the pull request numbers they
mention, and the decision docs that name the area or that its code points at. It leaves author
names out. If the survey says the clone is shallow, unshallow before this step when the history
matters, or accept a thin record and say so.

### 2. Read (one agent per area, in parallel with the how readers)

Hand the agent the brief in `references/why-brief.md` and the bundle. It reads the decision docs
first, then the introducing commits, then the pull requests the bundle names, opened through the
`vcs_host` adapter (`get_pr id=<n>`, read operations only; never comments, labels or reviews). For
each reason it finds it writes a claim:

- **From the record:** what the authors wrote, close to their words, with `source: record`,
  `confidence: certain` only when the text says it outright, and a ref (`commit:<sha>`, `pr:<n>`,
  `doc:<path>`).
- **Inference:** a reason the record implies but does not state, with `source: inference`,
  `confidence: likely` or `guessing`, and the refs it is inferred from when there are any.
- **Not answered:** a question a newcomer would ask that the record does not answer goes in
  `gaps`. It is never filled with a plausible story.

Hedges survive: when a commit says "for now" or "until we move to X", the claim keeps that.

### 3. Check

```bash
python3 .claude/skills/dev-onboard-how/scripts/check_card.py \
  --repo "$REPO" --card "$EVID/cards/$AREA.why.json" --expect-area "$AREA" --expect-lens why
```

`commit:` refs are checked against the repo's history; `doc:` refs must be files; `pr:` refs are
taken as given, since only the adapter can open them.

## Key principles

- **The record, not the code.** If only the code says it, it is a how claim or an inference.
- **Certain means quoted.** An inference that reads as fact is how a wrong reason spreads.
- **A gap is a finding.** "The record does not say why paid invoices cannot reopen" tells the next
  engineer to ask a person before changing it.
- **No people.** Claims cite what was decided, never who to blame.
