# Brain setup — standing up, and getting onto, the shared brain

> The knowledge brain is **one shared Postgres + `pgvector` database per team**, not a per-developer
> copy. This is how you create it once, point the framework at it, get a new machine (or a new
> teammate, or CI) onto it, verify it is the *shared* brain and not a local shadow of it, and what to
> do when it refuses to connect. Creating the database is a one-time admin step; everything after it
> is a few minutes per machine.

---

## 1. What the brain is, and why it is shared

Every loop reads and writes a small, compressed memory: selectors and routes (`app_map`), how a
feature *should* behave (`expectations`), per-test flake rates (`flakiness`), and symptom →
root-cause → fix-shape experience (`fix_lessons`), plus the `kb_control` flags (the R9 kill switch,
the `staging_only` posture), the loop-health metrics, the `kb_writes` provenance ledger and the
`kb_state` key/value store. That memory is the whole point of the design — *self-improvement is loop
evolution, not model evolution* ([`loop-engineering.md`](loop-engineering.md)) — and it only compounds
if **every** run writes into the **same** brain. A lesson learned on your desktop has to be warm on
your laptop, on a teammate's clone, and on a CI runner. Split the brain across machines and each one
relearns what the others already knew.

All access goes through the `qa-kb` skill's `kb` CLI
([`../.claude/skills/qa-kb/SKILL.md`](../.claude/skills/qa-kb/SKILL.md)) — never raw SQL, so
provenance, concurrency, and the propose→bless gate are always enforced.

The backend decision — why Postgres + `pgvector`, per store — is
[`decisions/0001-kb-memory-backend.md`](decisions/0001-kb-memory-backend.md). This page is the
operational half.

---

## 2. Create the database — once per team

Any Postgres the team can share will do: a managed cloud instance, a VM the team runs, a container
on a shared host. The requirements are short:

| Requirement | Why |
|---|---|
| PostgreSQL 15 or newer | the schema uses features of current Postgres; older may work, untested |
| the `pgvector` extension **available on the server** | `expectations` and `fix_lessons` carry vector columns; `CREATE EXTENSION vector` needs it installed (OS package / cloud "allowed extensions" list) |
| reachable from **more than one machine** | the entire reason the brain exists (§1); a loopback-only server is refused by design (§7) |
| TLS (`sslmode=require`) | token-based auth modes force it; use it for password auth too |

Create a dedicated database and a least-privilege role. The brain gets its **own database**, never a
schema inside the target app's database:

```sql
-- as an administrator on the server
CREATE ROLE loop_rw LOGIN PASSWORD '…';          -- or no password: see the auth modes in §3
CREATE DATABASE loop_kb OWNER loop_rw;
\c loop_kb
CREATE EXTENSION IF NOT EXISTS vector;          -- needs the rights to create extensions, once
```

The schema itself is applied in §5, through the `kb` CLI. Nothing else is created by hand.

---

## 3. Point the framework at it — `kb.config.json`

The pointer is **`.claude/loops/kb.config.json`, which is tracked in git**. Fill it in once, commit
it, and every clone, every machine and every CI checkout lands on the same brain with zero
hand-configuration. The file carries **no secret**: credentials come from the auth mode, never from
this file. Do not localize it, do not re-point it at a local database, and do not gitignore it.

```jsonc
{
  "database_url": "host=db.example.com port=5432 dbname=loop_kb user=loop_rw sslmode=require",
  "auth": { "mode": "password" },
  "embedding": { "provider": "openai", "model": "text-embedding-3-small", "dim": 1536,
                 "api_key_env": "OPENAI_API_KEY" },
  "secret_env_file": "kb.secret.env"
}
```

`database_url` is **explicit and never defaulted**: an empty value with no `KB_DATABASE_URL` in the
environment is an error, not a cue to guess. The CLI resolves the DSN in this order:
`kb --db <dsn>` → `KB_DATABASE_URL` → (for `auth.mode: url_env`, the variable `auth.env` names) →
`kb.config.json` `database_url`. Leave the overrides unset and you land on the shared brain.

### The auth modes

Pick the one that matches how your server authenticates. The tracked file names the *mode*; the
credential lives in the environment or the gitignored secret file.

| `auth.mode` | Where the credential comes from | Typical for |
|---|---|---|
| `password` | the DSN itself, `PGPASSWORD` / `~/.pgpass`, or the env var `auth.password_env` names (default `KB_DB_PASSWORD`, usually set in the secret env file) | a team Postgres, a container, most managed instances |
| `url_env` | the **full DSN including its credential** in the env var `auth.env` names (default `KB_DATABASE_URL`) — nothing but the mode in the tracked file | platforms that inject a `DATABASE_URL`-style secret |
| `token_command` | `auth.command` (an argv list or a shell string) prints a **short-lived DB token** that is used as the password, re-minted per call | cloud databases with IAM-style auth (e.g. a CLI's `generate-db-auth-token`) |

Examples:

```jsonc
"auth": { "mode": "password", "password_env": "KB_DB_PASSWORD" }
"auth": { "mode": "url_env", "env": "KB_DATABASE_URL" }
"auth": { "mode": "token_command",
          "command": ["aws", "rds", "generate-db-auth-token", "--hostname", "db.example.com",
                      "--port", "5432", "--username", "loop_rw"] }
```

`token_command` forces `sslmode=require` unless the DSN says otherwise.
A token is short-lived and re-minted on every `kb` call; there is nothing to cache or refresh.

### The embedding

`embedding.provider` is `openai`, `openai-compatible` (set `embedding.base_url` for a self-hosted or
gateway endpoint) or `stub` (**test-only** — it stamps fake vectors and must never back a real row).
`embedding.dim` must match the schema you apply (§5); changing the model or the dimension means
re-rendering and re-applying the schema and running `kb reembed`.

---

## 4. Per-machine setup

### 4.1 The secret env file

The **only** thing you supply per machine is the gitignored secret file next to the pointer,
`.claude/loops/kb.secret.env` (the name is `secret_env_file` in `kb.config.json`). It holds the
embedding key — the variable `embedding.api_key_env` names — and, for `auth.mode: password`, the DB
password:

```bash
# NOTE: `>` truncates. If the file already exists, open it and add the lines instead.
printf 'OPENAI_API_KEY=sk-…\nKB_DB_PASSWORD=…\n' >> .claude/loops/kb.secret.env
chmod 600 .claude/loops/kb.secret.env
```

Use an embedding key with a sane spend cap; it is only used for the configured embedding model.
Never commit this file and never paste a key into `kb.config.json`. Without the embedding key the
brain still *works*: writes land with `embed_status: "pending"` and a loud warning, and `kb reembed`
(or the scheduled `qa-dream` pass) backfills the vectors later. Semantic `search` is what breaks.

### 4.2 Authenticate, per the mode

- `password` / `url_env` — nothing beyond §4.1 (or the env var your platform injects).
- `token_command` — make sure the command runs by hand and prints a token:
  `"${command[@]}"` from your shell should succeed before any `kb` call will.

### 4.3 Put the `kb` wrapper on your PATH

Every skill in this repo calls `kb <verb> …` bare. If the wrapper is not on PATH those calls exit
**127** and the write is silently lost, so this step is not optional.

```bash
echo $PATH | tr ':' '\n'          # pick a directory you actually have and own
mkdir -p ~/.local/bin             # ~/bin or /usr/local/bin are equally fine — use yours
ln -s "$PWD/.claude/skills/qa-kb/scripts/kb" ~/.local/bin/kb   # run from the repo root
hash -r
which kb                          # must print your symlink
```

The wrapper execs the `kb.py` sitting beside it and resolves the brain directory from its own real
path, so the symlink works from any working directory. If `~/.local/bin` is not on your PATH, add it
to your shell profile rather than copying the script (a copy goes stale).

---

## 5. Apply the schema — once per brain, by an admin

The schema is a **template** (`.claude/skills/qa-kb/scripts/schema.sql.in`) because `pgvector`
columns carry a fixed dimension. `kb schema` renders it for `embedding.dim` / `embedding.model` from
`kb.config.json` and applies it through the configured auth mode. It is idempotent and it seeds the
control rows (`global_pause = false`, `staging_only = true`):

```bash
kb schema --apply                                   # render for the configured dim and apply
kb schema --dim 1024 --model my-embedder --apply    # a different embedder: re-render AND re-apply
kb schema --raw | psql "$KB_DATABASE_URL"           # or with your own client
```

Applying the schema is an **admin / migration action** — done once per brain, for everyone, and again
when a schema change lands. It is not a per-developer setup step, and a loop never performs it.

---

## 6. Verify you are on the shared brain

```bash
kb pause --status                 # cheapest real round-trip: connects, authenticates, reads kb_control
kb read app_map --area invoices   # rows other people's runs wrote (any area your team has worked in)
```

Two things to look for:

- **`{"ok": true, …}`** — you connected and authenticated. `pause --status` should report an explicit
  `"paused": false` on a healthy brain. (`"paused": null` means the seed row is missing — apply the
  schema, §5. Every deploy gate treats anything but an explicit `false` as *paused*.)
- **Other people's data.** On a brain that has been in use, `app_map` holds rows with `source` values
  like `qa-repro` / `qa-explore` / `qa-dream` and ticket keys you did not run. A brain that answers
  `ok:true` but is **empty everywhere** is either brand new or a local shadow — know which.

To prove the server itself is the shared one and not something on localhost (optional, needs `psql`):

```bash
psql "host=db.example.com port=5432 dbname=loop_kb user=loop_rw sslmode=require" \
     -c "select current_database(), inet_server_addr(), version();"
```

The shared brain answers your database name, a routable server address and the server's platform
string. A locally installed Postgres answers a null or loopback address — if you see that, you are
on a shadow (§7).

For a full install check, `python3 .claude/skills/qa-kb/scripts/harness.py` exercises every verb
against the live brain. It is safe to run: every row it writes carries `source=kb-harness`, it deletes
only by that source, and it fingerprints the rows it did not write to prove it left them untouched.

---

## 7. The unshared-brain guard — `KB_ALLOW_UNSHARED`

A silent fallback to a local database is the worst failure the brain can have: it returns `ok:true`
while the lesson reaches no one. So the CLI **refuses**, non-zero, when the resolved DSN's host is a
loopback name or address, or a unix socket — a brain only that one machine can reach.

A refusal means something re-pointed you: a stray `KB_DATABASE_URL` in your shell, a `--db` argument,
or a locally edited `kb.config.json`. Fix the source rather than working around it:
`env | grep KB_` and `git diff -- .claude/loops/kb.config.json`.

For genuinely offline work, set **`KB_ALLOW_UNSHARED=1` and name the local DSN explicitly** (`--db …`
or `KB_DATABASE_URL`). The CLI warns on every call. Anything written that way is a scratch pad: it
stays on that machine and never reaches the shared brain. Do not try to "sync" it by hand later; a
row that matters is re-learned or re-proposed against the real brain.

A refusal is not a lost write: a **mutating** verb spools to the outbox (§8) and returns
`queued:true`. Once you are pointed back at the shared brain, `kb outbox --replay` retargets and lands
it.

---

## 8. Troubleshooting

**`kb: command not found` / exit 127.** The wrapper is not on PATH (§4.3). Until it is, call it
explicitly — `python3 <repo>/.claude/skills/qa-kb/scripts/kb.py <verb> …` — rather than skipping the
call. A skipped `kb` call is a lost lesson.

**Authentication fails.** Read the mode you are in. `password`: is `KB_DB_PASSWORD` (or the variable
`auth.password_env` names) set in the secret file, and does `psql` with the same DSN accept it?
`url_env`: is the variable `auth.env` names exported in *this* shell? `token_command`: run the command
by hand — a CLI that is not logged in prints an error, not a token; tokens are short-lived. If authentication succeeds and the database still rejects you, you
are authenticated but not *authorized*: the role needs rights on `loop_kb`. **Scheduled runs**
(launchd, cron, a pipeline) inherit a minimal `PATH` — if the token CLI isn't on it, the token path
fails silently at night while it works by hand at noon. Give the job an explicit `PATH` and preflight
it (`.claude/loops/scheduler/`).

**The connection hangs, then fails with a timeout.** That is the network (a firewall or allow-list),
not your credentials. Because it is a *transport* failure, a **mutating** verb does not drop the
write: it queues to `<brain dir>/outbox.jsonl` (override with `KB_OUTBOX`) and returns
`{"ok": false, …, "queued": true}`. Once you can reach the server, drain it — `kb outbox --status`,
then `kb outbox --replay`. Reads never queue (a stale read is worse than no read).

**A refusal naming a loopback host.** The fail-closed guard (§7) doing its job. Find what re-pointed
you; do not set `KB_ALLOW_UNSHARED=1` to make the message go away.

**`"paused": null` or `global_pause row missing`.** The schema has not been applied on this brain, or
was applied for an older version. Apply it (§5); until then every ship gate fails closed.

**Semantic `search` fails but reads work.** The embedder is down (missing or expired key, a 429 from
the provider). Use the non-vector reads — `kb read fix_lessons --area <area>`,
`kb read expectations --area <area>`, `kb search app_map` (substring match) — and let `kb reembed`
catch up. Never fall back to `--embedder stub`: it stamps fake provenance and must never back a real
lesson.

**`vector` type does not exist.** `CREATE EXTENSION vector` has not run on this database, or the
extension is not installed on the server (§2). That is an admin step; the CLI will not attempt it.

---

## 9. Why this is fail-closed

- **The pointer is tracked.** `kb.config.json` ships in the repo, so a fresh clone, a second machine,
  a teammate, and CI all resolve the same brain with zero hand-configuration. The failure mode it
  prevents is a pointer that exists on exactly one laptop.
- **There is no local default.** A missing DSN is an error. A fallback that connected to whatever
  local Postgres happened to be running, committed, and returned `ok:true` would be a lost lesson
  that looked exactly like a successful one.
- **An unshared host is refused.** Being on the wrong brain fails **loudly** and non-zero, and going
  local is an explicit, deliberate act (`KB_ALLOW_UNSHARED=1`), never an accident.
- **An unreachable brain defers, it does not drop.** A transport failure queues mutating writes to
  the outbox for `kb outbox --replay`, so an expired token or a blocked network costs a delay, not a
  lesson.

---

## See also

- [`../.claude/skills/qa-kb/SKILL.md`](../.claude/skills/qa-kb/SKILL.md) — the `kb` CLI contract: every
  verb, the cross-cutting guarantees, and the degraded-mode behavior.
- [`getting-started.md`](getting-started.md) — where this step sits in pointing the framework at your app.
- [`loop-engineering.md`](loop-engineering.md) — why the brain exists at all, and the loop-readiness
  checklist every skill is held to.
- [`decisions/0001-kb-memory-backend.md`](decisions/0001-kb-memory-backend.md) — why Postgres +
  pgvector, per store.
