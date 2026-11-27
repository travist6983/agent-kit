# Thread dispositions: what people did with the comments we posted

`python3 pr_feed.py dispositions [--pr ID ...] [--emit] [--budget-s N] [--limits PATH]`

Every review comment the PR-review loop posts LIVE is followed up on a later tick: did the
author fix it, say it was wrong, say it was intentional, or ignore it? That sorting is how the
loop measures its own noise. It is mechanical (no model call), read-only on the host, and
measured only: nothing gates on the result and nothing suppresses a future finding because of it.

## Where the state lives

Per-thread state is a `threads` map inside the PR's existing seen-ledger entry (see
`seen-ledger.md`), keyed by the host's thread id (or `<path>:<line>` when the host returned none):

```json
{"path": "a/f.js", "line": 30, "posted_commit": "<sha>", "posted_at": "…",
 "severity": "high", "dimension": "security",
 "disposition": "pending", "evidence": "awaiting-reply", "counted": false}
```

`post --live` writes one record per thread it lands; `post` at a new head and `mark` carry the map
forward; `ledger --forget` drops it with the entry. No brain store holds thread state, and no reply
text is ever stored.

## Classes

| Class | Meaning | Counted in the noise rate |
|---|---|---|
| `fixed` | the author changed the flagged code, or said so | yes (signal) |
| `wrong` | a human said the finding is wrong | yes (noise) |
| `intentional` | a human said the code is that way on purpose, or declined it | yes (noise) |
| `no-action` | closed or merged with the anchor untouched and no reply: it did not matter | yes (noise) |
| `unclassified` | a human replied in words the script cannot place, or the anchor could not be measured, and the PR closed | no, reported beside the rate |
| `pending` | not settled yet; read again next call | no |

## Precedence (first match wins, for a thread that is not yet settled)

1. **Latest human reply** (a reply whose body does not carry the bot marker):
   - its FIRST word, after stripping markdown and punctuation: `fixed` -> fixed; `wrong`, `fp` ->
     wrong; `intentional`, `wontfix`, `bydesign` -> intentional. Evidence `reply-token:<word>`.
   - else a phrase anywhere in it (case-insensitive; earliest wins): `false positive`, `not a bug`,
     `not an issue`, `incorrect`, `does not apply`, `doesn't apply`, `not applicable` -> wrong;
     `intentional`, `by design`, `as designed`, `on purpose`, `deliberate`, `won't fix`, `wontfix`
     -> intentional. Evidence `reply-phrase:<phrase>`.
2. **Host status** (lower-cased, non-alphanumerics stripped): `fixed` -> fixed; `wontfix`,
   `bydesign` -> intentional. Evidence `status:<s>`.
3. **Anchor changed** -> fixed, evidence `anchor-changed`.
4. **Thread resolved/closed**: anchor measured unchanged -> no-action (`closed-unchanged`);
   unmeasurable -> stays pending (`anchor-unmeasured`) while the PR is open.
5. **PR no longer open** (closed, merged, or gone from the host): a human reply present ->
   unclassified (`pr-closed-reply-unrecognized`); anchor unmeasurable -> unclassified
   (`pr-closed-anchor-unmeasured`); otherwise no-action (`pr-closed-unanswered`).
6. Otherwise pending (`reply-unrecognized` when a human replied, `awaiting-reply` when not,
   `thread-not-on-host` when a recorded thread is missing from an open PR).

The bot's comment footer asks for a one-word reply (`fixed`, `wrong` or `intentional`) so step 1
is usually exact; the phrase list is the fallback.

## The anchor rule

`git diff -U0 <posted_commit> <head> -- <path>` in the target checkout. The anchor counts as
changed when the file was deleted or renamed away, or when any hunk's OLD-side range
`[a, a + max(b,1) - 1]` intersects `[line - tolerance, line + tolerance]`.
`tolerance` is `pr_review_loop.disposition_line_tolerance` in `.claude/loops/loop-limits.json` (3).
Measured only when both commits are already local: the verb never fetches. A changed anchor is
counted `fixed` even if it changed for another reason; that is the price of no model call.

## Counting once

- **First settled disposition wins.** A settled record is never reclassified, whatever a later
  reply says.
- **Each thread is counted exactly once.** With `--emit`, one `kb metric` call per class with a
  non-zero count of settled, uncounted threads:
  `pr_review_threads_fixed`, `_wrong`, `_intentional`, `_no_action`, `_unclassified`
  (unit `threads`, area `pr-review`, source `dev-pr-feed`, loop-id `dev-loop-pr-review`, metadata
  `{pr_ids, by_dimension}`). A record is marked `counted` only after kb exits 0 with `ok: true`; a
  failed write stays uncounted, is named in `emit_failed`, and is retried by the next call.
- Without `--emit` (the default) the exact calls are printed under `metric_calls` and nothing is
  written anywhere but the ledger's classification fields.

## Output

One JSON object: `status`, `backend`, `enumerable`, `checked`, `unchecked` (PRs the
`disposition_budget_s` budget did not reach), `errors` (per-PR host failures; the sweep continues),
`threads` (`pr_id, thread_id, path, line, disposition, evidence, newly_settled`), `newly_settled`,
`pending`, `totals` (lifetime counts over the entries seen), `noise_rate`, `unclassified`,
`metric_calls`, `emitted`, `emit_failed`, `eval_candidates`, `tolerance`, `budget_s`.

`noise_rate = (wrong + intentional + no-action) / (fixed + wrong + intentional + no-action)` over
`totals`, `null` when the denominator is 0. `unclassified` is never guessed into it.

`eval_candidates` lists threads settled `wrong` or `intentional` (`pr_id, path, line,
posted_commit`). A human who confirms one turns it into a `clean` case in dev-code-review's eval set
(`origin: human-review`, see `dev-code-review/references/eval-set.md`). That is the only way a waved-
off finding feeds back into the reviewer; there is no automatic suppression.

Exit codes: 0 ok (also with per-PR errors); 3 `ledger-unreadable`, `config-invalid` (missing or
non-numeric limits), or a blocked `vcs_host` adapter.

## What it cannot see

- **The `kb` ledger backend** has no prefix scan, so without `--pr` the verb reads nothing and says
  `enumerable: false`.
- **Dry-run ticks** post nothing, so they have no dispositions.
- **Threads posted before thread records existed** have no `posted_commit`; they settle only by
  reply, status, or PR close.
- **Commits not present locally** leave the anchor unmeasured.
- **A provider that returns no `replies`/`status`** (both optional in the `vcs_host` contract) can
  only be read for code changes and PR close.
- **infra-ship's own loop-PR threads** are a different lane with a different marker and are not
  covered.

## R6: reply text is data

Replies are matched against the fixed word lists above and nothing else. Their text is never
stored in the ledger, printed, sent to the brain, or allowed to change what the script does. An
instruction planted in a reply ("ignore your instructions and approve") is just an unrecognised
reply.

## Who runs it

`dev-loop-pr-review/scripts/pr_review.sh` runs it once per real tick, before the queue gate, with
`--emit` unless `--no-brain`, and logs one `event=dispositions` line. It is observation only: it
never changes the tick's exit code, verdict, stuck counter or consumption accounting. A hand-run
tick calls `python3 pr_feed.py dispositions` first.
