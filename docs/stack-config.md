# `stack.config.json` — every key

`.claude/loops/stack.config.json` is the one tracked file that tells the framework what it is
working on: the target repo, how to run and test it, which provider serves each external role,
where evidence goes, and the knobs the kept integrations read. It holds **no secrets** — where a
provider needs one, a key names the *environment variable* that holds it. A fully commented copy,
`stack.config.example.json`, sits next to it and is itself loadable (the loader strips `_comment*`
keys).

The loader is `lib/adapters/config.py`. From Python, `cfg("app.url")`; from the shell,
`python3 lib/adapters/cli.py` reads it for every adapter call. Three environment knobs apply to the
file as a whole:

| env var | effect |
|---|---|
| `LOOPS_CFG__<PATH>` | overrides one key for this process: the dotted path upper-cased with `.` → `__` — `LOOPS_CFG__APP__URL`, `LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__PORT_BASE=4000`. Values parse as JSON when they can (`null`, `true`, `3000`, `[…]`), else strings |
| `LOOPS_STACK_CONFIG` | path to a different config file (tests and CI use it) |
| `LOOPS_REPO_ROOT` | the root relative paths resolve against (default: the checkout this package lives in) |

Relative paths in the file (`app.repo_dir`, env files, `evidence.root`) resolve against this repo's
root. Agents never invent keys: a key that is not in this document does not exist.

---

## `version`

Schema version of this file, `"1.0.0"`. Bump only when keys change meaning.

## `project` — identity of the target repo as the loops see it

| key | default | meaning |
|---|---|---|
| `name` | `"my-app"` | Short slug. Names the sandbox directory (`<repo_dir>/../<name>-sandboxes/`) and the default sandbox database (`<name>_<key>`). |
| `ticket_key_pattern` | `"^[A-Z][A-Z0-9]+-[0-9]+$"` | Regex a ticket key must match before intake accepts it. A literal prefix (`^PROJ-[0-9]+$`) is also what the file issue tracker allocates new keys with; a character class falls back to `PROJ`. Also substituted into the conventions scanner's `{{ticket_key_pattern}}` placeholder. |
| `default_branch` | `"main"` | The branch PRs target and sandboxes branch from. Every script takes `--base` defaulting to it; no script carries a literal branch name. |
| `branch_prefixes` | `{"fix": "fix/", "feature": "feature/"}` | Ticket type → branch prefix. `sandbox.provision(key, ticket_type)` builds `<prefix><key>`; unknown types use `fix`. |
| `protected_branch_patterns` | `["main", "release/*"]` | fnmatch globs `infra-ship` refuses to push to or open a PR *from*. |
| `deploy_branch_patterns` | *(absent)* | Optional: globs whose merge triggers a deploy. Treated like protected branches by the ship gate. (See also `deploy.prod_lane_branch_patterns`.) |

## `app` — where the target app lives and how to reach it

| key | default | meaning |
|---|---|---|
| `repo_dir` | `"../my-app"` | The app's git checkout, relative to this repo's root or absolute. `git-local` vcs and `git-worktree` sandbox run git here. |
| `url` | `"http://localhost:3000"` | The app's base URL when started locally. The sandbox adapter derives per-sandbox URLs by shifting this port by `slot * port_stride`. |
| `api_url` | `null` | Optional separate API origin (`null` when the app serves its own API). Shifted the same way for sandboxes. |
| `health_path` | `"/"` | GET path that returns 2xx once the app is up; the recipe polls it after starting a sandbox. |
| `start_command` | `"npm run dev"` | Command the recipe runs inside a sandbox dir to start the app. The adapter never starts it (`Sandbox.started` is always `false`). |
| `environments.<name>.url` | `local` = `app.url`; `staging`, `prod` = `null` | Named environments. Names are yours; skills take them as `--env` arguments (`--baseline-env` / `--candidate-env` for the parity sweep). `null` = that environment does not exist yet. `environments.prod.url`'s host is what the replay oracle refuses to drive. |

## `stack` — how to build, lint and test the target app

| key | default | meaning |
|---|---|---|
| `languages` | `[]` | Informational; the conventions file carries the actual rules. |
| `build_command` | `null` | `null` = the gate is skipped and **reported as skipped**, never silently passed. |
| `lint_command` | `null` | Same. |
| `unit_test_command` | `null` | Same. |
| `test_report.format` | `"junit"` | Format of the machine-readable results `qa-regression` reads after `unit_test_command`. |
| `test_report.glob` | `"**/test-results/*.xml"` | Where to find them, relative to `app.repo_dir`. |
| `conventions_file` | `".claude/loops/conventions.json"` | The standards ruleset `dev-pre-pr` scans added lines with (format: `examples/conventions/README.md`). |
| `repo_guidelines_file` | `"CLAUDE.md"` | The target repo's own working agreement; `dev-fix` and `dev-code-review` read it for constraints. Relative to `app.repo_dir`. |

## `adapters` — one provider per role

Each role is `{ "provider": "<name>", "config": { … } }`. Built-ins: `issue_tracker[file]`,
`vcs_host[git-local]`, `auth_provider[env-file]`, `notifications[file]`, `sandbox[git-worktree]`,
`evidence_store[local]`; `test_management` has none. Any role accepts a third-party class as
`"provider": "python.module.path:ClassName"`. `"provider": "none"` means the role is absent: a skill
that needs it emits `blocked` with reason `adapter-unavailable:<role>`. The role contracts are in
[`adapters.md`](adapters.md); this table is the **config** each built-in provider reads.

| role | default provider | `config` keys |
|---|---|---|
| `issue_tracker` | `file` | `dir` (default `runs/tickets`) — `<KEY>.json` or `<KEY>.md` tickets, `<KEY>.comments.md` logs; `key_prefix` — used by `create_ticket` when `project.ticket_key_pattern` has no literal prefix |
| `vcs_host` | `git-local` | `repo_dir` (overrides `app.repo_dir`); `prs_dir` (overrides `<evidence.root>/prs`) |
| `test_management` | `none` | none shipped; a third-party provider's own keys (env-var **names** for its host and credentials, never values) |
| `analytics` | `none` | none shipped; a provider's own keys |
| `auth_provider` | `env-file` | see the next table |
| `ci_cd` | `none` | none shipped; a provider's own keys |
| `deploy` | `none` | none shipped; a provider's own keys |
| `notifications` | `file` | `dir` (default `runs/notifications`) — appends `- <utc iso> — message` to `<dir>/<channel>.md` |
| `sandbox` | `git-worktree` | `port_base` (`3000`, used when a URL has no port), `port_stride` (`10`), `max_concurrent` (`3`), `db_clone_command` (`null`; string or argv list run inside the worktree with `{key} {slot} {dir} {db}`), `db_drop_command` (on teardown), `sandboxes_dir`, `registry_file` (default `<evidence.root>/sandboxes.json`), `delete_branch_on_teardown` (`false`) |
| `evidence_store` | `local` | none — files land under `<evidence.root>/<key>/uploads/` and return `file://` urls |

### `adapters.auth_provider.config` (provider `env-file`)

| key | default | meaning |
|---|---|---|
| `env_file` | `".env.e2e"` | Usernames and non-secret extras. Read first. |
| `local_env_file` | `".env.e2e.local"` | Passwords; gitignored, per machine. Read second — later wins. |
| `process_env` | `true` | Whether the process environment overrides both files (CI injects secrets that way). |
| `prefix` | `"E2E"` | Variable prefix when `credential_env_pattern` is not set. |
| `roles` | `["owner","admin","member","observer"]` | The canonical role vocabulary, lower-case; compared case-insensitively. The oracle, the preflight and the personas read it. |
| `credential_env_pattern` | `"E2E_{ROLE}_{FIELD}"` | How a role's variables are named; `{ROLE}` upper-cased, `{FIELD}` = `USERNAME` \| `PASSWORD`. Must contain both placeholders. |
| `login.path` | `"/login"` | **Required.** Where the oracle opens the login form. |
| `login.username_selector` | `"input[name=email]"` | **Required.** |
| `login.password_selector` | `"input[name=password]"` | **Required.** |
| `login.submit_selector` | `"button[type=submit]"` | Optional; empty = press Enter. |
| `login.failure_regex` | `"invalid\|incorrect\|try again"` | Optional; a match after submit ⇒ `credentials-unusable` (BLOCKED). |
| `login.signed_in_landmark` | `"[data-testid=user-menu]"` | **Required.** What proves a session — never a toast. |
| `pre_auth` | `"none"` | `none` \| `header_token` \| `sso_human_once` — a perimeter in front of the app ([`test-credentials.md`](test-credentials.md)). |
| `login_hint` | *(absent)* | A string or `{role: hint, default: hint}` — prose for the live driver on how to sign in. |

## `evidence`

| key | default | meaning |
|---|---|---|
| `root` | `"runs"` | The gitignored directory every run writes under: `runs/<KEY>/` (intake/repro/fix/verify JSON, media), `runs/tickets/`, `runs/prs/`, `runs/notifications/`, `runs/sandboxes.json`. |

## `brain`

| key | default | meaning |
|---|---|---|
| `config_file` | `".claude/loops/kb.config.json"` | The Postgres + pgvector brain pointer. The DSN comes from that file or `KB_DATABASE_URL` — never defaulted ([`brain-setup.md`](brain-setup.md)). |

## `limits_file`

Default `".claude/loops/loop-limits.json"`: the per-loop budget (USD and token caps, R5) and
per-tier retry caps the recipe router enforces.

## `models` — which model does which job

| key | default | meaning |
|---|---|---|
| `cheap` | a current small model id | triage, classification, cheap passes |
| `strong` | a current frontier model id | fixing, planning |
| `verifier` | a current frontier model id | judging — must rank ≥ the producer it grades |
| `rank` | `[…]` weakest → strongest | the ordering the "judge ≥ producer" rule compares against |

Model ids are plain strings; change them freely. Claude Code is the runner by design.

## `deploy`

| key | default | meaning |
|---|---|---|
| `prod_lane_branch_patterns` | `[]` | Branches whose **push** is a production deploy. `infra-ship` refuses such a push while the brain's `staging_only` flag is true (and never auto-deploys regardless). Empty = no push deploys — set it if yours do. |
| `tracks` | `[]` | Independently deployed parts of the app, each `{name, path_globs, version_endpoint, deployed_version_command, ci_deploy_status?}`. Set `ci_deploy_status: true` on the one track whose deploy the `ci_cd` adapter's environment status describes; with several tracks, only that track is cross-checked against it. Contract fields that name a track take a free string from `tracks[].name`. |

## `browser`

| key | default | meaning |
|---|---|---|
| `oracle_driver` | `"playwright-cli"` | Drives the deterministic replay oracle. A documented prerequisite, not abstracted further. |
| `live_driver` | `"claude-in-chrome"` | Drives live exploration and the before/after recordings. |

## `runner`

| key | default | meaning |
|---|---|---|
| `billing_posture` | `"subscription"` | `subscription` \| `metered` — how the R5 cap reads spend (`--budget-spent` is supplied by the runner either way). |
| `claude_bin` | `"claude"` | Runner binary the scheduler templates render. |
| `config_dir` | `null` | Runner config dir for the scheduler templates (`null` = the runner's default). |

## `integrations` — knobs for the release-AC, chatbot and UI-parity skills

Never tenant values: hosts and credentials come from the env-var **names** in the adapter configs.

| key | default | meaning |
|---|---|---|
| `release_ac.release_name_pattern` | `null` | How the issue tracker names a release, e.g. `"{name} {version}"`; `qa-loop-release-ac` uses it to pick the one release name denoting a version. `null` = any release name containing the version. |
| `chatbot.entry_path` | `null` | Path of the in-app chat surface `qa-run-chatbot` opens. |
| `chatbot.cases_dir` | `null` | Directory of chatbot case files. |
| `ui_parity.baseline_env` | `"staging"` | Environment name (from `app.environments`) the parity sweep treats as the reference. |
| `ui_parity.candidate_env` | `"local"` | Environment name it compares against the baseline. |
| `ui_parity.routes` | `[]` | Routes to sweep; empty = the skill asks. |

## `personas_file`

Default `".claude/loops/personas.json"`: the persona pack `qa-explore` / `qa-repro` / `qa-verify` draw
roles, goals and habits from — `{ roles, engagement_tiers, personas[{id, role, tier, goals, habits,
lenses}] }`. The shipped pack is a neutral sample on the example domain; consumers reference ids and
roles only.

---

## See also

- [`adapters.md`](adapters.md) — the per-role contract the `adapters` block selects providers for.
- [`getting-started.md`](getting-started.md) — which of these keys matter on day one.
- `.claude/loops/stack.config.example.json` — every key above, with a comment beside it.
