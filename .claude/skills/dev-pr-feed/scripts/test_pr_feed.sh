#!/usr/bin/env bash
# Deterministic behaviour tests for dev-pr-feed.
#
# These cover the guarantees that, if they silently broke, would look like success while the loop
# either did nothing or spammed real pull requests. They run OFFLINE: a throwaway git repo stands
# in for the target app, the `git-local` vcs_host provider stands in for the host (its PRs and
# review threads are files under a temp evidence root), and a throwaway ledger + worktree root
# keep real state out of reach. No network, no credentials, no real pull request.
#
# Usage: bash .claude/skills/dev-pr-feed/scripts/test_pr_feed.sh

# NOTE ON tests/lib/brain_lock.sh: this suite deliberately does NOT take the shared brain lock.
# That lock guards suites that WRITE to the shared brain. This suite never touches the brain —
# the only brain contact would be the R9 pause read, and the tests point KB_CLI either at a
# nonexistent path (fail closed) or at a local fake that answers paused:false. Safe to run
# alongside the brain suites; if it ever gains a brain write, take the lock.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
F="$HERE/pr_feed.py"
CLI="$ROOT/lib/adapters/cli.py"
TMP="$(mktemp -d)"
PASS=0; FAIL=0

ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; echo "        $2"; FAIL=$((FAIL+1)); }
# top-level key lookup; prints the empty string when absent or unparseable
jget() { python3 -c "
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: sys.exit(0)
v=d.get(sys.argv[2])
print('' if v is None else v)" "$1" "$2" 2>/dev/null; }

# ── fixture: a target repo, a stack.config pointing at it, two PRs ──────────────────────────
BASE="trunk"                       # deliberately not "main": nothing may hard-code the branch
APP="$TMP/app"; EVID="$TMP/runs"
git init -q -b "$BASE" "$APP"
git -C "$APP" config user.email "owner@example.com"
git -C "$APP" config user.name "The Owner"
mkdir -p "$APP/a"
printf 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n' > "$APP/a/b.js"; printf 'x\n' > "$APP/a/c.js"
git -C "$APP" add -A && git -C "$APP" commit -qm "initial"
git -C "$APP" checkout -qb feature/task-filter
printf 'l1\nl2\nl3\nl4\nl5\nl6\nL7 changed\nl8\n' > "$APP/a/b.js"; printf 'y\n' > "$APP/a/c.js"
git -C "$APP" commit -qam "task filter"
git -C "$APP" checkout -qb feature/loop-opened "$BASE"
printf 'z\n' > "$APP/a/c.js"; git -C "$APP" commit -qam "loop PR"
git -C "$APP" checkout -q "$BASE"

cat > "$TMP/stack.config.json" <<JSON
{
  "version": "1.0.0",
  "project": {"name": "example-app", "ticket_key_pattern": "^[A-Z][A-Z0-9]+-[0-9]+$", "default_branch": "$BASE"},
  "app": {"repo_dir": "$APP", "url": "http://localhost:3000"},
  "adapters": {
    "vcs_host": {"provider": "git-local", "config": {}},
    "ci_cd": {"provider": "none", "config": {}}
  },
  "evidence": {"root": "$EVID"},
  "integrations": {"pr_review": {"loop_pr_label": "agent-loop"}}
}
JSON
export LOOPS_STACK_CONFIG="$TMP/stack.config.json"
export LOOP_PR_LEDGER_FILE="$TMP/ledger.json"
export LOOP_PR_WORKTREE_ROOT="$TMP/worktrees"
export LOOP_PR_AUTHOR_SCOPE=all
unset LOOP_PR_INCLUDE_LOOP_PRS LOOP_PR_MAX_FILES LOOP_PR_MAX_THREADS LOOP_PR_POSTING LOOP_APP_ROOT LOOP_PR_TARGET

python3 "$CLI" vcs_host open_pr branch=feature/task-filter title="Add task filter to project board" >/dev/null
python3 "$CLI" vcs_host open_pr branch=feature/loop-opened title="loop PR" labels='["agent-loop"]' >/dev/null
PR="feature/task-filter"; LOOPPR="feature/loop-opened"
SHA="$(git -C "$APP" rev-parse "$PR")"

echo "dev-pr-feed behaviour tests"
echo "target repo: $APP   ledger: $LOOP_PR_LEDGER_FILE"
echo

# --- 1. list returns candidates and every one matches the declared filter -------------------
python3 "$F" list > "$TMP/list.json" 2>&1
if [ "$(jget "$TMP/list.json" status)" = "ok" ]; then
  BAD=$(python3 -c "
import json;d=json.load(open('$TMP/list.json'))
print(sum(1 for r in d['pull_requests'] if r['target_branch']!='$BASE' or not r['head_commit']))")
  N=$(jget "$TMP/list.json" count)
  [ "$BAD" = "0" ] && [ "$N" = "2" ] && ok "list: both PRs target $BASE and carry a head commit" \
                   || bad "list: filter leaked" "$BAD rows violate the filter, count=$N: $(head -c 400 "$TMP/list.json")"
  HEAD_SEEN=$(python3 -c "
import json;d=json.load(open('$TMP/list.json'))
print([r['head_commit'] for r in d['pull_requests'] if r['pr_id']=='$PR'][0])")
  [ "$HEAD_SEEN" = "$SHA" ] && ok "list: head_commit is the branch tip git reports" \
                           || bad "list: wrong head_commit" "$HEAD_SEEN != $SHA"
  LOOPFLAG=$(python3 -c "
import json;d=json.load(open('$TMP/list.json'))
print([r['loop_pr'] for r in d['pull_requests'] if r['pr_id']=='$LOOPPR'][0])")
  [ "$LOOPFLAG" = "True" ] && ok "list: the labelled PR is flagged loop_pr" || bad "list: loop_pr flag missing" "$LOOPFLAG"
else
  bad "list: returned ok" "$(head -5 "$TMP/list.json")"
fi

# --- 1b. an absent vcs_host provider is a structured blocked, not a crash --------------------
LOOPS_CFG__ADAPTERS__VCS_HOST__PROVIDER=none python3 "$F" unreviewed > "$TMP/none.json" 2>/dev/null; CODE=$?
[ "$CODE" = "3" ] && [ "$(jget "$TMP/none.json" status)" = "blocked" ] \
  && [ "$(jget "$TMP/none.json" reason)" = "adapter-unavailable:vcs_host" ] \
  && ok "adapter none: blocked, reason adapter-unavailable:vcs_host, exit 3" \
  || bad "adapter none: not a structured blocked" "exit=$CODE $(head -c 300 "$TMP/none.json")"

# --- 2. an unmarked PR is unreviewed; a marked one is not -----------------------------------
python3 "$F" unreviewed > "$TMP/u1.json" 2>&1
IN=$(python3 -c "
import json;d=json.load(open('$TMP/u1.json'));print(any(r['pr_id']=='$PR' for r in d['unreviewed']))")
[ "$IN" = "True" ] && ok "unreviewed: an unmarked PR is queued" || bad "unreviewed: unmarked PR missing" "$(head -c 300 "$TMP/u1.json")"
LS=$(python3 -c "
import json;d=json.load(open('$TMP/u1.json'));print([r['pr_id'] for r in d['loop_prs_skipped']])")
[ "$LS" = "['$LOOPPR']" ] && ok "unreviewed: the loop-labelled PR is skipped and reported" || bad "unreviewed: loop PR not skipped" "$LS"

python3 "$F" mark --pr "$PR" --commit "$SHA" --verdict approve --findings-count 0 >/dev/null 2>&1
python3 "$F" unreviewed > "$TMP/u2.json" 2>&1
IN=$(python3 -c "
import json;d=json.load(open('$TMP/u2.json'));print(any(r['pr_id']=='$PR' for r in d['unreviewed']))")
[ "$IN" = "False" ] && ok "unreviewed: a marked PR is skipped" || bad "unreviewed: marked PR still queued" "$IN"

# --- 3. THE important one: a new commit re-queues the PR ------------------------------------
# If this breaks, the loop goes permanently silent on exactly the revisions it asked for.
python3 "$F" mark --pr "$PR" --commit "0000000000000000000000000000000000000000" --verdict approve --force >/dev/null 2>&1
python3 "$F" unreviewed > "$TMP/u3.json" 2>&1
REASON=$(python3 -c "
import json;d=json.load(open('$TMP/u3.json'))
r=[x for x in d['unreviewed'] if x['pr_id']=='$PR'];print(r[0]['reason'] if r else 'ABSENT')")
[ "$REASON" = "new-commits-since-review" ] \
  && ok "re-review: a new head commit re-queues the PR" \
  || bad "re-review: PR did NOT come back after a new commit" "reason=$REASON"

# --- 3a. the same, through a REAL push: the author commits again --------------------------
python3 "$F" mark --pr "$PR" --commit "$SHA" --verdict approve >/dev/null 2>&1
git -C "$APP" checkout -q "$PR" && printf 'w\n' >> "$APP/a/c.js" && git -C "$APP" commit -qam "address review" && git -C "$APP" checkout -q "$BASE"
SHA2="$(git -C "$APP" rev-parse "$PR")"
python3 "$F" unreviewed > "$TMP/u3a.json" 2>&1
REASON=$(python3 -c "
import json;d=json.load(open('$TMP/u3a.json'))
r=[x for x in d['unreviewed'] if x['pr_id']=='$PR'];print(r[0]['reason'] if r else 'ABSENT')")
[ "$REASON" = "new-commits-since-review" ] && ok "re-review: an actual new commit on the branch re-queues the PR" \
                                           || bad "re-review: real push not noticed" "reason=$REASON"
SHA="$SHA2"

# --- 3b. an abbreviated sha in the LEDGER must still count as reviewed ----------------------
python3 "$F" mark --pr "$PR" --commit "${SHA:0:10}" --verdict approve >/dev/null 2>&1
python3 "$F" unreviewed > "$TMP/u3b.json" 2>&1
IN=$(python3 -c "
import json;d=json.load(open('$TMP/u3b.json'));print(any(r['pr_id']=='$PR' for r in d['unreviewed']))")
[ "$IN" = "False" ] \
  && ok "abbreviated sha: a short sha in the ledger still counts as reviewed" \
  || bad "abbreviated sha: ledger re-queued a PR that was reviewed" "queued=$IN"

# --- 3c. mark refuses a sha that isn't the PR's head (a typo would cause endless re-review) --
python3 "$F" mark --pr "$PR" --commit "1234567890abcdef1234567890abcdef12345678" --verdict approve > "$TMP/m1.json" 2>&1
CLASS=$(jget "$TMP/m1.json" failure_class)
[ "$CLASS" = "stale-iteration" ] \
  && ok "mark: refuses a sha that doesn't match the PR head" \
  || bad "mark: accepted a bogus sha" "class=$CLASS"

# --- 4. a corrupt ledger fails CLOSED (never 'review everything') ---------------------------
echo '{not valid json' > "$LOOP_PR_LEDGER_FILE"
python3 "$F" unreviewed > "$TMP/u4.json" 2>&1; CODE=$?
CLASS=$(jget "$TMP/u4.json" failure_class)
[ "$CODE" = "3" ] && [ "$CLASS" = "ledger-unreadable" ] \
  && ok "fail-closed: a corrupt ledger refuses (exit 3, ledger-unreadable)" \
  || bad "fail-closed: corrupt ledger did not refuse" "exit=$CODE class=$CLASS"
rm -f "$LOOP_PR_LEDGER_FILE"

# --- 5. dry-run posts nothing and previews the exact payload --------------------------------
cat > "$TMP/review.json" <<'JSON'
{"verdict":"request-changes","findings":[
  {"severity":"critical","title":"T","file":"a/b.js","line":7,"summary":"s",
   "failure_scenario":"f","recommendation":"r"}]}
JSON
python3 "$F" post --pr "$PR" --findings "$TMP/review.json" --out "$TMP/preview.json" > "$TMP/p1.json" 2>&1
MODE=$(jget "$TMP/p1.json" mode)
ANCHOR=$(python3 -c "
import json;d=json.load(open('$TMP/preview.json'))
c=d['comments'][0]
print(c.get('path')=='a/b.js' and c.get('line')==7 and 'dev-pr-feed' in c.get('body','')
      and d.get('head_commit')=='$SHA' and d['target']['op']=='post_review_comment')" 2>/dev/null)
THREADS=$(python3 "$CLI" vcs_host list_review_threads "$PR" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['result']))")
[ "$MODE" = "dry-run" ] && [ "$ANCHOR" = "True" ] && [ "$THREADS" = "0" ] \
  && ok "dry-run: previews a file:line payload at the head commit, posts nothing" \
  || bad "dry-run: preview wrong" "mode=$MODE anchored=$ANCHOR threads_on_pr=$THREADS"

# --- 5b. a withheld finding is recorded but NEVER becomes a comment ------------------------
cat > "$TMP/review-withheld.json" <<'JSON'
{"verdict":"approve","findings":[
  {"severity":"high","title":"Real","file":"a/b.js","line":7,"summary":"s","failure_scenario":"f","recommendation":"r"},
  {"severity":"medium","title":"Author already answered this","file":"a/c.js","line":9,"summary":"s",
   "failure_scenario":"f","recommendation":"r","withheld":true,
   "withheld_reason":"the author's comment above the line already accepts this tradeoff"}]}
JSON
python3 "$F" post --pr "$PR" --findings "$TMP/review-withheld.json" --out "$TMP/pw.json" > "$TMP/pw-res.json" 2>&1
TC=$(jget "$TMP/pw-res.json" thread_count); WC=$(jget "$TMP/pw-res.json" withheld_count)
ANY=$(python3 -c "
import json;d=json.load(open('$TMP/pw.json'))
print(any('a/c.js' in json.dumps(t) for t in d['comments']))" 2>/dev/null)
[ "$TC" = "1" ] && [ "$WC" = "1" ] && [ "$ANY" = "False" ] \
  && ok "withheld: kept in the report, never turned into a comment" \
  || bad "withheld: wrong handling" "threads=$TC withheld=$WC leaked=$ANY"

# --- 5c. the duplicate guard recognises our own comments by marker, via the adapter --------
MARKER=$(python3 -c "import sys; sys.path.insert(0,'$HERE'); import pr_feed; print(pr_feed.BOT_MARKER)")
python3 "$CLI" vcs_host post_review_comment --json "{\"id\": \"$PR\", \"path\": \"a/b.js\", \"line\": 7, \"body\": \"**HIGH: x**\n\n_${MARKER} - reply._\"}" >/dev/null
python3 "$CLI" vcs_host post_review_comment --json "{\"id\": \"$PR\", \"path\": \"a/b.js\", \"line\": 9, \"body\": \"a human said something\"}" >/dev/null
cat > "$TMP/review-dup.json" <<'JSON'
{"verdict":"request-changes","findings":[
  {"severity":"high","title":"same again","file":"a/b.js","line":7,"summary":"s"},
  {"severity":"low","title":"human's line","file":"a/b.js","line":9,"summary":"s"}]}
JSON
python3 "$F" post --pr "$PR" --findings "$TMP/review-dup.json" --out "$TMP/pd.json" > "$TMP/pd-res.json" 2>&1
DUPQ=$(python3 -c "
import json;d=json.load(open('$TMP/pd-res.json'))
print(d.get('thread_count'), [(x['file'],x['line']) for x in d.get('duplicate_skipped',[])])")
[ "$DUPQ" = "1 [('a/b.js', 7)]" ] \
  && ok "duplicate guard: skips the anchor we already commented on, keeps the one a human wrote" \
  || bad "duplicate guard: marker matching wrong" "got=$DUPQ"

# --- 6. the stale-iteration guard abandons rather than mis-anchor ---------------------------
python3 "$F" post --pr "$PR" --findings "$TMP/review.json" \
  --reviewed-commit 0000000000000000000000000000000000000000 --out "$TMP/p2.json" > "$TMP/p3.json" 2>&1
CODE=$?; CLASS=$(jget "$TMP/p3.json" failure_class)
[ "$CLASS" = "stale-iteration" ] && [ "$CODE" = "2" ] \
  && ok "stale-iteration: a moved PR abandons the post" \
  || bad "stale-iteration: guard did not fire" "exit=$CODE class=$CLASS"

# --- 6b. an ABBREVIATED sha must NOT read as stale (it would silently skip live PRs) --------
python3 "$F" post --pr "$PR" --findings "$TMP/review.json" \
  --reviewed-commit "${SHA:0:8}" --out "$TMP/p2b.json" > "$TMP/p3b.json" 2>&1
CLASS=$(jget "$TMP/p3b.json" failure_class); MODE=$(jget "$TMP/p3b.json" mode)
[ -z "$CLASS" ] && [ "$MODE" = "dry-run" ] \
  && ok "abbreviated sha: an 8-char head commit is not mistaken for staleness" \
  || bad "abbreviated sha: false stale-iteration" "class=$CLASS mode=$MODE"

# --- 6c. a closed PR is dropped, never commented on -----------------------------------------
python3 "$F" post --pr "no-such-pr" --findings "$TMP/review.json" --out "$TMP/p6c.json" > "$TMP/p6c-res.json" 2>&1
[ "$(jget "$TMP/p6c-res.json" failure_class)" = "pr-closed" ] \
  && ok "pr-closed: a PR that is no longer an open candidate drops the findings" \
  || bad "pr-closed: not classified" "$(head -c 300 "$TMP/p6c-res.json")"

# --- 7. the R9 gate refuses a live post when the flag can't be read -------------------------
# NOTE: this deliberately points KB_CLI at a path that does not exist, so the pause check
# fails closed and the live branch is provably unreachable. Never let this test depend on the
# real pause flag being ON — the day someone turns it off, the suite would post real comments
# to a real pull request.
KB_CLI="$TMP/definitely-not-here.py" \
  python3 "$F" post --pr "$PR" --findings "$TMP/review.json" --live > "$TMP/p4.json" 2>&1
CODE=$?; CLASS=$(jget "$TMP/p4.json" failure_class)
[ "$CLASS" = "paused" ] && [ "$CODE" = "3" ] \
  && ok "R9: a live post is refused when the pause flag is unreadable (fail closed)" \
  || bad "R9: an unreadable pause flag did NOT block the live post" "exit=$CODE class=$CLASS"

# --- 7a. the live path, against the git-local provider with a fake brain saying paused:false --
cat > "$TMP/fake_kb.py" <<'PY'
import json, sys
if sys.argv[1:3] == ["pause", "--status"]:
    print(json.dumps({"paused": False, "action": "status"})); raise SystemExit(0)
raise SystemExit(2)
PY
cat > "$TMP/review-live.json" <<'JSON'
{"verdict":"request-changes","findings":[
  {"severity":"high","title":"Null workspace id","file":"a/c.js","line":1,"summary":"s","failure_scenario":"f","recommendation":"r"}]}
JSON
LOOPSHA="$(git -C "$APP" rev-parse "$LOOPPR")"
KB_CLI="$TMP/fake_kb.py" LOOP_PR_POSTING=live \
  python3 "$F" post --pr "$LOOPPR" --findings "$TMP/review-live.json" --reviewed-commit "$LOOPSHA" --live > "$TMP/live1.json" 2>&1
CODE=$?
LIVEQ=$(python3 -c "
import json;d=json.load(open('$TMP/live1.json'))
e=d.get('ledger_entry') or {}
print(d.get('status'), d.get('mode'), d.get('thread_count'), d.get('ledger_marked'), e.get('verdict'), e.get('mode'), e.get('write_ahead'))" 2>/dev/null)
ONPR=$(python3 "$CLI" vcs_host list_review_threads "$LOOPPR" | python3 -c "
import json,sys; t=json.load(sys.stdin)['result']; print(len(t), t[0]['path'] if t else None, t[0]['line'] if t else None)")
[ "$CODE" = "0" ] && [ "$LIVEQ" = "ok live 1 True posted live True" ] && [ "$ONPR" = "1 a/c.js 1" ] \
  && ok "live: posts through vcs_host.post_review_comment and records the PR write-ahead at its head" \
  || bad "live: post path wrong" "exit=$CODE $LIVEQ on_pr=$ONPR $(head -c 300 "$TMP/live1.json")"
KB_CLI="$TMP/fake_kb.py" LOOP_PR_POSTING=live \
  python3 "$F" post --pr "$LOOPPR" --findings "$TMP/review-live.json" --live > "$TMP/live2.json" 2>&1
CODE=$?
[ "$CODE" = "3" ] && [ "$(jget "$TMP/live2.json" failure_class)" = "already-posted" ] \
  && ok "live: a second live post at the same head is refused (already-posted, exit 3)" \
  || bad "live: duplicate live post not refused" "exit=$CODE $(head -c 300 "$TMP/live2.json")"
KB_CLI="$TMP/fake_kb.py" LOOP_PR_POSTING=dry-run \
  python3 "$F" post --pr "$PR" --findings "$TMP/review.json" --live --out "$TMP/armed.json" > "$TMP/live3.json" 2>&1
[ "$(jget "$TMP/live3.json" mode)" = "dry-run" ] && [ -n "$(jget "$TMP/live3.json" runner_posture)" ] \
  && ok "posture: a process armed LOOP_PR_POSTING=dry-run turns --live into a preview and says so" \
  || bad "posture: --live was not downgraded" "$(head -c 300 "$TMP/live3.json")"

# --- 7b. the R9 pause check is HARD-bounded, even when a grandchild holds the pipes ---------
# Regression for a real hang: the brain CLI shelled out to an auth helper, the grandchild
# inherited the captured pipes, and a nominal timeout took several times longer to return. A
# kill switch that slow is not a gate.
BOUND=$(python3 -c "
import sys, time, subprocess; sys.path.insert(0,'$HERE')
import pr_feed
cmd = ['/bin/sh','-c','(sleep 30 &) ; sleep 30']
t=time.time(); r = pr_feed.run_bounded(cmd, 3); el = time.time()-t
print('OK' if (r is None and el < 8) else f'BAD r={r} el={el:.1f}')" 2>/dev/null)
[ "$BOUND" = "OK" ] \
  && ok "R9 bound: a hung pause check is killed by process group, not waited on" \
  || bad "R9 bound: the timeout did not hold" "got=$BOUND"

# --- 7c. the tick lock OUTLIVES one process, and a crashed tick can't block forever ---------
python3 "$F" lock --release >/dev/null 2>&1
python3 "$F" lock > "$TMP/lk1.json" 2>&1; A=$(jget "$TMP/lk1.json" status)
python3 "$F" lock > "$TMP/lk2.json" 2>&1; RC=$?; B=$(jget "$TMP/lk2.json" status)
[ "$A" = "ok" ] && [ "$B" = "busy" ] && [ "$RC" = "4" ] \
  && ok "tick lock: a second tick in a SEPARATE process is refused (exit 4, busy)" \
  || bad "tick lock: overlap not prevented across processes" "first=$A second=$B rc=$RC"

python3 "$F" lock --release >/dev/null 2>&1
python3 "$F" lock --ttl 1 >/dev/null 2>&1
sleep 2
python3 "$F" lock > "$TMP/lk3.json" 2>&1
STOLE=$(python3 -c "
import json;d=json.load(open('$TMP/lk3.json'));print(d.get('lock',{}).get('stole_stale_lock'))" 2>/dev/null)
[ "$STOLE" = "True" ] \
  && ok "tick lock: a stale lock from a crashed tick is reclaimed, not honoured forever" \
  || bad "tick lock: stale lock blocked the loop" "stole=$STOLE"
python3 "$F" lock --release >/dev/null 2>&1

# --- 8. checkout isolates, teardown is idempotent -------------------------------------------
BEFORE_BRANCH=$(git -C "$APP" branch --show-current 2>/dev/null)
BEFORE_DIRTY=$(git -C "$APP" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
python3 "$F" checkout --pr "$PR" > "$TMP/c1.json" 2>&1
WT=$(jget "$TMP/c1.json" worktree)
if [ "$(jget "$TMP/c1.json" status)" = "ok" ] && [ -d "$WT" ]; then
  AFTER_BRANCH=$(git -C "$APP" branch --show-current 2>/dev/null)
  AFTER_DIRTY=$(git -C "$APP" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  [ "$BEFORE_BRANCH" = "$AFTER_BRANCH" ] && [ "$BEFORE_DIRTY" = "$AFTER_DIRTY" ] \
    && ok "checkout: worktree created, developer's checkout untouched" \
    || bad "checkout: MUTATED the developer's checkout" "$BEFORE_BRANCH/$BEFORE_DIRTY -> $AFTER_BRANCH/$AFTER_DIRTY"
  [ "$(git -C "$WT" rev-parse HEAD)" = "$SHA" ] && case "$WT" in "$LOOP_PR_WORKTREE_ROOT"/*) true;; *) false;; esac \
    && ok "checkout: the worktree sits at the PR head, under the configured worktree root" \
    || bad "checkout: wrong commit or location" "$(git -C "$WT" rev-parse HEAD) at $WT"
  DB=$(jget "$TMP/c1.json" diff_base)
  [ "$DB" = "$(git -C "$APP" merge-base "$BASE" "$PR")" ] && ok "checkout: diff_base is the merge base with $BASE" \
                                                          || bad "checkout: diff_base wrong" "$DB"
  python3 "$F" checkout --pr "$PR" > "$TMP/c1b.json" 2>&1
  [ "$(jget "$TMP/c1b.json" reused)" = "True" ] && ok "checkout: a second call reuses the worktree at the same head" \
                                                 || bad "checkout: not idempotent" "$(head -c 200 "$TMP/c1b.json")"
  python3 "$F" teardown --pr "$PR" >/dev/null 2>&1
  python3 "$F" teardown --pr "$PR" > "$TMP/t2.json" 2>&1
  [ "$(jget "$TMP/t2.json" status)" = "ok" ] && [ ! -d "$WT" ] \
    && ok "teardown: idempotent, worktree gone" \
    || bad "teardown: not idempotent or worktree remains" "$(head -3 "$TMP/t2.json")"
  git -C "$APP" show-ref --verify --quiet "refs/loops/pr-$(python3 -c "import sys; sys.path.insert(0,'$HERE'); import pr_feed; print(pr_feed.slug('$PR'))")" \
    && bad "teardown: the local review ref was left behind" "refs/loops/pr-*" \
    || ok "teardown: the local review ref is deleted"
else
  bad "checkout: failed" "$(head -5 "$TMP/c1.json")"
fi

# --- 8b. no target repo configured is config-invalid, not a traceback ----------------------
LOOP_APP_ROOT="$TMP/not-a-repo" python3 "$F" checkout --pr "$PR" > "$TMP/c2.json" 2>/dev/null; CODE=$?
[ "$(jget "$TMP/c2.json" failure_class)" = "checkout-failed" ] && [ "$CODE" = "2" ] \
  && ok "checkout: a non-repo LOOP_APP_ROOT is checkout-failed JSON" \
  || bad "checkout: bad repo root not classified" "exit=$CODE $(head -c 200 "$TMP/c2.json")"

# --- loop PRs stay out of the review QUEUE unless told otherwise — offline, no host read ------
LOOPQ=$(python3 - "$F" <<'PY' 2>&1
import importlib.util, io, json, os, sys, contextlib
os.environ.pop("LOOP_PR_INCLUDE_LOOP_PRS", None)
def load():
    spec = importlib.util.spec_from_file_location("pr_feed", sys.argv[1]); m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m); return m
m = load()
rows = [{"pr_id": 1, "title": "loop PR", "head_commit": "a", "loop_pr": True},
        {"pr_id": 2, "title": "human PR", "head_commit": "b", "loop_pr": False}]
m.candidates = lambda: [dict(r) for r in rows]
m.ledger_get = lambda pr_id: None
def run(mod):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try: mod.cmd_unreviewed(type("A", (), {"limit": 0})())
        except SystemExit: pass
    return json.loads(buf.getvalue())
d = run(m)
labelled = m.is_loop_pr(["20.1.0", "Agent-Loop"]) and m.is_loop_pr({"labels": [{"name": "AGENT-LOOP"}]})
unlabelled = m.is_loop_pr(None) or m.is_loop_pr(["20.1.0"])
os.environ["LOOP_PR_INCLUDE_LOOP_PRS"] = "1"
m2 = load(); m2.candidates = m.candidates; m2.ledger_get = m.ledger_get
d2 = run(m2)
print(json.dumps({"queued": [r["pr_id"] for r in d["unreviewed"]], "loop_skipped": [r["pr_id"] for r in d["loop_prs_skipped"]],
                  "labelled": labelled, "unlabelled": unlabelled, "queued_with_override": [r["pr_id"] for r in d2["unreviewed"]]}))
PY
)
echo "$LOOPQ" | grep -q '"queued": \[2\], "loop_skipped": \[1\]' \
  && ok "unreviewed: a loop-labelled PR is skipped and reported, a human PR is queued" \
  || bad "unreviewed: loop PR not skipped" "$LOOPQ"
echo "$LOOPQ" | grep -q '"labelled": true, "unlabelled": false' \
  && ok "is_loop_pr: matches the configured label case-insensitively among others, not an unlabelled PR" \
  || bad "is_loop_pr: wrong label match" "$LOOPQ"
echo "$LOOPQ" | grep -q '"queued_with_override": \[1, 2\]' \
  && ok "LOOP_PR_INCLUDE_LOOP_PRS=1 puts loop PRs back in the queue" \
  || bad "include-loop override ignored" "$LOOPQ"

# --- traceback-free failures: the queue gate in the runner can only classify JSON -----------
BADLEDGER=$(python3 - "$F" <<'PY' 2>&1
import importlib.util, io, json, sys, contextlib
spec = importlib.util.spec_from_file_location("pr_feed", sys.argv[1]); m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.candidates = lambda: [{"pr_id": 101, "title": "t", "head_commit": "a", "loop_pr": False}]
m.ledger_get = lambda pr_id: "x"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    try:
        m.cmd_unreviewed(type("A", (), {"limit": 3})())
    except SystemExit:
        pass
print(json.loads(buf.getvalue()).get("failure_class"))
PY
)
[ "$BADLEDGER" = "ledger-unreadable" ] \
  && ok "unreviewed: a non-object ledger entry fails closed as ledger-unreadable" \
  || bad "unreviewed: malformed ledger entry not classified" "$BADLEDGER"

# --- falsy ledgers, unknown backend / scope: refuse, never widen -----------------------------
for LJ in '[]' 'null' '0' 'false'; do
  echo "$LJ" > "$TMP/falsy-ledger.json"
  LOOP_PR_INCLUDE_LOOP_PRS=1 LOOP_PR_LEDGER_FILE="$TMP/falsy-ledger.json" \
    python3 "$F" unreviewed > "$TMP/falsy.json" 2>/dev/null
  [ "$(jget "$TMP/falsy.json" failure_class)" = "ledger-unreadable" ] \
    && ok "unreviewed: a top-level ledger of $LJ fails closed" \
    || bad "unreviewed: ledger $LJ not refused" "$(head -3 "$TMP/falsy.json")"
done
LOOP_PR_LEDGER_BACKEND=KB python3 "$F" unreviewed > "$TMP/badbk.json" 2>/dev/null
[ "$(jget "$TMP/badbk.json" failure_class)" = "config-invalid" ] \
  && ok "an unknown ledger backend is config-invalid, not the kb path" \
  || bad "unknown backend not refused" "$(head -3 "$TMP/badbk.json")"
for SC in 'OWN' 'own ' ''; do
  LOOP_PR_AUTHOR_SCOPE="$SC" python3 "$F" unreviewed > "$TMP/badsc.json" 2>/dev/null
  [ "$(jget "$TMP/badsc.json" failure_class)" = "config-invalid" ] \
    && ok "author scope '$SC' is config-invalid, not scope all" \
    || bad "author scope '$SC' not refused" "$(head -3 "$TMP/badsc.json")"
done

# --- author scope: own / others resolve against the configured identity --------------------
LOOP_PR_AUTHOR_SCOPE=own LOOP_PR_IDENTITY="the owner" python3 "$F" list > "$TMP/own.json" 2>/dev/null
LOOP_PR_AUTHOR_SCOPE=others LOOP_PR_IDENTITY="the owner" python3 "$F" list > "$TMP/others.json" 2>/dev/null
[ "$(jget "$TMP/own.json" count)" = "2" ] && [ "$(jget "$TMP/others.json" count)" = "0" ] \
  && ok "author scope: own keeps the identity's PRs, others drops them (case-insensitive)" \
  || bad "author scope: identity filter wrong" "own=$(jget "$TMP/own.json" count) others=$(jget "$TMP/others.json" count)"

# --- size gate: a real oversized PR is too_large; an unmeasurable one fails OPEN -------------
LOOP_PR_MAX_FILES=1 python3 "$F" unreviewed > "$TMP/size.json" 2>/dev/null
SIZEQ=$(python3 -c "
import json;d=json.load(open('$TMP/size.json'))
print([ (t['pr_id'], t['changed_files_at_least']) for t in d['too_large']], [r['pr_id'] for r in d['unreviewed']])")
[ "$SIZEQ" = "[('$PR', 2)] []" ] \
  && ok "size gate: a PR over LOOP_PR_MAX_FILES is listed too_large, not queued, not marked" \
  || bad "size gate: real measurement wrong" "$SIZEQ"
SIZEQ=$(python3 - "$F" <<'PY' 2>&1
import importlib.util, io, json, os, sys, contextlib
os.environ["LOOP_PR_MAX_FILES"] = "50"
spec = importlib.util.spec_from_file_location("pr_feed", sys.argv[1]); m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.candidates = lambda: [{"pr_id": 7, "title": "t", "head_commit": "a", "loop_pr": False}]
m.ledger_get = lambda pr_id: None
def boom(*a, **k):
    raise TypeError("list indices must be integers")
m.changed_file_count = boom
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    try:
        m.cmd_unreviewed(type("A", (), {"limit": 3})())
    except SystemExit:
        pass
o = json.loads(buf.getvalue())
print(o.get("status"), o.get("count"), "size_unknown" in (o.get("unreviewed") or [{}])[0])
PY
)
[ "$SIZEQ" = "ok 1 True" ] \
  && ok "size gate: an unexpected diff response fails OPEN (queued, size_unknown), not rc 1" \
  || bad "size gate crashed on an unexpected response" "$SIZEQ"

# --- health: conflicts and red builds are flagged; budget and errors are not lost ------------
HEALTHQ=$(python3 - "$F" <<'PY' 2>&1
import importlib.util, io, json, sys, contextlib
spec = importlib.util.spec_from_file_location("pr_feed", sys.argv[1]); m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.candidates = lambda: [
    {"pr_id": 1, "merge_status": "conflicts", "loop_pr": True},
    {"pr_id": 2, "merge_status": "mergeable", "loop_pr": False},
    {"pr_id": 3, "merge_status": "mergeable", "loop_pr": False},
    {"pr_id": 4, "merge_status": "mergeable", "loop_pr": False},
    {"pr_id": 5, "merge_status": "mergeable", "loop_pr": False}]
def checks(row):
    pr_id = row["pr_id"]
    if pr_id == 2: return [{"name": "ci-build", "status": "failed", "blocking": True, "build_id": 9}]
    if pr_id == 3: return [{"name": "ci-build", "status": "pending", "blocking": True, "build_id": 8}]
    if pr_id == 4: raise RuntimeError("auth-expired: boom")
    return [{"name": "ci-build", "status": "passed", "blocking": True, "build_id": 7}]
m.build_checks = checks
def run(budget):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try: m.cmd_health(type("A", (), {"budget_s": budget})())
        except SystemExit: pass
    return json.loads(buf.getvalue())
o = run(240)
print(json.dumps({"attention": sorted(x["pr_id"] for x in o["needs_attention"]), "pending": o["ci_pending"],
                  "healthy": o["healthy"], "unknown": o["unknown"], "errors": [e["pr_id"] for e in o["errors"]],
                  "loop_flag": [x["loop_pr"] for x in o["needs_attention"] if x["pr_id"] == 1]}))
PY
)
echo "$HEALTHQ" | grep -q '"attention": \[1, 2\]' \
  && ok "health: a conflicted PR and a red build are needs-attention" \
  || bad "health: attention set wrong" "$HEALTHQ"
echo "$HEALTHQ" | grep -q '"pending": \[3\], "healthy": \[5\], "unknown": \[4\], "errors": \[4\], "loop_flag": \[true\]' \
  && ok "health: pending CI is pending, a per-PR read error is recorded as unknown (not dropped, not a crash)" \
  || bad "health: pending/healthy/error handling wrong" "$HEALTHQ"
CIQ=$(python3 - "$F" <<'PY' 2>&1
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("pr_feed", sys.argv[1]); m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
from lib.adapters import BuildStatus
class FakeCi:
    def build_status(self, ref):
        return {"a": BuildStatus(ref=ref, id="77", status="completed", result="failed"),
                "b": BuildStatus(ref=ref, id="78", status="running", result=None),
                "c": BuildStatus(ref=ref, id="79", status="completed", result="succeeded")}[ref]
m.ci_adapter = lambda: FakeCi()
print(json.dumps([m.build_checks({"head_commit": h})[0]["status"] for h in ("a", "b", "c")]))
PY
)
[ "$CIQ" = '["failed", "pending", "passed"]' ] \
  && ok "health: ci_cd.build_status results normalize to failed / pending / passed" \
  || bad "health: BuildStatus normalization wrong" "$CIQ"
python3 "$F" health --budget-s 30 > "$TMP/h.json" 2>/dev/null
[ "$(jget "$TMP/h.json" status)" = "ok" ] && [ "$(jget "$TMP/h.json" ci)" = "adapter-unavailable:ci_cd" ] \
  && [ "$(jget "$TMP/h.json" checked)" = "2" ] \
  && ok "health: with ci_cd none the sweep still runs, names the absent adapter once, calls nothing red" \
  || bad "health: ci_cd none handled wrong" "$(head -c 300 "$TMP/h.json")"
MS=$(python3 -c "
import json;d=json.load(open('$TMP/h.json'))
print(sorted(d['healthy']+d['unknown']+[a['pr_id'] for a in d['needs_attention']]))")
[ "$MS" = "['$LOOPPR', '$PR']" ] && ok "health: every in-scope PR is accounted for" || bad "health: PRs dropped" "$MS"

rm -rf "$TMP"
echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
