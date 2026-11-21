#!/usr/bin/env bash
# Regression: dev-onboard-teach's two scripts. lesson_plan.py orders a map for a person from where
# work enters (an area nothing calls, then what it calls, all the way down), lists every area
# once even in a cycle, orders claims plain to deep, carries each claim's standing (verified,
# unverified, inference with its confidence) so a hedge is never lost, and grows the diagram one
# area at a time. record_gap.py keeps the questions the map could not answer, once each, and turns
# the open ones into relearn jobs shaped like the router's. Offline: no brain, no network, no model.
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TMP="$(mktemp -d 2>/dev/null)" || { echo "  onboard_teach: SKIPPED (mktemp failed)"; exit 0; }
trap 'rm -rf "$TMP"' EXIT
ROOT="$ROOT" TMP="$TMP" python3 - <<'PY'
import copy, json, os, subprocess, sys
ROOT, TMP = os.environ["ROOT"], os.environ["TMP"]
SC = os.path.join(ROOT, ".claude/skills/dev-onboard-teach/scripts")
LP, RG = os.path.join(SC, "lesson_plan.py"), os.path.join(SC, "record_gap.py")
MAP = json.load(open(os.path.join(ROOT, "contracts/samples/EXAMPLE-onboard/map.json")))
passed = failed = 0
n = [0]

def ck(desc, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print("  [FAIL] %s: got %r want %r" % (desc, got, want))

def put(obj):
    n[0] += 1
    p = os.path.join(TMP, "map%d.json" % n[0])
    json.dump(obj, open(p, "w"))
    return p

def plan(m, *extra):
    p = subprocess.run([sys.executable, LP, "--map", put(m)] + list(extra), capture_output=True, text=True)
    return (json.loads(p.stdout) if p.returncode == 0 else {"_exit": p.returncode})

# 1. order: from where work enters, depth first
p = plan(MAP)
ck("starts where work enters, follows what it calls", p["order"], ["entry", "invoices", "members"])
inv = next(s for s in p["sections"] if s["area"] == "invoices")
ck("claims go plain to deep, why last", [c["kind"] for c in inv["claims"]], ["how", "how", "owner", "gotcha", "why"])
ck("each claim carries a cite", all(c["cite"] for s in p["sections"] for c in s["claims"]), True)
ck("open questions are passed on", p["open_questions"], MAP["open_questions"])
ck("the summary opens the plan", p["summary"], MAP["summary"])

# 2. standing survives
m = copy.deepcopy(MAP)
for c in m["claims"]:
    if c["id"] == "invoices.c1":
        c["verified_round"] = 2
    if c["id"] == "invoices.c4":
        c.update(source="inference", confidence="likely", cites=[])
p = plan(m)
st = {c["id"]: c["standing"] for s in p["sections"] for c in s["claims"]}
ck("verified", st["invoices.c1"], "verified")
ck("unverified", st["invoices.c2"], "unverified")
ck("an inference keeps its confidence", st["invoices.c4"], "inference-likely")
ck("standing counted", p["standing_counts"], {"inference-likely": 1, "unverified": 6, "verified": 1})

# 3. cycles and islands still appear once
m = copy.deepcopy(MAP)
for a in m["areas"]:
    if a["id"] == "invoices":
        a["depends_on"] = ["entry"]
p = plan(m)
ck("a cycle still lists every area once", sorted(p["order"]), ["entry", "invoices", "members"])
ck("no area twice", len(p["order"]), 3)

# 4. the diagram grows one box at a time
p = plan(MAP)
ds = p["diagram_steps"]
ck("one step per area", [d["adds"] for d in ds], ["entry", "invoices", "members"])
ck("first step has one box and no arrow", ("-->" in ds[0]["mermaid"], ds[0]["mermaid"].count("[")), (False, 1))
ck("the arrow arrives with its target", "entry --> invoices" in ds[1]["mermaid"], True)
ck("last step has every box", ds[-1]["mermaid"].count("["), 3)

# 5. focus
p = plan(MAP, "--focus", "entry")
ck("focus teaches one area", p["order"], ["entry"])
ck("its dependencies are context", p["context_areas"], ["invoices"])
ck("an unknown focus is bad usage", plan(MAP, "--focus", "nope").get("_exit"), 2)

# 6. record_gap
G = os.path.join(TMP, "run", "teach-gaps.json")
def rg(*a):
    p = subprocess.run([sys.executable, RG, "--gaps", G] + list(a), capture_output=True, text=True)
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip().startswith("{") else {})
ck("first gap", rg("--question", "How do members get removed?", "--area", "members", "--at", "T")[1].get("id"), "g1")
ck("same question, other case and spacing, bumps", rg("--question", "how do  members get REMOVED")[1].get("action"), "bump")
rg("--question", "Why is the email lowercased?", "--area", "members", "--lens", "why", "--at", "T")
rg("--question", "Where are refunds handled?", "--at", "T")
rg("--question", "What does billing do?", "--area", "billing", "--at", "T")
d = json.load(open(G))
ck("one entry per distinct question", [g["id"] for g in d["gaps"]], ["g1", "g2", "g3", "g4"])
ck("asked count kept", d["gaps"][0]["asked"], 2)
rc, j = rg("--jobs", "--map", put(MAP))
ck("open gaps become router-shaped jobs", [(x["area"], x["skill"], x["mode"], x["questions"]) for x in j["jobs"]],
   [("members", "dev-onboard-how", "missing", ["g1"]), ("members", "dev-onboard-why", "missing", ["g2"])])
ck("topics carry the person's words", j["jobs"][0]["topics"], ["How do members get removed?"])
ck("no area, or an area the map lacks, is unplaced", [u["id"] for u in j["unplaced"]], ["g3", "g4"])
ck("close a gap", rg("--close", "g1", "--by-round", "2")[1].get("action"), "close")
rc, j = rg("--jobs")
ck("a closed gap makes no job; without --map an area is taken as given", [x["questions"] for x in j["jobs"]], [["g4"], ["g2"]])
ck("an empty question is refused", rg("--question", "  ? ")[0], 2)
ck("nothing to do is bad usage", rg()[0], 2)

print("  onboard_teach: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
PY
