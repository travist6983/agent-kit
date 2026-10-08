#!/usr/bin/env bash
# Regression: qa-kb DURABILITY + provable capture (brain-spec 1b/1c/1d).
#   1b  a knowledge write is NEVER silently dropped — not by a down embedder, not by a dead DB
#   1c  what a run wrote is provable from the ledger, without the agent remembering to say so
#   1d  a slice written as "Design System" is findable as design-system
# and the enforcement half: contracts/validate.py --receipts turns an artifact's
# "app_map_writes": [...] claim from recollection into something falsifiable (audit L5).
#
# Hits the REAL shared brain, marked source='test-kbdur' / ticket='KBDUR-0000', and deletes
# every marked row on the way in and out. Never touches an unmarked row; never pauses, decays
# or forgets. The outbox is redirected to a temp file so the real spool is never written.
#
# Runs against the LIVE embedder, whatever state it is in. When it is down (the 429) the write
# must STILL land as embed_status='pending' — that is the property under test, so no check here
# needs a stub embedder, and none of them stamp stub provenance on a real row.

# Shared brain lock: refuse rather than corrupt a concurrent run (see lib/brain_lock.sh).
# Nests quietly under run_all.sh, which already holds it.
. "$(cd "$(dirname "$0")" && pwd)/lib/brain_lock.sh"
brain_lock_acquire "test_kb_durability" || exit 3
trap brain_lock_release EXIT

SK="$(cd "$(dirname "$0")/../../skills" && pwd)"; export SK
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
KBPY="$SK/qa-kb/scripts/kb.py"
VAL="$ROOT/contracts/validate.py"
SRC=test-kbdur;      export SRC
TICKET=KBDUR-0000;   export TICKET
AREA_RAW="Test KBDur Area"
AREA_NORM=test-kbdur-area
MARKER=kbdur-marker-zzz
DEAD="host=127.0.0.1 port=1 dbname=nope connect_timeout=2"
TMP="$(mktemp -d)"
export KB_OUTBOX="$TMP/outbox.jsonl"     # never the real BRAIN_DIR spool
pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); else echo "  [FAIL] $1 — got '$2' want '$3'"; fail=$((fail+1)); fi; }
ckn(){ if [ "$2" != "$3" ]; then pass=$((pass+1)); else echo "  [FAIL] $1 — got '$2', wanted anything else"; fail=$((fail+1)); fi; }
# read one value out of a kb JSON response; empty string on any miss, never a traceback
jq(){ python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    v = ($1)
except Exception:
    v = ''
print('' if v is None else v)
"; }
spool(){ [ -f "$KB_OUTBOX" ] && wc -l < "$KB_OUTBOX" | tr -d ' ' || echo 0; }

# Cleanup connects the SAME way kb.py's subprocesses do (kb.resolve_dsn() + kb.connect(), so
# every auth.mode applies) — never a raw local-default psycopg2 connection (QA-KB-C3). Every DELETE is marker-scoped. Runs BEFORE, on
# EXIT, and once explicitly at the end where its result is ASSERTED — a self-cleaning test
# against a shared live brain that quietly fails to clean is how residue gets there, so the
# failure is printed, never swallowed, and `residue` makes it falsifiable.
_kbsql(){ python3 - "$1" <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["SK"], "qa-kb", "scripts"))
import kb as kbmod
src, tkt, mode = os.environ["SRC"], os.environ["TICKET"], sys.argv[1]
STORES = ("app_map", "expectations", "flakiness", "fix_lessons")
c = kbmod.connect(kbmod.resolve_dsn())
try:
    with c.cursor() as cur:
        if mode == "clean":
            for t in STORES:                       # metrics has NO source column — do not add one
                cur.execute("DELETE FROM %s WHERE source=%%s" % t, (src,))
            cur.execute("DELETE FROM metrics   WHERE loop_id=%s OR ticket=%s", (src, tkt))
            cur.execute("DELETE FROM kb_writes WHERE source=%s OR ticket=%s", (src, tkt))
            cur.execute("DELETE FROM kb_state  WHERE key LIKE %s", (src + "%",))
            c.commit()
            print("clean-ok")
        else:
            n = 0
            for t in STORES:
                cur.execute("SELECT count(*) AS c FROM %s WHERE source=%%s" % t, (src,))
                n += cur.fetchone()["c"]
            cur.execute("SELECT count(*) AS c FROM metrics WHERE loop_id=%s OR ticket=%s", (src, tkt))
            n += cur.fetchone()["c"]
            cur.execute("SELECT count(*) AS c FROM kb_writes WHERE source=%s OR ticket=%s", (src, tkt))
            n += cur.fetchone()["c"]
            cur.execute("SELECT count(*) AS c FROM kb_state WHERE key LIKE %s", (src + "%",))
            n += cur.fetchone()["c"]
            print(n)
finally:
    c.close()
PY
}
clean(){ out=$(_kbsql clean 2>&1) || { echo "  [WARN] cleanup FAILED — marked rows may survive in the shared brain:"; echo "$out" | tail -3; return 1; }; }
residue(){ _kbsql count 2>/dev/null || echo "?"; }

# guard: skip cleanly if the brain isn't reachable, so the suite still runs elsewhere
if ! python3 "$KBPY" pause --status >/dev/null 2>&1; then
  echo "  kb_durability: SKIPPED (brain unreachable)"; rm -rf "$TMP"; exit 0
fi
clean; trap 'clean; rm -rf "$TMP"' EXIT

# ── 1b: a down embedder defers SEARCHABILITY, never the lesson ───────────────
O=$(python3 "$KBPY" write fix_lessons --area "$AREA_RAW" --source $SRC \
      --signature-tag kbdur-durability \
      --symptom "kbdur durability probe: the loop wrote a lesson while the embedder was unavailable" \
      --root-cause "embed-before-insert made the embedder a gate on knowledge" \
      --fix-shape "insert with embedding NULL + embed_status pending; kb reembed heals it"); rc=$?
ck "fix_lesson write exits 0"      "$rc" "0"
ck "fix_lesson write ok:true"      "$(echo "$O" | jq "d['ok']")" "True"
EMBST=$(echo "$O" | jq "d.get('embed_status')")
LID=$(echo "$O" | jq "d['row']['id']")
ckn "fix_lesson row got an id"     "$LID" ""
ck "row is durably stored"         "$(python3 "$KBPY" read fix_lessons --area $AREA_NORM | jq "d['count']>=1")" "True"
case "$EMBST" in
  pending)
    echo "  (embedder DOWN — exercising the degraded-but-durable path)"
    ck "pending row has no vector"   "$(echo "$O" | jq "d['row']['embedding_present']")" "False"
    ck "dedup skipped on pending"    "$(echo "$O" | jq "d.get('dedup_skipped')")" "True"
    ck "warning names the embedder"  "$(echo "$O" | jq "str(d.get('warning','')).startswith('embedder_unavailable')")" "True" ;;
  ok)
    echo "  (embedder UP — exercising the embedded path)"
    ck "embedded row has a vector"   "$(echo "$O" | jq "d['row']['embedding_present']")" "True"
    ck "embedding_model recorded"    "$(echo "$O" | jq "bool(d['row'].get('embedding_model'))")" "True"
    ck "searchable by paraphrase"    "$(python3 "$KBPY" search fix_lessons --k 1 \
        --query "a lesson was written while the embedding service was unavailable" \
        | jq "any(r.get('signature_tag')=='kbdur-durability' for r in d.get('results',[]))")" "True" ;;
  *) echo "  [FAIL] embed_status is neither pending nor ok — got '$EMBST'"; fail=$((fail+1)) ;;
esac

# ── 1b: reembed is a maintenance verb, never a gate and never a shredder ─────
O=$(python3 "$KBPY" reembed --store fix_lessons --filter-source $SRC --dry-run); rc=$?
ck "reembed --dry-run exits 0"     "$rc" "0"
ck "dry-run embeds nothing"        "$(echo "$O" | jq "d['embedded']")" "0"
ck "dry-run is marked"             "$(echo "$O" | jq "d['dry_run']")" "True"
ck "reembed never auto-merges"     "$(echo "$O" | jq "d['auto_merge']")" "False"
O=$(python3 "$KBPY" reembed --store fix_lessons --filter-source $SRC); rc=$?
ck "reembed exits 0 either way"    "$rc" "0"
ck "reembed reports honestly"      "$(echo "$O" | jq "(d['embedded']+d['failed'])==d['scanned']")" "True"
ck "the row survived reembed"      "$(python3 "$KBPY" read fix_lessons --id $LID | jq "d['count']")" "1"

# ── 1b: a dead DB queues a MUTATING write and nothing else ──────────────────
ck "spool starts empty"            "$(spool)" "0"
O=$(python3 "$KBPY" --db "$DEAD" write app_map --kind selector --area "$AREA_RAW" \
      --key kbdur-queued-1 --value "{\"selector\":\"[data-test=$MARKER-queued]\"}" \
      --source $SRC --ticket $TICKET); rc=$?
ck "dead-DB write exits 1"         "$rc" "1"
ck "dead-DB write ok:false"        "$(echo "$O" | jq "d['ok']")" "False"
ck "dead-DB write QUEUES"          "$(echo "$O" | jq "d.get('queued')")" "True"
ck "spool depth 1"                 "$(spool)" "1"
ck "outbox --status sees it"       "$(python3 "$KBPY" outbox --status | jq "d['count']")" "1"
python3 "$KBPY" --db "$DEAD" read app_map --area $AREA_NORM >/dev/null 2>&1
ck "non-mutating verb never queues" "$(spool)" "1"
python3 "$KBPY" write app_map --kind selector --area "$AREA_RAW" --key kbdur-nope \
      --source $SRC >/dev/null 2>&1
ck "semantic failure never queues"  "$(spool)" "1"
O=$(python3 "$KBPY" outbox --clear); rc=$?
ck "clear without --yes refuses"    "$rc" "1"
ck "refused clear kept the entry"   "$(spool)" "1"
python3 "$KBPY" outbox --clear --yes >/dev/null
ck "clear --yes empties the spool"  "$(spool)" "0"

# queue through the ENV DSN so the replayed argv carries no dead --db, then drain for real
KB_DATABASE_URL="$DEAD" python3 "$KBPY" write app_map --kind selector --area "$AREA_RAW" \
      --key kbdur-key-2 --value "{\"selector\":\"[data-test=$MARKER-2]\"}" \
      --source $SRC --ticket $TICKET >/dev/null 2>&1
ck "env-DSN outage queued"          "$(spool)" "1"
O=$(python3 "$KBPY" outbox --replay); rc=$?
ck "replay exits 0"                 "$rc" "0"
ck "replay landed the write"        "$(echo "$O" | jq "d['replayed']")" "1"
ck "replay drained the spool"       "$(echo "$O" | jq "d['remaining']")" "0"
ck "the deferred row IS in the brain" \
   "$(python3 "$KBPY" read app_map --kind selector --area $AREA_NORM --key kbdur-key-2 | jq "d['count']")" "1"

# ── 1c: automatic receipts — the ledger, not the agent's recollection ───────
O=$(python3 "$KBPY" write app_map --kind selector --area "$AREA_RAW" --key kbdur-key-1 \
      --value "{\"selector\":\"[data-test=$MARKER-1]\"}" --source $SRC --ticket $TICKET)
RID=$(echo "$O" | jq "d['row']['id']")
RVER=$(echo "$O" | jq "d['row']['version']")
ckn "write returns a receipt id"    "$(echo "$O" | jq "d['receipt']['id']")" ""
ck  "receipt names the store"       "$(echo "$O" | jq "d['receipt']['store']")" "app_map"
ck  "receipt names the row"         "$(echo "$O" | jq "d['receipt']['row_id']")" "$RID"
# no --ticket flag: the runner's environment must tag the write for free
LOOP_TICKET=$TICKET python3 "$KBPY" write app_map --kind route --area "$AREA_RAW" \
      --key /kbdur/from-env --value '{"route":"/kbdur/from-env"}' --source $SRC >/dev/null
O=$(python3 "$KBPY" receipt --ticket $TICKET)
ck "receipt finds the writes"       "$(echo "$O" | jq "d['counts_by_store'].get('app_map',0)>=3")" "True"
ck "receipt attributes the source"  "$(echo "$O" | jq "'$SRC' in d['sources']")" "True"
ck "LOOP_TICKET tagged the write"  "$(echo "$O" | jq "any(r['key' if 'key' in r else 'summary']=='/kbdur/from-env' for r in d['writes_by_store']['app_map'])")" "True"
ck "receipt --since filters"        "$(python3 "$KBPY" receipt --ticket $TICKET --since 7d | jq "d['count']>=3")" "True"

# ── 1d: confirm is freshness, not an edit ───────────────────────────────────
O=$(python3 "$KBPY" confirm app_map --id $RID --source $SRC --ticket $TICKET); rc=$?
ck "confirm exits 0"                "$rc" "0"
ck "confirm does NOT bump version"  "$(echo "$O" | jq "d['row']['version']")" "$RVER"
ck "confirm keeps the value"        "$(echo "$O" | jq "'$MARKER-1' in json.dumps(d['row']['value'])")" "True"
ckn "confirm is ledgered"           "$(echo "$O" | jq "d['receipt']['id']")" ""
ck "confirm on a missing row fails" "$(python3 "$KBPY" confirm app_map --id 999999999 --source $SRC | jq "d['ok']")" "False"

# ── 1d: normalization on write AND read ─────────────────────────────────────
ck "write normalized the area"      "$(python3 "$KBPY" read app_map --id $RID | jq "d['rows'][0]['area']")" "$AREA_NORM"
ck "read by normalized area"        "$(python3 "$KBPY" read app_map --area $AREA_NORM | jq "d['count']>=3")" "True"
ck "read by the RAW spelling too"   "$(python3 "$KBPY" read app_map --area "$AREA_RAW" | jq "d['count']>=3")" "True"
ck "read reports how it matched"    "$(python3 "$KBPY" read app_map --area "$AREA_RAW" | jq "bool(d['normalization']['area']['matched'])")" "True"
O=$(python3 "$KBPY" write app_map --kind kbdur-weird-kind --area "$AREA_RAW" --key kbdur-odd \
      --value '{"note":"an unknown kind must still be stored"}' --source $SRC --ticket $TICKET)
ck "unknown kind still WRITES"      "$(echo "$O" | jq "d['ok']")" "True"
ck "unknown kind warns"             "$(echo "$O" | jq "d.get('warning')")" "unknown_kind"

# ── 1d: search app_map — find a row without knowing its key ─────────────────
ck "search finds by value text"     "$(python3 "$KBPY" search app_map --query "$MARKER-1" | jq "d['count']")" "1"
ck "search finds by key fragment"   "$(python3 "$KBPY" search app_map --query kbdur-key | jq "d['count']>=2")" "True"
ck "search narrows by area"         "$(python3 "$KBPY" search app_map --query "$MARKER" --area $AREA_NORM | jq "d['count']>=2")" "True"
ck "search misses cleanly"          "$(python3 "$KBPY" search app_map --query kbdur-no-such-thing | jq "d['count']")" "0"

# ── 1c: history makes the router non-stateless, and never errors ────────────
O=$(python3 "$KBPY" history --ticket KBDUR-NEVER-SEEN); rc=$?
ck "cold history exits 0"           "$rc" "0"
ck "cold history is ok:true"        "$(echo "$O" | jq "d['ok']")" "True"
ck "cold history says cold"         "$(echo "$O" | jq "d['cold']")" "True"
ck "cold history has no attempts"   "$(echo "$O" | jq "d['attempt_count']")" "0"
python3 "$KBPY" metric --name loop_outcome --value 1 --unit count --ticket $TICKET \
      --loop-id $SRC --source $SRC --area "$AREA_RAW" \
      --metadata '{"outcome":"escalated"}' >/dev/null
O=$(python3 "$KBPY" history --ticket $TICKET)
ck "warm history is not cold"       "$(echo "$O" | jq "d['cold']")" "False"
ck "warm history counts attempts"   "$(echo "$O" | jq "d['attempt_count']>=1")" "True"
ck "warm history reads the outcome" "$(echo "$O" | jq "d['last_outcome']['outcome']")" "escalated"
ck "warm history lists app_map"     "$(echo "$O" | jq "'app_map' in d['writes_by_store']")" "True"
ck "warm history normalizes areas"  "$(echo "$O" | jq "'$AREA_NORM' in d['areas']")" "True"

# ── enforcement: validate.py --receipts (audit L5) ──────────────────────────
EV="$TMP/evid/$TICKET"; mkdir -p "$EV"
cat > "$EV/repro.json" <<JSON
{"status":"reproduced","surface":"presentation/DOM",
 "symptom_quote":"kbdur receipts probe","replay_spec":"replay-oracle.sh",
 "app_map_writes":[{"kind":"selector","area":"$AREA_NORM","key":"kbdur-key-1"},
                   {"kind":"selector","area":"$AREA_NORM","key":"kbdur-key-2"}],
 "brain_writes":{"ok":2,"failed":0,"queued":0}}
JSON
ck "the honest artifact validates"  "$(python3 "$VAL" "$ROOT/contracts/qa-repro.json" "$EV/repro.json" >/dev/null 2>&1; echo $?)" "0"
O=$(python3 "$VAL" --receipts $TICKET --evid-root "$TMP/evid"); rc=$?
ck "true claim -> exit 0"           "$rc" "0"
ck "true claim -> PASS"             "$(echo "$O" | head -1 | cut -d' ' -f1)" "PASS"
ck "confirms counted separately"    "$(echo "$O" | grep -c 'app_map-confirms')" "1"
# now lie: claim a third selector the brain never received
python3 - "$EV/repro.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["app_map_writes"].append({"kind": "selector", "area": "test-kbdur-area", "key": "kbdur-never-written"})
d["brain_writes"]["ok"] = 3
json.dump(d, open(p, "w"))
PY
O=$(python3 "$VAL" --receipts $TICKET --evid-root "$TMP/evid"); rc=$?
ck "false claim -> exit 1"          "$rc" "1"
ck "false claim names the key"      "$(echo "$O" | grep -c 'kbdur-never-written')" "1"
# The SAME missing key, but this time the artifact declares it was spooled to the outbox:
# durable on disk, honestly reported, not yet in the brain. That is a delayed write, not a
# lie, and the check must say so instead of accusing the run.
python3 - "$EV/repro.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["brain_writes"] = {"ok": 2, "failed": 0, "queued": 1}   # keeps kbdur-never-written claimed
json.dump(d, open(p, "w"))
PY
O=$(python3 "$VAL" --receipts $TICKET --evid-root "$TMP/evid"); rc=$?
ck "a DECLARED queue is not drift"  "$rc" "0"
ck "and it says how to land it"     "$(echo "$O" | grep -c 'kb outbox --replay')" "1"
# one queued write cannot explain TWO missing keys — that is drift again
python3 - "$EV/repro.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["app_map_writes"].append({"kind": "selector", "area": "test-kbdur-area", "key": "kbdur-also-never"})
json.dump(d, open(p, "w"))
PY
ck "queue explains only its own"    "$(python3 "$VAL" --receipts $TICKET --evid-root "$TMP/evid" >/dev/null 2>&1; echo $?)" "1"
O=$(KB_DATABASE_URL="$DEAD" python3 "$VAL" --receipts $TICKET --evid-root "$TMP/evid"); rc=$?
ck "dead brain -> exit 3, NOT 1"    "$rc" "3"
ck "dead brain -> UNDETERMINED"     "$(echo "$O" | head -1 | cut -d' ' -f1)" "UNDETERMINED"
J=$(python3 "$VAL" --receipts $TICKET --evid-root "$TMP/evid" --json)
ck "--json carries the verdict"     "$(echo "$J" | jq "d['status']")" "drift"
ck "--json carries the findings"    "$(echo "$J" | jq "any(f['status']=='drift' for f in d['findings'])")" "True"
ck "--json names the ledger"        "$(echo "$J" | jq "d['ledger']['counts_by_store']['app_map']>=3")" "True"
ck "unknown ticket -> exit 0"       "$(python3 "$VAL" --receipts KBDUR-NEVER-SEEN --evid-root "$TMP/evid" >/dev/null 2>&1; echo $?)" "0"

# ── the shared brain must be exactly as we found it ─────────────────────────
ckn "the run really wrote rows"     "$(residue)" "0"
clean
ck "cleanup leaves NO marked row"   "$(residue)" "0"

echo "  kb_durability: $pass passed, $fail failed"
[ "$fail" = 0 ]
