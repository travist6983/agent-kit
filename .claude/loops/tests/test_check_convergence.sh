#!/usr/bin/env bash
# Regression: the checker node guarding the verify ∥ regression ∥ review convergence point.
#
# Running the three judges in parallel removed the natural checkpoints. This is what stops a
# bad artifact merging silently into dev-pre-pr beside two good ones. The five failure modes
# under test: empty/null, contradictory, off-topic, low-confidence, malformed.
#
# No DB, no network, no model calls. Scratch dir, self-cleaning.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
C="$SK/dev-loop-defect/scripts/check_convergence.py"
pass=0; fail=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# fresh evidence dir with good defaults; $1 = dir name
mkev(){ local d="$TMP/$1"; mkdir -p "$d"
  echo '{"verdict":"pass","confidence":90,"masking_suspected":false}' > "$d/verify.json"
  echo '{"status":"green","results":{"passed":10,"failed":0}}'        > "$d/regression.json"
  echo '{"verdict":"approve","findings":[]}'                          > "$d/review.json"
  echo "$d"; }

# t <desc> <want_status> <want_next_action> <want_exit> -- <args...>
t(){ local desc="$1" ws="$2" wna="$3" wex="$4"; shift 5
  local o rc gs gna
  o=$(python3 "$C" "$@" 2>&1); rc=$?
  gs=$(printf '%s' "$o"  | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])" 2>/dev/null)
  gna=$(printf '%s' "$o" | python3 -c "import sys,json;print(json.load(sys.stdin)['next_action'])" 2>/dev/null)
  if [ "$gs" = "$ws" ] && [ "$gna" = "$wna" ] && [ "$rc" = "$wex" ]; then pass=$((pass+1))
  else echo "  [FAIL] $desc"; echo "         got  status=$gs next=$gna exit=$rc"
       echo "         want status=$ws next=$wna exit=$wex"; fail=$((fail+1)); fi; }

# has a flag of kind $2 in output of running with args $@ (after --)
has_kind(){ local desc="$1" kind="$2"; shift 3
  local o; o=$(python3 "$C" "$@" 2>&1)
  if printf '%s' "$o" | python3 -c "
import sys,json;d=json.load(sys.stdin)
sys.exit(0 if any(f['kind']=='$kind' for f in d['flags']) else 1)" 2>/dev/null; then pass=$((pass+1))
  else echo "  [FAIL] $desc (no '$kind' flag)"; fail=$((fail+1)); fi; }

# ---------- clean ------------------------------------------------------------------------
D=$(mkev clean)
t "all three good → clean" clean continue 0 -- --evid "$D" --key PROJ-3000

# ---------- uncheckable fails CLOSED ------------------------------------------------------
t "missing evid dir → uncheckable" uncheckable escalate 2 -- --evid "$TMP/ghost" --key PROJ-1

# ---------- 1. EMPTY / null ----------------------------------------------------------------
D=$(mkev nullv); echo 'null' > "$D/verify.json"
t "verify is literal null → flagged" flagged escalate 1 -- --evid "$D" --key PROJ-1
has_kind "null verify flags empty" empty -- --evid "$D" --key PROJ-1

D=$(mkev emptyv); : > "$D/verify.json"
t "verify zero-length → flagged" flagged escalate 1 -- --evid "$D" --key PROJ-1

D=$(mkev emptyobj); echo '{}' > "$D/verify.json"
t "verify empty object → flagged" flagged escalate 1 -- --evid "$D" --key PROJ-1

D=$(mkev absent); rm "$D/verify.json"
t "required verify absent → flagged" flagged escalate 1 -- --evid "$D" --key PROJ-1
# review is optional by default — the standard defect recipe has no review stage
D=$(mkev norev); rm "$D/review.json"
t "review absent is OK by default" clean continue 0 -- --evid "$D" --key PROJ-1
t "review absent flagged when required" flagged escalate 1 -- --evid "$D" --key PROJ-1 --require verify,regression,review

# ---------- 2. CONTRADICTORY ----------------------------------------------------------------
D=$(mkev contra1); echo '{"status":"blocked-build"}' > "$D/regression.json"
t "pass verify + broken build → flagged" flagged escalate 1 -- --evid "$D" --key PROJ-1
has_kind "build contradiction flagged" contradictory -- --evid "$D" --key PROJ-1

D=$(mkev contra2)
echo '{"verdict":"approve","findings":[{"severity":"critical","title":"x"}]}' > "$D/review.json"
t "approve + unresolved CRITICAL → blocks" flagged re-fix 1 -- --evid "$D" --key PROJ-1
# CALIBRATION: an unresolved HIGH under approve WARNS, it does not block. Replaying the real
# corpus, one run's HIGH was addressed in a re-fix cycle and the artifact just never set
# `resolved` — blocking on that is a 25% false-blocker rate on real history.
D=$(mkev contra2h)
echo '{"verdict":"approve","findings":[{"severity":"high","title":"x"}]}' > "$D/review.json"
t "approve + unresolved HIGH → warns only" warnings continue 0 -- --evid "$D" --key PROJ-1
has_kind "HIGH flagged as unverifiable-claim" unverifiable-claim -- --evid "$D" --key PROJ-1
# a warning must never mask a real blocker elsewhere
D=$(mkev contra2hb)
echo '{"verdict":"approve","findings":[{"severity":"high","title":"x"}]}' > "$D/review.json"
echo 'null' > "$D/verify.json"
t "warning + real blocker → still blocks" flagged escalate 1 -- --evid "$D" --key PROJ-1
# a RESOLVED critical is fine — that is the normal re-fix cycle completing
D=$(mkev contra2b)
echo '{"verdict":"approve","findings":[{"severity":"critical","title":"x","resolved":true}]}' > "$D/review.json"
t "approve + RESOLVED critical → clean" clean continue 0 -- --evid "$D" --key PROJ-1

D=$(mkev contra3)
echo '{"verdict":"pass","confidence":90,"masking_suspected":true}' > "$D/verify.json"
t "masking suspected + approve → flagged" flagged escalate 1 -- --evid "$D" --key PROJ-1

# ---------- 3. OFF-TOPIC ---------------------------------------------------------------------
D=$(mkev offtopic); echo '{"verdict":"pass","confidence":90,"key":"PROJ-999"}' > "$D/verify.json"
t "artifact names another ticket → flagged" flagged escalate 1 -- --evid "$D" --key PROJ-3000
has_kind "wrong ticket flags off-topic" off-topic -- --evid "$D" --key PROJ-3000
t "same ticket, different case → clean" clean continue 0 -- --evid "$D" --key proj-999

# ---------- 4. LOW CONFIDENCE ------------------------------------------------------------------
D=$(mkev lowconf); echo '{"verdict":"pass","confidence":61}' > "$D/verify.json"
t "confidence below floor → flagged" flagged escalate 1 -- --evid "$D" --key PROJ-1
t "same conf, floor lowered → clean" clean continue 0 -- --evid "$D" --key PROJ-1 --min-confidence 60
# T2 has a higher floor than T1
D=$(mkev t2conf); echo '{"verdict":"pass","confidence":82}' > "$D/verify.json"
t "82 passes at T1"  clean   continue 0 -- --evid "$D" --key PROJ-1 --tier T1
t "82 fails at T2"   flagged escalate 1 -- --evid "$D" --key PROJ-1 --tier T2

# ---------- 5. MALFORMED -----------------------------------------------------------------------
D=$(mkev malformed); echo '{"verdict":' > "$D/verify.json"
t "unparseable JSON → flagged" flagged escalate 1 -- --evid "$D" --key PROJ-1
has_kind "bad JSON flags malformed" malformed -- --evid "$D" --key PROJ-1
D=$(mkev arr); echo '[1,2]' > "$D/verify.json"
t "array not object → flagged" flagged escalate 1 -- --evid "$D" --key PROJ-1
D=$(mkev conftype); echo '{"verdict":"pass","confidence":"high"}' > "$D/verify.json"
t "confidence not a number → flagged" flagged escalate 1 -- --evid "$D" --key PROJ-1

# ---------- judge strength ----------------------------------------------------------------------
D=$(mkev weak); echo '{"verdict":"pass","confidence":90,"model":"sonnet"}' > "$D/verify.json"
t "sonnet judge on opus fix → flagged" flagged escalate 1 -- --evid "$D" --key PROJ-1 --producer-model opus
has_kind "weak judge flagged" weak-judge -- --evid "$D" --key PROJ-1 --producer-model opus
t "opus judge on opus fix → clean"  clean continue 0 -- --evid "$D" --key PROJ-1 --producer-model sonnet
D=$(mkev strong); echo '{"verdict":"pass","confidence":90,"model":"fable"}' > "$D/verify.json"
t "stronger judge is fine" clean continue 0 -- --evid "$D" --key PROJ-1 --producer-model opus

echo "test_check_convergence: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
