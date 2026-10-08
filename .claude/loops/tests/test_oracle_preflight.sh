#!/usr/bin/env bash
# Regression: infra-worktree's oracle credential preflight (oracle_preflight.py on the
# auth_provider adapter).
#
# The trap it catches: credentials minted for a REMOTE environment (a shared staging) cannot
# authenticate a LOCAL sandbox. replay-oracle.sh then exits 2 = BLOCKED, qa-verify returns
# `blocked`, and dev-pre-pr blocks the ship — correctly, but only after the whole front half of
# the run has been paid for. This moves the discovery to minute one.
#
# Roles come from adapters.auth_provider.config.roles and variable names from its
# credential_env_pattern (E2E_{ROLE}_{FIELD}); production is whatever app.environments names
# prod. Both come from a scratch stack.config (--config) — nothing is a literal in the script.
#
# Static checks only — no app, no network, no model calls. Scratch dir, self-cleaning.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
P="$SK/infra-worktree/scripts/oracle_preflight.py"
pass=0; fail=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
CFG="$TMP/stack.config.json"
cat > "$CFG" <<'EOF'
{"project": {"name": "oracle-preflight-test", "default_branch": "main"},
 "app": {"url": "http://localhost:3000",
         "environments": {"local": {"url": "http://localhost:3000"},
                          "staging": {"url": "https://staging.example.com"},
                          "prod": {"url": "https://app.example.com"}}},
 "adapters": {"auth_provider": {"provider": "env-file",
   "config": {"env_file": ".env.e2e", "local_env_file": ".env.e2e.local", "process_env": false,
              "roles": ["owner", "admin", "member", "observer"],
              "credential_env_pattern": "E2E_{ROLE}_{FIELD}"}}}}
EOF

# t <desc> <want_ok> <want_exit> -- <args...>
t(){ local desc="$1" wok="$2" wex="$3"; shift 4
  local o rc gok
  o=$(python3 "$P" --config "$CFG" "$@" 2>&1); rc=$?
  gok=$(printf '%s' "$o" | python3 -c "import sys,json;print(json.load(sys.stdin)['ok'])" 2>/dev/null)
  if [ "$gok" = "$wok" ] && [ "$rc" = "$wex" ]; then pass=$((pass+1))
  else echo "  [FAIL] $desc (got ok=$gok exit=$rc, want ok=$wok exit=$wex)"; fail=$((fail+1)); fi; }

# grep the JSON's problems+warnings for a substring
mentions(){ local desc="$1" needle="$2"; shift 3
  local o; o=$(python3 "$P" --config "$CFG" "$@" 2>&1)
  if printf '%s' "$o" | python3 -c "
import sys,json;d=json.load(sys.stdin)
t=' '.join(d.get('problems',[])+d.get('warnings',[]))
sys.exit(0 if '$needle' in t else 1)" 2>/dev/null; then pass=$((pass+1))
  else echo "  [FAIL] $desc (never mentioned '$needle')"; fail=$((fail+1)); fi; }

LOCAL="$TMP/local.env"
cat > "$LOCAL" <<'EOF'
# a sandbox-scoped credential file
E2E_ENV=local
E2E_BASE_URL=http://localhost:3010
E2E_OWNER_USERNAME=owner@example.com
E2E_OWNER_PASSWORD=secret
EOF

STAGING="$TMP/staging.env"
cat > "$STAGING" <<'EOF'
# credentials minted for the shared staging environment, owner only
E2E_ENV=staging
E2E_BASE_URL=https://staging.example.com
E2E_OWNER_USERNAME=owner@example.com
E2E_OWNER_PASSWORD=secret
EOF

# ---------- happy path -------------------------------------------------------------------
t "local creds for local sandbox → ok" True 0 -- --env-file "$LOCAL" --app-url http://localhost:3010

# ---------- host mismatch WARNS, it does not block -------------------------------------------
# replay-oracle.sh reads ONLY the per-role username/password from this file and takes its
# target from APP_URL — it never reads E2E_BASE_URL. So a mismatch is a strong hint that the
# identity store differs, not the mechanism that fails. Blocking on a heuristic manufactures
# false escalations; --live is the conclusive check.
t "staging creds for local sandbox → warns, not blocks" True 0 -- --env-file "$STAGING" --app-url http://localhost:3010
mentions "names the mint environment" "minted for staging.example.com" \
  -- --env-file "$STAGING" --app-url http://localhost:3010
mentions "warns that the local identity provider may not know the user" "local identity provider" \
  -- --env-file "$STAGING" --app-url http://localhost:3010
# the same credentials against the host they were minted for are fine
t "staging creds for staging target → ok" True 0 -- --env-file "$STAGING" --app-url https://staging.example.com

# ---------- missing file / missing roles ----------------------------------------------------
t "no credential file → BLOCKED" False 1 -- --env-file "$TMP/ghost.env" --app-url http://localhost:3010
t "role with no creds → BLOCKED" False 1 -- --env-file "$STAGING" --app-url https://staging.example.com --roles admin
mentions "names the missing username key" "E2E_ADMIN_USERNAME" \
  -- --env-file "$STAGING" --app-url https://staging.example.com --roles admin
mentions "names the missing password key" "E2E_ADMIN_PASSWORD" \
  -- --env-file "$STAGING" --app-url https://staging.example.com --roles admin
# role names are lower-case canonical and compared case-insensitively
t "an upper-case role name resolves the same variables" False 1 -- --env-file "$STAGING" --app-url https://staging.example.com --roles ADMIN
mentions "upper-case role still names E2E_ADMIN_USERNAME" "E2E_ADMIN_USERNAME" \
  -- --env-file "$STAGING" --app-url https://staging.example.com --roles ADMIN
# owner present, admin absent → still blocked, because the run asked for both
t "one of two roles missing → BLOCKED" False 1 -- --env-file "$STAGING" --app-url https://staging.example.com --roles owner,admin

# ---------- empty values are missing, not present ----------------------------------------------
EMPTY="$TMP/empty.env"
cat > "$EMPTY" <<'EOF'
E2E_BASE_URL=http://localhost:3010
E2E_OWNER_USERNAME=
E2E_OWNER_PASSWORD=
EOF
t "empty credential values → BLOCKED" False 1 -- --env-file "$EMPTY" --app-url http://localhost:3010

# ---------- prod safety (prod = app.environments.prod in stack.config) ---------------------------
PROD="$TMP/prod.env"
cat > "$PROD" <<'EOF'
E2E_BASE_URL=https://app.example.com
E2E_OWNER_USERNAME=o@example.com
E2E_OWNER_PASSWORD=s
EOF
t "prod app_url → BLOCKED" False 1 -- --env-file "$PROD" --app-url https://app.example.com
mentions "refuses prod" "PRODUCTION" -- --env-file "$PROD" --app-url https://app.example.com
t "prod refusal is case-insensitive on the host" False 1 -- --env-file "$PROD" --app-url https://APP.example.com/login

# ---------- parser robustness ----------------------------------------------------------------------
QUOTED="$TMP/quoted.env"
cat > "$QUOTED" <<'EOF'
E2E_BASE_URL="http://localhost:3010"
E2E_OWNER_USERNAME='owner@example.com'
E2E_OWNER_PASSWORD="p@ss=word#notacomment"
EOF
t "quoted values parse → ok" True 0 -- --env-file "$QUOTED" --app-url http://localhost:3010

# a file with no E2E_BASE_URL at all must not invent a mismatch
NOURL="$TMP/nourl.env"
printf 'E2E_OWNER_USERNAME=o@example.com\nE2E_OWNER_PASSWORD=s\n' > "$NOURL"
t "no E2E_BASE_URL → no false mismatch" True 0 -- --env-file "$NOURL" --app-url http://localhost:3010

# ---------- auth_provider: none → BLOCKED with the adapter-unavailable reason, never a guess -------
NONECFG="$TMP/none.json"
python3 -c "import json,sys; c=json.load(open(sys.argv[1])); c['adapters']['auth_provider']['provider']='none'; json.dump(c, open(sys.argv[2],'w'))" "$CFG" "$NONECFG"
o=$(python3 "$P" --config "$NONECFG" --env-file "$LOCAL" --app-url http://localhost:3010 2>&1); rc=$?
if [ "$rc" = 1 ] && printf '%s' "$o" | python3 -c "import sys,json;d=json.load(sys.stdin);sys.exit(0 if d['status']=='blocked' and d['error'].startswith('adapter-unavailable:auth_provider') else 1)" 2>/dev/null
then pass=$((pass+1)); else echo "  [FAIL] auth_provider none → blocked adapter-unavailable:auth_provider (rc=$rc) :: $o"; fail=$((fail+1)); fi

# ---------- --live without an oracle degrades to a warning, never a false pass ------------------------
t "--live with no oracle still evaluates" True 0 -- --env-file "$LOCAL" --app-url http://localhost:3010 --live
mentions "--live without oracle warns" "skipped the live sign-in" \
  -- --env-file "$LOCAL" --app-url http://localhost:3010 --live

echo "test_oracle_preflight: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
