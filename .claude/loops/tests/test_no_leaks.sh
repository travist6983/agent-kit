#!/usr/bin/env bash
# Regression: the kit stays generic. No former-employer tooling, no personal projects,
# no one person's machine paths. Overlays hold that; the kit never does.
# Patterns are assembled from pieces so this file does not match itself.
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
pass=0; fail=0
t(){ local desc="$1" pat="$2"
  hits=$(cd "$ROOT" && git ls-files -co --exclude-standard -z | xargs -0 grep -nIiE "$pat" 2>/dev/null | grep -v '^\.claude/loops/tests/test_no_leaks\.sh:')
  if [ -z "$hits" ]; then pass=$((pass+1)); else echo "  [FAIL] $desc:"; echo "$hits" | head -5 | sed 's/^/      /'; fail=$((fail+1)); fi; }

t "former test-case vendor"      "test""rail"
t "former cloud auth mode"       "azure""|""\\bentra[_ ]"
t "former ticket system"         "\\bji""ra\\b"
t "former employer names"        "\\be""os\\b|at""las[- ]defect"
t "former ticket keys"           "\\bD""EV-[0-9]+"
t "personal projects"            "foun""drkids|ras""cal|lead""-scout|journey""man|pa""lay|a-1""bracket|pocket chro""nicle|ai-kids""-boxes"
t "personal config and paths"    "claude""-personal|/Users/tra""vis|travis""jterry"

echo "  no_leaks: $pass passed, $fail failed"
[ "$fail" = 0 ]
