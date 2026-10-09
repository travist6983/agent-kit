#!/usr/bin/env bash
# Regression: scripts/agent-kit drafts an overlay project from a repo and doctor judges it honestly.
# Uses a throwaway overlay and repo; never an owner's overlay.
unset AGENT_KIT_OVERLAY AGENT_KIT_PROJECT LOOPS_STACK_CONFIG
KIT="$(cd "$(dirname "$0")/../../.." && pwd)"
CLI="$KIT/scripts/agent-kit"
TMP="$(cd "$(mktemp -d)" && pwd -P)"; trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok(){ if eval "$2"; then pass=$((pass+1)); else echo "  [FAIL] $1"; fail=$((fail+1)); fi; }

mkdir -p "$TMP/overlay" "$TMP/My_Shop/web" "$TMP/My_Shop/api/tests"
git -C "$TMP/My_Shop" init -q -b trunk
printf '{"scripts":{"dev":"vite","test":"vitest run","lint":"eslint ."}}' > "$TMP/My_Shop/web/package.json"
touch "$TMP/My_Shop/web/pnpm-lock.yaml" "$TMP/My_Shop/api/uv.lock"
printf '[project]\nname="api"\n[tool.pytest.ini_options]\n[tool.ruff]\n' > "$TMP/My_Shop/api/pyproject.toml"
echo "# rules" > "$TMP/My_Shop/CLAUDE.md"
git -C "$TMP/My_Shop" add -A && git -C "$TMP/My_Shop" -c user.email=t@t -c user.name=t commit -qm init

export AGENT_KIT_OVERLAY="$TMP/overlay"
out=$("$CLI" init "$TMP/My_Shop" --ticket-prefix SH 2>&1)
CFG="$TMP/overlay/projects/my-shop/stack.config.json"
ok "init writes the normalized project folder" "[ -f '$CFG' ]"
ok "conventions and onboarding written" "[ -f '$TMP/overlay/projects/my-shop/conventions.json' ] && [ -f '$TMP/overlay/projects/my-shop/ONBOARDING.md' ]"
get(){ python3 -c "import json,sys;d=json.load(open('$CFG'));print(eval(sys.argv[1]))" "$1"; }
ok "default branch detected" "[ \"\$(get \"d['project']['default_branch']\")\" = trunk ]"
ok "ticket prefix applied" "[ \"\$(get \"d['project']['ticket_key_pattern']\")\" = '^SH-[0-9]+\$' ]"
ok "test commands each in their own subshell" "[ \"\$(get \"d['stack']['unit_test_command']\")\" = '(cd web && pnpm test) && (cd api && uv run pytest -q)' ]"
ok "start command from the dev script" "[ \"\$(get \"d['app']['start_command']\")\" = 'cd web && pnpm dev' ]"
ok "guidelines file found" "[ \"\$(get \"d['stack']['repo_guidelines_file']\")\" = '$TMP/My_Shop/CLAUDE.md' ]"
ok "a second init refuses without --force" "! '$CLI' init '$TMP/My_Shop' >/dev/null 2>&1"
"$CLI" doctor my-shop > "$TMP/doctor.txt" 2>&1; rc=$?
ok "doctor fails while app.url is unset" "[ $rc -eq 1 ] && grep -q 'FAIL  app.url' '$TMP/doctor.txt'"
ok "doctor reads the overlay config" "grep -q 'ok    config from the overlay' '$TMP/doctor.txt'"
ok "list shows the project" "'$CLI' list | grep -q '^my-shop'"
unset AGENT_KIT_OVERLAY
ok "no overlay is a clear error" "! '$CLI' list >/dev/null 2>&1"

echo "  agent_kit_cli: $pass passed, $fail failed"
[ "$fail" = 0 ]
