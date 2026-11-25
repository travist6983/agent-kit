#!/usr/bin/env bash
# Regression: the R8 meta-monitor. meta_monitor.py reads loop-health metrics through the kb CLI,
# compares prod_regressed_rate, escalation_rate and repeat_failure_by_area against loop-limits.json
# r8_meta_monitor, and decides trip / no-trip / insufficient-data. It is report-only unless the limits
# say trip_mode armed with a named armed_by; then a trip turns the R9 pause ON through `kb pause --on
# --by meta-monitor`, and it never unpauses. Offline: a fake kb records every call; no brain, no host.
#
# Usage: bash .claude/loops/tests/test_meta_monitor.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
MM="$ROOT/.claude/skills/dev-loop-defect/scripts/meta_monitor.py"
V="$ROOT/contracts/validate.py"
C="$ROOT/contracts/dev-loop-defect.meta-monitor.json"
KBPY="$ROOT/.claude/skills/qa-kb/scripts/kb.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0
ok()  { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL  $1"; echo "        $2"; FAIL=$((FAIL+1)); }
unset KB_ALLOW_UNSHARED AGENT_KIT_OVERLAY FAKE_PAUSED FAKE_PAUSE_FAIL FAKE_METRICS_FAIL FAKE_NO_BREAKDOWN

CALLS="$TMP/calls.jsonl"
ALLCALLS="$TMP/all-calls.jsonl"
: > "$ALLCALLS"
cat > "$TMP/fake_kb.py" <<PY
import json, os, sys
a = sys.argv[1:]
for f in ("$CALLS", "$ALLCALLS"):
    with open(f, "a") as fh:
        fh.write(json.dumps(a) + "\n")
def out(d, rc=0):
    print(json.dumps(d)); raise SystemExit(rc)
if a[:2] == ["metrics", "--breakdown"]:
    if os.environ.get("FAKE_METRICS_FAIL") == "1":
        out({"ok": False, "error": "fake brain down"}, 1)
    if os.environ.get("FAKE_NO_BREAKDOWN") == "1":
        out({"ok": True, "action": "metrics_summary", "summary": []})
    rows = json.load(open(os.environ["FAKE_BREAKDOWN"]))
    out({"ok": True, "action": "metrics_breakdown", "breakdown": rows})
if a[:2] == ["pause", "--status"]:
    out({"ok": True, "action": "status", "paused": os.environ.get("FAKE_PAUSED") == "1"})
if a[:2] == ["pause", "--on"]:
    if os.environ.get("FAKE_PAUSE_FAIL") == "1":
        out({"ok": False, "error": "fake write refused"}, 1)
    out({"ok": True, "action": "pause_set", "paused": True, "receipt": {"id": 41, "verb": "pause"}})
raise SystemExit(2)
PY
export KB_CLI="$TMP/fake_kb.py"

# limits <path> [key=json ...]   writes a limits file with r8_meta_monitor (shipped values + overrides)
limits() { python3 - "$@" <<'PY'
import json, sys
sec = {"window_days": 14, "min_loop_outcomes": 10, "escalation_rate_trip": 0.5, "min_prod_verdicts": 5,
       "prod_regressed_rate_trip": 0.2, "area_escalations_trip": 3, "trip_mode": "report-only",
       "armed_by": "", "rule": "test"}
doc = {"r8_meta_monitor": sec}
for kv in sys.argv[2:]:
    k, v = kv.split("=", 1)
    if k == "DROP":
        doc = {}
        continue
    sec[k] = json.loads(v)
json.dump(doc, open(sys.argv[1], "w"))
PY
}
# rows <path> <metric:area:outcome:n> ...
rows() { python3 - "$@" <<'PY'
import json, sys
out = []
for spec in sys.argv[2:]:
    m, a, o, n = spec.split(":")
    out.append({"metric": m, "area": a, "outcome": o, "n": int(n), "sum": int(n)})
json.dump(out, open(sys.argv[1], "w"))
PY
}
REPORT="$TMP/report.json"; limits "$REPORT"
ARMED="$TMP/armed.json"; limits "$ARMED" trip_mode='"armed"' armed_by='"a-human"'

# mm <limits> <breakdown-fixture> [args...]  → $OUT (stdout), $RC; calls.jsonl reset per run
mm() { local l="$1" f="$2"; shift 2; : > "$CALLS"
  OUT=$(FAKE_BREAKDOWN="$f" python3 "$MM" --limits "$l" "$@" 2>"$TMP/stderr"); RC=$?; }
get() { echo "$OUT" | python3 -c "import json,sys;d=json.loads(sys.stdin.read().strip().splitlines()[-1]);print(eval(sys.argv[1],{'d':d}))" "$1" 2>/dev/null; }
sig() { get "[s['status'] for s in d['signals'] if s['signal']=='$1']"; }
ncalls() { grep -c -- "$1" "$CALLS" 2>/dev/null || true; }

# ── (a) healthy window ─────────────────────────────────────────────────────────────────────
F="$TMP/healthy.json"
rows "$F" loop_outcome::escalated:2 loop_outcome::shipped:18 escalation:invoices::1 escalation:members::1 \
  prod_outcome::prod-confirmed:9 prod_outcome::prod-regressed:1
mm "$ARMED" "$F"
[ "$RC" = 0 ] && [ "$(get "d['decision']")" = no-trip ] && [ "$(get "d['action']")" = reported ] \
  && [ "$(ncalls '"pause"')" = 0 ] && ok "(a) healthy window: no-trip, reported, no pause call" \
  || bad "(a) healthy" "rc=$RC $OUT"

# ── (b) escalation_rate at the threshold trips, just under does not ────────────────────────
F="$TMP/esc-at.json"; rows "$F" loop_outcome:::10 escalation:a::1 escalation:b::1 escalation:c::1 escalation:d::1 escalation:e::1
mm "$REPORT" "$F"
[ "$(sig escalation_rate)" = "['breached']" ] && [ "$(get "d['decision']")" = trip ] \
  && [ "$(get "'escalation_rate' in d['breaches']")" = True ] && ok "(b) escalation_rate 5/10 >= 0.5 trips" \
  || bad "(b) escalation at threshold" "$OUT"
F="$TMP/esc-under.json"; rows "$F" loop_outcome:::10 escalation:a::1 escalation:b::1 escalation:c::1 escalation:d::1
mm "$REPORT" "$F"
[ "$(sig escalation_rate)" = "['ok']" ] && [ "$(get "d['decision']")" = no-trip ] \
  && ok "(b) escalation_rate 4/10 stays quiet" || bad "(b) escalation under" "$OUT"

# ── (c) prod_regressed_rate, inconclusive excluded from both sides ─────────────────────────
F="$TMP/prod.json"; rows "$F" prod_outcome::prod-confirmed:4 prod_outcome::prod-regressed:1 prod_outcome::inconclusive:20
mm "$REPORT" "$F"
[ "$(sig prod_regressed_rate)" = "['breached']" ] \
  && [ "$(get "[(s['numerator'],s['denominator']) for s in d['signals'] if s['signal']=='prod_regressed_rate']")" = "[(1, 5)]" ] \
  && [ "$(get "d['decision']")" = trip ] && ok "(c) prod_regressed_rate 1/5 >= 0.2 trips; inconclusive excluded" \
  || bad "(c) prod regressed" "$OUT"
F="$TMP/prod-under.json"; rows "$F" prod_outcome::prod-confirmed:5 prod_outcome::prod-regressed:1 prod_outcome::inconclusive:1
mm "$REPORT" "$F"
[ "$(sig prod_regressed_rate)" = "['ok']" ] && ok "(c) prod_regressed_rate 1/6 stays quiet" || bad "(c) prod under" "$OUT"
F="$TMP/prod-few.json"; rows "$F" prod_outcome::prod-confirmed:2 prod_outcome::prod-regressed:2 prod_outcome::inconclusive:9
mm "$REPORT" "$F"
[ "$(sig prod_regressed_rate)" = "['insufficient-data']" ] && ok "(c) 4 decisive verdicts < min_prod_verdicts: insufficient (inconclusive never counts)" \
  || bad "(c) prod few" "$OUT"

# ── (d) repeat failure by area ─────────────────────────────────────────────────────────────
F="$TMP/area.json"; rows "$F" escalation:invoices::3 escalation:members::2 escalation:::7
mm "$REPORT" "$F"
[ "$(sig repeat_failure_by_area)" = "['breached']" ] \
  && [ "$(get "d['breaches']")" = "['repeat_failure_by_area:invoices']" ] \
  && [ "$(get "any('unattributed' in w for w in d['warnings'])")" = True ] \
  && ok "(d) one area at area_escalations_trip trips and is named; unattributed reported" \
  || bad "(d) area" "$OUT"
F="$TMP/area-none.json"; rows "$F" escalation:::9
mm "$REPORT" "$F"
[ "$(sig repeat_failure_by_area)" = "['insufficient-data']" ] && [ "$(get "d['decision']")" = insufficient-data ] \
  && ok "(d) escalations with no area never trip an area" || bad "(d) unattributed" "$OUT"
F="$TMP/area-under.json"; rows "$F" escalation:invoices::2 escalation:members::1
mm "$REPORT" "$F"
[ "$(get "[(s['status'],s.get('area')) for s in d['signals'] if s['signal']=='repeat_failure_by_area']")" = "[('ok', 'invoices')]" ] \
  && ok "(d) 2 in the worst area stays ok and names it" || bad "(d) area under" "$OUT"

# ── (e) insufficient data, and one ok + two insufficient ───────────────────────────────────
F="$TMP/thin.json"; rows "$F" loop_outcome:::3 prod_outcome::prod-confirmed:1
mm "$REPORT" "$F"
[ "$(get "d['decision']")" = insufficient-data ] && [ "$RC" = 0 ] && ok "(e) below every minimum: insufficient-data" \
  || bad "(e) insufficient" "$OUT"
F="$TMP/one-ok.json"; rows "$F" loop_outcome:::12 prod_outcome::prod-confirmed:1
mm "$REPORT" "$F"
[ "$(get "d['decision']")" = no-trip ] && ok "(e) one ok signal + two insufficient: no-trip" || bad "(e) one ok" "$OUT"

# ── (f) more escalations than loop outcomes ────────────────────────────────────────────────
F="$TMP/under-recorded.json"; rows "$F" loop_outcome:::10 escalation:a::2 escalation:b::2 escalation:c::2 escalation:d::2 escalation:e::2 escalation:f::2
mm "$REPORT" "$F"
[ "$(sig escalation_rate)" = "['insufficient-data']" ] && [ "$(get "'escalation_rate' in d['breaches']")" = False ] \
  && [ "$(get "any('under-recorded' in w for w in d['warnings'])")" = True ] \
  && ok "(f) escalations > loop outcomes: a warning, no breach from that signal" || bad "(f) under-recorded" "$OUT"

# ── (g) report-only limits + breach ────────────────────────────────────────────────────────
BREACH="$TMP/breach.json"
rows "$BREACH" loop_outcome:::12 escalation:invoices::4 escalation:members::2 escalation:::1 \
  prod_outcome::prod-confirmed:6 prod_outcome::inconclusive:2
mm "$REPORT" "$BREACH"
[ "$RC" = 0 ] && [ "$(get "d['decision']")" = trip ] && [ "$(get "d['armed']")" = False ] \
  && [ "$(get "d['action']")" = reported ] && [ "$(get "d['report_only_reason']")" = trip-mode-report-only ] \
  && [ "$(ncalls '"--on"')" = 0 ] && [ "$(ncalls '"pause"')" = 0 ] \
  && ok "(g) report-only + breach: trip, armed false, reported, no pause call" || bad "(g) report-only" "rc=$RC $OUT"

# ── (h) armed + breach → exactly one pause --on, signed meta-monitor ───────────────────────
mm "$ARMED" "$BREACH"
REASON=$(python3 -c "
import json,sys
for l in open(sys.argv[1]):
    a=json.loads(l)
    if a[:2]==['pause','--on']: print(a[a.index('--reason')+1] if a[a.index('--by')+1]=='meta-monitor' else 'UNSIGNED')" "$CALLS")
[ "$RC" = 0 ] && [ "$(get "d['action']")" = paused ] && [ "$(get "d['armed']")" = True ] \
  && [ "$(ncalls '"--on"')" = 1 ] && echo "$REASON" | grep -q "escalation_rate" \
  && echo "$REASON" | grep -q "repeat_failure_by_area:invoices" && echo "$REASON" | grep -q "a-human" \
  && [ "$(get "d['pause']['receipt']['id']")" = 41 ] \
  && ok "(h) armed + breach: one pause --on --by meta-monitor naming the breaches, action paused" \
  || bad "(h) armed trip" "rc=$RC reason=$REASON $OUT"

# ── (i) armed + already paused ─────────────────────────────────────────────────────────────
FAKE_PAUSED=1 mm "$ARMED" "$BREACH"
[ "$RC" = 0 ] && [ "$(get "d['action']")" = already-paused ] && [ "$(ncalls '"--on"')" = 0 ] \
  && [ "$(get "d['pause']")" = None ] && ok "(i) already paused: no second pause, reason left alone" \
  || bad "(i) already paused" "rc=$RC $OUT"

# ── (j) armed + the pause write fails ──────────────────────────────────────────────────────
FAKE_PAUSE_FAIL=1 mm "$ARMED" "$BREACH"
[ "$RC" = 1 ] && [ "$(get "d['action']")" = pause-failed ] && ok "(j) pause write fails: exit 1, pause-failed" \
  || bad "(j) pause failed" "rc=$RC $OUT"

# ── (k) armed + no-trip ────────────────────────────────────────────────────────────────────
mm "$ARMED" "$TMP/healthy.json"
[ "$(ncalls '"pause"')" = 0 ] && ok "(k) armed + no-trip: no pause call of any kind" || bad "(k) armed no-trip" "$(cat "$CALLS")"

# ── (l) armed + --report-only ──────────────────────────────────────────────────────────────
mm "$ARMED" "$BREACH" --report-only
[ "$(ncalls '"pause"')" = 0 ] && [ "$(get "d['report_only_reason']")" = flag-report-only ] \
  && [ "$(get "d['armed']")" = False ] && [ "$(get "d['decision']")" = trip ] \
  && ok "(l) armed + --report-only: no pause call, flag-report-only" || bad "(l) report-only flag" "$OUT"

# ── (m) armed + --metrics-file ─────────────────────────────────────────────────────────────
python3 -c "import json,sys;json.dump({'ok':True,'breakdown':json.load(open(sys.argv[1]))},open(sys.argv[2],'w'))" "$BREACH" "$TMP/mfile.json"
mm "$ARMED" /nonexistent --metrics-file "$TMP/mfile.json"
[ "$RC" = 0 ] && [ ! -s "$CALLS" ] && [ "$(get "d['metrics_source']")" = file ] \
  && [ "$(get "d['report_only_reason']")" = metrics-file ] && [ "$(get "d['action']")" = reported ] \
  && ok "(m) armed + --metrics-file: no kb call at all, report-only" || bad "(m) metrics-file" "rc=$RC calls=$(cat "$CALLS") $OUT"

# ── (n) unreadable metrics ─────────────────────────────────────────────────────────────────
rm -f "$TMP/n-out.json"
FAKE_METRICS_FAIL=1 mm "$ARMED" "$BREACH" --out "$TMP/n-out.json"
[ "$RC" = 3 ] && [ "$(ncalls '"pause"')" = 0 ] && [ ! -e "$TMP/n-out.json" ] \
  && ok "(n) kb metrics failure: exit 3, no pause, no --out" || bad "(n) metrics fail" "rc=$RC $OUT"
FAKE_NO_BREAKDOWN=1 mm "$ARMED" "$BREACH" --out "$TMP/n-out.json"
[ "$RC" = 3 ] && [ "$(ncalls '"pause"')" = 0 ] && [ ! -e "$TMP/n-out.json" ] \
  && ok "(n) a reply without breakdown (older kb.py): exit 3, no pause, no --out" || bad "(n) no breakdown" "rc=$RC $OUT"
KB_CLI="$TMP/missing_kb.py" mm "$ARMED" "$BREACH"
[ "$RC" = 3 ] && ok "(n) kb CLI missing: exit 3" || bad "(n) kb missing" "rc=$RC $OUT"

# ── (o) bad limits exit 2 before any kb call ───────────────────────────────────────────────
badlim() { local name="$1"; shift; limits "$TMP/bad.json" "$@"; mm "$TMP/bad.json" "$BREACH"
  [ "$RC" = 2 ] && [ ! -s "$CALLS" ] && ok "(o) $name: exit 2, no kb call" || bad "(o) $name" "rc=$RC calls=$(cat "$CALLS") $OUT"; }
badlim "section missing" DROP=1
badlim "bool threshold" escalation_rate_trip=true
badlim "rate 0" prod_regressed_rate_trip=0
badlim "rate 1.5" escalation_rate_trip=1.5
badlim "window_days 0" window_days=0
badlim "area_escalations_trip 1" area_escalations_trip=1
badlim "string number" min_loop_outcomes='"10"'
badlim "unknown trip_mode" trip_mode='"auto"'
badlim "armed with empty armed_by" trip_mode='"armed"' armed_by='"  "'
mm "$TMP/no-such-limits.json" "$BREACH"
[ "$RC" = 2 ] && [ ! -s "$CALLS" ] && ok "(o) unreadable limits file: exit 2" || bad "(o) unreadable limits" "rc=$RC"

# ── (p) never unpauses ─────────────────────────────────────────────────────────────────────
if grep -q -- '--off' "$ALLCALLS"; then bad "(p) no --off call in any case" "$(grep -- '--off' "$ALLCALLS")"
else ok "(p) no case produced a pause --off call"; fi
n=$(grep -c -- '--off' "$MM" || true)
[ "$n" = 0 ] && ok "(p) meta_monitor.py contains no --off" || bad "(p) --off in script" "count=$n"

# ── (q) --out is contract-valid; the bad sample fails ──────────────────────────────────────
rm -f "$TMP/q.json"
mm "$REPORT" "$BREACH" --out "$TMP/q.json"
[ "$RC" = 0 ] && python3 "$V" "$C" "$TMP/q.json" >/dev/null 2>&1 && ok "(q) --out validates against the contract" \
  || bad "(q) --out" "rc=$RC $(cat "$TMP/stderr")"
rm -f "$TMP/q2.json"
mm "$ARMED" "$BREACH" --out "$TMP/q2.json"
python3 "$V" "$C" "$TMP/q2.json" >/dev/null 2>&1 && ok "(q) an armed, paused --out validates" || bad "(q) armed --out" "$(cat "$TMP/stderr")"
python3 "$V" "$C" "$ROOT/contracts/samples/EXAMPLE-meta-monitor/meta-monitor.json" >/dev/null 2>&1 \
  && ok "(q) the good sample validates" || bad "(q) good sample" "fails"
python3 "$V" "$C" "$ROOT/contracts/samples/EXAMPLE-bad-meta-monitor/meta-monitor.json" >/dev/null 2>&1 \
  && bad "(q) the bad sample (paused while unarmed) must fail" "it passes" || ok "(q) the bad sample fails META-MON-A1"
python3 - "$TMP/q.json" "$TMP/tamper.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d["decision"] = "no-trip"; json.dump(d, open(sys.argv[2], "w"))
PY
python3 "$V" "$C" "$TMP/tamper.json" >/dev/null 2>&1 && bad "(q) no-trip with a breached signal must fail D1" "passes" \
  || ok "(q) no-trip with a breached signal fails META-MON-D1"

# ── (r) the tracked limits file as shipped ─────────────────────────────────────────────────
TRACKED="$ROOT/.claude/loops/loop-limits.json"
python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))['r8_meta_monitor']
assert d['trip_mode']=='report-only' and d['armed_by']=='', d" "$TRACKED" 2>/dev/null \
  && ok "(r) tracked loop-limits.json ships report-only with no armed_by" || bad "(r) tracked posture" "not report-only"
mm "$TRACKED" /nonexistent --metrics-file "$TMP/mfile.json"
[ "$RC" = 0 ] && [ "$(get "d['decision']")" = trip ] && [ "$(get "d['action']")" = reported ] \
  && ok "(r) meta_monitor.py accepts the tracked limits" || bad "(r) tracked accepted" "rc=$RC $OUT"
: > "$CALLS"
OUT=$(FAKE_BREAKDOWN="$BREACH" python3 "$MM" 2>/dev/null); RC=$?
[ "$RC" = 0 ] && [ "$(get "d['armed']")" = False ] && [ "$(ncalls '"pause"')" = 0 ] \
  && ok "(r) default limits path resolves to the shipped report-only posture" || bad "(r) default path" "rc=$RC $OUT"

# ── (s) static seam checks (the --breakdown SQL itself runs only in the brain-backed harness) ─
grep -qE '^\s*(import|from)\s+psycopg2' "$MM" && bad "(s) no DB driver in the monitor" "imports psycopg2" \
  || ok "(s) meta_monitor.py imports no database driver"
grep -qiE '\b(SELECT|INSERT|UPDATE|DELETE)\b.*\b(FROM|INTO|SET)\b' "$MM" && bad "(s) no SQL in the monitor" "SQL found" \
  || ok "(s) meta_monitor.py contains no SQL"
grep -q -- '"--breakdown"' "$KBPY" && grep -q 'metrics_breakdown' "$KBPY" \
  && ok "(s) kb.py exposes metrics --breakdown" || bad "(s) kb.py breakdown" "missing"
grep -q -- '--breakdown' "$ROOT/.claude/skills/qa-kb/SKILL.md" && ok "(s) qa-kb SKILL.md documents --breakdown" \
  || bad "(s) SKILL.md breakdown" "missing"
grep -q 'metrics --breakdown' "$ROOT/.claude/skills/qa-kb/scripts/harness.py" \
  && ok "(s) harness.py carries the brain-backed --breakdown check" || bad "(s) harness check" "missing"

echo "  meta-monitor: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
