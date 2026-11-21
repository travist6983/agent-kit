#!/usr/bin/env python3
"""Grade one round of the codebase quiz. Deterministic: the judgment is the judge agent's,
everything checkable is checked here.

Inputs (all JSON files written by blind agents, see SKILL.md):
  --questions  {"questions": [{id, kind, area, question, key: {text, cites[]}}]}   (writer: read the code)
  --answers    {"answers":   [{id, text, cites[], from_claims[], not_in_map}]}     (examiner: read the map)
  --grades     {"grades":    [{id, grade: correct|partial|wrong, reason}]}         (judge: read key + answer)
  --map        map.json (contract dev-onboard-map.json)
  --repo       the codebase root the cites resolve against

What the script decides, in order, per question:
  1. The KEY's cites must resolve (file exists, lines in range, symbol present). A bad key is
     discarded and never counts against the map. Too many bad keys block the round.
  2. An examiner that said not_in_map scores missing; no judge grade is needed.
  3. Every claim id the answer names must exist in the map, and every file-and-line the answer
     cites must be covered by those claims. A cite nothing in the map covers means the examiner
     read the code: the whole round is blocked (examiner-not-blind), because its score would lie.
  4. Otherwise the judge's grade stands: correct 1, partial 0.5 (vague), wrong 0.
  5. A lost question whose key or claims touch a file changed since the map commit is stale.
  6. A lost why question is a why-gap.

Exit 0 written (any status) · 1 contract violation, nothing written · 2 bad usage.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cites as C  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
CONTRACT = os.path.join(ROOT, "contracts", "qa-onboard-quiz.json")
EMIT = os.path.join(ROOT, "contracts", "emit.py")
LIMITS = os.path.join(ROOT, ".claude", "loops", "loop-limits.json")

KINDS = ("locate", "trace", "predict", "why", "owner")
DEFAULTS = {"pass_threshold": 0.8, "area_floor": 0.6, "max_bad_key_rate": 0.25}


def limits():
    """onboard_loop block of loop-limits.json, falling back to the defaults above."""
    try:
        with open(LIMITS) as fh:
            block = json.load(fh).get("onboard_loop", {})
    except (OSError, ValueError):
        block = {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = block.get(k)
        if isinstance(v, (int, float)) and 0 <= v <= 1:
            out[k] = float(v)
    return out


def load(path, key):
    if not path or not os.path.isfile(path):
        return None
    with open(path) as fh:
        data = json.load(fh)
    return data.get(key) if isinstance(data, dict) else None


def changed_files(path):
    if not path:
        return set()
    with open(path) as fh:
        return set(l.strip() for l in fh if l.strip())


def touches(cite_list, changed):
    return any(c.get("path") in changed for c in cite_list if isinstance(c, dict))


def grade(a):
    lim = limits()
    threshold = a.threshold if a.threshold is not None else lim["pass_threshold"]
    floor = a.area_floor if a.area_floor is not None else lim["area_floor"]
    with open(a.map) as fh:
        amap = json.load(fh)
    claims = {c["id"]: c for c in amap.get("claims", [])}
    questions = load(a.questions, "questions")
    answers = load(a.answers, "answers")
    grades = load(a.grades, "grades")
    changed = changed_files(a.changed_files)

    base = {"project": amap.get("project", ""), "commit": amap.get("commit", ""), "round": a.round,
            "threshold": threshold, "area_floor": floor, "judged_blind": bool(a.judged_blind)}
    models = {k: v for k, v in (("writer", a.writer_model), ("examiner", a.examiner_model),
                                ("judge", a.judge_model)) if v}
    if models:
        base["models"] = models

    def blocked(reason, qs=None, counts=None):
        out = dict(base, status="blocked", blocked_reason=reason, score=0.0,
                   counts=counts or {k: 0 for k in ("total", "scored", "correct", "partial",
                                                    "wrong", "missing", "discarded")},
                   questions=qs or [])
        return out

    if questions is None or answers is None:
        return blocked("missing-input")
    if not questions:
        return blocked("no-questions")
    ans = {x.get("id"): x for x in answers}
    grd = {x.get("id"): x for x in (grades or [])}

    rows, not_blind, missing_grade = [], [], []
    for q in questions:
        kind = q.get("kind") if q.get("kind") in KINDS else "locate"
        row = {"id": str(q.get("id")), "kind": kind, "area": q.get("area", "unknown"),
               "question": q.get("question", ""), "grade": "missing", "failure_class": "missing",
               "score": 0.0}
        key_cites = (q.get("key") or {}).get("cites") or []
        row["key_cite_check"] = C.check_all(a.repo, key_cites)
        if row["key_cite_check"] != "ok":
            row.update(grade="discarded", failure_class="bad-key",
                       note="answer key does not resolve against the code; question not scored")
            rows.append(row)
            continue
        an = ans.get(q.get("id"))
        if an is None or an.get("not_in_map"):
            row["answer_cite_check"] = "none"
            row["from_claims"] = []
        else:
            named = [cid for cid in (an.get("from_claims") or [])]
            row["from_claims"] = named
            unknown = [cid for cid in named if cid not in claims]
            a_cites = an.get("cites") or []
            if unknown:
                row["answer_cite_check"] = "unknown-claim"
                row["note"] = "answer names claim ids the map does not have: " + ", ".join(unknown)
            else:
                row["answer_cite_check"] = C.check_all(a.repo, a_cites)
            claim_cites = [c for cid in named if cid in claims for c in claims[cid].get("cites", [])]
            uncovered = [c for c in a_cites if isinstance(c, dict) and not C.covered_by(c, claim_cites)]
            if uncovered:
                not_blind.append(row["id"])
                row["note"] = "answer cites code no named claim covers"
                row["failure_class"] = "outside-map"
                row["grade"] = "wrong"
                rows.append(row)
                continue
            g = (grd.get(q.get("id")) or {}).get("grade")
            if g not in ("correct", "partial", "wrong"):
                missing_grade.append(row["id"])
                rows.append(row)
                continue
            if unknown:
                g = "wrong"
            row["grade"] = g
            row["score"] = {"correct": 1.0, "partial": 0.5, "wrong": 0.0}[g]
            row["failure_class"] = {"correct": "none", "partial": "vague", "wrong": "wrong"}[g]
            if (grd.get(q.get("id")) or {}).get("reason") and "note" not in row:
                row["note"] = str(grd[q["id"]]["reason"])[:300]
        if row["failure_class"] != "none":
            leaned = [c for cid in row.get("from_claims", []) if cid in claims
                      for c in claims[cid].get("cites", [])]
            if changed and (touches(key_cites, changed) or touches(leaned, changed)):
                row["failure_class"] = "stale"
            elif kind == "why":
                row["failure_class"] = "why-gap"
        rows.append(row)

    counts = {"total": len(rows), "discarded": sum(r["grade"] == "discarded" for r in rows)}
    scored = [r for r in rows if r["grade"] != "discarded"]
    counts["scored"] = len(scored)
    for g in ("correct", "partial", "wrong", "missing"):
        counts[g] = sum(r["grade"] == g for r in scored)

    if not_blind:
        return blocked("examiner-not-blind", rows, counts)
    if missing_grade:
        return blocked("missing-grade", rows, counts)
    if counts["total"] and counts["discarded"] / counts["total"] > lim["max_bad_key_rate"]:
        return blocked("bad-key-rate", rows, counts)
    if not scored:
        return blocked("no-questions", rows, counts)

    score = round(sum(r["score"] for r in scored) / len(scored), 4)
    by_area = {}
    for r in scored:
        b = by_area.setdefault(r["area"], {"scored": 0, "points": 0.0})
        b["scored"] += 1
        b["points"] += r["score"]
    below = False
    for area, b in by_area.items():
        b["score"] = round(b["points"] / b["scored"], 4)
        del b["points"]
        b["below_floor"] = b["scored"] >= 2 and b["score"] < floor
        below = below or b["below_floor"]
    status = "pass" if score >= threshold and not below else "fail"
    return dict(base, status=status, score=score, counts=counts, by_area=by_area, questions=rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--answers", required=True)
    ap.add_argument("--grades", required=True)
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument("--threshold", type=float)
    ap.add_argument("--area-floor", type=float)
    ap.add_argument("--changed-files", help="file listing paths changed since the map commit, one per line")
    ap.add_argument("--judged-blind", action="store_true")
    ap.add_argument("--writer-model")
    ap.add_argument("--examiner-model")
    ap.add_argument("--judge-model")
    ap.add_argument("--started-at")
    a = ap.parse_args()
    if not os.path.isdir(a.repo) or not os.path.isfile(a.map):
        print(json.dumps({"ok": False, "error": "repo or map not found"}))
        return 2
    try:
        result = grade(a)
    except (ValueError, KeyError, TypeError) as e:
        print(json.dumps({"ok": False, "error": "unreadable input: %s" % e}))
        return 2
    cmd = [sys.executable, EMIT, "--contract", CONTRACT, "--out", a.out, "--from-json", "-"]
    if a.started_at:
        cmd += ["--started-at", a.started_at]
    p = subprocess.run(cmd, input=json.dumps(result), text=True, capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr or p.stdout)
        return 1
    print(json.dumps({"ok": True, "status": result["status"], "score": result["score"],
                      "blocked_reason": result.get("blocked_reason"), "out": a.out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
