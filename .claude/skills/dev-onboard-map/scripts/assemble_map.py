#!/usr/bin/env python3
"""Assemble the onboarding loop's map from the survey, the checked cards and the architect's lens.

The map (contracts/dev-onboard-map.json) is what the quiz's examiner answers from and what the
brain write-back promotes from, so its assembly is deterministic: the model's judgment arrives as
data (cards, and one architect.json), and this script merges it the same way every time.

  * areas come from survey.json, renamed, merged and described by architect.json
  * claims are the union of every card's claims plus the architect's own (owner and rule claims
    that span areas); a card in `correct` mode removes the claims it lists in drops[]
  * with --prior (a relearn round), the prior map's claims stay unless a newer card replaces
    them: a `fresh` card replaces every claim of its area and lens, any other mode adds and
    replaces by id. A claim that comes through unchanged (same id, text and cites) keeps its
    verified_round; a changed one starts unverified again, because the quiz has not tested it
  * commands are copied from the survey (the first declared command per purpose), never invented
  * open_questions are every card's gaps plus the architect's, each tagged with its area

architect.json (written by the dev-onboard-map agent; every key optional):
  {"summary": "...",
   "areas": {"<survey id>": {"id": "<new id>", "name": "...", "purpose": "..."}},
   "merge": {"<new id>": ["<survey id>", ...]},
   "claims": [ <claims with ids <area>.a<n>, same shape as card claims> ],
   "open_questions": ["..."]}

Usage: assemble_map.py --survey survey.json --cards <dir> --out map.json [--architect architect.json]
                       [--prior map.json] [--md map.md] [--started-at ISO]
Stdout: one JSON line {ok, areas, claims, dropped, kept_verified, out}. Exit 0 ok, 1 contract
violation (nothing written), 2 bad input.
"""
import argparse
import glob
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
CONTRACT = os.path.join(ROOT, "contracts", "dev-onboard-map.json")
EMIT = os.path.join(ROOT, "contracts", "emit.py")
NO_COMMIT = "0" * 40  # a tree with no git history: the staleness check treats it as stale everywhere
CLAIM_KEYS = ("id", "area", "kind", "text", "cites", "evidence", "confidence", "source",
              "verified_round")


def load(path):
    with open(path) as fh:
        return json.load(fh)


def area_mapping(survey, arch):
    """old survey id -> new map id, plus the new areas list."""
    rename = {}
    merges = arch.get("merge") or {}
    for new, olds in merges.items():
        for o in olds:
            rename[o] = new
    for old, spec in (arch.get("areas") or {}).items():
        if isinstance(spec, dict) and spec.get("id") and old not in rename:
            rename[old] = spec["id"]
    areas, order = {}, []
    for a in survey.get("areas", []):
        new = rename.get(a["id"], a["id"])
        if new not in areas:
            areas[new] = {"id": new, "name": new.replace("-", " ").title(), "paths": [],
                          "depends_on": set(), "_olds": []}
            order.append(new)
        tgt = areas[new]
        tgt["paths"] += [p for p in a.get("paths", []) if p not in tgt["paths"]]
        tgt["depends_on"] |= {rename.get(d, d) for d in a.get("depends_on", [])}
        tgt["_olds"].append(a["id"])
    for old, spec in (arch.get("areas") or {}).items():
        new = rename.get(old, old)
        if new in areas and isinstance(spec, dict):
            for k in ("name", "purpose"):
                if spec.get(k):
                    areas[new][k] = spec[k]
    out = []
    for aid in order:
        a = areas[aid]
        a["depends_on"] = sorted(a["depends_on"] - {aid})
        a.pop("_olds")
        out.append(a)
    return rename, out


def rehome(claim, rename, taken):
    """Move a claim to its renamed or merged area, keeping its id stable when it can."""
    c = {k: claim[k] for k in CLAIM_KEYS if k in claim}
    old = c.get("area")
    new = rename.get(old, old)
    if new != old:
        suffix = c["id"].split(".", 1)[1] if "." in c["id"] else c["id"]
        cid = "%s.%s" % (new, suffix)
        if cid in taken:
            cid = "%s.%s-%s" % (new, old, suffix)
        c["id"], c["area"] = cid, new
    return c


def lens_of(claim_id):
    """invoices.h3 -> how; core.invoices-w2 (merged) -> why; invoices.a1 -> architect."""
    suffix = claim_id.rsplit(".", 1)[-1].rsplit("-", 1)[-1]
    return {"h": "how", "w": "why", "a": "architect"}.get(suffix[:1], "other")


def same(a, b):
    return all(a.get(k) == b.get(k) for k in ("text", "cites", "kind", "area"))


def assemble(a):
    survey = load(a.survey)
    arch = load(a.architect) if a.architect else {}
    prior = load(a.prior) if a.prior else None
    rename, areas = area_mapping(survey, arch)
    area_ids = {x["id"] for x in areas}

    cards = []
    for p in sorted(glob.glob(os.path.join(a.cards, "*.how.json")) +
                    glob.glob(os.path.join(a.cards, "*.why.json"))):
        cards.append(load(p))

    claims = {}  # id -> claim, insertion-ordered
    prior_by_id = {}
    if prior:
        for c in prior.get("claims", []):
            prior_by_id[c["id"]] = c
            claims[c["id"]] = dict(c)

    dropped = set()
    gaps = []
    for card in cards:
        lens, mode = card.get("lens"), card.get("mode", "fresh")
        area = rename.get(card.get("area"), card.get("area"))
        if mode == "fresh":
            for cid in [k for k, c in claims.items()
                        if c.get("area") == area and lens_of(k) == lens]:
                claims.pop(cid)
        for d in card.get("drops") or []:
            did = d.get("id")
            if did in claims:
                claims.pop(did)
                dropped.add(did)
        for c in card.get("claims") or []:
            nc = rehome(c, rename, claims)
            claims[nc["id"]] = nc
        for g in card.get("gaps") or []:
            gaps.append("[%s] %s" % (area, g))
    for c in arch.get("claims") or []:
        nc = rehome(c, rename, claims)
        claims[nc["id"]] = nc

    kept = 0
    for cid, c in claims.items():
        pc = prior_by_id.get(cid)
        if pc is not None and same(c, pc) and pc.get("verified_round"):
            c["verified_round"] = pc["verified_round"]
            kept += 1
        else:
            c["verified_round"] = None

    commands = {}
    for cmd in survey.get("commands", []):
        p = cmd.get("purpose")
        if p and p != "other" and p not in commands and cmd.get("source") == "declared":
            commands[p] = cmd["run"]
    for cmd in survey.get("commands", []):
        p = cmd.get("purpose")
        if p and p != "other" and p not in commands:
            commands[p] = cmd["run"]

    open_q = []
    for q in gaps + list(arch.get("open_questions") or []):
        if q not in open_q:
            open_q.append(q)

    unknown = sorted({c["area"] for c in claims.values() if c.get("area") not in area_ids})
    if unknown:
        raise ValueError("claims for areas the survey does not have: %s" % ", ".join(unknown))
    summary = arch.get("summary") or next(
        (c.get("summary") for c in cards if c.get("lens") == "how" and c.get("summary")), "")
    return {
        "project": survey.get("project"),
        "commit": survey.get("commit") or NO_COMMIT,
        "summary": summary,
        "areas": areas,
        "claims": list(claims.values()),
        "commands": commands,
        "open_questions": open_q,
    }, {"dropped": len(dropped), "kept_verified": kept}


def render_md(m):
    """A plain reading of the map for a person; dev-onboard-teach starts here."""
    out = ["# %s" % m["project"], "", m.get("summary") or "_(no summary yet)_", ""]
    if m.get("commands"):
        out += ["## Commands", ""] + ["- **%s:** `%s`" % kv for kv in sorted(m["commands"].items())] + [""]
    out += ["## Areas", ""]
    for ar in m["areas"]:
        purpose = (": " + ar["purpose"]) if ar.get("purpose") else ""
        out.append("- **%s** (`%s`)%s" % (ar.get("name", ar["id"]), ", ".join(ar["paths"]), purpose))
    out.append("")
    for ar in m["areas"]:
        cs = [c for c in m["claims"] if c["area"] == ar["id"]]
        if not cs:
            continue
        out += ["## %s" % ar.get("name", ar["id"]), ""]
        for c in cs:
            cites = "; ".join(
                ("%s:%s-%s" % (x["path"], x.get("line_start"), x.get("line_end"))) if x.get("path")
                else x.get("ref", "") for x in c.get("cites", []))
            hedge = "" if c.get("source") != "inference" else " (inference, %s)" % c.get("confidence")
            out.append("- `%s` %s: %s%s%s" % (c["id"], c["kind"], c["text"], hedge,
                                               (" [%s]" % cites) if cites else ""))
        out.append("")
    if m.get("open_questions"):
        out += ["## Open questions", ""] + ["- %s" % q for q in m["open_questions"]] + [""]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="assemble the onboarding loop's map")
    ap.add_argument("--survey", required=True)
    ap.add_argument("--cards", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--architect")
    ap.add_argument("--prior")
    ap.add_argument("--md")
    ap.add_argument("--started-at")
    a = ap.parse_args()
    for p in (a.survey, a.architect, a.prior):
        if p and not os.path.isfile(p):
            print(json.dumps({"ok": False, "error": "not found: %s" % p}))
            return 2
    if not os.path.isdir(a.cards):
        print(json.dumps({"ok": False, "error": "cards dir not found"}))
        return 2
    try:
        m, stats = assemble(a)
    except (ValueError, KeyError, TypeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    cmd = [sys.executable, EMIT, "--contract", CONTRACT, "--out", a.out, "--from-json", "-"]
    if a.started_at:
        cmd += ["--started-at", a.started_at]
    p = subprocess.run(cmd, input=json.dumps(m), text=True, capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr or p.stdout)
        return 1
    if a.md:
        with open(a.md, "w") as fh:
            fh.write(render_md(m) + "\n")
    print(json.dumps(dict(ok=True, areas=len(m["areas"]), claims=len(m["claims"]), out=a.out, **stats)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
