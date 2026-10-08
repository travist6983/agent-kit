#!/usr/bin/env bash
# infra-prod-verify preflight — everything that must be true BEFORE anything touches production.
#
#   bash prod_verify_preflight.sh --ticket PROJ-123 [--env prod] [--mirror-env staging] [--evid-root runs]
#
# Checks, in order, and FAILS CLOSED on each:
#   1. R9 kill-switch — the brain's global_pause must read an explicit False. "True" or unreadable = stop.
#   2. The target environment has a URL in stack.config (app.environments.<env>.url).
#   3. The ticket's replay oracle exists (<evid-root>/<ticket>/replay-oracle.sh, or repro.json.replay_spec).
#   4. The oracle's prod-safety marker: the exact line `# PROD-SAFE: read-only`. Present -> smoke_mode=prod.
#      Absent -> smoke_mode=mirror when --mirror-env has a URL, else skipped-mutating. ALLOW_PROD is
#      never set here; it is set by the caller only when smoke_mode is prod.
#
# Prints ONE JSON object:
#   {"ok":true,"paused":false,"env":"prod","app_url":"…","oracle":"…","prod_safe":true,
#    "smoke_mode":"prod|mirror|skipped-mutating","mirror_env":"staging","mirror_url":"…"|null,
#    "allow_prod":"1"|null,"evid_dir":"…"}
#   {"ok":false,"abort":"global-pause|pause-check-failed|no-env-url|no-oracle|bad-args","detail":"…"}
# Exit 0 ok · 2 aborted. No network except the brain's pause flag; nothing is executed against the app.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${LOOPS_REPO_ROOT:-$(cd "$HERE/../../../.." && pwd)}"
KB="$HERE/../../qa-kb/scripts/kb.py"   # sibling skill, resolved from this script (stub-able in tests); call with python3
CLI="$REPO_ROOT/lib/adapters/cli.py"

jstr(){ python3 -c 'import json,sys;print(json.dumps(sys.argv[1] if sys.argv[1] != "" else None))' "$1"; }
cfg(){ python3 - "$REPO_ROOT" "$1" <<'PY'
import sys; sys.path.insert(0, sys.argv[1])
from lib.adapters import cfg
v = cfg(sys.argv[2], None)
print("" if v is None else v)
PY
}
abort(){ printf '{"ok":false,"abort":"%s","detail":%s}\n' "$1" "$(jstr "$2")"; exit 2; }

TICKET=""; ENV_NAME="prod"; MIRROR_ENV="staging"; EVID_ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ticket) TICKET="${2:-}"; shift 2 ;;
    --env) ENV_NAME="${2:-}"; shift 2 ;;
    --mirror-env) MIRROR_ENV="${2:-}"; shift 2 ;;
    --evid-root) EVID_ROOT="${2:-}"; shift 2 ;;
    *) abort bad-args "unknown argument $1" ;;
  esac
done
[ -n "$TICKET" ] || TICKET="${LOOP_TICKET:-}"
[ -n "$TICKET" ] || abort bad-args "--ticket (or LOOP_TICKET) is required"

# 1. R9 kill-switch. Ship/verify ONLY on an explicit not-paused; anything else is a refusal.
PAUSED=$(python3 "$KB" pause --status 2>/dev/null \
  | python3 -c "import sys,json;print((json.load(sys.stdin).get('control') or {}).get('value'))" 2>/dev/null)
if [ "$PAUSED" != "False" ]; then
  if [ "$PAUSED" = "True" ]; then
    abort global-pause "the brain's global_pause is SET (R9) — no prod action; escalate via dev-escalate"
  fi
  abort pause-check-failed "could not read the brain's global_pause (R9) — failing CLOSED"
fi

# 2. Target environment URL from stack.config.
APP_URL="$(cfg "app.environments.$ENV_NAME.url")"
[ -n "$APP_URL" ] || abort no-env-url "app.environments.$ENV_NAME.url is not set in stack.config.json — nothing to verify against"
APP_URL="${APP_URL%/}"

# 3. Evidence dir + the oracle.
[ -n "$EVID_ROOT" ] || EVID_ROOT="$(cfg evidence.root)"
[ -n "$EVID_ROOT" ] || EVID_ROOT="runs"
case "$EVID_ROOT" in /*) ;; *) EVID_ROOT="$REPO_ROOT/$EVID_ROOT" ;; esac
EVID_DIR="$EVID_ROOT/$TICKET"
ORACLE=""
if [ -f "$EVID_DIR/repro.json" ]; then
  ORACLE=$(python3 -c 'import json,sys
d=json.load(open(sys.argv[1])); s=d.get("replay_spec") or ""
print(s if isinstance(s,str) else (s.get("path") or s.get("file") or ""))' "$EVID_DIR/repro.json" 2>/dev/null)
  case "$ORACLE" in ""|/*) ;; *) ORACLE="$EVID_DIR/$ORACLE" ;; esac
fi
[ -n "$ORACLE" ] && [ -f "$ORACLE" ] || ORACLE="$EVID_DIR/replay-oracle.sh"
[ -f "$ORACLE" ] || abort no-oracle "no replay oracle for $TICKET under $EVID_DIR (repro.json.replay_spec or replay-oracle.sh) — qa-repro must have produced one"

# 4. Prod-safety marker decides where the smoke may run.
PROD_SAFE=false; SMOKE_MODE="skipped-mutating"; ALLOW_PROD=""; MIRROR_URL=""
if grep -qx '# PROD-SAFE: read-only' "$ORACLE"; then
  PROD_SAFE=true; SMOKE_MODE="prod"; ALLOW_PROD="1"
else
  [ -n "$MIRROR_ENV" ] && MIRROR_URL="$(cfg "app.environments.$MIRROR_ENV.url")"
  [ -n "$MIRROR_URL" ] && SMOKE_MODE="mirror"
fi

printf '{"ok":true,"paused":false,"env":%s,"app_url":%s,"oracle":%s,"prod_safe":%s,"smoke_mode":"%s","mirror_env":%s,"mirror_url":%s,"allow_prod":%s,"evid_dir":%s}\n' \
  "$(jstr "$ENV_NAME")" "$(jstr "$APP_URL")" "$(jstr "$ORACLE")" "$PROD_SAFE" "$SMOKE_MODE" \
  "$(jstr "$MIRROR_ENV")" "$(jstr "${MIRROR_URL%/}")" "$(jstr "$ALLOW_PROD")" "$(jstr "$EVID_DIR")"
