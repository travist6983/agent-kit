#!/usr/bin/env bash
# dream.sh — headless entry point for the qa-dream consolidation pass.
#
#   bash dream.sh [--since 7d] [--project=<slug>] [--dry-run] [--out FILE]
#                 [--max-sessions N] [--max-bytes N] [--no-brain] [--print]
#                 [--timeout S] [--grace S] [--model M] [--log PATH]
#                 [--catch-up] [--max-passes N] [--ticket T]
#                 [--budget-usd N] [--no-strict-mcp] [--no-assert]
#                 [--projects-root P] [--repo R] [--tasks-dir D] [--now TS]
#
# What it does:
#   1. Resolves the claude binary ($LOOP_CLAUDE_BIN, then stack.config `runner.claude_bin`,
#      then PATH — a real executable, never a shell alias or function) and the runner's
#      config dir (`runner.config_dir`, else $CLAUDE_CONFIG_DIR, else ~/.claude).
#   2. Takes TWO locks, in this order. First a single-instance lock so two timers (or a
#      human + the timer) can never dream over each other — a second invocation exits 0 with
#      "already running". Then the SHARED CROSS-AGENT lock (.claude/loops/
#      lib/agent_lock.sh), which every scheduled loop agent (e.g. com.loops.pr-review) takes
#      too, so two of them can never run at the same moment: two brain auth handshakes can
#      contend on one credential cache, two writers would land on the shared brain at once,
#      and two headless sessions draw on the same model-account rate-limit window.
#      Losing either bolt SKIPS the run, exit 0 — nothing waits, and a skipped night costs a
#      delay, never a lesson, because the watermark is durable.
#      LOOP_AGENT_LOCK_BYPASS=1 runs anyway (loudly) for a deliberate hand run.
#   3. Requires the brain to answer before spending a token: `kb pause --status` must
#      return an explicit ok:true (exit 20 if not) and paused:false (exit 0, skip, if on).
#   4. Reads the durable watermark from the brain (`kb state --key dream.watermark`) and
#      uses it as the window start; falls back to --since when the brain has no watermark
#      or is unreachable.
#   5. Runs harvest.py (deterministic, zero-LLM) into a brief.
#   6. Hands the brief to the qa-dream SKILL in a headless Claude session — under a
#      background-and-poll watchdog (TERM, grace, KILL) — which does the consolidation
#      and the brain writes. THE SKILL owns every brain write — this script writes
#      nothing to the brain itself.
#   7. Asserts the pass actually landed rows (`kb receipt`, scoped by LOOP_RUN_ID) or
#      that the report honestly says there was nothing to consolidate. Exit 21 if neither.
#   8. Appends one start line and one end line to the run log.
#
# --dry-run does the watermark read + harvest only: prints the brief, NO lock, NO claude
# call, NO brain writes, NO log line, NO assertion. Safe to run any time, even while a
# real dream is running.
#
# Safe to run twice: the watermark makes the second pass a near no-op.
#
# Exit codes:
#     0  success · dry-run · "already running" (a skipped night is not an error) ·
#        another scheduled loop agent holds the shared lock (likewise not an error) ·
#        paused (R9 kill switch on)
#     2  usage error
#     3  no real `claude` executable found
#     4  harvest failed / mktemp failed
#     5  cannot cd to the repo root
#    20  brain unreachable — `kb pause --status` did not return ok:true (fail loudly;
#        a silent success that writes nothing is the failure mode this removes)
#    21  post-run assertion failed — no ledger rows for this run id AND the report does
#        not say it had nothing to consolidate
#    22  watchdog timeout — the model call was TERMed/KILLed after --timeout seconds
#    23  lock error — could not acquire or reclaim a lock directory (the per-job one or the
#        shared cross-agent one), or the shared-lock library is missing so this run cannot
#        prove the other agent is idle
#    29  Anthropic rate limit (HTTP 429) — the runner's model account is out of its rolling
#        usage window. NOT a fault: nothing was written, the watermark did not move, and the
#        window is still unread for the next pass. Same number, same meaning in pr_review.sh
#     *  anything else is the claude exit code, passed through
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$SKILL_DIR/../../.." && pwd)"

# ── stack.config lookups (one helper, stdlib python, no network) ─────────────
# stack_cfg <dotted.key> [default] — prints the value from .claude/loops/stack.config.json
# (honouring the LOOPS_CFG__ env override), or the default when the file/key is absent.
# Every tunable below is env-first, then config, then a built-in default, so a test or a
# hand run can pin any of them without touching the tracked config.
stack_cfg() {
  python3 - "$REPO" "$1" "${2:-}" <<'PYCFG' 2>/dev/null || printf '%s' "${2:-}"
import json, sys
root, key, default = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    sys.path.insert(0, root)
    from lib.adapters.config import load_config
    v = load_config().get(key, None)
except Exception:
    v = None
if v is None or v == "":
    print(default, end="")
elif isinstance(v, (dict, list)):
    print(json.dumps(v), end="")
elif isinstance(v, bool):
    print("true" if v else "false", end="")
else:
    print(v, end="")
PYCFG
}

# ── tunables (deliberately at the top: change here, not in the body) ──────────
# Model pin: a settings.json change must never silently alter what a nightly run costs.
# $LOOP_DREAM_MODEL, else stack.config `models.strong` — the dream is a judgment pass
# (merge vs. split, cite vs. decline), so it is NOT a place to save tokens.
DREAM_MODEL_DEFAULT="${LOOP_DREAM_MODEL:-$(stack_cfg models.strong opus)}"
TIMEOUT_DEFAULT=1800          # seconds of wall clock for ONE model call
GRACE_DEFAULT=15              # seconds between TERM and KILL
MAX_PASSES_DEFAULT=3          # --catch-up bound
KB_TIMEOUT="${KB_TIMEOUT:-180}"   # seconds for ONE kb CLI call (a remote auth handshake can be slow)
# Tools the dream must never reach for. AskUserQuestion is the load-bearing one: a
# headless run has no human, so an ask is a deadlock, not a question. The rest are
# out-of-scope egress/side-effects (the SKILL declares only Read/Write/Edit/Glob/Grep/Bash).
# Built-in names only: the child runs --strict-mcp-config by default, so no MCP server
# loads at all. If you run --no-strict-mcp, append every write-capable MCP tool your stack
# loads (the vcs_host / ci_cd / issue_tracker servers) via $LOOP_DREAM_DISALLOWED_TOOLS —
# a bypassPermissions session must never be able to open a PR or post a comment.
DISALLOWED_TOOLS_DEFAULT="AskUserQuestion,WebFetch,WebSearch,SendMessage,SendUserFile,PushNotification,CronCreate,CronDelete,CronList,RemoteTrigger,EnterWorktree,ExitWorktree,NotebookEdit"

KB="${KB_BIN:-$REPO/.claude/skills/qa-kb/scripts/kb.py}"
LOOPS_DIR="$REPO/.claude/loops"
DREAMS_DIR="$LOOPS_DIR/dreams"
LOCK_DIR="${LOOP_DREAM_LOCK_DIR:-$LOOPS_DIR/dream.lock}"
# The SHARED cross-agent bolt — one bolt, every scheduled loop agent. Sourced here rather
# than reimplemented, so this runner and pr_review.sh can never drift into two locking
# styles; acquired after the per-job lock below; released in cleanup(). A missing library is
# FATAL on a real run (exit 23): running unserialized is the failure it exists to prevent,
# and it is a tracked file, so absent means broken install, not "optional".
AGENT_LOCK_LIB="${LOOP_AGENT_LOCK_LIB:-$LOOPS_DIR/lib/agent_lock.sh}"
AGENT_LOCK_LIB_OK=0
if [ -r "$AGENT_LOCK_LIB" ]; then
  # shellcheck disable=SC1090
  . "$AGENT_LOCK_LIB" && AGENT_LOCK_LIB_OK=1
fi

SINCE="7d"
DRY=0
PRINT=0
OUT=""
PROJECTS=()
EXTRA=()
NO_BRAIN=0
TIMEOUT="$TIMEOUT_DEFAULT"
GRACE="$GRACE_DEFAULT"
MODEL="$DREAM_MODEL_DEFAULT"
LOG_FILE=""
CATCH_UP=0
MAX_PASSES="$MAX_PASSES_DEFAULT"
NO_ASSERT=0
STRICT_MCP=1
BUDGET_USD=""
DREAM_TICKET="${LOOP_TICKET:-qa-dream}"
DISALLOWED_TOOLS="${LOOP_DREAM_DISALLOWED_TOOLS:-$DISALLOWED_TOOLS_DEFAULT}"

# Prints the header block above, up to (not including) the `set -uo pipefail` line — found
# rather than hard-coded, so adding a line to the header can never silently truncate --help.
usage() {
  local end
  end=$(( $(grep -n '^set -uo pipefail' "${BASH_SOURCE[0]}" | head -1 | cut -d: -f1) - 1 ))
  sed -n "2,${end}p" "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --since) SINCE="${2:-7d}"; shift 2 ;;
    --since=*) SINCE="${1#*=}"; shift ;;
    --project) PROJECTS+=("--project=${2:-}"); shift 2 ;;
    --project=*) PROJECTS+=("--project=${1#*=}"); shift ;;
    --out) OUT="${2:-}"; shift 2 ;;
    --out=*) OUT="${1#*=}"; shift ;;
    --max-sessions|--max-bytes) EXTRA+=("$1" "${2:-}"); shift 2 ;;
    --max-sessions=*|--max-bytes=*) EXTRA+=("$1"); shift ;;
    # Forwarded straight to harvest.py: point the pass at another checkout / projects dir,
    # or pin the clock so a brief is byte-reproducible. Also what lets the test suites drive
    # a real dream.sh against a FIXTURE instead of the operator's live transcripts.
    --projects-root|--repo|--tasks-dir|--now) EXTRA+=("$1" "${2:-}"); shift 2 ;;
    --projects-root=*|--repo=*|--tasks-dir=*|--now=*) EXTRA+=("$1"); shift ;;
    --all-projects|--no-subagents|--compact) EXTRA+=("$1"); shift ;;
    --no-brain) NO_BRAIN=1; EXTRA+=("$1"); shift ;;
    --dry-run|-n) DRY=1; shift ;;
    --print) PRINT=1; shift ;;
    --timeout) TIMEOUT="${2:-}"; shift 2 ;;
    --timeout=*) TIMEOUT="${1#*=}"; shift ;;
    --grace) GRACE="${2:-}"; shift 2 ;;
    --grace=*) GRACE="${1#*=}"; shift ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --model=*) MODEL="${1#*=}"; shift ;;
    --log) LOG_FILE="${2:-}"; shift 2 ;;
    --log=*) LOG_FILE="${1#*=}"; shift ;;
    --catch-up) CATCH_UP=1; shift ;;
    --max-passes) MAX_PASSES="${2:-}"; shift 2 ;;
    --max-passes=*) MAX_PASSES="${1#*=}"; shift ;;
    --ticket) DREAM_TICKET="${2:-}"; shift 2 ;;
    --ticket=*) DREAM_TICKET="${1#*=}"; shift ;;
    --budget-usd) BUDGET_USD="${2:-}"; shift 2 ;;
    --budget-usd=*) BUDGET_USD="${1#*=}"; shift ;;
    --no-strict-mcp) STRICT_MCP=0; shift ;;
    --no-assert) NO_ASSERT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "dream.sh: unknown flag $1" >&2; usage >&2; exit 2 ;;
  esac
done

for n in TIMEOUT GRACE MAX_PASSES; do
  eval "v=\$$n"
  case "$v" in ''|*[!0-9]*) echo "dream.sh: --${n} must be a whole number of seconds/passes (got '$v')" >&2; exit 2 ;; esac
done
[ "$MAX_PASSES" -ge 1 ] || { echo "dream.sh: --max-passes must be >= 1" >&2; exit 2; }
[ -n "$LOG_FILE" ] || LOG_FILE="$LOOPS_DIR/dream.log"

# ── resolve the claude binary: env → stack.config runner.claude_bin → PATH ──────
# A real executable file, never a shell alias or function (those do not survive into a
# script, and a wrapper that only prints a hint would make the whole pass a silent no-op).
resolve_claude() {
  local c cfg_bin
  cfg_bin="$(stack_cfg runner.claude_bin claude)"
  # a bare name in the config means "look it up on PATH"
  case "$cfg_bin" in */*) ;; *) cfg_bin="$(command -v "$cfg_bin" 2>/dev/null || true)" ;; esac
  for c in \
      "${LOOP_CLAUDE_BIN:-}" \
      "$cfg_bin" \
      "$(command -v claude 2>/dev/null || true)" \
      "$HOME/.claude/local/claude" ; do
    [ -n "$c" ] || continue
    if [ -x "$c" ] && [ ! -d "$c" ]; then echo "$c"; return 0; fi
  done
  return 1
}

# The runner's config dir: where Claude Code keeps the transcripts harvest.py reads and the
# settings the headless child loads. Exported so harvest.py derives the same default.
CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$(stack_cfg runner.config_dir "$HOME/.claude")}"
export CLAUDE_CONFIG_DIR
CLAUDE_BIN="$(resolve_claude || true)"
if [ "$DRY" -eq 0 ] && [ -z "$CLAUDE_BIN" ]; then
  echo "dream.sh: FATAL — no executable 'claude' found." >&2
  echo "  Looked at: \$LOOP_CLAUDE_BIN, stack.config runner.claude_bin, \$(command -v claude)," >&2
  echo "  ~/.claude/local/claude. Set LOOP_CLAUDE_BIN=/path/to/claude (or runner.claude_bin)" >&2
  echo "  and retry. An alias or shell function does not count — it needs a real file." >&2
  exit 3
fi

# ── small helpers ────────────────────────────────────────────────────────────
now_iso()   { date -u +%Y-%m-%dT%H:%M:%SZ; }
now_epoch() { date -u +%s; }

log_line() {
  # one appended, timestamped, grep-able line per event
  mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
  printf '%s dream.sh %s\n' "$(now_iso)" "$*" >> "$LOG_FILE" 2>/dev/null || true
}

# Parse the LAST JSON line of a kb response and print `<key>` (kb prints exactly one
# JSON object, but a warning line can precede it). Prints nothing on any failure.
json_key() {
  python3 -c '
import json, sys
key = sys.argv[1]
raw = sys.stdin.read().strip().splitlines()
for line in reversed(raw):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        o = json.loads(line)
    except Exception:
        continue
    if not isinstance(o, dict):
        continue
    v = o.get(key)
    if v is None:
        print("null")
    elif isinstance(v, bool):
        print("true" if v else "false")
    else:
        print(v)
    sys.exit(0)
' "$1" 2>/dev/null
}

# ── watchdog: background + poll + TERM + grace + KILL (portable: needs no `timeout` binary) ──
# Usage: run_watchdog <seconds> <grace> <cmd...>
# Sets WD_TIMED_OUT=0|1 and returns the command's exit code (or 124 on a timeout).
#
# WALL CLOCK, NOT A LOOP COUNT. Counting `sleep 1` iterations is wrong twice over: under a
# background-QoS scheduler (the scheduler definitions run this job niced and low-priority)
# an iteration is noticeably longer than a second because the OS coalesces timers, and the
# count stops entirely while the machine sleeps or the runner is
# SIGSTOPped, while every other clock in the system does not: the shared agent lock's age,
# the ttl this runner writes into it, and the watermark window are all wall clock. A bound
# that drifts from the numbers derived from it is not a bound. So the deadline is an epoch,
# read with `date +%s` each turn — which also means a call that slept through its deadline is
# stopped on wake rather than granted the rest of its budget again.
WD_TIMED_OUT=0
# Snapshot of WD_TIMED_OUT for the ONE call whose verdict has to outlive the call. Everything
# else that shells out goes through run_watchdog too, and run_watchdog zeroes the global on
# entry - so the global is only valid until the next helper runs. Without the snapshot, any
# helper that runs between the model call and the timeout branch clears the flag, and a
# wedged pass reports itself as something else entirely.
TIMED_OUT=0
CHILD_PID=""
run_watchdog() {
  local secs="$1" grace="$2"; shift 2
  local rc=0 deadline grace_end
  WD_TIMED_OUT=0
  # job control ON so the child becomes its own process-group leader and the whole
  # group (claude + everything it spawns) can be signalled together.
  set -m
  "$@" < /dev/null &
  CHILD_PID=$!
  set +m
  deadline=$(( $(date -u +%s) + secs ))
  while kill -0 "$CHILD_PID" 2>/dev/null; do
    if [ "$secs" -gt 0 ] && [ "$(date -u +%s)" -ge "$deadline" ]; then
      WD_TIMED_OUT=1
      echo "dream.sh: WATCHDOG — ${secs}s elapsed, sending TERM to pgid $CHILD_PID" >&2
      kill -TERM "-$CHILD_PID" 2>/dev/null || kill -TERM "$CHILD_PID" 2>/dev/null || true
      grace_end=$(( $(date -u +%s) + grace ))
      while kill -0 "$CHILD_PID" 2>/dev/null && [ "$(date -u +%s)" -lt "$grace_end" ]; do sleep 1; done
      if kill -0 "$CHILD_PID" 2>/dev/null; then
        echo "dream.sh: WATCHDOG — still alive after ${grace}s, sending KILL" >&2
        kill -KILL "-$CHILD_PID" 2>/dev/null || kill -KILL "$CHILD_PID" 2>/dev/null || true
      fi
      wait "$CHILD_PID" 2>/dev/null; rc=$?
      CHILD_PID=""
      return 124
    fi
    sleep 1
  done
  wait "$CHILD_PID" 2>/dev/null; rc=$?
  CHILD_PID=""
  return "$rc"
}

# ── single-instance lock: atomic mkdir + PID inside + stale reclaim ──────────
LOCK_HELD=0
LOCK_MSG=""

lock_owner_alive() {
  local pid="$1" cmd
  kill -0 "$pid" 2>/dev/null || return 1
  cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
  # ps said nothing → cannot tell → assume ALIVE. Never steal a lock on a guess.
  [ -z "$cmd" ] && return 0
  case "$cmd" in *dream.sh*) return 0 ;; esac
  return 1     # the pid was recycled by an unrelated process → the lock is stale
}

acquire_lock() {
  local tries=0 pid ts
  mkdir -p "$(dirname "$LOCK_DIR")" 2>/dev/null || true
  while : ; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      LOCK_HELD=1
      printf '%s\n' "$$"         > "$LOCK_DIR/pid"
      printf '%s\n' "$(now_iso)" > "$LOCK_DIR/started_at"
      printf '%s\n' "$RUN_TAG"   > "$LOCK_DIR/run"
      return 0
    fi
    pid="$(cat "$LOCK_DIR/pid" 2>/dev/null | head -1 | tr -d '[:space:]')"
    ts="$(cat "$LOCK_DIR/started_at" 2>/dev/null | head -1 | tr -d '[:space:]')"
    if [ -z "$pid" ] && [ "$tries" -lt 5 ]; then
      # a racing acquirer may have mkdir'd but not yet written its pid — give it a beat
      tries=$((tries + 1)); sleep 1; continue
    fi
    if [ -n "$pid" ] && lock_owner_alive "$pid"; then
      LOCK_MSG="already running since ${ts:-unknown} (pid $pid, lock $LOCK_DIR)"
      return 1
    fi
    echo "dream.sh: stale lock at $LOCK_DIR (pid ${pid:-none} is not alive) — reclaiming" >&2
    log_line "run_id=$RUN_TAG event=stale_lock_reclaimed dead_pid=${pid:-none} lock=$LOCK_DIR"
    rm -rf "$LOCK_DIR"
    tries=$((tries + 1))
    [ "$tries" -lt 8 ] || { LOCK_MSG="could not acquire or reclaim $LOCK_DIR after $tries tries"; return 2; }
  done
}

release_lock() {
  [ "${LOCK_HELD:-0}" -eq 1 ] || return 0
  local pid
  pid="$(cat "$LOCK_DIR/pid" 2>/dev/null | head -1 | tr -d '[:space:]')"
  # only ever remove a lock this process still owns
  [ "$pid" = "$$" ] && rm -rf "$LOCK_DIR"
  LOCK_HELD=0
}

cleanup() {
  if [ -n "${CHILD_PID:-}" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
    kill -TERM "-$CHILD_PID" 2>/dev/null || kill -TERM "$CHILD_PID" 2>/dev/null || true
  fi
  release_lock
  # Released on the SAME path as the per-job lock — EXIT, INT and TERM all reach here — so a
  # watchdog kill of this runner frees the shared bolt too instead of parking the other agent
  # until the TTL. Ownership-checked inside (never release what we do not own), idempotent,
  # and guarded because a signal can land before the library was ever sourced.
  command -v agent_lock_release >/dev/null 2>&1 && agent_lock_release
  return 0
}
trap 'cleanup' EXIT
trap 'trap - EXIT; echo "dream.sh: interrupted (INT) — releasing lock" >&2; cleanup; exit 130' INT
trap 'trap - EXIT; echo "dream.sh: terminated (TERM) — releasing lock" >&2; cleanup; exit 143' TERM

# ── kb calls, always under the watchdog (a hung brain must not hang the night) ──
kb_run() {   # kb_run <outfile> <args...>  → rc; response body in <outfile>
  local out="$1"; shift
  local _wd="$WD_TIMED_OUT" _rc=0
  run_watchdog "$KB_TIMEOUT" 5 python3 "$KB" "$@" > "$out" 2>&1; _rc=$?
  WD_TIMED_OUT="$_wd"      # a kb call must never overwrite the model call verdict
  return "$_rc"
}

RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_TAG="dream-$RUN_STAMP-$$"
DATE="$(date -u +%Y-%m-%d)"

# ── the honest post-failure state ────────────────────────────────────────────
# A non-zero exit does NOT imply the pass did nothing. The skill advances the
# watermark as its LAST step, so a run killed by the watchdog or the R5 budget
# AFTER it finished consolidating has a correctly-advanced watermark and real
# rows on disk. Asserting "not advanced / rows_written=0" in that case tells the
# operator the window will be re-covered when it will not. A budget or watchdog kill can
# land after the skill's last write, so the runner reads the state back instead of assuming
# it.
failure_truth() {   # $1 = watermark before the call; echoes: "<advanced|unchanged> <rows>"
  local before="$1" after rows
  after="$(read_watermark 2>/dev/null)"
  rows=0
  if [ "$NO_ASSERT" -eq 0 ]; then
    local out; out="$(mktemp -t qa-dream-ftruth)" || out="/tmp/qa-dream-ftruth.$$"
    if kb_run "$out" receipt --ticket "$DREAM_TICKET" --run-id "$RUN_ID" --since 1d 2>/dev/null; then
      rows="$(json_key count < "$out")"; [ -n "$rows" ] || rows=0
    fi
    rm -f "$out"
  fi
  if [ -n "$after" ] && [ "$after" != "$before" ]; then
    echo "advanced $rows $after"
  else
    echo "unchanged $rows $after"
  fi
}

report_failure() {  # $1 = human label for the kill reason, $2 = watermark before
  local t state rows after
  t="$(failure_truth "$2")"
  state="${t%% *}"; rows="$(echo "$t" | awk '{print $2}')"; after="$(echo "$t" | awk '{print $3}')"
  if [ "$state" = "advanced" ]; then
    echo "  BUT the pass had already finished: watermark ADVANCED to $after and $rows ledger row(s) landed." >&2
    echo "  This window will NOT be re-covered. Review $REPORT before re-running." >&2
  elif [ "$rows" -gt 0 ] 2>/dev/null; then
    echo "  Watermark unchanged, but $rows ledger row(s) already landed — the window WILL be re-covered." >&2
    echo "  Re-running is safe (hash/vector dedupe bumps rather than clones); undo with:" >&2
    echo "    kb undo --run-id $RUN_ID          # plan, then add --yes" >&2
  else
    echo "  Watermark unchanged and nothing was written — the next run re-covers this window." >&2
  fi
  FT_ROWS="$rows"
}

# ── window start: the durable watermark, else --since ────────────────────────
# UNDER THE WATCHDOG, like every other kb call here. It runs up to five times under the
# shared agent lock (harvest and WM_BEFORE on every pass, plus failure_truth), against a
# brain whose connect has no timeout of its own and whose auth handshake (whichever kb.config
# auth mode is in use) may have none either. Left unbounded, one hung brain hangs the whole
# pass and the ttl this runner writes into the shared lock stops being a bound on anything.
# kb_run caps it at KB_TIMEOUT like the rest; a timed-out read is "no watermark", which falls
# back to --since exactly as an unreachable brain does.
read_watermark() {
  local wm="" out
  if [ "$NO_BRAIN" -eq 0 ] && [ -f "$KB" ]; then
    out="$(mktemp -t qa-dream-wm 2>/dev/null)" || out="/tmp/qa-dream-wm.$$"
    kb_run "$out" state --key dream.watermark >/dev/null 2>&1
    wm="$(python3 - "$out" <<'PY' 2>/dev/null || true
import json, sys
try:
    lines = open(sys.argv[1]).read().strip().splitlines()
except Exception:
    raise SystemExit(0)
o = None
for line in reversed(lines):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        cand = json.loads(line)
    except Exception:
        continue
    if isinstance(cand, dict):
        o = cand
        break
if not isinstance(o, dict) or not o.get("ok"):
    raise SystemExit(0)
v = o.get("value")
ts = ""
if isinstance(v, dict):
    ts = v.get("ts") or v.get("watermark") or v.get("watermark_to") or ""
elif isinstance(v, str):
    ts = v
if isinstance(ts, str) and ts.strip():
    print(ts.strip())
PY
)"
    rm -f "$out"
  fi
  printf '%s' "$wm"
}

mkdir -p "$DREAMS_DIR"

# ── harvest one window into $1 ───────────────────────────────────────────────
# Sets BRIEF_LINE, EMPTY (EMPTY|NONEMPTY), SESSIONS_REMAINING, CATCH_UP_NEEDED (yes|no),
# CATCH_UP_STALLED (yes|no).
BRIEF_LINE=""; EMPTY="NONEMPTY"; SESSIONS_REMAINING=0; CATCH_UP_NEEDED="no"; CATCH_UP_STALLED="no"
harvest_into() {
  local out="$1" wm summary
  wm="$(read_watermark)"
  local args=()
  if [ -n "$wm" ]; then
    args=(--watermark "$wm")
    echo "dream.sh: watermark from brain = $wm" >&2
  else
    args=(--since "$SINCE")
    echo "dream.sh: no brain watermark — using --since $SINCE" >&2
  fi
  args+=(--out "$out")
  if [ ${#PROJECTS[@]} -gt 0 ]; then args+=("${PROJECTS[@]}"); fi
  if [ ${#EXTRA[@]} -gt 0 ]; then args+=("${EXTRA[@]}"); fi

  echo "dream.sh: harvesting → $out" >&2
  # harvest's own summary goes to stderr: stdout is reserved for the BRIEF, so
  # `dream.sh --dry-run | jq` gets exactly one JSON object.
  if ! python3 "$HERE/harvest.py" "${args[@]}" >&2; then
    echo "dream.sh: FATAL — harvest failed; nothing written, watermark untouched." >&2
    return 1
  fi

  summary="$(python3 - "$out" <<'PY'
import json, sys
o = json.load(open(sys.argv[1]))
c = o.get("candidate_counts", {})
print("sessions=%d tickets=%d candidates=%s window=%s..%s truncated=%s" % (
    o.get("sessions_scanned", 0), o.get("tickets_seen", 0),
    ",".join("%s:%d" % (k, v) for k, v in c.items() if v),
    o.get("watermark_from"), o.get("watermark_to"), o.get("truncated")))
print("EMPTY" if not any(v for k, v in c.items() if k != "metrics") else "NONEMPTY")
# `sessions_remaining`/`catch_up_needed` are the canonical keys; fall back to the
# compatibility alias `sessions_skipped_by_cap` so an older brief still drives the loop.
rem = o.get("sessions_remaining")
if rem is None:
    rem = o.get("sessions_skipped_by_cap") or 0
need = o.get("catch_up_needed")
if need is None:
    need = int(rem or 0) > 0
# harvest says so itself when the cap cannot clear the oldest scanned transcript (a cap of
# 1, or a whole batch sharing one second). Another pass would re-read the identical batch,
# so the catch-up loop must STOP rather than spend a model call per pass on no progress.
stalled = any(str(w).startswith("catch-up cannot advance:") for w in (o.get("warnings") or []))
print("%d %s %s" % (int(rem or 0), "yes" if need else "no", "yes" if stalled else "no"))
PY
)" || return 1
  BRIEF_LINE="$(echo "$summary" | sed -n '1p')"
  EMPTY="$(echo "$summary" | sed -n '2p')"
  SESSIONS_REMAINING="$(echo "$summary" | sed -n '3p' | awk '{print $1}')"
  CATCH_UP_NEEDED="$(echo "$summary" | sed -n '3p' | awk '{print $2}')"
  CATCH_UP_STALLED="$(echo "$summary" | sed -n '3p' | awk '{print $3}')"
  [ -n "$CATCH_UP_STALLED" ] || CATCH_UP_STALLED="no"
  echo "dream.sh: $BRIEF_LINE" >&2
  echo "dream.sh: sessions_remaining=$SESSIONS_REMAINING catch_up_needed=$CATCH_UP_NEEDED" >&2
  return 0
}

# ── temp brief location ──────────────────────────────────────────────────────
OUT_BASE="$OUT"
if [ -z "$OUT_BASE" ]; then
  TMPD="$(mktemp -d -t qa-dream)" || { echo "dream.sh: FATAL — mktemp failed" >&2; exit 4; }
  OUT_BASE="$TMPD/brief-$DATE.json"
fi

# ══ DRY RUN ═════════════════════════════════════════════════════════════════
# Unchanged behaviour: watermark read + harvest + print. No lock, no claude, no brain
# writes, no log line, no assertion. Safe alongside a real run.
if [ "$DRY" -eq 1 ]; then
  harvest_into "$OUT_BASE" || exit 4
  cat "$OUT_BASE"
  echo "dream.sh: --dry-run — no claude call, no brain writes, watermark untouched." >&2
  echo "dream.sh: brief kept at $OUT_BASE" >&2
  exit 0
fi

# ══ REAL RUN ════════════════════════════════════════════════════════════════
# NB: capture the rc explicitly — inside `if ! cmd; then`, `$?` is the negation's 0.
acquire_lock; LOCK_RC=$?
if [ "$LOCK_RC" -ne 0 ]; then
  case "$LOCK_RC" in
    1) echo "dream.sh: $LOCK_MSG — skipping this run." >&2
       log_line "run_id=$RUN_TAG event=skipped reason=already_running lock=$LOCK_DIR"
       exit 0 ;;   # a skipped nightly run is NOT an error
    *) echo "dream.sh: FATAL — $LOCK_MSG" >&2
       log_line "run_id=$RUN_TAG event=fatal reason=lock_error lock=$LOCK_DIR"
       exit 23 ;;
  esac
fi
echo "dream.sh: lock acquired at $LOCK_DIR (pid $$)" >&2

# ── the SHARED cross-agent lock: qa-dream and pr-review never run at once ────
# Taken AFTER the per-job lock, deliberately. The per-job lock is what gives the precise
# "already running" diagnosis for a SECOND DREAM, so keeping it first means this bolt's
# message is only ever about the OTHER agent — and the EXIT trap releases the per-job lock on
# the skip path below. There is no lock-ordering deadlock to worry about: neither bolt ever
# waits, whoever loses skips.
#
# TTL — the worst case THIS invocation could legitimately take, written INTO the lock. The
# lock never reclaims a VERIFIED-live holder at any age (it records this process's start
# time, so a recycled pid is detected exactly — see lib/agent_lock.sh). The ttl decides only
# (a) a lock whose holder `ps` cannot describe and (b) whether a live holder is reported
# OVERDUE — and pr-review counts an overdue skip toward its stuck counter, so a ttl that is
# too SHORT raises a false alarm rather than robbing a live dream. It is therefore derived
# from the real bounds, not guessed:
#     KB_CALL      = KB_TIMEOUT + 5         one kb_run: the watchdog's bound plus its grace
#     per pass     = TIMEOUT + GRACE        the model call — --timeout is PER PASS, not per run
#                  + 4 x KB_CALL            harvest's watermark read, WM_BEFORE, and the receipt
#                                           (or failure_truth's watermark read + receipt)
#                  + 300                    harvest.py and the runner's own python helpers —
#                                           deterministic seconds, the one part not watchdogged
#     bounded      = KB_CALL + MAX_PASSES x per pass       (+ the R9 pause gate up front)
#     ttl          = 2 x bounded            headroom for the unwatched part and a sleep in it
# Every term is wall clock, because run_watchdog measures wall clock. With the defaults
# (--max-passes 3 --timeout 1800 --grace 15, KB_TIMEOUT 180): 2 x (185 + 3 x 2855) = 17500s —
# a ceiling, deliberately far above what a pass normally takes.
DREAM_KB_CALL=$(( KB_TIMEOUT + 5 ))
DREAM_PASS_MAX=$(( TIMEOUT + GRACE + 4 * DREAM_KB_CALL + 300 ))
DREAM_AGENT_TTL=$(( 2 * (DREAM_KB_CALL + MAX_PASSES * DREAM_PASS_MAX) ))
if [ "$AGENT_LOCK_LIB_OK" -ne 1 ]; then
  if [ -n "${LOOP_AGENT_LOCK_BYPASS:-}" ]; then
    echo "dream.sh: *** LOOP_AGENT_LOCK_BYPASS *** — the shared agent lock library is" >&2
    echo "  missing ($AGENT_LOCK_LIB) and the bypass is set, so this run continues" >&2
    echo "  UNSERIALIZED against the other scheduled loop agents (e.g. pr-review)." >&2
    log_line "run_id=$RUN_TAG event=agent_lock_bypassed reason=lib_missing lib=$AGENT_LOCK_LIB"
  else
    echo "dream.sh: FATAL — the shared agent lock library is missing or unreadable:" >&2
    echo "    $AGENT_LOCK_LIB" >&2
    echo "  Without it this run cannot prove another scheduled agent is not already in a model" >&2
    echo "  call, a brain auth handshake, or a brain write. Refusing rather than running two" >&2
    echo "  agents at once. The file is tracked — restore it. To run anyway, say so:" >&2
    echo "    LOOP_AGENT_LOCK_BYPASS=1 $0 ..." >&2
    log_line "run_id=$RUN_TAG event=fatal reason=agent_lock_lib_missing lib=$AGENT_LOCK_LIB"
    exit 23
  fi
else
  agent_lock_acquire "qa-dream" "$RUN_TAG" "dream.sh" "$DREAM_AGENT_TTL"; AGENT_RC=$?
  # A reclaim is logged whether or not the acquire that followed it succeeded: "the other
  # agent's lock was dead and we cleared it" is the line that explains a wedged night after
  # the fact, and it is the one that must never be silent.
  [ -n "${AGENT_LOCK_RECLAIMED:-}" ] && \
    log_line "run_id=$RUN_TAG event=stale_agent_lock_reclaimed ${AGENT_LOCK_RECLAIMED} lock=$AGENT_LOCK_DIR"
  case "$AGENT_RC" in
    0) echo "dream.sh: shared agent lock acquired at $AGENT_LOCK_DIR (ttl ${DREAM_AGENT_TTL}s)" >&2 ;;
    1) if [ -n "${LOOP_AGENT_LOCK_BYPASS:-}" ]; then
         # A human debugging at 02:05 must not be silently skipped. The acquire was still
         # ATTEMPTED, so a hand run on a free bolt still excludes the timer; this branch is
         # only the busy case, and it proceeds WITHOUT the lock (AGENT_LOCK_HELD stays 0, so
         # cleanup will not release someone else's bolt).
         echo "dream.sh: *** LOOP_AGENT_LOCK_BYPASS *** — $AGENT_LOCK_MSG" >&2
         echo "  Running ANYWAY. Both agents are now live at once: expect contention on the" >&2
         echo "  brain credential cache, the shared brain, and the model-account rate-limit window." >&2
         log_line "run_id=$RUN_TAG event=agent_lock_bypassed reason=busy holder=${AGENT_LOCK_HOLDER_LABEL} holder_pid=${AGENT_LOCK_HOLDER_PID} holder_run=${AGENT_LOCK_HOLDER_RUN:-unknown} lock=$AGENT_LOCK_DIR"
       else
         echo "dream.sh: $AGENT_LOCK_MSG — skipping this run." >&2
         echo "  A skipped night is NOT an error: the watermark is durable, so the window is" >&2
         echo "  still unread and the next pass covers it. Nothing waits here on purpose." >&2
         if [ -n "${AGENT_LOCK_OVERDUE:-}" ]; then
           # A LIVE holder past its own recorded worst case. Not reclaimed (that would be the
           # double-run), but not quiet either: pr-review counts the same condition toward its
           # exit-24 stuck counter, and this line is the dream's half of that record.
           echo "  WARNING — that holder is OVERDUE ($AGENT_LOCK_OVERDUE) and still alive. If it" >&2
           echo "  is wedged, stop it by hand: ps -o pid,lstart,command -p ${AGENT_LOCK_HOLDER_PID}" >&2
           log_line "run_id=$RUN_TAG event=skipped reason=agent_lock_held_overdue holder=${AGENT_LOCK_HOLDER_LABEL} holder_pid=${AGENT_LOCK_HOLDER_PID} holder_run=${AGENT_LOCK_HOLDER_RUN:-unknown} holder_since=${AGENT_LOCK_HOLDER_SINCE:-unknown} $AGENT_LOCK_OVERDUE lock=$AGENT_LOCK_DIR"
         else
           log_line "run_id=$RUN_TAG event=skipped reason=agent_lock_held holder=${AGENT_LOCK_HOLDER_LABEL} holder_pid=${AGENT_LOCK_HOLDER_PID} holder_run=${AGENT_LOCK_HOLDER_RUN:-unknown} holder_since=${AGENT_LOCK_HOLDER_SINCE:-unknown} lock=$AGENT_LOCK_DIR"
         fi
         exit 0
       fi ;;
    *) echo "dream.sh: FATAL — $AGENT_LOCK_MSG" >&2
       log_line "run_id=$RUN_TAG event=fatal reason=agent_lock_error lock=$AGENT_LOCK_DIR"
       exit 23 ;;
  esac
fi

cd "$REPO" || { echo "dream.sh: FATAL — cannot cd to $REPO" >&2; exit 5; }

# ── the brain must answer BEFORE we spend a token ────────────────────────────
if [ "$NO_BRAIN" -eq 0 ]; then
  PAUSE_OUT="$(mktemp -t qa-dream-pause)" || PAUSE_OUT="/tmp/qa-dream-pause.$$"
  kb_run "$PAUSE_OUT" pause --status
  P_OK="$(json_key ok      < "$PAUSE_OUT")"
  P_PAUSED="$(json_key paused < "$PAUSE_OUT")"
  if [ "$P_OK" != "true" ]; then
    echo "dream.sh: FATAL — the brain did not answer (kb pause --status ok=${P_OK:-<none>})." >&2
    echo "  A dream that cannot reach the brain writes nothing; failing loudly instead of" >&2
    echo "  reporting a silent success. Response was:" >&2
    sed -n '1,6p' "$PAUSE_OUT" >&2
    rm -f "$PAUSE_OUT"
    log_line "run_id=$RUN_TAG event=fatal reason=brain_unreachable kb=$KB"
    exit 20
  fi
  if [ "$P_PAUSED" != "false" ]; then
    # R9, fail closed: only an explicit paused:false authorises a write pass.
    echo "dream.sh: R9 kill switch is ON (paused=$P_PAUSED) — writing nothing, exiting clean." >&2
    rm -f "$PAUSE_OUT"
    log_line "run_id=$RUN_TAG event=skipped reason=paused paused=$P_PAUSED"
    exit 0
  fi
  rm -f "$PAUSE_OUT"
  echo "dream.sh: brain reachable, paused=false — proceeding." >&2
else
  echo "dream.sh: --no-brain — skipping the brain health gate AND the post-run assertion." >&2
  NO_ASSERT=1
fi

# ── budget cap (R5): read the decided number, don't invent one ───────────────
# The limits file is stack.config `limits_file` (default .claude/loops/loop-limits.json),
# resolved against this repo when relative.
LIMITS_FILE="$(stack_cfg limits_file .claude/loops/loop-limits.json)"
case "$LIMITS_FILE" in /*) ;; *) LIMITS_FILE="$REPO/$LIMITS_FILE" ;; esac
if [ -z "$BUDGET_USD" ]; then
  BUDGET_USD="$(python3 - "$LIMITS_FILE" <<'PY' 2>/dev/null || echo 0
import json, sys
try:
    print(float(json.load(open(sys.argv[1]))["r5_budget"]["per_loop_usd_cap"]))
except Exception:
    print(0)
PY
)"
fi
case "$BUDGET_USD" in ''|*[!0-9.]*) BUDGET_USD=0 ;; esac

RC=0
PASS=0
TOTAL_ROWS=0
LAST_REPORT=""

while : ; do
  PASS=$((PASS + 1))
  RUN_ID="${RUN_TAG}-p${PASS}"
  REPORT="$DREAMS_DIR/${DATE}-${RUN_ID}.md"
  if [ "$PASS" -eq 1 ]; then OUT="$OUT_BASE"; else OUT="${OUT_BASE%.json}-p${PASS}.json"; fi
  START_EPOCH="$(now_epoch)"

  echo "dream.sh: ── pass $PASS/$MAX_PASSES · run_id=$RUN_ID ──" >&2
  log_line "run_id=$RUN_ID pass=$PASS event=start since=$SINCE model=$MODEL timeout=${TIMEOUT}s report=$REPORT"

  if ! harvest_into "$OUT"; then
    log_line "run_id=$RUN_ID pass=$PASS event=end rc=4 reason=harvest_failed"
    RC=4; break
  fi

  if [ "$PRINT" -eq 1 ]; then cat "$OUT"; fi

  if [ "$EMPTY" = "EMPTY" ]; then
    echo "dream.sh: brief has no candidates — nothing to consolidate." >&2
    echo "dream.sh: the skill still runs so the watermark + report are written honestly." >&2
  fi

  # ── hand the brief to the skill, headless ──────────────────────────────────
  PROMPT="Invoke the qa-dream skill for this consolidation pass.

BRIEF_FILE=$OUT
DREAM_DATE=$DATE
DREAM_RUN_ID=$RUN_ID
REPORT=$REPORT

The brief is already harvested (deterministic, zero-LLM) — do NOT re-run harvest.py.
Follow .claude/skills/qa-dream/SKILL.md exactly, in order: kb pause --status first
(fail closed), then kb outbox --replay and kb reembed, then consolidate, then write
app_map/fix_lessons/flakiness/metrics with --source qa-dream, PROPOSE expectations
(never bless), append skill defects to .claude/loops/skill-followups.md,
write the dated report to exactly \$REPORT above, and advance kb state --key
dream.watermark ONLY on a clean pass.

LOOP_RUN_ID=$RUN_ID and LOOP_TICKET=$DREAM_TICKET are already exported, so every kb
write is ledgered against this run — do NOT pass --run-id yourself.
The runner ASSERTS afterwards that this run landed at least one ledger row. If there was
genuinely nothing to consolidate, the report MUST say so in words — a 'Status:' line of
'empty-window' / 'paused' / 'brain-unreachable', or the phrase 'nothing to consolidate'.
An empty report with no rows written is treated as a FAILED run."

  echo "dream.sh: invoking $CLAUDE_BIN (CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR, model=$MODEL)" >&2
  echo "dream.sh: run_id=$RUN_ID  report=$REPORT" >&2
  echo "dream.sh: disallowedTools=$DISALLOWED_TOOLS" >&2

  # --output-format json is what makes a 429 VISIBLE here. In text mode the harness prints a
  # prose error and exits non-zero, which lands in the generic "claude exited $rc" bucket and
  # reads as a broken dream; the JSON result object carries `api_error_status` as an INTEGER,
  # the one unforgeable tell that the account is simply out of its rolling window. Nothing is
  # lost from the transcript: stdout is captured to be PARSED and then written straight back
  # out to the same stream, and this job never had its product on stdout anyway - that is the
  # dated report under dreams/.
  CLAUDE_ARGS=(-p "$PROMPT" --permission-mode bypassPermissions --model "$MODEL"
               --output-format json --disallowedTools "$DISALLOWED_TOOLS" --no-chrome)
  [ "$STRICT_MCP" -eq 1 ] && CLAUDE_ARGS+=(--strict-mcp-config)
  if [ "$(python3 -c "print(1 if float('${BUDGET_USD:-0}') > 0 else 0)" 2>/dev/null || echo 0)" = "1" ]; then
    CLAUDE_ARGS+=(--max-budget-usd "$BUDGET_USD")
    echo "dream.sh: R5 budget cap = \$$BUDGET_USD ($(basename "$LIMITS_FILE"))" >&2
  fi

  WM_BEFORE="$(read_watermark 2>/dev/null)"
  SESSION_OUT="$(mktemp -t qa-dream-session)" || SESSION_OUT="/tmp/qa-dream-session.$$"
  CLAUDE_CONFIG_DIR="$CLAUDE_CONFIG_DIR" LOOP_RUN_ID="$RUN_ID" LOOP_TICKET="$DREAM_TICKET" \
    run_watchdog "$TIMEOUT" "$GRACE" "$CLAUDE_BIN" "${CLAUDE_ARGS[@]}" > "$SESSION_OUT"
  rc=$?
  TIMED_OUT="$WD_TIMED_OUT"     # snapshot NOW: every later helper resets the global
  DUR=$(( $(now_epoch) - START_EPOCH ))
  cat "$SESSION_OUT"            # captured to be parsed, never swallowed

  # ── out of window, not broken: a model-account 429 ends the night quietly ────
  # Same tell, same wording and the SAME EXIT CODE as pr_review.sh, on purpose: the two
  # runners share one exit-code table. Otherwise a 429 is an ordinary non-zero exit, and a
  # night this job loses the rate-limit window to a sibling agent reads as a brain/dream
  # FAULT - the most misleading direction available. Nothing is retried: the
  # watermark is not advanced, so the unread sessions are simply still unread next pass.
  #
  # NARROW ON PURPOSE. The primary tell is the harness own integer field. The secondary
  # exists only because the error-variant result omits that key, and it demands the model
  # vendor marker (the harness writes "anthropic" / "rate_limit_error") beside the 429 -
  # never a bare rate-limit word, because the embedding provider behind qa-kb (kb.config
  # `embedding.provider`) throws its own 429 when ITS quota is exhausted. Reading THAT as
  # "the model account is out of window" would turn a dead embedder into a clean bill of
  # health. Never matched against `result`, which is the model own prose.
  #
  # NO APOSTROPHES IN THE PYTHON BELOW: bash 3.2 re-lexes a heredoc body inside $( ... ), and
  # a lone quote in a comment opens a shell string that never closes.
  RATE_LIMIT_TELL="$(python3 - "$SESSION_OUT" <<'PYX'
import json, re, sys
try:
    raw = open(sys.argv[1]).read()
except Exception:
    raise SystemExit(0)
cands = []
for line in raw.strip().splitlines():
    line = line.strip()
    if line.startswith("{"):
        try:
            cands.append(json.loads(line))
        except Exception:
            pass
try:
    cands.append(json.loads(raw))
except Exception:
    pass
FOUR29 = re.compile(r"(?:^|[^0-9])429(?:[^0-9]|$)")
for o in reversed(cands):
    if not isinstance(o, dict) or o.get("type") != "result":
        continue
    # PRIMARY - structured, unforgeable, exact.
    if o.get("is_error") is True and o.get("api_error_status") == 429:
        print("api_error_status=429")
        break
    # SECONDARY - the error variant carries no api_error_status; require a 429 AND the
    # model-vendor marker in the same harness-written string.
    if o.get("subtype") == "error_during_execution":
        hit = ""
        for e in (o.get("errors") or []):
            t = str(e).lower()
            if FOUR29.search(t) and ("anthropic" in t or "rate_limit_error" in t):
                hit = "errors_text"
                break
        if hit:
            print(hit)
            break
    break        # only the LAST result object carries the verdict for this pass
PYX
)"
  rm -f "$SESSION_OUT"

  if [ "$TIMED_OUT" -eq 1 ]; then
    echo "dream.sh: timed_out:true — the model call exceeded ${TIMEOUT}s and was killed." >&2
    FT_ROWS=0; report_failure "watchdog" "$WM_BEFORE"
    log_line "run_id=$RUN_ID pass=$PASS event=end rc=22 duration_s=$DUR rows_written=${FT_ROWS:-0} timed_out=true report=$REPORT"
    RC=22; break
  fi

  if [ -n "$RATE_LIMIT_TELL" ]; then
    echo "dream.sh: ANTHROPIC RATE LIMIT (HTTP 429) - the runner's model account is out of its" >&2
    echo "  rolling usage window. THIS IS NOT A FAULT and the brain is fine: nothing was" >&2
    echo "  consolidated, the watermark was NOT advanced, and this window is still unread" >&2
    echo "  for the next pass. Detected via $RATE_LIMIT_TELL in the harness result (claude exited $rc)." >&2
    echo "  The window is SHARED with the operator's own interactive usage AND with the other" >&2
    echo "  scheduled loop agents (e.g. pr-review), which can be in a model call at the same" >&2
    echo "  moment. If this repeats, the lever is the schedule (the scheduler definitions), not a retry." >&2
    FT_ROWS=0; report_failure "Anthropic rate limit (HTTP 429) - out of window, not a fault" "$WM_BEFORE"
    # `source=anthropic` / `outcome=anthropic-rate-limited` are the SAME tokens pr_review.sh
    # logs: one exit-code table, one grep. (Claude Code is the runner by design.)
    log_line "run_id=$RUN_ID pass=$PASS event=rate_limited source=anthropic tell=$RATE_LIMIT_TELL claude_rc=$rc"
    log_line "run_id=$RUN_ID pass=$PASS event=end rc=29 duration_s=$DUR rows_written=0 timed_out=false outcome=anthropic-rate-limited report=$REPORT"
    RC=29; break
  fi

  if [ $rc -ne 0 ]; then
    echo "dream.sh: claude exited $rc (R5 budget kill looks like: 'Exceeded USD budget')." >&2
    FT_ROWS=0; report_failure "exit $rc" "$WM_BEFORE"
    log_line "run_id=$RUN_ID pass=$PASS event=end rc=$rc duration_s=$DUR rows_written=${FT_ROWS:-0} timed_out=false report=$REPORT"
    RC=$rc; break
  fi

  # ── post-run assertion: did this run actually land anything? ───────────────
  ROWS=0
  if [ "$NO_ASSERT" -eq 0 ]; then
    RECEIPT_OUT="$(mktemp -t qa-dream-receipt)" || RECEIPT_OUT="/tmp/qa-dream-receipt.$$"
    kb_run "$RECEIPT_OUT" receipt --ticket "$DREAM_TICKET" --run-id "$RUN_ID" --since 1d
    R_OK="$(json_key ok    < "$RECEIPT_OUT")"
    R_N="$(json_key  count < "$RECEIPT_OUT")"
    case "$R_N" in ''|*[!0-9]*) R_N=0 ;; esac
    ROWS="$R_N"
    HONEST=0
    if [ -f "$REPORT" ] && grep -qiE 'nothing to consolidate|empty-window|Status:[^|]*(paused|brain-unreachable)' "$REPORT"; then
      HONEST=1
    fi
    if [ "$R_OK" != "true" ]; then
      echo "dream.sh: WARNING — could not read the write ledger (kb receipt ok=${R_OK:-<none>});" >&2
      echo "  treating the assertion as unproven, not as a pass." >&2
    fi
    if [ "$ROWS" -eq 0 ] && [ "$HONEST" -eq 0 ]; then
      echo "dream.sh: FATAL — run $RUN_ID wrote 0 ledger rows and $REPORT does not say it" >&2
      echo "  had nothing to consolidate. A silent success that writes nothing is the" >&2
      echo "  failure this gate exists to catch. Check with:" >&2
      echo "    python3 $KB receipt --ticket $DREAM_TICKET --run-id $RUN_ID --since 1d" >&2
      rm -f "$RECEIPT_OUT"
      log_line "run_id=$RUN_ID pass=$PASS event=end rc=21 duration_s=$DUR rows_written=0 timed_out=false report=$REPORT reason=no_rows_written"
      RC=21; break
    fi
    rm -f "$RECEIPT_OUT"
    if [ "$ROWS" -eq 0 ]; then
      echo "dream.sh: 0 rows — the report says so honestly (empty window / paused). OK." >&2
    else
      echo "dream.sh: assertion OK — $ROWS ledger rows for run_id=$RUN_ID." >&2
    fi
  fi

  TOTAL_ROWS=$((TOTAL_ROWS + ROWS))
  LAST_REPORT="$REPORT"
  log_line "run_id=$RUN_ID pass=$PASS event=end rc=0 duration_s=$DUR rows_written=$ROWS timed_out=false report=$REPORT sessions_remaining=$SESSIONS_REMAINING catch_up_needed=$CATCH_UP_NEEDED"

  # ── drain the backlog? ─────────────────────────────────────────────────────
  if [ "$CATCH_UP" -ne 1 ]; then
    if [ "$CATCH_UP_NEEDED" = "yes" ]; then
      echo "dream.sh: $SESSIONS_REMAINING session(s) still unread — re-run with --catch-up to drain them." >&2
    fi
    break
  fi
  if [ "$CATCH_UP_NEEDED" != "yes" ]; then
    echo "dream.sh: catch-up complete — no sessions remaining." >&2
    break
  fi
  if [ "$CATCH_UP_STALLED" = "yes" ]; then
    # harvest reported "catch-up cannot advance": the watermark does not clear the oldest
    # transcript this pass scanned, so pass N+1 would re-read the identical batch. Looping
    # would spend a full model call per remaining pass to consolidate the same material and
    # move the backlog by zero. Stop and name the fix instead of billing for a no-op.
    echo "dream.sh: catch-up STALLED — the watermark cannot clear this batch, so another" >&2
    echo "  pass would re-read it and drain nothing. $SESSIONS_REMAINING session(s) still" >&2
    echo "  unread. Raise --max-sessions (currently capped low, or a whole batch of" >&2
    echo "  transcripts shares one second). Stopping after pass $PASS rather than spending" >&2
    echo "  a model call per pass on no progress." >&2
    log_line "run_id=$RUN_TAG event=catch_up_stalled passes=$PASS sessions_remaining=$SESSIONS_REMAINING"
    break
  fi
  if [ "$PASS" -ge "$MAX_PASSES" ]; then
    echo "dream.sh: --max-passes $MAX_PASSES reached with $SESSIONS_REMAINING session(s) still" >&2
    echo "  unread. Not an error — the next scheduled run continues the catch-up." >&2
    log_line "run_id=$RUN_TAG event=catch_up_bounded passes=$PASS sessions_remaining=$SESSIONS_REMAINING"
    break
  fi
  echo "dream.sh: $SESSIONS_REMAINING session(s) remaining — starting catch-up pass $((PASS + 1))." >&2
done

if [ "$RC" -eq 0 ]; then
  echo "dream.sh: done — $PASS pass(es), $TOTAL_ROWS ledger row(s), report ${LAST_REPORT:-<none>}" >&2
fi
exit "$RC"
