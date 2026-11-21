#!/usr/bin/env bash
# Regression: dev-onboard-map assembles the map the same way every time and refuses a map the quiz
# could not trust. assemble_map.py renames and merges survey areas from the architect's lens,
# copies commands from the survey, turns card gaps into open questions, and on a relearn keeps a
# verified claim only while it is unchanged: a correct-mode card's drops are removed, a fresh card
# replaces its area's claims for its lens only, and an edited claim starts unverified again.
# check_map.py refuses two owners for one symbol, an uncited claim, a certain inference, a broken
# cite and a dependency on an area that does not exist. Offline: no brain, no network, no model.
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TMP="$(mktemp -d 2>/dev/null)" || { echo "  onboard_map: SKIPPED (mktemp failed)"; exit 0; }
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo" && cp -r "$HERE/fixtures/onboard-repo/." "$TMP/repo/"
ROOT="$ROOT" TMP="$TMP" python3 - <<'PY'
import copy, json, os, shutil, subprocess, sys
ROOT, TMP = os.environ["ROOT"], os.environ["TMP"]
AM = os.path.join(ROOT, ".claude/skills/dev-onboard-map/scripts/assemble_map.py")
CM = os.path.join(ROOT, ".claude/skills/dev-onboard-map/scripts/check_map.py")
V = os.path.join(ROOT, "contracts/validate.py")
S = os.path.join(ROOT, "contracts/samples/EXAMPLE-onboard")
repo = os.path.join(TMP, "repo")
SURVEY = os.path.join(S, "survey.json")
ARCH = json.load(open(os.path.join(S, "architect.json")))
HOW = json.load(open(os.path.join(S, "cards/invoices.how.json")))
WHY = json.load(open(os.path.join(S, "cards/invoices.why.json")))
passed = failed = 0
n = [0]

def ck(desc, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print("  [FAIL] %s: got %r want %r" % (desc, got, want))

def assemble(cards, arch=None, prior=None):
    n[0] += 1
    d = os.path.join(TMP, "run%d" % n[0])
    os.makedirs(os.path.join(d, "cards"))
    for c in cards:
        json.dump(c, open(os.path.join(d, "cards", "%s.%s.json" % (c["area"], c["lens"])), "w"))
    cmd = [sys.executable, AM, "--survey", SURVEY, "--cards", os.path.join(d, "cards"),
           "--out", os.path.join(d, "map.json"), "--md", os.path.join(d, "map.md")]
    if arch is not None:
        json.dump(arch, open(os.path.join(d, "architect.json"), "w"))
        cmd += ["--architect", os.path.join(d, "architect.json")]
    if prior is not None:
        json.dump(prior, open(os.path.join(d, "prior.json"), "w"))
        cmd += ["--prior", os.path.join(d, "prior.json")]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return {"_rc": p.returncode, "_err": p.stdout + p.stderr}
    m = json.load(open(os.path.join(d, "map.json")))
    m["_out"] = os.path.join(d, "map.json")
    m["_md"] = open(os.path.join(d, "map.md")).read()
    m["_stats"] = json.loads(p.stdout.strip().splitlines()[-1])
    return m

def check(m):
    n[0] += 1
    p = os.path.join(TMP, "lint%d.json" % n[0])
    json.dump({k: v for k, v in m.items() if not k.startswith("_")}, open(p, "w"))
    r = subprocess.run([sys.executable, CM, "--repo", repo, "--map", p, "--survey", SURVEY],
                       capture_output=True, text=True)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    out["_rc"] = r.returncode
    return out

def ids(m):
    return [c["id"] for c in m.get("claims", [])]

def claim(m, cid):
    return next(c for c in m["claims"] if c["id"] == cid)

def has(res, needle):
    return any(needle in e for e in res.get("errors", []))

# 1. first round: cards plus the architect's lens
m = assemble([HOW, WHY], ARCH)
ck("areas renamed by the lens", [a["id"] for a in m["areas"]], ["entry", "invoices", "members"])
ck("entry's edge follows the rename", next(a for a in m["areas"] if a["id"] == "entry")["depends_on"], ["invoices"])
ck("all claims in, architect last", ids(m), ["invoices.h1", "invoices.h2", "invoices.h3", "invoices.h4",
                                              "invoices.w1", "invoices.w2", "entry.a1"])
ck("nothing verified yet", {c["verified_round"] for c in m["claims"]}, {None})
ck("commands copied from the survey", m["commands"], {"run": "make run", "test": "make test", "lint": "make lint"})
ck("card gap is an open question", m["open_questions"], ["[invoices] Why paid invoices can never be reopened: no commit or doc says."])
ck("no git: the all-zero commit", m["commit"], "0" * 40)
ck("summary from the lens", m["summary"].startswith("Ledgerly is"), True)
ck("map.md names the areas", "**Entry point** (`app/__init__.py, app/main.py`): Wire" in m["_md"], True)
ck("map.md marks the inference", "(inference, likely)" in m["_md"], True)
v = subprocess.run([sys.executable, V, os.path.join(ROOT, "contracts/dev-onboard-map.json"), m["_out"]], capture_output=True)
ck("output validates against the contract", v.returncode, 0)
r = check(m)
ck("assembled map lints clean", (r["_rc"], r["errors"]), (0, []))
ck("an area with no claims is a warning", "area members has no claims" in r["warnings"], True)

# 2. a merge rewrites ids and unions paths
arch = copy.deepcopy(ARCH); arch["merge"] = {"core": ["invoices", "members"]}
m2 = assemble([HOW, WHY], arch)
ck("merged area", [a["id"] for a in m2["areas"]], ["entry", "core"])
ck("merged paths", next(a for a in m2["areas"] if a["id"] == "core")["paths"], ["app/invoices/", "app/members/"])
ck("claims rehomed", ids(m2)[:2], ["core.h1", "core.h2"])
ck("edge follows the merge", next(a for a in m2["areas"] if a["id"] == "entry")["depends_on"], ["core"])

# 3. relearn: verified survives only when unchanged; drops are removed
prior = copy.deepcopy({k: v for k, v in m.items() if not k.startswith("_")})
for c in prior["claims"]:
    if c["id"] in ("invoices.h1", "invoices.h2", "invoices.w1"):
        c["verified_round"] = 1
fix = copy.deepcopy(HOW)
fix.update(mode="correct", focus=["invoices.h3"], drops=[{"id": "invoices.h3", "reason": "get() copies the row", "replaced_by": "invoices.h5"}],
           claims=[dict(HOW["claims"][2], id="invoices.h5", text="get() hands back a copy; only update() changes the stored row.")])
m3 = assemble([fix], ARCH, prior)
ck("dropped claim gone", "invoices.h3" in ids(m3), False)
ck("replacement added", "invoices.h5" in ids(m3), True)
ck("unchanged claim keeps its round", claim(m3, "invoices.h1")["verified_round"], 1)
ck("new claim starts unverified", claim(m3, "invoices.h5")["verified_round"], None)
ck("why claims untouched by a how relearn", claim(m3, "invoices.w1")["verified_round"], 1)
ck("stats count it", (m3["_stats"]["dropped"], m3["_stats"]["kept_verified"]), (1, 3))

# 4. an edited claim starts over; a fresh card replaces only its own lens
edit = copy.deepcopy(HOW); edit["mode"] = "deepen"
edit["claims"] = [dict(HOW["claims"][1], text="pay() raises InvoiceError unless the invoice is open.")]
m4 = assemble([edit], ARCH, prior)
ck("edited claim unverified again", claim(m4, "invoices.h2")["verified_round"], None)
fresh = copy.deepcopy(HOW); fresh["claims"] = [HOW["claims"][0]]
m5 = assemble([fresh], ARCH, prior)
ck("fresh how card replaces the area's how claims", [i for i in ids(m5) if i.startswith("invoices.h")], ["invoices.h1"])
ck("and leaves its why claims", [i for i in ids(m5) if i.startswith("invoices.w")], ["invoices.w1", "invoices.w2"])

# 5. a claim for an area the survey does not have is bad input
stray = copy.deepcopy(HOW); stray["area"] = "billing"
for c in stray["claims"]:
    c["area"] = "billing"
ck("unknown area refused", assemble([stray], ARCH).get("_rc"), 2)

# 6. the lint refuses what would make the quiz meaningless
def lint(mut):
    mm = copy.deepcopy({k: v for k, v in m.items() if not k.startswith("_")})
    mut(mm)
    return check(mm)
r = lint(lambda mm: mm["claims"].append({"id": "entry.a2", "area": "entry", "kind": "owner", "text": "main owns invoices",
        "cites": [{"path": "app/invoices/service.py", "line_start": 8, "line_end": 8, "symbol": "class InvoiceService"}],
        "evidence": "pointed", "source": "code"}))
ck("two owners for one symbol", has(r, "two owners: class InvoiceService"), True)
r = lint(lambda mm: claim(mm, "invoices.h4").update(cites=[]))
ck("uncited claim", has(r, "invoices.h4: no cites"), True)
r = lint(lambda mm: claim(mm, "invoices.w2").update(confidence="certain"))
ck("certain inference", has(r, "invoices.w2: an inference cannot be certain"), True)
r = lint(lambda mm: claim(mm, "invoices.h2")["cites"][0].update(line_start=200, line_end=201))
ck("broken cite", has(r, "invoices.h2: cite app/invoices/service.py is bad-line"), True)
r = lint(lambda mm: mm["areas"][0].update(depends_on=["billing"]))
ck("dependency on a missing area", has(r, "depends on 'billing'"), True)
r = lint(lambda mm: claim(mm, "invoices.h1").update(evidence="said"))
ck("said only for inference", has(r, "evidence 'said' is only allowed on an inference"), True)
r = lint(lambda mm: mm["claims"].append(dict(claim(mm, "invoices.h1"))))
ck("duplicate id", has(r, "invoices.h1: duplicate id"), True)
r = check(json.load(open(os.path.join(S, "map.json"))))
ck("the sample map lints clean", r["_rc"], 0)

print("  onboard_map: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
PY
