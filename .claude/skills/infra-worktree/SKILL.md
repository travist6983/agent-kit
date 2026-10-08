---
name: infra-worktree
description: >-
  Provision and tear down an isolated branch + port sandbox (plus a database copy when the
  target repo supplies a clone command), so a loop never touches the developer's checkout and
  parallel loops can't collide. Trigger for "spin up a sandbox for PROJ-123", "provision a
  worktree for the fix", "open an isolated environment for this loop", or as the environment
  step every dev-loop recipe runs right after intake. A thin wrapper on the `sandbox` adapter
  (git-worktree by default) that adds the loop concerns: per-slot port offsets derived from
  app.url / app.api_url, a lock-protected race-safe registry, a concurrent-sandbox cap, port
  reclamation, a test-credential preflight, and crash-safe idempotent teardown — ALWAYS called
  in a finally. Returns started:false + next_steps; the recipe starts the app and proves
  reachability. Does NOT fix, verify, or ship.
allowed-tools: Read, Write, Bash, ToolSearch
---

# infra-worktree — the sandbox primitive every loop needs

Every loop runs code, but the spine skills assume an environment already exists. `infra-worktree`
is the primitive that creates one and — critically — **guarantees it's torn down**, even when a run
crashes mid-fix. It is a thin wrapper on the **`sandbox` adapter** (`lib/adapters`, role `sandbox`;
`git-worktree` ships as the default provider, chosen in `.claude/loops/stack.config.json`) and adds
the autonomous-loop concerns on top: R4 isolation, a resource cap, port reclamation, a credential
preflight, crash-safety. Read `references/sandbox-lifecycle.md` — the isolation model, the
crash-safety rule, and the real-vs-dry-run prerequisites all live there.

## What this block does NOT do (atomic boundary)
It does **not** reproduce, fix, verify, or ship. It provisions a sandbox, hands the recipe its
`urls.app` + `dir`, and tears it down. That's the whole job.

## Inputs / Outputs
**Inputs:** a sandbox `--key` (ticket key + slug, e.g. `PROJ-123-invoice-total`) with an optional
`--type fix|feature`, **or** the full `--branch` dev-intake named (mapped back onto key + type by
stripping a `project.branch_prefixes` prefix); optional `--base` (default `project.default_branch`),
`--cap` (default `adapters.sandbox.config.max_concurrent`), `--app-root` (default `app.repo_dir`,
or `LOOP_APP_ROOT`), `--dry-run`. Single config keys can be overridden per process with
`LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__<KEY>` / `LOOPS_CFG__ADAPTERS__AUTH_PROVIDER__CONFIG__<KEY>`;
`LOOP_SANDBOX_STATE=<dir>` points the registry at `<dir>/sandboxes.json` (tests).
**Output:** `provision` → `{ok, reused, dry_run, key, dir, branch, slot, ports{app, api?, offset},
urls{app, api?}, db, started:false, created_at, next_steps[], app_root, credentials{provider, file,
source, candidates[]}}` — the adapter's normalized `Sandbox` plus the wrapper's fields; `teardown` →
`{ok, key, removed, removed_worktree, removed_registry, db, idempotent:true}`; `list` → `{sandboxes[]}`.

Ports are **`app.url` / `app.api_url`'s port + slot × `port_stride`** (`port_base` when a url has no
port), scheme and host copied from the url. Slots run **1..max_concurrent — never 0**, because slot 0
IS the developer's checkout. The stride must be wider than the spread between the app's base ports,
or slot N's api port equals slot N−1's app port; the default stride of 10 covers any two ports fewer
than 10 apart. Exit codes: 0 ok/reused · 1 usage · 2 `sandbox-cap-reached` · 3 `sandbox-create-failed`
or blocked (`adapter-unavailable:sandbox`).

## Procedure
### 0. Reclaim ports before allocating one (run this FIRST)
```bash
python3 scripts/prune.py                      # dry-run: reports squatters, merged + aged trees
python3 scripts/prune.py --apply              # kills squatters, removes merged + aged trees
```
The adapter picks the first slot **not in the registry**. A sandbox that is not in the registry is
therefore invisible to allocation *while still holding real TCP ports*: a leftover tree whose
processes still listen on the next slot's ports makes a fresh loop either hand-patch its ports and
registry, or abandon isolation and run in the developer's own checkout — the exact failure this
skill exists to prevent.

Order is deliberate: **port reclamation first**, then merged-branch and age-out pruning. A tree with
**uncommitted changes is never pruned** — it is reported and the script exits 1, because someone's
work is in there.

**Dry-run by default.** Killing a process and removing a worktree are destructive, so nothing
happens without `--apply`. A registered, live sandbox is never touched.

> **Falling back to the developer's checkout is a logged decision, never a shrug.** If a sandbox
> cannot be provisioned, record `fallback_reason` in `context.json.environment` and **refuse** when
> any other loop is active — a loop that shares the developer's checkout ends up killing the
> developer's own running app to free a port. With `adapters.sandbox.provider: none`, loops
> serialize under the registry lock and the fallback is the only mode; it is still logged.

### 1. Provision at the start of the run
```bash
bash scripts/sandbox.sh provision --branch "$BRANCH" [--cap 3] [--base <project.default_branch>] [--app-root <app.repo_dir>] [--dry-run]
```
- `sandbox-cap-reached` (exit 2) → the recipe **queues or escalates**; do not force another environment.
- **`started` is `false` by design:** provisioning creates the worktree (and runs
  `db_clone_command` when the target repo configured one) — it does **not** install, migrate, or start
  servers. The **recipe** runs the returned `next_steps` (`cd dir` → `app.start_command` on
  `ports.app`), then **proves reachability** before any browser step (`curl -fsS "$urls_app$health_path"`
  until it answers 2xx). Never hand `urls.app` to `qa-repro`/`qa-verify` as if live without that proof.
- **Sign-in credentials come from the `auth_provider` adapter, not from the worktree.** The
  adapter reads `env_file` (tracked: usernames, hints) then `local_env_file` (gitignored:
  passwords), later wins, then the process environment. Provisioning **reports** which of those
  files exist (`credentials.file` = the first existing of `local_env_file`, `env_file`;
  `credentials.candidates` = both resolved paths) and copies nothing: the files live beside
  `stack.config.json`, not in the app checkout, so a worktree is not missing them.

  **Why two files.** A sandbox is always local (`urls.app`) and authenticates against whatever
  identity provider the target app runs with locally, against a copy of the local dev database.
  Credentials minted for a shared environment (`app.environments.staging`) belong to a *different
  identity store* and a *different database* — their users need not exist in a sandbox at all.
  Keeping the tracked file and the local file apart lets the loop have local users without
  destroying the shared-environment credentials other skills still depend on.

  The recipe turns this into `E2E_ENV_FILE` for every oracle run
  (`dev-loop-defect/references/recipe-engine.md`, *Environment*): the file that holds the
  password for the role the run needs, normally `credentials.file`. With no credentials for that
  role and no saved session, a signed-in oracle BLOCKs (exit 2). No browser storage state is
  copied — the oracle keeps its own sessions in `REPLAY_STATE_DIR`.
- **Prove those credentials BEFORE the loop spends anything on them (step 1b).** A file existing
  is not evidence that it works.

### 1b. Oracle credential preflight — fail at minute one, not at verify
```bash
python3 scripts/oracle_preflight.py --app-url "$URLS_APP" [--roles owner,admin] \
        [--env-file "$E2E_ENV_FILE"] [--live --oracle <evidence>/<KEY>/replay-oracle.sh]
# exit 0 ok · 1 credentials-unusable / credentials-missing / blocked (escalate) · 2 unreadable (fail closed)
```
The replay oracle signs in with the credentials the `auth_provider` adapter supplies for a role
(`E2E_<ROLE>_USERNAME` / `_PASSWORD` under the default `credential_env_pattern`). If it cannot, it
exits **2 = BLOCKED**, `qa-verify` returns `blocked`, and `dev-pre-pr` blocks the ship — all correct,
and all *after* the run has already paid for intake, provisioning, the repro walk and the fix.

The preflight asks the adapter for each role the run needs (default: the first of
`adapters.auth_provider.config.roles`; compare role names case-insensitively) and reports the
variable names, never the values.

**BLOCKS** (mechanical — the oracle provably cannot proceed):
- the adapter has **no credentials for a role this run needs** — a missing file, a missing
  variable, or an empty value (the oracle blocks on exactly this);
- `adapters.auth_provider.provider` is **`none`** — blocked with reason
  `adapter-unavailable:auth_provider` (a surface that needs no sign-in runs the oracle with `ROLE=none`);
- an `--app-url` whose host is the configured **production** environment
  (`app.environments.prod.url`), which the oracle refuses anyway.

**WARNS** (heuristic — a real risk, but not a mechanism):
- **the credentials were minted for a different host.** The oracle takes its target from
  `APP_URL` and reads *only* the per-role username/password; a recorded `E2E_BASE_URL` (or a
  role's `*_BASE_URL` extra) only says which environment the credentials were minted for. So
  `E2E_BASE_URL=https://staging.example.com` against a `localhost` sandbox does not itself break
  anything; it signals that the *identity store* probably differs — a user minted remotely is
  rejected outright by the local identity provider, and that is an account problem, not a URL
  problem. Blocking on a heuristic manufactures false escalations.

`--live` settles it by actually signing in; that is the only conclusive check, and it is worth the
few seconds before a long run. On a mechanical block the recipe **escalates with the preflight's
`remedy`** rather than starting a browser step.

- Then pass `urls.app` to `qa-repro`/`qa-verify` and `dir` to `dev-fix`.
- The registry is updated under an **exclusive lock** in one atomic transaction (cap check + slot
  allocation + write), and branch names reach the adapter via the environment — so parallel
  provisions can't race (R4) and a quote in a ticket-derived branch name can't inject (R6).

### 2. Run the loop inside the sandbox
The loop's repro/fix/verify/regression all target the sandbox's `urls.app` + `dir`. The developer's
checkout is never touched (R4 — and parallel loops each get their own slot/ports/branch, and their
own database when a clone command is configured).

### 3. ALWAYS tear down (in a `finally`, not the happy path)
```bash
bash scripts/sandbox.sh teardown --branch "$BRANCH" [--app-root <app.repo_dir>] [--dry-run]
```
Call this **unconditionally** at the end of the run — including on crash/escalation. It's
idempotent (`removed:0` if already gone), so a double-teardown is harmless, and it **never exits
non-zero**: an incomplete teardown is reported as `{ok:false, error:"teardown-incomplete",
failure_class:"leaked-sandbox"}` so the finally keeps running and the leak is visible. This is what
keeps worktrees, database copies (`db_drop_command`), and server processes from leaking.

## Success criteria & failure categories (declared up front)
**Success:** a loop gets an isolated `urls.app`/branch/dir (and database, when configured); the dev
checkout is never modified; after a normal run *and* a forced crash, **zero leaked**
worktrees/databases/processes; parallel sandboxes don't collide; the sandbox cap holds.
**Failure categories:**
- `sandbox-cap-reached` — at the sandbox cap → queue/escalate (not an error to retry into).
- `sandbox-create-failed` — the adapter could not create the worktree or the configured
  `db_clone_command` failed (bad base ref, missing `app.repo_dir`, database down) → escalate; the
  adapter compensates, so nothing leaks. `adapter-unavailable:sandbox` is the same exit with a
  structured `blocked` — the recipe serializes or records a logged fallback.
- `leaked-sandbox` — a teardown didn't run or did not complete (the bug this skill exists to
  prevent — always use `finally`, and read `teardown-incomplete`).
- `credentials-unusable` — the sandbox came up but the oracle cannot sign into it (step 1b). Caught
  here it costs a minute; missed, it surfaces as a `qa-verify` `blocked` verdict after the whole
  front half of the run, and that verdict says nothing about the fix.
- `port-squatted` — provisioning succeeded but the allocated ports are held by a **stale**
  sandbox's processes. The next loop given that slot starts against a dead tree's app and either
  hand-patches ports or abandons isolation. See step 0.

## Knowledge brain (qa-kb)
Writes nothing canonical. May record a `sandbox` **metric** (provision/teardown counts, cap hits) for
the meta-monitor (R5/R8). The registry (`<evidence.root>/sandboxes.json`) is runtime state, not knowledge.

## Key principles
1. **Isolate per loop (R4).** Unique branch + slot/ports (+ database when configured) → parallel
   loops can't collide.
2. **Bound the fleet.** A concurrent-sandbox cap, enforced; at cap, queue/escalate — never spawn
   unbounded. (A RESOURCE bound — distinct from the R5 per-loop spend breaker in `loop-limits.json`.)
3. **Teardown is sacred.** In a `finally`, idempotent, crash-safe. A leaked sandbox is the cardinal sin.
4. **Wrap, don't reinvent.** The `sandbox` adapter does the worktree/port/registry work and runs the
   target repo's own database hooks; this adds the loop concerns. Another sandbox mechanism is a
   provider swap in `stack.config.json`, not a skill change.
5. **Atomic.** Own the environment lifecycle; never fix, verify, or ship.
