#!/usr/bin/env bash
# Regression: dream.sh — the unattended-execution contract (spec FIX 4) plus the
# catch-up stall stop that wires harvest's FIX 3 signal into the runner.
#
# What this is the last line of defence for: the dream runs unattended, in the small hours,
# with nobody watching. (Its actual slot is in the plist — do not restate a clock time here.)
# Every property here is one that turns a silent bad night into a loud, bounded, reversible
# one — a second copy never dreams over the first, a hung model call cannot run till morning,
# an unreachable brain fails loudly instead of "succeeding" while writing nothing, and a
# catch-up that cannot advance stops instead of billing a model call per pass for no progress.
#
# SAFE BY CONSTRUCTION: never touches the live brain (a fake kb.py via $KB_BIN) and never
# invokes claude (fake binaries via $LOOP_CLAUDE_BIN). All scratch lives in a temp dir that
# is removed on exit; the lock is redirected out of the repo with $LOOP_DREAM_LOCK_DIR.
# No `timeout`/`gtimeout`/`flock` — none of them ships with stock macOS.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
DREAM="$REPO/.claude/skills/qa-dream/scripts/dream.sh"
FIX="$HERE/fixtures/dream-catchup"

pass=0; fail=0

# Pre-test snapshot. The residue checks below must prove THIS TEST left nothing —
# not that the repo has never run a dream. Real passes legitimately create
# dream.log and dated reports under dreams/, and once qa-dream actually ran on
# 2026-09-09 the old absolute assertions started failing on a healthy repo.
PRE_LOG="$([ -e "$REPO/.claude/loops/dream.log" ] && echo yes || echo no)"
PRE_DREAMS="$(ls -A "$REPO/.claude/loops/dreams" 2>/dev/null | sort | tr '\n' ' ' | xargs)"
ck(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); else echo "  [FAIL] $1 — got '$2' want '$3'"; fail=$((fail+1)); fi; }
ckin(){ if grep -qiF "$2" "$3" 2>/dev/null; then pass=$((pass+1)); else echo "  [FAIL] $1 — '$2' not in $(basename "$3")"; fail=$((fail+1)); fi; }
ckout(){ if grep -qiF "$2" "$3" 2>/dev/null; then echo "  [FAIL] $1 — '$2' unexpectedly present"; fail=$((fail+1)); else pass=$((pass+1)); fi; }

W="$(mktemp -d -t qa-dream-runner)" || { echo "  dream_runner: SKIPPED (mktemp failed)"; exit 0; }
cleanup(){ rm -rf "$W"; }
trap cleanup EXIT
mkdir -p "$W/bin"
export LOOP_DREAM_LOCK_DIR="$W/dream.lock"
# dream.sh now takes a SECOND bolt as well — the shared cross-agent lock com.loops.pr-review
# also takes (lib/agent_lock.sh). Redirect it for the same reason as the one above: left at
# its default this suite would contend with a REAL nightly dream or hourly tick, and every
# assertion below would then be measuring a skip instead of a run.
export LOOP_AGENT_LOCK_DIR="$W/agent.lock"

# ── fakes ────────────────────────────────────────────────────────────────────
# Healthy brain: not paused, no watermark, ledger shows rows so the assertion passes.
cat > "$W/bin/kb-ok.py" <<'PY'
import sys, json
a = sys.argv[1:]
def out(d):
    print(json.dumps(d)); sys.exit(0)
if not a: out({"ok": False})
if a[0] == "pause":   out({"ok": True, "paused": False})
if a[0] == "state":   out({"ok": True, "found": False})
if a[0] == "receipt": out({"ok": True, "count": 4, "counts_by_store": {"app_map": 4}})
out({"ok": True})
PY
# Unreachable brain — the exit-20 gate.
cat > "$W/bin/kb-dead.py" <<'PY'
import json
print(json.dumps({"ok": False, "error": "connection refused"}))
PY
# R9 kill switch ON.
cat > "$W/bin/kb-paused.py" <<'PY'
import sys, json
a = sys.argv[1:]
if a and a[0] == "pause": print(json.dumps({"ok": True, "paused": True}))
else: print(json.dumps({"ok": True, "found": False}))
PY
# Ledger shows ZERO rows — the exit-21 assertion.
cat > "$W/bin/kb-norows.py" <<'PY'
import sys, json
a = sys.argv[1:]
def out(d):
    print(json.dumps(d)); sys.exit(0)
if not a: out({"ok": False})
if a[0] == "pause":   out({"ok": True, "paused": False})
if a[0] == "state":   out({"ok": True, "found": False})
if a[0] == "receipt": out({"ok": True, "count": 0, "counts_by_store": {}})
out({"ok": True})
PY

cat > "$W/bin/claude-fast" <<'SH'
#!/usr/bin/env bash
sleep 1
echo '{"result":"ok"}'
SH
# Slow AND forks a child: what proves the watchdog signals the process GROUP, not just the pid.
cat > "$W/bin/claude-slow" <<SH
#!/usr/bin/env bash
( sleep 600 ) &
echo \$! > "$W/grandchild.pid"
sleep 600
SH
# A 429 from ANTHROPIC: the structured, unforgeable field on the result object. "Out of the
# rolling window" is not a fault and must not be reported as one — before this, it landed in
# the generic "claude exited $rc" bucket and a starved night read as a broken brain.
cat > "$W/bin/claude-429" <<'SH'
#!/usr/bin/env bash
echo '{"type":"result","subtype":"success","is_error":true,"api_error_status":429,"terminal_reason":"api_error","total_cost_usd":0}'
exit 1
SH
# A 429 from OPENAI — the qa-kb embedder behind this very job, running out of credit. It must
# NOT be read as the Anthropic subscription window: that would turn a dead embedder into a
# clean bill of health.
cat > "$W/bin/claude-oa429" <<'SH'
#!/usr/bin/env bash
echo '{"type":"result","subtype":"error_during_execution","is_error":true,"errors":["kb write failed: openai 429 insufficient_quota rate_limit_exceeded"]}'
exit 1
SH
chmod +x "$W/bin/claude-fast" "$W/bin/claude-slow" "$W/bin/claude-429" "$W/bin/claude-oa429"

BASE=(--no-brain --since 1d --max-sessions 1)

# ══ LOCK ═════════════════════════════════════════════════════════════════════
# A skipped nightly is NOT an error (exit 0): the alternative is a cron mailstorm that
# trains you to ignore it. But it must be loud enough to explain itself, and it must not
# harvest — two dreams consolidating the same window is exactly the double-write we ban.
LOOP_CLAUDE_BIN="$W/bin/claude-slow" bash "$DREAM" "${BASE[@]}" \
  --timeout 25 --grace 2 --log "$W/a.log" --out "$W/a.json" >"$W/a.out" 2>"$W/a.err" &
BG=$!
i=0; while [ ! -f "$LOOP_DREAM_LOCK_DIR/pid" ] && [ $i -lt 60 ]; do sleep 1; i=$((i+1)); done
ck "lock dir holds the running pid" "$(cat "$LOOP_DREAM_LOCK_DIR/pid" 2>/dev/null)" "$BG"

LOOP_CLAUDE_BIN="$W/bin/claude-fast" bash "$DREAM" "${BASE[@]}" \
  --log "$W/b.log" --out "$W/b.json" >"$W/b.out" 2>"$W/b.err"
ck  "2nd concurrent invocation exits 0 (a skipped night is not an error)" "$?" "0"
ckin "2nd says 'already running since'" "already running since" "$W/b.err"
ckout "2nd did NOT harvest" "harvesting" "$W/b.err"
ckin "the skip is logged" "reason=already_running" "$W/b.log"

wait "$BG"; ck "1st run hit the watchdog" "$?" "22"
ck "lock released on exit" "$([ -d "$LOOP_DREAM_LOCK_DIR" ] && echo present || echo gone)" "gone"

# ── stale lock: a dead pid must be reclaimed, not honoured forever ────────────
mkdir -p "$LOOP_DREAM_LOCK_DIR"
( exit 0 ) & DEAD=$!; wait $DEAD 2>/dev/null
echo "$DEAD" > "$LOOP_DREAM_LOCK_DIR/pid"
LOOP_CLAUDE_BIN="$W/bin/claude-fast" bash "$DREAM" "${BASE[@]}" \
  --log "$W/c.log" --out "$W/c.json" >"$W/c.out" 2>"$W/c.err"
ck   "stale lock (dead pid) does not block the run" "$?" "0"
ckin "stale lock is reported" "is not alive" "$W/c.err"
ckin "stale reclaim is logged" "event=stale_lock_reclaimed" "$W/c.log"

# ══ WATCHDOG ═════════════════════════════════════════════════════════════════
rm -rf "$LOOP_DREAM_LOCK_DIR" "$W/grandchild.pid"
S=$(date +%s)
LOOP_CLAUDE_BIN="$W/bin/claude-slow" bash "$DREAM" "${BASE[@]}" \
  --timeout 2 --grace 2 --log "$W/d.log" --out "$W/d.json" >"$W/d.out" 2>"$W/d.err"
RC=$?; ELAPSED=$(( $(date +%s) - S ))
ck   "watchdog exit code" "$RC" "22"
ck   "watchdog actually bounded the run (<30s, not 600)" "$([ "$ELAPSED" -lt 30 ] && echo yes || echo no)" "yes"
ckin "watchdog announces the TERM" "WATCHDOG" "$W/d.err"
ckin "timeout is logged" "timed_out=true" "$W/d.log"
GC="$(cat "$W/grandchild.pid" 2>/dev/null)"
if [ -n "$GC" ]; then
  sleep 1
  ck "the forked grandchild died too (process-GROUP kill)" \
     "$(kill -0 "$GC" 2>/dev/null && echo alive || echo dead)" "dead"
else
  echo "  [FAIL] grandchild pid was never recorded — the pgid check did not run"; fail=$((fail+1))
fi
ck "lock released after a timeout" "$([ -d "$LOOP_DREAM_LOCK_DIR" ] && echo present || echo gone)" "gone"

# ══ RATE LIMIT — out of window, not a fault ══════════════════════════════════
rm -rf "$LOOP_DREAM_LOCK_DIR"
LOOP_CLAUDE_BIN="$W/bin/claude-429" bash "$DREAM" "${BASE[@]}" --no-assert \
  --log "$W/r.log" --out "$W/r.json" >"$W/r.out" 2>"$W/r.err"
ck   "an Anthropic 429 exits 29, not the passthrough claude rc" "$?" "29"
ckin "it is named as a rate limit, not a fault" "ANTHROPIC RATE LIMIT" "$W/r.err"
ckin "the outcome is logged" "outcome=anthropic-rate-limited" "$W/r.log"
ckin "the tell is named" "tell=api_error_status=429" "$W/r.log"
ckout "it is NOT logged as a watchdog timeout" "timed_out=true" "$W/r.log"

rm -rf "$LOOP_DREAM_LOCK_DIR"
LOOP_CLAUDE_BIN="$W/bin/claude-oa429" bash "$DREAM" "${BASE[@]}" --no-assert \
  --log "$W/s.log" --out "$W/s.json" >"$W/s.out" 2>"$W/s.err"
RC=$?
ckout "a qa-kb OpenAI 429 is NOT the Anthropic window" "outcome=anthropic-rate-limited" "$W/s.log"
ck    "…it stays a plain non-zero exit" "$RC" "1"

# ══ BRAIN GATES ══════════════════════════════════════════════════════════════
# These run WITHOUT --no-brain (that flag skips the gate and the assertion by design).
GATE=(--since 1d --max-sessions 1)

rm -rf "$LOOP_DREAM_LOCK_DIR"
KB_BIN="$W/bin/kb-dead.py" LOOP_CLAUDE_BIN="$W/bin/claude-fast" \
  bash "$DREAM" "${GATE[@]}" --log "$W/e.log" --out "$W/e.json" >"$W/e.out" 2>"$W/e.err"
ck   "unreachable brain exits 20, not a silent success" "$?" "20"
ckout "no model call was made on a dead brain" "fake" "$W/e.out"
ckin "the dead brain is logged" "reason=brain_unreachable" "$W/e.log"

rm -rf "$LOOP_DREAM_LOCK_DIR"
KB_BIN="$W/bin/kb-paused.py" LOOP_CLAUDE_BIN="$W/bin/claude-slow" \
  bash "$DREAM" "${GATE[@]}" --log "$W/f.log" --out "$W/f.json" >"$W/f.out" 2>"$W/f.err"
ck   "R9 pause exits 0 and writes nothing" "$?" "0"
ckin "R9 pause is announced" "kill switch" "$W/f.err"
ckin "the pause is logged" "reason=paused" "$W/f.log"

rm -rf "$LOOP_DREAM_LOCK_DIR"
KB_BIN="$W/bin/kb-norows.py" LOOP_CLAUDE_BIN="$W/bin/claude-fast" \
  bash "$DREAM" "${GATE[@]}" --log "$W/g.log" --out "$W/g.json" >"$W/g.out" 2>"$W/g.err"
ck "a pass that wrote 0 rows and did not say why exits 21" "$?" "21"

# ══ CATCH-UP STALL (FIX 3 signal → FIX 4 loop) ═══════════════════════════════
# harvest warns "catch-up cannot advance" when the watermark cannot clear the oldest
# transcript the pass scanned (a cap of 1, or a whole batch inside one second). Looping
# there re-reads the identical batch: --max-passes model calls to drain zero sessions.
# Driven against the FIXTURE — never the operator's real transcripts.
if [ -d "$FIX" ]; then
  CU="$W/cu"; mkdir -p "$CU"; cp -R "$FIX/projects" "$FIX/repo" "$CU/"
  python3 - "$CU" <<'PY'
import os, sys, datetime
w = sys.argv[1]
def ep(h, m=0):
    return datetime.datetime(2026, 3, 15, h, m, 0, tzinfo=datetime.timezone.utc).timestamp()
PINS = {"sess-catchup-01.jsonl": ep(9),  "sess-catchup-02.jsonl": ep(10),
        "sess-catchup-03.jsonl": ep(11), "sess-catchup-04.jsonl": ep(12),
        "sess-catchup-05.jsonl": ep(13), "task-metadata.json":    ep(13, 30)}
for dp, _, names in os.walk(w):
    for n in names:
        if n in PINS:
            t = PINS[n]; os.utime(os.path.join(dp, n), (t, t))
PY
  HARV=(--projects-root "$CU/projects" --repo /tmp/loops-catchup-repo
        --tasks-dir "$CU/repo/runs" --all-projects --now 2026-03-16T00:00:00Z)

  rm -rf "$LOOP_DREAM_LOCK_DIR"
  LOOP_CLAUDE_BIN="$W/bin/claude-fast" bash "$DREAM" --no-brain --no-assert \
    --catch-up --max-passes 3 --max-sessions 1 --since 30d \
    --log "$W/h.log" --out "$W/h.json" "${HARV[@]}" >"$W/h.out" 2>"$W/h.err"
  ck   "a stalled catch-up still exits 0" "$?" "0"
  ck   "a stalled catch-up stops after ONE pass, not --max-passes" \
       "$(grep -c 'event=start' "$W/h.log")" "1"
  ckin "the stall is explained" "catch-up STALLED" "$W/h.err"
  ckin "the stall is logged" "event=catch_up_stalled" "$W/h.log"

  # Negative control: a HEALTHY catch-up must not be misread as stalled. (The fake kb never
  # persists a watermark — the SKILL writes it — so this bounds at --max-passes instead of
  # draining. The property under test is only that it is not flagged stalled.)
  rm -rf "$LOOP_DREAM_LOCK_DIR"
  LOOP_CLAUDE_BIN="$W/bin/claude-fast" bash "$DREAM" --no-brain --no-assert \
    --catch-up --max-passes 2 --max-sessions 2 --since 30d \
    --log "$W/i.log" --out "$W/i.json" "${HARV[@]}" >"$W/i.out" 2>"$W/i.err"
  ck    "a healthy catch-up is NOT flagged stalled" "$(grep -c 'catch_up_stalled' "$W/i.log")" "0"
  ck    "a healthy catch-up keeps going" "$(grep -c 'event=start' "$W/i.log")" "2"
else
  echo "  [FAIL] fixtures/dream-catchup missing — the stall checks did not run"; fail=$((fail+1))
fi

# ══ NO RESIDUE ═══════════════════════════════════════════════════════════════
# A test that dirties the brain dir it is testing is worse than no test.
ck "this test left no dream.lock behind" "$([ -e "$REPO/.claude/loops/dream.lock" ] && echo yes || echo no)" "no"
ck "this test never touched the shared cross-agent lock" \
   "$([ -e "$REPO/.claude/loops/agent.lock" ] && echo yes || echo no)" "no"
ck "this test did not create the repo dream.log" \
   "$([ -e "$REPO/.claude/loops/dream.log" ] && echo yes || echo no)" "$PRE_LOG"
ck "this test added no report to dreams/" \
   "$(ls -A "$REPO/.claude/loops/dreams" 2>/dev/null | sort | tr '\n' ' ' | xargs)" "$PRE_DREAMS"

echo "  dream_runner: $pass passed, $fail failed"
[ "$fail" = 0 ]
