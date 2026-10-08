# human-explore — the approval path

`human-explore` is the **human** learning producer, and it has a job no other skill has: it is the
**approval path** that promotes `proposed` expectations to `canonical`. Because a wrong canonical
expectation makes the evaluator optimize toward a wrong target, **only a human may bless** — and that
human is in this loop.

## Dual job

### 1. Capture the highest-quality signal (human drives, corrects live)
A tester drives the agent through a feature and **corrects it in real time** ("no, the Project filter
should persist after a reload", "that toast is wrong"). Those corrections are **ground truth** → they
become **canonical** expectations directly (`kb propose` then `kb bless --by <human>`, or bless an
existing proposal). The human's live judgment is the most valuable input the brain receives.

### 2. Promote proposed → canonical (the bless gate)
Review the `proposed` expectations that `qa-explore` / `qa-repro` / `qa-dream` have queued
(`kb search expectations --status proposed`), and **bless** the correct ones:
```bash
kb bless expectation --id <id> --by <human-id> [--expect-version <v>]
```
Reject the wrong ones (`kb reject ... --reason ...`). This gate is what keeps the evaluator honest —
it's the human checkpoint the whole propose→bless design depends on. **Never bless autonomously**: a
bless always carries a real `--by <human>`.

## Boundary
`human-explore` **writes canonical expectations** (with a human signature). It does not fix or ship. The human is always the authority on a bless.
