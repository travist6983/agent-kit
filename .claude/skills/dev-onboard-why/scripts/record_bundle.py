#!/usr/bin/env python3
"""Gather the record for one area into a compact bundle, so a why reader starts from evidence.

The why lens answers from what authors wrote down, not from the code. That record is scattered:
commit messages, the pull requests they came from, decision docs. Reading it raw costs context
fast, so this script collects the parts that touch one area and nothing else:

  * commits that touched the area's paths (newest first, capped), each with its sha, date,
    subject, a trimmed body and the pull request numbers it mentions ("#123", "Merge pull
    request #123", "(#123)"); the first commit to add each path is always kept, because that
    is where intent is usually stated
  * decision docs (ADRs, RFCs, docs/decisions) that mention the area's id or one of its paths,
    or that the area's own files point at
  * whether history is shallow, so a thin record is reported as thin, not as "no reasons"

No model, no network: pull requests are listed by number for the reader to open through the
vcs_host adapter (`get_pr`), read operations only. Author names and emails are left out on
purpose; the why lens cites what was decided, not who to blame.

Usage: record_bundle.py --repo <checkout> --area ID --paths p1 [p2 ...] --out bundle.json
                        [--limit 40] [--decisions d1.md ...] [--body-chars 600]
Stdout: one JSON line {ok, commits, prs, decisions, shallow, out}. Exit 0 ok, 2 bad usage.
"""
import argparse
import json
import os
import re
import subprocess
import sys

PR = re.compile(r"(?:Merge pull request #|pull request #|\(#|\bPR #?|(?<![\w/])#)(\d{1,6})\b")
SEP = "\x1e"
FMT = "%H%x1f%cs%x1f%s%x1f%b" + "%x1e"


def git(repo, *args):
    p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def is_git_root(repo):
    top = git(repo, "rev-parse", "--show-toplevel")
    return bool(top) and os.path.realpath(top.strip()) == os.path.realpath(repo)


def parse_log(out, body_chars):
    rows = []
    for rec in (out or "").split(SEP):
        rec = rec.strip("\n")
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) < 4:
            continue
        sha, date, subject, body = parts[0].strip(), parts[1], parts[2], parts[3].strip()
        prs = sorted({int(n) for n in PR.findall(subject + "\n" + body)})
        row = {"sha": sha, "date": date, "subject": subject, "prs": prs}
        if body:
            row["body"] = body[:body_chars] + ("..." if len(body) > body_chars else "")
        rows.append(row)
    return rows


def bundle(a):
    repo = os.path.abspath(a.repo)
    out = {"area": a.area, "paths": a.paths, "commits": [], "prs": [], "decisions": [],
           "shallow": False, "notes": []}
    if not is_git_root(repo):
        out["notes"].append("not a git checkout: no commits or pull requests to read; decision "
                            "docs only")
    else:
        out["shallow"] = (git(repo, "rev-parse", "--is-shallow-repository") or "").strip() == "true"
        if out["shallow"]:
            out["notes"].append("shallow clone: only the newest history is here, so absent "
                                "reasons may just be cut off")
        paths = [p.rstrip("/") or "." for p in a.paths]
        recent = parse_log(git(repo, "log", "-n", str(a.limit), "--format=" + FMT, "--", *paths),
                           a.body_chars)
        firsts = []
        for p in paths:
            # the oldest commit that added a file here. (git applies -n before --reverse and
            # before --diff-filter, so ask for every adding commit and keep the last one.)
            adds = (git(repo, "log", "--diff-filter=A", "--format=%H", "--", p) or "").split()
            if adds and adds[-1] not in {f["sha"] for f in firsts}:
                firsts += parse_log(git(repo, "log", "-1", "--format=" + FMT, adds[-1]),
                                    a.body_chars)
        seen, merged = set(), []
        for row in recent + firsts:
            if row["sha"] in seen:
                continue
            seen.add(row["sha"])
            row["introduced"] = any(f["sha"] == row["sha"] for f in firsts)
            merged.append(row)
        out["commits"] = merged
        out["prs"] = sorted({n for r in merged for n in r["prs"]})
        if len(recent) >= a.limit:
            out["notes"].append("history capped at %d commits; older reasons may exist" % a.limit)
    needles = [a.area] + [p.rstrip("/") for p in a.paths if p.strip("/")]
    area_text = ""  # the area's own files, to catch code that points at a decision doc
    for p in a.paths:
        full = os.path.join(repo, p)
        files = [full] if os.path.isfile(full) else [
            os.path.join(d, f) for d, _, fs in os.walk(full) for f in sorted(fs)]
        for f in sorted(files):
            try:
                if os.path.getsize(f) <= 1_000_000:
                    area_text += open(f, encoding="utf-8", errors="replace").read()
            except OSError:
                pass
    for d in sorted(set(a.decisions or [])):
        full = os.path.join(repo, d)
        try:
            text = open(full, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        hits = [n for n in needles if n and n in text]
        if d in area_text or os.path.basename(d) in area_text:
            hits.append("cited-by-code")
        if hits:
            first = next((l.strip("# ").strip() for l in text.splitlines() if l.strip()), "")
            out["decisions"].append({"path": d, "title": first[:120], "mentions": hits})
    if not out["commits"] and not out["decisions"]:
        out["notes"].append("no record touches this area; every why claim will be an inference "
                            "or a gap")
    return out


def main():
    ap = argparse.ArgumentParser(description="gather one area's record for the why lens")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--area", required=True)
    ap.add_argument("--paths", nargs="+", required=True)
    ap.add_argument("--decisions", nargs="*", default=[])
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--body-chars", type=int, default=600)
    a = ap.parse_args()
    if not os.path.isdir(a.repo):
        print(json.dumps({"ok": False, "error": "repo not found"}))
        return 2
    b = bundle(a)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(b, fh, indent=2)
        fh.write("\n")
    print(json.dumps({"ok": True, "commits": len(b["commits"]), "prs": len(b["prs"]),
                      "decisions": len(b["decisions"]), "shallow": b["shallow"], "out": a.out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
