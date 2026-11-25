#!/usr/bin/env bash
# Regression: the PR-review timing gate. A --live tick is refused (exit 25,
# reason=timing_unmeasured) until pr_review_loop.timing records a measurement by a named
# person with enough samples; a dry-run tick is never refused for it; measure_ticks.py
# proposes prs_per_tick from pr-review.log and writes nothing.
#
# SAFE BY CONSTRUCTION: every path is redirected into a temp dir; the --live cases stop at the
# preconditions (refusal) or at a runner lock this test already holds (skip), so no model
# call, no pr_feed, no brain and no VCS host is ever reached.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
PRR="$REPO/.claude/skills/dev-loop-pr-review/scripts/pr_review.sh"
MT="$REPO/.claude/skills/dev-loop-pr-review/scripts/measure_ticks.py"
pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); else echo "  [FAIL] $1 — got '$2' want '$3'"; fail=$((fail+1)); fi; }
ckin(){ if grep -qF "$2" "$3" 2>/dev/null; then pass=$((pass+1)); else echo "  [FAIL] $1 — '$2' not in $(basename "$3")"; fail=$((fail+1)); fi; }
ckout(){ if grep -qF "$2" "$3" 2>/dev/null; then echo "  [FAIL] $1 — '$2' unexpectedly present"; fail=$((fail+1)); else pass=$((pass+1)); fi; }

W="$(mktemp -d "${TMPDIR:-/tmp}/pr-review-timing.XXXXXX")" || { echo "  pr_review_timing: SKIPPED (mktemp failed)"; exit 0; }
HOLDER=""
trap '[ -n "$HOLDER" ] && kill "$HOLDER" 2>/dev/null; rm -rf "$W"' EXIT
mkdir -p "$W/bin"
printf '#!/bin/sh\necho "fake claude must never run" >&2\nexit 99\n' > "$W/bin/claude"; chmod +x "$W/bin/claude"
printf 'import sys\nprint("fake pr_feed must never run", file=sys.stderr)\nsys.exit(99)\n' > "$W/bin/pr_feed.py"
export LOOP_CLAUDE_BIN="$W/bin/claude" LOOP_PR_FEED="$W/bin/pr_feed.py" KB_BIN="$W/bin/pr_feed.py"
export LOOP_PR_REVIEW_LOCK_DIR="$W/pr-review.lock" LOOP_AGENT_LOCK_DIR="$W/agent.lock"
export LOOP_PR_REVIEW_STATE="$W/state.json" LOOP_PR_LEDGER_FILE="$W/ledger.json"
export LOOP_PR_WORKTREE_ROOT="$W/worktrees" LOOP_APP_ROOT="$REPO"
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN LOOP_PR_REVIEW_ALLOW_API_KEY
echo '{}' > "$LOOP_PR_LEDGER_FILE"

limits(){   # limits <out> <timing-json> [min_samples]
  python3 - "$REPO/.claude/loops/loop-limits.json" "$1" "$2" "${3:-5}" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["pr_review_loop"]["timing"] = json.loads(sys.argv[3])
d["pr_review_loop"]["timing_min_samples"] = json.loads(sys.argv[4])
json.dump(d, open(sys.argv[2], "w"))
PY
}
# The runner treats a lock as live only if its pid runs something named pr_review.sh, so the
# holder is a sleep started under that name; it is killed on exit.
bash -c 'exec -a pr_review.sh sleep 300' & HOLDER=$!
hold_lock(){ rm -rf "$LOOP_PR_REVIEW_LOCK_DIR"; mkdir -p "$LOOP_PR_REVIEW_LOCK_DIR"
  echo "$HOLDER" > "$LOOP_PR_REVIEW_LOCK_DIR/pid"; date -u +%Y-%m-%dT%H:%M:%SZ > "$LOOP_PR_REVIEW_LOCK_DIR/started_at"; }
live(){   # live <tag> <limits-file>
  hold_lock
  LOOPS_CFG__LIMITS_FILE="$2" bash "$PRR" --live --no-brain --no-assert --timeout 60 --grace 2 \
    --log "$W/$1.log" --report "$W/$1.md" >"$W/$1.out" 2>"$W/$1.err"; echo $?; }

# ── the shipped file: unmeasured → refused ──
RC="$(live shipped "$REPO/.claude/loops/loop-limits.json")"
ck   "the shipped limits refuse a live tick" "$RC" "25"
ckin "…with reason=timing_unmeasured" "reason=timing_unmeasured" "$W/shipped.log"

# ── each way of being unmeasured fails closed ──
for case in \
  'noby|{"measured": true, "measured_by": null, "samples": 9}' \
  'blankby|{"measured": true, "measured_by": "  ", "samples": 9}' \
  'few|{"measured": true, "measured_by": "Pat", "samples": 4}' \
  'strtrue|{"measured": "true", "measured_by": "Pat", "samples": 9}' \
  'strsamples|{"measured": true, "measured_by": "Pat", "samples": "9"}' \
  'notdict|true'; do
  tag="${case%%|*}"; limits "$W/$tag.json" "${case#*|}"
  RC="$(live "$tag" "$W/$tag.json")"
  ck   "$tag: refused" "$RC" "25"
  ckin "$tag: reason=timing_unmeasured" "reason=timing_unmeasured" "$W/$tag.log"
done
echo '{"pr_review_loop": {' > "$W/broken.json"
RC="$(live broken "$W/broken.json")"
ck   "unreadable limits: refused" "$RC" "25"
limits "$W/badmin.json" '{"measured": true, "measured_by": "Pat", "samples": 9}' 0
RC="$(live badmin "$W/badmin.json")"
ck   "timing_min_samples 0 is invalid: refused" "$RC" "25"

# ── measured → passes the gate (stops at the lock this test holds) ──
limits "$W/ok.json" '{"measured": true, "measured_by": "Pat", "samples": 5, "p90_duration_s": 1500}'
RC="$(live ok "$W/ok.json")"
ck    "measured: not refused" "$RC" "0"
ckout "measured: no timing refusal" "reason=timing_unmeasured" "$W/ok.log"
ckin  "measured: reached the runner lock" "already running" "$W/ok.err"
ckout "the fake claude never ran" "fake claude must never run" "$W/ok.err"

# ── a dry-run tick is not gated ──
rm -rf "$LOOP_PR_REVIEW_LOCK_DIR"
LOOPS_CFG__LIMITS_FILE="$REPO/.claude/loops/loop-limits.json" bash "$PRR" --dry-run --no-brain \
  >"$W/dry.out" 2>"$W/dry.err"
ckout "dry-run: no timing refusal" "timing is" "$W/dry.err"

# ── measure_ticks.py ──
cat > "$W/pr-review.log" <<'LOG'
ts=1 run_id=a event=end rc=0 duration_s=1800 outcome=reviewed marked=2 model_call=true
ts=2 run_id=b event=end rc=0 duration_s=900 outcome=reviewed marked=1
ts=3 run_id=c event=end rc=0 duration_s=1000 outcome=reviewed marked=1
ts=4 run_id=d event=end rc=22 duration_s=2400 outcome=watchdog-kill timed_out=true marked=0
ts=5 run_id=e event=end rc=0 duration_s=3 outcome=empty-queue marked=0 model_call=false
ts=6 run_id=f event=end rc=0 duration_s=1200 outcome=reviewed marked=1
ts=7 run_id=g event=refused reason=timing_unmeasured
LOG
OUT="$(python3 "$MT" --log "$W/pr-review.log" --limits "$REPO/.claude/loops/loop-limits.json")"; RC=$?
ck "measure: exit 0" "$RC" "0"
val(){ echo "$OUT" | python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }
ck "measure: kills, empty and refused ticks are not samples" "$(val samples)" "4"
ck "measure: not enough samples yet" "$(val enough)" "False"
ck "measure: p90 per PR" "$(val p90_per_pr_s)" "1200.0"
ck "measure: proposes (2400-600)//1200 = 1" "$(val proposed_prs_per_tick)" "1"
printf 'ts=1 event=end duration_s=500 outcome=reviewed marked=0\n' > "$W/none.log"
python3 "$MT" --log "$W/none.log" >/dev/null; ck "measure: zero usable ticks exits 2" "$?" "2"
python3 "$MT" --log "$W/missing.log" >/dev/null; ck "measure: missing log exits 2" "$?" "2"

echo "  pr_review_timing: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
