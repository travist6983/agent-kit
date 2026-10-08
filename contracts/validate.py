#!/usr/bin/env python3
"""Validate a skill's output JSON against a ratified contract schema.

Portable (stdlib only — no `jsonschema` dependency) so the runner, CI, or a human can run it anywhere.
Enforces exactly what the control plane relies on: the byte-for-byte key set (additionalProperties:false →
no extra/missing keys), enum membership, JSON types, and integer bounds.

It recurses into object properties AND array `items`, so it fully guards the OPTIONAL `media` array
added in qa-repro.json v1.2.0 / qa-verify.repro-judge.json v1.1.0: each media item's `kind` enum,
required `url`, and per-item `additionalProperties:false` are all checked, and a `media` that is a bare
string (the old prose-blob bug) fails as "expected type array, got str". No media-specific code — the
generic recursion covers it.

Usage:
  python3 contracts/validate.py contracts/qa-repro.json               <path>/repro.json
  python3 contracts/validate.py contracts/qa-verify.repro-judge.json  <path>/verify.json

Prints a PASS/FAIL report and exits 0 (contract-true) / 1 (violation) / 2 (bad usage or unreadable input).

RECEIPTS MODE — cross-check an artifact's CLAIMED brain writes against what the brain
actually recorded. A schema check proves the artifact is well-formed; it cannot prove
`"app_map_writes": [...]` was ever more than the agent's recollection. Since the kb CLI
ledgers every mutating verb into kb_writes inside the write's own transaction, the claim
is now falsifiable:

  python3 contracts/validate.py --receipts PROJ-123 [artifact.json|dir ...]
        [--evid-root DIR] [--kb PATH] [--since 7d] [--loop-id L] [--run-id R]
        (--evid-root defaults to stack.config `evidence.root`, else ./runs)
        [--strict] [--json]

Reads `app_map_writes` + `brain_writes` (repro.json), `flakiness_writes` (regression.json),
`lessons_consulted` + `pending_fix_lesson` (fix.json), and the `verdict` (verify.json /
judge.json), then compares them with `kb receipt --ticket <KEY>`.

  exit 0  no drift — every claimed write is in the ledger (or honestly nothing to compare)
  exit 1  DRIFT — the artifact claims a write the brain never received
  exit 2  bad usage / unreadable artifact
  exit 3  UNDETERMINED — the brain is unreachable, the kb CLI is missing, or kb_writes does
          not exist yet. Deliberately NOT 1: a dead brain is not evidence that an agent lied.

Ledger rows the artifact did NOT claim are reported as notes (with their source), not as
drift — other skills in the same loop legitimately write under the same ticket. `--strict`
promotes those notes to drift.
"""
import json
import os
import re
import subprocess
import sys

_JSON_TYPES = {
    "object": dict, "array": list, "string": str,
    "boolean": bool, "integer": int, "number": (int, float),
    "null": type(None),
}


def _types_of(spec):
    """JSON Schema `type` is a name OR a union list (["object","null"]). Always return a list."""
    jtype = spec.get("type")
    if jtype is None:
        return []
    return jtype if isinstance(jtype, list) else [jtype]


def _type_ok(value, jtype):
    """Accepts a single type name or a union list. Before this handled lists it raised
    `TypeError: unhashable type: 'list'` — a traceback, not a verdict — on any contract using
    the legal `"type": ["object", "null"]` form."""
    for t in (jtype if isinstance(jtype, list) else [jtype]):
        if t not in _JSON_TYPES:
            raise ValueError("contract error: unknown JSON Schema type %r" % (t,))
        # bool is a subclass of int in Python — guard so a bool never satisfies integer/number.
        if t in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, _JSON_TYPES[t]):
            return True
    return False


def _check(spec, val, path, errors):
    """Validate one value against one (sub)schema. Recurses into object properties + array items."""
    if "anyOf" in spec:
        # Pass if ANY alternative validates (used e.g. for design_decisions: string | object).
        alt_errs = []
        for alt in spec["anyOf"]:
            errs = []
            _check(alt, val, path, errs)
            if not errs:
                return
            alt_errs.append(errs)
        errors.append(f"{path}: matches no anyOf alternative "
                      f"(closest: {min(alt_errs, key=len)[0] if alt_errs else 'none'})")
        return
    jtype = spec.get("type")
    if jtype and not _type_ok(val, jtype):
        errors.append(f"{path}: expected type {jtype}, got {type(val).__name__}")
        return
    if "enum" in spec and val not in spec["enum"]:
        errors.append(f"{path}: {val!r} not in allowed values {spec['enum']}")
    # Membership, not equality — so the sub-checks still run under a union type. The isinstance
    # guards matter there too: ["integer","null"] must not compare None against `minimum`.
    jtypes = _types_of(spec)
    if "integer" in jtypes and isinstance(val, int) and not isinstance(val, bool):
        if "minimum" in spec and val < spec["minimum"]:
            errors.append(f"{path}: {val} < minimum {spec['minimum']}")
        if "maximum" in spec and val > spec["maximum"]:
            errors.append(f"{path}: {val} > maximum {spec['maximum']}")
    if "object" in jtypes and isinstance(val, dict):
        _check_object(spec, val, path, errors)
    if "array" in jtypes and isinstance(val, list) and "items" in spec:
        for i, item in enumerate(val):
            _check(spec["items"], item, f"{path}[{i}]", errors)


def _check_object(schema, doc, path, errors):
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    actual = set(doc.keys())
    pfx = (path + ".") if path else ""

    missing = required - actual
    if missing:
        errors.append(f"{path or '<root>'}: missing required key(s): {sorted(missing)}")
    if schema.get("additionalProperties", True) is False:
        extra = actual - set(props.keys())
        if extra:
            errors.append(f"{path or '<root>'}: extra key(s) not allowed by contract: {sorted(extra)}")
    for key, spec in props.items():
        if key in doc:
            _check(spec, doc[key], f"{pfx}{key}", errors)


def validate(schema, doc):
    if not isinstance(doc, dict):
        return [f"top-level value must be a JSON object, got {type(doc).__name__}"]
    errors = []
    _check_object(schema, doc, "", errors)
    if str(schema.get("$id", "")).endswith("/qa-regression.json"):
        _check_regression_c1(doc, errors)
    return errors


def _check_regression_c1(doc, errors):
    """QA-REGRESSION-C1, a cross-field rule the schema keywords can't express: a green verdict or a
    ship-ok needs at least one executed test in a non-empty area run. A real run once shipped on
    area_slice_run [] + ship-ok; this is the mechanical block against that shape."""
    if doc.get("status") != "green" and doc.get("next_action") != "ship-ok":
        return
    total = (doc.get("results") or {}).get("total")
    if isinstance(total, int) and not isinstance(total, bool) and total < 1:
        errors.append("QA-REGRESSION-C1: green/ship-ok with results.total < 1 — a zero-test run is blocked-slice-empty")
    if not doc.get("area_slice_run"):
        errors.append("QA-REGRESSION-C1: green/ship-ok with an empty area_slice_run — no area test ran")


# ── receipts mode: is the artifact's brain-write claim TRUE? ────────────────
# Enforcement half of the capture story. Everything here is READ-ONLY and goes through
# the kb CLI — never raw SQL (the qa-kb contract), so DSN/auth resolution, normalization
# and the degraded paths are all the brain's own.

RECEIPT_ARTIFACTS = ("repro.json", "fix.json", "regression.json", "verify.json", "judge.json")
# Only these ledger rows are "the run wrote a selector". A `confirm` refreshes freshness
# without changing the row, so counting it as a write would invent drift in both directions.
APP_MAP_WRITE_ACTIONS = ("insert", "update", "update_refresh")
MAX_LESSON_LOOKUPS = 25
_ID_IN_TEXT = re.compile(r"#(\d+)")


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_evid_root():
    """stack.config `evidence.root` when the adapter package is importable, else ./runs — the
    validator stays stdlib-portable, so the config read is best-effort and never raises."""
    root = _repo_root()
    try:
        if root not in sys.path:
            sys.path.insert(0, root)
        from lib.adapters import cfg  # noqa: WPS433 (lazy on purpose)
        value = cfg("evidence.root", "runs")
    except Exception:  # noqa: BLE001 — missing package / unreadable config is not a validator error
        value = "runs"
    return value if os.path.isabs(str(value)) else os.path.join(root, str(value))


def _last_json(text):
    """The kb CLI prints ONE JSON object on stdout; a driver may prepend a notice."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def kb_call(kb_path, args, timeout=60):
    """Run one read-only kb verb. Returns (payload, note). NEVER raises, never mutates."""
    if not kb_path or not os.path.exists(kb_path):
        return None, "kb_cli_missing: {}".format(kb_path)
    try:
        p = subprocess.run([sys.executable, kb_path] + list(args),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "kb_timeout after {}s".format(timeout)
    except Exception as e:                                    # noqa: BLE001
        return None, "kb_unreachable: {}: {}".format(type(e).__name__, e)
    payload = _last_json(p.stdout.decode("utf-8", "replace"))
    if payload is None:
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, "kb_no_json (rc={}): {}".format(p.returncode, (err[-1] if err else "")[:200])
    if payload.get("ok") is not True:
        return payload, "kb_error: {}".format(str(payload.get("error"))[:300])
    return payload, "ok"


def _ledger_unavailable(note):
    """A missing kb_writes table is 'the audit trail was never built here', not a lie."""
    n = (note or "").lower()
    return ("kb_writes" in n and "exist" in n) or "undefinedtable" in n


def _claim_key(item):
    """The app_map key an app_map_writes entry names, whatever shape the skill emitted."""
    if isinstance(item, str):
        return item.strip() or None
    if not isinstance(item, dict):
        return None
    for k in ("key", "app_map_key", "name"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    row = item.get("row")
    if isinstance(row, dict) and isinstance(row.get("key"), str) and row["key"].strip():
        return row["key"].strip()
    return None


def _lesson_id(item):
    """An int id, or an id embedded in a human handle like 'fix_lessons#41 state-drift'."""
    if isinstance(item, bool):
        return None
    if isinstance(item, int):
        return item
    if isinstance(item, str):
        s = item.strip()
        if s.isdigit():
            return int(s)
        m = _ID_IN_TEXT.search(s)
        if m:
            return int(m.group(1))
    return None


def discover_artifacts(ticket, given, evid_root):
    """Explicit paths win. Otherwise look for <root>/<TICKET>/ and <root>/*/<TICKET>/."""
    out, seen = [], set()

    def add(p):
        p = os.path.abspath(p)
        if p not in seen and os.path.isfile(p):
            seen.add(p)
            out.append(p)

    def add_dir(d):
        for name in RECEIPT_ARTIFACTS:
            add(os.path.join(d, name))

    if given:
        for g in given:
            if os.path.isdir(g):
                add_dir(g)
            else:
                add(g)
        return out
    roots = [evid_root] if evid_root else [_default_evid_root()]
    for root in roots:
        if not os.path.isdir(root):
            continue
        cand = os.path.join(root, ticket)
        if os.path.isdir(cand):
            add_dir(cand)
        for sub in sorted(os.listdir(root)):
            cand = os.path.join(root, sub, ticket)
            if os.path.isdir(cand):
                add_dir(cand)
    return out


def read_claims(paths):
    """Pull every brain-write CLAIM out of the artifacts. Unreadable files are RECORDED,
    never skipped silently — an artifact we could not read is not an artifact that agrees."""
    claims = {
        "app_map_writes": [], "app_map_keys": [], "app_map_from": [],
        "brain_writes": None, "brain_writes_from": None,
        "flakiness_writes": None, "flakiness_from": None, "regression_total": None,
        "lessons_consulted": [], "lessons_from": None,
        "pending_fix_lesson": "absent", "pending_from": None,
        "verdicts": [], "files": [], "unreadable": [],
    }
    for p in paths:
        try:
            with open(p) as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            claims["unreadable"].append({"path": p, "error": str(e)[:200]})
            continue
        if not isinstance(doc, dict):
            claims["unreadable"].append({"path": p, "error": "top-level value is not an object"})
            continue
        claims["files"].append(p)
        base = os.path.basename(p)

        amw = doc.get("app_map_writes")
        if isinstance(amw, list):
            claims["app_map_writes"].extend(amw)
            claims["app_map_keys"].extend([k for k in (_claim_key(i) for i in amw) if k])
            claims["app_map_from"].append(base)
        bw = doc.get("brain_writes")
        if isinstance(bw, dict):
            acc = claims["brain_writes"] or {"ok": 0, "failed": 0, "queued": 0}
            for k in ("ok", "failed", "queued"):
                v = bw.get(k)
                if isinstance(v, int) and not isinstance(v, bool):
                    acc[k] += v
            claims["brain_writes"] = acc
            claims["brain_writes_from"] = base
        fw = doc.get("flakiness_writes")
        if isinstance(fw, int) and not isinstance(fw, bool):
            claims["flakiness_writes"] = (claims["flakiness_writes"] or 0) + fw
            claims["flakiness_from"] = base
            res = doc.get("results")
            if isinstance(res, dict) and isinstance(res.get("total"), int):
                claims["regression_total"] = res["total"]
        lc = doc.get("lessons_consulted")
        if isinstance(lc, list):
            claims["lessons_consulted"].extend(lc)
            claims["lessons_from"] = base
        if "pending_fix_lesson" in doc:
            claims["pending_fix_lesson"] = doc["pending_fix_lesson"]
            claims["pending_from"] = base
        v = doc.get("verdict")
        if isinstance(v, str):
            claims["verdicts"].append({"file": base, "verdict": v})
    return claims


def _finding(name, status, detail, **extra):
    f = {"check": name, "status": status, "detail": detail}
    f.update(extra)
    return f


def check_receipts(ticket, paths, kb_path, since=None, loop_id=None, run_id=None, strict=False):
    """-> report dict. status: ok | drift | undetermined."""
    claims = read_claims(paths)
    report = {"mode": "receipts", "ticket": ticket, "artifacts": claims["files"],
              "unreadable": claims["unreadable"], "findings": [], "ledger": None,
              "status": "ok"}

    args = ["receipt", "--ticket", ticket]
    for flag, val in (("--since", since), ("--loop-id", loop_id), ("--run-id", run_id)):
        if val:
            args += [flag, val]
    payload, note = kb_call(kb_path, args)
    if note != "ok":
        report["status"] = "undetermined"
        report["findings"].append(_finding(
            "brain-reachable", "undetermined",
            ("kb_writes has not been created on this brain — apply the brain schema "
             "(`kb.py schema --apply`, see the qa-kb skill), then re-run"
             if _ledger_unavailable(note) else
             "the brain could not be reached, so no claim can be confirmed OR refuted"),
            error=note, kb=kb_path))
        return report

    rows = []
    for store_rows in (payload.get("writes_by_store") or {}).values():
        if isinstance(store_rows, list):
            rows.extend([r for r in store_rows if isinstance(r, dict)])
    by_store = {}
    for r in rows:
        by_store.setdefault(r.get("store") or "?", []).append(r)
    report["ledger"] = {
        "count": payload.get("count", len(rows)),
        "counts_by_store": payload.get("counts_by_store") or
                           dict((k, len(v)) for k, v in by_store.items()),
        "sources": payload.get("sources") or [],
        "first_at": payload.get("first_at"), "last_at": payload.get("last_at"),
    }

    def add(f):
        report["findings"].append(f)
        if f["status"] == "drift":
            report["status"] = "drift"
        elif f["status"] == "undetermined" and report["status"] == "ok":
            report["status"] = "undetermined"

    # ── app_map: claimed selector/route writes vs ledgered ones ──
    am_rows = [r for r in by_store.get("app_map", [])
               if r.get("verb") == "write" and r.get("action") in APP_MAP_WRITE_ACTIONS]
    confirms = [r for r in by_store.get("app_map", []) if r.get("verb") == "confirm"]
    if claims["app_map_from"]:
        queued = (claims["brain_writes"] or {}).get("queued", 0)
        claimed_n = len(claims["app_map_writes"])
        expected = max(0, claimed_n - queued)
        summaries = [(r.get("summary") or "") for r in am_rows]
        missing = [k for k in claims["app_map_keys"] if k[:200] not in summaries]
        detail = "claimed {} app_map write(s), ledger has {}".format(claimed_n, len(am_rows))
        if queued:
            detail += "; {} spooled to the outbox (not in the brain yet — replay them)".format(queued)
        if missing:
            # The outbox spools the whole argv, not the key, so we cannot tell WHICH claimed
            # key is sitting in it. If no more keys are missing than the artifact says were
            # queued, the queue fully explains the gap — that is a delayed write, not a lie.
            explained = queued and len(missing) <= queued
            add(_finding("app_map-writes", "note" if explained else "drift",
                         detail + "; key(s) not in the ledger: " + ", ".join(missing[:10])
                         + (" — consistent with the {} queued write(s); replay the outbox "
                            "(`kb outbox --replay`) and re-check".format(queued) if explained
                            else ""),
                         claimed=claimed_n, ledgered=len(am_rows), missing_keys=missing,
                         queued=queued, source_files=claims["app_map_from"]))
        elif len(am_rows) < expected:
            add(_finding("app_map-writes", "drift",
                         detail + "; {} claimed write(s) have no ledger row".format(expected - len(am_rows)),
                         claimed=claimed_n, ledgered=len(am_rows), expected=expected,
                         queued=queued, source_files=claims["app_map_from"]))
        else:
            add(_finding("app_map-writes", "ok", detail,
                         claimed=claimed_n, ledgered=len(am_rows), queued=queued,
                         source_files=claims["app_map_from"]))
        unclaimed = max(0, len(am_rows) - claimed_n)
        if unclaimed:
            srcs = sorted(set(r.get("source") or "?" for r in am_rows))
            add(_finding("app_map-unclaimed", "drift" if strict else "note",
                         "{} app_map ledger row(s) no artifact claims (sources: {})".format(
                             unclaimed, ", ".join(srcs)),
                         unclaimed=unclaimed, sources=srcs))
    elif am_rows:
        add(_finding("app_map-unclaimed", "note",
                     "{} app_map write(s) in the ledger; no artifact carries app_map_writes "
                     "to compare them against".format(len(am_rows)),
                     ledgered=len(am_rows)))
    if confirms:
        add(_finding("app_map-confirms", "note",
                     "{} selector(s) confirmed fresh this run (qa-verify's `kb confirm app_map "
                     "--id`) — freshness, not writes".format(len(confirms)),
                     confirmed=len(confirms)))

    # ── flakiness: one data point per spec RUN, or flake_rate is not a rate ──
    fl_rows = by_store.get("flakiness", [])
    if claims["flakiness_writes"] is not None:
        claimed_n = claims["flakiness_writes"]
        if len(fl_rows) < claimed_n:
            add(_finding("flakiness-writes", "drift",
                         "claimed {} flakiness write(s), ledger has {}".format(claimed_n, len(fl_rows)),
                         claimed=claimed_n, ledgered=len(fl_rows),
                         source_files=[claims["flakiness_from"]]))
        else:
            add(_finding("flakiness-writes", "ok",
                         "claimed {} flakiness write(s), ledger has {}".format(claimed_n, len(fl_rows)),
                         claimed=claimed_n, ledgered=len(fl_rows)))
        total = claims["regression_total"]
        if isinstance(total, int) and total > 0 and claimed_n < total:
            add(_finding("flakiness-rate-integrity", "note",
                         "{} spec(s) ran but only {} flakiness row(s) written — a rate built "
                         "from a subset of runs is biased; write one point per spec RUN, pass "
                         "or fail (R7 reads failures/runs)".format(total, claimed_n),
                         results_total=total, flakiness_writes=claimed_n))
    elif fl_rows:
        add(_finding("flakiness-unclaimed", "note",
                     "{} flakiness write(s) in the ledger; regression.json carries no "
                     "flakiness_writes to compare".format(len(fl_rows)), ledgered=len(fl_rows)))

    # ── fix_lessons: the recipe owes a lesson once qa-verify is green ──
    fx_rows = by_store.get("fix_lessons", [])
    pending = claims["pending_fix_lesson"]
    verdicts = [v["verdict"] for v in claims["verdicts"]]
    verified = "pass" in verdicts
    if pending == "absent":
        if fx_rows:
            add(_finding("fix_lesson-write", "note",
                         "{} fix_lesson(s) ledgered; fix.json carries no pending_fix_lesson "
                         "(pre-v1.1.0 artifact)".format(len(fx_rows)), ledgered=len(fx_rows)))
    elif pending is None:
        add(_finding("fix_lesson-write", "note",
                     "dev-fix declared no reusable lesson (pending_fix_lesson: null) — "
                     "an honest decline, not a missing write", ledgered=len(fx_rows)))
    elif isinstance(pending, dict):
        if fx_rows:
            add(_finding("fix_lesson-write", "ok",
                         "pending_fix_lesson proposed and {} fix_lesson(s) ledgered".format(len(fx_rows)),
                         ledgered=len(fx_rows), symptom=str(pending.get("symptom", ""))[:120]))
        elif verified:
            add(_finding("fix_lesson-write", "drift",
                         "qa-verify passed and dev-fix proposed a lesson, but no fix_lessons row "
                         "was ever written — a successful fix that taught the brain nothing",
                         ledgered=0, verdicts=claims["verdicts"],
                         symptom=str(pending.get("symptom", ""))[:120]))
        else:
            add(_finding("fix_lesson-write", "note",
                         "pending_fix_lesson proposed but not yet written — the write is the "
                         "recipe's, gated on a green qa-verify (verdicts seen: {})".format(
                             verdicts or "none"), ledgered=0))
    else:
        add(_finding("fix_lesson-write", "drift",
                     "pending_fix_lesson must be an object or null, got {}".format(
                         type(pending).__name__)))

    # ── lessons_consulted: does the brain still hold what the fix cited? ──
    if claims["lessons_consulted"]:
        ids, unverifiable = [], []
        for item in claims["lessons_consulted"]:
            lid = _lesson_id(item)
            if lid is None:
                unverifiable.append(str(item)[:80])
            elif lid not in ids:
                ids.append(lid)
        gone, undetermined = [], None
        for lid in ids[:MAX_LESSON_LOOKUPS]:
            pl, n = kb_call(kb_path, ["read", "fix_lessons", "--id", str(lid)])
            if n != "ok":
                undetermined = n
                break
            if not pl.get("count"):
                gone.append(lid)
        if undetermined:
            add(_finding("lessons-consulted", "undetermined",
                         "could not confirm the cited lesson rows", error=undetermined))
        elif gone:
            add(_finding("lessons-consulted", "drift",
                         "fix.json cites fix_lessons row(s) the brain does not have: {}".format(
                             ", ".join(str(g) for g in gone)),
                         missing_ids=gone, checked=len(ids[:MAX_LESSON_LOOKUPS])))
        else:
            add(_finding("lessons-consulted", "ok",
                         "{} cited lesson row(s) confirmed present".format(len(ids[:MAX_LESSON_LOOKUPS])),
                         checked=len(ids[:MAX_LESSON_LOOKUPS])))
        if unverifiable:
            add(_finding("lessons-consulted-unverifiable", "note",
                         "{} entr(y/ies) carry no row id, so they cannot be confirmed: {}".format(
                             len(unverifiable), ", ".join(unverifiable[:5])),
                         entries=unverifiable[:10]))

    if claims["unreadable"]:
        add(_finding("artifacts-readable", "drift",
                     "{} artifact(s) could not be read, so their claims are unchecked".format(
                         len(claims["unreadable"])),
                     files=[u["path"] for u in claims["unreadable"]]))
    if not claims["files"] and not claims["unreadable"]:
        add(_finding("artifacts-found", "note",
                     "no artifacts found to cross-check — reporting the ledger only"))
    return report


def _print_receipts(report):
    st = report["status"]
    head = {"ok": "PASS", "drift": "DRIFT", "undetermined": "UNDETERMINED"}[st]
    print("{}  receipts cross-check — {}".format(head, report["ticket"]))
    led = report["ledger"]
    if led:
        parts = ", ".join("{}={}".format(k, v) for k, v in sorted(led["counts_by_store"].items()))
        print("      ledger: {} row(s){}{}".format(
            led["count"], "  [" + parts + "]" if parts else "",
            "  sources: " + ", ".join(led["sources"]) if led["sources"] else ""))
    if report["artifacts"]:
        print("      artifacts: " + ", ".join(os.path.basename(p) for p in report["artifacts"]))
    for f in report["findings"]:
        mark = {"ok": "  ok  ", "note": "  note", "drift": " DRIFT",
                "undetermined": "  ????"}[f["status"]]
        print("      {} {:<32} {}".format(mark, f["check"], f["detail"]))
        if f.get("error"):
            print("             reason: {}".format(f["error"]))


def receipts_main(argv):
    ticket, given = None, []
    kb_path = os.path.join(_repo_root(), ".claude", "skills", "qa-kb", "scripts", "kb.py")
    evid_root = since = loop_id = run_id = None
    as_json = strict = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--kb", "--evid-root", "--since", "--loop-id", "--run-id"):
            if i + 1 >= len(argv):
                print("ERROR {} needs a value".format(a))
                return 2
            val = argv[i + 1]
            i += 2
            if a == "--kb":
                kb_path = val
            elif a == "--evid-root":
                evid_root = val
            elif a == "--since":
                since = val
            elif a == "--loop-id":
                loop_id = val
            else:
                run_id = val
            continue
        if a == "--json":
            as_json = True
        elif a == "--strict":
            strict = True
        elif a.startswith("--"):
            print("ERROR unknown flag {}\n".format(a))
            print(__doc__)
            return 2
        elif ticket is None:
            ticket = a
        else:
            given.append(a)
        i += 1
    if not ticket:
        print("ERROR --receipts needs a ticket key, e.g. --receipts PROJ-123\n")
        print(__doc__)
        return 2

    paths = discover_artifacts(ticket, given, evid_root)
    report = check_receipts(ticket, paths, kb_path, since=since, loop_id=loop_id,
                            run_id=run_id, strict=strict)
    if as_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_receipts(report)
    return {"ok": 0, "drift": 1, "undetermined": 3}[report["status"]]


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--receipts":
        return receipts_main(sys.argv[2:])
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    schema_path, doc_path = sys.argv[1], sys.argv[2]
    try:
        with open(schema_path) as f:
            schema = json.load(f)
        with open(doc_path) as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        # Missing/unreadable file or malformed JSON — a clean exit 2, not a traceback (CI-friendly).
        print(f"ERROR unreadable input: {e}")
        return 2
    errors = validate(schema, doc)

    title = schema.get("title", schema_path)
    if errors:
        print(f"FAIL  {doc_path}\n      contract: {title} v{schema.get('version','?')}")
        for e in errors:
            print(f"        - {e}")
        return 1
    keys = sorted(doc.keys())
    print(f"PASS  {doc_path}")
    print(f"      contract: {title} v{schema.get('version','?')}")
    print(f"      keys byte-for-byte: {keys}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
