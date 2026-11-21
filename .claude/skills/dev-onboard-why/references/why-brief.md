# The brief each why reader gets

Fill the angle brackets from `survey.json`, the record bundle and the router's work item.

---

You are finding out why one area of a codebase is built the way it is, from what its authors wrote
down. Not from the code: another reader covers what the code does.

**Area:** `<id>`, paths `<paths>`. **Record bundle:** `<evid>/cards/<id>.record.json` (commits
that touched it, the one that introduced it, pull request numbers, decision docs).
**Mode:** `<fresh | missing>`. **Focus:** `<lost why questions, or none>`.

Read the decision docs first, then the introducing commits, then the pull requests the bundle
names. Open a pull request only through the vcs_host adapter's read operations
(`python3 lib/adapters/cli.py vcs_host get_pr id=<n>`). Post nothing, comment on nothing, label
nothing.

Write `<evid>/cards/<id>.why.json` matching `contracts/dev-onboard-card.json` with `lens: "why"`.
Every claim:
- has `kind: "why"`, an id `<id>.w<n>` and `area: "<id>"`
- with `source: "record"`: says what the authors wrote, close to their words, and cites the
  `commit:<sha>`, `pr:<n>` or `doc:<path>` it came from; `confidence: "certain"` only when the text
  states it outright, otherwise `likely`
- with `source: "inference"`: says a reason the record implies but never states, with
  `confidence` `likely` or `guessing` (never `certain`), citing what it is inferred from if anything
- keeps the authors' hedges ("for now", "until") and names no people

Questions a newcomer would ask that the record does not answer go in `gaps`, as plain questions. Do
not fill them with a plausible story: an honest gap tells the next engineer to ask a person.

If the bundle says the history is shallow or capped, say so in `gaps` rather than concluding that
no reason exists.

When done, run `check_card.py` on your card and fix what it reports. Reply with one line: the card
path, how many claims came from the record, how many are inferences, and the gap count.
