---
name: dev-harden
description: >-
  Turn a correction that keeps coming back into the cheapest check that FAILS, instead of one more
  line of prose an agent can skim past. Runs between loop runs, never inside one. Collects the
  drafted rules dev-code-review attached to findings, the harden queue qa-dream and humans append,
  and review misses (prod-regressed runs and missed reviewer-eval cases), keeps what recurred or
  was confirmed, says what the reviewer lacked for a miss, walks the fix ladder top down
  (structure, static rule, verification, prose), proves a rule with the real pre-PR scanner,
  counts the copies already in the repo, and writes one harden.json per correction.
  Trigger for "harden this", "turn that correction into a check", "make that a lint rule", "the
  review missed this", "why did the reviewer miss it", "run harden", or as the scheduled job
  after the dream. PROPOSES only:
  a human applies a rule with rule_probe.py --apply --by. Does NOT edit the target repo, the
  conventions file, guidelines or skills, and never blesses an expectation.
allowed-tools: Read, Write, Glob, Grep, Bash
---

# dev-harden: corrections become checks, not prose

Every correction a human or a reviewer makes by hand is a lesson the next run can forget. A line in
a guidelines file is the weakest place to store it: the agent reads it once, competes it against
everything else in context, and repeats the mistake. A check that fails cannot be skimmed.
dev-harden is the step that moves a recurring correction up to the cheapest place that can enforce
it, and proves the check works before anyone relies on it.

The full loop, so each piece stays in its own skill:

1. **dev-code-review drafts.** When a validated finding is a mistake a check could catch, the
   reviewer attaches a `harden` block to it (band, why, candidate_rule, must_trip, must_pass). It
   never writes a rule.
2. **qa-dream and humans queue.** A fix lesson that recurs, or a human saying "stop making me
   correct this", becomes one line in `.claude/loops/harden-queue.jsonl`.
3. **A miss is queued.** A prod-regressed run, or a reviewer-eval case the reviewer missed, becomes
   a queue line with a `miss` block through `collect_misses.py` (this skill's own collector).
4. **dev-harden places and proves** (this skill), between runs.
5. **A human applies.** `rule_probe.py --apply --by NAME` is the only path into
   `conventions.json`, the same way `kb bless --by` is the only path to a blessed expectation.
6. **dev-pre-pr enforces.** From the next run the standards scan blocks the mistake, and the
   rule's `fix` text is what the agent reads when it re-fixes.

The idea of ranking fixes from structure down to prose, and of encoding lessons in structure,
comes from pstack (MIT, Lauren Tan). See `references/fix-ladder.md`.

## Success criteria and failure classes (declared up front)

**Success:** every ready candidate ends with exactly one `harden.json` that validates against
`contracts/dev-harden.json`, sits at the highest band that can really catch the mistake, and
records why every higher band was rejected. A proposed rule passed `rule_probe.py` with its
fixtures and carries its inventory count. A miss artifact names what the reviewer lacked.

**Failure classes (classify before adapting):**
- `prose-by-default`: proposed a guideline while a rule, a structural change or a verification
  could have caught it. validate.py's DEV-HARDEN-L1 refuses the artifact; fix the ladder walk.
- `one-off-hardened`: wrote a rule for a mistake seen once. Only recurrence or a human queues it.
- `weak-message`: a rule whose `fix` names the problem but not what to do instead. The probe
  refuses it; rewrite the message for the agent that will read it.
- `false-positive-rule`: the rule fires on compliant code. Narrow the pattern, add a must_pass
  fixture that proves it, or drop to a lower band.
- `broken-windows-ignored`: proposed a rule while existing copies remain and no cleanup ticket
  was drafted. The gate only reads added lines, so agents keep imitating the old copies.
- `miss-unexplained`: placed a review miss without stating what the reviewer lacked, or wrote
  `nothing` to skip the work. validate.py's DEV-HARDEN-M1 refuses the artifact; go back to the
  evidence and name the gap.
- `self-applied`: wrote to `conventions.json` or a guidelines file directly. Never; that is the
  human's step.

## What this skill does NOT do (atomic boundary)

It does not edit the target repo, `conventions.json`, any guidelines file or any skill. It does
not file tickets (it drafts them), does not bless expectations (it may propose one through
qa-kb), and does not apply a rule. It reads review artifacts, prod-verify results, eval scores and
the queue, runs the probe, and writes `<evid_root>/harden/<fingerprint>/`. The only other write is
appending miss entries to its own inbox, the harden queue, and only with `collect_misses.py --append`.

## Inputs

| Need | Source |
|---|---|
| evidence root | `evidence.root` in stack.config (default `runs/`), or `--evid-root` |
| review harden blocks | `<evid_root>/*/review.json`, findings with a `harden` block (dev-code-review v1.2.0) |
| prod misses | `<evid_root>/*/prod-verify.json` with verdict `prod-regressed` (infra-prod-verify) |
| eval misses | a reviewer-eval `score.json` (`reviewer_eval.py score --out FILE`), passed as `--eval-score` |
| the queue | `.claude/loops/harden-queue.jsonl` (fields: fingerprint, source, ref, statement, band?, candidate_rule?, miss?) |
| the target repo | the repo the loop works on, read only, for the inventory and for structure fixes |
| conventions file | the target's `conventions.json` if it has one, else `.claude/loops/conventions.json` |

## Procedure

### 0. Pause check
Run `kb pause --status` first. Proceed only on an explicit `"paused": false`. Paused means stop and
write nothing. If the brain is unreachable, carry on: only the verification band needs it, and
those candidates end `blocked` with reason `brain-unreachable`.

### 1. Collect
Queue the misses first, then collect everything:
```bash
python3 .claude/skills/dev-harden/scripts/collect_misses.py [--evid-root DIR] [--eval-score FILE ...] --append
python3 .claude/skills/dev-harden/scripts/collect_candidates.py [--evid-root DIR] [--min-occurrences 2]
```
`collect_misses.py` is idempotent by fingerprint. Report its `not_scored` (the reviewer never sat
the case, so it is not a miss), `false_flags` and `still_missed_after_decision` to the human; never
queue or hide them. An invalid `--eval-score` exits 2: stop and say so.

Work only the candidates with `ready: true`. A review miss is ready at one occurrence, because a
prod regression or a confirmed answer key is evidence, not a hunch. The rest wait for another
occurrence. Already decided fingerprints are listed under `decided` and are never redone (a
`blocked` one is retried).

### 1b. For a review miss, first say what the reviewer lacked
When the question is "why did the review miss this", the useful answer is what the reviewer did
not have in front of it, because that is the thing a check, a context fix or an instruction can
supply next time. Read `miss.had`, the run's `review.json`, `fix.json`, `context.json` and the
follow-up evidence (for an eval case, its `change.diff` through the materialized tree; never more
of the answer key than the score already exposes). Pick one kind and write one or two sentences
into `miss.lacked {kind, statement}`:

| kind | the reviewer lacked | the ladder walk starts at |
|---|---|---|
| `check` | a rule, type or test that would have failed | structure, then static |
| `context` | the code it needed (a caller, a bypasser, a sibling path) | structure; else prose aimed at the context pack or the reviewer brief |
| `expectation` | the intended behaviour, written where it reads | verification (`kb propose`) |
| `instruction` | being told to look (guidelines, `review_brief`, its own skill) | prose, naming the destination file in the guideline's first line |
| `nothing` | nothing: a judgment slip | `declined`; suggest it as an eval case via `reviewer_eval.py candidates` |

The kind says where to start, never which bands to skip: every band above the one you choose
still needs its `why_not_higher` reason. A human applies every outcome; dev-harden still edits no
guidelines, conventions or skill file. `references/fix-ladder.md` has an example per kind.

### 2. Walk the ladder, top down
Read `references/fix-ladder.md`. For each ready candidate, ask each band in order whether it can
catch this mistake, and stop at the first yes:

1. **structure**: can the code or API change so the mistake cannot be written (a typed wrapper, a
   single owner for the state, removing the footgun export)? Draft a ticket; do not code it.
2. **static**: can a pattern decide it from the added lines alone? Write a conventions rule, or,
   when it needs real parsing, draft a ticket for a target linter rule (ESLint, Ruff, a type).
3. **verification**: only running the app shows it? Propose an expectation through `kb propose`.
4. **prose**: nothing above can catch it without false positives. Draft a guidelines paragraph
   with one concrete failure example. Never write it to the file.

For every band you pass over, write one `why_not_higher` entry with the real reason. Declining is
allowed (`declined` with a reason) when the evidence is thin or the call is pure judgment that
review already covers.

### 3. Prove a conventions rule
Write the candidate (a collector candidate works as is) and probe it:
```bash
python3 .claude/skills/dev-harden/scripts/rule_probe.py --candidate cand.json --target-repo <target>
```
- Exit 3 lists failure classes. Fix the rule and probe again. After three tries, drop a band or
  decline, and say why.
- `fix` is written TO the agent: what to do instead and where the real fix lives, in one or two
  sentences. "Avoid setTimeout" fails; "Do not add a timer here. The flash means two pieces of
  state disagree for one frame: keep the value in one place" passes.
- `inventory.count > 0` means copies already exist. Draft a cleanup ticket listing
  `inventory.sample`, so the rule and the cleanup land together.

### 4. Emit
Write `<evid_root>/harden/<fingerprint>/harden.json` through
`contracts/emit.py --contract contracts/dev-harden.json`. Ticket drafts go beside it as
`ticket.md`. A miss artifact carries `review-miss` in `correction.sources` and the `miss` block
with `lacked` filled (DEV-HARDEN-M1). A conventions rule ends `rule-proposed` with the probe result; nothing reaches the
gate until a human runs:
```bash
python3 .claude/skills/dev-harden/scripts/rule_probe.py --candidate cand.json --apply --by NAME \
  --artifact <evid_root>/harden/<fingerprint>/harden.json --lesson-ref <ref>
```
It re-probes, appends the rule with a provenance comment, and flips the artifact to
`rule-applied` with `applied: {by, at}`.

### 5. Audit the existing rules
```bash
python3 .claude/skills/dev-harden/scripts/rule_probe.py --audit
```
Every rule already in the conventions file must say what to do instead. Report any `weak-message`
rule as a candidate of its own; do not rewrite it here.

## Key principles
1. **A check beats a sentence.** Prose is the last band, and the artifact must show why.
2. **Recurrence earns a rule.** One diff cannot show a mistake keeps coming back.
3. **Prove it with the real scanner.** The probe uses dev-pre-pr's own matcher, so a rule proven
   here behaves the same at the gate.
4. **Fix the broken windows with the rule.** The gate reads added lines only; old copies teach.
5. **A human applies.** The skill proposes; `--apply --by` is the one write path.
