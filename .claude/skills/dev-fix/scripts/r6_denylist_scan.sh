#!/usr/bin/env bash
# R6 blast-radius denylist scan — the MECHANICAL enforcement of the R6 gate.
# dev-fix runs it on its touched files BEFORE emitting fix.json; infra-ship's
# ship_preflight.sh runs it on the full change set as the LAST gate before
# commit/push/PR. Documented in ../references/blast-radius-denylist.md.
#
# Where the patterns come from (first match wins):
#   1. --globs FILE                       explicit override (tests)
#   2. `blast_radius_denylist_globs` in the target repo's conventions file —
#      `stack.conventions_file` in .claude/loops/stack.config.json (default
#      .claude/loops/conventions.json) — when that array is NON-EMPTY
#   3. ../assets/r6-denylist.globs        the framework default (the floor)
#
# Usage:
#   r6_denylist_scan.sh --diff            scan git changes: unstaged + staged + untracked vs HEAD
#   r6_denylist_scan.sh --files F1 [F2..] scan an explicit path list
#   ... | r6_denylist_scan.sh --stdin     scan newline-separated paths from stdin
#   [--globs FILE]                        override the pattern source (tests)
#
# Prints one JSON object: {"ok":bool,"scanned":N,"source":"...","denylisted":[{"file","pattern"}]}
# Exit codes: 0 = clean · 3 = DENYLIST HIT (blocked — forced human gate, R6)
#             2 = cannot scan (no patterns / git failure) — FAIL CLOSED, treat as blocked
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# <repo>/.claude/skills/dev-fix/scripts → <repo>; LOOPS_REPO_ROOT (the config loader's own
# override) wins when set, so a test or a runner can point at another checkout.
REPO_ROOT="${LOOPS_REPO_ROOT:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"
DEFAULT_GLOBS="$SCRIPT_DIR/../assets/r6-denylist.globs"
GLOBS=""
MODE=""; FILES=()
while [ $# -gt 0 ]; do case "$1" in
  --diff)  MODE="diff"; shift;;
  --stdin) MODE="stdin"; shift;;
  --files) MODE="files"; shift; while [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; do FILES+=("$1"); shift; done;;
  --globs) GLOBS="$2"; shift 2;;
  *) printf '{"ok":false,"error":"unknown arg %s"}\n' "$1"; exit 2;;
esac; done
if [ -z "$MODE" ]; then
  echo '{"ok":false,"error":"usage: r6_denylist_scan.sh --diff | --files F1.. | --stdin [--globs FILE]"}'; exit 2
fi
if [ -n "$GLOBS" ] && [ ! -f "$GLOBS" ]; then
  printf '{"ok":false,"error":"denylist globs not found: %s — FAIL CLOSED, treat as blocked"}\n' "$GLOBS"; exit 2
fi
if [ -z "$GLOBS" ] && [ ! -f "$DEFAULT_GLOBS" ]; then
  printf '{"ok":false,"error":"default denylist globs not found: %s — FAIL CLOSED, treat as blocked"}\n' "$DEFAULT_GLOBS"; exit 2
fi

LIST=""
case "$MODE" in
  diff)
    LIST=$( { git diff --name-only HEAD 2>/dev/null && git diff --name-only --cached 2>/dev/null \
              && git ls-files --others --exclude-standard 2>/dev/null; } | sort -u ) || true
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
      echo '{"ok":false,"error":"not a git repo — cannot scan diff; FAIL CLOSED"}'; exit 2
    fi;;
  files) LIST=$(printf '%s\n' "${FILES[@]:-}");;
  stdin) LIST=$(cat);;
esac

LIST="$LIST" GLOBS="$GLOBS" DEFAULT_GLOBS="$DEFAULT_GLOBS" REPO_ROOT="$REPO_ROOT" python3 - <<'PY'
import fnmatch, json, os, sys


def read_globs_file(path):
    pats = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                pats.append(line)
    return pats


def conventions_globs(repo_root):
    """`blast_radius_denylist_globs` from the configured conventions file, or None when the
    config/file is absent or the array is empty. Never raises: a broken config falls through
    to the framework default — the default is the floor, never a reason to skip the scan."""
    try:
        sys.path.insert(0, repo_root)
        from lib.adapters.config import load_config
        stack = load_config()
        conv_path = stack.resolve(stack.get("stack.conventions_file"), ".claude/loops/conventions.json")
        if conv_path is None or not conv_path.exists():
            return None, None
        with open(conv_path, encoding="utf-8") as fh:
            conv = json.load(fh)
        globs = conv.get("blast_radius_denylist_globs") or []
        globs = [str(g).strip() for g in globs if str(g).strip()]
        return (globs or None), str(conv_path)
    except Exception:
        return None, None


explicit = os.environ.get("GLOBS") or ""
if explicit:
    pats, source = read_globs_file(explicit), explicit
else:
    pats, source = conventions_globs(os.environ["REPO_ROOT"])
    if not pats:
        pats, source = read_globs_file(os.environ["DEFAULT_GLOBS"]), os.environ["DEFAULT_GLOBS"]

if not pats:
    print(json.dumps({"ok": False, "error": "denylist is empty (%s) — FAIL CLOSED, treat as blocked" % source}))
    sys.exit(2)

files = [l.strip() for l in os.environ["LIST"].splitlines() if l.strip()]


def hit(f, p):
    # case-insensitive; '*/X/*' must also match repo-root 'X/...'; slash-free patterns match the basename too
    f, p = f.lower(), p.lower()
    return (fnmatch.fnmatchcase(f, p)
            or (p.startswith("*/") and fnmatch.fnmatchcase(f, p[2:]))
            or ("/" not in p and fnmatch.fnmatchcase(os.path.basename(f), p)))


hits = []
for f in files:
    for p in pats:
        if hit(f, p):
            hits.append({"file": f, "pattern": p})
            break

print(json.dumps({"ok": not hits, "scanned": len(files), "source": source, "denylisted": hits}))
sys.exit(3 if hits else 0)
PY
