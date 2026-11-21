#!/usr/bin/env python3
"""Lint a onboarding-loop map (contracts/dev-onboard-map.json) before the quiz sees it.

The quiz grades what the map knows. This refuses a map that would make the quiz's grade mean
nothing, or that breaks the architect's rules:

  errors (exit 1)
  * a claim with no cites, unless it is a why inference
  * a code cite that does not resolve (missing file, lines out of range, symbol absent, a path
    that climbs out of the repo), checked with qa-onboard-quiz/scripts/cites.py
  * a why claim that is an inference but says `certain`, or a claim with evidence `said` that is
    not an inference
  * a claim for an area the map does not have, or a duplicate claim id
  * two owners: `owner` claims in different areas that cite the same file and symbol. Each piece
    of state has one owner; two means the map is confused about where it lives
  * an area that depends on an area the map does not have
  warnings (reported, exit 0)
  * an area with no claims, or with how claims but no owner or rule claim (the architect lens is
    missing for it)
  * survey code that no area's paths cover (with --survey)
  * claims citing only files outside their own area

Usage: check_map.py --repo <checkout> --map map.json [--survey survey.json]
Stdout: one JSON line {ok, claims, areas, errors, warnings}. Exit 0 ok, 1 errors, 2 bad usage.
"""
import argparse
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, ".claude", "skills", "qa-onboard-quiz", "scripts"))
import cites as C  # noqa: E402


def in_paths(path, prefixes):
    return any(path == p or (p.endswith("/") and path.startswith(p)) or path.startswith(p.rstrip("/") + "/")
               for p in prefixes)


def check(repo, m, survey=None):
    errors, warnings = [], []
    areas = {a["id"]: a for a in m.get("areas", [])}
    seen = set()
    per_area = defaultdict(list)
    owners = defaultdict(set)
    for c in m.get("claims", []):
        cid, area, kind = c.get("id", "?"), c.get("area"), c.get("kind")
        if cid in seen:
            errors.append("%s: duplicate id" % cid)
        seen.add(cid)
        if area not in areas:
            errors.append("%s: area %r is not in the map" % (cid, area))
        per_area[area].append(c)
        cl = [x for x in (c.get("cites") or []) if isinstance(x, dict)]
        inference = c.get("source") == "inference"
        if not cl and not (kind == "why" and inference):
            errors.append("%s: no cites" % cid)
        if inference and c.get("confidence") == "certain":
            errors.append("%s: an inference cannot be certain" % cid)
        if c.get("evidence") == "said" and not inference:
            errors.append("%s: evidence 'said' is only allowed on an inference" % cid)
        code = [x for x in cl if x.get("path")]
        for x in cl:
            res = C.check_cite(repo, x)
            if res != "ok":
                errors.append("%s: cite %s is %s" % (cid, x.get("path") or x.get("ref"), res))
        if kind == "owner":
            for x in code:
                if x.get("symbol"):
                    owners[(x["path"], x["symbol"])].add(area)
        if code and area in areas and not any(in_paths(x["path"], areas[area].get("paths", []))
                                              for x in code):
            warnings.append("%s: cites only files outside area %s" % (cid, area))
    for (path, sym), who in sorted(owners.items()):
        if len(who) > 1:
            errors.append("two owners: %s (%s) is claimed by %s" % (sym, path, ", ".join(sorted(who))))
    for aid, a in areas.items():
        for d in a.get("depends_on", []):
            if d not in areas:
                errors.append("area %s depends on %r, which the map does not have" % (aid, d))
        cs = per_area.get(aid, [])
        if not cs:
            warnings.append("area %s has no claims" % aid)
        elif any(c.get("kind") == "how" for c in cs) and not any(c.get("kind") in ("owner", "rule") for c in cs):
            warnings.append("area %s says how things work but not who owns what or where new code goes" % aid)
    if survey:
        prefixes = [p for a in areas.values() for p in a.get("paths", [])]
        for sa in survey.get("areas", []):
            for p in sa.get("paths", []):
                if not in_paths(p, prefixes) and not any(in_paths(q, [p]) for q in prefixes):
                    warnings.append("survey path %s is in no map area" % p)
    return {"ok": not errors, "claims": len(m.get("claims", [])), "areas": len(areas),
            "errors": errors, "warnings": warnings}


def main():
    ap = argparse.ArgumentParser(description="lint a onboarding-loop map")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--survey")
    a = ap.parse_args()
    if not os.path.isdir(a.repo) or not os.path.isfile(a.map):
        print(json.dumps({"ok": False, "errors": ["repo or map not found"]}))
        return 2
    m = json.load(open(a.map))
    s = json.load(open(a.survey)) if a.survey and os.path.isfile(a.survey) else None
    res = check(os.path.abspath(a.repo), m, s)
    print(json.dumps(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
