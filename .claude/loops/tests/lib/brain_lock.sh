#!/usr/bin/env bash
# Shared single-instance lock for every entry point that writes to the SHARED brain.
#
# WHY ONE LOCK, NOT ONE PER SUITE: the brain-touching suites each delete their own marker
# GLOBALLY at start — harness.py (source=kb-harness), test_kb_durability.sh (test-kbdur),
# test_fix_lessons.sh (test-fixlessons) — and app_map's UNIQUE(kind, area, key) carries no
# source column. So two concurrent runs delete each other's live fixtures AND collide on
# app_map_kind_area_key_key, producing reds that read exactly like product regressions.
# A lock that guards one of three doors is not a lock: all three point at THIS bolt.
#
# Two sessions running the suite at once cost real debugging time twice before this existed.
# The honest behaviour is to REFUSE and say why, never to emit results we know are lies.
#
# Usage (shell suites):
#     . "$(dirname "${BASH_SOURCE[0]}")/lib/brain_lock.sh"
#     brain_lock_acquire "test_kb_durability" || exit 3
#     trap brain_lock_release EXIT
#
# NESTING: run_all.sh acquires once and exports KB_LOCK_HELD, so the suites it
# invokes skip acquiring instead of self-deadlocking. Running a suite standalone still locks.

# KB_HOME is kb.py's own brain-dir override, so this bolt and harness.py's (KB_BRAIN_LOCK or
# <KB_HOME>/brain-tests.lock) always resolve to the SAME directory.
BRAIN_LOCK_DIR="${KB_BRAIN_LOCK:-${KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}/brain-tests.lock}"
_BRAIN_LOCK_HELD_BY_ME=0

# An exported marker OUTLIVES the process that set it: a child of a dead parent would inherit
# "someone holds it" and skip locking precisely when it is least safe. So the marker is only
# believed when the lock directory actually exists. Marker-set-but-dir-gone => acquire.
brain_lock_inherited() {
  [ -n "${KB_LOCK_HELD:-}" ] && [ -d "$BRAIN_LOCK_DIR" ]
}

_brain_lock_owner_alive() {
  local pid="$1" cmd
  kill -0 "$pid" 2>/dev/null || return 1
  cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
  [ -z "$cmd" ] && return 0          # ps said nothing -> cannot tell -> assume ALIVE
  case "$cmd" in
    *run_all.sh*|*harness.py*|*test_kb_durability*|*test_fix_lessons*) return 0 ;;
  esac
  return 1                            # pid recycled by something unrelated -> stale
}

brain_lock_acquire() {
  local who="${1:-suite}" tries=0 pid ts
  brain_lock_inherited && return 0     # a parent already holds it — nest quietly
  mkdir -p "$(dirname "$BRAIN_LOCK_DIR")" 2>/dev/null || true
  while : ; do
    if mkdir "$BRAIN_LOCK_DIR" 2>/dev/null; then
      _BRAIN_LOCK_HELD_BY_ME=1
      printf '%s\n' "$$"   > "$BRAIN_LOCK_DIR/pid"
      printf '%s\n' "$who" > "$BRAIN_LOCK_DIR/owner"
      date -u +%Y-%m-%dT%H:%M:%SZ > "$BRAIN_LOCK_DIR/started_at" 2>/dev/null || true
      export KB_LOCK_HELD=1
      return 0
    fi
    pid="$(head -1 "$BRAIN_LOCK_DIR/pid" 2>/dev/null | tr -d '[:space:]')"
    ts="$(head -1 "$BRAIN_LOCK_DIR/started_at" 2>/dev/null | tr -d '[:space:]')"
    if [ -z "$pid" ] && [ "$tries" -lt 5 ]; then
      tries=$((tries + 1)); sleep 1; continue   # racing acquirer hasn't written its pid yet
    fi
    if [ -n "$pid" ] && _brain_lock_owner_alive "$pid"; then
      echo "  REFUSED: another brain-touching suite is running (pid $pid, owner $(head -1 "$BRAIN_LOCK_DIR/owner" 2>/dev/null), since ${ts:-unknown})." >&2
      echo "  Two at once delete each other's fixtures and collide on app_map_kind_area_key_key —" >&2
      echo "  the reds would be lies, so this run refuses rather than producing them." >&2
      echo "  Wait for it, or if you are certain it is dead: rm -rf $BRAIN_LOCK_DIR" >&2
      return 1
    fi
    echo "  brain lock: stale at $BRAIN_LOCK_DIR (pid ${pid:-none} not alive) — reclaiming" >&2
    rm -rf "$BRAIN_LOCK_DIR"
    tries=$((tries + 1))
    [ "$tries" -lt 8 ] || { echo "  could not acquire or reclaim $BRAIN_LOCK_DIR" >&2; return 2; }
  done
}

brain_lock_release() {
  [ "${_BRAIN_LOCK_HELD_BY_ME:-0}" -eq 1 ] || return 0
  _BRAIN_LOCK_HELD_BY_ME=0
  unset KB_LOCK_HELD
  rm -rf "$BRAIN_LOCK_DIR"
}
