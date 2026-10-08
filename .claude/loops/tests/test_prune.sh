#!/usr/bin/env bash
# Regression: infra-worktree's port reclamation + sandbox prune (prune.py on the sandbox adapter).
#
# The failure it exists for: the sandbox adapter allocates slots from the REGISTRY, so a stale
# worktree that is not in the registry is invisible to allocation while still holding real TCP
# ports. Such a squatter makes a fresh loop hand-patch its ports, or give up on isolation and run
# in the developer's own checkout.
#
# The safety property under test: DRY-RUN BY DEFAULT, and a tree with uncommitted changes is
# NEVER pruned. No network, no model calls. Scratch dirs and a scratch stack.config (--config)
# whose app.url sits on an unlikely port, so the lsof scan never meets a real dev server.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
P="$SK/infra-worktree/scripts/prune.py"
pass=0; fail=0
TMP="$(cd "$(mktemp -d)" && pwd -P)"; trap 'rm -rf "$TMP"' EXIT

ok(){ if [ "$1" = "$2" ]; then pass=$((pass+1)); else
  echo "  [FAIL] $3 (got '$1' want '$2')"; fail=$((fail+1)); fi; }

jq_(){ printf '%s' "$1" | python3 -c "import sys,json;d=json.load(sys.stdin);print($2)" 2>/dev/null; }

# --- a fake app repo with a merged branch, an unmerged one, and a dirty one -----------------
APP="$TMP/app"; WT="$TMP/app-sandboxes"     # the adapter's default dir: <repo>/../<project.name>-sandboxes
mkdir -p "$APP" "$WT"
git -C "$APP" init -q -b main 2>/dev/null
git -C "$APP" config user.email t@example.com; git -C "$APP" config user.name T
echo one > "$APP/f.txt"; git -C "$APP" add -A; git -C "$APP" commit -qm init

git -C "$APP" worktree add -q -b merged-branch "$WT/merged" main 2>/dev/null
git -C "$APP" worktree add -q -b live-branch   "$WT/live"   main 2>/dev/null
git -C "$APP" worktree add -q -b dirty-branch  "$WT/dirty"  main 2>/dev/null
echo scratch > "$WT/dirty/uncommitted.txt"     # makes it dirty

CFG="$TMP/stack.config.json"
cat > "$CFG" <<EOF
{"project": {"name": "app", "default_branch": "main"},
 "app": {"repo_dir": "$APP", "url": "http://localhost:47100", "api_url": "http://localhost:47101"},
 "adapters": {"sandbox": {"provider": "git-worktree", "config": {"port_stride": 10, "max_concurrent": 3}}},
 "evidence": {"root": "$TMP/runs"}}
EOF
REG="$TMP/sandboxes.json"     # the adapter's registry shape: {key: {key, dir, branch, slot, ports, urls}}
cat > "$REG" <<EOF
{"PROJ-123": {"key": "PROJ-123", "branch": "live-branch", "slot": 1, "dir": "$WT/live",
  "ports": {"app": 47110, "api": 47111, "offset": 10},
  "urls": {"app": "http://localhost:47110", "api": "http://localhost:47111"}}}
EOF

OUT=$(python3 "$P" --config "$CFG" --app-root "$APP" --sandboxes-dir "$WT" --registry "$REG" --max-age-hours 0 2>&1)
rc=$?

# ---------- dry-run is the default ---------------------------------------------------------
ok "$(jq_ "$OUT" "d['mode']")" "dry-run" "dry-run by default"
ok "$(jq_ "$OUT" "len(d['actions'])")" "0" "dry-run performs no actions"

# ---------- a dirty tree is never pruned, and it fails loud ----------------------------------
ok "$(jq_ "$OUT" "len(d['blocked_dirty'])")" "1" "dirty tree reported"
ok "$(jq_ "$OUT" "d['blocked_dirty'][0]['worktree'].endswith('dirty')")" "True" "the DIRTY tree is the one blocked"
ok "$rc" "1" "exit 1 when a dirty tree needs attention"
ok "$(jq_ "$OUT" "d['ok']")" "False" "ok=false with a dirty tree"
# and it must NOT appear in either prunable list
ok "$(jq_ "$OUT" "any('dirty' in e['worktree'] for e in d['prunable_merged']+d['prunable_aged'])")" \
   "False" "dirty tree never listed as prunable"

# ---------- a registered, live sandbox is never touched ----------------------------------------
ok "$(jq_ "$OUT" "any('live' in e['worktree'] for e in d['prunable_merged']+d['prunable_aged'])")" \
   "False" "registered sandbox left alone"

# ---------- a merged branch is prunable ---------------------------------------------------------
ok "$(jq_ "$OUT" "any('merged' in e['worktree'] for e in d['prunable_merged'])")" \
   "True" "merged branch offered for prune"

# ---------- remedy is stated when there is something to do ----------------------------------------
ok "$(jq_ "$OUT" "'remedy' in d")" "True" "dry-run states the remedy"

# ---------- nothing was actually removed -----------------------------------------------------------
ok "$([ -d "$WT/merged" ] && echo yes)" "yes" "dry-run did not remove the merged tree"
ok "$([ -f "$WT/dirty/uncommitted.txt" ] && echo yes)" "yes" "dry-run did not touch the dirty tree"

# ---------- a clean tree with recent mtime is NOT aged out ------------------------------------------
OUT2=$(python3 "$P" --config "$CFG" --app-root "$APP" --worktree-dir "$WT" --registry "$REG" --max-age-hours 99999 2>&1)
ok "$(jq_ "$OUT2" "len(d['prunable_aged'])")" "0" "recent trees not aged out with a long max-age"

# ---------- no registry at all still works (everything unregistered) ----------------------------------
# no flags but --config: app.repo_dir, the sandboxes dir and the registry all come from the adapter
OUT3=$(python3 "$P" --config "$CFG" --max-age-hours 0 2>&1)
ok "$(jq_ "$OUT3" "d['registered_sandboxes']")" "0" "missing registry tolerated"
ok "$(jq_ "$OUT3" "d['sandboxes_dir'] == '$WT'")" "True" "the sandboxes dir comes from the adapter"
ok "$(jq_ "$OUT3" "d['scanned_ports']['bases']")" "[47100, 47101]" "scanned ports derive from app.url / app.api_url"
ok "$(jq_ "$OUT3" "any('live' in e['worktree'] for e in d['prunable_merged']+d['prunable_aged'])")" \
   "True" "with no registry, the live tree IS a candidate (nothing vouches for it)"

# ---------- bad app root fails closed ------------------------------------------------------------------
python3 "$P" --config "$CFG" --app-root "$TMP/ghost" >/dev/null 2>&1
ok "$?" "2" "missing app root exits 2"

# ---------- sandbox provider none: nothing to prune, a structured block (never a guess) ----------------
OUT5=$(LOOPS_CFG__ADAPTERS__SANDBOX__PROVIDER=none python3 "$P" --config "$CFG" 2>&1); rc5=$?
ok "$rc5" "2" "sandbox: none exits 2"
ok "$(jq_ "$OUT5" "d['status']")" "blocked" "sandbox: none is a structured block"
ok "$(jq_ "$OUT5" "d['error'].startswith('adapter-unavailable:sandbox')")" "True" "sandbox: none names adapter-unavailable:sandbox"

# ---------- port scan: an unheld range yields no squatters ------------------------------------------------
OUT4=$(python3 "$P" --config "$CFG" --app-root "$APP" --sandboxes-dir "$WT" --registry "$REG" \
        --slot-range 2:3 --max-age-hours 99999 2>&1)
ok "$(jq_ "$OUT4" "len(d['port_squatters'])")" "0" "no squatters on a free port range"

echo "test_prune: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
