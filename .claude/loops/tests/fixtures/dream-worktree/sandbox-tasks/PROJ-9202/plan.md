# PROJ-9202 — plan PLANMARKER

## The strategy that was picked
Render the matrix over the shipped ledger engine and change only the settings request shape.

## The strategy that was rejected
Reshape the legacy ledger component. It is shared with the dashboard body and the partner
application, so a one-page feature would carry a two-product blast radius.

## Acceptance
- the weekly tab's column headers match the dates the API returned, before and after a toggle
- the regression slice for the area is green with no new quarantines
