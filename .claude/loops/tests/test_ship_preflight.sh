#!/usr/bin/env bash
# Regression: infra-ship ship_preflight.sh branch gates — the prod-lane guard.
# Some target repos deploy on a branch NAME alone (a CD trigger on deploy/* or release/* with no PR
# in between), so `git push` of such a branch IS a production deploy. Those names are declared in
# stack.config `deploy.prod_lane_branch_patterns`; the preflight must FAIL CLOSED (abort
# prod-lane-branch, exit 7) on a matching branch unless staging_only is explicitly off AND
# SHIP_PROD_LANE_OK names that exact branch. The default is an empty list — no push is a deploy —
# and then the guard is inert. `project.protected_branch_patterns` are never pushed (exit 3, like
# the base branch).
# The branch name must be JSON-escaped on every line that emits it (git allows '"' in a branch
# name), and a lane push hidden behind git config — a remote.<r>.push refspec or a branch.<b>.merge
# upstream that lands bug/x on a lane ref — is refused too, override or not.
#
# Fully offline and deterministic: the preflight runs from a scratch copy of the skills tree
# whose qa-kb/scripts/kb.py is a stub (pause + staging_only answered from env), against a
# throwaway git repo with no remote, with a stack.config fixture (LOOPS_STACK_CONFIG) declaring
# the lanes. `curl`/`wget`/`gh` are PATH shims that log and fail — a call to any of them is a test
# failure. Never touches the shared brain.
#
# SHIP_PREFLIGHT_UNDER_TEST=<path> runs the suite against another copy (e.g. a skill snapshot).
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
PRE_SRC="${SHIP_PREFLIGHT_UNDER_TEST:-$SK/infra-ship/scripts/ship_preflight.sh}"
ROOT="$(cd "$(mktemp -d)" && pwd -P)"   # resolved: the preflight reports the config's real path
trap 'rm -rf "$ROOT"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }

# ── scratch skills tree: the preflight under test + the REAL R6 scan + a stub kb.py ─────
T="$ROOT/skills"
mkdir -p "$T/infra-ship/scripts" "$T/dev-fix/scripts" "$T/dev-fix/assets" "$T/qa-kb/scripts"
cp "$PRE_SRC" "$T/infra-ship/scripts/ship_preflight.sh"
cp "$SK/dev-fix/scripts/r6_denylist_scan.sh" "$T/dev-fix/scripts/"
cp "$SK/dev-fix/assets/r6-denylist.globs" "$T/dev-fix/assets/"
cat > "$T/qa-kb/scripts/kb.py" <<'PY'
# Stub of the qa-kb CLI: only the two reads ship_preflight makes. FAKE_STAGING=missing mimics
# an unseeded staging_only row (value null) — the preflight must treat it as staging posture.
import json, os, sys
a = sys.argv[1:]
if a[:1] == ["pause"]:
    print(json.dumps({"ok": True, "action": "status", "control": {"value": os.environ.get("FAKE_PAUSED", "false") == "true"}}))
elif a[:1] == ["flag"]:
    v = os.environ.get("FAKE_STAGING", "true")
    print(json.dumps({"ok": True, "action": "flag_status", "flag": "staging_only", "value": None if v == "missing" else v == "true"}))
else:
    print(json.dumps({"ok": False, "error": "stub: unexpected verb %r" % a})); sys.exit(1)
PY
PRE="$T/infra-ship/scripts/ship_preflight.sh"

# ── stack.config fixtures: one declaring prod lanes, one with the default (no lanes) ────────────
export LOOPS_REPO_ROOT="$(cd "$SK/../.." && pwd)"   # the framework checkout: lib/adapters/config.py
CFG_LANES="$ROOT/stack.lanes.json"; CFG_DEFAULT="$ROOT/stack.default.json"
cat > "$CFG_LANES" <<'JSON'
{"project": {"name": "ship-preflight-test", "default_branch": "main",
             "branch_prefixes": {"fix": "fix/", "feature": "feature/"},
             "protected_branch_patterns": ["main", "protected/*"]},
 "deploy": {"prod_lane_branch_patterns": ["deploy/*", "prod/*", "release/*"]}}
JSON
cat > "$CFG_DEFAULT" <<'JSON'
{"project": {"name": "ship-preflight-test", "default_branch": "main",
             "branch_prefixes": {"fix": "fix/", "feature": "feature/"},
             "protected_branch_patterns": ["main", "protected/*"]}}
JSON
export LOOPS_STACK_CONFIG="$CFG_LANES"

# ── no-network shims: anything outward is a failure ──────────────────────────────────────
SHIMS="$ROOT/shims"; mkdir -p "$SHIMS"; NETLOG="$ROOT/net.log"; : > "$NETLOG"
for c in curl wget gh; do
  printf '#!/bin/sh\necho "%s $*" >> "%s"\nexit 99\n' "$c" "$NETLOG" > "$SHIMS/$c"; chmod +x "$SHIMS/$c"
done
export PATH="$SHIMS:$PATH"

# ── throwaway repo with a benign change on every branch ──────────────────────────────────
REPO="$ROOT/repo"; mkdir -p "$REPO"; cd "$REPO" || exit 1
git init -q -b main 2>/dev/null || git init -q
git config user.email t@t; git config user.name t
echo x > base.js; git add base.js; git commit -qm base
on_branch(){ git checkout -q -B "$1" main 2>/dev/null; echo "change" > edit.js; }

# run <branch> [ENV=VAL ...] — sets OUT/RC; always unsets the override first
run(){ local b="$1"; shift; on_branch "$b"
  OUT=$(env -u SHIP_PROD_LANE_OK -u SHIP_DRY_RUN FAKE_STAGING=true "$@" bash "$PRE" --repo-dir "$REPO" 2>/dev/null); RC=$?; }
field(){ printf '%s' "$OUT" | python3 -c "import sys,json;v=json.load(sys.stdin).get('$1','<absent>');print(json.dumps(v))" 2>/dev/null || echo '<bad-json>'; }
expect(){ # desc want_rc [field=json-value ...] — one pass/fail per CASE, every miss printed
  local desc="$1" want="$2"; shift 2; local why=""
  [ "$RC" = "$want" ] || why="rc=$RC, want $want"
  for kv in "$@"; do local k="${kv%%=*}" v="${kv#*=}" got; got=$(field "$k")
    [ "$got" = "$v" ] || why="${why:+$why; }$k=$got, want $v"; done
  if [ -z "$why" ]; then ok; else bad "$desc ($why) :: $OUT"; fi; }

# 1. Every declared prod lane is refused in the default (staging) posture.
for lane in deploy prod release; do
  run "$lane/PROJ-123-typo"
  expect "$lane/* refused under staging_only" 7 'abort="prod-lane-branch"' 'ok=false' "branch=\"$lane/PROJ-123-typo\"" "lane=\"$lane\""
done
# 2. Case games do not slip past it (fail closed on the prefix).
run "Deploy/PROJ-123-typo";  expect "Deploy/* (capitalised) refused" 7 'abort="prod-lane-branch"'
# 3. The override alone is NOT enough while staging_only is on (or unreadable).
run "deploy/PROJ-123-typo" SHIP_PROD_LANE_OK=deploy/PROJ-123-typo
expect "override ignored while staging_only=true" 7 'abort="prod-lane-branch"' 'prod_deploy_allowed=false'
run "deploy/PROJ-123-typo" FAKE_STAGING=missing SHIP_PROD_LANE_OK=deploy/PROJ-123-typo
expect "override ignored while staging_only unreadable (fail closed)" 7 'abort="prod-lane-branch"'
# 4. staging_only off is NOT enough without the per-run override.
run "deploy/PROJ-123-typo" FAKE_STAGING=false
expect "staging_only=false alone does not unlock a prod lane" 7 'abort="prod-lane-branch"' 'prod_deploy_allowed=true'
# 5. An override naming a different branch (or a glob) does not unlock this one.
run "deploy/PROJ-123-typo" FAKE_STAGING=false SHIP_PROD_LANE_OK=deploy/PROJ-999-other
expect "override for another branch rejected" 7 'abort="prod-lane-branch"' 'override="deploy/PROJ-999-other"'
run "deploy/PROJ-123-typo" FAKE_STAGING=false 'SHIP_PROD_LANE_OK=deploy/*'
expect "glob override rejected" 7 'abort="prod-lane-branch"'
# 6. Both keys turned: proceeds, and the override is recorded in the preflight JSON.
run "deploy/PROJ-123-typo" FAKE_STAGING=false SHIP_PROD_LANE_OK=deploy/PROJ-123-typo
expect "staging off + exact override proceeds" 0 'ok=true' 'prod_lane=true' 'prod_lane_override="deploy/PROJ-123-typo"' 'commit_type="feat"' \
  "config=\"$CFG_LANES\""
# 7. Ordinary loop branches are untouched, and say so.
run "bug/PROJ-123-typo"
expect "bug/* still ships" 0 'ok=true' 'prod_lane=false' 'prod_lane_override=null' 'commit_type="fix"' \
  'prod_lane_patterns=["deploy/*", "prod/*", "release/*"]'
run "fix/task-list-header"
expect "fix/* is a fix, not a feat" 0 'ok=true' 'prod_lane=false' 'commit_type="fix"'
run "story/PROJ-123-thing"
expect "story/* still ships" 0 'ok=true' 'prod_lane=false' 'commit_type="feat"'
run "users/someone/deploy/x"
expect "deploy/ deeper in the name is not a lane" 0 'ok=true' 'prod_lane=false'
# 8. The existing gates keep their codes (pause beats everything; base/protected branch still 3).
run "deploy/PROJ-123-typo" FAKE_PAUSED=true
expect "R9 pause still first (exit 2)" 2 'abort="global-pause"'
run "protected/PROJ-123-typo"
expect "a project.protected_branch_patterns branch is never shipped (exit 3)" 3 'abort="on-base-branch"' 'protected_by="protected/*"'
git checkout -q main; echo "change" > edit.js
OUT=$(env -u SHIP_PROD_LANE_OK FAKE_STAGING=true bash "$PRE" --repo-dir "$REPO" 2>/dev/null); RC=$?
expect "main still on-base-branch (exit 3)" 3 'abort="on-base-branch"' 'base="main"'
# 9. A '"' in the branch name still yields ONE parseable JSON line, on the abort and ok paths.
run 'deploy/a"b';  expect "quoted branch: abort JSON parses" 7 'abort="prod-lane-branch"' 'branch="deploy/a\"b"'
run 'bug/a"b';     expect "quoted branch: ok JSON parses" 0 'ok=true' 'branch="bug/a\"b"'
# 10. A lane push hidden behind git config is refused (config via GIT_CONFIG_* env, so it never
# leaks into the next case), and the per-run override cannot unlock it.
PUSHCFG=(GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push)
run "bug/PROJ-123-typo" "${PUSHCFG[@]}" 'GIT_CONFIG_VALUE_0=refs/heads/bug/*:refs/heads/deploy/*'
expect "origin push refspec onto deploy/* refused" 7 'abort="prod-lane-branch"' 'lane="deploy"' 'via="remote.origin.push"'
run "bug/PROJ-123-typo" "${PUSHCFG[@]}" 'GIT_CONFIG_VALUE_0=+HEAD:refs/heads/Release/x' FAKE_STAGING=false SHIP_PROD_LANE_OK=bug/PROJ-123-typo
expect "mapped lane push not unlocked by staging off + override" 7 'abort="prod-lane-branch"' 'lane="release"'
run "bug/PROJ-123-typo" GIT_CONFIG_COUNT=3 GIT_CONFIG_KEY_0=branch.bug/PROJ-123-typo.pushRemote GIT_CONFIG_VALUE_0=fork \
  GIT_CONFIG_KEY_1=remote.fork.push GIT_CONFIG_VALUE_1=prod/x GIT_CONFIG_KEY_2=remote.fork.url GIT_CONFIG_VALUE_2=/nonexistent
expect "pushRemote's refspec onto prod/* refused" 7 'abort="prod-lane-branch"' 'via="remote.fork.push"'
run "bug/PROJ-123-typo" GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=branch.bug/PROJ-123-typo.merge GIT_CONFIG_VALUE_0=refs/heads/deploy/PROJ-123-typo
expect "upstream on deploy/* refused" 7 'abort="prod-lane-branch"' 'via="branch.bug/PROJ-123-typo.merge"' 'target="refs/heads/deploy/PROJ-123-typo"'
run "bug/PROJ-123-typo" "${PUSHCFG[@]}" 'GIT_CONFIG_VALUE_0=refs/heads/*:refs/heads/*'
expect "same-name push refspec still ships" 0 'ok=true' 'prod_lane=false'
# 10b. The default config declares NO prod lanes: no branch name, refspec or upstream is a deploy,
# the guard is inert, and the preflight says so (prod_lane_patterns []).
run "deploy/PROJ-123-typo" LOOPS_STACK_CONFIG="$CFG_DEFAULT"
expect "no lanes declared: deploy/* is an ordinary branch" 0 'ok=true' 'prod_lane=false' 'prod_lane_patterns=[]' "config=\"$CFG_DEFAULT\""
run "release/PROJ-123-typo" LOOPS_STACK_CONFIG="$CFG_DEFAULT"
expect "no lanes declared: release/* is an ordinary branch" 0 'ok=true' 'prod_lane=false'
run "bug/PROJ-123-typo" LOOPS_STACK_CONFIG="$CFG_DEFAULT" GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=branch.bug/PROJ-123-typo.merge GIT_CONFIG_VALUE_0=refs/heads/deploy/PROJ-123-typo
expect "no lanes declared: an upstream on deploy/* is not a lane push" 0 'ok=true' 'prod_lane=false'
run "protected/PROJ-123-typo" LOOPS_STACK_CONFIG="$CFG_DEFAULT"
expect "no lanes declared: protected branches are still refused (exit 3)" 3 'abort="on-base-branch"'
run "deploy/PROJ-123-typo" LOOPS_STACK_CONFIG="$CFG_DEFAULT" FAKE_PAUSED=true
expect "no lanes declared: R9 pause still refuses (exit 2)" 2 'abort="global-pause"'
# 11. Nothing outward was attempted.
if [ -s "$NETLOG" ]; then bad "preflight called the network: $(tr '\n' ';' < "$NETLOG")"; else ok; fi

cd /; echo "  ship_preflight: $pass passed, $fail failed"
[ "$fail" = 0 ]
