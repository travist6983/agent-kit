#!/usr/bin/env python3
"""dev-code-review's review depth: deterministic, zero model calls, no network, no brain.

WHY THIS EXISTS. Most small changes under review come back clean, and running six specialist spawns over a
three-line copy change spends the same as a review of a payment path. Depth scales HOW MUCH is
spawned by risk, read from the actual diff: a small, non-risky diff gets ONE combined spawn carrying
all six specialist lenses; a risky diff gets the six plus an extra security pass over the risky
files. The rule that makes this safe is the same one verification tiers follow: a level may shrink
how much is reviewed, never whether, and every unknown moves UP.

LEVELS (no level drops a dimension; security is never off):
  quick     one pass `combined`, specialists = all six, files = the brief's review_files
  standard  six passes, one per specialist (frontend, backend, data, security, tests, perf)
  deep      the six + `security-risk-paths` (security only) over the risk files, whole files and
            their callers; over every changed file when the risk is not path-attributed

THE RULE (closed `reasons` vocabulary; level = the highest any reason demands, never lowered):
  deep, status decided     blast-radius (R6 adopter list OR framework floor) · shared-core
                           (conventions shared_core_globs) · sensitive-area (intake.json sensitive)
                           · conventions-changed (a change may not strip its own denylist)
  deep, status unavailable r6-scan-unavailable (scan cannot run, a changed path holds a newline or
                           control character, or a hit maps to no changed file) · conventions-
                           unreadable · limits-invalid · seam-unreadable (--evid not a directory, or
                           intake.json malformed / `sensitive` not a boolean) · diff-unavailable
  standard floor           size-over-quick · changed-lines-unknown (binary / unreadable file, a
                           change with zero changed lines such as a mode-only change or an empty
                           new file, or a --changed-file override) · empty-diff · in-loop-review
  raise only               min-depth (--min-depth above the plan) · prior-record (record only: the
                           draft already carries a deeper depth.level)
  quick                    only when no other reason fired: reasons == ["small-diff"]
Size limits: loop-limits.json `review_depth` (quick_max_files, quick_max_changed_lines), NO default.

"Denylisted" and "shared core" have ONE definition: this script imports review_brief.py's helpers
(the R6 scan through the adopter list and the floor, resolve_projects.py's glob match). Risk is
scanned over EVERY changed file, both sides of a rename included (--no-renames).

Verbs (each prints ONE JSON object on stdout):
  plan   --repo DIR [--base B] [--limits PATH] [--evid DIR] [--min-depth L] [--changed-file F ...]
         exit 0 decided · 2 unavailable (the deep plan is still printed) or bad usage
  record --review DRAFT.json + the plan flags
         recomputes the plan (never trusts a plan file), keeps a deeper level already in the draft,
         writes `depth` atomically and touches no other key; idempotent. exit 0 (also when the plan
         is unavailable: the record is written at deep) · 2 unreadable draft, or a draft whose
         existing depth block names no valid level (untouched) / bad usage
  check  --review review.json + the plan flags
         read-only: exit 0 {"ok": true} · 1 {"ok": false, "violations": [{class, detail}]} ·
         2 uncheckable. Classes: depth-missing, depth-below-plan, risk-file-unrecorded,
         passes-mismatch, security-not-on-risk-path.

--base defaults to project.default_branch; --limits to stack.config `limits_file`, else
.claude/loops/loop-limits.json; --changed-file replaces the git-derived list (tests) and leaves the
line count unknown. The SKILL treats "no plan" as deep.

HONEST LIMIT: 3 files / 50 lines are ARGUED, NOT MEASURED (3 is assign_tier.py's T0 file bound).
Size counts every changed file, including ones the reviewer brief skips, and a PR re-review is sized
on the full diff against the base, not the interdiff: both err upward. The script proves the plan and
the record, not that the spawns ran as planned (self-reported, like judged_blind); no recipe, runner,
dev-pre-pr step or ship gate calls `check` yet. prs_per_tick was measured at the full fan-out and is
not re-derived. The reviewer eval set is reviewed in-loop (floor standard), so it does not score quick.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_brief as rb  # noqa: E402 — one definition of "denylisted" and "shared core"

SPECIALISTS = ("frontend", "backend", "data", "security", "tests", "perf")
LEVELS = ("quick", "standard", "deep")
RANK = {lv: i for i, lv in enumerate(LEVELS)}
DEEP_DECIDED = ("blast-radius", "shared-core", "sensitive-area", "conventions-changed")
DEEP_UNAVAILABLE = ("r6-scan-unavailable", "conventions-unreadable", "limits-invalid",
                    "seam-unreadable", "diff-unavailable")
STANDARD_FLOOR = ("size-over-quick", "changed-lines-unknown", "empty-diff", "in-loop-review")
LEVEL_OF = dict([(r, "deep") for r in DEEP_DECIDED + DEEP_UNAVAILABLE]
                + [(r, "standard") for r in STANDARD_FLOOR] + [("small-diff", "quick")])
RISK_PASS = "security-risk-paths"


# ---- facts -------------------------------------------------------------------------------------
def _count_lines(path):
    """Line count of an untracked file, or None when it is unreadable or binary."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except Exception:  # noqa: BLE001
        return None
    if b"\0" in data:
        return None
    n = data.count(b"\n")
    n += 1 if data and not data.endswith(b"\n") else 0
    return n or None  # an empty new file changes no line: its size says nothing, unknown


def diff_facts(repo, base, override):
    """(sorted changed files, changed_lines or None). Raises rb.Fatal on a git failure."""
    if override is not None:
        return sorted({f.strip() for f in override if f.strip()}), None
    if not base:
        raise rb.Fatal("cannot determine the base branch (no --base and no project.default_branch)")
    if not os.path.isdir(repo):
        raise rb.Fatal(f"--repo {repo} is not a directory")
    mb = rb.git(repo, "merge-base", base, "HEAD").strip()
    files, lines = set(), 0
    for rec in rb.git(repo, "diff", "--no-renames", "--numstat", "-z", mb).split("\0"):
        if not rec.strip():
            continue
        parts = rec.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, path = parts
        files.add(path.strip())
        if added == "-" or removed == "-" or (added == "0" and removed == "0"):
            lines = None  # binary, or a change with no line in it (mode-only, empty file): unknown
        elif lines is not None:
            lines += int(added) + int(removed)
    for path in rb.git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if not path.strip():
            continue
        files.add(path.strip())
        n = _count_lines(os.path.join(repo, path))
        lines = None if (n is None or lines is None) else lines + n
    return sorted(files), lines


def read_limits(path):
    """(limits dict) or raises rb.Unavailable."""
    try:
        with open(path) as fh:
            block = json.load(fh).get("review_depth")
    except Exception as e:  # noqa: BLE001
        raise rb.Unavailable(f"limits file unreadable ({path}): {e}")
    if not isinstance(block, dict):
        raise rb.Unavailable(f"no review_depth block in {path}: no decided size limit")
    out = {}
    for k in ("quick_max_files", "quick_max_changed_lines"):
        v = block.get(k)
        if not (isinstance(v, int) and not isinstance(v, bool) and v >= 1):
            raise rb.Unavailable(f"review_depth.{k}={v!r} in {path} is not an integer >= 1")
        out[k] = v
    return out


def _display(path):
    ap = os.path.abspath(path)
    root = os.path.abspath(rb.FRAMEWORK_ROOT)
    return os.path.relpath(ap, root) if ap.startswith(root + os.sep) else path


def passes_for(level):
    if level == "quick":
        return [{"name": "combined", "specialists": list(SPECIALISTS), "files": "review"}]
    six = [{"name": s, "specialists": [s], "files": "review"} for s in SPECIALISTS]
    if level == "deep":
        six.append({"name": RISK_PASS, "specialists": ["security"], "files": "risk"})
    return six


def _level(reasons):
    return max((LEVEL_OF.get(r, "deep") for r in reasons), key=lambda lv: RANK[lv])


def compute_depth(repo, base, limits_arg, evid, min_depth, changed_override):
    stack = rb._config()
    base = base or rb._stack_get(stack, "project.default_branch")
    reasons, errors = set(), []
    risk = {}
    files = lines = None

    try:
        files, lines = diff_facts(repo, base, changed_override)
    except rb.Fatal as e:
        reasons.add("diff-unavailable")
        errors.append(str(e))

    raw_conv, conv_path = rb.conventions_path(stack)
    conv = None
    try:
        with open(conv_path) as fh:
            conv = json.load(fh)
        if not isinstance(conv, dict):
            raise ValueError("not a JSON object")
    except Exception as e:  # noqa: BLE001 — shared core cannot be known
        conv = None
        reasons.add("conventions-unreadable")
        errors.append(f"conventions file {raw_conv} unreadable: {e}")

    if files:
        # The R6 scan reads one path per line, so a path with a newline or any other control
        # character cannot be scanned as itself (an author-chosen name could split a denylisted
        # path into harmless-looking pieces). Such a path, or a hit that maps back to no changed
        # file, is an unknown: deep, unavailable — never silently dropped.
        odd = [f for f in files if any(ord(c) < 32 or ord(c) == 127 for c in f)]
        if odd:
            reasons.add("r6-scan-unavailable")
            errors.append(f"{len(odd)} changed path(s) contain a newline or control character and "
                          f"cannot be scanned for R6: {[f for f in odd][:5]!r}")
        try:
            deny = rb.r6_hits(files, floor=False) | rb.r6_hits(files, floor=True)
            for f in deny:
                if f in files:
                    risk[f] = "blast-radius"
            stray = sorted(str(f) for f in deny if f not in files)
            if stray:
                reasons.add("r6-scan-unavailable")
                errors.append(f"R6 denylist scan named path(s) that are not a changed file: {stray[:5]!r}")
        except rb.Unavailable as e:
            reasons.add("r6-scan-unavailable")
            errors.append(str(e))
        if conv is not None:
            shared = [g for g in conv.get("shared_core_globs") or [] if isinstance(g, str) and g.strip()]
            if shared:
                try:
                    match = rb.load_glob_match()
                    for f in files:
                        if f not in risk and any(match(f, g) for g in shared):
                            risk[f] = "shared-core"
                except rb.Fatal as e:
                    reasons.add("conventions-unreadable")
                    errors.append(f"shared_core_globs cannot be matched: {e}")
        if any(rb._same_file(repo, f, conv_path) for f in files):
            reasons.add("conventions-changed")
    for r in risk.values():
        reasons.add(r)

    if evid:
        reasons.add("in-loop-review")
        ipath = os.path.join(evid, "intake.json")
        if not os.path.isdir(evid):
            reasons.add("seam-unreadable")
            errors.append(f"--evid {evid} is not a directory: the run's seams cannot be read")
        elif os.path.exists(ipath):
            try:
                with open(ipath) as fh:
                    intake = json.load(fh)
                if not isinstance(intake, dict):
                    raise ValueError("not a JSON object")
                sens = intake.get("sensitive", False)
                if not isinstance(sens, bool):
                    raise ValueError(f"sensitive is {sens!r}, not a boolean")
                if sens:
                    reasons.add("sensitive-area")
            except Exception as e:  # noqa: BLE001
                reasons.add("seam-unreadable")
                errors.append(f"{ipath} unreadable: {e}")

    lpath = rb.limits_path(stack, limits_arg)
    limits = None
    try:
        limits = read_limits(lpath)
    except rb.Unavailable as e:
        reasons.add("limits-invalid")
        errors.append(str(e))

    if files is not None:
        if not files:
            reasons.add("empty-diff")
        if lines is None:
            reasons.add("changed-lines-unknown")
        if limits and files and (len(files) > limits["quick_max_files"]
                                 or (lines is not None and lines > limits["quick_max_changed_lines"])):
            reasons.add("size-over-quick")
    if not reasons:
        reasons.add("small-diff")
    level = _level(reasons)
    if min_depth and RANK[min_depth] > RANK[level]:
        reasons.add("min-depth")
        level = min_depth
    unavailable = any(r in DEEP_UNAVAILABLE for r in reasons)
    plan = {
        "level": level,
        "status": "unavailable" if unavailable else "decided",
        "reasons": sorted(reasons),
        "files": len(files) if files is not None else None,
        "changed_lines": lines if files is not None else None,
        "risk_files": [{"file": f, "reason": risk[f]} for f in sorted(risk)],
        "passes": passes_for(level),
        "limits": limits,
        "source": f"{_display(lpath)}#review_depth",
    }
    if unavailable:
        plan["error"] = "; ".join(errors) or "unknown"
    return plan


RECORD_KEYS = ("level", "status", "reasons", "files", "changed_lines", "risk_files", "passes", "source")


def _record_of(plan):
    rec = {k: plan[k] for k in RECORD_KEYS}
    if "error" in plan:
        rec["error"] = plan["error"]
    return rec


def _plan(a):
    return compute_depth(a.repo, a.base, a.limits, a.evid, a.min_depth, a.changed_file)


# ---- verbs --------------------------------------------------------------------------------------
def cmd_plan(a):
    plan = _plan(a)
    return rb.emit(plan, 2 if plan["status"] == "unavailable" else 0)


def cmd_record(a):
    doc = rb.read_review(a.review)
    prior = doc.get("depth", None)
    if prior is not None and not (isinstance(prior, dict) and prior.get("level") in RANK):
        # A depth block the draft already carries but that names no known level is not ours to
        # overwrite: refuse, draft untouched, so the bad record is seen rather than papered over.
        raise rb.Fatal(f"review file {a.review} carries a depth block with no valid level "
                       f"({prior.get('level') if isinstance(prior, dict) else prior!r}); fix or remove it")
    plan = _plan(a)
    plevel = (prior or {}).get("level")
    if plevel in RANK and RANK[plevel] > RANK[plan["level"]]:
        plan["level"] = plevel
        plan["reasons"] = sorted(set(plan["reasons"]) | {"prior-record"})
        plan["passes"] = passes_for(plevel)
    doc["depth"] = _record_of(plan)
    rb.atomic_write(a.review, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return rb.emit({"level": plan["level"], "status": plan["status"], "reasons": plan["reasons"]}, 0)


def _security_on_risk(passes):
    return any(isinstance(p, dict) and p.get("files") == "risk"
               and "security" in (p.get("specialists") or []) for p in passes)


def cmd_check(a):
    doc = rb.read_review(a.review)
    plan = _plan(a)
    v = []

    def add(cls, detail):
        v.append({"class": cls, "detail": detail})

    dep = doc.get("depth")
    if not isinstance(dep, dict):
        add("depth-missing", "review.json has no depth block (run review_depth.py record)")
    else:
        lv = dep.get("level")
        if lv not in RANK or RANK[lv] < RANK[plan["level"]]:
            add("depth-below-plan", f"recorded level {lv!r} is below the recomputed {plan['level']!r} "
                                    f"({', '.join(plan['reasons'])})")
        recorded = {r.get("file") for r in dep.get("risk_files") or [] if isinstance(r, dict)}
        missing = sorted(r["file"] for r in plan["risk_files"] if r["file"] not in recorded)
        if missing:
            add("risk-file-unrecorded", f"risk file(s) absent from the record: {missing}")
        passes = dep.get("passes") if isinstance(dep.get("passes"), list) else []
        if lv in RANK and passes != passes_for(lv):
            add("passes-mismatch", f"recorded passes do not match level {lv!r}")
        if (lv == "deep" or dep.get("risk_files")) and not _security_on_risk(passes):
            add("security-not-on-risk-path", "no pass runs security over the risk files")
    if v:
        return rb.emit({"ok": False, "plan_level": plan["level"], "plan_status": plan["status"],
                        "violations": v}, 1)
    return rb.emit({"ok": True, "plan_level": plan["level"], "plan_status": plan["status"],
                    "level": dep.get("level")}, 0)


def main(argv):
    ap = argparse.ArgumentParser(prog="review_depth.py")
    sub = ap.add_subparsers(dest="verb")
    for name in ("plan", "record", "check"):
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".")
        p.add_argument("--base", default=None)
        p.add_argument("--limits", default=None)
        p.add_argument("--evid", default=None)
        p.add_argument("--min-depth", choices=LEVELS, default=None)
        p.add_argument("--changed-file", action="append", default=None)
        if name != "plan":
            p.add_argument("--review", required=True)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return rb.emit({"error": "bad usage"}, 2)
    if not a.verb:
        return rb.emit({"error": "bad usage: plan | record | check"}, 2)
    try:
        return {"plan": cmd_plan, "record": cmd_record, "check": cmd_check}[a.verb](a)
    except rb.Fatal as e:
        return rb.emit({"error": str(e)}, 2)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — never crash open
        print(json.dumps({"error": f"review_depth crashed: {e.__class__.__name__}: {e}"}))
        sys.exit(2)
