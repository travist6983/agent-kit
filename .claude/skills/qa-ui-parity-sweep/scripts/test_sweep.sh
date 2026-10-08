#!/usr/bin/env bash
# Offline tests for qa-ui-parity-sweep's deterministic half (sweep.py + the collector).
#
#   bash .claude/skills/qa-ui-parity-sweep/scripts/test_sweep.sh
#
# Fixture dumps (scripts/fixtures.py) stand in for what probe.js posts, so every grading rule, the
# coverage accounting (no silent skips), the budget phases, the fix cap and the report are checked
# without a browser. No browser, no vcs_host calls, no brain writes (writeback runs --dry-run).
# A test-local stack.config (LOOPS_STACK_CONFIG) names the two environments: baseline "design",
# candidate "staging". Needs bash + python3 + git.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
S="$HERE/sweep.py"
FX="$HERE/fixtures.py"
T="$(mktemp -d "${TMPDIR:-/tmp}/test-parity-sweep.XXXXXX")"
trap 'python3 "$S" stop --run "$T/run" >/dev/null 2>&1; rm -rf "$T"' EXIT
PASS=0; FAIL=0
ok()  { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL  $1"; [ -n "${2:-}" ] && echo "        $2"; FAIL=$((FAIL+1)); }
jq_() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2], {'d': d}))" "$@"; }

python3 "$FX" "$T/stack.config.json" --stack > /dev/null
python3 "$FX" "$T/routes.json" --routes > /dev/null
export LOOPS_STACK_CONFIG="$T/stack.config.json"

echo "qa-ui-parity-sweep — sweep.py offline tests"
python3 "$S" init --routes "$T/routes.json" --run "$T/run" > "$T/init.json"; R=$?
[ "$R" = 0 ] && [ "$(jq_ "$T/init.json" "d['routes'][0]['candidate_url']")" = "https://staging.example.com/a" ] \
  && [ "$(jq_ "$T/init.json" "d['envs']['baseline']['name'], d['envs']['candidate']['name']")" = "('design', 'staging')" ] \
  && ok "init: environments resolved from stack.config (integrations.ui_parity + app.environments), plan written, URLs joined" || bad "init" "rc=$R $(cat "$T/init.json")"
python3 "$S" init --routes "$T/routes.json" --run "$T/run" > /dev/null 2>&1; R=$?
[ "$R" = 1 ] && ok "init refuses to overwrite an existing run" || bad "init overwrite" "rc=$R"
python3 "$S" init --routes "$T/routes.json" --only alpha,nope --run "$T/run2" > /dev/null 2>&1; R=$?
[ "$R" = 1 ] && ok "init --only rejects an unknown route id" || bad "init --only" "rc=$R"
python3 "$S" init --routes "$T/routes.json" --candidate-env prod --run "$T/run3" > "$T/init3.json" 2>&1; R=$?
[ "$R" = 1 ] && grep -q "has no url under app.environments" "$T/init3.json" && ok "init: an environment without a url is a usage failure, never a guess" || bad "init env url" "rc=$R"
python3 "$S" init --routes "$T/routes.json" --candidate-url https://pr-42.example.com --run "$T/run4" > "$T/init4.json"; R=$?
[ "$R" = 0 ] && [ "$(jq_ "$T/init4.json" "d['routes'][0]['candidate_url']")" = "https://pr-42.example.com/a" ] \
  && ok "init: --candidate-url overrides the configured url" || bad "init url override" "rc=$R"
python3 "$S" init --run "$T/run5" > "$T/init5.json" 2>&1; R=$?
[ "$R" = 1 ] && grep -q "no routes" "$T/init5.json" && ok "init: no --routes and an empty integrations.ui_parity.routes is a usage failure (the template is never a silent default)" || bad "init no routes" "rc=$R"

# --- budget phases (rewind the clock in the plan) ---------------------------------------------
python3 "$S" budget --run "$T/run" --phase capture > /dev/null; R=$?
[ "$R" = 0 ] && ok "budget: capture phase open at t=0" || bad "budget open" "rc=$R"
python3 - "$T/run/plan.json" 2000 <<'PY'
import json, sys
p = json.load(open(sys.argv[1])); shift = float(sys.argv[2])
p["started_at"] -= shift; p["deadlines"] = {k: v - shift for k, v in p["deadlines"].items()}
json.dump(p, open(sys.argv[1], "w"))
PY
python3 "$S" budget --run "$T/run" --phase capture > "$T/b.json"; R1=$?
python3 "$S" budget --run "$T/run" --phase fixes > /dev/null; R2=$?
[ "$R1" = 3 ] && [ "$R2" = 0 ] && ok "budget: at 33m20s capture is closed (exit 3), fixes still open" || bad "budget phases" "capture=$R1 fixes=$R2"
python3 - "$T/run/plan.json" -2000 <<'PY'
import json, sys
p = json.load(open(sys.argv[1])); shift = float(sys.argv[2])
p["started_at"] -= shift; p["deadlines"] = {k: v - shift for k, v in p["deadlines"].items()}
json.dump(p, open(sys.argv[1], "w"))
PY

# --- collector round trip ------------------------------------------------------------------------
python3 "$S" serve --run "$T/run" --port 5197 --background > "$T/serve.json"; R=$?
if [ "$R" = 0 ]; then
  CODE="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5197/probe.js)"
  CFGN="$(curl -s http://127.0.0.1:5197/config.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['settle_selectors']), d['guard_allow_hosts'])")"
  curl -s -X POST --data '{"meta":{},"items":[]}' "http://127.0.0.1:5197/save?name=zeta/baseline/base.json" > /dev/null
  BAD="$(curl -s -o /dev/null -w '%{http_code}' -X POST --data '{}' "http://127.0.0.1:5197/save?name=../../escape.json")"
  NJ="$(curl -s -o /dev/null -w '%{http_code}' -X POST --data 'not json' "http://127.0.0.1:5197/save?name=zeta/candidate/base.json")"
  [ "$CODE" = 200 ] && [ -f "$T/run/dumps/zeta/baseline/base.json" ] && [ "$BAD" = 400 ] && [ "$NJ" = 422 ] \
    && ok "collector: serves probe.js, saves a dump, refuses path escape (400) and non-JSON (422)" || bad "collector" "probe=$CODE esc=$BAD nj=$NJ"
  [ "$CFGN" = "3 []" ] && ok "collector: /config.json serves the probe's per-stack knobs from the plan" || bad "collector config" "$CFGN"
  rm -rf "$T/run/dumps/zeta"
  python3 "$S" stop --run "$T/run" > /dev/null
else
  bad "collector background start" "$(cat "$T/serve.json")"
fi

# --- fixtures ------------------------------------------------------------------------------------
python3 "$FX" "$T/run" > /dev/null

python3 "$S" mark --run "$T/run" --route delta --status skipped > /dev/null 2>&1; R=$?
[ "$R" = 1 ] && ok "mark: a skip without --reason is refused" || bad "mark without reason" "rc=$R"
python3 "$S" mark --run "$T/run" --route delta --env candidate --status failed --reason "staging returned 502" > /dev/null; R=$?
[ "$R" = 0 ] && ok "mark: a failure with a reason is recorded" || bad "mark with reason" "rc=$R"

# console parsing: the text shape the browser tool's console reader prints
printf '[error] Uncaught ReferenceError: foo is not defined\n[warning] slow network\n[log] ok\n' | python3 "$S" console --run "$T/run" --route delta --env baseline > "$T/c.json"
[ "$(jq_ "$T/c.json" "(d['errors'], d['warnings'])")" = "(1, 1)" ] && ok "console: text lines parsed into 1 error + 1 warning" || bad "console parse" "$(cat "$T/c.json")"
rm -f "$T/run/console/delta.baseline.json"

python3 "$S" diff --run "$T/run" > "$T/diff.json"; R=$?
[ "$R" = 0 ] && ok "diff runs" || bad "diff" "rc=$R $(cat "$T/diff.json")"
F="$T/run/findings.json"
has() { jq_ "$F" "any(f['route']=='$1' and f['category']=='$2' and f['severity']=='$3' $4 for f in d['findings'])"; }
chk() { [ "$(has "$@")" = True ] && ok "grade: $1 $2 → $3 ${5:-}" || bad "grade: $1 $2 → $3 ${5:-}" "$(jq_ "$F" "[(f['category'],f['severity'],f['title'][:50]) for f in d['findings'] if f['route']=='$1']")"; }
chk alpha missing-in-candidate high "and 'Export' in f['title']" "(missing control)"
chk alpha hidden-in-candidate high "and 'Filter' in f['title']" "(hidden control)"
chk alpha disabled-in-candidate high "and 'Share' in f['title']" "(disabled control)"
chk alpha console-error high "and 'reading' in f['title']" "(candidate console error)"
chk alpha dialog-does-not-open high "and 'invite' in f['title']" "(dialog opens in the baseline only, trigger fine)"
N="$(jq_ "$F" "(sum(1 for f in d['findings'] if 'share' in f['title'].lower() and f['category']=='dialog-does-not-open'), [len(f.get('also',[])) for f in d['findings'] if f['category']=='disabled-in-candidate'])")"
[ "$N" = "(0, [1])" ] && ok "one cause, one High: the Share dialog failure folds into the disabled Share button" || bad "dialog fold" "$N"
chk alpha style-drift medium "and 'New project' in f['title']" "(padding + background)"
chk alpha label-drift medium "and 'Task List' in f['title']" "(Task List vs Task list)"
chk alpha missing-in-candidate medium "and 'Upcoming' in f['title']" "(missing heading)"
chk alpha extra-in-candidate medium "and 'Beta feedback' in f['title']" "(extra control)"
chk alpha style-drift medium "and 'Close' in f['title'] and 'dialog' in f['element']['where']" "(inside a dialog)"
chk alpha subpixel-drift low "and 'Sort' in f['title']" "(1px padding)"
chk alpha style-drift medium "and 'Overview' in f['title']" "(text-transform, not a label)"
N="$(jq_ "$F" "sum(1 for f in d['findings'] if f['category']=='label-drift' and 'OVERVIEW' in f['title'])")"
[ "$N" = 0 ] && ok "a case-only difference caused by text-transform is not label drift" || bad "text-transform label drift" "$N"
chk alpha subpixel-drift low "and 'Tint' in f['title']" "(colour within 3/255)"
chk gamma page-not-rendered high "" "(candidate root missing)"
N="$(jq_ "$F" "sum(1 for f in d['findings'] if 'rename' in f['title'])")"
[ "$N" = 0 ] && ok "a candidate trigger not found by the baseline's label is NOT graded 'dialog does not open'" || bad "trigger-not-found graded" "$N"
N="$(jq_ "$F" "sum(1 for f in d['findings'] if 'task' in f['title'].lower() and f['category'] in ('missing-in-candidate','extra-in-candidate'))")"
[ "$N" = 0 ] && ok "same component with different data is paired, not reported missing/extra" || bad "data pairing" "$N task-row findings"
N="$(jq_ "$F" "sum(1 for f in d['findings'] if f['category']=='console-error')")"
[ "$N" = 1 ] && ok "console: duplicate error deduped, the routes file's noise ignored, warnings not graded" || bad "console grading" "$N console findings"
N="$(jq_ "$F" "len(d['info']['baseline_console_errors']), len(d['info']['warnings']), d['info']['noise']")"
[ "$N" = "(1, 1, {'support chat widget': 1})" ] && ok "console info: baseline error + warning + noise count kept for the report" || bad "console info" "$N"
N="$(jq_ "$F" "sum(1 for f in d['findings'] if f['route']=='beta')")"
[ "$N" = 0 ] && ok "a route measured while hidden yields no style findings" || bad "hidden route" "$N findings"
N="$(jq_ "$F" "[f['id'] for f in d['findings']][:1]")"
[ "$N" = "['H-01']" ] && ok "findings ordered High first with stable IDs" || bad "ordering" "$N"
N="$(jq_ "$F" "[f['title'] for f in d['findings'] if f['category']=='different-design' or 'staging' in f['title']][:1]")"
grep -q "in staging" "$F" && ok "findings name the configured environments, not fixed labels" || bad "env labels in findings" "$N"

# locate against a tiny fake candidate repo + baseline repo (origin/main), brain off
mk_repo() {
  local r="$1"; shift
  git init -q "$r" && git -C "$r" checkout -q -b main
  while [ $# -gt 0 ]; do mkdir -p "$r/$(dirname "$1")"; printf '%s\n' "$2" > "$r/$1"; shift 2; done
  git -C "$r" add -A && git -C "$r" -c user.email=t@t -c user.name=t commit -q -m init
  git -C "$r" update-ref refs/remotes/origin/main HEAD
}
mk_repo "$T/app" "src/areas/projects/landing/ProjectsHeader.tsx" '<button className="new-project-btn">New project</button>' \
                 "src/areas/projects/landing/__tests__/ProjectsHeader.test.tsx" "expect(wrapper.find('.new-project-btn').text()).toBe('New project'); // new-project-btn new-project-btn" \
                 "src/areas/projects/landing/Other.tsx" '<div className="page-header"></div>'
mk_repo "$T/design" "components/projects/ProjectsHeader.tsx" '<button className="new-project-btn">New project</button>'
python3 "$S" locate --run "$T/run" --candidate-repo "$T/app" --baseline-repo "$T/design" --no-brain > "$T/loc.json"; R=$?
SRC="$(jq_ "$F" "[f.get('source',{}).get('candidate',[{}])[0].get('file') for f in d['findings'] if 'New project' in f['title']][0]")"
LSRC="$(jq_ "$F" "[f.get('source',{}).get('baseline',[{}])[0].get('file') for f in d['findings'] if 'New project' in f['title']][0]")"
[ "$R" = 0 ] && [ "$SRC" = "src/areas/projects/landing/ProjectsHeader.tsx" ] && [ "$LSRC" = "components/projects/ProjectsHeader.tsx" ] \
  && ok "locate: the element's own class names the candidate file and the baseline file (ref origin/<default_branch>)" || bad "locate" "rc=$R cand=$SRC base=$LSRC $(cat "$T/loc.json")"
N="$(jq_ "$F" "sum(1 for f in d['findings'] for x in (f.get('source') or {}).get('candidate', []) if '__tests__' in x['file'])")"
[ "$N" = 0 ] && ok "locate: test files are never offered as a likely source" || bad "locate tests" "$N"
python3 "$S" locate --run "$T/run" --no-brain > "$T/loc2.json" 2>&1; R=$?
[ "$R" = 1 ] && grep -q "no candidate repo" "$T/loc2.json" && ok "locate: no app.repo_dir and no --candidate-repo is a usage failure" || bad "locate no repo" "rc=$R"

python3 "$S" candidates --run "$T/run" > "$T/cand.json"
[ "$(jq_ "$T/cand.json" "[c['id'] for c in d['candidates']] and d['candidates'][0]['file']")" = "src/areas/projects/landing/ProjectsHeader.tsx" ] \
  && ok "candidates: a Medium style drift with one obvious file qualifies" || bad "candidates" "$(cat "$T/cand.json")"
python3 "$S" fixed --run "$T/run" --finding M-01 --pr 9001 --url https://x/9001 --branch fix/a --draft > /dev/null
python3 "$S" fixed --run "$T/run" --finding M-02 --pr 9002 --url https://x/9002 --branch fix/b --draft > /dev/null
python3 "$S" fixed --run "$T/run" --finding M-03 --pr 9003 --url https://x/9003 --branch fix/c --draft > /dev/null 2>&1; R=$?
[ "$R" = 3 ] && ok "fixed: a third fix PR is refused (cap 2)" || bad "fix cap" "rc=$R"

python3 "$S" report --run "$T/run" > "$T/rep.json"; R=$?
RP="$T/run/parity-report.md"
[ "$R" = 4 ] && [ "$(jq_ "$T/rep.json" "d['silent_skips']")" = "['delta (design)', 'epsilon']" ] \
  && ok "report: exits 4 and names each silent gap — a whole route (epsilon) AND one env of a route (delta design)" || bad "silent skip" "rc=$R $(cat "$T/rep.json")"
grep -q '| delta | not covered (failed) |' "$RP" && grep -q 'staging returned 502' "$RP" && ok "report: a route with no data reads 'not covered (failed)' with its reason, not 'partial'" || bad "not-covered row" "$(grep '| delta' "$RP")"
grep -q '| gamma | partial |' "$RP" && grep -q 'staging did not render' "$RP" && ok "report: a page the candidate did not render is not 'covered'" || bad "gamma row" "$(grep '| gamma' "$RP")"
[ "$(jq_ "$F" "sum(1 for f in d['findings'] if f['route']=='zeta')")" = 0 ] && grep -q 'the design page is a placeholder' "$RP" \
  && ok "a placeholder baseline page yields a coverage note, not a flood of 'extra in the candidate'" || bad "baseline placeholder" "$(jq_ "$F" "[f['title'][:40] for f in d['findings'] if f['route']=='zeta'][:3]")"
grep -q 'read-only guard was refusing' "$RP" && [ "$(jq_ "$F" "sum(1 for f in d['findings'] if 'notes' in f['title'])")" = 0 ] \
  && ok "a dialog blocked by the guard is a coverage note, never graded High" || bad "guard-blocked dialog"
grep -q 'staging trigger not found' "$RP" && ok "report: the unfound trigger is a coverage note on the route" || bad "trigger note"
grep -q '| beta | partial |' "$RP" && grep -q 'tab was hidden' "$RP" && ok "report: the hidden-tab route is partial, with the reason" || bad "hidden row"
grep -q '^## High — broken functionality or console errors (6)' "$RP" && grep -q '^## Medium' "$RP" && grep -q '^## Low' "$RP" \
  && ok "report: grouped High / Medium / Low" || bad "report groups" "$(grep '^## ' "$RP")"
grep -q -- '- \*\*Before (staging now):\*\*' "$RP" && grep -q -- '- \*\*After (match design):\*\*' "$RP" && grep -q 'Likely source:\*\* `src/areas/projects/landing/ProjectsHeader.tsx`' "$RP" \
  && ok "report: before / after / likely source on High and Medium items, labelled with the environment names" || bad "report fields"
grep -q '^# UI parity sweep — design vs staging' "$RP" && grep -q '| Route | Status | Matched | design only | staging only |' "$RP" \
  && ok "report: title and coverage table carry the configured environment names" || bad "report labels" "$(head -1 "$RP")"
grep -q '^- \*\*Also:\*\* dialog “share” opens in design but not in staging' "$RP" && ok "report: the folded dialog failure is shown on its trigger's finding" || bad "fold visible" "$(grep -n 'Also' "$RP")"
grep -q 'PR \[9001\](https://x/9001).*DRAFT' "$RP" && ok "report: draft fix PRs are listed and flagged as drafts" || bad "report fix PRs"
grep -q 'refused 1 write' "$RP" && grep -q 'PUT' "$RP" && ok "report: guard-refused writes are surfaced" || bad "report blocked writes"
grep -q 'Hydration mismatch' "$RP" && grep -q 'support chat widget ×1' "$RP" && ok "report: baseline-side errors and ignored noise are listed, ungraded" || bad "report info"

python3 "$S" mark --run "$T/run" --route epsilon --status skipped --reason "out of scope this run" > /dev/null
python3 "$S" mark --run "$T/run" --route delta --env baseline --status skipped --reason "staging was down; design not captured" > /dev/null
python3 "$S" report --run "$T/run" > "$T/rep2.json"; R=$?
[ "$R" = 0 ] && ok "report: once every route is accounted for, exit 0" || bad "report after mark" "rc=$R"

python3 "$S" fixed --run "$T/run" --skipped "no candidate this run" > /dev/null; R=$?
python3 "$S" report --run "$T/run" --regrade > "$T/rg.json"; R2=$?
[ "$R" = 0 ] && grep -q 'Fix phase: not opened — no candidate this run' "$T/run/parity-report.md" && ok "fixed --skipped without --finding records a run-level skip, shown in the report" || bad "run-level skip" "rc=$R"
[ "$R2" = 0 ] && grep -q '^Regraded ' "$T/run/parity-report.md" && ok "report --regrade: measures elapsed to the capture's last artifact and says so" || bad "regrade" "rc=$R2"
cp -R "$T/run" "$T/run-copy"; sleep 1; touch "$T/run-copy"/dumps/*/*/*.json
python3 "$S" report --run "$T/run-copy" --regrade > "$T/rg2.json"
E1="$(jq_ "$T/rg.json" "d['elapsed_s']")"; E2="$(jq_ "$T/rg2.json" "d['elapsed_s']")"
[ -n "$E1" ] && [ "$E1" = "$E2" ] && [ "$E1" -ge 0 ] && ok "report --regrade: a copied run (fresh mtimes) regrades to the same elapsed time" || bad "regrade on copy" "$(jq_ "$T/rg.json" "d['elapsed_s']") vs $(jq_ "$T/rg2.json" "d['elapsed_s']")"

# rehearsal: no PR may be recorded, writeback is forced dry, the report says so
python3 "$S" init --routes "$T/routes.json" --only alpha --run "$T/reh" --rehearsal > /dev/null
python3 "$S" fixed --run "$T/reh" --finding M-01 --pr 1 --url u --branch b > /dev/null 2>&1; R=$?
[ "$R" = 3 ] && ok "rehearsal: recording a PR is refused" || bad "rehearsal PR" "rc=$R"
mkdir -p "$T/reh/dumps" && cp -R "$T/run/dumps/alpha" "$T/reh/dumps/" && mkdir -p "$T/reh/console" && cp "$T/run/console/alpha."* "$T/reh/console/"
python3 "$S" diff --run "$T/reh" > /dev/null && python3 "$S" report --run "$T/reh" > /dev/null
python3 "$S" writeback --run "$T/reh" > "$T/wbr.json"; R=$?
[ "$R" = 0 ] && grep -q '"dry_run": true' "$T/wbr.json" && grep -q 'Rehearsal:' "$T/reh/parity-report.md" \
  && ok "rehearsal: writeback is forced to a dry run and the report is labelled a rehearsal" || bad "rehearsal writeback" "rc=$R"

# preflight: a port held by something that is not our collector stops the run (exit 5)
python3 -c "import socket,time; s=socket.socket(); s.bind(('127.0.0.1', 5198)); s.listen(); time.sleep(20)" &
HOLD=$!; sleep 1
python3 "$S" init --routes "$T/routes.json" --run "$T/pf" --port 5198 > /dev/null
LOOP_PARITY_SCREEN_LOCKED=0 python3 "$S" preflight --run "$T/pf" > "$T/pf.json"; R=$?
LOOP_PARITY_SCREEN_LOCKED=1 python3 "$S" preflight --run "$T/pf" > "$T/pf2.json"; R2=$?
kill "$HOLD" 2>/dev/null; wait "$HOLD" 2>/dev/null
[ "$R2" = 5 ] && grep -q '"screen-locked"' "$T/pf2.json" && ok "preflight: a locked screen stops the run (exit 5, screen-locked)" || bad "preflight lock" "rc=$R2"
[ "$R" = 5 ] && grep -q '"collector-blocked"' "$T/pf.json" && ok "preflight: a foreign process on the collector port stops the run (exit 5)" || bad "preflight port" "rc=$R $(cat "$T/pf.json")"
python3 - "$S" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("sweep", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
v = m.screen_locked()
print("screen_locked() ->", v)
sys.exit(0 if v in (True, False, None) else 1)
PY
[ $? = 0 ] && ok "preflight: screen_locked() answers True/False/None without raising" || bad "screen_locked"

python3 "$S" writeback --run "$T/run" --dry-run > "$T/wb.json"; R=$?
[ "$R" = 0 ] && [ "$(jq_ "$T/wb.json" "d['results'][0]['metric']['cmd'][:3]")" = "['metric', '--name', 'ui-parity-sweep']" ] \
  && [ "$(jq_ "$T/wb.json" "any('app_map' in r and r['app_map']['cmd'][:4]==['write','app_map','--kind','source-map'] for r in d['results'])")" = True ] \
  && ok "writeback --dry-run: a ui-parity-sweep metric + source-map app_map rows" || bad "writeback" "$(head -c 600 "$T/wb.json")"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
