#!/usr/bin/env bash
# Regression: every ratified contract validates its samples — and the zero-test
# regression.json (green/ship-ok with zero tests run, validate.py's C1 rule) must keep
# FAILING its contract. Pure validate.py, no DB/network.
# Sample sets: EXAMPLE-defect-loop = one full defect loop (repro → fix → verify → review →
# regression); EXAMPLE-defect-minimal = a minimal standard-defect intake + fix;
# EXAMPLE-intake-already-addressed = a hard-defect intake carrying the already_addressed block.
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
V="$ROOT/contracts/validate.py"
C="$ROOT/contracts"
pass=0; fail=0
must_pass(){ if python3 "$V" "$1" "$2" >/dev/null 2>&1; then pass=$((pass+1)); else echo "  [FAIL] $(basename "$1") ✗ $2"; fail=$((fail+1)); fi; }
must_fail(){ if python3 "$V" "$1" "$2" >/dev/null 2>&1; then echo "  [FAIL] $(basename "$1") wrongly PASSES $2"; fail=$((fail+1)); else pass=$((pass+1)); fi; }

# qa-repro
must_pass "$C/qa-repro.json" "$C/samples/EXAMPLE-defect-loop/repro.json"
must_pass "$C/qa-repro.json" "$C/samples/EXAMPLE-defect-loop/repro.with-media.json"
must_pass "$C/qa-repro.json" "$C/samples/EXAMPLE-inconclusive/repro.json"

# qa-verify repro-judge (v1.2.0: + inconclusive verdict, artifact = judge.json)
must_pass "$C/qa-verify.repro-judge.json" "$C/samples/EXAMPLE-defect-loop/verify.json"
must_pass "$C/qa-verify.repro-judge.json" "$C/samples/EXAMPLE-defect-loop/verify.with-media.json"
must_pass "$C/qa-verify.repro-judge.json" "$C/samples/EXAMPLE-judge-inconclusive/judge.json"

# dev-intake
must_pass "$C/dev-intake.json" "$C/samples/EXAMPLE-intake-already-addressed/intake.json"
must_pass "$C/dev-intake.json" "$C/samples/EXAMPLE-defect-minimal/intake.json"

# dev-fix — both sample receipts validate as-is (string design_decisions included)
must_pass "$C/dev-fix.json" "$C/samples/EXAMPLE-defect-loop/fix.json"
must_pass "$C/dev-fix.json" "$C/samples/EXAMPLE-defect-minimal/fix.json"
# dev-fix v1.2.0 (PR thread bar): must_read + must_read_reason + noticed_not_changed validate; a
# must_read_reason outside the enum must FAIL (select_pr_threads.py refuses it too — exit 2)
must_pass "$C/dev-fix.json" "$C/samples/EXAMPLE-fix-must-read/fix.json"
must_fail "$C/dev-fix.json" "$C/samples/EXAMPLE-bad-must-read-reason/fix.json"
must_fail "$C/dev-fix.json" "$C/samples/EXAMPLE-bad-must-read-no-ask/fix.json"   # must_read:true, no ask

# dev-code-review — the sample review validates as-is
must_pass "$C/dev-code-review.json" "$C/samples/EXAMPLE-defect-loop/review.json"

# dev-pre-pr — the readiness gate. A blocked verdict (stale evidence + violations) and a
# warnings verdict (heuristics cleared, no blockers).
must_pass "$C/dev-pre-pr.json" "$C/samples/EXAMPLE-pre-pr-blocked/pre-pr.json"
must_pass "$C/dev-pre-pr.json" "$C/samples/EXAMPLE-pre-pr-warnings/pre-pr.json"

# qa-run-case (v1.0.0: DN-6 full vocabulary PASS|FAIL|PARTIAL|BLOCKED|MANUAL + orthogonal drift[])
must_pass "$C/qa-run-case.json" "$C/samples/case-4012-run-9001/case-run.json"
must_pass "$C/qa-run-case.json" "$C/samples/case-4055-run-9007/case-run.json"
# negative: a lowercase/off-enum status must FAIL (locks the five-value case grade + four-value step grade)
must_fail "$C/qa-run-case.json" "$C/samples/EXAMPLE-bad-enum/case-run.json"

# qa-diagnose (v1.0.0: PROPOSE-ONLY diagnose lane — root_cause + failure_layer required, diff is a STRING)
must_pass "$C/qa-diagnose.json" "$C/samples/diag-app-bug/diagnose.json"
must_pass "$C/qa-diagnose.json" "$C/samples/diag-inconclusive/diagnose.json"
# negative: an off-enum failure_layer must FAIL (locks the four-value fault-layer vocabulary)
must_fail "$C/qa-diagnose.json" "$C/samples/EXAMPLE-bad-layer/diagnose.json"

# qa-regression v2.0.0 — normalized sample + blocked example pass; the zero-test artifact
# (green/ship-ok with zero tests — validate.py's C1 rule) must FAIL
must_pass "$C/qa-regression.json" "$C/samples/EXAMPLE-defect-loop/regression.json"
must_pass "$C/qa-regression.json" "$C/samples/EXAMPLE-blocked-slice-empty/regression.json"
must_fail "$C/qa-regression.json" "$C/samples/EXAMPLE-bad-green-zero-tests/regression.json"

echo "  contracts: $pass passed, $fail failed"
[ "$fail" = 0 ]
