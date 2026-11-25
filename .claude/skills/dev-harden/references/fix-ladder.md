# The fix ladder: where a correction should live

Adapted from pstack's ranking of fixes (MIT, Lauren Tan; github.com/cursor/plugins, `pstack`).
The rule there is short: encode a lesson in structure, not prose. A guideline is the last place
to put a lesson, because nothing fails when an agent ignores it.

Walk the bands top down for every candidate and stop at the first one that can catch the mistake
without false positives. Record a real reason for every band you pass over in `why_not_higher`;
validate.py (DEV-HARDEN-L1) refuses an artifact that skips a band silently.

| Band | Catches it by | Output here | Who lands it |
|---|---|---|---|
| structure | making the mistake impossible to write | `ticket.md` drafted | a loop or a human, in the target repo |
| static | failing on the added lines (conventions rule, linter, type) | `rule` (probed) or `ticket.md` for a target linter | a human `--apply`, or the ticket |
| verification | running the app and reading real state | `expectation` via `kb propose` | a human blesses it |
| prose | being read | `guideline` text with one failure example | a human pastes it |

## structure

Ask: is there a design where this mistake has nowhere to go? A typed wrapper instead of a raw call,
one owner for a piece of state instead of two copies kept in sync, removing the export that keeps
getting misused, a required argument instead of an optional one people forget.

Pick structure when the fix is local and boring. Pass over it, with the reason, when it needs a
redesign nobody has agreed to; the next band can hold the line until then.

## static

Two routes.

**Conventions rule** (this repo's gate). Use it when one regex over an added line decides it: a
banned call, a banned import, a hard-coded value, a suppression comment. Format is in
`dev-pre-pr/references/violation-rules.md`. Prove it with `scripts/rule_probe.py`.

**Target linter rule.** Use it when the check needs real parsing (scope, types, call graphs) or the
target already runs a linter the team trusts. Draft a ticket with the exact config; common
recipes:

- ESLint `no-restricted-syntax` with a selector, e.g.
  `CallExpression[callee.name='setTimeout'][arguments.1.value=0]`, and a `message`.
- ESLint `no-restricted-imports` / `no-restricted-globals` with `message` for a banned module or
  global.
- Ruff `banned-api` (`flake8-tidy-imports.banned-api`) with `msg` for a banned Python call.
- A type that cannot be built wrong, e.g. a branded id type, when the language has one.

Every one of these takes a message. Write it the same way as a conventions `fix`.

### Writing the message (the part the agent reads)

The message is an instruction to the next agent, at the moment it is about to repeat the mistake.

- Say what to do instead, not only what is wrong.
- Name where the real fix lives when the mistake is a symptom of something else.
- One or two sentences. No "please", no "consider".
- It is not the rule label. `rule_probe.py` rejects a `fix` under 40 characters or one that only
  repeats `rule`.

Weak: `Avoid setTimeout.`
Strong: `Do not add a timer here. The flash means two pieces of state disagree for one frame: keep
the value in one place and have the other read it.`

### Fixtures

Every rule ships with at least one `must_trip` (usually the offending line from the review) and
one `must_pass` (the compliant version, ideally the line a careful fix would write). The probe
runs each fixture alone through the real scanner. A rule that flags its own `must_pass` is a
false positive; narrow it before anything else.

### Broken windows

The gate reads added lines only. Copies already in the repo are not blocked, and an agent reading
nearby code will copy them. The probe's `inventory` counts them. When `count > 0`, draft a cleanup
ticket listing the sample, so the rule and the cleanup land together.

## verification

Only running the app proves it: a value that renders wrong, an ordering bug, a permission leak.
Propose an expectation through `kb propose` with the exact observable statement. Never bless it;
that is human-explore's step.

## prose

The last band. Use it when every band above would either miss the mistake or fire on correct
code. The paragraph must include one concrete example of the failure ("Wrong: ... Right: ..."),
because a rule without an example is the kind of prose agents already skim past. The artifact
carries the text; a human decides whether it goes into the guidelines.

## Review misses

A review miss (a prod-regressed run, or a reviewer-eval case the reviewer missed or only partly
caught) arrives through `collect_misses.py` with a `miss` block and an empty `lacked`. Before
placing it, answer one question: what did the reviewer not have in front of it? The answer is
what the next review needs, and validate.py (DEV-HARDEN-M1) refuses a miss artifact that does not
say it in `miss.lacked {kind, statement}`.

| kind | the reviewer lacked | example (neutral domain) | the walk starts at |
|---|---|---|---|
| `check` | a rule, type or test that would have failed | an Invoices total rounded in two places; no rule bans the second rounding call | structure, then static |
| `context` | the code it needed, outside the diff and the context pack | the fix scoped one invoice query helper; a second caller that builds the query by hand was not shown | structure; else prose aimed at the context pack or the reviewer brief |
| `expectation` | the intended behaviour, written where it reads | nobody wrote down that an archived Project keeps its Members read-only | verification (`kb propose`) |
| `instruction` | being told to look | the review brief for Workspaces never says to check the scope filter on new queries | prose, naming the destination file (target guidelines, conventions `review_brief`, or a skill reference) in the guideline's first line |
| `nothing` | nothing: the file, the brief and a matching rule were all there | a narrow special case approved despite the masking rule | `declined`; suggest the run as an eval case (`reviewer_eval.py candidates`) |

The kind tells you where to start, never which bands to skip: a miss walks the same ladder, with
the same `why_not_higher` duty for every band above the one it lands on. `nothing` must end
`declined` (no check or prose is justified when nothing was missing), and declining a miss that
did lack something needs a `decline_reason`. A human applies every outcome, including any edit to
a guidelines file, the review brief or a skill.

## Declining

`declined` is a real answer. Decline when the evidence is thin (one occurrence, no human ask), when
the call is a judgment the code review already makes well, or when every draft check fires on
correct code and the cost of the mistake is low.
