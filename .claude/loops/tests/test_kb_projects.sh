#!/usr/bin/env bash
# Regression: qa-kb projects + lessons + scope + overlay config + the in-place migration.
#   * project identity ($AGENT_KIT_PROJECT > git repo name > 'global'; the kit itself is 'global')
#   * overlay config lookup order ($KB_CONFIG > overlay/projects/<p> > overlay > kit) and the
#     secret env file resolved next to whichever config was chosen
#   * scope: a local host is refused under `shared`, allowed under a declared `single-machine`
#   * project isolation: a write in project A is invisible to B; 'global' rows reach both;
#     the same app_map key can exist once per project
#   * the lessons store: write / search (active only, scored) / bless / reject / near-duplicate
#     bump (hash AND vector guard) / forget / undo / pending-then-reembed
#   * migration: a brain built from the PRE-project schema upgrades in place via
#     `kb schema --apply`, keeps every row as project='global', and drains a write that was
#     queued as schema_outdated before the upgrade.
#
# ISOLATION: everything runs in two scratch Postgres SCHEMAS (kbt_<pid>_new / kbt_<pid>_old)
# created inside whatever database the configured brain names, via search_path, and dropped
# (CASCADE) on exit. It never reads or writes the brain's own tables. Uses the TEST-ONLY stub
# embedder (deterministic, meaning-free) — exact-text similarity is 1.0, which is all the
# dedupe/search plumbing checks here need. SKIPS when the brain is unreachable or a scratch
# schema cannot be created.
# Tests never use an owner's overlay: it points at a real brain. Point brain suites at a test
# database with KB_CONFIG (a config naming a throwaway DB) or KB_DATABASE_URL instead.
unset AGENT_KIT_OVERLAY AGENT_KIT_PROJECT

. "$(cd "$(dirname "$0")" && pwd)/lib/brain_lock.sh"
brain_lock_acquire "test_kb_projects" || exit 3
trap brain_lock_release EXIT

SK="$(cd "$(dirname "$0")/../../skills" && pwd)"; export SK
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"; export ROOT
KBPY="$SK/qa-kb/scripts/kb.py"

if ! python3 "$KBPY" pause --status >/dev/null 2>&1; then
  echo "  kb_projects: SKIPPED (brain unreachable)"; exit 0
fi

python3 - <<'PY'
import atexit, json, os, re, shutil, subprocess, sys, tempfile

SK, ROOT = os.environ["SK"], os.environ["ROOT"]
SCRIPTS = os.path.join(SK, "qa-kb", "scripts")
KB = os.path.join(SCRIPTS, "kb.py")
sys.path.insert(0, SCRIPTS)
import kb as kbmod
from psycopg2.extensions import make_dsn

# The pre-project schema, pinned to the last commit before projects/lessons landed, so this
# test keeps proving the upgrade path after the new schema is committed. HEAD is the fallback
# for a checkout without that history.
OLD_SCHEMA_REF = "1226a35df7aa17e74e45a53d3a961ba1bc929288"

passed, failed, skipped = [0], [0], []
def ck(name, got, want):
    if got == want:
        passed[0] += 1
    else:
        failed[0] += 1
        print("  [FAIL] {} — got {!r} want {!r}".format(name, got, want))
def ok_(name, cond, detail=""):
    ck(name + ((" — " + detail) if (detail and not cond) else ""), bool(cond), True)

BASE = kbmod.resolve_dsn()
PID = os.getpid()
NEW, OLD = "kbt_{}_new".format(PID), "kbt_{}_old".format(PID)
TMP = tempfile.mkdtemp(prefix="kbt-projects-")
NOGIT = os.path.join(TMP, "nogit"); os.makedirs(NOGIT)

def scratch_dsn(schema):
    return make_dsn(BASE, options="-csearch_path={},public".format(schema))

def admin(sql, params=None, schema=None):
    c = kbmod.connect(scratch_dsn(schema) if schema else BASE)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else None
        c.commit()
        return rows
    finally:
        c.close()

def drop_schemas():
    for s in (NEW, OLD):
        try:
            admin("DROP SCHEMA IF EXISTS {} CASCADE".format(s))
        except Exception as e:                       # noqa: BLE001
            print("  [WARN] could not drop scratch schema {}: {}".format(s, e))
    shutil.rmtree(TMP, ignore_errors=True)

try:
    for s in (NEW, OLD):
        admin("CREATE SCHEMA {}".format(s))
except Exception as e:                               # noqa: BLE001
    print("  kb_projects: SKIPPED (cannot create a scratch schema: {})".format(
        str(e).strip().splitlines()[0][:120]))
    drop_schemas()
    sys.exit(0)
atexit.register(drop_schemas)

# Every kb subprocess: scratch schema, temp outbox, a non-git cwd, no inherited project/tags.
BASE_ENV = dict(os.environ)
for k in ("AGENT_KIT_PROJECT", "AGENT_KIT_OVERLAY", "LOOP_TICKET", "LOOP_ID", "LOOP_RUN_ID"):
    BASE_ENV.pop(k, None)
BASE_ENV["KB_OUTBOX"] = os.path.join(TMP, "outbox.jsonl")

def kb(*args, schema=NEW, project=None, env=None, cwd=NOGIT, stub=True):
    e = dict(BASE_ENV)
    if schema:
        e["KB_DATABASE_URL"] = scratch_dsn(schema)
    if project:
        e["AGENT_KIT_PROJECT"] = project
    e.update(env or {})
    for k, v in list(e.items()):
        if v is None:
            e.pop(k)
    argv = [sys.executable, KB] + (["--embedder", "stub"] if stub else []) + [str(x) for x in args]
    r = subprocess.run(argv, capture_output=True, text=True, env=e, cwd=cwd)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        out = {"ok": None, "_stdout": r.stdout[-300:], "_stderr": r.stderr[-300:]}
    out["_rc"] = r.returncode
    return out

def py(code, env=None, cwd=NOGIT):
    """Import kb in a fresh interpreter (config + project resolve at import) and run code."""
    e = dict(BASE_ENV)
    e.update(env or {})
    for k, v in list(e.items()):
        if v is None:
            e.pop(k)
    r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r)\nimport kb\n%s"
                        % (SCRIPTS, code)], capture_output=True, text=True, env=e, cwd=cwd)
    return (r.stdout.strip() or r.stderr.strip()[-300:])

# ── 1. project identity ─────────────────────────────────────────────────────
P = "print(kb.PROJECT, kb.PROJECT_HOW)"
ck("env project wins (normalized)", py(P, env={"AGENT_KIT_PROJECT": "My App_2"}), "my-app-2 env")
repo = os.path.join(TMP, "Some_Repo"); os.makedirs(os.path.join(repo, "sub"))
subprocess.run(["git", "init", "-q", repo], check=True)
ck("git toplevel basename", py(P, cwd=os.path.join(repo, "sub")), "some-repo git")
ck("no git repo -> global", py(P), "global default")
ck("the kit itself -> global", py(P, cwd=ROOT), "global kit")
ck("--project overrides (normalized)", kb("status", "--project", "Flag Proj").get("project"), "flag-proj")

# ── 2. overlay config lookup order + secret file beside the chosen config ────
OV = os.path.join(TMP, "overlay")
os.makedirs(os.path.join(OV, "projects", "proj-a"))
DEAD = "host=127.0.0.1 port=1 dbname=nope connect_timeout=2"
def write_cfg(path, secret_val, extra=None):
    d = os.path.dirname(path)
    cfg = {"database_url": DEAD, "embedding": {"api_key_env": "KBTEST_KEY"}}
    cfg.update(extra or {})
    with open(path, "w") as fh:
        json.dump(cfg, fh)
    with open(os.path.join(d, "kb.secret.env"), "w") as fh:
        fh.write("KBTEST_SECRET_PROBE={}\n".format(secret_val))
write_cfg(os.path.join(OV, "kb.config.json"), "overlay-root")
write_cfg(os.path.join(OV, "projects", "proj-a", "kb.config.json"), "overlay-proj-a")
EXPL = os.path.join(TMP, "explicit.json"); write_cfg(EXPL, "explicit")
C = "import os; print(kb.CONFIG_SOURCE, os.environ.get('KBTEST_SECRET_PROBE'))"
noenv = {"KB_CONFIG": None, "KBTEST_SECRET_PROBE": None}
ck("KB_CONFIG beats the overlay",
   py(C, env=dict(noenv, KB_CONFIG=EXPL, AGENT_KIT_OVERLAY=OV, AGENT_KIT_PROJECT="proj-a")),
   "KB_CONFIG explicit")
ck("overlay/projects/<project> first",
   py(C, env=dict(noenv, AGENT_KIT_OVERLAY=OV, AGENT_KIT_PROJECT="proj-a")),
   "overlay-project overlay-proj-a")
ck("overlay root when no per-project config",
   py(C, env=dict(noenv, AGENT_KIT_OVERLAY=OV, AGENT_KIT_PROJECT="proj-b")),
   "overlay overlay-root")
os.makedirs(os.path.join(TMP, "empty-overlay"))
ck("kit default when the overlay has no config",
   py(C, env=dict(noenv, AGENT_KIT_OVERLAY=os.path.join(TMP, "empty-overlay"))), "kit None")
ck("kit default with no overlay", py(C, env=dict(noenv, AGENT_KIT_OVERLAY=None)), "kit None")
ck("overlay folder spelled like the repo dir",
   py(C, env=dict(noenv, AGENT_KIT_OVERLAY=OV),
      cwd=(lambda d: (os.makedirs(d), subprocess.run(["git", "init", "-q", d]), d)[2])(
          os.path.join(TMP, "proj-a"))),
   "overlay-project overlay-proj-a")
ck("--project re-selects the per-project overlay config",
   kb("status", "--project", "proj-a", schema=None,
      env=dict(noenv, KB_DATABASE_URL=None, AGENT_KIT_OVERLAY=OV)).get("config_source"),
   "overlay-project")
EMPTY = os.path.join(TMP, "emptytarget.json")
with open(EMPTY, "w") as fh:
    json.dump({"database_url": ""}, fh)
r = kb("status", schema=None, env={"KB_CONFIG": EMPTY, "KB_DATABASE_URL": None})
ok_("no-target error names the CHOSEN config", EMPTY in str(r.get("error")), str(r.get("error"))[:120])
r = kb("status", schema=None, env={"KB_CONFIG": os.path.join(TMP, "missing.json"),
                                   "KB_DATABASE_URL": None})
ok_("a KB_CONFIG that does not exist is an error", r.get("ok") is False and "no such file" in
    str(r.get("error")), str(r.get("error"))[:120])

# ── 3. scope ────────────────────────────────────────────────────────────────
LOCAL = kbmod._is_unshared_host(kbmod._dsn_host(BASE))
def scope_cfg(name, scope):
    p = os.path.join(TMP, name)
    with open(p, "w") as fh:
        json.dump({"database_url": scratch_dsn(NEW), "scope": scope}, fh)
    return p
SHARED_CFG, SINGLE_CFG = scope_cfg("shared.json", "shared"), scope_cfg("single.json", "single-machine")
noover = {"KB_DATABASE_URL": None, "KB_ALLOW_UNSHARED": None}
r = kb("pause", "--status", schema=None, env=dict(noover, KB_CONFIG=SHARED_CFG,
                                                    KB_DATABASE_URL=DEAD))
ok_("shared scope REFUSES a localhost target",
    r.get("ok") is False and "UnsharedBrainRefused" in str(r.get("error")), str(r)[:160])
r = kb("pause", "--status", schema=None, env=dict(noover, KB_CONFIG=scope_cfg("bad.json", "everyone")))
ok_("an unknown scope is a config error", r.get("ok") is False and "unknown scope" in str(r.get("error")))
if LOCAL:
    r = kb("pause", "--status", schema=None, env=dict(noover, KB_CONFIG=SINGLE_CFG))
    ck("single-machine scope ALLOWS the localhost target", r.get("ok"), True)
    r = kb("status", schema=None, env=dict(noover, KB_CONFIG=SINGLE_CFG))
    ck("status reports scope single-machine", r.get("scope"), "single-machine")
    r = kb("pause", "--status", schema=None, env=dict(noover, KB_CONFIG=SHARED_CFG,
                                                        KB_ALLOW_UNSHARED="1"))
    ck("KB_ALLOW_UNSHARED=1 still works under shared", r.get("ok"), True)
else:
    skipped.append("single-machine allow (the configured brain is not on this machine)")

# ── 4. migration of a pre-project brain (scratch schema OLD) ────────────────
def git_show(ref):
    r = subprocess.run(["git", "-C", ROOT, "show", ref + ":.claude/skills/qa-kb/scripts/schema.sql"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None
old_sql = git_show(OLD_SCHEMA_REF) or git_show("HEAD")
ok_("old schema available from git", bool(old_sql))
admin(old_sql, schema=OLD)
ok_("old schema really predates projects", "UNIQUE (kind, area, key)" in old_sql)
admin("""INSERT INTO app_map(kind, area, key, value, source) VALUES ('selector','mig','k1','{"v":1}','kbt-old');
         INSERT INTO expectations(area, statement, source, status) VALUES ('mig','old expectation','kbt-old','canonical');
         INSERT INTO flakiness(spec, area, runs, failures, source) VALUES ('mig.test > a','mig',4,1,'kbt-old');
         INSERT INTO fix_lessons(area, symptom, source) VALUES ('mig','old symptom','kbt-old');
         INSERT INTO metrics(metric, value) VALUES ('kbt_old_metric', 1);
         INSERT INTO kb_writes(verb, store, action, source) VALUES ('write','app_map','insert','kbt-old');""",
      schema=OLD)
r = kb("status", schema=OLD)
ck("status: old brain is present but not current",
   (r.get("schema", {}).get("present"), r.get("schema", {}).get("current")), (True, False))
r = kb("read", "app_map", "--area", "mig", schema=OLD)
ck("old brain still answers reads (unfiltered)", (r.get("count"), r.get("projects", {}).get("filter")),
   (1, "none"))
r = kb("write", "app_map", "--kind", "selector", "--area", "mig", "--key", "k1",
       "--value", '{"v":"alpha"}', "--source", "kbt-new", schema=OLD, project="alpha")
ck("a write to an old brain is QUEUED as schema_outdated",
   (r.get("queued"), str(r.get("error", "")).split(":")[0]), (True, "schema_outdated"))
with open(BASE_ENV["KB_OUTBOX"]) as fh:
    q = json.loads(fh.readline())
ck("the queued argv pins its project", q["argv"][-2:], ["--project", "alpha"])
r = kb("schema", "--apply", schema=OLD)
ck("kb schema --apply migrates in place", r.get("ok"), True)
r = kb("schema", "--apply", schema=OLD)
ck("re-applying is idempotent", r.get("ok"), True)
cnt = admin("""SELECT (SELECT count(*) FROM app_map) a, (SELECT count(*) FROM expectations) e,
                      (SELECT count(*) FROM flakiness) f, (SELECT count(*) FROM fix_lessons) l,
                      (SELECT count(*) FROM metrics) m,
                      (SELECT count(DISTINCT project) FROM app_map) ap,
                      (SELECT min(project) FROM fix_lessons) lp,
                      (SELECT min(project) FROM expectations) ep,
                      (SELECT min(project) FROM flakiness) fp,
                      (SELECT min(project) FROM kb_writes WHERE source='kbt-old') wp""", schema=OLD)[0]
ck("every pre-existing row kept", (cnt["a"], cnt["e"], cnt["f"], cnt["l"], cnt["m"]), (1, 1, 1, 1, 1))
ck("pre-existing rows became project='global'",
   (cnt["lp"], cnt["ep"], cnt["fp"], cnt["wp"]), ("global",) * 4)
cons = sorted(r["conname"] for r in admin(
    """SELECT conname FROM pg_constraint WHERE contype='u'
       AND conrelid IN ('app_map'::regclass, 'flakiness'::regclass)""", schema=OLD))
ck("unique constraints swapped to project-scoped", cons,
   ["app_map_project_kind_area_key_key", "flakiness_project_spec_signature_key"])
r = kb("status", schema=OLD)
ck("status: migrated brain is current", r.get("schema", {}).get("current"), True)
r = kb("outbox", "--replay", schema=OLD)
ck("the queued write drains after the upgrade", (r.get("replayed"), r.get("remaining")), (1, 0))
r = kb("read", "app_map", "--area", "mig", "--key", "k1", schema=OLD, project="alpha")
ck("…stamped with the project it was queued under",
   [(x["project"], x["value"]["v"]) for x in r.get("rows", [])], [("alpha", "alpha"), ("global", 1)])

# ── 5. fresh schema + project isolation (scratch schema NEW) ────────────────
r = kb("schema", "--apply")
ck("fresh schema applies", r.get("ok"), True)
r = kb("status")
ck("status: fresh brain is current, no warning",
   (r.get("schema", {}).get("current"), r.get("warning")), (True, None))
for p, v in (("alpha", "A"), ("beta", "B"), ("global", "G")):
    r = kb("write", "app_map", "--kind", "selector", "--area", "iso", "--key", "same-key",
           "--value", json.dumps({"v": v}), "--source", "kbt", "--project", p)
    ck("app_map same key in project {} INSERTS".format(p), (r.get("action"), r.get("project")),
       ("insert", p))
def vs(r):
    return sorted(x["value"]["v"] for x in r.get("rows", []))
ck("alpha sees alpha + global", vs(kb("read", "app_map", "--area", "iso", "--key", "same-key", project="alpha")), ["A", "G"])
ck("beta sees beta + global", vs(kb("read", "app_map", "--area", "iso", project="beta")), ["B", "G"])
ck("an unrelated project sees only global", vs(kb("read", "app_map", "--area", "iso", project="gamma")), ["G"])
ck("--project all sees every project", vs(kb("read", "app_map", "--area", "iso", "--project", "all")), ["A", "B", "G"])
r = kb("read", "app_map", "--area", "iso", "--key", "same-key", project="alpha")
ck("the current project's row sorts first", r["rows"][0]["project"], "alpha")
r = kb("write", "app_map", "--kind", "selector", "--area", "iso", "--key", "same-key",
       "--value", '{"v":"A2"}', "--source", "kbt", project="alpha")
ck("a re-write updates ONLY its own project's row", (r.get("action"), r["row"]["project"]),
   ("update_refresh", "alpha"))
ck("…beta's row untouched", vs(kb("read", "app_map", "--area", "iso", project="beta")), ["B", "G"])
ck("search app_map is project-scoped",
   sorted(x["project"] for x in kb("search", "app_map", "--query", "same-key", project="beta")["results"]),
   ["beta", "global"])
r = kb("write", "app_map", "--kind", "selector", "--area", "iso", "--key", "x", "--value", "{}",
       "--source", "kbt", "--project", "all")
ok_("--project all refuses a write", r.get("ok") is False and "read-only" in str(r.get("error")))

r = kb("write", "fix_lessons", "--area", "iso", "--symptom", "alpha only symptom", "--source", "kbt",
       "--dup-threshold", "1.5", project="alpha")
ck("fix_lesson stamped alpha", r.get("row", {}).get("project"), "alpha")
kb("write", "fix_lessons", "--area", "iso", "--symptom", "shared global symptom", "--source", "kbt",
   "--dup-threshold", "1.5", "--project", "global")
r = kb("search", "fix_lessons", "--query", "alpha only symptom", "--k", "5", project="beta")
ck("beta's fix_lesson search never returns alpha's row",
   sorted(x["project"] for x in r.get("results", [])), ["global"])
r = kb("search", "fix_lessons", "--query", "alpha only symptom", "--k", "5", project="alpha")
ck("alpha finds its own lesson first", (r["results"][0]["project"], r["results"][0]["similarity"] > 0.99),
   ("alpha", True))
r = kb("write", "fix_lessons", "--area", "iso", "--symptom", "alpha only symptom", "--source", "kbt",
       project="beta")
ck("an identical symptom in another project INSERTS (dedupe is per project)", r.get("action"), "insert")

kb("propose", "expectation", "--area", "iso", "--statement", "alpha expectation", "--source", "kbt",
   project="alpha")
ck("expectations: beta cannot see alpha's",
   kb("read", "expectations", "--area", "iso", project="beta").get("count"), 0)
ck("expectations: alpha can", kb("read", "expectations", "--area", "iso", project="alpha").get("count"), 1)
for p in ("alpha", "beta"):
    kb("write", "flakiness", "--spec", "iso.test > t", "--area", "iso", "--source", "kbt", project=p)
r = kb("read", "flakiness", "--spec", "iso.test > t", project="alpha")
ck("flakiness: same test id is a separate counter per project",
   [(x["project"], x["runs"]) for x in r.get("rows", [])], [("alpha", 1)])
r = kb("write", "app_map", "--kind", "route", "--area", "iso", "--key", "tkt", "--value", "{}",
       "--source", "kbt", "--ticket", "KBT-1", project="alpha")
r = kb("receipt", "--ticket", "KBT-1")
ck("provenance records the project",
   [x.get("project") for x in r.get("writes_by_store", {}).get("app_map", [])], ["alpha"])
r = kb("forget", "app_map", "--kind", "selector", "--area", "iso", "--key", "same-key", project="beta")
ck("forget by key removes only the current project's row", len(r.get("deleted", [])), 1)
ck("…alpha and global survive", vs(kb("read", "app_map", "--area", "iso", "--key", "same-key", "--project", "all")),
   ["A2", "G"])

# ── 6. lessons ──────────────────────────────────────────────────────────────
T1 = "Port 8000 is taken on this machine; run the API on 8010."
r = kb("write", "lesson", "--text", T1, "--kind", "Tooling", "--source", "kbt", project="alpha")
L1 = r.get("row", {}).get("id")
ck("write lesson inserts as proposed",
   (r.get("action"), r["row"]["status"], r["row"]["kind"], r["row"]["project"]),
   ("insert", "proposed", "tooling", "alpha"))
ck("a proposed lesson is not searchable",
   kb("search", "lessons", T1, project="alpha").get("count"), 0)
r = kb("write", "lesson", "--text", "port 8000 is TAKEN on this machine, run the API on 8010", "--source", "kbt",
       project="alpha")
ck("an exact (normalized) repeat bumps by hash",
   (r.get("action"), r.get("dedup_by"), r["row"]["id"], r["row"]["occurrences"]), ("bump", "hash", L1, 2))
admin("UPDATE lessons SET text_hash = NULL WHERE id = %s", (L1,), schema=NEW)
r = kb("write", "lesson", "--text", T1, "--source", "kbt", project="alpha")
ck("a near-duplicate bumps by vector (cosine >= 0.92)",
   (r.get("action"), r.get("dedup_by"), r["row"]["id"], r["row"]["occurrences"]), ("bump", "vector", L1, 3))
ok_("…and the bump moved last_seen_at", r["row"]["last_seen_at"] > r["row"]["created_at"])
r = kb("write", "lesson", "--text", T1, "--source", "kbt", project="beta")
ck("the same lesson in another project is a separate row", (r.get("action"), r["row"]["project"]),
   ("insert", "beta"))
r = kb("bless", "lesson", "--id", L1, "--by", "kbt-human", project="alpha")
ck("bless: proposed -> active", (r.get("action"), r["row"]["status"], r["row"]["blessed_by"]),
   ("bless", "active", "kbt-human"))
ck("bless twice fails cleanly", kb("bless", "lesson", "--id", L1).get("error"), "bless_failed")
r = kb("search", "lessons", T1, "--k", "3", project="alpha")
ck("search returns the active lesson with a score",
   (r.get("count"), r["results"][0]["id"] if r.get("results") else None,
    round(r["results"][0]["score"], 3) if r.get("results") else None), (1, L1, 1.0))
ck("--min-score filters", kb("search", "lessons", T1, "--min-score", "1.01", project="alpha").get("count"), 0)
ck("beta never sees alpha's active lesson", kb("search", "lessons", T1, project="beta").get("count"), 0)
G = kb("write", "lesson", "--text", "The user prefers multiple-choice questions.", "--kind", "preference",
       "--status", "active", "--source", "kbt", "--project", "global")
ck("write --status active", G.get("row", {}).get("status"), "active")
for p in ("alpha", "beta", "anything"):
    r = kb("search", "lessons", "--query", "The user prefers multiple-choice questions.", project=p)
    ck("a global active lesson reaches {}".format(p),
       [(x["id"], x["project"]) for x in r.get("results", [])][:1], [(G["row"]["id"], "global")])
R = kb("write", "lesson", "--text", "A lesson that turns out to be wrong.", "--source", "kbt", project="alpha")
r = kb("reject", "lesson", "--id", R["row"]["id"], "--reason", "wrong", project="alpha")
ck("reject: proposed -> rejected", r.get("row", {}).get("status"), "rejected")
ck("read lessons --status rejected", [x["id"] for x in kb("read", "lessons", "--status", "rejected",
                                                          project="alpha")["rows"]], [R["row"]["id"]])
ck("read lessons --status proposed,active (alpha + global)",
   sorted(x["status"] for x in kb("read", "lessons", "--status", "proposed,active", project="alpha")["rows"]),
   ["active", "active"])
ck("a rejected lesson never comes back from search",
   R["row"]["id"] in [x["id"] for x in kb("search", "lessons", "A lesson that turns out to be wrong.",
                                           "--k", "50", project="alpha").get("results", [])], False)
ck("bad --status is refused", kb("read", "lessons", "--status", "bogus").get("ok"), False)
ck("write --status rejected is refused at the CLI", kb("write", "lesson", "--text", "x", "--source", "kbt",
                                                       "--status", "rejected").get("ok"), None)
# undo + forget: same provenance semantics as every other store
r = kb("write", "lesson", "--text", "kbt undo probe lesson", "--source", "kbt", "--run-id", "kbt-run-1",
       project="alpha")
U = r["row"]["id"]
kb("write", "lesson", "--text", "kbt undo probe lesson", "--source", "kbt", "--run-id", "kbt-run-2",
   project="alpha")
r = kb("undo", "--run-id", "kbt-run-2", "--yes")
ck("undo reverses a lesson bump by decrementing",
   (r.get("undone"), r["results"][0]["mode"] if r.get("results") else None), (1, "decrement"))
r = kb("undo", "--run-id", "kbt-run-1", "--yes")
ck("undo removes a lesson the run created", r["results"][0]["mode"] if r.get("results") else None, "delete")
ck("…it is gone", kb("read", "lessons", "--id", U).get("count"), 0)
r = kb("forget", "lessons", "--id", L1)
ck("forget lessons --id deletes with a receipt", (r.get("deleted"), bool(r.get("receipt"))), ([L1], True))
# embedder down: the lesson still lands, pending; reembed heals it
down = {kbmod.EMB_KEY_ENV: ""}
r = kb("write", "lesson", "--text", "kbt pending lesson", "--source", "kbt", project="alpha",
       stub=False, env=down)
ck("embedder down: the lesson lands pending", (r.get("ok"), r.get("embed_status")), (True, "pending"))
r = kb("reembed", "--store", "lessons")
ck("reembed heals a pending lesson", (r.get("embedded"), r.get("failed")), (1, 0))

print("  kb_projects: {} passed, {} failed{}".format(
    passed[0], failed[0], "" if not skipped else " ({} skipped: {})".format(len(skipped), "; ".join(skipped))))
sys.exit(0 if failed[0] == 0 else 1)
PY
