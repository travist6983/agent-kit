#!/usr/bin/env bash
# Regression: in-loop review convergence. review_rounds.py records one fingerprinted round per
# distinct review.json; recipe_router.py --review-rounds escalates with failure_class
# review-stagnation when the same finding (file + bug_class) is open in the last
# review_convergence.stagnation_fix_cycles + 1 rounds. Offline: no brain, no host, no network.
#
# Usage: bash .claude/loops/tests/test_review_rounds.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
REC="$ROOT/.claude/skills/dev-loop-defect/scripts/review_rounds.py"
R="$ROOT/.claude/skills/dev-loop-defect/scripts/recipe_router.py"
V="$ROOT/contracts/validate.py"
C="$ROOT/contracts/dev-loop-defect.review-rounds.json"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0
ok()  { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL  $1"; echo "        $2"; FAIL=$((FAIL+1)); }
field() { python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(eval(sys.argv[2],{'d':d}))" "$1" "$2" 2>/dev/null; }

# review <path> <verdict> <findings-json>
review() { python3 - "$1" "$2" "$3" <<'PY'
import json, sys
json.dump({"verdict": sys.argv[2], "masking": False, "findings": json.loads(sys.argv[3]),
           "summary": "s"}, open(sys.argv[1], "w"))
PY
}
rec() { python3 "$REC" record --review "$1" --out "$2" --key PROJ-123; }
route() { python3 "$R" --stage code-review --result "${3:-request-changes}" --attempt "${2:-2}" --max 5 --review-rounds "$1" "${@:4}"; }
action() { python3 -c "import json,sys;print(json.load(sys.stdin)['action'])"; }

F1='[{"severity":"high","file":"src/a.js","line":10,"bug_class":"null-or-undefined","issue":"x is read without a guard"}]'
F1b='[{"severity":"high","file":"./src/a.js","line":31,"bug_class":"null-or-undefined","issue":"still unguarded on another path"}]'
F1c='[{"severity":"high","file":"src/a.js","line":12,"bug_class":"null-or-undefined","issue":"guard added at the caller only"}]'

# ── recorder ───────────────────────────────────────────────────────────────────────────────
D="$TMP/one"; mkdir -p "$D"
review "$D/review.json" request-changes "$F1"
rec "$D/review.json" "$D/rr.json" >/dev/null
if [ -f "$D/rr.json" ] && python3 "$V" "$C" "$D/rr.json" >/dev/null 2>&1; then ok "recorder writes a contract-valid file"
else bad "recorder writes a contract-valid file" "$(cat "$D/rr.json" 2>/dev/null)"; fi
out=$(rec "$D/review.json" "$D/rr.json")
n=$(field "$D/rr.json" "len(d['rounds'])")
rd=$(echo "$out" | python3 -c "import json,sys;print(json.load(sys.stdin)['recorded'])")
[ "$n" = 1 ] && [ "$rd" = False ] && ok "same review recorded twice adds no round" || bad "idempotent record" "rounds=$n recorded=$rd"

D="$TMP/resolved"; mkdir -p "$D"
review "$D/review.json" request-changes '[{"severity":"high","file":"a.js","bug_class":"wrong-logic","issue":"a","resolved":true},{"severity":"low","file":"b.js","bug_class":"performance","issue":"b","withheld":true},{"severity":"medium","file":"c.js","bug_class":"performance","issue":"c"}]'
rec "$D/review.json" "$D/rr.json" >/dev/null
n=$(field "$D/rr.json" "[o['file'] for o in d['rounds'][0]['open']]")
[ "$n" = "['c.js']" ] && ok "resolved and withheld findings are not open" || bad "resolved/withheld" "$n"

fp() { python3 -c "
import importlib.util,json,sys
s=importlib.util.spec_from_file_location('rr', sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
print('%s %s' % m.fingerprint(json.loads(sys.argv[2])))" "$REC" "$1"; }
a=$(fp '{"file":"src/a.js","line":10,"bug_class":"null-or-undefined","issue":"one wording"}')
b=$(fp '{"file":"./src/a.js","line":99,"bug_class":"null-or-undefined","issue":"a different wording"}')
[ "$a" = "$b" ] && [ "${a#* }" = class ] && ok "class fingerprint ignores line and wording" || bad "class fingerprint" "$a vs $b"
a=$(fp '{"file":"src/a.js","dimension":"backend","issue":"Off by 1 in loop"}')
b=$(fp '{"file":"src/a.js","dimension":"Backend","issue":"off by 2 in   loop!","bug_class":"other"}')
[ "$a" = "$b" ] && [ "${a#* }" = text ] && ok "text basis when bug_class absent or other (digits/punctuation normalised)" || bad "text fingerprint" "$a vs $b"

D="$TMP/pass"; mkdir -p "$D"
review "$D/r1.json" request-changes "$F1"; rec "$D/r1.json" "$D/rr.json" >/dev/null
review "$D/r2.json" request-changes "$F1b"; rec "$D/r2.json" "$D/rr.json" >/dev/null
v=$(field "$D/rr.json" "d['rounds_to_pass']")
review "$D/r3.json" approve '[]'; rec "$D/r3.json" "$D/rr.json" >/dev/null
w=$(field "$D/rr.json" "d['rounds_to_pass']")
[ "$v" = None ] && [ "$w" = 3 ] && ok "rounds_to_pass null until the third round approves, then 3" || bad "rounds_to_pass" "$v / $w"

cp "$D/rr.json" "$TMP/before.json"
echo "{not json" > "$TMP/broken-review.json"
python3 "$REC" record --review "$TMP/broken-review.json" --out "$D/rr.json" >/dev/null; rc=$?
cmp -s "$D/rr.json" "$TMP/before.json" && [ "$rc" = 2 ] && ok "unreadable review exits 2, file unchanged" || bad "unreadable review" "rc=$rc"
echo "{oops" > "$TMP/broken-rr.json"
python3 "$REC" record --review "$D/r1.json" --out "$TMP/broken-rr.json" >/dev/null; rc=$?
[ "$rc" = 2 ] && [ "$(cat "$TMP/broken-rr.json")" = "{oops" ] && ok "unreadable rounds file is never overwritten" || bad "unreadable rounds file" "rc=$rc"

# ── router, driven by files the recorder produced ──────────────────────────────────────────
D="$TMP/stuck"; mkdir -p "$D"
for f in "$F1" "$F1b" "$F1c"; do review "$D/r.json" request-changes "$f"; rec "$D/r.json" "$D/rr.json" >/dev/null; done
o=$(route "$D/rr.json" 2)
fc=$(echo "$o" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['action'], d.get('failure_class'), len(d.get('stagnant') or []))")
[ "$fc" = "escalate review-stagnation 1" ] && ok "3 rounds same file+class -> escalate review-stagnation at attempt 2/5" || bad "stagnation" "$o"

D="$TMP/two"; mkdir -p "$D"
for f in "$F1" "$F1b"; do review "$D/r.json" request-changes "$f"; rec "$D/r.json" "$D/rr.json" >/dev/null; done
[ "$(route "$D/rr.json" | action)" = re-fix ] && ok "2 rounds -> re-fix" || bad "2 rounds" "$(route "$D/rr.json")"

D="$TMP/classchange"; mkdir -p "$D"
for f in "$F1" '[{"severity":"high","file":"src/a.js","bug_class":"wrong-logic","issue":"x"}]' "$F1c"; do
  review "$D/r.json" request-changes "$f"; rec "$D/r.json" "$D/rr.json" >/dev/null; done
[ "$(route "$D/rr.json" | action)" = re-fix ] && ok "class changes in round 2 -> re-fix" || bad "class change" "$(route "$D/rr.json")"

D="$TMP/otherfile"; mkdir -p "$D"
for f in "$F1" '[{"severity":"high","file":"src/b.js","bug_class":"null-or-undefined","issue":"x"}]' "$F1c"; do
  review "$D/r.json" request-changes "$f"; rec "$D/r.json" "$D/rr.json" >/dev/null; done
[ "$(route "$D/rr.json" | action)" = re-fix ] && ok "same class moved to a different file -> re-fix" || bad "other file" "$(route "$D/rr.json")"

D="$TMP/reworded"; mkdir -p "$D"
for t in "the total is wrong when empty" "empty invoices sum incorrectly" "zero-line totals miscomputed"; do
  review "$D/r.json" request-changes "[{\"severity\":\"high\",\"file\":\"src/a.js\",\"issue\":\"$t\"}]"; rec "$D/r.json" "$D/rr.json" >/dev/null; done
[ "$(route "$D/rr.json" | action)" = re-fix ] && ok "reworded text without bug_class -> re-fix (the documented miss; the retry cap bounds it)" || bad "reworded" "$(route "$D/rr.json")"

echo '{"rounds": [{"round": 1}]}' > "$TMP/malformed.json"
o=$(route "$TMP/malformed.json")
echo "$o" | grep -q "review-rounds unreadable" && [ "$(echo "$o" | action)" = escalate ] && ok "malformed rounds file -> escalate" || bad "malformed" "$o"
o=$(route "$TMP/does-not-exist.json")
[ "$(echo "$o" | action)" = escalate ] && ok "missing rounds file -> escalate" || bad "missing file" "$o"

python3 -c "
import json,sys
d=json.load(open(sys.argv[1])); d.pop('review_convergence')
json.dump(d, open(sys.argv[2],'w'))" "$ROOT/.claude/loops/loop-limits.json" "$TMP/limits-no-rc.json"
o=$(LOOPS_CFG__LIMITS_FILE="$TMP/limits-no-rc.json" route "$TMP/two/rr.json")
echo "$o" | grep -q "stagnation_fix_cycles" && [ "$(echo "$o" | action)" = escalate ] && ok "limits without review_convergence -> escalate (fail closed)" || bad "no limits key" "$o"

[ "$(route "$TMP/stuck/rr.json" 2 approve | action)" = advance ] && ok "approve with the flag -> advance" || bad "approve" "$(route "$TMP/stuck/rr.json" 2 approve)"
[ "$(route "$TMP/stuck/rr.json" 2 request-changes --paused true | action)" = halt-paused ] && ok "--paused true beats stagnation" || bad "paused" ""
o=$(python3 "$R" --stage code-review --result request-changes --attempt 2 --max 5)
k=$(echo "$o" | python3 -c "import json,sys;d=json.load(sys.stdin);print(sorted(d), d['action'])")
[ "$k" = "['action', 'failure_class', 'reason'] escalate" ] && ok "no flag -> escalate (fail closed)" || bad "no flag" "$o"

# dev-code-review v1.8.0 / review-rounds v1.1.0: a needs-human review is recorded (contract-valid),
# is never a pass, and the router escalates it to a human even where stagnation would also fire
D="$TMP/nh"; mkdir -p "$D"
review "$D/review.json" needs-human '[{"severity":"critical","file":"src/a.js","line":3,"bug_class":"symptom-masking","issue":"catch returns ok","human_gate":true,"human_gate_reason":"split"}]'
o=$(rec "$D/review.json" "$D/rr.json")
if python3 "$V" "$C" "$D/rr.json" >/dev/null 2>&1 && [ "$(field "$D/rr.json" "d['rounds'][0]['verdict']")" = needs-human ]; then ok "needs-human round recorded, contract-valid"
else bad "needs-human round recorded" "$o"; fi
[ "$(field "$D/rr.json" "d['rounds_to_pass']")" = None ] && ok "needs-human is never a pass" || bad "needs-human pass" "$(cat "$D/rr.json")"
o=$(route "$TMP/stuck/rr.json" 2 needs-human)
fc=$(echo "$o" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['action'], d.get('failure_class'))")
[ "$fc" = "escalate review-needs-human" ] && ok "needs-human -> escalate review-needs-human (outranks stagnation)" || bad "needs-human route" "$o"

# the number lives in loop-limits.json, nowhere else in the router or recorder
if grep -nE "stagnation_fix_cycles *[=:] *[0-9]" "$R" "$REC" >/dev/null; then bad "threshold literal" "found in scripts"; else ok "threshold is not a literal in the router or recorder"; fi

echo "  review_rounds: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
