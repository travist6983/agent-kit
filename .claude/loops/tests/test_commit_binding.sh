#!/usr/bin/env bash
# Regression: the readiness verdict is bound to the commit that ships (commit_binding.py).
#
# The rule under test: an approval covers one specific commit. dev-pre-pr stamps the CONTENT it
# judged (path + git blob id of the change set vs the merge-base); infra-ship's check, after its
# commit and before the push, passes only when the commit is exactly that content and the verdict is
# ready|warnings with both the `judged` and `risk` blocks present. Any later edit, a judged file left
# out, an extra file committed, or no usable verdict refuses to ship.
#
# Offline: throwaway git repos with no remote and a stack.config fixture (LOOPS_STACK_CONFIG). No
# brain, no VCS host, no model calls. Scratch dir, self-cleaning.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
FW="$(cd "$SK/../.." && pwd)"
CB="$SK/dev-pre-pr/scripts/commit_binding.py"
ROOT="$(mktemp -d)"; trap 'rm -rf "$ROOT"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }
export LOOPS_REPO_ROOT="$FW"
unset KB_DATABASE_URL KB_CONFIG AGENT_KIT_OVERLAY KB_ALLOW_UNSHARED
printf '{"project": {"name": "commit-binding-test", "default_branch": "main"}}\n' > "$ROOT/stack.json"
export LOOPS_STACK_CONFIG="$ROOT/stack.json"

n=0
mkrepo(){ n=$((n+1)); R="$ROOT/repo$n"; mkdir -p "$R"
  ( cd "$R" && git init -q -b main && git config user.email t@t && git config user.name t
    mkdir -p src && printf 'a\n' > src/a.py && printf 'b\n' > src/b.py && printf 'readme\n' > README.md
    git add -A && git commit -qm base && git checkout -qb fix/x ); }
g(){ git -C "$R" "$@"; }
stamp(){ STAMP=$(python3 "$CB" stamp --repo "$R" "$@"); }
# prepr <status> [with_judged=1] [with_risk=1] [key]
prepr(){ printf '%s' "$STAMP" | python3 -c "
import json, sys
s = json.load(sys.stdin)['judged']
doc = {'status': '$1', 'next_action': 'ship-ok' if '$1' != 'blocked' else 're-fix', 'key': '${4:-PROJ-1}',
       'checks': {}, 'blockers': [] if '$1' != 'blocked' else ['x'], 'warnings': [], 'summary': 't'}
if '${2:-1}' == '1': doc['judged'] = s
if '${3:-1}' == '1':
    doc['risk'] = {'triage_tier': 'T1', 'diff_tier': 'T1', 'tier': 'T1', 'escalated': False, 'reasons': ['held'],
                   'human_gate': {'demanded': False, 'triggers': []}, 'human_approvals_required': 1,
                   'agent_approval_counts_as_human': False}
json.dump(doc, open('$ROOT/pre-pr.json', 'w'))"; }
check(){ OUT=$(python3 "$CB" check --pre-pr "${PP:-$ROOT/pre-pr.json}" --repo "$R" "$@"); RC=$?; }
f(){ printf '%s' "$OUT" | python3 -c "import sys,json; v=json.load(sys.stdin).get('$1'); print(v if isinstance(v,str) else json.dumps(v))" 2>/dev/null; }
expect(){ local desc="$1" rc="$2" field="$3" val="$4"
  if [ "$RC" = "$rc" ] && { [ -z "$field" ] || [ "$(f "$field")" = "$val" ]; }; then ok
  else bad "$desc: exit $RC $field=$(f "$field") (want exit $rc $field=$val)"; fi; }

# ---- 1. stamp is deterministic and independent of mtime ----------------------------------------
mkrepo; printf 'a2\n' > "$R/src/a.py"; stamp; S1="$STAMP"; touch -d '2001-01-01' "$R/src/a.py"; stamp
if [ "$(printf '%s' "$S1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["judged"]["change_fingerprint"])')" = \
     "$(printf '%s' "$STAMP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["judged"]["change_fingerprint"])')" ]; then ok
else bad "stamp not deterministic across mtime"; fi
if printf '%s' "$STAMP" | python3 -c 'import sys,json;d=json.load(sys.stdin)["judged"];assert d["dirty"] is True and d["files"]==1 and d["change_fingerprint"].startswith("sha256:")'; then ok; else bad "stamp shape: $STAMP"; fi

# ---- 2. dirty tree stamped, exactly those files committed → bound -------------------------------
mkrepo; printf 'a2\n' > "$R/src/a.py"; printf 'new\n' > "$R/src/c.py"; g rm -q src/b.py
stamp; prepr warnings; g add -A; g commit -qm fix; check
expect "commit == judged" 0 bound true
[ "$(f commit)" = "$(g rev-parse HEAD)" ] && ok || bad "check commit != HEAD"
if f pr_body_block | grep -q 'Human approvals required before merge: \*\*1\*\*' \
   && f pr_body_block | grep -q 'Risk tier: \*\*T1\*\*' \
   && f pr_body_block | grep -q "$(g rev-parse HEAD | cut -c1-12)" \
   && f pr_body_block | grep -q 'agent verdict and fills none of them' \
   && f pr_body_block | grep -q "branch protection"; then ok; else bad "pr_body_block content: $(f pr_body_block)"; fi
check --key PROJ-1; expect "matching --key" 0 bound true

# ---- 3. a file edited after the stamp, then committed → stale-readiness ------------------------
mkrepo; printf 'a2\n' > "$R/src/a.py"; stamp; prepr ready; printf 'a3 formatted\n' > "$R/src/a.py"
g add -A; g commit -qm fix; check
expect "edited after stamp" 1 abort stale-readiness

# ---- 4. a judged new file left out of the commit → stale -----------------------------------------
mkrepo; printf 'a2\n' > "$R/src/a.py"; printf 'new\n' > "$R/src/c.py"; stamp; prepr ready
g add src/a.py; g commit -qm fix; check
expect "judged file left out" 1 abort stale-readiness

# ---- 5. an unjudged extra file committed → stale -------------------------------------------------
mkrepo; printf 'a2\n' > "$R/src/a.py"; stamp; prepr ready; printf 'extra\n' > "$R/src/d.py"
g add -A; g commit -qm fix; check
expect "extra file committed" 1 abort stale-readiness

# ---- 6. report artifacts present at stamp and not committed → still bound ------------------------
mkrepo; printf 'a2\n' > "$R/src/a.py"; printf 'r\n' > "$R/pre-pr-report.md"; printf 'p\n' > "$R/plan.md"
stamp; prepr ready; g add src/a.py; g commit -qm fix; check
expect "artifacts excluded at stamp" 0 bound true
# ...but an artifact committed by mistake is caught
mkrepo; printf 'a2\n' > "$R/src/a.py"; printf 'r\n' > "$R/pre-pr-report.md"; stamp; prepr ready
g add -A; g commit -qm fix; check
expect "artifact committed by mistake" 1 abort stale-readiness

# ---- 7. stamp on an already-committed clean tree → dirty false, check passes ------------------------
mkrepo; printf 'a2\n' > "$R/src/a.py"; g add -A; g commit -qm fix; stamp
printf '%s' "$STAMP" | python3 -c 'import sys,json;assert json.load(sys.stdin)["judged"]["dirty"] is False' && ok || bad "clean tree dirty flag"
prepr ready; check; expect "already committed" 0 bound true

# ---- 8. base advanced with unrelated files, branch rebased → still bound ----------------------------
mkrepo; printf 'a2\n' > "$R/src/a.py"; g add -A; g commit -qm fix; stamp; prepr warnings
g checkout -q main; printf 'other\n' > "$R/other.txt"; g add -A; g commit -qm unrelated; g checkout -q fix/x
g rebase -q main; check
expect "rebased onto advanced base" 0 bound true

# ---- 9. no usable verdict → readiness-unbound (loop mode is the default) -----------------------------
mkrepo; printf 'a2\n' > "$R/src/a.py"; stamp; g add -A; g commit -qm fix
PP="$ROOT/none.json" check; expect "no pre-pr.json" 1 abort readiness-unbound
printf '{"status":' > "$ROOT/broken.json"; PP="$ROOT/broken.json" check; expect "malformed" 1 abort readiness-unbound
prepr blocked; check; expect "status blocked" 1 abort readiness-unbound
prepr ready 0 1; check; expect "no judged block" 1 abort readiness-unbound
prepr ready 1 0; check; expect "no risk block" 1 abort readiness-unbound
prepr ready 1 1 PROJ-9; check --key PROJ-1; expect "wrong --key" 1 abort readiness-unbound
prepr ready; python3 - "$ROOT/pre-pr.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d["risk"]["agent_approval_counts_as_human"] = True
json.dump(d, open(sys.argv[1], "w"))
PY
check; expect "contract-invalid verdict" 1 abort readiness-unbound
PP="$ROOT/none.json" check --loop; expect "--loop, no file" 1 abort readiness-unbound

# ---- 10. --no-loop with no pre-pr.json (the ticketless draft ship) → not-judged -----------------------
PP="$ROOT/none.json" check --no-loop
expect "--no-loop no file" 0 bound false
[ "$(f reason)" = "not-judged" ] && ok || bad "--no-loop reason: $(f reason)"
f pr_body_block | grep -q 'No readiness verdict covers this commit' && ok || bad "not-judged body block"
prepr blocked; check --no-loop; expect "--no-loop with a blocked file still checked" 1 abort readiness-unbound

# ---- 11. git cannot answer → exit 2 ---------------------------------------------------------------
mkdir -p "$ROOT/notgit"; OUT=$(python3 "$CB" stamp --repo "$ROOT/notgit"); RC=$?
[ "$RC" = 2 ] && ok || bad "stamp non-git dir exit $RC"
OUT=$(python3 "$CB" check --pre-pr "$ROOT/pre-pr.json" --repo "$ROOT/notgit"); RC=$?
[ "$RC" = 2 ] && ok || bad "check non-git dir exit $RC"
OUT=$(python3 "$CB" bogus 2>/dev/null); RC=$?; [ "$RC" = 2 ] && ok || bad "bad verb exit $RC"

# ---- 12. escalated risk renders the triage tier and the human gate ---------------------------------
mkrepo; printf 'a2\n' > "$R/src/a.py"; stamp; prepr warnings; g add -A; g commit -qm fix
python3 - "$ROOT/pre-pr.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["risk"].update({"triage_tier": "T0", "diff_tier": "T2", "tier": "T2", "escalated": True,
                  "reasons": ["diff: 9 files likely touched (>= 8)"], "human_approvals_required": 2,
                  "human_gate": {"demanded": True, "triggers": ["tests-weakened"]}})
json.dump(d, open(sys.argv[1], "w"))
PY
check
if f pr_body_block | grep -q 'raised from triage T0' && f pr_body_block | grep -q 'tests-weakened' \
   && f pr_body_block | grep -q 'required before merge: \*\*2\*\*'; then ok; else bad "escalated body block: $(f pr_body_block)"; fi
[ "$(f human_approvals_required)" = 2 ] && ok || bad "check echoes approvals"

echo "  commit_binding: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
