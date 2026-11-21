#!/usr/bin/env python3
"""mark_verified: stamp the claims a quiz round verified onto the map (deterministic, tested).

A claim is verified the first time a question that leaned on it is answered correctly with a clean
answer cite (route.json `verified_claims`). This stamps `verified_round` with that round, keeps an
earlier round where one is already set, and validates the map against its contract before writing,
so a bad stamp never lands. dev-onboard-map's assembly then keeps the stamp only while the claim is
unchanged. Write back promotes only stamped claims as active knowledge.

Usage:
  mark_verified.py --map <run>/map.json --route <run>/quiz/round-<n>/route.json [--out <map>]
Prints {"ok": true, "marked": [...], "kept": [...], "unknown": [...]}. Exit 0 written,
1 the result would not validate (nothing written), 2 bad usage.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", ".."))
CONTRACT = os.path.join(ROOT, "contracts", "dev-onboard-map.json")
VALIDATE = os.path.join(ROOT, "contracts", "validate.py")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--map", required=True)
    ap.add_argument("--route", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    try:
        m = json.load(open(a.map))
        r = json.load(open(a.route))
        rnd = int(r["round"])
        want = list(r.get("verified_claims", []))
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(json.dumps({"ok": False, "error": "unreadable input: %s" % e}))
        return 2
    by_id = {c.get("id"): c for c in m.get("claims", [])}
    marked, kept, unknown = [], [], []
    for cid in want:
        c = by_id.get(cid)
        if c is None:
            unknown.append(cid)
        elif isinstance(c.get("verified_round"), int):
            kept.append(cid)
        else:
            c["verified_round"] = rnd
            marked.append(cid)
    out = a.out or a.map
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=os.path.dirname(os.path.abspath(out)))
    with os.fdopen(fd, "w") as fh:
        json.dump(m, fh, indent=2)
        fh.write("\n")
    p = subprocess.run([sys.executable, VALIDATE, CONTRACT, tmp], capture_output=True, text=True)
    if p.returncode != 0:
        os.unlink(tmp)
        sys.stderr.write(p.stderr or p.stdout)
        return 1
    os.replace(tmp, out)
    print(json.dumps({"ok": True, "round": rnd, "marked": marked, "kept": kept,
                      "unknown": unknown, "out": out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
