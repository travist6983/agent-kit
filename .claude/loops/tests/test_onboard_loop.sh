#!/usr/bin/env bash
# Regression: dev-loop-onboard's decisions are scripts, and each one does what the recipe says.
# onboard_router.py turns a quiz round into exactly one action: a failed round relearns only the
# areas and failure classes it lost (wrong drops the claims it leaned on, stale re-surveys first,
# a why-gap goes to the why reader), a question lost twice the same way stops the loop, a blocked
# round swaps the role at fault and never touches the map, and pause and the consumption cap win
# over everything. mark_verified.py stamps the claims a round verified. map_staleness.py maps
# changed files to areas and falls back to a full relearn when the map's commit is unusable.
# onboard_writeback.py is a dry run unless told otherwise and promotes only verified navigational
# claims as active. decide.py keeps the decision log one row per line. Offline: no brain, no
# network, no model; the kb CLI is replaced by a stub that records its arguments.
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TMP="$(mktemp -d 2>/dev/null)" || { echo "  onboard_loop: SKIPPED (mktemp failed)"; exit 0; }
trap 'rm -rf "$TMP"' EXIT
ROOT="$ROOT" TMP="$TMP" HERE="$HERE" python3 - <<'PY'
import copy, json, os, shutil, subprocess, sys
ROOT, TMP, HERE = os.environ["ROOT"], os.environ["TMP"], os.environ["HERE"]
SC = os.path.join(ROOT, ".claude/skills/dev-loop-onboard/scripts")
RT, MV, ST, WB, DC = (os.path.join(SC, n) for n in (
    "onboard_router.py", "mark_verified.py", "map_staleness.py", "onboard_writeback.py", "decide.py"))
V = os.path.join(ROOT, "contracts/validate.py")
S = os.path.join(ROOT, "contracts/samples/EXAMPLE-onboard")
QUIZ = json.load(open(os.path.join(S, "quiz.json")))
MAP = json.load(open(os.path.join(S, "map.json")))
QUESTIONS = os.path.join(HERE, "fixtures/onboard-quiz/questions.json")
passed = failed = 0
n = [0]

def ck(desc, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print("  [FAIL] %s: got %r want %r" % (desc, got, want))

def put(obj, name):
    n[0] += 1
    d = os.path.join(TMP, "c%d" % n[0])
    os.makedirs(d)
    p = os.path.join(d, name)
    json.dump(obj, open(p, "w"))
    return p

def route(quiz, prior=None, *extra):
    q = put(quiz, "quiz.json")
    out = os.path.join(os.path.dirname(q), "route.json")
    cmd = [sys.executable, RT, "--quiz", q, "--out", out, "--started-at", "2026-01-01T00:00:00Z"]
    if prior is not None:
        cmd += ["--prior", put(prior, "quiz.json")]
    p = subprocess.run(cmd + list(extra), capture_output=True, text=True)
    if p.returncode != 0:
        return {"_exit": p.returncode, "_err": p.stderr + p.stdout}
    r = json.load(open(out))
    r["_out"] = out
    return r

def quiz(status="fail", rnd=1, questions=None, **kw):
    q = copy.deepcopy(QUIZ)
    q["status"], q["round"] = status, rnd
    if questions is not None:
        q["questions"] = questions
    q.update(kw)
    if status != "blocked":
        q.pop("blocked_reason", None)
    return q

def qn(qid, area, cls, grade=None, claims=(), kind="trace", cite="ok"):
    g = grade or ("correct" if cls == "none" else "discarded" if cls == "bad-key" else
                  "partial" if cls == "vague" else "wrong" if cls in ("wrong", "outside-map") else "missing")
    return {"id": qid, "kind": kind, "area": area, "question": "about " + qid, "grade": g,
            "failure_class": cls, "score": 1.0 if g == "correct" else 0.5 if g == "partial" else 0.0,
            "from_claims": list(claims), "key_cite_check": "ok", "answer_cite_check": cite}

# 1. the sample round: relearn the two lost areas, nothing else
r = route(QUIZ, None, "--questions", QUESTIONS, "--carryover-out", os.path.join(TMP, "r2", "carryover.json"))
ck("sample fail relearns", r.get("action"), "relearn")
ck("jobs by area and mode", [(j["area"], j["skill"], j["mode"]) for j in r["jobs"]],
   [("invoices", "dev-onboard-how", "deepen"), ("members", "dev-onboard-how", "missing")])
ck("lost questions carry over", r["carryover"], ["q2", "q6"])
ck("below floor reported", r["below_floor"], ["members"])
ck("correct answers verify their claims", "invoices.c1" in r["verified_claims"], True)
ck("relearn reader gets the topic, not the key", "key" in json.dumps(r["jobs"]), False)
co = json.load(open(os.path.join(TMP, "r2", "carryover.json")))
ck("carryover holds whole questions with keys", [(q["id"], "key" in q) for q in co["questions"]],
   [("q2", True), ("q6", True)])
ck("route validates", subprocess.run([sys.executable, V, os.path.join(ROOT, "contracts/dev-loop-onboard.json"),
                                      r["_out"]], capture_output=True).returncode, 0)

# 2. pass, and the rounds cap
ck("pass writes back", route(quiz("pass"))["action"], "write-back")
ck("fail on the last round stops", route(quiz("fail", 3))["action"], "stop-rounds")
ck("--max-rounds lowers the cap", route(quiz("fail", 2), None, "--max-rounds", "2")["action"], "stop-rounds")

# 3. stagnation: same id, same class, twice
lost6 = [qn("q6", "members", "missing")]
r = route(quiz("fail", 2, lost6), quiz("fail", 1, lost6))
ck("lost twice the same way stops", (r["action"], r["stagnant"]), ("stop-stagnant", ["q6"]))
r = route(quiz("fail", 2, lost6), quiz("fail", 1, [qn("q6", "members", "vague")]))
ck("a different class is progress, not stagnation", r["action"], "relearn")

# 4. each failure class picks its job
r = route(quiz("fail", 1, [qn("q1", "invoices", "wrong", claims=["invoices.c1", "invoices.c2"])]))
ck("wrong corrects and drops what it leaned on", [(j["mode"], j.get("drop_claims")) for j in r["jobs"]],
   [("correct", ["invoices.c1", "invoices.c2"])])
r = route(quiz("fail", 1, [qn("q3", "invoices", "stale")]))
ck("stale re-surveys then reads fresh", [(j["skill"], j["mode"]) for j in r["jobs"]],
   [("dev-onboard-how", "fresh"), ("dev-onboard-survey", "resurvey")])
r = route(quiz("fail", 1, [qn("q4", "invoices", "why-gap", kind="why")]))
ck("why-gap goes to the why reader", [(j["skill"], j["mode"]) for j in r["jobs"]], [("dev-onboard-why", "missing")])

# 5. the quiz's own faults never move the map
mixed = [qn("q1", "invoices", "bad-key"), qn("q2", "invoices", "outside-map"), qn("q6", "members", "missing")]
r = route(quiz("fail", 1, mixed))
ck("bad-key and outside-map are not carried", r["carryover"], ["q6"])
ck("outside-map asks for a fresh examiner", r.get("requiz"), {"fresh_examiner": True})
r = route(quiz("fail", 1, [qn("q1", "invoices", "bad-key"), qn("q2", "invoices", "outside-map")]))
ck("a fail with only quiz faults requizzes", (r["action"], r["jobs"]), ("requiz", []))
r = route(quiz("fail", 1, [qn("q1", "invoices", "none", claims=["invoices.c1"], cite="unknown-claim")]))
ck("a correct answer with a bad cite verifies nothing", r["verified_claims"], [])

# 6. blocked rounds
r = route(quiz("blocked", 1, [], blocked_reason="bad-key-rate"))
ck("bad keys: fresh writer, map unchanged", (r["action"], r.get("requiz"), r["jobs"]),
   ("requiz", {"fresh_writer": True}, []))
ck("peeking examiner: fresh examiner", route(quiz("blocked", 1, [], blocked_reason="examiner-not-blind")).get("requiz"),
   {"fresh_examiner": True})
ck("missing grade: rejudge", route(quiz("blocked", 1, [], blocked_reason="missing-grade")).get("requiz"), {"rejudge": True})
ck("missing input escalates", route(quiz("blocked", 1, [], blocked_reason="missing-input"))["action"], "escalate")
ck("blocked twice for one reason escalates",
   route(quiz("blocked", 2, [], blocked_reason="bad-key-rate"), quiz("blocked", 1, [], blocked_reason="bad-key-rate"))["action"],
   "escalate")

# 7. gates win over everything
ck("pause halts a pass", route(quiz("pass"), None, "--paused", "true")["action"], "halt-paused")
ck("a malformed pause fails closed", route(quiz("pass"), None, "--paused", "maybe")["action"], "halt-paused")
ck("the cap stops a relearn", route(QUIZ, None, "--budget-spent", "5", "--budget-cap", "5")["action"], "stop-budget")
ck("under the cap carries on", route(QUIZ, None, "--budget-spent", "4", "--budget-cap", "5")["action"], "relearn")
ck("missing quiz is bad usage", subprocess.run([sys.executable, RT, "--quiz", os.path.join(TMP, "nope.json"),
                                                "--out", os.path.join(TMP, "x.json")], capture_output=True).returncode, 2)

# 8. mark_verified
m = copy.deepcopy(MAP)
for c in m["claims"]:
    if c["id"] == "invoices.c5":
        c["verified_round"] = 1
mp = put(m, "map.json")
rr = copy.deepcopy(json.load(open(os.path.join(S, "route.json"))))
rr["round"], rr["verified_claims"] = 2, ["invoices.c1", "invoices.c5", "ghost.c9"]
p = subprocess.run([sys.executable, MV, "--map", mp, "--route", put(rr, "route.json")], capture_output=True, text=True)
res = json.loads(p.stdout or "{}")
got = {c["id"]: c.get("verified_round") for c in json.load(open(mp))["claims"]}
ck("stamps the round", (p.returncode, got["invoices.c1"]), (0, 2))
ck("keeps the earlier round", got["invoices.c5"], 1)
ck("reports an unknown claim", res.get("unknown"), ["ghost.c9"])
ck("stamped map still validates", subprocess.run([sys.executable, V, os.path.join(ROOT, "contracts/dev-onboard-map.json"), mp],
                                                 capture_output=True).returncode, 0)

# 9. map_staleness
def stale(repo, mpath, *extra):
    p = subprocess.run([sys.executable, ST, "--repo", repo, "--map", mpath] + list(extra), capture_output=True, text=True)
    return json.loads(p.stdout) if p.returncode == 0 else {"_exit": p.returncode}

tree = os.path.join(TMP, "tree")
shutil.copytree(os.path.join(HERE, "fixtures/onboard-repo"), tree)
s = stale(tree, mp)
ck("no git: unknown, relearn everything", (s["status"], s["stale_areas"]), ("unknown", ["entry", "invoices", "members"]))
g = os.path.join(TMP, "git")
shutil.copytree(os.path.join(HERE, "fixtures/onboard-repo"), g)
env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
def git(*a):
    return subprocess.run(["git", "-C", g] + list(a), capture_output=True, text=True, env=env).stdout.strip()
git("init", "-q"); git("add", "-A"); git("commit", "-qm", "base")
head = git("rev-parse", "HEAD")
m2 = copy.deepcopy(MAP); m2["commit"] = head
mp2 = put(m2, "map.json")
ck("same commit, clean tree: fresh", stale(g, mp2)["status"], "fresh")
with open(os.path.join(g, "app/invoices/service.py"), "a") as fh:
    fh.write("\n# changed\n")
git("commit", "-qam", "edit invoices")
with open(os.path.join(g, "app/members/extra.py"), "w") as fh:
    fh.write("X = 1\n")
with open(os.path.join(g, "billing.py"), "w") as fh:
    fh.write("Y = 2\n")
chg = os.path.join(TMP, "changed.txt")
s = stale(g, mp2, "--changed-out", chg)
ck("committed and untracked changes map to areas", (s["status"], s["stale_areas"]), ("stale", ["invoices", "members"]))
ck("a path no area covers is listed", s["unassigned"], ["billing.py"])
ck("changed list for the grader", sorted(open(chg).read().split()),
   ["app/invoices/service.py", "app/members/extra.py", "billing.py"])
m3 = copy.deepcopy(MAP); m3["commit"] = "1" * 40
ck("a commit the checkout lacks: unknown", stale(g, put(m3, "map.json"))["status"], "unknown")

# 10. onboard_writeback
qroot = os.path.join(TMP, "quiz")
def rounds(*qs):
    if os.path.isdir(qroot):
        shutil.rmtree(qroot)
    for q in qs:
        d = os.path.join(qroot, "round-%d" % q["round"])
        os.makedirs(d)
        json.dump(q, open(os.path.join(d, "quiz.json"), "w"))

stub = os.path.join(TMP, "kb_stub.py")
calls_log = os.path.join(TMP, "kb_calls.jsonl")
with open(stub, "w") as fh:
    fh.write("import json,os,sys\nopen(%r,'a').write(json.dumps(sys.argv[1:])+'\\n')\n"
             "sys.exit(3 if os.environ.get('KB_STUB_FAIL') in sys.argv else 0)\n" % calls_log)
mv = copy.deepcopy(MAP)
for c in mv["claims"]:
    if c["id"] in ("invoices.c1", "invoices.c5", "members.c1", "invoices.c2", "invoices.c4"):
        c["verified_round"] = 1
mvp = put(mv, "map.json")

def wb(*extra, env_extra=None):
    e = dict(os.environ, **(env_extra or {}))
    p = subprocess.run([sys.executable, WB, "--map", mvp, "--quiz-root", qroot, "--project", "ledgerly",
                        "--kb", stub] + list(extra), capture_output=True, text=True, env=e)
    return p.returncode, json.loads(p.stdout) if p.stdout.strip().startswith("{") else {}

rounds(quiz("fail", 1))
rc, pl = wb()
lessons = {c["claim_id"]: c["status"] for c in pl["calls"] if c["store"] == "lessons"}
ck("failed map: verified navigational claims only, active", lessons,
   {"invoices.c1": "active", "invoices.c5": "active", "members.c1": "active"})
ck("an unverified one stays in the map", pl["held_in_map"], ["entry.c2"])
ck("how and why claims never reach the brain", any(k in lessons for k in ("invoices.c2", "invoices.c4")), False)
ck("lessons are kind codebase", {c["argv"][c["argv"].index("--kind") + 1] for c in pl["calls"] if c["store"] == "lessons"},
   {"codebase"})
ck("no rounds_to_pass without a pass", any(c.get("name") == "onboard_rounds_to_pass" for c in pl["calls"]), False)
ck("dry run calls nothing", (rc, pl["mode"], os.path.exists(calls_log)), (0, "dry-run", False))
rounds(quiz("fail", 1), quiz("pass", 2))
rc, pl = wb()
lessons = {c["claim_id"]: c["status"] for c in pl["calls"] if c["store"] == "lessons"}
ck("passed map: unverified navigational claims proposed", lessons.get("entry.c2"), "proposed")
ck("metrics per round", sorted((c["name"], c["round"]) for c in pl["calls"] if c["store"] == "metrics"),
   [("onboard_bad_key_rate", 1), ("onboard_bad_key_rate", 2), ("onboard_quiz_score", 1),
    ("onboard_quiz_score", 2), ("onboard_rounds_to_pass", 2)])
rc, pl = wb("--all-proposed")
ck("--all-proposed writes nothing active", pl["summary"]["active"], 0)
rc, pl = wb("--apply")
logged = [json.loads(l) for l in open(calls_log)]
ck("apply runs every planned call", (rc, len(logged), pl["failed"]), (0, len(pl["calls"]), 0))
os.unlink(calls_log)
rc, pl = wb("--apply", env_extra={"KB_STUB_FAIL": "onboard_quiz_score"})
ck("a failed kb call is reported and exits 1", (rc, pl["failed"]), (1, 2))

# 11. decide.py
log = os.path.join(TMP, "run", "decisions.tsv")
for i, why in enumerate(["first\tcut", "second\nline"]):
    p = subprocess.run([sys.executable, DC, "--log", log, "--round", "1", "--stage", "survey",
                        "--decision", "cut %d areas" % (i + 3), "--why", why, "--at", "2026-01-01T00:00:00Z"],
                       capture_output=True, text=True)
lines = open(log).read().splitlines()
ck("header once, one row per decision", (len(lines), lines[0].split("\t")[0], json.loads(p.stdout)["rows"]), (3, "at", 2))
ck("tabs and newlines flattened", [len(l.split("\t")) for l in lines], [7, 7, 7])
ck("a decision with no why is refused", subprocess.run([sys.executable, DC, "--log", log, "--round", "1", "--stage", "x",
                                                        "--decision", "d", "--why", " "], capture_output=True).returncode, 2)

print("  onboard_loop: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
PY
