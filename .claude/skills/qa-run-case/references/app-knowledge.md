# App knowledge — where to confirm a mutation, and which read paths lie

> **The one job of a knowledge pack.** When a `verify` step needs you to confirm an artifact actually
> persisted, you must **read the real state on the channel it lives on** — never a success toast, never an
> assistant's inline echo. The pack is the adopter's accumulated map of *where* each artifact of the target
> app lives, *which URLs 404*, and *which read paths lie about the truth*. Consult it **before** you decide
> where to look. Guessing a `/<feature>` route 404s and burns a turn; grading persistence off a lying read
> path produces a **false PASS**, which is the single worst outcome this skill can emit.

This framework ships **no** app knowledge — it cannot, the target app is yours. This file defines the
**format** of a pack and the generic trap classes every pack should cover. The examples use the neutral
sample domain (Workspaces, Projects, Tasks, Comments, Invoices, Members, Settings); map your own nouns via
`conventions.domain_glossary`.

## Where the run gets its knowledge

1. **`KNOWLEDGE_DIR`** (prompt token, optional) — a directory of Markdown files in the format below. Read
   every `*.md` in it at step 0. Absent → skip to 2.
2. **`.claude/loops/conventions.json`** — `app_handles` (stable UI selectors the drivers may rely on —
   a primary nav, the toast region, a row test-id; empty = find everything by accessibility tree) and
   `domain_glossary` (the app's nouns → the case authors' nouns). How to read the app's **backend state**
   (an API endpoint per artifact, or a page-exposed client) is pack knowledge (§3 below); with none, the
   run verifies by reload + an unfiltered DOM read.
3. **The rendered UI.** With neither, navigate to `/`, read the page, and find the entry point for the
   artifact under test — never invent a route.

**Read-only at runtime.** A run **never** edits a pack or `conventions.json`. When you discover a *new*
route, widget, 404, or lying read path, append it to `<EVID_ROOT>/<KEY>/learnings.md` as a "Suggested
knowledge-pack update" (which section, the entry, the evidence). A maintainer folds durable learnings back
in deliberately.

**Headless contract.** Every place a field guide says "ask the user", "recommend a tenant switch", or
"surface it" becomes: **grade `BLOCKED` with the exact reason** and write `case-run.json`. When a
verification channel is unavailable (broken tenant, a 404 with no widget fallback, missing precondition
data you can't create), that is a `BLOCKED`, not a guess and not a FAIL of the workflow under test.

---

## Pack format (one or more `*.md` files under `KNOWLEDGE_DIR`)

### 1. Verification map — where artifacts live

Not every artifact type has a `/<feature>` URL. Some live **only** in a dashboard widget or a top-bar
popover. The table tells the run which `verify` surface to read.

| Artifact | Where it lives | How to open / confirm |
|---|---|---|
| **Tasks** | `/projects/<id>/tasks` **and** the dashboard "MY TASKS" widget | Navigate to the project, or open `/` and read the widget. The widget filters to the current member by default — see §2. |
| **Comments** | inline under the parent Task; **no `/comments` route — it 404s** | Open the Task; the comment list renders below the description. |
| **Invoices** | `/invoices` (list) and `/invoices/<id>` | Navigate directly; the list paginates at 25. |

### 2. Confirmed 404s — routes that look valid but aren't

List every route a case author might plausibly guess that does **not** exist, with the surface to use
instead. (`/comments` → open the parent Task; `/members/new` → Settings → Members → Invite.)

### 3. Persistence-verification traps — read paths that lie

The generic trap classes. Each pack states which of these its app has and the **independent read** that
tells the truth:

- **Render caps on inline lists.** An assistant/chat echo or a "recent" widget that shows at most N rows.
  A count read there is a render cap, not the truth. Independent read: the full list page or the API.
- **Default-filtered widgets.** A widget scoped to "mine" / "open" / "this week" hides rows that exist
  (an agent-created row often has no owner). Don't grade "not persisted" off a filtered widget.
  Independent read: an unfiltered list, or the API with the filter removed.
- **Read-path vs write-path divergence.** A value that is populated in the record but renders as `—` or a
  placeholder on one surface. Confirm against the surface that reads the stored field directly.
- **Optimistic UI.** The row appears before the request resolves; a failed save can leave it visible until
  reload. Independent read: reload, then read.
- **Swallowed errors in the app's own service layer.** If the pack documents a page-exposed client whose
  failed calls *resolve* with an error object rather than throwing, guard for that shape — a wrong path is
  a silent no-op that must **not** be reported as success. Prefer a plain same-origin `fetch` to the
  artifact's API endpoint (`credentials: 'include'`), whose `ok`/`status` cannot lie that way.

**Corollary — a read-window bug can hide a real persisted row (don't false-FAIL).** When a create looks
missing, cross-check a second channel (API, unfiltered list, a reload with the filter cleared) and the
creation timestamp before grading FAIL. If no second channel is reachable, that is **BLOCKED** with the
reason, not FAIL.

### 4. Real-time reactivity — when a cross-check needs a refresh

Which surfaces update live (web-socket/polling) and which need a reload before they reflect a mutation
made elsewhere. The run reloads before reading any surface not listed as live.

### 5. Perishable — verify against the current build

Selector quirks, coordinate fallbacks, modal-close behaviors, keyboard shortcuts that do or don't dismiss
a dialog. Everything in this section carries "verify against current build"; the run prefers a fresh
`find` by accessibility tree and reaches for a coordinate only when a ref click provably no-ops
(`chrome-driving.md` §1c).

---

## Hydration / "page is ready" check

The run must not sleep-and-hope after `navigate`. The pack may name a ready condition; without one the
run polls for `adapters.auth_provider.config.login.signed_in_landmark` (the signed-in marker) plus a
resolved location, and treats a fixed sleep as the fallback of last resort.

## Cross-reference — capturing new knowledge

Every entry a run would add to this pack is written to `<EVID_ROOT>/<KEY>/learnings.md` instead, in the
section shape above, with the evidence (screenshot filename, step number, observed value). Maintainers
fold it in; runs never do.
