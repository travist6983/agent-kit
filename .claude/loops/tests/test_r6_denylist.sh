#!/usr/bin/env bash
# Regression: R6 blast-radius denylist scan + ship_preflight integration. The denylist must be
# ENFORCED, not remembered. The framework default (dev-fix/assets/r6-denylist.globs) is the floor;
# a target repo's conventions file (blast_radius_denylist_globs) replaces it with its own names.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
SCAN="$SK/dev-fix/scripts/r6_denylist_scan.sh"
PRE="$SK/infra-ship/scripts/ship_preflight.sh"
KBPY="$SK/qa-kb/scripts/kb.py"
ROOT="$(mktemp -d)"
pass=0; fail=0
ok(){ pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }
expect_rc(){ # desc want_rc actual_rc
  if [ "$3" = "$2" ]; then ok; else bad "$1 (rc=$3, want $2)"; fi; }

# ── scan: explicit file lists ────────────────────────────────────────────────
bash "$SCAN" --files src/components/TaskPopup.jsx src/App.js >/dev/null 2>&1
expect_rc "benign files pass" 0 $?
O=$(bash "$SCAN" --files api/migrations/20260701_add_col.py); expect_rc "ORM migration blocked" 3 $?
echo "$O" | grep -q '"file": "api/migrations/20260701_add_col.py"' && ok || bad "hit names the file"
bash "$SCAN" --files src/.env.local            >/dev/null; expect_rc ".env.local blocked (basename)" 3 $?
# a production secrets/credentials file is blocked by the generic secrets globs; the secret SECTIONS
# of an otherwise-benign app config file are dev-fix's content-level job (references/blast-radius-denylist.md)
bash "$SCAN" --files config/secrets.production.yml >/dev/null; expect_rc "production secrets file blocked" 3 $?
bash "$SCAN" --files config/credentials.yml.enc   >/dev/null; expect_rc "credentials file blocked" 3 $?
bash "$SCAN" --files deploy/docker-compose.override.yml >/dev/null; expect_rc "docker-compose blocked" 3 $?
bash "$SCAN" --files build-pipelines.pr.yml    >/dev/null; expect_rc "pipeline yaml blocked" 3 $?
bash "$SCAN" --files db/patch_users.sql        >/dev/null; expect_rc "raw sql blocked" 3 $?
bash "$SCAN" --files .github/workflows/ci.yml  >/dev/null; expect_rc "gh workflow blocked" 3 $?
bash "$SCAN" --files a.txt --globs /nonexistent.globs >/dev/null 2>&1; expect_rc "missing globs FAILS CLOSED" 2 $?
echo "src/projects/TaskCard.jsx" | bash "$SCAN" --stdin >/dev/null; expect_rc "stdin mode benign" 0 $?

# ── scan: an adopter's conventions array replaces the default with the stack's own names ──
# (a stack-specific production settings file is blocked once the target repo declares it)
CONV_ROOT="$ROOT/conv"; mkdir -p "$CONV_ROOT"
echo '{"version": 1, "blast_radius_denylist_globs": ["*settings.production*", "*/migrate/*"]}' > "$CONV_ROOT/conventions.json"
echo "{\"stack\": {\"conventions_file\": \"$CONV_ROOT/conventions.json\"}}" > "$CONV_ROOT/stack.config.json"
O=$(LOOPS_STACK_CONFIG="$CONV_ROOT/stack.config.json" bash "$SCAN" --files api/config/settings.production.json)
expect_rc "a declared stack settings file is blocked" 3 $?
echo "$O" | grep -q "\"source\": \"$CONV_ROOT/conventions.json\"" && ok || bad "the hit names the conventions file as its source :: $O"
LOOPS_STACK_CONFIG="$CONV_ROOT/stack.config.json" bash "$SCAN" --files db/migrate/20260701_add_col.rb >/dev/null
expect_rc "a declared migration dir is blocked" 3 $?
echo '{"version": 1, "blast_radius_denylist_globs": []}' > "$CONV_ROOT/conventions.json"
LOOPS_STACK_CONFIG="$CONV_ROOT/stack.config.json" bash "$SCAN" --files api/migrations/x.py >/dev/null
expect_rc "an empty conventions array falls back to the default floor" 3 $?

# ── scan: --diff mode sees untracked files (a NEW migration must not slip by) ─
REPO="$ROOT/repo"; mkdir -p "$REPO"; cd "$REPO"
git init -q -b main; git config user.email t@example.com; git config user.name t
echo x > base.js; git add base.js; git commit -qm base
git checkout -qb bug/PROJ-123-test
echo y >> base.js                              # modified tracked
mkdir -p migrations && echo z > migrations/new.py   # untracked denylisted
O=$(bash "$SCAN" --diff); expect_rc "--diff blocks untracked migration" 3 $?
rm -rf migrations
bash "$SCAN" --diff >/dev/null; expect_rc "--diff clean after removal" 0 $?

# ── ship_preflight integration (needs the live brain for the R9 check) ──────
if python3 "$KBPY" pause --status >/dev/null 2>&1; then
  echo n > newfile.js                          # untracked benign: new files must ship too
  O=$(bash "$PRE" --repo-dir "$REPO"); rc=$?
  expect_rc "preflight ok on benign branch" 0 $rc
  echo "$O" | grep -q '"r6":"clean"'        && ok || bad "preflight reports r6 clean"
  echo "$O" | grep -q 'newfile.js'          && ok || bad "preflight preview includes untracked new file"
  mkdir -p migrations && echo z > migrations/evil.py
  O=$(bash "$PRE" --repo-dir "$REPO"); rc=$?
  expect_rc "preflight R6 hit exits 4" 4 $rc
  echo "$O" | grep -q '"abort":"r6-denylist"' && ok || bad "preflight abort=r6-denylist"
  rm -rf migrations
  git checkout -q main
  bash "$PRE" --repo-dir "$REPO" >/dev/null; expect_rc "preflight base-branch exits 3 (non-zero)" 3 $?
else
  echo "  (preflight cases SKIPPED — the brain is unreachable)"
fi

cd /; echo "  r6_denylist: $pass passed, $fail failed"
rm -rf "$ROOT"
[ "$fail" = 0 ]
