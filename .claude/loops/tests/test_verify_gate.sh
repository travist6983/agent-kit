#!/usr/bin/env bash
# Regression: dev-pre-pr's fix-verification gate (verify_gate.py).
#
# The invariant under test: a tier may shrink HOW MUCH is verified, never WHETHER. This gate
# is what makes the parallel verify ∥ regression ∥ review layer safe — without it a crashed
# or failed qa-verify reaches dev-pre-pr beside two green artifacts and nothing notices.
#
# No DB, no network, no model calls. Scratch dir, self-cleaning.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
G="$SK/dev-pre-pr/scripts/verify_gate.py"
pass=0; fail=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# t <desc> <want_fix_verified> <want_next_action> <want_exit> -- <args...>
t(){ local desc="$1" wfv="$2" wna="$3" wex="$4"; shift 5
  local o rc
  o=$(python3 "$G" "$@" 2>&1); rc=$?
  local gfv gna
  gfv=$(printf '%s' "$o" | python3 -c "import sys,json;print(json.load(sys.stdin)['fix_verified'])" 2>/dev/null)
  gna=$(printf '%s' "$o" | python3 -c "import sys,json;print(json.load(sys.stdin)['next_action'])" 2>/dev/null)
  if [ "$gfv" = "$wfv" ] && [ "$gna" = "$wna" ] && [ "$rc" = "$wex" ]; then
    pass=$((pass+1))
  else
    echo "  [FAIL] $desc"
    echo "         got  fix_verified=$gfv next_action=$gna exit=$rc"
    echo "         want fix_verified=$wfv next_action=$wna exit=$wex"
    fail=$((fail+1))
  fi; }

mk(){ printf '%s' "$2" > "$TMP/$1"; }

# ---------- the happy path -------------------------------------------------------------
mk pass.json '{"verdict":"pass","confidence":90,"masking_suspected":false,"replay":"red→green"}'
t "verdict=pass, fresh → ok" pass continue 0 -- --verify "$TMP/pass.json"

# ---------- the gate's whole reason for existing ---------------------------------------
t "MISSING inside a loop → escalate" missing escalate 1 -- --verify "$TMP/nope.json" --loop
t "MISSING outside a loop → n/a"     not-applicable continue 0 -- --verify "$TMP/nope.json" --no-loop
# fail closed: with no --loop/--no-loop we must assume we ARE in a loop (the stricter rule)
t "MISSING, loop unstated → blocks"  missing escalate 1 -- --verify "$TMP/nope.json"

# ---------- a fix that does not work ----------------------------------------------------
mk fail.json '{"verdict":"fail","confidence":40}'
t "verdict=fail → re-fix"      fail re-fix 1 -- --verify "$TMP/fail.json"
mk partial.json '{"verdict":"partial","confidence":55}'
t "verdict=partial → re-fix"   fail re-fix 1 -- --verify "$TMP/partial.json"

# blocked is NOT a failed fix — it is an absent test. Never burn a re-fix on no evidence.
mk blocked.json '{"verdict":"blocked","confidence":0}'
t "verdict=blocked → escalate"  fail escalate 1 -- --verify "$TMP/blocked.json"

# ---------- R3: a masked pass is a fail --------------------------------------------------
mk masked.json '{"verdict":"pass","confidence":88,"masking_suspected":true}'
t "pass + masking_suspected → re-fix" fail re-fix 1 -- --verify "$TMP/masked.json"

# ---------- malformed evidence is not evidence -------------------------------------------
mk bad.json '{"verdict":'
t "unparseable JSON → escalate"    malformed escalate 1 -- --verify "$TMP/bad.json"
mk novk.json '{"confidence":90}'
t "no verdict key → escalate"      malformed escalate 1 -- --verify "$TMP/novk.json"
mk arr.json '[{"verdict":"pass"}]'
t "array not object → escalate"    malformed escalate 1 -- --verify "$TMP/arr.json"
mk num.json '{"verdict":1}'
t "verdict not a string → escalate" malformed escalate 1 -- --verify "$TMP/num.json"
mk stale_vocab.json '{"verdict":"passed"}'
t "stale vocabulary → escalate"    malformed escalate 1 -- --verify "$TMP/stale_vocab.json"

# ---------- staleness: a PASS older than the code it graded ------------------------------
mk src.js 'console.log(1)'
sleep 1
mk fresh_pass.json '{"verdict":"pass","confidence":90}'
t "pass newer than source → ok" pass continue 0 -- --verify "$TMP/fresh_pass.json" --changed-file "$TMP/src.js"
sleep 1
mk src.js 'console.log(2)'   # source now newer than the verify
t "pass OLDER than source → re-fix" fail re-fix 1 -- --verify "$TMP/fresh_pass.json" --changed-file "$TMP/src.js"

# a changed file that does not exist must not be mistaken for staleness
t "nonexistent changed file ignored" pass continue 0 -- --verify "$TMP/pass.json" --changed-file "$TMP/ghost.js"

# ---------- git-derived staleness must fail closed when git cannot answer ------------------
t "git unavailable → escalate" malformed escalate 1 -- --verify "$TMP/pass.json" --changed-from-git --repo "$TMP"

echo "test_verify_gate: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
