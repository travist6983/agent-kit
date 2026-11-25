#!/usr/bin/env python3
"""review_rounds — record one fingerprinted round per dev-code-review review.json (deterministic, no model).

review.json is overwritten in place every fix cycle, so on its own it cannot say whether the SAME finding
survived the last fix. This recorder keeps the history in <evid>/<KEY>/review-rounds.json (contract
contracts/dev-loop-defect.review-rounds.json), one round per DISTINCT review.json, and recipe_router.py
--review-rounds reads it to call stagnation mechanically. It also yields rounds_to_pass (the round of the
first approve), which the recipe writes as the review_rounds_to_pass metric.

Usage:
  review_rounds.py record --review <evid>/<KEY>/review.json --out <evid>/<KEY>/review-rounds.json [--key KEY]

Behaviour:
  * idempotent: when the last recorded round has the same review_sha256, nothing is added;
  * `open` = every finding that is an object and not resolved:true and not withheld:true;
  * fingerprint(finding) = first 16 hex of sha256("class|" + file + "|" + bug_class) when bug_class is set
    and not `other` (basis class), else sha256("text|" + file + "|" + dimension + "|" + normalised text)
    (basis text). The LINE is never part of it: lines move between fix cycles;
  * the file is written through contracts/emit.py, so a contract violation never lands on disk.
This script holds NO threshold: the stagnation number lives in loop-limits.json and is read by the router.

Prints the written object plus `recorded: true|false`. Exit 0 ok, 1 contract violation (nothing written),
2 bad usage or unreadable input (nothing written). Stdlib only: no network, no brain.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", ".."))
CONTRACT = os.path.join(ROOT, "contracts", "dev-loop-defect.review-rounds.json")
EMIT = os.path.join(ROOT, "contracts", "emit.py")
VERDICTS = ("approve", "request-changes", "needs-human")


def norm_path(path):
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    return p or "-"


def norm_text(text):
    t = str(text or "").lower()
    t = re.sub(r"[0-9]", "", t)
    t = re.sub(r"[^a-z]+", " ", t)
    return t.strip()


def fingerprint(finding):
    """(hex16, basis) for one finding. file + bug_class, or file + dimension + normalised text."""
    path = norm_path(finding.get("file"))
    cls = finding.get("bug_class")
    if isinstance(cls, str) and cls and cls != "other":
        raw, basis = "class|" + path + "|" + cls, "class"
    else:
        text = finding.get("issue") or finding.get("title") or finding.get("summary") or ""
        dim = str(finding.get("dimension") or "").strip().lower()
        raw, basis = "text|" + path + "|" + dim + "|" + norm_text(text), "text"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], basis


def open_items(findings):
    out = []
    for f in findings:
        if not isinstance(f, dict) or f.get("resolved") is True or f.get("withheld") is True:
            continue
        fp, basis = fingerprint(f)
        item = {"fingerprint": fp, "basis": basis, "file": norm_path(f.get("file"))}
        for k in ("bug_class", "dimension", "severity"):
            if isinstance(f.get(k), str) and f.get(k):
                item[k] = f[k]
        text = f.get("issue") or f.get("title") or f.get("summary")
        if isinstance(text, str) and text:
            item["issue"] = text[:300]
        out.append(item)
    return out


def fail(msg, code=2):
    print(json.dumps({"ok": False, "error": msg}))
    return code


def cmd_record(a):
    try:
        with open(a.review, "rb") as fh:
            raw = fh.read()
        review = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as e:
        return fail("review unreadable: %s" % e)
    if not isinstance(review, dict) or review.get("verdict") not in VERDICTS \
            or not isinstance(review.get("findings"), list):
        return fail("review is not a review.json (needs verdict approve|request-changes|needs-human and a findings list)")

    state = {"rounds": [], "rounds_to_pass": None}
    if os.path.exists(a.out):
        try:
            with open(a.out) as fh:
                state = json.load(fh)
        except (OSError, ValueError) as e:
            return fail("existing rounds file unreadable, refusing to overwrite: %s" % e)
        if not isinstance(state, dict) or not isinstance(state.get("rounds"), list) \
                or not all(isinstance(r, dict) for r in state["rounds"]):
            return fail("existing rounds file has the wrong shape, refusing to overwrite")

    sha = hashlib.sha256(raw).hexdigest()
    rounds = state["rounds"]
    recorded = False
    if not rounds or rounds[-1].get("review_sha256") != sha:
        entry = {"round": len(rounds) + 1, "verdict": review["verdict"], "review_sha256": sha}
        att = review.get("attempt")
        if isinstance(att, int) and not isinstance(att, bool) and att >= 1:
            entry["attempt"] = att
        entry["open"] = open_items(review["findings"])
        rounds.append(entry)
        recorded = True

    passed = [r.get("round") for r in rounds if r.get("verdict") == "approve"]
    doc = {}
    key = a.key or state.get("key") or review.get("key")
    if isinstance(key, str) and key:
        doc["key"] = key
    doc["rounds"] = rounds
    doc["rounds_to_pass"] = passed[0] if passed else None

    if recorded:
        p = subprocess.run([sys.executable, EMIT, "--contract", CONTRACT, "--out", a.out,
                            "--from-json", "-", "--no-timing"],
                           input=json.dumps(doc), text=True, capture_output=True)
        if p.returncode != 0:
            sys.stderr.write(p.stderr or p.stdout)
            print(json.dumps({"ok": False, "error": "contract violation, nothing written"}))
            return 1 if p.returncode == 1 else 2
    out = dict(doc)
    out["ok"] = True
    out["recorded"] = recorded
    out["out"] = a.out
    print(json.dumps(out))
    return 0


def main():
    ap = argparse.ArgumentParser(description="record one fingerprinted review round")
    sub = ap.add_subparsers(dest="verb", required=True)
    r = sub.add_parser("record")
    r.add_argument("--review", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--key")
    a = ap.parse_args()
    if a.verb == "record":
        return cmd_record(a)
    return 2


if __name__ == "__main__":
    sys.exit(main())
