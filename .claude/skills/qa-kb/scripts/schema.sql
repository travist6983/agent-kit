-- qa-kb schema TEMPLATE — the loop knowledge brain (Postgres + pgvector, any host).
-- Rendered from scripts/schema.sql.in by `kb schema --dim N [--model M]` (defaults: kb.config.json
-- `embedding.dim` / `embedding.model`). This rendering: dim 1536, model 'text-embedding-3-small'.
-- scripts/schema.sql is the rendered default; re-render it when either value changes.
-- ADMIN / MIGRATION ACTION ONLY — applied once per brain, for everyone, when a schema change
-- lands; never a per-developer setup step. A local database is refused unless the config
-- declares "scope": "single-machine" (one person's brain on one machine, on purpose).
-- Apply through the configured auth mode:   kb schema --apply
-- or, with your own client:                   kb schema --raw | psql "$KB_DATABASE_URL"
-- Idempotent (safe to re-run). It is ALSO the migration: on a brain created by an older
-- version, re-applying it adds what is missing in place (see "Projects + lessons" at the end).

CREATE EXTENSION IF NOT EXISTS vector;

-- ── Store 1: app_map ── navigation memory (selectors/routes/area tags), keyed lookups.
CREATE TABLE IF NOT EXISTS app_map (
  id                bigserial PRIMARY KEY,
  project           text        NOT NULL DEFAULT 'global',  -- see "Projects + lessons" below
  kind              text        NOT NULL,            -- 'selector' | 'route' | 'area_tag'
  area              text        NOT NULL DEFAULT '',
  key               text        NOT NULL,
  value             jsonb       NOT NULL,
  source            text        NOT NULL,
  confidence        real        NOT NULL DEFAULT 1.0,
  status            text        NOT NULL DEFAULT 'canonical',
  last_confirmed_at timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  version           integer     NOT NULL DEFAULT 1,
  UNIQUE (project, kind, area, key)
);
CREATE INDEX IF NOT EXISTS app_map_area_idx ON app_map (area);

-- ── Store 2: expectations (pgvector) ── how features should behave; propose→bless.
CREATE TABLE IF NOT EXISTS expectations (
  id                bigserial PRIMARY KEY,
  project           text        NOT NULL DEFAULT 'global',
  area              text        NOT NULL DEFAULT '',
  statement         text        NOT NULL,
  embedding         vector(1536),
  embedding_model   text        NOT NULL DEFAULT 'text-embedding-3-small',
  embedding_dim     integer     NOT NULL DEFAULT 1536,
  status            text        NOT NULL DEFAULT 'proposed',  -- proposed|canonical|rejected|superseded|forgotten
  source            text        NOT NULL,
  confidence        real        NOT NULL DEFAULT 0.5,
  blessed_by        text,
  blessed_at        timestamptz,
  supersedes_id     bigint REFERENCES expectations(id),
  metadata          jsonb       NOT NULL DEFAULT '{}',
  last_confirmed_at timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  version           integer     NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS expectations_embedding_idx
  ON expectations USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS expectations_area_status_idx ON expectations (area, status);

-- ── Store 3: flakiness ── per-test flake rate + known-bad-strategy memory.
--    `spec` holds the test id ('<test file> > <test name>'); the column name is historical.
CREATE TABLE IF NOT EXISTS flakiness (
  id                bigserial PRIMARY KEY,
  project           text        NOT NULL DEFAULT 'global',
  spec              text        NOT NULL,
  area              text        NOT NULL DEFAULT '',
  signature         text        NOT NULL DEFAULT '',
  runs              integer     NOT NULL DEFAULT 0,
  failures          integer     NOT NULL DEFAULT 0,
  flake_rate        real        GENERATED ALWAYS AS
                      (CASE WHEN runs > 0 THEN failures::real / runs ELSE 0 END) STORED,
  quarantined       boolean     NOT NULL DEFAULT false,
  source            text        NOT NULL DEFAULT '',
  last_seen_at      timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  version           integer     NOT NULL DEFAULT 1,
  UNIQUE (project, spec, signature)
);
CREATE INDEX IF NOT EXISTS flakiness_area_idx ON flakiness (area);
CREATE INDEX IF NOT EXISTS flakiness_quarantined_idx ON flakiness (quarantined);

-- ── Store 4: fix_lessons (pgvector) ── symptom → root-cause-class → fix-shape; AI-writable experience.
-- Unlike expectations (a judgment oracle that needs a human bless), a fix lesson is accumulated
-- experience: the recipe writes it autonomously once a fix is verified and recalls it by semantic
-- symptom match before the next fix. Near-duplicate symptoms bump `occurrences` instead of inserting (R10).
CREATE TABLE IF NOT EXISTS fix_lessons (
  id                bigserial PRIMARY KEY,
  project           text        NOT NULL DEFAULT 'global',
  area              text        NOT NULL DEFAULT '',
  symptom           text        NOT NULL,                    -- observable signature (semantic-searchable)
  root_cause        text        NOT NULL DEFAULT '',         -- diagnosed cause class
  fix_shape         text        NOT NULL DEFAULT '',         -- the shape of the fix that worked
  signature_tag     text        NOT NULL DEFAULT '',         -- short handle, e.g. 'state-drift-claim-parity'
  embedding         vector(1536),
  embedding_model   text        NOT NULL DEFAULT 'text-embedding-3-small',
  embedding_dim     integer     NOT NULL DEFAULT 1536,
  occurrences       integer     NOT NULL DEFAULT 1,
  source            text        NOT NULL,
  ticket            text,
  metadata          jsonb       NOT NULL DEFAULT '{}',
  last_seen_at      timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  version           integer     NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS fix_lessons_embedding_idx
  ON fix_lessons USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS fix_lessons_area_idx ON fix_lessons (area);

-- ── Ops 1: kb_control ── global pause flag (R9 kill switch) + posture flags.
CREATE TABLE IF NOT EXISTS kb_control (
  flag       text PRIMARY KEY,
  value      boolean     NOT NULL DEFAULT false,
  reason     text,
  set_by     text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO kb_control (flag, value) VALUES ('global_pause', false)
  ON CONFLICT (flag) DO NOTHING;
-- staging_only: the checkable staging/dry-run posture. TRUE until the R2/R5/R6/R9 pre-launch
-- gates are verified; flip via `kb flag --name staging_only --off --by <human>`.
INSERT INTO kb_control (flag, value) VALUES ('staging_only', true)
  ON CONFLICT (flag) DO NOTHING;

-- ── Ops 2: metrics ── loop-health (R5 budget / R8 meta-monitor).
CREATE TABLE IF NOT EXISTS metrics (
  id          bigserial PRIMARY KEY,
  metric      text        NOT NULL,
  value       double precision NOT NULL,
  unit        text        NOT NULL DEFAULT 'count',
  area        text        NOT NULL DEFAULT '',
  loop_id     text,
  ticket      text,
  metadata    jsonb       NOT NULL DEFAULT '{}',
  recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS metrics_metric_time_idx ON metrics (metric, recorded_at);
CREATE INDEX IF NOT EXISTS metrics_loop_idx ON metrics (loop_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- Durability additions. All idempotent; safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────────

-- embed_status: 'ok' (vector present) | 'pending' (embedder was down at write time).
-- A pending row is DURABLE but not yet semantically searchable — `kb reembed` heals it.
-- Never drop a write just because the embedder is unavailable (rate limit, missing key).
ALTER TABLE fix_lessons  ADD COLUMN IF NOT EXISTS embed_status text NOT NULL DEFAULT 'ok';
ALTER TABLE expectations ADD COLUMN IF NOT EXISTS embed_status text NOT NULL DEFAULT 'ok';

-- app_map rows carry the ticket that taught them (provenance + `kb receipt`).
ALTER TABLE app_map      ADD COLUMN IF NOT EXISTS ticket text;

-- ── Ops 3: kb_writes ── the write ledger. Every successful mutating verb records
-- itself here IN THE SAME TRANSACTION as the knowledge write, so a rolled-back write
-- leaves no receipt and no agent has to remember to write itself one.
CREATE TABLE IF NOT EXISTS kb_writes (
  id          bigserial PRIMARY KEY,
  verb        text NOT NULL,               -- write | propose | confirm | quarantine | metric | bless ...
  store       text NOT NULL,               -- app_map | expectations | flakiness | fix_lessons | metrics
  action      text NOT NULL,               -- insert | update | update_refresh | bump | upsert | propose | confirm
  row_id      bigint,
  source      text NOT NULL DEFAULT '',
  ticket      text, loop_id text, run_id text,
  area        text NOT NULL DEFAULT '',
  summary     text NOT NULL DEFAULT '',    -- short human-readable: the key / spec / first 120 chars of statement
  recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS kb_writes_ticket_idx ON kb_writes (ticket);
CREATE INDEX IF NOT EXISTS kb_writes_time_idx   ON kb_writes (recorded_at);

-- ── Ops 4: kb_state ── durable key/value shared across machines (e.g. the dream watermark).
CREATE TABLE IF NOT EXISTS kb_state (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Hash dedupe. All idempotent; safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────────

-- The EXACT-duplicate guard that works with NO embedder. `symptom_hash` / `statement_hash`
-- are sha256 of the text normalized by kb.py's text_hash(): casefold → punctuation stripped →
-- whitespace collapsed to single spaces → trimmed. Matched together with the NORMALIZED area,
-- so 'Invoices' and 'invoices' are one bucket. This runs BEFORE any embedding attempt, so a
-- retry during an embedder outage bumps `occurrences` instead of inserting a clone — and an
-- exact duplicate short-circuits (and saves the API call) even when the embedder is healthy.
-- NULL = a row written before this landed; scripts/migrate_normalize.py backfills them.
ALTER TABLE fix_lessons  ADD COLUMN IF NOT EXISTS symptom_hash   text;
ALTER TABLE expectations ADD COLUMN IF NOT EXISTS statement_hash text;
CREATE INDEX IF NOT EXISTS fix_lessons_symptom_hash_idx    ON fix_lessons (symptom_hash);
CREATE INDEX IF NOT EXISTS expectations_statement_hash_idx ON expectations (statement_hash);

-- `kb undo` reads the ledger by run/source/ticket, so those three selectors need to be
-- indexed as well as `ticket` already is.
CREATE INDEX IF NOT EXISTS kb_writes_run_idx    ON kb_writes (run_id);
CREATE INDEX IF NOT EXISTS kb_writes_source_idx ON kb_writes (source);

-- ─────────────────────────────────────────────────────────────────────────────
-- Projects + lessons. All idempotent; safe to re-run. This block IS the in-place upgrade for
-- a brain created before projects existed: `kb schema --apply` runs it.
-- ─────────────────────────────────────────────────────────────────────────────

-- `project`: one person can point many of their own projects at one brain. Every knowledge
-- row is stamped with the project it was learned in (kb.py resolve_project: $AGENT_KIT_PROJECT,
-- else the cwd's git repo name, else 'global'); reads see <project> + 'global'. Rows that
-- existed before this column become 'global', so every project keeps seeing them.
ALTER TABLE app_map      ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT 'global';
ALTER TABLE expectations ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT 'global';
ALTER TABLE flakiness    ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT 'global';
ALTER TABLE fix_lessons  ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT 'global';
-- metrics are loop health and read globally, but stamped so they can be split by project;
-- kb_writes records the project of every write (provenance). kb_control (the R9 kill switch
-- and posture flags) and kb_state stay global on purpose: a pause pauses everything.
ALTER TABLE metrics      ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT 'global';
ALTER TABLE kb_writes    ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT 'global';
CREATE INDEX IF NOT EXISTS app_map_project_idx      ON app_map (project);
CREATE INDEX IF NOT EXISTS expectations_project_idx ON expectations (project);
CREATE INDEX IF NOT EXISTS flakiness_project_idx    ON flakiness (project);
CREATE INDEX IF NOT EXISTS fix_lessons_project_idx  ON fix_lessons (project);
CREATE INDEX IF NOT EXISTS metrics_project_idx      ON metrics (project);
CREATE INDEX IF NOT EXISTS kb_writes_project_idx    ON kb_writes (project);

-- The same key may now exist once per project: swap the old unique constraints for
-- project-scoped ones. Existing rows are all 'global', so the new constraint cannot collide.
ALTER TABLE app_map   DROP CONSTRAINT IF EXISTS app_map_kind_area_key_key;
ALTER TABLE flakiness DROP CONSTRAINT IF EXISTS flakiness_spec_signature_key;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conrelid = 'app_map'::regclass AND conname = 'app_map_project_kind_area_key_key') THEN
    ALTER TABLE app_map ADD CONSTRAINT app_map_project_kind_area_key_key UNIQUE (project, kind, area, key);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conrelid = 'flakiness'::regclass AND conname = 'flakiness_project_spec_signature_key') THEN
    ALTER TABLE flakiness ADD CONSTRAINT flakiness_project_spec_signature_key UNIQUE (project, spec, signature);
  END IF;
END $$;

-- ── Store 5: lessons (pgvector) ── general, non-defect working knowledge: tooling facts
-- ("port 8000 is taken on this machine, run the API on 8010"), preferences ("the user prefers
-- multiple-choice questions"), workflow and project facts. AI writes them `proposed` (or
-- `active` when told to); `kb bless lesson` makes one active and only active lessons are
-- returned by `kb search lessons`. An exact repeat (text_hash) or a near-duplicate (cosine
-- >= 0.92 by default, same project) bumps `occurrences` / `last_seen_at` instead of inserting.
CREATE TABLE IF NOT EXISTS lessons (
  id                bigserial PRIMARY KEY,
  project           text        NOT NULL DEFAULT 'global',
  kind              text        NOT NULL DEFAULT 'general',  -- free text: tooling | preference | workflow | project-fact | ...
  text              text        NOT NULL,
  text_hash         text,                                    -- kb.py text_hash(): the embedder-free exact-duplicate key
  embedding         vector(1536),
  embedding_model   text        NOT NULL DEFAULT 'text-embedding-3-small',
  embedding_dim     integer     NOT NULL DEFAULT 1536,
  embed_status      text        NOT NULL DEFAULT 'ok',      -- ok | pending (embedder was down; kb reembed heals)
  status            text        NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed', 'active', 'rejected')),
  source            text        NOT NULL,
  ticket            text,
  occurrences       integer     NOT NULL DEFAULT 1,
  blessed_by        text,
  blessed_at        timestamptz,
  metadata          jsonb       NOT NULL DEFAULT '{}',
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  last_seen_at      timestamptz NOT NULL DEFAULT now(),
  version           integer     NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS lessons_embedding_idx
  ON lessons USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS lessons_project_status_idx ON lessons (project, status);
CREATE INDEX IF NOT EXISTS lessons_text_hash_idx      ON lessons (text_hash);
