# How qa-ui-parity-sweep matches and grades

`sweep.py diff` implements everything here. It is written down so a reader of `parity-report.md`
can check a grade against a rule, and so a change to a rule is a deliberate edit to both. The two
sides are the **baseline** (the design reference) and the **candidate** (the deployment being
judged); the report prints the configured environment names in their place.

## What is compared

Per route, per environment, the probe records every **button-like control** (`button`,
`[role=button]`, `input[type=button|submit|reset]`, `[role=menuitem|tab|switch]`, plus the toolkit
selectors in the routes file's `probe.buttons_extra`) and every **heading** (`h1`–`h6`,
`[role=heading]`) under the content column, plus every **dialog** (`[role=dialog]`,
`[role=alertdialog]`, `dialog[open]`, plus `probe.dialogs_extra`) that is open at load or that a
trigger opened. For each: role, accessible name (aria-labelledby → aria-label → text → title),
visible text, visibility (display/visibility/opacity/size), disabled, box, icon, the class chain
(own + 4 ancestors, utility classes dropped), and ~45 visible-style properties (colour, background,
borders, radius, shadow, outline, font family/size/weight/style, line-height, letter-spacing,
transform, decoration, alignment, padding, margin, gap, cursor). Colours are normalised to `rgb()`
so `oklch(...)` on one side and a hex on the other compare by value.

The app shell (sidebars, top bar) is out of scope: only the content column is dumped, and the
candidate's column is pinned to the baseline column's measured width so boxes compare.

## Matching (by role and text, in this order)

1. **exact** — same role group (control / heading / dialog) and identical accessible name.
2. **loose** — same role group, names equal after lower-casing and dropping punctuation/spaces
   (`Task List` ↔ `Task list`). The pair is compared; the text difference is a label-drift finding.
3. **near** — same role group, name similarity ≥ 0.75 and within 48px vertically.
4. **data** — same structural signature (role + tag + own classes + parent classes) but different
   names: the same component showing different DATA (a record's title, a person). Compared for style
   only; never reported missing or extra. A baseline typically runs on mock data and the candidate
   on real data, so without this every row of every list would be a false "missing".

Whatever is left is **baseline-only** (missing in the candidate) or **candidate-only** (extra in the
candidate). Icon-only controls with no accessible name match on their icon identity; a control with
neither a name nor an icon identity ("[button]") never matches by text — only structurally (pass 4).
The probe takes a name from a labelled wrapper within two levels when the control itself has none
(a kebab menu whose `aria-label` sits on the wrapping div).

## Page-level rules (applied before per-element grading)

- **Different design** — the baseline shows ≥ 8 elements and < 40% of them have a counterpart: the
  candidate is showing another design (typically the previous page with the redesign behind a
  feature flag). ONE High, listing what is missing and what the candidate shows instead; no
  per-element missing/extra rows. Matched elements are still compared for style.
- **Baseline placeholder** — the baseline shows < 8 elements while the candidate shows ≥ 15 and
  ≥ 3× as many: the baseline has no design for this page yet. A coverage note; the candidate's
  elements are not graded as extra.
- **Hidden tab** — a dump taken while the tab was hidden is not evidence (frozen transitions and
  timers): no style comparison for that route, coverage `partial` with the reason.

## Grades

| Grade | Rule |
|---|---|
| **High** | A control the baseline shows is **missing**, **hidden** or **disabled** in the candidate (the design's action is not available — broken functionality). A dialog that **opens in the baseline and not in the candidate** from the same trigger. A candidate page that **did not render its content column** (error page, redirect, crash). A candidate **console error** that is not known noise (deduped per route). |
| **Medium** | (A case-only text difference where either side applies `text-transform` is reported as the transform, not as label drift — the browser's `innerText` applies the transform.) Visible **style drift** on a matched pair: any px property differing by **≥ 2px**, any colour change beyond 3/255 per channel, font family / weight / style, text-transform, decoration, shadow, background image, cursor, display/alignment, opacity ≥ 0.05; a box width/height ≥ 2px off. **Label drift** (text differs). **Icon drift**. A **missing or hidden heading**. An **extra** visible control or heading in the candidate. A control enabled in the candidate but disabled in the baseline. |
| **Low** | Cosmetic drift **under 2px** (≥ 0.5px — below that is layout rounding between two builds, ignored), or a colour within 3/255. |

One finding per element: its severity is the worst of its property differences, and the report
shows the differences at that severity (the full list is in `findings.json`). **One cause, one
High:** a dialog that does not open because its trigger is disabled, hidden or missing in the
candidate is folded into that trigger's finding (noted there), not counted twice.

## Not graded (reported under "Other observations")

- **Baseline-side console errors** — the reference's problems, not candidate parity.
- **Console warnings** — collected and listed, not graded (the High is errors).
- **Known noise** — the framework's neutral list (`assets/console-noise.json`: the browser,
  extensions, dev-server reload, common analytics, the guard's own refusals) plus the routes file's
  `console_noise` (this stack's widgets, analytics vendor, identity-provider dev banner, toolkit
  dev-mode notices). Listed with counts.
- **Writes the guard refused** — the page tried to write while only being viewed. Worth a look:
  a page that PUTs on view is doing work nobody asked for.
- A dialog trigger the candidate could not find by the baseline's label — a coverage note; the
  control diff already reports the label or the missing control.
- A dialog the candidate did not open while the read-only guard was refusing a request during the
  attempt — a coverage note (the app may need a POST-shaped read to open it); check it by hand.

## Before / after and likely source

- **Before (candidate now)** — what the candidate shows: the element and its values at the
  finding's grade.
- **After (match the baseline)** — the baseline's values; for a missing element, where the baseline
  shows it.
- **Likely source** — `sweep.py locate` greps `<remote>/<project.default_branch>` of the candidate
  repo (`app.repo_dir`, narrowed to the routes file's `source.candidate_paths`) for the element's own
  classes (×3), ancestor classes (×1) and its label (×2), +1 for the route's area folder, +1 for a
  component file (`source.component_extensions`), −1 for a logic directory (`source.logic_dirs`); a
  class found in more than 12 files says nothing and is skipped; test, story and fixture files are
  never offered. The baseline reference comes from the same search over `source.baseline_repo_dir`
  when the design reference has a repo. A mapping the brain already holds (`app_map` kind
  `source-map`) wins. Console errors point at the stack trace instead — the DOM does not name the
  file that threw.

## What makes a fix "obvious" (the fix phase)

`sweep.py candidates` offers a Medium **style-drift** finding only when: it is not a data pair; one
candidate file scores ≥ 3 and at least twice the runner-up; that file has one of the routes file's
`source.style_extensions` / `source.component_extensions` and is not on the R6 blast-radius
denylist (dev-fix's `r6_denylist_scan.sh`, reading the target repo's conventions file); a baseline
file was located (so there is a rule to port); and 1–4 medium properties differ (box-size-only drift
has no single rule to port). Anything else is left in the report for a human.
