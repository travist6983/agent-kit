# failure-taxonomy.md — the 9-cause taxonomy + failure→cause→fix tables

This is the transferable IP of the diagnose lane: the **root-cause taxonomy** the
Screenshot+Error analyzer reasons over, and the **failure→cause→fix lookup tables** that
turn a symptom into a concrete change. They are distilled from hands-on UI-test debugging and
are **stack-agnostic**: every app-specific fact (component names, copy strings, routes, role
names) is lifted out into the target repo's conventions ruleset (`conventions.app_handles`)
or shown with the neutral example domain (Workspaces, Projects, Tasks, Comments, Invoices,
Members, Settings; roles owner/admin/member/observer). Cross-reference `SKILL.md`
**§4 (diagnose with the agent swarm)** and **§5 (propose the fix as a diff)**.

> **The core adaptation — apply → propose.** In an interactive debugging session each "Fix"
> column is a mutation an editor performs and then re-runs. **In qa-diagnose nothing is ever
> applied.** Every "Fix" below is read as **the change to render into the proposed unified
> diff** (`diagnose.json.diff`) — the target file, the hunk, the exact edit — never an
> `Edit`. The skill has **no `Edit` tool**; propose-only is structurally enforced. The fix
> tables keep the causes, the fixes, and the test-id convention (`app_handles.test_id_pattern`)
> — the terminal verb changes from *do it* to *propose it*.

> **Scope — case code and product code, never a spec-suite file.** A fix lands in the
> generated-code tier's materialized case file (`context.target`, conventionally `case_code.py`)
> for a `test_bug`, or in the product source for an `app_bug`; an auth, data, or shared-state
> finding with no code change carries `diff: ""`. No fix below is ever rendered into a spec
> file, page object, or spec-suite helper. The locator calls are shown in the browser test
> tool's JS spelling (`browser.oracle_driver` in stack.config, default `playwright-cli`); the
> Python binding uses the same API (`get_by_role`, `wait_for(state="visible")`,
> `scroll_into_view_if_needed()`), so match the spelling of the code you actually read.

> **Perishable caveats (verify against the current build).** Line numbers, specific test-id
> values, component names, routes, and behaviours pinned to a particular change all drift.
> Before rendering any fix into a diff, re-anchor it against the source you actually read this
> run — never emit a hunk whose line numbers or selectors you did not confirm against HEAD. When
> a caveat can't be confirmed, lower `confidence` and say so in `notes` (or emit `diff: ""`
> and hand it to a human) rather than proposing a stale hunk.

---

## App handles (from `conventions.app_handles`)

The rows below name these handles. Each is **optional**; when a handle is absent the generic
rule applies and nothing is assumed about the app. The target repo fills the map in its
conventions file (`stack.conventions_file`, default `.claude/loops/conventions.json`).

| handle | what it holds | used by |
|---|---|---|
| `test_id_attribute` | the attribute the app uses for test ids (default `data-testid`) | every "add a test id" fix |
| `test_id_pattern` | the naming convention for test ids (default `{area}-{component}-{element}`) | every proposed test id |
| `component_glob` | where UI component source lives (e.g. `src/components/**`) | Agent B's source read |
| `i18n_catalog` | path of the source-language string catalog when UI copy is translated | "button text comes from the catalog" |
| `non_forwarding_wrappers` | wrapper components that do NOT forward arbitrary attributes to their root element | cause 1 |
| `two_step_pickers` | pickers whose search input appears only after an opener is clicked: `[{opener_text, search_placeholder}]` | cause 2 |
| `overlay_routes` | routes rendered as overlays/modals that do not survive `reload()` | cause 9 |
| `blocking_interstitials` | copy of modals that can block a page (role changed, session expired, announcement) | cause 7 |
| `seed_entities` | entity kinds whose names must never be hard-coded in a case | "hardcoded entity" rows |
| `realtime_timeout_ms` | how long a realtime push can take to reach the page (default `15000`) | timing rows |

---

## The 9-cause root-cause taxonomy (Screenshot + Error analyzer)

Given the error output and the failure screenshot, determine the root cause. Common causes:

1. **Element not found** — the selector is wrong or the element doesn't exist on the page
   - Check if the element is behind a permission gate (a conditional render keyed on role or
     editability — the UI framework's own syntax for "render only if")
   - Check if the test id was added to a wrapper component that does not forward attributes to
     its root element (`app_handles.non_forwarding_wrappers`), so it never reached the DOM
   - Check if the element needs scrolling into view

2. **Element not visible/hidden** — the element exists in DOM but isn't displayed
   - A two-step picker: the search input is revealed only after an opener is clicked
     (`app_handles.two_step_pickers`)
   - Element is below the fold (needs `scrollIntoViewIfNeeded()`)
   - Element is inside a collapsed section

3. **Strict mode violation (multiple matches)** — the selector matches 2+ elements
   - Component is reused (nav bar + widget, detail pane + side panel)
   - `hasText` filter matches substrings across unrelated elements
   - Need to scope locator to parent container

4. **Element disabled** — form validation blocking submit
   - Required field empty (check all form fields in screenshot)
   - Async validation error (a uniqueness or "already exists" message)
   - Dropdown with its placeholder still showing, not filled

5. **Wrong page** — navigation went to unexpected location
   - Route doesn't exist for this user's permissions
   - Navigation used hardcoded ids
   - Auth state expired

6. **Timeout** — element eventually appears but too slowly
   - Increase timeout
   - Realtime-push propagation delay (`app_handles.realtime_timeout_ms`)

7. **Shared state conflict** — another test run modified the same user/entity
   - A blocking interstitial (`app_handles.blocking_interstitials`, e.g. "Your permission level
     has changed") is covering the page
   - A one-shot action (approve/deny, accept-invite) was already consumed by a parallel run
   - The user's role differs from the case's precondition because a parallel run changed it
   - **Fix**: give the case its own user/entity, or dismiss the blocking interstitial before
     critical clicks; when the clash is shared tenant/workspace state no case-code change can
     own, it is a `data` finding with `diff: ""`

8. **Non-waiting visibility check** (tool-specific) — `locator.isVisible()` returns immediately
   without waiting
   - Code uses `isVisible({ timeout: 5000 })` but the timeout is silently ignored
   - **Fix**: replace with `waitFor({ state: 'visible', timeout: 5000 }).then(() => true).catch(() => false)`

9. **Overlay route lost on reload** — `page.reload()` navigates away from an overlay/modal route
   - Routes listed in `app_handles.overlay_routes` render as overlays on top of a parent page
   - Reloading lands on the parent (or home) page, not back in the overlay
   - **Fix**: re-navigate to the overlay route instead of `reload()`

**Mapping cause → `failure_layer`.** The taxonomy diagnoses the *mechanism*; the contract's
`failure_layer` records the *fault owner*. A cause can land on different layers depending on
what the source read shows — decide from the evidence, not the cause number:
- The element/selector is wrong **in the case code** (causes 1–3, 8, 9; a stale test id,
  a substring `hasText`, a non-waiting visibility check, a `reload()` on an overlay route) →
  **`test_bug`**.
- The element/state is wrong **in the product** (the app never rendered it, the store never
  updated, a real validation bug in causes 1/4) → **`app_bug`**.
- Auth expired, a route needs perms the env didn't grant, an infra/timeout/network fault
  (parts of causes 5, 6) → **`environment`**.
- A precondition entity is missing / a parallel test mutated shared state / a hardcoded name
  isn't in this workspace (causes 4, 7; hardcoded ids in 5) → **`data`**.

---

## Common fixes — failure → cause → fix lookup tables

Quick lookup for the most frequent failure patterns. Read each **Fix** as *the change to
render into the proposed diff*, never as an edit to apply. `data-testid` below stands for
`app_handles.test_id_attribute`.

### Element Not Found

| Symptom | Cause | Fix |
|---------|-------|-----|
| `getByTestId('x')` — not found | test id not added to the UI component | Add `data-testid="x"` to the element in the component file (name it per `test_id_pattern`) |
| `getByTestId('x')` — not found | test id set on a wrapper that does not forward attributes (`non_forwarding_wrappers`) | Use `getByRole('button', { name: '...' })` scoped to the parent instead, or add the test id to the wrapper's root element (`app_bug` when the wrapper should forward) |
| Timeout waiting for a Project/Task/Invoice | Hardcoded entity name doesn't exist in the test workspace | Select the first available item instead of a hardcoded name. Never hardcode the names of `seed_entities` |
| `getByRole('button', { name: 'X' })` — not found | Button text comes from the i18n catalog, not the literal in the case | Check `app_handles.i18n_catalog` for the actual rendered text |
| `getByRole('menuitem')` — not found | The UI kit renders menu items as anchors/divs without ARIA roles | Use a test id on the menu item, or `locator('<menu item selector>').filter({ hasText: '...' })` with the selector read from the component source |
| `getByPlaceholder('X')` — hidden | Two-step picker: search input is behind an opener (`two_step_pickers`) | Click the opener first, then fill the search input |
| Button not rendered at all | A permission gate or conditional render hides it | Navigate directly to the route (e.g. `page.goto('/projects/new')`) or satisfy the precondition that reveals it |

### Strict Mode Violation (Multiple Matches)

| Symptom | Cause | Fix |
|---------|-------|-----|
| Resolved to 2 elements | Component reused in nav bar + widget | Scope to parent: `widget.getByRole('button', { name: '...' })` |
| `hasText` matches 2 elements | Substring match hits unrelated elements | Use a test id for identity, not `hasText` |
| Two test-id matches | Same component rendered twice (e.g. a "New Task" menu in the toolbar and in the empty state) | Scope to the specific parent container |

### Element Disabled

| Symptom | Cause | Fix |
|---------|-------|-----|
| Submit button disabled | Required form field empty | Check screenshot — fill every required field (e.g. title, project picker, assignee) |
| Submit button disabled | Dropdown still shows its placeholder | Click dropdown and select a value |
| Submit button disabled | Async uniqueness validation error ("already exists", "please rename") | Use a unique name (`crypto.randomUUID()` suffix), wait for `toBeEnabled()` |
| Submit button disabled | Dependent fields auto-populated empty | Ensure the controlling field (e.g. the template/type picker) is selected first so dependents fill |

### Wrong Page / Navigation

| Symptom | Cause | Fix |
|---------|-------|-----|
| Landed on login page | Auth state expired | `environment`, not code — the control plane re-mints the test login through the `auth_provider` adapter; `diff: ""` |
| Landed on wrong page after click | Clicked wrong element (e.g. a dropdown item vs a tile) | Check screenshot, verify click target |
| `waitForURL` timeout | Navigation didn't happen | Check if the clicked element actually navigates or just opens a dropdown |
| Route not found / blank page | Direct route requires specific permissions | Navigate through the UI instead, or run as the role the route needs |

### Timing

| Symptom | Cause | Fix |
|---------|-------|-----|
| Element found but not visible | Below the fold | Add `element.scrollIntoViewIfNeeded()` before the visibility check |
| Realtime update not received | Assertion timeout shorter than push propagation | Increase to `{ timeout: <realtime_timeout_ms> }` (default `15_000`) |
| Form validation not settled | Async validation still running | Add `await expect(submitBtn).toBeEnabled({ timeout: 15_000 })` before click |
| `isVisible({ timeout: 5000 })` returns false immediately | `isVisible()` does NOT auto-wait — timeout is ignored | Use `waitFor({ state: 'visible', timeout })` then `.then(() => true).catch(() => false)` |

### Parallel Execution / Shared State

| Symptom | Cause | Fix |
|---------|-------|-----|
| A blocking interstitial covers the click target | Another run's teardown changed the user's role (or session) while this case was running | Dismiss the interstitial before critical clicks; if the role change itself is the clash, it is a `data` finding (give the case its own user) |
| One-shot action buttons missing (approve/deny, accept invite) | A parallel case already consumed the action | Don't share the user/entity with a concurrently running case — `data` |
| User's role is not the precondition role | Teardown from a previous case failed to restore it | Restore with the role that has the permission (an `owner`, not an `admin`, for ownership changes); re-navigate to the Settings route instead of `reload()` when it is an overlay route |
| Navigating to Settings can't find the "Settings" entry | `filter({ hasText: /^settings$/i })` is too strict | Use substring match `filter({ hasText: 'Settings' })` instead of the exact regex |
| Teardown error fails the case | The case's reset step throws during teardown | Catch and log the teardown error instead of letting it fail the case |
| Row text shows one role but the user actually has another | `row.textContent()` includes dropdown option text from all cells | Read the role cell specifically, not the full row |
| `page.reload()` loses the overlay page | The route is in `overlay_routes` — reload navigates to the parent | Re-navigate to the overlay route instead of `reload()` |

### Two-Step Picker Pattern

Shown with neutral copy; the real opener text and placeholder come from `app_handles.two_step_pickers`.

```js
// 1. Click the opener to reveal the search input
await container.getByText('Add member').click();

// 2. Fill search
await container.getByPlaceholder('Search members').fill(email);

// 3. Click the result filtered by a unique value, scoped to the picker's list
await container.getByRole('option').filter({ hasText: email }).first().click();
```

### Create-Entity Pattern

```js
// Navigate directly (the "New Project" button may be permission-gated)
await page.goto('/projects/new');

// Fill the unique field FIRST (lets async uniqueness validation settle)
await nameInput.fill(uniqueName);

// Add members, fill other fields...

// Handle a uniqueness retry
const renameMsg = page.getByText('A project with this name already exists.');
if (await renameMsg.isVisible({ timeout: 2000 }).catch(() => false)) {
    await nameInput.fill(`E2E Project ${crypto.randomUUID().slice(0, 8)}`);
}

// Wait for enabled before submit
await expect(submitBtn).toBeEnabled({ timeout: 15_000 });
```
