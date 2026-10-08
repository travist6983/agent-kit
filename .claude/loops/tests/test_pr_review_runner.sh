#!/usr/bin/env bash
# Regression: pr_review.sh — the two classification properties an unattended hourly tick
# is judged on. Both of these were real bugs, and both failed in the SAME direction: a tick
# that was broken, or starved, reported itself as something benign.
#
#   1. WATCHDOG VERDICT SURVIVES CLEANUP.  run_watchdog sets a GLOBAL, WD_TIMED_OUT, and
#      resets it to 0 on entry. hygiene_sweep() runs between the model call and the timeout
#      branch and calls feed_run DIRECTLY (`lock --release`) — not in a subshell — so the
#      flag was cleared in the parent shell two lines before it was read. Since a healthy
#      tick always OWNS the tick lock it left behind, the release path always fires, and
#      exit 22 was effectively dead in production: a wedged 40-minute tick logged
#      session-exit-124 (or a false anthropic-rate-limited) with timed_out=false, and the
#      consecutive-stuck counter was never touched. The fix is a snapshot taken on the line
#      after the call; this test is what keeps the snapshot.
#
#   2. THE 429 TELL IS ANTHROPIC-SPECIFIC.  exit 29 means "the subscription is out of its
#      rolling window — not a fault, stuck counter untouched, come back next hour". The
#      secondary tell used to accept any provider: `("API Error" in s or "rate_limit" in
#      s.lower())`. The one non-Anthropic 429 this system actually produces is the OpenAI
#      embedder behind qa-kb — the only genuinely metered line item here, which has already
#      hit real credit exhaustion — and its body says `rate_limit_exceeded`. So a DEAD BRAIN
#      EMBEDDER was reported as a healthy rolling window.
#
# SLOW ON PURPOSE, once: --timeout has a hard 60s floor (0 would disable the watchdog), so
# the wedged case really does have to wait out a minute. The other three cases exit at once.
#
# SAFE BY CONSTRUCTION: fake claude, fake pr_feed.py, fake kb.py (--no-brain on top), every
# path redirected into a temp dir that is removed on exit. Never reaches the live brain, the
# VCS host, or a token. No `timeout`/`flock` — neither exists on this machine.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
PRR="$REPO/.claude/skills/dev-loop-pr-review/scripts/pr_review.sh"

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); else echo "  [FAIL] $1 — got '$2' want '$3'"; fail=$((fail+1)); fi; }
ckin(){ if grep -qF "$2" "$3" 2>/dev/null; then pass=$((pass+1)); else echo "  [FAIL] $1 — '$2' not in $(basename "$3")"; fail=$((fail+1)); fi; }
ckout(){ if grep -qF "$2" "$3" 2>/dev/null; then echo "  [FAIL] $1 — '$2' unexpectedly present"; fail=$((fail+1)); else pass=$((pass+1)); fi; }

if [ ! -f "$PRR" ]; then
  echo "  pr_review_runner: SKIPPED — $PRR not present"
  exit 0
fi

W="$(mktemp -d -t pr-review-runner)" || { echo "  pr_review_runner: SKIPPED (mktemp failed)"; exit 0; }
cleanup(){ rm -rf "$W"; }
trap cleanup EXIT
mkdir -p "$W/bin" "$W/worktrees" "$W/brain"

# ── fake pr_feed ─────────────────────────────────────────────────────────────
# The tick lock is FREE until the session starts and HELD afterwards, stamped with the
# moment the session took it. That is the ownership window hygiene_sweep tests for, so the
# sweep concludes the lock is OURS and takes the `lock --release` path — the one that used
# to clobber the watchdog verdict. A foreign lock early-returns and never reproduces it.
cat > "$W/bin/pr_feed.py" <<PY
import json, os, sys
a = sys.argv[1:]
took = os.environ.get("FAKE_LOCK_STAMP", "$W/lock_taken_at")
if a and a[0] == "lock":
    if "--release" in a:
        try: os.remove(took)
        except Exception: pass
        print(json.dumps({"ok": True, "released": True})); raise SystemExit(0)
    if os.path.exists(took):
        at = open(took).read().strip()
        print(json.dumps({"ok": True, "status": "held", "lock": {"acquired_at": at}}))
    else:
        print(json.dumps({"ok": True, "status": "free", "lock": {}}))
    raise SystemExit(0)
if a and a[0] == "unreviewed":
    # FAKE_QUEUE drives the queue gate. Unset keeps the old legacy shape (no status/count), which
    # the gate must not recognise — so cases 1-5 still reach the session they are testing.
    q = os.environ.get("FAKE_QUEUE", "")
    cur = [{"pr_id": 5586, "head_commit": "aa", "verdict": "approve"}]
    if q == "empty":
        print(json.dumps({"status": "ok", "backend": "file", "count": 0, "unreviewed": [],
                          "already_reviewed": cur, "loop_prs_skipped": [{"pr_id": 5594, "title": "t"}],
                          "too_large": []}, indent=2)); raise SystemExit(0)
    if q == "prs":
        print(json.dumps({"status": "ok", "backend": "file", "count": 1,
                          "unreviewed": [{"pr_id": 5600, "head_commit": "bb"}],
                          "already_reviewed": cur, "loop_prs_skipped": [], "too_large": []}, indent=2)); raise SystemExit(0)
    if q in ("auth", "unavailable"):
        fc = "auth-expired" if q == "auth" else "api-unavailable"
        print(json.dumps({"status": "failed", "failure_class": fc, "message": "fake " + fc}, indent=2))
        raise SystemExit(2)
    print(json.dumps({"ok": True, "candidates": []})); raise SystemExit(0)
if a and a[0] == "health":
    h = os.environ.get("FAKE_HEALTH", "attention")
    if h == "fail":
        print(json.dumps({"status": "failed", "failure_class": "api-unavailable", "message": "fake"})); raise SystemExit(2)
    print(json.dumps({"status": "ok", "author_scope": "all", "checked": 3,
                      "needs_attention": [{"pr_id": 5589, "title": "t", "author": "a", "loop_pr": True,
                                           "merge_status": "conflicts", "url": "https://x/5589",
                                           "reasons": ["merge conflicts with the target branch"]}],
                      "healthy": [5595], "ci_pending": [5586], "unknown": [], "unchecked": [],
                      "errors": [], "truncated": False, "budget_s": 240}, indent=2)); raise SystemExit(0)
print(json.dumps({"ok": True}))
PY

# ── fake kb: healthy brain, not paused ───────────────────────────────────────
cat > "$W/bin/kb.py" <<PY
import json, sys
a = sys.argv[1:]
with open("$W/kb_calls.log", "a") as fh: fh.write(" ".join(a) + "\\n")
if a and a[0] == "pause":   print(json.dumps({"ok": True, "paused": False})); raise SystemExit(0)
if a and a[0] == "receipt": print(json.dumps({"ok": True, "count": 1, "counts_by_store": {"metrics": 1}})); raise SystemExit(0)
print(json.dumps({"ok": True}))
PY

# ── fake claude binaries ─────────────────────────────────────────────────────
# (1) WEDGED: stamps the tick lock as taken (so the runner owns it), prints a well-formed
#     result object, then runs long past --timeout so the watchdog must kill it. The result
#     object is what made the old bug report a FALSE outcome instead of merely a wrong one.
cat > "$W/bin/claude-wedged" <<SH
#!/usr/bin/env bash
python3 -c "import datetime,sys; sys.stdout.write(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))" > "$W/lock_taken_at"
echo '{"type":"result","subtype":"success","is_error":false,"total_cost_usd":0.01,"result":"ok"}'
sleep 600
SH

# (2) OPENAI 429: the qa-kb embedder running out of credit, surfaced through the error
#     variant. Must NOT be read as the Anthropic subscription window.
cat > "$W/bin/claude-openai429" <<SH
#!/usr/bin/env bash
python3 -c "import datetime,sys; sys.stdout.write(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))" > "$W/lock_taken_at"
echo '{"type":"result","subtype":"error_during_execution","is_error":true,"errors":["kb write failed: openai 429 insufficient_quota rate_limit_exceeded"]}'
exit 1
SH

# (3) ANTHROPIC 429: the real thing — the structured, unforgeable field. Positive control,
#     so tightening the secondary tell cannot quietly disable the guard altogether.
cat > "$W/bin/claude-anthropic429" <<SH
#!/usr/bin/env bash
python3 -c "import datetime,sys; sys.stdout.write(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))" > "$W/lock_taken_at"
echo '{"type":"result","subtype":"success","is_error":true,"api_error_status":429,"terminal_reason":"api_error","total_cost_usd":0}'
exit 1
SH
# (4) MARKED, THEN WEDGED: the first-live-tick shape. The session reviews and MARKS a PR (a new
#     id@commit in the ledger), then hangs in its wrap-up until the watchdog kills it. The
#     kill must stay rc 22 / timed_out=true, but the outcome must not claim the work was
#     lost. It also keeps the prompt, so the wall-clock reserve can be checked.
cat > "$W/bin/claude-marked-then-wedged" <<SH
#!/usr/bin/env bash
printf '%s' "\$2" > "$W/prompt.txt"
python3 -c "import datetime,sys; sys.stdout.write(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))" > "$W/lock_taken_at"
python3 -c "import json; json.dump({'9001': {'pr_id': 9001, 'head_commit': 'abcdef0123456789', 'mode': 'dry-run'}}, open('$W/ledger.json', 'w'))"
echo '{"type":"result","subtype":"success","is_error":false,"total_cost_usd":0.01,"result":"ok"}'
sleep 600
SH
# (5) QUICK: a session that starts, says so on disk, and ends clean. The queue-gate cases
#     only need to know WHETHER a session started.
cat > "$W/bin/claude-quick" <<SH
#!/usr/bin/env bash
touch "$W/claude_ran"
echo '{"type":"result","subtype":"success","is_error":false,"total_cost_usd":0.01,"result":"ok"}'
SH
chmod +x "$W/bin/claude-wedged" "$W/bin/claude-openai429" "$W/bin/claude-anthropic429" "$W/bin/claude-marked-then-wedged" "$W/bin/claude-quick"

export LOOP_PR_FEED="$W/bin/pr_feed.py"
export KB_BIN="$W/bin/kb.py"
export LOOP_PR_REVIEW_LOCK_DIR="$W/pr-review.lock"
# pr_review.sh now takes a THIRD bolt — the shared cross-agent lock com.loops.qa-dream also
# takes (lib/agent_lock.sh). Redirect it for the same reason as the one above: left at its
# default this suite would contend with a REAL nightly dream or hourly tick, and the wedged
# case below would then be measuring a skip instead of a watchdog kill.
export LOOP_AGENT_LOCK_DIR="$W/agent.lock"
export LOOP_PR_REVIEW_STATE="$W/state.json"
export LOOP_PR_LEDGER_FILE="$W/ledger.json"
export LOOP_PR_WORKTREE_ROOT="$W/worktrees"
export LOOP_APP_ROOT="$REPO"          # only needs to BE a git checkout; nothing is fetched
echo '{}' > "$LOOP_PR_LEDGER_FILE"

# A key in the operator shell would trip the exit-28 guard before any of this runs.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN LOOP_PR_REVIEW_ALLOW_API_KEY

run_tick() {   # run_tick <tag> <fake-claude> [extra args...]
  local tag="$1" bin="$2"; shift 2
  rm -rf "$LOOP_PR_REVIEW_LOCK_DIR" "$W/lock_taken_at"
  LOOP_CLAUDE_BIN="$W/bin/$bin" bash "$PRR" --no-brain --no-assert --limit 1 \
    --timeout 60 --grace 2 --log "$W/$tag.log" --report "$W/$tag.md" "$@" \
    >"$W/$tag.out" 2>"$W/$tag.err"
  echo $?
}

# ══ 1. WATCHDOG KILL + A TICK LOCK THE RUNNER OWNS ═══════════════════════════
# The exact shape the old bug needed: the release path runs, so the global is reset between
# the call and the read. Must still be 22 / timed_out=true.
RC="$(run_tick wd claude-wedged)"
ck   "a wedged tick that OWNS the tick lock exits 22" "$RC" "22"
ckin "the end line says timed_out=true" "timed_out=true" "$W/wd.log"
ckin "the outcome is watchdog-kill" "outcome=watchdog-kill" "$W/wd.log"
ckout "it is NOT reported as a rate limit" "anthropic-rate-limited" "$W/wd.log"
ckout "it is NOT reported as a plain session exit" "outcome=session-exit" "$W/wd.log"
# Prove the clobbering path actually ran — otherwise this test passes for the wrong reason.
ckin "hygiene_sweep really did release the lock (the path that used to clobber)" \
     "lock_released=yes" "$W/wd.log"

# ══ 2. AN OPENAI 429 IS NOT THE ANTHROPIC WINDOW ═════════════════════════════
RC="$(run_tick oa claude-openai429)"
ckout "a qa-kb OpenAI 429 is NOT classified as an Anthropic rate limit" \
      "outcome=anthropic-rate-limited" "$W/oa.log"
ckout "…and does not log a rate_limited event" "event=rate_limited" "$W/oa.log"
ck    "…it is a plain non-zero session exit instead" "$RC" "1"

# ══ 3. A REAL ANTHROPIC 429 STILL FIRES ══════════════════════════════════════
RC="$(run_tick an claude-anthropic429)"
ck   "a structured api_error_status 429 exits 29" "$RC" "29"
ckin "the outcome is anthropic-rate-limited" "outcome=anthropic-rate-limited" "$W/an.log"
ckin "the tell is named in the log" "tell=api_error_status=429" "$W/an.log"
ckin "the stuck counter is explicitly untouched" "stuck_counter=untouched" "$W/an.log"

# ══ 4. THE METERED-BILLING GUARD STILL REFUSES ═══════════════════════════════
# Presence only — the value is never read. A dummy string is enough and is not a credential.
rm -rf "$LOOP_PR_REVIEW_LOCK_DIR"
ANTHROPIC_API_KEY="not-a-real-key" LOOP_CLAUDE_BIN="$W/bin/claude-wedged" \
  bash "$PRR" --no-brain --no-assert --limit 1 --timeout 60 --grace 2 \
  --log "$W/key.log" --report "$W/key.md" >"$W/key.out" 2>"$W/key.err"
ck    "an API key in the environment refuses the tick (exit 28)" "$?" "28"
ckout "no part of the key value is echoed" "not-a-real-key" "$W/key.err"
ckin  "the refusal names the variable, not the value" "ANTHROPIC_API_KEY" "$W/key.err"
# The refusal must not claim the caps bound nothing — they do bind under metered billing.
ckout "the refusal no longer claims nothing bounds dollars" \
      "nothing in this chain" "$W/key.err"

# ══ 5. A KILL AFTER THE WORK LANDED IS NOT REPORTED AS LOST WORK ═════════════
# A real first tick reviewed and marked its only PR, then was TERMed seconds
# later while rewriting its report, and logged the same bare outcome a wedged session gets. The
# ledger is the witness: a new id@commit means a review finished before the kill.
echo '{}' > "$LOOP_PR_LEDGER_FILE"
RC="$(run_tick mk claude-marked-then-wedged)"
ck   "a tick killed AFTER marking still exits 22 (the budget was exceeded)" "$RC" "22"
ckin "…and still says timed_out=true" "timed_out=true" "$W/mk.log"
ckin "…but the outcome says the review landed" "outcome=watchdog-kill-after-review" "$W/mk.log"
ckin "…and counts what was marked" "marked=1" "$W/mk.log"
ckin "…and says so on stderr" "the WORK LANDED" "$W/mk.err"
ckin "case 1 (nothing marked) logged marked=0" "marked=0" "$W/wd.log"
ckout "case 1 (nothing marked) is NOT called after-review" "watchdog-kill-after-review" "$W/wd.log"
# The wall-clock reserve reaches the session: a deadline and a reserve it can act on.
ckin "the prompt carries the tick deadline" "TICK_DEADLINE_EPOCH=" "$W/prompt.txt"
ckin "the prompt carries the report reserve" "REPORT_RESERVE_S=180" "$W/prompt.txt"
ckin "the prompt tells the session to write the report early" "EARLY AND REPEATEDLY" "$W/prompt.txt"
# The deadline must be a real epoch, i.e. the `date` in the prompt was NOT command-substituted
# at build time (an unescaped backtick there would run `date` inside the runner).
ckin "the clock hint is literal text, not a substituted command" 'compare with `date +%s`' "$W/prompt.txt"
DL="$(sed -n 's/^TICK_DEADLINE_EPOCH=\([0-9]*\).*/\1/p' "$W/prompt.txt" | head -1)"
NOW="$(date +%s)"
ck   "the deadline is within the last few minutes (start + --timeout 60)" \
     "$([ -n "$DL" ] && [ "$DL" -le "$NOW" ] && [ "$DL" -ge $((NOW - 300)) ] && echo yes || echo no)" "yes"
echo '{}' > "$LOOP_PR_LEDGER_FILE"

# ══ 6. THE QUEUE GATE: EMPTY QUEUE AND DEAD AUTH NEVER START A SESSION ═══════
# 218 of 369 real ticks were empty-queue and each paid a whole session to say so. The gate
# answers empty-queue and auth-expired itself; every other queue result reaches the session.
# Run WITH the (fake) brain, so the runner's own metric write is exercised too.
gate_tick() {   # gate_tick <tag> <FAKE_QUEUE> [extra args...]
  local tag="$1" q="$2"; shift 2
  rm -rf "$LOOP_PR_REVIEW_LOCK_DIR" "$W/claude_ran"; : > "$W/kb_calls.log"
  FAKE_QUEUE="$q" LOOP_CLAUDE_BIN="$W/bin/claude-quick" bash "$PRR" --limit 2 \
    --timeout 60 --grace 2 --log "$W/$tag.log" --report "$W/$tag.md" "$@" \
    >"$W/$tag.out" 2>"$W/$tag.err"
  echo $?
}
ran(){ [ -e "$W/claude_ran" ] && echo yes || echo no; }

RC="$(gate_tick ge empty)"
ck    "an empty queue exits 0" "$RC" "0"
ck    "…and never starts a session" "$(ran)" "no"
ckin  "…logs outcome=empty-queue" "outcome=empty-queue" "$W/ge.log"
ckin  "…says no model call was made" "model_call=false" "$W/ge.log"
ckin  "…records zero consumption, labelled" "spend_usd=0 spend_basis=no-model-call" "$W/ge.log"
ckin  "…writes the report the session would have" "PR sweep — empty-queue — 0 candidates" "$W/ge.md"
ckin  "…names the PRs already current" "5586 (approve)" "$W/ge.md"
ckin  "…writes the pr-review metric, tagged with the run id" "metric --name pr-review --value 0" "$W/kb_calls.log"
ckin  "…metric carries the run id" "run-id pr-review-" "$W/kb_calls.log"
ckin  "…and the end line says it landed" "metric_rows=1" "$W/ge.log"

# Auth-expired is answered too, and still feeds the stuck counter: the third in a row exits 24.
RC1="$(gate_tick ga1 auth)"; RC2="$(gate_tick ga2 auth)"; RC3="$(gate_tick ga3 auth)"
ck    "an expired VCS-host login exits 0 the first time" "$RC1" "0"
ck    "…and never starts a session" "$(ran)" "no"
ckin  "…logs outcome=auth-expired" "outcome=auth-expired" "$W/ga1.log"
ckin  "…report says skipped (auth-expired)" "skipped (auth-expired)" "$W/ga1.md"
ck    "…the second in a row still exits 0" "$RC2" "0"
ck    "…the third in a row exits 24 (STUCK)" "$RC3" "24"
ckin  "…and logs the stuck event" "event=stuck reason=outcome outcome=auth-expired consecutive=3" "$W/ga3.log"

# An empty queue clears that streak, exactly as a session-reported one does.
gate_tick gc empty >/dev/null
ck    "an empty queue clears the stuck counter" \
      "$(python3 -c "import json; print(json.load(open('$LOOP_PR_REVIEW_STATE')).get('stuck_count'))")" "0"

# Anything else goes to the session: PRs waiting, a board outage, and --no-gate.
RC="$(gate_tick gp prs)"
ck    "a queue with PRs in it starts the session" "$(ran)" "yes"
ckin  "…and says why" "handing the tick to the session (queue)" "$W/gp.err"
ckout "…with no gated end line" "model_call=false" "$W/gp.log"
RC="$(gate_tick gu unavailable)"
ck    "an api-unavailable queue read still starts the session (a broken gate costs a call, never a review)" "$(ran)" "yes"
ckin  "…and names the failure class" "failure:api-unavailable" "$W/gu.err"
RC="$(gate_tick gn empty --no-gate)"
ck    "--no-gate starts a session even on an empty queue" "$(ran)" "yes"

# ══ 7. --health: AN EMPTY TICK SWEEPS PR HEALTH, REPORT-ONLY, NEVER CHANGING THE OUTCOME ═══
RC="$(gate_tick gh empty --health)"
ck    "an empty tick with --health still exits 0" "$RC" "0"
ck    "…still starts no session" "$(ran)" "no"
ckin  "…outcome is still empty-queue" "outcome=empty-queue" "$W/gh.log"
ckin  "…the report gains the health section" "PR health — 3 checked · 1 needs attention" "$W/gh.md"
ckin  "…naming the PR and why" "NEEDS ATTENTION 5589 (loop PR): merge conflicts" "$W/gh.md"
ckin  "…and saying loop PRs are report-only" "never rebases or pushes" "$W/gh.md"
ckin  "…the pr-health metric carries the count" "metric --name pr-health --value 1" "$W/kb_calls.log"
ckin  "…and the end line says so" "health=on needs_attention=1" "$W/gh.log"
ckin  "the report still opens with the sweep line" "PR sweep — empty-queue" "$W/gh.md"

RC="$(FAKE_HEALTH=fail gate_tick gf empty --health)"
ck    "a failed health sweep does not change the exit code" "$RC" "0"
ckin  "…nor the outcome" "outcome=empty-queue" "$W/gf.log"
ckin  "…and says in words it was not checked" "PR health — not checked (api-unavailable)" "$W/gf.md"
ckout "…with no pr-health metric" "pr-health" "$W/kb_calls.log"
ckin  "…logged as unmeasured, not zero" "needs_attention=unmeasured" "$W/gf.log"

RC="$(gate_tick gx empty)"
ckin  "without --health the sweep does not run" "health=off" "$W/gx.log"
ckout "…and the report has no health section" "PR health" "$W/gx.md"

RC="$(gate_tick gy prs --health)"
ck    "--health never replaces a review: a queue with PRs still starts the session" "$(ran)" "yes"
ckout "…and the sweep does not run on that tick" "health=on" "$W/gy.log"

# ══ NO RESIDUE ═══════════════════════════════════════════════════════════════
ck "this test left no runner lock in the repo" \
   "$([ -e "$REPO/.claude/loops/pr-review.lock" ] && echo yes || echo no)" "no"
ck "this test never touched the shared cross-agent lock" \
   "$([ -e "$REPO/.claude/loops/agent.lock" ] && echo yes || echo no)" "no"

echo "  pr_review_runner: $pass passed, $fail failed"
[ "$fail" = 0 ]
