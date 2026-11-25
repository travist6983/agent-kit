# dev-code-review eval cases (answer keys)

Each `<id>/` holds `case.json` (the answer key, `contracts/dev-code-review.eval-case.json`), `change.diff`
(a real `git diff`), `base/` (the files the diff modifies, before it) and an optional `fix.json`.
**This directory holds answer keys: the reviewer must never be pointed at it.** Run the reviewer only on a
tree built by `scripts/reviewer_eval.py materialize`, which never copies `case.json`.
How to run, score and grow the set: [`../../references/eval-set.md`](../../references/eval-set.md).
