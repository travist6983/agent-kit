---
name: qa-kb
description: >-
  The ONLY interface to the loop knowledge brain — always via the kb CLI, never raw SQL. Trigger
  for any "remember this", "have we seen this symptom", "is this test flaky", "is shipping paused",
  "record the cost/metric" inside a loop. Four stores: app_map (keyed selectors/routes),
  expectations (semantic; AI proposes, only a human blesses), flakiness (counters + R7 quarantine),
  fix_lessons (symptom → root-cause → fix-shape) — plus kb_control flags (global_pause kill switch
  R9, staging_only posture) and loop-health metrics (R5/R8). The CLI enforces provenance, optimistic
  concurrency (--expect-version), propose→bless, and fail-closed pause semantics — ship only on an
  explicit paused:false. Backend: Postgres + pgvector on any host, auth mode from kb.config.json.
---

# qa-kb — the knowledge brain seam

`qa-kb` is the single read/write/search interface to the defect loop's shared memory.
Every other skill (`qa-repro`, `qa-verify`, `dev-fix`, `qa-regression`, `qa-explore`,
`human-explore`, the recipes) reaches the brain **only** through this skill's `kb` CLI. Keeping it
small and storage-agnostic is deliberate: the brain is a *future separate service*, and callers
must not bind to Postgres details.

## Why this skill exists (the three loop-engineering principles it serves)

1. **Build the evaluator first.** `expectations` is the oracle the evaluator (`qa-verify`) checks
   against. A *wrong* canonical expectation makes the whole loop optimize toward a wrong target —
   so expectations are **propose-then-human-bless**, and you must `search` before you `propose` to
   avoid flooding the human queue with near-duplicates.
2. **Write reusable knowledge back.** Every run that learns a selector, route, flake rate, or
   expectation persists it here so the *next* task is easier. Store **compressed, actionable
   lessons — never raw transcripts.**
3. **Classify before adapting.** `flakiness` (with `flake_rate` + `quarantined`) and the failure
   signatures stored here are how a loop tells a regression from a flake, so a retry changes
   strategy instead of blindly repeating.

## Backend & setup — ONE shared brain, on any Postgres

The brain is a **single shared Postgres + pgvector database**, not a per-developer copy. Every
machine — your laptop, your second laptop, a teammate's checkout, a CI runner — reads and writes
the *same* rows. That is the whole point: a selector learned or a lesson written on one machine is
warm on the next. The host is yours to choose (a managed cloud Postgres, a team server, a container
the team shares); the framework only requires that it is reachable by more than one machine.
First-time access on a new machine or a fresh clone is [`docs/brain-setup.md`](../../../docs/brain-setup.md).

| | |
|---|---|
| Server | whatever `database_url` in `kb.config.json` names — e.g. `host=db.example.com port=5432 dbname=loop_kb user=loop_rw sslmode=require` |
| Database | its **own database**, separate from the target app's databases |
| Auth | `auth.mode` — `password` \| `url_env` \| `token_command` (see below) |
| Vectors | the `pgvector` extension, installed on the server (`CREATE EXTENSION vector` needs the rights once) |
| Embeddings | `embedding.provider` / `embedding.model` / `embedding.dim` — default an OpenAI-compatible endpoint, `text-embedding-3-small`, 1536 dims — stored per row as provenance so re-embedding on a model change is possible |

- Config: `.claude/loops/kb.config.json` — **tracked in git**. It is the shared pointer at the
  brain and holds **no secret**: credentials come from the auth mode, never from this file. Don't
  localize it; a fresh clone must land on the shared brain with zero hand-configuration.
  `KB_DATABASE_URL` in the environment (or `kb --db …`) overrides it for one process.
- Secret: `.claude/loops/kb.secret.env` — **gitignored and per-developer**. It holds the
  embedding key (the variable `embedding.api_key_env` names) and, for `auth.mode: password`, the
  DB password (`auth.password_env`, default `KB_DB_PASSWORD`). It is the *only* thing you supply by hand.

The three auth modes, so the tracked file never carries a credential:

| `auth.mode` | where the credential comes from |
|---|---|
| `password` | the DSN itself, `PGPASSWORD` / `~/.pgpass`, or the env var `auth.password_env` names (default `KB_DB_PASSWORD`, usually set in the secret env file) |
| `url_env` | the full DSN, credential included, in the env var `auth.env` names (default `KB_DATABASE_URL`) |
| `token_command` | `auth.command` (argv list or shell string) prints a short-lived DB token that is used as the password — e.g. a cloud CLI's `generate-db-auth-token` |

`token_command` forces `sslmode=require` unless the DSN says otherwise.

Per-machine setup is point-then-verify. Nothing is created:

```bash
cat .claude/loops/kb.config.json                                # database_url + auth.mode are the shared pointer
$EDITOR .claude/loops/kb.secret.env                                 # the key(s) kb.config.json names, e.g. OPENAI_API_KEY=…
kb pause --status                                               # a real read ⇒ you're on the shared brain
python3 .claude/skills/qa-kb/scripts/harness.py                 # fuller: exercises every verb (see below)
```

`harness.py` runs against the **live shared brain** by design — every row it writes carries
`source=kb-harness`, it deletes only by that source, and it fingerprints the rows it did *not* write
to prove it left them byte-identical. It exits non-zero on any failed check.

The schema is a **template** (`scripts/schema.sql.in`) because pgvector columns carry a fixed
dimension. `kb schema --dim N` renders it for your embedder; `scripts/schema.sql` is the rendered
default (1536). Applying it is an **admin/migration** action — done once per brain, for everyone,
when a schema change lands — not a per-dev setup step:

```bash
kb schema --apply                                   # through the configured auth mode; idempotent
kb schema --dim 1024 --model my-embedder --apply    # a different embedder: re-render AND re-apply
kb schema --raw | psql "$KB_DATABASE_URL"           # or with your own client
```

**A wrong brain fails LOUDLY, never silently.** There is no local default: an empty
`database_url` with no `KB_DATABASE_URL` is an error, and a DSN whose host is loopback or a unix
socket — a brain only that one machine can reach — is **refused** (non-zero exit) instead of being
connected to. A silent fallback to a local database returns `ok:true` while the lesson reaches no
one; that is a lost lesson, not a degraded mode. For deliberate offline work, set
`KB_ALLOW_UNSHARED=1` **and** name the local DSN explicitly (`--db …` or `KB_DATABASE_URL`).
Rows written that way live on that machine only and never reach the shared brain — treat it as a
scratch pad, not the brain.

## The interface — call the `kb` CLI

Call the **`kb` wrapper** (`.claude/skills/qa-kb/scripts/kb`), which must be **on your PATH** —
every example below, and every `kb …` line in the other skills, is a bare invocation. Symlink it
into a PATH directory once per machine (see [`docs/brain-setup.md`](../../../docs/brain-setup.md)).
If it isn't on PATH, `kb …` exits **127** (`kb: command not found`) and the write is simply lost —
so fall back to the explicit path rather than skipping the call:
`python3 <repo>/.claude/skills/qa-kb/scripts/kb.py <verb> ...`.

**Every command prints one JSON object** to stdout: `{"ok": true, ...}` on success, or
`{"ok": false, "error": "..."}` with a non-zero exit on failure. Parse the JSON; don't scrape text.
(The one exception is `kb schema --raw`, which prints SQL for piping.)

| Verb | Purpose | Example |
|---|---|---|
| `write app_map` | upsert a selector/route/area-tag (AI writes autonomously) | `kb write app_map --kind selector --area invoices --key submit_button --value '{"selector":"[data-test=invoice-submit]"}' --source qa-repro --ticket PROJ-123` |
| `read app_map` | exact/keyed lookup — `--id`, or `--area`/`--kind` for a slice, plus an optional `--key` that NARROWS (with or without `--kind`) | `kb read app_map --kind selector --area invoices --key submit_button` · `kb read app_map --area invoices` |
| `search app_map` | **fuzzy** lookup (ILIKE over `key`/`area`/`value`) when you don't know the exact key | `kb search app_map --query "assignee cleared on project change" --area tasks --k 20` |
| `confirm app_map` | refresh `last_confirmed_at` on a row you just USED — no value change, no version bump | `kb confirm app_map --id 118 --source qa-verify` |
| `propose expectation` | add a behavior expectation as **proposed**; returns nearest existing (dedup). An *identical* statement in the same area returns the EXISTING row — `action:"duplicate"`, `duplicate_of:<id>`, `ok:true`, **no second row** and no receipt | `kb propose expectation --area members --statement "Inviting a malformed email shows an inline error and blocks submit." --source qa-explore` |
| `search expectations` | **semantic** retrieval (oracle lookup + dedup) | `kb search expectations --query "invalid email should block the invite form" --status canonical --k 5` |
| `bless expectation` | promote proposed → **canonical** (human-only, via `human-explore`) | `kb bless expectation --id 42 --by <human>` |
| `reject expectation` | reject a proposal with a reason | `kb reject expectation --id 42 --by <human> --reason "dup of 12"` |
| `write flakiness` | atomic per-test run/failure counter (`--spec` is the test id: `<test file> > <test name>`, a convention the target repo supplies) | `kb write flakiness --spec "invoices.test > submits" --area invoices --failed --source qa-regression` |
| `search flakiness` | look up a test/signature's flake rate | `kb search flakiness --spec "invoices.test"` |
| `quarantine flakiness` | mark/unmark a flaky test (R7) | `kb quarantine flakiness --spec "invoices.test > submits" --on --reason "..."` |
| `write fix_lessons` | record a fix lesson — called by the **recipe** once `qa-verify` passes, never by `dev-fix` itself. **Two dedup guards:** an *exact* duplicate (same normalized symptom + area) bumps `occurrences` with **no embedder** (`dedup_by:"hash"`); a *near*-duplicate bumps via the vector guard (`dedup_by:"vector"`) (R10) | `kb write fix_lessons --area invoices --symptom "..." --root-cause "..." --fix-shape "..." --source dev-fix --ticket PROJ-123` |
| `search fix_lessons` | **semantic** recall by symptom — "have we fixed this shape before?" | `kb search fix_lessons --query "success toast but state unchanged" --k 3` |
| `reembed` | re-embed rows the embedder was down for (`embed_status='pending'`); reports a now-visible `similar_to` but **never auto-merges** | `kb reembed --store all --limit 50` · `kb reembed --dry-run` |
| `forget` | soft (status→forgotten) or `--hard` delete (R1). `--source` is a real **bulk selector** when no `--id`/`--key`/`--spec` is given: it plans first and applies only with `--yes` | `kb forget expectations --id 42 --soft` · `kb forget app_map --source qa-dream` (plan) · `… --source qa-dream --yes` |
| `undo` | **reverse a whole run from the `kb_writes` ledger** — rows the run created are removed (expectations **soft**-forgotten), a fix-lesson bump is decremented, and anything whose prior value the ledger never recorded is reported `skipped_unreversible`. **Dry run is the DEFAULT**; `--yes` applies. Idempotent, and it never selects its own reversal rows | `kb undo --run-id dream-<run>` (plan) · `kb undo --source qa-dream --since 7d --yes` |
| `decay` | brain-audit: report stale + near-duplicate-canonical conflicts + `stale_app_map` (unconfirmed selectors, report-only); `--apply` to prune (R1) | `kb decay --stale-days 30` |
| `pause` | the global kill switch (R9) — set/clear/read | `kb pause --on --reason "incident" --by <human>` · `kb pause --status` |
| `flag` | generic `kb_control` posture flags — notably `staging_only` (`ship_preflight` derives `prod_deploy_allowed` from it — failing closed to false — and refuses prod-lane branches unless it is off; it does not set `dry_run`, and PRs open live) | `kb flag --name staging_only --status` · `kb flag --name staging_only --off --by <human>` |
| `metric` / `metrics` | write a loop-health metric / read a trend summary (R5/R8); `metrics` filters by `--ticket` / `--area` | `kb metric --name cost_per_fix --value 0.42 --unit usd --loop-id L1 --ticket PROJ-123` · `kb metrics --summary --ticket PROJ-123` |
| `receipt` | what a run actually wrote — the `kb_writes` ledger for a ticket, grouped by store | `kb receipt --ticket PROJ-123 --since 7d` |
| `history` | prior-attempt memory for routing: attempts, writes-by-store, escalations, last outcome, areas. Safe on a cold ticket (empty structures, `ok:true`) | `kb history --ticket PROJ-123` |
| `state` | durable key/value shared across machines (e.g. the dream watermark) | `kb state --key dream.watermark` · `kb state --key dream.watermark --set '{"ts":"2000-01-01T00:00:00Z"}'` |
| `outbox` | the queued-write spool a DB outage fills — inspect / drain / discard | `kb outbox --status` · `kb outbox --replay --limit 50` · `kb outbox --clear --yes` |
| `schema` | render the schema for an embedding dimension, or apply it (admin, idempotent) | `kb schema --dim 1536` · `kb schema --apply` · `kb schema --raw` |

### Cross-cutting contract (what the verbs guarantee)

- **Provenance (R1):** every knowledge row carries `source`, `confidence`, `status`,
  `last_confirmed_at`; expectations also carry `embedding_model` + `embedding_dim`.
- **Automatic receipts (capture is provable, not remembered):** every successful *mutating* verb also
  inserts one `kb_writes` ledger row **in the same transaction** and returns
  `"receipt": {"id":…, "store":…, "row_id":…}`. Ticket/loop/run come from `--ticket`/`--loop-id` when
  given, else from `LOOP_TICKET` / `LOOP_ID` / `LOOP_RUN_ID`. So `kb receipt --ticket <KEY>` is
  the falsifiable record of what a run wrote — no agent has to write itself a receipt, and no artifact's
  claim about the brain has to be taken on trust.
  **Read the shape carefully:** `receipt` and `history` both return a `writes_by_store` key with
  *different* shapes — `receipt.writes_by_store` is `{store: [ …full ledger rows… ]}` (what
  `contracts/validate.py --receipts` cross-checks against), while `history.writes_by_store` is
  `{store: {count, last_at}}` (a roll-up for routing). Same name, different payload; branch on the verb
  you called, not on the key.
- **Normalization (write AND read):** `area` and `kind` are normalized on both sides —
  `"Workspace Settings"` → `workspace-settings`, `"Projects/Tasks"` → `projects-tasks` — so a slice
  read finds what a differently-cased run wrote. A `kind` outside the documented allowlist still
  writes, with `warning: "unknown_kind"` in the response; it is never a hard error.
- **Optimistic concurrency (R4):** pass `--expect-version N` on updates. If the row moved on,
  you get `{"ok": false, "error": "version_conflict", ...}` — **re-read and resolve, never clobber.**
  Parallel loops rely on this.
- **propose→bless (the judgment guard):** AI may `propose` / `search` / suggest-`reject`
  expectations; only `human-explore` may `bless` to canonical. Always `search` first — `propose`
  returns `nearest` neighbors and a `possible_duplicate` warning (default similarity ≥ 0.70) so the
  bless queue doesn't flood (R10).
- **Global pause (R9) — FAIL CLOSED:** `infra-ship` / `infra-prod-verify` / recipe engines **must**
  call `kb pause --status` before any deploy and may proceed **only** on an explicit
  `"paused": false`. Anything else — `"paused": true`, `"paused": null` (seed row missing),
  `ok:false`, DB unreachable, non-JSON output — means **halt and escalate**. "Couldn't verify the
  kill switch" is never "not paused". (`pause --on/--off` upserts the flag row, so a set can
  never silently no-op.)
- **Two dedup guards, and the response says which ran.** `write fix_lessons` and
  `propose expectation` first hash the text — `sha256` of *casefold → punctuation stripped →
  whitespace collapsed → trimmed* — and look for an existing row with the same
  `(normalized area, hash)`. That guard needs **no embedder**, so a retry is safe whether the
  embedder is up or down, and an exact repeat short-circuits before the API call. Only if it
  misses does the vector near-neighbour guard run. Read the two keys together:
  `dedup_by: "hash" | "vector" | null` (which guard *matched*; `null` = none did) and
  `dedup_skipped: true|false` (whether the **vector** guard was skipped — it always is when the
  embedder is down or the hash guard already matched).
  **The two guards have different reach, and the vector one is wider than it looks:** the hash
  guard is scoped to `(normalized area, hash)`, but `write fix_lessons`' vector guard bumps the
  single nearest embedded row in the **whole table, across every area**, at `--dup-threshold`
  (default 0.45). Cross-topic pairs land closer to that line than the "distinct ≈ 0.26"
  calibration suggests — a lesson from one area has measured **0.4468** against an unrelated
  area's row. So a `write` can bump, and `COALESCE` its `root_cause`/`fix_shape`/`ticket` onto, a
  lesson in another area. Read `dedup_by` and `row.id` on the response rather than assuming an
  insert; pass `--dup-threshold` above 1.0 when a write must be an insert (test fixtures,
  back-fills), and check `similarity` when it must be a bump.
- **Reversibility (R1):** every mutating verb ledgers itself, so `kb undo` can reverse a whole
  run without anyone having written down what it did. Undo removes what the run **created** and
  refuses to guess at what it merely **modified** — a row whose prior value was never recorded
  comes back `skipped_unreversible` rather than being deleted, because deleting it would destroy
  knowledge that predates the run. Expectations are only ever **soft**-forgotten by an undo: a
  human bless decision must never be silently destroyed.
  **Re-running an undo is safe, including the decrement.** A delete and a soft-forget evidence
  themselves — the row is gone, or its status says `forgotten` — but a decremented
  `occurrences` looks exactly like a counter that was never decremented, so the reversal of a
  `bump` is proved by the undo's own receipt (`(ledger #N)`) instead, and a second pass reports
  `mode: "skipped_missing"` with `already_undone: true`. Without that, re-running the verb —
  or running `undo --ticket` after `undo --run-id` — would walk a lesson that *predated* the bad
  run down to the floor of 1. Vector dedupe makes this the ordinary case, not a corner: when
  near-duplicate symptoms bump instead of inserting, most of a real run's undo plan is
  decrements.
- **Every write names the brain it landed in:** mutating verbs add `"brain": "<host>/<dbname>"`
  to the envelope (never a credential), so a write to the wrong target is visible in the output
  callers already parse.
- **Compressed lessons only:** statements/signatures/values hold distilled, actionable knowledge.

## Who writes what (do not cross these lines)

| Store | Query pattern | Who writes |
|---|---|---|
| `app_map` | keyed / exact | AI autonomously (`qa-repro`, `qa-explore`, `qa-dream`) |
| `expectations` | **semantic** | AI **proposes** (`qa-repro`, `qa-explore`, `qa-dream`); human **blesses** (`human-explore`) |
| `flakiness` | counters (+ semantic later) | AI autonomously (`qa-regression`, `qa-verify`, `qa-dream`) |
| `fix_lessons` | **semantic** (by symptom) | AI autonomously — the **recipe** writes one after `qa-verify` passes (never `dev-fix` itself); `qa-dream` consolidates the ones a run never got to write |

`app_map`, `flakiness`, and `fix_lessons` are mechanical/experiential — write them with confidence.
`expectations` are judgment — never write one straight to `canonical`. A fix lesson is *experience*
(what worked), not an *oracle* (what should be true) — so, unlike expectations, it needs no bless.
**`qa-dream` is an autonomous writer everywhere except `expectations`, where it is propose-only** — the
consolidation pass reads yesterday's transcripts and artifacts, so its judgment about *intended* behavior
is second-hand by construction and must go through the same human bless gate as any other proposal (R1).

## Common caller patterns

- **Before proposing an expectation:** `search expectations --query "<the behavior>"`. If a close
  match exists (similarity high), reference it instead of proposing a duplicate.
- **After `qa-repro` / `qa-explore` finds a selector:** `write app_map ... --source qa-repro`. When
  updating an existing row, pass `--expect-version` so a concurrent loop can't silently clobber.
- **After a skill *used* a stored selector and it worked:** `confirm app_map --id <id>` — that's what
  makes selector freshness falsifiable (and what `decay`'s `stale_app_map` report reads). Use `write`
  only when the value actually changed.
- **At intake:** `kb history --ticket <KEY>` — prior attempts, escalations, and last outcome, so the
  router isn't stateless on a ticket the loop has already tried.
- **At the end of a run:** `kb receipt --ticket <KEY>` — the run's brain summary comes from the ledger,
  not from the agent's recollection of what it wrote.
- **After a regression run:** `write flakiness --spec ... --area ... [--failed]` for **every test that
  ran, passes included** — a rate needs a denominator, and recording only the reds makes `flake_rate`
  a failure log. Then `quarantine` a test whose `flake_rate` ≥ 0.30 over ≥ 5 runs — the R7 threshold,
  defined in `.claude/loops/loop-limits.json` (not a per-run judgment call).
- **After a bad autonomous run (a dream that learned the wrong thing):** `kb undo --run-id <RUN>`
  to read the plan, then the same command with `--yes`. Prefer it to hand-deleting rows: it is scoped
  by the ledger, idempotent, and it soft-forgets expectations instead of destroying a bless decision.
  `kb forget <store> --source <writer> [--yes]` is the coarser cousin when you want *everything* one
  writer ever wrote.
- **Before any ship:** `kb pause --status` → if paused, stop and escalate; do **not** deploy.
- **End of every loop:** `kb metric --name cost_per_fix ...` (and tokens, escalation/flake/bless
  counts) so the meta-monitor (R8) and budget breaker (R5) have data.

## Stop conditions / failure modes

**A lost write is a lost lesson — so neither of the two outages that could lose one does.** Degradation
is explicit and *recoverable*, and the caller's job is to surface it, never to swallow it.

- **Embedder unavailable (a rate limit, or no key in the env var `embedding.api_key_env` names)** →
  `write fix_lessons` and `propose expectation` **still store the row**: it lands with no vector,
  `embed_status: "pending"`, and the response is `ok:true` (exit 0) carrying `embed_status: "pending"` +
  `warning: "embedder_unavailable: <err>"`. Only *searchability* is deferred, not the knowledge.
  **Retry is still safe:** the hash guard runs before any embedding attempt, so writing the *same*
  lesson again bumps `occurrences` (`action:"bump"`, `dedup_by:"hash"`) and proposing the *same*
  statement again returns the existing row (`action:"duplicate"`) — without the hash guard every
  retry under an outage would insert a clone, and retry is this design's own recovery path.
  `dedup_skipped: true` means only that the **vector** (near-duplicate) guard could not run, so a
  *paraphrase* may still slip through until the row is re-embedded. **Recover with `kb reembed`**
  (the `qa-dream` pass runs it first thing, so a day's pending rows land that night).
- **Brain not migrated (no `symptom_hash`/`statement_hash` column)** → the write still lands and the
  response carries `warning: "hash_dedupe_unavailable: … re-apply scripts/schema.sql"`; only the exact-
  duplicate guard is missing. Re-apply the schema (`kb schema --apply` — an **admin action on the
  shared brain**, see the setup section), then backfill the existing rows' hashes with
  `python3 scripts/migrate_normalize.py --section hashes` (dry run) `--apply --yes`.
- **Reading while the embedder is down** → vector `search` still fails clearly (it can't embed the
  query). The documented degraded path is the non-vector read: `kb read fix_lessons --area <area>` /
  `kb read expectations --area <area>`, plus `kb search app_map` (ILIKE, no embedder). Never work around
  an outage with `--embedder stub`: the stub is **TEST-ONLY** — never auto-selected, runs only on an
  explicit `--embedder stub` (schema/plumbing tests), stamps honest `stub-sha256-<dim>` provenance, and
  must never back a real proposal or lesson.
- **DB unreachable** → a *mutating* verb (`write|propose|metric|quarantine|confirm|state --set`) appends
  its full argv to `<BRAIN_DIR>/outbox.jsonl` (`KB_OUTBOX` overrides the path) and returns
  `{"ok": false, "error": "…", "queued": true, "outbox": "<path>"}`. The write is **deferred, not
  dropped**. Non-mutating verbs (read/search/`pause --status`/`flag --status`/metrics/receipt/history)
  never queue — a stale read is worse than no read. **Recover with `kb outbox --replay`** (replay is
  idempotent-safe: app_map/flakiness are upserts, a duplicate lesson bumps `occurrences`, metrics are
  an append log); `kb outbox --status` shows the depth and oldest entry. Still report the failure — a
  queued write is not a completed one.
  Replay **retargets**: the queued `--db` is by definition the address that was dead, so it is stripped
  and the replay uses this process's DSN (or the replay command's own `--db`), reporting
  `results[].retargeted_from`. An entry stays queued only while it genuinely still fails.
- **No brain target / bad auth config** → `{"ok": false, "error": "no brain target configured …",
  "queued": false}`. A config error is never queued: with no target there is nothing to replay
  against. Fix `kb.config.json` (or export `KB_DATABASE_URL`) and re-run.
- **`version_conflict`** → another writer won; re-read, merge intent, retry with the new version. A
  version conflict (or any other *semantic* failure) is a real disagreement and is **never** queued to
  the outbox — only a connection-level failure is.
- **Global pause is still fail-closed.** None of the above softens R9: an unverifiable `pause --status`
  halts a ship, and a queued/pending write never counts as a pause check.

## Files

- `scripts/kb.py` — the CLI (the contract above). Reads `.claude/loops/kb.config.json`
  (`KB_HOME` overrides the directory) and the secret env file beside it.
- `scripts/schema.sql.in` — the schema **template**; `scripts/schema.sql` — the rendered default
  (1536 dims, `text-embedding-3-small`). Re-render with `kb schema --dim N --model M --out scripts/schema.sql`
  when the embedder changes. Idempotent.
- `scripts/harness.py` — exercises every verb against the real backend, incl. the semantic-search
  proof and a simulated embedder outage; **this is the skill's "eval"**. Honours `KB_DATABASE_URL`,
  `KB_HARNESS_MARK`, `KB_HARNESS_TICKET`, `KB_BRAIN_LOCK` / `KB_LOCK_HELD`.
- `scripts/kb` — bash wrapper for `kb.py`; **put it on your PATH** so a bare `kb <verb>` resolves
  (it execs the `kb.py` beside it, so a symlink from anywhere works).
- `scripts/migrate_normalize.py` — two dry-run-by-default maintenance sections: the `(kind, area)`
  fold, and the `--section hashes` backfill that makes pre-existing rows exact-duplicate-guardable.

> The authoritative, machine-readable interface is also recorded in
> `.claude/loops/CAPABILITIES.md` (the contracts ledger). Downstream skills build
> against that ledger entry, not against this prose.
