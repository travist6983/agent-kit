#!/usr/bin/env python3
"""kb — the only read/write interface to the loop knowledge brain.

Storage-agnostic CLI over four knowledge stores (app_map, expectations, flakiness, fix_lessons)
plus three ops stores (kb_control = pause/posture flags, metrics = loop health,
kb_state = durable key/value) and the kb_writes ledger (automatic write receipts).

Every command prints one JSON object to stdout: {"ok": true, ...} on success, or
{"ok": false, "error": "..."} + non-zero exit on failure. Other skills shell out to
this and parse the JSON — keep the contract stable. New keys are ADDITIVE ONLY.
(The single documented exception is `kb schema --raw`, which prints SQL for piping.)

Durability guarantees (a lost write is a lost lesson):
  * Embedder down  -> the row still lands, with embedding NULL + embed_status='pending'
                      and a warning; `kb reembed` heals it later. Exit 0.
  * DB unreachable -> a MUTATING verb's full argv is appended to <BRAIN_DIR>/outbox.jsonl
                      and the response carries queued:true; `kb outbox --replay` drains it.
                      Semantic failures (version_conflict, bad args) NEVER queue.
                      Replay RETARGETS: the queued `--db` is the address that was dead, so it
                      is stripped and the replay uses this process's DSN (or the replay
                      command's own --db), reporting `retargeted_from`. Without that, an entry
                      queued with an explicit --db could never succeed and would be discarded
                      by the first `--clear`.
  * Every successful mutating verb writes one kb_writes ledger row IN THE SAME
    TRANSACTION, and returns it as "receipt" — so `kb receipt --ticket K` is a
    falsifiable record of what a run wrote.

There is no local default. The target is the DSN named in the config (`database_url`) or in
KB_DATABASE_URL (or `--db`); a missing DSN is an error, never a cue to guess. A loopback /
unix-socket host is REFUSED (see _assert_shared_brain) unless the config DECLARES
`"scope": "single-machine"` (one person, one machine, a brain on purpose) or
KB_ALLOW_UNSHARED=1 says offline work is intended — rows written that way reach no one else.

Backend: Postgres + pgvector, on any host. Database auth is one of the modes in
         kb.config.json `auth.mode`: password | url_env | token_command.
         Embeddings: per kb.config.json `embedding` (provider / model / dim / api_key_env).
Config:  the first of  $KB_CONFIG (explicit path)
                       → $AGENT_KIT_OVERLAY/projects/<project>/kb.config.json (if it exists)
                       → $AGENT_KIT_OVERLAY/kb.config.json (if it exists)
                       → <repo>/.claude/loops/kb.config.json (the kit's tracked example)
         Holds no secret. `scope` there is `shared` (default) or `single-machine`.
Secret:  `secret_env_file` (default kb.secret.env) resolved NEXT TO the chosen config — the
         embedding key named by `embedding.api_key_env`, plus a DB password for
         `auth.mode: password` — gitignored.
Project: every knowledge row carries `project` (see resolve_project): $AGENT_KIT_PROJECT, else
         the basename of the cwd's git toplevel, else 'global' (and 'global' inside the kit
         itself). Reads see <project> + 'global'; `--project all` sees everything.
Schema:  `kb schema --dim N` renders scripts/schema.sql.in for the configured embedding
         dimension (scripts/schema.sql is the rendered default); `kb schema --apply` applies
         it through the configured auth mode.
"""
import argparse
import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

import psycopg2
from psycopg2.extras import RealDictCursor, Json
from pgvector.psycopg2 import register_vector

# ── config / secrets ─────────────────────────────────────────────────────────

def find_brain_dir():
    env = os.environ.get("KB_HOME")
    if env:
        return env
    d = os.path.dirname(os.path.realpath(__file__))
    while d != "/":
        cand = os.path.join(d, ".claude", "loops")
        if os.path.isdir(cand):
            return cand
        d = os.path.dirname(d)
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", "loops")

BRAIN_DIR = find_brain_dir()

def load_secret_env(path):
    """Robust .env loader: handles `export`, surrounding quotes, whitespace."""
    if not path or not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k, v)

# ── project identity ─────────────────────────────────────────────────────────
# One person can point many of their own projects at ONE brain; every knowledge row carries the
# project it was learned in, and reads see <project> + 'global'. The project is resolved here,
# before the config, because an overlay may hold a per-project kb.config.json.

GLOBAL_PROJECT = "global"
ALL_PROJECTS = "all"           # read-side only: `--project all` lifts the project filter
_KIT_ROOT_CACHE = []


def _git_toplevel(cwd):
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                             capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return os.path.realpath(out.stdout.strip())


def kit_root():
    """The kit's own checkout: the git toplevel that holds this script (else four levels up)."""
    if not _KIT_ROOT_CACHE:
        here = os.path.dirname(os.path.realpath(__file__))
        _KIT_ROOT_CACHE.append(_git_toplevel(here) or os.path.realpath(
            os.path.join(here, "..", "..", "..", "..")))
    return _KIT_ROOT_CACHE[0]


def norm_project(name):
    """Projects are compared normalized ('My App' -> 'my-app'), like areas. '' -> 'global'."""
    s = (name or "").strip().lower()
    s = re.sub(r"[\s_/\\]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or GLOBAL_PROJECT


def resolve_project(override=None, cwd=None):
    """(project, how, raw). `--project` > $AGENT_KIT_PROJECT > basename of `git rev-parse
    --show-toplevel` in the cwd > 'global'. The kit's own checkout is 'global': knowledge learned
    while working ON the kit is not about any one project."""
    if override:
        return norm_project(override), "flag", override.strip()
    env = (os.environ.get("AGENT_KIT_PROJECT") or "").strip()
    if env:
        return norm_project(env), "env", env
    top = _git_toplevel(cwd or os.getcwd())
    if not top:
        return GLOBAL_PROJECT, "default", None
    if top == kit_root():
        return GLOBAL_PROJECT, "kit", None
    return norm_project(os.path.basename(top)), "git", os.path.basename(top)


# ── config lookup (kit default or an external overlay) ───────────────────────

KIT_CONFIG_PATH = os.path.join(BRAIN_DIR, "kb.config.json")


def resolve_config_path(project, raw_project=None):
    """(path, how). $KB_CONFIG > $AGENT_KIT_OVERLAY/projects/<project>/kb.config.json >
    $AGENT_KIT_OVERLAY/kb.config.json > the kit's .claude/loops/kb.config.json. Overlay files
    count only when they exist; an explicit $KB_CONFIG must exist (checked at use time)."""
    explicit = (os.environ.get("KB_CONFIG") or "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit)), "KB_CONFIG"
    overlay = (os.environ.get("AGENT_KIT_OVERLAY") or "").strip()
    if overlay:
        overlay = os.path.expanduser(overlay)
        names = [project]
        if raw_project and raw_project not in names:
            names.append(raw_project)          # an overlay folder spelled like the repo dir
        for name in names:
            cand = os.path.join(overlay, "projects", name, "kb.config.json")
            if os.path.isfile(cand):
                return cand, "overlay-project"
        cand = os.path.join(overlay, "kb.config.json")
        if os.path.isfile(cand):
            return cand, "overlay"
    return KIT_CONFIG_PATH, "kit"

# Defaults for the two sections a config may leave out. `database_url` has NO default on
# purpose (see NO_TARGET_MSG). The embedding defaults match kb.config.json as shipped; an
# adopter who changes `embedding.dim` re-renders the schema with `kb schema --dim N`.
DEFAULT_EMBEDDING = {"provider": "openai", "model": "text-embedding-3-small", "dim": 1536,
                     "api_key_env": "OPENAI_API_KEY"}
DEFAULT_AUTH = {"mode": "password"}
AUTH_MODES = ("password", "url_env", "token_command")
# `scope` declares who the brain is for. shared (default): a loopback / socket host is refused.
# single-machine: one person's brain on this machine — a local host is a declared choice.
SCOPES = ("shared", "single-machine")
DEFAULT_SCOPE = "shared"

NO_TARGET_MSG = (
    "no brain target configured — refusing to guess. Expected a non-empty `database_url` in "
    "{path}, or KB_DATABASE_URL in the environment (or --db). That file holds no credentials "
    "(those come from `auth.mode` and the gitignored secret env file), so an empty target means "
    "this checkout has not been pointed at a brain yet: set `database_url` once (in the kit's "
    "tracked config, or in an overlay config named by AGENT_KIT_OVERLAY / KB_CONFIG). Setup: "
    "docs/brain-setup.md. There is deliberately NO local default — a silent fallback to a "
    "host-less DSN builds a private brain on one machine, with writes returning ok:true while "
    "the lesson reaches no one.")


class BrainConfigError(RuntimeError):
    """No usable shared-brain target / auth configuration. Raised at USE time, never at import."""


class _Config(dict):
    """A dict whose only special case is `database_url`: looking it up when it is absent
    raises an actionable BrainConfigError instead of silently yielding a local default.

    Raising on LOOKUP rather than in load_config() is deliberate — importing kb.py must keep
    working without a config so `kb outbox` (the verb you reach for when the DB is
    unreachable, which never opens a connection) and `kb schema` still run, and so the
    module-scope EMB_MODEL/EMB_DIM reads below cannot be taken down by a config problem they
    don't share. It also means every other DSN consumer that reads CONFIG["database_url"]
    directly (harness.py, migrate_normalize.py, the test suites) inherits the same clear error."""

    def __missing__(self, key):
        if key == "database_url":
            raise BrainConfigError(NO_TARGET_MSG.format(path=CONFIG_PATH))
        raise KeyError(key)


def _strip_comments(obj):
    """Drop `_comment*` keys so the tracked config can document itself and stay valid JSON."""
    if isinstance(obj, dict):
        return dict((k, _strip_comments(v)) for k, v in obj.items()
                    if not str(k).startswith("_comment"))
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def load_config(path=None):
    # NOTE: no "database_url" default. The brain named in kb.config.json (or KB_DATABASE_URL)
    # is the only target; if the config supplies no usable target we fail closed.
    path = path or CONFIG_PATH
    cfg = _Config({
        "auth": dict(DEFAULT_AUTH),
        "embedding": dict(DEFAULT_EMBEDDING),
        "secret_env_file": "kb.secret.env",
        "scope": DEFAULT_SCOPE,
    })
    if os.path.exists(path):
        with open(path) as fh:
            raw = _strip_comments(json.load(fh))   # deliberately unwrapped: a malformed config must raise
        emb = dict(DEFAULT_EMBEDDING)
        emb.update(raw.pop("embedding", None) or {})
        auth = dict(DEFAULT_AUTH)
        auth.update(raw.pop("auth", None) or {})
        cfg.update(raw)
        cfg["embedding"], cfg["auth"] = emb, auth
    # A present-but-empty/non-string target is the same failure as no target at all — drop it
    # so the lookup above raises, instead of handing psycopg2 "" and getting a local socket.
    if not isinstance(cfg.get("database_url"), str) or not cfg["database_url"].strip():
        cfg.pop("database_url", None)
    return cfg


def init_config(project, raw_project=None):
    """Choose the config file for `project`, load it and the secret env file beside it, and
    (re)set the module globals every verb reads. Called at import and again by main() when
    `--project` names a different project (an overlay may hold a per-project config)."""
    global CONFIG_PATH, CONFIG_SOURCE, CONFIG, EMB_MODEL, EMB_DIM, EMB_KEY_ENV, PROJECT
    CONFIG_PATH, CONFIG_SOURCE = resolve_config_path(project, raw_project)
    CONFIG = load_config(CONFIG_PATH)
    # The secret env file lives NEXT TO whichever config was chosen (an overlay keeps its own).
    load_secret_env(os.path.join(os.path.dirname(CONFIG_PATH),
                                 CONFIG.get("secret_env_file", "kb.secret.env")))
    EMB_MODEL = CONFIG["embedding"]["model"]
    EMB_DIM = int(CONFIG["embedding"]["dim"])
    EMB_KEY_ENV = CONFIG["embedding"].get("api_key_env") or DEFAULT_EMBEDDING["api_key_env"]
    PROJECT = project


CONFIG_PATH = KIT_CONFIG_PATH
PROJECT, PROJECT_HOW, _PROJECT_RAW = resolve_project()
_IMPORT_PROJECT = (PROJECT, PROJECT_HOW, _PROJECT_RAW)
init_config(PROJECT, _PROJECT_RAW)


def brain_scope():
    return CONFIG.get("scope") or DEFAULT_SCOPE


def resolve_dsn(override=None):
    """THE one DSN resolver, shared by main(), harness.py and migrate_normalize.py:
    `--db` > KB_DATABASE_URL > (auth.mode url_env: the variable `auth.env` names) > config.
    Raises BrainConfigError when nothing names a target — never returns a guess."""
    if CONFIG_SOURCE == "KB_CONFIG" and not os.path.isfile(CONFIG_PATH):
        raise BrainConfigError("KB_CONFIG names {} but no such file exists".format(CONFIG_PATH))
    if override:
        return override
    env = os.environ.get("KB_DATABASE_URL")
    if env:
        return env
    auth = CONFIG["auth"]
    if auth.get("mode") == "url_env":
        name = auth.get("env") or "KB_DATABASE_URL"
        val = os.environ.get(name)
        if val:
            return val
        if not CONFIG.get("database_url"):
            raise BrainConfigError(
                "auth.mode is url_env but {} is not set and `database_url` is empty — export "
                "the full DSN (with its credential) in {}".format(name, name))
    return CONFIG["database_url"]


def validate_auth_config():
    """A config error, not a transport error: never queued, never guessed."""
    if CONFIG_SOURCE == "KB_CONFIG" and not os.path.isfile(CONFIG_PATH):
        raise BrainConfigError("KB_CONFIG names {} but no such file exists".format(CONFIG_PATH))
    scope = CONFIG.get("scope") or DEFAULT_SCOPE
    if scope not in SCOPES:
        raise BrainConfigError("unknown scope {!r} in {} — one of: {}".format(
            scope, CONFIG_PATH, ", ".join(SCOPES)))
    mode = CONFIG["auth"].get("mode") or "password"
    if mode not in AUTH_MODES:
        raise BrainConfigError("unknown auth.mode {!r} in {} — one of: {}".format(
            mode, CONFIG_PATH, ", ".join(AUTH_MODES)))
    if mode == "token_command" and not CONFIG["auth"].get("command"):
        raise BrainConfigError("auth.mode token_command needs `auth.command` (an argv list or a "
                               "shell string that prints a short-lived DB token)")
    return mode

# ── normalization ────────────────────────────────────────────────────────────
# `area` and `kind` are normalized on BOTH write and read, so a slice written as
# "Design System" is found by a caller asking for "design-system". Rows written before
# the normalization landed are still mixed-case until scripts/migrate_normalize.py runs,
# so every read (and the app_map write's existence lookup) ALSO tries the raw value and
# reports which variant matched. That fallback is the bridge, not the destination.

_NORM_SEP_RE = re.compile(r"[\s_/\\]+")
_NORM_DASH_RE = re.compile(r"-{2,}")

def _norm(s):
    """strip → lowercase → any run of whitespace/underscore/slash becomes one '-' →
    squeeze repeated '-' → trim leading/trailing '-'."""
    s = (s or "").strip().lower()
    s = _NORM_SEP_RE.sub("-", s)
    s = _NORM_DASH_RE.sub("-", s)
    return s.strip("-")

def norm_area(s):
    """'Design System' -> 'design-system'; 'Settings/People' -> 'settings-people'."""
    return _norm(s)

def norm_kind(s):
    """Same normalization as norm_area ('area_tag' -> 'area-tag')."""
    return _norm(s)

# Documented allowlist. An unknown kind still WRITES (never a hard error — that would
# break existing callers); the response simply carries warning:"unknown_kind".
KNOWN_KINDS_RAW = [
    "selector", "route", "area_tag", "env", "lesson", "followup", "replay_spec", "api",
    "component", "fact", "note", "nav", "action", "service", "endpoint", "config-gate",
    "access", "routing", "reference", "app-singleton", "data_location",
]
KNOWN_KINDS = set(norm_kind(k) for k in KNOWN_KINDS_RAW)

def variants(raw, normfn):
    """[normalized] or [normalized, raw] — the values a read/lookup should accept."""
    n = normfn(raw)
    out = [n]
    r = raw or ""
    if r != n:
        out.append(r)
    return out

def area_variants(raw):
    return variants(raw, norm_area)

def kind_variants(raw):
    return variants(raw, norm_kind)

def norm_sql(col):
    """The SQL twin of _norm(), so a read can match rows stored in ANY casing/spacing
    WITHOUT waiting for scripts/migrate_normalize.py. Kept character-for-character in step
    with _norm(): btrim → lower → [\\s_/\\]+ → '-' → squeeze '-' → btrim '-'.
    Un-indexed by design; these tables are hundreds of rows, and correctness beats a scan."""
    return ("btrim(regexp_replace(regexp_replace(lower(btrim(" + col + ")), "
            r"'[\s_/\\]+', '-', 'g'), '-{2,}', '-', 'g'), '-')")

def norm_pred(col):
    """WHERE fragment matching the exact variants OR anything that normalizes to the same
    value. Bind two params: (variants_list, normalized_value)."""
    return "({} = ANY(%s) OR {} = %s)".format(col, norm_sql(col))

def match_report(vs, rows, field):
    """How the rows were found: normalized | raw | both | normalized_scan | none."""
    got = set(r.get(field) for r in rows)
    return _match_verdict(vs, got, rows)

def match_info(vs, rows, field):
    """match_report + the spellings actually found, so a caller can SEE that its slice is
    still split across e.g. 'Invoices' and 'invoices' and needs migrate_normalize.py."""
    got = sorted(x for x in set(r.get(field) for r in rows) if x is not None)
    return {"tried": vs, "matched": _match_verdict(vs, set(got), rows), "stored": got}

def _match_verdict(vs, got, rows):
    hit_n = vs[0] in got
    hit_r = any(v in got for v in vs[1:])
    if hit_n and hit_r:
        return "both"
    if hit_n:
        return "normalized"
    if hit_r:
        return "raw"
    if rows:
        return "normalized_scan"   # matched only via norm_sql() — migrate_normalize.py pending
    return "none"

def fetch_by_area(cur, table, area, order_sql, a=None):
    """`--area` read shared by every store: exact variants OR a normalized match, within the
    invocation's project scope (current + 'global', or every project for `--project all`).
    Returns (rows, normalization_report)."""
    av = area_variants(area)
    pw, pp, po, pop = "TRUE", [], "", []
    if a is not None:
        pw, pp = proj_where(cur, a, table)
        po, pop = proj_order(cur, a, table)
    cur.execute("SELECT * FROM {} WHERE {} AND {} ORDER BY {}(area=%s) DESC, {}".format(
                    table, norm_pred("area"), pw, po, order_sql),
                tuple([av, av[0]] + pp + pop + [av[0]]))
    rows = cur.fetchall()
    return rows, {"area": match_info(av, rows, "area")}

# Language-agnostic test-file conventions: `<name>.spec.ts`, `<name>_test.py`, `<name>Tests.cs`,
# `<name>_spec.rb`, `<name>_test.go`, `<name>Test.java`, `test_<name>.py` … The test id itself
# ('<test file> > <test name>') is a convention the target repo supplies; this only derives a
# default area when the writer passed none.
_SPEC_EXT = r"\.(?:[cm]?[jt]sx?|py|rb|go|cs|java|kt|kts|php|rs|exs?|swift|scala|dart)"
_SPEC_SUFFIX_RE = re.compile(
    r"(?:[._-](?:spec|test|tests)|Tests?|Specs?)?" + _SPEC_EXT + r"$"
    r"|[._-](?:spec|test|tests)$|(?:Tests?|Specs?)$")
_SPEC_PREFIX_RE = re.compile(r"^(?:test|spec)[_-]")

def derive_area_from_spec(spec):
    """Flakiness --area is effectively required. When empty, derive it from the test id's
    (the --spec value's) leading segment before ':' or '>' (basename, test-suffix stripped), then normalize.
      'invoices.test.js > submits'                      -> 'invoices'
      'src/areas/tasks/__tests__/tasks.test.js:12'      -> 'tasks'
      'tests/test_members.py::test_invite'              -> 'members'
      'InvoiceTests.cs > Creates_draft'                 -> 'invoice'
    Returns '' when nothing can be derived (caller warns)."""
    if not spec:
        return ""
    seg = re.split(r"[:>]", spec, 1)[0].strip()
    seg = seg.replace("\\", "/").rsplit("/", 1)[-1]
    prev = None
    while prev != seg:                       # strip '.test.js' then a bare '.js', etc.
        prev = seg
        seg = _SPEC_SUFFIX_RE.sub("", seg)
    seg = _SPEC_PREFIX_RE.sub("", seg)
    return norm_area(seg)

# ── exact-duplicate hashing (the dedup guard that needs NO embedder) ─────────
# During an embedder outage (a rate-limit 429, a missing key) the pgvector neighbour query cannot
# run at all (it needs a vector for the incoming text), so without this guard `write fix_lessons`
# / `propose expectation` would INSERT unconditionally — and retry is this design's own recovery
# path, so every retry would clone the row. The hash guard runs BEFORE any embedding attempt,
# which means it: (a) makes retry safe with the embedder DOWN, and (b) short-circuits an exact
# duplicate when the embedder is UP, saving the API call.

_HASH_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_HASH_WS_RE = re.compile(r"\s+", re.UNICODE)

def normalize_text_for_hash(s):
    """The EXACT rule behind text_hash(), in this order:
         1. `casefold()` — Unicode-aware lowercasing.
         2. Drop every character that is neither a word character nor whitespace (i.e. all
            punctuation and symbols). Letters, digits, `_` and accents survive untouched —
            there is NO accent folding, so 'rôle' and 'role' stay different texts.
         3. Collapse every run of whitespace to a single space.
         4. Strip leading/trailing whitespace.
    Deterministic and stable: same text → same digest, on any machine, with no model, no
    network and no locale dependence. Changing this rule re-buckets the whole store, so it is
    versioned by being written down here — treat it as part of the schema."""
    s = _HASH_PUNCT_RE.sub("", (s or "").casefold())
    return _HASH_WS_RE.sub(" ", s).strip()

def text_hash(s):
    """sha256 hex digest of normalize_text_for_hash(s) — the exact-duplicate key."""
    return hashlib.sha256(normalize_text_for_hash(s).encode("utf-8")).hexdigest()

def source_variants(raw):
    """['qa-dream'] or ['qa-dream', 'QA Dream'] — the source spellings a DESTRUCTIVE selector
    (`forget --source`, `undo --source`) accepts. Deliberately exact-plus-normalized rather
    than the normalizing scan reads use: a bulk delete should match what you typed."""
    return variants(raw, _norm)

# The hash columns are added by schema.sql. A brain that has not had it re-applied must still
# TAKE WRITES, so column presence is probed once per process and the guard degrades to the
# vector path (with a warning) instead of failing the write.
_HAS_COL_CACHE = {}

def has_col(cur, table, col):
    key = (table, col)
    if key not in _HAS_COL_CACHE:
        cur.execute("""SELECT 1 FROM information_schema.columns
                       WHERE table_schema = current_schema()
                         AND table_name = %s AND column_name = %s""", (table, col))
        _HAS_COL_CACHE[key] = cur.fetchone() is not None
    return _HAS_COL_CACHE[key]

HASH_UNAVAILABLE = ("hash_dedupe_unavailable: {}.{} is missing — re-apply "
                    "scripts/schema.sql so retry dedupe works without an embedder")

def hash_dup_lookup(cur, table, hash_col, digest, area_raw, extra_sql="", extra_params=()):
    """The existing row with the SAME normalized text in the SAME area bucket, or None.
    Area is matched through norm_pred(), so 'Invoices' and 'invoices' are one bucket (that is
    the whole point of normalizing areas). Oldest row wins — it is the original that the later
    writes are duplicates OF."""
    av = area_variants(area_raw)
    cur.execute("SELECT * FROM {} WHERE {} = %s AND {} {} ORDER BY id LIMIT 1".format(
                    table, hash_col, norm_pred("area"), extra_sql),
                tuple([digest, av, av[0]] + list(extra_params)))
    return cur.fetchone()

def heal_hash(cur, table, hash_col, row, text):
    """Backfill a NULL hash on a row we just touched, so the guard progressively covers rows
    written before it existed even if migrate_normalize.py never runs. It is a DERIVED column:
    no version bump, no updated_at change — this is not an edit."""
    if row is None or row.get(hash_col) is not None:
        return row
    cur.execute("UPDATE {} SET {} = %s WHERE id = %s RETURNING *".format(table, hash_col),
                (text_hash(text), row["id"]))
    return cur.fetchone() or row

# The human-readable column each store is identified BY, for plans/receipts/undo summaries.
STORE_LABEL = {"app_map": "key", "expectations": "statement", "flakiness": "spec",
               "fix_lessons": "symptom", "metrics": "metric", "lessons": "text"}
# Stores whose rows have no 'forgotten' status — forget is always a hard delete there.
NO_SOFT_FORGET = ("flakiness", "fix_lessons", "lessons")

# ── embeddings ───────────────────────────────────────────────────────────────

def _openai_embed(texts):
    """An OpenAI-compatible embeddings endpoint. The key comes from the env var NAMED by
    `embedding.api_key_env` (never from the tracked config); `embedding.base_url` points the
    same client at any compatible server (a self-hosted model, a gateway)."""
    key = os.environ.get(EMB_KEY_ENV)
    if not key:
        raise RuntimeError("{} not set (needed for the {} embedder)".format(
            EMB_KEY_ENV, CONFIG["embedding"].get("provider")))
    base_url = CONFIG["embedding"].get("base_url") or None
    try:  # openai>=1.0
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url=base_url)
        resp = client.embeddings.create(model=EMB_MODEL, input=texts)
        return [d.embedding for d in resp.data]
    except ImportError:  # openai<1.0
        import openai
        openai.api_key = key
        if base_url:
            openai.api_base = base_url
        resp = openai.Embedding.create(model=EMB_MODEL, input=texts)
        return [d["embedding"] for d in resp["data"]]

def _stub_embed(texts):
    """TEST-ONLY. A deterministic NON-semantic vector (sha256 bytes, unit-normalized) — used
    only when explicitly requested via --embedder stub, for schema/plumbing tests. Never
    auto-selected and never a fallback: garbage vectors in the semantic stores make the
    evaluator optimize toward a lie. Rows it embeds carry `stub-sha256-<dim>` provenance so
    they can never be mistaken for real embeddings."""
    out = []
    for t in texts:
        vec = []
        i = 0
        while len(vec) < EMB_DIM:
            h = hashlib.sha256("{}|{}".format(t, i).encode()).digest()
            for b in h:
                if len(vec) >= EMB_DIM:
                    break
                vec.append((b / 255.0) * 2 - 1)
            i += 1
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        out.append([x / norm for x in vec])
    return out

# The embedder registry. A provider is a function texts -> vectors; `openai` also serves any
# OpenAI-compatible endpoint via `embedding.base_url`. `stub` is TEST-ONLY (see _stub_embed).
EMBEDDERS = {"openai": _openai_embed, "openai-compatible": _openai_embed, "stub": _stub_embed}

def get_embedder(choice):
    """FAIL CLOSED: the stub is used only on explicit request (--embedder stub or config
    provider 'stub'). With no provider key the real embedder raises a clear error instead of
    silently writing sha256 garbage. embed.model records what ACTUALLY embedded, so stub rows
    can never carry real-model provenance. An unknown provider is an embedder that is always
    "down": writes still land as pending (durability), and the warning names the problem."""
    provider = choice or CONFIG["embedding"].get("provider") or DEFAULT_EMBEDDING["provider"]
    if provider == "stub":
        fn, model = _stub_embed, "stub-sha256-{}".format(EMB_DIM)
    elif provider in EMBEDDERS:
        fn, model = EMBEDDERS[provider], EMB_MODEL
    else:
        def fn(texts, _p=provider):
            raise RuntimeError("unknown embedding.provider {!r} — one of: {}".format(
                _p, ", ".join(sorted(EMBEDDERS))))
        model = EMB_MODEL

    def embed(texts):
        return fn(texts)

    embed.provider = provider
    embed.model = model
    return embed, provider

def try_embed(embed, text):
    """(vector, error_string). NEVER raises — an embedder outage must not cost us the row."""
    try:
        return embed([text])[0], None
    except Exception as e:                                    # noqa: BLE001 — any embedder fault
        return None, "{}: {}".format(type(e).__name__, e)

# ── db ───────────────────────────────────────────────────────────────────────

def vlit(vec):
    """Render an embedding as a pgvector text literal for `%s::vector` binding."""
    return "[" + ",".join(str(float(x)) for x in vec) + "]"

# ── database auth modes (kb.config.json `auth.mode`) ───────────────────────────
# The tracked config never holds a credential. Each mode says where the credential comes
# from at connect time:
#   password       the DSN / PGPASSWORD / ~/.pgpass, or the env var `auth.password_env`
#                  names (default KB_DB_PASSWORD, typically set in the secret env file)
#   url_env        the full DSN, credential included, lives in the env var `auth.env` names
#                  (default KB_DATABASE_URL) — nothing secret in the tracked file
#   token_command  `auth.command` (argv list or shell string) prints a short-lived token that
#                  is used as the password — e.g. a cloud CLI's generate-db-auth-token
_token_cache = {}


def _token_command_token(auth):
    cmd = auth.get("command")
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    key = tuple(cmd)
    if key in _token_cache:
        return _token_cache[key]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         timeout=int(auth.get("timeout_seconds") or 60))
    if out.returncode != 0 or not out.stdout.strip():
        # The token itself is never echoed; stderr may carry the CLI's own guidance.
        raise RuntimeError("auth.command failed (rc {}): {}".format(
            out.returncode, (out.stderr or "").strip()[:300] or "no token on stdout"))
    _token_cache[key] = out.stdout.strip().splitlines()[-1].strip()
    return _token_cache[key]


def _dsn_params(dsn):
    try:
        from psycopg2.extensions import parse_dsn
        return parse_dsn(dsn)
    except Exception:
        return {}

def _dsn_host(dsn):
    return _dsn_params(dsn).get("host") or ""


def _auth_kwargs(dsn):
    """psycopg2.connect kwargs for the configured auth mode (kwargs override the DSN)."""
    auth = CONFIG["auth"]
    mode = auth.get("mode") or "password"
    kw = {}
    if mode == "password":
        pw = os.environ.get(auth.get("password_env") or "KB_DB_PASSWORD")
        if pw:
            kw["password"] = pw          # else libpq: DSN password / PGPASSWORD / ~/.pgpass
    elif mode == "url_env":
        pass                             # the DSN itself carries the credential
    elif mode == "token_command":
        kw["password"] = _token_command_token(auth)
    else:
        raise BrainConfigError("unknown auth.mode {!r} — one of: {}".format(mode, ", ".join(AUTH_MODES)))
    # A token over the wire must be encrypted; a DSN that names its own sslmode wins.
    if mode == "token_command" and "sslmode" not in _dsn_params(dsn):
        kw["sslmode"] = "require"
    return kw


LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "ip6-localhost", "ip6-loopback")


def _is_unshared_host(host):
    """True for a host only THIS machine can reach: none (a unix socket), a socket directory,
    or a loopback name/address. Any other host is, by definition, one others can share."""
    h = (host or "").strip().lower().strip("[]")
    return (h == "" or h.startswith("/") or h in LOOPBACK_HOSTS or h.startswith("127."))


class UnsharedBrainRefused(psycopg2.OperationalError):
    """A resolved DSN pointed at a brain only this machine can reach.

    Subclasses psycopg2.OperationalError ON PURPOSE. main() treats a transport failure from
    connect() as "queue this mutating verb to the outbox", and a refusal must behave
    IDENTICALLY — the durability contract (a knowledge write is never silently dropped) and
    the tests that pin it with a deliberately dead DSN must keep spooling, not crash in a new
    way. Being a psycopg2.Error also keeps any `except psycopg2.Error` handler working."""

LocalBrainRefused = UnsharedBrainRefused   # former name; same class


def _target_label(dsn):
    """'host/dbname' — for humans and logs. NEVER a password, token or user."""
    try:
        d = _dsn_params(dsn)
        if not d:
            raise ValueError("unparseable")
        return "{}/{}".format(d.get("host") or "(local socket)", d.get("dbname") or "?")
    except Exception:
        return "(unparseable dsn)"


def _assert_shared_brain(dsn):
    """Fail closed on a brain nobody else can reach.

    Every DSN reaches connect(), whatever its source (--db, KB_DATABASE_URL, or the
    config), so this one check covers them all. Without it a host-less DSN quietly finds a
    local database, commits, and reports ok:true — the exact failure that strands rows
    nobody else can ever read. The shared target is whatever the config names; this check
    does not pin a host, it refuses the hosts that cannot be shared."""
    host = _dsn_host(dsn)
    if not _is_unshared_host(host):
        return
    # A DECLARED single-machine brain: the config says, on purpose, that this brain is one
    # person's on one machine (e.g. one brain across all of that person's own projects). Not a
    # fallback — the config had to name both the local DSN and the scope.
    if brain_scope() == "single-machine":
        return
    # The escape hatch, for OFFLINE / LOCAL DEVELOPMENT ONLY. Rows written under it live
    # on that machine alone and never reach the shared brain — a scratch pad, not the brain.
    # It is loud on every single invocation on purpose: an unnoticed local brain is the bug.
    if os.environ.get("KB_ALLOW_UNSHARED") == "1":
        sys.stderr.write(
            "kb: WARNING — KB_ALLOW_UNSHARED=1, using NON-SHARED brain {}. "
            "Anything written here stays on this machine.\n".format(_target_label(dsn)))
        return
    raise UnsharedBrainRefused(
        "refusing to use a non-shared brain: resolved host {!r} is loopback / a local socket, "
        "which only this machine can reach. The brain is shared across every developer and "
        "every machine (configured target: {}). Point --db / KB_DATABASE_URL / {} at the shared "
        "brain, set \"scope\": \"single-machine\" in that config if a one-machine brain is the "
        "intent, or set KB_ALLOW_UNSHARED=1 for offline local development only. "
        "See docs/brain-setup.md.".format(
            host or "(none — local unix socket)",
            CONFIG.get("database_url") or "unset, see kb.config.json", CONFIG_PATH))


def connect(dsn):
    _assert_shared_brain(dsn)
    kwargs = {"cursor_factory": RealDictCursor}
    kwargs.update(_auth_kwargs(dsn))
    conn = psycopg2.connect(dsn, **kwargs)
    try:
        register_vector(conn)
    except Exception:
        pass  # vector type registers once the extension exists; harmless if missing
    return conn

def sanitize(row):
    """Drop the raw embedding from output (huge + not useful to callers)."""
    if row is None:
        return None
    row = dict(row)
    if "embedding" in row:
        emb = row.pop("embedding")
        row["embedding_present"] = emb is not None
    return row

# Which brain this process resolved, as 'host/dbname' — set by main() for MUTATING verbs only
# (see set_brain_target). Nothing used to print the target, so a write landing in the wrong
# brain was invisible; now every write says where it went, in the envelope callers already
# parse. Additive key, per the module contract. Never carries a credential.
_BRAIN_TARGET = None

def set_brain_target(dsn):
    global _BRAIN_TARGET
    _BRAIN_TARGET = _target_label(dsn)

def _with_target(kw):
    if _BRAIN_TARGET and "brain" not in kw:
        kw["brain"] = _BRAIN_TARGET
        kw.setdefault("scope", brain_scope())     # additive: which kind of brain it landed in
        kw.setdefault("project", PROJECT)         # additive: which project the write was stamped with
    return kw

# ── project scoping helpers ──────────────────────────────────────────────────
# Every handler reads the invocation's project from `a._project` (set by main()); `a._all` is
# true for `--project all`, which lifts the READ filter. Writes always stamp a single project.
# A brain whose schema predates the project column (schema.sql not yet re-applied) still
# answers reads — unfiltered, with a warning — and queues writes (see main()).

def cur_project(a):
    return getattr(a, "_project", None) or PROJECT

def all_projects(a):
    return bool(getattr(a, "_all", False))

def visible_projects(a):
    p = cur_project(a)
    return [p] if p == GLOBAL_PROJECT else [p, GLOBAL_PROJECT]

def has_project_col(cur, table):
    return has_col(cur, table, "project")

def proj_where(cur, a, table, col="project"):
    """(sql, params) — the default read scope: rows of the current project + 'global'.
    `--project all` (or a brain without the column yet) returns ("TRUE", [])."""
    if all_projects(a) or not has_project_col(cur, table):
        return "TRUE", []
    return "{} = ANY(%s)".format(col), [visible_projects(a)]

def proj_order(cur, a, table):
    """(sql, params) — current-project rows sort ahead of 'global' ones in a mixed read."""
    if all_projects(a) or not has_project_col(cur, table):
        return "", []
    return "(project = %s) DESC, ", [cur_project(a)]

def proj_report(cur, a, table):
    """The additive `projects` key on a read: what scope was applied."""
    if not has_project_col(cur, table):
        return {"filter": "none", "warning": "project_column_missing: re-apply the schema "
                "(kb schema --apply) to scope reads by project"}
    if all_projects(a):
        return {"filter": "all"}
    return {"filter": visible_projects(a), "current": cur_project(a)}

def ok(**kw):
    kw.setdefault("ok", True)
    print(json.dumps(_with_target(kw), default=str))
    return 0

def fail(error, **kw):
    kw["ok"] = False
    kw["error"] = error
    print(json.dumps(_with_target(kw), default=str))
    return 1

def parse_since(s):
    """'7d' / '24h' / '30m' / '90s' -> SQL interval string. Validated, no injection."""
    m = re.fullmatch(r"(\d+)\s*([smhdw])", (s or "").strip())
    if not m:
        return None
    n, unit = m.group(1), m.group(2)
    return {"s": "{} seconds".format(n), "m": "{} minutes".format(n), "h": "{} hours".format(n),
            "d": "{} days".format(n), "w": "{} weeks".format(n)}[unit]

def warnings_of(*ws):
    """Collapse a warning list into (primary, all) — `warning` stays a scalar string for
    existing parsers; `warnings` is the additive full list."""
    all_w = [w for w in ws if w]
    return (all_w[0] if all_w else None), all_w

# ── the write ledger ─────────────────────────────────────────────────────────

LEDGER_SQL = """INSERT INTO kb_writes
                  (verb, store, action, row_id, source, ticket, loop_id, run_id, area, summary)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id"""
LEDGER_PROJECT_SQL = """INSERT INTO kb_writes
                  (verb, store, action, row_id, source, ticket, loop_id, run_id, area, summary,
                   project)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id"""

# The loop runner tags every write for free by exporting these; a CLI flag always wins.
TAG_ENV = {"ticket": "LOOP_TICKET", "loop_id": "LOOP_ID", "run_id": "LOOP_RUN_ID"}

def _flag_or_env(a, attr, env):
    """CLI flag wins; otherwise the loop runner's environment tags the write."""
    v = getattr(a, attr, None)
    if v:
        return v
    v = os.environ.get(env)
    return v or None

def _tag(a, attr):
    return _flag_or_env(a, attr, TAG_ENV[attr])

def ledger(cur, a, verb, store, action, row_id=None, area="", summary="", project=None):
    """Insert ONE kb_writes row in the caller's transaction and return the receipt.

    Wrapped in a SAVEPOINT so a ledger-side fault (e.g. schema.sql not yet applied on this
    machine) degrades the receipt instead of destroying the knowledge write it is recording.
    The receipt is still transactional: rolling the write back rolls the receipt back too."""
    src = getattr(a, "source", None) or getattr(a, "by", None) or ""
    params = (verb, store, action, row_id, src,
              _tag(a, "ticket"), _tag(a, "loop_id"), _tag(a, "run_id"),
              norm_area(area), (summary or "")[:200])
    sql = LEDGER_SQL
    if has_project_col(cur, "kb_writes"):          # provenance records the project too
        sql = LEDGER_PROJECT_SQL
        params = params + (project or cur_project(a),)
    cur.execute("SAVEPOINT kb_ledger")
    try:
        cur.execute(sql, params)
        rid = cur.fetchone()["id"]
        cur.execute("RELEASE SAVEPOINT kb_ledger")
        return {"id": rid, "store": store, "row_id": row_id}
    except psycopg2.Error as e:
        cur.execute("ROLLBACK TO SAVEPOINT kb_ledger")
        return {"id": None, "store": store, "row_id": row_id,
                "error": "ledger_unavailable: {}: {}".format(type(e).__name__, str(e).strip())}

# ── verb handlers ────────────────────────────────────────────────────────────

def _app_map_lookup(cur, kind, area, key, project=None):
    """(row, matched) — the row this (kind, area, key) means, whatever casing it was stored
    under, IN THIS PROJECT (a 'global' row with the same key is a different row — a project
    write shadows it, never edits it). Preference order: exact normalized pair → exact raw
    pair → anything that normalizes to the same pair (newest first). Finding the
    pre-migration row is what stops a normalized write from silently forking a duplicate."""
    kv, av = kind_variants(kind), area_variants(area)
    psql, pparams = "", []
    if project is not None and has_project_col(cur, "app_map"):
        psql, pparams = " AND project = %s", [project]
    cur.execute("""SELECT * FROM app_map WHERE {} AND {} AND key=%s{}
                   ORDER BY (kind=%s AND area=%s) DESC, (kind=%s AND area=%s) DESC,
                            updated_at DESC LIMIT 1""".format(norm_pred("kind"), norm_pred("area"), psql),
                tuple([kv, kv[0], av, av[0], key] + pparams + [kv[0], av[0], kv[-1], av[-1]]))
    row = cur.fetchone()
    if row is None:
        return None, None
    if row["kind"] == kv[0] and row["area"] == av[0]:
        return row, "normalized"
    if row["kind"] == kv[-1] and row["area"] == av[-1]:
        return row, "raw"
    return row, "normalized_scan"

_FIX_LESSON_BUMP_SQL = """UPDATE fix_lessons SET occurrences = occurrences + 1, last_seen_at = now(),
                            updated_at = now(), version = version + 1,
                            root_cause = COALESCE(NULLIF(%s,''), root_cause),
                            fix_shape  = COALESCE(NULLIF(%s,''), fix_shape),
                            ticket     = COALESCE(%s, ticket)
                          WHERE id=%s RETURNING *"""

def bump_fix_lesson(cur, a, row_id):
    """THE bump. Both dedup guards (hash and vector) call this, so a hash bump and a vector
    bump are the same UPDATE with the same COALESCE semantics — the guard decides only WHICH
    row is bumped, never how."""
    cur.execute(_FIX_LESSON_BUMP_SQL,
                (a.root_cause or "", a.fix_shape or "",
                 _tag(a, "ticket"), row_id))
    return cur.fetchone()

def cmd_write(cur, a, embed):
    if a.store == "app_map":
        if not (a.kind and a.key and a.value):
            return fail("app_map write requires --kind --key --value")
        try:
            val = json.loads(a.value)
        except ValueError as e:
            return fail("bad --value JSON: {}".format(e))
        nkind, narea = norm_kind(a.kind), norm_area(a.area)
        w_kind = None if nkind in KNOWN_KINDS else "unknown_kind"
        proj = cur_project(a)
        existing, matched = _app_map_lookup(cur, a.kind, a.area, a.key, proj)
        # A row found only under its RAW (kind, area) is updated IN PLACE — renaming it here
        # could collide with an already-normalized twin. migrate_normalize.py owns renaming.
        w_raw = ("unnormalized_row: matched on the raw (kind, area); run "
                 "scripts/migrate_normalize.py to fold it into '{}/{}'".format(nkind, narea)
                 ) if matched == "raw" else None
        warning, all_w = warnings_of(w_kind, w_raw)

        if existing is None:
            cur.execute("""INSERT INTO app_map(kind,area,key,value,source,confidence,ticket,project)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                        (nkind, narea, a.key, Json(val), a.source, a.confidence,
                         _tag(a, "ticket"), proj))
            row = cur.fetchone()
            rec = ledger(cur, a, "write", "app_map", "insert", row["id"], narea, a.key)
            return ok(action="insert", row=sanitize(row), matched="new", receipt=rec,
                      warning=warning, warnings=all_w)

        if a.expect_version is not None:
            cur.execute("""UPDATE app_map SET value=%s, source=%s, confidence=%s,
                           ticket=COALESCE(%s, ticket),
                           last_confirmed_at=now(), updated_at=now(), version=version+1
                           WHERE id=%s AND version=%s RETURNING *""",
                        (Json(val), a.source, a.confidence,
                         _tag(a, "ticket"),
                         existing["id"], a.expect_version))
            updated = cur.fetchone()
            if updated is None:
                # SEMANTIC failure — a real disagreement. Never queued, never ledgered.
                return fail("version_conflict",
                            expected=a.expect_version, current=existing["version"], id=existing["id"])
            rec = ledger(cur, a, "write", "app_map", "update", updated["id"], updated["area"], a.key)
            return ok(action="update", row=sanitize(updated), matched=matched, receipt=rec,
                      warning=warning, warnings=all_w)

        cur.execute("""UPDATE app_map SET value=%s, source=%s, confidence=%s,
                       ticket=COALESCE(%s, ticket),
                       last_confirmed_at=now(), updated_at=now(), version=version+1
                       WHERE id=%s RETURNING *""",
                    (Json(val), a.source, a.confidence,
                     _tag(a, "ticket"), existing["id"]))
        row = cur.fetchone()
        rec = ledger(cur, a, "write", "app_map", "update_refresh", row["id"], row["area"], a.key)
        return ok(action="update_refresh", row=sanitize(row), matched=matched, receipt=rec,
                  warning=warning, warnings=all_w)

    if a.store == "flakiness":
        if not a.spec:
            return fail("flakiness write requires --spec")
        failed = 1 if a.failed else 0
        area = norm_area(a.area)
        derived = False
        if not area:
            area = derive_area_from_spec(a.spec)
            derived = bool(area)
        warning, all_w = warnings_of(
            None if area else "flakiness_area_empty: pass --area so the R7 slice can find this row")
        cur.execute("""INSERT INTO flakiness(spec, area, signature, runs, failures, source, last_seen_at,
                                             project)
                       VALUES(%s,%s,%s,1,%s,%s,now(),%s)
                       ON CONFLICT (project, spec, signature) DO UPDATE
                         SET runs = flakiness.runs + 1,
                             failures = flakiness.failures + EXCLUDED.failures,
                             area = CASE WHEN flakiness.area = '' THEN EXCLUDED.area ELSE flakiness.area END,
                             last_seen_at = now(), updated_at = now(),
                             version = flakiness.version + 1
                       RETURNING *""",
                    (a.spec, area, a.signature or "", failed, a.source, cur_project(a)))
        row = cur.fetchone()
        rec = ledger(cur, a, "write", "flakiness", "upsert", row["id"], row["area"], a.spec)
        return ok(action="upsert", row=sanitize(row), receipt=rec,
                  area_derived_from_spec=derived, warning=warning, warnings=all_w)

    if a.store in ("fix_lesson", "fix_lessons"):
        if not a.symptom:
            return fail("fix_lessons write requires --symptom")
        area = norm_area(a.area)
        proj = cur_project(a)
        dup_threshold = a.dup_threshold if a.dup_threshold is not None else 0.45
        try:
            meta = json.loads(a.metadata) if a.metadata else {}
        except ValueError as e:
            return fail("bad --metadata JSON: {}".format(e))

        # ── GUARD 1: exact duplicate, by hash. Runs BEFORE any embedding attempt, so a retry
        # is safe whether the embedder is up or down — and an exact repeat costs no API call.
        hashing = has_col(cur, "fix_lessons", "symptom_hash")
        w_hash = None if hashing else HASH_UNAVAILABLE.format("fix_lessons", "symptom_hash")
        shash = text_hash(a.symptom)
        if hashing:
            hit = hash_dup_lookup(cur, "fix_lessons", "symptom_hash", shash, a.area,
                                  extra_sql="AND project = %s", extra_params=(proj,))
            if hit is not None:
                row = bump_fix_lesson(cur, a, hit["id"])
                rec = ledger(cur, a, "write", "fix_lessons", "bump", row["id"], row["area"], a.symptom)
                warning, all_w = warnings_of(w_hash)
                return ok(action="bump", similarity=1.0, row=sanitize(row), receipt=rec,
                          dedup_by="hash", dedup_skipped=True,
                          embed_status=row.get("embed_status"),
                          warning=warning, warnings=all_w)

        vec, emb_err = try_embed(embed, a.symptom)

        if vec is None:
            # DURABILITY: the embedder is down (rate limit, missing key). Store the lesson anyway —
            # only its searchability is deferred. `kb reembed` heals it. Exit 0.
            emb_w = "embedder_unavailable: {}".format(emb_err)
            warning, all_w = warnings_of(emb_w, w_hash)
            cur.execute("""INSERT INTO fix_lessons
                             (area, symptom, root_cause, fix_shape, signature_tag,
                              embedding, embedding_model, embedding_dim, embed_status,
                              source, ticket, metadata, project{hc})
                           VALUES (%s,%s,%s,%s,%s,NULL,'',%s,'pending',%s,%s,%s,%s{hv})
                           RETURNING *""".format(hc=", symptom_hash" if hashing else "",
                                                 hv=", %s" if hashing else ""),
                        (area, a.symptom, a.root_cause or "", a.fix_shape or "",
                         a.signature_tag or "", EMB_DIM, a.source,
                         _tag(a, "ticket"), Json(meta), proj)
                        + ((shash,) if hashing else ()))
            row = cur.fetchone()
            rec = ledger(cur, a, "write", "fix_lessons", "insert_pending", row["id"], area, a.symptom)
            return ok(action="insert", row=sanitize(row), receipt=rec,
                      embed_status="pending", dedup_skipped=True, dedup_by=None,
                      warning=warning, warnings=all_w)

        ev = vlit(vec)
        # ── GUARD 2: near-duplicate symptom → bump occurrences (accumulated experience),
        # don't insert a clone (R10). Only reachable when the hash guard found nothing.
        # Scoped to THIS project: a bump edits the row, and another project's lesson is not
        # this project's to edit.
        cur.execute("""SELECT id, occurrences, 1-(embedding <=> %s::vector) AS similarity
                       FROM fix_lessons WHERE embedding IS NOT NULL AND project = %s
                       ORDER BY embedding <=> %s::vector LIMIT 1""", (ev, proj, ev))
        near = cur.fetchone()
        if near and near["similarity"] >= dup_threshold:
            row = bump_fix_lesson(cur, a, near["id"])
            if hashing:                      # heal the older row's NULL hash while we are here
                row = heal_hash(cur, "fix_lessons", "symptom_hash", row, row["symptom"])
            rec = ledger(cur, a, "write", "fix_lessons", "bump", row["id"], row["area"], a.symptom)
            warning, all_w = warnings_of(w_hash)
            return ok(action="bump", similarity=near["similarity"], row=sanitize(row),
                      receipt=rec, embed_status="ok", dedup_by="vector", dedup_skipped=False,
                      warning=warning, warnings=all_w)
        cur.execute("""INSERT INTO fix_lessons
                         (area, symptom, root_cause, fix_shape, signature_tag,
                          embedding, embedding_model, embedding_dim, embed_status,
                          source, ticket, metadata, project{hc})
                       VALUES (%s,%s,%s,%s,%s,%s::vector,%s,%s,'ok',%s,%s,%s,%s{hv})
                       RETURNING *""".format(hc=", symptom_hash" if hashing else "",
                                             hv=", %s" if hashing else ""),
                    (area, a.symptom, a.root_cause or "", a.fix_shape or "", a.signature_tag or "",
                     ev, embed.model, EMB_DIM, a.source,
                     _tag(a, "ticket"), Json(meta), proj)
                    + ((shash,) if hashing else ()))
        row = cur.fetchone()
        rec = ledger(cur, a, "write", "fix_lessons", "insert", row["id"], area, a.symptom)
        warning, all_w = warnings_of(w_hash)
        return ok(action="insert", row=sanitize(row), receipt=rec, embed_status="ok",
                  dedup_by=None, dedup_skipped=False, warning=warning, warnings=all_w)

    if a.store in ("lesson", "lessons"):
        return write_lesson(cur, a, embed)

    return fail("write: unsupported store {}".format(a.store))

def cmd_read(cur, a, embed):
    """Non-vector reads — the documented degraded path when the embedder is down.
    `area`/`kind` are normalized on the READ side too, and a row stored under any other
    casing/spacing still matches (see norm_pred); `normalization.matched` says how.
    Every slice read is scoped to the current project + 'global' (`--project all` lifts it);
    an `--id` read is an exact handle and is never project-filtered."""
    table = {"fix_lesson": "fix_lessons", "lesson": "lessons"}.get(a.store, a.store)
    if table == "lessons":
        return read_lessons(cur, a)
    if table in ("app_map", "expectations", "flakiness", "fix_lessons"):
        pw, pp = proj_where(cur, a, table)
        po, pop = proj_order(cur, a, table)
        preport = proj_report(cur, a, table)
        if getattr(a, "id", None):
            preport = {"filter": "id"}
    if a.store == "app_map":
        if a.id:
            cur.execute("SELECT * FROM app_map WHERE id=%s", (a.id,))
        elif a.key and not (a.area or a.kind):
            # a bare `--key` names no slice; it silently meant area='' and returned nothing.
            return fail("app_map read requires --id, --area, or --kind alongside --key")
        elif a.key:
            # `--key` narrows, with or without `--kind`. It used to be honoured ONLY when
            # `--kind` was also given: `read app_map --area X --key Y` silently dropped the
            # key and returned the WHOLE area, so a caller taking rows[0] got an arbitrary
            # row from the slice. A brain read that quietly answers a different question than
            # it was asked is worse than one that errors.
            av = area_variants(a.area)
            where = [norm_pred("area"), "key=%s", pw]
            params = [av, av[0], a.key] + pp
            order = ["(area=%s) DESC"]
            oparams = [av[0]]
            kv = None
            if a.kind:
                kv = kind_variants(a.kind)
                where.insert(0, norm_pred("kind"))
                params = [kv, kv[0]] + params
                order.insert(0, "(kind=%s) DESC")
                oparams = [kv[0]] + oparams
            # the current project's row sorts ahead of a 'global' row with the same key
            cur.execute("SELECT * FROM app_map WHERE {} ORDER BY {}{}, updated_at DESC".format(
                            " AND ".join(where), po, ", ".join(order)),
                        tuple(params + pop + oparams))
            rows_raw = cur.fetchall()
            norm = {"area": match_info(av, rows_raw, "area")}
            if kv is not None:
                norm["kind"] = match_info(kv, rows_raw, "kind")
            return ok(count=len(rows_raw), rows=[sanitize(r) for r in rows_raw],
                      normalization=norm, projects=preport)
        elif a.area:
            rows_raw, report = fetch_by_area(cur, "app_map", a.area, "kind, key", a)
            return ok(count=len(rows_raw), rows=[sanitize(r) for r in rows_raw],
                      normalization=report, projects=preport)
        else:
            return fail("app_map read requires --id, (--kind --key), or --area")
    elif a.store == "expectations":
        if a.id:
            cur.execute("SELECT * FROM expectations WHERE id=%s", (a.id,))
        elif a.area:
            rows_raw, report = fetch_by_area(cur, "expectations", a.area, "status, id", a)
            return ok(count=len(rows_raw), rows=[sanitize(r) for r in rows_raw],
                      normalization=report, projects=preport)
        else:
            return fail("expectations read requires --id or --area (use `search` for semantic)")
    elif a.store == "flakiness":
        if a.spec:
            cur.execute("SELECT * FROM flakiness WHERE spec=%s AND {} ORDER BY {}signature".format(pw, po),
                        tuple([a.spec] + pp + pop))
        elif a.area:
            rows_raw, report = fetch_by_area(cur, "flakiness", a.area, "spec", a)
            return ok(count=len(rows_raw), rows=[sanitize(r) for r in rows_raw],
                      normalization=report, projects=preport)
        else:
            return fail("flakiness read requires --spec or --area")
    elif a.store in ("fix_lessons", "fix_lesson"):
        if a.id:
            cur.execute("SELECT * FROM fix_lessons WHERE id=%s", (a.id,))
        elif a.area:
            rows_raw, report = fetch_by_area(cur, "fix_lessons", a.area, "occurrences DESC, id", a)
            return ok(count=len(rows_raw), rows=[sanitize(r) for r in rows_raw],
                      normalization=report, projects=preport)
        else:
            return fail("fix_lessons read requires --id or --area (use `search` for semantic)")
    else:
        return fail("read: unsupported store {}".format(a.store))
    rows = [sanitize(r) for r in cur.fetchall()]
    return ok(count=len(rows), rows=rows, projects=preport)

def cmd_propose(cur, a, embed):
    area = norm_area(a.area)
    proj = cur_project(a)
    try:
        meta = json.loads(a.metadata) if a.metadata else {}
    except ValueError as e:
        return fail("bad --metadata JSON: {}".format(e))

    # ── GUARD 1: exact duplicate, by hash, BEFORE any embedding attempt. A duplicate
    # proposal is the thing that floods the human bless queue (R10), and with the embedder
    # down the vector guard cannot run at all — so an identical statement returns the EXISTING row
    # and inserts nothing. Never an error: the caller's knowledge is already in the brain.
    hashing = has_col(cur, "expectations", "statement_hash")
    w_hash = None if hashing else HASH_UNAVAILABLE.format("expectations", "statement_hash")
    shash = text_hash(a.statement)
    if hashing:
        # 'forgotten' is deliberately excluded — a row taken out of play should not block a
        # fresh proposal. A 'rejected'/'canonical' hit DOES return, and its status is the
        # caller's signal that this exact statement has already been judged.
        hit = hash_dup_lookup(cur, "expectations", "statement_hash", shash, a.area,
                              extra_sql="AND status <> 'forgotten' AND project = %s",
                              extra_params=(proj,))
        if hit is not None:
            warning, all_w = warnings_of("duplicate_proposal", w_hash)
            return ok(action="duplicate", row=sanitize(hit), duplicate_of=hit["id"],
                      nearest=[], receipt=None, dedup_by="hash", dedup_skipped=True,
                      embed_status=hit.get("embed_status"), warning=warning, warnings=all_w)

    vec, emb_err = try_embed(embed, a.statement)

    if vec is None:
        # DURABILITY: proposal lands without a vector; VECTOR dedup needs one, so it is
        # skipped (the hash guard above already ran).
        emb_w = "embedder_unavailable: {}".format(emb_err)
        warning, all_w = warnings_of(emb_w, w_hash)
        cur.execute("""INSERT INTO expectations
                         (area, statement, embedding, embedding_model, embedding_dim,
                          embed_status, status, source, confidence, metadata, project{hc})
                       VALUES (%s,%s,NULL,'',%s,'pending','proposed',%s,%s,%s,%s{hv})
                       RETURNING *""".format(hc=", statement_hash" if hashing else "",
                                             hv=", %s" if hashing else ""),
                    (area, a.statement, EMB_DIM, a.source, a.confidence, Json(meta), proj)
                    + ((shash,) if hashing else ()))
        new = cur.fetchone()
        rec = ledger(cur, a, "propose", "expectations", "propose_pending",
                     new["id"], area, a.statement)
        return ok(action="propose", row=sanitize(new), nearest=[], receipt=rec,
                  embed_status="pending", dedup_skipped=True, dedup_by=None,
                  warning=warning, warnings=all_w)

    ev = vlit(vec)
    cur.execute("""INSERT INTO expectations
                     (area, statement, embedding, embedding_model, embedding_dim,
                      embed_status, status, source, confidence, metadata, project{hc})
                   VALUES (%s,%s,%s::vector,%s,%s,'ok','proposed',%s,%s,%s,%s{hv})
                   RETURNING *""".format(hc=", statement_hash" if hashing else "",
                                         hv=", %s" if hashing else ""),
                (area, a.statement, ev, embed.model, EMB_DIM, a.source, a.confidence, Json(meta), proj)
                + ((shash,) if hashing else ()))
    new = cur.fetchone()
    # ── GUARD 2: near-duplicate WARNING (never a merge — a human judges expectations).
    # Neighbours come from what this project can SEE (its own rows + 'global').
    cur.execute("""SELECT id, area, statement, status, project,
                          1-(embedding <=> %s::vector) AS similarity
                   FROM expectations
                   WHERE id <> %s AND embedding IS NOT NULL AND status <> 'forgotten'
                     AND project = ANY(%s)
                   ORDER BY embedding <=> %s::vector LIMIT %s""",
                (ev, new["id"], visible_projects(a), ev, a.k))
    neighbors = cur.fetchall()
    w_dup = "possible_duplicate" if neighbors and neighbors[0]["similarity"] >= a.dup_threshold else None
    warning, all_w = warnings_of(w_dup, w_hash)
    rec = ledger(cur, a, "propose", "expectations", "propose", new["id"], area, a.statement)
    return ok(action="propose", row=sanitize(new), nearest=neighbors, warning=warning,
              warnings=all_w, receipt=rec, embed_status="ok",
              dedup_by=None, dedup_skipped=False)

def cmd_search(cur, a, embed):
    if a.store == "app_map":
        # Fuzzy, embedder-free lookup (ILIKE over key / area / value) — the documented
        # degraded path when the embedder is down, and how a fix step finds a lesson row
        # without knowing its exact key.
        if not a.query:
            return fail("search app_map requires --query")
        k = a.k if a.k is not None else 20
        pat = "%{}%".format(a.query)
        pw, pp = proj_where(cur, a, "app_map")
        sql = ["""SELECT id, kind, area, key, value, source, confidence, status, ticket,
                         last_confirmed_at, updated_at, version{} FROM app_map
                  WHERE (key ILIKE %s OR area ILIKE %s OR value::text ILIKE %s)""".format(
                      ", project" if has_project_col(cur, "app_map") else ""),
               "AND " + pw]
        params = [pat, pat, pat] + pp
        norm = {}
        if a.area:
            av = area_variants(a.area)
            sql.append("AND " + norm_pred("area")); params += [av, av[0]]; norm["area"] = {"tried": av}
        if a.kind:
            kv = kind_variants(a.kind)
            sql.append("AND " + norm_pred("kind")); params += [kv, kv[0]]; norm["kind"] = {"tried": kv}
        sql.append("ORDER BY last_confirmed_at DESC, id DESC LIMIT %s"); params.append(k)
        cur.execute(" ".join(sql), params)
        rows = [sanitize(r) for r in cur.fetchall()]
        for field in list(norm):
            norm[field] = match_info(norm[field]["tried"], rows, field)
        return ok(count=len(rows), results=rows, normalization=norm or None,
                  projects=proj_report(cur, a, "app_map"))

    if a.store == "expectations":
        if not a.query:
            return fail("search expectations requires --query")
        k = a.k if a.k is not None else 5
        vec, emb_err = try_embed(embed, a.query)
        if vec is None:
            # Reads still fail CLEARLY — a silent empty result would look like "no oracle".
            return fail("embedder_unavailable: {}".format(emb_err),
                        degraded_path="kb read expectations --area <area> (non-vector)")
        ev = vlit(vec)
        pw, pp = proj_where(cur, a, "expectations")
        sql = ["SELECT id, area, statement, status, source, confidence, embed_status,",
               ("project, " if has_project_col(cur, "expectations") else ""),
               "1-(embedding <=> %s::vector) AS similarity FROM expectations",
               "WHERE embedding IS NOT NULL AND status <> 'forgotten'", "AND " + pw]
        params = [ev] + pp
        if a.status:
            sql.append("AND status = ANY(%s)"); params.append(a.status.split(","))
        if a.area:
            av = area_variants(a.area)
            sql.append("AND " + norm_pred("area")); params += [av, av[0]]
        sql.append("ORDER BY embedding <=> %s::vector LIMIT %s"); params += [ev, k]
        cur.execute(" ".join(sql), params)
        rows = cur.fetchall()
        if a.min_similarity is not None:
            rows = [r for r in rows if r["similarity"] >= a.min_similarity]
        return ok(count=len(rows), results=rows, projects=proj_report(cur, a, "expectations"))

    if a.store == "flakiness":
        pw, pp = proj_where(cur, a, "flakiness")
        if a.spec:
            cur.execute("SELECT * FROM flakiness WHERE spec ILIKE %s AND " + pw +
                        " ORDER BY flake_rate DESC", tuple(["%{}%".format(a.spec)] + pp))
        elif a.signature:
            cur.execute("SELECT * FROM flakiness WHERE signature ILIKE %s AND " + pw +
                        " ORDER BY flake_rate DESC", tuple(["%{}%".format(a.signature)] + pp))
        elif a.area:
            av = area_variants(a.area)
            cur.execute("SELECT * FROM flakiness WHERE " + norm_pred("area") + " AND " + pw +
                        " ORDER BY flake_rate DESC", tuple([av, av[0]] + pp))
        else:
            return fail("search flakiness requires --spec, --signature, or --area")
        rows = [sanitize(r) for r in cur.fetchall()]
        return ok(count=len(rows), results=rows, projects=proj_report(cur, a, "flakiness"))

    if a.store in ("fix_lessons", "fix_lesson"):
        if not a.query:
            return fail("search fix_lessons requires --query")
        k = a.k if a.k is not None else 5
        vec, emb_err = try_embed(embed, a.query)
        if vec is None:
            return fail("embedder_unavailable: {}".format(emb_err),
                        degraded_path="kb read fix_lessons --area <area> (non-vector)")
        ev = vlit(vec)
        pw, pp = proj_where(cur, a, "fix_lessons")
        sql = ["SELECT id, area, symptom, root_cause, fix_shape, signature_tag, occurrences,",
               "source, ticket, embed_status,",
               ("project, " if has_project_col(cur, "fix_lessons") else ""),
               "1-(embedding <=> %s::vector) AS similarity FROM fix_lessons",
               "WHERE embedding IS NOT NULL", "AND " + pw]
        params = [ev] + pp
        if a.area:
            av = area_variants(a.area)
            sql.append("AND " + norm_pred("area")); params += [av, av[0]]
        sql.append("ORDER BY embedding <=> %s::vector LIMIT %s"); params += [ev, k]
        cur.execute(" ".join(sql), params)
        rows = cur.fetchall()
        if a.min_similarity is not None:
            rows = [r for r in rows if r["similarity"] >= a.min_similarity]
        return ok(count=len(rows), results=rows, projects=proj_report(cur, a, "fix_lessons"))

    if a.store in ("lessons", "lesson"):
        return search_lessons(cur, a, embed)

    return fail("search: unsupported store {}".format(a.store))

def cmd_confirm(cur, a, embed):
    """Freshness, not change: refresh last_confirmed_at on a row a skill actually USED.
    Deliberately does NOT touch value/updated_at/version — a confirm is not an edit, and
    `decay`'s stale_app_map report is only meaningful if confirms are cheap and honest."""
    if a.store != "app_map":
        return fail("confirm: unsupported store {} (only app_map)".format(a.store))
    if a.source:
        cur.execute("""UPDATE app_map SET last_confirmed_at=now(), source=%s
                       WHERE id=%s RETURNING *""", (a.source, a.id))
    else:
        cur.execute("UPDATE app_map SET last_confirmed_at=now() WHERE id=%s RETURNING *", (a.id,))
    row = cur.fetchone()
    if row is None:
        return fail("not_found", id=a.id)
    rec = ledger(cur, a, "confirm", "app_map", "confirm", row["id"], row["area"], row["key"])
    return ok(action="confirm", row=sanitize(row), receipt=rec)

def cmd_reembed(cur, a, embed):
    """Maintenance verb, never a gate: heal rows the embedder was down for.

    Per-row failure is REPORTED and the row stays pending — an outage across every row still
    exits 0 with a summary. Reports a now-visible `similar_to` neighbour but NEVER
    auto-merges: a human (or the dream) decides what is really a duplicate."""
    stores = ["expectations", "fix_lessons"] if a.store in (None, "all") else [a.store]
    if a.store in (None, "all") and _table_exists(cur, "lessons"):
        stores.append("lessons")
    text_col = {"expectations": "statement", "fix_lessons": "symptom", "lessons": "text"}
    default_dup = {"expectations": 0.70, "fix_lessons": 0.45, "lessons": LESSON_DUP_THRESHOLD}
    results, receipts, embedded, failed, scanned = [], [], 0, 0, 0

    for store in stores:
        col = text_col[store]
        sql = ["SELECT id, {} AS area, {} AS text FROM {}".format(
                   "''" if store == "lessons" else "area", col, store),
               "WHERE (embed_status = 'pending' OR embedding IS NULL)"]
        params = []
        if a.filter_source:
            sql.append("AND source = %s"); params.append(a.filter_source)
        if a.id:
            sql.append("AND id = ANY(%s)"); params.append(list(a.id))
        sql.append("ORDER BY id LIMIT %s"); params.append(a.limit)
        cur.execute(" ".join(sql), params)
        rows = cur.fetchall()
        scanned += len(rows)

        for r in rows:
            if a.dry_run:
                results.append({"id": r["id"], "store": store, "ok": None, "dry_run": True,
                                "area": r["area"], "text": (r["text"] or "")[:120]})
                continue
            vec, emb_err = try_embed(embed, r["text"] or "")
            if vec is None:
                failed += 1
                results.append({"id": r["id"], "store": store, "ok": False, "error": emb_err[:300]})
                continue
            ev = vlit(vec)
            cur.execute("""UPDATE {} SET embedding=%s::vector, embedding_model=%s, embedding_dim=%s,
                             embed_status='ok', updated_at=now(), version=version+1
                           WHERE id=%s RETURNING id, version""".format(store),
                        (ev, embed.model, EMB_DIM, r["id"]))
            updated = cur.fetchone()
            thr = a.dup_threshold if a.dup_threshold is not None else default_dup[store]
            cur.execute("""SELECT id, {} AS text, 1-(embedding <=> %s::vector) AS similarity
                           FROM {} WHERE id <> %s AND embedding IS NOT NULL
                           ORDER BY embedding <=> %s::vector LIMIT 1""".format(col, store),
                        (ev, r["id"], ev))
            near = cur.fetchone()
            similar_to = None
            if near and near["similarity"] is not None and near["similarity"] >= thr:
                similar_to = {"id": near["id"], "similarity": near["similarity"],
                              "text": (near["text"] or "")[:120]}
            rec = ledger(cur, a, "reembed", store, "reembed", r["id"], r["area"] or "",
                         (r["text"] or "")[:120])
            embedded += 1
            receipts.append(rec)
            results.append({"id": r["id"], "store": store, "ok": True,
                            "version": updated["version"], "similar_to": similar_to,
                            "receipt_id": rec.get("id")})

    warning = None
    if failed:
        warning = ("embedder_unavailable for {} row(s) — they stay embed_status='pending'; "
                   "re-run `kb reembed` when the embedder recovers".format(failed))
    return ok(action="reembed", dry_run=bool(a.dry_run), stores=stores, scanned=scanned,
              embedded=embedded, failed=failed, results=results,
              receipt=receipts[0] if receipts else None, receipts=receipts,
              embedder=embed.provider, embedding_model=embed.model,
              warning=warning, auto_merge=False)

def cmd_bless(cur, a, embed):
    if a.store in ("lesson", "lessons"):
        return set_lesson_status(cur, a, "active")
    if not a.by:
        return fail("bless expectations requires --by <human> (the bless is a human judgment)")
    if a.expect_version is not None:
        cur.execute("""UPDATE expectations SET status='canonical', blessed_by=%s, blessed_at=now(),
                       last_confirmed_at=now(), updated_at=now(), version=version+1
                       WHERE id=%s AND status='proposed' AND version=%s RETURNING *""",
                    (a.by, a.id, a.expect_version))
    else:
        cur.execute("""UPDATE expectations SET status='canonical', blessed_by=%s, blessed_at=now(),
                       last_confirmed_at=now(), updated_at=now(), version=version+1
                       WHERE id=%s AND status='proposed' RETURNING *""",
                    (a.by, a.id))
    row = cur.fetchone()
    if row is None:
        return fail("bless_failed", detail="not found, not in 'proposed' state, or version conflict", id=a.id)
    rec = ledger(cur, a, "bless", "expectations", "bless", row["id"], row["area"], row["statement"],
                 project=row.get("project"))
    return ok(action="bless", row=sanitize(row), receipt=rec)

def cmd_reject(cur, a, embed):
    if a.store in ("lesson", "lessons"):
        return set_lesson_status(cur, a, "rejected")
    if not a.by:
        return fail("reject expectations requires --by <human>")
    cur.execute("""UPDATE expectations SET status='rejected', updated_at=now(), version=version+1,
                   metadata = metadata || %s
                   WHERE id=%s AND status='proposed' RETURNING *""",
                (Json({"rejected_by": a.by, "reject_reason": a.reason}), a.id))
    row = cur.fetchone()
    if row is None:
        return fail("reject_failed", detail="not found or not in 'proposed' state", id=a.id)
    rec = ledger(cur, a, "reject", "expectations", "reject", row["id"], row["area"], row["statement"],
                 project=row.get("project"))
    return ok(action="reject", row=sanitize(row), receipt=rec)

def cmd_quarantine(cur, a, embed):
    if a.on == a.off:
        return fail("quarantine requires exactly one of --on / --off")
    val = bool(a.on)
    pw, pp = proj_where(cur, a, "flakiness")     # the rows this project can see
    if a.signature is not None:
        cur.execute("""UPDATE flakiness SET quarantined=%s, updated_at=now(), version=version+1
                       WHERE spec=%s AND signature=%s AND {} RETURNING *""".format(pw),
                    tuple([val, a.spec, a.signature] + pp))
    else:
        cur.execute("""UPDATE flakiness SET quarantined=%s, updated_at=now(), version=version+1
                       WHERE spec=%s AND {} RETURNING *""".format(pw), tuple([val, a.spec] + pp))
    rows = [sanitize(r) for r in cur.fetchall()]
    if not rows:
        return fail("not_found", spec=a.spec)
    receipts = [ledger(cur, a, "quarantine", "flakiness", "quarantine_on" if val else "quarantine_off",
                       r["id"], r.get("area") or "", a.spec) for r in rows]
    return ok(action="quarantine", quarantined=val, rows=rows,
              receipt=receipts[0], receipts=receipts)

def cmd_forget(cur, a, embed):
    hard = a.hard or not a.soft
    table = {"lesson": "lessons"}.get(a.store, a.store)
    bulk = False
    # A selector other than --id is a DESTRUCTIVE match, so it is scoped to the current project
    # EXACTLY (not current + 'global'): forgetting from project A never reaches a 'global' row
    # or another project's. `--project all` widens it on purpose. --id is an exact handle.
    if all_projects(a) or not has_project_col(cur, table):
        pscope, pparams = "", ()
    else:
        pscope, pparams = " AND project = %s", (cur_project(a),)
    if a.id:
        where, params, selected_by = "id=%s", (a.id,), "id"
    elif table == "app_map" and a.kind and a.key:
        where = "kind = ANY(%s) AND area = ANY(%s) AND key=%s" + pscope
        params = (kind_variants(a.kind), area_variants(a.area), a.key) + pparams
        selected_by = "kind+key"
    elif table == "flakiness" and a.spec:
        where, params, selected_by = "spec=%s" + pscope, (a.spec,) + pparams, "spec"
    elif a.source:
        # `--source` is a real ROW SELECTOR when nothing else names a row (it stays the
        # ledger's provenance either way). This is the "undo one writer's whole contribution"
        # path qa-dream documents. Bulk by nature, so it PLANS FIRST: without --yes nothing
        # changes and the plan comes back ok:true.
        where, params = "source = ANY(%s)" + pscope, (source_variants(a.source),) + pparams
        selected_by, bulk = "source", True
    else:
        return fail("forget requires --id (or --kind/--key for app_map, --spec for flakiness, "
                    "or --source to select every row one writer wrote)")

    if bulk:
        if not hard and table in NO_SOFT_FORGET:
            return fail("{} has no soft-forget; use --hard".format(table))
        cols = ["id", "{} AS area".format("''" if table == "lessons" else "area"), "source",
                "{} AS summary".format(STORE_LABEL[table])]
        if table in ("app_map", "expectations", "lessons"):
            cols.append("status")
        cur.execute("SELECT {} FROM {} WHERE {} ORDER BY id".format(
                        ", ".join(cols), table, where), params)
        plan = [dict(r) for r in cur.fetchall()]
        for p in plan:
            p["summary"] = (p.get("summary") or "")[:120]
        if not a.yes:
            return ok(action="forget_plan", dry_run=True, store=table, selected_by=selected_by,
                      mode="hard" if hard else "soft", planned=len(plan), rows=plan,
                      matched_sources=sorted(set(p["source"] for p in plan)),
                      note="nothing was changed — re-run with --yes to apply")

    if hard:
        cur.execute("DELETE FROM {} WHERE {} RETURNING id".format(table, where), params)
        ids = [r["id"] for r in cur.fetchall()]
        receipts = [ledger(cur, a, "forget", table, "forget_hard", i) for i in ids]
        return ok(action="forget_hard", deleted=ids,
                  receipt=receipts[0] if receipts else None, receipts=receipts)
    if table in NO_SOFT_FORGET:
        return fail("{} has no soft-forget; use --hard".format(table))
    cur.execute("UPDATE {} SET status='forgotten', updated_at=now(), version=version+1 "
                "WHERE {} RETURNING id".format(table, where), params)
    ids = [r["id"] for r in cur.fetchall()]
    receipts = [ledger(cur, a, "forget", table, "forget_soft", i) for i in ids]
    return ok(action="forget_soft", marked=ids,
              receipt=receipts[0] if receipts else None, receipts=receipts)

# ── ledger-driven undo ───────────────────────────────────────────────────────
# One command to reverse a bad run. It reverses what the LEDGER says the run did — never a
# guess from the rows themselves — and two rules keep it from destroying knowledge it did not
# create:
#   * a row the run CREATED is removed (expectations SOFT, everything else hard);
#   * a row the run only TOUCHED is reversed only where the reversal is exact (a fix_lesson
#     bump decrements), and is otherwise reported as `skipped_unreversible`. Deleting it would
#     destroy knowledge that predates the run being undone — strictly worse than the run
#     never having happened.
# DRY RUN IS THE DEFAULT (it is destructive): `--yes` applies. Idempotent and re-runnable —
# a row already gone counts as `skipped_missing`, never an error.

UNDO_STORES = ("app_map", "expectations", "flakiness", "fix_lessons", "metrics", "lessons")
# Stores whose dedupe guard BUMPS occurrences — an undo reverses a bump by decrementing.
BUMP_STORES = ("fix_lessons", "lessons")
# Ledger actions that mean "this write brought the row into existence".
UNDO_CREATED = ("insert", "insert_pending", "propose", "propose_pending")
UNDO_APPLIED_MODES = ("delete", "soft_forget", "decrement")

class _UndoTags(object):
    """Provenance for the undo's OWN ledger rows: `source` is always 'kb-undo' (never the
    source being undone), while ticket/loop/run stay the selector's so the reversal lands on
    the same run's receipt."""
    def __init__(self, a):
        self.source = "kb-undo"
        self.ticket = a.ticket
        self.loop_id = a.loop_id
        self.run_id = a.run_id

def _undo_marker(ledger_id):
    """The undo receipt names the ledger row it reversed. The trailing ')' is load-bearing:
    it stops '(ledger #45)' from matching inside '(ledger #451)'."""
    return "(ledger #{})".format(ledger_id)

def _undo_receipt_for(cur, lrow):
    """The id of a PREVIOUS undo receipt for this exact ledger row, or None.

    Delete and soft_forget are self-evidencing — the row's own state proves the reversal
    already happened, so a second pass reports skipped_missing. A DECREMENT leaves nothing
    behind that says it happened, so without this it is applied again on every pass, and a
    row that predates the run being undone is driven down to the floor. (Live vector dedupe
    made that the common case: near-duplicate symptoms now bump instead of inserting, so a
    real run's undo plan is mostly decrements.) The receipt is written in the same
    transaction as the decrement, so a rolled-back undo leaves neither."""
    cur.execute("""SELECT id FROM kb_writes
                   WHERE verb='undo' AND store=%s AND row_id=%s
                     AND position(%s in summary) > 0
                   ORDER BY id LIMIT 1""",
                (lrow["store"], lrow["row_id"], _undo_marker(lrow["id"])))
    prior = cur.fetchone()
    return prior["id"] if prior else None

def _undo_one(cur, lrow, apply_it):
    """Decide — and when `apply_it`, perform — the reversal of ONE ledger row.
    `mode` is delete | soft_forget | decrement | skipped_missing | skipped_unreversible."""
    store, action, row_id = lrow["store"], lrow["action"], lrow["row_id"]
    res = {"ledger_id": lrow["id"], "store": store, "verb": lrow["verb"], "action": action,
           "row_id": row_id, "recorded_at": lrow["recorded_at"],
           "summary": (lrow["summary"] or "")[:120]}
    if store not in UNDO_STORES:               # kb_control (never touched) / kb_state
        res.update(mode="skipped_unreversible", reason="store '{}' is not reversible".format(store))
        return res
    if row_id is None:
        res.update(mode="skipped_unreversible", reason="the ledger row names no row_id")
        return res
    cur.execute("SELECT * FROM {} WHERE id=%s".format(store), (row_id,))   # store is allowlisted
    row = cur.fetchone()
    if row is None:
        res.update(mode="skipped_missing", reason="row already gone")
        return res
    res["label"] = str(row.get(STORE_LABEL[store]) or "")[:120]
    if row.get("project"):
        res["project"] = row["project"]

    created = action in UNDO_CREATED
    if store == "flakiness" and action == "upsert":
        # 'upsert' hides which half happened. Both now()s come from the same transaction, so
        # created_at == recorded_at IFF this ledger row is the one that created the row.
        created = row["created_at"] == lrow["recorded_at"]

    if not created:
        if store in BUMP_STORES and action == "bump":
            if apply_it:
                # LOCK THE ROW BEFORE READING THE RECEIPT. Every other mode re-checks itself in
                # the write (`DELETE … RETURNING`, `UPDATE … WHERE status <> 'forgotten'
                # RETURNING`), so a concurrent twin lands 0 rows and reports skipped_missing.
                # `occurrences = occurrences - 1` has no such check — it always succeeds — so
                # the receipt is the ONLY thing standing between a second pass and a lesson that
                # predates the run. Under READ COMMITTED two overlapping undos both read "no
                # receipt yet" (neither has committed), then both decrement: 3 → 1. Taking the
                # row lock first serialises them, and the loser's receipt read runs in a
                # statement snapshot taken AFTER the winner committed, so it sees the receipt.
                cur.execute("SELECT occurrences FROM {} WHERE id=%s FOR UPDATE".format(store),
                            (row_id,))
                locked = cur.fetchone()
                if locked is None:                     # deleted while we waited for the lock
                    res.update(mode="skipped_missing", reason="row already gone")
                    return res
                row = dict(row, occurrences=locked["occurrences"])
            prior = _undo_receipt_for(cur, lrow)
            if prior is not None:
                # Already reversed. Reported as skipped_missing — the same thing a re-run says
                # about a deleted row: there is nothing left owing on this ledger row.
                res.update(mode="skipped_missing", already_undone=True, undo_receipt_id=prior,
                           occurrences=row["occurrences"],
                           reason="this bump was already decremented by undo receipt #{}; "
                                  "decrementing again would take a row that predates the run "
                                  "below its pre-run value".format(prior))
                return res
            if apply_it:
                cur.execute("""UPDATE {} SET occurrences = GREATEST(occurrences - 1, 1),
                                 updated_at = now(), version = version + 1
                               WHERE id=%s RETURNING occurrences""".format(store), (row_id,))
                res["occurrences"] = cur.fetchone()["occurrences"]
            else:
                res["occurrences"] = max(row["occurrences"] - 1, 1)
            res.update(mode="decrement", partial=True,
                       reason=("occurrences decremented; the root_cause/fix_shape/ticket that "
                               "bump COALESCEd in are not recorded in the ledger and remain")
                       if store == "fix_lessons" else
                       "occurrences decremented; last_seen_at is not restored")
            return res
        res.update(mode="skipped_unreversible",
                   reason="'{}' modified a row this run did not create, and the ledger does "
                          "not record its prior value".format(action))
        return res

    if store == "expectations":
        # SOFT ONLY. A human bless decision must never be silently destroyed by a bulk undo —
        # the row (and its blessed_by/blessed_at) survives and can be restored by hand.
        if row["status"] == "forgotten":
            res.update(mode="skipped_missing", reason="already forgotten")
            return res
        if apply_it:
            cur.execute("""UPDATE expectations SET status='forgotten', updated_at=now(),
                             version=version+1
                           WHERE id=%s AND status <> 'forgotten' RETURNING id""", (row_id,))
            if cur.fetchone() is None:
                res.update(mode="skipped_missing", reason="already forgotten")
                return res
        res.update(mode="soft_forget", was_status=row["status"],
                   was_canonical=row["status"] == "canonical")
        return res

    if apply_it:
        cur.execute("DELETE FROM {} WHERE id=%s RETURNING id".format(store), (row_id,))
        if cur.fetchone() is None:
            res.update(mode="skipped_missing", reason="row already gone")
            return res
    res["mode"] = "delete"
    return res

def cmd_undo(cur, a, embed):
    where, params, selector = ["verb <> 'undo'"], [], {}
    # `verb <> 'undo'` above is load-bearing: without it a second pass would select the undo's
    # own reversal rows and start undoing the undo.
    if a.run_id:
        where.append("run_id = %s"); params.append(a.run_id); selector["run_id"] = a.run_id
    if a.source:
        sv = source_variants(a.source)
        where.append("source = ANY(%s)"); params.append(sv); selector["source"] = sv
    if a.ticket:
        where.append("ticket = %s"); params.append(a.ticket); selector["ticket"] = a.ticket
    if not params:
        # Deliberately NO env fallback (unlike provenance tags): a destructive selector must be
        # something you typed, not something your shell happened to export.
        return fail("undo requires an explicit --run-id, --source or --ticket")
    if a.loop_id:
        where.append("loop_id = %s"); params.append(a.loop_id); selector["loop_id"] = a.loop_id
    if a.store:
        where.append("store = %s"); params.append(a.store); selector["store"] = a.store
    if getattr(a, "project", None) and not all_projects(a) and has_project_col(cur, "kb_writes"):
        # an explicit --project narrows the ledger selection (never a selector on its own)
        where.append("project = %s"); params.append(cur_project(a)); selector["project"] = cur_project(a)
    if a.since:
        iv = parse_since(a.since)
        if not iv:
            return fail("bad --since (use e.g. 7d, 24h, 30m)")
        where.append("recorded_at >= now() - interval '{}'".format(iv)); selector["since"] = a.since

    dry_run = bool(a.dry_run) or not bool(a.yes)
    # Newest first: if a run inserted a row and then bumped it, the bump is reversed before the
    # insert is (the delete then subsumes it).
    cur.execute("SELECT * FROM kb_writes WHERE {} ORDER BY id DESC".format(" AND ".join(where)),
                params)
    lrows = cur.fetchall()
    results = [_undo_one(cur, r, not dry_run) for r in lrows]

    by_store, reversible, missing, unreversible = {}, 0, 0, 0
    for r in results:
        b = by_store.setdefault(r["store"], {"planned": 0, "undoable": 0, "undone": 0,
                                             "skipped_missing": 0, "skipped_unreversible": 0})
        b["planned"] += 1
        if r["mode"] in UNDO_APPLIED_MODES:
            b["undoable"] += 1
            reversible += 1
            if not dry_run:
                b["undone"] += 1
        elif r["mode"] == "skipped_missing":
            b["skipped_missing"] += 1; missing += 1
        else:
            b["skipped_unreversible"] += 1; unreversible += 1

    receipts = []
    if not dry_run:
        tags = _UndoTags(a)
        for r in results:
            if r["mode"] in UNDO_APPLIED_MODES:
                receipts.append(ledger(cur, tags, "undo", r["store"], "undo_" + r["mode"],
                                       r["row_id"], "", project=r.get("project"), summary=
                                       "undo {} of {}#{} {}: {}".format(
                                           r["mode"], r["store"], r["row_id"],
                                           _undo_marker(r["ledger_id"]),
                                           r.get("label") or r.get("summary") or "")))
    return ok(action="undo", dry_run=dry_run, selector=selector,
              planned=len(results), undoable=reversible,
              undone=0 if dry_run else reversible,
              skipped_missing=missing, skipped_unreversible=unreversible,
              by_store=by_store, results=results,
              receipt=receipts[0] if receipts else None, receipts=receipts,
              note=("nothing was changed — re-run with --yes to apply" if dry_run else None))

def cmd_decay(cur, a, embed):
    stale_iv = "{} days".format(int(a.stale_days))
    cur.execute("""SELECT id, area, statement, status, confidence, last_confirmed_at
                    FROM expectations
                    WHERE status NOT IN ('canonical','forgotten')
                      AND last_confirmed_at < now() - interval '{}'
                    ORDER BY last_confirmed_at""".format(stale_iv))
    stale = cur.fetchall()
    cur.execute("""SELECT a.id AS id_a, b.id AS id_b, 1-(a.embedding <=> b.embedding) AS similarity
                   FROM expectations a JOIN expectations b ON a.id < b.id
                   WHERE a.status='canonical' AND b.status='canonical'
                     AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
                     AND (1-(a.embedding <=> b.embedding)) >= %s
                   ORDER BY similarity DESC LIMIT 50""", (a.dup_threshold,))
    conflicts = cur.fetchall()
    # app_map staleness is REPORT-ONLY, always. `--apply` must never delete a selector:
    # an unconfirmed selector is un-audited, not wrong, and dropping it costs a real lesson.
    cur.execute("""SELECT area, count(*) AS n, min(last_confirmed_at) AS oldest_confirmed_at,
                          (array_agg(id ORDER BY last_confirmed_at))[1:10] AS sample_ids
                   FROM app_map
                   WHERE status <> 'forgotten'
                     AND last_confirmed_at < now() - interval '{}'
                   GROUP BY area ORDER BY n DESC, area""".format(stale_iv))
    stale_app_map = cur.fetchall()
    stale_app_map_total = sum(r["n"] for r in stale_app_map)
    applied, receipts = None, []
    if a.apply and stale:
        ids = [r["id"] for r in stale]
        cur.execute("UPDATE expectations SET status='forgotten', updated_at=now(), "
                    "version=version+1 WHERE id = ANY(%s)", (ids,))
        applied = {"forgotten": ids}
        receipts = [ledger(cur, a, "decay", "expectations", "decay_forget", i) for i in ids]
    return ok(action="decay", dry_run=not a.apply, stale_count=len(stale),
              receipt=receipts[0] if receipts else None, receipts=receipts,
              stale=stale, canonical_conflicts=conflicts, applied=applied,
              stale_app_map=stale_app_map, stale_app_map_total=stale_app_map_total,
              stale_app_map_apply="never — app_map staleness is report-only")

def cmd_pause(cur, a, embed):
    # Kill-switch semantics are FAIL CLOSED: `paused` is true/false only when
    # the flag row was actually read/written; a missing row reports paused=null + warning,
    # and callers may ship ONLY on an explicit paused=false.
    if a.status or (not a.on and not a.off):
        cur.execute("SELECT * FROM kb_control WHERE flag='global_pause'")
        row = cur.fetchone()
        if row is None:
            return ok(action="status", paused=None, control=None,
                      warning="global_pause row missing — FAIL CLOSED: treat as paused; run schema.sql to seed it")
        return ok(action="status", paused=row["value"] is True, control=row)
    if a.on and a.off:
        return fail("pause requires one of --on / --off / --status")
    val = bool(a.on)
    # Upsert: `pause --on` must never silently no-op because the seed row is absent.
    cur.execute("""INSERT INTO kb_control(flag, value, reason, set_by, updated_at)
                   VALUES('global_pause', %s, %s, %s, now())
                   ON CONFLICT (flag) DO UPDATE
                     SET value=EXCLUDED.value, reason=EXCLUDED.reason,
                         set_by=EXCLUDED.set_by, updated_at=now()
                   RETURNING *""", (val, a.reason, a.by))
    control = cur.fetchone()
    rec = ledger(cur, a, "pause", "kb_control", "pause_on" if val else "pause_off",
                 None, "", "global_pause={}".format(val))
    return ok(action="pause_set", paused=val, control=control, receipt=rec)

def cmd_flag(cur, a, embed):
    # Generic kb_control accessor — the checkable home for posture flags like 'staging_only'.
    # Same FAIL-CLOSED semantics as pause: only an explicit value authorizes the
    # permissive direction; a missing row / error means the safe direction.
    if a.status or (not a.on and not a.off):
        cur.execute("SELECT * FROM kb_control WHERE flag=%s", (a.name,))
        row = cur.fetchone()
        if row is None:
            return ok(action="flag_status", flag=a.name, value=None, control=None,
                      warning="{} row missing — FAIL CLOSED: treat as the safe direction; "
                              "seed via schema.sql or `kb flag --name {} --on/--off`".format(a.name, a.name))
        return ok(action="flag_status", flag=a.name, value=row["value"] is True, control=row)
    if a.on and a.off:
        return fail("flag requires one of --on / --off / --status")
    val = bool(a.on)
    cur.execute("""INSERT INTO kb_control(flag, value, reason, set_by, updated_at)
                   VALUES(%s, %s, %s, %s, now())
                   ON CONFLICT (flag) DO UPDATE
                     SET value=EXCLUDED.value, reason=EXCLUDED.reason,
                         set_by=EXCLUDED.set_by, updated_at=now()
                   RETURNING *""", (a.name, val, a.reason, a.by))
    control = cur.fetchone()
    rec = ledger(cur, a, "flag", "kb_control", "flag_set", None, "",
                 "{}={}".format(a.name, val))
    return ok(action="flag_set", flag=a.name, value=val, control=control, receipt=rec)

def cmd_metric(cur, a, embed):
    try:
        meta = json.loads(a.metadata) if a.metadata else {}
    except ValueError as e:
        return fail("bad --metadata JSON: {}".format(e))
    area = norm_area(a.area)
    # metrics are loop health, read globally by default — but stamped with the project so
    # `kb metrics --project X` can split them.
    stamp = has_project_col(cur, "metrics")
    cur.execute("""INSERT INTO metrics(metric, value, unit, area, loop_id, ticket, metadata{})
                   VALUES(%s,%s,%s,%s,%s,%s,%s{}) RETURNING *""".format(
                    ", project" if stamp else "", ", %s" if stamp else ""),
                (a.name, a.value, a.unit, area, a.loop_id,
                 _tag(a, "ticket"), Json(meta)) + ((cur_project(a),) if stamp else ()))
    row = cur.fetchone()
    rec = ledger(cur, a, "metric", "metrics", "insert", row["id"], area,
                 "{}={} {}".format(a.name, a.value, a.unit))
    return ok(action="metric", row=row, receipt=rec)

def cmd_metrics(cur, a, embed):
    where, params = [], []
    if a.metric:
        where.append("metric=%s"); params.append(a.metric)
    if a.loop_id:
        where.append("loop_id=%s"); params.append(a.loop_id)
    if a.ticket:
        where.append("ticket=%s"); params.append(a.ticket)
    if a.area:
        av = area_variants(a.area)
        where.append(norm_pred("area")); params += [av, av[0]]
    if getattr(a, "project", None) and not all_projects(a) and has_project_col(cur, "metrics"):
        where.append("project = %s"); params.append(cur_project(a))   # explicit only
    if a.since:
        iv = parse_since(a.since)
        if not iv:
            return fail("bad --since (use e.g. 7d, 24h, 30m)")
        where.append("recorded_at >= now() - interval '{}'".format(iv))
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    cur.execute("""SELECT metric, count(*) AS n, sum(value) AS sum, avg(value) AS avg,
                           min(value) AS min, max(value) AS max,
                           min(recorded_at) AS first_at, max(recorded_at) AS last_at
                    FROM metrics {} GROUP BY metric ORDER BY metric""".format(wsql), params)
    return ok(action="metrics_summary", summary=cur.fetchall())

def cmd_receipt(cur, a, embed):
    """What a run ACTUALLY wrote — read from the ledger, not from an agent's recollection."""
    where, params = ["ticket=%s"], [a.ticket]
    if a.loop_id:
        where.append("loop_id=%s"); params.append(a.loop_id)
    if a.run_id:
        where.append("run_id=%s"); params.append(a.run_id)
    if a.since:
        iv = parse_since(a.since)
        if not iv:
            return fail("bad --since (use e.g. 7d, 24h, 30m)")
        where.append("recorded_at >= now() - interval '{}'".format(iv))
    cur.execute("SELECT * FROM kb_writes WHERE {} ORDER BY recorded_at, id".format(" AND ".join(where)),
                params)
    rows = [dict(r) for r in cur.fetchall()]
    by_store = {}
    for r in rows:
        by_store.setdefault(r["store"], []).append(r)
    return ok(action="receipt", ticket=a.ticket, count=len(rows),
              counts_by_store=dict((k, len(v)) for k, v in by_store.items()),
              writes_by_store=by_store,
              sources=sorted(set(r["source"] for r in rows if r["source"])),
              run_ids=sorted(set(r["run_id"] for r in rows if r["run_id"])),
              loop_ids=sorted(set(r["loop_id"] for r in rows if r["loop_id"])),
              first_at=rows[0]["recorded_at"] if rows else None,
              last_at=rows[-1]["recorded_at"] if rows else None)

def cmd_history(cur, a, embed):
    """Prior-attempt memory so the router is NOT stateless on a ticket already tried.
    Safe on a cold ticket: empty structures, ok:true, never an error."""
    t = a.ticket
    cur.execute("""SELECT id, metric, value, unit, area, loop_id, metadata, recorded_at
                   FROM metrics WHERE ticket=%s ORDER BY recorded_at, id""", (t,))
    attempts = [dict(r) for r in cur.fetchall()]

    cur.execute("""SELECT store, count(*) AS n, max(recorded_at) AS last_at
                   FROM kb_writes WHERE ticket=%s GROUP BY store ORDER BY store""", (t,))
    writes_by_store = dict((r["store"], {"count": r["n"], "last_at": r["last_at"]})
                           for r in cur.fetchall())

    # escalations: what dev-escalate records (`kb metric --name escalation --value 1`).
    cur.execute("SELECT count(*) AS n FROM metrics WHERE ticket=%s AND metric ILIKE %s",
                (t, "%escalat%"))
    escalations = cur.fetchone()["n"]
    cur.execute("SELECT count(*) AS n FROM kb_writes WHERE ticket=%s AND source ILIKE %s",
                (t, "dev-escalate%"))
    escalation_writes = cur.fetchone()["n"]

    # last_outcome: the most recent metric that DECLARES an outcome (metadata.outcome, or a
    # metric literally named loop_outcome/outcome). Anything else would be a guess.
    cur.execute("""SELECT metric, value, unit, metadata, recorded_at FROM metrics
                   WHERE ticket=%s AND (metadata ? 'outcome' OR metric IN ('loop_outcome','outcome'))
                   ORDER BY recorded_at DESC, id DESC LIMIT 1""", (t,))
    lo = cur.fetchone()
    last_outcome = None
    if lo:
        meta = lo["metadata"] or {}
        last_outcome = {"outcome": meta.get("outcome") or lo["metric"],
                        "metric": lo["metric"], "value": lo["value"],
                        "recorded_at": lo["recorded_at"]}
    elif escalations:
        last_outcome = {"outcome": "escalated", "metric": "escalation", "value": escalations,
                        "recorded_at": None}

    cur.execute("""SELECT DISTINCT area FROM (
                     SELECT area FROM kb_writes WHERE ticket=%s
                     UNION ALL SELECT area FROM metrics WHERE ticket=%s
                   ) x WHERE area <> '' ORDER BY area""", (t, t))
    areas = [r["area"] for r in cur.fetchall()]

    cur.execute("""SELECT max(ts) AS last_activity_at FROM (
                     SELECT max(recorded_at) AS ts FROM kb_writes WHERE ticket=%s
                     UNION ALL SELECT max(recorded_at) FROM metrics WHERE ticket=%s
                   ) x""", (t, t))
    last_activity_at = cur.fetchone()["last_activity_at"]

    return ok(action="history", ticket=t, attempts=attempts, attempt_count=len(attempts),
              writes_by_store=writes_by_store, escalations=escalations,
              escalation_writes=escalation_writes, last_outcome=last_outcome,
              areas=areas, last_activity_at=last_activity_at,
              cold=not (attempts or writes_by_store))

def cmd_state(cur, a, embed):
    """Durable key/value shared across machines (the dream watermark lives here)."""
    if a.delete:
        cur.execute("DELETE FROM kb_state WHERE key=%s RETURNING key", (a.key,))
        row = cur.fetchone()
        rec = ledger(cur, a, "state", "kb_state", "delete", None, "", a.key)
        return ok(action="state_delete", key=a.key, deleted=bool(row), receipt=rec)
    if a.set_value is not None:
        warning = None
        try:
            val = json.loads(a.set_value)
        except ValueError:
            # Never drop a write over a quoting mistake — store it as a JSON string and say so.
            val = a.set_value
            warning = "value_not_json_stored_as_string"
        cur.execute("""INSERT INTO kb_state(key, value, updated_at) VALUES(%s,%s,now())
                       ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()
                       RETURNING *""", (a.key, Json(val)))
        row = cur.fetchone()
        rec = ledger(cur, a, "state", "kb_state", "set", None, "", a.key)
        return ok(action="state_set", key=a.key, row=dict(row), receipt=rec, warning=warning)
    cur.execute("SELECT * FROM kb_state WHERE key=%s", (a.key,))
    row = cur.fetchone()
    if row is None:
        return ok(action="state_get", key=a.key, found=False, value=None)
    return ok(action="state_get", key=a.key, found=True, value=row["value"], row=dict(row))

# ── lessons: general, non-defect knowledge (store 5) ─────────────────────────
# "port 8000 is taken on this machine, run the API on 8010", "the user prefers multiple-choice
# questions" — durable working knowledge that is not a defect fix (fix_lessons), not an oracle
# (expectations) and not a selector (app_map). AI writes them `proposed` by default; `bless`
# makes one `active`, and only active lessons come back from `search`. Same durability and
# dedupe shape as fix_lessons: an exact repeat bumps by hash with no embedder, a near-duplicate
# (cosine >= 0.92 by default, same project) bumps by vector, an embedder outage stores the row
# as embed_status='pending' for `kb reembed` to heal.

LESSON_DUP_THRESHOLD = 0.92
LESSON_STATUSES = ("proposed", "active", "rejected")
LESSON_WRITE_STATUSES = ("proposed", "active")

_LESSON_BUMP_SQL = """UPDATE lessons SET occurrences = occurrences + 1, last_seen_at = now(),
                        updated_at = now(), version = version + 1,
                        ticket = COALESCE(%s, ticket)
                      WHERE id=%s RETURNING *"""


def _table_exists(cur, table):
    cur.execute("SELECT to_regclass(%s) AS t", (table,))
    return cur.fetchone()["t"] is not None


def _lessons_missing():
    return fail("schema_outdated: the lessons store does not exist on this brain — "
                "re-apply the schema (kb schema --apply)", queued=False)


def bump_lesson(cur, a, row_id):
    cur.execute(_LESSON_BUMP_SQL, (_tag(a, "ticket"), row_id))
    return cur.fetchone()


def write_lesson(cur, a, embed):
    if not a.text:
        return fail("lesson write requires --text")
    status = a.status or "proposed"
    if status not in LESSON_WRITE_STATUSES:
        return fail("lesson write --status must be one of: {} (reject with `kb reject lesson`)"
                    .format(", ".join(LESSON_WRITE_STATUSES)))
    if not _table_exists(cur, "lessons"):
        return _lessons_missing()
    try:
        meta = json.loads(a.metadata) if a.metadata else {}
    except ValueError as e:
        return fail("bad --metadata JSON: {}".format(e))
    kind = norm_kind(a.kind) or "general"
    proj = cur_project(a)
    threshold = a.dup_threshold if a.dup_threshold is not None else LESSON_DUP_THRESHOLD
    thash = text_hash(a.text)

    # GUARD 1: exact duplicate in this project, by hash — before any embedding attempt.
    cur.execute("SELECT * FROM lessons WHERE text_hash = %s AND project = %s ORDER BY id LIMIT 1",
                (thash, proj))
    hit = cur.fetchone()
    if hit is not None:
        row = bump_lesson(cur, a, hit["id"])
        rec = ledger(cur, a, "write", "lessons", "bump", row["id"], "", a.text, project=proj)
        return ok(action="bump", similarity=1.0, row=sanitize(row), receipt=rec,
                  dedup_by="hash", dedup_skipped=True, existing_status=row["status"],
                  embed_status=row.get("embed_status"), warning=None, warnings=[])

    vec, emb_err = try_embed(embed, a.text)
    if vec is None:
        # DURABILITY: the lesson lands without a vector; `kb reembed` makes it searchable.
        warning, all_w = warnings_of("embedder_unavailable: {}".format(emb_err))
        cur.execute("""INSERT INTO lessons (project, kind, text, text_hash, embedding,
                                            embedding_model, embedding_dim, embed_status,
                                            status, source, ticket, metadata)
                       VALUES (%s,%s,%s,%s,NULL,'',%s,'pending',%s,%s,%s,%s) RETURNING *""",
                    (proj, kind, a.text, thash, EMB_DIM, status, a.source, _tag(a, "ticket"),
                     Json(meta)))
        row = cur.fetchone()
        rec = ledger(cur, a, "write", "lessons", "insert_pending", row["id"], "", a.text, project=proj)
        return ok(action="insert", row=sanitize(row), receipt=rec, embed_status="pending",
                  dedup_by=None, dedup_skipped=True, warning=warning, warnings=all_w)

    ev = vlit(vec)
    # GUARD 2: near-duplicate in THIS project → bump (a bump edits the row; another project's
    # lesson, or a 'global' one, is not this project's to edit).
    cur.execute("""SELECT id, 1-(embedding <=> %s::vector) AS similarity FROM lessons
                   WHERE embedding IS NOT NULL AND project = %s
                   ORDER BY embedding <=> %s::vector LIMIT 1""", (ev, proj, ev))
    near = cur.fetchone()
    if near and near["similarity"] >= threshold:
        row = bump_lesson(cur, a, near["id"])
        if row.get("text_hash") is None:
            cur.execute("UPDATE lessons SET text_hash=%s WHERE id=%s RETURNING *",
                        (text_hash(row["text"]), row["id"]))
            row = cur.fetchone() or row
        rec = ledger(cur, a, "write", "lessons", "bump", row["id"], "", a.text, project=proj)
        return ok(action="bump", similarity=near["similarity"], row=sanitize(row), receipt=rec,
                  dedup_by="vector", dedup_skipped=False, existing_status=row["status"],
                  embed_status="ok", warning=None, warnings=[])
    cur.execute("""INSERT INTO lessons (project, kind, text, text_hash, embedding,
                                        embedding_model, embedding_dim, embed_status,
                                        status, source, ticket, metadata)
                   VALUES (%s,%s,%s,%s,%s::vector,%s,%s,'ok',%s,%s,%s,%s) RETURNING *""",
                (proj, kind, a.text, thash, ev, embed.model, EMB_DIM, status, a.source,
                 _tag(a, "ticket"), Json(meta)))
    row = cur.fetchone()
    rec = ledger(cur, a, "write", "lessons", "insert", row["id"], "", a.text, project=proj)
    return ok(action="insert", row=sanitize(row), receipt=rec, embed_status="ok",
              dedup_by=None, dedup_skipped=False, warning=None, warnings=[])


def _status_list(raw, allowed):
    vals = [v.strip() for v in (raw or "").split(",") if v.strip()]
    bad = [v for v in vals if v not in allowed]
    return vals, bad


def read_lessons(cur, a):
    """Non-vector read of the lessons store — the review queue (`--status proposed`) and the
    degraded path while the embedder is down. Scoped like every read: current + 'global'."""
    if not _table_exists(cur, "lessons"):
        return _lessons_missing()
    if a.id:
        cur.execute("SELECT * FROM lessons WHERE id=%s", (a.id,))
        rows = [sanitize(r) for r in cur.fetchall()]
        return ok(count=len(rows), rows=rows, projects={"filter": "id"})
    statuses, bad = _status_list(a.status, LESSON_STATUSES)
    if bad:
        return fail("bad --status {} — one of: {}".format(",".join(bad), ", ".join(LESSON_STATUSES)))
    pw, pp = proj_where(cur, a, "lessons")
    po, pop = proj_order(cur, a, "lessons")
    where, params = [pw], list(pp)
    if statuses:
        where.append("status = ANY(%s)"); params.append(statuses)
    if a.kind:
        kv = kind_variants(a.kind)
        where.append(norm_pred("kind")); params += [kv, kv[0]]
    cur.execute("SELECT * FROM lessons WHERE {} ORDER BY {}status, occurrences DESC, id".format(
                    " AND ".join(where), po), tuple(params + pop))
    rows = [sanitize(r) for r in cur.fetchall()]
    return ok(count=len(rows), rows=rows, projects=proj_report(cur, a, "lessons"))


def search_lessons(cur, a, embed):
    """Semantic recall over ACTIVE lessons only (proposed ones wait for a bless; rejected ones
    never come back). Every result carries `similarity` (cosine, 1 = identical) and the same
    value as `score`."""
    if not a.query:
        return fail("search lessons requires a query (positional or --query)")
    if not _table_exists(cur, "lessons"):
        return _lessons_missing()
    k = a.k if a.k is not None else 5
    vec, emb_err = try_embed(embed, a.query)
    if vec is None:
        return fail("embedder_unavailable: {}".format(emb_err),
                    degraded_path="kb read lessons --status active (non-vector)")
    ev = vlit(vec)
    pw, pp = proj_where(cur, a, "lessons")
    sql = ["SELECT id, project, kind, text, status, occurrences, source, embed_status,",
           "last_seen_at, 1-(embedding <=> %s::vector) AS similarity FROM lessons",
           "WHERE embedding IS NOT NULL AND status = 'active' AND", pw]
    params = [ev] + pp
    if a.kind:
        kv = kind_variants(a.kind)
        sql.append("AND " + norm_pred("kind")); params += [kv, kv[0]]
    sql.append("ORDER BY embedding <=> %s::vector LIMIT %s"); params += [ev, k]
    cur.execute(" ".join(sql), params)
    rows = [dict(r) for r in cur.fetchall()]
    if a.min_similarity is not None:
        rows = [r for r in rows if r["similarity"] >= a.min_similarity]
    for r in rows:
        r["score"] = r["similarity"]
    return ok(count=len(rows), results=rows, projects=proj_report(cur, a, "lessons"))


def set_lesson_status(cur, a, target):
    """bless: proposed → active · reject: proposed → rejected. Optimistic concurrency via
    --expect-version, exactly like `bless expectation`."""
    if not _table_exists(cur, "lessons"):
        return _lessons_missing()
    by = a.by or a.source or ""
    vsql, vparams = "", []
    if getattr(a, "expect_version", None) is not None:
        vsql, vparams = " AND version=%s", [a.expect_version]
    if target == "active":
        cur.execute("""UPDATE lessons SET status='active', blessed_by=%s, blessed_at=now(),
                         updated_at=now(), version=version+1
                       WHERE id=%s AND status='proposed'{} RETURNING *""".format(vsql),
                    tuple([by or None, a.id] + vparams))
        verb = "bless"
    else:
        cur.execute("""UPDATE lessons SET status='rejected', updated_at=now(), version=version+1,
                         metadata = metadata || %s
                       WHERE id=%s AND status='proposed'{} RETURNING *""".format(vsql),
                    tuple([Json({"rejected_by": by, "reject_reason": getattr(a, "reason", "")}),
                           a.id] + vparams))
        verb = "reject"
    row = cur.fetchone()
    if row is None:
        return fail("{}_failed".format(verb),
                    detail="not found, not in 'proposed' state, or version conflict", id=a.id)
    rec = ledger(cur, a, verb, "lessons", verb, row["id"], "", row["text"], project=row["project"])
    return ok(action=verb, row=sanitize(row), receipt=rec)


# ── status: which brain, which scope, which project — and is the schema there ─

KNOWLEDGE_STORES = ("app_map", "expectations", "flakiness", "fix_lessons", "lessons")
OPS_TABLES = ("kb_control", "metrics", "kb_writes", "kb_state")


def cmd_status(a):
    """Never queues, never writes. Reports even when the brain is unreachable (ok:false then),
    so `kb status` is the first thing to run when a write lands somewhere unexpected."""
    info = dict(action="status", scope=brain_scope(), project=cur_project(a),
                project_source=getattr(a, "_project_how", PROJECT_HOW),
                config=CONFIG_PATH, config_source=CONFIG_SOURCE,
                embedding={"provider": CONFIG["embedding"].get("provider"),
                           "model": EMB_MODEL, "dim": EMB_DIM},
                allow_unshared_env=os.environ.get("KB_ALLOW_UNSHARED") == "1")
    try:
        dsn = resolve_dsn(a.db)
        validate_auth_config()
    except BrainConfigError as e:
        return fail(str(e), queued=False, **info)
    info["target"] = _target_label(dsn)
    info["brain"] = info["target"]
    info["local_host"] = _is_unshared_host(_dsn_host(dsn))
    try:
        conn = connect(dsn)
    except Exception as e:                                    # noqa: BLE001
        return fail("{}: {}".format(type(e).__name__, str(e).strip()), reachable=False, **info)
    try:
        with conn.cursor() as cur:
            tables = dict((t, _table_exists(cur, t)) for t in KNOWLEDGE_STORES + OPS_TABLES)
            counts, visible, by_project = {}, {}, {}
            for t in KNOWLEDGE_STORES:
                if not tables[t]:
                    continue
                cur.execute("SELECT count(*) AS n FROM {}".format(t))
                counts[t] = cur.fetchone()["n"]
                if has_project_col(cur, t):
                    cur.execute("SELECT project, count(*) AS n FROM {} GROUP BY project "
                                "ORDER BY project".format(t))
                    by_project[t] = dict((r["project"], r["n"]) for r in cur.fetchall())
                    visible[t] = sum(n for p, n in by_project[t].items()
                                     if p in visible_projects(a))
            schema_dim = None
            if tables["fix_lessons"]:
                cur.execute("""SELECT format_type(atttypid, atttypmod) AS t FROM pg_attribute
                               WHERE attrelid = to_regclass('fix_lessons')
                                 AND attname = 'embedding'""")
                r = cur.fetchone()
                m = re.search(r"\((\d+)\)", (r or {}).get("t") or "")
                schema_dim = int(m.group(1)) if m else None
            core = ("app_map", "expectations", "flakiness", "fix_lessons") + OPS_TABLES
            schema = {
                "present": all(tables[t] for t in core),
                "current": all(tables[t] for t in core) and tables["lessons"]
                           and all(has_project_col(cur, t) for t in
                                   ("app_map", "expectations", "flakiness", "fix_lessons",
                                    "kb_writes")),
                "tables": tables,
                "embedding_dim": schema_dim,
            }
        conn.rollback()
    finally:
        conn.close()
    info["embedding"]["schema_dim"] = schema_dim
    warning = None
    if not schema["present"]:
        warning = "schema_missing: run `kb schema --apply` (admin action)"
    elif not schema["current"]:
        warning = "schema_outdated: run `kb schema --apply` to add projects + lessons (idempotent)"
    elif schema_dim is not None and schema_dim != EMB_DIM:
        warning = "embedding_dim_mismatch: config says {}, brain columns are {}".format(EMB_DIM, schema_dim)
    return ok(reachable=True, schema=schema, row_counts=counts,
              row_counts_visible=visible, row_counts_by_project=by_project,
              warning=warning, **info)


HANDLERS = {
    "write": cmd_write, "read": cmd_read, "propose": cmd_propose, "search": cmd_search,
    "bless": cmd_bless, "reject": cmd_reject, "quarantine": cmd_quarantine,
    "forget": cmd_forget, "undo": cmd_undo, "decay": cmd_decay, "pause": cmd_pause,
    "flag": cmd_flag, "metric": cmd_metric, "metrics": cmd_metrics, "confirm": cmd_confirm,
    "reembed": cmd_reembed, "receipt": cmd_receipt, "history": cmd_history, "state": cmd_state,
}

# ── the outbox (transport failure) ───────────────────────────────────────────
# ONLY a connection-level failure on a MUTATING verb queues. A version_conflict, a bad
# argument, or a missing --key is a real disagreement with reality: it must fail loudly
# and must NEVER be queued — replaying a semantically broken command would loop forever.

MUTATING_VERBS = {"write", "propose", "metric", "quarantine", "confirm"}
_NO_QUEUE = False   # set while draining, so a replay can never re-queue into itself

def _queue(argv, args, err):
    """Append a mutating verb to the outbox. Its project is pinned into the queued argv, so a
    replay run from another directory (another project) still stamps the original one."""
    qargv = list(argv)
    if (args.verb in PROJECT_STAMPED_VERBS and "--project" not in qargv
            and not any(t.startswith("--project=") for t in qargv)):
        qargv += ["--project", cur_project(args)]
    try:
        path = outbox_append(qargv, err, args.verb)
        return fail(err, queued=True, outbox=path)
    except Exception as qe:  # noqa: BLE001 — say so loudly, never pretend it queued
        return fail(err, queued=False, outbox_error="{}: {}".format(type(qe).__name__, qe))

def is_mutating(args):
    if args.verb in MUTATING_VERBS:
        return True
    # `state --set` queues; `state --delete` deliberately does NOT — a delete replayed
    # after the key was legitimately re-set would destroy the newer value.
    if args.verb == "state" and getattr(args, "set_value", None) is not None:
        return True
    return False

def outbox_path():
    return os.environ.get("KB_OUTBOX") or os.path.join(BRAIN_DIR, "outbox.jsonl")

def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def outbox_append(argv, error, verb=None):
    path = outbox_path()
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    entry = {"ts": _now_iso(), "verb": verb or (argv[0] if argv else None), "argv": list(argv),
             "error": error, "cwd": os.getcwd(),
             "env": dict((k, os.environ[k]) for k in TAG_ENV.values() if os.environ.get(k))}
    with open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return path

def _outbox_load(path):
    """[(entry_dict, raw_line)] — unparseable lines survive as {} so nothing is silently lost."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        for line in fh:
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            try:
                out.append((json.loads(raw), raw))
            except ValueError:
                out.append(({}, raw))
    return out

def _outbox_rewrite(path, raw_lines):
    if not raw_lines:
        if os.path.exists(path):
            os.remove(path)
        return
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".outbox-", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        for raw in raw_lines:
            fh.write(raw + "\n")
    os.replace(tmp, path)

def _retarget_dsn(argv, db):
    """Strip the recorded `--db` from a queued argv and point the replay at TODAY's brain.

    The queued DSN is, by construction, the address that was UNREACHABLE when the write was
    made. Replaying it verbatim can never succeed: the entry fails forever, `remaining` never
    reaches 0, and the lesson is lost the moment anyone runs `outbox --clear`. `--replay` is an
    assertion that the brain is reachable NOW, from this process's configuration — so the
    replay invocation's own `--db` wins, and with none the config/env DSN is used.
    Returns (argv, stripped_dsn|None)."""
    out, stripped, i = [], None, 0
    while i < len(argv):
        t = argv[i]
        if t == "--db":
            stripped = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if t.startswith("--db="):
            stripped = t[len("--db="):]
            i += 1
            continue
        out.append(t)
        i += 1
    if db:
        out = ["--db", db] + out
    return out, stripped

def _last_json(text):
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {}

def cmd_outbox(a):
    """Runs WITHOUT a DB connection — it is the thing you reach for when the DB is down."""
    global _NO_QUEUE
    path = outbox_path()
    entries = _outbox_load(path)

    if a.clear:
        if not a.yes:
            return fail("outbox --clear discards queued writes — pass --yes to confirm",
                        outbox=path, count=len(entries))
        _outbox_rewrite(path, [])
        return ok(action="outbox_clear", outbox=path, discarded=len(entries))

    if a.replay:
        limit = a.limit if a.limit is not None else len(entries)
        results, remaining, attempted = [], [], 0
        _NO_QUEUE = True
        try:
            for entry, raw in entries:
                argv = entry.get("argv") or []
                if attempted >= limit:
                    remaining.append(raw)
                    continue
                attempted += 1
                if not argv:
                    results.append({"ts": entry.get("ts"), "argv": argv, "ok": False, "rc": 2,
                                    "dropped": True,
                                    "response": {"ok": False, "error": "unreplayable entry (no argv)"}})
                    continue
                argv, retargeted_from = _retarget_dsn(argv, a.db)
                saved = {}
                for k, v in (entry.get("env") or {}).items():
                    saved[k] = os.environ.get(k)
                    os.environ[k] = v
                buf = io.StringIO()
                try:
                    with contextlib.redirect_stdout(buf):
                        rc = main(argv)
                except SystemExit as e:      # argparse rejects a malformed queued argv
                    rc = int(e.code or 0)
                except Exception as e:       # noqa: BLE001
                    rc = 1
                    buf.write(json.dumps({"ok": False,
                                          "error": "{}: {}".format(type(e).__name__, e)}))
                finally:
                    for k, v in saved.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                resp = _last_json(buf.getvalue())
                succeeded = (rc == 0 and resp.get("ok") is True)
                res = {"ts": entry.get("ts"), "argv": argv, "ok": succeeded,
                       "rc": rc, "response": resp}
                if retargeted_from is not None:
                    res["retargeted_from"] = retargeted_from
                results.append(res)
                if not succeeded:
                    remaining.append(raw)
        finally:
            _NO_QUEUE = False
        _outbox_rewrite(path, remaining)
        replayed = sum(1 for r in results if r["ok"])
        return ok(action="outbox_replay", outbox=path, attempted=attempted, replayed=replayed,
                  failed=attempted - replayed, remaining=len(remaining), results=results)

    verbs = {}
    for entry, _ in entries:
        verbs[entry.get("verb") or "?"] = verbs.get(entry.get("verb") or "?", 0) + 1
    tss = [e.get("ts") for e, _ in entries if e.get("ts")]
    return ok(action="outbox_status", outbox=path, count=len(entries),
              oldest=min(tss) if tss else None, newest=max(tss) if tss else None,
              verbs=verbs)

# ── the schema (rendered for the configured embedding dimension) ─────────────
# pgvector columns are declared with a fixed dimension, so the schema is a TEMPLATE
# (scripts/schema.sql.in) rendered for `embedding.dim` / `embedding.model`. scripts/schema.sql
# is the rendered default (1536). Applying it is an ADMIN action, once per brain, through the
# configured auth mode — no psql, no vendor CLI needed. Idempotent (safe to re-run).

_SCRIPTS_DIR = os.path.dirname(os.path.realpath(__file__))
SCHEMA_TEMPLATE = os.path.join(_SCRIPTS_DIR, "schema.sql.in")
SCHEMA_DEFAULT = os.path.join(_SCRIPTS_DIR, "schema.sql")

def render_schema(dim, model):
    with open(SCHEMA_TEMPLATE) as fh:
        tpl = fh.read()
    return (tpl.replace("@EMBEDDING_DIM@", str(int(dim)))
               .replace("@EMBEDDING_MODEL@", (model or "").replace("'", "''")))

def cmd_schema(a):
    """`kb schema` never needs a connection unless --apply. Exit codes as every other verb."""
    dim = a.dim or EMB_DIM
    model = a.model or EMB_MODEL
    if int(dim) <= 0:
        return fail("bad --dim {!r}: must be a positive integer".format(a.dim))
    sql = render_schema(dim, model)
    base = dict(action="schema_render", dim=int(dim), embedding_model=model, template=SCHEMA_TEMPLATE)
    if a.raw:
        sys.stdout.write(sql)       # the one non-JSON output: for `kb schema --raw | psql …`
        return 0
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(sql)
        return ok(path=a.out, bytes=len(sql), **base)
    if not a.apply:
        return ok(sql=sql, **base)
    try:
        dsn = resolve_dsn(a.db)
        validate_auth_config()
    except BrainConfigError as e:
        return fail(str(e), queued=False)
    conn = None
    try:
        conn = connect(dsn)
        with conn.cursor() as cur:
            # The schema is also the MIGRATION (projects + lessons are added in place), so it is
            # applied to brains that already hold vectors. A new vector store at a different
            # dimension than the existing ones would split the brain — refuse instead.
            cur.execute("""SELECT format_type(atttypid, atttypmod) AS t FROM pg_attribute
                           WHERE attrelid = to_regclass('fix_lessons') AND attname = 'embedding'""")
            r = cur.fetchone()
            m = re.search(r"\((\d+)\)", (r or {}).get("t") or "")
            if m and int(m.group(1)) != int(dim):
                conn.rollback()
                return fail("embedding_dim_mismatch: this brain's vectors are {} dims but the schema "
                            "was rendered for {} — pass --dim {} (or re-embed the brain first)".format(
                                m.group(1), int(dim), m.group(1)), queued=False)
            cur.execute(sql)
            cur.execute("""INSERT INTO kb_writes(verb, store, action, row_id, source, summary)
                           VALUES('schema', 'schema', 'apply', NULL, %s, %s) RETURNING id""",
                        (a.source or "kb-schema", "dim={} model={}".format(int(dim), model)[:200]))
            rid = cur.fetchone()["id"]
        conn.commit()
        return ok(action="schema_apply", dim=int(dim), embedding_model=model,
                  brain=_target_label(dsn), scope=brain_scope(),
                  receipt={"id": rid, "store": "schema", "row_id": None})
    except Exception as e:                                    # noqa: BLE001
        if conn:
            conn.rollback()
        return fail("{}: {}".format(type(e).__name__, e), queued=False)
    finally:
        if conn:
            conn.close()

# ── arg parsing ──────────────────────────────────────────────────────────────

def _tag_args(p):
    """Provenance tags every mutating verb accepts. Absent flags fall back to
    LOOP_TICKET / LOOP_ID / LOOP_RUN_ID (TAG_ENV) so a runner tags writes for free."""
    p.add_argument("--ticket")
    p.add_argument("--loop-id")
    p.add_argument("--run-id")

PROJECT_HELP = ("project scope (default: $AGENT_KIT_PROJECT, else the cwd's git repo name, else "
                "'global'). Reads see <project> + 'global'; 'all' = every project (reads only)")

def _project_arg(p):
    p.add_argument("--project", help=PROJECT_HELP)

def build_parser():
    p = argparse.ArgumentParser(prog="kb", description="the loop knowledge brain interface")
    p.add_argument("--db", help="override DSN (else config/env)")
    p.add_argument("--project", dest="project_global", help=PROJECT_HELP)
    p.add_argument("--embedder", choices=sorted(EMBEDDERS),
                   help="force embedder (default: kb.config embedding.provider; a missing key in "
                        "the env var embedding.api_key_env names fails clearly — stub is TEST-ONLY, "
                        "NEVER auto-selected, and stamps stub provenance)")
    sub = p.add_subparsers(dest="verb", required=True)

    w = sub.add_parser("write"); w.add_argument("store", choices=["app_map", "flakiness", "fix_lesson", "fix_lessons", "lesson", "lessons"])
    w.add_argument("--kind"); w.add_argument("--area", default=""); w.add_argument("--key")
    w.add_argument("--value"); w.add_argument("--source", required=True)
    w.add_argument("--confidence", type=float, default=1.0); w.add_argument("--expect-version", type=int)
    w.add_argument("--spec"); w.add_argument("--signature", default=""); w.add_argument("--failed", action="store_true")
    w.add_argument("--symptom"); w.add_argument("--root-cause", default=""); w.add_argument("--fix-shape", default="")
    w.add_argument("--signature-tag", default=""); w.add_argument("--metadata")
    w.add_argument("--dup-threshold", type=float, default=None,
                   help="near-duplicate bump threshold (default 0.45 fix_lessons — calibrated: "
                        "same-lesson paraphrases ~0.57, distinct ~0.26 — and 0.92 lessons)")
    w.add_argument("--text", help="lesson text (write lesson)")
    w.add_argument("--status", choices=list(LESSON_WRITE_STATUSES),
                   help="lesson status on insert (default proposed; bless makes it active)")
    _tag_args(w); _project_arg(w)

    r = sub.add_parser("read"); r.add_argument("store", choices=["app_map", "flakiness", "expectations", "fix_lessons", "fix_lesson", "lessons", "lesson"])
    r.add_argument("--kind"); r.add_argument("--area"); r.add_argument("--key")
    r.add_argument("--spec"); r.add_argument("--id", type=int)
    r.add_argument("--status", help="lessons: comma list of proposed,active,rejected (default: all)")
    _project_arg(r)

    pr = sub.add_parser("propose"); pr.add_argument("store", choices=["expectation", "expectations"])
    pr.add_argument("--area", default=""); pr.add_argument("--statement", required=True)
    pr.add_argument("--source", required=True); pr.add_argument("--confidence", type=float, default=0.5)
    pr.add_argument("--metadata"); pr.add_argument("--k", type=int, default=5); pr.add_argument("--dup-threshold", type=float, default=0.70)
    _tag_args(pr); _project_arg(pr)

    s = sub.add_parser("search"); s.add_argument("store", choices=["app_map", "expectations", "flakiness", "fix_lessons", "fix_lesson", "lessons", "lesson"])
    s.add_argument("query_pos", nargs="?", metavar="QUERY", help="the query (same as --query)")
    s.add_argument("--query"); s.add_argument("--area"); s.add_argument("--kind"); s.add_argument("--status")
    s.add_argument("--spec"); s.add_argument("--signature")
    s.add_argument("--k", type=int, default=None,
                   help="result cap (default 5 for the semantic stores, 20 for app_map ILIKE)")
    s.add_argument("--min-similarity", "--min-score", dest="min_similarity", type=float)
    _project_arg(s)

    c = sub.add_parser("confirm"); c.add_argument("store", choices=["app_map"])
    c.add_argument("--id", type=int, required=True); c.add_argument("--source")
    _tag_args(c)

    re_ = sub.add_parser("reembed")
    re_.add_argument("--store", choices=["expectations", "fix_lessons", "lessons", "all"], default="all")
    re_.add_argument("--limit", type=int, default=100); re_.add_argument("--dry-run", action="store_true")
    re_.add_argument("--dup-threshold", type=float, default=None,
                     help="similar_to reporting threshold (default 0.70 expectations / 0.45 fix_lessons)")
    re_.add_argument("--filter-source", help="only rows with this exact source")
    re_.add_argument("--id", type=int, action="append", help="restrict to these row ids (repeatable)")
    re_.add_argument("--source", default="kb-reembed")
    _tag_args(re_)

    b = sub.add_parser("bless"); b.add_argument("store", choices=["expectation", "expectations", "lesson", "lessons"])
    b.add_argument("--id", type=int, required=True)
    b.add_argument("--by", help="who blessed it (REQUIRED for expectations — a human judgment)")
    b.add_argument("--expect-version", type=int); b.add_argument("--source", default="")
    _tag_args(b); _project_arg(b)

    rj = sub.add_parser("reject"); rj.add_argument("store", choices=["expectation", "expectations", "lesson", "lessons"])
    rj.add_argument("--id", type=int, required=True)
    rj.add_argument("--by", help="who rejected it (REQUIRED for expectations)")
    rj.add_argument("--reason", default=""); rj.add_argument("--expect-version", type=int)
    rj.add_argument("--source", default="")
    _tag_args(rj); _project_arg(rj)

    q = sub.add_parser("quarantine"); q.add_argument("store", choices=["flakiness"])
    q.add_argument("--spec", required=True); q.add_argument("--signature")
    q.add_argument("--on", action="store_true"); q.add_argument("--off", action="store_true"); q.add_argument("--reason", default="")
    q.add_argument("--source", default=""); _tag_args(q); _project_arg(q)

    f = sub.add_parser("forget"); f.add_argument("store", choices=["app_map", "expectations", "flakiness", "fix_lessons", "lessons", "lesson"])
    f.add_argument("--id", type=int); f.add_argument("--key"); f.add_argument("--kind")
    f.add_argument("--area", default=""); f.add_argument("--spec")
    f.add_argument("--hard", action="store_true"); f.add_argument("--soft", action="store_true")
    f.add_argument("--source", default="",
                   help="ledger provenance — AND, when no --id/--key/--spec is given, the row "
                        "SELECTOR: every row written by that source (plan first; --yes applies)")
    f.add_argument("--yes", action="store_true",
                   help="apply a bulk --source forget (without it you get the plan, ok:true)")
    _tag_args(f); _project_arg(f)

    un = sub.add_parser("undo", description=(
        "Reverse what one run wrote, driven by the kb_writes ledger. DRY RUN BY DEFAULT — "
        "--yes applies. Rows the run created are removed (expectations soft-forgotten); rows it "
        "only modified are reversed only where the reversal is exact, else reported as "
        "skipped_unreversible. Never touches kb_control."))
    un.add_argument("--run-id", help="selector: reverse this run's writes")
    un.add_argument("--source", help="selector: reverse everything this writer wrote")
    un.add_argument("--ticket", help="selector: reverse this ticket's writes")
    un.add_argument("--loop-id", help="extra narrowing filter (never a selector on its own)")
    un.add_argument("--store", choices=list(UNDO_STORES), help="narrow to one store")
    un.add_argument("--since", help="only ledger rows newer than e.g. 7d / 24h / 30m")
    un.add_argument("--dry-run", action="store_true",
                    help="explicit no-op (already the default; wins over --yes)")
    un.add_argument("--yes", action="store_true", help="APPLY the reversal")
    un.add_argument("--project", help="extra narrowing filter on the ledger's project (never a selector on its own)")

    d = sub.add_parser("decay"); d.add_argument("--apply", action="store_true")
    d.add_argument("--stale-days", type=int, default=30); d.add_argument("--dup-threshold", type=float, default=0.9)
    d.add_argument("--source", default=""); _tag_args(d)

    pa = sub.add_parser("pause"); pa.add_argument("--on", action="store_true")
    pa.add_argument("--off", action="store_true"); pa.add_argument("--status", action="store_true")
    pa.add_argument("--reason", default=""); pa.add_argument("--by", default=""); _tag_args(pa)

    fl = sub.add_parser("flag"); fl.add_argument("--name", required=True)
    fl.add_argument("--on", action="store_true"); fl.add_argument("--off", action="store_true")
    fl.add_argument("--status", action="store_true")
    fl.add_argument("--reason", default=""); fl.add_argument("--by", default=""); _tag_args(fl)

    m = sub.add_parser("metric"); m.add_argument("--name", required=True); m.add_argument("--value", type=float, required=True)
    m.add_argument("--unit", default="count"); m.add_argument("--area", default="")
    m.add_argument("--metadata"); m.add_argument("--source", default="")
    m.add_argument("--loop-id"); m.add_argument("--ticket"); m.add_argument("--run-id")
    _project_arg(m)

    ms = sub.add_parser("metrics"); ms.add_argument("--summary", action="store_true")
    ms.add_argument("--metric"); ms.add_argument("--since"); ms.add_argument("--loop-id")
    ms.add_argument("--ticket"); ms.add_argument("--area")
    ms.add_argument("--project", help="filter to one project (default: every project — metrics are loop health)")

    rc = sub.add_parser("receipt"); rc.add_argument("--ticket", required=True)
    rc.add_argument("--loop-id"); rc.add_argument("--run-id"); rc.add_argument("--since")

    h = sub.add_parser("history"); h.add_argument("--ticket", required=True)

    st = sub.add_parser("state"); st.add_argument("--key", required=True)
    st.add_argument("--set", dest="set_value", help="JSON value to store")
    st.add_argument("--delete", action="store_true")
    st.add_argument("--source", default=""); _tag_args(st)

    stt = sub.add_parser("status", description=(
        "Which brain this process resolves to and how: config file, scope, project, target "
        "(host/db, never a credential), schema present/current, row counts per store, embedding."))
    _project_arg(stt)

    ob = sub.add_parser("outbox")
    ob.add_argument("--status", action="store_true"); ob.add_argument("--replay", action="store_true")
    ob.add_argument("--clear", action="store_true"); ob.add_argument("--yes", action="store_true")
    ob.add_argument("--limit", type=int)

    sc = sub.add_parser("schema", description=(
        "Render the brain schema for an embedding dimension (default: kb.config embedding.dim), "
        "or apply it to the configured brain (admin action, idempotent)."))
    sc.add_argument("--dim", type=int, help="vector dimension (default: embedding.dim from kb.config)")
    sc.add_argument("--model", help="embedding_model column default (default: embedding.model)")
    sc.add_argument("--out", help="write the rendered SQL to this path (scripts/schema.sql is the shipped default)")
    sc.add_argument("--raw", action="store_true", help="print the SQL itself (for piping into psql)")
    sc.add_argument("--apply", action="store_true", help="apply to the configured brain through the auth mode")
    sc.add_argument("--source", default="kb-schema")
    return p

# Verbs whose writes are stamped with a project. A queued entry of one of these carries its
# project in the argv, so a replay from another directory still stamps the right one.
PROJECT_STAMPED_VERBS = {"write", "propose", "metric"}
# The table a stamped write needs the `project` column on (else: schema_outdated → queued).
def _stamp_table(args):
    if args.verb == "propose":
        return "expectations"
    if args.verb == "write":
        return {"fix_lesson": "fix_lessons", "lesson": "lessons"}.get(args.store, args.store)
    return None


def apply_project(args):
    """Resolve this invocation's project onto args (`_project`, `_all`, `_project_how`) and,
    when it differs from the one the module was loaded for, re-select the config (an overlay
    may hold a per-project kb.config.json). Returns an error string or None."""
    raw = getattr(args, "project", None) or getattr(args, "project_global", None)
    args._all = False
    if raw and raw.strip().lower() == ALL_PROJECTS:
        args._all = True
        proj, how, praw = _IMPORT_PROJECT
    elif raw:
        proj, how, praw = resolve_project(raw)
    else:
        proj, how, praw = _IMPORT_PROJECT
    args._project, args._project_how = proj, ("all" if args._all else how)
    if proj != PROJECT:
        init_config(proj, praw)
    if args._all and args.verb in PROJECT_STAMPED_VERBS:
        return "--project all is read-only: a write is stamped with exactly one project"
    return None


def main(argv=None):
    argv = list(argv) if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(argv)
    if getattr(args, "query_pos", None) and not getattr(args, "query", None):
        args.query = args.query_pos

    if args.verb == "outbox":          # never needs (or opens) a DB connection
        return cmd_outbox(args)
    perr = apply_project(args)
    if perr:
        return fail(perr, queued=False)
    if args.verb == "schema":          # a renderer; connects only on --apply, never queues
        return cmd_schema(args)
    if args.verb == "status":          # reports even when the brain is unreachable; never queues
        return cmd_status(args)

    try:
        dsn = resolve_dsn(args.db)
        validate_auth_config()
    except BrainConfigError as e:
        # Fail closed and LOUD. Deliberately does NOT queue: with no target we cannot say what
        # the write was destined for, and a replay would retarget it at a DSN we still lack.
        return fail(str(e), queued=False)
    if is_mutating(args):
        set_brain_target(dsn)          # every write reports the brain it landed in
    embed, provider = get_embedder(args.embedder)
    conn = None
    try:
        try:
            conn = connect(dsn)
        except Exception as e:         # TRANSPORT failure — the only thing that may queue
            err = "{}: {}".format(type(e).__name__, str(e).strip())
            if is_mutating(args) and not _NO_QUEUE:
                return _queue(argv, args, err)
            return fail(err, queued=False)
        stamp_table = _stamp_table(args)
        if stamp_table:
            # A brain whose schema predates projects cannot stamp this write. It is not a
            # semantic failure — the write is right, the brain is behind — so it is DEFERRED to
            # the outbox exactly like a transport failure, and lands on `outbox --replay` once
            # an admin has run `kb schema --apply`.
            with conn.cursor() as cur:
                ready = has_project_col(cur, stamp_table)
            if not ready:
                conn.rollback()
                err = ("schema_outdated: {}.project is missing — run `kb schema --apply` (admin, "
                       "idempotent), then `kb outbox --replay`".format(stamp_table))
                if not _NO_QUEUE:
                    return _queue(argv, args, err)
                return fail(err, queued=False)
        with conn.cursor() as cur:
            rc = HANDLERS[args.verb](cur, args, embed)
        if rc == 0:
            conn.commit()
        else:
            conn.rollback()            # a failed verb leaves NO ledger row either
        return rc
    except Exception as e:
        if conn:
            conn.rollback()
        return fail("{}: {}".format(type(e).__name__, e))
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    sys.exit(main())
