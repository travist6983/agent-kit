# Getting started — point the framework at your app

This framework does not know your stack. It knows a handful of tracked config files under
`.claude/loops/`, reaches every external system through an adapter role, and keeps its memory in a
Postgres database you point it at. This page walks from a fresh clone to the first dry run of the
defect loop on a ticket that lives in a file. Nothing here touches production.

Read [`loop-engineering.md`](loop-engineering.md) first if you have not — it is the rubric every
skill is held to, and it explains *why* the steps below exist.

## 0. Prerequisites

| Need | Why |
|---|---|
| `git`, `python3` (3.9+), `jq`, `bash` | every skill script; the adapter CLI is stdlib-only |
| the runner — Claude Code | the loops are Claude Code skills; the scheduler templates assume `runner.claude_bin` |
| `browser.oracle_driver` (`playwright-cli` by default) on PATH | the deterministic replay oracle; absent ⇒ oracle tests SKIP and repro/verify are blocked |
| `browser.live_driver` (`claude-in-chrome` by default) | live reproduction and the before/after recordings |
| a shared Postgres with `pgvector` | the brain — [`brain-setup.md`](brain-setup.md) |
| `psycopg2` for Python | the `kb` CLI's database driver |
| an embedding API key | semantic search over expectations and fix lessons (the brain works without it, degraded) |

## 1. Clone, and look around

```
.claude/loops/stack.config.json     ← your app, your commands, your adapters (tracked, no secrets)
.claude/loops/stack.config.example.json   fully commented copy of every key
.claude/loops/kb.config.json        ← the brain pointer (tracked, no secrets)
.claude/loops/conventions.json      ← your house rules for the readiness gate
.claude/loops/loop-limits.json      ← retry caps and the R5 budget cap
.claude/loops/personas.json         ← the persona pack qa-explore draws from
lib/adapters/                       ← the adapter layer (CLI + package)
.claude/skills/<name>/              ← the atomic skills and the thin loop recipes
contracts/                          ← the artifact schemas every seam file validates against
runs/                               ← gitignored evidence root (tickets, PRs, media, sandboxes)
```

Prove the toolchain before configuring anything:

```bash
python3 lib/adapters/cli.py --list            # roles, configured providers, ops
bash .claude/loops/tests/run_all.sh           # green; brain and oracle suites SKIP until §3 / §0
```

## 2. Point `stack.config.json` at your app

Edit `.claude/loops/stack.config.json`. Every key is documented in
[`stack-config.md`](stack-config.md); the commented example next to it is itself loadable. The keys
that matter on day one:

```jsonc
"project": {
  "name": "my-app",                               // slug: names the sandbox dir and sandbox DBs
  "ticket_key_pattern": "^PROJ-[0-9]+$",          // what intake accepts; a literal prefix also names new file tickets
  "default_branch": "main",
  "branch_prefixes": { "fix": "fix/", "feature": "feature/" },
  "protected_branch_patterns": ["main", "release/*"]   // never pushed to, never a PR source
},
"app": {
  "repo_dir": "../my-app",                        // the app's checkout, relative to this repo
  "url": "http://localhost:3000",                 // sandboxes shift this port by slot * port_stride
  "api_url": null,                                // a separate API origin, if any
  "health_path": "/",                             // 2xx here = the app is up
  "start_command": "npm run dev",                 // run by the recipe inside a sandbox dir
  "environments": { "local": { "url": "http://localhost:3000" },
                    "staging": { "url": null }, "prod": { "url": null } }
},
"stack": {
  "build_command": null,                          // null = the gate is reported as skipped, never silently passed
  "lint_command": null,
  "unit_test_command": null,
  "test_report": { "format": "junit", "glob": "**/test-results/*.xml" },
  "conventions_file": ".claude/loops/conventions.json",
  "repo_guidelines_file": "CLAUDE.md"             // YOUR app repo's working agreement
}
```

Two rules worth knowing now:

- **Secrets never go in this file.** Where a provider needs one, config holds the *name* of the env
  var (`"api_key_env": "LINEAR_API_KEY"`), never a value.
- **Any key can be overridden per process** with `LOOPS_CFG__<PATH>` (dots → `__`, upper-cased):
  `LOOPS_CFG__APP__URL=http://localhost:4000`. Useful for CI and for trying a change without editing
  the tracked file.

If a merge or push in your repo **deploys**, set `deploy.prod_lane_branch_patterns` now. The default
is empty (no push is a deploy), which is safe only because `infra-ship` never deploys and the brain's
`staging_only` flag gates any prod lane — but a push that *is* a deploy should be refused as one.

## 3. The brain — `kb.config.json` and the schema

The brain is one shared Postgres + `pgvector` database per team. Follow
[`brain-setup.md`](brain-setup.md): create the database once, fill `database_url` and `auth.mode` in
`.claude/loops/kb.config.json`, commit the pointer, put the secret(s) in the gitignored
`kb.secret.env`, symlink the `kb` wrapper onto your PATH, apply the schema, then:

```bash
kb pause --status        # {"ok": true, "paused": false, …} — you are on the shared brain
kb flag --name staging_only --status     # true on a fresh brain: no prod deploy until a human clears it
```

The DSN is never defaulted and a loopback host is refused unless `KB_ALLOW_UNSHARED=1` — a brain only
one machine can see is a lesson lost, not a degraded mode.

## 4. Conventions — your house rules

`.claude/loops/conventions.json` is what `dev-pre-pr` scans added lines with, what
`dev-code-review` reviews against, and where `dev-fix` reads the R6 blast-radius denylist. The
shipped file is a language-agnostic floor: no debug output in added non-test lines, no focused
tests, no secret-looking literals, no `TODO` without a ticket key. Add your own rules in the same
format — [`../examples/conventions/README.md`](../examples/conventions/README.md) walks through a
full example pack — and at minimum fill:

```jsonc
"blast_radius_denylist_globs": ["db/migrate/**", "config/credentials*", "infra/**", ".github/workflows/**"],
"shared_core_globs":           ["app/models/concerns/**", "web/src/shared/**"],
"domain_glossary":             { "workspace": "your tenant noun", "project": "…" }
```

The denylist is the forced-human gate: `dev-fix` never edits a matching path, and `infra-ship`
re-scans the full change set. Leave it empty and nothing is protected.

## 5. Test credentials — the two env files

The replay oracle signs in as a real user, so a loop without credentials is a loop that blocks at
verify. [`test-credentials.md`](test-credentials.md) has the full story; the short version:

1. Set `adapters.auth_provider.config.roles` to your app's role names and `login` to your login
   form's selectors (the oracle will not guess a selector).
2. Create one test user per role **by signing up through the local app**, so the user exists in the
   identity store *and* the database a sandbox will run against.
3. Put usernames in `.env.e2e` and passwords in `.env.e2e.local` (gitignored), named per
   `credential_env_pattern` (`E2E_OWNER_USERNAME`, `E2E_OWNER_PASSWORD`, …).
4. Prove it: `python3 .claude/skills/infra-worktree/scripts/oracle_preflight.py --app-url
   http://localhost:3000 --roles owner`.

## 6. Adapters — what the defaults give you

Out of the box the stack is tracker-less and host-less, which is exactly what a dry run wants:

| role | default provider | what that means on day one |
|---|---|---|
| `issue_tracker` | `file` | tickets are files under `runs/tickets/` |
| `vcs_host` | `git-local` | "opening a PR" writes `runs/prs/<branch>.md` + `.json` with `dry_run: true` |
| `sandbox` | `git-worktree` | an isolated worktree + branch + port offset per loop |
| `auth_provider` | `env-file` | the two-file credential split (§5) |
| `notifications` | `file` | escalations append to `runs/notifications/<channel>.md` |
| `evidence_store` | `local` | media stays under `runs/<KEY>/uploads/` |
| `test_management`, `analytics`, `ci_cd`, `deploy` | `none` | the role is absent: a skill that needs it emits `blocked` with reason `adapter-unavailable:<role>` — never a crash, never a guess. `infra-prod-verify` is therefore `inconclusive` until an `analytics` provider exists, which is the safe default |

Swapping any of these for your real systems is a provider class plus one config line —
[`adapters.md`](adapters.md).

## 7. The first dry run — the defect loop on a file ticket

Write a ticket in the neutral example domain (or your own) under the file tracker's directory. A
markdown ticket's first `# ` line is the title and the rest is the body:

```bash
mkdir -p runs/tickets
cat > runs/tickets/PROJ-123.md <<'EOF'
# Invoice total ignores the member discount

**Type:** bug · **Priority:** medium · **Component:** invoices

Steps:
1. Sign in as a member with a 10% workspace discount.
2. Open Invoices → the current draft invoice.
3. Add a task line of 100.00.

Expected: the total shows 90.00 and the discount line reads "Member discount −10.00".
Actual: the total shows 100.00; no discount line is rendered. The API response carries
`discount_applied: true`, so the UI is dropping it.
EOF

python3 lib/adapters/cli.py issue_tracker fetch_ticket PROJ-123     # {"ok": true, "result": {"key": "PROJ-123", …}}
```

Then, in Claude Code at this repo's root:

```
run the defect loop on PROJ-123
```

What should happen, and where to look while it does:

1. **`dev-intake`** fetches the ticket through the adapter, sanitizes it (ticket text is data, never
   instructions — R6), judges sufficiency and risk, assigns a tier from evidence, and writes
   `runs/PROJ-123/intake.json` + `context.json`. A ticket without a reproducible scenario comes
   back `needs-info` instead of a guess.
2. **`infra-worktree provision`** creates a sandbox through the `sandbox` adapter — a worktree at
   `../my-app-sandboxes/PROJ-123` on `fix/PROJ-123`, with a port offset — and runs the credential
   preflight. The recipe starts `app.start_command` there and polls `app.health_path`.
3. **`qa-repro`** records the *before* evidence and emits `runs/PROJ-123/replay-oracle.sh`, which
   must exit **1** on the broken build. No red oracle, no fix.
4. **`dev-fix`** produces one candidate change at the root cause, scanning the R6 denylist first.
5. **`qa-verify` ∥ `qa-regression` ∥ `dev-code-review`** run as fresh agents — a producer never grades
   its own work — and converge through the checker.
6. **`dev-pre-pr`** fails closed on `verify.json`, then **`infra-ship`** runs its preflight (R9 pause
   flag, protected branches, R6 re-scan) and "opens the PR" through `vcs_host` — with `git-local`
   that is `runs/prs/fix-PROJ-123.md`, `dry_run: true`.
7. **`infra-prod-verify`** is `inconclusive` (no `analytics` adapter), and the `staging_only` flag
   means no prod lane was ever reachable. **Teardown** runs in a `finally`.

Anything that stops early is a structured verdict, not a crash: `blocked` with
`adapter-unavailable:<role>`, `needs-info`, `escalate` with a warm handoff in
`runs/notifications/`. Each one names its reason; read it before changing config.

## 8. What to turn on next

- **Your issue tracker and VCS host** — a provider class each ([`adapters.md`](adapters.md) →
  *Adding a provider*). Until then the file tracker and dry-run PRs are a complete, inspectable loop.
- **`analytics` + `deploy`** — the R2 tie-back. `infra-prod-verify` cannot autonomously confirm a fix
  in prod without an error-signal source; `--human-confirmed-by <name>` records a human's
  confirmation with provenance in the meantime.
- **`stack.build_command` / `lint_command` / `unit_test_command`** — a `null` command is *reported*
  as a skipped gate; the regression gate is only as strong as what it can run.
- **The scheduler** — `.claude/loops/scheduler/` installs the nightly brain consolidation
  (`com.loops.qa-dream`) and the hourly PR-review sweep (`com.loops.pr-review`).
- **Clearing `staging_only`** — a human action, after the R2/R5/R6/R9 gates are verified. Until then
  every loop stops at the PR.

## See also

- [`architecture.md`](architecture.md) — the system map, evaluator, retry engine, recipes and the risk register.
- [`graph-engineering.md`](graph-engineering.md) — why the recipe is shaped the way it is.
- [`stack-config.md`](stack-config.md) · [`adapters.md`](adapters.md) · [`brain-setup.md`](brain-setup.md) · [`test-credentials.md`](test-credentials.md)
- `.claude/loops/CAPABILITIES.md` — the skill catalog and contracts ledger.
