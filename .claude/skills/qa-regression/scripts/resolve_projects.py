#!/usr/bin/env python3
"""resolve_projects.py — which build / lint / unit-test commands a set of changed paths implies.

    python3 resolve_projects.py --cwd <app checkout>              # diff vs merge-base(project.default_branch, HEAD)
    python3 resolve_projects.py --cwd <app checkout> --base release/2.4
    python3 resolve_projects.py src/tasks/service.js web/src/shared/api.ts
    git diff --name-only $(git merge-base "$BASE" HEAD) | python3 resolve_projects.py --stdin

With no paths, no --stdin and no --changed-files-from, it asks git itself: `git diff --name-only
$(git merge-base <base> HEAD)` plus untracked files, inside --cwd (default app.repo_dir from
stack.config, else .). --base defaults to project.default_branch — never a literal branch name.
Use the merge-base, not the base tip, or commits that landed on the base since you branched read
as deletions.

Reads the conventions ruleset named by `stack.conventions_file` in `.claude/loops/stack.config.json`
(through lib/adapters/config.py, so every key has a `LOOPS_CFG__*` env override):

  conventions.projects[]            {name, path_globs, build, lint, unit_test, test_report}
  conventions.test_path_regex       which changed paths are tests
  conventions.shared_core_globs     the paths whose change is the classic unrelated-break tell
  stack.build_command / stack.lint_command / stack.unit_test_command / stack.test_report
                                    the repo-wide fallback for a path no project claims ("other")

Prints ONE JSON object:

  {
    "base": "<project.default_branch>",
    "changed_files": [...],
    "projects": [ { "name", "matched": bool, "changed_files": [...], "changed_tests": [...],
                    "build", "lint", "unit_test", "test_report": {"format", "glob"} } ],
    "other": { ... same shape, "name": "other" } | null,
    "shared_core_hits": [...],
    "area_hints": [...]            # directory stems of the changed non-test files, deepest first
  }

Exit 0 always on a readable config; 2 on a usage / config error (a git diff that cannot be taken, or
neither --base nor project.default_branch set — there is no built-in default branch). Stdlib only. It runs no build, lint or test command — only git, and only when asked for the
diff. A project appears only when at least one changed path matched it; the first project whose
`path_globs` match a path owns that path (same rule dev-pre-pr applies), so the two gates can
never disagree about which commands a branch owes.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional


def _repo_root() -> Path:
    env = os.environ.get("LOOPS_REPO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # <root>/.claude/skills/qa-regression/scripts/resolve_projects.py
    return Path(__file__).resolve().parents[4]


sys.path.insert(0, str(_repo_root()))
try:
    from lib.adapters.config import load_config  # noqa: E402
except Exception as e:  # pragma: no cover - import guard
    print(json.dumps({"ok": False, "error": "cannot import lib.adapters.config: %s" % e}))
    sys.exit(2)


def _glob_match(path: str, pattern: str) -> bool:
    """`**` means any depth (including none); a bare name matches at the root. fnmatch otherwise."""
    p = PurePosixPath(path).as_posix()
    pat = pattern.strip()
    if not pat:
        return False
    if pat.endswith("/**"):
        stem = pat[:-3]
        return p == stem or p.startswith(stem + "/")
    if pat.startswith("**/"):
        tail = pat[3:]
        return fnmatch.fnmatch(p, tail) or fnmatch.fnmatch(p, "*/" + tail) or any(
            fnmatch.fnmatch(seg, tail) for seg in _suffixes(p))
    if "**" in pat:
        rx = re.escape(pat).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        return re.fullmatch(rx, p) is not None
    return fnmatch.fnmatch(p, pat)


def _suffixes(p: str) -> List[str]:
    parts = p.split("/")
    return ["/".join(parts[i:]) for i in range(len(parts))]


def _load_conventions(stack) -> Dict[str, Any]:
    rel = stack.get("stack.conventions_file", ".claude/loops/conventions.json")
    path = stack.resolve(rel)
    if path is None or not path.exists():
        raise FileNotFoundError("conventions file not found: %s (stack.conventions_file)" % path)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if not str(k).startswith("_comment")}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return strip(data)


def _project_entry(name: str, src: Dict[str, Any], stack, test_rx: Optional[re.Pattern],
                   files: List[str]) -> Dict[str, Any]:
    report = src.get("test_report") or stack.get("stack.test_report") or {}
    return {
        "name": name,
        "matched": bool(files),
        "changed_files": files,
        "changed_tests": [f for f in files if test_rx and test_rx.search(f)],
        "build": src.get("build"),
        "lint": src.get("lint"),
        "unit_test": src.get("unit_test"),
        "test_report": {"format": report.get("format", "junit"), "glob": report.get("glob")},
    }


def _git_changed(base: str, cwd: Path) -> List[str]:
    """Paths changed vs merge-base(base, HEAD), plus untracked files — what the branch owes."""
    def run(*args: str) -> str:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
        if r.returncode != 0:
            raise ValueError("git %s failed in %s: %s" % (" ".join(args), cwd, (r.stderr or r.stdout).strip()))
        return r.stdout
    merge_base = run("merge-base", base, "HEAD").strip()
    tracked = run("diff", "--name-only", merge_base).splitlines()
    untracked = run("ls-files", "--others", "--exclude-standard").splitlines()
    return [ln.strip() for ln in tracked + untracked if ln.strip()]


def _resolve_base(stack, base: Optional[str]) -> str:
    """--base, else project.default_branch. Never a literal branch name: a stack.config without
    project.default_branch is a config error (exit 2), not a silent diff against some default."""
    base = base or stack.get("project.default_branch", None)
    if not base:
        raise ValueError("project.default_branch not set and no --base")
    return str(base)


def resolve(changed: List[str], stack=None, base: Optional[str] = None) -> Dict[str, Any]:
    stack = stack or load_config()
    base = _resolve_base(stack, base)
    conv = _load_conventions(stack)
    projects = [p for p in (conv.get("projects") or []) if isinstance(p, dict) and p.get("name")]
    test_rx = re.compile(conv["test_path_regex"]) if conv.get("test_path_regex") else None
    shared_globs = conv.get("shared_core_globs") or []

    owned: Dict[str, List[str]] = {p["name"]: [] for p in projects}
    other: List[str] = []
    for f in changed:
        owner = next((p["name"] for p in projects if any(_glob_match(f, g) for g in p.get("path_globs") or [])), None)
        (owned[owner] if owner else other).append(f)

    out_projects = [_project_entry(p["name"], p, stack, test_rx, owned[p["name"]]) for p in projects if owned[p["name"]]]
    other_entry = None
    if other:
        other_entry = _project_entry("other", {
            "build": stack.get("stack.build_command"),
            "lint": stack.get("stack.lint_command"),
            "unit_test": stack.get("stack.unit_test_command"),
            "test_report": stack.get("stack.test_report"),
        }, stack, test_rx, other)

    non_tests = [f for f in changed if not (test_rx and test_rx.search(f))]
    hints: List[str] = []
    for f in non_tests:
        parent = PurePosixPath(f).parent.as_posix()
        while parent and parent != ".":
            if parent not in hints:
                hints.append(parent)
            parent = PurePosixPath(parent).parent.as_posix()
    hints.sort(key=lambda s: -s.count("/"))

    return {
        "base": base,
        "changed_files": changed,
        "projects": out_projects,
        "other": other_entry,
        "shared_core_hits": [f for f in changed if any(_glob_match(f, g) for g in shared_globs)],
        "area_hints": hints,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="changed paths (repo-relative, forward slashes)")
    ap.add_argument("--stdin", action="store_true", help="read changed paths from stdin, one per line")
    ap.add_argument("--changed-files-from", metavar="FILE", help="read changed paths from FILE, one per line")
    ap.add_argument("--config", metavar="PATH", help="an alternate stack.config.json")
    ap.add_argument("--base", metavar="BRANCH", help="merge-base branch (default: project.default_branch)")
    ap.add_argument("--cwd", metavar="DIR", help="the checkout to diff in (default: app.repo_dir, else .)")
    args = ap.parse_args(argv)

    try:
        stack = load_config(args.config) if args.config else load_config()
        base = _resolve_base(stack, args.base)
        changed = list(args.files)
        if args.stdin:
            changed += [ln.strip() for ln in sys.stdin if ln.strip()]
        if args.changed_files_from:
            with open(args.changed_files_from, "r", encoding="utf-8") as fh:
                changed += [ln.strip() for ln in fh if ln.strip()]
        if not changed and not args.stdin and not args.changed_files_from:
            cwd = Path(args.cwd) if args.cwd else (stack.repo_dir() or Path.cwd())
            changed = _git_changed(base, cwd)
        changed = [c.replace(os.sep, "/") for c in dict.fromkeys(changed)]
        print(json.dumps(resolve(changed, stack, base), indent=2))
        return 0
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
