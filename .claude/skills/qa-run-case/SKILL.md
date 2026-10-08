---
name: qa-run-case
description: >-
  Run ONE structured QA case against a live environment of the target app and emit a machine-proposed
  verdict — the agentic tier of the control plane's case runner. Trigger for "run qa-run-case on <key>",
  "agentically run this case", or when the runner dispatches a single case. Reads the case body INLINE
  from CASE_FILE (never searches for it), loads a runner-minted auth bundle from AUTH_DIR (storageState +
  run.env, never secrets in the prompt), drives every step through the live app — @playwright/cli
  headless by default (DRIVER=cli) or claude-in-chrome (DRIVER=chrome) — reads the real state
  independently (never trusts a success toast), writes step-<n>.json progressively for the live stepper,
  and grades PASS|FAIL|PARTIAL|BLOCKED|MANUAL + orthogonal label-drift per contracts/qa-run-case.json.
  PROPOSES a verdict; a human disposes it at the verdict gate. Never asks a question (marks BLOCKED with
  the exact reason instead). Does NOT pick cases, sync the test-management system, generate specs, fix
  code, or write to the issue tracker.
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Agent, ToolSearch, mcp__claude-in-chrome__tabs_context_mcp, mcp__claude-in-chrome__tabs_create_mcp, mcp__claude-in-chrome__navigate, mcp__claude-in-chrome__find, mcp__claude-in-chrome__form_input, mcp__claude-in-chrome__read_page, mcp__claude-in-chrome__get_page_text, mcp__claude-in-chrome__javascript_tool, mcp__claude-in-chrome__read_console_messages, mcp__claude-in-chrome__read_network_requests, mcp__claude-in-chrome__computer, mcp__claude-in-chrome__browser_batch, mcp__claude-in-chrome__gif_creator
---

# qa-run-case — drive the case, grade it, record the run

qa-run-case is the **agentic tier** of the case runner: given one structured case it drives the live
target app step by step, reads the truth on the channel each assertion lives on, and emits a
**machine-proposed** verdict the human accepts or overrides at the verdict gate. It *explores* the app from
the case text step by step — there is no generated test script to replay, so any structured case can run
the moment it exists.

The grading brain (step classifier, verify-criteria precedence, mutation-prefix convention, label-drift
detection, grading rubric) is driver-independent and lives in `references/step-classification.md`; the two
driving disciplines live beside it. The framework's headless-operation rules are baked in
**skill-native** (see "Key principles") so no caller has to inject them via prompt. Target-app knowledge
is a pack the adopter supplies — see `references/app-knowledge.md`.

**Why it must be excellent:** a false PASS here is worse than an honest BLOCKED — it tells QA a broken
workflow works. The spine of this skill is one rule: **read the real state independently; never accept
the app's own success toast as truth.** And because this runs **headless under `claude -p`**, every place
an interactive runner would pause to ask a human must instead resolve to a written verdict — usually
`BLOCKED: <exact reason>` — never a hang.

## What this block does NOT do (atomic boundary)

It does **not** pick which case to run, sync or read the test-management system (`test_management`
adapter), generate a spec file, diagnose a failure, fix code, branch, or write to the issue tracker
(`issue_tracker` adapter). It does **not** finalize a verdict — it *proposes*; the human latch is the
control plane's verdict gate. It runs **one case per invocation** (principle 7). Keeping it to "drive one
case → grade it → record it → emit the receipt" is what makes it composable and safe to run in a fleet.

## Inputs / Outputs

**Inputs** (all arrive as `KEY=value` tokens in the trigger prompt — the runner builds it; secrets never
ride the prompt). Defaults come from `.claude/loops/stack.config.json` (read through
`lib/adapters/config.py`; any key overridable per process via `LOOPS_CFG__<PATH>`):
- `KEY` — the run key, format `case-<case_id>-run-<batch_id>` (unique per **batch**, not per dispatch).
  All artifacts live under `<EVID_ROOT>/<KEY>/`.
- `CASE_FILE` — absolute path to `<EVID_ROOT>/<KEY>/case.json`, written by the runner before spawn. This
  **is** the case. **Never** search a repo for a case file (a synced `TC-*.md`, a cases directory) — there
  is no extraction phase (principle 3). Shape:
  `{title, preconditions, steps_structured: [{n, action, expected}], expected_result, hints}`.
- `APP_URL` — the target environment base URL (e.g. `https://staging.example.com`). Defaults to
  `app.url`; a named environment's URL is `app.environments.<name>.url`. Health-checked; the skill does
  **not** start servers.
- `EVID_ROOT` — absolute evidence root; the run's owned, watched workspace (principle 4). Defaults to
  `evidence.root` (`runs/`). All writes go under `<EVID_ROOT>/<KEY>/` — never a scratch dir inside a repo.
- `AUTH_DIR` — absolute path to the runner-minted auth bundle dir, `<EVID_ROOT>/<KEY>/auth/`, containing
  `storage-state.json` (the `auth_provider` session as Playwright storageState) + `run.env` (env + optional
  pre-auth token). Secrets travel in these **0600 files**, never in the prompt (principle 5). See
  `references/auth-bundle.md`.
- `DRIVER` — `cli` (default) drives via `@playwright/cli` (`browser.oracle_driver`; headless, ~10×
  cheaper); `chrome` drives via the claude-in-chrome MCP (`browser.live_driver`; diagnostic). One skill,
  driver switched by this token. See §Drivers.
- `CHROME_DEVICE_ID` — **chrome driver only**, optional; pins the exact Chrome instance (deterministic,
  headless-safe). Never `switch_browser`/`AskUserQuestion` to disambiguate — both deadlock a headless run.
- `ALLOW_PROD` — when unset/`0`, the prod guard refuses any `APP_URL` whose host equals the host of
  `app.environments.prod.url` (grade BLOCKED, state the reason). Set `1` only to deliberately target prod.
  With no prod URL configured the guard has nothing to compare against and passes — say so in the notes.
- `KNOWLEDGE_DIR` — optional; a directory holding the adopter's app-knowledge pack
  (`references/app-knowledge.md` describes the format). Absent → verification by accessibility tree and
  reload + unfiltered DOM read, plus `conventions.json`'s `app_handles` (stable selectors) /
  `domain_glossary`.

**Output** — a **flat** evidence dir (no `TC-<id>/` nesting, no scratch subdir) the control plane polls
live and parses on completion:
```
<EVID_ROOT>/<KEY>/
  case.json                # the input (runner-written); do not modify
  auth/                    # the input auth bundle (runner-written); read-only, never log its contents
  transcript.md            # step-by-step "action / expected / observed / PASS|FAIL"
  before.png               # baseline screenshot
  step-<n>-<verb>.png      # per-step screenshots
  step-<n>.json            # LIVE-PROGRESS receipt, written IMMEDIATELY after each step completes
  drift.json               # label-drift entries (may be absent — best-effort)
  learnings.md             # OPTIONAL: suggested knowledge-pack updates discovered this run
  media/<KEY>-run.gif|.mp4 # OPTIONAL run recording (best-effort)
  case-run.json            # CONTRACT — the only REQUIRED artifact; the receipt the control plane reads
```
`case-run.json` is the **ratified contract** (`contracts/qa-run-case.json`) — the runner returns it
verbatim; the control plane branches on `status`, records the raw grade + drift, and proposes a verdict.
Emit these three required keys (+ optional `failed_steps`, `manual_steps`, `drift`, `media`, `notes`):
- `status ∈ { PASS | FAIL | PARTIAL | BLOCKED | MANUAL }` — the case grade (see "Grading" below).
- `summary` — one honest line describing the outcome (verbatim mismatch for FAIL/PARTIAL).
- `steps` — `[{n, status ∈ PASS|FAIL|INFO|BLOCKED, observation}]`, one row per executed step.

**Verdict semantics (what each grade means downstream).** The control plane maps the case grade to a
machine status + a proposed verdict a human then disposes:
- **PASS** → status `passed`, proposed verdict `pass`. Every `verify` step held; mutations confirmed by an
  independent read; no console errors. May still carry a `label-drift` flag.
- **FAIL** → status `failed`, proposed verdict `fail`. A `verify` step failed, or a click/type couldn't
  find its target after the alternate-phrasing retry. Describe the mismatch **verbatim**.
- **PARTIAL** → status `failed` + `PARTIAL:` marker, proposed verdict `needs_review`. The mutation
  succeeded but a side-assertion drifted (toast text wrong, count off by one, wrong artifact location).
  Always describe the mismatch verbatim.
- **BLOCKED** → status `failed` + `BLOCKED:` marker, proposed verdict `blocked`. A precondition could not
  be satisfied — auth bundle missing/expired, dependent data absent, env probe failed, prod guard tripped.
  **This is the headless escape hatch: whenever an interactive runner would have asked a human, grade
  BLOCKED with the exact reason instead.** `BLOCKED:` wins over `PARTIAL:`/`MANUAL:`.
- **MANUAL** → status `error` + `MANUAL:` marker, proposed verdict `needs_review`. The case needs human
  judgment — every step was `manual-check`, the case is template-only ("verify it feels responsive"), or a
  step asked for a subjective call. Screenshots captured, no automatic grade.

`label-drift` is **orthogonal** — a flag, not a status. A case with any `drift[]` entry is tagged for the
drift table regardless of its PASS/FAIL/PARTIAL/MANUAL grade; it is never counted against the status.

## Drivers

One skill folder, two drivers, selected by the `DRIVER` token (the control plane also selects which
runner entry to spawn — `qa-run-case` vs `qa-run-case-chrome` — so the CLI entry never loads chrome tools
and a headless run never demands a live Chrome):
- **`cli` (default)** — drive the app with `@playwright/cli` shell invocations (`--raw`, `-s=<session>`,
  `state-load`, `run-code --filename=`). Headless, token-cheap, no MCP. Self-installs if missing. Full
  discipline in **`references/playwright-cli-driving.md`**. When the environment sits behind an HTTP
  perimeter (`adapters.auth_provider.config.pre_auth = "header_token"`), the pre-auth header is injected
  on every request to `E2E_PERIMETER_ORIGIN` — see `references/auth-bundle.md`.
- **`chrome`** — drive a real Chrome tab via the claude-in-chrome MCP (`tabs_context → navigate →
  find/computer`). Diagnostic (a human can watch). Relies on the paired Chrome's existing session. Full
  discipline in **`references/chrome-driving.md`**.

Step classification, verify-criteria precedence, the mutation prefix, label-drift detection, and the
grading rubric are **driver-independent** and live in **`references/step-classification.md`**. App
knowledge (routes, widget-only artifacts, selector quirks, persistence traps) is the adopter's knowledge
pack — **`references/app-knowledge.md`** says what goes in it and how the run reads it. Read the pack
before deciding where to confirm a mutation.

## Procedure

### 0. Bootstrap, reach the app, load the auth bundle, confirm the session
Read `references/auth-bundle.md` and the driver's reference (`playwright-cli-driving.md` §0 for cli,
`chrome-driving.md` §0 for chrome). Then:
1. **Prod guard first.** Resolve the production host from `app.environments.prod.url`. If `APP_URL`'s
   host equals it and `ALLOW_PROD` is not `1`, **STOP**: write `case-run.json` with `status: BLOCKED`,
   `summary: "BLOCKED: prod guard — refused production URL <url> without ALLOW_PROD"`, and return.
2. **Load the auth bundle.** Require `<AUTH_DIR>/storage-state.json` and `<AUTH_DIR>/run.env`. Missing or
   empty → **BLOCKED** ("auth bundle missing at <path>"). Source `run.env` for `E2E_BASE_URL`,
   `E2E_API_URL`, `E2E_ENVIRONMENT`, and the optional pre-auth triple `E2E_PERIMETER_ORIGIN`,
   `E2E_PERIMETER_HEADER`, `E2E_PERIMETER_TOKEN`. **Never** echo the storage state or the pre-auth token
   to the transcript, a screenshot, or stdout.
3. **Health-check `APP_URL`** (`curl -sk -o /dev/null -w "%{http_code}" "$APP_URL"`). Unreachable →
   **BLOCKED** ("app unreachable at <url>: <code>").
4. **Establish the driven session** (driver-specific): cli → open a `-s=tester` session, `state-load` the
   storage state, install the pre-auth route interception scoped to `E2E_PERIMETER_ORIGIN` **before**
   navigating (skipped honestly when the triple is empty); chrome → `tabs_context_mcp {createIfEmpty:true}`,
   capture the tab id, navigate.
5. **Navigate to `APP_URL` and confirm the session** with a `before.png`. The landing page must render
   signed in: the `adapters.auth_provider.config.login.signed_in_landmark` selector resolves and the URL is
   not under `login.path`. If it bounced to the login page or a perimeter challenge, the bundle is
   stale/pre-auth failed → **BLOCKED** ("auth bundle did not authenticate — session bounced to <where>").

### 1. Read the case (inline — never locate it)
Read `CASE_FILE` (the runner wrote it). Use `title`, `preconditions`, `steps_structured[{n,action,
expected}]`, `expected_result`, and `hints` (e.g. `hints.selectors`) directly. Do **not** search the repo
for the case (principle 3). If `steps_structured` is empty (a "Text"/template-only case), grade
**MANUAL** up front with no browser execution.

### 2. Precondition gate → BLOCKED, never ask
From the case's `preconditions` and your session read, decide whether the case is runnable. If a required
precondition can't be satisfied (a role you don't have, data that must pre-exist and doesn't and can't be
created, a prerequisite case that hasn't run), grade **BLOCKED** with that exact reason and write
`case-run.json`, then stop. **Never `AskUserQuestion`** — this skill runs headless; the BLOCKED escape
hatch replaces every ask (principle 2). If the missing data is something a user could create, prefer
creating it (with the `E2E-{run-timestamp}-` mutation prefix) over declaring BLOCKED.

### 3. Drive the steps — classify, execute, record each one immediately
For each step in `steps_structured` (classify on demand — a malformed step must not block earlier ones):
1. **Classify** the `action` by leading keyword → `navigate | click | type | select | verify | wait |
   manual-check` (the table in `references/step-classification.md`). Prefix any created/typed named entity
   with `E2E-{run-timestamp}-`; strip it before evaluating a "verify the title is X" assertion.
2. **Execute** via the active driver. On a `click`/`select` miss, try exactly ONE alternate phrasing
   (`getByText`, alternate role, a `hints.selectors` entry) — if it hits, **record a label-drift entry**
   (verbatim expected label, observed label, locator, verb) and continue; do not fail the step.
3. **Verify** steps: build the pass criterion from the step's `expected` (precedence: text-presence →
   url-match → element-visible → count-matches → manual-check) and **read the real state** on the channel
   the assertion lives on — never a toast. Record PASS/FAIL with the **observed** value.
4. **Record the step IMMEDIATELY** — this is what the live stepper depends on. After each step, write
   `<EVID_ROOT>/<KEY>/step-<n>.json`:
   `{"n": <n>, "status": "PASS|FAIL|INFO|BLOCKED", "note": "<observed>", "screenshot": "step-<n>-<verb>.png"}`
   and, for non-`verify` steps, a `step-<n>-<verb>.png` screenshot beside it. Append the step's
   Action/Expected/Observed/verdict (and any `Drift:` line) to `transcript.md`.
5. **Within-case failure handling:** a single step failing does **not** abort the case — record it and
   continue the remaining steps (a later step may still surface useful signal). A lost session (cli
   session died / chrome "extension not connected") → re-establish once and retry the step; a second
   consecutive loss → grade **BLOCKED** ("driver lost after 2 attempts at step <n>"), don't grind.

### 4. Record the run (optional) + collect media
Best-effort, never required. If you captured a run GIF (cli: periodic screenshots stitched, or chrome:
`gif_creator`), convert to MP4 (`scripts/gif_to_mp4.sh`) and upload both with the shared helper
`.claude/skills/qa-repro/scripts/upload_evidence_media.sh <KEY> <file> gif|mp4 "<label>"`, collecting each
returned `{kind, url, label}` object into `media[]`. The helper uploads through the `evidence_store`
adapter (`local` by default → a `file://` URL under `<evidence.root>/<KEY>/uploads/`); **omit `media`
entirely** if the adapter is unavailable or the upload fails — the run still validates. Per-step
screenshots stay local (referenced by filename in the step files); the control plane serves them from
the evidence dir.

### 5. Grade → the contract status + drift
Apply the rubric (`references/step-classification.md` §Grading):
- **PASS** — all `verify` steps held, no console errors, mutations confirmed via an independent read.
- **FAIL** — any `verify` failed, or a click/type couldn't find its target after the retry.
- **PARTIAL** — mutation succeeded but a side-assertion drifted. Describe the mismatch verbatim.
- **BLOCKED** — a precondition couldn't be satisfied (the escape hatch; §2/§0).
- **MANUAL** — needs human judgment (all steps `manual-check`, template-only, or a subjective check).

Collect every label-drift entry into `drift.json` and the contract's `drift[]`. `failed_steps` =
the `n` of every FAIL step; `manual_steps` = the `n` of every `manual-check` step.

### 6. Capture learnings (optional, run-scoped — not an auto-edit)
If you discovered a new selector quirk, a step-phrasing the classifier missed, a 404, or a persistence
trap, append it to `<EVID_ROOT>/<KEY>/learnings.md` as a "Suggested knowledge-pack update" (which section,
the entry, and the evidence). **Do not auto-edit the versioned `references/` files or the adopter's
knowledge pack mid-run** — a headless run must not mutate a working tree; a maintainer folds durable
learnings in deliberately (the qa-repro "never hand-patch the skill mid-run" doctrine). This keeps the
learning-capture loop without the side effect.

### 7. Emit `case-run.json` and stop
Write the contract artifact (§Output) — the **only required** file. One case per invocation; return the
evidence dir. The recipe/runner takes it from here; the human disposes the verdict at the gate.

## Success criteria & failure categories (declared up front)

**Success:** a correct `status`; every executed step has a `step-<n>.json` + (non-verify) a screenshot;
the transcript is complete; FAIL/PARTIAL mismatches are verbatim; drift is flagged not folded into status;
and `case-run.json` validates against `contracts/qa-run-case.json`.

**Failure categories (classify before retrying — a blind re-run is a bug, not a retry):**
- `false-pass-via-toast` — graded PASS off the app's success signal without an independent read. **Never.**
- `wrong-surface` — read the wrong channel/page for the assertion. Re-check the app-knowledge pack.
- `missing-precondition` — → **BLOCKED** with the exact reason (or create the data if a user could).
- `auth-bundle-failed` — bundle missing/expired/pre-auth-not-injected → **BLOCKED**, name which.
- `driver-lost` — session/extension died twice → **BLOCKED**, don't grind.
- `unclassifiable-step` — → `manual-check` step; if every step is that, the case is **MANUAL**.

## Knowledge contract

The durable app knowledge is the adopter's knowledge pack (routes, widget-only artifacts, selector quirks,
persistence traps, step-parser gaps) plus `conventions.json`'s `app_handles` / `domain_glossary`. A **run**
never edits them — it writes run-scoped `learnings.md` (step 6) that a maintainer folds in. Never invent a
`/<guessed-route>` URL — consult the pack first (guessing a route 404s and wastes a turn); with no pack,
start from `/` and find the entry point in the rendered UI.

## Key principles (the headless-operation rules, skill-native)
1. **Read the real state; never trust the toast.** An independent read of the truth channel is the grade.
2. **Never ask — BLOCKED with the reason.** Headless has no human in the loop; every former ask is a
   written BLOCKED verdict, never a hang.
3. **The case travels inline; never locate it.** `CASE_FILE` is the case — no discovery.
4. **One owned evidence dir.** Everything the run writes lands under `<EVID_ROOT>/<KEY>/`.
5. **Secrets in files, never in prompts or transcripts.** The auth bundle is 0600; never echo it.
6. **Record each step the instant it finishes.** The live stepper is only as fresh as `step-<n>.json`.
7. **One case per spawn.** Bounded context, clean per-case attribution.
8. **Propose, never dispose.** The verdict is machine-proposed; the human latch is the gate.
