#!/usr/bin/env bash
# Regression: qa-dream harvest.py — the deterministic, zero-LLM half of the consolidation
# pass (brain-spec 2a). The dream only earns its keep if the brief is REPRODUCIBLE (same
# inputs -> same bytes), BOUNDED (compressed lessons, never a transcript pile), and HONEST
# about an empty window, a broken line, or a brain it could not reach.
#
# Runs entirely against the synthetic fixture in fixtures/dream/ — NEVER the user's real
# ~/.claude/projects transcripts, which are appended to while a test reads them and would make
# every assertion here a coin flip. The fixture is copied to a temp dir and its mtimes are
# pinned from python (epoch, not `touch -t`, so the window does not shift with the machine's
# timezone); the tracked copy is checksummed before and after to prove nothing wrote to it.
#
# No brain, no network: every harvest here passes --no-brain, except the one check that
# points --kb at a missing binary to prove the degraded lookup reports UNKNOWN, not "absent".
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
H="$ROOT/.claude/skills/qa-dream/scripts/harvest.py"
SRCFX="$HERE/fixtures/dream"
NOW=2026-03-16T00:00:00Z          # pinned clock -> byte-reproducible brief
WM_FROM=2026-03-09T00:00:00Z      # --since 7d from NOW
WM_TO=2026-03-15T12:00:00Z        # the newest record actually CONSUMED, not the clock
TMP="$(mktemp -d)"
FX="$TMP/fx"
pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); else echo "  [FAIL] $1 — got '$2' want '$3'"; fail=$((fail+1)); fi; }
ckn(){ if [ "$2" != "$3" ]; then pass=$((pass+1)); else echo "  [FAIL] $1 — got '$2', wanted anything else"; fail=$((fail+1)); fi; }
# read one value out of a brief FILE; empty string on any miss, never a traceback
jf(){ python3 -c "
import os, sys, json
try:
    d = json.load(open(sys.argv[1]))
    s = [x for x in d.get('sessions', []) if x['session_id'] == 'sess-fixture-0001']
    s = s[0] if s else {}
    v = ($2)
except Exception:
    v = ''
print('' if v is None else v)
" "$1"; }
sum_dir(){ (cd "$1" && find . -type f | sort | xargs cksum) | cksum | cut -d' ' -f1; }

if [ ! -f "$H" ]; then
  echo "  dream_harvest: SKIPPED (qa-dream/scripts/harvest.py not present)"; rm -rf "$TMP"; exit 0
fi
if [ ! -d "$SRCFX" ]; then
  echo "  [FAIL] fixture missing: $SRCFX"; rm -rf "$TMP"; exit 1
fi
trap 'rm -rf "$TMP"' EXIT

FXSUM_BEFORE="$(sum_dir "$SRCFX")"
mkdir -p "$FX"; cp -R "$SRCFX/." "$FX/"
python3 - "$FX" <<'PY'
import calendar, os, sys
t = calendar.timegm((2026, 3, 15, 12, 0, 0, 0, 0, 0))   # 2026-03-15T12:00:00Z, TZ-independent
for dp, _dn, fn in os.walk(sys.argv[1]):
    for n in fn:
        os.utime(os.path.join(dp, n), (t, t))
PY
HARV(){ python3 "$H" --projects-root "$FX/projects" --repo "$FX/repo" --all-projects \
                     --no-brain --now "$NOW" --since 7d "$@"; }

# ── the baseline brief ──────────────────────────────────────────────────────
HARV --out "$TMP/b1.json" >/dev/null 2>"$TMP/b1.err"; rc=$?
B="$TMP/b1.json"
ck "harvest exits 0"               "$rc" "0"
ck "schema is declared"            "$(jf "$B" "d['schema']")" "qa-dream.brief/1"
ck "both sessions scanned"         "$(jf "$B" "d['sessions_scanned']")" "2"
ck "sessions_available reported"   "$(jf "$B" "d['sessions_available']")" "2"
ck "the run artifacts were read"   "$(jf "$B" "d['tickets_seen']")" "1"
ck "ticket key resolved"           "$(jf "$B" "d['tickets'][0]['key']")" "PROJ-9001"
ck "ticket dir is repo-relative"   "$(jf "$B" "d['tickets'][0]['dir']")" "repo/runs/PROJ-9001"
ck "ticket area normalized"        "$(jf "$B" "d['tickets'][0]['area']")" "settings-members"
ck "followups harvested"           "$(jf "$B" "any('FOLLOWUPMARKER' in f for f in d['tickets'][0]['followups'])")" "True"

# ── the candidate set: one of every kind the dream can write ────────────────
ck "app_map candidates"            "$(jf "$B" "d['candidate_counts']['app_map']")" "2"
ck "fix_lesson candidate"          "$(jf "$B" "d['candidate_counts']['fix_lessons']")" "1"
ck "expectation candidate"         "$(jf "$B" "d['candidate_counts']['expectations']")" "1"
ck "flakiness candidates"          "$(jf "$B" "d['candidate_counts']['flakiness']")" "2"
ck "env_lesson candidate"          "$(jf "$B" "d['candidate_counts']['env_lessons']")" "1"
ck "skill_followup candidate"      "$(jf "$B" "d['candidate_counts']['skill_followups']")" "1"
ck "metrics candidates"            "$(jf "$B" "d['candidate_counts']['metrics']")" "8"
ck "the backlog is a metric"       "$(jf "$B" "any(m['name'] == 'dream_sessions_remaining' for m in d['candidates']['metrics'])")" "True"
ck "expectations stay PROPOSE-only" "$(jf "$B" "all(c['propose_only'] for c in d['candidates']['expectations'])")" "True"
ck "fix_lesson gated on verify=pass" "$(jf "$B" "d['tickets'][0]['statuses']['verify']")" "pass"

# ── the window: data-derived, never the wall clock ──────────────────────────
ck "watermark_from is the window"  "$(jf "$B" "d['watermark_from']")" "$WM_FROM"
ck "watermark_to is data-derived"  "$(jf "$B" "d['watermark_to']")" "$WM_TO"
ckn "watermark_to is NOT now"      "$(jf "$B" "d['watermark_to']")" "$NOW"

# ── nothing outside the window, and nothing that is not the human's voice ───
for m in OUTOFWINDOWMARKER FUTUREMARKER TOOLRESULTMARKER SIGNATURELEAKMARKER QkFTRTY0TEVBS0FHRU1BUktFUg; do
  ck "no leakage: $m"              "$(grep -c "$m" "$B")" "0"
done

# ── caps: a brief, never a transcript pile ─────────────────────────────────
ck "human turns capped at 8"       "$(jf "$B" "len(s['human_turns'])")" "8"
ck "the cap keeps the LATEST"      "$(jf "$B" "any(h['text'].startswith('fixture human turn 04') for h in s['human_turns'])")" "True"
ck "the cap drops the oldest"      "$(jf "$B" "any(h['text'].startswith('fixture human turn 01') for h in s['human_turns'])")" "False"
ck "a long turn is truncated"      "$(jf "$B" "max(len(h['text']) for h in s['human_turns']) <= 200")" "True"
ck "and marked as truncated"       "$(jf "$B" "any(h['text'].endswith('…') for h in s['human_turns'])")" "True"
ck "failed commands capped at 6"   "$(jf "$B" "len(s['failed_commands'])")" "6"
ck "the oldest failure is dropped" "$(jf "$B" "any('run 01' in f['command'] for f in s['failed_commands'])")" "False"
ck "files_edited capped at 20"     "$(jf "$B" "len(s['files_edited'])")" "20"
ck "files keep first-seen order"   "$(jf "$B" "s['files_edited'][0].endswith('File01.js')")" "True"
ck "the 25th file is dropped"      "$(jf "$B" "any('File25' in p for p in s['files_edited'])")" "False"
ck "recap truncated to 320"        "$(jf "$B" "len(s['recap']) <= 320")" "True"

# ── subagents fold into their parent session ───────────────────────────────
ck "subagent recap folded in"      "$(jf "$B" "any('SUBAGENTRECAPMARKER' in r['recap'] for r in s['subagent_recaps'])")" "True"
ck "subagent failure folded in"    "$(jf "$B" "any('subagent-probe' in f['command'] for f in s['failed_commands'])")" "True"

# ── kb invocations are noticed (that is how the dream sees what a run wrote) ─
ck "kb calls captured"             "$(jf "$B" "len(s['kb_calls'])")" "2"
ck "kb verb + store parsed"        "$(jf "$B" "[(k['verb'], k['store']) for k in s['kb_calls']][0][0]")" "write"
ck "kb call outcome recorded"      "$(jf "$B" "all(k['ok'] is True for k in s['kb_calls'])")" "True"

# ── the whole point: the SAME trap in 2 sessions becomes ONE lesson ────────
ck "env lesson merges sessions"    "$(jf "$B" "d['candidates']['env_lessons'][0]['distinct_sessions']")" "2"
ck "env lesson is classified"      "$(jf "$B" "d['candidates']['env_lessons'][0]['signature']")" "loopsfixture:command-not-found"
ck "env lesson carries evidence"   "$(jf "$B" "len(d['candidates']['env_lessons'][0]['examples']) >= 1")" "True"
ck "the RECOVERY is the payload"   "$(jf "$B" "d['candidates']['env_lessons'][0]['examples'][0]['recovered_by'].startswith('python3 -m loopsfixture')")" "True"
ck "skill followup merges too"     "$(jf "$B" "d['candidates']['skill_followups'][0]['distinct_sessions']")" "2"
ck "skill followup is classified"  "$(jf "$B" "d['candidates']['skill_followups'][0]['pattern']")" "doc-wrong"

# ── determinism: same inputs -> the same bytes ─────────────────────────────
HARV --out "$TMP/b2.json" >/dev/null 2>&1
ck "two runs are byte-identical"   "$(cmp -s "$TMP/b1.json" "$TMP/b2.json" && echo same || echo differs)" "same"
HARV --compact --out "$TMP/c1.json" >/dev/null 2>&1
HARV --compact --out "$TMP/c2.json" >/dev/null 2>&1
ck "compact is deterministic too"  "$(cmp -s "$TMP/c1.json" "$TMP/c2.json" && echo same || echo differs)" "same"

# ── an empty window must never advance the watermark over unseen ground ────
HARV --since 1s --out "$TMP/e.json" >/dev/null 2>&1; rc=$?
ck "empty window exits 0"          "$rc" "0"
ck "empty window: no sessions"     "$(jf "$TMP/e.json" "d['sessions_scanned']")" "0"
ck "empty window: no tickets"      "$(jf "$TMP/e.json" "d['tickets_seen']")" "0"
ck "empty window: no candidates"   "$(jf "$TMP/e.json" "sum(v for k, v in d['candidate_counts'].items() if k != 'metrics')")" "0"
ck "watermark does NOT advance"    "$(jf "$TMP/e.json" "d['watermark_to'] == d['watermark_from']")" "True"

# ── session cap ────────────────────────────────────────────────────────────
HARV --max-sessions 1 --out "$TMP/m.json" >/dev/null 2>&1
ck "--max-sessions caps the scan"  "$(jf "$TMP/m.json" "d['sessions_scanned']")" "1"
ck "and says what it skipped"      "$(jf "$TMP/m.json" "d['sessions_skipped_by_cap']")" "1"
ck "the alias means 'deferred'"    "$(jf "$TMP/m.json" "d['sessions_skipped_by_cap'] == d['sessions_remaining']")" "True"

# ── budget: trim evidence, NEVER the product; then say so honestly ─────────
HARV --max-bytes 11000 --out "$TMP/t.json" >/dev/null 2>&1
ck "over budget -> truncated"      "$(jf "$TMP/t.json" "d['truncated']")" "True"
ck "trimming stayed within budget" "$(jf "$TMP/t.json" "d['budget_exceeded']")" "False"
ck "it names what it trimmed"      "$(jf "$TMP/t.json" "len(d['dropped']['session_fields']) > 0")" "True"
ck "candidates survive trimming"   "$(jf "$TMP/t.json" "sum(d['candidate_counts'].values())")" "16"
ck "tickets survive trimming"      "$(jf "$TMP/t.json" "len(d['tickets'])")" "1"
HARV --max-bytes 2500 --out "$TMP/x.json" >/dev/null 2>&1; rc=$?
ck "impossible budget still 0"     "$rc" "0"
ck "it ADMITS it cannot comply"    "$(jf "$TMP/x.json" "d['budget_exceeded']")" "True"
ck "and warns rather than lies"    "$(jf "$TMP/x.json" "any('max-bytes' in w for w in d['warnings'])")" "True"
ck "candidates STILL intact"       "$(jf "$TMP/x.json" "sum(d['candidate_counts'].values())")" "16"
ck "evidence thinned, not zeroed"  "$(jf "$TMP/x.json" "all(len(e['examples']) >= 1 for e in d['candidates']['env_lessons'])")" "True"

# ── a broken transcript is COUNTED, never fatal ───────────────────────────
ck "a malformed line is counted"   "$(jf "$B" "d['parse_stats']['malformed']")" "1"
ck "nothing was unreadable"        "$(jf "$B" "d['parse_stats']['unreadable']")" "0"
ck "no huge line yet"              "$(jf "$B" "d['parse_stats']['huge_lines']")" "0"
python3 - "$FX/projects/-tmp-loops-fixture-repo/sess-fixture-0002.jsonl" <<'PY'
import json, sys
with open(sys.argv[1], "a") as fh:          # a 3MB attachment line, generated not committed
    fh.write(json.dumps({"type": "user", "timestamp": "2026-03-15T10:05:00.000Z",
                         "huge": "A" * 3000000}) + "\n")
PY
HARV --out "$TMP/g.json" >/dev/null 2>&1; rc=$?
ck "a 3MB line does not crash"     "$rc" "0"
ck "the huge line is SKIPPED"      "$(jf "$TMP/g.json" "d['parse_stats']['huge_lines']")" "1"
ck "the session still harvests"    "$(jf "$TMP/g.json" "d['sessions_scanned']")" "2"

# ── bad input: one JSON error object, exit 2, never a traceback ───────────
python3 "$H" --since banana --no-brain >/dev/null 2>"$TMP/err1"; rc=$?
ck "--since banana exits 2"        "$rc" "2"
ck "and is machine-readable"       "$(python3 -c "import json,sys;print(json.load(open('$TMP/err1'))['ok'])" 2>/dev/null)" "False"
ck "no traceback on bad --since"   "$(grep -c Traceback "$TMP/err1")" "0"
python3 "$H" --now nope --no-brain >/dev/null 2>"$TMP/err2"; rc=$?
ck "--now nope exits 2"            "$rc" "2"
ck "no traceback on bad --now"     "$(grep -c Traceback "$TMP/err2")" "0"

# ── a missing projects root warns and yields an empty-but-VALID brief ─────
python3 "$H" --projects-root "$TMP/no-such-dir" --repo "$FX/repo" --all-projects --no-brain \
        --now "$NOW" --since 7d --out "$TMP/n.json" >/dev/null 2>&1; rc=$?
ck "missing projects root exits 0" "$rc" "0"
ck "it says no slugs matched"      "$(jf "$TMP/n.json" "any('no project slugs matched' in w for w in d['warnings'])")" "True"
ck "still a valid brief"           "$(jf "$TMP/n.json" "d['schema']")" "qa-dream.brief/1"

# ── the brain lookup degrades to UNKNOWN — never to 'absent' ──────────────
python3 "$H" --projects-root "$FX/projects" --repo "$FX/repo" --all-projects \
        --kb "$TMP/no-such-kb.py" --now "$NOW" --since 7d --out "$TMP/k.json" >/dev/null 2>&1; rc=$?
ck "a missing kb CLI exits 0"      "$rc" "0"
ck "the lookup names its failure"  "$(jf "$TMP/k.json" "d['tickets'][0]['brain']['lookup']")" "kb_cli_missing"
ck "and warns about it"            "$(jf "$TMP/k.json" "any('brain lookup unavailable' in w for w in d['warnings'])")" "True"
ck "already_in_brain stays UNKNOWN" "$(jf "$TMP/k.json" "all(c['already_in_brain'] is None for c in d['candidates']['fix_lessons'])")" "True"

# ── the fixture artifacts are contract-true (this is what makes them a
#    legitimate stand-in for a real run's evidence) ────────────────────────
V="$ROOT/contracts/validate.py"
T="$FX/repo/runs/PROJ-9001"
ck "fixture repro.json validates"      "$(python3 "$V" "$ROOT/contracts/qa-repro.json"      "$T/repro.json"      >/dev/null 2>&1; echo $?)" "0"
ck "fixture fix.json validates"        "$(python3 "$V" "$ROOT/contracts/dev-fix.json"       "$T/fix.json"        >/dev/null 2>&1; echo $?)" "0"
ck "fixture regression.json validates" "$(python3 "$V" "$ROOT/contracts/qa-regression.json" "$T/regression.json" >/dev/null 2>&1; echo $?)" "0"
ck "fixture verify.json validates"     "$(python3 "$V" "$ROOT/contracts/qa-verify.repro-judge.json" "$T/verify.json" >/dev/null 2>&1; echo $?)" "0"
ck "fixture carries the v1.1.0 keys"   "$(python3 -c "
import json; d = json.load(open('$T/fix.json'))
print(bool(d.get('lessons_consulted')) and isinstance(d.get('pending_fix_lesson'), dict))")" "True"

# ── the SIGNAL/NOISE bar: what the brief refuses to carry ──────────────────
# A dream brief full of junk produces a brain full of junk, so the filter is load-bearing.
# The fixture only contains real traps, so these guard the classifier directly — including
# the exact shapes that flooded a live 7-day harvest with 37 buckets of nothing.
hpy(){ python3 -c "
import sys
sys.path.insert(0, '$ROOT/.claude/skills/qa-dream/scripts')
import harvest as h
print($1)
"; }
ck "compound script is not named"  "$(hpy "h.command_head('cd /r/app\necho x\nsed -n 1,5p f')")" "?"
ck "loop header is not a command"  "$(hpy "h.command_head('for f in a b; do sed -n 1p \$f; done')")" "?"
ck "a blocked sleep names sleep"   "$(hpy "h.command_head('sleep 240; cd /r && git status')")" "sleep"
ck "cd hop peels to the real cmd"  "$(hpy "h.command_head('cd /r/app && grep -rn X src')")" "grep"
ck "wrappers peel (time/sudo)"     "$(hpy "h.command_head('time bash run_all.sh')")" "bash"
ck "keeps command-not-found"       "$(hpy "h.env_lesson_worthy('x:command-not-found', False)")" "True"
ck "keeps no-such-file"            "$(hpy "h.env_lesson_worthy('x:no-such-file', False)")" "True"
ck "keeps harness-blocked"         "$(hpy "h.env_lesson_worthy('sleep:harness-blocked', False)")" "True"
ck "keeps input-validation"        "$(hpy "h.env_lesson_worthy('x:input-validation', False)")" "True"
ck "DROPS a timeout (exit 143)"    "$(hpy "h.env_lesson_worthy('x:exit-143', True)")" "False"
ck "DROPS a bare exit-1"           "$(hpy "h.env_lesson_worthy('grep:exit-1', True)")" "False"
ck "DROPS an unclassified error"   "$(hpy "h.env_lesson_worthy('x:error', True)")" "False"
ck "DROPS a heredoc traceback"     "$(hpy "h.env_lesson_worthy('python3:python-traceback', True)")" "False"
ck "recovery alone is not a bar"   "$(hpy "h.env_lesson_worthy('x:exit-2', True) is h.env_lesson_worthy('x:exit-2', False)")" "True"
ck "a declined tool call is not a failure" \
   "$(hpy "bool(h.USER_REJECTED_RE.search(\"The user doesn't want to proceed with this tool use.\"))")" "True"
ck "assistant prose cannot raise classifier-gap" \
   "$(hpy "'classifier-gap' in h.ASSISTANT_SIGNAL_PATTERNS")" "False"
ck "assistant prose may raise doc-wrong" \
   "$(hpy "'doc-wrong' in h.ASSISTANT_SIGNAL_PATTERNS")" "True"
# and the drops are REPORTED, never silently swallowed
ck "filter reports its rejects"    "$(jf "$B" "isinstance(d['filtered']['env_lessons_filtered'], dict)")" "True"
ck "filter reports a total"        "$(jf "$B" "isinstance(d['filtered']['env_lessons_filtered_total'], int)")" "True"

# ══ CATCH-UP-SAFE WATERMARK (spec FIX 3) ═══════════════════════════════════
# The bug this replaces: the cap kept the NEWEST sessions (`files[-N:]`) while the watermark
# was derived from what it kept, so a capped run advanced the watermark over sessions it had
# never opened. Transcripts rotate in ~3-4 weeks, so that material was gone for good — a
# measured 30d window scanned 38 sessions and skipped 68.
#
# Own fixture: fixtures/dream-catchup/ — 5 NON-OVERLAPPING sessions pinned to DISTINCT
# mtimes (09:00…13:00) plus a run-artifact dir pinned LATER than all of them (13:30). The
# distinct mtimes matter: a second-resolution watermark cannot separate two files stamped in
# the same second, which is exactly the no-progress case asserted at the end of this block.
SRCCU="$HERE/fixtures/dream-catchup"
CU="$TMP/cu"
if [ ! -d "$SRCCU" ]; then
  echo "  [FAIL] catch-up fixture missing: $SRCCU"; fail=$((fail+1))
else
CUSUM_BEFORE="$(sum_dir "$SRCCU")"
mkdir -p "$CU"; cp -R "$SRCCU/." "$CU/"
python3 - "$CU" <<'PY'
import calendar, os, sys
def ep(h, m=0):
    return calendar.timegm((2026, 3, 15, h, m, 0, 0, 0, 0))   # TZ-independent, like above
PINS = {"sess-catchup-01.jsonl": ep(9),  "sess-catchup-02.jsonl": ep(10),
        "sess-catchup-03.jsonl": ep(11), "sess-catchup-04.jsonl": ep(12),
        "sess-catchup-05.jsonl": ep(13), "task-metadata.json":    ep(13, 30)}
for dp, _dn, fn in os.walk(sys.argv[1]):
    for n in fn:
        t = PINS.get(n)
        if t is not None:
            os.utime(os.path.join(dp, n), (t, t))
PY
CHARV(){ python3 "$H" --projects-root "$CU/projects" --repo "$CU/repo" --all-projects \
                      --no-brain --now "$NOW" "$@"; }
ids_of(){ python3 -c "
import json, sys
print(' '.join(s['session_id'] for s in json.load(open(sys.argv[1]))['sessions']))" "$1"; }

# ── pass 1: the cap keeps the OLDEST and the watermark stops where it read ──
CHARV --watermark "$WM_FROM" --max-sessions 2 --out "$TMP/cu1.json" >/dev/null 2>&1; rc=$?
C1="$TMP/cu1.json"
ck "capped pass exits 0"           "$rc" "0"
ck "the cap keeps the OLDEST"      "$(ids_of "$C1")" "sess-catchup-01 sess-catchup-02"
ck "the newest are NOT consumed"   "$(jf "$C1" "any(s['session_id'] == 'sess-catchup-05' for s in d['sessions'])")" "False"
ck "watermark = newest KEPT file"  "$(jf "$C1" "d['watermark_to']")" "2026-03-15T10:00:00Z"
ckn "watermark is NOT the clock"   "$(jf "$C1" "d['watermark_to']")" "$NOW"
ckn "nor the newest DEFERRED file" "$(jf "$C1" "d['watermark_to']")" "2026-03-15T13:00:00Z"
ck "nor the newer run artifact"    "$(jf "$C1" "d['watermark_to'] < '2026-03-15T13:30:00Z'")" "True"
ck "the leftover is reported"      "$(jf "$C1" "d['sessions_remaining']")" "3"
ck "catch_up_needed is raised"     "$(jf "$C1" "d['catch_up_needed']")" "True"
ck "the alias keeps agreeing"      "$(jf "$C1" "d['sessions_skipped_by_cap']")" "3"
ck "files read is reported"        "$(jf "$C1" "d['session_files_read']")" "2"
ck "the backlog reaches the brain" "$(jf "$C1" "[m['value'] for m in d['candidates']['metrics'] if m['name'] == 'dream_sessions_remaining'][0]")" "3"
CHARV --watermark "$WM_FROM" --max-sessions 2 --out "$TMP/cu1b.json" >/dev/null 2>&1
ck "a capped pass is deterministic" "$(cmp -s "$C1" "$TMP/cu1b.json" && echo same || echo differs)" "same"

# ── the drain: pass N+1 starts where pass N stopped, until the backlog is 0 ─
# The load-bearing assertion is the union: every session is harvested exactly once across
# the drain. That is the property the old `files[-N:]` cap destroyed.
wm="$WM_FROM"; passes=0; seen=""; need="True"
while [ "$passes" -lt 8 ]; do
  passes=$((passes+1))
  CHARV --watermark "$wm" --max-sessions 2 --out "$TMP/cud$passes.json" >/dev/null 2>&1
  seen="$seen $(ids_of "$TMP/cud$passes.json")"
  need="$(jf "$TMP/cud$passes.json" "d['catch_up_needed']")"
  wm="$(jf "$TMP/cud$passes.json" "d['watermark_to']")"
  [ "$need" = "True" ] || break
done
ck "the drain terminates"          "$passes" "4"
ck "catch_up_needed clears"        "$need" "False"
ck "EVERY session was harvested"   "$(echo $seen | tr ' ' '\n' | sort -u | tr '\n' ' ')" \
   "sess-catchup-01 sess-catchup-02 sess-catchup-03 sess-catchup-04 sess-catchup-05 "
ck "and none of them twice"        "$(echo $seen | tr ' ' '\n' | sort | uniq -d | wc -l | tr -d ' ')" "0"
# an already-drained session yields nothing, yet must still count as processed — otherwise
# it refills the cap on every pass and the backlog never drains
ck "a re-read session is empty"    "$(jf "$TMP/cud2.json" "d['sessions_empty_in_window']")" "1"
ck "yet the watermark moved on"    "$(jf "$TMP/cud2.json" "d['watermark_to'] > d['watermark_from']")" "True"
ck "the last pass is uncapped"     "$(jf "$TMP/cud4.json" "d['watermark_to']")" "2026-03-15T13:30:00Z"
CHARV --watermark "$wm" --max-sessions 2 --out "$TMP/cuz.json" >/dev/null 2>&1
ck "a drained re-run reads nothing" "$(jf "$TMP/cuz.json" "d['sessions_available']")" "0"
ck "and holds the watermark still"  "$(jf "$TMP/cuz.json" "d['watermark_to'] == d['watermark_from']")" "True"

# ── the degenerate cap: no progress is possible, so SAY so ─────────────────
# --max-sessions 1 is always eaten by the one file the next pass re-reads. A nightly job
# that silently loops forever is the exact failure this fix exists to remove.
CHARV --watermark "$WM_FROM" --max-sessions 1 --out "$TMP/cus.json" >/dev/null 2>&1
ck "a cap of 1 cannot drain"       "$(jf "$TMP/cus.json" "any('catch-up cannot advance' in w for w in d['warnings'])")" "True"
ck "and it still loses nothing"    "$(jf "$TMP/cus.json" "d['sessions_remaining']")" "4"

# ── the byte budget trims the BRIEF, never the pass ────────────────────────
# Headroom is deliberate. The minimum achievable brief on this fixture is 4,586 bytes, so
# 4600 left a 14-byte margin — any future field added to the brief's always-present
# structure would have turned this check red for a reason unrelated to what it tests.
# What it tests is that the byte budget FORCES session drops; sessions_dropped is asserted
# separately, so the exact ceiling is not the assertion's meaning.
# 5200, not the original 4000: the brief's non-trimmable floor rose when SF-4 added the
# always-present `artifact_sources` / `artifact_sources_skipped` lists (two absolute temp
# paths on this fixture) and the prose counters. The assertion is unchanged in meaning —
# whole sessions are dropped, the watermark does not move, and the result really fits.
CHARV --watermark "$WM_FROM" --max-bytes 5200 --out "$TMP/cub.json" >/dev/null 2>&1
ck "budget drops whole sessions"   "$(jf "$TMP/cub.json" "d['dropped']['sessions_dropped'] > 0")" "True"
ck "the watermark does NOT move"   "$(jf "$TMP/cub.json" "d['watermark_to']")" "2026-03-15T13:30:00Z"
ck "and the drop is admitted"      "$(jf "$TMP/cub.json" "any('max-bytes dropped whole sessions' in w for w in d['warnings'])")" "True"
ck "budget stayed inside budget"   "$(jf "$TMP/cub.json" "d['budget_exceeded']")" "False"
ck "trimmed brief is really <=5200" "$(python3 -c "import os;print(os.path.getsize('$TMP/cub.json') - 1 <= 5200)")" "True"
ck "catch-up fixture untouched"    "$(sum_dir "$SRCCU")" "$CUSUM_BEFORE"
fi

# ══ THE SANDBOX BLIND SPOT + PROSE EVIDENCE (SF-4) ═════════════════════════
# The defect: the artifact scan defaulted to `<repo>/runs` for the repo roots only,
# while the loops run inside `infra-worktree` sandboxes — so a window in which a real loop
# ran reported `tickets_seen: 0` and every candidate its artifacts would feed was invisible.
# Three dreams in a row wrote ZERO fix_lessons for exactly this reason, and `tickets_seen: 0`
# read as "no tickets" when it meant "did not look".
#
# First, on the baseline fixture (a repo root that is NOT a git repo): the scan says what it
# looked at, and a failed `git worktree list` degrades instead of taking the pass down.
ck "the scan names its sources"    "$(jf "$B" "d['artifact_sources'][0]")" "$FX/repo/runs"
ck "one source, not a duplicate"   "$(jf "$B" "len(d['artifact_sources'])")" "1"
ck "a non-git repo is not fatal"   "$(jf "$B" "any(x['reason'] == 'git_worktree_list_failed' for x in d['artifact_sources_skipped'])")" "True"
ck "and it names the repo"         "$(jf "$B" "[x['path'] for x in d['artifact_sources_skipped'] if x['reason'] == 'git_worktree_list_failed'][0]")" "$FX/repo"
ck "every ticket carries a source" "$(jf "$B" "all(t['source_dir'] for t in d['tickets'])")" "True"
ck "the source is the dir scanned" "$(jf "$B" "d['tickets'][0]['source_dir']")" "repo/runs"

SRCWT="$HERE/fixtures/dream-worktree"
WT="$TMP/wt"
if [ ! -d "$SRCWT" ]; then
  echo "  [FAIL] worktree fixture missing: $SRCWT"; fail=$((fail+1))
elif ! command -v git >/dev/null 2>&1; then
  echo "  worktree block: SKIPPED (git not on PATH)"
else
WTSUM_BEFORE="$(sum_dir "$SRCWT")"
mkdir -p "$WT"; cp -R "$SRCWT/." "$WT/"
# a REAL git repo with a REAL worktree: the porcelain output is parsed exactly as git emits
# it (unsorted, resolved paths, `prunable` on a deleted worktree), not as a stub imagines it.
G(){ git -c user.email=fixture@example.invalid -c user.name=fixture -c commit.gpgsign=false "$@"; }
(
  set -e
  G -c init.defaultBranch=main init -q "$WT/repo"
  # seed.txt ONLY: loop artifacts are untracked working files, exactly as they are in a real
  # sandbox — and committing runs/ would put a copy of it in every worktree checkout.
  G -C "$WT/repo" add seed.txt
  G -C "$WT/repo" commit -q -m "loops dream-worktree fixture"
  G -C "$WT/repo" worktree add -q -b loops-fx-live "$WT/live-worktree"
  G -C "$WT/repo" worktree add -q -b loops-fx-gone "$WT/gone-worktree"
) >"$TMP/git-setup.log" 2>&1
cp -R "$WT/sandbox-tasks" "$WT/live-worktree/runs" 2>>"$TMP/git-setup.log"
rm -rf "$WT/gone-worktree"          # still LISTED by git, but gone from disk
ln -s "$WT/repo" "$WT/repo-link"    # git reports the RESOLVED path; we pass the symlink
python3 - "$WT" <<'PY'
import calendar, os, sys
t = calendar.timegm((2026, 3, 15, 12, 0, 0, 0, 0, 0))   # same instant as fixtures/dream
for dp, _dn, fn in os.walk(sys.argv[1]):
    if os.sep + "runs" not in dp + os.sep:
        continue                                        # leave git's own files alone
    for n in fn:
        os.utime(os.path.join(dp, n), (t, t))
PY
if [ ! -d "$WT/live-worktree/runs/PROJ-9202" ]; then
  echo "  [FAIL] worktree setup failed (git init / worktree add):"
  sed 's/^/      /' "$TMP/git-setup.log" | tail -5; fail=$((fail+1))
else
HARVW(){ python3 "$H" --projects-root "$TMP/no-projects-here" --repo "$WT/repo-link" \
                      --all-projects --no-brain --now "$NOW" --since 7d "$@"; }
HARVW --out "$TMP/w1.json" >/dev/null 2>&1; rc=$?
W="$TMP/w1.json"

# ── FIX 1: the sandbox is scanned, exactly once ───────────────────────────
ck "worktree pass exits 0"         "$rc" "0"
ck "THE FIX: the sandbox ticket"   "$(jf "$W" "any(t['key'] == 'PROJ-9202' for t in d['tickets'])")" "True"
ck "the repo-root ticket survives" "$(jf "$W" "any(t['key'] == 'PROJ-9201' for t in d['tickets'])")" "True"
ck "three tickets, no double-count" "$(jf "$W" "d['tickets_seen']")" "3"
ck "the sandbox is a named source" "$(jf "$W" "any(p.endswith('/live-worktree/runs') for p in d['artifact_sources'])")" "True"
ck "the MAIN worktree is deduped"  "$(jf "$W" "len(d['artifact_sources'])")" "2"
ck "deduped by realpath, not text" "$(jf "$W" "len(d['artifact_sources']) == len(set(os.path.realpath(p) for p in d['artifact_sources']))")" "True"
ck "and the trap was really live"  "$(jf "$W" "any(p != os.path.realpath(p) for p in d['artifact_sources'])")" "True"
ck "sources are sorted"            "$(jf "$W" "d['artifact_sources'] == sorted(d['artifact_sources'])")" "True"
ck "a pruned worktree is skipped"  "$(jf "$W" "any(x['reason'] == 'worktree_missing' and x['path'].endswith('/gone-worktree') for x in d['artifact_sources_skipped'])")" "True"
ck "skips are sorted too"          "$(jf "$W" "[x['path'] for x in d['artifact_sources_skipped']] == sorted(x['path'] for x in d['artifact_sources_skipped'])")" "True"
ck "provenance on the ticket"      "$(jf "$W" "[t['source_dir'] for t in d['tickets'] if t['key'] == 'PROJ-9202'][0].endswith('/live-worktree/runs')")" "True"
HARVW --out "$TMP/w2.json" >/dev/null 2>&1
ck "a worktree pass is deterministic" "$(cmp -s "$W" "$TMP/w2.json" && echo same || echo differs)" "same"

# ── FIX 2: prose is EVIDENCE — bounded, de-drafted, and counted ───────────
ck "a prose-only dir is harvested" "$(jf "$W" "any(t['key'] == 'PROJ-9203' for t in d['tickets'])")" "True"
ck "plan.md is listed as artifact" "$(jf "$W" "[t['artifacts'] for t in d['tickets'] if t['key'] == 'PROJ-9203'][0]")" "['plan.md']"
ck "the FINAL body was taken"      "$(grep -c FINALBODYMARKER "$W")" "1"
ck "plan.md rides along"           "$(grep -c PLANMARKER "$W")" "1"
ck "the plan-only dir's prose"     "$(grep -c PLANONLYMARKER "$W")" "1"
for m in PLAINBODYMARKER INITIALBODYMARKER TAILCUTMARKER IMAGELINEMARKER BASE64BODYMARKER \
         GENERATEDWITHMARKER COAUTHORMARKER SESSIONLINKMARKER TRAILERSESSIONMARKER; do
  ck "prose drops: $m"             "$(grep -c "$m" "$W")" "0"
done
ck "two prose files per ticket max" "$(jf "$W" "max(len(t['prose_evidence']) for t in d['tickets'])")" "2"
ck "and the final one comes first" "$(jf "$W" "[p['file'] for t in d['tickets'] if t['key'] == 'PROJ-9202' for p in t['prose_evidence']]")" "['pr-body.final.md', 'plan.md']"
# against the LITERAL 2000, not against d['caps'] — a cap that only agrees with itself is
# not a bound, and raising the cap must fail this check rather than move it.
ck "every prose file is capped"    "$(jf "$W" "all(p['chars'] <= 2000 for t in d['tickets'] for p in t['prose_evidence'])")" "True"
ck "and the cap is declared"       "$(jf "$W" "d['caps']['prose_file_chars']")" "2000"
ck "the cap is COUNTED, per file"  "$(jf "$W" "d['filtered']['prose_chars_dropped']['PROJ-9202/pr-body.final.md'] > 1000")" "True"
ck "and in total"                  "$(jf "$W" "d['filtered']['prose_chars_dropped_total'] > 1000")" "True"
ck "unused drafts are counted"     "$(jf "$W" "d['filtered']['prose_files_skipped_total']")" "2"
ck "stripped boilerplate counted"  "$(jf "$W" "d['filtered']['prose_boilerplate_lines_dropped_total'] >= 6")" "True"
ck "prose stays UNDER the budget"  "$(jf "$W" "sum(p['chars'] for t in d['tickets'] for p in t['prose_evidence']) <= 3 * d['caps']['prose_file_chars']")" "True"
# the zero-LLM line: prose feeds the agent's judgment, it never becomes a candidate by itself
ck "prose synthesises NO lesson"   "$(jf "$W" "d['candidate_counts']['fix_lessons']")" "0"
ck "nor an expectation"            "$(jf "$W" "d['candidate_counts']['expectations']")" "0"

# ── the byte budget thins prose before it drops whole sessions, and says so ─
HARVW --max-bytes 3000 --out "$TMP/wb.json" >/dev/null 2>&1
ck "tight budget thins the prose"  "$(jf "$TMP/wb.json" "d['dropped']['prose_thinned']")" "True"
ck "thinned, never zeroed"         "$(jf "$TMP/wb.json" "all(len(p['text']) > 0 for t in d['tickets'] for p in t['prose_evidence'])")" "True"
ck "the thinning stays counted"    "$(jf "$TMP/wb.json" "d['filtered']['prose_chars_dropped_total'] > $(jf "$W" "d['filtered']['prose_chars_dropped_total']")")" "True"
ck "tickets still survive it"      "$(jf "$TMP/wb.json" "d['tickets_seen']")" "3"

# ── --tasks-dir still overrides the whole default ─────────────────────────
HARVW --tasks-dir "$WT/repo/runs" --out "$TMP/wt-only.json" >/dev/null 2>&1
ck "--tasks-dir overrides entirely" "$(jf "$TMP/wt-only.json" "d['artifact_sources'][0]")" "$WT/repo/runs"
ck "…to exactly what was asked for" "$(jf "$TMP/wt-only.json" "len(d['artifact_sources'])")" "1"
ck "so the sandbox is NOT scanned" "$(jf "$TMP/wt-only.json" "any(t['key'] == 'PROJ-9202' for t in d['tickets'])")" "False"
ck "and only its ticket is seen"   "$(jf "$TMP/wt-only.json" "d['tickets_seen']")" "1"

# ── no git binary at all: degrade to the repo roots, never crash ──────────
mkdir -p "$TMP/emptybin"
PATH="$TMP/emptybin" "$(command -v python3)" "$H" --projects-root "$TMP/no-projects-here" \
     --repo "$WT/repo-link" --all-projects --no-brain --now "$NOW" --since 7d \
     --out "$TMP/wnogit.json" >/dev/null 2>&1; rc=$?
ck "a missing git exits 0"         "$rc" "0"
ck "the repo root is still scanned" "$(jf "$TMP/wnogit.json" "len(d['artifact_sources'])")" "1"
ck "and the sandbox is honestly lost" "$(jf "$TMP/wnogit.json" "any(t['key'] == 'PROJ-9202' for t in d['tickets'])")" "False"
ck "the failure is named, not hidden" "$(jf "$TMP/wnogit.json" "any(x['reason'] == 'git_worktree_list_failed' for x in d['artifact_sources_skipped'])")" "True"
fi
ck "worktree fixture untouched"    "$(sum_dir "$SRCWT")" "$WTSUM_BEFORE"
fi

# ── read-only discipline: the TRACKED fixture is never written to ─────────
ck "tracked fixture untouched"     "$(sum_dir "$SRCFX")" "$FXSUM_BEFORE"

echo "  dream_harvest: $pass passed, $fail failed"
[ "$fail" = 0 ]
