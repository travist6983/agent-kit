#!/usr/bin/env bash
# Regression: qa-kb fix_lessons store (4th store) — insert / dedup-bump / semantic recall.
# Hits the real shared brain with source='test-fixlessons' and cleans up after. Needs the
# embedding key (kb.config.json `embedding.api_key_env`). SKIPS when the brain is unreachable.

# Shared brain lock: refuse rather than corrupt a concurrent run (see lib/brain_lock.sh).
# Nests quietly under run_all.sh, which already holds it.
. "$(cd "$(dirname "$0")" && pwd)/lib/brain_lock.sh"
brain_lock_acquire "test_fix_lessons" || exit 3
trap brain_lock_release EXIT

SK="$(cd "$(dirname "$0")/../../skills" && pwd)"; export SK
KBPY="$SK/qa-kb/scripts/kb.py"
SRC=test-fixlessons; export SRC
pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); else echo "  [FAIL] $1 — got '$2' want '$3'"; fail=$((fail+1)); fi; }

# Cleanup connects the SAME way kb.py's subprocesses do (kb.resolve_dsn() + kb.connect(), so
# every auth.mode applies) — never a raw local-default psycopg2 connection (QA-KB-C3). Runs BEFORE and AFTER (trap) so repeated
# runs against the persistent live brain stay idempotent (X-REAL-C1).
#
# kb_writes is swept too: every mutating verb now ledgers itself in the same transaction, so
# deleting only the knowledge rows leaves ORPHAN audit rows in the shared brain on every run.
# Each DELETE gets its own transaction — on a machine that has not applied the new schema.sql,
# a missing kb_writes table would otherwise abort the whole transaction and silently strand the
# fix_lessons rows too (the failure is invisible behind the >/dev/null this cleanup runs under).
clean(){ python3 - <<'PY' >/dev/null 2>&1
import os, sys
sys.path.insert(0, os.path.join(os.environ["SK"], "qa-kb", "scripts"))
import kb as kbmod
c = kbmod.connect(kbmod.resolve_dsn())
for sql in ("DELETE FROM fix_lessons WHERE source=%s",
            "DELETE FROM kb_writes   WHERE source=%s"):
    try:
        with c.cursor() as cur:
            cur.execute(sql, (os.environ["SRC"],))
        c.commit()
    except Exception:
        c.rollback()
c.close()
PY
}

# guard: skip cleanly if the brain isn't reachable (e.g. no key / DB) so the suite still runs elsewhere
if ! python3 "$KBPY" pause --status >/dev/null 2>&1; then
  echo "  fix_lessons: SKIPPED (brain unreachable)"; exit 0
fi
clean; trap clean EXIT

python3 "$KBPY" write fix_lessons --area "Project Setup" --source $SRC --signature-tag "state-drift-claim-parity" \
  --symptom "In Project Setup the assistant claims it saved data then re-asks for the same already-saved data; chat says a field is empty while the project board already shows it filled." \
  --root-cause "Stage stuck (no advance after the mutation) + no per-turn re-read of state." \
  --fix-shape "Auto-advance once the mutation completes; re-read current state each turn." >/dev/null

O2=$(python3 "$KBPY" write fix_lessons --area "Project Setup" --source $SRC \
  --symptom "The project setup assistant says it finished assigning the members, then immediately asks again for members already saved on the project.")
ck "paraphrase BUMPS (R10)" "$(echo "$O2"|python3 -c "import sys,json;print(json.load(sys.stdin)['action'])")" "bump"
ck "occurrences → 2"        "$(echo "$O2"|python3 -c "import sys,json;print(json.load(sys.stdin)['row']['occurrences'])")" "2"

A3=$(python3 "$KBPY" write fix_lessons --area "Tasks" --source $SRC \
  --symptom "Deleting a task from the task list shows a success toast but the task stays after reload." \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['action'])")
ck "distinct symptom INSERTS" "$A3" "insert"

TAG=$(python3 "$KBPY" search fix_lessons --k 1 \
  --query "the assistant insists a field isn't filled in yet even though the UI already shows it saved" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['results'][0]['signature_tag'])")
ck "semantic recall → right lesson" "$TAG" "state-drift-claim-parity"

echo "  fix_lessons: $pass passed, $fail failed"
[ "$fail" = 0 ]
