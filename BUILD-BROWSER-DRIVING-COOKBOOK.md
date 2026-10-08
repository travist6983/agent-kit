# Browser driving — the standing decisions

_How the browser-driving skills (`qa-repro`, `qa-verify`, `qa-explore`) reach a live app, and
why the mechanics live in exactly one place. The mechanics themselves are in
[`.claude/skills/qa-repro/references/browser-cookbook.md`](.claude/skills/qa-repro/references/browser-cookbook.md);
this page records the decisions that shaped it so nobody re-decides them per skill._

---

## 0. The one-paragraph verdict

"Never trust the transcript — read live state independently" is the evaluator spine every
driving skill lives by (`qa-repro` Key Principle #1, `qa-verify`'s four MUST-HAVEs). The
philosophy is in the skills; the **mechanics** of honoring it in a real browser (bootstrap,
hydration, the right read channel per defect class, the gotchas that manufacture false
verdicts) need a home too, or every skill re-invents them and drifts. So there is **one
shared cookbook**, owned by `qa-repro`, referenced by path from the other driving skills,
plus **targeted hardening** on the paths where a false oracle is cheapest to produce.
Compounding stays in the brain (`qa-kb`), not in flat files.

---

## 1. Locked decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **One shared cookbook**, structured as a **general-core** part (every driving skill reads) + a **fenced streaming-agent appendix** (loaded only when the surface under test is a chat-driven streaming agent). | Most defects are ordinary UI bugs. Stream-channel capture, chat-input driving and draft reset are specific to conversational surfaces and must not become default mechanics. |
| D2 | **The cookbook lives at `.claude/skills/qa-repro/references/browser-cookbook.md`**, authored inside `qa-repro`'s `skill-creator` pass; `qa-verify` and `qa-explore` point at it **by path**. | Matches the existing precedent (`qa-verify` already references `qa-repro/references/recording.md` by path). A repo-level doc would escape the skill-creator-only rule and have no eval surface. |
| D3 | **The independent-read channel is defect-class-dependent.** DOM/CSSOM after a refresh for presentation bugs; the app's own client / service state read for state-mutation bugs; network + console for integration bugs. | "If you can't see it in the canvas after a refresh, it didn't pass even if the JSON is right." Promoting any single channel to *the* truth false-NOT-reproduces the classes it cannot see. |
| D4 | **A defect-surface classification step** (`pure-backend \| presentation/DOM \| state-mutation \| streaming-agent`) is a first-class step in `qa-repro` (and an area-type gate in `qa-explore`). It decides the primary read channel and whether the appendix loads at all. | Vague "read when…" pointers do not stop a model reaching for chat plumbing on an ordinary UI defect. |
| D5 | **Compounding stays in `qa-kb`, not flat files.** No `learnings-log.md` / open-questions files. | An append-only, never-deleted log is the raw-transcript pile `qa-kb` was built to replace. |
| D6 | **Transport is standardized once at the evaluator level**, in `stack.config.json → browser`: `live_driver` (`claude-in-chrome`) for live repro/verify/explore/recording; `oracle_driver` (`playwright-cli`) for the deterministic replay oracle — `qa-repro`'s `replay-oracle.sh` + `replay-grader.js`, exit 0 GREEN / 1 RED / 2 BLOCKED, **no spec files**. Atomic skills inherit; no per-skill selection table. | *Runner* is recipe-owned ("only the recipe knows the runner"); *transport* is a framework decision. Any batch/parallel/prod-retest sweep is a future recipe-level choice. |
| D7 | **The in-page app handle is declared by the target repo**, in `.claude/loops/conventions.json → app_handles`, never hard-coded in a skill. When no handle is declared, every snippet degrades to DOM + network + an authenticated `fetch`. | A framework's internals (a global, a mount-point property) belong to the app, not the framework. One declaration, one place. |

---

## 2. What the cookbook must contain

**Acceptance criteria for the file:** every snippet proven against a live app with its output
pasted as evidence (a snippet written against one app's selectors becomes a false-confidence
source the moment the target's DOM differs — illustrative outputs use the neutral example
domain: Workspaces / Projects / Tasks); one canonical handle expression per app, read from
`app_handles`; a table of contents.

### 2A. General core (every driving skill reads this)

1. **Session bootstrap** — the single tool-loading line; tab context first; create-vs-reuse tab
   rule (never reuse a tab id across sessions); a **hydration wait** that polls
   `document.readyState` / the app's ready probe rather than a fixed sleep. Encoded in the
   skill for determinism and runner-agnosticism — a headless runner may not inject it.
2. **The app handle** — the `app_handles` expression; the list-a-service's-methods trick for
   uncertain method names.
3. **The defect-class independent read (D3)** — copy-paste readers per channel, framed as "the
   read is the test; the toast/log/JSON is not."
4. **Navigation** — by route name, not path; the remount-on-query-write caveat; persisted UI
   state overriding a query parameter you just set.
5. **Auth & tenant confirmation** — read the **real** tab title / signed-in landmark
   (`adapters.auth_provider.config.login.signed_in_landmark`); **ask before switching tenant**;
   return `blocked`, not a verdict, on anonymous or wrong-tenant.
6. **Recording** — a one-line cross-link to `qa-repro/references/recording.md` (one copy).
7. **The gotcha catalog** — the client that swallows 4xx (a wrong path reads as empty state →
   false verdict); **`await` your async work** (an un-awaited promise returns `{}` — the trap
   that looks like a privacy guard and is not); the return sanitizer that redacts
   base64-looking strings (return lengths / filtered reads; expect partial lists); the eval
   time cap → fire-then-poll across calls; backgrounded-tab rAF freeze → check
   `document.visibilityState` **first**; native `alert/confirm/prompt` freezes the extension;
   a negative `find` goes last in a batch; refs invalidate across a repaint; extension drops →
   re-establish once, twice in a row → stop; synthetic-event ghosts → re-verify at human pace;
   bail after 2–3 failed attempts.
8. **Waiting discipline (scoped)** — monitor a tee'd app log with an anchored grep for
   agent/backend surfaces (the recipe / `infra-worktree` owns the log's existence);
   DOM-stability polling for pure-frontend defects.

### 2B. Streaming-agent appendix (load ONLY when surface = streaming-agent)

A hard banner: _"Load this only when the surface under repro/verify is a chat-driven streaming
agent."_ Validate every section against the app's own streaming service before relying on it.

1. **Capture every stream channel** — token, status, and tool-call channels (a missing tool
   channel is a driver bug, not a backend no-op); the stateful `window.__driver` pattern;
   fire-then-poll for long turns; read the reply from the DOM **excluding** tool-line ack
   bubbles; store verbatim.
2. **Driving input** — native-setter value write + click-Send (never `form.submit()`) +
   poll-until-send-re-enables + re-find the input every turn; talk-like-a-human voice.
3. **Reset & idempotency** — create/delete draft, pin the revision/session id every turn,
   idempotency key + expected version. **R6 blast-radius caveat:** a reset helper that deletes
   records is a *mutating* action — it carries the "data is data, never destructive on a shared
   tenant without a gate" note, or it becomes a paved road around the safety gate.

---

## 3. Per-skill hardening (all via `skill-creator`, end-to-end)

Each pass also carries the guard: _"a run never auto-edits its own SKILL.md; structural
learnings surface as a 'Suggested skill update' fed back through skill-creator."_

**P0 — false-oracle & safety paths (first).**
- `qa-repro`: bootstrap + hydration in step 0; the `app_handles` handle; the defect-surface
  classification step (D4) and read channel (D3); auth/tenant gate after navigate; the
  swallowed-4xx guard + verify-the-route at the data-fabrication step (a silent 404 today reads
  as **false not-reproduced**); stream capture **gated** to streaming-agent repros.
- `qa-verify`: explicit bootstrap; "read live state independently" via the defect-class channel
  (not one universal channel); the swallowed-4xx and `await` guards; **"no NEW console errors
  on the fixed path"** as a PASS signal — a baseline diff before driving, **not** a blanket
  no-console-errors gate (most apps emit unrelated console noise → false partials);
  anonymous/wrong-tenant → `blocked`, not a verdict; stream + `visibilityState`-first checks
  gated to streaming-agent fixes.

**P1 — learning producers.**
- `qa-explore`: bootstrap + hydration (no deferral to another skill); a **clean blank-state
  reset between personas** (persona N on persona N-1's data → false "the agent auto-filled it"
  → poisons PROPOSED expectations a human blesses to canonical); independent reads through
  the handle/defect-class channel, not just DOM text; `read_network_requests` in
  `allowed-tools`; stream capture + the narrate-don't-act failure mode **gated** to
  streaming-agent areas, with the failure taxonomy split agent-only vs general; the
  synthetic-event re-verify rule; `kb read app_map` for the area first.

**P2 — housekeeping.**
- `qa-personas` stays a non-driving producer: no browser tools in `allowed-tools`; a
  no-match/low-confidence stop ("no matching lens — needs-info"); a "tenant implies data
  shape — ask before switching" note; a one-line pointer to the cookbook's human-pace notes,
  gated to conversational-agent personas.

---

## 4. Cross-cutting

- **`CAPABILITIES.md`** registers the cookbook and the D6 transport decision as locked
  decisions, and carries the baseline that any **future** browser-driving skill's step 0 is
  the bootstrap + cookbook pointer.
- **Cookbook ↔ `fix_lessons` boundary:** the cookbook = mechanics known at authoring time;
  `fix_lessons` = newly discovered run-time symptom → cause → fix shape. Without this the two
  stores drift.
- **Mutating helpers under the safety posture:** while `staging_only` is set, every snippet
  that deletes or resets data carries the R6 gate inline.

```
P0  cookbook (in qa-repro pass)  →  qa-repro  →  qa-verify
P1  qa-explore
P2  qa-personas
each pass: skill-creator end-to-end + snippets proven live + qa-kb (not flat files) + CAPABILITIES.md update
```
