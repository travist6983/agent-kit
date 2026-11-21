#!/usr/bin/env python3
"""record_gap: keep the questions a person asked that the map could not answer (deterministic, tested).

When teaching from the map, a question the map cannot answer is the most useful thing the session
produces: it is exactly what the next relearn round should read for. This appends it to the run's
teach-gaps.json (one entry per distinct question, matched case and space insensitively), and
--jobs turns the open gaps into relearn jobs in the same shape onboard_router.py writes, so the
recipe runs them with the readers it already has: a how gap goes to dev-onboard-how in missing
mode, a why gap to dev-onboard-why in missing mode. A gap with no known area is listed under
`unplaced`; the recipe places it with the survey before anyone reads.

The person's question is recorded as they asked it. Nothing here answers it: an answer comes only
from a reader that cites the code or the record.

Usage:
  record_gap.py --gaps <run>/teach-gaps.json --question "..." [--area <id>] [--lens how|why] [--at T]
  record_gap.py --gaps <run>/teach-gaps.json --jobs [--map <run>/map.json] [--out jobs.json]
  record_gap.py --gaps <run>/teach-gaps.json --close g2 --by-round 2
--map, with --jobs, moves a gap whose area the map does not have into `unplaced`.
Prints JSON. Exit 0, or 2 on bad usage.
"""
import argparse
import datetime
import json
import os
import sys


def norm(q):
    return " ".join(q.lower().split()).rstrip("?. ")


def load(path):
    if os.path.isfile(path) and os.path.getsize(path):
        with open(path) as fh:
            return json.load(fh)
    return {"gaps": []}


def save(path, data):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gaps", required=True)
    ap.add_argument("--question")
    ap.add_argument("--area", default="")
    ap.add_argument("--lens", choices=["how", "why"], default="how")
    ap.add_argument("--at")
    ap.add_argument("--jobs", action="store_true")
    ap.add_argument("--map")
    ap.add_argument("--out")
    ap.add_argument("--close")
    ap.add_argument("--by-round", type=int)
    a = ap.parse_args()
    try:
        data = load(a.gaps)
        gaps = data["gaps"]
    except (OSError, ValueError, KeyError) as e:
        print(json.dumps({"ok": False, "error": "unreadable gaps file: %s" % e}))
        return 2

    if a.question is not None:
        if not norm(a.question):
            print(json.dumps({"ok": False, "error": "empty question"}))
            return 2
        for g in gaps:
            if norm(g["question"]) == norm(a.question):
                g["asked"] = g.get("asked", 1) + 1
                if a.area and not g.get("area"):
                    g["area"] = a.area
                save(a.gaps, data)
                print(json.dumps({"ok": True, "action": "bump", "id": g["id"], "asked": g["asked"]}))
                return 0
        nid = "g%d" % (max([int(g["id"][1:]) for g in gaps if g["id"][1:].isdigit()] or [0]) + 1)
        at = a.at or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        gaps.append({"id": nid, "question": a.question.strip(), "area": a.area, "lens": a.lens,
                     "asked": 1, "asked_at": at, "status": "open"})
        save(a.gaps, data)
        print(json.dumps({"ok": True, "action": "insert", "id": nid}))
        return 0

    if a.close:
        for g in gaps:
            if g["id"] == a.close:
                g["status"] = "closed"
                if a.by_round:
                    g["closed_round"] = a.by_round
                save(a.gaps, data)
                print(json.dumps({"ok": True, "action": "close", "id": g["id"]}))
                return 0
        print(json.dumps({"ok": False, "error": "no gap %s" % a.close}))
        return 2

    if a.jobs:
        known = None
        if a.map:
            try:
                known = {ar["id"] for ar in json.load(open(a.map))["areas"]}
            except (OSError, ValueError, KeyError) as e:
                print(json.dumps({"ok": False, "error": "unreadable map: %s" % e}))
                return 2
        jobs, unplaced = {}, []
        for g in gaps:
            if g.get("status") != "open":
                continue
            if not g.get("area") or (known is not None and g["area"] not in known):
                unplaced.append({"id": g["id"], "question": g["question"], "lens": g["lens"]})
                continue
            skill = "dev-onboard-why" if g["lens"] == "why" else "dev-onboard-how"
            j = jobs.setdefault((g["area"], skill), {"area": g["area"], "skill": skill, "mode": "missing",
                                                     "questions": [], "topics": []})
            j["questions"].append(g["id"])
            j["topics"].append(g["question"])
        res = {"ok": True, "jobs": [jobs[k] for k in sorted(jobs)], "unplaced": unplaced}
        text = json.dumps(res, indent=2)
        if a.out:
            with open(a.out, "w") as fh:
                fh.write(text + "\n")
        print(text)
        return 0

    print(json.dumps({"ok": False, "error": "nothing to do: pass --question, --close or --jobs"}))
    return 2


if __name__ == "__main__":
    sys.exit(main())
