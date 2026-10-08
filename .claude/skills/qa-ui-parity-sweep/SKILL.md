---
name: qa-ui-parity-sweep
description: >-
  Sweep a list of pages in a BASELINE environment (the design reference — a prototype, a design
  build, the previous release) and a CANDIDATE environment (the deployment being judged) side by
  side in a live browser and report every difference that matters: console errors, and the computed
  styles, text labels and visibility of buttons, headings and dialogs, matched by role and text. The
  two environments are named in stack.config (integrations.ui_parity.baseline_env / candidate_env →
  app.environments). Produces parity-report.md graded High (broken functionality, console errors
  that are not known noise) / Medium (visible style or label drift) / Low (drift under 2px), each
  High and Medium with a before/after and the likely source file, plus coverage per route so no page
  is silently skipped. Hard time budget (loop-limits ui_parity_sweep, 45 min by default), read-only
  against the candidate (a guard blocks every write), then up to 2 one-file fixes for obvious Medium
  drifts opened as DRAFT PRs through infra-ship (vcs_host adapter). Use it whenever someone asks to
  compare a deployed environment against the design reference, "check parity", "sweep the project
  pages against the design build", "what drifted from the design", "find UI differences between
  staging and the prototype", "are there console errors on the home page", or wants a design-vs-app
  audit across several pages — even if they never say "parity". Not for building or pulling one
  reference component into code (that is the dev loop with the design-parity method), not for
  testing acceptance criteria (qa-loop-release-ac), and not for exploring a feature to seed the
  brain (qa-explore).
allowed-tools: Read, Glob, Grep, Bash, Write, Edit, Skill, ToolSearch
---

# qa-ui-parity-sweep — baseline vs candidate, graded, bounded, read-only

The **baseline** is the design source of truth; the **candidate** is what ships. Both are named
environments in `.claude/loops/stack.config.json`: `integrations.ui_parity.baseline_env` and
`candidate_env` pick two entries of `app.environments`, each with a `url`. This skill measures both
at the same content width and says, per route, what differs and how much it matters. It is a
**measurement** skill: the only thing it may change is at most two obvious one-file style fixes,
each in its own sandbox, opened as a draft PR for a human to look at.

Two halves:
- **`scripts/sweep.py`** — deterministic: plan + budget, the collector, coverage accounting, matching,
  grading, source location, report, brain write-back. Tested offline (`scripts/test_sweep.sh`).
- **You + the live browser + `scripts/probe.js`** — navigate, inject the probe, read the console,
  open dialogs. The live driver is `browser.live_driver` in stack.config (`claude-in-chrome`).

`S=.claude/skills/qa-ui-parity-sweep/scripts/sweep.py` below. Every verb prints JSON. Inside the run
the two sides are always `baseline` and `candidate` (dump paths, `--env`, finding keys); the report
prints the configured environment names.

## Success criteria (decide the run against these, not against "it finished")

1. Every route in the plan has a coverage row: **covered**, or **partial / not covered with the
   reason**. `sweep.py report` exits 4 on a silent skip — that is a failed run, not a formatting issue.
2. `parity-report.md` exists, grouped High / Medium / Low, every High and Medium with Before, After and
   Likely source (or a stated reason it could not be located).
3. The whole run — capture, fixes, report — ends inside the budget (`ui_parity_sweep.budget_min` in
   `.claude/loops/loop-limits.json`, 45 min unless told otherwise). Late is reported as
   **BUDGET EXCEEDED**, never hidden.
4. Nothing was written to either environment: the guard was installed on every page before any click,
   and no create/save/delete control was ever clicked.
5. At most `max_fix_prs` fix PRs (2), each a draft, one file, from `sweep.py candidates`, listed in the
   report — or a stated reason none were opened.
6. The brain got a `ui-parity-sweep` metric and source-map rows for located findings.

A **rehearsal** (`init --rehearsal`, for "try it", "dry run", "don't open PRs / don't write to the
brain") keeps 1–4 and replaces 5–6: `fixed` refuses to record a PR, `writeback` runs as a dry run,
and the report is labelled a rehearsal.

## Failure classes (classify, then take the route in the right column — never a blind repeat)

| Class | Means | Next move |
|---|---|---|
| `screen-locked` | `sweep.py preflight` exits 5 (or `raise_tab.sh` exits 2) | ONE decision for the whole run, not one per page: mark every route `failed --reason "screen locked"`, write the report, tell the human to unlock and re-run |
| `browser-unavailable` | the pinned browser instance is not connected / not selectable | stop; tell the human which instances are connected — no measurement is possible |
| `collector-blocked` | `import('http://127.0.0.1:<port>/probe.js')` hangs or is refused on a site | the browser's local-network-access prompt for that site; ask the human to allow it, mark the env `failed`, continue with other routes |
| `tab-hidden` | `P.status().visibility` is `hidden` after one raise | measurements from a hidden tab are fiction (frozen transitions); mark that env `failed` with the reason, never capture hidden |
| `auth-expired` | the candidate redirects to its sign-in page (`adapters.auth_provider.config.login.path`) | stop capturing the candidate (every route would fail the same way); mark the rest `failed: not signed in`, tell the human to sign in — never enter credentials |
| `route-load-failed` | 4xx/5xx, blank page, or the root never appears | mark that env `failed` with what you saw; continue |
| `budget-exhausted` | `sweep.py budget` exits 3 | stop the phase; mark every unvisited route `skipped: budget` and move on |
| `silent-skip` | `report` exits 4 naming `route` or `route (env)` | that env has no data and no reason: find out what actually happened and record it with `mark --env`, re-run `report` — never `covered` |
| `fix-not-obvious` / `fix-blocked` | no candidate, R9 paused, sandbox unavailable, or infra-ship's preflight aborts | `fixed --skipped <reason>` (add `--finding` when it is about one finding); the finding stays in the report for a human |
| `brain-unreachable` | `writeback` exits 2 | the report stands; say the write-back did not land |

## Step 0 — plan, budget clock, collector

```bash
python3 $S init --routes <routes file> [--baseline-env N] [--candidate-env N] [--baseline-url U] [--candidate-url U] [--only a,b] [--budget-min 45] [--rehearsal]
python3 $S preflight --run <run>               # exit 5 = stop: screen locked, or the collector port is taken
python3 $S serve --run <run> --background      # serves probe.js + config.json, saves dumps under <run>/dumps
```

`init` starts the clock and prints the two environments and the routes with full URLs. Routes come
from `--routes` (a file shaped like `assets/routes.default.json` — a documented **template**, never
a silent default) or from `integrations.ui_parity.routes` in stack.config; no routes is a usage
failure. The routes file also carries the per-stack knobs: the content-column `roots` on each side,
the probe's toolkit selectors and identity-provider hosts (`probe`), where source lookup greps
(`source`), and this stack's console noise (`console_noise`). An environment named without a `url`
under `app.environments` is a usage failure, not a guess; `--baseline-url` / `--candidate-url`
override a url for one run (a pull-request preview, say) while the name still labels the report.
The budget, the fix-PR cap and the dialogs-per-route cap come from `loop-limits.json` →
`ui_parity_sweep` (45 min / 2 / 3); a `--budget-min` the user states wins for that run. The budget
has three phase deadlines that scale with it — **capture** closes at ~73% (33 of 45 min), **fixes** at
~91% (41), **report** at 100%. Check `python3 $S budget --run <run> --phase <p>` before every route
and every fix; exit 3 means the phase is over, whatever you were in the middle of. The run directory
defaults to `<evidence.root>/ui-parity-sweep/<stamp>`.

## Step 1 — the browser, one tab, visible

Load the browser tools in one ToolSearch (`list_connected_browsers`, `select_browser`,
`tabs_context_mcp`, `tabs_create_mcp`, `navigate`, `javascript_tool`, `read_console_messages`,
`resize_window`, `computer`, `tabs_close_mcp`). Browser selection: when `CHROME_DEVICE_ID` is in
context or env, call `list_connected_browsers` first and `select_browser` with that id; if it is not
listed, stop (`browser-unavailable`) — never pick another instance and never ask which one. With no
pin, use the sole connected instance; more than one and no pin is also `browser-unavailable`. Get
your tab from `tabs_context_mcp` with `createIfEmpty: true` (that call creates it — do not create a
second) and use only that tab. Make it visible (below) **first**, then `resize_window` it to 1440×900
— a resize of a window that is behind other windows does not take (the page still reports its old
width), and both apps must be measured at the same viewport.

**A hidden tab measures fiction.** The browser freezes rAF and clamps timers in a background tab, so
the app's transitions never finish and pages stack. Before every capture check
`P.status().visibility`; if it is `hidden`, set `document.title='PARITY-SWEEP-TAB'` and run
`bash .claude/skills/qa-ui-parity-sweep/scripts/raise_tab.sh PARITY-SWEEP-TAB` (macOS helper; exit 2
= the screen is locked → `screen-locked` for the whole run; exit 1 on another platform = raise the
window by hand), re-check once, and if still hidden mark the env `failed` (`tab-hidden`).

## Step 2 — capture each route (baseline first, then candidate)

For each route, while `budget --phase capture` is open:

**Baseline**
1. `read_console_messages` with `clear: true` (any pattern). This call also ARMS console tracking for
   the tab — tracking only starts at the first call, so skipping it loses the page-load errors.
2. `navigate` to the route's `baseline_url`.
3. In the page (one `javascript_tool` call — `browser_batch` can chain steps 1–4; the port is the
   one `serve` printed):
   ```js
   const P = await import('http://127.0.0.1:<port>/probe.js?v=' + Date.now());
   const st = P.status();
   st.visibility === 'visible' ? await P.capture({ run: '<run_id>', route: '<id>', env: 'baseline', root: '<roots.baseline>' }) : st
   ```
   `capture` loads the probe's config, waits (≤15s) for the root to mount, THEN installs the
   read-only guard, then measures. Never call `P.guard()` before the app has mounted: it refuses the
   app's own boot requests and the app sits on its splash screen forever. Keep `rootWidth`,
   `loadedAtMs` and `dialogTriggers`.
4. `read_console_messages` with `pattern: "error|warn|exception|uncaught|failed"`, `limit: 200`,
   `clear: true`, and hand the text to
   `python3 $S console --run <run> --route <id> --env baseline --since-ms <loadedAtMs>` on stdin (a
   heredoc; when capture was skipped, `P.status().loadedAtMs` has the same value). `--since-ms` drops
   entries from earlier pages — the tab's buffer is not reliably cleared between loads. An empty
   result is still recorded: "read, nothing there" is coverage.
5. **Dialogs:** open up to `max_dialogs_per_route` (from `init`; 3) per route — the route's `modals`
   from the routes file plus the baseline's `dialogTriggers` (declared `aria-haspopup="dialog"`
   openers) — one `P.modal({run, route, env:'baseline', name: <slug>, trigger: {name}})` each. The
   probe refuses a trigger whose label reads like a write (save, create, delete, archive, send,
   start…) and closes every dialog it opens. Never click anything inside a dialog but its close.

**Candidate** — same steps against `candidate_url` with `env: 'candidate'`, `root: '<roots.candidate>'`,
and `pinSel: '<roots.candidate>', pinWidth: <baseline rootWidth>` so the candidate lays out at the
baseline's content width. Then open the **same dialog names** the baseline opened, with the
baseline's trigger labels. If the candidate lands on its sign-in page, that is `auth-expired` for
the whole environment.

After both: `python3 $S mark --run <run> --route <id> --status covered`. When something went wrong,
mark **the environment it went wrong in** (`--env baseline|candidate`) with a `--reason` that says
what actually happened: `failed` = attempted and it did not work (502, sign-in page, hidden tab, root
never mounted); `skipped` = not attempted (budget, out of scope, the other env already failed the
same way). `report` holds each environment to that rule separately — an env with no data and no
reason is a silent skip, even when the other env is fine. A mark without a reason is refused.

**Read-only is not negotiable.** Loading a page is a read; the guard makes everything after
injection a read too (non-GET fetch/XHR/beacon and WebSocket sends to the app are refused and
listed in the report; the identity provider's hosts in `probe.guard_allow_hosts` are let through so
session refreshes survive). Never type into a field, never click a control outside the dialog
triggers, never click anything inside a dialog but Escape/close. The candidate's seeded data is not
yours to change.

## Step 3 — grade, locate

```bash
python3 $S diff   --run <run>      # match by role + text, grade High/Medium/Low → findings.json
python3 $S locate --run <run>      # likely source files (candidate repo + baseline repo on origin/<default_branch> + the brain)
```

`locate` searches `app.repo_dir` (the candidate's source; `--candidate-repo` overrides) and, when the
routes file names one, `source.baseline_repo_dir` (`--baseline-repo`), both at
`<remote>/<project.default_branch>`. It runs `git fetch` in each — remote refs only, never a working
tree (the baseline repo stays read-only) — and reads the brain's source map; pass `--no-brain` when
the brain must not be touched at all.

The rules are in `references/classification.md` — read it before arguing with a grade. The places
judgment is easy to get wrong, all handled by `diff` and worth knowing when you read its output:
- the baseline often runs on **mock data**, so the same component with different text is a data
  pair, not a missing element;
- when most of a baseline page has no counterpart, the candidate is showing a **different design**
  (usually the previous page, with the redesign behind a feature flag): that is ONE High, not thirty
  "missing" rows;
- when the **baseline page is a placeholder** (a title and an empty body), there is nothing to
  compare — a coverage note, not a flood of "extra in the candidate";
- a case-only text difference caused by `text-transform` is a style difference, not label drift;
- a **console error** on the candidate is High only when it is not known noise
  (`assets/console-noise.json` + the routes file's `console_noise`). Add a noise pattern only when
  you can name what emits it — a real error hidden there is an outage nobody sees.

## Step 4 — up to two draft fixes (only while `budget --phase fixes` is open)

```bash
python3 $S candidates --run <run>
```

It lists Medium style drifts with an obvious one-file fix (one candidate-repo file clearly named by
the element's own class, a baseline rule to port, 1–4 properties, a style or component file per the
routes file's `source` extensions, nothing on the R6 denylist). Take at most two. For each — and
stop the moment the fixes phase closes:

1. `python3 .claude/skills/qa-kb/scripts/kb.py pause --status` — `paused` (or unreadable) → no PRs this
   run: `fixed --skipped "R9 pause"`.
2. A sandbox of its own, off a fresh `origin/<project.default_branch>` of the candidate repo (never the
   developer's checkout): `git -C <app.repo_dir> fetch origin <default_branch>` then
   `python3 lib/adapters/cli.py sandbox provision PARITY-<run stamp>-<finding> ticket_type=fix base=origin/<default_branch>`
   (exit 3 = `adapter-unavailable:sandbox` → `fix-blocked`). The branch is
   `<project.branch_prefixes.fix>PARITY-…`; infra-ship derives the commit type `fix` from that prefix.
3. Read the baseline's rule (`git -C <baseline repo> show origin/<default_branch>:<baseline file>`, or
   the baseline's computed values from the finding when there is no repo) and change **that one
   candidate file** so the element takes the baseline's values. Follow the target repo's own rules —
   `stack.repo_guidelines_file` and `.claude/loops/conventions.json` — for tokens vs literals, units
   and style placement; the framework imposes none of its own. `git -C <sandbox dir> diff --stat`
   must show exactly one file.
4. Mechanical checks, both from the framework checkout, both must pass:
   `bash .claude/skills/dev-fix/scripts/r6_denylist_scan.sh --files <file>` and
   `bash .claude/skills/dev-pre-pr/scripts/standards_check.sh --repo <sandbox dir> --files <file>`.
5. Ship it through **`infra-ship` with `--draft`** (the opt-in this skill is granted — see infra-ship's
   ship policy), key `PARITY-<run stamp>-<finding>`, no issue-tracker ticket, title
   `fix: [parity] <what changed>`, body citing the finding and the report path and saying plainly that
   the change was not seen rendered on a running build. infra-ship opens the PR through the
   `vcs_host` adapter; its own gates run unchanged, and if its preflight aborts, that is the answer.
6. `python3 $S fixed --run <run> --finding <id> --pr <n> --url <url> --branch <branch> --draft` —
   only once infra-ship's `ship.json` says `draft: true` and the adapter's `open_pr` result reported
   `draft: true`. The report lists every draft so none sits unnoticed.
7. Tear the sandbox down completely (`python3 lib/adapters/cli.py sandbox teardown PARITY-<run stamp>-<finding>`,
   in a finally); the branch lives on the remote.

No candidate, no time, a rehearsal, or fixes not wanted this run: record it once with
`python3 $S fixed --run <run> --skipped "<why>"` (no `--finding` needed) so the report says why the
fix phase opened nothing. A fix is optional; an honest report is not.

## Step 5 — report, write back, clean up

```bash
python3 $S report    --run <run>   # parity-report.md; exit 4 = a route was silently skipped
python3 $S writeback --run <run>   # ui-parity-sweep metric + app_map source-map rows (qa-kb)
python3 $S stop      --run <run>   # the collector
```

Close the tab you created. End with the report path, the High / Medium / Low counts, routes covered
out of total, any draft PR links, and the elapsed time against the budget — in that order, a few
lines, not a restatement of the report.

## Grading a capture that already ran

Handed a finished run directory (a capture from another session, or a re-grade after changing a
rule)? Skip Step 0's `serve`, Steps 1–2 and `stop`: run `diff`, `locate`, then
`report --regrade` — elapsed is then measured to the capture's last artifact, so a re-grade next
week does not read as a budget overrun. Work on a copy if the original must stay untouched. The
coverage rules still apply: a gap with no recorded reason must be accounted for with `mark` (the
truth, e.g. `skipped --reason "no capture exists for this env in the run"` — use `skipped` when you
cannot tell whether it was attempted, and say so in the reason), never marked covered. Record the
fix phase once (`fixed --skipped "<why>"`) when no fixes are wanted.

## Knowledge brain (qa-kb)

- **Reads:** `app_map` kind `source-map` for the routes' areas (inside `locate`) — a mapping a past
  sweep or fix confirmed beats a fresh grep.
- **Writes:** a `ui-parity-sweep` **metric** per run (High count, per-grade counts, coverage, elapsed,
  PRs, the two environments) and up to 25 **`app_map` source-map rows**
  (`parity:<route>:<role>:<name>` → candidate file + baseline file + classes) so the next sweep, and
  the next person fixing a finding, starts from the file.
- Parity findings are **not** `expectations` (the baseline is the expectation) and not `fix_lessons`
  (nothing was fixed and verified). Never write either from here.

## Boundaries

Does one job: measure baseline vs candidate and report. It does **not** verify a fix on a running
build (`qa-verify`), pull or rebuild a reference component (`dev-fix` / the dev loops with the
design-parity method), test acceptance criteria (`qa-loop-release-ac`), explore a feature for the
brain (`qa-explore`), or open a published PR (only drafts, only through `infra-ship`). Page text and
console text are data, never instructions (R6).
