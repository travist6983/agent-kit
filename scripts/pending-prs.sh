#!/usr/bin/env bash
# pending-prs.sh — which pull requests does the PR-review loop still owe a review?
#
# A thin operator wrapper over the queue read the loop itself makes:
#
#   python3 .claude/skills/dev-pr-feed/scripts/pr_feed.py unreviewed
#
# pr_feed reads the board through the vcs_host adapter named in .claude/loops/stack.config.json,
# drops drafts, PRs not targeting the base branch and PRs outside the author scope, sets aside
# PRs carrying the loop label (opened by a loop, reviewed in-loop), and consults the seen-ledger,
# keyed on PR id + HEAD COMMIT: a PR is pending when it has no entry (never-reviewed) or its
# entry's head commit differs from the PR's current head (new-commits-since-review). This script
# re-derives none of that — one implementation, one answer. It adds exactly two things:
#
#   1. The author scope the SCHEDULED tick runs with, so a bare call answers for that scope:
#        --scope flag  >  LOOP_PR_AUTHOR_SCOPE  >  pr_review_loop.author_scope (loop-limits.json)  >  own
#      A bare pr_feed call defaults to `own`; the runner (pr_review.sh) ignores the environment
#      and reads the JSON. loop-limits.json counts only when its block is valid as a pair —
#      prs_per_tick 1..10 and author_scope own|all|others — the runner's own rule. The resolved
#      scope and where it came from are printed in `filter`, so it is never a guess.
#   2. --format ids / count for shell use.
#
# FAIL CLOSED is inherited, not re-implemented: a failed read prints pr_feed's own
# {"status":"failed","failure_class":…} (or {"status":"blocked",…}) JSON on stdout WHATEVER
# --format says, with pr_feed's exit code, so a caller can always tell an empty queue from a
# broken read. "Everything is pending" is how an outage becomes duplicate comments on
# colleagues' pull requests; this script never produces it.
#
# Usage:
#   scripts/pending-prs.sh [--scope own|all|others] [--limit N] [--format json|ids|count]
#                          [--include-loop-prs]
#
#   --limit N           return at most N pending PRs (0 or absent = all), newest first
#   --format json       (default) pr_feed's `unreviewed` object plus a `filter` block
#   --format ids        pending PR ids, one per line (nothing on an empty queue)
#   --format count      the pending count only
#   --include-loop-prs  queue loop-labelled PRs too (LOOP_PR_INCLUDE_LOOP_PRS=1)
#
# Exit codes: 0 ok (an empty queue is ok) · 1 usage error in THIS script's arguments ·
# otherwise pr_feed's: 2 failure (auth-expired | api-unavailable | config-invalid) ·
# 3 fail-closed refusal (ledger-unreadable) or a blocked adapter (adapter-unavailable:vcs_host).
#
# Environment (all optional):
#   LOOP_PR_FEED            the queue reader (default: .claude/skills/dev-pr-feed/scripts/pr_feed.py)
#   LOOP_LOOP_LIMITS        the limits file (default: .claude/loops/loop-limits.json)
#   LOOP_PR_AUTHOR_SCOPE    see scope resolution above
#   Everything pr_feed reads passes straight through: LOOP_PR_LEDGER_BACKEND, LOOP_PR_LEDGER_FILE,
#   LOOP_PR_INCLUDE_LOOP_PRS, LOOP_PR_MAX_FILES, LOOP_PR_TARGET, LOOP_PR_IDENTITY, KB_CLI,
#   LOOPS_STACK_CONFIG. See dev-pr-feed's SKILL.md for each.
#
# Read-only: no lock, no model call, no ledger write. Needs bash + python3 (stdlib only).

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
FEED="${LOOP_PR_FEED:-$REPO/.claude/skills/dev-pr-feed/scripts/pr_feed.py}"
LIMITS_FILE="${LOOP_LOOP_LIMITS:-$REPO/.claude/loops/loop-limits.json}"
LIMIT_CEILING=10   # the runner's validity rule for pr_review_loop.prs_per_tick

SCOPE=""
SCOPE_SRC=""
LIMIT=0
FORMAT="json"
INCLUDE_LOOP="${LOOP_PR_INCLUDE_LOOP_PRS-}"

usage() { sed -n '/^# Usage:/,/^#   --include-loop-prs/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# Usage errors use the same JSON envelope pr_feed uses, minus a failure_class (they are not a
# board failure). Values travel as arguments, never interpolated into program text.
usage_fail() {
  python3 -c 'import json,sys; print(json.dumps({"status": "usage", "message": sys.argv[1]}, indent=2, sort_keys=True))' "$1"
  exit 1
}

config_fail() {   # a setting this script cannot work with: pr_feed's config-invalid, exit 2
  python3 -c 'import json,sys; print(json.dumps({"status": "failed", "failure_class": "config-invalid", "message": sys.argv[1]}, indent=2, sort_keys=True))' "$1"
  exit 2
}

need_val() { [ "$2" -ge 2 ] || usage_fail "$1 needs a value"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --scope)    need_val "$1" $#; SCOPE="$2"; SCOPE_SRC="flag"; shift 2 ;;
    --scope=*)  SCOPE="${1#*=}"; SCOPE_SRC="flag"; shift ;;
    --limit)    need_val "$1" $#; LIMIT="$2"; shift 2 ;;
    --limit=*)  LIMIT="${1#*=}"; shift ;;
    --format)   need_val "$1" $#; FORMAT="$2"; shift 2 ;;
    --format=*) FORMAT="${1#*=}"; shift ;;
    --include-loop-prs) INCLUDE_LOOP=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) usage_fail "unknown argument: $1" ;;
  esac
done

[ "$SCOPE_SRC" != "flag" ] || [ -n "$SCOPE" ] || usage_fail "--scope needs a value"
case "$LIMIT" in ''|*[!0-9]*) usage_fail "--limit must be a whole number (got '$LIMIT')" ;; esac
case "$FORMAT" in json|ids|count) ;; *) usage_fail "--format must be json, ids or count (got '$FORMAT')" ;; esac
[ -r "$FEED" ] || config_fail "the queue reader is not readable: $FEED (set LOOP_PR_FEED or restore dev-pr-feed)"

# ── author scope: flag > env > loop-limits.json > own ─────────────────────────
# Set-but-empty counts as set (pr_feed reads os.environ.get) and is handed to pr_feed as-is,
# which refuses it as config-invalid — never silently widened to another scope.
if [ -z "$SCOPE_SRC" ] && [ -n "${LOOP_PR_AUTHOR_SCOPE+set}" ]; then
  SCOPE="$LOOP_PR_AUTHOR_SCOPE"; SCOPE_SRC="env LOOP_PR_AUTHOR_SCOPE"
fi
if [ -z "$SCOPE_SRC" ]; then
  SCOPE="$(python3 - "$LIMITS_FILE" "$LIMIT_CEILING" <<'PY' 2>/dev/null
import json, sys
o = json.load(open(sys.argv[1]))["pr_review_loop"]
n, s = int(o["prs_per_tick"]), str(o["author_scope"])
if n < 1 or n > int(sys.argv[2]) or s not in ("own", "all", "others"):
    raise SystemExit(1)
print(s)
PY
)" && [ -n "$SCOPE" ] && SCOPE_SRC="loop-limits.json"
fi
if [ -z "$SCOPE_SRC" ]; then SCOPE="own"; SCOPE_SRC="default (no valid pr_review_loop in loop-limits.json)"; fi

# ── the read, through pr_feed (and so through the vcs_host adapter) ──────────
FEED_ARGS=(unreviewed)
[ "$LIMIT" -eq 0 ] || FEED_ARGS+=(--limit "$LIMIT")
if [ -n "$INCLUDE_LOOP" ]; then
  OUT="$(LOOP_PR_AUTHOR_SCOPE="$SCOPE" LOOP_PR_INCLUDE_LOOP_PRS="$INCLUDE_LOOP" python3 "$FEED" "${FEED_ARGS[@]}")"
else
  OUT="$(LOOP_PR_AUTHOR_SCOPE="$SCOPE" python3 "$FEED" "${FEED_ARGS[@]}")"
fi
RC=$?

# ── shape the answer; never turn a failure into an empty queue ───────────────
PP_OUT="$OUT" PP_RC="$RC" PP_SCOPE="$SCOPE" PP_SCOPE_SRC="$SCOPE_SRC" PP_LIMIT="$LIMIT" \
PP_FORMAT="$FORMAT" PP_INCLUDE_LOOP="$INCLUDE_LOOP" python3 - <<'PY'
import json, os, sys

E = os.environ.get
rc, fmt = int(E("PP_RC") or "0"), E("PP_FORMAT")
raw = E("PP_OUT") or ""
try:
    o = json.loads(raw[raw.index("{"):])
except Exception:
    o = None
if not isinstance(o, dict):
    # Not pr_feed's envelope at all (a traceback, nothing): say so as a failure, never as empty.
    sys.stdout.write(raw if raw.strip() else "")
    if not raw.endswith("\n") and raw:
        sys.stdout.write("\n")
    print(json.dumps({"status": "failed", "failure_class": "api-unavailable",
                      "message": "the queue reader returned no JSON (rc %d)" % rc}, indent=2, sort_keys=True))
    sys.exit(rc or 2)
ok = rc == 0 and o.get("status") == "ok" and isinstance(o.get("unreviewed"), list)
if ok:
    o["filter"] = {**(o.get("filter") or {}),
                   "status": "open", "is_draft": False,
                   "author_scope": E("PP_SCOPE"), "author_scope_source": E("PP_SCOPE_SRC"),
                   "loop_prs": "included" if E("PP_INCLUDE_LOOP") else "skipped",
                   "limit": int(E("PP_LIMIT") or "0") or None, "source": "scripts/pending-prs.sh"}
if fmt == "json" or not ok:
    json.dump(o, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
elif fmt == "ids":
    for r in o["unreviewed"]:
        print(r["pr_id"])
else:
    print(len(o["unreviewed"]))
sys.exit(rc)
PY
