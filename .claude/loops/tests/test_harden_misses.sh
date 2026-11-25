#!/usr/bin/env bash
# Regression: review MISSES reach dev-harden. collect_misses.py must queue every prod-regressed run
# and every reviewer-eval case the reviewer missed or only partly caught, with a `miss` block and an
# empty `lacked`; never call an unscored case a miss; refuse an eval score it cannot trust (exit 2);
# print by default and append idempotently with --append. collect_candidates.py must treat a miss as
# ready at one occurrence and skip a forged review-miss line. validate.py DEV-HARDEN-M1 must refuse a
# harden.json for a miss that does not say what the reviewer lacked. Scratch dirs only; the repo's
# harden queue is never touched.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
ROOT_REPO="$(cd "$SK/../.." && pwd)"
MISSES="$SK/dev-harden/scripts/collect_misses.py"
COLLECT="$SK/dev-harden/scripts/collect_candidates.py"
EV="$SK/dev-code-review/scripts/reviewer_eval.py"
V="$ROOT_REPO/contracts/validate.py"
C="$ROOT_REPO/contracts"
REPO_QUEUE="$ROOT_REPO/.claude/loops/harden-queue.jsonl"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }
jp(){ python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(eval(sys.argv[1]))" "$2" <<<"$1" 2>/dev/null || echo ERR; }
eq(){ if [ "$2" = "$3" ]; then ok; else bad "$1 (got $2, want $3)"; fi; }
repo_queue_before="absent"; [ -e "$REPO_QUEUE" ] && repo_queue_before="$(cksum < "$REPO_QUEUE")"

E="$T/runs"; Q="$T/q.jsonl"
mk(){ mkdir -p "$E/$1"; printf '%s\n' "$2" > "$E/$1/prod-verify.json"; }

# ── 1. a prod-regressed run with an approving review is one miss, printed only ──
mk PROJ-7 '{"verdict": "prod-regressed", "reason": "error rate on the invoice list rose after deploy", "rollback_recommended": true, "escalate": true}'
echo '{"verdict": "approve", "findings": [], "masking": false}' > "$E/PROJ-7/review.json"
echo '{}' > "$E/PROJ-7/context.json"
out="$(python3 "$MISSES" --evid-root "$E" --queue "$Q")"; rc=$?
eq "prod miss rc" "$rc" 0
eq "one entry" "$(jp "$out" 'len(d["entries"])')" 1
eq "fingerprint" "$(jp "$out" 'd["entries"][0]["fingerprint"]')" review-miss-prod-proj-7
eq "source" "$(jp "$out" 'd["entries"][0]["source"]')" review-miss
eq "origin" "$(jp "$out" 'd["entries"][0]["miss"]["origin"]')" prod-regressed
eq "review verdict" "$(jp "$out" 'd["entries"][0]["miss"]["review_verdict"]')" approve
eq "lacked empty" "$(jp "$out" 'd["entries"][0]["miss"]["lacked"]')" None
eq "had.reviewed" "$(jp "$out" 'd["entries"][0]["miss"]["had"]["reviewed"]')" True
eq "had.context_pack" "$(jp "$out" 'd["entries"][0]["miss"]["had"]["context_pack"]')" True
eq "appended false" "$(jp "$out" 'd["appended"]')" False
[ ! -e "$Q" ] && ok || bad "print-only default must not create the queue"

# ── 2. prod-confirmed and inconclusive are not misses ────────────────────────
mk PROJ-8 '{"verdict": "prod-confirmed", "reason": "signal dropped"}'
mk PROJ-9 '{"verdict": "inconclusive", "reason": "no analytics adapter"}'
out="$(python3 "$MISSES" --evid-root "$E" --queue "$Q")"
eq "confirmed/inconclusive add nothing" "$(jp "$out" '[e["ref"] for e in d["entries"]]')" "['PROJ-7/prod-verify.json']"

# ── 3. a regression with no review at all is still a miss, visibly unreviewed ─
mk PROJ-10 '{"verdict": "prod-regressed", "reason": "members page 500s"}'
out="$(python3 "$MISSES" --evid-root "$E" --queue "$Q")"
eq "unreviewed verdict" "$(jp "$out" '[e for e in d["entries"] if e["fingerprint"]=="review-miss-prod-proj-10"][0]["miss"]["review_verdict"]')" None
eq "unreviewed had" "$(jp "$out" '[e for e in d["entries"] if e["fingerprint"]=="review-miss-prod-proj-10"][0]["miss"]["had"]["reviewed"]')" False
eq "no-review statement" "$(jp "$out" '"no review ran" in [e for e in d["entries"] if e["fingerprint"]=="review-miss-prod-proj-10"][0]["statement"]')" True

# ── 4. a malformed prod-verify.json is reported, never dropped ───────────────
mkdir -p "$E/PROJ-11"; echo 'not json' > "$E/PROJ-11/prod-verify.json"
out="$(python3 "$MISSES" --evid-root "$E" --queue "$Q")"; rc=$?
eq "malformed rc" "$rc" 0
eq "malformed skipped" "$(jp "$out" 'len(d["skipped"])')" 1
eq "still two prod entries" "$(jp "$out" 'len(d["entries"])')" 2

# ── 5. the real scorer with no reviews: nothing is a miss, every case unscored ─
mkdir -p "$T/empty-reviews"
python3 "$EV" score --reviews "$T/empty-reviews" --out "$T/score-none.json" >/dev/null; rc=$?
eq "scorer rc with no reviews" "$rc" 2
eq "scorer status" "$(jp "$(cat "$T/score-none.json")" 'd["status"]')" incomplete
ncases="$(jp "$(cat "$T/score-none.json")" 'len(d["cases"])')"
out="$(python3 "$MISSES" --evid-root "$T/no-runs" --queue "$Q" --eval-score "$T/score-none.json")"; rc=$?
eq "unscored rc" "$rc" 0
eq "unscored: no entries" "$(jp "$out" 'len(d["entries"])')" 0
eq "unscored: all reported" "$(jp "$out" 'len(d["not_scored"])')" "$ncases"

# ── 6. a handwritten score: missed + partial queue, false flag reported ──────
cat > "$T/score.json" <<'J'
{"status": "fail", "recall": 0.4, "false_flag_rate": 0.5,
 "bad": {"cases": 3, "bugs": 4, "caught": 2, "missed": 2},
 "clean": {"cases": 2, "false_flagged": 1, "noise_findings": 1},
 "thresholds": {"min_recall": 0.8, "max_false_flag_rate": 0.2, "line_tolerance": 2, "source": "loop-limits.json reviewer_eval"},
 "case_set": {"count": 5, "sha256": "1d18e767dbc4c637f78a0bf0db7f2f7b9f988aaf769304d205c643868beba68e"},
 "per_class": [],
 "cases": [
  {"id": "bad-data-missing-workspace-scope", "kind": "bad", "result": "caught", "verdict": "request-changes", "masking": false,
   "bugs": [{"file": "api/src/invoices/listInvoices.js", "line_start": 6, "line_end": 7, "class": "data", "caught": true, "matched_line": 6}],
   "unmatched_findings": 0, "unlocatable_findings": 0, "reasons": []},
  {"id": "bad-masking-narrow-special-case", "kind": "bad", "result": "missed", "verdict": "approve", "masking": false,
   "bugs": [{"file": "web/src/invoices/invoiceTotal.js", "line_start": 3, "line_end": 5, "class": "masking", "caught": false, "matched_line": null}],
   "unmatched_findings": 0, "unlocatable_findings": 0, "reasons": []},
  {"id": "bad-security-authz-check-removed", "kind": "bad", "result": "partial", "verdict": "request-changes", "masking": false,
   "bugs": [{"file": "api/src/projects/archiveProject.js", "line_start": 11, "line_end": 13, "class": "security", "caught": true, "matched_line": 12},
            {"file": "api/src/projects/archiveProject.js", "line_start": 20, "line_end": 21, "class": "data", "caught": false, "matched_line": null}],
   "unmatched_findings": 1, "unlocatable_findings": 0, "reasons": []},
  {"id": "clean-status-badge-from-props", "kind": "clean", "result": "false-flagged", "verdict": "request-changes", "masking": false,
   "unlocatable_findings": 0, "reasons": []},
  {"id": "clean-archived-project-transitions-invoice", "kind": "clean", "result": "clean", "verdict": "approve", "masking": false,
   "unlocatable_findings": 0, "reasons": []}
 ],
 "reasons": ["recall 0.4 < min_recall 0.8"]}
J
python3 "$V" "$C/dev-code-review.eval-score.json" "$T/score.json" >/dev/null && ok || bad "handwritten score must validate"
out="$(python3 "$MISSES" --evid-root "$T/no-runs" --queue "$Q" --eval-score "$T/score.json")"; rc=$?
eq "eval rc" "$rc" 0
eq "eval entries" "$(jp "$out" '[e["fingerprint"] for e in d["entries"]]')" "['review-miss-eval-bad-masking-narrow-special-case', 'review-miss-eval-bad-security-authz-check-removed']"
eq "origins" "$(jp "$out" '[e["miss"]["origin"] for e in d["entries"]]')" "['eval-missed', 'eval-partial']"
eq "partial keeps only the uncaught bug" "$(jp "$out" 'd["entries"][1]["miss"]["locations"]')" "[{'file': 'api/src/projects/archiveProject.js', 'line_start': 20, 'line_end': 21, 'class': 'data'}]"
eq "eval ref" "$(jp "$out" 'd["entries"][0]["ref"]')" "eval:bad-masking-narrow-special-case@1d18e767dbc4"
eq "false flag reported" "$(jp "$out" 'len(d["false_flags"])')" 1
eq "partial statement" "$(jp "$out" '"1 of 2 confirmed bug(s) not located (data)" in d["entries"][1]["statement"]')" True

# ── 7. an eval score that cannot be trusted is exit 2, never skipped ─────────
python3 -c "import json; d=json.load(open('$T/score.json')); d['cases'][1]['result']='lost'; json.dump(d, open('$T/score-bad.json','w'))"
python3 "$MISSES" --evid-root "$E" --queue "$Q" --eval-score "$T/score-bad.json" >/dev/null; rc=$?
eq "invalid score rc" "$rc" 2
python3 "$MISSES" --evid-root "$E" --queue "$Q" --eval-score "$T/nope.json" >/dev/null; rc=$?
eq "missing score rc" "$rc" 2
[ ! -e "$Q" ] && ok || bad "a refused run must not write the queue"

# ── 8. --append writes once; a second append is byte-identical ───────────────
out="$(python3 "$MISSES" --evid-root "$E" --queue "$Q" --eval-score "$T/score.json" --append)"; rc=$?
eq "append rc" "$rc" 0
eq "append flag" "$(jp "$out" 'd["appended"]')" True
n="$(jp "$out" 'len(d["entries"])')"
eq "append entries" "$n" 4
eq "queue lines" "$(wc -l < "$Q" | tr -d ' ')" "$n"
eq "every line a miss" "$(python3 -c "import json,sys; print(all(isinstance(json.loads(l)['miss'], dict) for l in open(sys.argv[1])))" "$Q")" True
cp "$Q" "$T/q.copy"
out="$(python3 "$MISSES" --evid-root "$E" --queue "$Q" --eval-score "$T/score.json" --append)"
cmp -s "$Q" "$T/q.copy" && ok || bad "second --append must leave the queue byte-identical"
eq "already queued" "$(jp "$out" 'len(d["already_queued"])')" "$n"
eq "nothing new" "$(jp "$out" 'len(d["entries"])')" 0

# ── 9. collect_candidates: a miss is ready at one; a forged miss is skipped ───
echo '{"source":"fix_lesson","ref":"fix_lessons#9","statement":"Do not retry on 4xx"}' >> "$Q"
echo '{"source":"review-miss","statement":"x"}' >> "$Q"
out="$(python3 "$COLLECT" --evid-root "$E" --queue "$Q")"; rc=$?
eq "collect rc" "$rc" 0
eq "misses ready at one" "$(jp "$out" 'all(c["ready"] and c["occurrences"]==1 and isinstance(c["miss"]["origin"], str) for c in d["candidates"] if "review-miss" in c["sources"])')" True
eq "four miss candidates" "$(jp "$out" 'len([c for c in d["candidates"] if "review-miss" in c["sources"]])')" 4
eq "one-off fix_lesson held" "$(jp "$out" '[c["ready"] for c in d["candidates"] if c["sources"]==["fix_lesson"]]')" "[False]"
eq "forged miss skipped" "$(jp "$out" '[s["reason"] for s in d["skipped"]]')" "['review-miss without a miss block']"

# ── 10. a decided miss is not re-queued; a repeat is reported to a human ─────
FP="review-miss-eval-bad-masking-narrow-special-case"
mkdir -p "$E/harden/$FP"; cp "$C/samples/EXAMPLE-harden-miss-declined/harden.json" "$E/harden/$FP/harden.json"
eq "sample fingerprint matches" "$(jp "$(cat "$E/harden/$FP/harden.json")" 'd["fingerprint"]')" "$FP"
out="$(python3 "$COLLECT" --evid-root "$E" --queue "$Q")"
eq "decided listed" "$(jp "$out" '[x["status"] for x in d["decided"] if x["fingerprint"]=="'"$FP"'"]')" "['declined']"
out="$(python3 "$MISSES" --evid-root "$E" --queue "$T/fresh-q.jsonl" --eval-score "$T/score.json")"
eq "still missed after decision" "$(jp "$out" '[x["fingerprint"] for x in d["still_missed_after_decision"]]')" "['$FP']"
eq "not an entry" "$(jp "$out" '"'"$FP"'" in [e["fingerprint"] for e in d["entries"]]')" False

# ── 11. DEV-HARDEN-M1: a miss must say what the reviewer lacked ─────────────
must_pass(){ python3 "$V" "$C/dev-harden.json" "$1" >/dev/null 2>&1 && ok || bad "should pass: $1"; }
must_m1(){ o="$(python3 "$V" "$C/dev-harden.json" "$1" 2>&1)"; rc=$?
  if [ "$rc" != 0 ] && grep -q DEV-HARDEN-M1 <<<"$o"; then ok; else bad "should fail DEV-HARDEN-M1: $1"; fi; }
must_pass "$C/samples/EXAMPLE-harden-miss/harden.json"
must_pass "$C/samples/EXAMPLE-harden-miss-declined/harden.json"
must_m1 "$C/samples/EXAMPLE-bad-harden-miss-no-lacked/harden.json"
must_m1 "$C/samples/EXAMPLE-bad-harden-miss-nothing-ruled/harden.json"
mut(){ python3 - "$C/samples/EXAMPLE-harden-miss/harden.json" "$T/$1.json" "$2" <<'P'
import json, sys
d = json.load(open(sys.argv[1])); exec(sys.argv[3]); json.dump(d, open(sys.argv[2], "w"))
P
must_m1 "$T/$1.json"; }
mut no-source 'd["correction"]["sources"] = ["human"]'
mut no-block 'del d["miss"]'
mut decline-no-reason 'd.update(status="declined", band="none", ticket_draft=None, decline_reason=None)'
mut blank-statement 'd["miss"]["lacked"]["statement"] = "   "'

# ── 12. older artifacts are untouched by M1 ──────────────────────────────────
must_pass "$C/samples/EXAMPLE-harden-rule/harden.json"
must_pass "$C/samples/EXAMPLE-harden-prose/harden.json"

repo_queue_after="absent"; [ -e "$REPO_QUEUE" ] && repo_queue_after="$(cksum < "$REPO_QUEUE")"
eq "repo harden queue untouched" "$repo_queue_after" "$repo_queue_before"

echo "  harden-misses: $pass passed, $fail failed"
[ "$fail" = 0 ]
