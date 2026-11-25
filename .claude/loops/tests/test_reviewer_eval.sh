#!/usr/bin/env bash
# Regression: dev-code-review's eval set and its zero-LLM scorer (reviewer_eval.py). The shipped
# cases stay well-formed (every confirmed bug on a line its diff adds), materialize never lets the
# answer key into the reviewer's sandbox, and score keeps its definitions: recall over confirmed
# bugs within line_tolerance, masking bugs need masking=true, harmful false flags on clean diffs,
# a missing or invalid review makes the run incomplete (exit 2) instead of vanishing from the
# denominator, and two runs are byte-identical. Scratch dirs only; no brain, no network, no model.
unset AGENT_KIT_OVERLAY AGENT_KIT_PROJECT
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
ROOT="$(cd "$SK/../.." && pwd)"
EV="$SK/dev-code-review/scripts/reviewer_eval.py"
CASES="$SK/dev-code-review/evals/cases"
LIMITS="$ROOT/.claude/loops/loop-limits.json"
V="$ROOT/contracts/validate.py"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }
jp(){ python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(eval(sys.argv[1]))" "$2" <<<"$1" 2>/dev/null || echo ERR; }
eq(){ if [ "$2" = "$3" ]; then ok; else bad "$1 (got $2, want $3)"; fi; }
run(){ out="$(python3 "$EV" "$@" 2>/dev/null)"; rc=$?; }
# mut FILE 'python statements on d' — rewrite one JSON file in place
mut(){ python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); exec(sys.argv[2]); json.dump(d,open(p,"w"),indent=2)' "$1" "$2"; }
case_of(){ python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$CASES/$1/case.json" "$2"; }
result_of(){ jp "$1" "[c for c in d['cases'] if c['id']=='$2'][0]['result']"; }

# perfect reviews: every bad case caught at line_start with the right masking call, every clean
# case approved with no findings
mkperfect(){ python3 - "$CASES" "$1" <<'PY'
import json, os, sys
cases, out = sys.argv[1], sys.argv[2]
for cid in sorted(os.listdir(cases)):
    p = os.path.join(cases, cid, "case.json")
    if not os.path.isfile(p):
        continue
    c = json.load(open(p))
    os.makedirs(os.path.join(out, cid))
    if c["kind"] == "bad":
        r = {"verdict": "request-changes",
             "masking": any(b["class"] == "masking" for b in c["bugs"]),
             "findings": [{"dimension": b["class"], "severity": "high", "confidence": 90,
                           "file": b["file"], "line": b["line_start"], "issue": "confirmed bug"}
                          for b in c["bugs"]],
             "summary": "request changes"}
    else:
        r = {"verdict": "approve", "masking": False, "findings": [], "summary": "clean diff"}
    json.dump(r, open(os.path.join(out, cid, "review.json"), "w"), indent=2)
PY
}
STRICT="$T/limits-strict.json"
echo '{"reviewer_eval": {"min_recall": 0.75, "max_false_flag_rate": 0.0, "line_tolerance": 3, "rule": "test fixture"}}' > "$STRICT"
FULL="$T/limits-full.json"
echo '{"reviewer_eval": {"min_recall": 1.0, "max_false_flag_rate": 0.0, "line_tolerance": 3, "rule": "test fixture"}}' > "$FULL"

# ── 1. the shipped set is well-formed ─────────────────────────────────────────
run check
eq "check shipped rc" "$rc" 0
eq "check shipped status" "$(jp "$out" 'd["status"]')" ok
eq "at least 4 bad cases" "$(jp "$out" 'd["bad"] >= 4')" True
eq "at least 2 clean cases" "$(jp "$out" 'd["clean"] >= 2')" True

# ── 2. materialize: a real repo, the receipt, never the answer key ────────────
IDS="$(cd "$CASES" && for d in */; do [ -f "$d/case.json" ] && echo "${d%/}"; done)"
for id in $IDS; do
  run materialize --case "$id" --out "$T/m/$id"
  eq "materialize $id rc" "$rc" 0
  key="$(jp "$out" 'd["key"]')"
  [ -n "$(git -C "$T/m/$id/repo" diff main --stat 2>/dev/null)" ] && ok || bad "materialize $id: empty diff vs main"
  [ -f "$T/m/$id/runs/$key/fix.json" ] && ok || bad "materialize $id: no runs/$key/fix.json"
  same="$(python3 - "$SK/dev-code-review/scripts" "$CASES/$id/change.diff" "$T/m/$id/repo" <<'PY'
import subprocess, sys
sys.path.insert(0, sys.argv[1]); import reviewer_eval as r
want = r.parse_diff(open(sys.argv[2]).read())[0]
got = r.parse_diff(subprocess.run(["git", "-C", sys.argv[3], "diff", "--no-color", "--no-ext-diff", "main"],
                                  stdout=subprocess.PIPE).stdout.decode())[0]
print(want == got)
PY
)"
  eq "materialize $id: git diff main reproduces change.diff" "$same" True
done
eq "no case.json in any materialized tree" "$(find "$T/m" -name case.json | wc -l | tr -d ' ')" 0
run materialize --case clean-status-badge-from-props --out "$T/m/clean-status-badge-from-props"
eq "materialize into a non-empty dir refused" "$rc" 2
run materialize --case clean-status-badge-from-props --out "$CASES/clean-status-badge-from-props/x"
eq "materialize inside the cases dir refused" "$rc" 2
[ ! -e "$CASES/clean-status-badge-from-props/x" ] && ok || { bad "materialize wrote into the cases dir"; rm -rf "$CASES/clean-status-badge-from-props/x"; }
run materialize --case no-such-case --out "$T/m/none"
eq "materialize unknown case refused" "$rc" 2

# ── 3. perfect reviews pass, and the score is contract-valid ──────────────────
mkperfect "$T/perfect"
run score --reviews "$T/perfect" --out "$T/perfect.score.json"
eq "perfect rc" "$rc" 0
eq "perfect status" "$(jp "$out" 'd["status"]')" pass
eq "perfect recall" "$(jp "$out" 'd["recall"]')" 1.0
eq "perfect false_flag_rate" "$(jp "$out" 'd["false_flag_rate"]')" 0.0
python3 "$V" "$ROOT/contracts/dev-code-review.eval-score.json" "$T/perfect.score.json" >/dev/null 2>&1 \
  && ok || bad "perfect score does not validate against dev-code-review.eval-score.json"

# ── 4. one bad case missed ────────────────────────────────────────────────────
NBUGS="$(jp "$out" 'd["bad"]["bugs"]')"
cp -r "$T/perfect" "$T/r4"
mut "$T/r4/bad-data-missing-workspace-scope/review.json" 'd.update(verdict="approve", findings=[], masking=False)'
run score --reviews "$T/r4" --limits "$FULL"
eq "one miss under min_recall 1.0 rc" "$rc" 1
eq "one miss status" "$(jp "$out" 'd["status"]')" fail
eq "missed case result" "$(result_of "$out" bad-data-missing-workspace-scope)" missed
run score --reviews "$T/r4"
want="$(python3 -c "print(round(($NBUGS-1)/$NBUGS, 4))")"
eq "one miss recall under the tracked limits" "$(jp "$out" 'd["recall"]')" "$want"

# ── 5. line tolerance is inclusive, and only that wide ────────────────────────
TOL="$(python3 -c "import json; print(json.load(open('$LIMITS'))['reviewer_eval']['line_tolerance'])")"
LE="$(case_of bad-data-missing-workspace-scope 'c["bugs"][0]["line_end"]')"
cp -r "$T/perfect" "$T/r5"
mut "$T/r5/bad-data-missing-workspace-scope/review.json" "d['findings'][0]['line']=$LE+$TOL"
run score --reviews "$T/r5"
eq "off by exactly line_tolerance: caught" "$(result_of "$out" bad-data-missing-workspace-scope)" caught
mut "$T/r5/bad-data-missing-workspace-scope/review.json" "d['findings'][0]['line']=$LE+$TOL+1"
run score --reviews "$T/r5"
eq "off by line_tolerance+1: missed" "$(result_of "$out" bad-data-missing-workspace-scope)" missed
eq "off by line_tolerance+1: an unmatched finding" \
  "$(jp "$out" "[c for c in d['cases'] if c['id']=='bad-data-missing-workspace-scope'][0]['unmatched_findings']")" 1

# ── 6. a masking bug needs masking=true ───────────────────────────────────────
cp -r "$T/perfect" "$T/r6"
mut "$T/r6/bad-masking-narrow-special-case/review.json" 'd["masking"]=False'
run score --reviews "$T/r6"
eq "masking bug located, masking false: missed" "$(result_of "$out" bad-masking-narrow-special-case)" missed
# contract v1.8.0: the validators split on the mask, so it is human-gated and the review routes to a
# human (needs-human) with masking still false — kept, never approved: a catch, not a miss
mut "$T/r6/bad-masking-narrow-special-case/review.json" 'd["verdict"]="needs-human"; d["human_gate"]=True; [f.update(human_gate=True, human_gate_reason="split") for f in d["findings"]]'
run score --reviews "$T/r6"
eq "masking bug human-gated under needs-human: caught" "$(result_of "$out" bad-masking-narrow-special-case)" caught
mut "$T/r6/bad-masking-narrow-special-case/review.json" 'd["verdict"]="request-changes"'
run score --reviews "$T/r6"
eq "a gated finding under request-changes breaks DEV-REVIEW-G1: invalid-review" "$(result_of "$out" bad-masking-narrow-special-case)" invalid-review

# ── 7. a finding must be locatable, in the right file ─────────────────────────
cp -r "$T/perfect" "$T/r7"
mut "$T/r7/bad-data-missing-workspace-scope/review.json" 'del d["findings"][0]["line"]'
run score --reviews "$T/r7"
eq "no line: missed" "$(result_of "$out" bad-data-missing-workspace-scope)" missed
eq "no line: unlocatable_findings 1" \
  "$(jp "$out" "[c for c in d['cases'] if c['id']=='bad-data-missing-workspace-scope'][0]['unlocatable_findings']")" 1
mut "$T/r7/bad-data-missing-workspace-scope/review.json" \
  "d['findings'][0]['line']=$LE; d['findings'][0]['file']='api/src/invoices/other.js'"
run score --reviews "$T/r7"
eq "right line, wrong file: missed" "$(result_of "$out" bad-data-missing-workspace-scope)" missed
mut "$T/r7/bad-data-missing-workspace-scope/review.json" \
  "d['findings'][0]['file']='./api/src/invoices/listInvoices.js'"
run score --reviews "$T/r7"
eq "a leading ./ is normalised: caught" "$(result_of "$out" bad-data-missing-workspace-scope)" caught

# ── 8. harmful false flags on a clean diff; noise is counted, not gated ───────
C8=clean-status-badge-from-props
cp -r "$T/perfect" "$T/r8"
mut "$T/r8/$C8/review.json" 'd["verdict"]="request-changes"'
run score --reviews "$T/r8" --limits "$STRICT"
eq "clean + request-changes: false-flagged" "$(result_of "$out" $C8)" false-flagged
eq "clean + request-changes rc" "$rc" 1
LOW='{"dimension": "frontend", "severity": "low", "confidence": 85, "file": "web/src/invoices/StatusBadge.jsx", "line": 2, "issue": "nit"}'
mut "$T/r8/$C8/review.json" "d['verdict']='approve'; d['findings']=[json.loads('''$LOW''')]"
run score --reviews "$T/r8" --limits "$STRICT"
eq "clean + approve + low finding: clean" "$(result_of "$out" $C8)" clean
eq "clean + low finding: noise_findings 1" "$(jp "$out" 'd["clean"]["noise_findings"]')" 1
eq "clean + low finding rc" "$rc" 0
mut "$T/r8/$C8/review.json" "d['findings'][0]['severity']='high'"
run score --reviews "$T/r8" --limits "$STRICT"
eq "clean + unresolved high: false-flagged" "$(result_of "$out" $C8)" false-flagged
mut "$T/r8/$C8/review.json" "d['findings'][0]['resolved']=True"
run score --reviews "$T/r8" --limits "$STRICT"
eq "clean + resolved high: clean" "$(result_of "$out" $C8)" clean
# contract v1.8.0: masking true under approve is itself invalid, so it rides request-changes here
mut "$T/r8/$C8/review.json" "d['findings']=[]; d['masking']=True; d['verdict']='request-changes'"
run score --reviews "$T/r8" --limits "$STRICT"
eq "clean + masking true: false-flagged" "$(result_of "$out" $C8)" false-flagged
GATED='{"dimension": "masking", "severity": "low", "confidence": 60, "file": "web/src/invoices/StatusBadge.jsx", "line": 2, "issue": "fallback hides a missing status", "bug_class": "symptom-masking", "human_gate": true, "human_gate_reason": "validators split"}'
mut "$T/r8/$C8/review.json" "d['masking']=False; d['verdict']='needs-human'; d['human_gate']=True; d['findings']=[json.loads('''$GATED''')]"
run score --reviews "$T/r8" --limits "$STRICT"
eq "clean + needs-human: false-flagged" "$(result_of "$out" $C8)" false-flagged

# ── 9. an unscored case is a reported failure, never filtered away ────────────
cp -r "$T/perfect" "$T/r9"
rm "$T/r9/bad-security-authz-check-removed/review.json"
run score --reviews "$T/r9"
eq "missing review rc" "$rc" 2
eq "missing review status" "$(jp "$out" 'd["status"]')" incomplete
eq "missing review result" "$(result_of "$out" bad-security-authz-check-removed)" no-review
eq "missing review: its bug counts as missed" "$(jp "$out" 'd["bad"]["missed"]')" 1
cp -r "$T/perfect" "$T/r9b"
mut "$T/r9b/$C8/review.json" 'd["verdict"]="maybe"'
run score --reviews "$T/r9b"
eq "invalid review rc" "$rc" 2
eq "invalid review result" "$(result_of "$out" $C8)" invalid-review
echo '{not json' > "$T/r9b/$C8/review.json"
run score --reviews "$T/r9b"
eq "unparsable review result" "$(result_of "$out" $C8)" invalid-review

# ── 10. deterministic: byte-identical runs, --out == stdout ───────────────────
python3 "$EV" score --reviews "$T/r4" > "$T/s1.json" 2>/dev/null
python3 "$EV" score --reviews "$T/r4" > "$T/s2.json" 2>/dev/null
cmp -s "$T/s1.json" "$T/s2.json" && ok || bad "two score runs differ"
python3 "$EV" score --reviews "$T/r4" --out "$T/s3.json" > "$T/s3.stdout" 2>/dev/null
cmp -s "$T/s3.json" "$T/s3.stdout" && ok || bad "--out bytes differ from stdout"

# ── 11. the bar comes from loop-limits.json, never a silent default ───────────
run score --reviews "$T/perfect"
eq "thresholds.source names loop-limits" "$(jp "$out" '"loop-limits" in d["thresholds"]["source"]')" True
eq "tracked reviewer_eval numbers are numeric" "$(python3 -c "
import json; d=json.load(open('$LIMITS'))['reviewer_eval']
num=lambda v: isinstance(v,(int,float)) and not isinstance(v,bool)
print(num(d['min_recall']) and num(d['max_false_flag_rate']) and isinstance(d['line_tolerance'],int) and bool(d['rule']))")" True
echo '{"r5_budget": {}}' > "$T/limits-none.json"
run score --reviews "$T/perfect" --limits "$T/limits-none.json"
eq "limits without reviewer_eval rc" "$rc" 2
echo '{"reviewer_eval": {"min_recall": "high", "max_false_flag_rate": 0.0, "line_tolerance": 3}}' > "$T/limits-bad.json"
run score --reviews "$T/perfect" --limits "$T/limits-bad.json"
eq "non-numeric min_recall rc" "$rc" 2

# ── 12. check refuses a malformed set, naming the case ────────────────────────
mkset(){ rm -rf "$T/set"; cp -r "$CASES" "$T/set"; }
refuses(){ run check --cases "$T/set"; eq "$1 rc" "$rc" 1
  eq "$1 names $2" "$(jp "$out" "any('$2' in e for e in d['errors'])")" True; }
mkset; mut "$T/set/bad-data-missing-workspace-scope/case.json" 'd["bugs"][0].update(line_start=1, line_end=1)'
refuses "bug range not on an added line" bad-data-missing-workspace-scope
mkset; mut "$T/set/$C8/case.json" 'd["bugs"]=[{"file":"web/src/invoices/StatusBadge.jsx","line_start":2,"line_end":2,"class":"frontend"}]'
refuses "clean case with bugs" $C8
mkset; mut "$T/set/$C8/case.json" 'd["id"]="renamed"'
refuses "id != directory name" $C8
mkset; rm "$T/set/bad-masking-narrow-special-case/change.diff"
refuses "missing change.diff" bad-masking-narrow-special-case
mkset; rm -rf "$T/set/clean-status-badge-from-props" "$T/set/clean-archived-project-transitions-invoice"
refuses "a set with zero clean cases" "<set>"
mkset; rm "$T/set/bad-masking-narrow-special-case/change.diff"
run score --reviews "$T/perfect" --cases "$T/set"
eq "score on an invalid case set rc" "$rc" 2

# ── 13. candidates: read-only lister over an evidence root ────────────────────
E="$T/evid"; mkdir -p "$E/PROJ-1" "$E/PROJ-2" "$E/PROJ-3" "$E/PROJ-4"
echo '{"verdict": "prod-regressed"}' > "$E/PROJ-1/prod-verify.json"
echo '{"verdict": "approve", "masking": false, "findings": [], "summary": "ok"}' > "$E/PROJ-1/review.json"
echo '{}' > "$E/PROJ-2/escalate.json"
echo '{"verdict": "approve", "masking": false, "summary": "ok", "findings": [{"severity": "high", "issue": "x", "resolved": true}]}' > "$E/PROJ-3/review.json"
echo '{"verdict": "approve", "masking": false, "findings": [], "summary": "ok"}' > "$E/PROJ-4/review.json"
before="$(cd "$E" && find . -type f | sort | xargs cat | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')"
run candidates --evidence-root "$E"
eq "candidates rc" "$rc" 0
eq "candidates keys" "$(jp "$out" '",".join(c["key"] for c in d["candidates"])')" "PROJ-1,PROJ-2,PROJ-3"
eq "candidates origins" "$(jp "$out" '";".join(",".join(c["origins"]) for c in d["candidates"])')" "prod-regressed;escalation;re-fix"
eq "prod-regressed run's review verdict reported" "$(jp "$out" 'd["candidates"][0]["review_verdict"]')" approve
mkset; mut "$T/set/$C8/case.json" 'd["source"].update(origin="escalation", ref="PROJ-2", confirmed_by="reviewer@example.com")'
run candidates --evidence-root "$E" --cases "$T/set"
eq "a key already a case moves to already_cases" "$(jp "$out" '",".join(d["already_cases"])')" PROJ-2
eq "and leaves the candidates" "$(jp "$out" '",".join(c["key"] for c in d["candidates"])')" "PROJ-1,PROJ-3"
after="$(cd "$E" && find . -type f | sort | xargs cat | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')"
eq "candidates writes nothing" "$after" "$before"
run candidates --evidence-root "$T/no-such-dir"
eq "candidates on a missing root rc" "$rc" 2

# ── 14. offline by construction ───────────────────────────────────────────────
for pat in 'kb\.py' 'KB_' 'urllib' 'http\.client' 'socket' 'requests'; do
  if grep -qE "$pat" "$EV"; then bad "reviewer_eval.py mentions $pat"; else ok; fi
done

echo "  reviewer_eval: $pass passed, $fail failed"
[ "$fail" = 0 ]
