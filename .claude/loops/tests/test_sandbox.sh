#!/usr/bin/env bash
# Regression: infra-worktree sandbox lifecycle (R4/R5/crash-safe) — the thin wrapper over the
# `sandbox` adapter (git-worktree provider). Hermetic: a scratch git repo stands in for the target
# app's checkout (app.repo_dir), the registry and the worktrees land in scratch dirs, and every
# config value the script reads is overridden through LOOPS_CFG__* for this process only. No target
# app, no database, no network.
#
# Covers: port offsets derived from app.url / app.api_url (offset = slot x stride, never 0 — slot 0 is
# the developer's checkout); the concurrent-sandbox cap (exit 2, sandbox-cap-reached); idempotent
# re-provision; crash-safe teardown (removed 0 on a second call, unknown key harmless); a freed slot
# is reused; a ticket-derived key with quotes neither breaks the script nor the registry JSON (R6);
# parallel provisions under the registry lock get unique offsets and the cap holds; --branch maps back
# onto (key, type) through project.branch_prefixes; the credential-file REPORT (never a copy) follows
# adapters.auth_provider.config; the db_clone_command hook runs live and is suppressed by --dry-run;
# provider `none` blocks provision (exit 3) and never fails teardown.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
S="$SK/infra-worktree/scripts/sandbox.sh"
pass=0; fail=0
ck(){ if echo "$2" | grep -q "$3"; then pass=$((pass+1)); else echo "  [FAIL] $1 :: $2"; fail=$((fail+1)); fi; }
j(){ python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))" "$1" 2>/dev/null; }

if ! command -v git >/dev/null 2>&1; then echo "  sandbox: SKIP (needs git)"; exit 0; fi

ROOT="$(mktemp -d)"; APP="$ROOT/app"; mkdir -p "$APP"
git -C "$APP" init -q -b main
git -C "$APP" -c user.name=t -c user.email=t@example.com -c commit.gpgsign=false -c core.hooksPath=/dev/null \
  commit -q --allow-empty -m init
printf 'E2E_OWNER_USERNAME=owner@example.com\n' > "$ROOT/.env.e2e"          # the tracked half (no secrets)

export LOOP_SANDBOX_STATE="$ROOT/state"; mkdir -p "$LOOP_SANDBOX_STATE"     # registry → scratch
export LOOPS_CFG__APP__REPO_DIR="$APP"
export LOOPS_CFG__APP__URL="http://localhost:4000"
export LOOPS_CFG__APP__API_URL="http://localhost:4001"
export LOOPS_CFG__APP__START_COMMAND="npm run dev"
export LOOPS_CFG__PROJECT__NAME="example-app"
export LOOPS_CFG__PROJECT__DEFAULT_BRANCH="main"
export LOOPS_CFG__ADAPTERS__SANDBOX__PROVIDER="git-worktree"
export LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__PORT_STRIDE=10
export LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__SANDBOXES_DIR="$ROOT/sandboxes"
export LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__DB_CLONE_COMMAND=null
export LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__DB_DROP_COMMAND=null
export LOOPS_CFG__ADAPTERS__AUTH_PROVIDER__CONFIG__ENV_FILE="$ROOT/.env.e2e"
export LOOPS_CFG__ADAPTERS__AUTH_PROVIDER__CONFIG__LOCAL_ENV_FILE="$ROOT/.env.e2e.local"
cleanup(){ rm -rf "$ROOT"; }
trap cleanup EXIT

# ---------- ports, urls, isolation -------------------------------------------------------------
O=$(bash "$S" provision --key PROJ-1 --cap 2 --dry-run); rc=$?
[ "$rc" = 0 ] && pass=$((pass+1)) || { echo "  [FAIL] provision A exits 0 (rc=$rc) :: $O"; fail=$((fail+1)); }
ck "A offset 10 (never 0 = dev checkout)"   "$O" '"offset":10'
ck "A app url = app.url port + 10"          "$O" '"app":"http://localhost:4010"'
ck "A api url = app.api_url port + 10"      "$O" '"api":"http://localhost:4011"'
ck "A branch = branch_prefixes.fix + key"   "$O" '"branch":"fix/PROJ-1"'
ck "A started:false (recipe starts app)"    "$O" '"started":false'
ck "A dry_run:true"                         "$O" '"dry_run":true'
ck "A next_steps name the start command"    "$O" 'npm run dev'
ck "A reports the credential file"          "$O" "\"file\":\"$ROOT/.env.e2e\""
ck "A credential source is env_file"        "$O" '"source":"env_file"'
WT=$(echo "$O" | j "d['dir']")
[ -n "$WT" ] && [ -d "$WT" ] && git -C "$WT" rev-parse --abbrev-ref HEAD 2>/dev/null | grep -q '^fix/PROJ-1$' \
  && pass=$((pass+1)) || { echo "  [FAIL] A worktree exists on its own branch ($WT)"; fail=$((fail+1)); }
[ -n "$WT" ] && [ ! -e "$WT/.env.e2e" ] && pass=$((pass+1)) \
  || { echo "  [FAIL] credential file is REPORTED, never copied into the worktree"; fail=$((fail+1)); }
echo "$O" | grep -qE 'e2e_ready|e2e_auth_present|e2e_env_copied' \
  && { echo "  [FAIL] no readiness claim about credentials :: $O"; fail=$((fail+1)); } || pass=$((pass+1))

O=$(bash "$S" provision --key PROJ-2 --cap 2 --dry-run)
ck "B offset 20 — spaced x10 (R4)"          "$O" '"offset":20'
ck "B app url 4020"                         "$O" '"app":"http://localhost:4020"'
O=$(bash "$S" provision --key PROJ-3 --cap 2 --dry-run); rc=$?
ck "C at cap → R5 resource block"           "$O" 'sandbox-cap-reached'
[ "$rc" = "2" ] && pass=$((pass+1)) || { echo "  [FAIL] cap exits 2 (got $rc)"; fail=$((fail+1)); }
O=$(bash "$S" provision --key PROJ-1 --cap 2 --dry-run); ck "A reused (idempotent)" "$O" '"reused":true'
O=$(bash "$S" teardown  --key PROJ-1 --dry-run);         ck "teardown A removed 1" "$O" '"removed":1'
[ ! -e "$WT" ] && pass=$((pass+1)) || { echo "  [FAIL] teardown removed the worktree dir"; fail=$((fail+1)); }
O=$(bash "$S" teardown  --key PROJ-1 --dry-run);         ck "teardown A again crash-safe removed 0" "$O" '"removed":0'
O=$(bash "$S" provision --key PROJ-3 --cap 2 --dry-run); ck "C reuses freed slot offset 10" "$O" '"offset":10'
O=$(bash "$S" teardown  --key never-provisioned --dry-run); ck "teardown unknown key harmless" "$O" '"removed":0'

# ---------- --branch maps back onto (key, type) via project.branch_prefixes ----------------------
O=$(bash "$S" provision --branch feature/PROJ-7 --cap 9 --dry-run)
ck "--branch feature/… → key PROJ-7"        "$O" '"key":"PROJ-7"'
ck "--branch feature/… keeps the branch"    "$O" '"branch":"feature/PROJ-7"'
O=$(bash "$S" teardown --branch feature/PROJ-7 --dry-run); ck "teardown by --branch" "$O" '"removed":1'
O=$(bash "$S" provision --branch hotfix/PROJ-8 --cap 9 --dry-run); rc=$?
ck "unknown prefix → branch-prefix-unknown" "$O" 'branch-prefix-unknown'
[ "$rc" = 1 ] && pass=$((pass+1)) || { echo "  [FAIL] unknown prefix exits 1 (got $rc)"; fail=$((fail+1)); }
O=$(bash "$S" provision --cap 9 --dry-run); ck "no key and no branch → usage error" "$O" '"no-key"'

# ---------- R6: a quote in a ticket-derived key must not break the script or the registry --------
O=$(bash "$S" provision --key "PROJ-4-o'brien\"case" --cap 9 --dry-run); rc=$?
[ "$rc" = "0" ] && pass=$((pass+1)) || { echo "  [FAIL] quoted key provisions (rc=$rc) :: $O"; fail=$((fail+1)); }
echo "$O" | python3 -c "import sys,json;json.load(sys.stdin)" 2>/dev/null \
  && pass=$((pass+1)) || { echo "  [FAIL] quoted key emits valid JSON"; fail=$((fail+1)); }
bash "$S" list | python3 -c "import sys,json;json.load(sys.stdin)" 2>/dev/null \
  && pass=$((pass+1)) || { echo "  [FAIL] registry still valid JSON after quoted key"; fail=$((fail+1)); }
bash "$S" teardown --key "PROJ-4-o'brien\"case" --dry-run >/dev/null
O=$(bash "$S" provision --key "../escape" --cap 9 --dry-run); rc=$?
[ "$rc" != 0 ] && echo "$O" | grep -q 'invalid-key\|branch\|key' && pass=$((pass+1)) \
  || { echo "  [FAIL] a path-traversal key is refused (rc=$rc) :: $O"; fail=$((fail+1)); }

# ---------- M1: 6 PARALLEL provisions, cap 4 → exactly 4 land with 4 UNIQUE offsets ---------------
bash "$S" teardown --key PROJ-2 --dry-run >/dev/null; bash "$S" teardown --key PROJ-3 --dry-run >/dev/null
for i in 1 2 3 4 5 6; do bash "$S" provision --key "PROJ-10$i" --cap 4 --dry-run & done >/dev/null 2>&1
wait
N=$(bash "$S" list | python3 -c "import sys,json;print(len(json.load(sys.stdin)['sandboxes']))")
U=$(bash "$S" list | python3 -c "import sys,json;print(len({s['ports']['offset'] for s in json.load(sys.stdin)['sandboxes']}))")
[ "$N" = "4" ] && pass=$((pass+1)) || { echo "  [FAIL] parallel: cap held under race (n=$N want 4)"; fail=$((fail+1)); }
[ "$U" = "$N" ] && pass=$((pass+1)) || { echo "  [FAIL] parallel: offsets unique under race ($U/$N)"; fail=$((fail+1)); }
for i in 1 2 3 4 5 6; do bash "$S" teardown --key "PROJ-10$i" --dry-run >/dev/null; done
[ "$(bash "$S" list | j "len(d['sandboxes'])")" = "0" ] && pass=$((pass+1)) || { echo "  [FAIL] registry empty after teardown"; fail=$((fail+1)); }

# ---------- credential report follows the auth_provider config ------------------------------------
printf 'E2E_OWNER_PASSWORD=not-a-secret\n' > "$ROOT/.env.e2e.local"
O=$(bash "$S" provision --key PROJ-20 --cap 9 --dry-run)
ck "local_env_file wins when present"       "$O" '"source":"local_env_file"'
bash "$S" teardown --key PROJ-20 --dry-run >/dev/null
rm -f "$ROOT/.env.e2e" "$ROOT/.env.e2e.local"
O=$(bash "$S" provision --key PROJ-21 --cap 9 --dry-run); rc=$?
[ "$rc" = 0 ] && pass=$((pass+1)) || { echo "  [FAIL] missing credential files never fail provision (rc=$rc)"; fail=$((fail+1)); }
ck "no credential file → file:null"         "$O" '"file":null'
ck "…but the candidates are named"          "$O" "$ROOT/.env.e2e"
bash "$S" teardown --key PROJ-21 --dry-run >/dev/null

# ---------- the db_clone_command hook: runs live, suppressed by --dry-run -------------------------
export LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__DB_CLONE_COMMAND='echo cloned {db} for {key} slot {slot}'
export LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__DB_DROP_COMMAND='echo dropped {db}'
O=$(bash "$S" provision --key PROJ-30 --cap 9)
ck "live provision runs db_clone_command"   "$O" 'cloned example_app_proj_30 for PROJ-30 slot 1'
ck "live provision reports the db name"     "$O" '"name":"example_app_proj_30"'
ck "live provision dry_run:false"           "$O" '"dry_run":false'
O=$(bash "$S" teardown --key PROJ-30)
ck "live teardown runs db_drop_command"     "$O" 'dropped example_app_proj_30'
O=$(bash "$S" provision --key PROJ-31 --cap 9 --dry-run)
ck "--dry-run suppresses the db hook"       "$O" '"db":null'
bash "$S" teardown --key PROJ-31 --dry-run >/dev/null
export LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__DB_CLONE_COMMAND='exit 7'
O=$(bash "$S" provision --key PROJ-32 --cap 9); rc=$?
ck "failing db hook → sandbox-create-failed" "$O" 'sandbox-create-failed'
[ "$rc" = 3 ] && pass=$((pass+1)) || { echo "  [FAIL] failing db hook exits 3 (got $rc)"; fail=$((fail+1)); }
[ "$(bash "$S" list | j "len(d['sandboxes'])")" = "0" ] && pass=$((pass+1)) || { echo "  [FAIL] failed provision leaves no registry entry"; fail=$((fail+1)); }
unset LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__DB_CLONE_COMMAND LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__DB_DROP_COMMAND

# ---------- provider none: provision blocks (exit 3), teardown never fails -------------------------
export LOOPS_CFG__ADAPTERS__SANDBOX__PROVIDER=none
O=$(bash "$S" provision --key PROJ-40 --cap 9 --dry-run); rc=$?
ck "sandbox:none → status blocked"          "$O" '"status":"blocked"'
ck "sandbox:none → adapter-unavailable:sandbox" "$O" 'adapter-unavailable:sandbox'
[ "$rc" = 3 ] && pass=$((pass+1)) || { echo "  [FAIL] sandbox:none exits 3 (got $rc)"; fail=$((fail+1)); }
O=$(bash "$S" teardown --key PROJ-40 --dry-run); rc=$?
[ "$rc" = 0 ] && pass=$((pass+1)) || { echo "  [FAIL] teardown under none still exits 0 (got $rc)"; fail=$((fail+1)); }
ck "teardown under none is idempotent"      "$O" '"removed":0'
O=$(bash "$S" list); ck "list under none is an empty list" "$O" '"sandboxes":\[\]'
unset LOOPS_CFG__ADAPTERS__SANDBOX__PROVIDER

echo "  sandbox: $pass passed, $fail failed"
[ "$fail" = 0 ]
