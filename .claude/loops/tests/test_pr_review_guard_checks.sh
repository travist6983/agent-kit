#!/usr/bin/env bash
# Regression: the PR-review runner's posting guard refuses to post (or preview) a review.json
# that lacks the validation / depth blocks or fails finding_votes.py check. The guard is the
# python file pr_review.sh generates per tick; this test extracts it verbatim, gives it a
# guard.json and a FAKE real pr_feed that records calls, and asserts what reaches it.
# Offline: no brain, no VCS host, no model calls; scratch dir, self-cleaning.
set -u
FW="$(cd "$(dirname "$0")/../../.." && pwd)"; SK="$FW/.claude/skills"
PRR="$SK/dev-loop-pr-review/scripts/pr_review.sh"; D="$SK/dev-code-review/scripts"
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pr-guard.XXXXXX")" || { echo "  pr_review_guard_checks: SKIPPED (mktemp failed)"; exit 0; }
trap 'rm -rf "$ROOT"' EXIT
export LOOPS_REPO_ROOT="$FW"; unset KB_DATABASE_URL KB_CONFIG AGENT_KIT_OVERLAY KB_ALLOW_UNSHARED
pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); else echo "  [FAIL] $1 — got '$2' want '$3'"; fail=$((fail+1)); fi; }

G="$ROOT/guard"; mkdir -p "$G"
python3 - "$PRR" "$G/pr_feed.py" <<'PY'
import sys
s = open(sys.argv[1]).read()
start = s.index("<<'PYGUARD'"); start = s.index("\n", start) + 1
end = s.index("\nPYGUARD\n", start)
open(sys.argv[2], "w").write(s[start:end] + "\n")
PY
cat > "$ROOT/real_feed.py" <<PY
import json, sys
open("$ROOT/calls.log", "a").write(" ".join(sys.argv[1:]) + "\n")
if sys.argv[1] == "scope":
    o = sys.argv[sys.argv.index("--out") + 1]; f = sys.argv[sys.argv.index("--findings") + 1]
    open(o, "w").write(open(f).read())
print(json.dumps({"status": "ok", "mode": "dry-run", "posted": []}))
PY
guardjson(){ python3 -c "import json,sys; json.dump({'real': sys.argv[1], 'posting': 'dry-run', 'scope': 'own', 'max_threads': 3,
  'receipt': sys.argv[2], 'run_id': 'r1', 'violations': sys.argv[3], 'finding_votes': sys.argv[4]}, open(sys.argv[5], 'w'))" \
  "$ROOT/real_feed.py" "$ROOT/receipt.json" "$ROOT/viol.txt" "$1" "$G/guard.json"; }
post(){ : > "$ROOT/calls.log"; OUT="$(python3 "$G/pr_feed.py" post --pr 7 --findings "$1" 2>"$ROOT/err")"; RC=$?
  POSTED=$(grep -c '^post ' "$ROOT/calls.log"); }
edit(){ python3 -c "import json,sys; p=sys.argv[1]; d=json.load(open(p)); $2; json.dump(d, open(p,'w'))" "$1"; }

# a passing review, built by the real dev-code-review scripts, with its votes dir beside it
R="$ROOT/repo"; mkdir -p "$R/api"
( cd "$R" && git init -q -b main && git config user.email t@t && git config user.name t \
  && echo x > api/a.py && git add -A && git commit -qm b && git checkout -qb fix/x && echo y >> api/a.py )
E="$ROOT/pr-7"; mkdir -p "$E/review-validation"; echo '{"findings": []}' > "$E/review-validation/candidates.json"
python3 "$D/finding_votes.py" ballot --dir "$E/review-validation" >/dev/null
python3 "$D/finding_votes.py" tally --dir "$E/review-validation" --review "$E/review.json" >/dev/null
edit "$E/review.json" 'd["verdict"]="approve"; d["summary"]="clean"'
python3 "$D/review_depth.py" record --review "$E/review.json" --repo "$R" >/dev/null
cp "$E/review.json" "$ROOT/good.json"

guardjson "$D/finding_votes.py"
post "$E/review.json"
ck "a passing review reaches the real post" "$RC/$POSTED" "0/1"

cp "$ROOT/good.json" "$E/review.json"; edit "$E/review.json" 'd.pop("validation")'
post "$E/review.json"; ck "no validation block: refused, nothing posted" "$RC/$POSTED" "3/0"
grep -q "review checks failed" "$ROOT/viol.txt" && pass=$((pass+1)) || { echo "  [FAIL] refusal not recorded as an intervention"; fail=$((fail+1)); }
cp "$ROOT/good.json" "$E/review.json"; edit "$E/review.json" 'd.pop("depth")'
post "$E/review.json"; ck "no depth block: refused" "$RC/$POSTED" "3/0"
cp "$ROOT/good.json" "$E/review.json"; edit "$E/review.json" 'd["validation"]["kept"]=2'
post "$E/review.json"; ck "validation that does not match the votes: refused" "$RC/$POSTED" "3/0"
cp "$ROOT/good.json" "$E/review.json"; mv "$E/review-validation" "$ROOT/rv"
post "$E/review.json"; ck "no votes dir beside the review: refused" "$RC/$POSTED" "3/0"
mv "$ROOT/rv" "$E/review-validation"
echo '{oops' > "$E/review.json"
post "$E/review.json"; ck "unreadable review: refused" "$RC/$POSTED" "3/0"
cp "$ROOT/good.json" "$E/review.json"; guardjson "$ROOT/nope.py"
post "$E/review.json"; ck "checker missing: refused, never passed through" "$RC/$POSTED" "3/0"
guardjson "$D/finding_votes.py"
: > "$ROOT/calls.log"; python3 "$G/pr_feed.py" unreviewed >/dev/null 2>&1
ck "other verbs pass straight through" "$(grep -c '^unreviewed' "$ROOT/calls.log")" "1"

echo "  pr_review_guard_checks: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
