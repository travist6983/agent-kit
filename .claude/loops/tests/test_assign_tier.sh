#!/usr/bin/env bash
# Regression: dev-intake's tier + model assignment.
#
# The rules are CALIBRATED against a corpus of real runs, so this suite replays a calibration
# corpus of that shape and asserts the tier each case would have received. That is the point: a
# tiering rule nobody scored against history is a guess with a table around it.
#
# Calibration corpus (case ids are local to this suite):
#   case-a  1 file / 1 line FE                    -> T0
#   case-b  2 files (1 prod + 1 test), BE only    -> T0   (the reference case)
#   case-c  5 files FE, shared stylesheet         -> T1   (correctly NOT T0)
#   case-d  3 files, FE+BE                        -> T1
#   case-e/f/g  features                          -> T2
#   case-h  escalated, 75 min                     -> T1 on first look (the known miss)
#
# No DB, no network, no model calls.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
T="$SK/dev-intake/scripts/assign_tier.py"
pass=0; fail=0

# tier <desc> <want_tier> -- <args...>
tier(){ local desc="$1" want="$2"; shift 3
  local got; got=$(python3 "$T" "$@" 2>&1 | python3 -c "import sys,json;print(json.load(sys.stdin)['tier'])" 2>/dev/null)
  if [ "$got" = "$want" ]; then pass=$((pass+1)); else
    echo "  [FAIL] $desc → $got (want $want)"; fail=$((fail+1)); fi; }

field(){ local desc="$1" expr="$2" want="$3"; shift 4   # 3 vars + the literal --
  local got; got=$(python3 "$T" "$@" 2>&1 | python3 -c "import sys,json;d=json.load(sys.stdin);print($expr)" 2>/dev/null)
  if [ "$got" = "$want" ]; then pass=$((pass+1)); else
    echo "  [FAIL] $desc → $got (want $want)"; fail=$((fail+1)); fi; }

# ---------- replay of the calibration corpus --------------------------------------------------
tier "case-a (1 file, 1 line, FE)"    T0 -- --issue-type Bug --layer FE --files 1 --scenarios 1
tier "case-b (2 files, BE only)"      T0 -- --issue-type Bug --layer BE --files 2 --scenarios 1
tier "case-c (5 files, shared styles)" T1 -- --issue-type Bug --layer FE --files 5 --scenarios 2
tier "case-d (3 files, FE+BE)"        T1 -- --issue-type Bug --layer both --files 3 --scenarios 2
tier "case-e (feature)"               T2 -- --issue-type Story --layer both --files 6 --scenarios 3
tier "case-f (feature, migration)"    T2 -- --issue-type Story --layer both --files 12 --scenarios 4
tier "case-g (feature, FE)"           T2 -- --issue-type Story --layer FE --files 5 --scenarios 2
# the known miss: nothing in the ticket predicted the 75-minute escalation
tier "case-h first look (the miss)"   T1 -- --issue-type Bug --layer FE --files 4 --scenarios 2
# ...but a SECOND run of the same ticket must never repeat it
tier "case-h with a prior attempt"    T2 -- --issue-type Bug --layer FE --files 4 --scenarios 2 --prior-attempts 1

# ---------- T0 requires ALL of its conditions ------------------------------------------------
tier "4 files breaks T0"        T1 -- --issue-type Bug --layer BE --files 4 --scenarios 1
tier "3 scenarios breaks T0"    T1 -- --issue-type Bug --layer BE --files 2 --scenarios 3
tier "2 pages breaks T0"        T1 -- --issue-type Bug --layer BE --files 2 --scenarios 1 --pages 2
tier "layer=both breaks T0"     T1 -- --issue-type Bug --layer both --files 2 --scenarios 1
tier "flaky area breaks T0"     T1 -- --issue-type Bug --layer BE --files 2 --scenarios 1 --flake-rate 0.42
tier "clean area keeps T0"      T0 -- --issue-type Bug --layer BE --files 2 --scenarios 1 --flake-rate 0.10

# ---------- sensitive areas can NEVER be T0 ----------------------------------------------------
tier "billing is T2 regardless of size" T2 -- --issue-type Bug --layer BE --files 1 --scenarios 1 --risk billing
tier "auth is T2"                        T2 -- --issue-type Bug --layer FE --files 1 --scenarios 1 --risk auth
tier "payments is T2"                    T2 -- --issue-type Bug --layer BE --files 1 --scenarios 1 --risk payments
tier "shared-core is T2"                 T2 -- --issue-type Bug --layer FE --files 2 --scenarios 1 --shared-core
tier "migration is T2"                   T2 -- --issue-type Bug --layer BE --files 2 --scenarios 1 --migration
tier "multi-role is T2"                  T2 -- --issue-type Bug --layer FE --files 2 --scenarios 1 --roles 3

# ---------- unsure moves UP, never down -----------------------------------------------------------
tier "low confidence lifts T0→T1"   T1 -- --issue-type Bug --layer BE --files 2 --scenarios 1 --confidence 65
tier "very low confidence → T2"     T2 -- --issue-type Bug --layer BE --files 2 --scenarios 1 --confidence 40
tier "high confidence keeps T0"     T0 -- --issue-type Bug --layer BE --files 2 --scenarios 1 --confidence 95

# ---------- unknown/missing facts must not yield T0 --------------------------------------------------
tier "no file estimate → not T0"    T1 -- --issue-type Bug --layer BE --scenarios 1
tier "no scenarios → not T0"        T1 -- --issue-type Bug --layer BE --files 2
tier "unknown layer → not T0"       T1 -- --issue-type Bug --files 2 --scenarios 1

# ---------- the model plan -----------------------------------------------------------------------------
field "T0 puts the FIX on opus" "d['models']['fix']" opus -- --issue-type Bug --layer BE --files 2 --scenarios 1
field "T0 judge >= producer"    "d['models']['verify']" opus -- --issue-type Bug --layer BE --files 2 --scenarios 1
field "T0 review on opus"       "d['models']['review']" opus -- --issue-type Bug --layer BE --files 2 --scenarios 1
field "T0 intake is cheap"      "d['models']['intake']" sonnet -- --issue-type Bug --layer BE --files 2 --scenarios 1
field "T0 repro is cheap"       "d['models']['repro']" sonnet -- --issue-type Bug --layer BE --files 2 --scenarios 1
field "T1 repro upgrades"       "d['models']['repro']" opus -- --issue-type Bug --layer FE --files 5 --scenarios 2

# ---------- caps follow the tier -------------------------------------------------------------------------
field "T0 repro cap is 1"   "d['retry_caps']['repro']" 1 -- --issue-type Bug --layer BE --files 2 --scenarios 1
field "T0 verify cap is 1"  "d['retry_caps']['verify']" 1 -- --issue-type Bug --layer BE --files 2 --scenarios 1
field "T0 fix cap is 2"     "d['retry_caps']['fix']" 2 -- --issue-type Bug --layer BE --files 2 --scenarios 1
field "T2 fix cap is 3"     "d['retry_caps']['fix']" 3 -- --issue-type Story --files 9 --scenarios 5
field "T0 target is 8-12m"  "d['target_wall_clock_minutes']" "[8, 12]" -- --issue-type Bug --layer BE --files 2 --scenarios 1

echo "test_assign_tier: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
