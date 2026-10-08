#!/usr/bin/env bash
# infra-ship preflight — the SAFE, testable gate that runs BEFORE any push / PR / deploy.
# Checks the R9 kill-switch (qa-kb global_pause), the base/protected-branch guard, the prod-lane
# branch guard, the R6 blast-radius denylist (diff scan — the LAST mechanical check before
# anything ships), derives the commit type from the branch prefix, and previews the files that
# would be committed (modified + staged + untracked) — WITHOUT pushing, creating a PR, or
# deploying. Prints one JSON object.
#
# Usage: ship_preflight.sh [--repo-dir DIR] [--base BRANCH]
#   --base defaults to stack.config `project.default_branch`.
# Config (stack.config.json, read through lib/adapters/config.py; env overrides LOOPS_CFG__*):
#   project.default_branch              the PR base; shipping FROM it is refused
#   project.protected_branch_patterns   globs that are never pushed by a loop (refused like the base)
#   deploy.prod_lane_branch_patterns    globs whose push IS a production deploy in the target repo
#                                       (default [] — no branch name deploys anything)
#   project.branch_prefixes             {ticket_type: "prefix/"} — commit type derives from it
# Env:   SHIP_DRY_RUN=1                 no-PR rehearsal (dry_run:true); PRs open live otherwise
#        SHIP_PROD_LANE_OK=<branch>     per-run human override for a prod-lane branch (see 4)
#        SHIP_SCAFFOLD_PATHS            newline- or colon-separated repo-relative paths the sandbox
#                                       provisioner rewrites in every worktree; excluded from the R6
#                                       scan ONLY inside a linked worktree (see 5)
#        LOOPS_REPO_ROOT                this framework's checkout (needed when this script runs
#                                       from a copy, e.g. a test tree); else found by walking up
# Exit codes (every abort is NON-ZERO so `&&`-composing callers stop):
#   0 ok · 1 bad-repo-dir · 2 R9 pause (set or unreadable) · 3 on-base-branch (base or protected)
#   4 R6 denylist hit · 5 R6 scan failed (fail closed) · 6 nothing-to-ship
#   7 prod-lane-branch (pushing this branch — or where git config would push it — IS a prod deploy)
# Every string that can carry user/git-controlled text (branch, file lists) goes through jstr —
# git allows '"' in a branch name, and an unescaped one turns the one JSON line into garbage.
set -uo pipefail

jstr(){ python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$1"; }
lower(){ printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KB="$SCRIPT_DIR/../../qa-kb/scripts/kb.py"   # call kb.py with python3 (NOT the bash wrapper)
R6SCAN="$SCRIPT_DIR/../../dev-fix/scripts/r6_denylist_scan.sh"
REPO_DIR="."; BASE_ARG=""
while [ $# -gt 0 ]; do case "$1" in
  --repo-dir) REPO_DIR="${2:-.}"; shift 2;;
  --base)     BASE_ARG="${2:-}"; shift 2;;
  *) shift;;
esac; done

# 0. Stack config — the ONLY source of branch names and lane patterns (no literal `main`).
# The framework root is $LOOPS_REPO_ROOT, else the nearest ancestor holding lib/adapters/config.py.
FW_ROOT="${LOOPS_REPO_ROOT:-}"
if [ -z "$FW_ROOT" ]; then
  d="$SCRIPT_DIR"
  while [ "$d" != "/" ] && [ -n "$d" ]; do
    if [ -f "$d/lib/adapters/config.py" ]; then FW_ROOT="$d"; break; fi
    d="$(dirname "$d")"
  done
fi
CFG_JSON=$(python3 - "$FW_ROOT" <<'PY' 2>/dev/null
import json, sys
root = sys.argv[1]
out = {"source": "defaults", "default_branch": "main", "protected": [], "prod_lanes": [], "branch_prefixes": {}}
try:
    if root:
        sys.path.insert(0, root)
    from lib.adapters.config import load_config
    c = load_config()
    out["source"] = str(c.path)
    out["default_branch"] = str(c.get("project.default_branch") or "main")
    out["protected"] = [str(p) for p in (c.get("project.protected_branch_patterns") or []) if p]
    out["prod_lanes"] = [str(p) for p in (c.get("deploy.prod_lane_branch_patterns") or []) if p]
    bp = c.get("project.branch_prefixes") or {}
    out["branch_prefixes"] = {str(k): str(v) for k, v in bp.items() if v} if isinstance(bp, dict) else {}
except Exception as exc:  # no config reachable: schema defaults, and say so
    out["error"] = str(exc)
print(json.dumps(out))
PY
)
[ -n "$CFG_JSON" ] || CFG_JSON='{"source":"defaults","default_branch":"main","protected":[],"prod_lanes":[],"branch_prefixes":{}}'
cfg_field(){ printf '%s' "$CFG_JSON" | python3 -c "import sys,json;v=json.load(sys.stdin).get('$1');print(v if isinstance(v,str) else json.dumps(v))"; }
cfg_lines(){ printf '%s' "$CFG_JSON" | python3 -c "
import sys,json;v=json.load(sys.stdin).get('$1') or []
for x in (v if isinstance(v,list) else ['%s=%s'%(k,v[k]) for k in v]): print(x)"; }
CONFIG_SOURCE=$(cfg_field source)
BASE="${BASE_ARG:-$(cfg_field default_branch)}"
PROTECTED=(); PROD_LANES=(); PREFIX_MAP=()
while IFS= read -r l; do [ -n "$l" ] && PROTECTED+=("$l"); done < <(cfg_lines protected)
while IFS= read -r l; do [ -n "$l" ] && PROD_LANES+=("$l"); done < <(cfg_lines prod_lanes)
while IFS= read -r l; do [ -n "$l" ] && PREFIX_MAP+=("$l"); done < <(cfg_lines branch_prefixes)

cd "$REPO_DIR" 2>/dev/null || { echo '{"ok":false,"abort":"bad-repo-dir"}'; exit 1; }

# 1. R9 kill-switch — the most important gate. Refuse to ship if the brain's global_pause is set.
PAUSED=$(python3 "$KB" pause --status 2>/dev/null \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('control',{}).get('value'))" 2>/dev/null)
# Fail CLOSED: ship ONLY on an explicit not-paused. "True" = paused; anything else = couldn't verify → refuse.
if [ "$PAUSED" != "False" ]; then
  if [ "$PAUSED" = "True" ]; then
    echo '{"ok":false,"abort":"global-pause","detail":"qa-kb global_pause is SET (R9) — refusing to ship. Escalate via dev-escalate."}'
  else
    echo '{"ok":false,"abort":"pause-check-failed","detail":"could not read qa-kb global_pause (R9) — failing CLOSED, refusing to ship."}'
  fi
  exit 2
fi

# 2. Base / protected-branch guard (never ship FROM the base branch or a protected one).
BRANCH=$(git branch --show-current 2>/dev/null)
BRANCH_L=$(lower "$BRANCH")
PROTECTED_BY=""
if [ -z "$BRANCH" ] || [ "$BRANCH_L" = "$(lower "$BASE")" ]; then
  PROTECTED_BY="$BASE"
else
  for pat in ${PROTECTED[@]+"${PROTECTED[@]}"}; do
    p=$(lower "$pat"); p="${p#refs/heads/}"
    # shellcheck disable=SC2254  # unquoted on purpose: $p is a glob
    case "$BRANCH_L" in $p) PROTECTED_BY="$pat"; break;; esac
  done
fi
if [ -n "$PROTECTED_BY" ]; then
  printf '{"ok":false,"abort":"on-base-branch","branch":%s,"base":%s,"protected_by":%s,"detail":"a loop never ships from the base branch or a protected branch (project.default_branch / project.protected_branch_patterns)."}\n' \
    "$(jstr "$BRANCH")" "$(jstr "$BASE")" "$(jstr "$PROTECTED_BY")"
  exit 3
fi

# 3. Staging posture: read from the brain's staging_only flag — never a hardcode.
# FAIL CLOSED: anything but an explicit False (a human ran `kb flag --off`) means staging posture.
# Read here, before the prod-lane guard, because that guard is the one place it refuses anything.
STAGING=$(python3 "$KB" flag --name staging_only --status 2>/dev/null \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('value'))" 2>/dev/null)
if [ "$STAGING" = "False" ]; then STAGING_ONLY=false; else STAGING_ONLY=true; fi

# staging_only gates the PROD DEPLOY. It does NOT gate opening a PR, and it does NOT set dry_run.
#
# Conflating the two means the loop can never open a pull request no matter how good the fix
# is — every autonomous run dies at the last step and hands a human a branch to push by hand,
# which defeats the point of the loop. A PR is not a deploy: it is the human review gate —
# required reviewers and branch protection decide when it completes. Opening one is how a human
# gets to see the work at all.
#
# So: dry_run (the PR) is its own switch, defaulting to LIVE, and prod_deploy_allowed stays
# bound to staging_only and remains false until R2/R5/R6/R9 are verified. To go back to a
# no-PR rehearsal posture, export SHIP_DRY_RUN=1 — an explicit, per-run opt-in.
if [ "${SHIP_DRY_RUN:-0}" = "1" ]; then DRYRUN=true; else DRYRUN=false; fi
PROD_DEPLOY_ALLOWED=false
if [ "$STAGING_ONLY" = "false" ]; then PROD_DEPLOY_ALLOWED=true; fi

# 4. Prod-lane guard — the one place prod_deploy_allowed REFUSES something. Some repos deploy on
# a branch NAME alone: a CD trigger on `release/*` (or whatever the adopter configured) with no PR
# in between, so `git push` of such a branch IS the production deploy this skill must never do on
# its own. Those names live in stack.config `deploy.prod_lane_branch_patterns`; an empty list
# (the default) means no push is a deploy and this guard is inert. Humans may cut lane branches by
# hand — this only stops infra-ship from being the one that pushes. Proceeding takes BOTH keys:
# the brain out of staging posture (prod_deploy_allowed) AND a per-run human override naming this
# exact branch (SHIP_PROD_LANE_OK=<branch> — no globs, no reuse across branches). Matched
# case-insensitively: fail closed on a Release/ that a trigger might still treat as release/.
lane_of(){ # the matched lane's first path segment when a ref (optional +, optional refs/heads/, any case) is a lane
  local r p pat; r=$(lower "$1"); r="${r#+}"; r="${r#refs/heads/}"
  for pat in ${PROD_LANES[@]+"${PROD_LANES[@]}"}; do
    p=$(lower "$pat"); p="${p#refs/heads/}"
    # shellcheck disable=SC2254  # unquoted on purpose: $p is a glob
    case "$r" in $p) echo "${p%%/*}"; return 0;; esac
  done; }
LANE=$(lane_of "$BRANCH"); LANE_VIA=branch-name; LANE_TARGET="$BRANCH"
# 4a. The local NAME is not the whole story: git config can land `git push -u <remote> fix/x` on a
# lane ref. Checked, deliberately coarse (any hit refuses — fail closed, no refspec evaluation):
#   - a push refspec (remote.<r>.push) whose destination is a lane ref, for the push remote and
#     for any remote this branch could push to instead (branch.<b>.pushRemote, remote.pushDefault)
#     — a refspec without ':' pushes to its own name, so its source counts as the destination;
#   - an upstream (branch.<b>.merge) that is a lane ref (push.default=upstream pushes there).
# The per-run override does NOT unlock these: it names a branch, and a config remap is not a
# branch a human chose to push — remove the mapping instead.
PUSH_REMOTE=$(git config --get "branch.$BRANCH.pushRemote" 2>/dev/null || git config --get remote.pushDefault 2>/dev/null || echo origin)
[ -n "$PUSH_REMOTE" ] || PUSH_REMOTE=origin
if [ -z "$LANE" ]; then
  PUSH_REMOTES=$(printf '%s\n' origin "$PUSH_REMOTE" "$(git config --get "branch.$BRANCH.pushRemote" 2>/dev/null)" \
                   "$(git config --get remote.pushDefault 2>/dev/null)" | grep -v '^$' | sort -u)
  while IFS= read -r REM; do
    while IFS= read -r SPEC; do
      [ -n "$SPEC" ] || continue
      L=$(lane_of "${SPEC##*:}")
      if [ -n "$L" ]; then LANE=$L; LANE_VIA="remote.$REM.push"; LANE_TARGET="$SPEC"; break 2; fi
    done < <(git config --get-all "remote.$REM.push" 2>/dev/null)
  done <<< "$PUSH_REMOTES"
fi
if [ -z "$LANE" ]; then
  UPSTREAM=$(git config --get "branch.$BRANCH.merge" 2>/dev/null)
  L=$(lane_of "$UPSTREAM")
  if [ -n "$L" ]; then LANE=$L; LANE_VIA="branch.$BRANCH.merge"; LANE_TARGET="$UPSTREAM"; fi
fi
PROD_LANE=false; PROD_LANE_OVERRIDE=null
if [ -n "$LANE" ]; then
  PROD_LANE=true
  OVR="${SHIP_PROD_LANE_OK:-}"
  OVR_JSON=$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]) if sys.argv[1] else "null")' "$OVR")
  if [ "$LANE_VIA" = "branch-name" ]; then
    DETAIL="pushing a $LANE/* branch is a production deploy in the target repo (deploy.prod_lane_branch_patterns) — infra-ship never pushes one on its own. Ship from a loop branch (project.branch_prefixes); proceeding needs staging_only off AND SHIP_PROD_LANE_OK set to this exact branch by a human, for this run."
  else
    DETAIL="git config $LANE_VIA maps this push onto a $LANE/* ref ($LANE_TARGET), which is a production deploy in the target repo (deploy.prod_lane_branch_patterns) — infra-ship never pushes one. Remove that mapping; SHIP_PROD_LANE_OK does not unlock a config-mapped lane push."
  fi
  if [ "$LANE_VIA" != "branch-name" ] || [ "$PROD_DEPLOY_ALLOWED" != "true" ] || [ "$OVR" != "$BRANCH" ]; then
    printf '{"ok":false,"abort":"prod-lane-branch","branch":%s,"lane":%s,"via":%s,"target":%s,"staging_only":%s,"prod_deploy_allowed":%s,"override":%s,"detail":%s}\n' \
      "$(jstr "$BRANCH")" "$(jstr "$LANE")" "$(jstr "$LANE_VIA")" "$(jstr "$LANE_TARGET")" "$STAGING_ONLY" \
      "$PROD_DEPLOY_ALLOWED" "$OVR_JSON" "$(jstr "$DETAIL")"
    exit 7
  fi
  PROD_LANE_OVERRIDE="$OVR_JSON"
fi

# 5. R6 blast-radius denylist — scan EVERYTHING that would ship (unstaged+staged+untracked).
# This is the last mechanical gate before commit/push/PR; a confused or prompt-injected fix that
# touched a migration/CI/secret/auth/IaC path stops HERE.
if [ ! -x "$R6SCAN" ] && [ ! -f "$R6SCAN" ]; then
  echo '{"ok":false,"abort":"r6-scan-failed","detail":"r6_denylist_scan.sh not found — failing CLOSED, refusing to ship."}'
  exit 5
fi
# SANDBOX SCAFFOLDING: a sandbox provisioner may rewrite a few environment files in every
# worktree it provisions (a port offset, a cloned DB name). They are dirty in EVERY loop worktree
# BY CONSTRUCTION and are never part of a fix — but such files often match the R6 denylist
# (app settings, launch config), so an unfiltered scan would abort EVERY autonomous ship with
# r6-denylist. The recipe names them in SHIP_SCAFFOLD_PATHS (repo-relative, newline- or
# colon-separated; empty = exclude nothing). Excluded ONLY inside a sandbox worktree: in the
# developer's real checkout no exclusion applies and a genuine settings change still trips R6.
# Detected via git's worktree marker, not a path guess — a linked worktree has a .git FILE.
SCAFFOLD_RE=$(printf '%s' "${SHIP_SCAFFOLD_PATHS:-}" | tr ':' '\n' | grep -v '^$' \
  | python3 -c 'import re,sys;ps=[l.rstrip("\n") for l in sys.stdin if l.strip()];print("^(" + "|".join(re.escape(p) for p in ps) + ")$" if ps else "")')
IS_SANDBOX=false
if [ -f .git ] && grep -q '^gitdir:' .git 2>/dev/null; then IS_SANDBOX=true; fi

SHIP_FILES=$( { git diff --name-only HEAD 2>/dev/null; git diff --name-only --cached 2>/dev/null; \
                git ls-files --others --exclude-standard 2>/dev/null; } | sort -u | grep -v '^$' )
EXCLUDED_SCAFFOLD=""
if [ "$IS_SANDBOX" = "true" ] && [ -n "$SCAFFOLD_RE" ]; then
  EXCLUDED_SCAFFOLD=$(printf '%s\n' "$SHIP_FILES" | grep -E "$SCAFFOLD_RE" | paste -sd, -)
  SHIP_FILES=$(printf '%s\n' "$SHIP_FILES" | grep -vE "$SCAFFOLD_RE")
fi

# An empty working set is NOT necessarily nothing to ship: the loop commits to the branch
# before teardown (so a crash can never lose the fix), which leaves the tree clean while the
# branch still carries commits to push and PR. Only abort when there is neither.
AHEAD=0
if git rev-parse --verify --quiet "refs/remotes/$PUSH_REMOTE/$BRANCH" >/dev/null 2>&1; then
  AHEAD=$(git rev-list --count "refs/remotes/$PUSH_REMOTE/$BRANCH".."$BRANCH" 2>/dev/null || echo 0)
else
  BASE_REF=""
  if git rev-parse --verify --quiet "refs/remotes/$PUSH_REMOTE/$BASE" >/dev/null 2>&1; then BASE_REF="refs/remotes/$PUSH_REMOTE/$BASE";
  elif git rev-parse --verify --quiet "refs/heads/$BASE" >/dev/null 2>&1; then BASE_REF="refs/heads/$BASE"; fi
  [ -n "$BASE_REF" ] && AHEAD=$(git rev-list --count "$BASE_REF".."$BRANCH" 2>/dev/null || echo 0)
fi
if [ -z "$SHIP_FILES" ] && [ "${AHEAD:-0}" -eq 0 ]; then
  echo '{"ok":false,"abort":"nothing-to-ship","detail":"no source changes to commit and no unpushed commits on the branch (sandbox scaffolding excluded)."}'
  exit 6
fi
if [ -z "$SHIP_FILES" ]; then
  # Already committed by the loop — skip the file scan, R6 already ran before that commit.
  R6_OUT='{"ok":true,"scanned":0,"denylisted":[],"note":"working tree clean; changes already committed"}'
  R6_RC=0
fi
if [ -n "$SHIP_FILES" ]; then
  # --stdin, never `xargs ... --files`: xargs replaces the scan's own exit code with its own,
  # so a genuine denylist HIT (3) would arrive as 1 and be mislabelled "r6-scan-failed" instead
  # of "r6-denylist". Both fail closed, but the operator would be told the wrong reason.
  R6_OUT=$(printf '%s\n' "$SHIP_FILES" | bash "$R6SCAN" --stdin 2>/dev/null); R6_RC=$?
fi
if [ "$R6_RC" = "3" ]; then
  printf '{"ok":false,"abort":"r6-denylist","detail":"change set touches the R6 blast-radius denylist — forced human gate (dev-escalate), never an autonomous ship.","scan":%s}\n' "$R6_OUT"
  exit 4
elif [ "$R6_RC" != "0" ]; then
  printf '{"ok":false,"abort":"r6-scan-failed","detail":"denylist scan errored (rc=%s) — failing CLOSED, refusing to ship.","scan":%s}\n' "$R6_RC" "${R6_OUT:-null}"
  exit 5
fi

# 6. Derive the commit type from the branch prefix. stack.config `project.branch_prefixes`
# ({ticket_type: "prefix/"}) is consulted first (fix → fix, feature → feat, any other key is the
# type itself); then the conventional prefixes. A lane branch that passed the human override is
# typed the same way — there are no lane-specific arms, so nothing falls through unnoticed:
# anything unrecognised is `feat` and is visible as such in the preview.
TYPE=""
for kv in ${PREFIX_MAP[@]+"${PREFIX_MAP[@]}"}; do
  k="${kv%%=*}"; v=$(lower "${kv#*=}")
  case "$BRANCH_L" in "$v"*)
    case "$k" in fix|bug|defect) TYPE=fix;; feature|story) TYPE=feat;; *) TYPE="$k";; esac; break;;
  esac
done
if [ -z "$TYPE" ]; then
  case "$BRANCH_L" in
    bug/*|defect/*|fix/*|hotfix/*) TYPE=fix ;;
    story/*|feature/*|feat/*)      TYPE=feat ;;
    refactor/*)                    TYPE=refactor ;;
    chore/*)                       TYPE=chore ;;
    docs/*)                        TYPE=docs ;;
    test/*|tests/*)                TYPE=test ;;
    *)                             TYPE=feat ;;
  esac
fi

# 7. Preview EVERYTHING that would be committed — modified, staged, AND untracked (new files
# must ship too: `git add -u` alone drops them) — excluding report artifacts.
# Same list the R6 gate just cleared (scaffolding already excluded), minus report artifacts —
# so what infra-ship commits is exactly what was scanned. Never `git add -A`.
CHANGED=$(printf '%s\n' "$SHIP_FILES" \
  | grep -vE '^(plan|TASK|code-review-report|pre-pr-report)\.md$|-report\.md$' | paste -sd, -)
NEW_FILES=$(git ls-files --others --exclude-standard 2>/dev/null \
  | { if [ "$IS_SANDBOX" = "true" ] && [ -n "$SCAFFOLD_RE" ]; then grep -vE "$SCAFFOLD_RE"; else cat; fi; } | paste -sd, -)
LANES_JSON=$(printf '%s' "$CFG_JSON" | python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin).get('prod_lanes') or []))")

printf '{"ok":true,"abort":null,"paused":false,"r6":"clean","staging_only":%s,"prod_deploy_allowed":%s,"prod_lane":%s,"prod_lane_override":%s,"prod_lane_patterns":%s,"branch":%s,"base":%s,"remote":%s,"commit_type":"%s","changed_source":%s,"new_files":%s,"excluded_scaffolding":%s,"dry_run":%s,"config":%s,"next":"format -> commit -> push -> vcs_host open_pr  [PR opens LIVE; staging_only gates the PROD DEPLOY only, which stays disabled until R2/R5/R6/R9. Set SHIP_DRY_RUN=1 for a no-PR rehearsal.]"}\n' \
  "$STAGING_ONLY" "$PROD_DEPLOY_ALLOWED" "$PROD_LANE" "$PROD_LANE_OVERRIDE" "$LANES_JSON" "$(jstr "$BRANCH")" "$(jstr "$BASE")" "$(jstr "$PUSH_REMOTE")" "$TYPE" \
  "$(jstr "$CHANGED")" "$(jstr "$NEW_FILES")" "$(jstr "$EXCLUDED_SCAFFOLD")" "$DRYRUN" "$(jstr "$CONFIG_SOURCE")"
