#!/usr/bin/env bash
# Regression: the dev-loop-* retry/escalate router (the shared engine). No DB / network needed.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
R="$SK/dev-loop-defect/scripts/recipe_router.py"
pass=0; fail=0
t(){ local desc="$1" want="$2"; shift 2
  got=$(python3 "$R" "$@" | python3 -c "import sys,json;print(json.load(sys.stdin)['action'])")
  if [ "$got" = "$want" ]; then pass=$((pass+1)); else echo "  [FAIL] $desc → $got (want $want)"; fail=$((fail+1)); fi; }

# v2.0.0: there is no heal lane — a stale pre-2.0.0 "drift" result must escalate, never route to a dead skill
t "retired drift result→escalate"      escalate    --stage regression --result drift --attempt 1 --max 3
t "same-area→re-fix"                   re-fix      --stage regression --result same-area-regression --attempt 1 --max 3
t "unrelated-break→escalate(no retry)" escalate    --stage regression --result unrelated-break --attempt 1 --max 3
t "same-area at cap→escalate"          escalate    --stage regression --result same-area-regression --attempt 3 --max 3
t "verify fail at cap→escalate"        escalate    --stage verify --result fail --attempt 3 --max 3
t "verify pass low-conf sensitive→esc" escalate    --stage verify --result pass --confidence 70 --sensitive true
t "verify pass high-conf→advance"      advance     --stage verify --result pass --confidence 95
# replay-oracle.sh exit 2 → qa-verify `blocked`: no evidence about the fix, so escalate — never burn a re-fix on it
t "verify blocked (oracle exit 2)→esc" escalate    --stage verify --result blocked --attempt 1 --max 3
t "R5 budget breach→stop-budget"       stop-budget --stage fix --result fixed --budget-spent 100 --budget-cap 100
t "R9 paused→halt-paused"              halt-paused --stage ship --result ok --paused true
t "prod-regressed→rollback"            rollback    --stage prod-verify --result prod-regressed
t "prod-confirmed→advance"             advance     --stage prod-verify --result prod-confirmed
t "review request-changes→re-fix"      re-fix      --stage code-review --result request-changes --attempt 1 --max 3
t "repro not_reproduced→reject"        reject      --stage repro --result not_reproduced
t "repro inconclusive→re-repro"        re-repro    --stage repro --result inconclusive --attempt 1 --max 3
t "repro inconclusive at cap→escalate" escalate    --stage repro --result inconclusive --attempt 3 --max 3
t "repro STALE hyphen form→escalate"   escalate    --stage repro --result not-reproduced
t "blocked-environment→escalate"       escalate    --stage regression --result blocked-environment --attempt 1 --max 3
t "blocked-slice-empty→escalate"       escalate    --stage regression --result blocked-slice-empty --attempt 1 --max 3
# adversarial / precedence
t "paused beats budget breach"         halt-paused --stage fix --result fixed --paused true --budget-spent 100 --budget-cap 100
# DEV-LOOP-DEFECT-M4 — safety flags fail CLOSED on malformed input
t "malformed --paused True→halt"       halt-paused --stage fix --result fixed --paused True
t "malformed --paused garbage→halt"    halt-paused --stage ship --result ok --paused maybe
t "malformed --sensitive→sensitive"    escalate    --stage verify --result pass --confidence 70 --sensitive Yup
# X-SAFE-C1 — cap defaults from loop-limits.json (15.0); explicit 0 disables
t "config default cap catches spend"   stop-budget --stage fix --result fixed --budget-spent 20
t "explicit --budget-cap 0 disables"   advance     --stage fix --result fixed --budget-spent 100 --budget-cap 0
t "regression flaky→advance"           advance     --stage regression --result flaky --attempt 1 --max 3
t "ship preflight global-pause→halt"   halt-paused --stage ship --result global-pause

echo "  recipe_router: $pass passed, $fail failed"
[ "$fail" = 0 ]
