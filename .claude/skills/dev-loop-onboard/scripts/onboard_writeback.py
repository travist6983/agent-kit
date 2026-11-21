#!/usr/bin/env python3
"""onboard_writeback: what the onboarding loop writes to the shared brain, planned then applied.

Only navigational claims reach the brain, because they are what a later run on this repo needs
first and goes stale slowest: who owns a piece of state (owner), where new code of a kind belongs
(rule), traps (gotcha), how to build and test (command) and where things live (where). How, what
and why claims stay in the map file, which is kept beside the run. Inferences never go in.

Which status a claim gets:
  * verified (a quiz round stamped verified_round)          -> lessons, status active
  * unverified, and the map passed                         -> lessons, status proposed, for a person
  * unverified, and the map did not pass                   -> not written; it stays in the map file
--all-proposed writes every claim as proposed instead, for a reviewer who wants to bless each one.

Metrics, from every quiz round under --quiz-root: onboard_quiz_score per round,
onboard_bad_key_rate per round, and onboard_rounds_to_pass when a round passed.

DRY RUN BY DEFAULT: it prints the plan (every kb call, argv and all) and changes nothing.
--apply runs the calls through the qa-kb CLI and reports each result; a failed call is reported,
never skipped silently. Usage:
  onboard_writeback.py --map <run>/map.json --quiz-root <run>/quiz --project <name>
                       [--loop-id ID] [--run-id ID] [--all-proposed] [--out plan.json]
                       [--apply] [--kb <path to kb.py>]
Exit 0 planned or all applied, 1 a kb call failed, 2 bad usage.
"""
import argparse
import glob
import json
import os
import subprocess
import sys

ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", ".."))
DEFAULT_KB = os.path.join(ROOT, ".claude", "skills", "qa-kb", "scripts", "kb.py")
NAVIGATIONAL = ("owner", "rule", "gotcha", "command", "where")
SOURCE = "dev-loop-onboard"


def rounds(root):
    out = []
    for p in glob.glob(os.path.join(root, "round-*", "quiz.json")):
        try:
            q = json.load(open(p))
            out.append((int(q.get("round", 0)), q))
        except (OSError, ValueError, TypeError):
            continue
    return [q for _, q in sorted(out, key=lambda t: t[0])]


def where(c):
    for ci in c.get("cites", []):
        if ci.get("path"):
            if ci.get("line_start"):
                return "%s:%d" % (ci["path"], ci["line_start"])
            return ci["path"]
    return ""


def tags(a):
    t = ["--project", a.project]
    if a.loop_id:
        t += ["--loop-id", a.loop_id]
    if a.run_id:
        t += ["--run-id", a.run_id]
    return t


def plan(a, m, quizzes):
    passed = any(q.get("status") == "pass" for q in quizzes)
    calls, held = [], []
    for c in sorted(m.get("claims", []), key=lambda c: c.get("id", "")):
        if c.get("kind") not in NAVIGATIONAL or c.get("source") == "inference":
            continue
        ok = isinstance(c.get("verified_round"), int)
        if a.all_proposed:
            status = "proposed"
        elif ok:
            status = "active"
        elif passed:
            status = "proposed"
        else:
            held.append(c["id"])
            continue
        loc = where(c)
        text = c["text"].strip() + (" (%s)" % loc if loc else "")
        meta = {"claim_id": c["id"], "claim_kind": c["kind"], "commit": m.get("commit", ""),
                "verified_round": c.get("verified_round"), "map_passed": passed,
                "cites": c.get("cites", [])}
        calls.append({"store": "lessons", "claim_id": c["id"], "status": status,
                      "argv": ["write", "lessons", "--kind", "codebase", "--area", c.get("area", ""),
                               "--text", text, "--status", status, "--source", SOURCE,
                               "--metadata", json.dumps(meta, sort_keys=True)] + tags(a)})
    for q in quizzes:
        rnd = int(q.get("round", 0))
        meta = json.dumps({"round": rnd, "status": q.get("status"), "commit": q.get("commit", "")},
                          sort_keys=True)
        cnt = q.get("counts", {})
        total = cnt.get("total") or 0
        metrics = [("onboard_quiz_score", float(q.get("score", 0.0)), "ratio")]
        if total:
            metrics.append(("onboard_bad_key_rate", round(cnt.get("discarded", 0) / total, 4), "ratio"))
        for name, val, unit in metrics:
            calls.append({"store": "metrics", "name": name, "round": rnd,
                          "argv": ["metric", "--name", name, "--value", repr(val), "--unit", unit,
                                   "--source", SOURCE, "--metadata", meta] + tags(a)})
    first_pass = next((int(q.get("round", 0)) for q in quizzes if q.get("status") == "pass"), None)
    if first_pass:
        calls.append({"store": "metrics", "name": "onboard_rounds_to_pass", "round": first_pass,
                      "argv": ["metric", "--name", "onboard_rounds_to_pass", "--value", str(float(first_pass)),
                               "--unit", "rounds", "--source", SOURCE] + tags(a)})
    return {"project": a.project, "commit": m.get("commit", ""), "map_passed": passed,
            "rounds": len(quizzes), "held_in_map": held, "calls": calls,
            "summary": {"active": sum(1 for c in calls if c.get("status") == "active"),
                        "proposed": sum(1 for c in calls if c.get("status") == "proposed"),
                        "metrics": sum(1 for c in calls if c["store"] == "metrics"),
                        "held_in_map": len(held)}}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--map", required=True)
    ap.add_argument("--quiz-root", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--loop-id")
    ap.add_argument("--run-id")
    ap.add_argument("--all-proposed", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--kb", default=os.environ.get("KB_BIN") or DEFAULT_KB)
    a = ap.parse_args()
    try:
        m = json.load(open(a.map))
    except (OSError, ValueError) as e:
        print(json.dumps({"ok": False, "error": "unreadable map: %s" % e}))
        return 2
    if not os.path.isdir(a.quiz_root):
        print(json.dumps({"ok": False, "error": "quiz root not found: %s" % a.quiz_root}))
        return 2
    p = plan(a, m, rounds(a.quiz_root))
    p["mode"] = "apply" if a.apply else "dry-run"
    failed = 0
    if a.apply:
        for call in p["calls"]:
            r = subprocess.run([sys.executable, a.kb] + call["argv"], capture_output=True, text=True)
            call["exit"] = r.returncode
            call["result"] = (r.stdout or r.stderr).strip()[-400:]
            failed += r.returncode != 0
        p["failed"] = failed
    text = json.dumps(p, indent=2)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text + "\n")
    print(text)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
