#!/usr/bin/env bash
# Regression: the SHARED CROSS-AGENT LOCK (.claude/loops/lib/agent_lock.sh) and
# the two runners that take it — qa-dream's dream.sh and dev-loop-pr-review's pr_review.sh.
#
# WHAT THIS IS THE LAST LINE OF DEFENCE FOR. Both jobs fire at :00. Each used to take only
# its own single-instance lock and neither read the other's, so once a day — and every time
# the host sleeps through both slots, because launchd COALESCES missed calendar intervals
# into ONE event on wake — the two started together: two brain-credential refreshes contending
# on one token cache (it can turn a 1-second pause check into a multi-minute hang), two
# writers on the shared brain, and two headless Claude sessions drawing the same account-wide
# 5-hour rate-limit window. Every property below is
# one that keeps the fix from becoming a NEW failure — because a lock is the one mechanism
# whose bugs are all silent:
#
#   * A HOLDS → B SKIPS, exit 0, with an event that NAMES the holder. A skipped night or hour
#     is never a failure in this system (both queues are durable), but an unexplained one is
#     indistinguishable from a dead loop.
#   * STALE RECLAIM. A SIGKILLed runner runs no trap. Without reclaim the other agent then
#     skips forever and reports exit 0 every time — a dead loop reading as 500 healthy runs.
#   * …BUT A LIVE HOLDER IS NEVER ROBBED. A reclaim that fires on a running agent causes the
#     exact concurrency the lock exists to prevent, which is worse than no lock at all.
#   * TTL BEATS A RECYCLED PID. `kill -0` on a recycled pid says "alive" forever, so liveness
#     alone can wedge the bolt permanently. The age check is what makes that impossible — and
#     it uses the ttl THE HOLDER RECORDED, not the reader's own (a pr-review tick is bounded
#     at ~40 min and a dream is not; reading a dream's lock against pr-review's ceiling would
#     rob a healthy dream at minute 41).
#   * RELEASE IS OWNERSHIP-CHECKED. This repo already has a release-what-you-do-not-own bug
#     (pr_feed.py's `lock --release`); reproducing it here would unlock a RUNNING agent.
#   * THE BYPASS WORKS AND IS LOUD. A human debugging at 02:05 must not be silently skipped.
#   * A WATCHDOG-KILLED HOLDER RELEASES. The kill path is the likeliest way a holder dies.
#
# SAFE BY CONSTRUCTION: fake claude, fake kb.py, fake pr_feed.py, and every lock — both
# per-job ones and the shared one — redirected into a temp dir that is removed on exit. Never
# reaches the live brain, the VCS host's PR board, a token, or the repo's own lock paths. No
# `timeout`/`gtimeout`/`flock`: none of them ships with stock macOS.
#
# SLOW ON PURPOSE, once: proving a watchdog-killed holder RELEASES the shared bolt means
# actually waiting out a watchdog. Everything else is immediate.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
LIB="$REPO/.claude/loops/lib/agent_lock.sh"
DREAM="$REPO/.claude/skills/qa-dream/scripts/dream.sh"
PRR="$REPO/.claude/skills/dev-loop-pr-review/scripts/pr_review.sh"

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); else echo "  [FAIL] $1 — got '$2' want '$3'"; fail=$((fail+1)); fi; }
ckin(){ if grep -qF "$2" "$3" 2>/dev/null; then pass=$((pass+1)); else echo "  [FAIL] $1 — '$2' not in $(basename "$3")"; fail=$((fail+1)); fi; }
ckout(){ if grep -qF "$2" "$3" 2>/dev/null; then echo "  [FAIL] $1 — '$2' unexpectedly present"; fail=$((fail+1)); else pass=$((pass+1)); fi; }

for f in "$LIB" "$DREAM" "$PRR"; do
  [ -f "$f" ] || { echo "  agent_lock: SKIPPED — $f not present"; exit 0; }
done

W="$(mktemp -d -t loops-agent-lock)" || { echo "  agent_lock: SKIPPED (mktemp failed)"; exit 0; }
KILL_LIST="$W/pids"; : > "$KILL_LIST"
cleanup(){
  while read -r p; do [ -n "$p" ] && kill -KILL "-$p" 2>/dev/null; kill -KILL "$p" 2>/dev/null; done < "$KILL_LIST" 2>/dev/null
  rm -rf "$W"
}
trap cleanup EXIT
mkdir -p "$W/bin" "$W/fakeproc" "$W/worktrees"

PRE_DREAMS="$(ls -A "$REPO/.claude/loops/dreams" 2>/dev/null | sort | tr '\n' ' ' | xargs)"

iso_ago(){ python3 -c "
import datetime, sys
d = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=int(sys.argv[1]))
print(d.strftime('%Y-%m-%dT%H:%M:%SZ'))" "$1"; }

# ── fakes ────────────────────────────────────────────────────────────────────
# Healthy brain, never paused, ledger shows rows so no assertion trips.
cat > "$W/bin/kb.py" <<'PY'
import json, sys
a = sys.argv[1:]
def out(d):
    print(json.dumps(d)); sys.exit(0)
if not a: out({"ok": False})
if a[0] == "pause":   out({"ok": True, "paused": False})
if a[0] == "state":   out({"ok": True, "found": False})
if a[0] == "receipt": out({"ok": True, "count": 2, "counts_by_store": {"app_map": 2}})
out({"ok": True})
PY

# pr_feed: the tick lock is always FREE, the queue always empty. Nothing this test asserts
# depends on pr_feed behaviour; it exists so pr_review.sh's own machinery does not error.
cat > "$W/bin/pr_feed.py" <<'PY'
import json, sys
a = sys.argv[1:]
if a and a[0] == "lock":
    if "--release" in a: print(json.dumps({"ok": True, "released": True}))
    else: print(json.dumps({"ok": True, "status": "free", "lock": {}}))
    raise SystemExit(0)
if a and a[0] == "unreviewed":
    print(json.dumps({"ok": True, "candidates": []})); raise SystemExit(0)
print(json.dumps({"ok": True}))
PY

# Every fake claude records that it was CALLED. "did the skipping run spend a model call?"
# is the question a skip test has to answer, and it cannot be answered from an exit code.
cat > "$W/bin/claude-fast" <<'SH'
#!/usr/bin/env bash
echo called >> "${LOOP_TEST_CALLS:-/dev/null}"
echo '{"type":"result","subtype":"success","is_error":false,"total_cost_usd":0.01,"result":"ok"}'
SH
# Holds for a long time so the runner that launched it is demonstrably still holding the
# shared bolt while the other agent tries for it.
cat > "$W/bin/claude-hold" <<'SH'
#!/usr/bin/env bash
echo called >> "${LOOP_TEST_CALLS:-/dev/null}"
echo '{"type":"result","subtype":"success","is_error":false,"total_cost_usd":0.01,"result":"ok"}'
sleep 600
SH
chmod +x "$W/bin/claude-fast" "$W/bin/claude-hold"

# A process whose `ps -o command=` really does contain "dream.sh" — what the liveness check
# matches on. A bare `sleep` would not: the check deliberately refuses to believe a pid whose
# command is something unrelated, which is how a RECYCLED pid is spotted.
#
# `exec -a` is load-bearing, not style, and BOTH halves of it are:
#   * `exec` so this is ONE process. Without it a bash holds a `sleep` child, and killing the
#     pid we recorded orphans the sleeper — which goes on holding this test's stdout pipe for
#     the rest of its sleep. Under run_all.sh that is a suite that prints its result and then
#     hangs for minutes with nothing left to say. (Measured: a 5-minute hang after a 34-second
#     run.)
#   * `-a` so the surviving process still LOOKS like dream.sh to `ps -o command=`. A bare
#     `exec sleep 120` would report "sleep", and the liveness check would correctly call it a
#     recycled pid — the test would then be asserting the opposite of what it means to.
cat > "$W/fakeproc/dream.sh" <<'SH'
#!/usr/bin/env bash
exec -a "dream.sh-test-holder" sleep 120
SH
chmod +x "$W/fakeproc/dream.sh"
# Every fake holder is launched detached from this test's stdio for the same reason.
spawn_holder(){   # spawn_holder → echoes the pid of a live process that looks like dream.sh
  bash "$W/fakeproc/dream.sh" >/dev/null 2>&1 </dev/null &
  local p=$!
  echo "$p" >> "$KILL_LIST"
  echo "$p"
}

export LOOP_AGENT_LOCK_DIR="$W/agent.lock"
export LOOP_DREAM_LOCK_DIR="$W/dream.lock"
export LOOP_PR_REVIEW_LOCK_DIR="$W/pr-review.lock"
export LOOP_PR_REVIEW_STATE="$W/pr-state.json"
export LOOP_PR_LEDGER_FILE="$W/ledger.json"
export LOOP_PR_WORKTREE_ROOT="$W/worktrees"
export LOOP_APP_ROOT="$REPO"          # only needs to BE a git checkout; nothing is fetched
export LOOP_PR_FEED="$W/bin/pr_feed.py"
export KB_BIN="$W/bin/kb.py"
echo '{}' > "$LOOP_PR_LEDGER_FILE"
# A key in the operator's shell would trip pr_review.sh's exit-28 guard before any of this.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN LOOP_PR_REVIEW_ALLOW_API_KEY LOOP_AGENT_LOCK_BYPASS

DREAM_ARGS=(--no-brain --no-assert --since 1d --max-sessions 1)
PR_ARGS=(--no-brain --no-assert --limit 1 --timeout 60 --grace 2)

# Plants a lock by hand, exactly as agent_lock_acquire would have written it.
plant_lock(){   # plant_lock <label> <pid> <proc> <ttl> <started_at>
  rm -rf "$LOOP_AGENT_LOCK_DIR"; mkdir -p "$LOOP_AGENT_LOCK_DIR"
  printf '%s\n' "$2" > "$LOOP_AGENT_LOCK_DIR/pid"
  printf '%s\n' "$1" > "$LOOP_AGENT_LOCK_DIR/label"
  printf '%s\n' "planted-run" > "$LOOP_AGENT_LOCK_DIR/run"
  printf '%s\n' "$3" > "$LOOP_AGENT_LOCK_DIR/proc"
  printf '%s\n' "$4" > "$LOOP_AGENT_LOCK_DIR/ttl"
  printf '%s\n' "$5" > "$LOOP_AGENT_LOCK_DIR/started_at"
}

# ══ 1. THE LIBRARY ITSELF ════════════════════════════════════════════════════
# Direct, deterministic unit checks of the three decisions a lock gets wrong silently.

# ── 1a. a LIVE holder is never robbed ────────────────────────────────────────
LIVE_PID="$(spawn_holder)"
plant_lock "qa-dream" "$LIVE_PID" "dream.sh" 7200 "$(iso_ago 30)"
( . "$LIB"; agent_lock_acquire "pr-review" "r-unit-a" "pr_review.sh" 3315; echo "rc=$?" > "$W/u1.rc"
  printf '%s\n' "$AGENT_LOCK_MSG" > "$W/u1.msg" ) 2>"$W/u1.err"
ck   "a lock held by a LIVE matching pid is refused, not stolen" "$(cat "$W/u1.rc")" "rc=1"
ck   "…and the lock directory is still there" \
     "$([ -d "$LOOP_AGENT_LOCK_DIR" ] && echo present || echo gone)" "present"
ckin "…and the refusal names the holder" "qa-dream" "$W/u1.msg"
ckin "…and names its pid" "pid $LIVE_PID" "$W/u1.msg"

# ── 1b. the TTL beats a live pid (the PID-RECYCLING case) ────────────────────
# Same live pid, same matching command — but the lock is older than the ttl ITS OWN HOLDER
# recorded. Liveness alone would honour this forever; that is the permanent wedge.
plant_lock "qa-dream" "$LIVE_PID" "dream.sh" 60 "$(iso_ago 3600)"
( . "$LIB"; agent_lock_acquire "pr-review" "r-unit-b" "pr_review.sh" 3315; echo "rc=$?" > "$W/u2.rc"
  printf '%s\n' "$AGENT_LOCK_RECLAIMED" > "$W/u2.rec"
  printf '%s\n' "$AGENT_LOCK_HELD" > "$W/u2.held" ) 2>"$W/u2.err"
ck   "a lock past the holder's OWN recorded ttl is reclaimed even with a live pid" "$(cat "$W/u2.rc")" "rc=0"
ck   "…and the reclaimer ends up holding it" "$(cat "$W/u2.held")" "1"
ckin "…and the reason is age, not death" "age_exceeded" "$W/u2.rec"
ckin "…and the reclaim says whose ttl it used" "ttl_s=60" "$W/u2.rec"
rm -rf "$LOOP_AGENT_LOCK_DIR"

# ── 1c. the reader must NOT substitute its own ttl for the holder's ──────────
# A pr-review tick is bounded at ~40 min; a dream is not. The age here is chosen to sit
# BETWEEN the two ceilings — 60 minutes old, under qa-dream's recorded 4530s and over
# pr-review's own 3315s — because that is the only window where the two rules disagree. Read
# against the holder's number it is a healthy dream and must be left alone; read against the
# reader's, the next hourly tick robs it mid-run and both agents go live at once.
plant_lock "qa-dream" "$LIVE_PID" "dream.sh" 4530 "$(iso_ago 3600)"
( . "$LIB"; agent_lock_acquire "pr-review" "r-unit-c" "pr_review.sh" 3315; echo "rc=$?" > "$W/u3.rc" ) 2>"$W/u3.err"
ck "a 60-minute dream is NOT reclaimed by a tick whose own ceiling is 55 min" "$(cat "$W/u3.rc")" "rc=1"

# ── 1d. release never removes a lock this process does not own ───────────────
rm -rf "$LOOP_AGENT_LOCK_DIR"
( . "$LIB"
  agent_lock_acquire "qa-dream" "r-unit-d" "dream.sh" 4530 >/dev/null
  # Somebody else reclaimed it and took it while we held it — the one case where a release
  # would unlock a RUNNING agent.
  printf '%s\n' "999999"        > "$LOOP_AGENT_LOCK_DIR/pid"
  printf '%s\n' "someone-elses" > "$LOOP_AGENT_LOCK_DIR/run"
  agent_lock_release ) 2>"$W/u4.err"
ck   "release refuses a lock that is no longer ours" \
     "$([ -d "$LOOP_AGENT_LOCK_DIR" ] && echo present || echo gone)" "present"
ckin "…and says why" "NOT releasing" "$W/u4.err"
rm -rf "$LOOP_AGENT_LOCK_DIR"

# ── 1e. release when nothing was ever acquired is a silent no-op ─────────────
# The EXIT trap in both runners can fire before any acquire ran.
plant_lock "qa-dream" "$LIVE_PID" "dream.sh" 7200 "$(iso_ago 10)"
( . "$LIB"; agent_lock_release; echo "rc=$?" > "$W/u5.rc" ) 2>"$W/u5.err"
ck "release with nothing held exits 0 and touches nothing" "$(cat "$W/u5.rc")" "rc=0"
ck "…the stranger's lock survives" \
   "$([ -d "$LOOP_AGENT_LOCK_DIR" ] && echo present || echo gone)" "present"
kill "$LIVE_PID" 2>/dev/null; wait "$LIVE_PID" 2>/dev/null
rm -rf "$LOOP_AGENT_LOCK_DIR"

# ══ 2. STALE RECLAIM THROUGH A REAL RUNNER ═══════════════════════════════════
# A dead pid must not wedge the other agent forever, and the reclaim must be LOGGED — that
# line is the only explanation an operator ever gets for a night the bolt was cleared.
( exit 0 ) & DEAD=$!; wait "$DEAD" 2>/dev/null
plant_lock "pr-review" "$DEAD" "pr_review.sh" 3315 "$(iso_ago 30)"
LOOP_TEST_CALLS="$W/s.calls" LOOP_CLAUDE_BIN="$W/bin/claude-fast" \
  bash "$DREAM" "${DREAM_ARGS[@]}" --log "$W/s.log" --out "$W/s.json" >"$W/s.out" 2>"$W/s.err"
ck   "a stale shared lock (dead pid) does not block the other agent" "$?" "0"
# On its own that exit 0 proves nothing: a SKIP is also 0, which is exactly how a wedged
# bolt would pass this test while blocking the agent forever. The next four say which 0.
ckout "…and it is not a skip wearing a clean exit code" "reason=agent_lock_held" "$W/s.log"
ckin "the stale reclaim is announced" "stale shared lock" "$W/s.err"
ckin "the stale reclaim is logged" "event=stale_agent_lock_reclaimed" "$W/s.log"
ckin "…with the dead holder named" "stale_label=pr-review" "$W/s.log"
ck   "…and the run really proceeded to a model call" \
     "$([ -s "$W/s.calls" ] && echo called || echo no)" "called"
ck   "…and released the shared lock on the way out" \
     "$([ -d "$LOOP_AGENT_LOCK_DIR" ] && echo present || echo gone)" "gone"

# ══ 3. A LIVE HOLDER STOPS A REAL RUNNER ═════════════════════════════════════
LIVE_PID="$(spawn_holder)"
plant_lock "qa-dream" "$LIVE_PID" "dream.sh" 4530 "$(iso_ago 20)"
LOOP_TEST_CALLS="$W/h.calls" LOOP_CLAUDE_BIN="$W/bin/claude-fast" \
  bash "$PRR" "${PR_ARGS[@]}" --log "$W/h.log" --report "$W/h.md" >"$W/h.out" 2>"$W/h.err"
ck   "pr-review skips CLEANLY while qa-dream holds the bolt" "$?" "0"
ckin "the skip names the holder" "held by qa-dream" "$W/h.err"
ckin "the skip is logged as its own reason" "reason=agent_lock_held" "$W/h.log"
ckin "…naming the other agent" "holder=qa-dream" "$W/h.log"
ck   "a skipped tick spends NO model call" \
     "$([ -s "$W/h.calls" ] && echo called || echo no)" "no"
ck   "a skipped tick does not steal the bolt" \
     "$([ -d "$LOOP_AGENT_LOCK_DIR" ] && echo present || echo gone)" "present"
ck   "…and the holder's pid is untouched inside it" \
     "$(head -1 "$LOOP_AGENT_LOCK_DIR/pid" 2>/dev/null)" "$LIVE_PID"
# pr-review's consecutive-stuck counter (exit 24) must stay out of this: the contract for
# this skip is "exit 0, never an error", and stale reclaim already bounds how long it lasts.
ckout "the cross-agent skip does not feed the stuck counter" "event=stuck" "$W/h.log"

# ── 3b. the BYPASS runs anyway, loudly, and steals nothing ───────────────────
LOOP_AGENT_LOCK_BYPASS=1 LOOP_TEST_CALLS="$W/by.calls" LOOP_CLAUDE_BIN="$W/bin/claude-fast" \
  bash "$PRR" "${PR_ARGS[@]}" --log "$W/by.log" --report "$W/by.md" >"$W/by.out" 2>"$W/by.err"
ckin "the bypass is announced in capitals" "LOOP_AGENT_LOCK_BYPASS" "$W/by.err"
ckin "the bypass is logged" "event=agent_lock_bypassed" "$W/by.log"
ckin "…with the holder it ran over" "holder=qa-dream" "$W/by.log"
ck   "the bypassed tick really did run" \
     "$([ -s "$W/by.calls" ] && echo called || echo no)" "called"
ck   "…and did NOT release the lock it never owned" \
     "$([ -d "$LOOP_AGENT_LOCK_DIR" ] && echo present || echo gone)" "present"
ck   "…leaving the holder's pid intact" \
     "$(head -1 "$LOOP_AGENT_LOCK_DIR/pid" 2>/dev/null)" "$LIVE_PID"
kill "$LIVE_PID" 2>/dev/null; wait "$LIVE_PID" 2>/dev/null
rm -rf "$LOOP_AGENT_LOCK_DIR"

# ══ 4. TWO REAL RUNNERS, THE ACTUAL COLLISION ════════════════════════════════
# qa-dream holds for real; pr-review must skip. Then the WATCHDOG kills the dream — the
# likeliest way a holder dies — and the bolt must come back on that path, because it is the
# one path where the holder never reaches its own last line.
rm -rf "$LOOP_DREAM_LOCK_DIR" "$LOOP_PR_REVIEW_LOCK_DIR"
LOOP_TEST_CALLS="$W/d.calls" LOOP_CLAUDE_BIN="$W/bin/claude-hold" \
  bash "$DREAM" "${DREAM_ARGS[@]}" --timeout 25 --grace 2 \
  --log "$W/d.log" --out "$W/d.json" >"$W/d.out" 2>"$W/d.err" &
BG=$!; echo "$BG" >> "$KILL_LIST"
i=0; while [ ! -f "$LOOP_AGENT_LOCK_DIR/pid" ] && [ $i -lt 60 ]; do sleep 1; i=$((i+1)); done
ck   "the running dream holds the shared bolt" \
     "$(head -1 "$LOOP_AGENT_LOCK_DIR/pid" 2>/dev/null)" "$BG"
ck   "…and says who it is, for a human reading the directory" \
     "$(head -1 "$LOOP_AGENT_LOCK_DIR/label" 2>/dev/null)" "qa-dream"
ckin "…and records its run id" "dream-" "$LOOP_AGENT_LOCK_DIR/run"
ckin "…and a one-line holder summary" "qa-dream" "$LOOP_AGENT_LOCK_DIR/holder"
# The ttl a dream writes must be ITS OWN worst case, not a shared constant — derived in
# dream.sh from the real bounds (see the TTL block above DREAM_AGENT_TTL there):
#     KB_CALL  = KB_TIMEOUT + 5                        = 180 + 5            = 185
#     per pass = TIMEOUT + GRACE + 4 x KB_CALL + 300   = 25 + 2 + 740 + 300 = 1067
#     ttl      = 2 x (KB_CALL + MAX_PASSES x per pass) = 2 x (185 + 3 x 1067) = 6772
ck   "…and its OWN worst-case ttl" "$(head -1 "$LOOP_AGENT_LOCK_DIR/ttl" 2>/dev/null)" "6772"

LOOP_TEST_CALLS="$W/p.calls" LOOP_CLAUDE_BIN="$W/bin/claude-fast" \
  bash "$PRR" "${PR_ARGS[@]}" --log "$W/p.log" --report "$W/p.md" >"$W/p.out" 2>"$W/p.err"
ck   "pr-review skips while a REAL dream is mid-run" "$?" "0"
ckin "…naming qa-dream as the holder" "held by qa-dream" "$W/p.err"
ckin "…and logging the distinct reason" "reason=agent_lock_held" "$W/p.log"
ck   "…without spending a model call" "$([ -s "$W/p.calls" ] && echo called || echo no)" "no"
ck   "…and without taking its own runner lock away with it" \
     "$([ -d "$LOOP_PR_REVIEW_LOCK_DIR" ] && echo present || echo gone)" "gone"

wait "$BG"; ck "the dream hit its watchdog" "$?" "22"
ck "a WATCHDOG-KILLED holder releases the shared bolt" \
   "$([ -d "$LOOP_AGENT_LOCK_DIR" ] && echo present || echo gone)" "gone"
ck "…and its own per-job lock too" \
   "$([ -d "$LOOP_DREAM_LOCK_DIR" ] && echo present || echo gone)" "gone"

# ══ 5. THE OTHER DIRECTION ═══════════════════════════════════════════════════
# Symmetry is not free: each runner takes the bolt in its own code, so "pr-review blocks
# qa-dream" is a genuinely separate path from "qa-dream blocks pr-review".
rm -rf "$LOOP_AGENT_LOCK_DIR" "$LOOP_DREAM_LOCK_DIR" "$LOOP_PR_REVIEW_LOCK_DIR"
LOOP_TEST_CALLS="$W/q.calls" LOOP_CLAUDE_BIN="$W/bin/claude-hold" \
  bash "$PRR" "${PR_ARGS[@]}" --log "$W/q.log" --report "$W/q.md" >"$W/q.out" 2>"$W/q.err" &
BG2=$!; echo "$BG2" >> "$KILL_LIST"
i=0; while [ ! -f "$LOOP_AGENT_LOCK_DIR/pid" ] && [ $i -lt 60 ]; do sleep 1; i=$((i+1)); done
ck "the running tick holds the shared bolt" \
   "$(head -1 "$LOOP_AGENT_LOCK_DIR/label" 2>/dev/null)" "pr-review"
# timeout 60 + grace 2 + 900
ck "…with the tick's OWN worst-case ttl" "$(head -1 "$LOOP_AGENT_LOCK_DIR/ttl" 2>/dev/null)" "962"

LOOP_TEST_CALLS="$W/e.calls" LOOP_CLAUDE_BIN="$W/bin/claude-fast" \
  bash "$DREAM" "${DREAM_ARGS[@]}" --log "$W/e.log" --out "$W/e.json" >"$W/e.out" 2>"$W/e.err"
ck   "qa-dream skips while a REAL pr-review tick is mid-run" "$?" "0"
ckin "…naming pr-review as the holder" "held by pr-review" "$W/e.err"
ckin "…and logging the distinct reason" "reason=agent_lock_held" "$W/e.log"
ckin "…with the holder in the log line" "holder=pr-review" "$W/e.log"
ck   "…without spending a model call" "$([ -s "$W/e.calls" ] && echo called || echo no)" "no"
ck   "…and without harvesting the window it is not going to consolidate" \
     "$(grep -c 'harvesting' "$W/e.err" 2>/dev/null)" "0"

# A TERM'd holder must release too — pr_review.sh routes signals through on_signal, which is
# a DIFFERENT path from the EXIT trap the watchdog case above exercised.
kill -TERM "$BG2" 2>/dev/null
wait "$BG2" 2>/dev/null
i=0; while [ -d "$LOOP_AGENT_LOCK_DIR" ] && [ $i -lt 20 ]; do sleep 1; i=$((i+1)); done
ck "a TERM'd holder releases the shared bolt (the on_signal path)" \
   "$([ -d "$LOOP_AGENT_LOCK_DIR" ] && echo present || echo gone)" "gone"

# ══ 6. NO RESIDUE ════════════════════════════════════════════════════════════
# A test that dirties the very directory it is testing is worse than no test.
ck "this test left no shared lock in the repo" \
   "$([ -e "$REPO/.claude/loops/agent.lock" ] && echo yes || echo no)" "no"
ck "this test left no dream.lock in the repo" \
   "$([ -e "$REPO/.claude/loops/dream.lock" ] && echo yes || echo no)" "no"
ck "this test left no pr-review.lock in the repo" \
   "$([ -e "$REPO/.claude/loops/pr-review.lock" ] && echo yes || echo no)" "no"
ck "this test added no report to dreams/" \
   "$(ls -A "$REPO/.claude/loops/dreams" 2>/dev/null | sort | tr '\n' ' ' | xargs)" "$PRE_DREAMS"

echo "  agent_lock: $pass passed, $fail failed"
[ "$fail" = 0 ]
