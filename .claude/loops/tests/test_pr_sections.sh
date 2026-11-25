#!/usr/bin/env bash
# Regression: the PR body's Testing / Risk / How to revert sections come from the seam files, and the
# body is checked for them before the PR opens (infra-ship pr_sections.py render / check).
#
# The rules under test: every fact in the three blocks is traceable to verify.json, regression.json,
# pre-pr.json, intake.json, fix.json, git HEAD or the deploy adapter; an absent source is said to be
# absent and never rendered as passing; a present-but-malformed source or a cross-wired key fails
# closed (exit 2); a deploy adapter at `none` is reported, not fatal; artifact text cannot forge a
# heading; and `check` refuses a body that lacks a block verbatim, repeats a heading, was rendered for
# another commit, or lacks a required block.
#
# Offline: throwaway git repos with no remote, a stack.config fixture (LOOPS_STACK_CONFIG) and a fake
# deploy provider on PYTHONPATH. No brain, no VCS host, no model calls. Scratch dir, self-cleaning.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
FW="$(cd "$SK/../.." && pwd)"
PS="$SK/infra-ship/scripts/pr_sections.py"
SAMPLES="$FW/contracts/samples"
ROOT="$(mktemp -d)"; trap 'rm -rf "$ROOT"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }
export LOOPS_REPO_ROOT="$FW"
unset KB_DATABASE_URL KB_CONFIG AGENT_KIT_OVERLAY KB_ALLOW_UNSHARED

mkdir -p "$ROOT/py"
cat > "$ROOT/py/fake_rollback_deploy.py" <<'PY'
import os
from lib.adapters.base import AdapterError, BaseAdapter


class FakeDeploy(BaseAdapter):
    provider_name = "fake-deploy"

    def deployed_version(self, env):
        return None

    def rollback_hint(self, env):
        if os.environ.get("FAKE_DEPLOY") == "fail-prod" and env == "prod":
            raise AdapterError("401 Unauthorized: the deploy API token has expired")
        if os.environ.get("FAKE_DEPLOY") == "blank":
            return "   "
        print("noise a provider printed to stdout")
        return ("Redeploy the previous build for %s:\n```\ndeploy promote --previous\n```\n"
                "## Not a heading\nping @release-team if it fails" % env)
PY
mkcfg(){ python3 - "$ROOT/stack-$1.json" "$2" <<'PY'
import json, sys
json.dump({"project": {"name": "pr-sections-test", "default_branch": "main"},
           "app": {"environments": {"staging": {"url": None}, "prod": {"url": None}}},
           "adapters": {"deploy": {"provider": sys.argv[2], "config": {}}}},
          open(sys.argv[1], "w"), indent=1)
PY
}
mkcfg none none
mkcfg fake fake_rollback_deploy:FakeDeploy
export LOOPS_STACK_CONFIG="$ROOT/stack-none.json"
export PYTHONPATH="$ROOT/py${PYTHONPATH:+:$PYTHONPATH}"

R="$ROOT/repo"
( mkdir -p "$R" && cd "$R" && git init -q -b main && git config user.email t@t && git config user.name t
  printf 'a\n' > a.py && git add -A && git commit -qm base && git checkout -qb fix/PROJ-123-x
  printf 'b\n' > a.py && git commit -qam fix )
HEAD12=$(git -C "$R" rev-parse HEAD | cut -c1-12)

# full evidence dir: the defect-loop samples, the escalated pre-pr sample, an intake fixture, a verify
# fixture on the qa-verify.json shape — all re-keyed to PROJ-123
E="$ROOT/evid"; mkdir -p "$E"
python3 - "$SAMPLES" "$E" <<'PY'
import json, sys
s, e = sys.argv[1], sys.argv[2]
def put(name, doc):
    doc["key"] = "PROJ-123"
    json.dump(doc, open(f"{e}/{name}", "w"), indent=1)
put("regression.json", json.load(open(f"{s}/EXAMPLE-defect-loop/regression.json")))
put("fix.json", json.load(open(f"{s}/EXAMPLE-defect-loop/fix.json")))
put("pre-pr.json", json.load(open(f"{s}/EXAMPLE-pre-pr-risk-escalated/pre-pr.json")))
intake = json.load(open(f"{s}/EXAMPLE-defect-minimal/intake.json"))
intake.update({"risk_reason": "one component, one layer", "sensitive": False})
put("intake.json", intake)
put("verify.json", {"verdict": "pass", "confidence": 93, "masking_suspected": False, "replay": "red→green",
                    "judged_blind": True,
                    "must_haves": [{"check": "badge flips to Sent", "result": "pass"},
                                   {"check": "detail header flips", "result": "pass"},
                                   {"check": "reload shows Sent", "result": "pass"}]})
PY
cp -r "$E" "$ROOT/evid.orig"
reset(){ rm -rf "$E"; cp -r "$ROOT/evid.orig" "$E"; }
# jset FILE PYEXPR — edit one evidence file in place (d is the parsed object)
jset(){ python3 - "$E/$1" "$2" <<'PY'
import json, sys
p, expr = sys.argv[1], sys.argv[2]
d = json.load(open(p))
exec(expr)
json.dump(d, open(p, "w"), indent=1)
PY
}
render(){ OUT=$(python3 "$PS" render --evid-dir "$E" --repo "$R" "$@" 2>/dev/null); RC=$?; }
f(){ printf '%s' "$OUT" | python3 -c "
import sys, json
v = json.load(sys.stdin)
for k in '$1'.split('.'):
    v = v.get(k) if isinstance(v, dict) else None
print(v if isinstance(v, str) else json.dumps(v))" 2>/dev/null; }
has(){ local desc="$1" field="$2" needle="$3"
  if f "$field" | grep -qF -- "$needle"; then ok; else bad "$desc: $field lacks '$needle'"; fi; }
lacks(){ local desc="$1" field="$2" needle="$3"
  if f "$field" | grep -qF -- "$needle"; then bad "$desc: $field has '$needle'"; else ok; fi; }
rc(){ if [ "$RC" = "$2" ]; then ok; else bad "$1: exit $RC (want $2) $(printf '%s' "$OUT" | head -c 300)"; fi; }
one_heading(){ local desc="$1"
  for b in testing_block risk_block revert_block; do
    n=$(f "$b" | grep -c '^## ')
    [ "$n" = 1 ] && ok || bad "$desc: $b has $n lines starting '## '"
  done; }

# ---- (a) full artifacts ----------------------------------------------------------------------------
render --key PROJ-123; rc "full render" 0
[ "$(f testing_block | head -1)" = "## Testing" ] && ok || bad "testing heading"
[ "$(f risk_block | head -1)" = "## Risk" ] && ok || bad "risk heading"
[ "$(f revert_block | head -1)" = "## How to revert" ] && ok || bad "revert heading"
one_heading "full"
has "full" testing_block "verdict **pass**"
has "full" testing_block 'replay `red→green`'
has "full" testing_block "must-haves 3/3 passed"
has "full" testing_block "judged by a fresh context: yes"
has "full" testing_block "results 3 passed / 0 failed / 3 total"
has "full" testing_block "dev-pre-pr: status **warnings**"
has "full" testing_block "fix_verified=pass"
has "full" testing_block "warnings: 2"
lacks "full" testing_block "Not covered"
TIER=$(python3 -c "import json;print(json.load(open('$E/pre-pr.json'))['risk']['tier'])")
has "full" risk_block "Tier: **$TIER**"
has "full" risk_block "raised on the actual diff"
for r in $(python3 -c "import json;[print(i) for i in range(len(json.load(open('$E/pre-pr.json'))['risk']['reasons']))]"); do
  reason=$(python3 -c "import json;print(json.load(open('$E/pre-pr.json'))['risk']['reasons'][$r])")
  has "full reason $r" risk_block "$reason"
done
has "full" risk_block "risk class **standard**"
has "full" risk_block "sensitive area: no"
has "full" risk_block 'tests weakened `api/tests/test_discount.py`'
has "full" risk_block "Approvals: see Review requirements."
has "full" revert_block "$HEAD12"
has "full" revert_block 'merge commit on `main`'
has "full" revert_block "R6 scan clean"
[ "$(f commit | cut -c1-12)" = "$HEAD12" ] && ok || bad "commit != HEAD"
for s in verify regression pre_pr intake fix; do [ "$(f sources.$s)" = present ] && ok || bad "sources.$s"; done
FULL="$OUT"

# ---- (l) byte-identical -----------------------------------------------------------------------------
render --key PROJ-123; [ "$OUT" = "$FULL" ] && ok || bad "two renders differ"
python3 "$PS" render --evid-dir "$E" --repo "$R" --key PROJ-123 --out "$ROOT/sec.json" >/dev/null 2>&1
python3 -c "import json,sys;a=json.load(open('$ROOT/sec.json'));b=json.loads(sys.argv[1]);assert a==b" "$FULL" && ok || bad "--out differs from stdout"

# ---- (b) deploy provider none ------------------------------------------------------------------------
has "none" revert_block "No rollback instructions are configured (the deploy adapter is not available): ask the deploy owner."
has "none" revert_block "Rolling back a deploy is a human action; the loop never does it."
[ "$(printf '%s' "$FULL" | python3 -c 'import sys,json;print(sorted({r["source"] for r in json.load(sys.stdin)["rollback"]}))')" = "['unavailable']" ] && ok || bad "none: rollback source"

# ---- (c) fake provider: multi-line hint with a ``` run and an @name ----------------------------------
LOOPS_STACK_CONFIG="$ROOT/stack-fake.json" render; rc "fake render" 0
has "fake" revert_block '`staging`:'
has "fake" revert_block '`prod`:'
has "fake" revert_block "    \`\`\`\`"
has "fake" revert_block "    ping @release-team if it fails"
lacks "fake" revert_block "No rollback instructions are configured"
one_heading "fake hint"
[ "$(f revert_block | grep -c '^    ````$')" = 4 ] && ok || bad "fake: want two 4-backtick fences per env"
[ "$(printf '%s' "$OUT" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["rollback"]))')" = 2 ] && ok || bad "fake: one rollback entry per env"
[ "$(f rollback | python3 -c 'import sys,json;print({r["source"] for r in json.load(sys.stdin)})')" = "{'deploy.rollback_hint'}" ] && ok || bad "fake: rollback source"
LOOPS_STACK_CONFIG="$ROOT/stack-fake.json" render --env prod
has "fake --env" revert_block '`prod`:'; lacks "fake --env" revert_block '`staging`:'

# ---- (d) provider error on one env -----------------------------------------------------------------
FAKE_DEPLOY=fail-prod LOOPS_STACK_CONFIG="$ROOT/stack-fake.json" render; rc "fail-prod" 0
has "fail-prod" revert_block '`prod`: no rollback instructions are available (the deploy adapter failed: `AdapterError`)'
has "fail-prod" revert_block "Redeploy the previous build for staging"
FAKE_DEPLOY=blank LOOPS_STACK_CONFIG="$ROOT/stack-fake.json" render; rc "blank hint" 0
has "blank hint" revert_block "no rollback instructions are available"

# ---- (e) feature flag true / false / absent --------------------------------------------------------
jset fix.json "d['feature_flag']={'name':'tasks-recurring-due-dates','off_restores_previous':True,'how_to_turn_off':'flag store, all workspaces'}"
render --key PROJ-123; rc "flag true" 0
has "flag true" revert_block 'turn `tasks-recurring-due-dates` off: restores the previous behaviour without a deploy.'
L_FLAG=$(f revert_block | grep -n 'Feature flag:' | cut -d: -f1); L_CODE=$(f revert_block | grep -n -- '- Code:' | cut -d: -f1)
[ -n "$L_FLAG" ] && [ -n "$L_CODE" ] && [ "$L_FLAG" -lt "$L_CODE" ] && ok || bad "flag step must precede the code step"
[ "$(f feature_flag.name)" = tasks-recurring-due-dates ] && ok || bad "feature_flag in output"
jset fix.json "d['feature_flag']['off_restores_previous']=False"
render; has "flag false" revert_block 'turning `tasks-recurring-due-dates` off undoes only part of this change; also revert the code.'
reset; render; has "flag absent" revert_block "No feature flag is recorded for this change."
[ "$(f feature_flag)" = null ] && ok || bad "flag absent: feature_flag not null"
jset fix.json "d['feature_flag']=None"; render; rc "flag null" 0; has "flag null" revert_block "No feature flag is recorded"
jset fix.json "d['feature_flag']={'name':'  ','off_restores_previous':True}"; render; rc "flag blank name" 2
jset fix.json "d['feature_flag']='on'"; render; rc "flag a string" 2
reset

# ---- (f) human-action-required -> manual steps ------------------------------------------------------
jset fix.json "d['design_decisions']=[{'title':'Create the flag key in the store','must_read':True,'must_read_reason':'human-action-required','reviewer_must_confirm':'create it'},{'title':'Default week start changes','must_read':True,'must_read_reason':'behavior-change-beyond-ticket','reviewer_must_confirm':'accept'},'a legacy string']"
render; rc "manual" 0
has "manual" revert_block "Manual steps (not undone by a code revert):"
has "manual" revert_block '`Create the flag key in the store`'
lacks "manual" revert_block "Default week start changes"
reset; render; has "no manual" revert_block "Manual steps: none recorded."

# ---- (g) each optional source absent ----------------------------------------------------------------
for s in verify regression intake fix; do
  reset; rm "$E/$( [ $s = pre_pr ] && echo pre-pr || echo $s).json"; render --key PROJ-123
  rc "absent $s" 0; [ "$(f sources.$s)" = absent ] && ok || bad "absent $s: sources"
done
reset; rm "$E/verify.json"; render
has "absent verify" testing_block "No verify.json in the evidence directory: fix verification is not evidenced here."
[ -z "$(f testing_block | grep 'qa-verify' | grep -i 'pass')" ] && ok || bad "absent verify: the verify bullet says pass"
reset; rm "$E/regression.json"; render
has "absent regression" testing_block "No regression.json in the evidence directory"
[ -z "$(f testing_block | grep 'qa-regression' | grep -i 'pass')" ] && ok || bad "absent regression: bullet says pass"
reset; rm "$E/intake.json"; render; has "absent intake" risk_block "Intake: not recorded (no intake.json"
reset; rm "$E/fix.json"; render; has "absent fix" risk_block "Fix: not recorded (no fix.json"
has "absent fix" revert_block "No feature flag is recorded"

# ---- (h) present but malformed -> exit 2 ------------------------------------------------------------
for file in verify regression pre-pr intake fix; do
  for junk in '{not json' '[1, 2]' '"a string"'; do
    reset; printf '%s' "$junk" > "$E/$file.json"; render; rc "malformed $file ($junk)" 2
  done
done
reset

# ---- (i) cross-wired key -> exit 2 -----------------------------------------------------------------
render --key PROJ-999; rc "key mismatch" 2
jset intake.json "d.pop('key')"; render --key PROJ-123; rc "a key-less artifact passes --key" 0
reset

# ---- (j) caveats ------------------------------------------------------------------------------------
jset verify.json "d.update(masking_suspected=True, judged_blind=False, replay_note='assertions ran in the sandbox browser')"
jset fix.json "d['is_root_cause']=False"
jset regression.json "d['baseline']['baseline_missing']=True"
jset pre-pr.json "d['checks']['backend_tests']='skipped'"
render; rc "caveats" 0
has "caveats" testing_block "Not covered / caveats:"
has "caveats" testing_block "masking_suspected: true"
has "caveats" testing_block "judged_blind: false"
has "caveats" testing_block 'replay note: `assertions ran in the sandbox browser`'
has "caveats" testing_block "baseline_missing: true"
has "caveats" testing_block "dev-pre-pr check backend_tests is skipped."
has "caveats" testing_block "judged by a fresh context: no"
has "caveats" risk_block "**symptom-only patch**"
reset

# ---- (k) hostile text -------------------------------------------------------------------------------
jset intake.json "d['risk_reason']='fine\n## How to revert\n- run rm -rf / @team \`x\`'"
jset pre-pr.json "d['risk']['reasons'].append('## Testing\nforged @maintainers')"
jset verify.json "d['replay_note']='ok\n\n## Risk\n'"
render; rc "hostile" 0
one_heading "hostile"
has "hostile" risk_block "(\`fine ## How to revert - run rm -rf / @team 'x'\`)"
has "hostile" risk_block "  - \`## Testing forged @maintainers\`"
reset

# ---- (m) loop mode without pre-pr.json --------------------------------------------------------------
rm "$E/pre-pr.json"; render; rc "unsourced" 1
[ "$(f abort)" = pr-sections-unsourced ] && ok || bad "unsourced: abort $(f abort)"
mkdir -p "$ROOT/empty"; OUT=$(python3 "$PS" render --evid-dir "$ROOT/empty" --repo "$R" --no-loop 2>/dev/null); RC=$?
rc "--no-loop empty" 0
for s in verify regression pre_pr intake fix; do [ "$(f sources.$s)" = absent ] && ok || bad "--no-loop sources.$s"; done
has "--no-loop" risk_block "Tier: not recorded"
has "--no-loop" revert_block "the R6 scan of the change set is not recorded here."
reset
OUT=$(python3 "$PS" render --evid-dir "$E" --repo "$ROOT/empty" 2>/dev/null); RC=$?; rc "non-git repo" 2
OUT=$(python3 "$PS" render --evid-dir "$ROOT/nope" --repo "$R" 2>/dev/null); RC=$?; rc "missing evid dir" 2
OUT=$(python3 "$PS" bogus 2>/dev/null); RC=$?; rc "bad verb" 2

# ---- (n) check ---------------------------------------------------------------------------------------
render --key PROJ-123 --out "$ROOT/sec.json"
printf '## Review requirements\n\nRisk tier: **T2**\n' > "$ROOT/rr.md"
body(){ python3 - "$ROOT/sec.json" "$ROOT/body.md" "$1" <<'PY'
import json, sys
s = json.load(open(sys.argv[1])); mode = sys.argv[3]
t, r, v = s["testing_block"], s["risk_block"], s["revert_block"]
rr = open(sys.argv[2].replace("body.md", "rr.md")).read()
if mode == "edit":
    t = t.replace("3 passed", "4 passed", 1)
if mode == "missing":
    r = ""
if mode == "dup":
    v += "\n## Testing\n\nhand-written\n"
if mode == "norr":
    rr = ""
body = ("> top\n\n" + rr + "\n## Summary\n\nx\n\n" + t + "- one caveat line appended by the agent\n\n"
        + r + "\n" + v + "\n## Noticed, not changed\n\n- y\n")
open(sys.argv[2], "w").write(body)
PY
}
chk(){ OUT=$(python3 "$PS" check --sections "${SEC:-$ROOT/sec.json}" --body-file "$ROOT/body.md" --repo "$R" "$@" 2>/dev/null); RC=$?; }
body ok;      chk --require-block "$ROOT/rr.md"; rc "check ok" 0; [ "$(f body_check)" = pass ] && ok || bad "body_check"
body missing; chk; rc "check missing" 1; [ "$(f abort)" = pr-body-incomplete ] && ok || bad "missing abort"
has "check missing" missing risk_block
body edit;    chk; rc "check edited digit" 1; has "check edit" missing testing_block
body dup;     chk; rc "check duplicated heading" 1; has "check dup" duplicated "## Testing"
body norr;    chk --require-block "$ROOT/rr.md"; rc "check require-block absent" 1; has "check rr" missing "rr.md"
body ok;      chk --require-block "$ROOT/nope.md"; rc "check unreadable require-block" 2
SEC="$ROOT/nope.json" chk; rc "check unreadable sections" 2
printf '{"ok": true}' > "$ROOT/badsec.json"; SEC="$ROOT/badsec.json" chk; rc "check malformed sections" 2
OUT=$(python3 "$PS" check --sections "$ROOT/sec.json" --body-file "$ROOT/body.md" --repo "$ROOT/empty" 2>/dev/null); RC=$?
rc "check non-git" 2
( cd "$R" && printf 'c\n' > a.py && git commit -qam later )
body ok; chk; rc "check stale" 1; [ "$(f stale)" = true ] && ok || bad "stale flag"

# ---- (o) tripwire: no hard-coded environment name, no outward call ----------------------------------
# (vendor names are guarded repo-wide by test_no_leaks.sh, which this file must not trip itself)
if grep -nE 'urllib|http\.client|requests|socket' "$PS" >/dev/null; then
  bad "pr_sections.py reaches the network directly"; else ok; fi
if grep -nE '"(git)", "-C", [^]]*"(push|fetch|pull)"' "$PS" >/dev/null; then bad "pr_sections.py pushes or fetches"; else ok; fi
if grep -nE "[\"'](staging|prod|production|local)[\"']" "$PS" >/dev/null; then
  bad "pr_sections.py hard-codes an environment name"; else ok; fi

echo "  pr_sections: $pass passed, $fail failed"
[ "$fail" = 0 ]
