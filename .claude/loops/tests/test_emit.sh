#!/usr/bin/env bash
# Regression: contracts/emit.py — deterministic seam-file emission.
#
# What it must guarantee: a contract violation NEVER lands on disk (no half-written artifact
# for a downstream node to read), scalars are coerced from the schema so the string-vs-int
# rejection loop cannot happen, and timing/attempt are stamped without being asked.
#
# No DB, no network, no model calls. Scratch dir, self-cleaning.
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
E="$ROOT/contracts/emit.py"
C="$ROOT/contracts/qa-verify.json"
pass=0; fail=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

ok(){ if [ "$1" = "$2" ]; then pass=$((pass+1)); else
  echo "  [FAIL] $3 (got '$1' want '$2')"; fail=$((fail+1)); fi; }

# ---------- happy path -------------------------------------------------------------------
O="$TMP/verify.json"
python3 "$E" --contract "$C" --out "$O" \
  --set verdict=pass --set confidence=90 --set masking_suspected=false >/dev/null 2>&1
ok "$?" "0" "valid artifact writes"
ok "$([ -f "$O" ] && echo yes)" "yes" "file exists"

# scalars coerced from the SCHEMA, not guessed: confidence is an integer, not "90"
ok "$(python3 -c "import json;print(type(json.load(open('$O'))['confidence']).__name__)")" \
   "int" "confidence coerced to int"
ok "$(python3 -c "import json;print(type(json.load(open('$O'))['masking_suspected']).__name__)")" \
   "bool" "masking_suspected coerced to bool"

# timing stamped without being asked
ok "$(python3 -c "import json;print('timing' in json.load(open('$O')))")" "True" "timing auto-stamped"
ok "$(python3 -c "import json;print('started_at' in json.load(open('$O'))['timing'])")" "True" "timing.started_at present"

# ---------- the guarantee that matters: a violation never lands ----------------------------
O2="$TMP/bad.json"
python3 "$E" --contract "$C" --out "$O2" --set verdict=nonsense --set confidence=90 \
  --set masking_suspected=false >/dev/null 2>&1
ok "$?" "1" "bad enum exits 1"
ok "$([ -f "$O2" ] && echo yes || echo no)" "no" "bad enum wrote NOTHING"
ok "$([ -f "$O2.emit-tmp" ] && echo yes || echo no)" "no" "no temp file left behind"

O3="$TMP/extra.json"
python3 "$E" --contract "$C" --out "$O3" --set verdict=pass --set confidence=90 \
  --set masking_suspected=false --set bogus_key=1 >/dev/null 2>&1
ok "$?" "1" "unknown key exits 1 (additionalProperties:false)"
ok "$([ -f "$O3" ] && echo yes || echo no)" "no" "unknown key wrote NOTHING"

O4="$TMP/missing.json"
python3 "$E" --contract "$C" --out "$O4" --set verdict=pass >/dev/null 2>&1
ok "$?" "1" "missing required field exits 1"
ok "$([ -f "$O4" ] && echo yes || echo no)" "no" "missing required wrote NOTHING"

O5="$TMP/range.json"
python3 "$E" --contract "$C" --out "$O5" --set verdict=pass --set confidence=150 \
  --set masking_suspected=false >/dev/null 2>&1
ok "$?" "1" "out-of-range integer exits 1"

# an existing GOOD artifact must survive a failed re-emit (no truncation)
cp "$O" "$TMP/keep.json"
python3 "$E" --contract "$C" --out "$TMP/keep.json" --set verdict=nonsense >/dev/null 2>&1
ok "$(python3 -c "import json;print(json.load(open('$TMP/keep.json'))['verdict'])")" \
   "pass" "failed re-emit left the good artifact intact"

# ---------- --set-json for arrays/objects ---------------------------------------------------
O6="$TMP/mh.json"
python3 "$E" --contract "$C" --out "$O6" --set verdict=pass --set confidence=88 \
  --set masking_suspected=false \
  --set-json must_haves='[{"check":"replay","result":"pass"}]' >/dev/null 2>&1
ok "$?" "0" "--set-json array accepted"
ok "$(python3 -c "import json;print(len(json.load(open('$O6'))['must_haves']))")" "1" "array landed"

python3 "$E" --contract "$C" --out "$TMP/bj.json" --set verdict=pass --set confidence=88 \
  --set masking_suspected=false --set-json must_haves='[{' >/dev/null 2>&1
ok "$?" "2" "malformed --set-json exits 2 (usage)"

# ---------- attempt / retry_reason stamping ---------------------------------------------------
O7="$TMP/att.json"
python3 "$E" --contract "$C" --out "$O7" --set verdict=fail --set confidence=40 \
  --set masking_suspected=false --attempt 2 --retry-reason "flaky-store match" >/dev/null 2>&1
ok "$(python3 -c "import json;print(json.load(open('$O7'))['attempt'])")" "2" "attempt stamped"
ok "$(python3 -c "import json;print(json.load(open('$O7'))['retry_reason'])")" \
   "flaky-store match" "retry_reason stamped"

# ---------- --from-json + explicit timing -------------------------------------------------------
O8="$TMP/fj.json"
echo '{"verdict":"pass","confidence":91,"masking_suspected":false}' | \
  python3 "$E" --contract "$C" --out "$O8" --from-json - \
  --started-at 2026-09-16T22:13:42Z --ended-at 2026-09-16T22:16:37Z >/dev/null 2>&1
ok "$?" "0" "--from-json accepted"
ok "$(python3 -c "import json;print(json.load(open('$O8'))['timing']['duration_s'])")" \
   "175.0" "duration computed from explicit timestamps"

# ---------- dry run touches nothing ---------------------------------------------------------------
O9="$TMP/dry.json"
python3 "$E" --contract "$C" --out "$O9" --set verdict=pass --set confidence=90 \
  --set masking_suspected=false --dry-run >/dev/null 2>&1
ok "$?" "0" "dry-run exits 0"
ok "$([ -f "$O9" ] && echo yes || echo no)" "no" "dry-run wrote nothing"

# ---------- works for the other contracts too --------------------------------------------------------
O10="$TMP/review.json"
python3 "$E" --contract "$ROOT/contracts/dev-code-review.json" --out "$O10" \
  --set verdict=approve --set masking=false --set summary="looks fine" \
  --set-json findings='[]' >/dev/null 2>&1
ok "$?" "0" "dev-code-review contract emits"
ok "$(python3 -c "import json;print('timing' in json.load(open('$O10')))")" "True" "timing on review.json"

echo "test_emit: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
