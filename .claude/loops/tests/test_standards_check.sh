#!/usr/bin/env bash
# Regression: dev-pre-pr's deterministic conventions scan (scripts/standards_check.sh). The
# gate's value depends on it being right in BOTH directions — a missed violation lets a bad
# branch through, and a false positive on pre-existing code teaches developers to stop reading
# the report. So every default rule (.claude/loops/conventions.json `rules`) is proven positive
# AND negative here, the pre-existing-code case is a first-class test, and a custom --rules pack
# proves the ENGINE (added lines only, suppress marker, heuristics never block, sibling/absent
# checks, exit 0/2/3, fail closed) independently of whatever rules a target repo ships.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
SCAN="$SK/dev-pre-pr/scripts/standards_check.sh"
ROOT="$(mktemp -d)"
pass=0; fail=0
ok(){ pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }
expect_rc(){ if [ "$3" = "$2" ]; then ok; else bad "$1 (rc=$3, want $2)"; fi; }
has(){ echo "$1" | grep -q "$2" && ok || bad "$3"; }
hasnt(){ echo "$1" | grep -q "$2" && bad "$3" || ok; }
# jq-free JSON probe: jp <json> <python expr over d>
jp(){ python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(eval(sys.argv[1]))" "$2" <<<"$1" 2>/dev/null || echo ERR; }

mkrepo(){ R="$ROOT/$1"; mkdir -p "$R"; cd "$R"; git init -q -b main
          git config user.email t@example.com; git config user.name t; }

# ── 1. every default rule fires on an added line ────────────────────────────
mkrepo violations
mkdir -p src/lib src/__tests__
echo base > README.md; git add -A; git commit -qm base
echo 'export function save(x) { console.log("saving", x); return x }' > src/app.js
printf 'def total(xs):\n    print("debug", xs)\n    return sum(xs)\n' > src/lib/cart.py
echo 'it.only("adds an item", () => {})' > src/__tests__/cart.test.js
echo 'const apiKey = "abcd1234efgh5678ijkl";' > src/config.js
echo '// TODO fix the rounding' > src/rounding.js
O=$(bash "$SCAN" --repo "$R"); expect_rc "violations exit 3 (blocking)" 3 $?
for rid in no-debug-console no-debug-print no-focused-tests no-secret-literals todo-without-ticket; do
  has "$O" "\"$rid\"" "rule $rid did not fire"
done
ck_line(){ [ "$(jp "$O" "[v['line'] for v in d['violations'] if v['rule_id']=='$1']")" = "$2" ] && ok || bad "$1 reported at the wrong line (want $2)"; }
ck_line no-debug-print "[2]"
ck_line no-focused-tests "[1]"
[ "$(jp "$O" 'd["blocking"]')" = "1" ] && ok || bad "only the non-heuristic high (focused test) blocks"

# ── 2. compliant code produces ZERO findings (the false-positive test) ──────
mkrepo clean
mkdir -p src/lib src/__tests__ scripts docs
echo base > README.md; git add -A; git commit -qm base
cat > src/app.js <<'J'
import { logger } from './logger'
// TODO PROJ-123: retry on a 409
export function save(x) { logger.info('saving', x); return x }
const apiKey = process.env.API_KEY
console.log('ready') // standards:allow — CLI banner, stdout is the contract
J
printf 'import logging\nlog = logging.getLogger(__name__)\ndef total(xs):\n    log.debug("total %%s", xs)\n    return sum(xs)\n' > src/lib/cart.py
printf 'import sys\nprint("usage: run.py <file>")\n' > scripts/run.py
cat > src/__tests__/cart.test.js <<'J'
it('adds an item', () => { console.log('fixture dump'); const password = "not-a-real-password-123" })
J
printf 'password = "supersecretvalue123"\n' > docs/setup.md
O=$(bash "$SCAN" --repo "$R"); expect_rc "compliant code exits 0" 0 $?
has "$O" '"violations": \[\]' "compliant code has zero violations"
[ "$(jp "$O" 'd["scanned"]')" = "5" ] && ok || bad "every added file was scanned (scanned=5)"

# ── 3. PRE-EXISTING violations are never reported (added lines only) ────────
mkrepo preexisting
mkdir -p src/__tests__
printf 'it.only("legacy", () => {})\nconsole.log("legacy")\n' > src/__tests__/legacy.test.js
printf 'export const a = 1\nconsole.log("legacy debug")\n' > src/legacy.js
git add -A; git commit -qm "legacy"; git checkout -qb fix/PROJ-1
echo 'export const b = 2' >> src/legacy.js                     # touch the legacy file with a CLEAN line
echo 'export const c = 3' > src/new.js
git add -A; git commit -qm "PROJ-1 add b"
O=$(bash "$SCAN" --repo "$R"); expect_rc "untouched legacy violations do not block" 0 $?
hasnt "$O" "legacy.test.js" "a pre-existing file must not appear in the report"
hasnt "$O" "legacy debug" "a pre-existing line in a TOUCHED file must not be reported"
echo 'console.log("new debug")' >> src/legacy.js               # now ADD a violating line to it
O=$(bash "$SCAN" --repo "$R")
[ "$(jp "$O" "[(v['file'], v['line']) for v in d['violations']]")" = "[('src/legacy.js', 4)]" ] && ok \
  || bad "only the newly added line (src/legacy.js:4) is reported"

# ── 4. heuristics are LEADS: reported, but never blocking on their own ──────
mkrepo heuristics
mkdir -p src
echo base > README.md; git add -A; git commit -qm base
echo 'console.debug("state", s)' > src/state.js
echo 'const authToken = "zyxwvutsrqponmlk9876";' > src/client.js   # critical, but heuristic
O=$(bash "$SCAN" --repo "$R"); expect_rc "heuristic-only findings do NOT block (exit 0) — even a critical one" 0 $?
has "$O" "no-debug-console" "debug-console heuristic still reported"
has "$O" "no-secret-literals" "secret-literal heuristic still reported"
has "$O" '"heuristic": true' "heuristic findings are labeled"
has "$O" '"blocking": 0' "heuristic findings are not counted as blocking"
has "$O" '"confirm_question"' "heuristic findings carry the question the gate must answer"

# ── 5. exclude_path is honored: a CLI entry point may print ─────────────────
mkrepo excluded
mkdir -p tools src/lib
echo base > README.md; git add -A; git commit -qm base
printf 'print("done")\n' > tools/migrate_data.py
printf 'print("leftover")\n' > src/lib/core.py
O=$(bash "$SCAN" --repo "$R"); expect_rc "excluded path + heuristic exit 0" 0 $?
hasnt "$O" "tools/migrate_data.py" "an excluded CLI path must not trip no-debug-print"
has "$O" "src/lib/core.py" "the same print() in library code still fires"

# ── 6. tests are exempt from skip_tests rules; the ticket-key pattern comes from config ─
mkrepo exemptions
mkdir -p src/__tests__ src
echo base > README.md; git add -A; git commit -qm base
echo 'const secret = "abcdefghijklmnop1234"; console.log(secret)' > src/__tests__/thing.test.js
printf '// TODO SHOP-42: drop the shim\n// TODO PROJ-7: not this tracker\n' > src/shim.js
O=$(LOOPS_CFG__PROJECT__TICKET_KEY_PATTERN='^SHOP-[0-9]+$' bash "$SCAN" --repo "$R"); expect_rc "test file + owned TODO exit 0" 0 $?
hasnt "$O" "thing.test.js" "test files must be skipped by skip_tests rules"
[ "$(jp "$O" "[v['line'] for v in d['violations'] if v['rule_id']=='todo-without-ticket']")" = "[2]" ] && ok \
  || bad "todo-without-ticket follows project.ticket_key_pattern (SHOP-42 owned, PROJ-7 not)"

# ── 7. FAIL CLOSED — a broken/missing rule file must never read as clean ────
bash "$SCAN" --repo "$R" --rules /nonexistent.json >/dev/null 2>&1
expect_rc "missing rules file FAILS CLOSED (exit 2)" 2 $?
echo '{ not json' > "$ROOT/bad.json"
bash "$SCAN" --repo "$R" --rules "$ROOT/bad.json" >/dev/null 2>&1
expect_rc "invalid rules json FAILS CLOSED (exit 2)" 2 $?
echo '{"version": "1.0.0", "projects": []}' > "$ROOT/norules.json"
bash "$SCAN" --repo "$R" --rules "$ROOT/norules.json" >/dev/null 2>&1
expect_rc "a conventions file with no rules array FAILS CLOSED (exit 2)" 2 $?
bash "$SCAN" --repo "$ROOT/not-a-repo" >/dev/null 2>&1
expect_rc "non-git dir FAILS CLOSED (exit 2)" 2 $?

# ── 8. the router routes every pre-pr verdict ───────────────────────────────
RT="$SK/dev-loop-defect/scripts/recipe_router.py"
r(){ python3 "$RT" --stage pre-pr --result "$1" --attempt "${2:-1}" --max 3; }
has "$(r ready)"            '"advance"'  "router: ready advances"
has "$(r warnings)"         '"advance"'  "router: warnings advances (a MEDIUM must not spin the loop)"
has "$(r blocked)"          '"re-fix"'   "router: blocked routes back to dev-fix"
has "$(r blocked 3)"        '"escalate"' "router: blocked at the retry cap escalates"
has "$(r scan-unavailable)" '"escalate"' "router: scan-unavailable escalates (a re-fix cannot fix a broken scan)"

# ── 9. the ENGINE with a custom --rules pack (no default rule involved) ─────
cat > "$ROOT/custom.json" <<'JSON'
{
  "suppress_marker": "lint:ok",
  "test_path_regex": "(^|/)test/",
  "rules": [
    {"id": "no-fstring-sql", "severity": "high", "include_path": "\\.py$", "skip_tests": true,
     "pattern": "\\.execute\\(\\s*f[\"']", "rule": "f-string SQL", "fix": "bind parameters"},
    {"id": "prefer-logger", "severity": "high", "include_path": "\\.py$", "heuristic": true,
     "pattern": "^\\s*print\\(", "rule": "print in library code", "fix": "use the logger"},
    {"id": "handler-needs-validator", "type": "sibling_file", "severity": "high",
     "include_path": "(^|/)handlers/[^/]+Handler\\.py$", "strip_suffix": "Handler",
     "sibling_template": "{stem}Validator{ext}", "rule": "a handler ships with its validator", "fix": "add the validator"},
    {"id": "lock-needs-release", "type": "file_absent", "severity": "medium", "include_path": "\\.py$",
     "pattern": "\\.acquire\\(", "absent_pattern": "\\.release\\(", "rule": "a lock is acquired but never released", "fix": "release it in a finally"}
  ]
}
JSON
mkrepo custom
mkdir -p app/handlers test
printf 'def legacy(c, x):\n    c.execute(f"select {x}")\n' > app/old.py
git add -A; git commit -qm base; git checkout -qb feature/PROJ-2
cat > app/new.py <<'P'
def find(c, x):
    c.execute(f"select * from t where id = {x}")
    c.execute(f"select 1 -- {x}")  # lint:ok reviewed: x is an int
    print("found")
P
printf 'def t(c):\n    c.execute(f"select {1}")\n' > test/test_new.py
echo 'class MakeThingHandler: pass' > app/handlers/MakeThingHandler.py
echo 'class AddThingHandler: pass' > app/handlers/AddThingHandler.py
echo 'class AddThingValidator: pass' > app/handlers/AddThingValidator.py
printf 'lock.acquire()\ntry:\n    work()\nfinally:\n    lock.release()\n' > app/guarded.py
printf 'lock.acquire()\nwork()\n' > app/leaky.py
echo 'console.log("would trip the DEFAULT rules")' > app/ui.js
O=$(bash "$SCAN" --repo "$R" --rules "$ROOT/custom.json"); expect_rc "custom pack: a non-heuristic high blocks (exit 3)" 3 $?
ids(){ jp "$O" "sorted((v['rule_id'], v['file'], v['line']) for v in d['violations'])"; }
WANT="[('handler-needs-validator', 'app/handlers/MakeThingHandler.py', 1), ('lock-needs-release', 'app/leaky.py', 1), ('no-fstring-sql', 'app/new.py', 2), ('prefer-logger', 'app/new.py', 4)]"
[ "$(ids)" = "$WANT" ] && ok || bad "custom pack findings — got $(ids)"
hasnt "$O" "app/old.py" "custom pack: pre-existing f-string SQL never reported (added lines only)"
hasnt "$O" '"line": 3' "custom pack: the configured suppress marker (lint:ok) silences its line"
hasnt "$O" "test_new.py" "custom pack: its own test_path_regex exempts test/"
hasnt "$O" "AddThingHandler" "custom pack: a handler WITH its sibling validator is clean"
hasnt "$O" "guarded.py" "custom pack: a file that also releases the lock is clean"
hasnt "$O" "app/ui.js" "custom pack replaces the default rules — console.log is not a rule here"
[ "$(jp "$O" 'd["blocking"]')" = "2" ] && ok || bad "custom pack: 2 blocking — the two highs; the heuristic high and the medium are not counted"
[ "$(jp "$O" 'd["rules_file"]')" = "$ROOT/custom.json" ] && ok || bad "custom pack: the report names the rules file it used"
# Resolve the blockers; only the heuristic and the medium remain → the gate opens.
printf 'def find(c, x):\n    c.execute("select * from t where id = %%s", (x,))\n    print("found")\n' > app/new.py
echo 'class MakeThingValidator: pass' > app/handlers/MakeThingValidator.py
O=$(bash "$SCAN" --repo "$R" --rules "$ROOT/custom.json"); expect_rc "custom pack: heuristic + medium only exit 0" 0 $?
[ "$(jp "$O" 'sorted(v["rule_id"] for v in d["violations"])')" = "['lock-needs-release', 'prefer-logger']" ] && ok \
  || bad "custom pack: the non-blocking findings are still reported"

cd /; echo "  standards_check: $pass passed, $fail failed"
rm -rf "$ROOT"
[ "$fail" = 0 ]
