#!/usr/bin/env bash
# infra-worktree — sandbox lifecycle for autonomous loops.
# A THIN wrapper over the `sandbox` adapter (lib/adapters, role `sandbox`, provider from
# .claude/loops/stack.config.json — `git-worktree` by default). The adapter owns the mechanism:
# the worktree, the slot/port allocation under an exclusive lock, the registry, the optional
# db_clone_command / db_drop_command hooks. This script owns the loop-facing contract: the flags,
# the failure classes, the exit codes, the credential-file report, and "teardown never fails".
#
# Usage:
#   sandbox.sh provision (--key KEY [--type fix|feature] | --branch BRANCH) [--base BR] [--cap N]
#                        [--app-root PATH] [--dry-run]
#   sandbox.sh teardown  (--key KEY | --branch BRANCH) [--app-root PATH] [--dry-run]
#   sandbox.sh list
#
# KEY is the sandbox key — normally the ticket key plus slug (PROJ-123-invoice-total). The adapter
# derives the branch as project.branch_prefixes[type] + KEY (fix/PROJ-123-invoice-total). A full
# --branch is accepted and mapped back onto (key, type) by stripping a configured prefix, so the
# branch dev-intake named is the branch the sandbox gets.
#
# Isolation (R4): the adapter allocates slot 1..max_concurrent under an fcntl lock in ONE
# transaction (cap check + slot + registry write), so parallel provisions cannot race. Slot 0 is
# never allocated — that is the developer's own checkout. Ports are the port of app.url / app.api_url
# plus slot * port_stride; the stride must exceed the spread between the app's base ports so no
# sandbox's port can equal another sandbox's port. Every dynamic value reaches python through the
# ENVIRONMENT, never string interpolation — a ticket-derived branch name cannot inject (R6).
#
# The concurrent-sandbox CAP (adapters.sandbox.config.max_concurrent, --cap overrides) is a
# RESOURCE bound — distinct from the R5 per-loop spend breaker in loop-limits.json.
#
# Provision does NOT start the app: it returns started:false + next_steps; the recipe runs
# app.start_command in `dir` and proves reachability on urls.app + app.health_path.
# Provision also reports the test-credential file the auth_provider adapter is configured with
# (first existing of local_env_file, env_file) so the recipe can hand it to the replay oracle and
# prove it with oracle_preflight.py BEFORE spending anything on a browser step.
#
# --dry-run keeps the registry/slot/worktree bookkeeping (cheap, local git) but suppresses the
# db_clone_command / db_drop_command hooks and marks the result dry_run:true.
#
# Environment (all optional): LOOP_APP_ROOT = --app-root default · LOOP_SANDBOX_STATE = a directory
# whose sandboxes.json becomes the registry (tests) · LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__<KEY> and
# LOOPS_CFG__ADAPTERS__AUTH_PROVIDER__CONFIG__<KEY> override single config keys for this process.
#
# Exit codes: 0 ok / reused / idempotent-teardown · 1 usage · 2 sandbox-cap-reached (resource bound)
#             3 sandbox-create-failed, or blocked (adapter-unavailable:sandbox — provider `none`)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../../.." && pwd)"          # the checkout that holds lib/ and .claude/

cmd="${1:-}"; shift 2>/dev/null || true
KEY=""; BRANCH=""; TYPE=""; BASE=""; CAP=""; APP_ROOT="${LOOP_APP_ROOT:-}"; DRY=0
while [ $# -gt 0 ]; do case "$1" in
  --key)       KEY="$2"; shift 2;;
  --branch)    BRANCH="$2"; shift 2;;
  --type)      TYPE="$2"; shift 2;;
  --base)      BASE="$2"; shift 2;;      # ref a NEW branch forks from (default project.default_branch)
  --cap)       CAP="$2"; shift 2;;       # overrides adapters.sandbox.config.max_concurrent
  --app-root)  APP_ROOT="$2"; shift 2;;  # overrides app.repo_dir
  --dry-run)   DRY=1; shift;;
  *) shift;;
esac; done

case "$cmd" in provision|teardown|list) ;; *)
  echo '{"ok":false,"error":"usage: provision|teardown|list"}'; exit 1;; esac
if [ "$cmd" != "list" ] && [ -z "$KEY" ] && [ -z "$BRANCH" ]; then
  echo '{"ok":false,"error":"no-key","hint":"pass --key KEY (ticket key + slug) or --branch BRANCH"}'; exit 1
fi

export SBX_CMD="$cmd" SBX_KEY="$KEY" SBX_BRANCH="$BRANCH" SBX_TYPE="$TYPE" SBX_BASE="$BASE" \
       SBX_CAP="$CAP" SBX_APP_ROOT="$APP_ROOT" SBX_DRY="$DRY" SBX_REPO_ROOT="$REPO_ROOT" SBX_HERE="$HERE"
exec python3 - <<'PY'
import json
import os
import sys

sys.path.insert(0, os.environ["SBX_REPO_ROOT"])
sys.path.insert(0, os.environ["SBX_HERE"])
from lib.adapters import AdapterError, AdapterUnavailable, cfg, get, load_config  # noqa: E402
from role_overrides import env_overrides, role_config  # noqa: E402

J = lambda o: print(json.dumps(o, separators=(",", ":"), default=str))  # noqa: E731

cmd = os.environ["SBX_CMD"]
dry = os.environ["SBX_DRY"] == "1"
key, branch, ttype = os.environ["SBX_KEY"], os.environ["SBX_BRANCH"], os.environ["SBX_TYPE"] or "fix"

# -- the adapter: stack.config < per-key env overrides (LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__*) < flags
override = env_overrides("sandbox")
if os.environ["SBX_CAP"]:
    override["max_concurrent"] = int(os.environ["SBX_CAP"])
if os.environ["SBX_APP_ROOT"]:
    override["repo_dir"] = os.environ["SBX_APP_ROOT"]
if os.environ.get("LOOP_SANDBOX_STATE"):                     # tests point the registry at a scratch dir
    override["registry_file"] = os.path.join(os.environ["LOOP_SANDBOX_STATE"], "sandboxes.json")
if dry:
    override["db_clone_command"] = None
    override["db_drop_command"] = None
sb = get("sandbox", config_override=override or None, cache=False)
stack = load_config()

# -- key <-> branch ----------------------------------------------------------------------------
prefixes = cfg("project.branch_prefixes") or {}
if branch and not key:
    for t, p in sorted(prefixes.items(), key=lambda kv: -len(kv[1] or "")):
        if p and branch.startswith(p):
            key, ttype = branch[len(p):], t
            break
    else:
        J({"ok": False, "error": "branch-prefix-unknown", "branch": branch,
           "hint": "the sandbox adapter names branches project.branch_prefixes[type] + key; "
                   "pass --key KEY --type TYPE, or add the prefix to project.branch_prefixes"})
        sys.exit(1)
key = key.strip().replace("\\", "-").replace(" ", "-")
if cmd != "list" and (not key or "/" in key or key.startswith(".")):
    J({"ok": False, "error": "invalid-key", "key": key, "hint": "a sandbox key has no '/'"})
    sys.exit(1)


def blocked(e, code):
    J({"ok": False, "status": "blocked", "error": e.message, "role": e.role, "op": e.op, "reason": e.reason})
    sys.exit(code)


def credential_report():
    """Which test-credential file the auth_provider adapter will read — reported, not copied, so the
    recipe can set E2E_ENV_FILE and oracle_preflight.py can prove it. No claim is made that it works."""
    auth = role_config(stack, "auth_provider")
    out = {"provider": stack.adapter_spec("auth_provider")["provider"], "file": None, "source": None, "candidates": []}
    for name in ("local_env_file", "env_file"):
        p = stack.resolve(auth.get(name)) if auth.get(name) else None
        if p is None:
            continue
        out["candidates"].append(str(p))
        if out["file"] is None and p.is_file():
            out["file"], out["source"] = str(p), name
    return out


if cmd == "provision":
    try:
        try:
            live_before = {e["key"] for e in sb.list() if e.get("exists")}
        except AdapterUnavailable:
            live_before = set()                     # provision below raises the same error, labelled with its own op
        s = sb.provision(key=key, ticket_type=ttype, base=os.environ["SBX_BASE"] or cfg("project.default_branch"))
    except AdapterUnavailable as e:
        blocked(e, 3)
    except AdapterError as e:
        d = e.to_dict()
        if "max_concurrent" in d:                       # the adapter's cap refusal → the loop's failure class
            J({"ok": False, "error": "sandbox-cap-reached", "active": len(d.get("active") or []),
               "cap": d["max_concurrent"], "detail": e.message})
            sys.exit(2)
        J({"ok": False, "error": "sandbox-create-failed", "key": key,
           "detail": e.message, **{k: v for k, v in d.items() if k not in ("ok", "error")}})
        sys.exit(3)
    out = {"ok": True, "reused": key in live_before, "dry_run": dry, **s.to_dict()}
    out["app_root"] = str(getattr(sb, "repo_dir", None) or cfg("app.repo_dir") or "")
    out["credentials"] = credential_report()
    J(out)
    sys.exit(0)

if cmd == "teardown":
    # Crash-safe: called from a finally, so it must never raise and never fail the caller. The
    # adapter's teardown is itself idempotent (missing dir / missing entry are not errors).
    try:
        r = sb.teardown(key=key)
    except AdapterUnavailable as e:
        J({"ok": True, "key": key, "removed": 0, "idempotent": True, "dry_run": dry, "note": e.message})
        sys.exit(0)
    except AdapterError as e:
        J({"ok": False, "error": "teardown-incomplete", "failure_class": "leaked-sandbox", "key": key,
           "detail": e.message, "idempotent": True, "dry_run": dry})
        sys.exit(0)
    removed = 1 if (r.get("removed_registry") or r.get("removed_worktree")) else 0
    J({"ok": True, "removed": removed, "idempotent": True, "dry_run": dry, **r})
    sys.exit(0)

if cmd == "list":
    try:
        J({"sandboxes": sb.list()})
    except AdapterUnavailable as e:
        J({"sandboxes": [], "note": e.message})
    sys.exit(0)
PY
