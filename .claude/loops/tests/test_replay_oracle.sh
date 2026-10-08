#!/usr/bin/env bash
# Regression: qa-repro's replay-oracle template — the exit-code contract qa-repro, dev-fix, qa-verify,
# infra-prod-verify and the recipe router all route on (0 GREEN · 1 RED · 2 BLOCKED, one JSON line).
# Hermetic: a static page on 127.0.0.1, ROLE=none (no sign-in), scratch REPLAY_STATE_DIR. No brain, no target app.
# The production refusal is proven against the LIVE local page (PROD_HOSTS=127.0.0.1): if the refusal ever
# stopped firing, the green oracle would reach the page and exit 0 — it cannot pass by being unreachable.
# SKIPS cleanly when playwright-cli is not installed.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
A="$SK/qa-repro/assets"
if ! command -v playwright-cli >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  echo "  replay_oracle: SKIP (needs playwright-cli, python3 and curl)"; exit 0
fi
bash -n "$A/replay-oracle.template.sh" || { echo "  [FAIL] template does not parse"; exit 1; }

ROOT="$(mktemp -d)"; SITE="$ROOT/site"; mkdir -p "$SITE"
cat > "$SITE/index.html" <<'EOF'
<!doctype html><html><head><title>Widgets</title></head>
<body><h1>Widgets: 3</h1><button>Save</button><label>Name <input></label></body></html>
EOF
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }
PORT=$(free_port); DEAD=$(free_port)
(cd "$SITE" && exec python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1) & SRV=$!
trap 'kill $SRV 2>/dev/null; wait $SRV 2>/dev/null; rm -rf "$ROOT"' EXIT
for _ in $(seq 1 50); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.1; done
URL="http://127.0.0.1:$PORT"

# mk NAME FILL1 [GRADER] — a filled copy of the template; FILL1 replaces the `blocked "FILL 1 not written"` line.
# FILL1 "-" keeps the template's placeholder; GRADER omitted keeps the template's placeholder grader.
mk() {
  local d="$ROOT/$1"; mkdir -p "$d"
  cp "$A/replay-grader.template.js" "$d/replay-grader.js"
  [ -n "${3:-}" ] && printf '%s\n' "$3" > "$d/replay-grader.js"
  FILL1="$2" python3 - "$A/replay-oracle.template.sh" "$d/replay-oracle.sh" <<'PY'
import os, sys
src = open(sys.argv[1]).read(); fill = os.environ["FILL1"]
line = 'blocked "FILL 1 not written"\n'
assert src.count(line) == 1, "template FILL 1 sentinel missing"
open(sys.argv[2], "w").write(src if fill == "-" else src.replace(line, fill + "\n"))
PY
  echo "$d/replay-oracle.sh"
}

pass=0; fail=0
# run NAME WANT_EXIT WANT_PASS ORACLE [ENV...] — asserts the exit code AND exactly one JSON line with that `pass`
# (the line is kept in LAST_OUT so `obs` can check WHY it blocked)
run() {
  local name="$1" want="$2" wantpass="$3" oracle="$4"; shift 4
  local out rc
  out=$(env -u APP_URL -u ROLE -u ALLOW_PROD -u PROD_HOSTS -u ORACLE_ROLES REPLAY_STATE_DIR="$ROOT/state" E2E_ENV_FILE="$ROOT/no-such.env" "$@" bash "$oracle" 2>/dev/null); rc=$?
  LAST_OUT="$out"
  local got
  got=$(printf '%s' "$out" | python3 -c 'import json,sys
lines=sys.stdin.read().splitlines()
if len(lines)!=1: print("lines=%d" % len(lines)); raise SystemExit
try: d=json.loads(lines[0])
except Exception: print("not-json"); raise SystemExit
print({True:"true",False:"false",None:"null"}.get(d.get("pass"),"bad") if set(d)=={"pass","observed"} else "bad-keys")')
  if [ "$rc" = "$want" ] && [ "$got" = "$wantpass" ]; then pass=$((pass+1));
  else echo "  [FAIL] $name: exit=$rc (want $want) pass=$got (want $wantpass) out=[$out]"; fail=$((fail+1)); fi
}
# obs NAME SUBSTRING — the last run's `observed` names the reason (a BLOCKED for the wrong reason is a false pass)
obs() { case "$LAST_OUT" in *"$2"*) pass=$((pass+1));; *) echo "  [FAIL] $1: observed lacks '$2' — out=[$LAST_OUT]"; fail=$((fail+1));; esac; }

STEPS="pw goto \"\$APP_URL/\" >/dev/null 2>&1 || blocked \"step 1: could not open /\"
pw click \"getByRole('button', { name: 'Save' })\" >/dev/null 2>&1 || blocked \"step 2: Save button not found\""
GREEN="async page => { const t = await page.locator('h1').innerText(); return { pass: t === 'Widgets: 3', observed: 'h1=' + t }; }"
RED="async page => { const t = await page.locator('h1').innerText(); return { pass: t === 'Widgets: 0', observed: 'h1=' + t }; }"
THROW="async page => { throw new Error('grader boom'); }"

run "green grader -> 0"                      0 true  "$(mk green "$STEPS" "$GREEN")" APP_URL="$URL" ROLE=none
run "red grader -> 1"                        1 false "$(mk red "$STEPS" "$RED")"     APP_URL="$URL" ROLE=none
run "throwing grader -> 2"                   2 null  "$(mk throw "$STEPS" "$THROW")" APP_URL="$URL" ROLE=none
run "missing FILL-1 locator -> 2 (not graded)" 2 null "$(mk nolocator "pw goto \"\$APP_URL/\" >/dev/null 2>&1 || blocked \"step 1\"
pw click \"getByRole('button', { name: 'Nope' })\" >/dev/null 2>&1 || blocked \"step 2: Nope button not found\"" "$GREEN")" APP_URL="$URL" ROLE=none
run "failed goto in FILL 1 -> 2"             2 null  "$(mk deadgoto "pw goto \"http://127.0.0.1:$DEAD/\" >/dev/null 2>&1 || blocked \"step 1: could not open\"" "$GREEN")" APP_URL="$URL" ROLE=none
run "unfilled template -> 2 (never RED)"     2 null  "$(mk unfilled -)"               APP_URL="$URL" ROLE=none
run "placeholder grader -> 2 (never RED)"    2 null  "$(mk nograder "$STEPS")"        APP_URL="$URL" ROLE=none
run "dead host -> 2"                         2 null  "$ROOT/green/replay-oracle.sh"   APP_URL="http://127.0.0.1:$DEAD" ROLE=none
run "prod host, no ALLOW_PROD -> 2"          2 null  "$ROOT/green/replay-oracle.sh"   APP_URL="$URL" PROD_HOSTS="127.0.0.1" ROLE=none
obs "prod host, no ALLOW_PROD: refused as production" "refusing production host"
run "prod host, ALLOW_PROD but not PROD-SAFE -> 2" 2 null "$ROOT/green/replay-oracle.sh" APP_URL="$URL" PROD_HOSTS="127.0.0.1" ALLOW_PROD=1 ROLE=none
obs "prod host, not PROD-SAFE: refused as production" "not marked # PROD-SAFE: read-only"
run "prod host glob (*.example.com) -> 2"    2 null  "$ROOT/green/replay-oracle.sh"   APP_URL="https://app.example.com/" PROD_HOSTS="*.example.com" ROLE=none
obs "prod host glob: refused before any request" "refusing production host"
run "prod host, ALLOW_PROD + PROD-SAFE -> 0 (the gate is not a blanket block)" 0 true \
  "$(mk prodsafe "# PROD-SAFE: read-only
$STEPS" "$GREEN")" APP_URL="$URL" PROD_HOSTS="127.0.0.1" ALLOW_PROD=1 ROLE=none
run "invalid ROLE -> 2"                      2 null  "$ROOT/green/replay-oracle.sh"   APP_URL="$URL" ROLE=wizard
obs "invalid ROLE: blocked as an invalid role" "invalid ROLE"
run "missing APP_URL -> 2"                   2 null  "$ROOT/green/replay-oracle.sh"   ROLE=none

# ROLE never reaches a shell eval, and the credential file is read literally, never sourced
run "ROLE injection -> 2, nothing executed"  2 null  "$ROOT/green/replay-oracle.sh"   APP_URL="$URL" ROLE="OWNER_\$(touch $ROOT/pwned-role)"
[ ! -e "$ROOT/pwned-role" ] && pass=$((pass+1)) || { echo "  [FAIL] ROLE was executed"; fail=$((fail+1)); }
printf 'E2E_OWNER_USERNAME=$(touch %s/pwned-env)\nE2E_OWNER_PASSWORD=`touch %s/pwned-env2`\n' "$ROOT" "$ROOT" > "$ROOT/creds.env"
out=$(env -u ALLOW_PROD -u PROD_HOSTS -u ORACLE_ROLES -u E2E_OWNER_USERNAME -u E2E_OWNER_PASSWORD REPLAY_STATE_DIR="$ROOT/state" E2E_ENV_FILE="$ROOT/creds.env" APP_URL="$URL" ROLE=OWNER bash "$ROOT/green/replay-oracle.sh" 2>/dev/null); rc=$?
[ "$rc" = 2 ] && [ ! -e "$ROOT/pwned-env" ] && [ ! -e "$ROOT/pwned-env2" ] && pass=$((pass+1)) \
  || { echo "  [FAIL] credential file executed or sign-in not BLOCKED: exit=$rc out=[$out]"; fail=$((fail+1)); }
# ...and the values WERE read (literally): it got past "no credentials" to the sign-in form itself.
case "$out" in *"no credentials"*) echo "  [FAIL] credential file not read at all — out=[$out]"; fail=$((fail+1));; *) pass=$((pass+1));; esac
run "signed-in role, no credential file -> 2" 2 null "$ROOT/green/replay-oracle.sh"   APP_URL="$URL" ROLE=OWNER
obs "signed-in role, no credential file: names the missing variables" "E2E_OWNER_USERNAME / E2E_OWNER_PASSWORD"

echo "  replay_oracle: $pass passed, $fail failed"
[ "$fail" = 0 ]
