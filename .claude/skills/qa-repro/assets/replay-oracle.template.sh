#!/usr/bin/env bash
# replay-oracle.sh — <KEY>: <one-line symptom>
# The deterministic replay oracle qa-repro emits and qa-verify re-runs. Driven by playwright-cli;
# there is no spec file and no test runner. qa-repro renders this template into the evidence dir with
# scripts/render_oracle.py (which fills the CONFIG block from stack.config), fills the two marked blocks,
# and leaves the rest alone.
#
# RUN: APP_URL=<url> ROLE=<the role this bug needs> bash replay-oracle.sh
#
# REPLAY PROCEDURE (from brief.json.repro_steps — the human-readable half of the oracle):
#   1. <step>
#   2. <step>
#   EXPECTED (verbatim): <expected_verbatim>
#   ACTUAL   (verbatim): <actual_verbatim>
#
# It runs in its own headless browser signed in as the test account for ROLE (the auth_provider
# adapter's credentials) — NOT the browser session the repro was recorded in — so any data the bug
# needs must exist for that account.
# Pure-backend surface (no UI): keep the header, helpers, prod-host refusal and reachability check;
# replace the browser session, sign-in and both FILL blocks with the API request(s) and an assertion
# that ends in `say true ...; exit 0` / `say false ...; exit 1` / `blocked ...`. No replay-grader.js.
#
# Exit 0 = the CORRECT behavior holds (GREEN) · 1 = the bug is present (RED) · 2 = could not run (BLOCKED —
# never read as red or green). Always prints exactly one JSON line: {"pass": true|false|null, "observed": "..."}
#
# Env: APP_URL (required) · ROLE = one of the configured roles (CONFIG block below; default = the first one)
#        or none (= no sign-in). Compared case-insensitively; never evaluated.
#      E2E_ENV_FILE / E2E_LOCAL_ENV_FILE — the credential files (defaults rendered from stack.config); read
#        literally, never sourced. A credential variable already in the process env wins (CI injects secrets that way).
#      REPLAY_STATE_DIR (default ~/.loop-replay) — a saved session is reused and refreshed there, never in the evidence dir
#      PROD_HOSTS — space-separated production host globs (rendered from app.environments.prod.url). A matching
#        APP_URL is refused.
#      ALLOW_PROD=1 — infra-prod-verify only. A production host is refused even then unless this file has the exact
#        line `# PROD-SAFE: read-only`, which qa-repro adds only when FILL 1 and the grader create, change and delete nothing.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
say() { python3 -c 'import json,sys; v={"true":True,"false":False}.get(sys.argv[1]); print(json.dumps({"pass": v, "observed": sys.argv[2]}))' "$1" "$2"; }
blocked() { say null "BLOCKED: $1"; exit 2; }
js_str() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]), end="")' "$1"; }   # a shell string as a JS literal

# ── CONFIG — rendered from stack.config by qa-repro/scripts/render_oracle.py. Single-quoted literals; an env var
#    set by the caller (ORACLE_ROLES, LOGIN_PATH, …) wins over the rendered value. Keep the `# @cfg` tags.
CFG_ROLES='owner admin member observer'            # @cfg adapters.auth_provider.config.roles (lower-case, space-separated; '' = ROLE=none only)
CFG_LOGIN_PATH='/login'                            # @cfg adapters.auth_provider.config.login.path
CFG_USERNAME_SELECTOR='input[name=email]'          # @cfg adapters.auth_provider.config.login.username_selector
CFG_PASSWORD_SELECTOR='input[name=password]'       # @cfg adapters.auth_provider.config.login.password_selector
CFG_SUBMIT_SELECTOR='button[type=submit]'          # @cfg adapters.auth_provider.config.login.submit_selector ('' or 'Enter' = press Enter)
CFG_FAILURE_REGEX='invalid|incorrect|try again'    # @cfg adapters.auth_provider.config.login.failure_regex
CFG_SIGNED_IN_LANDMARK='[data-testid=user-menu]'   # @cfg adapters.auth_provider.config.login.signed_in_landmark
CFG_CRED_PATTERN='E2E_{ROLE}_{FIELD}'              # @cfg adapters.auth_provider.config.credential_env_pattern (FIELD = USERNAME | PASSWORD)
CFG_PRE_AUTH='none'                                # @cfg adapters.auth_provider.config.pre_auth (none | sso_human_once)
CFG_ENV_FILE=''                                    # @cfg adapters.auth_provider.config.env_file (resolved to an absolute path)
CFG_LOCAL_ENV_FILE=''                              # @cfg adapters.auth_provider.config.local_env_file (resolved to an absolute path)
CFG_PROD_HOSTS=''                                  # @cfg app.environments.prod.url (host globs; '' = no host is treated as production)
ROLES="${ORACLE_ROLES-$CFG_ROLES}"; LOGIN_PATH="${LOGIN_PATH-$CFG_LOGIN_PATH}"
USERNAME_SELECTOR="${LOGIN_USERNAME_SELECTOR-$CFG_USERNAME_SELECTOR}"; PASSWORD_SELECTOR="${LOGIN_PASSWORD_SELECTOR-$CFG_PASSWORD_SELECTOR}"
SUBMIT_SELECTOR="${LOGIN_SUBMIT_SELECTOR-$CFG_SUBMIT_SELECTOR}"; FAILURE_REGEX="${LOGIN_FAILURE_REGEX-$CFG_FAILURE_REGEX}"
SIGNED_IN_LANDMARK="${LOGIN_SIGNED_IN_LANDMARK-$CFG_SIGNED_IN_LANDMARK}"; CRED_PATTERN="${CREDENTIAL_ENV_PATTERN-$CFG_CRED_PATTERN}"
PRE_AUTH="${AUTH_PRE_AUTH-$CFG_PRE_AUTH}"; ENV_FILE="${E2E_ENV_FILE-$CFG_ENV_FILE}"; LOCAL_ENV_FILE="${E2E_LOCAL_ENV_FILE-$CFG_LOCAL_ENV_FILE}"
PROD_HOSTS="${PROD_HOSTS-$CFG_PROD_HOSTS}"

[ -n "${APP_URL:-}" ] || blocked "APP_URL not set"
APP_URL="${APP_URL%/}"
# ROLE: default = the first configured role (none when nothing is configured); validated by string equality
# against the configured list, lower-cased — it never reaches a glob, a case pattern or an eval.
ROLE="${ROLE-}"; [ -n "$ROLE" ] || ROLE="${ROLES%% *}"; [ -n "$ROLE" ] || ROLE=none
ROLE_LC="$(printf '%s' "$ROLE" | tr 'A-Z' 'a-z')"
if [ "$ROLE_LC" != none ]; then
  role_ok=no; for r in $ROLES; do [ "$r" = "$ROLE_LC" ] && role_ok=yes; done
  [ "$role_ok" = yes ] || blocked "invalid ROLE '$ROLE' (one of: ${ROLES:-<no roles configured — auth_provider is none>} | none)"
fi
command -v playwright-cli >/dev/null || blocked "playwright-cli not installed (npm install -g @playwright/cli@latest)"
# Production refusal: the host of APP_URL against the rendered/overridden PROD_HOSTS globs.
APP_HOST="$(printf '%s' "$APP_URL" | sed -E 's#^[A-Za-z][A-Za-z0-9+.-]*://##; s#^[^/@]*@##; s#[/:?].*$##')"
is_prod=no; for pat in $PROD_HOSTS; do case "$APP_HOST" in $pat) is_prod=yes;; esac; done
if [ "$is_prod" = yes ]; then
  [ "${ALLOW_PROD:-}" = 1 ] || blocked "refusing production host $APP_URL"
  grep -qx '# PROD-SAFE: read-only' "${BASH_SOURCE[0]}" || blocked "refusing production host $APP_URL: this oracle is not marked # PROD-SAFE: read-only"
fi
code=$(curl -sk -o /dev/null -m 15 -w '%{http_code}' "$APP_URL/")
case "$code" in 2??|3??|401|403) ;; *) blocked "app unreachable at $APP_URL (HTTP $code)";; esac

S="replay-$$"
cd "$HERE" || blocked "cannot cd to $HERE"   # playwright-cli writes .playwright-cli/ into the CWD — keep it in the evidence dir
# playwright-cli exits 0 even when an action fails (a missing locator, a failed goto, a throwing run-code) and
# only prints "### Error". pw() fails on that output too, so every `|| blocked` below really fires.
pw() { local o rc; o=$(playwright-cli -s="$S" --raw "$@" 2>&1); rc=$?; printf '%s\n' "$o"; [ "$rc" -eq 0 ] && [[ $'\n'"$o" != *$'\n### Error'* ]]; }
trap 'playwright-cli -s="$S" close >/dev/null 2>&1' EXIT
pw open >/dev/null 2>&1 || blocked "playwright-cli open failed"

# ── sign in ──────────────────────────────────────────────────────────────────────────────────────
# Signed in = the configured landmark is visible (auth_provider.config.login.signed_in_landmark).
# The landmark and timeout are staged in variables first: macOS /bin/bash 3.2 mangles the JS braces
# when a ${1:-…} expansion and a nested $(…) sit inside one double-quoted command substitution.
signed_in() {
  local t="${1:-30000}" lm js out
  lm=$(js_str "$SIGNED_IN_LANDMARK")
  js="async page => { try { await page.locator($lm).first().waitFor({ state: 'visible', timeout: $t }); return 'ok'; } catch (e) { return 'no'; } }"
  out=$(pw run-code "$js")
  [ "$out" = '"ok"' ]
}
# cred_value NAME — the process env wins; then the local (secret) file, then the shared file. Files are read
# literally, line by line — never sourced or eval'd — so a value like `$(…)` is just text.
cred_value() {
  local v="${!1-}"; if [ -n "$v" ]; then printf '%s' "$v"; return; fi
  python3 - "$1" "$ENV_FILE" "$LOCAL_ENV_FILE" <<'PY'
import re, sys
name, files = sys.argv[1], [f for f in sys.argv[2:] if f]
val = ""
for path in files:                       # later file wins, like the env-file auth adapter
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        continue
    with fh:
        for line in fh:
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m and m.group(1) == name:
                v = m.group(2)
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                val = v
print(val, end="")
PY
}
if [ "$ROLE_LC" != none ]; then
  STATE_DIR="${REPLAY_STATE_DIR:-$HOME/.loop-replay}"
  STATE="$STATE_DIR/$(printf '%s' "$APP_URL" | sed -E 's#^https?://##; s#[^A-Za-z0-9.-]#_#g')-$ROLE_LC.json"
  ok=no
  if [ -f "$STATE" ]; then
    pw state-load "$STATE" >/dev/null 2>&1 && pw goto "$APP_URL/" >/dev/null 2>&1 && signed_in 15000 && ok=yes
  fi
  if [ "$ok" = no ]; then
    case "$PRE_AUTH" in
      none) ;;
      sso_human_once) blocked "pre_auth=sso_human_once and no valid saved session at $STATE — a human signs in once with playwright-cli and saves the state there";;
      *) blocked "pre_auth=$PRE_AUTH is not supported by the replay oracle (none | sso_human_once)";;
    esac
    # Variable names from credential_env_pattern — ROLE upper-cased, `-` → `_` (the env-file adapter's rule).
    R="$(printf '%s' "$ROLE_LC" | tr 'a-z-' 'A-Z_' | tr -c 'A-Z0-9_' '_')"
    UNAME_VAR="${CRED_PATTERN//\{ROLE\}/$R}"; UNAME_VAR="${UNAME_VAR//\{FIELD\}/USERNAME}"
    PASS_VAR="${CRED_PATTERN//\{ROLE\}/$R}";  PASS_VAR="${PASS_VAR//\{FIELD\}/PASSWORD}"
    [[ "$UNAME_VAR" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && "$PASS_VAR" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
      || blocked "credential_env_pattern '$CRED_PATTERN' does not render a valid variable name"
    USERNAME=$(cred_value "$UNAME_VAR")
    PASSWORD=$(cred_value "$PASS_VAR")
    [ -n "$USERNAME" ] && [ -n "$PASSWORD" ] \
      || blocked "no saved session and no credentials for role $ROLE_LC: set $UNAME_VAR / $PASS_VAR in ${ENV_FILE:-<E2E_ENV_FILE>}${LOCAL_ENV_FILE:+ / $LOCAL_ENV_FILE} or the environment"
    pw goto "$APP_URL$LOGIN_PATH" >/dev/null 2>&1
    pw fill "$USERNAME_SELECTOR" "$USERNAME" >/dev/null 2>&1 || blocked "login form not found at $APP_URL$LOGIN_PATH ($USERNAME_SELECTOR)"
    pw fill "$PASSWORD_SELECTOR" "$PASSWORD" >/dev/null 2>&1 || blocked "password field not found at $APP_URL$LOGIN_PATH ($PASSWORD_SELECTOR)"
    if [ -z "$SUBMIT_SELECTOR" ] || [ "$SUBMIT_SELECTOR" = Enter ]; then pw press Enter >/dev/null 2>&1
    else pw click "$SUBMIT_SELECTOR" >/dev/null 2>&1 || blocked "submit control not found at $APP_URL$LOGIN_PATH ($SUBMIT_SELECTOR)"; fi
    if ! signed_in 30000; then
      why=$(pw run-code "async page => (await page.locator('body').innerText()).match(new RegExp($(js_str "${FAILURE_REGEX:-invalid|incorrect}") + '[^\\n]*', 'i'))?.[0] || page.url()")
      blocked "sign-in as $ROLE_LC failed: $why"
    fi
    mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR" && pw state-save "$STATE" >/dev/null 2>&1 && chmod 600 "$STATE" 2>/dev/null
  fi
fi

# ── FILL 1: the replay procedure — role/label locators only, never snapshot refs (e12) ───────────────
# Replace the `blocked` line and these examples with the real steps; end each in `|| blocked "step N: ..."`.
blocked "FILL 1 not written"
# pw goto  "$APP_URL/<route>"                        >/dev/null 2>&1 || blocked "step 1: could not open <route>"
# pw click "getByRole('button', { name: '<name>' })" >/dev/null 2>&1 || blocked "step 2: <name> button not found"
# pw fill  "getByLabel('<label>')" "<value>"         >/dev/null 2>&1 || blocked "step 3: <label> field not found"

# ── FILL 2: the grader lives in replay-grader.js next to this file ────────────────────────────────────
[ -f "$HERE/replay-grader.js" ] || blocked "replay-grader.js missing next to replay-oracle.sh"
OUT=$(pw run-code --filename="$HERE/replay-grader.js" 2>&1) || blocked "grader threw: $(printf '%s' "$OUT" | tail -c 400)"
PASS_VAL=$(printf '%s' "$OUT" | python3 -c 'import json,sys
try: d=json.loads(sys.stdin.read()); print("true" if d.get("pass") is True else "false" if d.get("pass") is False else "none", end="")
except Exception: print("none", end="")')
OBS=$(printf '%s' "$OUT" | python3 -c 'import json,sys
try: print(json.loads(sys.stdin.read()).get("observed",""), end="")
except Exception: print("", end="")')
case "$PASS_VAL" in
  true)  say true  "$OBS"; exit 0 ;;
  false) say false "$OBS"; exit 1 ;;
  *)     blocked "grader returned no {pass, observed}: $(printf '%s' "$OUT" | tail -c 400)" ;;
esac
