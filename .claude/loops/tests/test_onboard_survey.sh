#!/usr/bin/env bash
# Regression: dev-onboard-survey's survey.py counts and reads, it never guesses. On the fixture repo
# it finds the declared commands with their lines, the manifest entry point, the decision record,
# and three areas with the import edge between them. Under git it records the commit and the
# hotspots; a shallow clone says so. A JavaScript monorepo splits into its packages; an oversized
# directory splits into its children; generated and vendored files stay out; and two runs on the
# same commit write the same survey. Offline: scratch copies only, no brain, no network, no model.
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TMP="$(mktemp -d 2>/dev/null)" || { echo "  onboard_survey: SKIPPED (mktemp failed)"; exit 0; }
trap 'rm -rf "$TMP"' EXIT
command -v git >/dev/null || { echo "  onboard_survey: SKIPPED (needs git)"; exit 0; }
mkdir -p "$TMP/repo" && cp -r "$HERE/fixtures/onboard-repo/." "$TMP/repo/"
ROOT="$ROOT" TMP="$TMP" python3 - <<'PY'
import json, os, subprocess, sys
ROOT, TMP = os.environ["ROOT"], os.environ["TMP"]
S = os.path.join(ROOT, ".claude/skills/dev-onboard-survey/scripts/survey.py")
V = os.path.join(ROOT, "contracts/validate.py")
C = os.path.join(ROOT, "contracts/dev-onboard-survey.json")
passed = failed = 0
n = [0]
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
           GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")

def sh(cwd, *cmd):
    return subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, env=ENV)

def run(repo, *extra):
    n[0] += 1
    out = os.path.join(TMP, "survey.%d.json" % n[0])
    p = subprocess.run([sys.executable, S, "--repo", repo, "--out", out, "--project", "demo"]
                       + list(extra), capture_output=True, text=True)
    if p.returncode != 0:
        return {"_rc": p.returncode, "_err": p.stderr + p.stdout}
    d = json.load(open(out))
    d["_out"] = out
    return d

def ck(desc, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print("  [FAIL] %s: got %r want %r" % (desc, got, want))

def area(d, aid):
    return next((a for a in d.get("areas", []) if a["id"] == aid), {})

def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(text)

repo = os.path.join(TMP, "repo")

# 1. the fixture as a plain tree: commands, entry point, record, areas, edges
r = run(repo)
ck("tree source", r.get("source"), "tree")
ck("no commit without git", r.get("commit"), None)
ck("declared commands with their lines",
   [(c["run"], c["line"]) for c in r.get("commands", []) if c["source"] == "declared"],
   [("make run", 1), ("make test", 4), ("make lint", 7)])
ck("pyproject script is the entry point", [(e["path"], e["reason"]) for e in r.get("entry_points", [])],
   [("app/main.py", "manifest")])
ck("main guard line kept on the manifest entry", r.get("entry_points", [{}])[0].get("line"), 20)
ck("decision record found", r.get("decisions"), ["docs/decisions/0001-store-in-memory.md"])
ck("three areas", [a["id"] for a in r.get("areas", [])], ["app", "invoices", "members"])
ck("loose files listed by path, not swallowing subdirs", area(r, "app").get("paths"),
   ["app/__init__.py", "app/main.py"])
ck("import edge app -> invoices", area(r, "app").get("depends_on"), ["invoices"])
ck("test named for its area", area(r, "invoices").get("tests"), ["tests/test_invoices.py"])
ck("tests are not code", r.get("totals", {}).get("code_files"), 7)
v = subprocess.run([sys.executable, V, C, r["_out"]], capture_output=True)
ck("output validates against the contract", v.returncode, 0)

# 2. under git: the commit is HEAD, hotspots count commits, history is not shallow
sh(repo, "git", "init", "-q")
sh(repo, "git", "add", "-A")
sh(repo, "git", "commit", "-qm", "init")
with open(os.path.join(repo, "app/invoices/service.py"), "a") as fh:
    fh.write("\n# touched\n")
sh(repo, "git", "commit", "-qam", "touch service")
head = sh(repo, "git", "rev-parse", "HEAD").stdout.strip()
r = run(repo)
ck("git source", r.get("source"), "git")
ck("commit is HEAD", r.get("commit"), head)
ck("hottest file first", (r.get("hotspots") or [{}])[0], {"path": "app/invoices/service.py", "commits": 2})
ck("area carries its churn", area(r, "invoices").get("hotspot_commits"), 4)
ck("not shallow", r.get("history", {}).get("shallow"), False)

# 3. same commit, same survey (timing aside)
r2 = run(repo)
strip = lambda d: {k: v for k, v in d.items() if k not in ("timing", "_out", "attempt")}
ck("deterministic", strip(r2), strip(r))

# 4. a shallow clone says so
shallow = os.path.join(TMP, "shallow")
sh(TMP, "git", "clone", "-q", "--depth", "1", "file://" + repo, shallow)
r = run(shallow)
ck("shallow flagged", r.get("history", {}).get("shallow"), True)
ck("shallow noted", any("shallow" in x for x in r.get("notes", [])), True)

# 5. a JavaScript monorepo splits into its packages; generated and vendored code stays out
mono = os.path.join(TMP, "mono")
write(mono, "package.json", json.dumps({"name": "m", "scripts": {"build": "turbo build", "test": "turbo test"}}, indent=2))
write(mono, "pnpm-lock.yaml", "lockfileVersion: 9\n")
write(mono, "packages/api/src/server.ts", "import { price } from '../../core/src/price'\nexport const x = price(1)\n")
write(mono, "packages/core/src/price.ts", "export function price(n: number) {\n  return n * 2\n}\n")
write(mono, "packages/core/src/price.test.ts", "import { price } from './price'\n")
write(mono, "packages/web/src/index.js", "console.log(1)\n")
write(mono, "packages/web/dist/bundle.min.js", "x\n" * 50)
write(mono, "node_modules/left-pad/index.js", "module.exports = 1\n")
r = run(mono)
ck("one area per package", [a["id"] for a in r.get("areas", [])], ["api", "core", "web"])
ck("cross-package import edge", area(r, "api").get("depends_on"), ["core"])
ck("pnpm repo uses pnpm", [c["run"] for c in r.get("commands", [])], ["pnpm build", "pnpm test"])
ck("server.ts is a conventional entry", [e["path"] for e in r.get("entry_points", [])],
   ["packages/api/src/server.ts", "packages/web/src/index.js"])
ck("vendored and built code is not counted", r.get("totals", {}).get("code_files"), 3)

# 6. an oversized directory splits into its children; small ones stay whole
big = os.path.join(TMP, "big")
for sub in ("alpha", "beta"):
    for i in range(3):
        write(big, "svc/%s/m%d.py" % (sub, i), "x = 1\n" * 40)
write(big, "tools/gen.py", "print(1)\n")
r = run(big, "--max-area-lines", "100")
ck("oversized svc split, tools whole", [a["id"] for a in r.get("areas", [])], ["alpha", "beta", "tools"])
r = run(big, "--max-area-lines", "1000")
ck("under the cap it stays one area", [a["id"] for a in r.get("areas", [])], ["svc", "tools"])

# 7. a missing repo is bad usage, not an empty survey
p = subprocess.run([sys.executable, S, "--repo", os.path.join(TMP, "nope"), "--out", os.path.join(TMP, "x.json")],
                   capture_output=True, text=True)
ck("missing repo exits 2", p.returncode, 2)

print("  onboard_survey: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
PY
