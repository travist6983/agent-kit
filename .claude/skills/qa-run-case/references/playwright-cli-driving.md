# DRIVER=cli — driving `@playwright/cli` headlessly

> **The `DRIVER=cli` discipline for qa-run-case.** The default driver: drive the live target app with
> `@playwright/cli` shell invocations — headless, no MCP, ~10× cheaper than the chrome driver. Read this
> before step 0 of the SKILL when `DRIVER` is unset or `cli`. The CLI is `browser.oracle_driver` in
> `.claude/loops/stack.config.json`. This is a **headless `claude -p` run**: there is **no human in the
> loop**, so every place an interactive runner would pause to ask — pick an env, pick a browser, retry
> credentials — is replaced here by **grade BLOCKED with the exact reason**. There is **no
> `AskUserQuestion` anywhere in this file.**

**POSIX shell only.** Every snippet below is bash; the `playwright-cli` invocations themselves are a
cross-platform Node CLI. Where macOS and Linux differ (file mtime), the portable form is given in §5.

**Two load-bearing bash facts for this harness.** (1) The Bash tool **blocks a bare foreground `sleep`** —
never issue `sleep 3` on its own line; chain it (`sleep 3; playwright-cli …`) or replace it with a
Playwright wait (§2 `wait`). (2) **Shell env does NOT persist between Bash calls** (the shell reinitializes
each call) — so every command that needs the auth-bundle values must **re-source `run.env` in that same
command**, or be chained into one command with the step that started the session. Working directory
persists; env does not. Use absolute paths throughout.

**Config reads.** Wherever a snippet needs a value from `stack.config.json`, read it through the adapter
package (never hard-code it):

```bash
REPO_ROOT="${LOOPS_REPO_ROOT:-$(git rev-parse --show-toplevel)}"
cfg() { python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from lib.adapters import cfg; v = cfg(sys.argv[2]); print("" if v is None else v)' "$REPO_ROOT" "$1"; }
# e.g. cfg app.environments.prod.url  ·  cfg adapters.auth_provider.config.login.signed_in_landmark
```

---

## 0. Bootstrap → session → perimeter → health-check (the ordered preamble)

Do these **in order**. Any failure here is a setup failure → write `case-run.json` with `status: BLOCKED`
and the exact reason, then stop. Never ask.

### 0a. Self-install `@playwright/cli` if missing

```bash
playwright-cli --version 2>/dev/null || npm install -g @playwright/cli@latest
```

If `playwright-cli --version` prints `command not found`, install the global package, then re-run
`--version` to confirm. Capture the version string — it goes in the run notes. If the install itself fails
(no network, npm error), grade **BLOCKED** ("playwright-cli unavailable and self-install failed: <tail>").

**Read the CLI's own bundled SKILL.md once** for the authoritative command surface (it ships one). Resolve
its path with `npm root -g`, never a hard-coded path:

```bash
cat "$(npm root -g)/@playwright/cli/node_modules/playwright-core/lib/tools/cli-client/skill/SKILL.md"
```

### 0b. Prod guard (before anything touches the network — see §5)

If `APP_URL`'s host equals the host of `app.environments.prod.url` and `ALLOW_PROD` is not `1` →
**BLOCKED** and return immediately. Do this before opening a browser. With no prod URL configured the
guard has nothing to compare against and passes — say so in `notes`.

### 0c. Open ONE persistent session, load auth, install the perimeter route — as ONE chained command

The auth bundle lives at `<AUTH_DIR>/` (= `<EVID_ROOT>/<KEY>/auth/`): `storage-state.json` (the
`auth_provider` session, a Playwright `storageState` dict) + `run.env` (env vars incl. the optional
pre-auth triple). Require both; missing/empty → **BLOCKED** ("auth bundle missing at <AUTH_DIR>"). The
key contract is `references/auth-bundle.md`.

Because shell env does not persist between Bash calls **and** the CLI's `run-code` executes inside the
long-lived process spawned by `open` (so it inherits *that* process's env, not a later command's), the
robust pattern is to **source `run.env` and start the session in one command**, then install the perimeter
route **before** the first navigation:

```bash
set -a; . "$AUTH_DIR/run.env"; set +a                    # export E2E_BASE_URL, E2E_API_URL, E2E_ENVIRONMENT,
                                                          # E2E_PERIMETER_ORIGIN, E2E_PERIMETER_HEADER, E2E_PERIMETER_TOKEN
playwright-cli -s=tester open --persistent               # blank page — do NOT pass APP_URL yet
playwright-cli -s=tester --raw state-load "$AUTH_DIR/storage-state.json"
playwright-cli -s=tester run-code --filename="$EVID_ROOT/$KEY/cli-scripts/inject-perimeter.js"
```

- **Open blank, not on `APP_URL`.** A route interception only affects *future* requests, so it must be
  registered before you navigate. If `open` navigated straight to `APP_URL` the first (perimeter-gated)
  request would fly before the route exists. Open `--persistent` with **no URL** (blank page), then goto in
  0e.
- `-s=tester` names the single session. `--persistent` keeps the browser alive across every later
  `playwright-cli` call in the run; `playwright-cli -s=tester close` at the very end (optional — leaving it
  open is fine for a runner-spawned process since the runner tears it down anyway).
- **Never echo** `storage-state.json` or `E2E_PERIMETER_TOKEN` to the transcript, a screenshot, or stdout.
  They are 0600 secrets. The injection script (§3) reads the token from `process.env` — it never appears in
  the `cli-scripts/` artifact.

### 0d. Health-check `APP_URL`

```bash
code=$(curl -sk -o /dev/null -w "%{http_code}" "$APP_URL"); echo "$code"
```

`-s` silent, `-k` tolerate a perimeter's cert. A non-2xx/3xx (e.g. `000` unreachable, `502`, `503`) →
**BLOCKED** ("app unreachable at <APP_URL>: <code>"). A `401`/`403` here is expected when an HTTP perimeter
is configured (`pre_auth: "header_token"` — that is the perimeter answering) and is *not* itself a block —
the route injection is what carries you past it. With `pre_auth: "none"`, a `401`/`403` on the bare origin
is worth a note but the authenticated-session check in 0e is the real verdict.

### 0e. Navigate and confirm the authenticated session

```bash
LANDMARK="$(cfg adapters.auth_provider.config.login.signed_in_landmark)"
LOGIN_PATH="$(cfg adapters.auth_provider.config.login.path)"
playwright-cli -s=tester --raw goto "$APP_URL" ; sleep 4 ; \
  playwright-cli -s=tester --raw screenshot --filename="$EVID_ROOT/$KEY/before.png" ; \
  playwright-cli -s=tester --raw eval "location.pathname" ; \
  playwright-cli -s=tester --raw eval "!!document.querySelector('$LANDMARK')"
```

The landing page must render **signed in**: the `login.signed_in_landmark` selector resolves (`true`) and
the path is not under `login.path`. If the page bounced to the **login page or a perimeter challenge**
(landmark absent, path under `login.path`, or a challenge page from the perimeter), the bundle is stale or
the perimeter header didn't inject → **BLOCKED** ("auth bundle did not authenticate — session bounced to
<where>"). Confirm the perimeter header actually rode a request (§3) before blaming the session state.

If the adopter's knowledge pack (`references/app-knowledge.md`) names a stronger ready/authenticated
signal — a title pattern, a tenant label — use it in addition to the landmark, never instead of it.

---

## 1. Idiomatic `@playwright/cli` usage (this is ~95% of the token efficiency)

1. **Default to `--raw`.** For any known-shape command, `--raw` strips the status banner and the
   accessibility-snapshot block:
   - `click`, `fill`, `goto`, `press`, `select`, `state-load`, `state-save`, `screenshot` → output is
     **empty on success**. Empty stdout = it worked.
   - `eval` → returns the **raw JSON value** (e.g. `true`, `"/projects"`, `7`, `"Tasks · Example Workspace"`).
   - `request-headers`, `requests` → the raw introspection payload.
2. **Name the session `-s=tester`** on **every** invocation. qa-run-case drives ONE case with ONE session
   (unlike a multi-role smoke runner). Omitting `-s` risks hitting the wrong/default session after a
   relaunch. Always `-s=tester`.
3. **Chain independent actions in one Bash turn with `;`.** Each `playwright-cli` call is sub-second, so
   `click ; sleep 1 ; screenshot ; eval` in one line saves an LLM turn at near-zero latency cost. This is
   the CLI's core advantage over the MCP driver — use it aggressively (esp. action + its screenshot).
4. **Prefer role/text locators over re-snapshotting.** `getByRole('button', { name: 'Add Task' })` and
   `getByText('…')` **survive a full DOM rebuild** (route change, modal open) — they re-resolve at click
   time. Take a full `snapshot` **only** when you need fresh refs for an unfamiliar element, and write it to
   a file, never to stdout: `playwright-cli -s=tester snapshot --filename="$EVID_ROOT/$KEY/cli-scripts/snap-step-<n>.yaml"` then `Read` that file. The file path is what the transcript cites.
5. **`run-code --filename=<file>` for any JS longer than ~3 lines.** Write the JS into
   `<EVID_ROOT>/<KEY>/cli-scripts/<name>.js` and run it with `--filename` — this dodges shell-quoting hell
   and leaves a citable artifact. The script's last expression is its return value; **`await` all async
   work** (an un-awaited Promise serializes to `{}` and you silently lose the result). Never write a secret
   into a `cli-scripts/` file — read it from `process.env` inside the script.
6. **`requests` + `request-headers <n>` for network introspection** — purpose-built, cleaner than a
   `performance.getEntriesByType` eval. Use it to confirm status codes independently and to verify the
   perimeter header injected (§3). Note tracking starts when the page begins loading; a request made before
   the browser opened won't appear.
7. **`state-save` / `state-load`** persist/restore the Playwright storage state (cookies + localStorage).
   qa-run-case **loads** the runner-minted bundle (`state-load "$AUTH_DIR/storage-state.json"`, 0c); it
   generally does **not** save — the runner owns auth minting through the `auth_provider` adapter. The one
   exception: after a mid-run relaunch (§4) you `state-load` the same bundle again to restore the session
   into the fresh context.
8. **Keep the browser open across the whole run.** Open `--persistent` once (0c); every later call reuses
   it via `-s=tester`. Don't reopen per step.

---

## 2. Verb → `playwright-cli` mapping

Classify each `steps_structured[].action` by leading keyword (see `references/step-classification.md` for
the full table); execute per this driver map. Prefix any created/typed named entity with
`E2E-{run-timestamp}-` (or the prefix the adopter's knowledge pack documents); strip that prefix before
evaluating a "verify the title is X" assertion.

| verb | `playwright-cli` execution |
|---|---|
| **navigate** | `playwright-cli -s=tester --raw goto "<url>"` then a wait. If the target is a popover/widget with no route, `goto "$APP_URL/"` then `click` the entry-point button. Consult the knowledge pack (`references/app-knowledge.md`) for real routes — **never guess a `/<feature>`-style URL** (a wrong path 404s and wastes a turn). With no pack, start from `/` and find the entry point in the rendered UI. |
| **click** | `playwright-cli -s=tester --raw click "getByRole('button', { name: '<label>' })"`. On a miss, try **exactly ONE** alternate phrasing (`getByText('<label>')`, an alternate role, or a `hints.selectors` entry). If the alternate hits → record a **label-drift** entry and continue; if it misses too → **fail the step** (do not loop). |
| **type** | `playwright-cli -s=tester --raw fill "<locator>" "<value>"`. Add `--submit` when the next step is "press Enter" (saves a turn). Mutation steps get the `E2E-{run-timestamp}-` prefix. |
| **select** | native `<select>`: `playwright-cli -s=tester --raw select "<locator>" "<option>"`. Custom dropdown: `click` the trigger, then `click` the option (same one-alternate-then-fail + drift rule as click). |
| **verify** | an `eval`-based assertion — read the **real state**, never a success toast. Pick the channel by the `expected` text (precedence below), capture the JSON return, record PASS/FAIL with the **observed** value verbatim. |
| **wait** | `sleep <N>` **chained** with the next action (`sleep <N>; playwright-cli …`) — never a bare foreground `sleep` (the Bash tool blocks it). Better: a deterministic Playwright wait via `run-code` — `await page.waitForSelector('<sel>', { timeout: <ms> })` or `await page.waitForLoadState('networkidle')` — poll for the condition rather than guessing a duration. A short `run-code` delay (`await page.waitForTimeout(<ms>)`) is the fallback when only a fixed pause will do. |
| **manual-check** | can't be auto-executed → `screenshot`, note "Step N: <verbatim action> — could not be auto-classified, needs human review", and mark the step for a MANUAL tally. Do not fail the case here. |

### Verify-criteria `eval` snippets (precedence order — first that fits wins)

- **text-presence** — `playwright-cli -s=tester --raw eval "document.body.innerText.includes('<expected>')"` → expect `true`.
- **url-match** — `playwright-cli -s=tester --raw eval "location.pathname"` → returns e.g. `"/projects/42/tasks"`; match it against a regex derived from the expected result.
- **element-visible** — `playwright-cli -s=tester --raw eval "!!document.querySelector('<selector>')"` → expect `true`. Prefer a selector from `conventions.json` `app_handles` or the case's `hints.selectors` over one you guessed.
- **count-matches** — `playwright-cli -s=tester --raw eval "document.querySelectorAll('<selector>').length"` → returns a number; compare to the expected count (e.g. `document.querySelectorAll('[data-testid=task-row]').length`).
- **manual-check** — fallback when none of the above express the expectation → treat the step as `manual-check`.

For a **state-mutation** verify (did a save persist?), the DOM can be optimistic/stale — read the system of
record instead. In precedence: (1) an authenticated API read, when the knowledge pack names the endpoint
for that artifact (the session cookies ride a same-origin `fetch`; `E2E_API_URL` is the API origin);
(2) a page-exposed client the pack documents; (3) a reload followed by an unfiltered list read. Longer
than 3 lines → `run-code`:

```js
// cli-scripts/verify-persist.js — read the truth channel, not the toast
// The endpoint and the shape come from the adopter's knowledge pack; this is the neutral example domain.
const api = process.env.E2E_API_URL || location.origin;
const r = await fetch(`${api}/projects/${projectId}/tasks?all=1`, { credentials: 'include' });
// Guard the failure shape explicitly — a client that RESOLVES on 4xx/5xx makes a wrong path a silent no-op.
r.ok
  ? ({ kind: 'OK', count: (await r.json()).length })
  : ({ kind: 'ERROR', status: r.status });
```

---

## 3. Perimeter injection — the pre-auth header route interception (CRITICAL when configured)

A hosted environment may sit behind **two** gates: (1) an optional **HTTP perimeter** (a gateway or an
identity proxy in front of the app), passed with a fresh header minted per run by the runner —
`adapters.auth_provider.config.pre_auth = "header_token"`; (2) the **app login**, passed by the loaded
`storage-state.json`. The perimeter header is **not** in the storage state — you must inject it on
**every** request to the perimeter origin yourself. With `pre_auth: "none"` (the default) or
`"sso_human_once"`, the triple in `run.env` is empty and this whole section is a no-op.

**Two rules this driver MUST preserve** (each is a day of debugging when broken):

1. **Never `setExtraHTTPHeaders` / a global header.** A custom header on a **cross-origin**
   identity-provider request trips a CORS preflight the provider rejects → session refresh breaks → the SPA
   bounces to the login page. The header must be **scoped to the perimeter origin only**.
2. **Exactly ONE `context.route` per origin.** `route.continue()` is terminal — two routes registered on
   the same origin clobber each other and whichever registered first silently stops applying. One route,
   one origin.

So: **one `context.route(\`${E2E_PERIMETER_ORIGIN}/**\`, …)`** that merges a fresh
`E2E_PERIMETER_HEADER: E2E_PERIMETER_TOKEN` onto the existing request headers and continues. Written as
the `inject-perimeter.js` that 0c runs (reads everything from `process.env` — the secret never lands in the
artifact):

```js
// cli-scripts/inject-perimeter.js — run via `playwright-cli -s=tester run-code --filename=…` BEFORE goto.
// Origin, header name and token come from run.env (sourced into this process's env at `open` time); NOT hardcoded.
const origin = process.env.E2E_PERIMETER_ORIGIN;
const header = process.env.E2E_PERIMETER_HEADER;
const token  = process.env.E2E_PERIMETER_TOKEN;
if (token && origin && header) {
  const ctx = page.context();                       // the CLI exposes `page`; route on its context
  await ctx.route(`${origin}/**`, async (route) => {
    await route.continue({
      headers: { ...route.request().headers(), [header]: token },
    });
  });
  ({ injected: true, origin, header });             // scalar return; never return the token
} else {
  ({ injected: false, reason: 'E2E_PERIMETER_TOKEN, E2E_PERIMETER_HEADER or E2E_PERIMETER_ORIGIN empty' });
}
```

- **Empty `E2E_PERIMETER_TOKEN` ⇒ skip injection, honestly.** That is the guard in the script above and
  the "empty string ⇒ no perimeter" convention the runner follows when the environment has no pre-auth
  gate. An env with no perimeter needs no header — proceed to `goto` without injection. If the app then
  serves without a challenge, fine; if it challenges, that's an honest BLOCKED (below), not a bug to paper
  over.
- **Scope strictly to `E2E_PERIMETER_ORIGIN`.** Do not add the identity provider's origin, and do not route
  `${APP_URL}/**` blindly if `APP_URL`'s origin differs from `E2E_PERIMETER_ORIGIN` — the perimeter origin
  is sourced from the auth provider's login origin and is the authoritative perimeter host (it is *sourced
  differently* from `E2E_BASE_URL` even when they look equal). One origin, from `run.env`.
- **Confirm the header actually rode a request** after 0e's `goto`, without leaking its value:

  ```bash
  set -a; . "$AUTH_DIR/run.env"; set +a
  playwright-cli -s=tester --raw requests | head -20        # find the request # hitting the perimeter origin
  playwright-cli -s=tester --raw request-headers <n> | grep -qi "^${E2E_PERIMETER_HEADER}:" && echo HEADER_PRESENT || echo HEADER_ABSENT
  ```

  Report only the boolean `HEADER_PRESENT` / `HEADER_ABSENT` — never the token. If `HEADER_ABSENT` and the
  token was non-empty, the injection didn't take (env didn't propagate to the CLI process, or `open`
  navigated before the route registered) — reopen blank, re-`state-load`, re-run `inject-perimeter.js`,
  then `goto` again (§4 relaunch).
- **If navigation still bounces to the login page or a perimeter challenge** after a confirmed injection
  (or after an honest no-perimeter skip), the perimeter/auth is not satisfiable headlessly → **grade
  BLOCKED** ("auth bundle did not authenticate — session bounced to <login path | perimeter challenge>;
  perimeter header <present|absent>"). Never attempt an interactive login — there is no human.

---

## 4. Per-step recording + within-case failure handling

### Record every step the instant it finishes (the live stepper depends on it)

After each step, write **both** a live-progress receipt and (for non-`verify` steps) a screenshot, beside
the case in the flat evidence dir — no `TC-<id>/` nesting:

```bash
# chain the action, its screenshot, and the receipt write in ONE Bash turn
playwright-cli -s=tester --raw click "getByRole('button', { name: 'Add Task' })" ; sleep 1 ; \
  playwright-cli -s=tester --raw screenshot --filename="$EVID_ROOT/$KEY/step-3-click.png"
```

Then write `<EVID_ROOT>/<KEY>/step-<n>.json` **immediately** (this is what the control plane polls for the
live stepper — it is only as fresh as this file):

```json
{ "n": 3, "status": "PASS", "note": "clicked Add Task; modal opened", "screenshot": "step-3-click.png" }
```

`status ∈ PASS|FAIL|INFO|BLOCKED` (a `wait`/`navigate` with nothing to assert is `INFO`). Append the step's
verbatim **Action / Expected / Observed / verdict** to `<EVID_ROOT>/<KEY>/transcript.md`; if the step
produced a label-drift entry, append a `Drift:` line and add it to `<EVID_ROOT>/<KEY>/drift.json`. Skip the
screenshot for pure `verify` steps (no visible state change) but still write their `step-<n>.json`.

### Within-case failure handling

- **A single step fails** → record the FAIL, **continue** the remaining steps. A downstream step often
  still yields signal, and the report is more useful complete. One failing step does not abort the case.
- **Element not found** → one alternate phrasing (§2 click/select), then fail the step. **Never loop** on a
  missing element.
- **Session lost** — stdout says "Browser session not found", "Target closed", or "Chromium has crashed".
  **Relaunch once and retry the step:**

  ```bash
  set -a; . "$AUTH_DIR/run.env"; set +a
  playwright-cli -s=tester open --persistent
  playwright-cli -s=tester --raw state-load "$AUTH_DIR/storage-state.json"
  playwright-cli -s=tester run-code --filename="$EVID_ROOT/$KEY/cli-scripts/inject-perimeter.js"   # REINSTALL — new context, route is gone
  playwright-cli -s=tester --raw goto "$APP_URL"
  ```

  The relaunch context is **fresh**, so you must re-`state-load` **and** re-run `inject-perimeter.js`
  before renavigating — the perimeter route does not survive a new context. Then retry the failed step
  once.
- **Second consecutive session loss** → **grade BLOCKED** ("driver lost after 2 attempts at step <n>") and
  stop. Don't grind a broken session.

---

## 5. Prod guard, portable mtime, and the no-ask law

### Prod guard (run in 0b, before the network)

The production host is **config**, never a literal in this skill:

```bash
PROD_URL="$(cfg app.environments.prod.url)"
host_of() { python3 -c 'import sys, urllib.parse; print((urllib.parse.urlsplit(sys.argv[1]).hostname or "").lower())' "$1"; }
if [ -n "$PROD_URL" ] && [ "$(host_of "$APP_URL")" = "$(host_of "$PROD_URL")" ] && [ "${ALLOW_PROD:-0}" != "1" ]; then
  echo "BLOCKED: prod guard"            # → write case-run.json BLOCKED, return
fi
```

If `APP_URL` resolves to the production host and `ALLOW_PROD` is not `1`, **do not open a browser** —
write `case-run.json` with `status: BLOCKED`, `summary: "BLOCKED: prod guard — refused production URL
<url> without ALLOW_PROD"`, and return. An empty `PROD_URL` (no prod environment configured) means the
guard passes — record "prod guard: no prod URL configured" in `notes` so the gap is visible.

### File mtime — portable (macOS and Linux runners)

To check the age of an on-disk artifact — e.g. is the auth bundle stale, when was `storage-state.json`
minted — use a form that works on both BSD and GNU userlands. BSD `stat -f %m` errors on Linux and GNU
`stat -c %Y` errors on macOS, so go through Python:

```bash
mtime=$(python3 -c 'import os, sys; print(int(os.path.getmtime(sys.argv[1])))' "$AUTH_DIR/storage-state.json")
age=$(( $(date +%s) - mtime ))                          # seconds old
echo "$age"
```

A runner-minted pre-auth token is short-lived (the TTL is the auth provider's, not this skill's); if the
bundle mtime shows it is well past a plausible TTL and the session bounces, that's a stale-bundle
**BLOCKED** the runner re-mints on retry, not something to retry here.

### The no-ask law (absolute)

There is **no `AskUserQuestion`, no `switch_browser`, no interactive prompt anywhere in this driver.** This
runs headless under `claude -p` with no human to answer. Every place an interactive smoke runner would
pause — confirm the env, disambiguate a browser, retry failed credentials, choose whether to switch
tenant — becomes, here, **grade BLOCKED with the exact reason** and stop. A false PASS is worse than an
honest BLOCKED. When in doubt: read the real state (§2), and if you can't satisfy the precondition, name
it in `summary` and BLOCK.
