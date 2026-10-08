# qa-explore — persona-driven walk + the propose-only producer boundary

`qa-explore` is a **learning producer**: it walks a feature *before* there's a bug, so the brain is
already warm when a loop arrives. Its value is seeding `app_map` (so `qa-repro` finds selectors fast)
and **proposing** expectations (so the evaluator has candidate oracles for a human to bless).

## Walk it like real users (persona-driven — use `qa-personas`)
A happy-path click-through misses the failures that matter. Drive the feature as a **range of real
personas** via the **`qa-personas`** skill. The pack is `personas_file` in
`.claude/loops/stack.config.json` (`.claude/loops/personas.json`): every persona has an `id`, a `role`
(one of the pack's `roles`, which mirror the `auth_provider` roles — e.g. owner/admin/member/observer)
and an engagement `tier` (one of the pack's `engagement_tiers`). Pick a **spread across roles and
tiers** — one persona per tier (`pick_persona.py --spread`; `--list` shows the whole pack), so a deeply
engaged member, a brand-new one, a skeptical one and an observer or external reviewer with limited
access each get a turn — and walk it *in character* as each, since each lens surfaces failures the
others miss. Refer to personas by `id` and `role` only; never invent one that isn't in the pack. Layer
the pack's `overlays` on top — the **adversarial** overlay (odd inputs, injection-shaped text, rapid
edits) and the **marathon** overlay (long session, many ops). Record which persona found what.

Sign in as each persona's role through the `auth_provider` adapter (credentials by role; the login
path and selectors come from `adapters.auth_provider.config.login`). A role the adapter cannot
serve is a reported gap in the spread, not a reason to walk as whoever is already signed in.

The failure-mode taxonomy to watch for: narrate-don't-act, fabricated/stale ids, silent field clobber,
parallel-write conflicts, "success" with no real state change.

**Read state independently** even while exploring — record what the DOM/API *actually* shows, never what
the app claims (the `qa-verify` "no trust-me" rule applies here too). Example on the neutral domain:
after "Create invoice" reports success, re-read the Invoices list (or `GET` the API) and confirm the
new row exists with the amount you entered — the toast is the app's claim, the row is the state.

## What qa-explore WRITES (the producer boundary — do not cross it)
| Store | What | Authority |
|---|---|---|
| `app_map` | selectors, routes, area tags discovered on the walk | **autonomous** (mechanical) |
| `flakiness` | anything intermittent/timing-sensitive noticed | **autonomous** |
| `expectations` | how the feature behaves (what *should* be true) | **PROPOSE ONLY** — never canonical |

The expectations boundary is the whole point of the propose→bless guard: a wrong **canonical**
expectation makes the evaluator optimize toward a wrong target. So `qa-explore` only ever **proposes**;
a human promotes via `human-explore`. Always `kb search expectations` first so you reference an existing
one instead of proposing a near-duplicate (R10 in the risk register, `docs/architecture.md` §7: human
blessing is the scaling bottleneck, and every duplicate proposal spends it).

## Don't auto-file issues
If a walk surfaces a likely bug, **draft** an issue in the `issue_tracker` adapter's `create_ticket`
shape — `{ title, body, type: "bug", priority, labels[] }`, the body carrying surface + repro +
expected/actual + the persona that found it — and return it in `draft_tickets[]`. **Do not file it.**
Auto-filing is an outward action and risks flooding the issue tracker; a human (or a gated recipe)
decides whether to file the draft (`issue_tracker create_ticket`, the draft object passed as its
single `draft` argument — the exact call is in `SKILL.md` §5) or hand it to `dev-intake`. The
ticket key is allocated by the tracker when a human files it — a draft has no key; refer to a
filed one by its tracker key (pattern `project.ticket_key_pattern`, e.g. `PROJ-123`).

## Output
`explore.json` `{ area, app_map_writes: [...], proposed_expectations: [...], flakiness_notes: [...],
draft_tickets: [...] }`, written under the run's evidence directory (`evidence.root`, default `runs/`).
The brain is the real product; the JSON is the receipt.
