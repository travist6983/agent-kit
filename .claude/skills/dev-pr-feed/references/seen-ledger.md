# The seen-ledger — storage, semantics, and why the default backend is a file

## What it has to guarantee

The ledger answers one question: *have we already reviewed this pull request, as it stands right
now?* Three properties make it correct:

1. **Keyed on PR id + head commit.** Keying on the id alone makes the loop review each PR exactly
   once, ever — including going silent on the revision the author pushed in response to our own
   findings. That failure is invisible: the loop keeps running, reports "0 unreviewed", and looks
   healthy while doing nothing.
2. **Durable across a crash.** The file backend writes to a temp file, `fsync`s, then `os.replace`s.
   A crash mid-write leaves the previous ledger intact. A *truncated* ledger is worse than a stale
   one, because a half-written file is indistinguishable from "never reviewed" — and that means
   duplicate comments on a real person's PR.
3. **Fail closed on read.** If the ledger can't be read, `unreviewed` exits `3` with
   `ledger-unreadable` rather than returning every candidate. Reviewing everything because the
   ledger is down is how a small outage turns into a dozen pull requests full of duplicate bot
   comments.

## The entry

```json
{
  "pr_id": "123",
  "head_commit": "0123456789abcdef0123456789abcdef01234567",
  "reviewed_at": "<utc iso timestamp>",
  "verdict": "request-changes",
  "findings": 2,
  "mode": "dry-run",
  "reviewer": "dev-pr-feed"
}
```

`pr_id` is whatever the `vcs_host` adapter calls the PR — a number on most hosts, a branch name on
the `git-local` provider — stored as a string. The live path adds `write_ahead: true`,
`run_id` and `reviewed_commit_verified`, and moves `verdict` through `posting` → `posted`
(or `posted-partial` / `post-unconfirmed` when a send failed mid-way).

`mode` matters more than it looks: it distinguishes "we reviewed this and showed you a preview"
from "we reviewed this and the author has seen our comments." When you flip the loop to live
posting, entries written in `dry-run` mode are the ones whose findings never actually reached
anyone — `ledger --forget <pr>` re-queues them. A `live` entry at the PR's current head is also
what `post --live` refuses to repeat (`already-posted`).

## Two backends

### `file` (default) — `.claude/loops/pr-review-ledger.json`

A single JSON object keyed by PR id. Chosen as the default for two reasons:

**It is runtime state, not knowledge.** `loop-limits.json` already draws this line for
`infra-worktree`: *"The registry (`sandboxes.json`) is runtime state, not knowledge."* A record of
which PR ids we happened to have looked at is the same kind of thing — it teaches the next task
nothing, doesn't generalize, and would never be worth consolidating in a dream pass. The brain's
four stores are `app_map`, `expectations`, `flakiness` and `fix_lessons`; a review checklist isn't
any of them, and adding it would blur a separation `CLAUDE.md` is emphatic about keeping.

**It has no dependencies.** The file backend needs nothing but the filesystem, so the loop runs
on a machine that cannot reach the brain, and the one brain contact the live path makes — the R9
pause read — fails closed on its own. A ledger that lives only in the brain would make the whole
loop unrunnable whenever the brain is.

### `kb` — via `kb state --key loops.pr_review.<pr_id>`

Set `LOOP_PR_LEDGER_BACKEND=kb` to store entries in the brain's `kb_state` table instead. Use
this when the loop runs on more than one machine and the ledger has to be shared — that is the
case the file backend genuinely can't serve, and it's the reason the seam exists rather than
being hard-coded. The brain CLI path is `KB_CLI` (default: the `qa-kb` skill's `kb.py`).

One limitation to know: `kb` has no prefix-scan verb, so `ledger` (the whole-ledger dump) reports
that it can't enumerate under this backend. `unreviewed` is unaffected — it queries per candidate.

## The tick lock lives beside the ledger

`lock` writes `pr-review-tick.lock` in the ledger's directory: a record with an owner pid, an
acquire time and an expiry. It outlives a single process on purpose — a tick is an agent making
many calls over many minutes, so an `flock` held for one invocation would be released the moment
the probe exits. A lock past its TTL is stale and gets taken, so a crashed tick never blocks the
loop forever. Two processes with different `LOOP_PR_LEDGER_FILE` values therefore have different
locks; the runner pins one.

## Operational notes

- `ledger --forget <pr>` re-queues a single PR. Useful after a bad review, or to re-run a PR whose
  findings only ever went to a dry-run preview.
- The ledger is small and human-readable on purpose. When the loop does something surprising, the
  first useful question is "what did it think it had already seen?", and that should be one `cat`
  away, not a database query.
- Nothing prunes it. Entries for completed PRs are harmless — a PR that's no longer open never
  appears as a candidate, so its entry is simply never consulted again.
