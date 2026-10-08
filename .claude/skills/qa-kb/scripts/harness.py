#!/usr/bin/env python3
"""qa-kb harness — exercises every kb verb against the real shared brain.

Two headline checks:
  * #7  the semantic-search proof: a blessed expectation retrieved by a differently-worded
        query — the thing flat-files/SQLite-LIKE can't do.
  * #15+ the DURABILITY proof: neither a dead embedder nor a dead database may lose a write.

The embedder is a live external dependency, so checks that genuinely require it run in one of
two modes and say which: EMBEDDER LIVE (semantic proof) or EMBEDDER DOWN (degraded-contract
proof — the failure must be loud, the row must still be stored, and `reembed` must see it).
A check is never quietly "passed" because it did not run.

DUAL MODE IS A REQUIREMENT, NOT AN ACCIDENT. An embedder outage can last weeks; the suite
must prove the same invariants on both paths, so DOWN is *simulated* on demand rather than
waited for. Every check line is tagged with the mode that produced it.

    python3 harness.py                # auto: whichever mode the environment is actually in
    python3 harness.py --mode live    # require a live embedder (fails loudly if it is down)
    python3 harness.py --mode down    # simulate the outage (the embedding key env var cleared)
    python3 harness.py --both         # live pass THEN simulated-down pass; both totals reported

Exits non-zero if any check FAILS. Skips are impossible: every check has a live and a
degraded form.

SHARED-BRAIN RULES (this runs against the REAL shared brain, which other sessions are writing):
  * every row the harness writes carries source=MARK, and cleanup() deletes ONLY by that
    source — never by ticket, because a fix_lessons bump COALESCEs the caller's ticket onto
    the row it bumped, so a ticket-scoped DELETE could destroy a real lesson;
  * no fix_lessons write may reach the VECTOR dedupe guard with an unbounded threshold. The
    guard bumps the single nearest row in the WHOLE table, across areas — a probe symptom
    measured 0.4468 against a real production lesson, against a 0.45 default. So a harness
    write either pins `--dup-threshold` above 1.0 (guard off, insert guaranteed) or computes
    the threshold from a measured top-2 so only a harness row can possibly match;
  * `foreign_fingerprint()` is compared before and after: the rows the harness did not write
    must come out byte-identical in count, occurrences and ticket stamping.
"""
import atexit
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile

# The harness never uses an owner's overlay: it points at a real brain. Use KB_CONFIG or
# KB_DATABASE_URL to aim it at a throwaway database.
os.environ.pop("AGENT_KIT_OVERLAY", None)
os.environ.pop("AGENT_KIT_PROJECT", None)
import threading

HERE = os.path.dirname(os.path.realpath(__file__))
KB = os.path.join(HERE, "kb.py")
sys.path.insert(0, HERE)
import kb as kbmod  # same DSN resolution + connect (configured auth mode) as the kb subprocesses

# Resolve the DSN exactly like kb.py's main() so cleanup() and the kb subprocesses always
# talk to the SAME database — never a local-vs-shared split-brain. (--db > KB_DATABASE_URL >
# kb.config.json; raises BrainConfigError when nothing names a target.)
try:
    DSN = kbmod.resolve_dsn()
    NO_BRAIN = None
except kbmod.BrainConfigError as e:  # nothing names a brain: report it, never a traceback
    DSN, NO_BRAIN = None, str(e)
# Provenance markers so we can clean up our own rows. OVERRIDABLE, because cleanup() deletes
# by them GLOBALLY (at start and via atexit) and this brain is shared: two harness runs at once
# — an A/B of two kb.py revisions is the obvious way to get there — delete each other's fixtures
# mid-flight and report spurious reds that look like product regressions. Set
# KB_HARNESS_MARK to a unique value to run a second instance alongside another. Note that
# run_all.sh's tidy sweeps the DEFAULT marker only, so a custom-marked run relies on its own
# cleanup (which is registered with atexit, so it survives a crash).
MARK = os.environ.get("KB_HARNESS_MARK") or "kb-harness"
HTICKET = os.environ.get("KB_HARNESS_TICKET") or "HARNESS-0000"
UNREACHABLE = "host=127.0.0.1 port=1 dbname=nope connect_timeout=2"
BROKEN_EMBEDDER = {kbmod.EMB_KEY_ENV: ""}   # deterministic embedder outage, quota-independent
EMB_PROVIDER = kbmod.CONFIG["embedding"].get("provider") or "openai"

TMPDIR = tempfile.mkdtemp(prefix="kb-harness-")
OUTBOX = os.path.join(TMPDIR, "outbox.jsonl")   # never the real BRAIN_DIR outbox

NO_VECTOR_DEDUPE = ("--dup-threshold", "1.5")   # cosine can't exceed 1.0 → guard 2 cannot fire

# The mode this pass is running in ("live" | "down"), and the env forced onto every kb
# subprocess to produce it. DOWN is simulated, so the degraded contract is proved on demand
# instead of only when the embedding provider happens to be broken.
MODE = "live"
FORCE_ENV = {}
_CLEANUP_REGISTERED = False

checks = []
def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    print("  [{}][{}] {}".format("PASS" if cond else "FAIL", MODE, name)
          + ("  — {}".format(detail) if detail else ""))

def kb(*args, **kw):
    expect_ok = kw.pop("expect_ok", True)
    env_extra = kw.pop("env", None)
    env = None
    if FORCE_ENV or env_extra:
        env = dict(os.environ)
        env.update(FORCE_ENV)              # the simulated outage, when this pass is DOWN
        env.update(env_extra or {})        # a check's own explicit override still wins
    r = subprocess.run([sys.executable, KB] + [str(x) for x in args],
                       capture_output=True, text=True, env=env)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    except Exception:
        print("  !! non-JSON output:", repr(r.stdout), repr(r.stderr)); raise
    out["_rc"] = r.returncode
    if expect_ok and not out.get("ok"):
        print("  !! unexpected failure:", json.dumps(out), "stderr:", r.stderr)
    return out

def kb_parallel(n, *args):
    """Run the same kb command n times CONCURRENTLY and return every parsed response.

    The brain is shared and the skill documents `undo` as idempotent, so "safe when run twice"
    has to mean safe when run twice AT ONCE. Sequential idempotence is proved by a receipt;
    concurrent idempotence needs that receipt to be read under a row lock, which is a different
    property and cannot be exercised one subprocess at a time."""
    env = None
    if FORCE_ENV:
        env = dict(os.environ); env.update(FORCE_ENV)
    outs = [None] * n
    def go(i):
        r = subprocess.run([sys.executable, KB] + [str(x) for x in args],
                           capture_output=True, text=True, env=env)
        try:
            outs[i] = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
        except Exception:
            outs[i] = {"ok": False, "_stdout": r.stdout[-200:], "_stderr": r.stderr[-200:]}
    ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in ts: t.start()
    for t in ts: t.join()
    return outs

def cleanup():
    conn = kbmod.connect(DSN)  # NB: connect() opens a tx (register_vector) — commit, don't autocommit
    with conn.cursor() as c:
        # KNOWLEDGE rows are deleted ONLY by the harness's own source. Selecting them by
        # `ticket=HTICKET` as well used to look equivalent — every harness write passes
        # --source MARK — but it is not: `bump` does `ticket = COALESCE(<caller>, ticket)`, so
        # a probe that vector-matched a REAL lesson would stamp HARNESS-0000 onto it and this
        # DELETE would then destroy a production row. Ledger/metrics rows below are
        # append-only records of the harness's own writes, so ticket/loop scoping is safe there.
        c.execute("DELETE FROM expectations WHERE source=%s", (MARK,))
        c.execute("DELETE FROM app_map     WHERE source=%s", (MARK,))
        c.execute("DELETE FROM flakiness   WHERE source=%s", (MARK,))
        c.execute("DELETE FROM fix_lessons WHERE source=%s", (MARK,))
        c.execute("DELETE FROM metrics     WHERE loop_id=%s OR ticket=%s", (MARK, HTICKET))
        c.execute("DELETE FROM kb_control  WHERE flag='harness-probe'")
        # the ledger + durable state the durability checks write. `run_id LIKE 'kb-harness%'`
        # also catches the undo checks' OWN reversal rows, which the ledger stamps
        # source='kb-undo' (never the source being undone) — deleting by that source would
        # reach into a real operator's undo history on this shared brain.
        c.execute("DELETE FROM kb_writes   WHERE source=%s OR loop_id=%s OR ticket=%s "
                  "OR run_id LIKE %s", (MARK, MARK, HTICKET, MARK + "%"))
        c.execute("DELETE FROM kb_state    WHERE key LIKE 'harness-%'")
    conn.commit()
    conn.close()
    shutil.rmtree(TMPDIR, ignore_errors=True)

def source_rows(table, source=MARK):
    """Count the rows one source wrote — read straight from the brain, so a bulk-delete check
    can state its OWN denominator instead of trusting the verb it is testing."""
    conn = kbmod.connect(DSN)
    with conn.cursor() as c:
        c.execute("SELECT count(*) AS n FROM {} WHERE source=%s".format(table), (source,))
        n = c.fetchone()["n"]
    conn.commit()
    conn.close()
    return n

def foreign_fingerprint():
    """id → (occurrences, ticket) for every fix_lessons row this harness did NOT write. On a
    shared brain 'my checks passed' is not enough: the vector dedupe guard bumps the nearest
    row in the whole table across areas, and a bump COALESCEs the caller's ticket onto it.
    Compared start vs end BY ID — another session inserting a lesson mid-run is normal and must
    not turn this red; a row that was already there coming back changed (or gone) is not."""
    conn = kbmod.connect(DSN)
    with conn.cursor() as c:
        c.execute("SELECT id, occurrences, ticket FROM fix_lessons WHERE source <> %s", (MARK,))
        fp = dict((r["id"], (r["occurrences"], r["ticket"])) for r in c.fetchall())
    conn.commit()
    conn.close()
    return fp

def nearest_lesson(query_text):
    """Read-only top-2 for a symptom the harness is ABOUT to write. Used to pin
    --dup-threshold between our own row and everything else, so the vector guard can only
    ever match a harness row. Returns [] when the embedder is down."""
    r = kb("search", "fix_lessons", "--query", query_text, "--k", "2", expect_ok=False)
    return r.get("results") or []

def lesson_occ(row_id):
    """A fix_lesson's occurrences, or None when the row is GONE. Never indexes into an empty
    result: a broken undo that deletes the row must surface as a red check, not a traceback
    that kills the run before the summary and the shared-brain check."""
    rows = kb("read", "fix_lessons", "--id", row_id).get("rows") or []
    return rows[0]["occurrences"] if rows else None

def vector_bump_threshold(anchor_id, paraphrase):
    """(threshold, top2) for a write that must bump OUR anchor by vector similarity — or
    (None, top2) when the anchor is NOT the nearest row, in which case the caller must not
    write at all. GUARD 2 bumps the single nearest row in the whole table regardless of area,
    so 'probably ours' is not good enough on a shared brain: the threshold is pinned halfway
    between the anchor and the runner-up, which makes any other row unmatchable."""
    near = nearest_lesson(paraphrase)
    if not near or near[0].get("id") != anchor_id or near[0].get("similarity", 0) < 0.45:
        return None, near
    second = near[1]["similarity"] if len(near) > 1 else 0.0
    return "{:.6f}".format((near[0]["similarity"] + second) / 2.0), near

def embedder_live():
    """One probe, so the semantic checks state honestly which contract they proved."""
    r = kb("search", "expectations", "--query", "harness embedder liveness probe", "--k", "1",
           expect_ok=False)
    return not str(r.get("error", "")).startswith("embedder_unavailable")


# ── single-instance lock ─────────────────────────────────────────────────────
# cleanup() deletes WHERE source=MARK GLOBALLY, and app_map's UNIQUE(kind, area, key)
# carries no source column — so two harness runs at once delete each other's live
# fixtures mid-run AND collide on app_map_kind_area_key_key. The reds that produces read
# exactly like product regressions (they cost a real session an hour of false-lead
# debugging). Setting KB_HARNESS_MARK does NOT isolate runs: the fixture keys are
# hardcoded, so the unique constraint still collides. Until those keys derive from MARK,
# the honest fix is to refuse to run concurrently rather than produce lies.
# Mirrors qa-dream's dream.lock: atomic mkdir + pid inside + stale reclaim.
# ONE global lock on purpose — the collision is in the constraint, not in MARK.
LOCK_DIR = (os.environ.get("KB_BRAIN_LOCK") or os.environ.get("KB_HARNESS_LOCK")
            or os.path.join(kbmod.BRAIN_DIR, "brain-tests.lock"))
_LOCK_HELD = False

def _lock_owner_alive(pid):
    """True if pid is a LIVE harness. Never steal a lock on a guess: if we cannot tell
    what the process is, assume it is alive."""
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    except PermissionError:
        return True                     # exists, owned by someone else
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return True                     # cannot tell -> assume alive
    if not cmd.strip():
        return True                     # ps said nothing -> assume alive
    return any(t in cmd for t in ("harness.py", "run_all.sh",
                                  "test_kb_durability", "test_fix_lessons"))

def acquire_lock():
    """Returns None on success, or a human-readable refusal string.

    If run_all.sh (or any parent) already holds the shared lock we nest quietly. The marker
    alone is NOT trusted: an exported var outlives the process that set it, so a child of a
    dead parent would inherit "held" and skip locking precisely when it is least safe. The
    lock DIRECTORY must exist too — marker-set-but-dir-gone means acquire, not proceed."""
    global _LOCK_HELD
    if os.environ.get("KB_LOCK_HELD") and os.path.isdir(LOCK_DIR):
        return None
    for _ in range(8):
        try:
            os.mkdir(LOCK_DIR)
        except FileExistsError:
            pass
        else:
            _LOCK_HELD = True
            with open(os.path.join(LOCK_DIR, "pid"), "w") as fh:
                fh.write(str(os.getpid()))
            with open(os.path.join(LOCK_DIR, "started_at"), "w") as fh:
                fh.write(datetime.datetime.now(datetime.timezone.utc).isoformat())
            atexit.register(release_lock)
            return None
        try:
            with open(os.path.join(LOCK_DIR, "pid")) as fh:
                pid = int(fh.read().strip() or 0)
        except Exception:
            pid = 0
        try:
            with open(os.path.join(LOCK_DIR, "started_at")) as fh:
                started = fh.read().strip()
        except Exception:
            started = "unknown"
        if pid and _lock_owner_alive(pid):
            return ("another harness is already running (pid {}, since {}, lock {}).\n"
                    "  Two runs at once delete each other's fixtures and collide on\n"
                    "  app_map_kind_area_key_key — the reds would be lies, so this run is\n"
                    "  refusing rather than producing them. Wait for it, or if you are certain\n"
                    "  it is dead: rm -rf {}".format(pid, started, LOCK_DIR, LOCK_DIR))
        print("  harness: stale lock at {} (pid {} is not alive) — reclaiming"
              .format(LOCK_DIR, pid or "none"), file=sys.stderr)
        shutil.rmtree(LOCK_DIR, ignore_errors=True)
    return "could not acquire or reclaim {} after 8 tries".format(LOCK_DIR)

def release_lock():
    global _LOCK_HELD
    if not _LOCK_HELD:
        return
    _LOCK_HELD = False
    shutil.rmtree(LOCK_DIR, ignore_errors=True)


def main(want_mode="auto"):
    """One pass. `want_mode` is auto | live | down; DOWN is simulated, never waited for."""
    global MODE, FORCE_ENV
    del checks[:]                          # a --both run reports each pass on its own
    MODE, FORCE_ENV = "live", {}
    print("== qa-kb harness ==")
    cleanup()                   # idempotency: fresh state before checks…
    global _CLEANUP_REGISTERED
    if not _CLEANUP_REGISTERED:  # …and no residue left in the shared brain after, even on a crash
        atexit.register(cleanup)
        _CLEANUP_REGISTERED = True
    os.environ["KB_OUTBOX"] = OUTBOX
    for k in kbmod.TAG_ENV.values():     # the env-autotag checks set these explicitly
        os.environ.pop(k, None)

    if want_mode == "down":
        MODE, FORCE_ENV = "down", dict(BROKEN_EMBEDDER)
    live = embedder_live()
    if want_mode == "live" and not live:
        print("  !! --mode live demanded, but the embedder is DOWN. The semantic contract "
              "cannot be proved right now; refusing to pass a degraded run off as a live one.")
        return 1
    MODE = "live" if live else "down"
    print("  MODE: EMBEDDER {}".format(
        "LIVE — semantic proofs run (real {} embeddings)".format(EMB_PROVIDER) if live else
        "DOWN — degraded-contract proofs run instead ({})".format(
            "SIMULATED: {} cleared".format(kbmod.EMB_KEY_ENV) if FORCE_ENV
            else "the real embedder is unavailable")))
    fp_before = foreign_fingerprint()
    print("  watching {} fix_lessons rows the harness does not own (they must not move)".format(
        len(fp_before)))

    # 1) app_map insert
    r = kb("write", "app_map", "--kind", "selector", "--area", "harness_billing",
           "--key", "submit_button", "--value", json.dumps({"selector": "[data-test=billing-submit]"}),
           "--source", MARK)
    check("app_map insert", r.get("action") == "insert" and r["row"]["version"] == 1)
    v1 = r["row"]["version"] if r.get("row") else None
    id1 = r["row"]["id"] if r.get("row") else None

    # 2) read it back
    r = kb("read", "app_map", "--kind", "selector", "--area", "harness_billing", "--key", "submit_button")
    got = r["rows"][0]["value"]["selector"] if r.get("rows") else None
    check("app_map read-back", got == "[data-test=billing-submit]", got or "")

    # 2b) --key must NARROW even without --kind. It used to be dropped whenever --kind was
    # absent, so `read --area X --key Y` returned the whole slice and rows[0] was an
    # arbitrary row — a brain read quietly answering a different question than it was asked.
    kb("write", "app_map", "--kind", "route", "--area", "harness_billing",
       "--key", "harness_second_key", "--value", json.dumps({"route": "/harness"}), "--source", MARK)
    slice_all = kb("read", "app_map", "--area", "harness_billing")
    narrowed = kb("read", "app_map", "--area", "harness_billing", "--key", "harness_second_key")
    bare = kb("read", "app_map", "--key", "harness_second_key", expect_ok=False)
    check("app_map read: --key narrows without --kind",
          slice_all.get("count") == 2 and narrowed.get("count") == 1
          and narrowed["rows"][0]["key"] == "harness_second_key"
          and bare.get("ok") is False,
          "slice={} narrowed={}".format(slice_all.get("count"), narrowed.get("count")))

    # 3) optimistic-concurrency update (correct version)
    r = kb("write", "app_map", "--kind", "selector", "--area", "harness_billing",
           "--key", "submit_button", "--value", json.dumps({"selector": "[data-test=billing-submit-v2]"}),
           "--source", MARK, "--expect-version", v1)
    check("app_map versioned update", r.get("action") == "update" and r["row"]["version"] == 2)

    # 4) stale write must be rejected, not clobber (R4)
    r = kb("write", "app_map", "--kind", "selector", "--area", "harness_billing",
           "--key", "submit_button", "--value", json.dumps({"selector": "x"}),
           "--source", MARK, "--expect-version", v1, expect_ok=False)
    check("app_map stale write rejected", r.get("ok") is False and r.get("error") == "version_conflict")

    # 5) propose an expectation, 6) bless it
    r = kb("propose", "expectation", "--area", "harness_signup", "--source", MARK,
           "--statement", "Signing up with a malformed email shows an inline validation error and does not submit.")
    e1 = r["row"]["id"] if r.get("row") else None
    check("expectation propose", e1 is not None and r["row"]["status"] == "proposed")
    r = kb("bless", "expectation", "--id", e1, "--by", MARK)
    check("expectation bless", r.get("row", {}).get("status") == "canonical")

    if live:
        # 7) *** SEMANTIC SEARCH DEMO *** — differently worded query must retrieve e1
        r = kb("search", "expectations", "--status", "canonical,proposed", "--k", "3",
               "--query", "invalid email address should display a validation message and block the form")
        top = r["results"][0] if r.get("results") else {}
        sim = top.get("similarity")
        check("SEMANTIC: paraphrase retrieves blessed expectation",
              top.get("id") == e1 and sim is not None and sim >= 0.5,
              "top id={} (want {}), cosine similarity={}".format(top.get("id"), e1, sim))

        # 8) propose a near-duplicate → dedup warning (R10)
        r = kb("propose", "expectation", "--area", "harness_signup", "--source", MARK, "--dup-threshold", "0.65",
               "--statement", "Entering an invalid email should show a validation message and prevent submission.")
        e2 = r["row"]["id"] if r.get("row") else None
        near_top = r["nearest"][0] if r.get("nearest") else {}
        check("dedup warning on near-duplicate",
              r.get("warning") == "possible_duplicate" and near_top.get("id") == e1,
              "nearest id={} sim={}".format(near_top.get("id"), near_top.get("similarity")))
    else:
        # 7-degraded) vector search must fail LOUDLY and name the non-vector fallback —
        # an empty result set would be indistinguishable from "no oracle exists".
        r = kb("search", "expectations", "--query", "anything at all", expect_ok=False)
        check("DEGRADED: vector search fails loudly + names the non-vector path",
              r.get("ok") is False and str(r.get("error", "")).startswith("embedder_unavailable")
              and "kb read expectations" in str(r.get("degraded_path", "")),
              str(r.get("error", ""))[:60])
        # 8-degraded) the blessed row is still reachable by the documented degraded read
        r = kb("read", "expectations", "--area", "harness-signup")
        ids = [x["id"] for x in r.get("rows", [])]
        check("DEGRADED: non-vector read still returns the blessed expectation",
              e1 in ids, "ids={}".format(ids))
        e2 = kb("propose", "expectation", "--area", "harness_signup", "--source", MARK,
                "--statement", "Entering an invalid email should show a validation message and prevent submission."
                )["row"]["id"]

    # 9) flakiness counters
    kb("write", "flakiness", "--spec", "harness.spec > submits", "--area", "harness", "--source", MARK)
    kb("write", "flakiness", "--spec", "harness.spec > submits", "--area", "harness", "--source", MARK, "--failed")
    r = kb("read", "flakiness", "--spec", "harness.spec > submits")
    row = r["rows"][0] if r.get("rows") else {}
    check("flakiness counters + flake_rate",
          row.get("runs") == 2 and row.get("failures") == 1 and abs(row.get("flake_rate", 0) - 0.5) < 1e-6,
          "runs={} failures={} rate={}".format(row.get("runs"), row.get("failures"), row.get("flake_rate")))

    # 10) quarantine (R7)
    kb("quarantine", "flakiness", "--spec", "harness.spec > submits", "--on", "--reason", "test",
       "--source", MARK)
    r = kb("read", "flakiness", "--spec", "harness.spec > submits")
    check("flakiness quarantine", r["rows"][0].get("quarantined") is True)

    # 11) global pause flag (R9) — SAVE/RESTORE prior state; this is the LIVE shared kill
    # switch, so the harness must never force-clear it. If a human set it ON
    # for a real incident, leave it alone entirely.
    prior = kb("pause", "--status").get("control") or {}
    if prior.get("value") is True:
        check("global pause: already ON (incident?) — left untouched, fail-safe", True,
              "set_by={} reason={}".format(prior.get("set_by"), prior.get("reason")))
    else:
        paused_on = False
        try:
            kb("pause", "--on", "--reason", "harness self-test", "--by", MARK)
            paused_on = kb("pause", "--status").get("paused") is True
        finally:
            kb("pause", "--off", "--reason", "harness restore (prior state: off)", "--by", MARK)
        r2 = kb("pause", "--status")
        check("global pause on→restored to prior (off)", paused_on and r2.get("paused") is False)

    # 11b) generic kb_control flag verb (the staging_only posture home) — exercised on a
    # THROWAWAY flag so the live posture is never touched; staging_only itself is read-only here.
    r = kb("flag", "--name", "harness-probe", "--on", "--by", MARK, "--reason", "verb test")
    check("flag set (upsert)", r.get("value") is True)
    r = kb("flag", "--name", "harness-probe", "--off", "--by", MARK)
    r2 = kb("flag", "--name", "harness-probe", "--status")
    check("flag status read-back", r2.get("value") is False)
    r3 = kb("flag", "--name", "staging_only", "--status")
    check("staging_only flag readable", r3.get("ok") is True and r3.get("value") is not None,
          "value={}".format(r3.get("value")))

    # 12) metrics (R5/R8)
    kb("metric", "--name", "cost_per_fix", "--value", "0.40", "--unit", "usd", "--loop-id", MARK,
       "--source", MARK)
    kb("metric", "--name", "cost_per_fix", "--value", "0.60", "--unit", "usd", "--loop-id", MARK,
       "--source", MARK)
    r = kb("metrics", "--summary", "--metric", "cost_per_fix", "--loop-id", MARK)
    s = r["summary"][0] if r.get("summary") else {}
    check("metrics summary", s.get("n") == 2 and abs(float(s.get("sum", 0)) - 1.0) < 1e-6,
          "n={} sum={}".format(s.get("n"), s.get("sum")))

    # 13) forget (soft) + 14) decay audit (R1)
    kb("forget", "expectations", "--id", e2, "--soft", "--source", MARK)
    r = kb("read", "expectations", "--id", e2)
    check("forget soft → status forgotten", r["rows"][0].get("status") == "forgotten")
    r = kb("decay")
    check("decay dry-run audit", r.get("ok") is True and r.get("dry_run") is True)
    # 14b) decay also reports app_map staleness — and NEVER prunes it, whatever --apply says
    check("decay reports stale_app_map (report-only)",
          isinstance(r.get("stale_app_map"), list) and r.get("stale_app_map_total") is not None
          and "never" in str(r.get("stale_app_map_apply")),
          "stale_app_map_total={}".format(r.get("stale_app_map_total")))

    # ── DURABILITY: a lost write is a lost lesson ────────────────────────────────

    # 15) embedder down → the lesson STILL lands, pending, exit 0
    r = kb("write", "fix_lessons", "--area", "Harness Durability", "--source", MARK,
           "--ticket", HTICKET, "--symptom", "harness: embedder outage must not cost the lesson",
           "--root-cause", "embed-before-insert + rollback-on-exception",
           "--fix-shape", "insert with embedding NULL + embed_status=pending", env=BROKEN_EMBEDDER)
    fl_id = r.get("row", {}).get("id")
    check("pending-embed: fix_lesson survives an embedder outage",
          r.get("ok") is True and r.get("_rc") == 0 and r.get("embed_status") == "pending"
          and r.get("dedup_skipped") is True
          and str(r.get("warning", "")).startswith("embedder_unavailable")
          and r.get("row", {}).get("embedding_present") is False,
          "id={} embed_status={}".format(fl_id, r.get("embed_status")))

    # 15b) same for a proposed expectation
    r = kb("propose", "expectation", "--area", "Harness Durability", "--source", MARK,
           "--ticket", HTICKET,
           "--statement", "harness: a proposal made during an embedder outage is still stored.",
           env=BROKEN_EMBEDDER)
    ex_id = r.get("row", {}).get("id")
    check("pending-embed: expectation survives an embedder outage",
          r.get("ok") is True and r.get("_rc") == 0 and r.get("embed_status") == "pending"
          and r.get("nearest") == [] and r.get("dedup_skipped") is True,
          "id={}".format(ex_id))

    # 16) reembed --dry-run lists the pending rows and changes nothing
    r = kb("reembed", "--store", "all", "--filter-source", MARK, "--dry-run")
    dry_ids = set(x["id"] for x in r.get("results", []))
    check("reembed --dry-run lists pending rows, changes nothing",
          r.get("ok") is True and r.get("dry_run") is True and r.get("embedded") == 0
          and {fl_id, ex_id} <= dry_ids, "scanned={}".format(r.get("scanned")))

    # 16b) reembed with a dead embedder: exit 0, per-row failure, rows STAY pending.
    # It is a maintenance verb, not a gate — it must never take a loop down.
    r = kb("reembed", "--store", "all", "--filter-source", MARK, env=BROKEN_EMBEDDER)
    still = kb("read", "fix_lessons", "--id", fl_id)["rows"][0]
    check("reembed survives a dead embedder (exit 0, rows stay pending)",
          r.get("ok") is True and r.get("_rc") == 0 and r.get("embedded") == 0
          and r.get("failed") >= 2 and still.get("embed_status") == "pending",
          "failed={} warning={}".format(r.get("failed"), str(r.get("warning"))[:40]))

    # 16c) reembed success path — stub embedder, scoped to THIS harness's own rows, which
    # cleanup() deletes. (Stub provenance is stamped honestly; it never backs a real lesson.)
    r = kb("--embedder", "stub", "reembed", "--store", "all", "--filter-source", MARK,
           "--source", MARK)
    healed_ids = set(x["id"] for x in r.get("results", []) if x.get("ok"))
    healed = kb("read", "fix_lessons", "--id", fl_id)["rows"][0]
    check("reembed heals a pending row (vector + model + version bump, no auto-merge)",
          r.get("failed") == 0 and r.get("embedded") == r.get("scanned") and r.get("scanned") >= 2
          and {fl_id, ex_id} <= healed_ids and r.get("auto_merge") is False
          and healed.get("embed_status") == "ok" and healed.get("embedding_present") is True
          and healed.get("embedding_model") == "stub-sha256-{}".format(kbmod.EMB_DIM)
          and healed.get("version") == 2,
          "embedded={}/{} status={}".format(r.get("embedded"), r.get("scanned"),
                                            healed.get("embed_status")))

    # 17) OUTBOX — a dead DB defers a write, it never drops it
    r = kb("outbox", "--status")
    check("outbox starts empty (isolated temp spool)", r.get("count") == 0 and r.get("outbox") == OUTBOX)

    r = kb("--db", UNREACHABLE, "write", "app_map", "--kind", "selector", "--area", "harness_billing",
           "--key", "queued_by_db_flag", "--value", json.dumps({"selector": "[data-test=queued]"}),
           "--source", MARK, expect_ok=False)
    check("outbox: unreachable DB queues a MUTATING verb",
          r.get("ok") is False and r.get("queued") is True and r.get("outbox") == OUTBOX,
          str(r.get("error", ""))[:50])

    r = kb("--db", UNREACHABLE, "read", "app_map", "--area", "harness_billing", expect_ok=False)
    r2 = kb("--db", UNREACHABLE, "pause", "--status", expect_ok=False)
    depth = kb("outbox", "--status").get("count")
    check("outbox: NON-mutating verbs never queue (a stale read is worse than no read)",
          r.get("queued") is False and r2.get("queued") is False and depth == 1,
          "depth={}".format(depth))

    r = kb("write", "app_map", "--kind", "selector", "--area", "harness_billing",
           "--key", "submit_button", "--value", json.dumps({"selector": "y"}),
           "--source", MARK, "--expect-version", v1, expect_ok=False)
    r2 = kb("write", "app_map", "--kind", "selector", "--area", "harness_billing",
            "--source", MARK, expect_ok=False)
    depth = kb("outbox", "--status").get("count")
    check("outbox: SEMANTIC failures (version_conflict, bad args) never queue",
          r.get("error") == "version_conflict" and r.get("queued") is None
          and r2.get("queued") is None and depth == 1,
          "depth still {}".format(depth))

    # never lossy: a replay that STILL cannot reach the brain keeps the entry. The operator's
    # own --db wins on a replay, so pointing it at the dead DSN is how you force that case.
    r = kb("--db", UNREACHABLE, "outbox", "--replay")
    depth = kb("outbox", "--status").get("count")
    check("outbox: a replay that fails again KEEPS the entry (never lossy)",
          r.get("attempted") == 1 and r.get("replayed") == 0 and r.get("remaining") == 1 and depth == 1,
          "remaining={} depth={}".format(r.get("remaining"), depth))

    # ...and the queued --db is RETARGETED on a normal replay. Without that, an entry queued
    # with an explicit --db names the address that was dead and can never succeed: it would be
    # retried forever and then discarded by the first `outbox --clear`.
    r = kb("outbox", "--replay")
    depth = kb("outbox", "--status").get("count")
    landed = kb("read", "app_map", "--area", "harness-billing", "--key", "queued_by_db_flag")
    res0 = (r.get("results") or [{}])[0]
    check("outbox: replay RETARGETS a queued dead --db onto the live brain",
          r.get("replayed") == 1 and r.get("remaining") == 0 and depth == 0
          and res0.get("retargeted_from") == UNREACHABLE and landed.get("count") == 1,
          "replayed={} remaining={} landed={}".format(
              r.get("replayed"), r.get("remaining"), landed.get("count")))

    r = kb("outbox", "--clear", expect_ok=False)
    check("outbox --clear refuses without --yes", r.get("ok") is False and "yes" in str(r.get("error")))
    kb("outbox", "--clear", "--yes")

    # the realistic outage: the DSN itself was down, so the replayed argv resolves the real one
    outage = {"KB_DATABASE_URL": UNREACHABLE, kbmod.TAG_ENV["ticket"]: HTICKET,
              kbmod.TAG_ENV["loop_id"]: MARK, kbmod.TAG_ENV["run_id"]: MARK}
    q1 = kb("write", "app_map", "--kind", "selector", "--area", "Harness Billing",
            "--key", "queued_then_replayed", "--value", json.dumps({"selector": "[data-test=deferred]"}),
            "--source", MARK, env=outage, expect_ok=False)
    q2 = kb("metric", "--name", "harness_queued_metric", "--value", "7", "--loop-id", MARK,
            "--source", MARK, env=outage, expect_ok=False)
    q3 = kb("state", "--key", "harness-queued-state", "--set", '{"queued": true}',
            "--source", MARK, env=outage, expect_ok=False)
    check("outbox: write/metric/state --set all queue on a DSN outage",
          q1.get("queued") is True and q2.get("queued") is True and q3.get("queued") is True
          and kb("outbox", "--status").get("count") == 3)

    r = kb("outbox", "--replay")
    landed = kb("read", "app_map", "--kind", "selector", "--area", "harness-billing",
                "--key", "queued_then_replayed")
    st = kb("state", "--key", "harness-queued-state")
    check("outbox --replay drains the spool and the deferred writes LAND",
          r.get("replayed") == 3 and r.get("remaining") == 0
          and kb("outbox", "--status").get("count") == 0
          and landed.get("count") == 1 and st.get("found") is True,
          "replayed={} app_map={} state={}".format(r.get("replayed"), landed.get("count"), st.get("found")))
    check("outbox replay preserves the run's env tags (ticket survives the outage)",
          landed["rows"][0].get("ticket") == HTICKET, landed["rows"][0].get("ticket"))

    # 18) RECEIPTS — what a run wrote is provable without the agent remembering
    r = kb("receipt", "--ticket", HTICKET, "--since", "7d")
    counts = r.get("counts_by_store", {})
    check("receipt: the ledger groups this ticket's writes by store",
          r.get("ok") is True and counts.get("app_map", 0) >= 1 and counts.get("fix_lessons", 0) >= 1
          and counts.get("expectations", 0) >= 1 and counts.get("metrics", 0) >= 1
          and MARK in r.get("sources", []),
          "counts={}".format(counts))
    check("receipt: env auto-tagging filled run_id/loop_id with no flag passed",
          MARK in r.get("run_ids", []) and MARK in r.get("loop_ids", []),
          "runs={} loops={}".format(r.get("run_ids"), r.get("loop_ids")))

    # 19) HISTORY — the router is no longer stateless on a retried ticket
    kb("metric", "--name", "escalation", "--value", "1", "--loop-id", MARK, "--ticket", HTICKET,
       "--source", MARK)
    kb("metric", "--name", "loop_outcome", "--value", "1", "--loop-id", MARK, "--ticket", HTICKET,
       "--source", MARK, "--metadata", json.dumps({"outcome": "escalated"}))
    r = kb("history", "--ticket", HTICKET)
    check("history: attempts, writes-by-store, escalations, last_outcome",
          r.get("ok") is True and r.get("attempt_count", 0) >= 2 and r.get("escalations") == 1
          and (r.get("last_outcome") or {}).get("outcome") == "escalated"
          and "app_map" in r.get("writes_by_store", {}) and r.get("cold") is False,
          "attempts={} escalations={}".format(r.get("attempt_count"), r.get("escalations")))
    r = kb("history", "--ticket", "HARNESS-NEVER-SEEN")
    check("history: a COLD ticket returns empty structures, ok:true (never an error)",
          r.get("ok") is True and r.get("attempts") == [] and r.get("writes_by_store") == {}
          and r.get("escalations") == 0 and r.get("last_outcome") is None and r.get("cold") is True)

    # 20) CONFIRM — freshness without an edit (what makes selector staleness falsifiable)
    before = kb("read", "app_map", "--id", id1)["rows"][0]
    r = kb("confirm", "app_map", "--id", id1, "--source", MARK, "--ticket", HTICKET)
    after = r.get("row", {})
    check("confirm app_map: refreshes last_confirmed_at, does NOT bump version or change value",
          r.get("ok") is True and after.get("version") == before.get("version")
          and after.get("value") == before.get("value")
          and after.get("last_confirmed_at") > before.get("last_confirmed_at")
          and (r.get("receipt") or {}).get("id") is not None,
          "v{}→v{}".format(before.get("version"), after.get("version")))

    # 21) SEARCH app_map — fuzzy, embedder-free lookup by content
    r = kb("search", "app_map", "--query", "billing-submit-v2", "--k", "10")
    ids = [x["id"] for x in r.get("results", [])]
    check("search app_map: ILIKE over key/area/value finds a row by its VALUE",
          r.get("ok") is True and id1 in ids, "ids={}".format(ids))
    r = kb("search", "app_map", "--query", "queued_then_replayed", "--area", "Harness Billing",
           "--kind", "Selector")
    check("search app_map: --area/--kind filters normalize too",
          r.get("count") == 1, "count={} norm={}".format(r.get("count"), r.get("normalization")))

    # 22) NORMALIZATION — write mixed-case, read canonical (and vice versa)
    r = kb("write", "app_map", "--kind", "Selector", "--area", "Harness Foo Bar",
           "--key", "norm_key", "--value", json.dumps({"selector": "#norm"}), "--source", MARK)
    stored = r.get("row", {})
    check("normalization on WRITE: 'Selector'/'Harness Foo Bar' stored as 'selector'/'harness-foo-bar'",
          stored.get("kind") == "selector" and stored.get("area") == "harness-foo-bar",
          "{}/{}".format(stored.get("kind"), stored.get("area")))
    r = kb("read", "app_map", "--kind", "selector", "--area", "harness-foo-bar", "--key", "norm_key")
    check("normalization on READ: the canonical form finds it", r.get("count") == 1)
    r = kb("read", "app_map", "--area", "Harness/Foo_Bar")
    check("normalization on READ: a differently-punctuated area finds the same slice",
          r.get("count") == 1 and r["normalization"]["area"]["tried"][0] == "harness-foo-bar",
          "matched={}".format(r["normalization"]["area"]["matched"]))
    r = kb("write", "app_map", "--kind", "Selector", "--area", "harness-foo-bar", "--key", "norm_key",
           "--value", json.dumps({"selector": "#norm2"}), "--source", MARK)
    check("normalization: a re-write matches the SAME row (no duplicate fork)",
          r.get("action") in ("update", "update_refresh") and r["row"]["id"] == stored.get("id")
          and r.get("matched") == "normalized")

    # 22b) the pre-migration bridge: a row stored un-normalized is still found and updated
    conn = kbmod.connect(DSN)
    with conn.cursor() as c:
        c.execute("""INSERT INTO app_map(kind, area, key, value, source)
                     VALUES ('Legacy_Kind', 'Harness Legacy Area', 'legacy_key',
                             '{"selector": "#legacy"}', %s) RETURNING id""", (MARK,))
        legacy_id = c.fetchone()["id"]
    conn.commit(); conn.close()
    r = kb("read", "app_map", "--kind", "legacy-kind", "--area", "harness-legacy-area", "--key", "legacy_key")
    check("pre-migration bridge: a raw-cased row is found by its NORMALIZED name",
          r.get("count") == 1 and r["rows"][0]["id"] == legacy_id
          and r["normalization"]["area"]["matched"] == "normalized_scan",
          "matched={}".format(r["normalization"]["area"]["matched"]))
    r = kb("write", "app_map", "--kind", "Legacy_Kind", "--area", "Harness Legacy Area",
           "--key", "legacy_key", "--value", json.dumps({"selector": "#legacy2"}), "--source", MARK)
    check("pre-migration bridge: a write UPDATES the raw row instead of forking a duplicate",
          r["row"]["id"] == legacy_id and r.get("matched") == "raw"
          and "unnormalized_row" in " ".join(r.get("warnings", [])),
          "matched={} action={}".format(r.get("matched"), r.get("action")))

    # 22c) an unknown kind still writes — a hard error here would break existing callers
    r = kb("write", "app_map", "--kind", "not-a-real-kind", "--area", "harness-foo-bar",
           "--key", "weird", "--value", json.dumps({"x": 1}), "--source", MARK)
    check("unknown kind writes with warning:'unknown_kind' (never a hard error)",
          r.get("ok") is True and r.get("warning") == "unknown_kind")

    # 22d) flakiness derives its area from the test id (--spec) when none is given (R7 slices need it)
    r = kb("write", "flakiness", "--spec", "harness-derive_test.py > widget renders", "--source", MARK)
    check("flakiness: --area derived from the spec's leading segment",
          r["row"]["area"] == "harness-derive" and r.get("area_derived_from_spec") is True,
          "area={}".format(r["row"]["area"]))

    # 23) STATE — durable key/value shared across machines (the dream watermark)
    kb("state", "--key", "harness-watermark", "--set", json.dumps({"ts": "2000-01-01T00:00:00Z"}),
       "--source", MARK)
    r = kb("state", "--key", "harness-watermark")
    check("state: set → get round-trips JSON",
          r.get("found") is True and r["value"]["ts"] == "2000-01-01T00:00:00Z")
    r = kb("state", "--key", "harness-never-set")
    check("state: a missing key is ok:true + found:false (not an error)",
          r.get("ok") is True and r.get("found") is False and r.get("value") is None)
    kb("state", "--key", "harness-watermark", "--delete", "--source", MARK)
    check("state: delete", kb("state", "--key", "harness-watermark").get("found") is False)

    # ── HASH DEDUPE: retry is safe with the embedder UP **or** DOWN ─────────────
    # Under the live 429 the vector guard cannot run (it needs a vector for the incoming
    # text), so every retry used to INSERT a clone — and retry is this design's own recovery
    # path. These checks hold in BOTH embedder modes: the hash guard runs first, always.

    # 24) the normalization rule itself, in-process and deterministic
    same = (kbmod.text_hash("  The Toast   fires, but state is UNCHANGED!! ")
            == kbmod.text_hash("the toast fires but state is unchanged"))
    diff = kbmod.text_hash("task owner cleared") != kbmod.text_hash("task owner retained")
    check("text_hash: case/punctuation/whitespace normalize, different text stays different",
          same and diff and kbmod.normalize_text_for_hash(" A,  b! ") == "a b",
          kbmod.text_hash("a")[:16])

    SYM = "harness: the same lesson written twice must bump, never clone"
    r = kb("write", "fix_lessons", "--area", "Harness Dedupe", "--source", MARK, "--ticket", HTICKET,
           "--symptom", SYM, "--root-cause", "no exact-duplicate guard before the vector guard",
           "--fix-shape", "hash the normalized text and check it first", *NO_VECTOR_DEDUPE)
    d1 = r.get("row", {}).get("id")
    check("hash dedupe: the first write INSERTS and stores a symptom_hash",
          r.get("action") == "insert" and r.get("dedup_by") is None
          and bool(r.get("row", {}).get("symptom_hash")),
          "id={} hash={}".format(d1, str(r.get("row", {}).get("symptom_hash"))[:12]))

    # 25) the headline: the SAME text again bumps by hash — no embedder involved
    r = kb("write", "fix_lessons", "--area", "Harness Dedupe", "--source", MARK, "--ticket", HTICKET,
           "--symptom", SYM)
    check("hash dedupe: an identical retry BUMPS (dedup_by=hash), whatever the embedder is doing",
          r.get("action") == "bump" and r.get("dedup_by") == "hash"
          and r.get("row", {}).get("id") == d1 and r.get("row", {}).get("occurrences") == 2
          and r.get("dedup_skipped") is True and (r.get("receipt") or {}).get("id") is not None,
          "action={} by={} occ={}".format(r.get("action"), r.get("dedup_by"),
                                          r.get("row", {}).get("occurrences")))

    # 26) same lesson, noisier spelling AND a differently-cased area — one bucket
    r = kb("write", "fix_lessons", "--area", "harness_dedupe", "--source", MARK, "--ticket", HTICKET,
           "--symptom", "HARNESS:  the same lesson, written twice — must bump; never clone!",
           "--root-cause", "harness: root cause filled in on the retry")
    after = kb("read", "fix_lessons", "--id", d1)["rows"][0]
    check("hash dedupe: punctuation/case + 'Harness Dedupe' vs 'harness_dedupe' are ONE bucket",
          r.get("action") == "bump" and r.get("dedup_by") == "hash"
          and r.get("row", {}).get("id") == d1 and r.get("row", {}).get("occurrences") == 3
          and after.get("root_cause") == "harness: root cause filled in on the retry",
          "occ={} root_cause coalesced={}".format(r.get("row", {}).get("occurrences"),
                                                  after.get("root_cause")[:32]))

    # 27) a genuinely different symptom is never a HASH hit — proved with the VECTOR guard
    # pinned off, so the assertion is the same in both embedder modes and the write cannot
    # touch a row the harness does not own. (This probe measured cosine 0.4468 against a real
    # production lesson in another area, against a 0.45 default: it was three thousandths from
    # bumping a real row, stamping HARNESS-0000 on it via bump's COALESCE, and having cleanup
    # delete it. The vector guard's reach is proved deliberately in 27b instead.)
    r = kb("write", "fix_lessons", "--area", "Harness Dedupe", "--source", MARK, "--ticket", HTICKET,
           "--symptom", "harness: an unrelated symptom about invoice totals rounding differently",
           *NO_VECTOR_DEDUPE)
    check("hash dedupe: a different symptom is never a hash hit (it INSERTS its own row)",
          r.get("ok") is True and r.get("action") == "insert" and r.get("dedup_by") is None
          and r.get("row", {}).get("id") != d1,
          "action={} by={} id={}".format(r.get("action"), r.get("dedup_by"),
                                         r.get("row", {}).get("id")))

    # 27b) the VECTOR guard's own proof, with no way to reach a foreign row: write an anchor
    # with the guard off, MEASURE the paraphrase's nearest rows, and only then write it — with
    # the threshold pinned between our anchor and the runner-up. If a real lesson were ever
    # nearest, this check FAILS instead of quietly bumping production knowledge.
    VSYM = "harness anchor: the list keeps a stale row after the delete call returns"
    VPARA = "harness paraphrase: after a delete succeeds the list still shows the removed row"
    va = kb("write", "fix_lessons", "--area", "Harness Vector", "--source", MARK,
            "--ticket", HTICKET, "--symptom", VSYM, *NO_VECTOR_DEDUPE).get("row", {}).get("id")
    if live:
        thr, near = vector_bump_threshold(va, VPARA)
        r = (kb("write", "fix_lessons", "--area", "Harness Vector", "--source", MARK,
                "--ticket", HTICKET, "--symptom", VPARA, "--dup-threshold", thr)
             if thr else {"skipped": "the anchor was not the nearest row — refused to write"})
        check("VECTOR dedupe: a paraphrase BUMPS the anchor (dedup_by=vector), never a clone",
              thr is not None and r.get("action") == "bump" and r.get("dedup_by") == "vector"
              and r.get("row", {}).get("id") == va and r.get("row", {}).get("occurrences") == 2
              and r.get("dedup_skipped") is False and r.get("similarity", 0) >= 0.45,
              "nearest={} action={} by={} sim={}".format(
                  [(x["id"], round(x["similarity"], 4)) for x in near],
                  r.get("action"), r.get("dedup_by"), r.get("similarity")))
    else:
        r = kb("write", "fix_lessons", "--area", "Harness Vector", "--source", MARK,
               "--ticket", HTICKET, "--symptom", VPARA)
        check("DEGRADED: with no embedder the vector guard cannot run — a paraphrase is stored "
              "as its own pending row, never merged on a guess",
              r.get("action") == "insert" and r.get("dedup_by") is None
              and r.get("dedup_skipped") is True and r.get("embed_status") == "pending"
              and r.get("row", {}).get("id") != va,
              "action={} by={} status={}".format(r.get("action"), r.get("dedup_by"),
                                                 r.get("embed_status")))

    # 28) the same guard on the bless queue: an identical proposal returns the EXISTING row
    STMT = "Harness: an identical proposal must never create a second row in the bless queue."
    p1 = kb("propose", "expectation", "--area", "Harness Dedupe", "--source", MARK,
            "--ticket", HTICKET, "--statement", STMT)
    p2 = kb("propose", "expectation", "--area", "harness-dedupe", "--source", MARK,
            "--ticket", HTICKET, "--statement", STMT)
    dup_id = p1.get("row", {}).get("id")
    same_stmt = kb("read", "expectations", "--area", "harness-dedupe")
    n_stmt = sum(1 for x in same_stmt.get("rows", []) if x.get("statement") == STMT)
    check("hash dedupe: a duplicate PROPOSAL returns the existing row (ok:true, no second row)",
          p2.get("ok") is True and p2.get("_rc") == 0 and p2.get("action") == "duplicate"
          and p2.get("duplicate_of") == dup_id and p2.get("row", {}).get("id") == dup_id
          and p2.get("dedup_by") == "hash" and p2.get("receipt") is None and n_stmt == 1,
          "action={} duplicate_of={} rows_with_that_statement={}".format(
              p2.get("action"), p2.get("duplicate_of"), n_stmt))

    # 29) …but a FORGOTTEN row must not block a fresh proposal (it is deliberately out of play)
    kb("forget", "expectations", "--id", dup_id, "--soft", "--source", MARK)
    p3 = kb("propose", "expectation", "--area", "Harness Dedupe", "--source", MARK,
            "--ticket", HTICKET, "--statement", STMT)
    check("hash dedupe: a forgotten twin does not block a new proposal",
          p3.get("action") == "propose" and p3.get("row", {}).get("id") != dup_id,
          "action={} id={}".format(p3.get("action"), p3.get("row", {}).get("id")))

    # ── UNDO: one command reverses a bad run, from the ledger ───────────────────
    # The fixture PINS THE DEDUPE PATH so the ledger shape is identical whether the embedder is
    # up or down: run A's lesson is written with the vector guard off (a guaranteed INSERT),
    # and run B repeats the SAME TEXT so the hash guard — which needs no embedder — guarantees
    # a BUMP. Written while the embedder was down, this fixture leaned on "a new symptom always
    # inserts", which is only true on the hash path: with vector dedupe live, run A's write
    # bumped a neighbouring harness lesson at cosine 0.76, so its ledger row said `bump`, undo
    # correctly decremented instead of deleting, and four checks failed on the fixture's
    # assumption rather than on undo. The fixture now states the shape it depends on, and the
    # first check below fails loudly if the writes ever stop producing it.
    UA, UB = MARK + "-undo-a", MARK + "-undo-b"

    ua_map = kb("write", "app_map", "--kind", "selector", "--area", "Harness Undo",
                "--key", "undo_target", "--value", json.dumps({"selector": "#undo"}),
                "--source", MARK, "--ticket", HTICKET, "--run-id", UA)["row"]["id"]
    USYM = "harness: a lesson one run created and another run bumped"
    wa = kb("write", "fix_lessons", "--area", "Harness Undo", "--source", MARK,
            "--ticket", HTICKET, "--run-id", UA, "--symptom", USYM, *NO_VECTOR_DEDUPE)
    ua_les = wa["row"]["id"]
    ua_exp = kb("propose", "expectation", "--area", "Harness Undo", "--source", MARK,
                "--ticket", HTICKET, "--run-id", UA,
                "--statement", "Harness: an undo soft-forgets a proposal, it never deletes it.")["row"]["id"]
    kb("metric", "--name", "harness_undo_metric", "--value", "3", "--loop-id", UA,
       "--ticket", HTICKET, "--run-id", UA, "--source", MARK)

    # run B only TOUCHES run A's rows: a hash bump + an app_map update_refresh
    wb = kb("write", "fix_lessons", "--area", "Harness Undo", "--source", MARK, "--ticket", HTICKET,
            "--run-id", UB, "--symptom", USYM)
    mb = kb("write", "app_map", "--kind", "selector", "--area", "Harness Undo", "--key", "undo_target",
            "--value", json.dumps({"selector": "#undo-v2"}), "--source", MARK, "--ticket", HTICKET,
            "--run-id", UB)

    # 29b) the fixture states its own ledger shape, in either embedder mode. Without this the
    # four undo checks below could silently start asserting a different sequence of actions.
    check("undo fixture: run A CREATED the lesson (insert) and run B only BUMPED it by hash + "
          "refreshed the app_map row — the same ledger shape on either embedder path",
          wa.get("action") == "insert" and wa.get("dedup_by") is None
          and wb.get("action") == "bump" and wb.get("dedup_by") == "hash"
          and wb.get("row", {}).get("id") == ua_les
          and wb.get("row", {}).get("occurrences") == 2
          and mb.get("action") == "update_refresh" and mb.get("row", {}).get("id") == ua_map,
          "A={}/{} B={}/{} occ={} map={}".format(
              wa.get("action"), wa.get("dedup_by"), wb.get("action"), wb.get("dedup_by"),
              wb.get("row", {}).get("occurrences"), mb.get("action")))

    # 30) dry run is the DEFAULT for a destructive verb — plan only, nothing changes
    r = kb("undo", "--run-id", UB, "--ticket", HTICKET)
    occ_now = lesson_occ(ua_les)
    modes = sorted(x["mode"] for x in r.get("results", []))
    check("undo: DRY RUN is the default — a full plan, undone:0, nothing changed",
          r.get("ok") is True and r.get("dry_run") is True and r.get("planned") == 2
          and r.get("undoable") == 1 and r.get("undone") == 0 and occ_now == 2
          and modes == ["decrement", "skipped_unreversible"]
          and "--yes" in str(r.get("note")),
          "planned={} undoable={} modes={} occ={}".format(r.get("planned"), r.get("undoable"),
                                                          modes, occ_now))

    # 31) applying run B's undo DECREMENTS the bump — it must never delete a row run B
    #     did not create, and it cannot invent the app_map value the update overwrote
    r = kb("undo", "--run-id", UB, "--ticket", HTICKET, "--yes")
    still = kb("read", "fix_lessons", "--id", ua_les)
    map_still = kb("read", "app_map", "--id", ua_map)
    check("undo: a BUMP is reversed by a decrement, and a row the run didn't create survives",
          r.get("undone") == 1 and r.get("skipped_unreversible") == 1
          and still.get("count") == 1 and (still.get("rows") or [{}])[0].get("occurrences") == 1
          and map_still.get("count") == 1,
          "occ={} app_map_rows={}".format((still.get("rows") or [{}])[0].get("occurrences"),
                                          map_still.get("count")))

    # 32) run A created everything, so undoing it removes it — expectations SOFT
    r = kb("undo", "--run-id", UA, "--ticket", HTICKET, "--yes")
    gone_map = kb("read", "app_map", "--id", ua_map)
    gone_les = kb("read", "fix_lessons", "--id", ua_les)
    # …read defensively: if undo ever HARD-deleted a proposal this must report a red check,
    # not die with an IndexError before the summary prints.
    exp_row = (kb("read", "expectations", "--id", ua_exp).get("rows") or [{}])[0]
    met = kb("metrics", "--summary", "--metric", "harness_undo_metric", "--loop-id", UA)
    by = r.get("by_store", {})
    check("undo: rows the run CREATED are removed — hard, except a soft-forgotten expectation",
          r.get("ok") is True and r.get("dry_run") is False and r.get("undone") == r.get("planned")
          and gone_map.get("count") == 0 and gone_les.get("count") == 0
          and exp_row.get("status") == "forgotten" and met.get("summary") == []
          and by.get("expectations", {}).get("undone") == 1,
          "undone={}/{} by_store={}".format(r.get("undone"), r.get("planned"), by))

    # 33) idempotent + it must never select its OWN undo rows (verb='undo' is excluded)
    r2 = kb("undo", "--run-id", UA, "--ticket", HTICKET, "--yes")
    verbs = sorted(set(x["verb"] for x in r2.get("results", [])))
    check("undo: re-runnable — a second pass is all skipped_missing and never eats its own rows",
          r2.get("planned") == r.get("planned") and r2.get("undone") == 0
          and r2.get("skipped_missing") == r2.get("planned") and "undo" not in verbs,
          "planned={} missing={} verbs={}".format(r2.get("planned"), r2.get("skipped_missing"), verbs))

    # 33b) THE DECREMENT IS THE ONE MODE THAT DOES NOT EVIDENCE ITSELF. A deleted row proves
    # its own reversal by being gone and a soft-forgotten one by its status, but a decremented
    # counter looks exactly like a counter that was never decremented — so a second pass used
    # to decrement it AGAIN, walking a lesson that PREDATES the bad run down to the floor of 1,
    # from a verb documented as idempotent. Hash-only dedupe hides this (bumps are rare);
    # with the vector guard live a real run's undo plan is mostly decrements, and the
    # same damage follows from the ordinary habit of re-running `undo --ticket` after
    # `undo --run-id`. The bump is produced by the VECTOR guard when the embedder is up and by
    # the HASH guard when it is down — the invariant asserted is identical either way.
    UP, UD = MARK + "-undo-pre", MARK + "-undo-d"
    PSYM = "harness: a lesson that predates the run being undone"
    PPARA = "harness: a lesson already in the brain before the run that gets undone"
    pid = kb("write", "fix_lessons", "--area", "Harness Undo", "--source", MARK, "--ticket", HTICKET,
             "--run-id", UP, "--symptom", PSYM, *NO_VECTOR_DEDUPE)["row"]["id"]
    for _ in range(2):                      # …with real history on it: occurrences 1 → 3
        kb("write", "fix_lessons", "--area", "Harness Undo", "--source", MARK, "--ticket", HTICKET,
           "--run-id", UP, "--symptom", PSYM)
    pre_occ = lesson_occ(pid)

    thr, near = vector_bump_threshold(pid, PPARA) if live else (None, [])
    bad = (kb("write", "fix_lessons", "--area", "Harness Undo", "--source", MARK, "--ticket", HTICKET,
              "--run-id", UD, "--symptom", PPARA, "--dup-threshold", thr) if thr else
           kb("write", "fix_lessons", "--area", "Harness Undo", "--source", MARK, "--ticket", HTICKET,
              "--run-id", UD, "--symptom", PSYM))
    want_by = "vector" if live else "hash"
    passes = []
    for _ in range(3):
        u = kb("undo", "--run-id", UD, "--ticket", HTICKET, "--yes")
        passes.append((u, lesson_occ(pid)))
    (u1, occ1), (u2, occ2), (u3, occ3) = passes
    check("undo: re-running a decrement is a NO-OP — a lesson that predates the run never "
          "drops below its pre-run occurrences ({} bump)".format(want_by),
          pre_occ == 3 and bad.get("action") == "bump" and bad.get("dedup_by") == want_by
          and bad.get("row", {}).get("id") == pid and bad.get("row", {}).get("occurrences") == 4
          and u1.get("undone") == 1 and occ1 == 3
          and u2.get("undone") == 0 and u2.get("skipped_missing") == u2.get("planned") == 1
          and u3.get("undone") == 0 and occ2 == 3 and occ3 == 3
          and all(x.get("already_undone") is True for x in u2.get("results", [])),
          "by={} pre={} after passes 1/2/3 = {}/{}/{} nearest={}".format(
              bad.get("dedup_by"), pre_occ, occ1, occ2, occ3,
              [(x["id"], round(x["similarity"], 4)) for x in near]))

    # 33c) …AND SAFE WHEN RUN TWICE AT ONCE. The receipt makes a *sequential* re-run a no-op,
    # but every other undo mode re-checks itself inside its own write (`DELETE … RETURNING`,
    # `UPDATE … WHERE status <> 'forgotten' RETURNING`), so a concurrent twin lands zero rows
    # and reports skipped_missing on its own. `occurrences = occurrences - 1` has no such
    # check — it always succeeds — so the receipt is the ONLY guard, and under READ COMMITTED
    # two overlapping undos both read "no receipt yet" before either commits and both
    # decrement (measured: 3 → 2, reproducible, on the hash AND vector bump paths). The row
    # lock in _undo_one() serialises them. This is not a hypothetical on a brain several
    # sessions write to at once, and it is invisible to any check that runs one kb at a time.
    UR = MARK + "-undo-race"
    RSYM = "harness: an anchor lesson two simultaneous reversals both aim at"
    RPARA = "harness: the lesson that two overlapping undo passes each try to reverse"
    rid = kb("write", "fix_lessons", "--area", "Harness Undo", "--source", MARK, "--ticket", HTICKET,
             "--run-id", UR + "-pre", "--symptom", RSYM, *NO_VECTOR_DEDUPE)["row"]["id"]
    for _ in range(2):
        kb("write", "fix_lessons", "--area", "Harness Undo", "--source", MARK, "--ticket", HTICKET,
           "--run-id", UR + "-pre", "--symptom", RSYM)
    r_pre = lesson_occ(rid)
    rthr, rnear = vector_bump_threshold(rid, RPARA) if live else (None, [])
    rbad = (kb("write", "fix_lessons", "--area", "Harness Undo", "--source", MARK, "--ticket", HTICKET,
               "--run-id", UR, "--symptom", RPARA, "--dup-threshold", rthr) if rthr else
            kb("write", "fix_lessons", "--area", "Harness Undo", "--source", MARK, "--ticket", HTICKET,
               "--run-id", UR, "--symptom", RSYM))
    # Which guard produced the bump is NOT what this check is about — the lock has to hold for
    # both. So the assertion is that the bump landed on OUR anchor by the guard the fixture
    # actually pinned: `vector` when the embedder is up AND the anchor measured nearest,
    # `hash` when vector_bump_threshold() refused (anchor not nearest) or the embedder is down.
    # Demanding "vector" whenever `live` would be the same unpinned assumption that broke the
    # four checks above — on a shared brain another row can sit closer than our own anchor.
    r_by = "vector" if rthr else "hash"
    racers = kb_parallel(2, "undo", "--run-id", UR, "--ticket", HTICKET, "--yes")
    r_after = lesson_occ(rid)
    undone = sorted(x.get("undone") or 0 for x in racers)
    check("undo: two CONCURRENT undos of the same bump decrement it ONCE — the receipt is read "
          "under a row lock, so the loser reports skipped_missing",
          r_pre == 3 and rbad.get("action") == "bump" and rbad.get("dedup_by") == r_by
          and rbad.get("row", {}).get("id") == rid
          and rbad.get("row", {}).get("occurrences") == 4
          and all(x.get("ok") is True for x in racers) and undone == [0, 1]
          and sorted(x["mode"] for r in racers for x in r.get("results", []))
              == ["decrement", "skipped_missing"]
          and r_after == r_pre,
          "by={} pre={} after={} undone={} modes={} anchor={} nearest={}".format(
              rbad.get("dedup_by"), r_pre, r_after, undone,
              [x["mode"] for r in racers for x in r.get("results", [])], rid,
              [(x["id"], round(x["similarity"], 4)) for x in rnear]))

    r = kb("undo", expect_ok=False)
    r2 = kb("undo", "--run-id", MARK + "-never-ran")
    check("undo: refuses without an explicit selector; a selector matching nothing is ok:true/0",
          r.get("ok") is False and "--run-id" in str(r.get("error"))
          and r2.get("ok") is True and r2.get("planned") == 0 and r2.get("by_store") == {})

    # 34) the hard safety line: an undo may NEVER flip a kb_control flag back. A kill switch is
    # not a knowledge row, and "the run that set it is being undone" must not clear a pause.
    UC = MARK + "-undo-c"
    kb("flag", "--name", "harness-probe", "--on", "--by", MARK, "--run-id", UC, "--ticket", HTICKET)
    r = kb("undo", "--run-id", UC, "--ticket", HTICKET, "--yes")
    flag = kb("flag", "--name", "harness-probe", "--status")
    check("undo: kb_control is never reversed — a flag set inside the run stays set",
          r.get("planned") == 1 and r.get("undone") == 0 and r.get("skipped_unreversible") == 1
          and flag.get("value") is True,
          "planned={} undone={} flag={}".format(r.get("planned"), r.get("undone"), flag.get("value")))
    kb("flag", "--name", "harness-probe", "--off", "--by", MARK)

    # ── FORGET --SOURCE: one writer's whole contribution, planned before applied ─
    # Runs LAST: it deliberately selects every app_map row this harness wrote.
    n_mark = source_rows("app_map")
    r = kb("forget", "app_map", "--source", MARK)
    still_there = source_rows("app_map")
    check("forget --source: PLANS a bulk delete without --yes and changes nothing",
          r.get("ok") is True and r.get("action") == "forget_plan" and r.get("dry_run") is True
          and r.get("selected_by") == "source" and r.get("planned") == n_mark and n_mark >= 1
          and still_there == n_mark and r.get("matched_sources") == [MARK],
          "planned={} rows_still_there={}".format(r.get("planned"), still_there))

    r = kb("forget", "app_map", "--source", MARK, "--yes")
    left = source_rows("app_map")
    check("forget --source --yes: deletes exactly the planned rows, with a receipt each",
          r.get("action") == "forget_hard" and len(r.get("deleted", [])) == n_mark
          and len(r.get("receipts", [])) == n_mark and left == 0,
          "deleted={} left={}".format(len(r.get("deleted", [])), left))

    # SHARED BRAIN: the rows the harness does not own must be exactly as it found them.
    # The vector guard reaches across areas into real production lessons, so this is a
    # first-class check, not a comment: a bumped foreign row shows up as a changed
    # occurrences sum, and a foreign row wearing HARNESS-0000 is one cleanup() from deletion.
    fp_after = foreign_fingerprint()
    drifted = dict((i, {"was": was, "now": fp_after.get(i)})
                   for i, was in fp_before.items() if fp_after.get(i) != was)
    stamped = sorted(i for i, (_occ, tk) in fp_after.items() if tk == HTICKET)
    check("shared brain: every row the harness didn't write came out as it was found "
          "(occurrences + ticket) — rows other sessions added mid-run are theirs, not drift",
          not drifted and not stamped,
          "checked={} drifted={} harness_stamped={} added_by_others={}".format(
              len(fp_before), drifted or "none", stamped or "none",
              sorted(set(fp_after) - set(fp_before)) or "none"))

    print("\n== summary ==")
    passed = sum(1 for _, okk, _ in checks if okk)
    for name, okk, _ in checks:
        if not okk:
            print("  FAILED: {}".format(name))
    print("  MODE={} — {}/{} checks passed  (embedder {})".format(
        MODE, passed, len(checks),
        "LIVE" if live else ("DOWN, SIMULATED" if FORCE_ENV else "DOWN")))
    return 0 if passed == len(checks) else 1

def run():
    argv = sys.argv[1:]
    both = "--both" in argv
    want = "auto"
    if "--mode" in argv:
        i = argv.index("--mode") + 1
        want = argv[i] if i < len(argv) else ""
    if want not in ("auto", "live", "down"):
        print("usage: harness.py [--mode auto|live|down] [--both]"); return 2

    # Refuse to run concurrently BEFORE touching the shared brain (see LOCK_DIR).
    refusal = acquire_lock()
    if refusal:
        print("== qa-kb harness ==\n  REFUSED: " + refusal)
        return 3
    try:
        return _run_locked(want, both)
    finally:
        release_lock()

def _run_locked(want, both):
    if not both:
        return main(want)

    # --both: the live contract AND the simulated outage, in one run, with both totals.
    rc_live = main("live")
    n_live = (sum(1 for _, o, _ in checks if o), len(checks))
    print()
    rc_down = main("down")
    n_down = (sum(1 for _, o, _ in checks if o), len(checks))
    print("\n== both modes ==")
    print("  MODE=live {}/{}   MODE=down {}/{}".format(n_live[0], n_live[1], n_down[0], n_down[1]))
    if n_live[1] == 0:
        print("  !! the LIVE pass could not run — the embedder is down, so the semantic "
              "contract is UNPROVEN. The degraded contract above is not a substitute.")
    elif n_live[1] != n_down[1]:
        print("  !! the two modes ran a different NUMBER of checks — every check must have a "
              "live and a degraded form, so one path is silently skipping work")
        return 1
    return rc_live or rc_down

if __name__ == "__main__":
    if NO_BRAIN:
        print("== qa-kb harness ==\n  CANNOT RUN: no brain configured — " + NO_BRAIN)
        sys.exit(2)
    sys.exit(run())
