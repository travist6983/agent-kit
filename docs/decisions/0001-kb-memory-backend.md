# ADR-0001 — `qa-kb` memory backend

- **Status:** **Accepted — shipped.**
- **Decision owner:** the owner of this framework
- **Affects:** the `qa-kb` skill, and through it every loop (intake, repro, verify, regression, the
  scheduled consolidation pass)

> This is the first decision the build order demands (see
> [`../architecture.md`](../architecture.md) §8). The knowledge brain **must not default to flat
> files**. The backend is a deliberate, per-store decision driven by the **retrieval pattern**, not
> the write pattern. Nothing else gets built until this is signed off, because `qa-kb` is the seam
> every other skill reads and writes through.

---

## Context

The brain has stores that are **kept deliberately separate** (they must not be merged — mechanical
knowledge is written autonomously; judgment is propose-then-bless):

| Store | Dominant query | Who writes |
|---|---|---|
| `app_map` | **Keyed / exact** — "selector for the invoice submit button", "tests tagged `invoices`" | AI, autonomously |
| `expectations` | **Semantic** — "is there already an expectation about inviting a member with a bad email?" *before* proposing | AI **proposes**; human **blesses** to canonical |
| `flakiness` | **Counters + semantic-ish** — per-test flake rate; "is this failure like a known-flaky one?" | AI, autonomously |

**Why `expectations` specifically needs semantic search:** the propose-then-bless guard only works
if, before proposing, the agent can find *semantically similar* existing expectations. Exact-match
can't see that "bad email shows inline error" and "invalid email displays validation message" are
the same rule. Without embeddings the human bless-queue floods with near-duplicates (R10) and
conflicting oracles silently coexist and poison the evaluator (R1). This is the one place vectors
clearly earn their keep.

---

## Options considered

1. **Flat committed JSON/YAML files** (one per store). Simple, diffable, versioned in git.
   ✗ No semantic search for `expectations`. ✗ Provenance/forget (R1) becomes file surgery.
   ✗ Concurrent writes (R4) are last-write-wins on a file. ✗ Doesn't scale to the metrics
   store (R5/R8). **Rejected** as the primary backend — the anti-pattern the build order warns against.
2. **SQLite.** Relational, no server. ✗ Still no native vector search without an extension;
   ✗ concurrent multi-runner writes are weak; ✗ a per-machine file is, by construction, a brain
   only one machine can see. **Rejected.**
3. **Postgres + `pgvector`** (a database most teams already know how to run).
   ✓ Relational tables for `app_map` + metrics; ✓ vector similarity for `expectations`;
   ✓ counters (+ optional later vector column) for `flakiness`; ✓ provenance/forget = columns
   + SQL; ✓ optimistic-concurrency on writes; ✓ one backend, three access patterns; ✓ runs
   anywhere — managed cloud, a team server, a shared container. **Recommended.**
4. **Dedicated vector DB** (Pinecone / Weaviate / Qdrant) alongside Postgres. ✓ Best-in-class
   vector search. ✗ A second system to operate, secure, and back up; ✗ splits the brain across
   two stores; ✗ overkill at this volume. **Rejected for now** — revisit only if pgvector
   recall/latency becomes a real bottleneck.

---

## Recommendation

**Consolidate every store in one Postgres database**, using the right shape per store:

| Store | Backend | Shape |
|---|---|---|
| `app_map` | **Relational table** | Keyed rows: `(kind, area, key) → selector/route`, with area-tag columns for regression slicing. Vectors add nothing for exact lookups. |
| `expectations` | **`pgvector` table** | Each expectation embedded; query by cosine similarity to dedup before proposing and to retrieve the right oracle at verify time. |
| `flakiness` | **Counter table** (vector column later) | Per-test flake-rate counters now; add a vector column for failure-similarity matching only if it proves valuable. |
| `metrics` | **Relational table** | cost-per-fix, escalation/flake/bless trends for the meta-monitor (R5/R8). |
| `control` | **Keyed flag table** | The global pause flag / kill switch (R9) and the `staging_only` posture that `infra-ship` + `infra-prod-verify` check before any deploy. |

### Cross-cutting requirements (all become columns + SQL in Postgres)

- **Provenance + forget (R1):** every row carries `source`, `last_confirmed_at`,
  `confidence`, `status (proposed|canonical|…)`. The scheduled brain audit (stale decay,
  dead-route pruning, conflict resolution) is just SQL.
- **Embeddings (R1):** store the **embedding model + dimension** alongside every vector so
  re-embedding is possible on a model change.
- **Concurrency (R4):** writes use optimistic-concurrency checks (version/`updated_at`
  guard), not blind last-write-wins, so parallel loops don't clobber each other's learning.

### The `qa-kb` interface stays storage-agnostic regardless

The skill exposes verbs — `read · write · search · forget/decay · propose→bless` — and
callers never see SQL. This is the seam a future standalone brain service would sit behind: the
backend can still evolve (table → vector → dedicated DB) **without rewriting a single caller.**

---

## Consequences

- **+** One system most teams already run, secure, and back up. Semantic search where it
  matters; exact speed where it matters. R1/R4/R5/R8/R9 requirements become cheap (columns + SQL).
- **−** Adds the `pgvector` extension and an embedding step in the `expectations` write path
  (needs an embedding model + a place to call it).
- **−** Schema changes now govern the brain — and they are deliberately an admin action
  (`kb schema --apply`), never something a loop performs. The same denylist that keeps `dev-fix`
  out of the target app's migrations (R6) keeps it out of the brain's.

---

## Decision — as shipped

Every question below was settled during the build and is what the framework ships. This section is
the record, not an open ask; re-opening any of it needs a new ADR.

1. **Backend:** **Postgres + `pgvector`** for the whole brain, **on any host**. `app_map` and
   `flakiness` are relational tables; `expectations` and `fix_lessons` carry `vector(N)` columns.
   Nothing lives in committed files. The framework does not care whether the server is a managed
   cloud instance, a team-run VM, or a container the team shares — only that more than one machine
   can reach it.
2. **Database instance:** its **own database**, separate from the target app's databases, named by
   the DSN in `.claude/loops/kb.config.json` (`database_url`) or by `KB_DATABASE_URL`. The DSN is
   **never defaulted**: an empty value is an error, and a loopback / unix-socket host is refused
   unless `KB_ALLOW_UNSHARED=1` declares deliberate offline work. `pgvector` must be installed on
   the server (`CREATE EXTENSION vector`, once, by someone with the rights).
3. **Authentication:** chosen per deployment by `kb.config.json` `auth.mode` —
   `password` (DSN / `PGPASSWORD` / `~/.pgpass` / the env var `auth.password_env` names),
   `url_env` (the full DSN with its credential in the env var `auth.env` names),
   or `token_command` (a command that prints a short-lived DB token, e.g. a cloud CLI's
   `generate-db-auth-token`). `token_command` forces `sslmode=require`. **No
   credential is ever stored in the tracked pointer file.**
4. **Embedding model:** configured, not hard-coded — `embedding.provider` / `embedding.model` /
   `embedding.dim` / `embedding.api_key_env` in `kb.config.json`. The default is an OpenAI-compatible
   endpoint with `text-embedding-3-small` at 1536 dimensions. The schema is a **template** rendered
   for the configured dimension (`kb schema --dim N`); every row records `embedding_model` +
   `embedding_dim` so a model change can be re-embedded (`kb reembed`). The hash `stub` embedder is
   test-only and never backs a real row.
5. **`flakiness`:** shipped **counters only**, as recommended. The vector column stays a later
   option; R7 quarantine does not need it.
6. **Migrations tool:** raw idempotent SQL — `.claude/skills/qa-kb/scripts/schema.sql.in`, rendered
   and applied as a deliberate admin action (`kb schema --apply`, or `kb schema --raw | psql …`)
   against the shared server. The brain does **not** ride the target app's migration tooling and is
   not part of the app's migration lifecycle.

**One brain, shared** — the decisive consequence. There is exactly one brain per team and every
developer and every machine uses it. The pointer `.claude/loops/kb.config.json` is **tracked in
git**, so a fresh clone or a second machine lands on it automatically; a host only one machine can
reach is **refused** rather than silently written to; there is no local fallback. See
[`../brain-setup.md`](../brain-setup.md) for setup and access and [`../../CLAUDE.md`](../../CLAUDE.md)
for the standing rule.

**Scope note:** this ADR was written when the brain had *three* stores. It shipped with **four** —
`fix_lessons` (symptom → root-cause → fix-shape) was added during the build — plus the operational
tables `kb_control`, `metrics`, `kb_writes` (the provenance ledger behind `kb receipt` / `kb undo`),
and `kb_state` (durable cross-machine key/value).
