#!/usr/bin/env bash
# The loop framework — full deterministic regression suite.
# Runs every tested skill core + the qa-kb 4-store harness. Re-runnable and safe:
# everything is dry-run / scratch-state / self-cleaning. Runs GREEN on a machine with no brain
# and no browser driver: brain-backed suites (and the harness) SKIP when the brain is
# unreachable, and oracle suites SKIP when playwright-cli is absent.
#
# Every test_*.sh in this directory is picked up automatically — that IS the registration.
# Current members, and what each is the last line of defence for:
#   test_contracts        every ratified contract still validates its samples (and the
#                         zero-tests-but-green regression artifact still FAILS)
#   test_emit             contracts/emit.py never lands a contract violation on disk
#   test_kb_durability    a knowledge write is never silently dropped, and the artifact's
#                         claim about it is falsifiable (validate.py --receipts)
#   test_dream_harvest    the dream brief is reproducible, bounded, and honest — run against
#                         a synthetic fixture, never the user's live transcripts
#   test_dream_runner     dream.sh survives being unattended: single-instance lock + stale
#                         reclaim, watchdog (process-GROUP kill), the exit 20/21 brain gates,
#                         R9 pause, and the catch-up stall stop. Fake kb + fake claude only —
#                         it never reaches the live brain or spends a token.
#   test_agent_lock       the two scheduled agents never run at once: the shared cross-agent
#                         bolt (lib/agent_lock.sh) and both runners' use of it — A holds/B
#                         skips in both directions, stale reclaim, a live holder never
#                         robbed, the TTL beating a recycled pid, ownership on release, the
#                         bypass, and a watchdog-killed holder letting go. Fake claude/kb/
#                         pr_feed, every lock redirected out of the repo.
#   test_replay_oracle    qa-repro's playwright-cli replay oracle keeps its exit contract:
#                         0 green · 1 red · 2 blocked, one JSON line — a missing locator, an
#                         unfilled template, a prod host or a bad ROLE is BLOCKED, never graded.
#                         Local static page + ROLE=none; SKIPS without playwright-cli.
#   test_ship_preflight   infra-ship never pushes a prod-lane branch (one matching
#                         deploy.prod_lane_branch_patterns — that push IS a production deploy):
#                         abort prod-lane-branch, exit 7, unless staging_only is off AND
#                         SHIP_PROD_LANE_OK names that exact branch. Stub kb.py + a config
#                         fixture — offline, never touches the shared brain.
#   test_release_ac_deploy  qa-loop-release-ac proves each deploy track deployed by ITS OWN
#                         deploy source, never another track's version; a failed lookup is a
#                         deploy-unknown stop. Adapter fakes + a throwaway git repo — offline.
#   test_assign_tier · test_check_convergence · test_descriptions · test_escalation_render ·
#   test_fix_lessons · test_oracle_preflight · test_personas · test_pr_review_runner ·
#   test_pr_thread_bar · test_prod_signal · test_prune · test_r6_denylist ·
#   test_recipe_router · test_sandbox · test_standards_check · test_verify_gate
# Usage:  bash run_all.sh
HERE="$(cd "$(dirname "$0")" && pwd)"
SK="$(cd "$HERE/../../skills" && pwd)"
echo "════════════ loop framework — regression suite ════════════"
suites=0; failed=0

# Serialize every brain-touching suite in this run against any OTHER run on this machine.
# Acquired ONCE here; the suites below (and harness.py) see KB_LOCK_HELD and nest
# quietly instead of self-deadlocking. Two concurrent run_alls corrupt each other through
# kb-harness / test-kbdur / test-fixlessons cleanup and emit reds that look like regressions.
. "$HERE/lib/brain_lock.sh"
brain_lock_acquire "run_all.sh" || {
  echo ""; echo "════════════ run_all REFUSED — another brain-touching run is live ════════════"
  exit 3
}
trap brain_lock_release EXIT

for t in "$HERE"/test_*.sh; do
  echo ""; echo "▶ $(basename "$t" .sh)"
  if bash "$t"; then :; else failed=$((failed+1)); fi
  suites=$((suites+1))
done

echo ""; echo "▶ qa-kb harness (4 stores)"
# Same reachability guard as the brain-backed suites: no brain (unconfigured, unreachable or
# refused) is a SKIP, not a red. A brain that answers but fails the harness is still a red.
if ! python3 "$SK/qa-kb/scripts/kb.py" pause --status >/dev/null 2>&1; then
  echo "  qa-kb harness: SKIPPED (brain unreachable)"
else
  H=$(python3 "$SK/qa-kb/scripts/harness.py" 2>&1)
  SUM=$(echo "$H" | grep -oE "[0-9]+/[0-9]+ checks passed" | head -1)
  echo "  ${SUM:-<harness produced no summary — see: python3 $SK/qa-kb/scripts/harness.py>}"
  if [ -n "$SUM" ] && [ "${SUM%%/*}" = "$(echo "$SUM" | sed -E 's#^[0-9]+/([0-9]+).*#\1#')" ]; then :;
  else failed=$((failed+1)); fi
fi
suites=$((suites+1))

# tidy: drop known test residue so the brain stays pristine after a suite run, then SAY what
# is left. Connects via kb.py's own DSN resolution (kb.resolve_dsn() + kb.connect(), so every
# auth.mode applies) — the old raw local-default
# psycopg2 connection targeted a DIFFERENT database than the kb subprocesses, which is how
# residue accumulated in the live brain (QA-KB-C3 / X-REAL-C1).
#
# kb_writes + kb_state are swept too: every mutating verb now ledgers itself, so a suite that
# deletes only its knowledge rows leaves orphan audit rows behind on every single run. The
# residue line at the end makes that class of leak visible instead of letting it accumulate
# silently — a cleanup whose failure is swallowed is how the brain got dirty in the first place.
TIDY=$(SK="$SK" python3 - 2>&1 <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["SK"], "qa-kb", "scripts"))
import kb as kbmod
SOURCES = ("kb-harness", "test-fixlessons", "test-kbdur")
LOOPS   = ("kb-harness", "test-fixlessons", "test-kbdur")
TICKETS = ("HARNESS-0000", "KBDUR-0000")
try:
    c = kbmod.connect(kbmod.resolve_dsn())
except Exception as e:
    print("skip: brain unreachable (%s)" % type(e).__name__); raise SystemExit(0)
try:
    with c.cursor() as cur:
        for t in ("app_map", "expectations", "flakiness", "fix_lessons"):
            cur.execute("DELETE FROM %s WHERE source = ANY(%%s)" % t, (list(SOURCES),))
        cur.execute("DELETE FROM metrics   WHERE loop_id = ANY(%s) OR ticket = ANY(%s)",
                    (list(LOOPS), list(TICKETS)))
        cur.execute("DELETE FROM kb_writes WHERE source = ANY(%s) OR ticket = ANY(%s)",
                    (list(SOURCES), list(TICKETS)))
        cur.execute("DELETE FROM kb_state  WHERE key LIKE 'harness-%' OR key LIKE 'kb-harness%'"
                    " OR key LIKE 'test-kbdur%'")
    c.commit()
    with c.cursor() as cur:
        left = 0
        for t in ("app_map", "expectations", "flakiness", "fix_lessons"):
            cur.execute("SELECT count(*) AS n FROM %s WHERE source = ANY(%%s)" % t, (list(SOURCES),))
            left += cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM kb_writes WHERE source = ANY(%s) OR ticket = ANY(%s)",
                    (list(SOURCES), list(TICKETS)))
        left += cur.fetchone()["n"]
    print("clean" if left == 0 else "WARNING: %d marked test row(s) still in the shared brain" % left)
except Exception as e:
    print("WARNING: tidy FAILED (%s: %s) — marked test rows may survive" % (type(e).__name__, e))
finally:
    c.close()
PY
)
case "$TIDY" in
  clean|skip*) : ;;
  *) echo ""; echo "  ⚠ brain tidy: $(echo "$TIDY" | tail -1)" ;;
esac

echo ""; echo "════════════ $((suites-failed))/$suites suites GREEN ════════════"
[ "$failed" = 0 ]
