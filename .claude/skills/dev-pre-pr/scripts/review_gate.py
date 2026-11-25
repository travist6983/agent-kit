#!/usr/bin/env python3
"""dev-pre-pr's code-review gate — deterministic, zero model calls.

The invariant: a review.json that reaches pre-pr must be one the review checks accept. Before
this gate, finding_votes.py / review_depth.py / review_brief.py each had a `check` verb that no
gate called, so an old-style review (one context, no validation, no depth record) still passed.

Usage:
  python3 review_gate.py --evid <EVID_ROOT>/<KEY> --repo <dir> [--base B] [--require-review]

  --require-review   the recipe has a code-review stage (dev-loop-hard-defect,
                     dev-loop-feature-complete): a missing review.json BLOCKS. Without it
                     (dev-loop-defect has no review stage) a missing review.json is
                     not-applicable and passes. A PRESENT review.json is always checked.

What a present review.json must satisfy (every one fails closed):
  * it parses as a JSON object;
  * it carries a `validation` block (independent validators, finding_votes.py tally) and a
    `depth` block (review_depth.py record);
  * finding_votes.py check --dir <evid>/review-validation passes;
  * review_depth.py check passes (with --evid, so an in-loop review is floored at standard);
  * review_brief.py check passes (it accepts a review with no brief configured).

Exit codes: 0 pass (or not-applicable) · 1 BLOCK, the caller emits status=blocked ·
2 bad usage. Stdout is one JSON object: {status, reason, checks, next_action}.
A check script that cannot run (missing, crashes) is a BLOCK, never a pass.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REVIEW_SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "..", "dev-code-review", "scripts"))


def run_check(name, args):
    script = os.path.join(REVIEW_SCRIPTS, name)
    if not os.path.isfile(script):
        return {"check": name, "rc": None, "ok": False, "detail": "check script missing"}
    try:
        p = subprocess.run([sys.executable, script, "check", *args],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001 — a check that cannot run blocks
        return {"check": name, "rc": None, "ok": False, "detail": f"could not run: {e}"}
    detail = (p.stdout or p.stderr or "").strip()[-600:]
    return {"check": name, "rc": p.returncode, "ok": p.returncode == 0, "detail": detail}


def emit(status, reason, checks, rc):
    nxt = {"pass": "advance", "not-applicable": "advance"}.get(status, "block: re-run dev-code-review")
    print(json.dumps({"status": status, "reason": reason, "checks": checks, "next_action": nxt}))
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evid", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base")
    ap.add_argument("--require-review", action="store_true")
    try:
        a = ap.parse_args()
    except SystemExit:
        return 2
    if not os.path.isdir(a.evid):
        return emit("blocked", f"evidence dir not found: {a.evid}", [], 1)
    review = os.path.join(a.evid, "review.json")
    if not os.path.exists(review):
        if a.require_review:
            return emit("blocked", "review-missing: this recipe has a code-review stage", [], 1)
        return emit("not-applicable", "no review.json and no code-review stage", [], 0)
    try:
        doc = json.load(open(review, encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("not an object")
    except Exception as e:  # noqa: BLE001
        return emit("blocked", f"review-unreadable: {e}", [], 1)
    missing = [k for k in ("validation", "depth") if not isinstance(doc.get(k), dict)]
    if missing:
        return emit("blocked", "review-incomplete: missing " + ", ".join(missing), [], 1)

    base = ["--base", a.base] if a.base else []
    checks = [
        run_check("finding_votes.py", ["--dir", os.path.join(a.evid, "review-validation"), "--review", review]),
        run_check("review_depth.py", ["--repo", a.repo, *base, "--evid", a.evid, "--review", review]),
        run_check("review_brief.py", ["--repo", a.repo, *base, "--review", review]),
    ]
    failed = [c["check"] for c in checks if not c["ok"]]
    if failed:
        return emit("blocked", "review-check-failed: " + ", ".join(failed), checks, 1)
    return emit("pass", "review checks passed", checks, 0)


if __name__ == "__main__":
    sys.exit(main())
