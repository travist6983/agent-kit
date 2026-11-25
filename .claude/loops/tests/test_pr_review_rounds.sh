#!/usr/bin/env bash
# Regression: review convergence on the PR-review loop (dev-pr-feed). The seen-ledger counts
# review ROUNDS (consecutive reviewed head commits that did not pass); at
# pr_review_loop.max_review_rounds_per_pr a PR stops being queued and gets ONE step-back comment;
# a re-review is an interdiff; `rounds --emit` writes rounds-to-pass and cap-hit metrics once.
#
# OFFLINE BY CONSTRUCTION, the same fixture as test_pr_dispositions.sh: a throwaway git repo is
# the target app, the `git-local` vcs_host provider is the host (PRs and threads are files under
# a temp evidence root), and KB_CLI points at a local fake (pause flag + metric log). The real
# brain is never contacted and KB_ALLOW_UNSHARED is never set.
#
# Usage: bash .claude/loops/tests/test_pr_review_rounds.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
F="$ROOT/.claude/skills/dev-pr-feed/scripts/pr_feed.py"
CLI="$ROOT/lib/adapters/cli.py"
LIMITS="$ROOT/.claude/loops/loop-limits.json"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0
ok()  { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL  $1"; echo "        $2"; FAIL=$((FAIL+1)); }
jq_() { python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
print(eval(sys.argv[2], {'d': d}))" "$1" "$2" 2>/dev/null; }
led() { python3 -c "
import json,sys
d=json.load(open(sys.argv[1])).get(sys.argv[2]) or {}
print(eval(sys.argv[3], {'e': d, 'r': d.get('rounds') or {}}))" "$LOOP_PR_LEDGER_FILE" "$1" "$2" 2>/dev/null; }

# ── fixture ──────────────────────────────────────────────────────────────────────────────────
BASE="trunk"
APP="$TMP/app"; EVID="$TMP/runs"; PRS="$EVID/prs"
git init -q -b "$BASE" "$APP"
git -C "$APP" config user.email "owner@example.com"
git -C "$APP" config user.name "The Owner"
for d in a b c e; do
  mkdir -p "$APP/$d"
  python3 -c "print('\n'.join('$d%d' % i for i in range(1, 41)))" > "$APP/$d/f.js"
done
git -C "$APP" add -A && git -C "$APP" commit -qm "initial"
for d in a b c e; do
  git -C "$APP" checkout -qb "feature/$d" "$BASE"
  python3 - "$APP/$d/f.js" 5 <<'PY'
import sys
p, n = sys.argv[1], int(sys.argv[2]); L = open(p).read().split("\n"); L[n - 1] += " changed"; open(p, "w").write("\n".join(L))
PY
  git -C "$APP" commit -qam "feature $d"
done
git -C "$APP" checkout -q "$BASE"

# bump <branch-letter> <line>: one more commit on feature/<x> changing that line of <x>/f.js
bump() {
  git -C "$APP" checkout -q "feature/$1"
  python3 - "$APP/$1/f.js" "$2" <<'PY'
import sys
p, n = sys.argv[1], int(sys.argv[2]); L = open(p).read().split("\n"); L[n - 1] += " again"; open(p, "w").write("\n".join(L))
PY
  git -C "$APP" commit -qam "bump $1 $2"
  git -C "$APP" checkout -q "$BASE"
}
head_of() { git -C "$APP" rev-parse "feature/$1"; }

cat > "$TMP/stack.config.json" <<JSON
{
  "version": "1.0.0",
  "project": {"name": "example-app", "ticket_key_pattern": "^[A-Z][A-Z0-9]+-[0-9]+$", "default_branch": "$BASE"},
  "app": {"repo_dir": "$APP", "url": "http://localhost:3000"},
  "adapters": {"vcs_host": {"provider": "git-local", "config": {}}, "ci_cd": {"provider": "none", "config": {}}},
  "evidence": {"root": "$EVID"},
  "integrations": {"pr_review": {"loop_pr_label": "agent-loop"}}
}
JSON
export LOOPS_STACK_CONFIG="$TMP/stack.config.json"
export LOOP_PR_LEDGER_FILE="$TMP/ledger.json"
export LOOP_PR_WORKTREE_ROOT="$TMP/worktrees"
export LOOP_PR_AUTHOR_SCOPE=all
export LOOP_PR_LEDGER_BACKEND=file
unset LOOP_PR_INCLUDE_LOOP_PRS LOOP_PR_MAX_FILES LOOP_PR_MAX_THREADS LOOP_PR_POSTING LOOP_APP_ROOT LOOP_PR_TARGET KB_ALLOW_UNSHARED

cat > "$TMP/fake_kb.py" <<PY
import json, os, sys
a = sys.argv[1:]
if a[:2] == ["pause", "--status"]:
    print(json.dumps({"ok": True, "paused": os.environ.get("FAKE_KB_PAUSED") == "1"})); raise SystemExit(0)
with open("$TMP/kb_calls.jsonl", "a") as fh: fh.write(json.dumps(a) + "\n")
if a and a[0] == "metric":
    if os.environ.get("FAKE_KB_FAIL") == "1":
        print(json.dumps({"ok": False, "error": "fake brain down"})); raise SystemExit(2)
    with open("$TMP/metric_calls.jsonl", "a") as fh: fh.write(json.dumps(a) + "\n")
    print(json.dumps({"ok": True, "action": "metric"})); raise SystemExit(0)
raise SystemExit(2)
PY
export KB_CLI="$TMP/fake_kb.py"
: > "$TMP/metric_calls.jsonl"; : > "$TMP/kb_calls.jsonl"

for d in a b c e; do python3 "$CLI" vcs_host open_pr branch="feature/$d" title="Feature $d" >/dev/null; done
A="feature/a"; B="feature/b"; C="feature/c"; E="feature/e"
mark() { python3 "$F" mark --pr "$1" --commit "$2" --verdict "${3:-request-changes}" --findings-count "${4:-2}" --mode "${5:-dry-run}"; }
in_list() { python3 -c "
import json,sys
d=json.load(open(sys.argv[1])); print(any(str(r['pr_id'])==sys.argv[3] for r in d.get(sys.argv[2]) or []))" "$1" "$2" "$3" 2>/dev/null; }

echo "dev-pr-feed review-convergence tests"
echo

# --- honesty: the two numbers live in loop-limits.json with their rule strings ------------------
python3 -c "
import json; d=json.load(open('$LIMITS'))
assert d['review_convergence']['stagnation_fix_cycles'] == 2 and 'ARGUED' in d['review_convergence']['rule']
p=d['pr_review_loop']; assert p['max_review_rounds_per_pr'] == 3 and 'ARGUED' in p['max_review_rounds_per_pr_rule']" 2>/dev/null \
  && ok "loop-limits.json carries both numbers with honest rule strings" || bad "limits honesty" "missing number or rule"

# --- 1. rounds: one per head, a pass resets ----------------------------------------------------
mark "$A" "$(head_of a)" >/dev/null; mark "$A" "$(head_of a)" >/dev/null
[ "$(led "$A" "r['open'], r['total']")" = "(1, 1)" ] && ok "mark twice at one head counts one round" || bad "one round per head" "$(led "$A" "r")"
bump a 10; mark "$A" "$(head_of a)" >/dev/null
bump a 15; mark "$A" "$(head_of a)" > "$TMP/m3.json"
[ "$(jq_ "$TMP/m3.json" "d['round_cap_reached']")" = True ] && ok "mark reports round_cap_reached at the third non-passing head" || bad "round_cap_reached" "$(cat "$TMP/m3.json" | head -c 300)"
bump a 20
python3 "$F" unreviewed > "$TMP/u1.json" 2>&1
[ "$(in_list "$TMP/u1.json" round_capped "$A")" = True ] && [ "$(in_list "$TMP/u1.json" unreviewed "$A")" = False ] \
  && ok "the fourth push of a 3-round PR is round_capped, not unreviewed" || bad "cap" "$(head -c 400 "$TMP/u1.json")"
[ "$(jq_ "$TMP/u1.json" "d['max_review_rounds_per_pr']")" = 3 ] && ok "unreviewed reports the cap it applied" || bad "cap echo" ""
[ "$(jq_ "$TMP/u1.json" "[r['review_round'] for r in d['unreviewed'] if r['pr_id']=='$B']")" = "[1]" ] \
  && ok "a queued never-reviewed PR is review_round 1" || bad "review_round" "$(jq_ "$TMP/u1.json" "d['unreviewed']")"

mark "$B" "$(head_of b)" >/dev/null
bump b 10; mark "$B" "$(head_of b)" approve 0 >/dev/null
[ "$(led "$B" "r['open'], r['last_pass']['round'], r['last_pass']['counted']")" = "(0, 2, False)" ] \
  && ok "a pass resets open and records last_pass.round" || bad "pass" "$(led "$B" "r")"
mark "$B" "$(head_of b)" posted 0 live >/dev/null
[ "$(led "$B" "r['open'], r['last_pass']['round']")" = "(0, 2)" ] && ok "a second mark at the passing head changes nothing" || bad "pass idempotent" "$(led "$B" "r")"

# --- 2. limits without the key -> config-invalid ------------------------------------------------
python3 -c "
import json; d=json.load(open('$LIMITS')); d['pr_review_loop'].pop('max_review_rounds_per_pr')
json.dump(d, open('$TMP/limits-nokey.json','w'))"
python3 "$F" unreviewed --limits "$TMP/limits-nokey.json" > "$TMP/u2.json" 2>&1; CODE=$?
[ "$CODE" = 3 ] && [ "$(jq_ "$TMP/u2.json" "d['failure_class']")" = config-invalid ] \
  && ok "limits file missing max_review_rounds_per_pr -> exit 3 config-invalid" || bad "missing key" "exit=$CODE"

# --- 3. live post then mark keeps rounds and threads; C reaches the cap with a live thread --------
echo '{"findings":[{"severity":"high","file":"c/f.js","line":5,"title":"T5","summary":"s","bug_class":"null-or-undefined","instances":[{"file":"c/f.js","line":7}]}]}' > "$TMP/rev-c.json"
LOOP_PR_POSTING=live python3 "$F" post --pr "$C" --findings "$TMP/rev-c.json" --reviewed-commit "$(head_of c)" --live > "$TMP/pc.json" 2>&1
mark "$C" "$(head_of c)" request-changes 1 dry-run >/dev/null
[ "$(led "$C" "r['open'], len(e.get('threads') or {})")" = "(1, 1)" ] && ok "mark after a live post keeps rounds (one) and the thread" || bad "post+mark" "$(led "$C" "r, e.get('threads')")"
grep -q '\*\*Class:\*\* `null-or-undefined` - fix every instance' "$PRS/feature-c.threads.md" \
  && grep -q '\*\*Also at:\*\* `c/f.js:7`' "$PRS/feature-c.threads.md" \
  && ok "the posted comment carries the class line and the instances" || bad "class rendering" "$(head -20 "$PRS/feature-c.threads.md")"
[ "$(led "$C" "[t.get('bug_class') for t in e['threads'].values()]")" = "['null-or-undefined']" ] \
  && ok "the thread record keeps bug_class" || bad "thread bug_class" ""
bump c 10; mark "$C" "$(head_of c)" >/dev/null
bump c 15; mark "$C" "$(head_of c)" >/dev/null

# --- 4. ledger --reset-rounds re-queues A and keeps its state ----------------------------------
cp "$LOOP_PR_LEDGER_FILE" "$TMP/ledger-before-reset.json"
python3 "$F" ledger --reset-rounds "$A" > "$TMP/rr.json" 2>&1
python3 "$F" unreviewed > "$TMP/u3.json" 2>&1
[ "$(in_list "$TMP/u3.json" unreviewed "$A")" = True ] && [ "$(led "$A" "r['open'], r['total']")" = "(0, 3)" ] \
  && ok "ledger --reset-rounds re-queues the PR and keeps the round total" || bad "reset" "$(led "$A" "r")"
cp "$TMP/ledger-before-reset.json" "$LOOP_PR_LEDGER_FILE"     # put A back at the cap

# --- 5. step-back: dry-run, no-live-rounds, pause, posture, live once ---------------------------
SNAP_A="$(cat "$PRS/feature-a.threads.md" 2>/dev/null | shasum)"
python3 "$F" step-back --pr "$A" --out "$TMP/sb-a.json" > "$TMP/sb1.json" 2>&1
[ -f "$TMP/sb-a.json" ] && [ "$(jq_ "$TMP/sb1.json" "len(d['previewed'])")" = 1 ] \
  && [ "$(cat "$PRS/feature-a.threads.md" 2>/dev/null | shasum)" = "$SNAP_A" ] \
  && [ "$(jq_ "$TMP/sb-a.json" "(d['comments'][0]['path'], d['comments'][0]['line'])")" = "(None, None)" ] \
  && ok "step-back dry-run writes one PR-level preview and posts nothing" || bad "step-back dry-run" "$(head -c 400 "$TMP/sb1.json")"
python3 "$F" step-back --pr "$A" > "$TMP/sb2.json" 2>&1
[ "$(jq_ "$TMP/sb2.json" "d['skipped'][0]['reason']")" = already-previewed ] && ok "a second dry-run step-back is skipped already-previewed" || bad "previewed twice" "$(head -c 300 "$TMP/sb2.json")"
LOOP_PR_POSTING=live python3 "$F" step-back --pr "$A" --live > "$TMP/sb3.json" 2>&1
[ "$(jq_ "$TMP/sb3.json" "d['skipped'][0]['reason']")" = no-live-rounds ] && ok "a live step-back on a PR with no live-posted thread is refused (no-live-rounds)" || bad "no-live-rounds" "$(head -c 300 "$TMP/sb3.json")"
python3 "$F" step-back --pr "$B" > "$TMP/sb4.json" 2>&1
[ "$(jq_ "$TMP/sb4.json" "d['skipped'][0]['reason']")" = not-capped ] && ok "a PR under the cap is skipped not-capped" || bad "not-capped" ""

SNAP_C="$(shasum < "$PRS/feature-c.threads.md")"
FAKE_KB_PAUSED=1 LOOP_PR_POSTING=live python3 "$F" step-back --pr "$C" --live > "$TMP/sb5.json" 2>&1; CODE=$?
[ "$CODE" = 3 ] && [ "$(jq_ "$TMP/sb5.json" "d['failure_class']")" = paused ] && [ "$(shasum < "$PRS/feature-c.threads.md")" = "$SNAP_C" ] \
  && ok "R9 pause: live step-back exits 3 and the threads file is unchanged" || bad "paused" "exit=$CODE $(head -c 300 "$TMP/sb5.json")"
LOOP_PR_POSTING=dry-run python3 "$F" step-back --pr "$C" --live --out "$TMP/sb-c.json" > "$TMP/sb6.json" 2>&1
[ "$(jq_ "$TMP/sb6.json" "d['mode']")" = dry-run ] && [ "$(shasum < "$PRS/feature-c.threads.md")" = "$SNAP_C" ] \
  && ok "LOOP_PR_POSTING=dry-run downgrades --live to a preview" || bad "posture" "$(head -c 300 "$TMP/sb6.json")"
LOOP_PR_POSTING=live python3 "$F" step-back --due --live > "$TMP/sb7.json" 2>&1
N=$(grep -c 'dev-pr-feed` step-back' "$PRS/feature-c.threads.md")
[ "$(jq_ "$TMP/sb7.json" "[p['pr_id'] for p in d['posted']]")" = "['$C']" ] && [ "$N" = 1 ] \
  && ok "live step-back --due posts exactly one PR-level thread on the capped PR with live threads" || bad "live step-back" "n=$N $(head -c 400 "$TMP/sb7.json")"
LOOP_PR_POSTING=live python3 "$F" step-back --pr "$C" --live > "$TMP/sb8.json" 2>&1
[ "$(jq_ "$TMP/sb8.json" "d['skipped'][0]['reason']")" = already-posted ] && [ "$(grep -c 'dev-pr-feed` step-back' "$PRS/feature-c.threads.md")" = 1 ] \
  && ok "a second live step-back is skipped already-posted" || bad "posted twice" "$(head -c 300 "$TMP/sb8.json")"

# The step-back marker must never be read as a finding anchor.
python3 -c "
import importlib.util,sys
s=importlib.util.spec_from_file_location('pf', '$F'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
assert m.BOT_MARKER not in m.STEP_BACK_MARKER" 2>/dev/null && ok "STEP_BACK_MARKER does not contain BOT_MARKER" || bad "marker overlap" ""
echo '{"findings":[{"severity":"critical","title":"PR-level","summary":"no anchor"}]}' > "$TMP/rev-c-unanchored.json"
LOOP_PR_POSTING=live python3 "$F" post --pr "$C" --findings "$TMP/rev-c-unanchored.json" --reviewed-commit "$(head_of c)" --live > "$TMP/pc2.json" 2>&1
[ "$(jq_ "$TMP/pc2.json" "(d['thread_count'], d['duplicate_skipped'])")" = "(1, [])" ] \
  && ok "an unanchored finding still posts after a step-back (not swallowed as a duplicate)" || bad "unanchored after step-back" "$(head -c 400 "$TMP/pc2.json")"

# --- 6. rounds: read-only by default; --emit once, retried after a failure ----------------------
: > "$TMP/kb_calls.jsonl"
python3 "$F" rounds > "$TMP/r1.json" 2>&1
[ "$(wc -c < "$TMP/kb_calls.jsonl" | tr -d ' ')" = 0 ] && [ "$(jq_ "$TMP/r1.json" "sorted(d['capped'])")" = "['$A', '$C']" ] \
  && ok "rounds without --emit calls kb zero times and names the capped PRs" || bad "rounds read-only" "$(head -c 300 "$TMP/r1.json")"
FAKE_KB_FAIL=1 python3 "$F" rounds --emit > "$TMP/r2.json" 2>&1
[ "$(jq_ "$TMP/r2.json" "len(d['emit_failed']) >= 1 and not d['emitted']")" = True ] && [ "$(led "$B" "r['last_pass']['counted']")" = False ] \
  && ok "a failing kb leaves rounds-to-pass uncounted" || bad "emit failure" "$(head -c 300 "$TMP/r2.json")"
python3 "$F" rounds --emit > "$TMP/r3.json" 2>&1
R=$(python3 -c "
import json
rows=[json.loads(l) for l in open('$TMP/metric_calls.jsonl')]
def v(a,k): return a[a.index(k)+1]
print(sorted((v(a,'--name'), v(a,'--value'), v(a,'--unit')) for a in rows))")
[ "$R" = "[('pr_review_round_cap_hit', '1', 'prs'), ('pr_review_round_cap_hit', '1', 'prs'), ('pr_review_rounds_to_pass', '2', 'rounds')]" ] \
  && ok "rounds --emit writes rounds_to_pass once (retried) and one cap hit per capped PR" || bad "emit" "$R"
python3 "$F" rounds --emit > "$TMP/r4.json" 2>&1
[ "$(wc -l < "$TMP/metric_calls.jsonl" | tr -d ' ')" = 3 ] && ok "a second rounds --emit writes nothing" || bad "double count" "$(wc -l < "$TMP/metric_calls.jsonl")"

# --- 7. interdiff: checkout scope, post scope, scope verb, rewritten history -------------------
python3 "$F" checkout --pr "$E" > "$TMP/c1.json" 2>&1
[ "$(jq_ "$TMP/c1.json" "(d['review_scope'], d['interdiff_command'])")" = "('full', None)" ] && ok "checkout on a first review: review_scope full" || bad "first checkout" "$(head -c 400 "$TMP/c1.json")"
python3 "$F" teardown --pr "$E" >/dev/null 2>&1
E1="$(head_of e)"; mark "$E" "$E1" >/dev/null
bump e 20; E2="$(head_of e)"
python3 "$F" checkout --pr "$E" > "$TMP/c2.json" 2>&1
IC="$(jq_ "$TMP/c2.json" "d['interdiff_command']")"
[ "$(jq_ "$TMP/c2.json" "d['review_scope']")" = interdiff ] && echo "$IC" | grep -q "diff $E1 $E2" \
  && ok "checkout after a new commit: interdiff with a command naming both commits" || bad "interdiff checkout" "$(head -c 500 "$TMP/c2.json")"
python3 "$F" teardown --pr "$E" >/dev/null 2>&1
cat > "$TMP/rev-e.json" <<JSON
{"findings":[
 {"severity":"high","file":"e/f.js","line":20,"title":"in range","summary":"s"},
 {"severity":"medium","file":"e/f.js","line":30,"title":"out of range","summary":"s"},
 {"severity":"critical","file":"e/f.js","line":35,"title":"critical out of range","summary":"s"}]}
JSON
python3 "$F" post --pr "$E" --findings "$TMP/rev-e.json" --reviewed-commit "$E2" --out "$TMP/pe.preview.json" > "$TMP/pe.json" 2>&1
[ "$(jq_ "$TMP/pe.preview.json" "sorted(c['line'] for c in d['comments'])")" = "[20, 35]" ] \
  && [ "$(jq_ "$TMP/pe.json" "([o['line'] for o in d['out_of_scope']], d['review_scope'])")" = "([30], 'interdiff')" ] \
  && ok "post on a re-review withholds the out-of-range medium, keeps in-range and the critical" || bad "post scope" "$(head -c 500 "$TMP/pe.json")"
python3 "$F" scope --pr "$E" --findings "$TMP/rev-e.json" --out "$TMP/scoped-e.json" > "$TMP/se.json" 2>&1
[ "$(jq_ "$TMP/scoped-e.json" "[f['line'] for f in d['findings'] if f.get('withheld')]")" = "[30]" ] \
  && ok "the scope verb stamps the same set withheld" || bad "scope verb" "$(head -c 400 "$TMP/se.json")"
mark "$E" "$E2" >/dev/null
git -C "$APP" checkout -q "$E"; echo "// amended" >> "$APP/e/f.js"; git -C "$APP" commit -q --amend -am "bump e 20 (amended)"; git -C "$APP" checkout -q "$BASE"
python3 "$F" checkout --pr "$E" > "$TMP/c3.json" 2>&1
[ "$(jq_ "$TMP/c3.json" "(d['review_scope'], d.get('interdiff_unavailable'))")" = "('full', 'history-rewritten')" ] \
  && ok "after a rewritten history: review_scope full, history-rewritten" || bad "rewritten" "$(head -c 500 "$TMP/c3.json")"
python3 "$F" teardown --pr "$E" >/dev/null 2>&1

# --- 8. the cap never fails open: omitted --findings-count, capped-at-head, bad scope input ------
# C sits at the cap (open=3) at the head it was last reviewed on. A hand-typed recovery `mark`
# that leaves --findings-count off (the runner prints exactly such commands) must NOT be a pass.
CH="$(head_of c)"
python3 "$F" mark --pr "$C" --commit "$CH" --verdict request-changes --mode dry-run > "$TMP/m8a.json" 2>&1
python3 "$F" mark --pr "$C" --commit "$CH" --verdict reviewed --mode live > "$TMP/m8b.json" 2>&1
python3 "$F" mark --pr "$C" --commit "$CH" --verdict posted --mode live > "$TMP/m8c.json" 2>&1
[ "$(led "$C" "r['open'], 'last_pass' in r")" = "(3, False)" ] \
  && [ "$(jq_ "$TMP/m8a.json" "(d['round_cap_reached'], d['recorded']['findings'])")" = "(True, None)" ] \
  && [ "$(jq_ "$TMP/m8b.json" "d['round_cap_reached']")" = True ] \
  && ok "mark without --findings-count is never a pass (request-changes, reviewed, posted): the cap holds" \
  || bad "omitted findings-count" "$(led "$C" "r") $(head -c 300 "$TMP/m8a.json")"
python3 "$F" unreviewed > "$TMP/u8.json" 2>&1
[ "$(in_list "$TMP/u8.json" round_capped "$C")" = True ] && [ "$(in_list "$TMP/u8.json" already_reviewed "$C")" = False ] \
  && [ "$(jq_ "$TMP/u8.json" "[r.get('awaiting_push') for r in d['round_capped'] if r['pr_id']=='$C']")" = "[True]" ] \
  && ok "a PR capped at its already-reviewed head is listed round_capped (awaiting_push), not already_reviewed" \
  || bad "capped at head" "$(head -c 500 "$TMP/u8.json")"
python3 "$F" rounds --emit > "$TMP/r8.json" 2>&1
[ "$(wc -l < "$TMP/metric_calls.jsonl" | tr -d ' ')" = 3 ] \
  && ok "rounds --emit writes no rounds_to_pass for the pass that never happened" || bad "phantom pass" "$(tail -2 "$TMP/metric_calls.jsonl")"
bump c 25
python3 "$F" unreviewed > "$TMP/u9.json" 2>&1
[ "$(in_list "$TMP/u9.json" round_capped "$C")" = True ] && [ "$(in_list "$TMP/u9.json" unreviewed "$C")" = False ] \
  && ok "after the next push the PR is still round_capped, not re-queued" || bad "capped after push" "$(head -c 400 "$TMP/u9.json")"
# An explicit zero still passes, so the rule is 'explicit', not 'never'.
mark "$E" "$(head_of e)" reviewed 0 >/dev/null 2>&1
[ "$(led "$E" "r['open']")" = 0 ] && ok "an explicit --findings-count 0 is still a pass" || bad "explicit zero" "$(led "$E" "r")"

for kind in missing empty malformed; do
  case "$kind" in
    missing) IN="$TMP/no-such-review.json" ;;
    empty) IN="$TMP/empty-review.json"; : > "$IN" ;;
    malformed) IN="$TMP/bad-review.json"; echo '{"findings": [' > "$IN" ;;
  esac
  rm -f "$TMP/scoped-$kind.json"
  python3 "$F" scope --pr "$E" --findings "$IN" --out "$TMP/scoped-$kind.json" > "$TMP/sc-$kind.json" 2>&1; CODE=$?
  [ "$CODE" = 3 ] && [ "$(jq_ "$TMP/sc-$kind.json" "(d['status'], d['failure_class'])")" = "('failed', 'config-invalid')" ] \
    && [ ! -e "$TMP/scoped-$kind.json" ] \
    && ok "scope with a $kind --findings file: structured config-invalid, exit 3, nothing written" \
    || bad "scope $kind" "exit=$CODE $(head -c 300 "$TMP/sc-$kind.json")"
done

echo
echo "  pr_review_rounds: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
