#!/usr/bin/env python3
"""onboard_router: the next move after one quiz round of the onboarding loop (deterministic, tested).

It reads the round's quiz.json and the round before it, and returns exactly one action:

  halt-paused    the R9 pause flag is set (a malformed flag fails closed)
  stop-budget    the R5 consumption cap was reached
  write-back     the map passed
  requiz         the round was blocked in a way a fresh role can fix; quiz again, relearn nothing
  relearn        the map lost questions; run the per-area jobs, reassemble, quiz again
  stop-stagnant  a question was lost this round and last round for the same reason
  stop-rounds    out of rounds; hand back what is still unknown
  escalate       blocked in a way another quiz cannot fix, or blocked twice for the same reason

Each lost question's failure class picks its job (docs/onboarding-loop.md, Classify before
relearning). bad-key and outside-map are never the map's fault, so they change a quiz role and
never send a reader back into the code. It reads nothing but its arguments, the quiz files and
the tracked limits file: no brain, no network, no model.

Usage:
  onboard_router.py --quiz <quiz-root>/round-<n>/quiz.json --out <same dir>/route.json
                    [--prior <quiz-root>/round-<n-1>/quiz.json] [--max-rounds N]
                    [--questions <same dir>/questions.json --carryover-out <quiz-root>/round-<n+1>/carryover.json]
                    [--budget-spent X] [--budget-cap Y] [--paused true|false] [--started-at T]
On relearn, --carryover-out gets the lost questions whole (key included) from --questions, which
qa-onboard-quiz re-asks unchanged next round. Prints {"ok": true, "action": ..., "reason": ...}. Exit 0 written, 1 contract
violation (nothing written), 2 bad usage.
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", ".."))
CONTRACT = os.path.join(ROOT, "contracts", "dev-loop-onboard.json")
EMIT = os.path.join(ROOT, "contracts", "emit.py")
LIMITS = os.path.join(ROOT, ".claude", "loops", "loop-limits.json")

_TRUE, _FALSE = {"true", "1", "yes", "on"}, {"false", "0", "no", "off", ""}
NOT_THE_MAP = ("bad-key", "outside-map")
# failure class -> the jobs it starts, in order. stale re-surveys the area before reading it.
JOBS = {
    "missing": [("dev-onboard-how", "missing")],
    "wrong": [("dev-onboard-how", "correct")],
    "vague": [("dev-onboard-how", "deepen")],
    "stale": [("dev-onboard-survey", "resurvey"), ("dev-onboard-how", "fresh")],
    "why-gap": [("dev-onboard-why", "missing")],
}
# blocked reason -> the role a requiz replaces. missing-input is not a role problem.
REQUIZ = {
    "bad-key-rate": "fresh_writer",
    "no-questions": "fresh_writer",
    "examiner-not-blind": "fresh_examiner",
    "missing-grade": "rejudge",
}


def limits():
    try:
        with open(LIMITS) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    rounds = data.get("onboard_loop", {}).get("max_rounds")
    cap = data.get("r5_budget", {}).get("per_loop_usd_cap")
    return (rounds if isinstance(rounds, int) and rounds >= 1 else 3,
            float(cap) if isinstance(cap, (int, float)) and cap > 0 else None)


def load(path):
    if not path:
        return None
    with open(path) as fh:
        return json.load(fh)


def lost(quiz):
    """Questions the map lost: a real failure class on a question that was scored."""
    return [q for q in quiz.get("questions", [])
            if q.get("failure_class") not in (None, "none") + NOT_THE_MAP
            and q.get("grade") != "discarded"]


def verified(quiz):
    """Claims a correct answer leaned on, where the answer's cites checked out."""
    out = []
    for q in quiz.get("questions", []):
        if q.get("grade") == "correct" and q.get("answer_cite_check", "ok") in ("ok", "none"):
            for c in q.get("from_claims", []):
                if c not in out:
                    out.append(c)
    return out


def jobs_for(questions):
    jobs = {}
    for q in questions:
        for skill, mode in JOBS[q["failure_class"]]:
            j = jobs.setdefault((q["area"], skill, mode), {
                "area": q["area"], "skill": skill, "mode": mode,
                "questions": [], "drop_claims": [], "topics": []})
            j["questions"].append(q["id"])
            if q.get("question"):
                j["topics"].append(q["question"])
            if mode == "correct":
                for c in q.get("from_claims", []):
                    if c not in j["drop_claims"]:
                        j["drop_claims"].append(c)
    out = []
    for j in sorted(jobs.values(), key=lambda j: (j["area"], j["skill"], j["mode"])):
        if not j["drop_claims"]:
            del j["drop_claims"]
        if not j["topics"]:
            del j["topics"]
        out.append(j)
    return out


def route(a):
    max_rounds, file_cap = limits()
    if a.max_rounds is not None and a.max_rounds >= 1:
        max_rounds = a.max_rounds
    cap = a.budget_cap if a.budget_cap is not None else file_cap
    quiz = load(a.quiz)
    prior = load(a.prior) if a.prior and os.path.isfile(a.prior) else None
    rnd = int(quiz.get("round", 1))
    status = quiz.get("status")
    r = {"project": quiz.get("project", ""), "round": rnd, "max_rounds": max_rounds,
         "quiz_status": status if status in ("pass", "fail", "blocked") else "blocked",
         "jobs": [], "carryover": [], "verified_claims": verified(quiz), "stagnant": [],
         "below_floor": sorted(k for k, v in (quiz.get("by_area") or {}).items()
                               if isinstance(v, dict) and v.get("below_floor"))}
    if quiz.get("commit"):
        r["commit"] = quiz["commit"]
    if isinstance(quiz.get("score"), (int, float)):
        r["score"] = quiz["score"]
    if cap is not None:
        r["budget"] = {"spent": max(a.budget_spent, 0.0), "cap": cap}

    def done(action, reason):
        r["action"], r["reason"] = action, reason
        return r

    pv = a.paused.strip().lower()
    if pv not in _TRUE and pv not in _FALSE:
        return done("halt-paused", "malformed --paused value %r: cannot prove un-paused, failing closed (R9)" % a.paused)
    if pv in _TRUE:
        return done("halt-paused", "the global pause flag is set (R9)")
    if cap is not None and a.budget_spent >= cap:
        return done("stop-budget", "consumption %.2f reached the R5 cap %.2f" % (a.budget_spent, cap))
    if status not in ("pass", "fail", "blocked"):
        return done("escalate", "quiz status %r is not pass, fail or blocked" % status)
    if status == "pass":
        return done("write-back", "score %s met the threshold with no area under the floor" % quiz.get("score"))

    if status == "blocked":
        why = quiz.get("blocked_reason", "")
        role = REQUIZ.get(why)
        if role is None:
            return done("escalate", "round blocked (%s): another quiz cannot fix missing inputs" % (why or "no reason"))
        if prior and prior.get("status") == "blocked" and prior.get("blocked_reason") == why:
            return done("escalate", "blocked twice in a row for the same reason (%s)" % why)
        if rnd >= max_rounds:
            return done("stop-rounds", "round %d of %d was blocked (%s); no rounds left" % (rnd, max_rounds, why))
        r["requiz"] = {role: True}
        return done("requiz", "round blocked (%s): replace that role and quiz again, the map is unchanged" % why)

    # status == fail
    lost_now = lost(quiz)
    r["carryover"] = [q["id"] for q in lost_now]
    if prior:
        before = {q["id"]: q.get("failure_class") for q in lost(prior)}
        r["stagnant"] = [q["id"] for q in lost_now if before.get(q["id"]) == q["failure_class"]]
    if any(q.get("failure_class") == "outside-map" for q in quiz.get("questions", [])):
        r["requiz"] = {"fresh_examiner": True}
    if r["stagnant"]:
        return done("stop-stagnant", "lost again for the same reason: %s" % ", ".join(r["stagnant"]))
    if rnd >= max_rounds:
        return done("stop-rounds", "round %d of %d failed; handing back what is still unknown" % (rnd, max_rounds))
    if not lost_now:
        r["requiz"] = {"fresh_writer": True, "fresh_examiner": True}
        return done("requiz", "the round failed on questions that were not the map's fault")
    r["jobs"] = jobs_for(lost_now)
    return done("relearn", "%d lost question(s) across %d area(s)" % (
        len(lost_now), len({q["area"] for q in lost_now})))


def write_carryover(a, result):
    """The lost questions, whole, for the next round. A question whose full text is not in
    questions.json is carried as the quiz recorded it and flagged, so the writer re-keys it."""
    full = {}
    if a.questions and os.path.isfile(a.questions):
        full = {q.get("id"): q for q in load(a.questions).get("questions", [])}
    seen = {q.get("id"): q for q in load(a.quiz).get("questions", [])}
    out = []
    for qid in result["carryover"]:
        if qid in full:
            out.append(full[qid])
        else:
            q = seen.get(qid, {"id": qid})
            out.append({k: q[k] for k in ("id", "kind", "area", "question") if k in q}
                       | {"needs_key": True})
    d = os.path.dirname(os.path.abspath(a.carryover_out))
    os.makedirs(d, exist_ok=True)
    with open(a.carryover_out, "w") as fh:
        json.dump({"from_round": result["round"], "questions": out}, fh, indent=2)
        fh.write("\n")
    return len(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--quiz", required=True)
    ap.add_argument("--prior")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-rounds", type=int)
    ap.add_argument("--budget-spent", type=float, default=0.0)
    ap.add_argument("--budget-cap", type=float)
    ap.add_argument("--paused", default="false")
    ap.add_argument("--questions")
    ap.add_argument("--carryover-out")
    ap.add_argument("--started-at")
    a = ap.parse_args()
    if not os.path.isfile(a.quiz):
        print(json.dumps({"ok": False, "error": "quiz not found: %s" % a.quiz}))
        return 2
    try:
        result = route(a)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(json.dumps({"ok": False, "error": "unreadable input: %s" % e}))
        return 2
    cmd = [sys.executable, EMIT, "--contract", CONTRACT, "--out", a.out, "--from-json", "-"]
    if a.started_at:
        cmd += ["--started-at", a.started_at]
    p = subprocess.run(cmd, input=json.dumps(result), text=True, capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr or p.stdout)
        return 1
    carried = None
    if a.carryover_out and result["action"] == "relearn":
        carried = write_carryover(a, result)
    print(json.dumps({"ok": True, "action": result["action"], "reason": result["reason"],
                      "jobs": len(result["jobs"]), "carried": carried, "out": a.out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
