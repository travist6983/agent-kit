---
name: qa-diagnose
description: >-
  Diagnose ONE failed automated test run handed over by the control plane and PROPOSE a minimal fix as a
  unified diff — the propose-only diagnose lane of the QA-automation loop. Trigger for "diagnose this
  failed run", "diagnose diag-<record_id>", "why did case <case-id> fail", "propose a fix for this failed
  case", or as the runner skill behind the control plane's diagnose step. Reads the failure INLINE from
  CONTEXT_FILE (never searches for it), READS the failure screenshot FIRST, runs a two-agent swarm
  (Screenshot+Error analyzer over the 9-cause taxonomy + Source-code analyzer), synthesizes the root
  cause, and emits a MINIMAL unified diff as a STRING into diagnose.json per contracts/qa-diagnose.json.
  PROPOSE-ONLY: it NEVER applies the fix (it has no Edit tool — propose-only is structurally enforced).
  Classifies the fault as app_bug|test_bug|environment|data. Single pass, no re-run/iterate loop. Never
  asks a question (emits a diagnosis with diff:"" + notes instead). Diagnoses the generated-code tier
  (the materialized case file in the evidence dir); a run on any other tier — one whose target is a spec
  file inside a spec suite — is out of scope and gets diff:"" + notes. Reads app handles and the test-id
  convention from the conventions ruleset named in stack.config, never from memory. Does NOT apply/commit
  a fix, run the test harness, run/read/diff spec-suite files, pick which run to diagnose, or write to the
  issue tracker.
allowed-tools: Read, Write, Glob, Grep, Bash, Agent, ToolSearch
---

# qa-diagnose — diagnose the failure, propose the fix, never apply it

qa-diagnose is the **propose-only diagnose lane** of the QA-automation loop: given one **failed** run
it gathers the evidence, reasons over the root cause with an agent swarm, and emits a **machine-proposed
minimal fix as a unified diff** — a diagnosis a human / a developer reviews and decides whether to land.
It keeps the evidence discipline of a hands-on debugging session — the same 9-cause taxonomy, the same
failure→cause→fix tables — with the **one structural change that defines this lane**: it **never applies
a fix**. There is no `Edit` in its tool-list; its terminal action is to *render* the fix into
`diagnose.json.diff`, never to *do* it.

**Why it must be excellent:** a diagnosis is only as good as the evidence it stands on, and the single
hardest-won rule of UI-test debugging is the spine of this skill: **read the failure screenshot before
proposing anything.** A confident diff built on a guessed selector or an unread screenshot is worse than
an honest "inconclusive" — it sends a developer down a wrong path. And because this runs **headless under
`claude -p`**, every place an interactive session would pause to iterate or ask must instead resolve to a
single written diagnosis — `diff: ""` with a `notes` explanation when a fix can't be determined — never
a hang.

## What this block does NOT do (atomic boundary)

It does **not** apply, commit, or open a pull request for the fix (it *proposes* a diff — a human / a
developer decides). It does **not** re-run and iterate to green (nothing is applied, so there is nothing
to re-verify — a **single** diagnosis pass). It does **not** run the test harness (its fixtures and
environment live inside the control plane, out of reach by design), pick which run to diagnose, sync the
test-management system, or write to the issue tracker. It **never generates, re-runs, reads, or proposes
a diff to a spec-suite file** — a run on a spec-based tier gets an honest out-of-scope diagnosis (§1),
not a spec fix. It runs **one failed run per invocation**. Keeping it to "gather evidence → find the
root cause → propose the diff → emit the receipt" is what makes it composable and safe to run in a fleet.

## Configuration it reads

Every app-specific fact comes from config, never from memory. All paths are read through
`lib/adapters/config.py` (`cfg("…")`) against `.claude/loops/stack.config.json`:

| key | what this skill uses it for |
|---|---|
| `app.repo_dir` | the default target checkout when `REPO_DIR` is not passed |
| `app.url`, `app.environments.<env>.url` | the environments a `context.app_url` may legitimately point at; a read-only live probe only ever targets one of these |
| `browser.oracle_driver` | the browser test tool the case code drives (default `playwright-cli`); decides which locator-API spelling the taxonomy's tool-specific rows apply to |
| `stack.languages` | the language(s) of the product source Agent B reads |
| `stack.conventions_file` | path to the conventions ruleset (default `.claude/loops/conventions.json`) |

From the conventions ruleset the skill reads two maps:

- **`app_handles`** — the target app's handles for UI testing: the test-id attribute and naming pattern,
  where UI components live, the i18n catalog, overlay routes that do not survive a reload, interstitials
  that can block a page, two-step pickers, wrapper components that do not forward attributes, and the
  realtime-propagation timeout. The taxonomy names each handle it consults
  (`references/failure-taxonomy.md`, "App handles"). A handle that is absent falls back to the generic
  rule in the taxonomy; the skill never substitutes a remembered value.
- **`domain_glossary`** — the app's nouns, so `root_cause` and `notes` speak the adopter's language
  (the examples in this skill use the neutral domain: Workspaces, Projects, Tasks, Comments, Invoices,
  Members, Settings).

A missing or unparsable conventions file is **not** a stop: diagnose on the generic taxonomy and record
`conventions: unavailable` in `notes` so a reviewer knows the app handles were not consulted.

## Inputs / Outputs

**Inputs** (all arrive as `KEY=value` tokens in the trigger prompt — the runner builds it):
- `KEY` — the run key, format `diag-<record_id>`. `record_id` is the control plane's opaque run id. All
  artifacts live under `<EVID_ROOT>/<KEY>/`.
- `CONTEXT_FILE` — absolute path to `<EVID_ROOT>/<KEY>/context.json`, written by the runner **before
  spawn**. This **is** the failure to diagnose. **Never** search the repo for the run, the case code, or
  the screenshot — the context carries everything. Shape:
  ```json
  {
    "tier": "pytest",
    "repo_dir": "/abs/target-repo",
    "target": "case_code.py",
    "failure": {
      "error_summary": "…",
      "output_tail": "…",
      "failed_steps": [3],
      "screenshots": ["…"]
    },
    "app_url": "https://staging.example.com"
  }
  ```
  `tier` names the harness the case ran under; `target` is the materialized case file. This skill
  diagnoses the **generated-code tier**: `target` is a file the runner materialized into the evidence
  dir (`case_code.py` is the conventional name; the runner picks the extension for its harness). The
  control plane can still hand over a **spec-based** tier — `target` is a path into a spec suite — and
  that run is out of scope (§1). `app_url` is one of `app.environments.<env>.url` from stack.config.
- `REPO_DIR` — absolute path to the target repository checkout (also echoed in `context.repo_dir`;
  defaults to `app.repo_dir`). Anything read under here is **read-only** — never edit the working tree.
- `EVID_ROOT` — absolute evidence root; the run's owned workspace. All writes go under `<EVID_ROOT>/<KEY>/`
  — never `.scratch/` inside a repo. `EVID_ROOT` is **absolute** (the runner's cwd is this framework's
  checkout, not the target repo). When the runner does not pass it, use `evidence.root` from stack.config
  (default `runs`) resolved against this framework's checkout.

**Output** — a **flat** evidence dir the control plane parses on completion:
```
<EVID_ROOT>/<KEY>/
  context.json     # the input (runner-written); do not modify
  case_code.py     # the materialized generated case code (runner-written); read-only
  transcript.md    # OPTIONAL: evidence gathered / hypotheses / what the swarm found
  diagnose.json    # CONTRACT — the only REQUIRED artifact; the receipt the control plane reads
```
`diagnose.json` is the **ratified contract** (`contracts/qa-diagnose.json`) — the runner returns it
verbatim; the control plane stores the diagnosis against the run (**propose-only — no fix is
auto-applied**) and branches on `failure_layer` to route the finding. Emit these two required keys
(+ optional `summary`, `diff`, `confidence`, `notes`):
- `root_cause` (string) — the synthesized root cause in one honest paragraph: WHICH file/element and WHY,
  symptom vs. cause, grounded in the evidence actually read.
- `failure_layer ∈ { app_bug | test_bug | environment | data }` — where the fault lives (the routing key).
- `diff` (string, optional) — the PROPOSED minimal fix as a unified diff. **May be `""`** when no fix can
  be determined — in that case `notes` MUST say what a human should investigate.

## Procedure

### 1. Read the context (inline — never locate it)
Read `CONTEXT_FILE` (the runner wrote it). Use `tier`, `repo_dir`, `target`, `failure` (`error_summary`,
`output_tail`, `failed_steps`, `screenshots`), and `app_url` directly. Do **not** search the repo for the
run or its artifacts. If `CONTEXT_FILE` is missing/empty/unparseable, emit a diagnosis with `diff: ""`,
`failure_layer: environment`, and a `notes` line naming the missing input, then stop (the headless
give-up path — never ask).

**Spec-based tier → out of scope, stop.** If `target` is not a file materialized in `<EVID_ROOT>/<KEY>/`
— it is a path into a spec suite, and `tier` names a spec runner — this lane does not diagnose it: do
**not** re-run it, do **not** read the spec, and do **not** propose a diff to it. Emit `diagnose.json`
with `diff: ""`; a `root_cause` that says the run is on an unsupported spec-based tier and was not
diagnosed, quoting `failure.error_summary` so the reviewer still sees what failed; `failure_layer` as a
best-guess prior from the captured screenshot + `error_summary` alone (`test_bug` when they don't
discriminate); a low `confidence`; and `notes` saying the tier is unsupported, the prior is a guess, and
the case should be re-run on a supported tier (the agentic `qa-run-case` run, or the generated-code
tier) and that run diagnosed instead. Then stop.

### 2. Gather the evidence — screenshot READ FIRST
Before forming any hypothesis, gather **both** pieces of evidence:
1. **The failure screenshot** — for each path in `failure.screenshots`, **READ it with the Read tool.**
   This shows the actual page state at failure and is the **most important** piece of evidence (the #1
   rule). If `screenshots` is empty, proceed on code + error alone and lower confidence accordingly —
   nothing is re-run to produce one.
2. **Error output** — parse `failure.error_summary` + `failure.output_tail` for: which locator/assertion
   failed, expected vs. received, the failing line, and the call log (what it tried) when the output
   carries one.

### 3. Read the case code — never run the harness
The generated code is materialized by the control plane to `<EVID_ROOT>/<KEY>/<target>` (conventionally
`case_code.py`). Diagnose from that code + the failure evidence + (optionally) probing the live app
**READ-ONLY** at `context.app_url` — and only when that URL is one of the configured
`app.environments`; an unknown host is noted, never probed. **Do NOT try to run the test harness** — its
fixtures and environment live inside the control plane and are out of reach by design; attempting it
wastes turns and proves nothing. There is no reproduce step: diagnose from the captured evidence.

### 4. Diagnose with the agent swarm (over the 9-cause taxonomy)
Launch **two agents in parallel**, each reading the full taxonomy + failure→cause→fix tables from
**`references/failure-taxonomy.md`** and the `app_handles` map from the conventions file:

- **Agent A — Screenshot + Error analyzer.** Give it the full error output and the screenshot path (tell
  it to READ the file). It reasons over the **9-cause taxonomy** (element-not-found, hidden, strict-mode,
  disabled, wrong-page, timeout, shared-state, non-waiting visibility check, overlay route lost on reload
  — spelled out in the reference) to determine the root cause, which file(s) are implicated, and the
  exact change.
- **Agent B — Source-code analyzer.** Give it `context.target` and the failing line + one-line error. It
  reads the test/case source and the UI component(s) it interacts with (located via
  `app_handles.component_glob` when set, otherwise by searching `repo_dir` for the element's test id,
  text, or class) — finding the element by class/test-id/text, checking its conditional-render and
  visibility conditions in whatever form the UI framework expresses them, whether the component is
  rendered in multiple places, and form validation rules — and returns the component path, the element's
  actual markup, any hiding conditions, and what the test should do differently.

Synthesize both returns into a single root cause, distinguishing **symptom from cause**. Map the cause to
`failure_layer` per the reference's "cause → failure_layer" guidance (decide from the evidence, not the
cause number): the case code is wrong → `test_bug`; the product is wrong → `app_bug`; auth/infra/
timeout/route-perms → `environment`; a missing/wrong precondition entity or shared-state clash → `data`.

### 5. Propose the fix as a unified diff — never apply it
Synthesize the root cause into a **minimal fix**, then **render it as a unified diff string** into
`diagnose.json.diff` — *propose it, do not apply it*. Keep all of the reasoning (which file, what change,
the test-id convention from `app_handles.test_id_pattern`, the FIXES tables) — only the terminal verb is
*propose*, never *edit*:
- The diff MUST be a real unified diff: `diff --git a/<path> b/<path>`, `---`/`+++` headers, and `@@`
  hunk(s) with context lines. Scope it as tightly as the root cause allows — ideally one file, the fewest
  hunks. Paths are the materialized case file (`context.target`) for a `test_bug`, or repo-relative to
  the source you read for an `app_bug`. **Never a spec-suite file** — a spec-based fix is out of scope
  (§1). **Never a file matching `blast_radius_denylist_globs`** from the conventions file — a finding
  that lands there is described in `notes` with `diff: ""` and handed to a human.
- **Re-anchor against the current build.** Line numbers, test-id values, and component names are
  perishable (see the reference's caveats) — confirm the hunk's line numbers/selectors against the source
  you actually read this run. Never emit a hunk you did not ground in a real read.
- **Single pass — no re-run/iterate loop.** Nothing is applied, so there is nothing to re-verify. Do
  **not** loop back to "confirm the fix worked" — you can't (and shouldn't) apply it.

### 6. Stopping rule — when you can't determine a fix, give up honestly
Bounded effort: if after the swarm you cannot determine a fix (evidence too thin, screenshot missing and
the code alone doesn't discriminate, multiple layers equally plausible), **emit a valid diagnosis with
`diff: ""`** and put **exactly what a human must investigate** in `notes` (`failure_layer` is your
best-guess prior — say in `notes` that it's a guess, not a confident call). This is the headless give-up
path — it replaces every place an interactive session would have iterated or asked. **Never
`AskUserQuestion`** — this skill runs headless.

### 7. Emit `diagnose.json` and stop
Write the contract artifact (§Output) — the **only required** file. Optionally write a `transcript.md`
(evidence gathered, hypotheses, swarm findings) for a human reviewer. One run per invocation; return the
evidence dir. The control plane stores the diagnosis and routes on `failure_layer`; a human / a developer
decides whether to land the proposed diff.

## Worked example (neutral domain)

A run on `PROJ-123`'s case "Member can comment on a Task" fails at step 3 with
`getByTestId('task-comment-submit') — strict mode violation: resolved to 2 elements`. The screenshot
shows the Task detail page with the comment composer open **and** a second composer in the activity
side panel. Agent B finds `CommentComposer` rendered in both `TaskDetail` and `ActivityPanel`. Root
cause: the case locator is unscoped and the component is legitimately reused → `test_bug`, and the
proposed diff scopes the locator to the detail pane container in `case_code.py`. Had the side-panel
composer been a product regression (it is not supposed to render for `observer`), the same evidence
would have produced `app_bug` with the diff against the component — the evidence decides the layer, not
the cause number.

## Key principles
1. **Read the screenshot before proposing anything.** The failure screenshot is the truth of the page
   state — the #1 rule.
2. **Never guess selectors — read the source.** The Source-code analyzer reads the actual UI component /
   case code; a diff built on a guessed selector is a wrong diff.
3. **Propose, never apply.** The terminal action is a unified-diff string in `diagnose.json`, never an
   `Edit`. The skill has no `Edit` tool — propose-only is structural, not just prose.
4. **Single pass, no iterate loop.** Nothing is applied, so there is nothing to re-run. One diagnosis per
   spawn.
5. **Never ask — give up honestly with `diff: ""`.** Headless has no human in the loop; every former ask
   or iterate becomes a written diagnosis with a `notes` investigation list, never a hang.
6. **Track what you considered.** Note alternatives weighed and perishable caveats in `notes` so a
   reviewer sees the reasoning, not just the verdict.
7. **Don't run the test harness.** Its fixtures and environment are out of reach by design — diagnose
   from code + evidence + a read-only live probe.
8. **App facts come from config.** Test-id conventions, overlay routes, blocking interstitials, pickers
   and i18n live in `conventions.app_handles`; a handle that is not configured is a generic rule, never
   a remembered value from some other app.
