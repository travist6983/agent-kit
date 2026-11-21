#!/usr/bin/env bash
# Regression: dev-harden's deterministic half. rule_probe.py must refuse a rule that would mislead
# the gate (weak message, id collision, a fixture that disproves it), count the copies already in
# a repo, refuse --apply without a human --by, and leave conventions.json byte-identical apart from
# the appended rule. collect_candidates.py must group by fingerprint, gate on recurrence, and skip
# what is already decided. Scratch dirs only; the shipped conventions file is never written.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
ROOT_REPO="$(cd "$SK/../.." && pwd)"
PROBE="$SK/dev-harden/scripts/rule_probe.py"
COLLECT="$SK/dev-harden/scripts/collect_candidates.py"
SHIPPED="$ROOT_REPO/.claude/loops/conventions.json"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }
jp(){ python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(eval(sys.argv[1]))" "$2" <<<"$1" 2>/dev/null || echo ERR; }
eq(){ if [ "$2" = "$3" ]; then ok; else bad "$1 (got $2, want $3)"; fi; }

# ── 1. the shipped rules all tell the agent what to do instead ───────────────
out="$(python3 "$PROBE" --audit --conventions "$SHIPPED")"; rc=$?
eq "audit of shipped conventions" "$rc" 0
eq "audit status" "$(jp "$out" 'd["status"]')" audit-pass

# ── 2. a good rule passes and inventories existing copies ────────────────────
TGT="$T/target"; mkdir -p "$TGT/src/__tests__"; cd "$TGT"; git init -q -b main
git config user.email t@example.com; git config user.name t
echo 'setTimeout(() => close(), 0)' > src/a.ts
echo 'window.setTimeout(done, 0);' > src/b.tsx
echo 'setTimeout(() => close(), 0)' > src/__tests__/a.test.ts
echo 'setTimeout(tick, 250)' > src/c.ts
git add -A; git commit -qm base; cd - >/dev/null
cat > "$T/good.json" <<'J'
{"rule": {"id": "no-zero-delay-timer", "severity": "high", "include_path": "\\.(js|jsx|ts|tsx)$",
  "skip_tests": true, "pattern": "\\bsetTimeout\\s*\\([^;]*,\\s*0\\s*\\)",
  "rule": "Zero-delay timer used to paper over an ordering bug",
  "fix": "Do not add a timer here. The flash means two pieces of state disagree for one frame: keep the value in one place.",
  "heuristic": false},
 "fixtures": {"must_trip": [{"path": "src/menu.tsx", "text": "setTimeout(() => close(), 0)\n"}],
              "must_pass": [{"path": "src/menu.tsx", "text": "setTimeout(tick, 250)\n"}]}}
J
out="$(python3 "$PROBE" --candidate "$T/good.json" --target-repo "$TGT" --conventions "$SHIPPED")"; rc=$?
eq "good rule probe rc" "$rc" 0
eq "good rule status" "$(jp "$out" 'd["status"]')" probe-pass
eq "inventory skips tests, finds 2" "$(jp "$out" 'd["inventory"]["count"]')" 2
eq "would block" "$(jp "$out" 'd["would_block"]')" True

# ── 3. a bad rule is refused, every class named ──────────────────────────────
cat > "$T/bad.json" <<'J'
{"rule": {"id": "no-debug-console", "severity": "high", "include_path": "\\.(js|ts)$",
  "pattern": "setTimeout", "rule": "No timers", "fix": "No timers", "heuristic": false},
 "fixtures": {"must_trip": [{"path": "src/a.ts", "text": "setTimeout(f, 0)\n"}],
              "must_pass": [{"path": "src/a.ts", "text": "setTimeout(f, 250)\n"}]}}
J
out="$(python3 "$PROBE" --candidate "$T/bad.json" --conventions "$SHIPPED")"; rc=$?
eq "bad rule rc" "$rc" 3
classes="$(jp "$out" '",".join(sorted({f["class"] for f in d["failures"]}))')"
eq "bad rule classes" "$classes" "false-positive,id-collision,weak-message"
cat > "$T/nofx.json" <<'J'
{"rule": {"id": "x-rule", "severity": "low", "include_path": ".", "pattern": "x(",
  "rule": "x", "fix": "x"}}
J
out="$(python3 "$PROBE" --candidate "$T/nofx.json" --conventions "$SHIPPED")"; rc=$?
eq "invalid regex rc" "$rc" 3
case "$(jp "$out" '",".join(f["class"] for f in d["failures"])')" in *invalid-rule*) ok;; *) bad "invalid regex not classed invalid-rule";; esac

# ── 4. apply is a human step, and preserves the file ─────────────────────────
cp "$SHIPPED" "$T/conv.json"
python3 "$PROBE" --candidate "$T/good.json" --conventions "$T/conv.json" --apply >/dev/null; rc=$?
eq "apply without --by" "$rc" 2
cmp -s "$SHIPPED" "$T/conv.json" && ok || bad "apply without --by changed the file"
cp "$ROOT_REPO/contracts/samples/EXAMPLE-harden-rule/harden.json" "$T/harden.json"
python3 "$PROBE" --candidate "$T/good.json" --conventions "$T/conv.json" --apply --by tester \
  --artifact "$T/harden.json" --lesson-ref PROJ-1 >/dev/null; rc=$?
eq "apply with --by" "$rc" 0
python3 - "$SHIPPED" "$T/conv.json" <<'PY' && ok || bad "apply did not preserve the existing rules byte for byte"
import json, sys
a, b = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
assert b["rules"][:-1] == a["rules"] and b["rules"][-1]["id"] == "no-zero-delay-timer"
assert "approved by tester" in b["rules"][-1]["_comment_provenance"]
assert {k: v for k, v in b.items() if k != "rules"} == {k: v for k, v in a.items() if k != "rules"}
PY
eq "artifact flipped" "$(jp "$(cat "$T/harden.json")" 'd["status"]+":"+d["applied"]["by"]')" "rule-applied:tester"
python3 "$ROOT_REPO/contracts/validate.py" "$ROOT_REPO/contracts/dev-harden.json" "$T/harden.json" >/dev/null && ok || bad "applied artifact no longer validates"
out="$(python3 "$PROBE" --candidate "$T/good.json" --conventions "$T/conv.json")"
case "$(jp "$out" '",".join(f["class"] for f in d["failures"])')" in *id-collision*) ok;; *) bad "re-probe after apply should collide";; esac
# the scanner itself now enforces the applied rule with its fix text
SR="$T/scanrepo"; mkdir -p "$SR/src"; cd "$SR"; git init -q -b main
git config user.email t@example.com; git config user.name t
echo base > README.md; git add -A; git commit -qm base; git checkout -qb feat
echo 'setTimeout(() => close(), 0)' > src/menu.ts; git add -A; git commit -qm change; cd - >/dev/null
out="$(bash "$SK/dev-pre-pr/scripts/standards_check.sh" --repo "$SR" --base main --rules "$T/conv.json" 2>/dev/null)"
echo "$out" | grep -q 'keep the value in one place' && ok || bad "gate did not report the applied rule's fix text"

# ── 5. collect: group, recurrence gate, decided skip ─────────────────────────
E="$T/runs"; mkdir -p "$E/PROJ-1" "$E/PROJ-2"
cp "$ROOT_REPO/contracts/samples/EXAMPLE-review-harden/review.json" "$E/PROJ-1/"
cp "$ROOT_REPO/contracts/samples/EXAMPLE-review-harden/review.json" "$E/PROJ-2/"
printf '%s\n' '{"source":"human","ref":"s1","statement":"Query helpers take the workspace first"}' \
  'not json' '{"source":"fix_lesson","ref":"fix_lessons#9","statement":"Do not retry on 4xx"}' > "$T/q.jsonl"
out="$(python3 "$COLLECT" --evid-root "$E" --queue "$T/q.jsonl")"
eq "timer grouped across tickets" "$(jp "$out" '[c["occurrences"] for c in d["candidates"] if c["fingerprint"]=="no-zero-delay-timer"][0]')" 2
eq "human queue is ready" "$(jp "$out" '[c["ready"] for c in d["candidates"] if c["fingerprint"].startswith("query-helpers")][0]')" True
eq "one-off lesson waits" "$(jp "$out" '[c["ready"] for c in d["candidates"] if c["fingerprint"]=="do-not-retry-on-4xx"][0]')" False
eq "bad queue line skipped" "$(jp "$out" 'len(d["skipped"])')" 1
python3 -c "import json,sys;json.dump([c for c in json.loads(sys.argv[1])['candidates'] if c['fingerprint']=='no-zero-delay-timer'][0],open(sys.argv[2],'w'))" "$out" "$T/c0.json"
python3 "$PROBE" --candidate "$T/c0.json" --conventions "$SHIPPED" >/dev/null && ok || bad "a collector candidate does not probe as is"
mkdir -p "$E/harden/no-zero-delay-timer"; cp "$ROOT_REPO/contracts/samples/EXAMPLE-harden-rule/harden.json" "$E/harden/no-zero-delay-timer/"
out="$(python3 "$COLLECT" --evid-root "$E" --queue "$T/q.jsonl")"
eq "decided fingerprint skipped" "$(jp "$out" '",".join(x["fingerprint"] for x in d["decided"])')" no-zero-delay-timer

echo "  harden: $pass passed, $fail failed"
[ "$fail" = 0 ]
