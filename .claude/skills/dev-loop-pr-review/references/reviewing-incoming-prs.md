# Reviewing someone else's pull request

`dev-code-review` was built to judge a diff **our own loop produced**. An incoming pull request is
a different situation in one way that changes the output: a person already thought about this code,
and often left their reasoning in the diff. This file is the discipline for that difference.

The lesson behind it is general: a reviewer can produce findings that are factually correct and
still should not be posted. The rules below are what separates the two.

## The rebuttal rule

**Before reporting a finding, look for the author's answer to it — in a code comment, the commit
message, the PR description, or an existing PR thread. If they already addressed it and your
finding doesn't engage with their reasoning, don't post it.**

The shape of the case that produces this rule: a reviewer flags a per-item query inside the
bulk-archive handler for Tasks — a genuine, unambiguous violation of the repo's "never query in a
loop" rule, correctly located. Three lines above it, the author had written:

> *Going through the shared archive routine rather than setting the status inline is what records
> the pre-archive status and publishes the per-task updated event the activity feed listens to.*

The recommendation ("use the id set you already built") would have silently dropped the per-task
domain events. The finding was true and useless — worse than useless, because a reviewer who reads
one comment that ignores what they already wrote stops reading the next one.

### What counts as a rebuttal

A rebuttal names the tradeoff and accepts it. It reads like:

- *"Two extra periods are added because the range boundaries rarely line up — over-generating is
  safe: `dropOutOfRange()` removes any period that ends before the range starts."*
- *"`comment` is the entity name the update-comment handler already publishes; the realtime
  listeners key off that string, so it must not be renamed here."*

These tell you the author saw the thing you're about to say. **Post anyway only if you can show the
rebuttal is wrong** — and then your finding must be about the rebuttal, not about the original
observation. "You said X is safe because Y, but Y doesn't hold because Z" is a great comment.
Repeating the original observation at someone who already answered it is not.

### What does NOT count as a rebuttal

- A comment that merely *describes* the code (*"loop over the tasks and archive each"*) — that's a
  label, not a defence.
- A comment that acknowledges a limitation without engaging the consequence. On one PR the author
  wrote *"it is only on an unassigned task that an attachments-only save now skips a request
  carrying no changes"* — they saw the skipped request and judged it harmless. They had **not**
  seen that the only close-dialog call lives inside that same `if`. The finding engaged with what
  they missed, so it was posted, and it was the best finding of that sweep.
- A TODO or "we should fix this later." That's agreement, not rebuttal.

The discriminating question: **does their reasoning survive the thing I'm about to say?** If yes,
stay quiet. If no, say the part that breaks it.

## Never say the same thing twice

A PR re-enters the queue every time its author pushes, and an unfixed finding will be re-derived
on every one of those ticks. `dev-pr-feed post` therefore reads the PR's existing threads first and
drops any finding whose `file:line` already carries one of ours (recognised by the bot marker
`dev-pr-feed` stamps on every thread it posts). Skipped anchors come back as `duplicate_skipped` —
report them in the tick summary so a repeat is visible rather than silent.

The anchor is skipped **regardless of thread status**. Re-raising a thread a human resolved is
worse than a duplicate: it is arguing with them, and it teaches people that resolving a bot's
comment doesn't work.

This shows up on the second tick, not in design: a PR comes back with new commits, the original
finding is still unfixed, and without the guard the loop posts it a second time — the precise spam
the whole design exists to prevent. Take the general lesson: the re-review path is where a
PR-review loop turns into a nuisance, so anything it re-derives needs a reason to be said again.

**When re-reviewing, spend the effort on what is new.** Tell the reviewer what is already posted
and unfixed so it doesn't burn a pass rediscovering it. If a branch was rebased, separate the
author's new work from upstream code that merely arrived with the rebase — reporting the latter is
a fast way to look like you didn't read the diff.

## Withheld findings are recorded, never deleted

A finding you decline to post still goes in `review.json` with `"withheld": true` and a
`withheld_reason`. Two reasons this matters: the human running the sweep can overrule you, and a
finding that keeps getting withheld for the same reason across many PRs is a signal that a *rule*
is wrong — which is the sort of thing the brain should eventually learn.

Report withheld findings in the tick summary. Silently dropping them makes the loop look cleaner
than it is, and hides the judgment you actually exercised.

## An undelivered finding is not a finished review

The ledger's job is to answer "have we reviewed this?" — but what the author actually cares about
is "did anyone tell me?". When a post is refused (R9 pause, a permissions error), the honest state
is *not reviewed yet*: leave the PR unmarked so the next tick delivers the findings for real.

The tempting shortcut — mark it, note the previews, move on — produces a loop that reports clean
sweeps while quietly dropping real findings on the floor. A review nobody received did not happen.

The exception is a genuinely clean review. Zero findings means there was nothing to deliver, so
marking it is accurate.

## Verify the anchor, not just the claim

A finding's `file:line` becomes the comment's position on the PR. A comment pointing at the wrong
line reads as carelessness and taints the finding attached to it, however good that finding is.

Confirm every anchor against the checked-out worktree before emitting — `sed -n '<line>p'` on the
file is enough — and confirm the line is one this PR actually touched. Reporting a pre-existing
line as if the PR introduced it is the most common way to be technically right and wrong.

Also check counts you state as fact. On one PR a finding said a shared component change reached
"4 existing consumers"; it was 5. Nobody would have caught it, and it would have been quietly
wrong in a comment with the author's name on it.

## There is no `fix.json`, and no repro oracle

`dev-code-review` normally receives the failure class the fix targets. An incoming PR has none, so:

- **Don't manufacture a masking verdict.** R3 asks whether a fix hid the symptom of a *reproduced
  bug*. With no oracle there's nothing to have masked. `masking: true` here can only mean "this
  diff looks like it suppresses a symptom on its own terms" — say that, or say nothing.
- **The target repo's guidelines still apply in full** — the file named by
  `stack.repo_guidelines_file` and the ruleset in `.claude/loops/conventions.json` — and so do the
  six specialists. Only the R3 judge loses its footing.

## Severity, and what's worth a comment at all

`critical` and `high` are for defects: wrong output, a crash, a permission hole, a scale failure.

`medium` is worth posting when it's a *fact the reviewer needs in order to approve* — for example
"this PR is titled flag-gated, but these design tokens are redefined at the root and affect every
usage regardless of the flag." That isn't a bug; it's a scope disclosure, and it changes what
approving means. That is a good comment.

What isn't worth a comment: anything you'd preface with "nit," anything the author would answer
with "yes, on purpose," and anything you'd struggle to defend if they pushed back. **Zero findings
is a normal, healthy outcome** — a clean report that says *what it checked and cleared* is more
useful to the author than a padded list would have been.
