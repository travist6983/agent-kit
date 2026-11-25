#!/usr/bin/env bash
# Regression: dev-code-review's independent finding validation (finding_votes.py ballot / tally /
# check) + loop-limits review_validation + contract v1.6.0 (DEV-REVIEW-V1) + v1.8.0 protected
# findings (DEV-REVIEW-G1): a masking or critical finding is dropped only by a unanimous reject; any
# split keeps it human-gated and the verdict needs-human.
#
# The rules under test: a producer never grades its own work, so the specialists only PROPOSE
# candidates and N fresh validators vote. A finding is reported only when the specialist confidence
# is >= the floor AND >= quorum returned ballots confirm at >= the floor. There is no default: a
# missing or invalid limits block is exit 2. Fewer than quorum returned ballots on a non-empty
# ballot is exit 2 and nothing is written. A missing or malformed ballot is never a confirm. tally is
# a pure, byte-idempotent function of (candidates, votes, limits), so a quorum or floor can be
# replayed with zero model calls. check names each tampering.
#
# Offline: scratch dirs, a throwaway git repo with no remote for review_brief finalize. No brain, no
# VCS host, no model calls. Self-cleaning.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
FW="$(cd "$SK/../.." && pwd)"
FV="$SK/dev-code-review/scripts/finding_votes.py"
RB="$SK/dev-code-review/scripts/review_brief.py"
V="$FW/contracts/validate.py"
EMIT="$FW/contracts/emit.py"
C="$FW/contracts/dev-code-review.json"
ROOT="$(mktemp -d)"; trap 'rm -rf "$ROOT"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }

export LOOPS_REPO_ROOT="$FW"
unset KB_DATABASE_URL KB_CONFIG AGENT_KIT_OVERLAY KB_ALLOW_UNSHARED

L="$ROOT/limits.json"
cp "$FW/.claude/loops/loop-limits.json" "$L"
# alt <out> <python expr applied to block b>
alt(){ python3 - "$L" "$1" "$2" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); b = d.get("review_validation")
exec(sys.argv[3])
json.dump(d, open(sys.argv[2], "w"))
PY
}

run(){ OUT=$(python3 "$FV" "$@" 2>/dev/null); RC=$?; }
f(){ printf '%s' "$OUT" | python3 -c "import sys,json
d=json.load(sys.stdin); v=d
for k in '$1'.split('.'):
    v = v.get(k) if isinstance(v, dict) else (v[int(k)] if isinstance(v, list) and k.isdigit() and int(k) < len(v) else None)
print(json.dumps(v, sort_keys=True) if not isinstance(v, str) else v)" 2>/dev/null; }
want(){ local got; got=$(f "$2"); if [ "$got" = "$3" ]; then ok; else bad "$1: $2=$got (want $3)"; fi; }
want_rc(){ if [ "$RC" = "$2" ]; then ok; else bad "$1: exit $RC (want $2) $(printf '%s' "$OUT" | head -c 300)"; fi; }
has(){ if f "$2" | grep -qF -- "$3"; then ok; else bad "$1: $2 lacks $3: $(f "$2")"; fi; }
# jf <file> <python expr over d> — prints the expression's value
jf(){ python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps($2, sort_keys=True) if not isinstance($2, str) else $2)" "$1" 2>/dev/null; }
jwant(){ local got; got=$(jf "$2" "$3"); if [ "$got" = "$4" ]; then ok; else bad "$1: $3=$got (want $4)"; fi; }
# jedit <file> <python statement over d>
jedit(){ python3 -c "import json,sys; p=sys.argv[1]; d=json.load(open(p)); $2; json.dump(d, open(p,'w'), indent=2)" "$1"; }

# cands <dir> <JSON list of findings>
cands(){ mkdir -p "$1"; printf '{"findings": %s}\n' "$2" > "$1/candidates.json"; }
# ids <dir> — export I0 I1 I2 ... from ballot.json order
ids(){ eval "$(python3 -c "import json,sys
b=json.load(open(sys.argv[1]+'/ballot.json'))
for i,f in enumerate(b['findings']): print(f'I{i}={f[\"finding_id\"]}')" "$1")"; }
# vote <dir> <vN> <validator id> <fid:verdict:conf[:serious]> ...
vote(){ local d="$1" vf="$2" vid="$3"; shift 3
  python3 - "$d/votes/$vf.json" "$vid" "$@" <<'PY'
import json, sys
out, vid = sys.argv[1], sys.argv[2]
votes = []
for spec in sys.argv[3:]:
    p = spec.split(":")
    v = {"finding_id": p[0], "verdict": p[1], "reason": "fixture"}
    try:
        v["confidence"] = int(p[2])
    except ValueError:
        v["confidence"] = p[2]
    if len(p) > 3:
        v["serious"] = p[3] == "true"
    votes.append(v)
json.dump({"validator": vid, "votes": votes}, open(out, "w"))
PY
}

FA='{"dimension":"data","severity":"high","confidence":92,"file":"api/src/invoices/list.js","line":7,"bug_class":"authz-or-authn-gap","issue":"Invoices query drops the workspace scope.","suggestion":"keep the predicate","fingerprint":"abc"}'
FB='{"dimension":"tests","severity":"medium","confidence":85,"file":"./api/test/invoices/list.test.js","line":12,"bug_class":"missing-or-weak-test","issue":"No test   for a second Workspace."}'
FC='{"dimension":"perf","severity":"low","confidence":80,"file":"api/src/invoices/list.js","line":9,"bug_class":"performance","issue":"In-memory sort of Members."}'
FLOW='{"dimension":"backend","severity":"high","confidence":79,"file":"api/src/members/read.js","line":3,"bug_class":"wrong-logic","issue":"A hunch."}'
FDUP='{"dimension":"security","severity":"critical","confidence":88,"file":"api/src/invoices/list.js","line":7,"bug_class":"authz-or-authn-gap","issue":"Member reads another Workspace."}'
FMASK='{"dimension":"masking","severity":"critical","confidence":90,"file":"api/src/invoices/send.js","line":17,"bug_class":"symptom-masking","issue":"catch returns ok:true and hides the failed send."}'

# ---------- (a) limits: no default, constraints enforced --------------------------------------------
D="$ROOT/a"; cands "$D" "[$FA]"
alt "$ROOT/l-none.json" 'd.pop("review_validation")'
run ballot --dir "$D" --limits "$ROOT/l-none.json"; want_rc "limits block missing" 2
has "limits block missing names it" error "no review_validation block"
alt "$ROOT/l-qgtv.json" 'b["quorum"]=4'
run ballot --dir "$D" --limits "$ROOT/l-qgtv.json"; want_rc "quorum > validators" 2
alt "$ROOT/l-q0.json" 'b["quorum"]=0'
run ballot --dir "$D" --limits "$ROOT/l-q0.json"; want_rc "quorum 0" 2
alt "$ROOT/l-f101.json" 'b["confidence_floor"]=101'
run ballot --dir "$D" --limits "$ROOT/l-f101.json"; want_rc "floor 101" 2
alt "$ROOT/l-bool.json" 'b["validators"]=True'
run ballot --dir "$D" --limits "$ROOT/l-bool.json"; want_rc "bool validators" 2
[ ! -f "$D/ballot.json" ] && ok || bad "a failed ballot wrote ballot.json"
run ballot --dir "$ROOT/nodir" --limits "$L"; want_rc "no candidates.json" 2
mkdir -p "$ROOT/badshape"; echo '{"findings": {}}' > "$ROOT/badshape/candidates.json"
run ballot --dir "$ROOT/badshape" --limits "$L"; want_rc "candidates.json wrong shape" 2
python3 -c "import json;b=json.load(open('$FW/.claude/loops/loop-limits.json'))['review_validation'];assert 1<=b['quorum']<=b['validators'] and 0<=b['confidence_floor']<=100 and 'ARGUED' in b['rule']" \
  && ok || bad "tracked loop-limits review_validation invalid or not honest"

# ---------- (b) ballot: ids, idempotent, stripped, floor, dedup --------------------------------------
D="$ROOT/b"; cands "$D" "[$FA, $FB, $FC, $FLOW, $FDUP]"
run ballot --dir "$D" --limits "$L"; want_rc "ballot runs" 0
want "ballot status" status ballot-ready
want "dedup by file+line+bug_class" deduped 1
want "candidates after dedup" candidates 4
want "below_floor counts the 79" below_floor 1
want "on_ballot" on_ballot 3
want "validators from limits" validators 3
want "vote_files" vote_files '["votes/v1.json", "votes/v2.json", "votes/v3.json"]'
[ -d "$D/votes" ] && ok || bad "votes/ not created"
jwant "dedup keeps the critical" "$D/candidates.json" '[c["severity"] for c in d["findings"] if c["line"]==7]' '["critical"]'
jwant "every candidate has an id" "$D/candidates.json" 'all(c["finding_id"].startswith("F-") and len(c["finding_id"])==12 for c in d["findings"])' 'true'
jwant "ballot strips confidence/suggestion/fingerprint" "$D/ballot.json" 'any(k in f for f in d["findings"] for k in ("confidence","suggestion","fingerprint","harden"))' 'false'
jwant "ballot keeps the issue and file" "$D/ballot.json" 'all("issue" in f and "file" in f for f in d["findings"])' 'true'
jwant "the 79 is not on the ballot" "$D/ballot.json" '[f["line"] for f in d["findings"] if f["file"]=="api/src/members/read.js"]' '[]'
cp "$D/candidates.json" "$ROOT/b-c1"; cp "$D/ballot.json" "$ROOT/b-b1"
run ballot --dir "$D" --limits "$L"; want "second ballot dedups nothing" deduped 0
cmp -s "$D/candidates.json" "$ROOT/b-c1" && cmp -s "$D/ballot.json" "$ROOT/b-b1" && ok || bad "ballot is not idempotent"
# the id ignores a leading ./ and whitespace / case in the issue
D2="$ROOT/b2"; cands "$D2" '[{"dimension":"tests","severity":"medium","confidence":85,"file":"api/test/invoices/list.test.js","line":12,"bug_class":"missing-or-weak-test","issue":"no test for a second  workspace."}]'
python3 "$FV" ballot --dir "$D2" --limits "$L" >/dev/null
a=$(jf "$D/candidates.json" '[c["finding_id"] for c in d["findings"] if c["line"]==12][0]'); b=$(jf "$D2/candidates.json" 'd["findings"][0]["finding_id"]')
[ "$a" = "$b" ] && ok || bad "finding_id not normalized ($a vs $b)"

# ---------- (c) zero candidates ---------------------------------------------------------------------
D="$ROOT/c"; cands "$D" "[$FLOW]"
run ballot --dir "$D" --limits "$L"; want "nothing at the floor: no-candidates" status no-candidates
want "no validators spawned" validators 0
want "no vote files" vote_files '[]'
run tally --dir "$D" --review "$ROOT/c-review.json" --limits "$L"; want_rc "tally on no-candidates" 0
want "tally status no-candidates" status no-candidates
jwant "draft validation no-candidates" "$ROOT/c-review.json" 'd["validation"]["status"]' no-candidates
jwant "draft findings empty" "$ROOT/c-review.json" 'd["findings"]' '[]'
jwant "rejected counts the below-floor candidate" "$ROOT/c-review.json" '[d["validation"]["candidates"], d["validation"]["kept"], d["validation"]["rejected"]]' '[1, 0, 1]'
jedit "$ROOT/c-review.json" 'd["verdict"]="approve"; d["summary"]="clean"'
python3 "$V" "$C" "$ROOT/c-review.json" >/dev/null 2>&1 && ok || bad "no-candidates draft + verdict does not validate"
D="$ROOT/c0"; cands "$D" "[]"
run ballot --dir "$D" --limits "$L"; want "empty candidates: no-candidates" status no-candidates
run tally --dir "$D" --review "$ROOT/c0-review.json" --limits "$L"; want_rc "tally on empty" 0
jwant "empty: candidates 0" "$ROOT/c0-review.json" 'd["validation"]["candidates"]' 0

# ---------- (d) three ballots: quorum and floor --------------------------------------------------------
D="$ROOT/d"; cands "$D" "[$FA, $FB, $FC]"
python3 "$FV" ballot --dir "$D" --limits "$L" >/dev/null; ids "$D"
# I0 (FA): confirm 95, confirm 80, confirm 79 -> 2 at floor, kept (80 counts, 79 does not)
# I1 (FB): confirm 90, pre-existing, reject -> 1, rejected
# I2 (FC, specialist 80): confirm 85, confirm 81, reject -> 2, kept
vote "$D" v1 v1 "$I0:confirm:95" "$I1:confirm:90" "$I2:confirm:85"
vote "$D" v2 v2 "$I0:confirm:80" "$I1:pre-existing:90" "$I2:confirm:81"
vote "$D" v3 v3 "$I0:confirm:79" "$I1:reject:70" "$I2:reject:60"
R="$ROOT/d-review.json"
run tally --dir "$D" --review "$R" --limits "$L"; want_rc "tally runs" 0
want "kept" kept 2
want "rejected" rejected 1
want "split" split 1
want "returned" validators_returned 3
jwant "kept ids" "$R" '[x["finding_id"] for x in d["findings"]]' "[\"$I0\", \"$I2\"]"
jwant "I0 votes: 79 not counted, 80 counts" "$R" 'd["findings"][0]["votes"]' '{"cast": 3, "confirm": 2, "pre_existing": 0, "reject": 0}'
jwant "kept finding keeps the specialist object" "$R" '[d["findings"][0]["confidence"], d["findings"][0]["suggestion"]]' '[92, "keep the predicate"]'
jwant "validation independent" "$R" 'd["validation"]["status"]' independent
jwant "validation source" "$R" 'd["validation"]["source"]' "$L#review_validation"
# a pre-existing vote is not a confirm: I1 with confirm + pre-existing is still out
jwant "pre-existing not counted as a confirm" "$R" "\"$I1\" in [x['finding_id'] for x in d['findings']]" 'false'

# ---------- (e) missing ballots ------------------------------------------------------------------------
D="$ROOT/e"; cands "$D" "[$FA, $FB]"
python3 "$FV" ballot --dir "$D" --limits "$L" >/dev/null; ids "$D"
vote "$D" v1 v1 "$I0:confirm:90" "$I1:reject:90"
vote "$D" v3 v3 "$I0:confirm:90" "$I1:confirm:90"
run tally --dir "$D" --review "$ROOT/e-review.json" --limits "$L"; want_rc "2 of 3 returned still tallies" 0
want "validators_returned 2" validators_returned 2
want "missing named" missing_validators '["votes/v2.json"]'
jwant "cast is 2" "$ROOT/e-review.json" 'd["findings"][0]["votes"]["cast"]' 2
rm "$D/votes/v3.json"
echo '{"keep": "me"}' > "$ROOT/e-keep.json"; cp "$ROOT/e-keep.json" "$ROOT/e-keep.orig"
run tally --dir "$D" --review "$ROOT/e-keep.json" --limits "$L"; want_rc "1 of 3 returned, quorum 2" 2
want "validation-unavailable" error validation-unavailable
want "names both missing" missing '["votes/v2.json", "votes/v3.json"]'
cmp -s "$ROOT/e-keep.json" "$ROOT/e-keep.orig" && ok || bad "validation-unavailable touched the draft"
run tally --dir "$D" --review "$ROOT/e-absent.json" --limits "$L"; want_rc "unavailable, absent draft" 2
[ ! -e "$ROOT/e-absent.json" ] && ok || bad "validation-unavailable created a draft"

# ---------- (f) malformed ballots and votes --------------------------------------------------------------
D="$ROOT/f"; cands "$D" "[$FA, $FB]"
python3 "$FV" ballot --dir "$D" --limits "$L" >/dev/null; ids "$D"
echo '{"validator": "v1", "votes": [' > "$D/votes/v1.json"
vote "$D" v2 vX "$I0:confirm:90" "$I1:confirm:90"
vote "$D" v3 vX "$I0:confirm:90" "$I1:confirm:90"
run tally --dir "$D" --review "$ROOT/f1.json" --limits "$L"; want_rc "malformed + duplicate id: 1 returned < quorum" 2
want "returned counts only the first vX" validators_returned 1
D="$ROOT/f2"; cands "$D" "[$FA, $FB]"
python3 "$FV" ballot --dir "$D" --limits "$L" >/dev/null; ids "$D"
vote "$D" v1 v1 "F-0000000000:confirm:90" "$I0:approve:90" "$I1:confirm:high"
vote "$D" v2 v2 "$I0:confirm:90" "$I0:confirm:90" "$I1:confirm:90"
vote "$D" v3 v3 "$I0:confirm:90" "$I1:confirm:90"
run tally --dir "$D" --review "$ROOT/f2.json" --limits "$L"; want_rc "abstains tally" 0
has "unknown id warned" warnings "unknown finding_id"
has "off-enum warned" warnings "off the enum"
has "string confidence warned" warnings "not an integer 0-100"
has "double vote warned" warnings "second vote"
# I0: v1 off-enum (abstain), v2 double (abstain), v3 confirm -> 1, rejected; I1: v1 abstain, v2, v3 -> 2, kept
jwant "abstains are never confirms" "$ROOT/f2.json" '[x["finding_id"] for x in d["findings"]]' "[\"$I1\"]"
jwant "abstain I1 votes" "$ROOT/f2.json" 'd["findings"][0]["votes"]' '{"cast": 3, "confirm": 2, "pre_existing": 0, "reject": 0}'

# ---------- (g) masking and counts -------------------------------------------------------------------------
D="$ROOT/g"; cands "$D" "[$FMASK, $FDUP, $FB]"
python3 "$FV" ballot --dir "$D" --limits "$L" >/dev/null; ids "$D"
for v in v1 v2 v3; do vote "$D" $v $v "$I0:confirm:90" "$I1:confirm:90" "$I2:confirm:90"; done
run tally --dir "$D" --review "$ROOT/g1.json" --limits "$L"
want "kept masking finding sets masking" masking true
want "unanimous confirms gate nothing" human_gate false
want "critical count" critical 2
want "high count" high 0
# (g2) owner decision: a masking or critical finding is never voted down by the quorum. A 1-of-3
# confirm on the mask used to drop it; now it is kept, human-gated, and does not set masking.
D="$ROOT/g2"; cands "$D" "[$FMASK, $FA]"
python3 "$FV" ballot --dir "$D" --limits "$L" >/dev/null; ids "$D"
vote "$D" v1 v1 "$I0:confirm:90" "$I1:confirm:90"
vote "$D" v2 v2 "$I0:reject:90" "$I1:confirm:90"
vote "$D" v3 v3 "$I0:reject:90" "$I1:confirm:90"
run tally --dir "$D" --review "$ROOT/g2.json" --limits "$L"
want "masking split 1-of-3: kept" kept 2
want "masking split: human_gated 1" human_gated 1
want "masking split: review human_gate" human_gate true
want "masking split: masking stays false (undecided)" masking false
want "masking split: critical counted" critical 1
want "high counted" high 1
jwant "the mask carries human_gate + a reason" "$ROOT/g2.json" '[d["findings"][0]["human_gate"], "1 confirm, 2 reject" in d["findings"][0]["human_gate_reason"]]' '[true, true]'
jwant "the plain high carries no gate" "$ROOT/g2.json" '"human_gate" in d["findings"][1]' false
jwant "draft human_gate" "$ROOT/g2.json" '[d["human_gate"], d["validation"]["human_gated"]]' '[true, 1]'
python3 "$FV" tally --dir "$D" --review "$ROOT/g2d.json" --limits "$L" --derive-verdict >/dev/null
jwant "derive: a split mask is needs-human, not request-changes" "$ROOT/g2d.json" 'd["verdict"]' needs-human
python3 "$V" "$C" "$ROOT/g2d.json" >/dev/null 2>&1 && ok || bad "a derived needs-human review does not validate: $(python3 "$V" "$C" "$ROOT/g2d.json" 2>&1 | tail -3)"
# (g3) a unanimous reject is the only way a protected finding leaves
D="$ROOT/g3"; cands "$D" "[$FMASK, $FA]"
python3 "$FV" ballot --dir "$D" --limits "$L" >/dev/null; ids "$D"
for v in v1 v2 v3; do vote "$D" $v $v "$I0:reject:90" "$I1:confirm:90"; done
run tally --dir "$D" --review "$ROOT/g3.json" --limits "$L"
want "unanimous reject drops the mask" kept 1
want "unanimous reject: no gate" human_gate false
want "unanimous reject: masking false" masking false
want "critical 0" critical 0
# (g4) a unanimous confirm keeps the mask as today: masking true, no gate, request-changes
D="$ROOT/g4"; cands "$D" "[$FMASK]"
python3 "$FV" ballot --dir "$D" --limits "$L" >/dev/null; ids "$D"
for v in v1 v2 v3; do vote "$D" $v $v "$I0:confirm:90"; done
python3 "$FV" tally --dir "$D" --review "$ROOT/g4.json" --limits "$L" --derive-verdict >/dev/null
jwant "unanimous confirm: masking true, no gate, request-changes" "$ROOT/g4.json" '[d["masking"], d["human_gate"], "human_gate" in d["findings"][0], d["verdict"]]' '[true, false, false, "request-changes"]'
# (g5) a critical (not masking) split: 2-of-3 used to pass quorum as a plain finding; any split is a gate.
# An abstain (no vote on it) and a pre-existing vote are a split too, never a reject.
FCRIT='{"dimension":"security","severity":"critical","confidence":88,"file":"api/src/members/role.js","line":4,"bug_class":"authz-or-authn-gap","issue":"An observer can promote themselves to owner."}'
FCRIT2='{"dimension":"data","severity":"critical","confidence":90,"file":"api/src/tasks/delete.js","line":9,"bug_class":"wrong-logic","issue":"Deleting a Task deletes every Comment in the Project."}'
FCRIT3='{"dimension":"backend","severity":"critical","confidence":90,"file":"api/src/projects/archive.js","line":2,"bug_class":"wrong-logic","issue":"Archive drops the Project row."}'
D="$ROOT/g5"; cands "$D" "[$FCRIT, $FCRIT2, $FCRIT3]"
python3 "$FV" ballot --dir "$D" --limits "$L" >/dev/null; ids "$D"
vote "$D" v1 v1 "$I0:confirm:90" "$I1:reject:90" "$I2:pre-existing:90"
vote "$D" v2 v2 "$I0:confirm:90" "$I1:reject:90" "$I2:reject:90"
vote "$D" v3 v3 "$I0:reject:90" "$I2:reject:90"
run tally --dir "$D" --review "$ROOT/g5.json" --limits "$L"
want "critical splits (2-1, reject+abstain, pre-existing+rejects) all kept" kept 3
want "critical splits all gated" human_gated 3
jwant "abstain named in the reason" "$ROOT/g5.json" '"1 abstain" in d["findings"][1]["human_gate_reason"]' true
# (g6) a protected candidate goes on the ballot whatever the specialist confidence; an ordinary one does not
LOWMASK='{"dimension":"masking","severity":"medium","confidence":40,"file":"api/src/tasks/save.js","line":5,"bug_class":"swallowed-error","issue":"Errors on save are logged and dropped."}'
D="$ROOT/g6"; cands "$D" "[$LOWMASK, $FLOW]"
run ballot --dir "$D" --limits "$L"
want "low-confidence mask on the ballot" on_ballot 1
want "protected counted" protected 1
want "ordinary 79 still below the floor" below_floor 1
ids "$D"
# (g7) three below-floor confirms on a mask are not a unanimous confirm at the floor: a split
for v in v1 v2 v3; do vote "$D" $v $v "$I0:confirm:70"; done
run tally --dir "$D" --review "$ROOT/g7.json" --limits "$L"
want "below-floor confirms on a mask: gated" human_gated 1
jwant "below-floor confirms named" "$ROOT/g7.json" '"+3 below the floor" in d["findings"][0]["human_gate_reason"]' true
# a protected split still fails closed when fewer than quorum ballots return
D="$ROOT/g8"; cands "$D" "[$FMASK]"
python3 "$FV" ballot --dir "$D" --limits "$L" >/dev/null; ids "$D"
vote "$D" v1 v1 "$I0:confirm:90"
run tally --dir "$D" --review "$ROOT/g8.json" --limits "$L"; want_rc "protected, 1 of 3 returned: validation-unavailable" 2
want "protected under quorum: validation-unavailable" error validation-unavailable
# replay: no quorum or floor can vote a protected finding down
alt "$ROOT/l-gq3.json" 'b["quorum"]=3'
run tally --dir "$ROOT/g2" --review "$ROOT/g2-q3.json" --limits "$ROOT/l-gq3.json"
want "replay quorum 3: the split mask is still kept and gated" human_gated 1
alt "$ROOT/l-gf99.json" 'b["confidence_floor"]=99'
run tally --dir "$ROOT/g2" --review "$ROOT/g2-f99.json" --limits "$ROOT/l-gf99.json"
want "replay floor 99: the split mask is still gated" human_gated 1
run tally --dir "$ROOT/g3" --review "$ROOT/g3-q1.json" --limits "$ROOT/l-gq3.json"
want "replay quorum 3: a unanimous reject still drops it" human_gated 0

# ---------- (h) idempotent, pops headline/brief, --derive-verdict ---------------------------------------------
R="$ROOT/h.json"
echo '{"verdict": "approve", "summary": "mine", "headline": "0 findings: nothing needs fixing.", "brief": {"status": "unavailable"}}' > "$R"
python3 "$FV" tally --dir "$ROOT/g3" --review "$R" --limits "$L" >/dev/null
jwant "headline popped" "$R" '"headline" in d' false
jwant "brief popped" "$R" '"brief" in d' false
jwant "verdict untouched without --derive-verdict" "$R" '[d["verdict"], d["summary"]]' '["approve", "mine"]'
cp "$R" "$ROOT/h.1"
python3 "$FV" tally --dir "$ROOT/g3" --review "$R" --limits "$L" >/dev/null
cmp -s "$R" "$ROOT/h.1" && ok || bad "tally is not byte-idempotent"
python3 "$FV" tally --dir "$ROOT/g3" --review "$R" --limits "$L" --derive-verdict >/dev/null
jwant "derive: a kept high is request-changes" "$R" 'd["verdict"]' request-changes
jwant "derive keeps an existing summary" "$R" 'd["summary"]' mine
python3 "$FV" tally --dir "$ROOT/c" --review "$ROOT/h2.json" --limits "$L" --derive-verdict >/dev/null
jwant "derive: nothing kept is approve" "$ROOT/h2.json" 'd["verdict"]' approve
jwant "derive sets a summary when absent" "$ROOT/h2.json" 'bool(d["summary"])' true
# a re-review: an earlier resolved finding is carried, needs no votes, and is not counted
echo '{"findings": [{"severity": "high", "issue": "old", "resolved": true}]}' > "$ROOT/h3.json"
python3 "$FV" tally --dir "$ROOT/g3" --review "$ROOT/h3.json" --limits "$L" >/dev/null
jwant "resolved finding carried" "$ROOT/h3.json" '[x.get("resolved", False) for x in d["findings"]]' '[false, true]'
jwant "resolved not counted in high" "$ROOT/h3.json" 'd["high"]' 1

# ---------- (i) replay with alternative limits ------------------------------------------------------------------
D="$ROOT/d"
sha_before=$(cat "$D"/votes/*.json "$D/ballot.json" "$D/candidates.json" | sha1sum)
alt "$ROOT/l-q3.json" 'b["quorum"]=3'
run tally --dir "$D" --review "$ROOT/i-q3.json" --limits "$ROOT/l-q3.json"
want "replay quorum 3: nothing has 3 confirms" kept 0
want "replay quorum 3: rejected 3" rejected 3
alt "$ROOT/l-f90.json" 'b["confidence_floor"]=90'
run tally --dir "$D" --review "$ROOT/i-f90.json" --limits "$ROOT/l-f90.json"
want "replay floor 90: FA has 1 confirm >= 90, FC specialist 80 < 90" kept 0
alt "$ROOT/l-q1.json" 'b["quorum"]=1'
run tally --dir "$D" --review "$ROOT/i-q1.json" --limits "$ROOT/l-q1.json"
want "replay quorum 1 keeps the 1-of-3" kept 3
alt "$ROOT/l-v5.json" 'b["validators"]=5; b["quorum"]=4'
run tally --dir "$D" --review "$ROOT/i-v5.json" --limits "$ROOT/l-v5.json"; want_rc "replay quorum above the run's validators" 2
sha_after=$(cat "$D"/votes/*.json "$D/ballot.json" "$D/candidates.json" | sha1sum)
[ "$sha_before" = "$sha_after" ] && ok || bad "a replay touched the working files"

# ---------- (j) noticed_candidates --------------------------------------------------------------------------------
D="$ROOT/j"; cands "$D" "[$FA, $FB]"
python3 "$FV" ballot --dir "$D" --limits "$L" >/dev/null; ids "$D"
vote "$D" v1 v1 "$I0:pre-existing:90:true" "$I1:pre-existing:90:false"
vote "$D" v2 v2 "$I0:pre-existing:85:true" "$I1:pre-existing:90:true"
vote "$D" v3 v3 "$I0:reject:85" "$I1:reject:90"
run tally --dir "$D" --review "$ROOT/j.json" --limits "$L"
want "noticed: the serious pre-existing one at quorum" noticed_candidates "[{\"file\": \"api/src/invoices/list.js\", \"finding_id\": \"$I0\", \"issue\": \"Invoices query drops the workspace scope.\", \"line\": 7}]"
want "noticed candidate not kept" kept 0

# ---------- (k) through finalize, validate.py and emit.py -----------------------------------------------------------
REPO="$ROOT/repo"; mkdir -p "$REPO"
( cd "$REPO" && git init -q -b main && git config user.email t@t && git config user.name t && git commit -q --allow-empty -m base )
printf '{"project": {"name": "fv-test", "default_branch": "main"}, "stack": {"conventions_file": "%s"}}' "$ROOT/no-conv.json" > "$ROOT/stack.json"
R="$ROOT/k.json"
python3 "$FV" tally --dir "$ROOT/d" --review "$R" --limits "$L" >/dev/null
jedit "$R" 'd["verdict"]="request-changes"; d["summary"]="request-changes: the Invoices scope."; d["key"]="PROJ-123"'
LOOPS_STACK_CONFIG="$ROOT/stack.json" python3 "$RB" finalize --review "$R" --repo "$REPO" --base main >/dev/null 2>&1 && ok || bad "review_brief finalize failed on tally output"
python3 "$V" "$C" "$R" >/dev/null 2>&1 && ok || bad "tally + finalize output does not validate: $(python3 "$V" "$C" "$R" 2>&1 | tail -3)"
mkdir -p "$ROOT/emit/PROJ-123"
python3 "$EMIT" --contract "$C" --out "$ROOT/emit/PROJ-123/review.json" --from-json "$R" --no-timing >/dev/null 2>&1 && [ -f "$ROOT/emit/PROJ-123/review.json" ] && ok || bad "emit.py --from-json refused tally output"

# ---------- (l) check ------------------------------------------------------------------------------------------------
run check --dir "$ROOT/d" --review "$R" --limits "$L"; want_rc "check on an honest review" 0
want "check ok" ok true
# tamper <class> <python statement>
tamper(){ cp "$R" "$ROOT/t.json"; jedit "$ROOT/t.json" "$2"
  run check --dir "$ROOT/d" --review "$ROOT/t.json" --limits "$L"
  if [ "$RC" = 1 ] && f violations | grep -qF "\"$1\""; then ok; else bad "check tamper $1: exit $RC $(f violations)"; fi; }
tamper validation-missing 'd.pop("validation")'
tamper finding-without-quorum 'd["findings"].append({"severity":"low","issue":"unvoted","file":"x.js","line":1})'
tamper validated-finding-missing 'd["findings"].pop(0)'
tamper votes-tampered 'd["findings"][0]["votes"]["confirm"]=3'
tamper masking-mismatch 'd["masking"]=True'
tamper counts-wrong 'd["high"]=0'
tamper validation-counts-wrong 'd["validation"]["kept"]=5'
python3 "$FV" tally --dir "$ROOT/g" --review "$ROOT/l-g.json" --limits "$L" >/dev/null
jedit "$ROOT/l-g.json" 'd["verdict"]="approve"; d["summary"]="x"'
run check --dir "$ROOT/g" --review "$ROOT/l-g.json" --limits "$L"
if [ "$RC" = 1 ] && f violations | grep -qF '"approve-with-blocking"'; then ok; else bad "check approve with a kept critical: exit $RC"; fi
# a human-gated review: check accepts it honest and names each way it is bent
run check --dir "$ROOT/g2" --review "$ROOT/g2d.json" --limits "$L"; want_rc "check on an honest needs-human review" 0
gtamper(){ cp "$ROOT/g2d.json" "$ROOT/tg.json"; jedit "$ROOT/tg.json" "$2"
  run check --dir "$ROOT/g2" --review "$ROOT/tg.json" --limits "$L"
  if [ "$RC" = 1 ] && f violations | grep -qF "\"$1\""; then ok; else bad "check gate tamper $1: exit $RC $(f violations)"; fi; }
gtamper human-gate-mismatch 'd["findings"][0].pop("human_gate")'
gtamper human-gate-mismatch 'd["human_gate"]=False'
gtamper human-gate-not-routed 'd["verdict"]="request-changes"'
gtamper approve-with-blocking 'd["verdict"]="approve"'
gtamper human-gate-withheld 'd["findings"][0]["withheld"]=True'
gtamper finding-without-quorum 'd["findings"].append(dict(d["findings"][0], finding_id="F-0000000000"))'
run check --dir "$ROOT/nodir" --review "$R" --limits "$L"; want_rc "check uncheckable" 2
run check --dir "$ROOT/d" --review "$ROOT/missing.json" --limits "$L"; want_rc "check on a missing review" 2

# ---------- (m) DEV-REVIEW-V1 through validate.py ------------------------------------------------------------------------
vfail(){ cp "$R" "$ROOT/v.json"; jedit "$ROOT/v.json" "$2"
  if python3 "$V" "$C" "$ROOT/v.json" 2>&1 | grep -q "DEV-REVIEW-V1"; then ok; else bad "V1 did not fire: $1"; fi; }
vpass(){ cp "$R" "$ROOT/v.json"; jedit "$ROOT/v.json" "$2"
  if python3 "$V" "$C" "$ROOT/v.json" >/dev/null 2>&1; then ok; else bad "V1 refused: $1: $(python3 "$V" "$C" "$ROOT/v.json" 2>&1 | tail -2)"; fi; }
vfail "confirm below quorum" 'd["findings"][0]["votes"]["confirm"]=1'
vfail "open count != kept" 'd["validation"]["kept"]=1; d["validation"]["rejected"]=2'
vfail "kept + rejected != candidates" 'd["validation"]["rejected"]=5'
vfail "open finding without votes" 'd["findings"][0].pop("votes")'
vfail "cast != validators_returned" 'd["findings"][0]["votes"]["cast"]=2'
vfail "returned below quorum" 'd["validation"]["validators_returned"]=1'
vpass "a resolved finding without votes" 'd["findings"].append({"severity":"high","issue":"old","resolved":True})'
vpass "an old review without validation" 'd.pop("validation"); [x.pop("votes") for x in d["findings"]]'
# DEV-REVIEW-G1 (v1.8.0) over a real needs-human tally
gfail(){ cp "$ROOT/g2d.json" "$ROOT/vg.json"; jedit "$ROOT/vg.json" "$2"
  if python3 "$V" "$C" "$ROOT/vg.json" 2>&1 | grep -q "DEV-REVIEW-G1"; then ok; else bad "G1 did not fire: $1"; fi; }
gfail "split mask without human_gate" 'd["findings"][0].pop("human_gate")'
gfail "gated finding under request-changes" 'd["verdict"]="request-changes"'
gfail "gated finding under approve" 'd["verdict"]="approve"'
gfail "gated finding withheld" 'd["findings"][0]["withheld"]=True; d["findings"][0]["withheld_reason"]="nit cap"'
gfail "gate without a reason" 'd["findings"][0].pop("human_gate_reason")'
gfail "gate on a non-protected finding" 'd["findings"][1]["human_gate"]=True; d["findings"][1]["human_gate_reason"]="x"'
gfail "gate on a unanimous confirm" 'd["findings"][0]["votes"]["confirm"]=3; d["findings"][0]["votes"]["reject"]=0'
gfail "review human_gate false with a gated finding" 'd["human_gate"]=False'
gfail "needs-human with nothing gated" 'd["findings"][0].pop("human_gate"); d["findings"][0].pop("human_gate_reason"); d["findings"][0]["votes"]["confirm"]=3; d["findings"][0]["votes"]["reject"]=0; d["human_gate"]=False'
gfail "masking true under approve" 'd["findings"].pop(0); d["human_gate"]=False; d["masking"]=True; d["verdict"]="approve"'
python3 "$V" "$C" "$FW/contracts/samples/EXAMPLE-review-human-gate/review.json" >/dev/null 2>&1 && ok || bad "EXAMPLE-review-human-gate fails"
python3 "$V" "$C" "$FW/contracts/samples/EXAMPLE-bad-review-human-gate/review.json" >/dev/null 2>&1 && bad "EXAMPLE-bad-review-human-gate passes" || ok
python3 "$V" "$C" "$FW/contracts/samples/EXAMPLE-review-validated/review.json" >/dev/null 2>&1 && ok || bad "EXAMPLE-review-validated fails"
python3 "$V" "$C" "$FW/contracts/samples/EXAMPLE-bad-review-validation/review.json" >/dev/null 2>&1 && bad "EXAMPLE-bad-review-validation passes" || ok

# ---------- offline tripwire -------------------------------------------------------------------------------------------------
if grep -qE "import (socket|urllib|http|requests)|psycopg|KB_ALLOW_UNSHARED=1" "$FV"; then bad "finding_votes.py reaches the network or the brain"; else ok; fi

echo "  finding_votes: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
