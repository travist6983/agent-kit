#!/usr/bin/env bash
# Regression: dev-code-review's risk-based review depth (review_depth.py plan / record / check).
#
# The rules under test: quick (ONE combined spawn carrying all six specialist lenses) only for a
# diff within loop-limits review_depth (files AND changed lines) with no risk path, no unknown and
# no --evid; standard = six spawns; deep = six + a security pass over the risk files. A path hit by
# the R6 denylist (adopter list OR framework floor, both sides of a rename), shared_core_globs, an
# intake `sensitive: true` or a change to the conventions file is deep. Every unknown (R6 scan
# cannot run, conventions unreadable, limits missing/invalid, malformed intake.json, git failure) is
# deep with status unavailable and exit 2, the plan still printed. An unknown line count, an empty
# diff, --changed-file and --evid are never quick. A level is only raised, never lowered. At EVERY
# level the passes cover all six specialists (asserted after every plan below).
#
# Offline: throwaway git repos with no remote, a stack.config + conventions + limits fixture through
# LOOPS_STACK_CONFIG. No brain, no VCS host, no model calls. Scratch dir, self-cleaning.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
FW="$(cd "$SK/../.." && pwd)"
RD="$SK/dev-code-review/scripts/review_depth.py"
RB="$SK/dev-code-review/scripts/review_brief.py"
V="$FW/contracts/validate.py"
C="$FW/contracts/dev-code-review.json"
PY="$(command -v python3)"
ROOT="$(mktemp -d)"; trap 'rm -rf "$ROOT"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }

export LOOPS_REPO_ROOT="$FW"
unset KB_DATABASE_URL KB_CONFIG AGENT_KIT_OVERLAY KB_ALLOW_UNSHARED

# conv <file> [denylist JSON array]
conv(){ "$PY" - "$1" "${2:-[]}" <<'PY'
import json, sys
d = {"test_path_regex": "(_test\\.|(^|/)tests?/)", "shared_core_globs": ["api/core/**"],
     "blast_radius_denylist_globs": json.loads(sys.argv[2]), "rules": []}
json.dump(d, open(sys.argv[1], "w"), indent=1)
PY
}
# stackcfg <stack file> <conventions file> <limits file>
stackcfg(){ printf '{"project": {"name": "rd-test", "default_branch": "main"}, "stack": {"conventions_file": "%s"}, "limits_file": "%s"}' "$2" "$3" > "$1"; }

cp "$FW/.claude/loops/loop-limits.json" "$ROOT/limits.json"
# limits variant <out> <python statement on d["review_depth"] or "pop">
limvar(){ "$PY" - "$ROOT/limits.json" "$1" "$2" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
if sys.argv[3] == "pop":
    d.pop("review_depth")
else:
    exec(sys.argv[3], {"b": d["review_depth"]})
json.dump(d, open(sys.argv[2], "w"))
PY
}
conv "$ROOT/conv.json"
stackcfg "$ROOT/stack.json" "$ROOT/conv.json" "$ROOT/limits.json"
conv "$ROOT/conv-adopter.json" '["secret-stuff/**"]'
stackcfg "$ROOT/stack-adopter.json" "$ROOT/conv-adopter.json" "$ROOT/limits.json"
export LOOPS_STACK_CONFIG="$ROOT/stack.json"

n=0
mkrepo(){ n=$((n+1)); R="$ROOT/repo$n"; mkdir -p "$R"
  ( cd "$R" && git init -q -b main && git config user.email t@t && git config user.name t
    mkdir -p api/invoices api/members api/core web/src secret-stuff
    printf 'x = 1\n' > api/invoices/total.py; printf 'y = 1\n' > api/members/read.py
    printf 'z = 1\n' > api/core/base.py; printf 'a\n' > web/src/x.js; printf 's\n' > secret-stuff/a.txt
    printf '\0\1\2' > web/src/logo.bin
    git add -A && git commit -qm base && git checkout -qb fix/x ); }
put(){ mkdir -p "$(dirname "$R/$1")"; printf '%s\n' "${2:-changed}" >> "$R/$1"; }
# putn <file> <n lines>
putn(){ mkdir -p "$(dirname "$R/$1")"; for _ in $(seq 1 "$2"); do printf 'l\n' >> "$R/$1"; done; }

SIX='["backend", "data", "frontend", "perf", "security", "tests"]'
f(){ printf '%s' "$OUT" | "$PY" -c "import sys,json
d=json.load(sys.stdin); v=d
for k in '$1'.split('.'):
    v = v.get(k) if isinstance(v, dict) else None
print(json.dumps(v, sort_keys=True) if not isinstance(v, str) else v)" 2>/dev/null; }
# every printed plan: the union of pass specialists is the six (u)
six(){ local u; u=$(printf '%s' "$OUT" | "$PY" -c "import sys,json
d=json.load(sys.stdin)
if 'passes' not in d: print('skip'); raise SystemExit
print(json.dumps(sorted({s for p in d['passes'] for s in p['specialists']})))" 2>/dev/null)
  if [ "$u" = "skip" ]; then return; fi
  if [ "$u" = "$SIX" ]; then ok; else bad "$1: passes cover $u, not all six"; fi; }
run(){ OUT=$("$PY" "$RD" "$@" 2>/dev/null); RC=$?; six "run $*"; }
plan(){ run plan --repo "$R" "$@"; }
want(){ local got; got=$(f "$2"); if [ "$got" = "$3" ]; then ok; else bad "$1: $2=$got (want $3)"; fi; }
want_rc(){ if [ "$RC" = "$2" ]; then ok; else bad "$1: exit $RC (want $2) $OUT"; fi; }
has(){ if f "$2" | grep -qF -- "$3"; then ok; else bad "$1: $2 lacks $3: $(f "$2")"; fi; }
npasses(){ printf '%s' "$OUT" | "$PY" -c "import sys,json; print(len(json.load(sys.stdin)['passes']))" 2>/dev/null; }
want_passes(){ if [ "$(npasses)" = "$2" ]; then ok; else bad "$1: $(npasses) passes (want $2)"; fi; }
riskpass(){ if printf '%s' "$OUT" | "$PY" -c "import sys,json
d=json.load(sys.stdin); sys.exit(0 if any(p['files']=='risk' and 'security' in p['specialists'] for p in d['passes']) else 1)"; then ok
  else bad "$1: no security pass over the risk files"; fi; }
jget(){ "$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))" "$@"; }
deep_unavail(){ want_rc "$1" 2; want "$1" level deep; want "$1" status unavailable; has "$1" reasons "$2"
  if [ -n "$(f error)" ] && [ "$(f error)" != "null" ]; then ok; else bad "$1: no error string"; fi; riskpass "$1"; }

# ---- (a) a tiny, non-risky diff: quick, one combined pass naming all six ------------------------
mkrepo; put api/invoices/total.py; plan
want_rc "quick" 0
want "quick" level quick
want "quick" status decided
want "quick" reasons '["small-diff"]'
want "quick" files 1
want "quick" changed_lines 1
want "quick" passes "[{\"files\": \"review\", \"name\": \"combined\", \"specialists\": [\"frontend\", \"backend\", \"data\", \"security\", \"tests\", \"perf\"]}]"
want "quick" risk_files '[]'
want "quick" source "$ROOT/limits.json#review_depth"
P1="$OUT"; plan
if [ "$P1" = "$OUT" ]; then ok; else bad "plan output differs across runs"; fi
OUT=$("$PY" "$RD" plan --repo "$R" --limits "$FW/.claude/loops/loop-limits.json" 2>/dev/null)
want "shipped limits source is repo-relative" source '.claude/loops/loop-limits.json#review_depth'

# ---- (b) four files: standard, six passes ------------------------------------------------------
mkrepo; put api/invoices/total.py; put api/members/read.py; put web/src/x.js; put web/src/y.js; plan
want_rc "four files" 0
want "four files" level standard
want "four files" reasons '["size-over-quick"]'
want_passes "four files" 6
want "four files" passes '[{"files": "review", "name": "frontend", "specialists": ["frontend"]}, {"files": "review", "name": "backend", "specialists": ["backend"]}, {"files": "review", "name": "data", "specialists": ["data"]}, {"files": "review", "name": "security", "specialists": ["security"]}, {"files": "review", "name": "tests", "specialists": ["tests"]}, {"files": "review", "name": "perf", "specialists": ["perf"]}]'

# ---- (c) lines over the cap: standard; exactly at both caps: quick -------------------------------
mkrepo; putn api/invoices/total.py 51; plan
want "51 lines" level standard
want "51 lines" reasons '["size-over-quick"]'
mkrepo; putn api/invoices/total.py 48; put api/members/read.py; put web/src/x.js; plan
want "at both caps" files 3
want "at both caps" changed_lines 50
want "at both caps" level quick

# ---- (d) a binary change: the line count is unknown, never quick ----------------------------------
mkrepo; printf '\3\0\4' >> "$R/web/src/logo.bin"; plan
want "binary tracked" level standard
want "binary tracked" reasons '["changed-lines-unknown"]'
want "binary tracked" changed_lines null
mkrepo; printf '\0\0' > "$R/web/src/new.bin"; plan
want "binary untracked" level standard
want "binary untracked" changed_lines null
mkrepo; put api/invoices/new.py; plan
want "untracked text counted" changed_lines 1
want "untracked text counted" level quick

# ---- (e) --changed-file override: never quick ---------------------------------------------------
mkrepo; plan --changed-file api/invoices/total.py
want "override" level standard
want "override" reasons '["changed-lines-unknown"]'

# ---- (f) empty diff: standard -------------------------------------------------------------------
mkrepo; plan
want_rc "empty" 0
want "empty" level standard
want "empty" reasons '["empty-diff"]'

# ---- (g) a tiny diff on an adopter-denylisted path: deep ------------------------------------------
mkrepo; put secret-stuff/a.txt
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-adopter.json" "$PY" "$RD" plan --repo "$R" 2>/dev/null); RC=$?; six "adopter"
want_rc "adopter denylist" 0
want "adopter denylist" level deep
want "adopter denylist" status decided
want "adopter denylist" reasons '["blast-radius"]'
want "adopter denylist" risk_files '[{"file": "secret-stuff/a.txt", "reason": "blast-radius"}]'
want_passes "adopter denylist" 7
riskpass "adopter denylist"
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-adopter.json" "$PY" "$RD" plan --repo "$R" --min-depth quick 2>/dev/null); RC=$?; six "min quick"
want "--min-depth quick never lowers" level deep

# ---- (h) the framework floor still applies when the adopter list omits the category ---------------
for p in db/migrations/001.sql .github/workflows/ci.yml; do
  mkrepo; put "$p"
  OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-adopter.json" "$PY" "$RD" plan --repo "$R" 2>/dev/null); RC=$?; six "floor $p"
  want "floor $p" level deep
  want "floor $p" risk_files "[{\"file\": \"$p\", \"reason\": \"blast-radius\"}]"
  riskpass "floor $p"
done

# ---- (i) shared core: deep ----------------------------------------------------------------------
mkrepo; put api/core/base.py; plan
want "shared core" level deep
want "shared core" reasons '["shared-core"]'
want "shared core" risk_files '[{"file": "api/core/base.py", "reason": "shared-core"}]'

# ---- (j) a rename out of a denylisted directory: the old path is scanned -------------------------
mkrepo; ( cd "$R" && git mv secret-stuff/a.txt api/invoices/a.txt && git commit -qm mv )
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-adopter.json" "$PY" "$RD" plan --repo "$R" 2>/dev/null); RC=$?; six "rename"
want "rename" level deep
has "rename" risk_files 'secret-stuff/a.txt'

# ---- (k) the diff edits the conventions file: deep --------------------------------------------------
mkrepo; conv "$R/conventions.json"; ( cd "$R" && git checkout -q main && git add conventions.json && git commit -qm conv && git checkout -q fix/x && git merge -q main )
stackcfg "$ROOT/stack-self.json" "$R/conventions.json" "$ROOT/limits.json"
put api/invoices/total.py
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-self.json" "$PY" "$RD" plan --repo "$R" 2>/dev/null); RC=$?
want "conventions unchanged" level quick
"$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); d['shared_core_globs']=[]; json.dump(d, open(sys.argv[1],'w'))" "$R/conventions.json"
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-self.json" "$PY" "$RD" plan --repo "$R" 2>/dev/null); RC=$?; six "conv changed"
want_rc "conventions changed" 0
want "conventions changed" level deep
has "conventions changed" reasons conventions-changed
riskpass "conventions changed"

# ---- (l) conventions missing / not JSON: deep, unavailable, exit 2, plan still printed ----------
mkrepo; put api/invoices/total.py
stackcfg "$ROOT/stack-noconv.json" "$ROOT/missing-conv.json" "$ROOT/limits.json"
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-noconv.json" "$PY" "$RD" plan --repo "$R" 2>/dev/null); RC=$?; six "noconv"
deep_unavail "conventions missing" conventions-unreadable
printf '{bad' > "$ROOT/conv-bad.json"; stackcfg "$ROOT/stack-badconv.json" "$ROOT/conv-bad.json" "$ROOT/limits.json"
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-badconv.json" "$PY" "$RD" plan --repo "$R" 2>/dev/null); RC=$?; six "badconv"
deep_unavail "conventions not JSON" conventions-unreadable
printf '[1]' > "$ROOT/conv-arr.json"; stackcfg "$ROOT/stack-arrconv.json" "$ROOT/conv-arr.json" "$ROOT/limits.json"
OUT=$(LOOPS_STACK_CONFIG="$ROOT/stack-arrconv.json" "$PY" "$RD" plan --repo "$R" 2>/dev/null); RC=$?; six "arrconv"
deep_unavail "conventions not an object" conventions-unreadable

# ---- (m) limits block missing / 0 / bool / string: deep limits-invalid exit 2 ----------------------
limvar "$ROOT/lim-none.json" pop
limvar "$ROOT/lim-zero.json" 'b["quick_max_files"] = 0'
limvar "$ROOT/lim-bool.json" 'b["quick_max_changed_lines"] = True'
limvar "$ROOT/lim-str.json" 'b["quick_max_files"] = "3"'
for x in none zero bool str; do
  plan --limits "$ROOT/lim-$x.json"
  deep_unavail "limits $x" limits-invalid
  want "limits $x" limits null
done
printf '{bad' > "$ROOT/lim-badjson.json"; plan --limits "$ROOT/lim-badjson.json"
deep_unavail "limits not JSON" limits-invalid

# ---- (n) the R6 scan cannot run (no bash on PATH): deep r6-scan-unavailable --------------------------
mkdir -p "$ROOT/nobash"; ln -s "$(command -v git)" "$ROOT/nobash/git"
OUT=$(PATH="$ROOT/nobash" "$PY" "$RD" plan --repo "$R" 2>/dev/null); RC=$?; six "nobash"
deep_unavail "R6 scan unavailable" r6-scan-unavailable

# ---- (o) --evid: in-loop floor, sensitive intake, malformed intake ---------------------------------
mkdir -p "$ROOT/ev-none" "$ROOT/ev-sens" "$ROOT/ev-plain" "$ROOT/ev-bad" "$ROOT/ev-arr"
printf '{"key": "PROJ-123", "sensitive": true}' > "$ROOT/ev-sens/intake.json"
printf '{"key": "PROJ-123", "sensitive": false}' > "$ROOT/ev-plain/intake.json"
printf '{bad' > "$ROOT/ev-bad/intake.json"; printf '[1]' > "$ROOT/ev-arr/intake.json"
plan --evid "$ROOT/ev-none"
want_rc "in-loop" 0; want "in-loop" level standard; want "in-loop" reasons '["in-loop-review"]'
plan --evid "$ROOT/ev-plain"
want "in-loop, not sensitive" level standard
plan --evid "$ROOT/ev-sens"
want_rc "sensitive" 0; want "sensitive" level deep; has "sensitive" reasons sensitive-area; riskpass "sensitive"
plan --evid "$ROOT/ev-bad"
deep_unavail "intake malformed" seam-unreadable
plan --evid "$ROOT/ev-arr"
deep_unavail "intake not an object" seam-unreadable

# ---- (p) --min-depth raises, never lowers ---------------------------------------------------------
plan --min-depth deep
want "min deep" level deep; want "min deep" reasons '["min-depth", "small-diff"]'; riskpass "min deep"
plan --min-depth standard
want "min standard" level standard
plan --min-depth quick
want "min quick on quick" level quick; want "min quick on quick" reasons '["small-diff"]'

# ---- (q) --repo not a git repo / missing: deep diff-unavailable, JSON still printed ------------------
mkdir -p "$ROOT/notgit"
run plan --repo "$ROOT/notgit"
deep_unavail "not a git repo" diff-unavailable
want "not a git repo" files null
want "not a git repo" risk_files '[]'
run plan --repo "$ROOT/nope"
deep_unavail "no such dir" diff-unavailable

# ---- (r) record ---------------------------------------------------------------------------------
draft(){ "$PY" - "$1" <<'PY'
import json, sys
doc = {"key": "PROJ-123", "verdict": "approve", "masking": False, "critical": 0, "high": 0,
       "findings": [],
       "validation": {"status": "no-candidates", "validators": 0, "validators_returned": 0, "quorum": 2,
                      "confidence_floor": 80, "candidates": 0, "kept": 0, "rejected": 0},
       "summary": "The reviewer's rationale."}
json.dump(doc, open(sys.argv[1], "w"), indent=2)
PY
}
mkrepo; put api/invoices/total.py
draft "$ROOT/r.json"; cp "$ROOT/r.json" "$ROOT/r.orig"
run record --review "$ROOT/r.json" --repo "$R"
want_rc "record" 0; want "record" level quick
if [ "$(jget "$ROOT/r.json" "d['depth']['level']")" = "quick" ]; then ok; else bad "record: depth not written"; fi
if "$PY" -c "import json,sys; a=json.load(open(sys.argv[1])); b=json.load(open(sys.argv[2])); b.pop('depth'); sys.exit(a!=b)" "$ROOT/r.orig" "$ROOT/r.json"; then ok
else bad "record changed a key other than depth"; fi
if [ "$(jget "$ROOT/r.json" "sorted(d['depth'])")" = "['changed_lines', 'files', 'level', 'passes', 'reasons', 'risk_files', 'source', 'status']" ]; then ok
else bad "record: depth keys $(jget "$ROOT/r.json" "sorted(d['depth'])")"; fi
cp "$ROOT/r.json" "$ROOT/r.first"
run record --review "$ROOT/r.json" --repo "$R"
if cmp -s "$ROOT/r.json" "$ROOT/r.first"; then ok; else bad "record not idempotent"; fi
# finalize after record keeps depth (the SKILL's order: tally, record, finalize)
"$PY" "$RB" finalize --review "$ROOT/r.json" --repo "$R" >/dev/null 2>&1
if [ "$(jget "$ROOT/r.json" "d['depth'] == json.load(open('$ROOT/r.first'))['depth']")" = "True" ]; then ok; else bad "finalize dropped or changed depth"; fi
# a deeper level already recorded is kept
draft "$ROOT/rp.json"; "$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); d['depth']={'level':'deep'}; json.dump(d, open(sys.argv[1],'w'))" "$ROOT/rp.json"
run record --review "$ROOT/rp.json" --repo "$R"
want "prior-record" level deep; has "prior-record" reasons prior-record
if [ "$(jget "$ROOT/rp.json" "d['depth']['level'], len(d['depth']['passes'])")" = "('deep', 7)" ]; then ok; else bad "prior-record: $(jget "$ROOT/rp.json" "d['depth']")"; fi
cp "$ROOT/rp.json" "$ROOT/rp.first"; run record --review "$ROOT/rp.json" --repo "$R"
if cmp -s "$ROOT/rp.json" "$ROOT/rp.first"; then ok; else bad "prior-record not idempotent"; fi
# unavailable plan: the record is still written, at deep, exit 0
draft "$ROOT/ru.json"; run record --review "$ROOT/ru.json" --repo "$R" --limits "$ROOT/lim-none.json"
want_rc "record unavailable" 0
if [ "$(jget "$ROOT/ru.json" "d['depth']['level'], d['depth']['status'], 'error' in d['depth']")" = "('deep', 'unavailable', True)" ]; then ok; else bad "record unavailable: $(jget "$ROOT/ru.json" "d['depth']")"; fi
# a risk diff recorded deep
mkrepo; put db/migrations/001.sql
draft "$ROOT/rd.json"; run record --review "$ROOT/rd.json" --repo "$R"
if [ "$(jget "$ROOT/rd.json" "d['depth']['level'], d['depth']['risk_files']")" = "('deep', [{'file': 'db/migrations/001.sql', 'reason': 'blast-radius'}])" ]; then ok; else bad "record deep: $(jget "$ROOT/rd.json" "d['depth']")"; fi
# an unreadable draft is untouched, exit 2
for body in '{bad' '[1]'; do
  printf '%s' "$body" > "$ROOT/rn.json"; cp "$ROOT/rn.json" "$ROOT/rn.first"
  run record --review "$ROOT/rn.json" --repo "$R"; want_rc "record unreadable $body" 2
  if cmp -s "$ROOT/rn.json" "$ROOT/rn.first"; then ok; else bad "record touched an unreadable draft"; fi
done

# ---- (s) recorded drafts validate against the contract ------------------------------------------
for x in r rp ru rd; do
  if "$PY" "$V" "$C" "$ROOT/$x.json" >/dev/null; then ok; else bad "$x.json fails the contract: $("$PY" "$V" "$C" "$ROOT/$x.json")"; fi
done
# and DEV-REVIEW-D1 refuses the tampered shapes
d1(){ cp "$ROOT/$1.json" "$ROOT/d1.json"; "$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); exec(sys.argv[2]); json.dump(d, open(sys.argv[1],'w'))" "$ROOT/d1.json" "$2"
  if "$PY" "$V" "$C" "$ROOT/d1.json" 2>&1 | grep -q 'DEV-REVIEW-D1'; then ok; else bad "D1 accepted: $2"; fi; }
d1 rd 'd["depth"]["level"] = "quick"'
d1 rd 'd["depth"]["passes"] = d["depth"]["passes"][:6]'
d1 rd 'd["depth"]["passes"] = [p for p in d["depth"]["passes"] if p["name"] != "perf"]'
d1 r 'd["depth"]["passes"][0]["specialists"].remove("security")'
d1 r 'd["depth"]["reasons"] = ["size-over-quick"]'
d1 r 'd["depth"]["status"] = "unavailable"'
d1 r 'd["depth"]["changed_lines"] = None'
d1 ru 'd["depth"]["level"] = "standard"; d["depth"]["passes"] = d["depth"]["passes"][:6]'

# ---- (t) check ----------------------------------------------------------------------------------
run check --review "$ROOT/rd.json" --repo "$R"
want_rc "check clean" 0; want "check clean" ok true
tamper(){ cp "$ROOT/rd.json" "$ROOT/t.json"; "$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); exec(sys.argv[2]); json.dump(d, open(sys.argv[1],'w'))" "$ROOT/t.json" "$1"
  run check --review "$ROOT/t.json" --repo "$R"; want_rc "tamper $2" 1; has "tamper $2" violations "\"$2\""; }
tamper 'd["depth"]["level"] = "quick"; d["depth"]["passes"] = d["depth"]["passes"][:1]' depth-below-plan
tamper 'd["depth"]["risk_files"] = []' risk-file-unrecorded
tamper 'd["depth"]["passes"] = d["depth"]["passes"][:6]' security-not-on-risk-path
tamper 'd["depth"]["passes"] = d["depth"]["passes"][:6]' passes-mismatch
tamper 'del d["depth"]' depth-missing
printf '{bad' > "$ROOT/tb.json"; run check --review "$ROOT/tb.json" --repo "$R"; want_rc "check unreadable" 2

# ---- (v) the shipped loop-limits block -----------------------------------------------------------
if "$PY" -c "import json,sys
b=json.load(open('$FW/.claude/loops/loop-limits.json'))['review_depth']
ok=all(isinstance(b[k], int) and not isinstance(b[k], bool) and b[k] >= 1 for k in ('quick_max_files','quick_max_changed_lines'))
sys.exit(0 if ok and 'ARGUED, NOT MEASURED' in b['rule'] else 1)"; then ok; else bad "shipped review_depth block invalid"; fi

# ---- (w) bad usage ------------------------------------------------------------------------------
for args in "" "plan --min-depth bogus" "record --repo $R" "check" "frobnicate"; do
  # shellcheck disable=SC2086
  run $args; want_rc "usage '$args'" 2
  if [ -n "$(f error)" ] && [ "$(f error)" != "null" ]; then ok; else bad "usage '$args': no error key"; fi
done

# ---- offline tripwire ---------------------------------------------------------------------------
if grep -qE 'urllib|requests|socket|kb\.py' "$RD"; then bad "review_depth.py reaches for the network or the brain"; else ok; fi

echo "  review_depth: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
