# infra-worktree — sandbox lifecycle, isolation, and crash-safety

## What it wraps
The **`sandbox` adapter** (`lib/adapters`, role `sandbox`) does the heavy lifting. The default
provider, `git-worktree`, implements the role as:
- `provision(key, ticket_type, base)` — `git worktree add <app.repo_dir>/../<project.name>-sandboxes/<key>`
  on branch `project.branch_prefixes[ticket_type] + key` (forked from `base` when the branch is new),
  allocates slot `1..max_concurrent` under an `fcntl` lock in `<evidence.root>/sandboxes.json`, shifts
  the port of `app.url` / `app.api_url` by `slot × port_stride`, and runs the target repo's
  `db_clone_command` (template vars `{key} {slot} {dir} {db}`) inside the worktree when one is
  configured. Returns the normalized `Sandbox`.
- `teardown(key)` — runs `db_drop_command` when configured, removes the worktree, prunes, drops the
  registry entry. A missing dir or entry is not an error.
- `list()` — the registry entries plus an `exists` flag.

`infra-worktree` does **not** reinvent these. It adds the autonomous-loop concerns on top, and it
calls the adapter the way every skill does — by role, never by provider. A target repo with its own
sandbox tooling (a container per branch, a cloud dev environment) registers a provider under
`adapters.sandbox` and this skill does not change.

## What infra-worktree adds (`scripts/sandbox.sh`, `scripts/prune.py`, `scripts/oracle_preflight.py`)
| Concern | How |
|---|---|
| **R4 — no collisions** | each sandbox gets a **unique slot, 1..max_concurrent, never 0** (0 is the developer's own checkout). Ports are the app's configured base ports + slot × `port_stride`; the stride must exceed the spread between base ports (two ports one apart cross-collide under a stride of 1 — slot N's api port is slot N−1's app port). The registry transaction runs under an exclusive lock, so two PARALLEL provisions can't pick the same slot or overrun the cap. |
| **Bounded resources** | a **concurrency cap** (`adapters.sandbox.config.max_concurrent`, `--cap` overrides). At cap, `provision` returns `sandbox-cap-reached` (exit 2) — the recipe queues or escalates, never spawns unbounded environments. (A resource bound, distinct from the R5 per-loop spend breaker in `loop-limits.json`.) |
| **Branch fidelity** | `--branch fix/PROJ-123-invoice-total` is mapped back onto `(key, type)` by stripping a `project.branch_prefixes` prefix, so the branch dev-intake named is the branch the sandbox gets. A prefix the config does not know is a usage error (`branch-prefix-unknown`), never a silently different branch. |
| **Idempotent provision** | provisioning a key that already has a live sandbox **reuses** it (`reused:true`), never duplicates. |
| **Crash-safe teardown** | `teardown` cleans up regardless of registry state and **never exits non-zero**; an incomplete teardown is reported as `teardown-incomplete` / `leaked-sandbox` so the finally keeps running and the leak is visible. Tearing down twice is harmless (`removed:0`). |
| **Freed-slot reuse** | a torn-down sandbox's slot returns to the pool. |
| **Compensation** | a failed create (worktree or `db_clone_command`) removes its own worktree and registry slot — nothing leaks from a failure. |
| **Sign-in credentials** | provisioning **reports** the `auth_provider` adapter's credential files (`credentials.file` = the first existing of `local_env_file`, `env_file`; `credentials.candidates` = both resolved paths) and copies nothing — the files live beside `stack.config.json`, not in the app checkout. No browser storage state is involved; a missing file never fails provision, it fails the **preflight**. |
| **Credential preflight** | `oracle_preflight.py` asks the adapter for every role the run needs and blocks mechanically (missing file / variable / empty value, provider `none`, production target); a credentials-minted-elsewhere host mismatch is a warning; `--live` is the only conclusive check. |
| **Port reclamation** | `prune.py` scans every slot's ports for a listener whose cwd is a sandbox dir with no registry entry — a squatter the registry-based allocator cannot see — and reports it; `--apply` kills it, then prunes merged and aged-out trees. A dirty tree is never pruned. |
| **Injection-safe (R6)** | every dynamic value (branch, key, cap, paths) reaches python through the environment, never string interpolation, so a quote in a ticket-derived branch name cannot break the script or corrupt the registry. |

State lives in `<evidence.root>/sandboxes.json` (`runs/` by default — gitignored runtime state).
Tests point it elsewhere with `LOOP_SANDBOX_STATE=<dir>` (the registry becomes `<dir>/sandboxes.json`)
or `adapters.sandbox.config.registry_file`.

## The cardinal rule: teardown in a `finally`, never a happy-path step
The whole reason this exists is the crashed run. A loop that dies mid-`dev-fix` must not leak a
worktree, a database copy, or a server process. So the recipe wraps the run:
```
provision → (repro → fix → verify → …)  ALWAYS  teardown   # teardown runs even if the middle throws
```
`teardown` is idempotent and never exits non-zero precisely so the `finally` can call it unconditionally.

## --dry-run vs real provisioning
`--dry-run` keeps the registry/cap/slot bookkeeping and the (cheap, local) `git worktree add`, but
suppresses `db_clone_command` / `db_drop_command` and marks the result `dry_run:true` — that is how
the lifecycle is tested without a database. **Real** provisioning needs, on the machine that runs the
loop: the app checkout at `app.repo_dir` (or `--app-root`), a reachable `base` ref (fetch first if
the branch only exists on the remote), and whatever `db_clone_command` needs (a database server,
the dev database to copy). Those are environmental prerequisites — `docs/getting-started.md` covers
them. Until they're wired on the loop runner, recipes use `--dry-run` or a single shared dev server
(logged as a fallback, forbidden while another loop is active).

With `adapters.sandbox.provider: none`, `provision` emits a structured `blocked`
(`adapter-unavailable:sandbox`, exit 3), `teardown` is a no-op that still exits 0, and `prune.py`
reports blocked (exit 2): loops then serialize under the registry lock and run in the developer's
own checkout as a logged decision.

## Output contract
`provision` → `{ok, reused, dry_run, key, dir, branch, slot, ports{app, api?, offset}, urls{app, api?},
db, started, created_at, next_steps[], app_root, credentials{provider, file, source, candidates[]}}`
(or `{ok:false, error, …}`). `ports.api` / `urls.api` appear only when `app.api_url` is configured.
`db` is the clone command's result (`{command, returncode, name, stdout_tail}`) or `null`. `started`
is always `false`: provision does NOT install/migrate/start — the recipe runs `next_steps`
(`cd dir` → `app.start_command` on `ports.app`) and then PROVES reachability (probe
`urls.app` + `app.health_path` until it answers 2xx) before handing `urls.app` to any browser skill.
`teardown` → `{ok, key, removed, removed_worktree, removed_registry, db, idempotent}`. `list` →
`{sandboxes[]}`. Exit codes: 0 ok/reused/idempotent-teardown · 1 usage · 2 `sandbox-cap-reached` ·
3 `sandbox-create-failed` or blocked (the failed provision's slot is compensated/removed).

## Worked example (the neutral domain)
Ticket `PROJ-123` ("Invoice total rounds the wrong way"), `stack.config.json` with
`app.url = http://localhost:3000`, `app.api_url = null`, `port_stride = 10`, `max_concurrent = 3`:

```bash
bash scripts/sandbox.sh provision --branch fix/PROJ-123-invoice-total
# → {"ok":true,"key":"PROJ-123-invoice-total","branch":"fix/PROJ-123-invoice-total","slot":1,
#    "ports":{"offset":10,"app":3010},"urls":{"app":"http://localhost:3010"},"started":false,
#    "next_steps":["cd …/my-app-sandboxes/PROJ-123-invoice-total","start the app on port 3010: npm run dev",
#                  "prove reachability: curl -fsS http://localhost:3010/","teardown in a finally: …"],
#    "credentials":{"provider":"env-file","file":"…/.env.e2e.local","source":"local_env_file",…}}
python3 scripts/oracle_preflight.py --app-url http://localhost:3010 --roles owner
bash scripts/sandbox.sh teardown --branch fix/PROJ-123-invoice-total      # in the finally
```
