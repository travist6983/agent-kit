# architect.json: the map agent's one file

`assemble_map.py` reads this file and the cards, and nothing the agent says in prose. Every key is
optional. A worked example for the fixture repo is `contracts/samples/EXAMPLE-onboard/architect.json`.

```json
{
  "summary": "Two or three sentences: the general name for the thing, then what this codebase uses it for.",
  "areas": {
    "<survey area id>": { "id": "<new id, only to rename>", "name": "Human name", "purpose": "One sentence: the job it does." }
  },
  "merge": { "<new id>": ["<survey id>", "<survey id>"] },
  "claims": [
    {
      "id": "<area>.a1",
      "area": "<area after renames and merges>",
      "kind": "owner",
      "text": "InvoiceService is the only thing that changes an invoice's status.",
      "cites": [{ "path": "app/invoices/service.py", "line_start": 17, "line_end": 23, "symbol": "def pay" }],
      "evidence": "walked",
      "source": "code"
    }
  ],
  "open_questions": ["Cards disagree on X: invoices.h2 says A, entry.h1 says B; the cited lines do not settle it."]
}
```

## What earns a lens claim

Only facts that span areas, or that no single reader could see from inside one area:

| kind | the question it answers | a good one |
|---|---|---|
| `owner` | which module is the one that changes this state? | names one module and cites the write |
| `rule` | where does a new X go? | names the place and cites an existing X that lives there |

A lens claim is held to the same standard as a card claim: narrowest cite, a symbol in the cited
lines, evidence `pointed` or better. If the cards do not give you enough to cite it, it is an open
question, not a claim.

## Renames and merges

- Rename when the survey's path id would mislead a reader (`app` holding only the entry point).
- Merge when two directories are one concept (a model and its repository split by layer, in a
  codebase where the team clearly treats them as one feature).
- Do not split here. An area too large for one reader is split by the survey's cap or by the how
  stage working file by file.

Claim ids follow their area: `app.h2` becomes `entry.h2` after a rename, and `members.h1` becomes
`core.h1` after a merge (or `core.members-h1` if `core.h1` is taken). Keep renames stable between
rounds, or carried-over quiz questions will name ids the map no longer has.
