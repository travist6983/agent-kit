#!/usr/bin/env bash
# Offline tests for scripts/pending-prs.sh.
#
#   bash scripts/test_pending_prs.sh
#
# Part 1 drives the wrapper with a STUB queue reader (a fake pr_feed.py that records its argv +
# environment and replays a fixture), so the wrapper's own behaviour — scope resolution, the
# output formats, failure pass-through, usage errors — is tested without any board at all.
#
# Part 2 runs the wrapper END TO END over the real pr_feed.py and the real `git-local` vcs_host
# provider, against a throwaway stack.config (LOOPS_STACK_CONFIG) whose PR board is a fixture
# directory and whose seen-ledger is a temp file. Nothing reaches a VCS host, the brain, or the
# tracked config. Needs bash + python3 + git.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PP="$HERE/pending-prs.sh"
FEED="$REPO/.claude/skills/dev-pr-feed/scripts/pr_feed.py"
T="$(mktemp -d "${TMPDIR:-/tmp}/test-pending-prs.XXXXXX")"
trap 'rm -rf "$T"' EXIT
PASS=0; FAIL=0
ok()  { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL  $1"; [ -n "${2:-}" ] && echo "        $2"; FAIL=$((FAIL+1)); }

# ── Part 1: the stub queue reader ────────────────────────────────────────────
cat > "$T/stub_feed.py" <<'PY'
import os, sys
with open(os.environ["STUB_ARGS"], "w") as fh:
    fh.write("\n".join(sys.argv[1:]) + "\n")
    fh.write("LOOP_PR_AUTHOR_SCOPE=%s\n" % os.environ.get("LOOP_PR_AUTHOR_SCOPE", "<unset>"))
    fh.write("LOOP_PR_INCLUDE_LOOP_PRS=%s\n" % os.environ.get("LOOP_PR_INCLUDE_LOOP_PRS", "<unset>"))
sys.stdout.write(open(os.environ["STUB_OUT"]).read())
sys.exit(int(os.environ.get("STUB_RC", "0")))
PY
cat > "$T/q-empty.json" <<'J'
{"status":"ok","backend":"file","count":0,"unreviewed":[],"already_reviewed":[{"pr_id":"9","verdict":"approve"}],"loop_prs_skipped":[],"too_large":[]}
J
cat > "$T/q-two.json" <<'J'
{"status":"ok","backend":"file","count":2,"unreviewed":[{"pr_id":"12","reason":"never-reviewed"},{"pr_id":"7","reason":"new-commits-since-review"}],"already_reviewed":[],"loop_prs_skipped":[],"too_large":[]}
J
echo '{"status":"failed","failure_class":"ledger-unreadable","message":"seen-ledger unreadable"}' > "$T/q-ledger.json"
echo '{"status":"blocked","failure_class":"config-invalid","reason":"adapter-unavailable:vcs_host"}' > "$T/q-blocked.json"
printf 'Traceback (most recent call last):\n  boom\n' > "$T/q-garbage.json"
printf '{"pr_review_loop":{"prs_per_tick":2,"author_scope":"others"}}' > "$T/limits-others.json"
printf '{"pr_review_loop":{"prs_per_tick":99,"author_scope":"all"}}' > "$T/limits-bad.json"

# stub <fixture> <rc> -- <wrapper args…>   (extra env via the STUB_ENV array)
STUB_ENV=()
stub() {
  local q="$1" rc="$2"; shift 3
  rm -f "$T/args"
  env -u LOOP_PR_AUTHOR_SCOPE -u LOOP_PR_INCLUDE_LOOP_PRS \
      LOOP_PR_FEED="$T/stub_feed.py" LOOP_LOOP_LIMITS="$T/limits-others.json" \
      STUB_ARGS="$T/args" STUB_OUT="$T/$q" STUB_RC="$rc" ${STUB_ENV[@]+"${STUB_ENV[@]}"} \
      bash "$PP" "$@" > "$T/out" 2> "$T/err"
}
argline() { tr '\n' '|' < "$T/args"; }
jget() { python3 -c 'import json,sys; o=json.load(open(sys.argv[1])); print(eval(sys.argv[2], {"o": o}))' "$T/out" "$1" 2>/dev/null; }

echo "Part 1 — pending-prs.sh over a stub queue reader"
stub q-empty.json 0 --; R=$?
[ "$R" = 0 ] && [ "$(jget 'o["count"]')" = 0 ] && [ "$(jget 'o["filter"]["source"]')" = "scripts/pending-prs.sh" ] \
  && ok "empty queue: exit 0, JSON carries the filter block" || bad "empty queue" "rc=$R $(cat "$T/out")"
[ "$(argline)" = "unreviewed|LOOP_PR_AUTHOR_SCOPE=others|LOOP_PR_INCLUDE_LOOP_PRS=<unset>|" ] \
  && ok "bare call: scope from loop-limits.json, no --limit, loop PRs not included" || bad "bare call argv" "$(argline)"
[ "$(jget 'o["filter"]["author_scope_source"]')" = "loop-limits.json" ] && ok "filter names loop-limits.json as the scope source" || bad "scope source" "$(jget 'o["filter"]')"

stub q-two.json 0 -- --scope all --limit 3 --format ids; R=$?
[ "$R" = 0 ] && [ "$(tr '\n' ' ' < "$T/out")" = "12 7 " ] && ok "--format ids: one id per line, pr_feed's order kept" || bad "--format ids" "rc=$R $(cat "$T/out")"
[ "$(argline)" = "unreviewed|--limit|3|LOOP_PR_AUTHOR_SCOPE=all|LOOP_PR_INCLUDE_LOOP_PRS=<unset>|" ] \
  && ok "--scope and --limit are handed to pr_feed (scope via LOOP_PR_AUTHOR_SCOPE)" || bad "flag pass-through" "$(argline)"
stub q-two.json 0 -- --scope=own --format=count; R=$?
[ "$R" = 0 ] && [ "$(cat "$T/out")" = "2" ] && grep -q '^LOOP_PR_AUTHOR_SCOPE=own$' "$T/args" && ok "--format count and --scope= forms" || bad "--format count" "rc=$R $(cat "$T/out")"
stub q-empty.json 0 -- --limit 0 --format ids; R=$?
[ "$R" = 0 ] && [ ! -s "$T/out" ] && ! grep -q -- '--limit' "$T/args" && ok "--limit 0 is omitted; ids on an empty queue prints nothing" || bad "--limit 0" "rc=$R $(argline)"
stub q-empty.json 0 -- --include-loop-prs; R=$?
grep -q '^LOOP_PR_INCLUDE_LOOP_PRS=1$' "$T/args" && [ "$(jget 'o["filter"]["loop_prs"]')" = "included" ] \
  && ok "--include-loop-prs sets LOOP_PR_INCLUDE_LOOP_PRS=1 for pr_feed and says so in filter" || bad "--include-loop-prs" "$(argline)"

# Scope resolution: flag > env > loop-limits.json > own.
STUB_ENV=(LOOP_PR_AUTHOR_SCOPE=own); stub q-empty.json 0 -- --scope all; S_FLAG="$(jget 'o["filter"]["author_scope"]')"
STUB_ENV=(LOOP_PR_AUTHOR_SCOPE=own); stub q-empty.json 0 --; S_ENV="$(jget 'o["filter"]["author_scope"]') $(jget 'o["filter"]["author_scope_source"]')"
STUB_ENV=(); stub q-empty.json 0 --; S_FILE="$(jget 'o["filter"]["author_scope"]')"
STUB_ENV=(LOOP_LOOP_LIMITS="$T/limits-bad.json"); stub q-empty.json 0 --; S_BAD="$(jget 'o["filter"]["author_scope"]')"
STUB_ENV=(LOOP_LOOP_LIMITS="$T/no-such-file.json"); stub q-empty.json 0 --; S_NONE="$(jget 'o["filter"]["author_scope"]')"
STUB_ENV=()
[ "$S_FLAG" = all ] && [ "$S_ENV" = "own env LOOP_PR_AUTHOR_SCOPE" ] && [ "$S_FILE" = others ] && [ "$S_BAD" = own ] && [ "$S_NONE" = own ] \
  && ok "scope: flag=all > env=own > file=others; invalid or missing file -> own" \
  || bad "scope resolution" "flag='$S_FLAG' env='$S_ENV' file='$S_FILE' bad='$S_BAD' none='$S_NONE'"
# A set-but-empty env scope is passed to pr_feed as-is (it refuses it), never widened here.
STUB_ENV=(LOOP_PR_AUTHOR_SCOPE=); stub q-empty.json 0 --; STUB_ENV=()
grep -q '^LOOP_PR_AUTHOR_SCOPE=$' "$T/args" && ok "set-but-empty LOOP_PR_AUTHOR_SCOPE is handed to pr_feed untouched" || bad "empty env scope" "$(argline)"

# Failures pass through: pr_feed's JSON, pr_feed's exit code, whatever --format says.
stub q-ledger.json 3 -- --format count; R=$?
[ "$R" = 3 ] && [ "$(jget 'o["failure_class"]')" = "ledger-unreadable" ] && ok "ledger-unreadable: JSON printed under --format count, exit 3" || bad "failure under --format count" "rc=$R $(cat "$T/out")"
stub q-blocked.json 3 -- --format ids; R=$?
[ "$R" = 3 ] && [ "$(jget 'o["reason"]')" = "adapter-unavailable:vcs_host" ] && ok "blocked adapter: JSON printed under --format ids, exit 3" || bad "blocked under --format ids" "rc=$R $(cat "$T/out")"
stub q-garbage.json 1 -- --format count; R=$?
[ "$R" = 1 ] && grep -q '"api-unavailable"' "$T/out" && grep -q 'Traceback' "$T/out" && ok "non-JSON output: raw text kept, failure JSON appended, non-zero exit" || bad "garbage output" "rc=$R $(cat "$T/out")"
stub q-garbage.json 0 -- --format count; R=$?
[ "$R" = 2 ] && grep -q '"api-unavailable"' "$T/out" && ok "non-JSON output with rc 0 is still a failure (exit 2), never a count" || bad "garbage rc 0" "rc=$R $(cat "$T/out")"
# count 0 with rows is a reader bug: it must not read as empty.
echo '{"status":"ok","count":0,"unreviewed":[{"pr_id":"7"}],"already_reviewed":[],"loop_prs_skipped":[],"too_large":[]}' > "$T/q-liar.json"
stub q-liar.json 0 -- --format count; R=$?
[ "$R" = 0 ] && [ "$(cat "$T/out")" = "1" ] && ok "count is derived from the rows, never from a count field" || bad "liar count" "rc=$R $(cat "$T/out")"

# Usage errors are this script's own: exit 1, JSON envelope, pr_feed never called.
for argv in "--limit x" "--format xml" "--bogus" "--scope=" "--scope"; do
  set -- $argv
  stub q-empty.json 0 -- "$@"; R=$?
  [ "$R" = 1 ] && [ "$(jget 'o["status"]')" = "usage" ] && [ ! -f "$T/args" ] && ok "argv '$argv' -> usage, exit 1, no read" || bad "argv '$argv'" "rc=$R $(cat "$T/out")"
done
env LOOP_PR_FEED="$T/no-such-feed.py" bash "$PP" --scope all > "$T/out" 2>/dev/null; R=$?
[ "$R" = 2 ] && [ "$(jget 'o["failure_class"]')" = "config-invalid" ] && ok "missing queue reader -> config-invalid, exit 2" || bad "missing feed" "rc=$R $(cat "$T/out")"
bash "$PP" --help > "$T/out" 2>/dev/null; R=$?
[ "$R" = 0 ] && grep -q -- '--format count' "$T/out" && ok "--help prints the usage block" || bad "--help" "rc=$R"

# ── Part 2: end to end over the real pr_feed.py + the git-local provider ─────
echo
echo "Part 2 — pending-prs.sh end to end (real pr_feed.py, git-local vcs_host, fixture board)"
mkdir -p "$T/app" "$T/prs" "$T/runs"
git -C "$T/app" init -q
cat > "$T/stack.json" <<EOF
{"version": "1.0.0",
 "project": {"name": "my-app", "default_branch": "main"},
 "app": {"repo_dir": "$T/app"},
 "adapters": {"vcs_host": {"provider": "git-local", "config": {"prs_dir": "$T/prs"}}},
 "evidence": {"root": "$T/runs"},
 "integrations": {"pr_review": {"loop_pr_label": "agent-loop"}}}
EOF
# A board that exercises every filter: a draft, another base branch, both authors, the loop
# label in mixed case, and heads that match the ledger in every way the ledger compares them.
python3 - "$T" <<'PY'
import json, os, sys
t = sys.argv[1]
ME, YOU = "owner@example.com", "reviewer@example.com"
def pr(i, author=ME, head=None, draft=False, base="main", labels=None):
    return {"id": str(i), "url": "file://%s/prs/%d.md" % (t, i), "branch": "feature/%d" % i, "base": base,
            "title": "PR %d" % i, "body": "", "state": "open", "draft": draft, "labels": labels or [],
            "author": author.upper() if i % 2 else author, "created_at": "2000-01-01T00:%02d:00Z" % (i % 60),
            "dry_run": True, "raw": {"head_commit": head or "%040x" % i, "base_commit": "b" * 40}}
board = [
    pr(101), pr(102, YOU), pr(103, draft=True), pr(104, base="release/1"),
    pr(105, labels=["21.0.0", "Agent-Loop"]), pr(106, YOU, labels=["agent-loop"]),
    pr(107, head="abcdef1234567890abcdef1234567890abcdef12"),   # ledger: 7-char prefix -> current
    pr(108, head="abcdef0000000000000000000000000000000000"),   # ledger: 6-char prefix -> pending
    pr(109, YOU, head="c" * 40),                               # ledger: same commit -> current
    pr(110, head="d" * 40),                                    # ledger: older commit -> pending
    pr(111, YOU),                                              # ledger: entry without head_commit
]
for p in board:
    json.dump(p, open(os.path.join(t, "prs", p["id"] + ".json"), "w"))
json.dump({
    "107": {"head_commit": "ABCDEF1", "reviewed_at": "2000-01-02T10:00:00+00:00", "verdict": "approve"},
    "108": {"head_commit": "abcdef", "verdict": "approve"},
    "109": {"head_commit": "c" * 40, "verdict": "request-changes"},
    "110": {"head_commit": "e" * 40, "verdict": "approve"},
    "111": {"verdict": "approve"},
    "105": {"head_commit": "0" * 40},
}, open(os.path.join(t, "ledger.json"), "w"))
open(os.path.join(t, "ledger-corrupt.json"), "w").write('{"101": {"head_commit": ')
PY
e2e() {   # e2e <ledger> <args…>
  local ledger="$1"; shift
  env -u LOOP_PR_AUTHOR_SCOPE -u LOOP_PR_INCLUDE_LOOP_PRS \
      LOOPS_STACK_CONFIG="$T/stack.json" LOOP_PR_LEDGER_FILE="$T/$ledger" LOOP_PR_LEDGER_BACKEND=file \
      LOOP_PR_IDENTITY=owner@example.com LOOP_LOOP_LIMITS="$T/limits-others.json" \
      bash "$PP" "$@" > "$T/out" 2> "$T/err"
}
[ -r "$FEED" ] || { bad "pr_feed.py not found at $FEED"; echo; echo "$PASS passed, $FAIL failed"; exit 1; }

e2e ledger.json --scope all --format ids; R=$?
GOT="$(tr '\n' ' ' < "$T/out")"
[ "$R" = 0 ] && [ "$GOT" = "111 110 108 102 101 " ] && ok "scope all: drafts, other bases, loop PRs and ledger-current PRs excluded -> $GOT" || bad "scope all ids" "rc=$R ids='$GOT' $(cat "$T/err")"
e2e ledger.json --scope own --format ids; GOT="$(tr '\n' ' ' < "$T/out")"
[ "$GOT" = "110 108 101 " ] && ok "scope own: only the configured identity's PRs (case-insensitive) -> $GOT" || bad "scope own" "ids='$GOT'"
e2e ledger.json --scope others --format count
[ "$(cat "$T/out")" = "2" ] && ok "scope others: everyone except the configured identity -> 2" || bad "scope others" "$(cat "$T/out")"
e2e ledger.json --format count   # no flag, no env: loop-limits.json says others
[ "$(cat "$T/out")" = "2" ] && ok "bare call runs with loop-limits.json's scope (others)" || bad "bare e2e scope" "$(cat "$T/out")"
e2e ledger.json --scope all --limit 2 --format ids; GOT="$(tr '\n' ' ' < "$T/out")"
[ "$GOT" = "111 110 " ] && ok "--limit 2 keeps the two newest" || bad "--limit 2" "ids='$GOT'"
e2e ledger.json --scope all --include-loop-prs --format ids; GOT="$(tr '\n' ' ' < "$T/out")"
[ "$GOT" = "111 110 108 106 105 102 101 " ] && ok "--include-loop-prs puts the loop-labelled PRs back in the queue" || bad "include loop PRs" "ids='$GOT'"
e2e ledger.json --scope all
python3 - "$T/out" <<'PY' && ok "JSON: loop PRs named under loop_prs_skipped, current PRs under already_reviewed, reasons set" || bad "JSON shape" "$(head -c 600 "$T/out")"
import json, sys
o = json.load(open(sys.argv[1]))
assert o["status"] == "ok" and o["count"] == 5
assert sorted(r["pr_id"] for r in o["loop_prs_skipped"]) == ["105", "106"]
assert sorted(r["pr_id"] for r in o["already_reviewed"]) == ["107", "109"]
reasons = {r["pr_id"]: r["reason"] for r in o["unreviewed"]}
assert reasons == {"101": "never-reviewed", "102": "never-reviewed", "108": "new-commits-since-review",
                   "110": "new-commits-since-review", "111": "new-commits-since-review"}, reasons
assert o["filter"]["author_scope"] == "all" and o["filter"]["author_scope_source"] == "flag"
PY
e2e no-such-ledger.json --scope all --format count
[ "$(cat "$T/out")" = "7" ] && ok "missing ledger file: every candidate is never-reviewed (7)" || bad "missing ledger" "$(cat "$T/out")"
e2e ledger-corrupt.json --scope all --format count; R=$?
[ "$R" = 3 ] && grep -q '"ledger-unreadable"' "$T/out" && ok "corrupt ledger: ledger-unreadable JSON under --format count, exit 3 (fail closed)" || bad "corrupt ledger" "rc=$R $(cat "$T/out")"
env -u LOOP_PR_AUTHOR_SCOPE LOOPS_STACK_CONFIG="$T/stack.json" LOOP_PR_LEDGER_FILE="$T/ledger.json" \
    LOOPS_CFG__ADAPTERS__VCS_HOST__PROVIDER=none bash "$PP" --scope all --format count > "$T/out" 2>/dev/null; R=$?
[ "$R" = 3 ] && [ "$(jget 'o["reason"]')" = "adapter-unavailable:vcs_host" ] && ok "vcs_host provider none: blocked adapter-unavailable:vcs_host, exit 3, never an empty queue" || bad "provider none" "rc=$R $(cat "$T/out")"
e2e ledger.json --scope all --format count 2>/dev/null
LOOP_PR_AUTHOR_SCOPE=OWN env LOOPS_STACK_CONFIG="$T/stack.json" LOOP_PR_LEDGER_FILE="$T/ledger.json" bash "$PP" --format count > "$T/out" 2>/dev/null; R=$?
[ "$R" = 2 ] && [ "$(jget 'o["failure_class"]')" = "config-invalid" ] && ok "LOOP_PR_AUTHOR_SCOPE=OWN: pr_feed's config-invalid, exit 2 (not widened)" || bad "bad env scope" "rc=$R $(cat "$T/out")"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
