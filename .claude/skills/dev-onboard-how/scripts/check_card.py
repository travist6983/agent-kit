#!/usr/bin/env python3
"""Check a onboarding-loop card (contracts/dev-onboard-card.json) against the repo before the map reads it.

The contract checks shape. This checks truth that a script can check:
  * every code cite resolves: the file exists inside the repo, the lines are in range, and the
    named symbol sits in (or within a few lines of) the cited range (qa-onboard-quiz/scripts/cites.py)
  * the lenses stay apart: a how card makes no why claims; a why card makes only why claims
  * a how claim cites code and was at least pointed at; `said` is never enough for code
  * a why claim from the record cites the record: commit:<sha> must exist in the repo's history
    when the repo is a git checkout, doc:<path> must be a real file, pr:<n> is accepted as given
    (the vcs_host adapter is the only thing that can open it)
  * a why claim that is an inference is never `certain`, and is the only claim allowed no cites
  * ids are unique and carry the area and lens prefix (<area>.h<n> / <area>.w<n>)

Usage: check_card.py --repo <checkout> --card <cards/area.lens.json> [--expect-area ID] [--expect-lens how|why]
Stdout: one JSON line {ok, area, lens, claims, errors:[...], warnings:[...]}. Exit 0 ok, 1 errors, 2 bad usage.
Each error names the claim id, so a card can be handed back to its reader once with the list.
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, ".claude", "skills", "qa-onboard-quiz", "scripts"))
import cites as C  # noqa: E402

REF = re.compile(r"^(commit|pr|doc|issue):(.+)$")


def commit_exists(repo, sha, _cache={}):
    """True / False under git, None when the repo has no history to ask."""
    key = (repo, sha)
    if key not in _cache:
        top = subprocess.run(["git", "-C", repo, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
        if top.returncode != 0 or os.path.realpath(top.stdout.strip()) != os.path.realpath(repo):
            _cache[key] = None
        else:
            p = subprocess.run(["git", "-C", repo, "cat-file", "-e", sha + "^{commit}"],
                               capture_output=True)
            _cache[key] = p.returncode == 0
    return _cache[key]


def check(repo, card, expect_area=None, expect_lens=None):
    errors, warnings = [], []
    area, lens = card.get("area"), card.get("lens")
    if expect_area and area != expect_area:
        errors.append("card: area is %r, expected %r" % (area, expect_area))
    if expect_lens and lens != expect_lens:
        errors.append("card: lens is %r, expected %r" % (lens, expect_lens))
    letter = {"how": "h", "why": "w"}.get(lens, "?")
    seen = set()
    claims = card.get("claims") or []
    for c in claims:
        cid = c.get("id", "?")
        if cid in seen:
            errors.append("%s: duplicate id" % cid)
        seen.add(cid)
        if not re.match(r"^%s\.%s\d+$" % (re.escape(str(area)), letter), cid):
            errors.append("%s: id must look like %s.%s<n>" % (cid, area, letter))
        if c.get("area") != area:
            errors.append("%s: claim area %r is not the card's area %r" % (cid, c.get("area"), area))
        kind, source, ev = c.get("kind"), c.get("source"), c.get("evidence")
        cl = [x for x in (c.get("cites") or []) if isinstance(x, dict)]
        code = [x for x in cl if x.get("path")]
        refs = [x["ref"] for x in cl if x.get("ref")]
        if lens == "how":
            if kind == "why":
                errors.append("%s: a how card makes no why claims; the why lens answers from the record" % cid)
            if not code:
                errors.append("%s: a how claim needs a file-and-line cite" % cid)
            if ev == "said":
                errors.append("%s: evidence 'said' is never enough for a claim about code" % cid)
            if source not in (None, "code"):
                errors.append("%s: a how claim's source is the code, not %r" % (cid, source))
        elif lens == "why":
            if kind != "why":
                errors.append("%s: a why card makes only why claims (got %r)" % (cid, kind))
            if source == "inference":
                if c.get("confidence") == "certain":
                    errors.append("%s: an inference cannot be certain" % cid)
                if not c.get("confidence"):
                    errors.append("%s: an inference must say how sure it is" % cid)
            elif source == "record":
                if not refs:
                    errors.append("%s: a claim from the record cites the record (commit:, pr:, doc:)" % cid)
            else:
                errors.append("%s: a why claim's source is record or inference, not %r" % (cid, source))
        for r in refs:
            m = REF.match(r)
            if not m:
                errors.append("%s: ref %r is not commit:<sha>, pr:<n>, doc:<path> or issue:<id>" % (cid, r))
                continue
            kind_r, val = m.group(1), m.group(2)
            if kind_r == "doc" and C.check_cite(repo, {"ref": r}) != "ok":
                errors.append("%s: doc ref %s is not a file in the repo" % (cid, val))
            elif kind_r == "commit":
                if not re.match(r"^[0-9a-f]{7,40}$", val):
                    errors.append("%s: commit ref %r is not a sha" % (cid, val))
                else:
                    found = commit_exists(repo, val)
                    if found is False:
                        errors.append("%s: commit %s is not in this repo's history" % (cid, val))
                    elif found is None:
                        warnings.append("%s: commit %s not checked (no git history)" % (cid, val))
        for x in code:
            res = C.check_cite(repo, x)
            if res != "ok":
                errors.append("%s: cite %s:%s-%s is %s" % (cid, x.get("path"), x.get("line_start"),
                                                           x.get("line_end"), res))
            elif not x.get("symbol"):
                warnings.append("%s: cite %s has no symbol, so drift will go unnoticed" % (cid, x.get("path")))
        if not cl and not (lens == "why" and source == "inference"):
            errors.append("%s: no cites" % cid)
    if not claims and not card.get("gaps"):
        errors.append("card: no claims and no gaps; a reader that found nothing says what it could not answer")
    return {"ok": not errors, "area": area, "lens": lens, "claims": len(claims),
            "errors": errors, "warnings": warnings}


def main():
    ap = argparse.ArgumentParser(description="check a onboarding-loop card against the repo")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--card", required=True)
    ap.add_argument("--expect-area")
    ap.add_argument("--expect-lens", choices=["how", "why"])
    a = ap.parse_args()
    if not os.path.isdir(a.repo) or not os.path.isfile(a.card):
        print(json.dumps({"ok": False, "errors": ["repo or card not found"]}))
        return 2
    try:
        card = json.load(open(a.card))
    except ValueError as e:
        print(json.dumps({"ok": False, "errors": ["card is not JSON: %s" % e]}))
        return 2
    res = check(os.path.abspath(a.repo), card, a.expect_area, a.expect_lens)
    print(json.dumps(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
