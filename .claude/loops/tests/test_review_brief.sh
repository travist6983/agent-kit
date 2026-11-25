#!/usr/bin/env bash
# Regression: dev-code-review's per-area reviewer brief (review_brief.py plan / finalize / check).
#
# The rules under test: ONE optional conventions key, `review_brief`; absent = today's behaviour
# (nothing skipped, no nit cap, no limits block needed) except the count-first headline, which is
# always written. A skip glob never covers a file hit by the R6 denylist (adopter list AND the
# framework floor) or by shared_core_globs. Every failure degrades to `unavailable` (review
# everything, no cap). The nit cap withholds open lows beyond max_nits (or loop-limits
# review_brief.default_max_nits), never anything else. finalize is idempotent; check catches each
# kind of tampering with a named class.
#
# Offline: throwaway git repos with no remote, a stack.config + conventions + limits fixture through
# LOOPS_STACK_CONFIG. No brain, no VCS host, no model calls. Scratch dir, self-cleaning.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
FW="$(cd "$SK/../.." && pwd)"
RB="$SK/dev-code-review/scripts/review_brief.py"
V="$FW/contracts/validate.py"
C="$FW/contracts/dev-code-review.json"
ROOT="$(mktemp -d)"; trap 'rm -rf "$ROOT"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }

export LOOPS_REPO_ROOT="$FW"
unset KB_DATABASE_URL KB_CONFIG AGENT_KIT_OVERLAY KB_ALLOW_UNSHARED

BRIEF='{"_comment": "fixture", "max_nits": 2,
  "skip_paths": ["web/src/vendor/**", "**/*.snap", ".github/workflows/**", "db/migrations/**", "api/core/**", "secret-stuff/**"],
  "areas": [{"name": "invoices", "owner": "billing-team", "path_globs": ["api/invoices/**"],
             "failure_looks_like": ["an invoice total that disagrees with its line items"],
             "not_worth_flagging": ["naming in generated serializers"]},
            {"name": "members", "path_globs": ["api/members/**"],
             "failure_looks_like": ["a member reading another workspace"]}]}'

# conv <file> <review_brief JSON or ""> [denylist JSON array]
conv(){ python3 - "$1" "$2" "${3:-[]}" <<'PY'
import json, sys
d = {"test_path_regex": "(_test\\.|(^|/)tests?/)", "shared_core_globs": ["api/core/**"],
     "blast_radius_denylist_globs": json.loads(sys.argv[3]), "rules": []}
if sys.argv[2]:
    d["review_brief"] = json.loads(sys.argv[2])
json.dump(d, open(sys.argv[1], "w"), indent=1)
PY
}
# stackcfg <stack file> <conventions file> <limits file>
stackcfg(){ printf '{"project": {"name": "rb-test", "default_branch": "main"}, "stack": {"conventions_file": "%s"}, "limits_file": "%s"}' "$2" "$3" > "$1"; }

cp "$FW/.claude/loops/loop-limits.json" "$ROOT/limits.json"
python3 - "$ROOT/limits.json" "$ROOT/limits-none.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d.pop("review_brief", None); json.dump(d, open(sys.argv[2], "w"))
PY
python3 - "$ROOT/limits.json" "$ROOT/limits-7.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d["review_brief"]["default_max_nits"] = 7; json.dump(d, open(sys.argv[2], "w"))
PY
conv "$ROOT/conv.json" "$BRIEF"
stackcfg "$ROOT/stack.json" "$ROOT/conv.json" "$ROOT/limits.json"
export LOOPS_STACK_CONFIG="$ROOT/stack.json"

n=0
mkrepo(){ n=$((n+1)); R="$ROOT/repo$n"; mkdir -p "$R"
  ( cd "$R" && git init -q -b main && git config user.email t@t && git config user.name t
    mkdir -p api/invoices api/members api/core web/src/vendor
    printf 'x = 1\n' > api/invoices/total.py; printf 'y = 1\n' > api/members/read.py
    printf 'z = 1\n' > api/core/base.py; printf 'a\n' > web/src/vendor/x.js
    git add -A && git commit -qm base && git checkout -qb fix/x ); }
put(){ mkdir -p "$(dirname "$R/$1")"; printf '%s\n' "${2:-changed}" >> "$R/$1"; }

run(){ OUT=$(python3 "$RB" "$@" 2>/dev/null); RC=$?; }
plan(){ run plan --repo "$R" "$@"; }
f(){ printf '%s' "$OUT" | python3 -c "import sys,json
d=json.load(sys.stdin); v=d
for k in '$1'.split('.'):
    v = v.get(k) if isinstance(v, dict) else None
print(json.dumps(v, sort_keys=True) if not isinstance(v, str) else v)" 2>/dev/null; }
want(){ local got; got=$(f "$2"); if [ "$got" = "$3" ]; then ok; else bad "$1: $2=$got (want $3)"; fi; }
want_rc(){ if [ "$RC" = "$2" ]; then ok; else bad "$1: exit $RC (want $2) $OUT"; fi; }
has(){ if f "$2" | grep -qF -- "$3"; then ok; else bad "$1: $2 lacks $3: $(f "$2")"; fi; }
lacks(){ if f "$2" | grep -qF -- "$3"; then bad "$1: $2 has $3: $(f "$2")"; else ok; fi; }
jget(){ python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))" "$@"; }

# draft <file> <findings JSON>
draft(){ python3 - "$1" "$2" <<'PY'
import json, sys
fs = json.loads(sys.argv[2])
doc = {"key": "PROJ-123", "verdict": "request-changes" if any(f["severity"] in ("critical", "high") for f in fs) else "approve",
       "masking": False, "critical": sum(1 for f in fs if f["severity"] == "critical"),
       "high": sum(1 for f in fs if f["severity"] == "high"), "findings": fs,
       "summary": "The reviewer's rationale."}
json.dump(doc, open(sys.argv[1], "w"), indent=2)
PY
}
low(){ printf '{"dimension":"backend","severity":"low","confidence":%s,"file":"api/invoices/total.py","line":%s,"issue":"nit %s","bug_class":"other"}' "$1" "$2" "$2"; }
HIGH='{"dimension":"backend","severity":"high","confidence":95,"file":"api/invoices/total.py","line":1,"issue":"total ignores discounts","bug_class":"wrong-logic"}'

# ---- (a) no review_brief key: absent, nothing skipped, headline still written ---------------------
conv "$ROOT/conv-none.json" ""
stackcfg "$ROOT/stack-none.json" "$ROOT/conv-none.json" "$ROOT/limits-none.json"
mkrepo; put web/src/vendor/x.js; put api/invoices/total.py
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-none.json" python3 "$RB" plan --repo "$R" 2>/dev/null); RC=$?
want_rc "absent" 0
want "absent" status absent
want "absent" skipped '[]'
want "absent" max_nits null
want "absent" review_files '["api/invoices/total.py", "web/src/vendor/x.js"]'
draft "$ROOT/a.json" "[$(low 90 1),$(low 85 2),$(low 80 3)]"
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-none.json" python3 "$RB" finalize --review "$ROOT/a.json" --repo "$R" 2>/dev/null); RC=$?
want_rc "absent finalize (no limits block)" 0
if [ "$(jget "$ROOT/a.json" "'brief' in d")" = "False" ]; then ok; else bad "absent: finalize wrote a brief"; fi
if [ "$(jget "$ROOT/a.json" "d['headline']")" = "3 findings (3 low)." ]; then ok; else bad "absent: headline $(jget "$ROOT/a.json" "d.get('headline')")"; fi
if [ "$(jget "$ROOT/a.json" "sum(1 for x in d['findings'] if x.get('withheld'))")" = "0" ]; then ok; else bad "absent: a nit was withheld with no brief"; fi

# ---- (b) applied: a vendor file is skipped --------------------------------------------------------
mkrepo; put web/src/vendor/x.js; put api/invoices/total.py; plan
want_rc "applied" 0
want "applied" status applied
want "applied" skipped '[{"file": "web/src/vendor/x.js", "pattern": "web/src/vendor/**"}]'
want "applied" review_files '["api/invoices/total.py"]'
want "applied" max_nits 2

# ---- (c) refused by the framework floor (adopter list empty → floor is the effective list) -------
mkrepo; put .github/workflows/ci.yml; put db/migrations/001.sql; put web/src/vendor/x.js; plan
want "floor" skip_refused '[{"file": ".github/workflows/ci.yml", "pattern": ".github/workflows/**", "reason": "blast-radius"}, {"file": "db/migrations/001.sql", "pattern": "db/migrations/**", "reason": "blast-radius"}]'
has "floor stays reviewed" review_files '.github/workflows/ci.yml'
has "floor stays reviewed" review_files 'db/migrations/001.sql'
lacks "floor not skipped" skipped 'ci.yml'

# ---- (d) refused by the adopter's list AND still by the floor for a path that list omits -------
conv "$ROOT/conv-adopter.json" "$BRIEF" '["secret-stuff/**"]'
stackcfg "$ROOT/stack-adopter.json" "$ROOT/conv-adopter.json" "$ROOT/limits.json"
mkrepo; put secret-stuff/a.txt; put .github/workflows/ci.yml
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-adopter.json" python3 "$RB" plan --repo "$R" 2>/dev/null); RC=$?
want_rc "adopter list" 0
want "adopter + floor" skip_refused '[{"file": ".github/workflows/ci.yml", "pattern": ".github/workflows/**", "reason": "blast-radius"}, {"file": "secret-stuff/a.txt", "pattern": "secret-stuff/**", "reason": "blast-radius"}]'
want "adopter + floor" skipped '[]'

# ---- (e) refused: shared core ---------------------------------------------------------------------
mkrepo; put api/core/base.py; plan
want "shared core" skip_refused '[{"file": "api/core/base.py", "pattern": "api/core/**", "reason": "shared-core"}]'
want "shared core" review_files '["api/core/base.py"]'

# ---- (f) malformed brief → exit 2, unavailable, nothing skipped -----------------------------------
mkrepo; put web/src/vendor/x.js
for body in '{"max_nits": 1, "bogus": 1}' '{"max_nits": -1}' '{"max_nits": true}' '{"skip_paths": ["**"]}' \
            '{"skip_paths": ["*"]}' '{"areas": [{"name": "a", "path_globs": ["x/**"], "failure_looks_like": []}]}' \
            '{"areas": [{"name": "a", "path_globs": ["x/**"], "failure_looks_like": ["x"]}, {"name": "a", "path_globs": ["y/**"], "failure_looks_like": ["y"]}]}' \
            '[1]'; do
  conv "$ROOT/conv-bad.json" "$body"; stackcfg "$ROOT/stack-bad.json" "$ROOT/conv-bad.json" "$ROOT/limits.json"
  OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-bad.json" python3 "$RB" plan --repo "$R" 2>/dev/null); RC=$?
  want_rc "malformed $body" 2
  want "malformed $body" status unavailable
  want "malformed $body" skipped '[]'
  want "malformed $body" review_files '["web/src/vendor/x.js"]'
  if [ -n "$(f error)" ] && [ "$(f error)" != "null" ]; then ok; else bad "malformed $body: no error string"; fi
done
draft "$ROOT/f.json" "[$(low 90 1),$(low 85 2),$(low 80 3)]"
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-bad.json" python3 "$RB" finalize --review "$ROOT/f.json" --repo "$R" 2>/dev/null); RC=$?
want_rc "unavailable is not a finalize failure" 0
if [ "$(jget "$ROOT/f.json" "d['brief']['status']")" = "unavailable" ]; then ok; else bad "unavailable: brief block not written"; fi
if [ "$(jget "$ROOT/f.json" "sum(1 for x in d['findings'] if x.get('withheld'))")" = "0" ]; then ok; else bad "unavailable: nits withheld"; fi
if python3 "$V" "$C" "$ROOT/f.json" >/dev/null; then ok; else bad "unavailable output fails the contract"; fi

# ---- (g) the diff changes the conventions file itself → unavailable --------------------------------
mkrepo; conv "$R/conventions.json" "$BRIEF"; ( cd "$R" && git checkout -q main && git add conventions.json && git commit -qm conv && git checkout -q fix/x && git merge -q main )
stackcfg "$ROOT/stack-self.json" "$R/conventions.json" "$ROOT/limits.json"
put web/src/vendor/x.js
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-self.json" python3 "$RB" plan --repo "$R" 2>/dev/null); RC=$?
want "conventions unchanged" status applied
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); d['review_brief']['skip_paths'].append('api/**'); json.dump(d, open(sys.argv[1],'w'))" "$R/conventions.json"
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-self.json" python3 "$RB" plan --repo "$R" 2>/dev/null); RC=$?
want_rc "self-granted skip" 2
want "self-granted skip" status unavailable
want "self-granted skip" skipped '[]'

# ---- (h) areas: only those with a changed file, with failure_looks_like ---------------------------
mkrepo; put api/invoices/total.py; plan
want "areas" areas '[{"failure_looks_like": ["an invoice total that disagrees with its line items"], "files": ["api/invoices/total.py"], "name": "invoices", "not_worth_flagging": ["naming in generated serializers"], "owner": "billing-team"}]'

# ---- (i) the nit cap ----------------------------------------------------------------------------
mkrepo; put api/invoices/total.py
draft "$ROOT/i.json" "[$(low 95 1),$(low 81 2),$(low 90 3),$(low 82 4),$HIGH]"
run finalize --review "$ROOT/i.json" --repo "$R"
want_rc "cap" 0
want "cap" nits_withheld 2
if [ "$(jget "$ROOT/i.json" "sorted(x['line'] for x in d['findings'] if x.get('withheld'))")" = "[2, 4]" ]; then ok
else bad "cap: wrong nits withheld $(jget "$ROOT/i.json" "[x['line'] for x in d['findings'] if x.get('withheld')]")"; fi
if [ "$(jget "$ROOT/i.json" "[x for x in d['findings'] if x['severity']=='high'][0].get('withheld')")" = "None" ]; then ok; else bad "cap touched the high"; fi
if [ "$(jget "$ROOT/i.json" "(d['verdict'], d['critical'], d['high'], d['masking'])")" = "('request-changes', 0, 1, False)" ]; then ok; else bad "cap changed the verdict/counts"; fi
if [ "$(jget "$ROOT/i.json" "d['brief']['nits_withheld']")" = "2" ]; then ok; else bad "cap: brief.nits_withheld"; fi
if jget "$ROOT/i.json" "[x['withheld_reason'] for x in d['findings'] if x.get('withheld')][0]" | grep -q 'nit cap (review_brief max_nits=2)'; then ok; else bad "cap: withheld_reason"; fi
# a human-gated low masking finding (dev-code-review v1.8.0) is never withheld by the nit cap and does
# not take a nit slot: the cap withholds the same two ordinary lows
GLOW='{"dimension":"masking","severity":"low","confidence":40,"file":"api/invoices/total.py","line":9,"issue":"rounding error swallowed","bug_class":"symptom-masking","human_gate":true,"human_gate_reason":"validators split"}'
mkrepo; put api/invoices/total.py
draft "$ROOT/ig.json" "[$GLOW,$(low 95 1),$(low 81 2),$(low 90 3),$(low 82 4)]"
run finalize --review "$ROOT/ig.json" --repo "$R"
want "gated low: cap" nits_withheld 2
if [ "$(jget "$ROOT/ig.json" "[x.get('withheld') for x in d['findings'] if x.get('human_gate')]")" = "[None]" ]; then ok; else bad "nit cap withheld a human-gated finding"; fi
if [ "$(jget "$ROOT/ig.json" "sorted(x['line'] for x in d['findings'] if x.get('withheld'))")" = "[2, 4]" ]; then ok; else bad "gated low took a nit slot"; fi
# brief omits max_nits → default from the fixture limits; the number is READ
B2=$(printf '%s' "$BRIEF" | python3 -c "import json,sys; d=json.load(sys.stdin); d.pop('max_nits'); print(json.dumps(d))")
conv "$ROOT/conv-nomax.json" "$B2"
stackcfg "$ROOT/stack-nomax.json" "$ROOT/conv-nomax.json" "$ROOT/limits.json"
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-nomax.json" python3 "$RB" plan --repo "$R" 2>/dev/null)
want "default_max_nits" max_nits 5
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-nomax.json" python3 "$RB" plan --repo "$R" --limits "$ROOT/limits-7.json" 2>/dev/null)
want "default_max_nits read" max_nits 7
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-nomax.json" python3 "$RB" plan --repo "$R" --limits "$ROOT/limits-none.json" 2>/dev/null); RC=$?
want_rc "no limits block + no max_nits" 2
want "no limits block + no max_nits" status unavailable
python3 - "$ROOT/limits.json" "$ROOT/limits-neg.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d["review_brief"]["default_max_nits"] = -1; json.dump(d, open(sys.argv[2], "w"))
PY
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-nomax.json" python3 "$RB" plan --repo "$R" --limits "$ROOT/limits-neg.json" 2>/dev/null); RC=$?
want_rc "negative default_max_nits" 2
B0=$(printf '%s' "$BRIEF" | python3 -c "import json,sys; d=json.load(sys.stdin); d['max_nits']=0; print(json.dumps(d))")
conv "$ROOT/conv-zero.json" "$B0"; stackcfg "$ROOT/stack-zero.json" "$ROOT/conv-zero.json" "$ROOT/limits.json"
draft "$ROOT/i0.json" "[$(low 95 1),$(low 81 2),$HIGH]"
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-zero.json" python3 "$RB" finalize --review "$ROOT/i0.json" --repo "$R" 2>/dev/null)
if [ "$(jget "$ROOT/i0.json" "d['headline']")" = "1 finding (1 high)." ]; then ok; else bad "max_nits 0: headline $(jget "$ROOT/i0.json" "d['headline']")"; fi

# ---- (j) headline strings -------------------------------------------------------------------------
mkrepo; put api/invoices/total.py
draft "$ROOT/j0.json" "[]"; run finalize --review "$ROOT/j0.json" --repo "$R"
if [ "$(jget "$ROOT/j0.json" "d['headline']")" = "0 findings: nothing needs fixing." ]; then ok; else bad "j: zero headline"; fi
if [ "$(jget "$ROOT/j0.json" "d['summary']")" = "0 findings: nothing needs fixing. The reviewer's rationale." ]; then ok; else bad "j: zero summary $(jget "$ROOT/j0.json" "d['summary']")"; fi
draft "$ROOT/j1.json" "[$HIGH]"; run finalize --review "$ROOT/j1.json" --repo "$R"
if [ "$(jget "$ROOT/j1.json" "d['headline']")" = "1 finding (1 high)." ]; then ok; else bad "j: one high"; fi
CRIT='{"dimension":"security","severity":"critical","confidence":95,"file":"api/members/read.py","line":1,"issue":"cross-workspace read","bug_class":"authz-or-authn-gap"}'
RES='{"severity":"high","confidence":90,"file":"api/x.py","line":1,"issue":"fixed","resolved":true}'
WH='{"severity":"medium","confidence":90,"file":"api/y.py","line":1,"issue":"volume","withheld":true,"withheld_reason":"volume cap"}'
draft "$ROOT/j3.json" "[$CRIT,$(low 90 1),$(low 91 2),$RES,$WH]"; run finalize --review "$ROOT/j3.json" --repo "$R"
if [ "$(jget "$ROOT/j3.json" "d['headline']")" = "3 findings (1 critical, 2 low)." ]; then ok; else bad "j: mixed $(jget "$ROOT/j3.json" "d['headline']")"; fi

# ---- (k) idempotent + contract-valid ----------------------------------------------------------
cp "$ROOT/i.json" "$ROOT/i.first"
mkrepo; put api/invoices/total.py
run finalize --review "$ROOT/i.json" --repo "$R"
if cmp -s "$ROOT/i.json" "$ROOT/i.first"; then ok; else bad "finalize not idempotent"; fi
for x in a i i0 j0 j1 j3; do
  if python3 "$V" "$C" "$ROOT/$x.json" >/dev/null; then ok; else bad "$x.json fails the contract: $(python3 "$V" "$C" "$ROOT/$x.json")"; fi
done

# ---- (l) --report -------------------------------------------------------------------------------
draft "$ROOT/l.json" "[$HIGH]"
printf '# Review PROJ-123\n\nbody\n' > "$ROOT/l.md"
run finalize --review "$ROOT/l.json" --repo "$R" --report "$ROOT/l.md"
if [ "$(head -1 "$ROOT/l.md")" = "1 finding (1 high)." ] && [ "$(sed -n 3p "$ROOT/l.md")" = "# Review PROJ-123" ]; then ok; else bad "report prepend: $(cat "$ROOT/l.md")"; fi
cp "$ROOT/l.md" "$ROOT/l.first"
run finalize --review "$ROOT/l.json" --repo "$R" --report "$ROOT/l.md"
if cmp -s "$ROOT/l.md" "$ROOT/l.first"; then ok; else bad "report second run differs"; fi
printf '7 findings (7 low).\n\n# Review\n' > "$ROOT/l2.md"
run finalize --review "$ROOT/l.json" --repo "$R" --report "$ROOT/l2.md"
if [ "$(cat "$ROOT/l2.md")" = "$(printf '1 finding (1 high).\n\n# Review')" ]; then ok; else bad "report replace: $(cat "$ROOT/l2.md")"; fi
run finalize --review "$ROOT/l.json" --repo "$R" --report "$ROOT/nope.md"
if [ ! -e "$ROOT/nope.md" ]; then ok; else bad "report created from nothing"; fi

# ---- (m) check ---------------------------------------------------------------------------------
mkrepo; put api/invoices/total.py; put web/src/vendor/x.js; put .github/workflows/ci.yml
draft "$ROOT/m.json" "[$(low 95 1),$(low 81 2),$(low 90 3),$HIGH]"
printf '# Review\n' > "$ROOT/m.md"
run finalize --review "$ROOT/m.json" --repo "$R" --report "$ROOT/m.md"
run check --review "$ROOT/m.json" --repo "$R" --report "$ROOT/m.md"
want_rc "check clean" 0
tamper(){ cp "$ROOT/m.json" "$ROOT/mt.json"; python3 -c "import json,sys; d=json.load(open(sys.argv[1])); exec(sys.argv[2]); json.dump(d, open(sys.argv[1],'w'))" "$ROOT/mt.json" "$1"
  run check --review "$ROOT/mt.json" --repo "$R"; want_rc "tamper $2" 1; has "tamper $2" violations "\"$2\""; }
tamper 'del d["brief"]' brief-not-applied
tamper 'd["brief"]["skipped"].append({"file": ".github/workflows/ci.yml", "pattern": ".github/workflows/**"})' skipped-protected-path
tamper '[x.pop("withheld") for x in d["findings"] if x.get("withheld")]' nit-cap-exceeded
tamper 'd["headline"] = "9" + d["headline"][1:]; d["summary"] = d["headline"]' headline-count-wrong
tamper 'd["summary"] = "no lead"' summary-does-not-lead-with-count
tamper 'del d["headline"]' headline-missing
printf 'something else\n' > "$ROOT/m2.md"
run check --review "$ROOT/m.json" --repo "$R" --report "$ROOT/m2.md"; want_rc "report lead" 1
has "report lead" violations report-does-not-lead-with-count
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-none.json" python3 "$RB" check --review "$ROOT/m.json" --repo "$R" 2>/dev/null); RC=$?
want_rc "claimed without config" 1; has "claimed without config" violations brief-claimed-without-config
printf '{bad' > "$ROOT/mbad.json"; run check --review "$ROOT/mbad.json" --repo "$R"; want_rc "check unreadable" 2

# ---- (n) finalize on an unreadable draft: exit 2, untouched ----------------------------------------
for body in '{bad' '[1]'; do
  printf '%s' "$body" > "$ROOT/n.json"; cp "$ROOT/n.json" "$ROOT/n.first"
  run finalize --review "$ROOT/n.json" --repo "$R"; want_rc "finalize unreadable $body" 2
  if cmp -s "$ROOT/n.json" "$ROOT/n.first"; then ok; else bad "finalize touched an unreadable draft"; fi
done
mkdir -p "$ROOT/notgit"; draft "$ROOT/ng.json" "[]"; cp "$ROOT/ng.json" "$ROOT/ng.first"
run finalize --review "$ROOT/ng.json" --repo "$ROOT/notgit"; want_rc "finalize non-git" 2
if cmp -s "$ROOT/ng.json" "$ROOT/ng.first"; then ok; else bad "finalize wrote after a git failure"; fi

# ---- (o) plan is byte-identical across runs ------------------------------------------------------
plan; P1="$OUT"; plan
if [ "$P1" = "$OUT" ]; then ok; else bad "plan output differs across runs"; fi

# ---- (p) offline tripwire ------------------------------------------------------------------------
if grep -qE 'urllib|requests|socket|kb\.py' "$RB"; then bad "review_brief.py reaches for the network or the brain"; else ok; fi

# ---- (q) the example pack's brief is valid -----------------------------------------------------
stackcfg "$ROOT/stack-pack.json" "$FW/examples/conventions/example-pack.json" "$ROOT/limits.json"
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-pack.json" python3 "$RB" plan --repo "$R" --changed-file web/src/generated/api.ts --changed-file db/migrate/001_add.rb --changed-file app/services/invoices/send.rb 2>/dev/null); RC=$?
want_rc "example pack" 0
want "example pack" status applied
has "example pack skips generated" skipped web/src/generated/api.ts
has "example pack refuses migrate" skip_refused db/migrate/001_add.rb

# ---- (r) the shipped conventions file carries no brief (absent = current behaviour) ---------------
if python3 -c "import json,sys; sys.exit('review_brief' in json.load(open('$FW/.claude/loops/conventions.json')))"; then ok
else bad "the tracked conventions.json gained a review_brief"; fi

echo "  review_brief: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
