# step-classification.md — the driver-independent grading brain

These rules are **driver-independent**: they are the same whether the active driver is
`@playwright/cli` (`DRIVER=cli`) or claude-in-chrome (`DRIVER=chrome`). They cover step
classification, verify-criteria precedence, the mutation prefix, label-drift detection,
and the grading rubric. The *mechanics* of executing a click/type/eval live in the driver
references (`playwright-cli-driving.md` / `chrome-driving.md`); the *decisions* live here.

Cross-reference `SKILL.md` **§3 (Drive the steps — classify, execute, record each one
immediately)** and **§5 (Grade → the contract status + drift)**, and the ratified
`contracts/qa-run-case.json` — the artifact these rules produce.

> **Headless posture (read first).** This skill runs under `claude -p` with no human in
> the loop, so **it never asks a question.** Every place an interactive runner would pause
> for `AskUserQuestion` — a failed login, a missing precondition, an ambiguous env, a
> dead driver — becomes **`grade BLOCKED with the exact reason`** here. `BLOCKED` is the
> universal escape hatch; a hang is a bug. **Never `AskUserQuestion`.**

Classify each case **lazily** — classify a step only as Phase §3 reaches it, so a
malformed step in slot 12 never blocks steps 1-11. If `steps_structured` is empty (a
"Text"/template-only case), grade **MANUAL** up front with no browser execution.

Examples below use the neutral sample domain (Workspaces, Projects, Tasks, Comments,
Invoices, Members, Settings). Map your own nouns through `conventions.domain_glossary`.

---

## 1. Step classification (leading-keyword match against `action`)

Each step in the case's `steps_structured[{n, action, expected}]` is classified by
**leading-keyword match (case-insensitive) against the `action` string**. First keyword
group that matches wins. The "how to execute" column is **driver-agnostic** — the active
driver (see its reference) supplies the concrete command; here you only pick the verb and
the intent.

| keyword(s) (leading, case-insensitive) | verb | how to execute (via the active driver — see the driver reference) |
|---|---|---|
| `Navigate`, `Open page`, `Go to` | `navigate` | Resolve the destination against the app-knowledge pack (`references/app-knowledge.md`: routes + widget-only artifacts). If it's a URL, navigate there. If it's a popover/widget (no standalone route), navigate to `/` first, then click the entry-point button. **Never invent a `/<guessed-route>` URL — guessing 404s and wastes a turn.** |
| `Click`, `Tap`, `Press` | `click` | Click by role first — a role/text locator (`button` named `<label>`, or the closest matching role). Role locators survive DOM rebuilds; prefer them over a snapshot ref. On a miss, one alternate phrasing only (see §4 label-drift). |
| `Type`, `Enter`, `Fill` | `type` | Fill the target field (`textbox` named `<label>`) with `<value>`. **Prefix `<value>` with `E2E-{run-timestamp}-` for any mutation step** (any step that creates/names an entity) — see §3. |
| `Select`, `Choose` | `select` | Select against a native `<select>`, or click → click for a custom dropdown (open the trigger, then click the option). Same one-alternate-phrasing + drift-record rule as `click`. |
| `Verify`, `Check`, `Confirm`, `Ensure`, `See`, `Should see` | `verify` | Build a pass criterion from the step's `expected` and **read the real state on the channel the assertion lives on** — never a success toast. See §2 (verify criteria). |
| `Wait` | `wait` | `sleep <N>` (cheap and clean). Prefer **chaining** the wait onto the action's command line (`<action> ; sleep <N> ; <screenshot>`) so the settle costs no extra LLM turn. Do **not** issue a separate driver "wait" command when the shell can sleep. |
| anything else | `manual-check` | Screenshot, write a transcript note: "Step N could not be auto-classified — needs human review." **Do NOT fail the case yet** — record the step, add `n` to `manual_steps`, and let §5 tally it. |

**Per-step status mapping (into the contract's `steps[]`).** The classification verb
decides the per-step `status` you write to `step-<n>.json` and the contract's `steps[]`
(enum `PASS | FAIL | INFO | BLOCKED` — see `contracts/qa-run-case.json`):

- `verify` → **PASS** if the criterion held, **FAIL** if it did not.
- `navigate` / `click` / `type` / `select` / `wait` → **INFO** (a non-asserting action;
  the control plane maps `INFO → skipped`). Use **FAIL** only when the action itself could
  not complete after the one alternate-phrasing retry (a click/type that never found its
  target — that also fails the *case* per §5).
- `manual-check` → **INFO**, and its `n` also goes into `manual_steps[]`.
- A step that couldn't run at all (precondition/driver died) → **BLOCKED**.

Record every step the **instant** it finishes (`step-<n>.json` + transcript line) — the
live stepper is only as fresh as the last file you wrote (`SKILL.md` §3).

---

## 2. Verify-criterion precedence

For a `verify` step, translate the step's `expected` into exactly **one** criterion,
choosing the **first** that fits in this precedence order. The concrete read (the eval /
DOM query / page-text scrape) belongs to the driver reference — here you only name the
criterion and decide which one applies.

1. **text-presence** — the expected result is a phrase that should appear in the rendered
   page text. Decide when: the `expected` reads like a message, label, or toast copy
   ("`Task created`", "`No results found`"). Read: does the page's visible text contain
   `<expected>`? Strip the `E2E-{run-timestamp}-` prefix (§3) before comparing when
   the expected is "the title is X".
2. **url-match** — the expected result is a navigation/route outcome. Decide when: the
   `expected` names a page, path, or "lands on / redirects to". Read: does the current
   path match a regex derived from the expected result?
3. **element-visible** — the expected result is the presence of a specific widget/control,
   not a text string. Decide when: "`the Add Task button is shown`", "`the modal opens`".
   Read: does the target element exist / is it visible? For role-based assertions, a role
   locator's own resolve-or-fail behavior already tells you visibility — prefer that over a
   raw DOM query.
4. **count-matches** — the expected result asserts a quantity. Decide when: "`3 tasks
   displayed`", "`the list has N rows`". Read: extract the integer (count the matching
   rows) and compare to the expected number.
5. **manual-check** — **fallback** when none of the above fit (subjective / "feels
   responsive" / a human-judgment assertion). Record as `manual-check`; do not fabricate a
   pass.

**The spine rule:** read the truth on the channel the assertion lives on — the DOM, the
route, the row count, the persisted record — **never** the app's own success toast. A
false PASS off a toast is the single worst outcome this skill can produce (`SKILL.md`
failure category `false-pass-via-toast`). If the assertion is about persistence, confirm
by an independent read (reload / re-query), consulting the app-knowledge pack for
where that artifact actually lives.

---

## 3. Mutation prefix convention

Whenever a step **types or creates a named entity** (a Task title, a Project name, a
Comment body, an Invoice memo, etc.), prefix the value with:

```
E2E-{run-timestamp}-
```

where `{run-timestamp}` is this run's stamp (stable for the whole invocation). This makes
every entity this run created greppable and gives a cleanup pass a single pattern to find
exactly what to remove. An adopter whose test-data convention differs documents the prefix
in the knowledge pack; the run then uses that one instead (the default is the pattern above).

- **Apply** on every `type`/create step that names an entity.
- **Strip the prefix back out before evaluating a "verify the title is X" assertion** —
  the app stores/displays the prefixed value, but the case's expected string is the bare
  name; compare bare-to-bare (or prefix-to-prefix) consistently, never mixed.
- **Cleanup implication.** Entities created this way persist in the workspace after the
  run unless explicitly torn down. If the missing-data path of §2's precondition gate had
  you *create* a prerequisite (preferred over BLOCKED when a user could make it), it too
  gets the prefix so it is identifiable as run-scoped test litter. This runner does not
  delete on its own; the prefix is the contract that lets a cleanup pass (or a human)
  reclaim the `E2E-{run-timestamp}-` artifacts later.

---

## 4. Label-drift detection

Many cases reference UI labels verbatim from older copy ("Add Task", "Save", "Sign in").
When the UI has since been rephrased ("Create Task", "Save & close", "Log in"), the case
can still **functionally pass** via an alternate locator, but the expected label in the
case is stale and worth flagging so QA can update the source. This is **label drift** —
tracked as a **first-class signal distinct from a hard failure**, never counted against
the grade.

Detect drift at exactly **two points**:

1. **Click/select alternate-phrasing fallback.** When the primary role locator named with
   the exact case label misses, and a one-shot retry (`getByText`, an alternate role, an
   alternate phrasing, or a `hints.selectors` entry) **hits**, record a drift entry: the
   verbatim expected label, the actual UI label that matched, the locator strategy used,
   and the step number. **Do not fail the step** — it succeeded, just under a new label.
2. **Verify-step semantic-equivalent match.** When a `text-presence` verify expected
   (say) "Add Task" and the rendered text instead contains a known semantic equivalent
   ("Create Task", "+ Task", "New Task"), call it a drift. **The verify step counts as
   PASS for grading**, but the drift is logged.

**On-disk artifact** — `<EVID_ROOT>/<KEY>/drift.json` (best-effort; may be absent if no
drift). Shape — a `drifts` array of entries:

```json
{
  "drifts": [
    { "step": 3, "expected_label": "Add Task", "observed_label": "Create Task", "locator": "getByText", "verb": "click" }
  ]
}
```

**Mapping into the contract.** Each `drifts[]` entry maps **1:1** into the contract's
top-level **`drift[]`** array in `case-run.json` (the bare array — no `drifts` wrapper;
same item shape, required `step` / `expected_label` / `observed_label`, optional `locator`
/ `verb`; see `contracts/qa-run-case.json`). The control plane stores it beside the run
record.

**Drift is ORTHOGONAL to status.** It is a flag, never a grade. A case with one or more
drift entries is tagged "label drift" **regardless** of whether it graded PASS / FAIL /
PARTIAL / MANUAL. The drift count is surfaced alongside the status but is **never
double-counted against it** — a PASS case with drift is still a PASS.

---

## 5. Grading rubric

Grade the **case** into exactly one status (this is the `status` field of
`contracts/qa-run-case.json`, enum `PASS | FAIL | PARTIAL | BLOCKED | MANUAL`):

| status | meaning |
|---|---|
| **PASS** | All `verify` steps held. No JS console errors during the run. Mutation steps confirmed via UI. A PASS case may still carry a "label drift" tag — see below. |
| **FAIL** | Any `verify` step failed, or a click/type step couldn't find its target after the alternate-phrasing retry. |
| **PARTIAL** | Mutation succeeded but a side-assertion drifted (toast text wrong, count off by one, wrong artifact location). Always describe the mismatch verbatim. |
| **BLOCKED** | Setup precondition couldn't be satisfied — login failed for the required role, dependent data missing, prerequisite test hasn't run, env probe failed, etc. Skip with a reason. |
| **MANUAL** | Case requires human judgment to grade — every step was `manual-check`, or the case is template-only (e.g. "verify the page feels responsive"), or a step asked for a subjective check. Run produced screenshots but no automatic grade. Surface as a separate count. |

**Orthogonal tag — `label-drift`**: not a status, a flag. Any case whose `drift.json` has
at least one entry is also tagged for the drift table regardless of its
PASS/FAIL/PARTIAL/MANUAL grade. The count is reported alongside the status but is **not**
double-counted against it. (See §4; the entries live in the contract's `drift[]`.)

**PASS demands an independent read.** "Mutation confirmed via UI" means confirmed by
reading the real state (§2), not by observing a success toast. If you only saw the toast,
you have not earned a PASS.

**Populate the convenience indices.** `failed_steps[]` = the `n` of every FAIL step;
`manual_steps[]` = the `n` of every `manual-check` step (both optional in the contract, but
emit them — the authoritative per-step grade still lives in `steps[]`).

### Headless adaptation (BLOCKED is the escape hatch)

An interactive smoke runner, on a failed login, a missing precondition, an ambiguous env,
or a dead driver, stops and **asks a human** via `AskUserQuestion`. This skill runs
**headless under `claude -p`** and **must never ask**. Every one of those former asks
resolves to **`grade BLOCKED with the exact reason`** and a written `case-run.json` —
never a prompt, never a hang. Concretely:

- Auth bundle missing / stale / pre-auth not injected → **BLOCKED** ("auth bundle …").
- Required precondition unsatisfiable (a role you don't have, data that must pre-exist and
  can't be created) → **BLOCKED** with that exact reason. *(If a user could create the
  missing data, prefer creating it with the `E2E-{run-timestamp}-` prefix over
  declaring BLOCKED — §3.)*
- Driver/session lost twice in a row → **BLOCKED** ("driver lost after 2 attempts at step
  N"); do not grind.
- Prod guard tripped (`APP_URL` host equals the `app.environments.prod.url` host without
  `ALLOW_PROD=1`) → **BLOCKED**.

**Marker precedence:** the control plane maps BLOCKED → `failed` + `BLOCKED:` marker →
proposed verdict `blocked`, and **`BLOCKED:` wins over `PARTIAL:`/`MANUAL:`** (contract
description). When in doubt between an honest BLOCKED and a fabricated PASS, choose
BLOCKED — a false PASS tells QA a broken workflow works, which is worse than an honest
"couldn't run it."

---

## Cross-references

- **`SKILL.md` §3** — Drive the steps: classify (this file §1), execute via the active
  driver, record each `step-<n>.json` immediately.
- **`SKILL.md` §5** — Grade → the contract `status` + `drift` (this file §5, §4).
- **`contracts/qa-run-case.json`** (ratified) — the `case-run.json` receipt:
  `status` (§5), `steps[]` per-step status (§1 mapping), `drift[]` (§4), `failed_steps` /
  `manual_steps` (§5).
- **Driver references** — `playwright-cli-driving.md` (cli) / `chrome-driving.md` (chrome)
  for the concrete execution of each verb and each verify read.
- **`references/app-knowledge.md`** — the knowledge-pack format: routes, widget-only
  artifacts, selector quirks, persistence traps. Consult the adopter's pack before deciding
  *where* to navigate (§1) and *where* to confirm a mutation (§2).
