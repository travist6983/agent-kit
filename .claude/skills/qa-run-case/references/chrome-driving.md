# chrome-driving — the DRIVER=chrome discipline (claude-in-chrome MCP)

> **The diagnostic driver.** `DRIVER=chrome` drives a real Chrome tab through the claude-in-chrome MCP
> (`browser.live_driver` in `.claude/loops/stack.config.json`) so a human can watch the run happen. It is
> the complement to `DRIVER=cli` (`references/playwright-cli-driving.md`): cli is the default, headless,
> ~10× cheaper, self-authenticating replay path; chrome is the eyes-on, exploratory path that leans on an
> already-open browser. Same case↔run↔verdict spine, same `case-run.json` contract, same grading rubric
> (`references/step-classification.md`) — only the driving primitives differ. Read this whole file before
> the first `navigate`.

**The cardinal rule (why this file, and the whole skill, exists).** A success toast, a green console, a 200 in
the network tab, an assistant echoing "done" — **none of those are evidence a user-facing thing is right.**
Evidence is *you* re-reading the real state through the app's own DOM/API after the action. Every snippet
here serves that one discipline. A false PASS is worse than an honest BLOCKED.

**Headless, so it never asks.** This skill runs under `claude -p`. There is no human in the loop. Every place
an interactive runner would pause to `AskUserQuestion` — instance ambiguity, wrong tenant, an unsatisfiable
precondition, a driver that died — becomes **`grade BLOCKED with the exact reason` and stop**, never a hang.
`switch_browser` and `AskUserQuestion` both block on a human; **neither is ever called in this driver.**

Examples use the neutral sample domain (Workspaces, Projects, Tasks, Comments, Invoices, Members,
Settings). The target app's own routes, widgets and quirks come from the adopter's knowledge pack
(`references/app-knowledge.md`), never from this file.

---

## 0. Bootstrap — load tools, pin the instance, establish the tab, confirm the session

### 0a. Load every chrome tool in ONE ToolSearch
The `mcp__claude-in-chrome__*` tools are **deferred** — calling one before its schema is loaded fails with
`InputValidationError`. So the **first action of the run** loads every tool you'll need in a **single**
ToolSearch (each extra call is a wasted round-trip):

```
ToolSearch select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__find,mcp__claude-in-chrome__browser_batch,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__get_page_text,mcp__claude-in-chrome__read_console_messages,mcp__claude-in-chrome__read_network_requests,mcp__claude-in-chrome__javascript_tool,mcp__claude-in-chrome__list_connected_browsers,mcp__claude-in-chrome__select_browser
```

That one call loads the ten core driving tools (`tabs_context_mcp`, `tabs_create_mcp`, `navigate`, `find`,
`browser_batch`, `computer`, `get_page_text`, `read_console_messages`, `read_network_requests`,
`javascript_tool`) **plus** the two instance-selection tools (`list_connected_browsers`, `select_browser`)
used in §0b. Add `gif_creator` to the same call if you plan a run recording (§3). Only fire a second
ToolSearch if a later step genuinely needs a tool you didn't anticipate.

### 0b. Deterministic instance selection — pin or fail, never ask
More than one Chrome may have the extension connected at once (e.g. two profiles on the same box).
`list_connected_browsers`' own contract tells an *interactive* agent to `AskUserQuestion`, and
`switch_browser` broadcasts a pairing prompt and **waits up to two minutes for a human to click Connect**.
**Both deadlock a headless `claude -p` run** — a run that sits on that prompt exits without writing an
artifact. In an autonomous run you select **deterministically by deviceId, or you BLOCKED-fail fast — you
never ask and never broadcast.**

```
1. list = list_connected_browsers()                         # enumerate connected extension instances
2. want = env CHROME_DEVICE_ID  OR  the CHROME_DEVICE_ID= prompt token   # runner-passed hint (per §Inputs)
3. if want is set:
     want ∈ list.deviceId  → select_browser(want)           # exact match, NO broadcast, NO prompt
     else                  → BLOCKED "CHROME_DEVICE_ID=<want> not connected; connected: [<name (id)>, …]"
4. elif len(list) == 0     → BLOCKED "no Chrome extension connected"
5. elif len(list) == 1     → select_browser(list[0].deviceId)   # the sole connected instance — happy path
6. else (≥2, no hint)      → BLOCKED "N Chrome instances connected and no CHROME_DEVICE_ID set:
                              [<name (id)>, …]. Connect exactly one, or pass CHROME_DEVICE_ID."
```

- `select_browser(deviceId)` is the **only** sanctioned selection primitive here — it picks a specific
  instance **without** broadcasting a pairing request, so it never prompts. That is why it is safe and
  `switch_browser` is not.
- **NEVER `AskUserQuestion` or `switch_browser`.** The "you MUST ask the user" language in the
  `list_connected_browsers` description assumes an interactive human; in a headless run, following it is the
  bug. Every ambiguity above resolves to a written **BLOCKED** with the connected set named, not a wait.
- **A single connected instance is the recommended runner posture** — then step 5 always fires and
  `CHROME_DEVICE_ID` is never needed. The hint (step 3) is the escape hatch for hosts that keep several
  profiles connected. `CHROME_DEVICE_ID` arrives on two independent channels — a host-level env var the
  runner forwards, or a per-run `CHROME_DEVICE_ID=<id>` prompt token; read whichever is present, prefer the
  explicit prompt token when both are.

### 0c. Establish the tab, capture its id
Only after instance selection:

```
mcp__claude-in-chrome__tabs_context_mcp { createIfEmpty: true }
```

**Capture the returned tab id** and thread it through every subsequent `read_network_requests` /
`read_console_messages` call. A defect-style run drives a fresh tab (`tabs_context_mcp` establishes context;
`tabs_create_mcp` opens a new one), then navigates. **Never reuse a tab id from a prior session.**

### 0d. The perimeter caveat — chrome rides the paired Chrome's EXISTING session
The chrome driver does **not** inject auth. Unlike the cli driver — which loads `storage-state.json` and,
when `adapters.auth_provider.config.pre_auth = "header_token"`, injects the pre-auth header on every request
to `E2E_PERIMETER_ORIGIN` (that plumbing is `references/auth-bundle.md`, and it is **cli's job, not this
driver's**) — chrome **attaches to whatever session the paired Chrome already holds**, including any
perimeter cookie. There is no login step and no header injection here. Consequences:

- **Confirm where you landed before trusting anything.** After navigate + ready-check, confirm the page is
  signed in: the `adapters.auth_provider.config.login.signed_in_landmark` selector resolves and the URL is
  not under `login.path`. If the knowledge pack names a tenant/workspace indicator (a title pattern, a
  workspace switcher label), read it too so you know *which* tenant you are in.
- **If the paired Chrome is NOT authenticated to the target env** — the navigate bounces to the login page
  or a perimeter challenge, or the landmark never appears — the session is missing or stale. **Grade
  BLOCKED** (`"auth bundle did not authenticate — chrome session bounced to <where>"`) and stop. This
  driver cannot mint a session; a missing one is a precondition failure, not something to fight.
- **Wrong tenant is also BLOCKED, never a switch.** Switching workspace/tenant changes the user's active
  context everywhere; a headless run must not do it silently and must not ask. If the confirmed tenant is
  not the case's expected one, BLOCKED with `"chrome session on wrong tenant: <observed>; expected
  <case's>"`.

Run the §0 prod guard (SKILL.md §0.1 — `APP_URL` host equals the `app.environments.prod.url` host without
`ALLOW_PROD=1`) and the `APP_URL` health-check the same as the cli path — a tripped guard is an immediate
BLOCKED before any browser action.

---

## 1. The `tabs_context → navigate → find/computer` sequence

1. `tabs_context_mcp { createIfEmpty: true }` (done in §0c) — capture the tab id.
2. **Navigate** to `APP_URL` (`mcp__claude-in-chrome__navigate`). Wait for the app to be **ready** — poll
   the DOM for the signed-in landmark plus a resolved location (or the ready condition the knowledge pack
   names) rather than a fixed sleep; a bare wait is flaky. Screenshot to `before.png` and confirm the
   authenticated session per §0d.
3. **Per step** (classify on demand per §2, then execute):
   - For a mutating case, navigate to the relevant baseline page first (per the knowledge pack's
     verification map) and screenshot — that is the "before" frame.
   - Drive with `find` → interact, batched into one `browser_batch` where the actions are related.
   - After a mutating step, read the truth channel independently (§4) and append the observed value to
     `transcript.md`.

### 1a. THE CENTRAL RULE — re-find every element by the accessibility tree; never reuse coordinates
Before you interact with **any** element, `find` it fresh in the current page's accessibility tree. **Never
carry a ref or a coordinate across a page context** — refs are bound to tree *position at find-time*, not to
element identity, and coordinates drift with layout, scroll state, and viewport scaling. An input that
renders at one position after arriving from page A renders somewhere else after arriving from page B;
hard-coded coordinates miss it. This single rule is the spine of reliable chrome driving.

- A ref goes stale the instant the tree reorders (a new row pushes list entries down; a dropdown
  repaints). Re-`find` immediately before the click that uses the ref — don't `find`-then-click across a
  repaint.
- For lists that reorder or float an active item to the top (a workspace switcher, a "recent" list), use
  `find "<Workspace name> option"` → `scroll_to ref` → click the **ref**, never a coordinate.

### 1b. Batch related actions into ONE `browser_batch`
Aggregate a coherent micro-sequence — **click → wait → screenshot** — into a single `browser_batch` call
rather than three round-trips. Keep the batch small enough that no ref inside it survives a repaint caused
by an earlier action in the same batch (that is the failure mode in §1c). A `find` that misses **aborts the
whole batch** (`actions[N] failed`), and `find` raises an *error* (not an empty list) on no-match — so put
any **negative-assertion** `find` (proving something is *not* visible) **last** in the batch, and treat its
"does not contain / does not appear" error text as positive evidence of absence.

### 1c. `computer` is the FALLBACK — only when a find-ref click silently no-ops
`find` + ref-click is the default. `computer` (screenshot + coordinate-click) is the **escape hatch for the
narrow cases where a ref click registers but does nothing** — no error, the click "succeeds," the page
doesn't change. When that happens, `computer`-screenshot to see the true layout, then coordinate-click.

The no-op *classes* below are **why to re-find by ref, and why a fallback exists** — not a coordinate table.
The specific controls and coordinates of a given app belong in the adopter's knowledge pack (§5
"Perishable"), carry "verify against current build", and drift with every release:

- **A modal's close control whose ref click no-ops.** `find` returns a ref for the close "X", the click
  registers without error, the modal stays. Fallback: screenshot, then coordinate-click the X; and don't
  assume Escape dismisses it — check the pack, or verify once and record it in `learnings.md`.
- **Chained menu items whose refs invalidate across the dropdown repaint.** If the menu-item click is
  queued in the **same** `browser_batch` as the dropdown-open click, the item ref invalidates across the
  open-repaint and misses. Fallback: open the dropdown in batch 1, screenshot, then click the item in
  batch 2 — re-`find` it first; coordinate-click only if the fresh ref also no-ops.
- **Cards where the obvious target doesn't navigate.** A card with no clickable wrapper in the a11y tree
  navigates from a child — the `heading` ref, or the thumbnail `image` — not the card itself. `find` the
  child that actually carries the link and click that.

The lesson every bullet teaches: **prefer the ref; reach for `computer` coordinates only when the ref click
provably no-ops, and re-screenshot before trusting any coordinate.**

---

## 2. Verb → chrome mapping

Classification itself (leading-keyword → verb) is driver-independent and lives in
`references/step-classification.md`; this is how each verb **executes in chrome**:

| verb | chrome execution |
|---|---|
| `navigate` | Translate the destination through the knowledge pack's verification map. If it's a real route, `navigate` there. If the artifact is a popover/widget with **no** URL (guessing `/<feature>` 404s), `navigate` to `/` first, then `find` + click the entry-point button. With no pack, start from `/` and find the entry point in the rendered UI — never invent a route. |
| `click` | `find` the element by accessibility tree using the quoted label, then click it (batched via `browser_batch`). One alternate phrasing on a miss (label vs aria-label, `getByText`, a `hints.selectors` entry); if the alternate hits, record a **label-drift** entry and continue. `computer` coordinate-click only on the §1c no-op cases. |
| `type` | `find` the target input, then set its value. `form_input` is the primitive; if setting a value via `javascript_tool` on a framework with reactive bindings, use the **native-setter + dispatch `input`** trick (a bare `.value =` does not notify the framework). Prefix any created/typed named entity with `E2E-{run-timestamp}-`; strip that prefix before evaluating a "verify the title is X" assertion. |
| `select` | `find` the select/combobox, choose the named option. For custom pickers that render the choice as a chip or an "N selected" indicator rather than plain text, verify the choice via `get_page_text` for the name, not a `value` attribute. |
| `verify` | Build the pass criterion from the step's `expected` and **read the real state on the channel the assertion lives on** (§2a criteria + §4). Record PASS/FAIL with the **observed** value verbatim. |
| `wait` | `browser_batch` `wait` with the stated duration — or, better, poll the DOM for the actual condition (element present, spinner gone, text stable). Don't sleep-and-hope. |
| `manual-check` | Screenshot + a transcript note ("step N needs human review"). **Do not fail the case** — tally it; if *every* step is manual-check the case grades **MANUAL**. |

### 2a. Verify criteria — precedence, each via a chrome read
Translate `expected` into the first criterion that fits, in this order:

1. **text-presence** — `get_page_text` (or a scoped DOM read) **contains** the expected string. Substring
   match when the expected wraps in quotes.
2. **url-match** — the current URL matches a regex derived from `expected` (read `window.location.href` /
   `location.pathname` via `javascript_tool`).
3. **element-visible** — a `find` query for the named element returns a hit. (A `find` *error* is the
   negative form — proof it is **not** visible.) Prefer a selector from `conventions.json` `app_handles` or
   the case's `hints.selectors` when one exists.
4. **count-matches** — for "N tasks displayed"-style assertions, regex-extract the number from
   `get_page_text` (or count DOM nodes) and compare.
5. **manual-check** — fallback when none of the above fit; screenshot + note, no automatic grade.

**A `verify` never grades off a toast** — the toast is the app's self-report; the criterion above must read
the artifact's real channel (§4).

---

## 3. Per-step recording, screenshots, run media, disconnect handling

### 3a. Write `step-<n>.json` IMMEDIATELY — the live stepper depends on it
The instant a step completes, before starting the next one, write
`<EVID_ROOT>/<KEY>/step-<n>.json`:

```json
{ "n": <n>, "status": "PASS|FAIL|INFO|BLOCKED", "note": "<observed value, verbatim>", "screenshot": "step-<n>-<verb>.png" }
```

The control plane polls these files to drive the live stepper — a step that isn't written yet is a step the
UI can't show. Don't batch the writes. Alongside it, append the step's `Action / Expected / Observed /
verdict` (and any `Drift:` line) to `transcript.md`.

### 3b. Screenshots
For every non-`verify` step, capture a `step-<n>-<verb>.png` beside the JSON — via `computer` (screenshot)
or a `browser_batch` screenshot action. The baseline goes to `before.png` (§1). Screenshots stay **local**;
the control plane serves them from the evidence dir, so just reference them by filename in the step file.
Store everything under `<EVID_ROOT>/<KEY>/` — never a scratch dir inside a repo (`mkdir -p
"$EVID_ROOT/$KEY"` if needed; POSIX shell only).

### 3c. Optional run recording
Best-effort, never required. If a watchable recording helps, capture frames with `gif_creator`, then convert
with `scripts/gif_to_mp4.sh` and upload with the shared helper
`.claude/skills/qa-repro/scripts/upload_evidence_media.sh <KEY> <file> gif|mp4 "<label>"`, collecting each
returned object into the contract's `media[]`. The helper goes through the `evidence_store` adapter
(`local` by default). If the adapter is unavailable or the upload fails, **omit `media` entirely** — the run
still validates.

### 3d. Disconnect handling — one retry, then BLOCKED
- **"Browser extension is not connected"** — a single drop is transient. Call `tabs_context_mcp` (no args,
  or `{ createIfEmpty: true }`) to **re-establish** the tab context, then **retry the failed step once**.
- **A second consecutive disconnect** → **grade BLOCKED** (`"driver lost after 2 attempts at step <n>"`) and
  stop. Do not soldier on and do not ask a human. Same one-retry-then-BLOCKED rule for a `find` that keeps
  missing after its single alternate phrasing — bail out of the rabbit hole after 2–3 failed attempts rather
  than looping.
- A **single step failing** does **not** abort the case — record it and continue the remaining steps (a
  later step may still surface useful signal).

---

## 4. Persistence traps — read the real state, never the toast

This is the highest-value, most transferable section. A target app's own read paths (a success toast, an
assistant's inline echo, a default-filtered widget) are **unreliable persistence oracles** in predictable
ways. The whole grading discipline reduces to: **after a mutation, read the artifact's true channel
independently — never accept the app's self-report as proof it persisted.**

**The spine — read the real state, never the toast.** A "Task created" toast, an assistant saying "done", a
200 in the network tab: each proves a *request happened*, not that a *user-facing record is correct and
persisted*. Confirm persistence by an **independent read** on the artifact's system of record:
- **DOM / widget / page** for a rendered value or a presentation assertion (`get_page_text`, a scoped DOM
  read after refresh).
- **The API** for backend state — a same-origin `fetch` to the endpoint the knowledge pack names for that
  artifact via `javascript_tool` (`credentials: 'include'`; the session cookies ride along), or a
  page-exposed client the pack documents. **Guard the failure shape explicitly**: a client that *resolves*
  on 4xx/5xx with an error object makes a wrong path a silent no-op that must **not** be reported as
  success. With no pack-documented read, reload and read an **unfiltered** list.
- **network + console** (`read_network_requests` / `read_console_messages` with the captured tab id) for an
  integration assertion (a call 4xx/5xx, a missing header, a stalled stream).

**The trap classes** (each pack states which of these its app has — `references/app-knowledge.md` §3):
- **Render caps on inline lists.** An assistant or "recent" widget that shows at most N rows. A count read
  there is a render cap, not the truth. Cross-check the full list page or the API.
- **A create can persist while the widget lies.** A dashboard widget defaulting to a "Mine" / "Open" /
  "This week" filter hides rows that exist — an agent-created row often has no owner and goes invisible
  even after refresh. Don't grade "not persisted" off a filtered widget. Preferred independent read: the
  unfiltered list or the API with the filter removed; cross-check the creation timestamp against this run
  to rule out a stale or echoed confirmation.
- **Read-path vs write-path divergence.** A value populated in the record (an auto-defaulted due date)
  renders as `—` on one surface but correctly on another. Cross-check against the surface that reads the
  stored field directly, never the one that formats it.
- **Optimistic UI.** The row appears before the request resolves; a failed save can leave it visible until
  reload. Reload, then read.

**Because there is no human to ask, an ambiguous persistence read resolves to a grade, never a question.** If
you cannot reach a truth channel to confirm a mutation (the API path 404s, the widget is filter-hidden and
the alternate read channel is unavailable, the tenant lacks the data shape the case needs), **grade BLOCKED
with the exact reason** — never `AskUserQuestion`, never a hang. A confirmed independent read that
contradicts the case's expectation is **FAIL** (mismatch verbatim); a mutation that succeeded but whose
side-assertion drifted (wrong toast text, count off by one, wrong artifact location) is **PARTIAL**. Grading
PASS off a toast without an independent read is the `false-pass-via-toast` failure category — **never do
it.**
