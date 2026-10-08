#!/usr/bin/env bash
# agent_lock.sh — THE SHARED CROSS-AGENT LOCK. One bolt, every scheduled loop agent.
#
# WHY THIS EXISTS. Each scheduled runner (com.loops.qa-dream, com.loops.pr-review, and any
# runner added later) takes its OWN single-instance lock, and none of them reads the others'.
# Two jobs scheduled on the same minute — or on different minutes on a laptop, because
# launchd COALESCES missed calendar intervals into one event on wake, so a machine asleep
# through both slots fires both at the same instant — therefore start together, and then:
#     * when the brain's auth mode mints a CLI token (kb.config `auth.mode` token_command),
#       two mints contend on one credential cache and a 1-second pause check
#       becomes a multi-minute hang — inside a watchdogged tick, that is a tick that does nothing;
#     * two writers land on the shared brain at once;
#     * two headless Claude sessions draw on the same account-wide rate-limit window — the
#       window that is ALSO the operator's own interactive budget under the subscription
#       posture, and the one thing two concurrent agents are guaranteed to compete for.
# A different minute is not a fix that survives a wake. A lock is.
#
# THE SHAPE IS DELIBERATELY THE ONE ALREADY IN THE RUNNERS — atomic `mkdir`, the pid written
# inside, stale reclaim by PID liveness. Copied on purpose: a second, different locking style
# in the same system is a second set of edge cases to learn. Read dream.sh's acquire_lock or
# pr_review.sh's and most of this will not surprise you. The differences, each forced by the
# lock being SHARED rather than per-job (or by a race the per-job locks never had to face):
#
#   1. LIVENESS MUST RECOGNISE SOMEONE ELSE'S PROCESS. Each runner's own check matches its
#      own script name (`case "$cmd" in *dream.sh*)`). Here the holder is usually the OTHER
#      script, so the lock RECORDS the holder's process name and the check matches THAT.
#   2. LIVENESS IS IDENTITY, NOT AGE. The holder also records its process START TIME, so a
#      reader can tell "that pid is still our holder" from "that pid was recycled" exactly —
#      see "WHO COUNTS AS ALIVE" below.
#   3. THE TTL IS THE HOLDER'S, NOT THE READER'S. The holder writes its OWN worst-case
#      lifetime into `ttl`. A reader uses it only where identity cannot be proven, and to call
#      a live holder OVERDUE — never its own ceiling (pr_review.sh's --timeout is right for a
#      tick and wrong for a dream, which legitimately runs far longer).
#   4. NOBODY WAITS. Every caller either gets the lock or SKIPS ITS RUN, exit 0. Every queue
#      is durable (the dream watermark; the PR seen-ledger keyed on id + head commit), so a
#      skipped night or hour costs a delay and never a lesson. Waiting would just move the
#      overlap later and hold a second `claude` process open while it waited.
#   5. RECLAIMERS ARE SERIALIZED — see below. Two agents reaching one dead lock at the same
#      moment is the NORMAL case for this lock (one launchd wake fires both), not an edge.
#
# STALE RECLAIM IS NOT OPTIONAL and is the half that actually bites. A runner SIGKILLed by
# the OS, by `launchctl kill -9`, or by a watchdog that outlived its own parent leaves this
# directory behind with no trap ever running. Without reclaim, the other agent then skips
# every run forever and reports exit 0 each time — a dead loop reading as a healthy one, the
# exact failure mode the rest of this system is built to make impossible.
#
# WHO COUNTS AS ALIVE. The holder records its pid AND its process start time (`ps -o
# lstart=`, pinned to LC_ALL=C TZ=UTC so a launchd job and a shell agree on the string). A
# reader classifies the recorded holder:
#     * pid not alive                                    → DEAD      → reclaim now
#     * pid alive, command is not the recorded runner    → RECYCLED  → reclaim now
#     * pid alive, command matches, start time differs   → RECYCLED  → reclaim now
#     * pid alive, command matches, start time matches   → ALIVE     → never reclaimed
#     * `ps` says nothing at all                         → UNKNOWN   → reclaim only past the ttl
#     * no start time on record (an older build, or an acquire killed half-way)
#                                                        → the old rule: reclaim past the ttl
# A VERIFIED-LIVE HOLDER IS NEVER ROBBED, AT ANY AGE. An age-based rule ("older than its ttl →
# reclaim regardless of what ps says") robs a healthy long run: the runners' watchdogs count
# `sleep 1` iterations (measurably slow under ProcessType=Background + Nice), lock age keeps
# counting while the machine sleeps and the runner does not, and some brain reads run with no
# bound at all — so a healthy dream past its own number would be robbed by the next hourly
# tick, which is the double-run this file exists to prevent. The start time detects pid
# RECYCLING exactly, which was the only job the age rule ever had. A live holder past its ttl
# is reported as OVERDUE ($AGENT_LOCK_OVERDUE) and the reader still skips; pr_review.sh counts
# that skip toward its exit-24 stuck counter, so a genuinely wedged holder becomes loud within
# a few hours instead of being silently robbed.
#
# RECLAIMERS ARE SERIALIZED. Deciding "stale" is a read, a python age computation and a `ps`
# — tens of milliseconds — and a bare `rm -rf` after it is a race: two agents arriving
# together at one dead lock BOTH judge it stale; the slower one's `rm -rf` lands after the
# faster one's `mkdir` and deletes a freshly acquired lock, and both return 0 (measured, at a
# stagger of a few tens of milliseconds, this loses most of the time). So a reclaim runs under
# a kernel flock on "$AGENT_LOCK_DIR.reclaim" — a plain file beside the lock; the kernel drops
# an flock when its holder dies, even on SIGKILL, so the mutex itself can never go stale —
# and removes the directory ONLY IF its pid, run id and start stamp are still exactly the ones
# that were judged stale. The loser of the race finds a different holder, re-reads it, and
# skips like anyone else.
#
# OWNERSHIP ON RELEASE. A process only ever removes a lock it can PROVE is its own — the pid
# inside must be this process AND the run id must be the one we wrote. A release with no
# ownership check is a known bug class (pr_feed.py's `lock --release` still has it), and
# reproducing it here would be worse: the only thing between a release-what-you-do-not-own
# bug and two live agents is that the reclaiming process usually happens to be the owner.
#
# USAGE
#     . "$LOOPS_DIR/lib/agent_lock.sh"
#     agent_lock_acquire <label> <run-id> <process-name> <ttl-seconds>   # 0 held · 1 busy · 2 error
#     agent_lock_release                                                 # ownership-checked, idempotent
#   On 1, $AGENT_LOCK_MSG says who is in there and $AGENT_LOCK_HOLDER_{LABEL,PID,RUN,SINCE,AGE}
#   carry the fields for a log line; $AGENT_LOCK_OVERDUE is non-empty ("age_s=… ttl_s=…") when
#   that holder is verifiably alive but past the worst case it recorded. On a reclaim THIS
#   process actually performed, $AGENT_LOCK_RECLAIMED describes it ("dead_pid …" /
#   "recycled_pid …" / "age_exceeded …") so the CALLER logs it in its own format — this file
#   deliberately writes no run log of its own and calls nothing the caller has to have defined.
#
# ENV
#   LOOP_AGENT_LOCK_DIR          override the lock path (tests point it out of the repo)
#   LOOP_AGENT_LOCK_BYPASS=1     handled by the CALLERS, not here: they log loudly and carry
#                                 on when the bolt is busy. Acquiring is still attempted, so a
#                                 deliberate hand run still excludes the timer when it can.
#   LOOP_AGENT_LOCK_TTL_FALLBACK the ttl used only for a lock that records none (see below)
#   LOOP_AGENT_LOCK_KNOWN_PROCS  extra runner script names (space-separated) that count as a
#                                 real holder when a lock records no `proc`; a new scheduled
#                                 runner registers itself here (see scheduler/JOB-CONTRACT.md)

# The bolt itself. Default: beside the loops directory's other runtime state, so one `ls`
# shows an operator every lock in the system. Derived from this file's own location — a
# second hard-coded copy of the loops dir is a second thing to go stale.
AGENT_LOCK_DIR="${LOOP_AGENT_LOCK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)/agent.lock}"

# Used ONLY when a lock directory records no readable `ttl` of its own — an interrupted
# acquire, a truncated file, a lock written by an older build. The cost of the two errors is
# asymmetric: too long delays one skippable run of the other agent, too short is the
# concurrent double-run this whole file exists to prevent. PID liveness and the recorded
# start time clear the ordinary crash (and a recycled pid) in seconds; a ttl only ever decides
# the rare cases where `ps` cannot say, and whether a live holder is reported OVERDUE.
AGENT_LOCK_TTL_FALLBACK="${LOOP_AGENT_LOCK_TTL_FALLBACK:-7200}"

# Process names that count as "a real loop runner" when a lock records no `proc` of its own.
# Space-separated; bash 3.2 (the macOS default) has no arrays worth the trouble here. The two
# shipped runners are built in; LOOP_AGENT_LOCK_KNOWN_PROCS appends more.
AGENT_LOCK_KNOWN_PROCS="dream.sh pr_review.sh ${LOOP_AGENT_LOCK_KNOWN_PROCS:-}"

AGENT_LOCK_HELD=0
AGENT_LOCK_RUN=""
AGENT_LOCK_TTL=""
AGENT_LOCK_MSG=""
AGENT_LOCK_RECLAIMED=""
AGENT_LOCK_OVERDUE=""
AGENT_LOCK_HOLDER_LABEL=""
AGENT_LOCK_HOLDER_PID=""
AGENT_LOCK_HOLDER_RUN=""
AGENT_LOCK_HOLDER_SINCE=""
AGENT_LOCK_HOLDER_PROC=""
AGENT_LOCK_HOLDER_LSTART=""
AGENT_LOCK_HOLDER_AGE=""

_agent_lock_field() {   # _agent_lock_field <filename> → the first line, whitespace stripped
  head -1 "$AGENT_LOCK_DIR/$1" 2>/dev/null | tr -d '[:space:]'
}

# A label may legitimately contain no spaces (they are log-line fields), but read it as a
# whole line anyway rather than assuming — a field read is not the place to enforce a format.
_agent_lock_text() {    # _agent_lock_text <filename> → the first line, trailing newline gone
  head -1 "$AGENT_LOCK_DIR/$1" 2>/dev/null | tr -d '\r\n'
}

_agent_lock_now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# The process start time of <pid>, whitespace-free, or "" when `ps` cannot say. LC_ALL and TZ
# are PINNED: `ps -o lstart=` prints local time in the current locale, so a launchd job and a
# shell with a different TZ or LANG would otherwise disagree about the very same process and
# call a live holder "recycled" — the one misreading that robs a running agent.
_agent_lock_lstart() {  # _agent_lock_lstart <pid> → e.g. "FriSep1116:29:462026" | ""
  [ -n "${1:-}" ] || return 0
  LC_ALL=C TZ=UTC ps -o lstart= -p "$1" 2>/dev/null | tr -d '[:space:]'
}

# Seconds since <iso>, or "" when it cannot be parsed. Empty means "no age evidence", and
# every caller below treats that as "do not reclaim on age" — an unparseable timestamp must
# never read as infinitely old.
_agent_lock_age() {     # _agent_lock_age <iso> → seconds | ""
  [ -n "${1:-}" ] || return 0
  python3 - "$1" <<'PY' 2>/dev/null
import datetime, sys
try:
    t = sys.argv[1].replace("Z", "+00:00")
    d = datetime.datetime.fromisoformat(t)
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    print(int((datetime.datetime.now(datetime.timezone.utc) - d).total_seconds()))
except Exception:
    pass
PY
}

# Who is the recorded holder, really? Prints exactly one word (the table in the header):
#   dead · recycled · alive · unverified (command matches, no start time on record) · unknown
# Same substring rule as both runners' own lock_owner_alive, widened to recognise a SIBLING
# agent's process rather than only this script's own name.
_agent_lock_holder_state() {   # _agent_lock_holder_state <pid> <proc|""> <lstart|"">
  local pid="${1:-}" proc="${2:-}" want="${3:-}" cmd p hit have
  [ -n "$pid" ] || { echo dead; return 0; }
  kill -0 "$pid" 2>/dev/null || { echo dead; return 0; }
  cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
  # ps said nothing → cannot tell. Never steal a lock on a guess; only the ttl may, below.
  [ -n "$cmd" ] || { echo unknown; return 0; }
  hit=0
  if [ -n "$proc" ]; then
    case "$cmd" in *"$proc"*) hit=1 ;; esac
  else
    for p in $AGENT_LOCK_KNOWN_PROCS; do
      case "$cmd" in *"$p"*) hit=1 ;; esac
    done
  fi
  [ "$hit" -eq 1 ] || { echo recycled; return 0; }   # the pid now runs something unrelated
  [ -n "$want" ] || { echo unverified; return 0; }
  have="$(_agent_lock_lstart "$pid")"
  [ -n "$have" ] || { echo unknown; return 0; }
  # Same pid, same command, DIFFERENT start time: a later process of the same name got the pid.
  if [ "$have" = "$want" ]; then echo alive; else echo recycled; fi
}

# Reads the holder's fields into AGENT_LOCK_HOLDER_* (called on every loop turn, so a lock
# that changes hands mid-retry is described by whoever holds it NOW).
_agent_lock_read_holder() {
  AGENT_LOCK_HOLDER_PID="$(_agent_lock_field pid)"
  AGENT_LOCK_HOLDER_RUN="$(_agent_lock_field run)"
  AGENT_LOCK_HOLDER_SINCE="$(_agent_lock_field started_at)"
  AGENT_LOCK_HOLDER_PROC="$(_agent_lock_field proc)"
  AGENT_LOCK_HOLDER_LSTART="$(_agent_lock_field lstart)"
  AGENT_LOCK_HOLDER_LABEL="$(_agent_lock_text label)"
  [ -n "$AGENT_LOCK_HOLDER_LABEL" ] || AGENT_LOCK_HOLDER_LABEL="unknown"
}

# Remove the lock directory — but ONLY IF it is still the exact instance that was judged
# stale, and only while holding the reclaim flock. See "RECLAIMERS ARE SERIALIZED" above.
#   0 removed · 1 not removed: it changed hands or is already gone (re-read it) ·
#   2 another reclaimer held the flock for 10s · 3 could not remove, or could not run at all
_agent_lock_reclaim() {   # _agent_lock_reclaim <judged-pid> <judged-run> <judged-started_at>
  python3 - "$AGENT_LOCK_DIR" "${1:-}" "${2:-}" "${3:-}" <<'PY' 2>/dev/null
import fcntl, os, shutil, sys, time
lock, want = sys.argv[1], tuple(sys.argv[2:5])
try:
    fh = open(lock + ".reclaim", "a")
except OSError:
    raise SystemExit(3)
deadline = time.time() + 10
while True:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except OSError:
        if time.time() >= deadline:
            raise SystemExit(2)
        time.sleep(0.02)
def field(name):
    # the same normalisation as _agent_lock_field: first line, every whitespace char removed
    try:
        with open(os.path.join(lock, name)) as f:
            return "".join(f.readline().split())
    except OSError:
        return ""
if not os.path.isdir(lock):
    raise SystemExit(1)
if (field("pid"), field("run"), field("started_at")) != want:
    raise SystemExit(1)
shutil.rmtree(lock, ignore_errors=True)
raise SystemExit(3 if os.path.exists(lock) else 0)
PY
}

# agent_lock_acquire <label> <run-id> <process-name> <ttl-seconds>
#   0 = acquired (AGENT_LOCK_HELD=1)
#   1 = another agent holds it (AGENT_LOCK_MSG + AGENT_LOCK_HOLDER_* say who; AGENT_LOCK_OVERDUE
#       is set when that holder is verifiably alive but past its own recorded worst case)
#   2 = could not acquire or reclaim after the retry bound
agent_lock_acquire() {
  local label="${1:-agent}" run="${2:-}" proc="${3:-}" ttl="${4:-}"
  local tries=0 age httl holder_desc state why rrc

  case "$ttl" in ''|*[!0-9]*) ttl="$AGENT_LOCK_TTL_FALLBACK" ;; esac
  # A ttl below a minute is a typo, not a policy: it would make every legitimate run
  # reclaimable almost immediately, which is worse than no lock at all.
  [ "$ttl" -ge 60 ] 2>/dev/null || ttl="$AGENT_LOCK_TTL_FALLBACK"

  AGENT_LOCK_MSG=""
  AGENT_LOCK_RECLAIMED=""
  AGENT_LOCK_OVERDUE=""
  AGENT_LOCK_HOLDER_AGE=""
  mkdir -p "$(dirname "$AGENT_LOCK_DIR")" 2>/dev/null || true

  while : ; do
    if mkdir "$AGENT_LOCK_DIR" 2>/dev/null; then
      AGENT_LOCK_HELD=1
      AGENT_LOCK_RUN="$run"
      AGENT_LOCK_TTL="$ttl"
      # pid FIRST, then the start time that proves which process owns that pid, then the
      # rest. started_at goes LAST: a reader that finds the directory without it knows the
      # acquire is still in flight and waits for it (the tries<5 branch below) instead of
      # judging — or reclaiming — a half-written lock.
      printf '%s\n' "$$"                         > "$AGENT_LOCK_DIR/pid"
      printf '%s\n' "$(_agent_lock_lstart "$$")" > "$AGENT_LOCK_DIR/lstart"
      printf '%s\n' "$proc"                      > "$AGENT_LOCK_DIR/proc"
      printf '%s\n' "$label"                     > "$AGENT_LOCK_DIR/label"
      printf '%s\n' "$run"                       > "$AGENT_LOCK_DIR/run"
      printf '%s\n' "$ttl"                       > "$AGENT_LOCK_DIR/ttl"
      printf '%s\n' "$(_agent_lock_now_iso)"     > "$AGENT_LOCK_DIR/started_at"
      # One line for a human who runs `cat` on the directory at 02:05 and wants to know who
      # is in there without reading seven files. Never parsed — the files are the record.
      printf '%s (pid %s) run %s started %s ttl %ss\n' \
        "$label" "$$" "${run:-none}" "$(_agent_lock_now_iso)" "$ttl" > "$AGENT_LOCK_DIR/holder"
      return 0
    fi

    _agent_lock_read_holder
    if { [ -z "$AGENT_LOCK_HOLDER_PID" ] || [ -z "$AGENT_LOCK_HOLDER_SINCE" ]; } && [ "$tries" -lt 5 ]; then
      # A racing acquirer may have mkdir'd and not finished writing (started_at is its last
      # field) — give it a beat rather than concluding the lock is corrupt or stale.
      tries=$((tries + 1)); sleep 1; continue
    fi

    httl="$(_agent_lock_field ttl)"
    case "$httl" in ''|*[!0-9]*) httl="$AGENT_LOCK_TTL_FALLBACK" ;; esac
    age="$(_agent_lock_age "$AGENT_LOCK_HOLDER_SINCE")"
    AGENT_LOCK_HOLDER_AGE="$age"
    holder_desc="${AGENT_LOCK_HOLDER_LABEL} pid ${AGENT_LOCK_HOLDER_PID:-none}"
    state="$(_agent_lock_holder_state "$AGENT_LOCK_HOLDER_PID" "$AGENT_LOCK_HOLDER_PROC" "$AGENT_LOCK_HOLDER_LSTART")"
    why=""
    case "$state" in
      dead)
        why="dead_pid stale_pid=${AGENT_LOCK_HOLDER_PID:-none} stale_label=${AGENT_LOCK_HOLDER_LABEL}" ;;
      recycled)
        why="recycled_pid stale_pid=${AGENT_LOCK_HOLDER_PID:-none} stale_label=${AGENT_LOCK_HOLDER_LABEL}" ;;
      unknown|unverified)
        # No identity proof either way — `ps` said nothing, or the lock predates the start
        # time. The holder's own ttl is the only evidence left, and it is used as before.
        if [ -n "$age" ] && [ "$age" -gt "$httl" ] 2>/dev/null; then
          why="age_exceeded age_s=$age ttl_s=$httl holder_state=$state stale_label=${AGENT_LOCK_HOLDER_LABEL} stale_pid=${AGENT_LOCK_HOLDER_PID:-none}"
        fi ;;
    esac

    # ── alive, or unprovable and inside its ttl → BUSY. Skip, never wait. ────────
    if [ -z "$why" ]; then
      if [ "$state" = "alive" ] && [ -n "$age" ] && [ "$age" -gt "$httl" ] 2>/dev/null; then
        AGENT_LOCK_OVERDUE="age_s=$age ttl_s=$httl"
        echo "agent_lock: the shared lock is ${age}s old — PAST the ${httl}s worst case its holder" >&2
        echo "  recorded ($holder_desc). That pid is VERIFIABLY the same live process (command and" >&2
        echo "  start time match), so it is NOT reclaimed: robbing a running agent is the" >&2
        echo "  double-run this lock exists to prevent. If it is wedged, stop it by hand:" >&2
        echo "    ps -o pid,lstart,command -p ${AGENT_LOCK_HOLDER_PID}" >&2
      fi
      AGENT_LOCK_MSG="the shared agent lock is held by ${AGENT_LOCK_HOLDER_LABEL} (pid ${AGENT_LOCK_HOLDER_PID}, run ${AGENT_LOCK_HOLDER_RUN:-unknown}, since ${AGENT_LOCK_HOLDER_SINCE:-unknown}${AGENT_LOCK_OVERDUE:+, OVERDUE $AGENT_LOCK_OVERDUE}, lock $AGENT_LOCK_DIR)"
      return 1
    fi

    # ── reclaim: serialized, and only the exact instance that was judged stale ──
    echo "agent_lock: stale shared lock at $AGENT_LOCK_DIR ($holder_desc: ${why%% *}) — reclaiming" >&2
    _agent_lock_reclaim "$AGENT_LOCK_HOLDER_PID" "$AGENT_LOCK_HOLDER_RUN" "$AGENT_LOCK_HOLDER_SINCE"; rrc=$?
    case "$rrc" in
      0) AGENT_LOCK_RECLAIMED="$why" ;;
      1) echo "agent_lock: …another agent reclaimed or replaced it first — re-reading the holder" >&2 ;;
      2) echo "agent_lock: …another agent is mid-reclaim — re-reading the holder" >&2 ;;
      *) echo "agent_lock: …could not remove $AGENT_LOCK_DIR (rc $rrc)" >&2 ;;
    esac
    tries=$((tries + 1))
    [ "$tries" -lt 8 ] || { AGENT_LOCK_MSG="could not acquire or reclaim $AGENT_LOCK_DIR after $tries tries"; return 2; }
  done
}

# Ownership-checked and idempotent. Safe to call from an EXIT trap that fires before any
# acquire ever ran, and safe to call twice.
agent_lock_release() {
  [ "${AGENT_LOCK_HELD:-0}" -eq 1 ] || return 0
  local pid run
  pid="$(_agent_lock_field pid)"
  run="$(_agent_lock_field run)"
  # BOTH must match. The pid alone is not proof: this process could have been reclaimed-past
  # and a LATER acquirer could — vanishingly rarely, but the cost is two live agents — be
  # running under a recycled pid. The run id is unique per invocation, so the pair is.
  if [ "$pid" = "$$" ] && { [ -z "${AGENT_LOCK_RUN:-}" ] || [ "$run" = "${AGENT_LOCK_RUN}" ]; }; then
    rm -rf "$AGENT_LOCK_DIR"
  else
    echo "agent_lock: NOT releasing $AGENT_LOCK_DIR — it is no longer ours (holder pid" >&2
    echo "  '${pid:-none}' run '${run:-none}' vs ours $$ / '${AGENT_LOCK_RUN:-none}'). Something" >&2
    echo "  reclaimed it while we held it; removing it now would unlock a RUNNING agent." >&2
  fi
  AGENT_LOCK_HELD=0
}
