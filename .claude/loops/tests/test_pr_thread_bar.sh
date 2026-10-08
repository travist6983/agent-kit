#!/usr/bin/env bash
# Regression: the PR thread bar (infra-ship select_pr_threads.py). A PR thread is an interrupt and
# the expected count is zero — a PR for two simple bugs once got 14 threads and the reviewer called
# every one noise. The bar must be ENFORCED by the funnel, not remembered by the agent: only
# classified must-read items post, the cap holds per PR across re-ships, and nothing posts twice.
# The funnel writes vendor-neutral thread specs {path, line, body} — exactly the kwargs of the
# vcs_host adapter's post_review_comment — and reads the adapter's list_review_threads output back.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
CLI="$REPO/lib/adapters/cli.py"
SEL="$SK/infra-ship/scripts/select_pr_threads.py"
FX="$(cd "$(dirname "$0")" && pwd)/fixtures/pr-thread-bar"
LIMITS="$(cd "$(dirname "$0")/.." && pwd)/loop-limits.json"
ROOT="$(mktemp -d)"
pass=0; fail=0
ok(){ pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }
expect_rc(){ # desc want_rc actual_rc
  if [ "$3" = "$2" ]; then ok; else bad "$1 (rc=$3, want $2)"; fi; }
jq_is(){ # desc json-file jq-expr expected
  local got; got=$(jq -r "$3" "$2" 2>/dev/null)
  if [ "$got" = "$4" ]; then ok; else bad "$1 (got '$got', want '$4')"; fi; }
top_has(){ # desc json-file grep-pattern
  if jq -r '.description_top' "$2" | grep -q -- "$3"; then ok; else bad "$1"; fi; }
cap4(){ echo '{"defect_pr_threads":{"max_per_pr":4}}' > "$ROOT/cap4.json"; echo "$ROOT/cap4.json"; }
# thread specs → the adapter's list_review_threads shape (status active, string ids t1, t2, …)
as_threads(){ # out-file spec-file...
  local out="$1"; shift
  python3 - "$out" "$@" <<'PY'
import json, sys
specs = [json.load(open(p)) for p in sys.argv[2:]]
json.dump([dict(s, thread_id="t%d" % i, status="active") for i, s in enumerate(specs, 1)], open(sys.argv[1], "w"))
PY
}

# ── the calibration case: the noisy PR's 11 decision/alternative entries → zero threads, zero banner ──────
python3 "$SEL" --fix "$FX/noisy-fix.json" > "$ROOT/noisy.json"; expect_rc "the noisy fix.json selects cleanly" 0 $?
jq_is "the noisy PR posts 0 threads"         "$ROOT/noisy.json" '.counts.threads' 0
jq_is "the noisy PR keeps all 11 off the PR" "$ROOT/noisy.json" '.counts.not_posted' 11
jq_is "the noisy PR has no banner"           "$ROOT/noisy.json" '.description_top' ""
python3 "$SEL" --fix "$FX/noisy-new-fix.json" > "$ROOT/noisyn.json"
jq_is "new-style noisy PR (must_read:false) posts 0" "$ROOT/noisyn.json" '.counts.threads' 0

# ── improvised caveats go through the funnel too; must_read:false never posts ─────────────────────
python3 "$SEL" --fix "$FX/noisy-fix.json" --candidates "$FX/caveat-candidates.json" > "$ROOT/cav.json"
jq_is "pre-existing caveat candidate posts 0" "$ROOT/cav.json" '.counts.threads' 0
jq_is "caveat is counted as a candidate"      "$ROOT/cav.json" '.counts.candidates' 12

# ── the cap: 4 must-reads at cap 2 → the top 2 by priority post, 2 land in "Before merging" ───────
python3 "$SEL" --fix "$FX/four-must-read-fix.json" --out-dir "$ROOT/threads" > "$ROOT/four.json"
expect_rc "four must-reads select cleanly" 0 $?
jq_is "tracked cap applies by default"        "$ROOT/four.json" '.cap' 2
jq_is "cap 2 → 2 threads"                     "$ROOT/four.json" '.counts.threads' 2
jq_is "overflow → 2 before-merging bullets"   "$ROOT/four.json" '.counts.before_merging' 2
jq_is "over_cap is reported"                  "$ROOT/four.json" '.counts.over_cap' 2
jq -e '.warnings | any(test("against a cap of 2"))' "$ROOT/four.json" >/dev/null && ok || bad "overflow raises a warning"
jq_is "string + must_read:false not posted"   "$ROOT/four.json" '.counts.not_posted' 2
jq_is "rank 1 = ticket-not-fully-fixed"       "$ROOT/four.json" '.threads[0].reason' ticket-not-fully-fixed
jq_is "rank 2 = human-action-required"        "$ROOT/four.json" '.threads[1].reason' human-action-required
jq_is "product-call is the last to post"      "$ROOT/four.json" '.before_merging[1].reason' product-call
top_has "banner counts every item and says where it is" "$ROOT/four.json" '^> ⚠️ \*\*4 items need your eyes before merge\*\* — 2 as active threads on this PR, 2 listed below\.'
top_has "overflow rendered as a Before merging section"   "$ROOT/four.json" '^## ⚠️ Before merging'
[ -f "$ROOT/threads/thread-1.json" ] && [ -f "$ROOT/threads/thread-2.json" ] && [ ! -f "$ROOT/threads/thread-3.json" ] && ok || bad "exactly one thread spec per posted thread"
jq_is "thread spec is exactly the post_review_comment kwargs" "$ROOT/threads/thread-1.json" 'keys | join(",")' "body,line,path"
jq_is "thread spec carries nothing vendor-shaped"  "$ROOT/threads/thread-1.json" 'has("comments") or has("threadContext") or has("status")' false
jq_is "an entry with no files is PR-level"    "$ROOT/threads/thread-1.json" '[.path, .line] | map(. // "none") | join(",")' "none,none"
jq_is "summary says it is not anchored"       "$ROOT/four.json" '.threads[0].anchored' false
jq -r '.body' "$ROOT/threads/thread-1.json" | head -1 | grep -q '^\*\*⚠️ Not fully fixed — Third symptom (export totals) not fixed\*\*$' && ok || bad "heading is label — title"
jq -r '.body' "$ROOT/threads/thread-1.json" | tail -1 | grep -q '^\*\*Before merging:\*\* ' && ok || bad "body ends in the reviewer's ask"

# anchors (cap raised to 4 through a limits FILE — --max can only lower it)
python3 "$SEL" --fix "$FX/four-must-read-fix.json" --limits "$(cap4)" --out-dir "$ROOT/all" > "$ROOT/all.json"
jq_is "cap 4 → 4 threads"                       "$ROOT/all.json" '.counts.threads' 4
jq_is "a file with no line is PR-level"         "$ROOT/all/thread-2.json" '.path // "none"' none
jq_is "path:line anchors the file"              "$ROOT/all/thread-3.json" '.path' src/components/DatePicker.jsx
jq_is "path:line anchors the line"              "$ROOT/all/thread-3.json" '.line' 33
jq_is "the anchor is repo-relative (the host adds any prefix)" "$ROOT/all/thread-3.json" '.path | startswith("/")' false
jq_is "summary says it is anchored"             "$ROOT/all.json" '.threads[2].anchored' true
jq_is "./ prefix stripped from anchor"          "$ROOT/all/thread-4.json" '.path' src/projects/TaskCard.jsx
jq_is "anchor line carried"                     "$ROOT/all/thread-4.json" '.line' 88
cat > "$ROOT/anchors.json" <<'EOF'
{"design_decisions":[
 {"title":"col","must_read":true,"must_read_reason":"product-call","reviewer_must_confirm":"a","files":["src/a.jsx:12:7"]},
 {"title":"abs","must_read":true,"must_read_reason":"product-call","reviewer_must_confirm":"b","files":["/Users/me/wt/src/b.jsx:5"]},
 {"title":"zero","must_read":true,"must_read_reason":"product-call","reviewer_must_confirm":"c","files":["src/c.jsx:0"]},
 {"title":"odd digit","must_read":true,"must_read_reason":"product-call","reviewer_must_confirm":"d","files":["src/d.jsx:²"]}]}
EOF
python3 "$SEL" --fix "$ROOT/anchors.json" --limits "$(cap4)" --out-dir "$ROOT/anch" > "$ROOT/anch.json"; expect_rc "odd anchors never crash" 0 $?
jq_is "path:line:col → the line, not the column" "$ROOT/anch/thread-1.json" '.line' 12
jq_is "path:line:col → the path without :line"   "$ROOT/anch/thread-1.json" '.path' src/a.jsx
jq_is "a filesystem path is PR-level"            "$ROOT/anch/thread-2.json" '.path // "none"' none
jq -e '.warnings | any(test("filesystem path"))' "$ROOT/anch.json" >/dev/null && ok || bad "a filesystem path warns"
jq_is "line 0 is PR-level"                       "$ROOT/anch/thread-3.json" '.line // "none"' none
jq_is "a non-ASCII digit is PR-level"            "$ROOT/anch/thread-4.json" '.line // "none"' none

# ── cap sources: 0 means none; the tracked limits file is read; a bad value falls back loudly ────
python3 "$SEL" --fix "$FX/four-must-read-fix.json" --max 0 > "$ROOT/zero.json"
jq_is "cap 0 → 0 threads"                  "$ROOT/zero.json" '.counts.threads' 0
jq_is "cap 0 → all 4 in Before merging"    "$ROOT/zero.json" '.counts.before_merging' 4
top_has "cap 0 banner points below, not at threads" "$ROOT/zero.json" '^> ⚠️ \*\*4 items need your eyes before merge\*\* — listed below\.'
python3 "$SEL" --fix "$FX/four-must-read-fix.json" --limits "$LIMITS" > "$ROOT/lim.json"
jq_is "tracked loop-limits.json is the cap source" "$ROOT/lim.json" '.cap_source' "loop-limits.json defect_pr_threads.max_per_pr"
jq_is "tracked cap is 2"                           "$ROOT/lim.json" '.cap' 2
python3 "$SEL" --fix "$FX/four-must-read-fix.json" --max 3 >/dev/null; expect_rc "--max above the cap exits 2 (it can only lower it)" 2 $?
for v in '-1' '"2"' '2.0' 'true'; do
  echo "{\"defect_pr_threads\":{\"max_per_pr\":$v}}" > "$ROOT/v.json"
  python3 "$SEL" --fix "$FX/four-must-read-fix.json" --limits "$ROOT/v.json" > "$ROOT/vout.json"
  jq_is "max_per_pr=$v falls back to 2" "$ROOT/vout.json" '.cap' 2
done
for v in '2' '"2"' '[]' 'null'; do
  echo "{\"defect_pr_threads\":$v}" > "$ROOT/v.json"
  python3 "$SEL" --fix "$FX/four-must-read-fix.json" --limits "$ROOT/v.json" > "$ROOT/vout.json"; expect_rc "defect_pr_threads=$v does not crash" 0 $?
done
python3 "$SEL" --fix "$FX/four-must-read-fix.json" --limits "$ROOT/missing.json" > "$ROOT/miss.json"
jq_is "missing limits file falls back to 2" "$ROOT/miss.json" '.cap' 2

# ── re-ship: the cap is per PR, nothing posts twice, stale specs never re-post ────────────────────
# ship 1 writes threads 1+2 into task/pr-threads; build the PR's thread list from what was posted
python3 "$SEL" --fix "$FX/four-must-read-fix.json" --out-dir "$ROOT/task/pr-threads" > "$ROOT/ship1.json"
python3 - "$ROOT/task/pr-threads" "$ROOT/existing.json" <<'PY'
import glob, json, sys
posted = [dict(json.load(open(p)), thread_id="t%d" % i, status="active")
          for i, p in enumerate(sorted(glob.glob(sys.argv[1] + "/thread-*.json")), 1)]
human = {"thread_id": "t90", "body": "**⚠️ Looks** like a human wrote this", "path": None, "line": None, "status": "active"}
system = {"thread_id": "t91", "body": None, "is_system": True}
deleted = {"thread_id": "t92", "body": "**⚠️ Your call — deleted thread**", "deleted": True, "status": "active"}
# the adapter CLI's envelope is accepted as is
json.dump({"ok": True, "role": "vcs_host", "op": "list_review_threads",
           "result": posted + [human, system, deleted]}, open(sys.argv[2], "w"))
PY
# ship 2 — same fix.json, SAME out-dir, --existing: nothing new, and thread-1/2.json must be gone
python3 "$SEL" --fix "$FX/four-must-read-fix.json" --existing "$ROOT/existing.json" --out-dir "$ROOT/task/pr-threads" > "$ROOT/ship2.json"
expect_rc "re-ship selects cleanly" 0 $?
jq_is "re-ship: both threads already on the PR"   "$ROOT/ship2.json" '.counts.already_posted' 2
jq_is "re-ship: no new thread"                    "$ROOT/ship2.json" '.counts.threads' 0
jq_is "re-ship: human/system/deleted not counted" "$ROOT/ship2.json" '.counts.existing_loop_threads' 2
[ -z "$(ls "$ROOT/task/pr-threads"/thread-*.json 2>/dev/null)" ] && ok || bad "stale thread specs removed from a reused out-dir"
top_has "re-ship banner still counts the threads on the PR" "$ROOT/ship2.json" '^> ⚠️ \*\*4 items need your eyes before merge\*\* — 2 as active threads on this PR, 2 listed below\.'
# ship 3 — a re-fix rewords both titles: same reason + same file (or no file) → no second pair of threads
python3 - "$FX/four-must-read-fix.json" "$ROOT/reworded.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for e in d["design_decisions"]:
    if isinstance(e, dict) and e.get("must_read"):
        e["title"] = "Reworded: " + e["title"]
json.dump(d, open(sys.argv[2], "w"))
PY
python3 "$SEL" --fix "$ROOT/reworded.json" --existing "$ROOT/existing.json" --out-dir "$ROOT/task/pr-threads" > "$ROOT/ship3.json"
jq_is "reworded re-fix: the per-PR cap is already used up" "$ROOT/ship3.json" '.counts.threads' 0
[ -z "$(ls "$ROOT/task/pr-threads"/thread-*.json 2>/dev/null)" ] && ok || bad "reworded re-fix writes no thread spec"
# a resolved loop thread still uses its slot, but the banner only counts active ones
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); d['result'][0]['status']='resolved'; json.dump(d, open(sys.argv[2],'w'))" "$ROOT/existing.json" "$ROOT/existing-resolved.json"
python3 "$SEL" --fix "$FX/four-must-read-fix.json" --existing "$ROOT/existing-resolved.json" > "$ROOT/ship4.json"
jq_is "a resolved loop thread still uses a slot" "$ROOT/ship4.json" '.counts.threads' 0
top_has "the banner counts only active threads"  "$ROOT/ship4.json" '^> ⚠️ \*\*3 items need your eyes before merge\*\* — 1 as active thread on this PR, 2 listed below\.'
python3 "$SEL" --fix "$FX/four-must-read-fix.json" --existing "$FX/no-decisions-fix.json" >/dev/null; expect_rc "a malformed existing-threads file exits 2" 2 $?

# ── end to end through the vcs_host adapter (git-local): post the specs, read them back ──────────
GITR="$ROOT/repo"
git init -q -b main "$GITR" && git -C "$GITR" -c user.email=loop@example.com -c user.name=loop commit -q --allow-empty -m init \
  && git -C "$GITR" checkout -q -b fix/PROJ-123
cat > "$ROOT/stack.config.json" <<EOF
{"project": {"name": "thread-bar-test", "default_branch": "main"},
 "app": {"repo_dir": "$GITR"},
 "adapters": {"vcs_host": {"provider": "git-local", "config": {"repo_dir": "$GITR"}}},
 "evidence": {"root": "$ROOT/runs"}}
EOF
vcs(){ LOOPS_STACK_CONFIG="$ROOT/stack.config.json" LOOPS_REPO_ROOT="$ROOT" python3 "$CLI" vcs_host "$@"; }
vcs open_pr branch=fix/PROJ-123 title="Fix PROJ-123" body=b > "$ROOT/e2e-pr.json"; expect_rc "git-local opens the dry-run PR" 0 $?
vcs list_review_threads id=fix/PROJ-123 > "$ROOT/e2e-before.json"
python3 "$SEL" --fix "$FX/four-must-read-fix.json" --limits "$(cap4)" --existing "$ROOT/e2e-before.json" --out-dir "$ROOT/e2e" > "$ROOT/e2e1.json"
jq_is "e2e: an empty PR takes all 4 at cap 4" "$ROOT/e2e1.json" '.counts.threads' 4
posted_ids=""; post_rc=0
for spec in "$ROOT"/e2e/thread-*.json; do
  vcs post_review_comment id=fix/PROJ-123 --json "$(cat "$spec")" > "$ROOT/e2e-post.json" || post_rc=$?
  posted_ids="$posted_ids${posted_ids:+,}$(jq -r '.result.thread_id' "$ROOT/e2e-post.json")"
done
expect_rc "e2e: every spec posts as post_review_comment kwargs" 0 "$post_rc"
vcs list_review_threads id=fix/PROJ-123 > "$ROOT/e2e-after.json"
jq_is "e2e: 4 threads on the PR"                "$ROOT/e2e-after.json" '.result | length' 4
jq_is "e2e: a posted thread opens active"       "$ROOT/e2e-after.json" '[.result[].status] | unique | join(",")' active
jq_is "e2e: the anchor survives the round trip" "$ROOT/e2e-after.json" '[.result[] | select(.line == 33) | .path] | join(",")' src/components/DatePicker.jsx
python3 "$SEL" --fix "$FX/four-must-read-fix.json" --limits "$(cap4)" --existing "$ROOT/e2e-after.json" --out-dir "$ROOT/e2e" > "$ROOT/e2e2.json"
jq_is "e2e: re-ship against the adapter's own read-back posts nothing" "$ROOT/e2e2.json" '.counts.threads' 0
jq_is "e2e: …and recognises all 4 as already posted"                    "$ROOT/e2e2.json" '.counts.already_posted' 4

# ── extra candidates join the same ranking; a duplicate across sources posts once ─────────────────
python3 "$SEL" --fix "$FX/no-decisions-fix.json" --candidates "$FX/gap-candidates.json" > "$ROOT/gap.json"
jq_is "a must-read candidate from outside fix.json posts" "$ROOT/gap.json" '.counts.threads' 1
jq_is "no design_decisions key is fine"                   "$ROOT/gap.json" '.counts.candidates' 1
top_has "singular banner" "$ROOT/gap.json" '^> ⚠️ \*\*1 item needs your eyes before merge\*\* — see the active thread on this PR\.$'
python3 -c "import json,sys; c=json.load(open(sys.argv[1])); json.dump({'design_decisions': c}, open(sys.argv[2],'w'))" "$FX/gap-candidates.json" "$ROOT/gapfix.json"
python3 "$SEL" --fix "$ROOT/gapfix.json" --candidates "$FX/gap-candidates.json" > "$ROOT/dup.json"
jq_is "the same item in fix.json and candidates posts once" "$ROOT/dup.json" '.counts.threads' 1
jq -e '.not_posted | any(.why | test("duplicate"))' "$ROOT/dup.json" >/dev/null && ok || bad "the duplicate is reported as not posted"

# ── headings are normalised, so a multi-line title still dedupes against itself ──────────────────
cat > "$ROOT/multiline.json" <<'EOF'
{"design_decisions":[{"decision":"Export totals are still wrong.\nThe export reads a denormalized column.","must_read":true,"must_read_reason":"ticket-not-fully-fixed","reviewer_must_confirm":"Ticket it\nor hold?"}]}
EOF
python3 "$SEL" --fix "$ROOT/multiline.json" --out-dir "$ROOT/ml" > /dev/null
jq -r '.body' "$ROOT/ml/thread-1.json" | head -1 | grep -q '^\*\*⚠️ Not fully fixed — Export totals are still wrong. The export reads a denormalized column.\*\*$' && ok || bad "a multi-line title renders as one heading line"
as_threads "$ROOT/ml-existing.json" "$ROOT/ml/thread-1.json"
python3 "$SEL" --fix "$ROOT/multiline.json" --existing "$ROOT/ml-existing.json" > "$ROOT/ml2.json"
jq_is "a multi-line title dedupes against itself on re-ship" "$ROOT/ml2.json" '.counts.already_posted' 1

# ── an unclassified must-read stops the ship (exit 4); bad input exits 2 — never posted around ─────────────────────────────
python3 "$SEL" --fix "$FX/bad-reason-fix.json" > "$ROOT/bad.json"; expect_rc "unknown must_read_reason exits 4 (unclassified)" 4 $?
grep -q 'set must_read:false' "$ROOT/bad.json" && ok || bad "bad-reason error says how to fix it"
jq_is "the error names the class" "$ROOT/bad.json" '.exit' 4
python3 "$SEL" --fix "$FX/no-ask-fix.json"      >/dev/null; expect_rc "must_read with no reviewer ask exits 4" 4 $?
python3 "$SEL" --fix "$FX/stringly-flag-fix.json" >/dev/null; expect_rc "non-boolean must_read exits 4" 4 $?
echo '{"design_decisions":[{"title":["a"],"must_read":true,"must_read_reason":"product-call","reviewer_must_confirm":"x"}]}' > "$ROOT/t.json"
python3 "$SEL" --fix "$ROOT/t.json" >/dev/null; expect_rc "a non-string title on a must-read exits 4" 4 $?
echo '{"design_decisions":[{"title":["a"],"must_read":false},{"title":5}]}' > "$ROOT/t2.json"
python3 "$SEL" --fix "$ROOT/t2.json" > "$ROOT/t2out.json"; expect_rc "odd titles on ignored entries never crash" 0 $?
echo '{"design_decisions":[{"title":"x","must_read":true,"must_read_reason":["product-call"],"reviewer_must_confirm":"x"}]}' > "$ROOT/t3.json"
python3 "$SEL" --fix "$ROOT/t3.json" >/dev/null; expect_rc "a list-valued reason exits 4" 4 $?
python3 "$SEL" --fix "$ROOT/nope.json"          >/dev/null; expect_rc "missing fix.json exits 2" 2 $?
python3 "$SEL" --fix "$FX/no-decisions-fix.json" --max -1 >/dev/null 2>&1; expect_rc "negative --max exits 2" 2 $?

# ── "Noticed, not changed": fix.json + the review report's bullets, deduped, 3 on the PR, rest to the report ─
cat > "$ROOT/noticed-fix.json" <<'EOF'
{"design_decisions":[],"noticed_not_changed":["UpdateProjectPermissions never checks canEdit (same workspace)","ProjectSettingsPage full-replacement Save can lose concurrent grants"]}
EOF
cat > "$ROOT/review.md" <<'EOF'
# Review — PROJ-123
## Findings
- HIGH: failed load wipes grants (fixed in cycle 1)
## Noticed, not changed
- updateprojectpermissions never checks canedit (same workspace)
- TaskListQuery issues one query per assignee
* LegacyExport ignores the workspace time zone
## Checked and cleared
- Checkbox consumers
EOF
python3 "$SEL" --fix "$ROOT/noticed-fix.json" --review-report "$ROOT/review.md" > "$ROOT/noticed.json"; expect_rc "noticed sources select cleanly" 0 $?
jq_is "noticed block holds at most 3 lines"          "$ROOT/noticed.json" '.noticed_block | split("\n") | map(select(startswith("- "))) | length' 3
jq_is "noticed: a case-insensitive duplicate is merged" "$ROOT/noticed.json" '[.noticed_block, (.noticed_overflow|join(" "))] | join(" ") | [scan("(?i)updateprojectpermissions")] | length' 1
jq_is "noticed: the 4th item goes to the run report" "$ROOT/noticed.json" '.noticed_overflow[0]' "LegacyExport ignores the workspace time zone"
jq -r '.noticed_block' "$ROOT/noticed.json" | grep -q 'Checkbox consumers' && bad "noticed: bullets from other sections leak in" || ok
jq_is "noticed items are never threads" "$ROOT/noticed.json" '.counts.threads' 0
python3 "$SEL" --fix "$FX/noisy-fix.json" > "$ROOT/nonoticed.json"
jq_is "no noticed items → no block" "$ROOT/nonoticed.json" '.noticed_block' ""
python3 "$SEL" --fix "$ROOT/noticed-fix.json" --review-report "$ROOT/missing.md" >/dev/null; expect_rc "a missing review report exits 2" 2 $?

# ── the read-back audit catches what went up outside the funnel (the noisy PR's caveat threads) ───
AUD="$SK/infra-ship/scripts/audit_pr_threads.py"
python3 - "$ROOT" <<'PY'
import json, sys
R = sys.argv[1]
sysmsg = {"thread_id": "1", "body": "the owner created the pull request", "is_system": True, "status": "active"}
def t(i, content, who="loop-bot@example.com"):
    return {"thread_id": i, "body": content, "path": None, "line": None, "status": "active", "author": who}
before = [sysmsg]
funnel = t(101, "**⚠️ Not fully fixed — Export totals still wrong**\n\nx\n\n**Before merging:** hold?")
caveat = t(102, "**Caveat — this change has NO automated test coverage.**\n\nThe unit suite does not reach this path...")
colleague = t(103, "Can you also check the export?", who={"name": "A Colleague", "email": "colleague@example.com"})
json.dump(before, open(f"{R}/before.json", "w"))
json.dump([sysmsg, funnel], open(f"{R}/after-clean.json", "w"))
json.dump({"ok": True, "result": [sysmsg, funnel, caveat, colleague]}, open(f"{R}/after-noise.json", "w"))
three = [t(200 + i, f"**⚠️ Your call — item {i}**\n\nx\n\n**Before merging:** y") for i in range(3)]
json.dump([sysmsg] + three, open(f"{R}/after-overcap.json", "w"))
PY
python3 "$AUD" --before "$ROOT/before.json" --after "$ROOT/after-clean.json" --posted-ids 101 --runner loop-bot@example.com > "$ROOT/aud1.json"
expect_rc "a clean ship audits clean" 0 $?
jq_is "clean: one thread via the funnel" "$ROOT/aud1.json" '.via_funnel' 1
python3 "$AUD" --before "$ROOT/before.json" --after "$ROOT/after-noise.json" --posted-ids 101 --runner loop-bot@example.com > "$ROOT/aud2.json"
expect_rc "an improvised caveat thread is a violation (exit 3)" 3 $?
jq_is "the caveat is caught as outside the funnel" "$ROOT/aud2.json" '.outside_funnel[0].id' 102
jq_is "a colleague's comment is not the loop's noise" "$ROOT/aud2.json" '.outside_funnel | length' 1
jq_is "noise_thread is set" "$ROOT/aud2.json" '.noise_thread' true
python3 "$AUD" --before "$ROOT/before.json" --after "$ROOT/after-overcap.json" --posted-ids 200,201,202 > "$ROOT/aud3.json"
expect_rc "three loop threads on one PR is over the cap (exit 3)" 3 $?
jq_is "over_cap_on_pr is set" "$ROOT/aud3.json" '.over_cap_on_pr' true
python3 "$AUD" --before "$ROOT/before.json" --after "$ROOT/before.json" > "$ROOT/aud4.json"
expect_rc "nothing posted audits clean" 0 $?
# the adapter's thread ids are opaque strings; the e2e post above returned git-local ids (t1…t4)
python3 "$AUD" --before "$ROOT/e2e-before.json" --after "$ROOT/e2e-after.json" --posted-ids "$posted_ids" --limits "$(cap4)" > "$ROOT/aud5.json"
expect_rc "e2e: the adapter's own read-back audits clean" 0 $?
jq_is "e2e: every new thread came through the funnel" "$ROOT/aud5.json" '.via_funnel' 4
python3 "$AUD" --before "$ROOT/e2e-before.json" --after "$ROOT/e2e-after.json" --posted-ids "t1,t2" --limits "$(cap4)" > "$ROOT/aud6.json"
expect_rc "e2e: a thread the funnel did not post is a violation (exit 3)" 3 $?
python3 "$AUD" --before "$ROOT/before.json" --after "$FX/no-decisions-fix.json" >/dev/null; expect_rc "a malformed --after file exits 2" 2 $?

echo "  pr_thread_bar: $pass passed, $fail failed"
rm -rf "$ROOT"
[ "$fail" = 0 ]
