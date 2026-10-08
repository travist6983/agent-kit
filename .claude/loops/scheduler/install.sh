#!/usr/bin/env bash
# install.sh — render, preflight, install (or remove) a scheduled loop agent under launchd.
#
#   bash install.sh --check                            # render + lint + preflight only. Touches nothing. SAFE.
#   bash install.sh --yes                              # render to ~/Library/LaunchAgents and bootstrap it
#   bash install.sh --uninstall                        # bootout + delete the installed copy
#   bash install.sh --label com.loops.pr-review --check
#
# THE TRACKED FILES ARE TEMPLATES, NOT PLISTS. `<label>.plist.template` carries every
# DECISION (schedule, arguments, priority class) and a {{PLACEHOLDER}} for every value that
# belongs to one machine. This script renders the template for the machine it runs on and
# installs the result; the template is never edited for a machine and never installed as is.
#   {{REPO_DIR}}    this checkout, derived from this script's own location
#   {{HOME}}        the installing user's real $HOME
#   {{CLAUDE_BIN}}  LOOP_INSTALL_CLAUDE_BIN, else stack.config runner.claude_bin (a path, or
#                   a bare name resolved on the job PATH) — VERIFIED executable, never guessed
#   {{CONFIG_DIR}}  LOOP_INSTALL_CONFIG_DIR, else stack.config runner.config_dir, else
#                   $CLAUDE_CONFIG_DIR, else $HOME/.claude — VERIFIED to be a directory
#   {{PATH}}        LOOP_INSTALL_PATH, else derived from where claude, python3 and git resolve
#                   plus the standard system directories — every binary the job needs is
#                   then re-resolved UNDER THAT PATH, which is the real test
# Every rendered value is printed, in the preflight and again at install time. A value that
# cannot be resolved FAILS the preflight and names the override to pass; nothing is guessed.
#
# --label selects which template in this directory to act on. It defaults to
# com.loops.qa-dream. ONE installer serves every agent on purpose: the `-t 0` refusal, the
# typed YES, bootout-before-bootstrap, and the resolve-under-the-job's-PATH preflight are
# the whole value of this file, and a sibling copy is a second place for them to drift.
# Per-job differences live in an optional sidecar `<LABEL>.job.sh` beside the template,
# never in an if-label branch. JOB-CONTRACT.md is the contract a job and its sidecar meet.
#
# ┌─ RUN THIS YOURSELF ───────────────────────────────────────────────────────┐
# │ Bootstrapping a loop agent arms an unattended job that WRITES TO THE      │
# │ SHARED BRAIN and consumes model budget — and com.loops.pr-review, once    │
# │ its ProgramArguments carry --live, POSTS COMMENTS ONTO PULL REQUESTS      │
# │ under the operator's own vcs_host identity. That is a human decision, so  │
# │ without --yes this script refuses to install from a non-interactive shell │
# │ (an agent, a pipeline, a `bash install.sh` inside a tool call).           │
# │ Interactively it asks, and only a literal typed YES proceeds.             │
# └───────────────────────────────────────────────────────────────────────────┘
#
# See README.md for the cadence, how to verify a run happened, and how to disable.
# Never hardcode a cadence or an argument list in a message: the template is the source of
# truth, and a stale string here tells the operator a falsehood at the exact moment they are
# deciding whether the thing they just armed is correct. Every fact printed below is READ
# BACK OUT OF THE RENDERED PLIST that is about to be installed.
set -uo pipefail

# ── stack.config lookups (one helper, stdlib python, no network) ─────────────
# stack_cfg <dotted.key> [default] — the value from .claude/loops/stack.config.json
# (honouring the LOOPS_CFG__ env override), or the default when the file/key is absent.
# Sidecars use it too, so it is defined before anything sources them.
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

# kb_cfg <dotted.key> [default] — the same, for the brain pointer (kb.config.json, named by
# stack.config brain.config_file). Used only to learn which CLI the brain's auth mode needs.
kb_cfg() {
  python3 - "$REPO" "$(stack_cfg brain.config_file .claude/loops/kb.config.json)" "$1" "${2:-}" <<'PYCFG' 2>/dev/null || printf '%s' "${2:-}"
import json, os, sys
root, rel, key, default = sys.argv[1:5]
try:
    p = rel if os.path.isabs(rel) else os.path.join(root, rel)
    v = json.load(open(p))
    for part in key.split("."):
        v = v[part]
except Exception:
    v = None
if v is None or v == "":
    print(default, end="")
elif isinstance(v, (dict, list)):
    print(json.dumps(v), end="")
else:
    print(v, end="")
PYCFG
}

# ── the operator-facing description of a schedule, derived from the plist ────
# Every failure path prints a POINTER at the plist rather than a guess. The one rule
# that matters: launchd treats an ABSENT field as a WILDCARD, never as zero — so a
# minute-only StartCalendarInterval is HOURLY, and rendering it "every day at 00:17"
# is precisely the confidently-false sentence the header comment above exists to stop.
describe_schedule() {
  python3 - "$1" <<'PYEOF' 2>/dev/null || echo "see StartCalendarInterval in the plist"
import plistlib, sys
DAYS = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}
try:
    sci = plistlib.load(open(sys.argv[1], 'rb')).get('StartCalendarInterval')
except Exception:
    print("see StartCalendarInterval in the plist"); raise SystemExit
if isinstance(sci, dict):
    sci = [sci]
if not sci:
    print("no StartCalendarInterval set"); raise SystemExit

days  = [e.get('Weekday') for e in sci]
hours = set(e.get('Hour') for e in sci)
mins  = set(e.get('Minute') for e in sci)

# HOURLY, in both of its correct spellings: a single minute, no weekday, and either
# Hour omitted entirely (the wildcard) or all 24 hours listed explicitly.
if all(d is None for d in days) and len(mins) == 1 and None not in mins:
    m = sorted(mins)[0]
    if hours == {None}:
        print("every hour at :%02d local (Hour is unset — launchd reads an absent "
              "field as a wildcard)" % m)
        raise SystemExit
    if hours == set(range(24)):
        print("every hour at :%02d local (spelled out as 24 entries)" % m)
        raise SystemExit

# Minute omitted is the OTHER wildcard, and a nastier one: an Hour with no Minute
# fires SIXTY times inside that hour. Say so rather than render a tidy "03:00".
if None in mins:
    if hours == {None}:
        print("EVERY MINUTE of every day — neither Hour nor Minute is set, and launchd "
              "reads both absent fields as wildcards")
    else:
        hh = ", ".join("%02d" % h for h in sorted(h for h in hours if h is not None))
        print("EVERY MINUTE of the %s hour(s) local — Minute is unset, and launchd reads "
              "an absent field as a wildcard" % hh)
    raise SystemExit

# Anything left with a wildcard Hour is a shape this helper has no honest sentence
# for. Point at the plist instead of guessing.
if None in hours:
    print("see StartCalendarInterval in the plist (mixed wildcard Hour entries)")
    raise SystemExit

def hm(e): return "%02d:%02d" % (e.get('Hour'), e.get('Minute'))

if all(d is None for d in days):
    times = sorted(set(hm(e) for e in sci))
    print("every day at " + ", ".join(times) + " local")
else:
    print(" / ".join(DAYS.get(e.get('Weekday'), '?') for e in sci) + ", " + hm(sci[0]) + " local")
PYEOF
}

# ── the command the job will actually run, derived from the plist ────────────
# ProgramArguments[1:] — argv[0] is always /bin/bash and is printed by the caller when
# it wants it. There is exactly ONE renderer of this line so a second, hand-typed copy
# cannot drift away from it.
job_command() {
  python3 - "$1" <<'PYEOF' 2>/dev/null || echo "see ProgramArguments in the plist"
import plistlib, sys
a = plistlib.load(open(sys.argv[1], 'rb')).get('ProgramArguments') or []
if len(a) < 2:
    print("see ProgramArguments in the plist"); raise SystemExit
print(" ".join(a[1:]))
PYEOF
}

# ── render the template onto THIS machine ────────────────────────────────────
# Pure substitution; python decides nothing about paths and bash decides nothing about
# formatting. Writes <out-plist> and a TAB-separated <out-report>: one VALUE line per
# placeholder, one UNRESOLVED line per placeholder this script could not fill, one
# LEFTOVER line per {{…}} that survived (a typo in the template), and a final COUNT line
# whose presence proves the pass ran to the end.
render_template() {   # render_template <template> <out-plist> <out-report>
  python3 - "$1" "$2" "$3" \
    "REPO_DIR=$REPO" "HOME=$HOME" "CLAUDE_BIN=$R_CLAUDE_BIN" "CONFIG_DIR=$R_CONFIG_DIR" "PATH=$R_PATH" <<'PYEOF'
import re, sys
src, out, report = sys.argv[1:4]
vals = dict(kv.split("=", 1) for kv in sys.argv[4:])
rep = []
def line(*parts): rep.append("\t".join(str(p) for p in parts))
try:
    text = open(src, encoding="utf-8").read()
except Exception as exc:
    line("ERROR", "cannot read %s: %s" % (src, exc))
    open(report, "w").write("\n".join(rep) + "\n")
    raise SystemExit(3)
used = set(re.findall(r"\{\{([A-Z_]+)\}\}", text))
bad = 0
for k in sorted(used):
    v = vals.get(k, "")
    if not v:
        bad += 1
        line("UNRESOLVED", k)
        continue
    text = text.replace("{{%s}}" % k, v)
    line("VALUE", k, v)
left = sorted(set(re.findall(r"\{\{[^}]*\}\}", text)))
for l in left:
    bad += 1
    line("LEFTOVER", l)
if bad == 0:
    open(out, "w", encoding="utf-8").write(text)
line("COUNT", len(used), bad)
open(report, "w").write("\n".join(rep) + "\n")
raise SystemExit(0 if bad == 0 else 1)
PYEOF
}

# Print whatever the render pass reported. Called twice on purpose: once in the preflight,
# once at install time — "it told the operator one thing and did another" is precisely the
# bug the header of this file exists to prevent, and a rendering nobody saw is that bug.
# NO REPORT IS NOT "NOTHING TO REPORT": a pass that never ran (python3 broken on the
# operator's PATH) must read as a failure, not as a clean render.
print_render_report() {
  if [ -z "$RENDER_REPORT" ] || [ ! -s "$RENDER_REPORT" ]; then
    bad "the render pass produced NO report — nothing here knows what this plist would contain."
    say "        See the failure above."
    return 1
  fi
  say "        Rendered for this machine (template → installed copy):"
  while IFS="$(printf '\t')" read -r kind a b c; do
    case "$kind" in
      VALUE)      say "          {{$a}} = $b" ;;
      UNRESOLVED) bad "{{$a}} could not be resolved on this machine — see the preflight line that names its override" ;;
      LEFTOVER)   bad "the template still contains $a after rendering — an unknown placeholder; fix the template" ;;
      ERROR)      bad "$a" ;;
    esac
  done < "$RENDER_REPORT"
}

# Print the header block: line 2 up to the first line that is not a comment. The
# boundary is a PROPERTY of the text, not a line number and not a grep target, so
# nothing inserted below can ever truncate --help or spill shell source into it.
usage() {
  awk 'NR==1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
DEST_DIR="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
LOOPS_DIR="$REPO/.claude/loops"
KB="$REPO/.claude/skills/qa-kb/scripts/kb.py"

# The default keeps every existing invocation — README.md, FIRST-RUN.md, muscle memory —
# working with no arguments at all.
DEFAULT_LABEL="com.loops.qa-dream"
LABEL="${LABEL:-$DEFAULT_LABEL}"

MODE="install"
ASSUME_YES=0
# A two-token flag whose value is MISSING must be caught BEFORE the shift. `shift 2` on a
# one-element argv fails with "shift count out of range", there is no `set -e`, and the loop
# then re-enters with exactly the same argv — forever.
while [ $# -gt 0 ]; do
  case "$1" in
    --check|-n)   MODE="check"; shift ;;
    --uninstall)  MODE="uninstall"; shift ;;
    --yes|-y)     ASSUME_YES=1; shift ;;
    --label)      [ $# -ge 2 ] || { echo "install.sh: --label needs a value (e.g. --label com.loops.pr-review)" >&2; exit 2; }
                  LABEL="$2"; shift 2 ;;
    --label=*)    LABEL="${1#*=}"; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "install.sh: unknown flag $1" >&2; exit 2 ;;
  esac
done

case "$LABEL" in
  ''|*/*) echo "install.sh: --label must be a bare launchd label (got '$LABEL')" >&2; exit 2 ;;
esac

TEMPLATE="$HERE/$LABEL.plist.template"
DEST="$DEST_DIR/$LABEL.plist"
# Only name the flag back to the operator when they actually needed it.
LABEL_ARG=""
[ "$LABEL" = "$DEFAULT_LABEL" ] || LABEL_ARG=" --label $LABEL"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }

# ── uninstall ────────────────────────────────────────────────────────────────
if [ "$MODE" = "uninstall" ]; then
  say "Removing $LABEL from $DOMAIN …"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && ok "booted out" || warn "was not loaded (fine)"
  if [ -f "$DEST" ]; then rm -f "$DEST" && ok "deleted $DEST"; else warn "no $DEST to delete"; fi
  say ""
  say "Done. The tracked template at $TEMPLATE is untouched — re-run install.sh to put it back."
  exit 0
fi

# ── preflight: everything the job needs must exist BEFORE it is armed ────────
say "Preflight for $LABEL"
FAILED=0

if [ -f "$TEMPLATE" ]; then
  ok "template present: $TEMPLATE"
else
  bad "missing template: $TEMPLATE"
  say ""
  say "  Agents tracked in this directory:"
  for p in "$HERE"/*.plist.template; do [ -e "$p" ] && say "    $(basename "$p" .plist.template)"; done
  say ""
  say "Preflight FAILED — not installing. The template is the source of every check below it,"
  say "so nothing further was tested."
  exit 1
fi

# ── resolve the machine values the template needs ────────────────────────────
# TWO CLASSES OF VALUE, TREATED DIFFERENTLY ON PURPOSE:
#   * DERIVABLE — the repo root (this script's location) and HOME. Never asked, never wrong.
#   * NOT DERIVABLE — the claude binary, its config dir, and the job PATH. A claude binary is
#     wherever that machine's npm/homebrew/asdf put it; a PATH is a machine's own list.
#     Writing one from a guess would be a GUESS printed as a fact. They are RESOLVED from
#     stack.config `runner.*` (or the LOOP_INSTALL_* override) and then VERIFIED; an
#     unresolvable one fails the preflight and names the override.
R_CLAUDE_BIN="${LOOP_INSTALL_CLAUDE_BIN:-$(stack_cfg runner.claude_bin claude)}"
R_CONFIG_DIR="${LOOP_INSTALL_CONFIG_DIR:-$(stack_cfg runner.config_dir "")}"
[ -n "$R_CONFIG_DIR" ] || R_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
case "$R_CONFIG_DIR" in "~"|"~/"*) R_CONFIG_DIR="$HOME${R_CONFIG_DIR#\~}" ;; esac

# A bare name (the stack.config default is `claude`) is resolved on the OPERATOR's PATH once,
# here, and the absolute result is what gets pinned into the plist — a job must never trust a
# bare `claude`, which on some machines is a shell alias that only prints a hint.
case "$R_CLAUDE_BIN" in
  /*) ;;
  *)  found="$(command -v "$R_CLAUDE_BIN" 2>/dev/null || true)"
      if [ -n "$found" ]; then
        # follow symlinks so a version-manager shim's directory does not end up on the job PATH twice
        R_CLAUDE_BIN="$(cd "$(dirname "$found")" && pwd)/$(basename "$found")"
      else
        R_CLAUDE_BIN=""
      fi ;;
esac

if [ -n "$R_CLAUDE_BIN" ] && [ -x "$R_CLAUDE_BIN" ] && [ -f "$R_CLAUDE_BIN" ]; then
  ok "claude binary resolves: $R_CLAUDE_BIN  (from ${LOOP_INSTALL_CLAUDE_BIN:+LOOP_INSTALL_CLAUDE_BIN}${LOOP_INSTALL_CLAUDE_BIN:-stack.config runner.claude_bin})"
else
  bad "no executable claude binary at '${R_CLAUDE_BIN:-$(stack_cfg runner.claude_bin claude)}' — set stack.config runner.claude_bin to an absolute path, or pass LOOP_INSTALL_CLAUDE_BIN=/path/to/claude"
  FAILED=1
fi
if [ -d "$R_CONFIG_DIR" ]; then
  ok "claude config dir exists: $R_CONFIG_DIR  (from ${LOOP_INSTALL_CONFIG_DIR:+LOOP_INSTALL_CONFIG_DIR}${LOOP_INSTALL_CONFIG_DIR:-stack.config runner.config_dir / CLAUDE_CONFIG_DIR / ~/.claude})"
else
  bad "claude config dir is not a directory: $R_CONFIG_DIR — it is the Claude Code config dir the job reads, so it must already exist; set runner.config_dir or pass LOOP_INSTALL_CONFIG_DIR=<path>"
  FAILED=1
fi

# The job PATH. Overridable; otherwise derived from where the three binaries the job cannot
# do without actually resolve on this machine, plus the standard system directories.
# Deduplicated, order preserved. Every binary below is then re-resolved UNDER this PATH.
if [ -n "${LOOP_INSTALL_PATH:-}" ]; then
  R_PATH="$LOOP_INSTALL_PATH"
  PATH_SRC="LOOP_INSTALL_PATH"
else
  R_PATH=""
  add_dir() { case ":$R_PATH:" in *":$1:"*) ;; *) R_PATH="${R_PATH:+$R_PATH:}$1" ;; esac; }
  [ -n "$R_CLAUDE_BIN" ] && add_dir "$(dirname "$R_CLAUDE_BIN")"
  for b in python3 git; do
    f="$(command -v "$b" 2>/dev/null || true)"; [ -n "$f" ] && add_dir "$(dirname "$f")"
  done
  for d in /opt/homebrew/bin /opt/homebrew/sbin /usr/local/bin /usr/bin /bin /usr/sbin /sbin; do
    [ -d "$d" ] && add_dir "$d"
  done
  PATH_SRC="derived (claude, python3, git locations + system dirs)"
fi
ok "job PATH: $R_PATH  ($PATH_SRC)"

# ── render the copy this machine would actually install ──────────────────────
# EVERY check below reads $SRC, the RENDERED plist: a preflight that verified the template
# and then installed something else would be testing an intention.
SRC=""
RENDER_REPORT=""
WORK_DIR="$(mktemp -d -t loops-scheduler-install)" || WORK_DIR=""
if [ -z "$WORK_DIR" ]; then
  bad "mktemp -d failed — cannot render the plist, and nothing unrendered is ever installed. Refusing to guess."
  FAILED=1
else
  trap 'rm -rf "$WORK_DIR"' EXIT
  RENDER_REPORT="$WORK_DIR/render.txt"
  RT_RC=0
  render_template "$TEMPLATE" "$WORK_DIR/$LABEL.plist" "$RENDER_REPORT" || RT_RC=$?
  # The COUNT line is the LAST thing the pass writes, so its presence is the proof the pass
  # ran to the end — a report truncated by a crash mid-write cannot fake it.
  if [ -s "$RENDER_REPORT" ] && grep -q '^COUNT' "$RENDER_REPORT" 2>/dev/null && [ "$RT_RC" -eq 0 ] && [ -f "$WORK_DIR/$LABEL.plist" ]; then
    SRC="$WORK_DIR/$LABEL.plist"
    ok "rendered: $SRC"
    print_render_report
  else
    FAILED=1
    [ -s "$RENDER_REPORT" ] && print_render_report
    bad "the render pass did not complete (rc $RT_RC) — nothing is installed from a template this"
    say "        script could not fully render. It is a python3 pass: \`python3 -c 'import plistlib'\`"
    say "        must work in THIS shell (not just under the job PATH — the checks below test that one)."
  fi
fi

# Stop here if there is no rendered plist. Everything below READS it, and PlistBuddy answers
# a missing file on STDOUT with "File Doesn't Exist, Will Create: …" — which sails straight
# into $SCRIPT and $JOB_WD and produces a page of confident nonsense. One honest failure
# beats twelve invented ones.
if [ -z "$SRC" ]; then
  say ""
  say "Preflight FAILED — not installing. Fix the items above and re-run."
  exit 1
fi

if plutil -lint "$SRC" >/dev/null 2>&1; then
  ok "plutil -lint clean (rendered copy)"
else
  bad "plutil -lint FAILED on the rendered copy:"; plutil -lint "$SRC"; FAILED=1
fi
# plistlib is stricter than plutil (it rejects `--` inside an XML comment, for one), and it
# is what describe_schedule / job_command parse with — so a plist plutil likes and plistlib
# does not would print "see … in the plist" for every derived line. Catch it here, by name.
if python3 -c 'import plistlib,sys; plistlib.load(open(sys.argv[1],"rb"))' "$SRC" 2>/dev/null; then
  ok "plistlib parses it"
else
  bad "python plistlib CANNOT parse the rendered plist (a double hyphen inside an XML comment is the usual cause) — every derived line below would be a shrug"
  FAILED=1
fi

# Pull the real values out of the rendered plist rather than re-typing them here — the plist
# is the single source of truth, so a preflight that hard-coded paths could pass while the
# plist pointed somewhere else.
read_plist() { /usr/libexec/PlistBuddy -c "Print :$1" "$SRC" 2>/dev/null; }

# THE LABEL INSIDE THE FILE IS THE ONE LAUNCHD USES. `--label` only picks which template to
# read; `launchctl bootstrap` registers the job under the plist's own Label key. When the two
# disagree, every command this installer prints — `launchctl print`, `launchctl list | grep`,
# the bootout-before-bootstrap below, and `--uninstall` — targets a label that does not exist,
# and the operator is left with an hourly job the installer cannot remove. That is the
# copy-paste error the next agent added to this directory will make, so check it here.
PLIST_LABEL="$(read_plist 'Label')"
if [ "$PLIST_LABEL" = "$LABEL" ]; then
  ok "plist Label matches the filename: $PLIST_LABEL"
else
  bad "LABEL MISMATCH — the template is $LABEL.plist.template but its Label key says '${PLIST_LABEL:-<missing>}'."
  say "        launchd would register the job as '${PLIST_LABEL:-<missing>}', while every command"
  say "        below (and --uninstall) would target '$LABEL'. Fix one of them so they agree."
  FAILED=1
fi

SCRIPT="$(read_plist 'ProgramArguments:1')"
JOB_PATH="$(read_plist 'EnvironmentVariables:PATH')"
JOB_HOME="$(read_plist 'EnvironmentVariables:HOME')"
JOB_CLAUDE="$(read_plist 'EnvironmentVariables:LOOP_CLAUDE_BIN')"
JOB_CONFIG="$(read_plist 'EnvironmentVariables:CLAUDE_CONFIG_DIR')"
JOB_WD="$(read_plist 'WorkingDirectory')"
JOB_OUT_LOG="$(read_plist 'StandardOutPath')"
JOB_ERR_LOG="$(read_plist 'StandardErrorPath')"
# The runner's own name, so the lines below name the real script instead of hard-coding
# one runner's name into an installer that arms several.
RUNNER_NAME="$(basename "${SCRIPT:-the runner}")"

# The runner-written one-line-per-event summary log, derived from the launchd transcript
# path by the house convention `<job>.launchd.{out,err}.log` + `<job>.log` (JOB-CONTRACT.md).
case "$JOB_OUT_LOG" in
  *.launchd.out.log) JOB_SUMMARY_LOG="${JOB_OUT_LOG%.launchd.out.log}.log" ;;
  *)                 JOB_SUMMARY_LOG="" ;;
esac
JOB_ARTIFACT_DIR=""       # sidecar: where the job drops its per-run reports
JOB_EXTRA_BINS=""         # sidecar: binaries beyond python3/git/bash this job needs
JOB_KICKSTART_WARNING="this runs the job for real, right now"
JOB_GATE_NOTE=""          # sidecar: an extra paragraph for the human gate

# ── per-job sidecar, if this label has one ───────────────────────────────────
# Sourced AFTER the plist reads so job_preflight() can use them. A missing sidecar is
# the generic behaviour, never a failure — that is what keeps this file label-agnostic.
# A sidecar is configuration, not a check, so loading one prints nothing: its effect is
# visible in the extra checks it adds and the epilogue it writes. Failing to load is
# loud, because then the job would be armed with the generic assumptions instead.
JOB_SIDECAR="$HERE/$LABEL.job.sh"
if [ -f "$JOB_SIDECAR" ]; then
  # shellcheck disable=SC1090
  . "$JOB_SIDECAR" || { bad "sidecar failed to load: $JOB_SIDECAR"; FAILED=1; }
fi

if [ -f "$SCRIPT" ]; then ok "$RUNNER_NAME present: $SCRIPT"; else bad "ProgramArguments points at a missing script: $SCRIPT"; FAILED=1; fi
if bash -n "$SCRIPT" 2>/dev/null; then ok "$RUNNER_NAME parses"; else bad "$RUNNER_NAME has a syntax error"; FAILED=1; fi

# The shared cross-agent lock library. Every armed job hard-requires it — each refuses to
# start (exit 23) rather than run unserialized against the others — so a missing file here
# means every future run of this job is a no-op that only the summary log would record.
# Checked at arm time, where a human is reading, instead of at the scheduled hour, where
# nobody is.
AGENT_LOCK_LIB="$LOOPS_DIR/lib/agent_lock.sh"
if [ -r "$AGENT_LOCK_LIB" ]; then
  if bash -n "$AGENT_LOCK_LIB" 2>/dev/null; then ok "shared agent lock library present and parses"
  else bad "shared agent lock library has a syntax error: $AGENT_LOCK_LIB"; FAILED=1; fi
else
  bad "shared agent lock library missing: $AGENT_LOCK_LIB — every run of this job would refuse (exit 23)"; FAILED=1
fi

# A launchd job is a PERMANENT, machine-level reference to a PATH. If the file at that path
# is untracked, a `git clean -fdx`, a fresh clone, a checkout that drops untracked files —
# any of them — leaves launchd spawning /bin/bash on a path that no longer exists. bash
# exits 127, the runner never starts, so the summary log gets NO line at all and the only
# trace is a LastExitStatus nobody reads. It also means the runner that holds a vcs_host
# credential has never been through review and cannot be reverted. Warn by name.
UNTRACKED=""
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  for f in "$TEMPLATE" "$SCRIPT" "$HERE/$LABEL.job.sh"; do
    [ -e "$f" ] || continue
    git -C "$REPO" ls-files --error-unmatch "$f" >/dev/null 2>&1 || UNTRACKED="$UNTRACKED $f"
  done
  if [ -z "$UNTRACKED" ]; then
    ok "template, runner and sidecar are all tracked in git"
  else
    warn "NOT TRACKED IN GIT — arming binds a scheduled job to files that exist only in this working tree:"
    for f in $UNTRACKED; do say "          $f"; done
    say "        A \`git clean -fdx\`, a fresh clone, or a checkout that drops untracked files"
    say "        turns every future run into /bin/bash on a missing path: exit 127, and NOT ONE"
    say "        LINE in the summary log, because the runner never starts. Commit them first."
  fi
else
  warn "$REPO is not a git checkout — cannot tell whether the armed files are tracked"
fi
if [ -d "$JOB_WD" ]; then ok "WorkingDirectory exists: $JOB_WD"; else bad "WorkingDirectory missing: $JOB_WD"; FAILED=1; fi
if [ -d "$JOB_HOME" ]; then ok "HOME exists: $JOB_HOME"; else bad "HOME missing: $JOB_HOME"; FAILED=1; fi
if [ -d "$JOB_CONFIG" ]; then ok "CLAUDE_CONFIG_DIR exists: $JOB_CONFIG"; else bad "CLAUDE_CONFIG_DIR missing: $JOB_CONFIG"; FAILED=1; fi
if [ -x "$JOB_CLAUDE" ]; then ok "LOOP_CLAUDE_BIN is executable: $JOB_CLAUDE"; else bad "LOOP_CLAUDE_BIN not executable: $JOB_CLAUDE"; FAILED=1; fi

# Which extra binary the BRAIN's auth mode shells out to, if any. kb.py swallows a
# FileNotFoundError from that CLI, so one missing from the JOB's PATH is a silent auth
# failure at the scheduled hour, not a loud one. Read the mode out of kb.config rather
# than assuming any particular cloud.
KB_AUTH_MODE="$(kb_cfg auth.mode password)"
BRAIN_BINS=""
case "$KB_AUTH_MODE" in
  token_command) BRAIN_BINS="$(kb_cfg auth.command "" | python3 -c 'import json,sys
v=sys.stdin.read().strip()
try:
    o=json.loads(v)
    print(o[0] if isinstance(o,list) and o else str(o).split()[0] if o else "")
except Exception:
    print(v.split()[0] if v else "")' 2>/dev/null)" ;;
esac
[ -n "$BRAIN_BINS" ] && say "        brain auth mode is '$KB_AUTH_MODE' — it shells out to \`$BRAIN_BINS\`, so that is checked under the job PATH too"

# The whole point of the explicit PATH: resolve each binary USING THE JOB'S PATH, not yours.
# `hash -r` FIRST, inside the substitution's own subshell: bash answers `command -v` out of
# its hash table before it searches a PATH, and this script has already RUN python3 by the
# time it gets here. Without the flush the probe reports whichever python3 the OPERATOR's
# PATH resolved under a line that says "resolves under the job PATH".
for bin in python3 git bash $BRAIN_BINS $JOB_EXTRA_BINS; do
  found="$(hash -r 2>/dev/null; PATH="$JOB_PATH" command -v "$bin" 2>/dev/null || true)"
  if [ -n "$found" ]; then ok "\`$bin\` resolves under the job PATH → $found"
  else bad "\`$bin\` DOES NOT resolve under the job PATH ($JOB_PATH) — pass LOOP_INSTALL_PATH=<path> that includes it"; FAILED=1; fi
done

# The brain's Python driver. It may live in a USER site-dir under HOME (a `pip install --user`),
# so prove the JOB's python3 under the JOB's HOME can import it, not yours under yours.
if PATH="$JOB_PATH" HOME="$JOB_HOME" python3 -c 'import psycopg2, pgvector.psycopg2' 2>/dev/null; then
  ok "python3 can import psycopg2 + pgvector under the job PATH+HOME"
else
  bad "python3 CANNOT import psycopg2 / pgvector under the job PATH+HOME — the brain would be dead (kb.py imports both at startup)"
  FAILED=1
fi

# Finding the binaries is not the same as the brain ANSWERING. Both runners gate on
# `kb pause --status` returning ok:true (exit 20 otherwise), so run that exact call under the
# job's PATH and HOME — the credential cache or secret env file a configured auth mode reads
# lives under HOME. Expired credentials are far better read here, while a human is watching,
# than at the scheduled hour from a job that has nobody to tell.
if [ -f "$KB" ]; then
  BRAIN_STATUS="$(PATH="$JOB_PATH" HOME="$JOB_HOME" python3 "$KB" pause --status 2>/dev/null | python3 -c '
import json,sys
try:
    o=json.load(sys.stdin)
    print("ok" if o.get("ok") is True else "down", "paused" if o.get("paused") else "unpaused")
except Exception:
    print("down unknown")' 2>/dev/null)"
  case "$BRAIN_STATUS" in
    "ok paused")   warn "brain answers under the job PATH+HOME, and the R9 pause flag is ON — the job will arm, and every run will exit 0 having done nothing until \`kb pause --off\`" ;;
    "ok unpaused") ok "brain answers under the job PATH+HOME (kb pause --status → ok:true, paused:false)" ;;
    *)             bad "the brain does NOT answer under the job PATH+HOME (kb pause --status did not return ok:true) — every run would exit 20. Check kb.config.json, its auth mode ('$KB_AUTH_MODE') and the secret env file; see docs/brain-setup.md"
                   FAILED=1 ;;
  esac
else
  bad "kb.py not found at $KB — the runners cannot gate on the brain"; FAILED=1
fi

if [ -d "$LOOPS_DIR" ]; then ok "loops dir present: $LOOPS_DIR"; else bad "missing $LOOPS_DIR"; FAILED=1; fi

# ── whatever else THIS job needs, from its sidecar ───────────────────────────
if command -v job_preflight >/dev/null 2>&1; then
  job_preflight || FAILED=1
fi

say ""
if [ "$FAILED" -ne 0 ]; then
  say "Preflight FAILED — not installing. Fix the items above and re-run."
  exit 1
fi
say "Preflight clean."

# ── what is armed RIGHT NOW, as opposed to what would be installed ───────────
# Everything above reads $SRC, the rendered copy. The job launchd actually runs is $DEST,
# and nothing else reads it — so a hand edit to ~/Library/LaunchAgents (adding --live, say)
# would otherwise leave `--check` printing a green "posting posture: DRY-RUN" for a job that
# posts to real pull requests every hour. --check must describe the ARMED job, or it is
# describing an intention. The comparison is against the RENDERED copy, which is what this
# machine would install, so the placeholders never show up as a false "someone edited it".
describe_armed() {
  if [ ! -f "$DEST" ]; then
    say "  armed   : NOT INSTALLED — no $DEST. Nothing is running this label from a plist."
    return 0
  fi
  if diff -q <(plutil -p "$SRC" 2>/dev/null) <(plutil -p "$DEST" 2>/dev/null) >/dev/null 2>&1; then
    say "  armed   : identical to what this machine would install from the tracked template"
  else
    warn "ARMED COPY DIFFERS FROM WHAT THIS MACHINE WOULD INSTALL — launchd is running the armed one:"
    say "            template: $TEMPLATE"
    say "            armed   : $DEST"
    say "        Someone edited the installed copy, the template changed since the last install,"
    say "        or a render value (claude binary, config dir, PATH) changed. Diff them, then"
    say "        re-install to make the template authoritative:"
    say "            diff <(plutil -p $DEST) <(bash $HERE/install.sh$LABEL_ARG --check >/dev/null; plutil -p $SRC)"
    say "  armed schedule: $(describe_schedule "$DEST")"
    say "  armed command : $(job_command "$DEST")"
    if job_command "$DEST" | grep -q -- '--live'; then
      warn "THE ARMED COPY POSTS LIVE (--live is in ITS ProgramArguments) whatever the template says."
    fi
  fi
}

if [ "$MODE" = "check" ]; then
  say ""
  say "Would arm : $LABEL   (plist Label: ${PLIST_LABEL:-<missing>})"
  say "  schedule: $(describe_schedule "$SRC")"
  say "  command : $(job_command "$SRC")"
  say "  source  : $TEMPLATE, rendered for this machine (values listed above)"
  describe_armed
  say ""
  say "--check only: nothing was copied, nothing was bootstrapped."
  exit 0
fi

# ── the human gate ───────────────────────────────────────────────────────────
if [ "$ASSUME_YES" -ne 1 ]; then
  if [ ! -t 0 ]; then
    say ""
    say "REFUSING to install from a non-interactive shell without --yes."
    say "Arming $LABEL starts unattended brain writes and model consumption — that is"
    say "the operator's call, not a script's. Re-run in your own terminal:"
    say "    bash $HERE/install.sh$LABEL_ARG --yes"
    [ -n "$JOB_GATE_NOTE" ] && { say ""; say "$JOB_GATE_NOTE"; }
    exit 2
  fi
  say ""
  say "About to install $LABEL into $DOMAIN:"
  say "    schedule : $(describe_schedule "$SRC")"
  say "    command  : /bin/bash $(job_command "$SRC")"
  say "    logs     : ${JOB_SUMMARY_LOG:-see StandardOutPath in the plist} (summary)"
  say "               $JOB_OUT_LOG"
  say "               $JOB_ERR_LOG"
  say "    source   : $TEMPLATE, rendered for this machine. The values again, since this is"
  say "               the moment you are deciding:"
  print_render_report
  if [ -n "$UNTRACKED" ]; then
    say ""
    say "    !! NOT IN GIT — this scheduled job will point at files that exist only here:"
    for f in $UNTRACKED; do say "         $f"; done
    say "    !! One \`git clean -fdx\` and every future run is exit 127 with no log line."
  fi
  [ -n "$JOB_GATE_NOTE" ] && { say ""; say "$JOB_GATE_NOTE"; }
  printf 'Type YES to proceed: '
  read -r reply
  [ "$reply" = "YES" ] || { say "Aborted — nothing changed."; exit 0; }
fi

# ── install ──────────────────────────────────────────────────────────────────
mkdir -p "$DEST_DIR" || { bad "cannot create $DEST_DIR"; exit 1; }
cp "$SRC" "$DEST" || { bad "cannot copy to $DEST"; exit 1; }
ok "installed → $DEST   (rendered from $TEMPLATE; never byte-identical to it)"
print_render_report

# bootout first so a re-install picks up an edited template (bootstrap on an already-loaded
# label is an error, not an update).
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && ok "booted out the previous copy" || true

if launchctl bootstrap "$DOMAIN" "$DEST"; then
  ok "bootstrapped $LABEL into $DOMAIN"
else
  bad "launchctl bootstrap failed (rc $?)"
  say "  If it says 'Load failed: 5: Input/output error', check Console.app for the"
  say "  real reason, or re-run after 'launchctl bootout $DOMAIN/$LABEL'."
  exit 1
fi

cat <<EOF

Installed. RunAtLoad is false, so NOTHING has run yet — the first run is $(describe_schedule "$SRC").

Verify it is armed:
    launchctl print $DOMAIN/$LABEL | sed -n '1,25p'
    launchctl list | grep $LABEL

Run one now, on purpose ($JOB_KICKSTART_WARNING):
    launchctl kickstart -k $DOMAIN/$LABEL

Watch what it did:
    tail -n 20 ${JOB_SUMMARY_LOG:-$JOB_OUT_LOG}
    tail -n 60 $JOB_ERR_LOG
EOF
[ -n "$JOB_ARTIFACT_DIR" ] && say "    ls -t $JOB_ARTIFACT_DIR/ | head"

cat <<EOF

Disable without uninstalling (R9 kill switch — every runner fails closed on it):
    python3 $KB pause --on --by "\$USER" --reason "…"
    python3 $KB pause --off --by "\$USER"
EOF

command -v job_epilogue >/dev/null 2>&1 && job_epilogue

cat <<EOF

Remove entirely:
    bash $HERE/install.sh$LABEL_ARG --uninstall
EOF
