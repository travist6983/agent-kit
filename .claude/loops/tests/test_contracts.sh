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
# dev-fix v1.4.0 (root_cause_trace): a fix at the producer of a should-never-exist state passes; the
# same receipt with the fix landed at the symptom site must FAIL (validate.py DEV-FIX-RC1)
must_pass "$C/dev-fix.json" "$C/samples/EXAMPLE-fix-root-cause/fix.json"
must_fail "$C/dev-fix.json" "$C/samples/EXAMPLE-bad-fix-guard-at-symptom/fix.json"

# dev-code-review — the sample review validates as-is
must_pass "$C/dev-code-review.json" "$C/samples/EXAMPLE-defect-loop/review.json"
# dev-code-review v1.2.0: a finding may carry a drafted `harden` block (band, why, candidate_rule,
# must_trip, must_pass); an off-enum band must FAIL
must_pass "$C/dev-code-review.json" "$C/samples/EXAMPLE-review-harden/review.json"
must_fail "$C/dev-code-review.json" "$C/samples/EXAMPLE-bad-review-harden-band/review.json"
# dev-code-review v1.3.0: a finding may carry an integer `line` (EXAMPLE-review-harden has one, above);
# a line written as a range string must FAIL (the eval scorer locates a catch by file + integer line)
must_fail "$C/dev-code-review.json" "$C/samples/EXAMPLE-bad-review-line/review.json"
# dev-code-review v1.4.0 (review convergence): bug_class from the closed vocabulary + instances
# validate; a free-text bug_class must FAIL (the fingerprint needs one word per class)
must_pass "$C/dev-code-review.json" "$C/samples/EXAMPLE-review-bug-class/review.json"
must_fail "$C/dev-code-review.json" "$C/samples/EXAMPLE-bad-review-bug-class/review.json"
# dev-loop-defect.review-rounds v1.0.0 (proposed): review_rounds.py's per-round history;
# a verdict off the enum must FAIL
must_pass "$C/dev-loop-defect.review-rounds.json" "$C/samples/EXAMPLE-review-rounds/review-rounds.json"
must_fail "$C/dev-loop-defect.review-rounds.json" "$C/samples/EXAMPLE-bad-review-rounds/review-rounds.json"

# dev-code-review eval case (v1.0.0, proposed): a signed re-fix bad case and a signed clean case pass;
# a bad case naming no bug must FAIL (validate.py DEV-REVIEW-EVAL-K1)
must_pass "$C/dev-code-review.eval-case.json" "$C/samples/EXAMPLE-reviewer-eval-bad/case.json"
must_pass "$C/dev-code-review.eval-case.json" "$C/samples/EXAMPLE-reviewer-eval-clean/case.json"
must_fail "$C/dev-code-review.eval-case.json" "$C/samples/EXAMPLE-bad-reviewer-eval-case/case.json"
# dev-code-review eval score (v1.0.0, proposed): real reviewer_eval.py output over the seed set
must_pass "$C/dev-code-review.eval-score.json" "$C/samples/EXAMPLE-reviewer-eval/score.json"

# dev-harden (v1.0.0, proposed): a proposed rule and a proposed guideline pass; an off-enum band
# fails, and a guideline that skips the static/verification bands without a reason must FAIL
# (validate.py's DEV-HARDEN-L1 anti-prose guard)
must_pass "$C/dev-harden.json" "$C/samples/EXAMPLE-harden-rule/harden.json"
must_pass "$C/dev-harden.json" "$C/samples/EXAMPLE-harden-prose/harden.json"
must_fail "$C/dev-harden.json" "$C/samples/EXAMPLE-bad-harden-band/harden.json"
must_fail "$C/dev-harden.json" "$C/samples/EXAMPLE-bad-harden-prose-skip/harden.json"

# dev-pre-pr — the readiness gate. A blocked verdict (stale evidence + violations) and a
# warnings verdict (heuristics cleared, no blockers).
must_pass "$C/dev-pre-pr.json" "$C/samples/EXAMPLE-pre-pr-blocked/pre-pr.json"
must_pass "$C/dev-pre-pr.json" "$C/samples/EXAMPLE-pre-pr-warnings/pre-pr.json"
# dev-pre-pr v1.3.0: the post-diff `risk` block + the `judged` commit binding. EXAMPLE-pre-pr-blocked
# above carries neither (they are optional); a T0 run that outgrew its tier passes as `warnings`;
# lowering the tier (DEV-PRE-PR-T1), counting an agent approval as human (enum [false]) and saying
# `ready` while escalated (DEV-PRE-PR-R1) must FAIL
must_pass "$C/dev-pre-pr.json" "$C/samples/EXAMPLE-pre-pr-risk-escalated/pre-pr.json"
must_fail "$C/dev-pre-pr.json" "$C/samples/EXAMPLE-bad-pre-pr-deescalated/pre-pr.json"
must_fail "$C/dev-pre-pr.json" "$C/samples/EXAMPLE-bad-pre-pr-agent-approval/pre-pr.json"
must_fail "$C/dev-pre-pr.json" "$C/samples/EXAMPLE-bad-pre-pr-ready-escalated/pre-pr.json"

# qa-verify collateral (v1.0.0, proposed): a rung-4 safety fact with its command and output passes;
# the same claim without them must FAIL (validate.py QA-COLLATERAL-P1)
must_pass "$C/qa-verify.collateral.json" "$C/samples/EXAMPLE-collateral-safe/collateral.json"
must_fail "$C/qa-verify.collateral.json" "$C/samples/EXAMPLE-bad-collateral-unbacked/collateral.json"

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

# onboarding loop (v1.0.0: the map every claim cites, and the blind quiz that grades it)
must_pass "$C/dev-onboard-survey.json" "$C/samples/EXAMPLE-onboard/survey.json"
# negative: a command purpose outside the vocabulary must FAIL (the survey never invents a kind)
must_fail "$C/dev-onboard-survey.json" "$C/samples/EXAMPLE-onboard-bad-command/survey.json"
must_pass "$C/dev-onboard-card.json" "$C/samples/EXAMPLE-onboard/cards/invoices.how.json"
must_pass "$C/dev-onboard-card.json" "$C/samples/EXAMPLE-onboard/cards/invoices.why.json"
# negative: a lens other than how or why must FAIL (the two lenses are the only readers)
must_fail "$C/dev-onboard-card.json" "$C/samples/EXAMPLE-onboard-bad-card/invoices.how.json"
must_pass "$C/dev-onboard-map.json" "$C/samples/EXAMPLE-onboard/map.json"
must_pass "$C/qa-onboard-quiz.json" "$C/samples/EXAMPLE-onboard/quiz.json"
# negative: an off-enum failure_class must FAIL (locks the classify-before-relearn vocabulary)
must_fail "$C/qa-onboard-quiz.json" "$C/samples/EXAMPLE-onboard-bad-class/quiz.json"
must_pass "$C/dev-loop-onboard.json" "$C/samples/EXAMPLE-onboard/route.json"
# negative: an action the recipe does not know and a reader mode that does not exist
must_fail "$C/dev-loop-onboard.json" "$C/samples/EXAMPLE-onboard-bad-route/route.json"

echo "  contracts: $pass passed, $fail failed"
[ "$fail" = 0 ]
