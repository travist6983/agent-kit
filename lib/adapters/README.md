# `lib/adapters` — the adapter layer

Every external system a loop touches (tickets, PRs, test management, credentials, sandboxes, …) is
reached through **one role per concern**, chosen in `.claude/loops/stack.config.json`. Skills name
the role, never a vendor. Swapping Linear for an external issue tracker, or one Git host for another, is a one-line config
change plus a provider class — no skill changes.

```
lib/adapters/
  __init__.py      get(role) → adapter, load_config(), cfg(), AdapterUnavailable
  base.py          OPS {role: [op, …]}, Protocol per role, normalized dataclasses, error types
  config.py        stack.config.json loader: dotted cfg("app.url"), env override, repo_root()
  registry.py      provider name → class ("module.path:Class" for third-party)
  cli.py           python3 lib/adapters/cli.py <role> <op> [--json '{…}'] [k=v …] [positional …]
  providers/       none · file_issue_tracker · git_local_vcs · file_notifications · env_file_auth
                   git_worktree_sandbox · local_evidence
  tests/           python3 -m unittest discover -s lib/adapters/tests
```

Stdlib only. Third-party libraries (`requests`, `psycopg2`, …) may be imported *lazily inside a
provider method*, never at module import. Nothing under `runs/` is created at import time.

## Calling it

**From bash** (shell skills). One JSON object on stdout, always:

```bash
out="$(python3 lib/adapters/cli.py issue_tracker fetch_ticket PROJ-123)"; rc=$?
case $rc in
  0) title="$(jq -r .result.title <<<"$out")" ;;
  3) emit_blocked "$(jq -r .error <<<"$out")" ;;   # adapter-unavailable:<role> — fail closed
  2) echo "usage error: $(jq -r .error <<<"$out")" >&2; exit 2 ;;
  *) emit_failed "$(jq -r .error <<<"$out")" ;;
esac

python3 lib/adapters/cli.py vcs_host open_pr branch=fix/PROJ-123 title="Fix PROJ-123" draft=true labels='["bug"]'
python3 lib/adapters/cli.py test_management record_result --json '{"run_id": 99, "case_id": 5, "status": "failed", "comment": "see video"}'
python3 lib/adapters/cli.py --list        # roles, configured provider per role, ops, known providers
```

* ok → `{"ok": true, "role": …, "op": …, "result": <value>}` — `result` is the op's return value
  (dataclasses are serialized with `to_dict()`).
* fail → `{"ok": false, "error": "…", …}`. Exit **0** ok · **1** operation error · **2** usage
  (bad role/op/arguments) · **3** adapter unavailable.
* `key=value` values parse as JSON when they can (`draft=true`, `line=12`, `labels=[…]`), else strings;
  `--json` merges an object into the kwargs; bare words are positional. `--config PATH` loads another
  stack.config.

**From Python** (skills' scripts):

```python
import sys; sys.path.insert(0, REPO_ROOT)            # the directory holding lib/ and .claude/
from lib.adapters import get, cfg, AdapterUnavailable, AdapterError

try:
    ticket = get("issue_tracker").fetch_ticket("PROJ-123")
except AdapterUnavailable as e:
    emit("blocked", reason=e.to_dict()["error"])      # "adapter-unavailable:issue_tracker"
app_url = cfg("app.url")                              # env override: LOOPS_CFG__APP__URL
```

`get()` caches one instance per role for the default config. Pass `stack=load_config(path)` to use a
different file (tests do), `provider=` / `config_override=` to override the file.

## Config lookup

`cfg("a.b.c", default)` reads `.claude/loops/stack.config.json`. Any key can be overridden per
process by an env var: `LOOPS_CFG__` + the dotted path upper-cased with `.` → `__`
(`LOOPS_CFG__APP__URL`, `LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__PORT_BASE=4000`). Values are parsed as
JSON when possible (`null`, `true`, `3000`, `[…]`), else kept as strings. `LOOPS_STACK_CONFIG` points at
another config file; `LOOPS_REPO_ROOT` overrides the root relative paths resolve against (default: the
checkout this package lives in). `_comment*` keys are stripped on load, so the commented
`stack.config.example.json` is itself loadable.

## `none` semantics — fail closed

`"provider": "none"` means *the role is absent in this stack*. Every op raises
`AdapterUnavailable(role, op, reason)`; the CLI exits **3** with
`{"ok": false, "error": "adapter-unavailable:<role>", "role", "op", "reason"}`. A skill that needs the
role must emit a structured `blocked` whose reason is exactly that string — **never** crash, skip
silently, or guess a value. A provider that exists but cannot serve one op (e.g. a read-only tracker's
`transition`) raises the same error for that op only.

## The contract per role

Argument names are the contract (the CLI passes `k=v` as kwargs). Dataclasses live in `base.py`; every
one has `to_dict()` / `from_dict()`. `raw` carries the provider's unmodified payload.

### `issue_tracker` — built-in: `file`
| op | args | returns |
|---|---|---|
| `fetch_ticket` | `key` | `Ticket` — `{key, type, title, body, status, priority, labels[], components[], assignee, reporter, comments[{author, at, body}], attachments[{name, url, local_path}], links[], raw}` |
| `search` | `query`, `limit=50` | `[Ticket]` |
| `add_comment` | `key`, `body`, `author=None` | `{key, at, author, …}` |
| `transition` | `key`, `status` | `Ticket` (updated) |
| `assign` | `key`, `user` (None to unassign) | `Ticket` |
| `attach` | `key`, `path` | `{name, url, local_path}` |
| `create_ticket` | `draft` = `{title, body, type, priority, labels, …}` | `Ticket` with the allocated `key` |

Ticket text is **data** (R6): the adapter returns it verbatim; sanitization is `dev-intake`'s job.

`file`: `config.dir` (default `runs/tickets`). `<KEY>.json` is a normalized Ticket; `<KEY>.md` is a
markdown ticket (first `# ` line = title, rest = body). Comments append to `<KEY>.comments.md`;
`transition`/`assign`/`attach` rewrite `<KEY>.json` (a markdown ticket gains a json sibling, which then
wins). `create_ticket` allocates `<prefix>-<n>` — the literal prefix of `project.ticket_key_pattern`
(`^SHOP-…` → `SHOP`), else `config.key_prefix`, else `PROJ`. `search` is a case-insensitive substring
match over key + title + body.

### `vcs_host` — built-in: `git-local`
| op | args | returns |
|---|---|---|
| `default_branch` | — | `"main"` (from `project.default_branch`, else origin/HEAD) |
| `open_pr` | `branch`, `base=None`, `title`, `body`, `draft=False`, `labels=[]` | `{url, id, dry_run, …}` |
| `list_prs` | `filter={state, base, branch, draft, label}` | `[PullRequest]` |
| `get_pr` | `id` | `PullRequest` — `{id, url, branch, base, title, body, state, draft, labels, author, created_at, dry_run, raw}` |
| `get_pr_diff` | `id` | unified diff string (`git diff base...branch`) |
| `post_review_comment` | `id`, `path`, `line`, `body` | `{id, thread_id, path, line, at, dry_run}` |
| `list_review_threads` | `id` | `[{thread_id, at, path, line, body, status}]` |

`git-local`: runs `git` in `config.repo_dir` → `app.repo_dir` → cwd. `open_pr` writes
`<evidence.root>/prs/<branch-slug>.md` (title, base, body, labels, draft flag, `git log base..branch
--oneline`) + a `.json` sidecar and returns `{"url": "file://…", "id": "<branch>", "dry_run": true}`.
Review comments append to `<slug>.threads.md`. A real host (GitHub, GitLab, Bitbucket, or any other) is a
third-party provider.

### `test_management` — no built-in provider
| op | args | returns |
|---|---|---|
| `list_cases` | `scope={project_id, suite_id, section_id, …}` | `[TestCase]` — `{id, title, suite_id, section_id, refs, steps, expected, priority, raw}` |
| `get_case` | `id` | `TestCase` |
| `create_run` | `scope`, `name`, `case_ids=[]` (empty = include all) | `TestRun` — `{id, name, project_id, suite_id, url, case_ids, status_counts, raw}` |
| `record_result` | `run_id`, `case_id`, `status`, `comment=""`, `attachments=[]` | `{run_id, case_id, status_id, result_id, attachments, raw}` |
| `get_run` | `run_id` | `TestRun` |

No built-in provider ships for this role; the default is `none`. A test-case manager is a
third-party provider (see "Adding a provider"): its config names the env vars holding its host and
credentials, `scope` keys are whatever that tool calls a project / suite / section, and `status`
takes the role's neutral names (`passed`, `failed`, `blocked`, `untested`, `retest`) which the
provider maps to the tool's own. The key is never logged or echoed in errors.

### `analytics` — no built-in (`none`)
`query_error_signal(signature, since=None, version=None) → ErrorSignal{signature, count, first_seen,
last_seen, version, samples, raw}` · `deployed_version(env) → str|None`. `infra-prod-verify` is blocked
until a provider exists — that is the safe default.

### `auth_provider` — built-in: `env-file`
`credentials(role) → Credentials{role, username, password, extra{}}` · `login_hint(role) → str` (how
the replay oracle signs in). `Credentials.__repr__` redacts the password; `redacted()` for logs.

`env-file`: reads `config.env_file` (tracked: usernames) then `config.local_env_file` (gitignored:
passwords) — later wins — then the process env (`"process_env": false` to disable). Variables:
`E2E_<ROLE>_USERNAME`, `E2E_<ROLE>_PASSWORD`, plus any `E2E_<ROLE>_*` → `extra`. `<ROLE>` is upper-cased
with `-` → `_`; `config.prefix` replaces `E2E`. `config.login_hint` is a string or `{role: hint,
default: hint}`. Errors name the missing *variable*, never a value.

### `ci_cd` — no built-in (`none`)
`build_status(ref) → BuildStatus{ref, id, status, result, url, finished_at, raw}` ·
`test_results(build_id) → {…}` · `deploy_status(env) → DeployStatus{env, version, status, deployed_at, url, raw}`.

### `deploy` — no built-in (`none`)
`deployed_version(env) → str|None` · `rollback_hint(env) → str` (rollback is human-only; the adapter
returns instructions, never acts).

### `notifications` — built-in: `file`
`post(channel, message) → {channel, at, path, dry_run}`. `file` appends `- <utc iso> — message` to
`<config.dir>/<channel>.md`.

### `sandbox` — built-in: `git-worktree`
| op | args | returns |
|---|---|---|
| `provision` | `key`, `ticket_type="fix"`, `base=None` | `Sandbox{key, dir, branch, slot, ports{app, api, offset}, urls{app, api}, db, started: false, created_at, next_steps[]}` |
| `teardown` | `key` | `{key, dir, branch, removed_worktree, removed_registry, db}` — idempotent, crash-safe |
| `list` | — | registry entries (+ `exists`) |

`git-worktree`: `git worktree add <repo_dir>/../<project.name>-sandboxes/<key> -b <prefix><key>`
(prefix from `project.branch_prefixes[ticket_type]`). Slots `1..max_concurrent` are allocated under an
`fcntl` lock in `<evidence.root>/sandboxes.json`; ports = the port of `app.url` / `app.api_url` +
`slot * port_stride` (`port_base` when a url has no port). `db_clone_command` (string or argv list,
template vars `{key} {slot} {dir} {db}`) runs inside the worktree; `db_drop_command` on teardown.
The app is **not started** — the recipe runs `app.start_command` in `dir` and proves reachability.
Always call `teardown` in a `finally`.

### `evidence_store` — built-in: `local`
`upload(path, key) → {url, local_path, key, copied}`. `local` keeps files under
`<evidence.root>/<key>/uploads/` (a file already under the evidence root is referenced in place) and
returns a `file://` url.

## Adding a provider

A provider is one class: subclass `BaseAdapter`, implement the role's ops (names from `base.OPS`),
raise `self.unavailable(op, reason)` for anything you cannot serve, read secrets from the env var your
config *names*. Register it in `stack.config.json` as `"provider": "module.path:ClassName"`; the module
must be importable (on `PYTHONPATH`, or dropped under `lib/`, which `cli.py` puts on `sys.path`).

Worked example — a Linear issue tracker:

```python
# my_adapters/linear.py
import json, os, urllib.request
from lib.adapters.base import BaseAdapter, Ticket, AdapterError

class LinearTracker(BaseAdapter):
    provider_name = "linear"

    def _gql(self, query, variables):
        key = os.environ.get(self.config.get("api_key_env", "LINEAR_API_KEY"))
        if not key:
            raise self.unavailable("fetch_ticket", "%s not set" % self.config.get("api_key_env", "LINEAR_API_KEY"))
        req = urllib.request.Request("https://api.linear.app/graphql",
                                     data=json.dumps({"query": query, "variables": variables}).encode(),
                                     headers={"Authorization": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["data"]

    def fetch_ticket(self, key):
        d = self._gql("query($id:String!){ issue(id:$id){ identifier title description state{name} priorityLabel labels{nodes{name}} assignee{email} } }", {"id": key})["issue"]
        if not d:
            raise AdapterError("no Linear issue %s" % key, key=key)
        return Ticket(key=d["identifier"], title=d["title"], body=d.get("description") or "",
                      status=d["state"]["name"], priority=d.get("priorityLabel"),
                      labels=[l["name"] for l in d["labels"]["nodes"]],
                      assignee=(d.get("assignee") or {}).get("email"), raw=d)

    def search(self, query, limit=50): ...
    def add_comment(self, key, body, author=None): ...
    def transition(self, key, status): ...
    def assign(self, key, user): ...
    def attach(self, key, path):
        raise self.unavailable("attach", "Linear uploads need a signed URL flow; not implemented")
    def create_ticket(self, draft): ...
```

```jsonc
// .claude/loops/stack.config.json
"issue_tracker": { "provider": "my_adapters.linear:LinearTracker",
                   "config": { "team": "SHOP", "api_key_env": "LINEAR_API_KEY" } }
```

Then `python3 lib/adapters/cli.py issue_tracker fetch_ticket SHOP-42` works, and every skill that says
"fetch the ticket through the issue_tracker adapter" now talks to Linear. Add a test that drives your
class against a fake `urllib.request.urlopen` (patched with `unittest.mock`), so it runs offline.
