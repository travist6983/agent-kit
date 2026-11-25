#!/usr/bin/env bash
# Regression: dev-pre-pr's review_gate.py — a review.json reaching pre-pr must pass the three
# review checks (finding_votes, review_depth, review_brief) and carry validation + depth blocks.
# Every gap fails closed. Offline: a throwaway git repo, the real dev-code-review scripts build
# the passing review. No brain, no VCS host, no model calls.
set -u
FW="$(cd "$(dirname "$0")/../../.." && pwd)"; SK="$FW/.claude/skills"
D="$SK/dev-code-review/scripts"; G="$SK/dev-pre-pr/scripts/review_gate.py"
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/review-gate.XXXXXX")" || { echo "  review_gate: SKIPPED (mktemp failed)"; exit 0; }
trap 'rm -rf "$ROOT"' EXIT
export LOOPS_REPO_ROOT="$FW"; unset KB_DATABASE_URL KB_CONFIG AGENT_KIT_OVERLAY KB_ALLOW_UNSHARED
pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); else echo "  [FAIL] $1 — got '$2' want '$3'"; fail=$((fail+1)); fi; }
gate(){ OUT="$(python3 "$G" "$@")"; RC=$?; }
st(){ printf '%s' "$OUT" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])"; }
edit(){ python3 -c "import json,sys; p=sys.argv[1]; d=json.load(open(p)); $2; json.dump(d, open(p,'w'))" "$1"; }

R="$ROOT/repo"; mkdir -p "$R/api"
( cd "$R" && git init -q -b main && git config user.email t@t && git config user.name t \
  && echo x > api/a.py && git add -A && git commit -qm base && git checkout -qb fix/x && echo y >> api/a.py )
E="$ROOT/PROJ-1"; mkdir -p "$E/review-validation"
echo '{"findings": []}' > "$E/review-validation/candidates.json"
python3 "$D/finding_votes.py" ballot --dir "$E/review-validation" >/dev/null
python3 "$D/finding_votes.py" tally --dir "$E/review-validation" --review "$E/review.json" >/dev/null
edit "$E/review.json" 'd["verdict"]="approve"; d["summary"]="clean"; d["key"]="PROJ-1"'
python3 "$D/review_depth.py" record --review "$E/review.json" --repo "$R" --evid "$E" >/dev/null
python3 "$D/review_brief.py" finalize --review "$E/review.json" --repo "$R" >/dev/null
cp "$E/review.json" "$ROOT/good.json"
reset(){ cp "$ROOT/good.json" "$E/review.json"; }

gate --evid "$E" --repo "$R" --require-review
ck "a review built by the real scripts passes" "$RC/$(st)" "0/pass"

reset; edit "$E/review.json" 'd.pop("validation")'
gate --evid "$E" --repo "$R"; ck "no validation block blocks (even without --require-review)" "$RC/$(st)" "1/blocked"
reset; edit "$E/review.json" 'd.pop("depth")'
gate --evid "$E" --repo "$R"; ck "no depth block blocks" "$RC/$(st)" "1/blocked"
reset; edit "$E/review.json" 'd["depth"]["level"]="quick"; d["depth"]["passes"]=d["depth"]["passes"][:1]'
gate --evid "$E" --repo "$R"; ck "an in-loop review recorded at quick blocks (depth check)" "$RC/$(st)" "1/blocked"
reset; edit "$E/review.json" 'd["validation"]["kept"]=3'
gate --evid "$E" --repo "$R"; ck "validation counts that do not match the votes block" "$RC/$(st)" "1/blocked"
reset; mv "$E/review-validation" "$ROOT/rv"
gate --evid "$E" --repo "$R"; ck "missing validation evidence dir blocks" "$RC/$(st)" "1/blocked"
mv "$ROOT/rv" "$E/review-validation"
echo '{not json' > "$E/review.json"
gate --evid "$E" --repo "$R"; ck "unparsable review.json blocks" "$RC/$(st)" "1/blocked"
echo '[]' > "$E/review.json"
gate --evid "$E" --repo "$R"; ck "non-object review.json blocks" "$RC/$(st)" "1/blocked"
rm -f "$E/review.json"
gate --evid "$E" --repo "$R" --require-review; ck "missing review with a review stage blocks" "$RC/$(st)" "1/blocked"
gate --evid "$E" --repo "$R"; ck "missing review without a review stage is not-applicable" "$RC/$(st)" "0/not-applicable"
gate --evid "$ROOT/nope" --repo "$R"; ck "missing evidence dir blocks" "$RC/$(st)" "1/blocked"
python3 "$G" --repo "$R" >/dev/null 2>&1; ck "bad usage exits 2" "$?" "2"

echo "  review_gate: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
