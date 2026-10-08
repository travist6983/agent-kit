#!/usr/bin/env bash
# Regression: infra-prod-verify decision logic (R2). No DB / network needed.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
E="$SK/infra-prod-verify/scripts/evaluate_prod_signal.py"
pass=0; fail=0
t(){ local desc="$1" want="$2"; shift 2
  got=$(python3 "$E" "$@" | python3 -c "import sys,json;print(json.load(sys.stdin)['verdict'])")
  if [ "$got" = "$want" ]; then pass=$((pass+1)); else echo "  [FAIL] $desc → $got (want $want)"; fail=$((fail+1)); fi; }

t "big drop→confirmed"              prod-confirmed --smoke pass --before-rate 100 --after-rate 5
t "weak drop→inconclusive"          inconclusive   --smoke pass --before-rate 100 --after-rate 90
t "got worse→regressed"             prod-regressed --smoke pass --before-rate 100 --after-rate 120
t "smoke fail→regressed"            prod-regressed --smoke fail --before-rate 100 --after-rate 2
t "new signature→regressed"         prod-regressed --smoke pass --before-rate 100 --after-rate 2 --new-signature true
# boundary: exactly at the min-drop threshold (50% drop, min_drop 0.5) → confirmed
t "exactly at min-drop→confirmed"   prod-confirmed --smoke pass --before-rate 100 --after-rate 50
# rollback flag must be set on regression
RB=$(python3 "$E" --smoke pass --before-rate 100 --after-rate 120 | python3 -c "import sys,json;print(json.load(sys.stdin)['rollback_recommended'])")
if [ "$RB" = "True" ]; then pass=$((pass+1)); else echo "  [FAIL] regression sets rollback_recommended"; fail=$((fail+1)); fi

echo "  prod_signal: $pass passed, $fail failed"
[ "$fail" = 0 ]
