#!/usr/bin/env bash
# Regression: dev-escalate handoff renderer — assembles full loop memory from artifacts. No DB/network.
# Covers C1 (string design_decisions), C2 (loud failure, no silent empty handoff),
# M1/M4 (contract v1.2.0 media[] + media/ dir scan + legacy shapes).
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
RENDER="$SK/dev-escalate/scripts/render_escalation.py"
ROOT="$(mktemp -d)"; TD="$ROOT/PROJ-123"; mkdir -p "$TD/media"
cat > "$TD/intake.json"     <<'EOF'
{"key":"PROJ-123","area":"Task Board","recipe":"dev-loop-hard-defect","risk_class":"hard","confidence":62}
EOF
cat > "$TD/brief.json"      <<'EOF'
{"key":"PROJ-123","surface":"Project task board","repro_steps":["open the Q3 Launch project","remove a task"],"expected_verbatim":"task removed and stays removed","actual_verbatim":"task stays after a success toast"}
EOF
# ratified v1.2.0 shape: media is an ARRAY of {kind,url,label}; replay_spec names the oracle
cat > "$TD/repro.json"      <<'EOF'
{"status":"reproduced","surface":"state-mutation","symptom_quote":"task stays after a success toast","app_map_writes":[],"replay_spec":"replay-oracle.sh","media":[{"kind":"before-gif","url":"https://evidence.example.com/PROJ-123-before.gif?sig=1","label":"Reproduced (before) — GIF"}]}
EOF
# real runs emit design_decisions as plain STRINGS — must not crash (C1)
cat > "$TD/fix.json"        <<'EOF'
{"status":"could-not-fix","failure_class":"bypasser-missed","is_root_cause":false,"root_cause":"shared remove helper bypassed by 3 call sites","confidence":40,"design_decisions":["Kept the shared helper; patched the deepest call site","{\"title\":\"Dict form\",\"kind\":\"alt-fix\",\"decision\":\"n/a\",\"alternatives_considered\":[\"per-site patches\"],\"reviewer_must_confirm\":\"helper is the right seam\"}"]}
EOF
python3 - "$TD/fix.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d["design_decisions"][1] = json.loads(d["design_decisions"][1])  # one string + one dict entry
json.dump(d, open(p, "w"))
PY
cat > "$TD/verify.json"     <<'EOF'
{"verdict":"fail","confidence":70,"masking_suspected":false,"video2":{"gif":"media/PROJ-123-after.gif"}}
EOF
cat > "$TD/regression.json" <<'EOF'
{"status":"unrelated-break","next_action":"escalate","failed_specs":[{"spec":"tests/invoices/totals.test.js > includes tax","class":"unrelated-break"}]}
EOF
touch "$TD/media/PROJ-123-before.mp4" "$TD/media/PROJ-123-after.gif"
OUT=$(python3 "$RENDER" --task-dir "$TD" --reason "unrelated-break")
rc=$?
pass=0; fail=0
ck(){ if echo "$OUT" | grep -q "$2"; then pass=$((pass+1)); else echo "  [FAIL] $1"; fail=$((fail+1)); fi; }
[ "$rc" = 0 ] && pass=$((pass+1)) || { echo "  [FAIL] happy path exit 0"; fail=$((fail+1)); }
ck "failure class"              "unrelated-break"
ck "root cause"                 "shared remove helper"
ck "expected verbatim"          "stays removed"
ck "area"                       "Task Board"
ck "failing test named"         "tests/invoices/totals.test.js > includes tax"
ck "dry_run default true"       '"dry_run": true'
ck "C1 string decision"         "Kept the shared helper"
ck "C1 dict decision"           "per-site patches"
ck "M1 contract media\[\] URL"  "evidence.example.com/PROJ-123-before.gif"
ck "M1 media/ dir scan"         "media/PROJ-123-before.mp4"
ck "M1 legacy video2"           "PROJ-123-after.gif"
ck "brief steps win repro slot" "open the Q3 Launch project"

# PR thread bar (dev-fix contract v1.2.0) — must_read entries render as actions; everything else,
# including the plan-synthesis runner-up, renders as one line of context with no "please confirm"
MR="$ROOT/mustread"; mkdir -p "$MR"
cat > "$MR/fix.json" <<'EOF'
{"key":"PROJ-124","status":"fixed","root_cause":"x","is_root_cause":true,"confidence":80,
 "design_decisions":[
  {"title":"Export totals still wrong","decision":"Needs a migration R6 forbids.","must_read":true,"must_read_reason":"ticket-not-fully-fixed","reviewer_must_confirm":"Ticket the migration or hold the PR?"},
  {"title":"Fixed the shared Checkbox","decision":"Watch the prop instead of a fourth key workaround.","alternatives_considered":["a fourth key workaround"],"must_read":false,"reviewer_must_confirm":"Agree?"}],
 "plan_synthesis":{"chosen":{"strategy":"fix the component"},"alternatives":[{"strategy":"key the Edit column","why_not":"symptom-level"}],"considered":2}}
EOF
MOUT=$(python3 "$RENDER" --task-dir "$MR" --reason staged | python3 -c 'import json,sys; print(json.load(sys.stdin)["comment"])')
ckm(){ if echo "$MOUT" | grep -q "$2"; then pass=$((pass+1)); else echo "  [FAIL] $1"; fail=$((fail+1)); fi; }
ckm "must_read entry is under Needs your decision" "Needs your decision"
ckm "must_read entry carries its reason"          "\[ticket-not-fully-fixed\]"
ckm "must_read entry carries the ask"             "Please confirm: Ticket the migration or hold the PR?"
ckm "non-must-read entry is one line of context"  "^- Fixed the shared Checkbox — Watch the prop instead of a fourth key workaround. (also considered: a fourth key workaround)"
ckm "plan-synthesis runner-up is in the context"  "Runner-up strategy (re-fix candidate): key the Edit column — not chosen: symptom-level"
[ "$(echo "$MOUT" | grep -c 'Please confirm')" = 1 ] && pass=$((pass+1)) || { echo "  [FAIL] only the must_read entry asks for confirmation"; fail=$((fail+1)); }
# noticed_not_changed + review.md's "Noticed, not changed" reach the handoff (the STAGE path never runs infra-ship)
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); d['noticed_not_changed']=['UpdateProjectPermissions never checks canEdit']; json.dump(d, open(sys.argv[1],'w'))" "$MR/fix.json"
printf '# Review\n## Findings\n- none\n## Noticed, not changed\n- ProjectSettingsPage full-replacement Save can lose concurrent grants\n- updateprojectpermissions never checks canedit\n' > "$MR/review.md"
NOUT=$(python3 "$RENDER" --task-dir "$MR" --reason staged | python3 -c 'import json,sys; print(json.load(sys.stdin)["comment"])')
echo "$NOUT" | grep -q "Noticed, not changed" && pass=$((pass+1)) || { echo "  [FAIL] handoff has a Noticed, not changed block"; fail=$((fail+1)); }
echo "$NOUT" | grep -q "^- UpdateProjectPermissions never checks canEdit$" && pass=$((pass+1)) || { echo "  [FAIL] fix.json noticed_not_changed rendered"; fail=$((fail+1)); }
echo "$NOUT" | grep -q "^- ProjectSettingsPage full-replacement Save can lose concurrent grants$" && pass=$((pass+1)) || { echo "  [FAIL] review.md noticed bullets rendered"; fail=$((fail+1)); }
[ "$(echo "$NOUT" | grep -ci 'updateprojectpermissions')" = 1 ] && pass=$((pass+1)) || { echo "  [FAIL] noticed items deduped case-insensitively"; fail=$((fail+1)); }
echo "$OUT" | python3 -c 'import json,sys; c=json.load(sys.stdin)["comment"]; sys.exit(0 if "Noticed, not changed" in c and "(none)" in c.split("Noticed, not changed",1)[1][:120] else 1)' && pass=$((pass+1)) || { echo "  [FAIL] no noticed items renders (none)"; fail=$((fail+1)); }
# the legacy fixture above has no must_read anywhere → no action section, all context
if echo "$OUT" | grep -q "Needs your decision"; then echo "  [FAIL] legacy entries must not render as actions"; fail=$((fail+1)); else pass=$((pass+1)); fi

# M4 — with no brief.json, the ratified replay_spec (not the extinct 'method' key) fills the repro slot
SPECONLY="$ROOT/speconly"; mkdir -p "$SPECONLY"; cp "$TD/repro.json" "$SPECONLY/"
if python3 "$RENDER" --task-dir "$SPECONLY" 2>/dev/null | grep -q "replay oracle: replay-oracle.sh"; then
  pass=$((pass+1)); else echo "  [FAIL] M4 replay_spec fallback"; fail=$((fail+1)); fi

# C2 — loud failures, never a silent empty handoff
if python3 "$RENDER" --task-dir "$TD" --config /nonexistent.json >/dev/null 2>&1; then
  echo "  [FAIL] C2 missing config must exit non-zero"; fail=$((fail+1)); else pass=$((pass+1)); fi
EMPTY="$ROOT/empty"; mkdir -p "$EMPTY"
if python3 "$RENDER" --task-dir "$EMPTY" >/dev/null 2>&1; then
  echo "  [FAIL] C2 empty task-dir must exit non-zero"; fail=$((fail+1)); else pass=$((pass+1)); fi
CORRUPT="$ROOT/corrupt"; mkdir -p "$CORRUPT"; echo '{nope' > "$CORRUPT/repro.json"
if python3 "$RENDER" --task-dir "$CORRUPT" >/dev/null 2>&1; then
  echo "  [FAIL] C2 corrupt artifact must exit non-zero"; fail=$((fail+1)); else pass=$((pass+1)); fi

echo "  escalation_render: $pass passed, $fail failed"
rm -rf "$ROOT"
[ "$fail" = 0 ]
