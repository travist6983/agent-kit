# Question kinds for the codebase quiz

A good question is one a new engineer on this codebase would need answered before changing it.
Five kinds cover what "understanding a codebase" means in practice. They map onto how, why and
blast radius: where things are and how they run, why they are that way, and what breaks if you
touch them.

| Kind | Asks | Example | A good key cites |
|---|---|---|---|
| `locate` | where something lives | Where would a malformed email be rejected? | the function that rejects it |
| `trace` | what happens, step by step, when X | What happens when you pay an invoice that is already paid? | the branch that decides it |
| `predict` | what would change or break if X | If you edit the dict store.get returns, does the total change? | the line that makes the answer true |
| `why` | the reason it is built this way | Why is there no database? | a decision doc, commit or PR, never the code alone |
| `owner` | which module owns a state or a decision, where new code goes | Which code may change an invoice's status? | the owner, not a caller |

## How many

Per round: two questions per area at minimum, so the area floor can bite, and about one question
per 300 lines of code in the area beyond that, capped at eight per area. At least one `trace` or
`predict` per area, because locate questions alone reward a file index, not understanding. Ask a
`why` question only where the record has an answer (a decision doc, a commit message, a PR); a why
question whose key cites only code is a bad key.

## What makes a key good

- It cites the narrowest lines that prove it and names a symbol that sits in those lines. The grader
  checks both, so a drifted line number is caught instead of silently trusted.
- It is answerable. A question whose answer is "it depends" or needs a running production system is
  out of scope for this exam.
- It does not leak the map. The writer never sees the map, so it cannot write questions shaped to it.

## Carryover

Questions lost in the last round come back unchanged in the next one. A relearn that only fixes the
question it was told about, and nothing near it, will pass the carryover and fail the new ones. That
is the point of writing new questions every round.
