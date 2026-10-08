# Loop state — PROJ-301 + PROJ-302 (dev-loop-hard-defect), end of loop, ready to ship

The calibration scenario: two simple bugs in one sharing dialog. Everything upstream is done and green;
what remains is dev-fix's final `fix.json` (Step 8) and infra-ship's PR body (Step 4) + threads (Step 6).

## Tickets
- **PROJ-301** — Project "Share" dialog: checking **Edit** for a member doesn't enable *Save
  permissions*, and the value doesn't persist after reopening. Release target: **Release 2.4.0**.
- **PROJ-302** — Workspace "Share" dialog: checking **View** for *Entire workspace* doesn't enable
  *Save*, which blocks the PROJ-298 workaround. Release target: **Release 2.4.0**.
- Branch: `fix/PROJ-301-share-permissions-checkbox-binding`. Routed to hard-defect because the fix
  touches the shared design-system package (`packages/ui/src/Checkbox.js`).

## Root cause (diagnosed, verified)
1. The design-system `Checkbox` takes `initialValue` + an `onChange` callback and **snapshots the prop
   once at mount**, so a value the parent pushes after mount never reaches the rendered box. Callers had
   worked around it by keying the component on its own value to force a remount — three times in
   `SharePermissionsDialog.js` and once in `ExportProjectDialog.js` (with a code comment describing the
   bug). The **View** column had the remount-key workaround; the **Edit** column did not. Every project
   call site opens the dialog with `permissions: null`, so it mounts all-false and re-seeds when the fetch
   lands: View shows the truth, Edit keeps drawing the mount-time `false`; clicking it writes a value the
   local state already holds, so `dirty` never flips (PROJ-301).
2. The edit gate read a `null` permissions blob as "you may not edit", conflating "fetch in flight" and
   "no grants recorded" with "no rights". A workspace nobody can see is exactly the one whose blob is
   empty, so the only dialog that can grant access opened permanently read-only (PROJ-302).

## What the fix changed (5 files)
- `packages/ui/src/Checkbox.js:41` — watch `initialValue` and sync; deliberately **no** `onChange` call
  on a programmatic push (`onChange` means "the user toggled this"; echoing a push would loop).
- `web/src/areas/projects/SharePermissionsDialog.js` — removed the three remount-key workarounds (they
  also discarded focus); after loading settles, a `null` blob resolves through an **empty permission
  set** (member → false, observer → false, admin/owner → true — matching the permission service's own
  short-circuit; the server stays the authority); new explicit `loading` state (dialog says "Loading
  permissions…" instead of a live-looking form that swallows clicks); new `loadFailed` state — a **failed
  load fails closed** (checkboxes disabled, Save not rendered, "Could not load permissions."); the
  Workspace-admins row's `disabled` gained the `!canEditPermissions` guard its sibling rows already had
  (a read-only viewer could toggle it; Save was hidden so nothing persisted).
- `web/src/services/projects.js:118` — new `projects.getRevision()`; `getRevisionPermissions()` now
  expressed on top of it (the dialog must tell "request failed" from "revision has no grants"; mirrors
  the workspace service).
- `web/src/services/workspaces.js` — `workspaces.getRevision()` now honours its doc comment (returns
  null on failure).
- `web/src/areas/projects/ExportProjectDialog.js:57` — retired its remount-key workaround and the
  now-false comment claiming Checkbox seeds once at mount.

Blast radius measured: only 3 files render `<Checkbox>`, only 2 pass `initialValue`.

## Plan-synthesis (ran — hard-defect mode)
- **Chosen:** fix `Checkbox` itself (watch the prop) + resolve the null blob properly.
- Runner-up: give the Edit checkboxes the same value-bearing remount key as View — rejected,
  symptom-level (R3): the component still can't accept a pushed value, the other callers keep the
  workaround, focus is still discarded on every programmatic change.
- Also considered: switch `Checkbox` to a controlled two-way-binding API — rejected, a breaking API
  change to a globally used design-system component for no benefit these tickets need.
- Also considered: keep the empty-set fallback unconditional and rely on the server to reject a bad
  save — rejected: the server would NOT reject it (an admin/owner is authorised to set permissions), so
  a full-replacement Save built from an unloaded blob would wipe every grant. Prevented on the client.

## Review gate (dev-code-review) — 2 cycles
- Cycle 1 → `request-changes`: HIGH — treating a failed load as "no grants" gives an owner a live blank
  dialog whose Save wipes every grant. **Fixed** in re-fix cycle 1 (the `loadFailed` fail-closed path).
  MEDIUM — `ExportProjectDialog`'s comment is now false. **Fixed** (retired with its workaround).
- Cycle 2 → `approve`. While validating, the reviewer noticed two **pre-existing** issues outside the diff
  (rejected as findings because they are pre-existing):
  - `api/src/projects/set_project_permissions.py` (`PUT projects/{id}/permissions`) never calls
    `permission_service.has_permission`, unlike the two sibling handlers that gate on `can_edit`. Any
    authenticated workspace member could rewrite project-level sharing, including granting themselves
    workspace-wide edit. Intra-workspace only (reads are partitioned on workspace id). Not reachable
    from this dialog.
  - `WorkspaceSettingsPage` passes `officialPermissions.permissions` captured at page-load time and Save
    is a full replacement — grants added by someone else since the page loaded would be silently
    overwritten.

## Verification
- `qa-verify`: pass (confidence 88). Live browser oracle in an isolated sandbox, both tickets + the
  fail-closed path with a real 404 (no stubbing). Before/after recordings exist.
  | Case | Before | After |
  |---|---|---|
  | Stored `workspace.canEdit:true`, dialog opened | Edit unchecked, Save disabled | Edit checked |
  | Uncheck / re-check Edit | — | Save enables / back to disabled |
  | No grants, check Edit | Save stays disabled | enables, persists, reopens checked |
  | Workspace with no blob | all disabled, no Save | grantable; View enables Save |
  | Load fails (real 404) | (would have been) live blank form | disabled, no Save, error shown |
- Unit suite **822 / 59 files** green (was 817/58); 5 new tests, one falsifiable (reverting the watch
  turns exactly one red). Lint + formatter clean.
- `qa-regression`: green on build + lint + the unit suite above. Nothing automated drives the share
  dialog end to end, so `qa-verify`'s live browser oracle above is the only UI-level signal for this
  change.
- `dev-pre-pr`: `ready` — conventions scan 0 blocking across 227 added lines.
