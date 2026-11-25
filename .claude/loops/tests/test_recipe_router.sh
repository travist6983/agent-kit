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
# a rounds file with one round open: converging, so request-changes re-fixes
RR1="$(mktemp "${TMPDIR:-/tmp}/rr1.XXXXXX")"; trap 'rm -f "$RR1"' EXIT
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); d['rounds']=d['rounds'][:1]; json.dump(d,open(sys.argv[2],'w'))" \
  "$SK/../../contracts/samples/EXAMPLE-review-rounds/review-rounds.json" "$RR1"
t "review request-changes→re-fix"      re-fix      --stage code-review --result request-changes --attempt 1 --max 3 --review-rounds "$RR1"
t "request-changes, no rounds→escalate" escalate   --stage code-review --result request-changes --attempt 1 --max 3
t "approve needs no rounds→advance"    advance     --stage code-review --result approve
# a review that produced no verdict is not a finding about the fix: escalate, never re-fix
t "review validation-unavailable→esc"  escalate    --stage code-review --result validation-unavailable --attempt 1 --max 3
t "review blocked→escalate"            escalate    --stage code-review --result blocked --attempt 1 --max 3
# dev-code-review v1.8.0: a split on a masking or critical finding is needs-human — escalate at the
# first attempt, never re-fix, and say so with failure_class review-needs-human (even with a
# review-rounds file that would otherwise call stagnation, and far below the retry cap)
t "review needs-human→escalate"        escalate    --stage code-review --result needs-human --attempt 1 --max 5
fcj(){ python3 "$R" "$@" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('failure_class'), 'human' in d.get('reason',''))"; }
got=$(fcj --stage code-review --result needs-human --attempt 1 --max 5)
if [ "$got" = "review-needs-human True" ]; then pass=$((pass+1)); else echo "  [FAIL] needs-human failure_class/reason → $got"; fail=$((fail+1)); fi
got=$(fcj --stage code-review --result needs-human --attempt 2 --max 5 --review-rounds "$SK/../../contracts/samples/EXAMPLE-review-rounds/review-rounds.json")
if [ "$got" = "review-needs-human True" ]; then pass=$((pass+1)); else echo "  [FAIL] needs-human with review-rounds → $got (a human gate outranks stagnation)"; fail=$((fail+1)); fi
got=$(fcj --stage code-review --result request-changes --attempt 1 --max 5 --review-rounds "$RR1")
if [ "$got" = "None False" ]; then pass=$((pass+1)); else echo "  [FAIL] request-changes must stay a plain re-fix → $got"; fail=$((fail+1)); fi
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
# commit binding (commit_binding.py check): a commit that is not what dev-pre-pr judged, or no
# bound verdict at all, escalates — deterministic, never re-shipped or re-fixed
t "ship stale-readiness→escalate"      escalate    --stage ship --result stale-readiness
t "ship readiness-unbound→escalate"    escalate    --stage ship --result readiness-unbound
# pr_sections.py check refused the assembled body (a rendered block missing, edited or doubled, or
# rendered for another commit): no PR opened; the existing unknown-abort fall-through escalates — pinned
t "ship pr-body-incomplete→escalate"   escalate    --stage ship --result pr-body-incomplete
t "ship pr-sections-unsourced→escalate" escalate   --stage ship --result pr-sections-unsourced
fc=$(python3 "$R" --stage ship --result stale-readiness | python3 -c "import sys,json;print(json.load(sys.stdin).get('failure_class'))")
if [ "$fc" = "stale-readiness" ]; then pass=$((pass+1)); else echo "  [FAIL] stale-readiness failure_class → $fc"; fail=$((fail+1)); fi
fc=$(python3 "$R" --stage ship --result readiness-unbound | python3 -c "import sys,json;print(json.load(sys.stdin).get('failure_class'))")
if [ "$fc" = "readiness-unbound" ]; then pass=$((pass+1)); else echo "  [FAIL] readiness-unbound failure_class → $fc"; fail=$((fail+1)); fi
# Review convergence (item 3): the same file+bug_class open in 3 consecutive recorded rounds is
# stagnation and escalates BEFORE the retry cap; without --review-rounds the stage is unchanged.
# Full coverage lives in test_review_rounds.sh.
RR="$SK/../../contracts/samples/EXAMPLE-review-rounds/review-rounds.json"
t "review stagnation→escalate (attempt<max)" escalate --stage code-review --result request-changes --attempt 2 --max 5 --review-rounds "$RR"
t "review no --review-rounds→escalate" escalate    --stage code-review --result request-changes --attempt 2 --max 5

echo "  recipe_router: $pass passed, $fail failed"
[ "$fail" = 0 ]
