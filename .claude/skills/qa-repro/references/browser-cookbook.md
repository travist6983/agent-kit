# Browser-Driving Cookbook
> **The repo's single home for browser-driving mechanics.** `qa-repro` owns this file; `qa-verify` and `qa-explore` point at it **by path** (the same way `qa-verify` already references `qa-repro/references/recording.md`). Don't copy it into other skill dirs — one copy, referenced, so it can't drift.

**How to use it.** Read **Part A (general core)** whenever you drive Chrome for a defect. Read **Part B (streaming-agent appendix)** _only_ when the surface under test is a chat-driven streaming AI agent (an assistant or builder that answers over SSE) — for an ordinary UI defect (a row won't delete, a date renders wrong, a count is off) Part B's chat machinery is dead weight.

**The cardinal rule (why this file exists at all).** The assistant's words, a success toast, a green log, a 200 in the network tab — **none of those are evidence a user-facing thing is right.** Evidence is _you_ reading the real state through the app's own services/DOM after the action. Every snippet below serves that one discipline. (This is `qa-repro` Principle 1 and `qa-verify`'s "no trust-me" MUST-HAVE, made mechanical.)

**What is app-specific, and where it lives.** Everything in Part A is framework-agnostic except one thing: the **in-page app handle** — a JavaScript expression that reaches the running app's own API client / router / session, so your independent reads go through the exact authenticated pipeline the app uses. The target repo declares it in `.claude/loops/conventions.json → app_handles` (the sub-keys this file reads are listed in §A2). **When no handle is declared, every snippet degrades to DOM + network + an authenticated `fetch`** — slower to write, equally valid as evidence. Never hardcode a framework's internals (a global, a mount-point property) into a skill; put them in `app_handles` and reference them from here.

**Provenance.** The Part A mechanics (the `{}` trap, the swallowed-error trap, the backgrounded-tab freeze, the deterministic instance selection) were validated against a live reference app; the outputs shown are illustrative on the neutral example domain (Workspaces / Projects / Tasks). Part B is a general pattern for streaming surfaces and **must be validated against your app's own streaming service before you rely on it** — each Part B section says so.

* * *
## Table of contents
**Part A — general core**

- A0. Transport — the standing decision (don't re-decide per skill)

- A1. Session bootstrap (ToolSearch + tabs + hydration wait)

- A1.1. Deterministic Chrome instance selection (headless-safe — never deadlock)

- A2. The app handle — reach the app's own client, drive its services

- A3. `javascript_tool` return rules — the `{}` trap and how to avoid it

- A4. The independent read — pick the channel by defect class

- A5. Navigation by route name

- A6. Auth & tenant confirmation

- A7. Network & console as evidence

- A8. Waiting correctly

- A9. Recording (pointer)

- A10. Gotcha catalog (general-core subset)


**Part B — streaming-agent appendix (validate before relying on)**

- B1. Capture every stream channel

- B2. The `window.__driver` stateful pattern

- B3. Driving a chat input

- B4. Reset & idempotency (+ the R6 safety caveat)


* * *
# Part A — General core
## A0. Transport — the standing decision
You do **not** choose a transport per skill. It is standardized once at the evaluator level (`stack.config.json → browser`; see `CAPABILITIES.md` → Locked decisions):

- **`browser.live_driver` — Chrome MCP (`claude-in-chrome`)** drives the **live** app for repro / verify / explore / recording. It attaches to the user's already-authenticated Chrome, so there's no login step — and that's exactly why these skills confirm _which_ session they landed in (A6) instead of owning auth.

- **`browser.oracle_driver` — `playwright-cli`** runs the **deterministic replay** oracle: qa-repro's `replay-oracle.sh` + `replay-grader.js`, a headless script in its own private session that signs in as the `auth_provider` test account for the role the bug needs (login recipe rendered from `adapters.auth_provider.config.login`), replays the steps with role/label/CSS locators, and exits 0 GREEN / 1 RED / 2 BLOCKED (templates in `qa-repro/assets/`, renderer in `qa-repro/scripts/render_oracle.py`, recipe in `qa-repro` step 4; a pure-backend oracle is the same script with no browser). It does not use the Chrome transport and is not driven turn-by-turn from here. It is also **not a test file**: there are **no spec files** in the loops — nothing generates, runs, or heals one. The regression gate is the unit-test suite (`qa-regression`), not a browser.


A `playwright-cli` batch/parallel/prod sweep beyond the replay oracle, if ever needed, is a **recipe/runner** decision ("only the recipe knows the runner") — not something an atomic skill picks.
## A1. Session bootstrap
The `mcp__claude-in-chrome__*` tools are **deferred** — calling one before loading it fails with `InputValidationError`. So the **first action** of any browser phase loads every tool you expect, in **one** `ToolSearch` (each extra call is a wasted round-trip):

```
ToolSearch select:mcp__claude-in-chrome__list_connected_browsers,mcp__claude-in-chrome__select_browser,mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__read_page,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__javascript_tool,mcp__claude-in-chrome__read_console_messages,mcp__claude-in-chrome__read_network_requests,mcp__claude-in-chrome__find,mcp__claude-in-chrome__get_page_text
```

Add `gif_creator` for a recording skill; `form_input` if you'll use it instead of the JS send path. Only fire a second `ToolSearch` if a later step needs a tool you didn't anticipate. **`list_connected_browsers` + `select_browser` are loaded up front on purpose** — the very first browser action must be the deterministic instance-selection in §A1.1, before `tabs_context_mcp`.

> **Why encode this when the harness usually injects it.** In an interactive Claude Code session a global reminder nudges the bootstrap, so a skill can drive Chrome even without this line. But these skills are **runner-agnostic** (a CI runner or the control plane may not inject that reminder), so the explicit bootstrap is what makes the skill portable. Cheap insurance.

**Then** `tabs_context_mcp` **first**, to establish tab state before acting:

```
mcp__claude-in-chrome__tabs_context_mcp { createIfEmpty: true }
```

**Create vs reuse a tab.** A defect-style skill creates a **fresh** tab per session (`tabs_context_mcp` → `tabs_create_mcp`), then navigates. **Never reuse a tab id from a prior session.**

**Hydration wait after navigate.** A single-page app needs a few seconds to hydrate; a fixed `sleep` is flaky. Poll for a real readiness signal instead — the app handle **and** a resolved route when `app_handles` is declared, else the signed-in landmark from `adapters.auth_provider.config.login.signed_in_landmark`:

```js
// Run in javascript_tool after navigate. Note: top-level await, result is the LAST expression (see A3).
// H  = app_handles.expr      (e.g. "window.__app")                — null when the repo declares none
// RD = app_handles.ready_expr (e.g. "H.route && H.route.name")   — a truthy check on H
const getApp = () => { try { return (/* app_handles.expr */ null); } catch { return null; } };
const landmark = '[data-testid=user-menu]';          // adapters.auth_provider.config.login.signed_in_landmark
let H = null, ready = false;
for (let i = 0; i < 30 && !ready; i++) {
  H = getApp();
  ready = H ? !!(/* app_handles.ready_expr */ H.route && H.route.name) : !!document.querySelector(landmark);
  if (!ready) await new Promise(r => setTimeout(r, 200));
}
({ hydrated: ready, route: H && H.route ? H.route.name : location.pathname })
```
## A1.1. Deterministic Chrome instance selection (headless-safe — never deadlock)
**This is the FIRST browser action — run it before `tabs_context_mcp`.** More than one Chrome may have the extension installed and connected to the account at once (two profiles on the same box). `list_connected_browsers`' own contract then says the agent **must call `AskUserQuestion`** to let a human pick, and `switch_browser` broadcasts a pairing prompt and **waits up to 2 minutes** for a human to click *Connect*. **Both paths block on a human that a headless `claude -p` run does not have** — that is exactly the deadlock that silently kills an autonomous run (it sits on the ambiguity and exits without writing its artifact). So in an autonomous run you pick **deterministically, by deviceId, or you fail fast — you never ask and never broadcast.**

**Selection algorithm (deterministic; the only sanctioned path in an autonomous run):**

```
1. list = list_connected_browsers()                 # enumerate every connected extension instance
2. want = env CHROME_DEVICE_ID (or the CHROME_DEVICE_ID= prompt token)   # the runner-passed hint
3. if want is set:
     if want ∈ list.deviceId  → select_browser(want)         # exact, no broadcast, no prompt
     else                     → FAIL FAST (tooling-failed): "CHROME_DEVICE_ID=<want> not connected;
                                connected: [<name (deviceId)>, …]"   # loud, immediate, no hang
4. elif len(list) == 0:      → FAIL FAST (tooling-failed): "no Chrome extension connected."
5. elif len(list) == 1:      → select_browser(list[0].deviceId)   # unambiguous — the happy path
6. else (len ≥ 2, no hint):  → FAIL FAST (tooling-failed): "N Chrome instances connected and no
                                CHROME_DEVICE_ID set: [<name (deviceId)>, …]. Connect exactly one, or
                                pass CHROME_DEVICE_ID."          # NEVER AskUserQuestion / switch_browser
```
(Order matters: test the `want` hint, then the zero / one / many counts in that sequence — each branch is
reachable and its diagnostic names the case it actually caught.)

- **`select_browser(deviceId)` is the deterministic primitive** — it "selects a specific Chrome browser by deviceId **without broadcasting a pairing request**." It never prompts. That is why it's safe here and `switch_browser` is not.
- **Never call `AskUserQuestion` or `switch_browser` for instance selection in an autonomous run.** The `list_connected_browsers` description's "you MUST ask the user" guidance assumes an interactive human; it does **not** apply to a headless skill run, and following it there is the bug. (Interactive `/`-driven use is free to ask — this rule is scoped to autonomous runs.)
- **Fail fast, loud, and fast.** A tooling-failure here is `tooling-failed` (qa-repro → `status: inconclusive`; qa-verify repro-judge → **no verdict**, see that skill's fail-fast rule) — it must be an **immediate** exit with a one-line diagnostic naming the connected set, **not** a wait-for-timeout hang. The runner's subprocess timeout is the backstop, not the mechanism.
- **Single-instance is the recommended production posture.** The cleanest runner host connects **exactly one** Chrome extension instance, so step 5 always fires and `CHROME_DEVICE_ID` is never needed. The hint (step 3) is the escape hatch for hosts that must keep several profiles connected.

**Where `CHROME_DEVICE_ID` comes from — two independent channels.** (1) A **host-level env var**: if the runner *process* has `CHROME_DEVICE_ID` set, it forwards it (it's not secret-named) as a default for every run. (2) A **per-run prompt token** `CHROME_DEVICE_ID=<deviceId>` that the ratified command carries (see `contracts/README.md`), which the runner adds per request from its inputs. These are **separate** sources — a per-request input is *not* auto-exported to the process env, and vice-versa. Read whichever is present; if both, treat the explicit prompt token as the per-request override. Get the deviceId once from an interactive `list_connected_browsers` on the host and pin it (as the env default and/or the per-run input).

## A2. The app handle — reach the app's own client, drive its services
The single most powerful pattern: reach the running app's own client object and drive **its** services, rather than hand-rolling `fetch`/DOM/`new Date()`. Which expression reaches it is **app-specific and lives in config**, never in a skill. `conventions.json → app_handles` is a plain object; the sub-keys this cookbook reads (all optional — an absent key means "use the DOM/network fallback"):

| `app_handles.<key>` | what it is | example (neutral) |
| --- | --- | --- |
| `expr` | JS expression that evaluates to the handle `H` | `window.__app` |
| `ready_expr` | truthy when the app has hydrated (may reference `H`) | `H.route && H.route.name` |
| `api_get` | how to issue an authenticated GET through the app's client (`{route}` placeholder) | `H.api.get({route})` |
| `api_error_check` | expression over a response `r` that is **true when the call failed** | `!r || r.error` |
| `router_push` | navigate by route name (`{name}` placeholder) | `H.router.push({ name: {name} })` |
| `list_routes_expr` | enumerate named routes | `H.router.getRoutes().map(r => r.name)` |
| `route_name_expr` | the current route's name | `H.route.name` |
| `session_expr` | the authenticated principal + tenant | `({ user: H.session.user.id, tenant: H.session.workspaceId })` |
| `stream_send` | the production streaming-send method (Part B only) | `H.assistant.sendMessageStreaming` |
| `api_base_path` | the API prefix the client prepends (for A7's `urlPattern`) | `/api/` |

Read them once at the start of a browser phase (`python3 -c 'import json; print(json.load(open("<agents-repo>/.claude/loops/conventions.json"))["app_handles"])'`) and substitute into the snippets below, which write the handle as `H`:

```js
const H = /* app_handles.expr */ window.__app;
```

Everything hangs off `H` **when the repo declares those sub-keys**:

```js
await H.api.get('projects');                  // authenticated GET — the client injects the auth header(s) the backend needs
H.router.push({ name: 'ProjectBoard' });      // route by NAME (A5)
H.session.user.id; H.session.workspaceId;     // the current authenticated principal + tenant
```

**Why drive through the app's client, not raw `fetch`.** The client injects the bearer _and_ any tenant/session header the backend needs to resolve the principal — so you exercise the **exact authenticated pipeline production uses**. A raw `fetch` can miss a header, 401, and test a path the app never takes. (If you must `fetch`, copy the headers from a real request in `read_network_requests` — A7.)

**The "list a service's methods" trick** — use it instead of guessing a method name (services are under active development):

```js
Object.getOwnPropertyNames(Object.getPrototypeOf(H.api)).filter(n => n !== 'constructor')
```

**Discover the service map** with `Object.keys(H)` rather than carrying a list in a skill — it changes with every release, and a stale list is a wrong-surface trap. Record the services you actually used in `app_map` (`kb write app_map --kind handle …`) so the next run on that surface starts warm.

**No handle declared?** Then `H` is `null` and you read through the DOM (A4 presentation read), the network tab (A7) and an authenticated `fetch` whose headers you copied from a real request. Say so in `brief.json.evidence_used` — it is a valid read, just a slower one.
## A3. `javascript_tool` return rules — the `{}` trap
`javascript_tool` returns the value of the **last expression**, with REPL semantics (top-level `await` works). Two rules will save you from misdiagnosing a working backend:

**Rule 1 —** `await` **your async work; never return an un-awaited Promise.** This is the single biggest footgun, and it bites the `(async () => { … })()` IIFE idiom that older snippets use everywhere. An un-awaited Promise is `typeof object` with **zero own enumerable keys**, so it serializes to `{}` — you silently lose your result and may conclude "nothing happened."

```js
// ✗ WRONG — returns {} (the Promise has no own keys). Proven live.
(async () => { const H = window.__app; return { route: H.route.name }; })()

// ✓ RIGHT — top-level await, result is the LAST expression
const H = window.__app;
const data = await H.api.get('projects');
({ route: H.route.name, projects: Array.isArray(data) ? data.length : null })

// ✓ ALSO RIGHT — if you like the IIFE shape, AWAIT it
await (async () => { /* … */ return { ok: true }; })()
```

> **URLs are not the problem; un-awaited Promises are.** Tested live: a synchronous object holding full URLs _with a query string_ (`https://app.example.com/api/projects?x=1`) returned **intact**. If you ever see a populated object come back `{}` and you _did_ await, fall back to stashing on `window.__x` and reading it in a second sync call, and confirm side-effects via `read_network_requests` (A7).

**Rule 2 — the real return sanitizer redacts base64-looking strings.** Returning arrays of identifiers can come back with entries replaced by `"[BLOCKED: Base64 encoded data]"` (some minified method/route names trip a base64 heuristic). It's a per-string redaction, not a whole-object nuke. Workaround: return `.length`, a filtered subset, or specific known names; expect partial lists when you introspect minified prototypes.

**Keep returns small and scalar.** Slice GUIDs to 8 chars, return counts and keys rather than whole objects — easier to read and avoids tripping Rule 2.
## A4. The independent read — pick the channel by defect class
"Read the real state" is not one snippet — **the system of record depends on the bug.** Classify the defect, then read the channel where the truth actually lives. (Driving the wrong channel is how you false-pass a render bug whose API is green, or false-not-reproduce a data bug whose DOM looks fine.)

| Defect class | Primary read | Why |
| --- | --- | --- |
| **presentation / rendering** (layout, date format, a value shown wrong, a control's state) | **DOM / CSSOM** after refresh | the rendered pixels _are_ the truth; the API would read green while the UI is broken |
| **state-mutation / backend-state** (a save didn't persist, a field cleared, a count is off) | the app's API client (`app_handles.api_get`) **/** a service's state getter — or a hard reload + DOM when no handle is declared | the DB/service is the truth; the DOM may be stale or optimistic |
| **integration** (a call 4xx/5xx, a header missing, a stream stalls) | **network + console** (A7) | the request/response is the truth |

**Presentation read** (a nav link, for instance, returns its computed color and font size):

```js
const el = document.querySelector('SELECTOR');
const r = el.getBoundingClientRect();
const cs = getComputedStyle(el);
({ text: el.textContent.replace(/\s+/g,' ').slice(0,120),
   rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
   color: cs.color, fontSize: cs.fontSize, fontWeight: cs.fontWeight, display: cs.display })
```

**State-mutation read** — success vs failure shapes, with the failure check from config:

```js
const H = window.__app;                                  // app_handles.expr
const r = await H.api.get('projects/42/tasks');          // app_handles.api_get with {route}
const failed = (!r || r.error);                          // app_handles.api_error_check — the repo declares its client's error shape
failed
  ? ({ kind: 'FAILED', status: r && r.status })
  : ({ kind: 'SUCCESS', tasks: Array.isArray(r) ? r.length : null })
```

> **Know whether the app's API client THROWS or RESOLVES on 4xx/5xx.** Many clients _resolve_ with an error object instead of throwing — then **a wrong path looks like a successful no-op** and a "mutation" that 404'd reads as done. That is why `app_handles.api_error_check` exists: guard every read/mutation with it, never with "it didn't throw":
>
> ```js
> if (/* app_handles.api_error_check */ !r || r.error) { /* treat as failure — do NOT report success */ }
> ```
>
> Verify the **real** route before trusting it (the client prepends `app_handles.api_base_path`; a route that 404s under the prefix is a wrong route, not an empty result). Confirm with the network tab (A7) when in doubt.
## A5. Navigation by route name
Navigate by route **NAME**, not path — names are stable, paths change. With a declared router handle:

```js
const H = window.__app;                                  // app_handles.expr
H.router.push({ name: 'ProjectBoard' });                 // app_handles.router_push with {name}
await new Promise(r => setTimeout(r, 3500));             // let the route load + fire its API calls
H.route.name                                             // app_handles.route_name_expr
```

To discover a real route name instead of guessing: `app_handles.list_routes_expr` (e.g. `H.router.getRoutes().map(r => r.name).filter(Boolean).sort()`). Without a router handle, `navigate` to the path and confirm `location.pathname` + a landmark element after the hydration wait (A1).

**Query-param caveats** (carry these when a wizard/tabs surface is in play):

- Writing a query param (`?tab`/`?view`/`?step`) can **remount the page and wipe child state** (e.g. a live chat) depending on how the route is keyed. Re-establish what you were driving after such a write.

- `localStorage` can override a `?tab=…` you just set (a persisted "last tab"). If the URL must win, set the storage key first, then navigate.

## A6. Auth & tenant confirmation
Chrome-MCP skills **attach** to the user's authenticated Chrome — they never log in. So after navigate + hydrate, **confirm where you landed** before trusting anything:

- **Logged in** = the configured landmark is present: `adapters.auth_provider.config.login.signed_in_landmark` (the same selector the replay oracle waits for, so the live run and the oracle agree on what "signed in" means).

- **Which tenant** (workspace / organisation / account — whatever the app's unit of isolation is) = `app_handles.session_expr` when declared, else whatever the UI shows (an account menu, a header badge). Compare it with the tenant the ticket's evidence names.

```js
const landmark = '[data-testid=user-menu]';              // adapters.auth_provider.config.login.signed_in_landmark
const H = window.__app;                                  // app_handles.expr (may be null)
({ loggedIn: !!document.querySelector(landmark),
   tenant: H && H.session ? H.session.workspaceId : null,  // app_handles.session_expr
   title: document.title, url: location.href })
```

- **Not logged in / wrong tenant?** Surface it and stop (for a verifier, return `blocked`, not a verdict) — a wrong-tenant or anonymous session produces confident-but-wrong findings. Bugs are tenant-data-shape-dependent.

- **Switching tenant is the user's call — ASK first** (it changes their active context everywhere). Don't switch autonomously.

- **Identity-provider quirks on `localhost`** (consent screens that cannot be skipped, redirect URIs that refuse a loopback origin) can block invite / set-password / auto-login flows locally. Verify those flows on a deployed environment from `app.environments` instead of burning iterations fighting the local case; record the quirk in `app_map` so the next run skips the detour.

## A7. Network & console as evidence
**Network side-channel** — filter to the API prefix the client uses (`app_handles.api_base_path`) or the route under test:

```
mcp__claude-in-chrome__read_network_requests { tabId: <id>, urlPattern: "projects/42/tasks|/api/", limit: 20 }
// → e.g. /api/projects/42/tasks GET 200, /api/tasks POST 201, OPTIONS preflights 204
```

Two uses: (1) see the exact request shape the frontend sent vs what the backend returned; (2) **confirm status independently** of a JS return — if a `javascript_tool` read is ever ambiguous, the network tab is ground truth. Note: **tracking starts on first call**, so if the page loaded before you called it, navigate/act again to populate it.

**Console** (filter to cut noise; "no new console errors on the exercised path" is a grading signal — but diff against a baseline, since many apps emit unrelated console noise on many pages, so a blanket "zero console errors" gate would manufacture false fails):

```
mcp__claude-in-chrome__read_console_messages { tabId: <id>, pattern: "error|exception|<feature>" }
```
## A8. Waiting correctly
Match the wait to the surface — don't `sleep`-and-hope:

- **Pure-frontend** (most defects): poll the DOM for the condition you actually need (an element present, text stable, a spinner gone). The hydration poll in A1 is the template.

- **Agent/backend surfaces** (Part B): the robust wait is **Monitor a tee'd log with an anchored,** `--line-buffered` **grep** — the grep _is_ the wait condition. This presumes a tee'd log exists (a path the recipe / sandbox step provisions when it starts the app — there is none by default); on a pure-frontend defect there is no such log and DOM polling is the wait. `--line-buffered` is mandatory or grep buffers and Monitor sees nothing.

**Check** `document.visibilityState` **FIRST** when "clicks do nothing," menus won't open, or screenshots look stale: a **backgrounded Chrome tab freezes** `requestAnimationFrame` — transitions wedge and screenshots go stale. This is the single most misdiagnosed symptom. Verify via DOM probes, not screenshots, and bring the tab to the foreground.
## A9. Recording (pointer)
For defect before/after media (`gif_creator` → GIF + transcoded MP4, frame padding, watermark off, `cp`-not-`mv`, the PASS gate, the `evidence_store` upload), use `recording.md` — one copy, don't duplicate it here.
## A10. Gotcha catalog (general-core subset)
Encode the relevant ones into any browser phase:

- **An API client that swallows 4xx/5xx** resolves with an error object; a wrong path is a silent no-op. Guard with `app_handles.api_error_check` (A4). _(Live-confirmed on a reference app.)_

- **Un-awaited Promise returns** `{}` → `await` your async work (A3). _(Live-confirmed; this is NOT a URL/privacy-guard issue.)_

- **Base64-looking strings come back** `[BLOCKED: …]` in returns → read them another way (A3). _(Live-confirmed.)_

- `find` **raises an ERROR (not an empty list) on no-match** — text like "does not contain" is positive evidence of "not visible." A failing `find` mid-`browser_batch` aborts the whole batch → put a negative `find` last.

- **Refs invalidate across a repaint** — don't `find`-then-`click` a just-opened dropdown item in the same batch; re-find after the repaint. Re-find inputs every turn; never reuse refs across pages.

- **Viewport coordinates are scaled** vs the real viewport — prefer refs/JS over hardcoded coordinates; re-screenshot before relying on a coordinate.

- **Never trigger native** `alert`**/**`confirm`**/**`prompt` — they freeze the extension and kill the session mid-recording. Avoid Delete buttons / native-modal triggers; dismiss a pending dialog via `javascript_tool` first if unavoidable.

- **Extension drops mid-run** → `tabs_context_mcp` (no args) to re-establish + retry once; **two disconnects in a row → surface and stop**, don't soldier on.

- **Bail out of rabbit holes after 2–3 failed attempts** — one alternate phrasing for a missing element, then fail the step; one retry on a disconnect, then surface.

- **A synthetic-event harness over-creates / ghosts** where a real user doesn't — re-verify a suspected duplicate/ghost at human pace before calling it a product bug (driver artifact vs real bug).

- **Don't probe a live user tab carelessly** — many frontends pipe 4xx to red error toasts that reach the error-signal source the `analytics` adapter reads as real errors. Prefer reading an in-flight response or the server log over firing exploratory bad requests.


* * *
# Part B — Streaming-agent appendix
> **LOAD THIS ONLY when the surface under test is a chat-driven streaming AI agent** (an assistant or builder that answers over server-sent events and acts through tool calls). For an ordinary UI defect, skip it entirely.
>
> ⚠️ **Provenance: a general pattern, NOT validated against your app.** The method names and selectors below are placeholders — **confirm them with the A2 list-methods trick against a live session of your app's streaming service (`app_handles.stream_send`) before relying on them.** When you validate one, record the real name in `app_map`, not in this file.
## B1. Capture every stream channel
A streaming agent emits on **separate** channels; subscribing to only one makes a working backend look like a no-op. Capture all of them — typically:

- `onToken` → the assistant's prose (final text).

- `onStatus` → ephemeral status copy (verification, etc.).

- `onToolCall` → **the tool-line acks** — fires once per `tool_call` with the backend tool name (`create_task`, …). This is how you see _what the agent did_.

> Hard-won lesson: a run that "saw no tool-lines" only because it subscribed to `onStatus` but not `onToolCall` produces a false "the agent didn't act" finding that later has to be retracted. **A missing tool-line is a driver mistake, not a backend bug.** Consume every event type before concluding the backend didn't do X.
## B2. The `window.__driver` stateful pattern
Install a driver once that remembers session id / revision id / history across turns, then drive one turn per call. Prefer the **real production frontend service** (`app_handles.stream_send`, e.g. `H.assistant.sendMessageStreaming`) so you exercise the exact path the chat UI uses; fall back to a raw SSE helper on the app's client only if the service method is unavailable. Mirror the shape below as `window.__driver`:

```js
window.__driver = window.__driver || {
  sessionId: null, revisionId: null, history: [],
  async send(text) {
    const H = window.__app;                                  // app_handles.expr
    const events = { tokens: [], status: [], tools: [] };
    await H.assistant.sendMessageStreaming({                 // app_handles.stream_send — confirm the real signature (A2)
      sessionId: this.sessionId, revisionId: this.revisionId, text,
      onToken: t => events.tokens.push(t),
      onStatus: s => events.status.push(s),
      onToolCall: c => events.tools.push(c.name),
    });
    this.history.push({ text, tools: events.tools.slice() });
    return { tools: events.tools, text: events.tokens.join('').slice(0, 200) };
  },
};
'installed'
```

> **The ~45s** `javascript_tool` **cap.** Don't poll a turn to completion inside one eval (a long turn times out mid-turn). **Fire** `send()` **in one call, then poll for done in _separate_ short calls** (text-stability on the last assistant bubble). And remember A3: `await` inside each call, return scalars.
## B3. Driving a chat input
- **Native-setter trick.** `ta.value = text` alone does **not** update an input bound by a reactive framework — the framework listens for the `input` event from the _native_ value setter. Use the prototype setter, then dispatch `input`:
  ```js
  const ta = document.querySelector('textarea[name=message]');
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(ta, 'create a task called Follow up');
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ```
  **Click the Send button; never** `form.submit()` (custom handlers; `.submit()` sends nothing). **Poll until the Send button re-enables** between turns (it stays disabled ~1–2s after a stream ends).

- **No handle reach (attached Chrome only)?** Re-find the input every turn via the accessibility tree (`find "message input"`) — coordinates shift across page contexts; never trust prior coords.

- **Talk like a human** when the agent under test is what real users hit: lowercase, contractions, natural-language dates ("next Tuesday at 10am") not ISO — unless you're testing an ISO failure path. (See A3 for the return rules that apply to the send snippet.)

## B4. Reset & idempotency (+ the R6 safety caveat)
Clean blank state per persona/case (a persona starting on the prior one's data yields false "the agent auto-filled it" findings). Pin the revision/session id every turn (omit it and the server may fall back to a canonical draft that isn't the open one). Pass an idempotency key + expected version (optimistic concurrency) on mutations when the API supports them.

> ⚠️ **R6 blast-radius caveat — read before pasting a reset helper.** A reset that `delete`s records is a **mutating** action. While the `staging_only` posture flag is set, these skills run **staging-only / dry-run** — so: only ever run a destructive reset against a disposable record you created in a non-production environment, **never** against shared or production tenant data, and never without an explicit gate. A reset helper is a paved road; don't let it become a paved road _around_ the safety gate. Verify the API path is real first (A4 — a wrong path can 404 silently and a "reset" that no-ops looks successful).
