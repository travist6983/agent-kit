# Setups: personal machine vs. a new codebase

This kit is used in two kinds of setup:

- **Personal:** your own projects on your own machine, with a brain database on that machine.
- **New codebase:** a job or client project in a different repository, with a different team, Claude account and brain database.

Both run the same kit. What changes is the **overlay** (your config), the **brain** (the memory database) and the **Claude account**. This page explains the pieces, compares the two setups side by side, and walks through connecting a fresh project.

> **Status.** Some pieces described here are still being built. They are marked **(planned)**, and the checklist at the end shows what's done.

---

## 1. The pieces

```
┌──────────────────────────── agent-kit (this repo) ────────────────────────────┐
│ skills/ loops/ adapters/ contracts/   the framework: defect, feature, review  │
│ kb CLI                                the only way anything reads/writes a brain│
│ mods/                                 Claude Code mods (marketplace agent-kit) │
│ templates/overlay/  (planned)         starting point for a new overlay         │
└───────────────────────────────────────────────────────────────────────────────┘
          used by ▼                                   used by ▼
┌──── personal overlay ─────┐                ┌──── client overlay ──────┐
│ kb.config → local brain   │                │ kb.config → client brain │
│ projects/<repo>/          │                │ projects/<repo>/         │
│ mods/ personal-only       │                │ mods/ client-only (opt.) │
└────────────┬──────────────┘                └────────────┬─────────────┘
             ▼                                            ▼
   Postgres on this Mac                         Postgres + pgvector the
   (scope: single-machine)                      client hosts (shared)
```

| Piece | What it is | Shared? |
|---|---|---|
| **Kit** | This repo: the skills, adapters, contracts, the `kb` CLI and the generic mods. It holds no person, company or project data, and the leak check (`.claude/loops/tests/test_no_leaks.sh`) enforces that. | Yes. Clone it anywhere. |
| **Overlay** | A small folder (ideally its own repo) holding one owner's config: the brain pointer, per-project settings, and mods that only make sense for that owner. | No. One per owner: you, client A, client B. |
| **Brain** | A Postgres + pgvector database holding what the agents learn: the app map, expectations, flaky tests, fix lessons and general lessons. It is reached only through the `kb` CLI. | One per overlay. Brains never share data. |
| **Mods** | Claude Code plugins: status lines, panes, guards. The kit's mods are generic; overlay mods are owner-specific. | Kit mods yes; overlay mods no. |
| **Claude account** | The Claude Code config folder a session runs under (`~/.claude`, or another one via `CLAUDE_CONFIG_DIR`). Plugins, settings and mod data live in it. | One per context: personal, each employer. |

### How the kit finds its config

The kit stays the same everywhere; only the config it reads changes. Lookup order:

1. `AGENT_KIT_OVERLAY`, a path to an overlay folder set in the account's settings `env` block or the shell.
2. That overlay's `projects/<repo name>/` folder for the project you're in, where `<repo name>` is the git repo's root folder name.
3. The kit's commented examples under `.claude/loops/`. These are examples only and never used for a real run.

So `kb.config.json`, `stack.config.json` and `conventions.json` for real projects never live in the kit.

---

## 2. Side by side

| | **Personal (this Mac)** | **New codebase (job / client)** |
|---|---|---|
| Machine | Your Mac | Usually the employer's or client's laptop. Could be yours, with a separate account. |
| Claude account | Your personal config folder: `~/.claude`, or e.g. `CLAUDE_CONFIG_DIR=~/.claude-me` behind an alias | That job's account: `~/.claude` on their machine, or its own `CLAUDE_CONFIG_DIR` |
| Kit | Your clone, e.g. `~/repos/agent-kit` | `git clone git@github.com:travist6983/agent-kit` (or install the mods from GitHub only) |
| Overlay | Your overlay repo, e.g. `~/repos/my-agents` | A new overlay from `templates/overlay/` (planned), e.g. `~/work/<client>-agents`. Keep it in the client's git, not yours. |
| Brain host | Postgres on your machine, e.g. database `personal_brain` | Postgres + pgvector the client provides: a managed service or their server. Never your Mac. |
| Brain `scope` | `"scope": "single-machine"`: a local database is allowed because you declared it. | `shared`, the default: local or socket hosts are refused, so the brain can't silently land on one laptop. |
| Who else uses the brain | Only you | The client's team, CI and scheduled agents. Everyone sees the same lessons. |
| Embeddings | Your OpenAI key in the overlay's `kb.secret.env`, with a hard spend limit | The client's key, from their secret store |
| Projects in one brain | All your own repos, separated by the `project` column, plus a `global` group for cross-project lessons | Usually one product, or a few of theirs |
| Kit mods | All six, user scope | All six, user scope, installed under the job's account |
| Overlay mods | Your own marketplace in the overlay, e.g. a mod for one project, installed at local scope in that repo | Whatever that client needs, or none |
| Adapters (tickets, PRs, notifications) | File-based tickets or GitHub issues; GitHub via `gh` | Whatever they use. A missing provider is written once in `lib/adapters/providers/` and reused. |
| Backups | Nightly `pg_dump` to the overlay's `backup/` (planned) | The client's database backups |
| What must never cross over | Personal lessons never go into a client brain, and client code, lessons or credentials never come onto this Mac's brain or overlay. To move a lesson, copy it by hand, deliberately. ||

### Why a client brain is "shared" and the personal one isn't

The framework's rule is that a brain must be reachable by everyone who works on the product: teammates, CI, scheduled agents. Otherwise lessons pile up on one laptop and the next run starts from zero. For a team that rule is right, so client setups keep it.

For one person on one machine it's just overhead, so the personal overlay sets `"scope": "single-machine"`. That is an explicit statement in config, not a workaround, and the brain mod displays it.

---

## 3. Adding a project you already have to the personal setup

Use this for any repo of yours on the machine.

1. **Open a session in the project** under your personal account. The kit mods are already active: ship, dev-servers, burn-meter, secret-catcher, traffic-control and pr.
2. **(planned) Run `/agent-kit init`.** It reads the repo (package.json, pyproject, CI workflows, CLAUDE.md) and drafts `<overlay>/projects/<repo>/`:
   - `stack.json`: build, lint, unit-test and E2E commands; default branch; branch prefixes; environments (`local` URL, staging URL); ticket key pattern.
   - `conventions.json`: house rules, the blast-radius denylist (migrations, secrets, auth, infrastructure, CI), shared-core paths.
3. **Check the draft.** Commands must actually run, and URLs must be the ones you use.
4. **Give the project a CLAUDE.md** if it doesn't have one. The agents read it as the project's house rules (`stack.repo_guidelines_file`).
5. **Save its run recipes** the first time you use them:
   - `/up` starts the dev servers and saves them.
   - `/ship` deploys and saves the deploy command.
6. **Optional, for browser repro and verify:**
   - `.env.e2e` (tracked: role names and URLs) and `.env.e2e.local` (gitignored: passwords) for the test accounts. See [test-credentials.md](test-credentials.md).
   - `playwright-cli` on PATH.
7. **Dry run.** Give the defect loop one small bug as a file ticket. It runs in a worktree, verifies, and stops at "PR opened". Nothing deploys while the brain's `staging_only` flag is on.

The project now has its own lessons in the personal brain under `project = <repo>`. It also sees `global` lessons such as tool quirks and your preferences.

---

## 4. Setting up a new codebase (job / client)

Use this when the code, team and data belong to someone else.

### 4.1 On their side, before you start

- **A Postgres database with pgvector**, reachable by everyone who will run agents (you, teammates, CI). Any managed Postgres works.
- **A database user** for the agents, with read/write on that database only.
- **An embedding API key** (OpenAI, or a compatible endpoint), paid for by them.
- **Access to their ticket system, code host and CI**, through the CLIs or APIs the adapters use.
- **Agreement that agents may open PRs**, with deploys staying human-gated until the safety gates are verified (see CLAUDE.md, "Safety").

### 4.2 Steps

1. **Claude account.** Use the job's account. On a shared machine, give it its own config folder:
   `alias claude-<client>='CLAUDE_CONFIG_DIR=~/.claude-<client> command claude'`
2. **Kit mods:**
   `/plugin install <mod> --marketplace travist6983/agent-kit` for each mod, or clone the kit and add `mods/` as a local marketplace. The mods keep their data in that account's config folder, so nothing mixes with your personal data.
3. **Overlay (planned: `templates/overlay/`).** Copy the template to a folder in the client's git. Fill in:
   - `kb.config.json`: `database_url` (no password), `auth.mode` (`password`, `url_env` or `token_command`), `embedding`. Leave `scope` unset, so it stays shared.
   - `kb.secret.env` (gitignored): the database password or token command input, and the embedding key.
4. **Point the account at the overlay:** set `AGENT_KIT_OVERLAY` in that account's `settings.json` `env` block.
5. **Create the brain schema once, for everyone:** `kb schema --apply`. Then check that you're on the shared brain:
   - `kb flag --name staging_only --status` should read `true`.
   - A teammate's machine should see the same `kb metrics`.
6. **Adapters.** In `projects/<repo>/stack.json`, choose a provider per role:
   - `issue_tracker`
   - `vcs_host`
   - `notifications`
   - `sandbox` (git worktrees)
   - `evidence` (local folder)

   If their tool has no provider yet, write one in `lib/adapters/providers/` against the contract in [adapters.md](adapters.md). That's the only kit change a new job should need. Keep it vendor-neutral and contribute it back.
7. **Onboard each repo** as in section 3, steps 2–7, with the overlay being the client's.
8. **CI or scheduled runs (optional):** the scheduler templates in `.claude/loops/scheduler/` need the runner's Claude binary, the overlay path and the secret env file on that runner.

### 4.3 What stays out of the client setup

- Your personal overlay, personal brain and personal-only mods.
- `global` lessons from your personal brain. Copy one by hand if it truly applies.
- Your API keys. The client pays for and owns its embedding key and database.

---

## 5. Data and secrets: where everything lives

| Thing | Personal | Client |
|---|---|---|
| Brain rows (lessons, app map, expectations…) | Postgres `personal_brain` on this Mac, plus nightly dumps in `<overlay>/backup/` (gitignored) | The client's database |
| Embedding key, database password | `<overlay>/kb.secret.env` (gitignored, mode 600) | Client overlay's `kb.secret.env`, or their secret manager |
| Project settings | `<overlay>/projects/<repo>/` | Client overlay `projects/<repo>/` |
| Keys pasted into a prompt by mistake | `secret-catcher` moves them to `<config>/secrets/<project>.env` | Same mod, in that account's config folder |
| Mod data (recipes, locks, budget) | Your personal config folder | That account's config folder |
| Test-account passwords | `<project>/.env.e2e.local` (gitignored) | Same, in the client repo |

---

## 6. Checklist

### A personal setup, from nothing

1. Clone the kit and install its mods under your personal account (`mods/README.md`).
2. Create your overlay repo: `kb.config.json`, gitignored `kb.secret.env`, `projects/`, optional `mods/`.
3. Set `AGENT_KIT_OVERLAY` in your account's `settings.json` `env` block.
4. Install Postgres with pgvector and `psycopg2`. Create the database, `CREATE EXTENSION vector`, then `kb schema --apply`.
5. In `kb.config.json`, set `"scope": "single-machine"` and the embedding provider. Put the embedding key in `kb.secret.env`, with a spending limit on the key.
6. **(planned)** Set up a nightly `pg_dump` backup of the brain.
7. Onboard each project as in section 3.

### Connecting any fresh project: what you'll need

**Every project:**
- [ ] A git repo with a default branch, and a remote if you want PRs.
- [ ] The build, lint and unit-test commands, and they pass on a clean checkout.
- [ ] A CLAUDE.md, or other guidelines file, with the project's house rules.
- [ ] `projects/<repo>/stack.json` + `conventions.json` in the overlay, including the blast-radius denylist.
- [ ] `gh auth login` (for the pr mod, PRs and checks).

**For browser repro and verify:**
- [ ] The local app URL (and staging, if any).
- [ ] Test accounts per role: `.env.e2e` with roles and URLs, `.env.e2e.local` with passwords.
- [ ] `playwright-cli` on PATH; the Claude in Chrome extension for live driving.

**For a new codebase (client) only, in addition:**
- [ ] A shared Postgres + pgvector database, a database user, and the DSN.
- [ ] An embedding key owned by the client.
- [ ] Its own Claude account / config folder, and `AGENT_KIT_OVERLAY` set in it.
- [ ] Adapter providers for their ticket system and code host (write one if missing).
- [ ] `kb schema --apply` run once, then confirm `staging_only` is `true` before any loop runs.
