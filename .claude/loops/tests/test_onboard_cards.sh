#!/usr/bin/env bash
# Regression: the how and why lenses stay honest before anything reaches the map. check_card.py
# passes the sample cards and refuses a how claim with no cite, a cite past the end of a file, a
# symbol that drifted out of its lines, a how card making why claims, a why inference marked
# certain, a record claim with no record ref, a doc ref that is not a file, a commit that is not
# in the history, a claim from another area and a duplicate id. record_bundle.py gives the why
# reader the commits that touched an area (with the one that introduced it), the pull request
# numbers they mention and the decision docs that name it, and says so when there is no history.
# Offline: scratch copies only, no brain, no network, no model.
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TMP="$(mktemp -d 2>/dev/null)" || { echo "  onboard_cards: SKIPPED (mktemp failed)"; exit 0; }
trap 'rm -rf "$TMP"' EXIT
command -v git >/dev/null || { echo "  onboard_cards: SKIPPED (needs git)"; exit 0; }
mkdir -p "$TMP/repo" && cp -r "$HERE/fixtures/onboard-repo/." "$TMP/repo/"
ROOT="$ROOT" TMP="$TMP" python3 - <<'PY'
import copy, json, os, subprocess, sys
ROOT, TMP = os.environ["ROOT"], os.environ["TMP"]
CK = os.path.join(ROOT, ".claude/skills/dev-onboard-how/scripts/check_card.py")
RB = os.path.join(ROOT, ".claude/skills/dev-onboard-why/scripts/record_bundle.py")
SAMP = os.path.join(ROOT, "contracts/samples/EXAMPLE-onboard/cards")
repo = os.path.join(TMP, "repo")
HOW = json.load(open(os.path.join(SAMP, "invoices.how.json")))
WHY = json.load(open(os.path.join(SAMP, "invoices.why.json")))
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
           GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
passed = failed = 0
n = [0]

def sh(*cmd):
    return subprocess.run(list(cmd), cwd=repo, capture_output=True, text=True, env=ENV)

def card(base, mut=None, extra=()):
    n[0] += 1
    d = copy.deepcopy(base)
    if mut:
        mut(d)
    p = os.path.join(TMP, "card.%d.json" % n[0])
    json.dump(d, open(p, "w"))
    r = subprocess.run([sys.executable, CK, "--repo", repo, "--card", p] + list(extra),
                       capture_output=True, text=True)
    out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    out["_rc"] = r.returncode
    return out

def ck(desc, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print("  [FAIL] %s: got %r want %r" % (desc, got, want))

def has(res, needle):
    return any(needle in e for e in res.get("errors", []))

def claim(d, cid):
    return next(c for c in d["claims"] if c["id"] == cid)

# 1. the samples pass
ck("sample how card passes", card(HOW, extra=["--expect-area", "invoices", "--expect-lens", "how"])["_rc"], 0)
ck("sample why card passes", card(WHY)["_rc"], 0)

# 2. how lens failures
r = card(HOW, lambda d: claim(d, "invoices.h1").update(cites=[]))
ck("uncited how claim refused", (r["_rc"], has(r, "invoices.h1: a how claim needs a file-and-line cite")), (1, True))
r = card(HOW, lambda d: claim(d, "invoices.h2")["cites"][0].update(line_start=90, line_end=95))
ck("cite past the end of the file", has(r, "invoices.h2: cite app/invoices/service.py:90-95 is bad-line"), True)
r = card(HOW, lambda d: claim(d, "invoices.h2")["cites"][0].update(symbol="def refund"))
ck("symbol drifted out of its lines", has(r, "is symbol-absent"), True)
r = card(HOW, lambda d: claim(d, "invoices.h3").update(kind="why"))
ck("how card cannot make why claims", has(r, "a how card makes no why claims"), True)
r = card(HOW, lambda d: claim(d, "invoices.h3").update(evidence="said"))
ck("said is not enough for code", has(r, "evidence 'said' is never enough"), True)
r = card(HOW, lambda d: claim(d, "invoices.h4").update(area="members"))
ck("claim from another area", has(r, "is not the card's area"), True)
r = card(HOW, lambda d: claim(d, "invoices.h4").update(id="invoices.h1"))
ck("duplicate id", has(r, "invoices.h1: duplicate id"), True)
r = card(HOW, lambda d: claim(d, "invoices.h4").update(id="invoices.c4"))
ck("id carries the lens letter", has(r, "id must look like invoices.h<n>"), True)
r = card(HOW, lambda d: claim(d, "invoices.h1")["cites"][0].update(path="../../etc/passwd"))
ck("cite out of the repo is a bad path", has(r, "is bad-path"), True)
r = card(HOW, lambda d: d.update(area="members"), extra=["--expect-area", "invoices"])
ck("expected area enforced", has(r, "card: area is 'members', expected 'invoices'"), True)

# 3. why lens failures
r = card(WHY, lambda d: claim(d, "invoices.w2").update(confidence="certain"))
ck("inference cannot be certain", has(r, "invoices.w2: an inference cannot be certain"), True)
r = card(WHY, lambda d: claim(d, "invoices.w1").update(cites=[{"path": "app/invoices/store.py", "line_start": 1, "line_end": 1}]))
ck("record claim needs a record ref", has(r, "a claim from the record cites the record"), True)
r = card(WHY, lambda d: claim(d, "invoices.w1").update(cites=[{"ref": "doc:docs/decisions/0009-nope.md"}]))
ck("doc ref must be a file", has(r, "doc ref docs/decisions/0009-nope.md is not a file"), True)
r = card(WHY, lambda d: claim(d, "invoices.w1").update(kind="how"))
ck("why card makes only why claims", has(r, "a why card makes only why claims"), True)
r = card(WHY, lambda d: claim(d, "invoices.w1").update(cites=[{"ref": "blame:me"}]))
ck("unknown ref kind", has(r, "is not commit:<sha>"), True)
r = card(WHY, lambda d: claim(d, "invoices.w1").update(cites=[{"ref": "commit:abc1234"}]))
ck("commit ref without history is a warning", (r["_rc"], any("not checked" in w for w in r.get("warnings", []))), (0, True))
r = card(WHY, lambda d: d.update(claims=[], gaps=[]))
ck("an empty card must say what it could not answer", has(r, "no claims and no gaps"), True)
ck("a card of only gaps is honest", card(WHY, lambda d: d.update(claims=[]))["_rc"], 0)

# 4. record bundle without git: decisions only, and it says so
out = os.path.join(TMP, "bundle.0.json")
subprocess.run([sys.executable, RB, "--repo", repo, "--area", "invoices", "--paths", "app/invoices/",
                "--decisions", "docs/decisions/0001-store-in-memory.md", "--out", out], capture_output=True)
b = json.load(open(out))
ck("no-git bundle has no commits", b["commits"], [])
ck("no-git bundle says so", any("not a git checkout" in x for x in b["notes"]), True)
ck("decision doc that names a path is listed", [d["path"] for d in b["decisions"]], ["docs/decisions/0001-store-in-memory.md"])

# 5. under git: commits for the area only, the introducing commit kept, PR numbers pulled out
sh("git", "init", "-q")
sh("git", "add", "app/invoices/", "docs/")
sh("git", "commit", "-qm", "Add invoices (#12)", "-m", "Rules live in the service so the store can be swapped.")
sh("git", "add", "-A")
sh("git", "commit", "-qm", "Add members and entry point")
for i in range(3):
    with open(os.path.join(repo, "app/invoices/service.py"), "a") as fh:
        fh.write("\n# change %d\n" % i)
    sh("git", "commit", "-qam", "Merge pull request #%d from branch" % (20 + i))
first = sh("git", "rev-list", "--max-parents=0", "HEAD").stdout.strip()
out = os.path.join(TMP, "bundle.1.json")
subprocess.run([sys.executable, RB, "--repo", repo, "--area", "invoices", "--paths", "app/invoices/",
                "--decisions", "docs/decisions/0001-store-in-memory.md", "--out", out, "--limit", "2"],
               capture_output=True)
b = json.load(open(out))
ck("members commit left out", any("members" in c["subject"] for c in b["commits"]), False)
ck("newest two plus the introducing commit", len(b["commits"]), 3)
ck("introducing commit kept past the cap", [c["sha"] for c in b["commits"] if c["introduced"]], [first])
ck("intent in the body survives", "store can be swapped" in next(c for c in b["commits"] if c["introduced"]).get("body", ""), True)
ck("pull request numbers", b["prs"], [12, 21, 22])
ck("cap noted", any("capped" in x for x in b["notes"]), True)
ck("no author in the bundle", any("author" in c for c in b["commits"]), False)

# 6. now the commit ref is checked for real
r = card(WHY, lambda d: claim(d, "invoices.w1").update(cites=[{"ref": "commit:" + first}]))
ck("real commit ref passes", r["_rc"], 0)
r = card(WHY, lambda d: claim(d, "invoices.w1").update(cites=[{"ref": "commit:deadbeef"}]))
ck("unknown commit refused", has(r, "commit deadbeef is not in this repo's history"), True)

print("  onboard_cards: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
PY
