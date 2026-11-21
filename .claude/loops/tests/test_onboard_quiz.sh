#!/usr/bin/env bash
# Regression: qa-onboard-quiz's grader keeps the evaluator honest. A key that cites nothing real
# is discarded and never counts against the map; an examiner that cites code the map does not
# cover blocks the round; a missing judge grade blocks instead of scoring; stale and why-gap
# classes are assigned; one weak area fails the round even when the mean passes. Offline: the
# fixture repo is copied to a scratch dir, no brain, no network, no model.
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TMP="$(mktemp -d 2>/dev/null)" || { echo "  onboard_quiz: SKIPPED (mktemp failed)"; exit 0; }
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo" && cp -r "$HERE/fixtures/onboard-repo/." "$TMP/repo/"
ROOT="$ROOT" TMP="$TMP" FIX="$HERE/fixtures/onboard-quiz" python3 - <<'PY'
import copy, json, os, subprocess, sys
ROOT, TMP, FIX = os.environ["ROOT"], os.environ["TMP"], os.environ["FIX"]
G = os.path.join(ROOT, ".claude/skills/qa-onboard-quiz/scripts/grade_quiz.py")
MAP = os.path.join(ROOT, "contracts/samples/EXAMPLE-onboard/map.json")
V = os.path.join(ROOT, "contracts/validate.py")
C = os.path.join(ROOT, "contracts/qa-onboard-quiz.json")
base = {k: json.load(open(os.path.join(FIX, k + ".json"))) for k in ("questions", "answers", "grades")}
passed = failed = 0
n = [0]

def run(mut=None, extra=()):
    n[0] += 1
    d = copy.deepcopy(base)
    if mut:
        mut(d)
    paths = {}
    for k, v in d.items():
        paths[k] = os.path.join(TMP, "%s.%d.json" % (k, n[0]))
        json.dump(v, open(paths[k], "w"))
    out = os.path.join(TMP, "quiz.%d.json" % n[0])
    p = subprocess.run([sys.executable, G, "--repo", os.path.join(TMP, "repo"), "--map", MAP,
                        "--questions", paths["questions"], "--answers", paths["answers"],
                        "--grades", paths["grades"], "--out", out, "--judged-blind"] + list(extra),
                       capture_output=True, text=True)
    if p.returncode != 0:
        return {"_rc": p.returncode, "_err": p.stderr + p.stdout}
    q = json.load(open(out))
    q["_out"] = out
    return q

def ck(desc, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print("  [FAIL] %s: got %r want %r" % (desc, got, want))

def q(res, qid):
    return next((x for x in res.get("questions", []) if x["id"] == qid), {})

def ans(d, qid):
    return next(x for x in d["answers"]["answers"] if x["id"] == qid)

def key(d, qid):
    return next(x for x in d["questions"]["questions"] if x["id"] == qid)["key"]

# 1. the sample round: the mean is close, but members is blind on one question -> fail
r = run()
ck("sample status", r.get("status"), "fail")
ck("sample score", r.get("score"), 0.7857)
ck("partial is vague", q(r, "q2").get("failure_class"), "vague")
ck("not_in_map is missing", q(r, "q6").get("grade"), "missing")
ck("weak area flagged below floor", r.get("by_area", {}).get("members", {}).get("below_floor"), True)
v = subprocess.run([sys.executable, V, C, r["_out"]], capture_output=True)
ck("output validates against the contract", v.returncode, 0)

# 2. the map learns the lowercase fact -> pass
def learned(d):
    a = ans(d, "q6"); a.update(text="lowercases it", not_in_map=False, from_claims=["members.c1"],
                              cites=[{"path": "app/members/invite.py", "line_start": 10, "line_end": 10}])
    d["grades"]["grades"].append({"id": "q6", "grade": "correct", "reason": "ok"})
r = run(learned)
ck("full marks on members passes", (r.get("status"), r.get("by_area", {}).get("members", {}).get("below_floor")), ("pass", False))

# 3. a key that points past the end of the file is discarded, not scored
def badkey(d):
    key(d, "q7")["cites"] = [{"path": "app/main.py", "line_start": 90, "line_end": 95}]
r = run(badkey)
ck("bad line key discarded", (q(r, "q7").get("grade"), q(r, "q7").get("failure_class"), q(r, "q7").get("key_cite_check")), ("discarded", "bad-key", "bad-line"))
ck("discarded not scored", r.get("counts", {}).get("scored"), 6)

# 4. two bad keys of seven is past the bad-key rate -> blocked
def twobad(d):
    badkey(d)
    key(d, "q1")["cites"] = [{"path": "app/invoices/service.py", "line_start": 8, "line_end": 23, "symbol": "class LedgerService"}]
r = run(twobad)
ck("bad-key rate blocks", (r.get("status"), r.get("blocked_reason")), ("blocked", "bad-key-rate"))
ck("symbol drift detected", q(r, "q1").get("key_cite_check"), "symbol-absent")

# 5. an examiner that cites code the map does not cover has read the code -> blocked
def peek(d):
    ans(d, "q7")["cites"] = [{"path": "app/main.py", "line_start": 12, "line_end": 17}]
r = run(peek)
ck("examiner not blind blocks", (r.get("status"), r.get("blocked_reason")), ("blocked", "examiner-not-blind"))
ck("peeking answer marked", q(r, "q7").get("failure_class"), "outside-map")

# 6. a judge grade that never came back blocks; it is never scored as a pass or a fail
def nograde(d):
    d["grades"]["grades"] = [g for g in d["grades"]["grades"] if g["id"] != "q5"]
r = run(nograde)
ck("missing grade blocks", (r.get("status"), r.get("blocked_reason")), ("blocked", "missing-grade"))

# 7. a lost question on a file changed since the map commit is stale, not wrong
chg = os.path.join(TMP, "changed.txt"); open(chg, "w").write("app/members/invite.py\n")
r = run(extra=["--changed-files", chg])
ck("changed file makes the miss stale", q(r, "q6").get("failure_class"), "stale")
ck("unchanged miss keeps its class", q(r, "q2").get("failure_class"), "vague")

# 8. a lost why question is a why-gap
def whylost(d):
    next(g for g in d["grades"]["grades"] if g["id"] == "q4")["grade"] = "wrong"
r = run(whylost)
ck("lost why is why-gap", q(r, "q4").get("failure_class"), "why-gap")

# 9. an answer naming a claim id the map does not have is wrong
def fake(d):
    ans(d, "q5")["from_claims"] = ["members.c9"]
    ans(d, "q5")["cites"] = []
r = run(fake)
ck("unknown claim id", (q(r, "q5").get("grade"), q(r, "q5").get("answer_cite_check")), ("wrong", "unknown-claim"))

# 10. a key that climbs out of the repo is a bad path, never a read
def escape(d):
    key(d, "q3")["cites"] = [{"path": "../../../../etc/hostname", "line_start": 1, "line_end": 1}]
r = run(escape)
ck("path traversal refused", q(r, "q3").get("key_cite_check"), "bad-path")

# 11. no questions at all is blocked, never a pass
def empty(d):
    d["questions"]["questions"] = []
r = run(empty)
ck("no questions blocks", (r.get("status"), r.get("blocked_reason")), ("blocked", "no-questions"))

# 12. the negative sample keeps failing its contract
v = subprocess.run([sys.executable, V, C, os.path.join(ROOT, "contracts/samples/EXAMPLE-onboard-bad-class/quiz.json")], capture_output=True)
ck("off-enum failure_class fails the contract", v.returncode, 1)

print("  onboard_quiz: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
PY
