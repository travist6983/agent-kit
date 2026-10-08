#!/usr/bin/env python3
"""Reclaim ports and prune stale sandboxes — the friction that actually costs runs.

WHY THIS EXISTS. Provisioning is cheap. What is NOT cheap is a **stale sandbox squatting on the
ports a new sandbox was just allocated**: a leftover tree whose processes are still listening on
the next slot's ports makes a fresh loop either hand-patch its ports and registry, or give up on
isolation and run in the developer's own checkout — which defeats the reason the sandbox exists.

The `sandbox` adapter allocates slots from the REGISTRY. A tree that is not in the registry is
invisible to that check while still holding real TCP ports. This script closes that gap.

Order matters — step 1 is the whole point; 2-4 are housekeeping:
  1. PORT RECLAMATION — a port in the slot range held by a process whose cwd is a sandbox dir with
     no registry entry means that tree is dead. Report it; with --apply, kill it.
  2. AGE OUT        — no registry entry and mtime > --max-age-hours.
  3. MERGED         — the tree's branch is merged into the base.
  4. NEVER prune a tree with uncommitted changes. Report it and FAIL LOUD.

Everything about WHERE sandboxes live and WHICH ports they use comes from the `sandbox` adapter
(adapters.sandbox in stack.config.json): app.repo_dir, the sandboxes dir, the registry file, the
app/api base ports and the slot stride. Flags override for scratch runs.

Usage:
  python3 prune.py [--app-root <app.repo_dir>] [--sandboxes-dir DIR] [--registry <sandboxes.json>]
                   [--slot-range 1:20] [--max-age-hours 24] [--base <project.default_branch>]
                   [--apply] [--config <stack.config.json>]

DRY-RUN BY DEFAULT. Killing a process and deleting a worktree are destructive and not always
reversible, so nothing happens without --apply.

Exit: 0 clean or dry-run · 1 something needs attention (uncommitted changes found)
      2 error, or blocked (adapter-unavailable:sandbox — provider `none` has nothing to prune).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[3]                              # .claude/skills/infra-worktree/scripts → repo
for _p in (str(_REPO_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.adapters import AdapterError, AdapterUnavailable, get, load_config  # noqa: E402
from role_overrides import env_overrides  # noqa: E402


def sh(args, timeout=30):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 127, "", str(e)


def listening_pids(port):
    rc, out, _ = sh(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"])
    if rc != 0 or not out:
        return []
    return [p for p in out.split("\n") if p.strip().isdigit()]


def pid_cwd(pid):
    rc, out, _ = sh(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    if rc != 0:
        return None
    for line in out.split("\n"):
        if line.startswith("n"):
            return line[1:]
    return None


def pid_started(pid):
    rc, out, _ = sh(["ps", "-o", "lstart=", "-p", str(pid)])
    return out if rc == 0 else ""


def git(repo, *args, timeout=30):
    return sh(["git", "-C", repo, *args], timeout=timeout)


def base_ports(stack, sb):
    """The un-offset ports a slot shifts: the port of app.url / app.api_url, else port_base."""
    port_base = int(getattr(sb, "port_base", None) or (sb.config.get("port_base") if hasattr(sb, "config") else 0) or 3000)
    ports = []
    for key in ("app.url", "app.api_url"):
        url = stack.get(key)
        if url:
            ports.append(urlsplit(str(url)).port or port_base)
    return sorted(set(ports or [port_base]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-root", help="the app checkout (default app.repo_dir)")
    ap.add_argument("--worktree-dir", "--sandboxes-dir", dest="sandboxes_dir",
                    help="where sandboxes live (default: the adapter's sandboxes dir)")
    ap.add_argument("--registry", help="sandboxes.json (default: the adapter's registry file)")
    ap.add_argument("--slot-range", default="1:20", help="LOW:HIGH slots to scan for port squatters")
    ap.add_argument("--max-age-hours", type=float, default=24.0)
    ap.add_argument("--base", help="merge target (default project.default_branch)")
    ap.add_argument("--apply", action="store_true", help="actually kill/remove (default: dry-run)")
    ap.add_argument("--config", help="an alternate stack.config.json")
    a = ap.parse_args()

    stack = load_config(a.config) if a.config else load_config()
    override = env_overrides("sandbox")                 # LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__* per key
    if a.app_root:
        override["repo_dir"] = a.app_root
    if a.sandboxes_dir:
        override["sandboxes_dir"] = a.sandboxes_dir
    if a.registry:
        override["registry_file"] = a.registry
    sb = get("sandbox", stack=stack, config_override=override or None, cache=False)

    # -- where things are: ask the adapter -------------------------------------------------------
    try:
        registered = sb.list()
    except AdapterUnavailable as e:
        print(json.dumps({"ok": False, "status": "blocked", "error": "adapter-unavailable:%s" % e.role, "detail": e.message, "reason": e.reason,
                          "note": "no sandbox provider is configured, so there is nothing to prune"}, indent=1))
        return 2
    try:
        app_root = str(getattr(sb, "repo_dir", None) or stack.repo_dir() or "")
    except AdapterError as e:
        print(json.dumps({"ok": False, "error": "app-root-not-found", "detail": e.message,
                          "hint": "set app.repo_dir in stack.config.json or pass --app-root"}, indent=1))
        return 2
    if not app_root or not os.path.isdir(app_root):
        print(json.dumps({"ok": False, "error": "app-root-not-found", "tried": app_root or "<unset>",
                          "hint": "set app.repo_dir in stack.config.json or pass --app-root"}, indent=1))
        return 2
    wtdir = a.sandboxes_dir or getattr(sb, "sandboxes_dir", None)
    if not wtdir:
        print(json.dumps({"ok": False, "error": "sandboxes-dir-unknown",
                          "hint": "this sandbox provider does not expose its directory; pass --sandboxes-dir"}, indent=1))
        return 2
    wtdir = os.path.abspath(os.path.expanduser(str(wtdir)))
    base = a.base or stack.get("project.default_branch") or "HEAD"
    stride = int(getattr(sb, "port_stride", None) or 10)
    bases = base_ports(stack, sb)

    reg_paths = {os.path.abspath(s.get("dir", "")) for s in registered if s.get("dir")}
    reg_slots = {int(s.get("slot", 0)) for s in registered if s.get("slot") is not None}

    lo, _, hi = a.slot_range.partition(":")
    lo, hi = int(lo), int(hi)

    squatters, aged, merged, dirty, notes = [], [], [], [], []

    # ---- 1. PORT RECLAMATION — the step that actually matters -------------------------------------
    if not shutil.which("lsof"):
        notes.append("lsof not available — port reclamation SKIPPED. This is the one check that catches "
                     "the failure this script exists for; treat a run without it as unprotected rather "
                     "than clean.")
    else:
        for slot in range(max(lo, 1), hi + 1):          # slot 0 is the developer's checkout — never scanned
            for port in (b + slot * stride for b in bases):
                for pid in listening_pids(port):
                    cwd = pid_cwd(pid) or ""
                    inside = os.path.abspath(cwd) if cwd else ""
                    owned = any(inside.startswith(p) for p in reg_paths if p)
                    if owned or slot in reg_slots:
                        continue  # a live, registered sandbox — never touch it
                    squatters.append({
                        "port": port, "slot": slot, "pid": int(pid),
                        "cwd": cwd or "<unknown>", "started": pid_started(pid),
                        "why": "holds a sandbox port but belongs to no registry entry — the adapter "
                               "allocates slots from the registry, so this process is invisible to "
                               "allocation while still holding the port. The next loop given this slot "
                               "would start against a dead tree's app.",
                    })

    # ---- 2-4. sandbox housekeeping ---------------------------------------------------------------------
    if os.path.isdir(wtdir):
        for name in sorted(os.listdir(wtdir)):
            path = os.path.join(wtdir, name)
            if not os.path.isdir(path):
                continue
            if os.path.abspath(path) in reg_paths:
                continue  # an active sandbox

            # 4 FIRST — a tree with uncommitted work is never pruned, whatever else it is.
            rc, out, _ = git(path, "status", "--porcelain")
            if rc == 0 and out:
                dirty.append({"worktree": path, "changes": len(out.split("\n")),
                              "why": "uncommitted changes — NOT pruned. Someone's work is in here; a human decides."})
                continue
            if rc != 0:
                notes.append(f"{name}: not a readable git worktree — left alone")
                continue

            rc, branch, _ = git(path, "rev-parse", "--abbrev-ref", "HEAD")
            branch = branch if rc == 0 else "<unknown>"

            rc, _, _ = git(app_root, "merge-base", "--is-ancestor", branch, base)
            if rc == 0 and branch not in ("<unknown>", base):
                merged.append({"worktree": path, "branch": branch,
                               "why": f"branch is merged into {base} — safe to prune"})
                continue

            age_h = (time.time() - os.path.getmtime(path)) / 3600.0
            if age_h > a.max_age_hours:
                aged.append({"worktree": path, "branch": branch, "age_hours": round(age_h, 1),
                             "why": f"no registry entry and untouched for {age_h:.1f}h"})

    # ---- act, or don't --------------------------------------------------------------------------------
    actions = []
    if a.apply:
        for s in squatters:
            rc, _, err = sh(["kill", "-TERM", str(s["pid"])])
            actions.append({"action": "kill", "pid": s["pid"], "port": s["port"], "ok": rc == 0, "error": err or None})
        for entry in merged + aged:
            rc, _, err = git(app_root, "worktree", "remove", "--force", entry["worktree"], timeout=120)
            actions.append({"action": "worktree-remove", "worktree": entry["worktree"], "ok": rc == 0, "error": err or None})
        if merged or aged:
            git(app_root, "worktree", "prune")

    result = {
        "ok": not dirty,
        "mode": "apply" if a.apply else "dry-run",
        "app_root": app_root,
        "sandboxes_dir": wtdir,
        "base": base,
        "scanned_ports": {"bases": bases, "stride": stride, "slots": f"{max(lo, 1)}:{hi}"},
        "registered_sandboxes": len(registered),
        "port_squatters": squatters,
        "prunable_merged": merged,
        "prunable_aged": aged,
        "blocked_dirty": dirty,
        "actions": actions,
        "notes": notes,
    }
    if not a.apply and (squatters or merged or aged):
        result["remedy"] = ("re-run with --apply to reclaim these. Nothing was changed: killing a process "
                            "and removing a worktree are destructive.")
    print(json.dumps(result, indent=1))
    return 1 if dirty else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"prune crashed: {e}"}, indent=1))
        sys.exit(2)
