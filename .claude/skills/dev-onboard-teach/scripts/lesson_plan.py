#!/usr/bin/env python3
"""lesson_plan: the order to explain a codebase map to a person in (deterministic, tested).

A person learns a system best from where work enters it, outward, one part at a time. This turns
map.json into that order without a model: first the summary and the commands, then the areas,
starting with an area nothing else calls (where requests come in) and following its depends_on
all the way down before the next such area, ties by id. An area in a cycle or reachable from nothing comes last, in id
order, so every area appears exactly once.

Within an area, claims go plain to deep: what it is, where things live, how it runs, who owns
what, where new code goes, traps, commands, then why. Every claim carries its standing, so the
teacher never states more than the map knows:
  verified           a quiz question that leaned on it was answered correctly
  unverified         in the map, not yet tested by the quiz
  inference-likely / inference-guessing   a why claim the record does not state outright

diagram_steps builds the area diagram up one area at a time (Mermaid flowchart text), so a
person sees each new box arrive with the one or two arrows it brings.

Usage:
  lesson_plan.py --map <run>/map.json [--focus <area id>] [--out plan.json]
--focus teaches one area, with the areas it depends on named as context only.
Prints the plan as JSON. Exit 0, or 2 on bad usage (unreadable map, unknown focus area).
"""
import argparse
import json
import sys

KIND_ORDER = ("what", "where", "how", "owner", "rule", "gotcha", "command", "why")


def standing(c):
    if c.get("source") == "inference":
        return "inference-" + (c.get("confidence") or "guessing")
    return "verified" if isinstance(c.get("verified_round"), int) else "unverified"


def cite(c):
    for ci in c.get("cites", []):
        if ci.get("path"):
            if ci.get("line_start"):
                end = ci.get("line_end") or ci["line_start"]
                return "%s:%d-%d" % (ci["path"], ci["line_start"], end) if end != ci["line_start"] \
                    else "%s:%d" % (ci["path"], ci["line_start"])
            return ci["path"]
        if ci.get("ref"):
            return ci["ref"]
    return ""


def order(areas):
    ids = sorted(a["id"] for a in areas)
    deps = {a["id"]: [d for d in a.get("depends_on", []) if d in ids] for a in areas}
    called = {d for ds in deps.values() for d in ds}
    seen, out = set(), []

    def visit(i):
        if i in seen:
            return
        seen.add(i)
        out.append(i)
        for d in sorted(deps[i]):
            visit(d)

    for i in ids:
        if i not in called:
            visit(i)
    out.extend(i for i in ids if i not in seen)
    return out, deps


def node(i):
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in i)


def diagram_steps(seq, deps, names):
    steps, shown = [], []
    for i in seq:
        shown.append(i)
        lines = ["flowchart LR"]
        for s in shown:
            lines.append('  %s["%s"]' % (node(s), names[s].replace('"', "'")))
        for s in shown:
            for d in deps[s]:
                if d in shown:
                    lines.append("  %s --> %s" % (node(s), node(d)))
        steps.append({"adds": i, "mermaid": "\n".join(lines)})
    return steps


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--map", required=True)
    ap.add_argument("--focus")
    ap.add_argument("--out")
    a = ap.parse_args()
    try:
        m = json.load(open(a.map))
        areas = m["areas"]
    except (OSError, ValueError, KeyError) as e:
        print(json.dumps({"ok": False, "error": "unreadable map: %s" % e}))
        return 2
    by_id = {ar["id"]: ar for ar in areas}
    names = {ar["id"]: ar.get("name") or ar["id"] for ar in areas}
    seq, deps = order(areas)
    context = []
    if a.focus:
        if a.focus not in by_id:
            print(json.dumps({"ok": False, "error": "unknown area %r; areas: %s" % (a.focus, ", ".join(seq))}))
            return 2
        context = deps[a.focus]
        seq = [a.focus]
    rank = {k: i for i, k in enumerate(KIND_ORDER)}
    claims = {}
    for c in m.get("claims", []):
        claims.setdefault(c.get("area"), []).append(c)
    sections = []
    for i in seq:
        cs = sorted(claims.get(i, []), key=lambda c: (rank.get(c.get("kind"), len(rank)), c.get("id", "")))
        sections.append({
            "area": i, "name": names[i], "purpose": by_id[i].get("purpose", ""),
            "depends_on": deps[i],
            "claims": [{"id": c["id"], "kind": c.get("kind"), "text": c.get("text", ""),
                        "cite": cite(c), "standing": standing(c)} for c in cs],
            "empty": not cs})
    plan = {"ok": True, "project": m.get("project", ""), "commit": m.get("commit", ""),
            "summary": m.get("summary", ""), "commands": m.get("commands", {}),
            "order": seq, "context_areas": context, "sections": sections,
            "open_questions": m.get("open_questions", []),
            "diagram_steps": diagram_steps(context + seq, deps, names)}
    counts = {}
    for s in sections:
        for c in s["claims"]:
            counts[c["standing"]] = counts.get(c["standing"], 0) + 1
    plan["standing_counts"] = dict(sorted(counts.items()))
    text = json.dumps(plan, indent=2)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
