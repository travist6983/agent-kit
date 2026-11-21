#!/usr/bin/env python3
"""map_staleness: which areas of a saved map the code has moved under (deterministic, tested).

A map records the commit it describes. This diffs that commit against the checkout (committed
changes plus anything modified, staged or untracked in the working tree), maps every changed path
to the area whose path prefix covers it most specifically, and says which areas to relearn. A path
no area covers is listed on its own, because it means the survey's area cut is out of date too.

When the map's commit cannot be used (the all-zero commit of a map built from a tree with no
history, a commit the checkout does not have, or a checkout that is not a git repo) the answer is
`unknown` and every area is stale: a full relearn is the honest fallback, never a guess.

Usage:
  map_staleness.py --repo <checkout> --map <run>/map.json [--out stale.json]
                   [--changed-out changed.txt]
--changed-out writes the changed paths one per line, which qa-onboard-quiz's grader takes as
--changed-files so a lost question that rests on moved code is classed stale.
Prints the result as JSON. Exit 0, or 2 on bad usage.
"""
import argparse
import json
import os
import subprocess
import sys

ZERO = "0" * 40


def git(repo, *args):
    p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)
    return p.returncode, p.stdout


def covers(prefix, path):
    prefix = prefix.rstrip("/")
    return prefix in ("", ".") or path == prefix or path.startswith(prefix + "/")


def owner(areas, path):
    best, best_len = None, -1
    for ar in areas:
        for pre in ar.get("paths", []):
            if covers(pre, path) and len(pre.rstrip("/")) > best_len:
                best, best_len = ar["id"], len(pre.rstrip("/"))
    return best


def changed_since(repo, commit):
    rc, out = git(repo, "diff", "--name-only", "--no-renames", commit, "HEAD", "--")
    if rc != 0:
        return None
    paths = set(l for l in out.splitlines() if l)
    rc, out = git(repo, "status", "--porcelain", "--untracked-files=all", "--no-renames")
    if rc == 0:
        for line in out.splitlines():
            if len(line) > 3:
                paths.add(line[3:].strip().strip('"'))
    return sorted(paths)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--out")
    ap.add_argument("--changed-out")
    a = ap.parse_args()
    try:
        m = json.load(open(a.map))
    except (OSError, ValueError) as e:
        print(json.dumps({"ok": False, "error": "unreadable map: %s" % e}))
        return 2
    if not os.path.isdir(a.repo):
        print(json.dumps({"ok": False, "error": "repo not found: %s" % a.repo}))
        return 2
    areas = m.get("areas", [])
    commit = m.get("commit", "")
    rc, head = git(a.repo, "rev-parse", "HEAD")
    head = head.strip() if rc == 0 else ""
    res = {"ok": True, "map_commit": commit, "head": head}

    reason = None
    if not head:
        reason = "the checkout is not a git repository"
    elif not commit or commit == ZERO:
        reason = "the map was built from a tree with no history"
    elif git(a.repo, "cat-file", "-e", commit + "^{commit}")[0] != 0:
        reason = "the map's commit is not in this checkout"
    changed = None if reason else changed_since(a.repo, commit)
    if changed is None and not reason:
        reason = "git could not diff from the map's commit"

    if reason:
        res.update(status="unknown", reason=reason + "; relearn every area",
                   changed_files=[], stale_areas=sorted(ar["id"] for ar in areas), unassigned=[])
    else:
        stale, unassigned = {}, []
        for p in changed:
            o = owner(areas, p)
            if o is None:
                unassigned.append(p)
            else:
                stale.setdefault(o, []).append(p)
        res.update(status="stale" if changed else "fresh", changed_files=changed,
                   stale_areas=sorted(stale), files_by_area=stale, unassigned=unassigned)
        if unassigned:
            res["reason"] = "%d changed path(s) belong to no area; re-survey before relearning" % len(unassigned)
    if a.changed_out:
        with open(a.changed_out, "w") as fh:
            fh.write("".join(p + "\n" for p in res["changed_files"]))
    text = json.dumps(res, indent=2)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
