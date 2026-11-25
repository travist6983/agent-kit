#!/usr/bin/env bash
# Regression: dev-pre-pr's post-diff risk reclassification (risk_reclass.py).
#
# The rules under test: the DIFF sets the tier, through assign_tier.py's own decide() (one rule
# set, never two); the result never moves below the triage tier; the old "files > 2x likely_files"
# prose rule fires mechanically; an independent second check can demand a human anyway; the
# number of HUMAN approvals comes from loop-limits.json risk_reclass (read, not hard-coded) and an
# agent approval never counts as one. Every unanswerable input fails closed with exit 2.
#
# Offline: throwaway git repos with no remote, a stack.config + conventions fixture through
# LOOPS_STACK_CONFIG. No brain, no VCS host, no model calls. Scratch dir, self-cleaning.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
FW="$(cd "$SK/../.." && pwd)"
RR="$SK/dev-pre-pr/scripts/risk_reclass.py"
ROOT="$(mktemp -d)"; trap 'rm -rf "$ROOT"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }

export LOOPS_REPO_ROOT="$FW"
unset KB_DATABASE_URL KB_CONFIG AGENT_KIT_OVERLAY KB_ALLOW_UNSHARED
cat > "$ROOT/conventions.json" <<'JSON'
{"test_path_regex": "(\\.spec\\.|\\.test\\.|_test\\.|_spec\\.|(^|/)tests?/|(^|/)__tests__/)",
 "projects": [{"name": "api", "path_globs": ["api/**"]}, {"name": "web", "path_globs": ["web/**"]}],
 "shared_core_globs": ["api/core/**"],
 "blast_radius_denylist_globs": [],
 "rules": []}
JSON
cp "$FW/.claude/loops/loop-limits.json" "$ROOT/limits.json"
cat > "$ROOT/stack.json" <<JSON
{"project": {"name": "risk-reclass-test", "default_branch": "main"},
 "stack": {"conventions_file": "$ROOT/conventions.json"},
 "limits_file": "$ROOT/limits.json"}
JSON
export LOOPS_STACK_CONFIG="$ROOT/stack.json"

# ---- a fresh repo per case: main with a base tree, then a fix branch ----------------------------
n=0
mkrepo(){ n=$((n+1)); R="$ROOT/repo$n"; E="$ROOT/evid$n"; mkdir -p "$R" "$E"
  ( cd "$R" && git init -q -b main && git config user.email t@t && git config user.name t
    mkdir -p api/tests api/core web
    for i in 1 2 3 4 5 6 7 8 9 10; do printf 'def f%s():\n    return %s\n' "$i" "$i" > "api/m$i.py"; done
    printf 'def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n' > api/tests/test_m.py
    printf 'x = 1\n' > api/core/base.py
    printf 'export const a = 1;\n' > web/a.js
    git add -A && git commit -qm base && git checkout -qb fix/x ); }
touch_src(){ local k; for k in $(seq 1 "$1"); do printf '# changed\n' >> "$R/api/m$k.py"; done; }
ctx(){ printf '%s' "$1" > "$E/context.json"; }
intake(){ printf '%s' "$1" > "$E/intake.json"; }
fixj(){ printf '%s' "$1" > "$E/fix.json"; }
BUG='{"issue_type":"Bug","risk_class":"standard","sensitive":false}'
T0CTX='{"tier":"T0","layer":"BE","likely_files":["api/m1.py","api/m2.py"],"gherkin":[{"scenario":"s1"}]}'

run(){ OUT=$(python3 "$RR" --evid "$E" --repo "$R" "$@" 2>/dev/null); RC=$?; }
f(){ printf '%s' "$OUT" | python3 -c "import sys,json
d=json.load(sys.stdin); v=d
for k in '$1'.split('.'):
    v = v.get(k) if isinstance(v, dict) else None
print(json.dumps(v) if not isinstance(v, str) else v)" 2>/dev/null; }
want(){ local desc="$1" path="$2" exp="$3" got; got=$(f "$path")
  if [ "$got" = "$exp" ]; then ok; else bad "$desc: $path=$got (want $exp)"; fi; }
want_rc(){ if [ "$RC" = "$2" ]; then ok; else bad "$1: exit $RC (want $2) $OUT"; fi; }

# ---- 1. small diff, T0 triage → stays T0, one human ----------------------------------------------
mkrepo; ctx "$T0CTX"; intake "$BUG"; touch_src 2; run
want_rc "T0 + 2 files" 0
want "T0 + 2 files" risk.tier T0
want "T0 + 2 files" risk.escalated false
want "T0 + 2 files" risk.human_approvals_required 1
want "T0 + 2 files" risk.agent_approval_counts_as_human false
want "T0 + 2 files" risk.human_gate.demanded false

# ---- 2. T0 + 5 files → T1, escalated with a files reason (and scope surprise: 5 > 2x2) ------------
mkrepo; ctx "$T0CTX"; intake "$BUG"; touch_src 5; run
want "T0 + 5 files" risk.tier T1
want "T0 + 5 files" risk.escalated true
if f risk.reasons | grep -q 'files=5'; then ok; else bad "T0 + 5 files: no files reason in $(f risk.reasons)"; fi
if f warnings | grep -q 'rose from T0 to T1'; then ok; else bad "T0 + 5 files: no escalation warning"; fi
want "T0 + 5 files" risk.agent_approval_counts_as_human false

# ---- 3. T0 + 8 files → T2, two humans ------------------------------------------------------------
mkrepo; ctx "$T0CTX"; intake "$BUG"; touch_src 8; run
want "T0 + 8 files" risk.tier T2
want "T0 + 8 files" risk.diff_tier T2
want "T0 + 8 files" risk.human_approvals_required 2

# ---- 4. never de-escalates: T2 triage + a one-file diff stays T2 -----------------------------------
mkrepo; ctx '{"tier":"T2","layer":"BE","likely_files":["api/m1.py"],"gherkin":[{"scenario":"s1"}]}'
intake "$BUG"; touch_src 1; run
want "T2 + 1 file" risk.tier T2
want "T2 + 1 file" risk.diff_tier T0
want "T2 + 1 file" risk.escalated false
want "T2 + 1 file" risk.triage_tier T2

# ---- 5. scope surprise fires at > 2x likely_files, not at exactly 2x -----------------------------
# triage T1 so decide() (T1 for 3..7 files) cannot move it: only the surprise can.
mkrepo; ctx '{"tier":"T1","layer":"BE","likely_files":["api/m1.py","api/m2.py"],"gherkin":[{"scenario":"s1"}]}'
intake "$BUG"; touch_src 4; run
want "exactly 2x" risk.tier T1
want "exactly 2x" risk.escalated false
mkrepo; ctx '{"tier":"T1","layer":"BE","likely_files":["api/m1.py","api/m2.py"],"gherkin":[{"scenario":"s1"}]}'
intake "$BUG"; touch_src 5; run
want "above 2x" risk.tier T2
want "above 2x" risk.diff_tier T1
want "above 2x" risk.escalated true
if f risk.reasons | grep -q 'scope surprise: 5 non-test files'; then ok; else bad "above 2x: no scope-surprise reason"; fi

# ---- 6. cross-layer: a diff spanning two projects with triage layer BE is not T0 -----------------
mkrepo; ctx "$T0CTX"; intake "$BUG"; touch_src 1; printf '// x\n' >> "$R/web/a.js"; run
want "two projects" risk.tier T1
want "two projects" risk.escalated true
if f risk.reasons | grep -q 'cross-layer'; then ok; else bad "two projects: no cross-layer reason"; fi

# ---- 7. a shared-core hit → T2 ---------------------------------------------------------------------
mkrepo; ctx "$T0CTX"; intake "$BUG"; printf 'y = 2\n' >> "$R/api/core/base.py"; run
want "shared core" risk.tier T2
if f risk.facts.shared_core_hits | grep -q 'api/core/base.py'; then ok; else bad "shared core: hit not recorded"; fi

# ---- 8. the human gate, each trigger alone at T0 → approvals = human_gate_min_approvals ------------
mkrepo; ctx "$T0CTX"; intake "$BUG"; touch_src 1; fixj '{"is_root_cause": false}'; run
want "symptom-only" risk.tier T0
want "symptom-only" risk.human_gate.triggers '["symptom-only-fix"]'
want "symptom-only" risk.human_approvals_required 2
mkrepo; ctx "$T0CTX"; intake "$BUG"; touch_src 1; fixj '{"is_root_cause": true}'; run
want "root cause true" risk.human_gate.demanded false

mkrepo; ctx "$T0CTX"; intake "$BUG"; touch_src 1
printf 'def test_a():\n    assert 1\n' > "$R/api/tests/test_m.py"; run   # test_b removed
want "removed test lines" risk.human_gate.triggers '["tests-weakened"]'
want "removed test lines" risk.human_approvals_required 2
want "removed test lines" risk.facts.tests_weakened '["api/tests/test_m.py"]'

mkrepo; ctx "$T0CTX"; intake "$BUG"; touch_src 1; ( cd "$R" && git rm -q api/tests/test_m.py ); run
want "deleted test" risk.human_gate.triggers '["tests-weakened"]'

mkrepo; ctx "$T0CTX"; intake "$BUG"; touch_src 1; mkdir -p "$R/api/tests"
printf 'def test_new():\n    assert 3\n' > "$R/api/tests/test_new.py"
printf '\ndef test_c():\n    assert 4\n' >> "$R/api/tests/test_m.py"; run   # added only
want "new + extended test" risk.human_gate.demanded false
want "new + extended test" risk.tier T0
want "added tests do not count as files" risk.facts.files 1
want "added tests counted as test_files" risk.facts.test_files 2

mkrepo; ctx "$T0CTX"; intake "$BUG"; touch_src 1; mkdir -p "$R/api/migrations"
printf 'ALTER TABLE x;\n' > "$R/api/migrations/0002.sql"; run
want "denylisted path" risk.human_gate.triggers '["blast-radius"]'
want "denylisted path" risk.human_approvals_required 2

mkrepo; ctx "$T0CTX"; intake '{"issue_type":"Bug","risk_class":"standard","sensitive":true}'; touch_src 1; run
if f risk.human_gate.triggers | grep -q 'sensitive-area'; then ok; else bad "sensitive: no sensitive-area trigger"; fi
want "sensitive" risk.human_gate.demanded true

# ---- 9. no context / intake → untiered, baseline T1 -------------------------------------------------
mkrepo; touch_src 1; run
want_rc "untiered" 0
want "untiered" risk.triage_tier null
want "untiered" risk.tier T1
want "untiered" risk.escalated false

# ---- 10. a higher tier in tier_history wins over context.tier ---------------------------------------
mkrepo; ctx '{"tier":"T0","tier_history":[{"tier":"T0","reason":"triage"},{"tier":"T1","reason":"scope"}],"layer":"BE","likely_files":["api/m1.py"],"gherkin":[{"scenario":"s1"}]}'
intake "$BUG"; touch_src 1; run
want "tier_history" risk.triage_tier T1
want "tier_history" risk.tier T1

# ---- 11. the numbers are READ: a fixture with T2 = 3 changes the output -------------------------------
mkrepo; ctx "$T0CTX"; intake "$BUG"; touch_src 8
python3 - "$ROOT/limits.json" "$ROOT/limits3.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d["risk_reclass"]["human_approvals_by_tier"]["T2"] = 3
json.dump(d, open(sys.argv[2], "w"))
PY
run --limits "$ROOT/limits3.json"
want "limits read (T2=3)" risk.human_approvals_required 3

# ---- 12. fail closed (exit 2) ----------------------------------------------------------------------
badlim(){ python3 - "$ROOT/limits.json" "$ROOT/bad.json" "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); exec(sys.argv[3])
json.dump(d, open(sys.argv[2], "w"))
PY
}
mkrepo; ctx "$T0CTX"; intake "$BUG"; touch_src 1
badlim 'del d["risk_reclass"]["human_gate_min_approvals"]'; run --limits "$ROOT/bad.json"; want_rc "limits missing a number" 2
badlim 'd["risk_reclass"]["human_approvals_by_tier"]["T0"] = 0'; run --limits "$ROOT/bad.json"; want_rc "approvals 0" 2
badlim 'd["risk_reclass"]["scope_surprise_factor"] = True'; run --limits "$ROOT/bad.json"; want_rc "factor true" 2
badlim 'del d["risk_reclass"]'; run --limits "$ROOT/bad.json"; want_rc "no block" 2
run --limits "$ROOT/nope.json"; want_rc "limits unreadable" 2
mkdir -p "$ROOT/notgit"; OUT=$(python3 "$RR" --evid "$E" --repo "$ROOT/notgit" 2>/dev/null); RC=$?; want_rc "non-git dir" 2
OUT=$(LOOPS_CFG__PROJECT__DEFAULT_BRANCH="" python3 "$RR" --evid "$E" --repo "$R" 2>/dev/null); RC=$?; want_rc "no base" 2
run --base no-such-branch; want_rc "unknown base" 2
if printf '%s' "$OUT" | python3 -c "import sys,json; assert 'error' in json.load(sys.stdin)" 2>/dev/null; then ok; else bad "exit 2 prints {error}"; fi

# ---- 13. the emitted block embeds in a contract-valid pre-pr.json ---------------------------------
mkrepo; ctx "$T0CTX"; intake "$BUG"; touch_src 8; run
printf '%s' "$OUT" | python3 -c "
import json, sys
r = json.load(sys.stdin)
doc = {'status': 'warnings', 'next_action': 'ship-ok', 'checks': {}, 'blockers': [],
       'warnings': r['warnings'], 'summary': 'test', 'risk': r['risk']}
json.dump(doc, open('$ROOT/pre-pr.json', 'w'))"
if python3 "$FW/contracts/validate.py" "$FW/contracts/dev-pre-pr.json" "$ROOT/pre-pr.json" >/dev/null; then ok
else bad "emitted risk block fails the contract: $(python3 "$FW/contracts/validate.py" "$FW/contracts/dev-pre-pr.json" "$ROOT/pre-pr.json")"; fi

# ---- 14. tripwire: the vcs_host adapter can never approve, vote or merge ---------------------------
if python3 -c "
import re, sys; sys.path.insert(0, '$FW')
from lib.adapters.base import OPS
bad = [o for o in OPS['vcs_host'] if re.search(r'approve|vote|merge', o, re.I)]
sys.exit(1 if bad else 0)"; then ok; else bad "vcs_host OPS gained an approve/vote/merge op — an agent could fill a human slot"; fi

# ---- 15. no tier threshold is duplicated: the script imports decide() --------------------------
if grep -q 'mod.decide' "$RR" && ! grep -qE '>= *8|<= *3' "$RR"; then ok; else bad "risk_reclass.py carries its own tier thresholds"; fi

echo "  risk_reclass: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
