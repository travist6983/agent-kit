#!/usr/bin/env bash
# upload_evidence_media.sh — upload ONE evidence artifact (a GIF or an MP4) through the `evidence_store`
# adapter at capture time and print one `media` entry the control plane (and later infra-ship) can consume.
#
# This file is the SINGLE home of the upload helper. Every capture skill calls it:
#   qa-repro    → before-gif / before-mp4  (the reproduced bug)
#   qa-verify   → after-gif  / after-mp4   (the verified fix / fix-verify), or a live re-observation
#   qa-run-case → gif / mp4                (a recorded case run)
#
# Usage:  upload_evidence_media.sh <KEY> <local-file> <kind> [label]
#   kind ∈ before-gif | before-mp4 | after-gif | after-mp4 | gif | mp4
# Prints ONE JSON object to STDOUT (nothing else):  {"kind":..,"url":..,"label":..}
# The `url` is whatever the configured provider returns — a `file://` path for the default `local`
# provider, a signed read URL for a remote one. Callers collect these objects into `media[]`.
#
# BEST-EFFORT by contract: `media[]` is OPTIONAL in every consuming contract. If the adapter is `none`,
# its config is incomplete, or the upload fails, this script prints nothing to stdout, a reason to
# stderr, and exits non-zero (3 = adapter unavailable, 1 = anything else) — the caller SKIPS that media
# entry and the run still validates. Never let a missing store break a reproduction/verification.
#
# Config: `adapters.evidence_store` in .claude/loops/stack.config.json — the provider and its options.
# Secrets are env vars the config NAMES; this script never reads, echoes or hardcodes one. A remote
# provider is responsible for the object's Content-Type (video/mp4 | image/gif, derivable from the
# file extension) and for any cross-origin read rule its bucket needs — documented with the provider,
# never set from here.
set -euo pipefail

KEY="${1:?usage: upload_evidence_media.sh <KEY> <local-file> <kind> [label]}"
FILE="${2:?usage: upload_evidence_media.sh <KEY> <local-file> <kind> [label]}"
KIND="${3:?usage: upload_evidence_media.sh <KEY> <local-file> <kind> [label]}"
LABEL="${4:-$KIND}"

die() { echo "upload_evidence_media: $1" >&2; exit "${2:-1}"; }   # die <message> [exit-code]

case "$KIND" in
  before-gif|before-mp4|after-gif|after-mp4|gif|mp4) ;;
  *) die "invalid kind '$KIND' (must be before-gif|before-mp4|after-gif|after-mp4|gif|mp4)";;
esac
[ -f "$FILE" ] || die "file not found: $FILE"
case "$KIND:$FILE" in
  *-gif:*.gif|gif:*.gif|*-mp4:*.mp4|mp4:*.mp4) ;;
  *) die "kind '$KIND' does not match the file extension of $FILE";;
esac
command -v python3 >/dev/null 2>&1 || die "python3 not installed"

# The framework checkout this script lives in: <repo>/.claude/skills/qa-repro/scripts/ → <repo>.
# LOOPS_REPO_ROOT (the adapter package's own override) wins, so a recipe can point at another checkout.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${LOOPS_REPO_ROOT:-$(cd "$HERE/../../../.." && pwd)}"
CLI="$REPO_ROOT/lib/adapters/cli.py"
[ -f "$CLI" ] || die "adapter CLI not found at $CLI (set LOOPS_REPO_ROOT to the framework checkout)"

ARGS="$(python3 -c 'import json,sys; print(json.dumps({"path": sys.argv[1], "key": sys.argv[2]}))' "$FILE" "$KEY")"
set +e
OUT="$(python3 "$CLI" evidence_store upload --json "$ARGS" 2>/dev/null)"; rc=$?
set -e
case "$rc" in
  0) ;;
  3) die "evidence_store adapter unavailable ($(printf '%s' "$OUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("error","adapter-unavailable:evidence_store"))
except Exception: print("adapter-unavailable:evidence_store")')) — media[] is optional, skipping" 3;;
  *) die "upload failed for $FILE: $(printf '%s' "$OUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("error","adapter error"))
except Exception: print("adapter error (exit '"$rc"')")')";;
esac

# ONE JSON object on stdout: {kind, url, label}. Built with python so the url/label are quoted correctly.
printf '%s' "$OUT" | python3 -c '
import json, sys
kind, label = sys.argv[1], sys.argv[2]
try:
    url = json.load(sys.stdin)["result"]["url"]
except Exception:
    url = None
if not url:
    sys.stderr.write("upload_evidence_media: adapter returned no url\n"); sys.exit(1)
print(json.dumps({"kind": kind, "url": url, "label": label}, ensure_ascii=False))
' "$KIND" "$LABEL"
