## PROJ-9202 — FINALBODYMARKER the invoice matrix renders its own cadence columns

![before and after](media/IMAGELINEMARKER-before.png)

A new renderer over the shipped engine, not a second ledger. Every write goes through a
service that already exists, so the legacy grid, the dashboard body and the partner copy
all keep the behaviour they had before this branch existed.

### The root cause

Writing one display toggle sent a full settings request, which re-asserted every other field
from the client's last read — and one of those fields is synced onto the ledger's interval
offset. Choosing "hide total" therefore moved the team's week boundary from Sunday–Saturday to
Monday–Sunday and shifted every weekly column by a day. The command's fields are nullable by
design; the toggle now sends only itself, and the boundary stops moving.

### The fix shape

One guard at the deepest shared point: the settings mutator builds a sparse request instead of
a dense one. Twelve call sites reach that mutator and none of them had to change, which is the
tell that the patch is at the root rather than at a symptom. A narrow special-case in the
weekly column builder would have greened the replay and left the same corruption reachable from
the three other toggles that share the request shape.

### What was considered and rejected

Reshaping the legacy ledger component was the obvious move and the wrong one: it is rendered
by the dashboard body and by the partner application, neither of which is behind this page's
feature flag, so the blast radius of a shared edit is two products wide for a one-page feature.
Patching the column builder instead of the request shape would have fixed the weekly tab and
left the monthly and quarterly tabs quietly wrong, because they read the same offset. Freezing
the offset on the server was rejected for the opposite reason — it would have made a legitimate
week-start change impossible without a migration, trading one defect for a missing capability.

### Evidence

The replay drives the toggle, reads the rendered column headers out of the DOM rather than
trusting the success toast, and compares them against the dates the API returned. It is red on
the parent commit and green on this one, and the regression slice for the area is green with no
new quarantines. The before recording shows the columns shifting on a toggle that claims to
change nothing but a column's visibility; the after recording shows them holding still.

### Carried to a human, not decided here

Three acceptance criteria need a database migration, which this loop will not write: the
per-measurable cumulative-total flag, the per-row aggregate opt-out, and the yes/no measurable
format. Each is filed as a follow-up on the pull request rather than half-built here, because a
migration is on the blast-radius denylist and a forced human gate is the point of that rule.

### Notes for the reviewer

The diff reads larger than it is: most of it is the renderer's template, and the behavioural
change is confined to the request builder and its test. Read the request builder first, then
the replay, and the rest is presentation. TAILCUTMARKER is the last line of this body and is
here so a truncation test has something to prove.

🤖 Generated with [Claude Code](https://claude.com/claude-code) GENERATEDWITHMARKER

https://claude.ai/code/session_SESSIONLINKMARKER

Co-Authored-By: COAUTHORMARKER <noreply@example.invalid>
Claude-Session: https://claude.ai/code/session_TRAILERSESSIONMARKER

Inline evidence blob: data:image/png;base64,QkFTRTY0Qk9EWU1BUktFUg== BASE64BODYMARKER
