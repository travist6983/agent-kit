#!/usr/bin/env python3
"""decide: append one row to the onboarding run's decision log, decisions.tsv (tested).

One row per decision: when, which round and stage, what was decided, why, the evidence it rests on
(a file path, a claim id, a question id) and how it came out. Append only: a row is never edited,
so a reviewer who steps away can read the run in order. Tabs and newlines in a value become
spaces, so the file always stays one row per line. The header is written once, on first use.

Usage:
  decide.py --log <run>/decisions.tsv --round N --stage <stage> --decision "..." --why "..."
            [--evidence "..."] [--result "..."] [--at <ISO time>]
Prints {"ok": true, "rows": <rows after this one>}. Exit 0, or 2 on bad usage.
"""
import argparse
import datetime
import json
import os
import sys

HEADER = ["at", "round", "stage", "decision", "why", "evidence", "result"]


def clean(v):
    return " ".join(str(v or "").replace("\t", " ").split())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--log", required=True)
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--stage", required=True)
    ap.add_argument("--decision", required=True)
    ap.add_argument("--why", required=True)
    ap.add_argument("--evidence", default="")
    ap.add_argument("--result", default="")
    ap.add_argument("--at")
    a = ap.parse_args()
    if not clean(a.decision) or not clean(a.why):
        print(json.dumps({"ok": False, "error": "a decision needs both what and why"}))
        return 2
    at = a.at or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = not os.path.isfile(a.log) or os.path.getsize(a.log) == 0
    d = os.path.dirname(os.path.abspath(a.log))
    os.makedirs(d, exist_ok=True)
    with open(a.log, "a") as fh:
        if new:
            fh.write("\t".join(HEADER) + "\n")
        fh.write("\t".join(clean(v) for v in (at, a.round, a.stage, a.decision, a.why,
                                              a.evidence, a.result)) + "\n")
    with open(a.log) as fh:
        rows = sum(1 for _ in fh) - 1
    print(json.dumps({"ok": True, "rows": rows, "log": a.log}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
