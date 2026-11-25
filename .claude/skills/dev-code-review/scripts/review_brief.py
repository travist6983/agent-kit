#!/usr/bin/env python3
"""dev-code-review's per-area reviewer brief: deterministic, zero model calls, no network, no brain.

WHY THIS EXISTS. The people who own an area of the target repo know what failure looks like there,
which paths are noise (generated code, snapshots, vendored files) and how many nits they are
willing to read. The reviewer brief is where they say so: ONE optional key, `review_brief`, in the
conventions file (`stack.conventions_file`, beside `rules[]`). Absent = today's behaviour.

  "review_brief": {
    "max_nits":   <int >= 0>                      optional; else loop-limits review_brief.default_max_nits
    "skip_paths": [<glob>, ...]                   optional; never a match-all glob
    "areas": [ { "name", "path_globs": [...], "failure_looks_like": [...],
                 "owner"?, "not_worth_flagging"?: [...] } ]          optional
  }
  Keys starting with `_comment` are ignored at every level; any other unknown key, a wrong type,
  an empty or duplicate area name or a match-all skip glob makes the brief MALFORMED.

A brief can add scrutiny, cap nits and skip paths. It can never skip a path that is
  * hit by the effective R6 denylist (dev-fix/scripts/r6_denylist_scan.sh --stdin), or
  * hit by the framework floor (the same scan with --globs dev-fix/assets/r6-denylist.globs),
    because an adopter's list REPLACES the floor and may omit a category, or
  * matched by `shared_core_globs`.
Such a file is listed in `skip_refused` and reviewed as normal. Globs use resolve_projects.py's own
`_glob_match` (imported by path), the rule `projects[].path_globs` and `shared_core_globs` use.

Every failure degrades to "review everything, no cap" (status `unavailable`): a malformed brief, an
R6 scan that cannot run, a missing/invalid limits number when the brief omits max_nits, or a diff
that changes the conventions file itself (a change under review may not grant itself skip paths).

Verbs (each prints ONE JSON object on stdout):
  plan     --repo DIR [--base B] [--limits PATH] [--changed-file F ...]
           exit 0 applied|absent · exit 2 unavailable (plan still printed) or git/usage failure
  finalize --review DRAFT.json --repo DIR [...] [--report review.md]
           recomputes the plan (never trusts a plan file), then rewrites DRAFT in place, atomically:
           withholds open `low` findings over the cap (applied only), writes `brief`, writes the
           count-first `headline` (ALWAYS, brief or not) at the start of `summary` and as the first
           line of review.md. Idempotent. exit 0 · exit 2 unreadable draft (untouched) / git / usage
  check    --review review.json --repo DIR [...] [--report review.md]
           read-only re-check: exit 0 consistent · 1 {"ok": false, "violations": [...]} · 2 uncheckable

--base defaults to project.default_branch; --limits to stack.config `limits_file`, else
.claude/loops/loop-limits.json; --changed-file replaces the git-derived list (tests).

HONEST LIMIT: the reviewer's own `finalize` call is the enforcement point. `check` exists and is
tested, but no recipe, runner, dev-pre-pr step or ship gate calls it yet.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

FRAMEWORK_ROOT = os.environ.get("LOOPS_REPO_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
SKILLS = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
RESOLVE = os.path.join(SKILLS, "qa-regression", "scripts", "resolve_projects.py")
R6SCAN = os.path.join(SKILLS, "dev-fix", "scripts", "r6_denylist_scan.sh")
R6FLOOR = os.path.join(SKILLS, "dev-fix", "assets", "r6-denylist.globs")
DEFAULT_LIMITS = os.path.join(".claude", "loops", "loop-limits.json")
DEFAULT_CONVENTIONS = ".claude/loops/conventions.json"

SEVERITIES = ("critical", "high", "medium", "low")
BRIEF_KEYS = {"max_nits", "skip_paths", "areas"}
AREA_KEYS = {"name", "path_globs", "failure_looks_like", "owner", "not_worth_flagging"}
NIT_REASON_PREFIX = "nit cap (review_brief max_nits="
HEADLINE_RX = re.compile(r"^(\d+) findings?\b")
HEADLINE_STRIP_RX = re.compile(r"^(?:0 findings: nothing needs fixing\.|\d+ findings? \([^)]*\)\.|\d+ findings?\.)\s*")


class Fatal(Exception):
    """git / usage / framework failure: exit 2, nothing written."""


class Unavailable(Exception):
    """The brief cannot be applied honestly: review everything, no cap."""


def emit(doc, code):
    print(json.dumps(doc, indent=1, sort_keys=True))
    return code


# ---- config ----------------------------------------------------------------------------------
def _config():
    try:
        sys.path.insert(0, FRAMEWORK_ROOT)
        from lib.adapters.config import load_config
        return load_config()
    except Exception:  # noqa: BLE001 — no stack.config: framework defaults
        return None


def _stack_get(stack, key, default=None):
    try:
        return (stack.get(key) if stack else None) or default
    except Exception:  # noqa: BLE001
        return default


def conventions_path(stack):
    raw = _stack_get(stack, "stack.conventions_file", DEFAULT_CONVENTIONS)
    try:
        p = stack.resolve(raw, DEFAULT_CONVENTIONS) if stack else None
    except Exception:  # noqa: BLE001
        p = None
    return raw, str(p) if p is not None else os.path.join(FRAMEWORK_ROOT, raw)


def limits_path(stack, arg):
    if arg:
        return arg
    if stack is not None:
        try:
            p = stack.resolve(stack.get("limits_file"), DEFAULT_LIMITS)
            if p is not None:
                return str(p)
        except Exception:  # noqa: BLE001
            pass
    return os.path.join(FRAMEWORK_ROOT, DEFAULT_LIMITS)


def _nonneg_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def read_default_max_nits(path):
    try:
        with open(path) as fh:
            block = json.load(fh).get("review_brief")
    except Exception as e:  # noqa: BLE001
        raise Unavailable(f"limits file unreadable ({path}): {e}")
    if not isinstance(block, dict):
        raise Unavailable(f"no review_brief block in {path} and the brief states no max_nits — "
                          "no decided number, the cap is not applied")
    v = block.get("default_max_nits")
    if not _nonneg_int(v):
        raise Unavailable(f"review_brief.default_max_nits={v!r} in {path} is not an integer >= 0")
    return v


def load_glob_match():
    spec = importlib.util.spec_from_file_location("resolve_projects", RESOLVE)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        raise Fatal("resolve_projects.py could not be imported (lib.adapters.config unavailable)")
    return mod._glob_match


# ---- the brief ---------------------------------------------------------------------------------
def _real_keys(d):
    return {k for k in d if not str(k).startswith("_comment")}


def _str_list(v, what, non_empty):
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise Unavailable(f"review_brief malformed: {what} must be a list of non-empty strings")
    if non_empty and not v:
        raise Unavailable(f"review_brief malformed: {what} must not be empty")
    return [x.strip() for x in v]


def _match_all(glob):
    g = glob.strip()
    return g == "" or set(g) <= {"*", "/"}


def validate_brief(b):
    if not isinstance(b, dict):
        raise Unavailable(f"review_brief malformed: must be an object, got {type(b).__name__}")
    extra = _real_keys(b) - BRIEF_KEYS
    if extra:
        raise Unavailable(f"review_brief malformed: unknown key(s) {sorted(extra)}")
    out = {"max_nits": None, "skip_paths": [], "areas": []}
    if "max_nits" in b:
        if not _nonneg_int(b["max_nits"]):
            raise Unavailable(f"review_brief malformed: max_nits={b['max_nits']!r} is not an integer >= 0")
        out["max_nits"] = b["max_nits"]
    if "skip_paths" in b:
        if not isinstance(b["skip_paths"], list) or not all(isinstance(x, str) for x in b["skip_paths"]):
            raise Unavailable("review_brief malformed: skip_paths must be a list of strings")
        for g in b["skip_paths"]:
            if _match_all(g):
                raise Unavailable(f"review_brief malformed: skip glob {g!r} matches everything — "
                                  "a brief may narrow a review, never switch it off")
        out["skip_paths"] = [g.strip() for g in b["skip_paths"]]
    if "areas" in b:
        if not isinstance(b["areas"], list):
            raise Unavailable("review_brief malformed: areas must be a list")
        seen = set()
        for i, a in enumerate(b["areas"]):
            if not isinstance(a, dict):
                raise Unavailable(f"review_brief malformed: areas[{i}] is not an object")
            extra = _real_keys(a) - AREA_KEYS
            if extra:
                raise Unavailable(f"review_brief malformed: areas[{i}] unknown key(s) {sorted(extra)}")
            name = a.get("name")
            if not isinstance(name, str) or not name.strip():
                raise Unavailable(f"review_brief malformed: areas[{i}].name must be a non-empty string")
            if name.strip() in seen:
                raise Unavailable(f"review_brief malformed: duplicate area name {name!r}")
            seen.add(name.strip())
            owner = a.get("owner")
            if owner is not None and not isinstance(owner, str):
                raise Unavailable(f"review_brief malformed: areas[{i}].owner must be a string")
            out["areas"].append({
                "name": name.strip(),
                "owner": owner,
                "path_globs": _str_list(a.get("path_globs"), f"areas[{i}].path_globs", True),
                "failure_looks_like": _str_list(a.get("failure_looks_like"),
                                                f"areas[{i}].failure_looks_like", True),
                "not_worth_flagging": _str_list(a.get("not_worth_flagging", []),
                                                f"areas[{i}].not_worth_flagging", False),
            })
    return out


# ---- the diff and the scans -------------------------------------------------------------------
def git(repo, *args):
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        raise Fatal(f"git {' '.join(args)} could not run: {e}")
    if r.returncode != 0:
        raise Fatal("git %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return r.stdout


def changed_files(repo, base, override):
    if override is not None:
        return sorted({f.strip() for f in override if f.strip()})
    if not base:
        raise Fatal("cannot determine the base branch (no --base and no project.default_branch)")
    if not os.path.isdir(repo):
        raise Fatal(f"--repo {repo} is not a directory")
    mb = git(repo, "merge-base", base, "HEAD").strip()
    files = git(repo, "diff", "--name-only", mb).splitlines()
    files += git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({f.strip() for f in files if f.strip()})


def r6_hits(files, floor):
    if not files:
        return set()
    cmd = ["bash", R6SCAN, "--stdin"] + (["--globs", R6FLOOR] if floor else [])
    try:
        r = subprocess.run(cmd, input="\n".join(files) + "\n", capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        raise Unavailable(f"R6 denylist scan could not run: {e}")
    which = "framework floor" if floor else "effective list"
    if r.returncode not in (0, 3):
        raise Unavailable(f"R6 denylist scan ({which}) could not run (exit {r.returncode}): "
                          f"{r.stdout.strip()[:300]}")
    try:
        hits = json.loads(r.stdout).get("denylisted") or []
    except Exception:  # noqa: BLE001
        raise Unavailable(f"R6 denylist scan ({which}) printed no JSON")
    if r.returncode == 3 and not hits:
        raise Unavailable(f"R6 denylist scan ({which}) reported a hit without naming it")
    return {h.get("file") for h in hits if isinstance(h, dict)}


def _same_file(repo, rel, target):
    try:
        return os.path.realpath(os.path.join(repo, rel)) == os.path.realpath(target)
    except Exception:  # noqa: BLE001
        return False


# ---- the plan -----------------------------------------------------------------------------------
def compute_plan(repo, base, limits_arg, changed_override):
    stack = _config()
    base = base or _stack_get(stack, "project.default_branch")
    changed = changed_files(repo, base, changed_override)
    raw_conv, conv_path = conventions_path(stack)
    plan = {"status": "absent", "source": f"{raw_conv}#review_brief", "changed_files": changed,
            "review_files": list(changed), "skipped": [], "skip_refused": [], "max_nits": None,
            "areas": [], "warnings": []}
    try:
        with open(conv_path) as fh:
            conv = json.load(fh)
    except Exception:  # noqa: BLE001 — no readable conventions: no brief, today's behaviour
        return plan
    if not isinstance(conv, dict) or "review_brief" not in conv:
        return plan
    glob_match = load_glob_match()
    try:
        brief = validate_brief(conv["review_brief"])
        if any(_same_file(repo, f, conv_path) for f in changed):
            raise Unavailable("the diff under review changes the conventions file that holds the "
                              "brief — a change may not grant itself skip paths")
        max_nits = brief["max_nits"]
        if max_nits is None:
            max_nits = read_default_max_nits(limits_path(stack, limits_arg))
        candidates = {}
        for f in changed:
            pat = next((g for g in brief["skip_paths"] if glob_match(f, g)), None)
            if pat is not None:
                candidates[f] = pat
        cand = sorted(candidates)
        deny = r6_hits(cand, floor=False) | r6_hits(cand, floor=True)
        shared = [g for g in conv.get("shared_core_globs") or [] if isinstance(g, str) and g.strip()]
        skipped, refused = [], []
        for f in cand:
            if f in deny:
                refused.append({"file": f, "pattern": candidates[f], "reason": "blast-radius"})
            elif any(glob_match(f, g) for g in shared):
                refused.append({"file": f, "pattern": candidates[f], "reason": "shared-core"})
            else:
                skipped.append({"file": f, "pattern": candidates[f]})
    except Unavailable as e:
        plan.update(status="unavailable", error=str(e))
        return plan
    skip_set = {s["file"] for s in skipped}
    review = [f for f in changed if f not in skip_set]
    areas = []
    for a in brief["areas"]:
        files = sorted(f for f in changed if any(glob_match(f, g) for g in a["path_globs"]))
        if files:
            areas.append({"name": a["name"], "owner": a["owner"], "files": files,
                          "failure_looks_like": a["failure_looks_like"],
                          "not_worth_flagging": a["not_worth_flagging"]})
    areas.sort(key=lambda x: x["name"])
    warnings = []
    if changed and not review:
        warnings.append("all-changed-files-skipped")
    plan.update(status="applied", review_files=review, skipped=skipped, skip_refused=refused,
                max_nits=max_nits, areas=areas, warnings=warnings)
    return plan


# ---- findings, headline ------------------------------------------------------------------------
def _findings(doc):
    f = doc.get("findings")
    return [x for x in f if isinstance(x, dict)] if isinstance(f, list) else []


def _open(f):
    return f.get("resolved") is not True and f.get("withheld") is not True


def headline_of(doc):
    opened = [f for f in _findings(doc) if _open(f)]
    n = len(opened)
    if n == 0:
        return "0 findings: nothing needs fixing."
    counts = [(s, sum(1 for f in opened if f.get("severity") == s)) for s in SEVERITIES]
    parts = ", ".join(f"{c} {s}" for s, c in counts if c)
    word = "finding" if n == 1 else "findings"
    return f"{n} {word} ({parts})." if parts else f"{n} {word}."


def _open_lows(doc):
    # a human-gated finding (contract v1.8.0) needs a human decision: the nit cap never withholds it
    return [f for f in _findings(doc) if _open(f) and f.get("severity") == "low"
            and f.get("human_gate") is not True]


def _conf(f):
    c = f.get("confidence")
    return c if isinstance(c, (int, float)) and not isinstance(c, bool) else 0


def _line(f):
    ln = f.get("line")
    return ln if isinstance(ln, int) and not isinstance(ln, bool) else 0


def apply_nit_cap(doc, max_nits):
    lows = sorted(_open_lows(doc), key=lambda f: (-_conf(f), str(f.get("file") or ""), _line(f),
                                                  str(f.get("issue") or "")))
    for f in lows[max_nits:]:
        f["withheld"] = True
        f["withheld_reason"] = (f"{NIT_REASON_PREFIX}{max_nits}): a low finding beyond the code "
                                "owner's nit cap; kept here, not posted and not counted as open")


def nits_withheld(doc):
    return sum(1 for f in _findings(doc) if f.get("withheld") is True
               and str(f.get("withheld_reason") or "").startswith(NIT_REASON_PREFIX))


def lead_summary(doc, headline):
    old = doc.get("summary") if isinstance(doc.get("summary"), str) else ""
    prev = doc.get("headline")
    if isinstance(prev, str) and prev and old.startswith(prev):
        old = old[len(prev):].lstrip()
    else:
        old = HEADLINE_STRIP_RX.sub("", old, count=1)
    return headline + (" " + old if old else "")


def lead_report(path, headline):
    """The new review.md text, or None when the file does not exist (nothing is created)."""
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    lines = text.split("\n")
    if lines and HEADLINE_RX.match(lines[0]):
        lines[0] = headline
        return "\n".join(lines)
    return headline + "\n\n" + text


def atomic_write(path, text):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".review_brief.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_review(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as e:  # noqa: BLE001
        raise Fatal(f"review file unreadable ({path}): {e.__class__.__name__}: {e}")
    if not isinstance(doc, dict):
        raise Fatal(f"review file {path} is not a JSON object")
    return doc


# ---- verbs --------------------------------------------------------------------------------------
def cmd_plan(a):
    plan = compute_plan(a.repo, a.base, a.limits, a.changed_file)
    return emit(plan, 2 if plan["status"] == "unavailable" else 0)


def cmd_finalize(a):
    doc = read_review(a.review)
    plan = compute_plan(a.repo, a.base, a.limits, a.changed_file)
    if plan["status"] == "applied":
        apply_nit_cap(doc, plan["max_nits"])
        doc["brief"] = {
            "status": "applied", "source": plan["source"], "max_nits": plan["max_nits"],
            "nits_withheld": nits_withheld(doc), "skipped": plan["skipped"],
            "skip_refused": plan["skip_refused"],
            "areas": [{"name": x["name"], "files": x["files"]} for x in plan["areas"]],
        }
    elif plan["status"] == "unavailable":
        doc["brief"] = {"status": "unavailable", "source": plan["source"], "error": plan["error"]}
    else:
        doc.pop("brief", None)
    headline = headline_of(doc)
    doc["summary"] = lead_summary(doc, headline)
    doc["headline"] = headline
    report = lead_report(a.report, headline)
    atomic_write(a.review, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    if report is not None:
        atomic_write(a.report, report)
    return emit({"status": plan["status"], "headline": headline, "nits_withheld": nits_withheld(doc),
                 "skipped": plan["skipped"], "skip_refused": plan["skip_refused"],
                 "warnings": plan["warnings"] + ([f"brief unavailable: {plan['error']}"]
                                                 if plan["status"] == "unavailable" else [])}, 0)


def cmd_check(a):
    doc = read_review(a.review)
    plan = compute_plan(a.repo, a.base, a.limits, a.changed_file)
    v = []

    def add(cls, detail):
        v.append({"class": cls, "detail": detail})

    want = headline_of(doc)
    have = doc.get("headline")
    if not isinstance(have, str) or not have:
        add("headline-missing", "review.json has no headline (run review_brief.py finalize)")
    elif have != want:
        add("headline-count-wrong", f"headline {have!r} but the open findings say {want!r}")
    lead = have if isinstance(have, str) and have else want
    summary = doc.get("summary") if isinstance(doc.get("summary"), str) else ""
    if not summary.startswith(lead):
        add("summary-does-not-lead-with-count", f"summary does not start with {lead!r}")
    if a.report:
        first = None
        if os.path.isfile(a.report):
            with open(a.report, encoding="utf-8") as fh:
                first = fh.read().split("\n", 1)[0]
        if first != lead:
            add("report-does-not-lead-with-count",
                f"review.md first line is {first!r}, want {lead!r}")
    brief = doc.get("brief") if isinstance(doc.get("brief"), dict) else None
    bstatus = brief.get("status") if brief else None
    if plan["status"] == "applied" and bstatus != "applied":
        add("brief-not-applied", f"the conventions file has a valid brief but review.json brief "
                                 f"status is {bstatus!r}")
    if bstatus == "applied" and plan["status"] != "applied":
        add("brief-claimed-without-config", f"review.json says the brief was applied but the plan "
                                            f"status is {plan['status']!r}")
    if bstatus == "applied":
        claimed = {s.get("file") for s in brief.get("skipped") or [] if isinstance(s, dict)}
        refused = {s["file"] for s in plan["skip_refused"]}
        bad = sorted(claimed & refused)
        if bad:
            add("skipped-protected-path", f"skipped a path the brief may never skip: {bad}")
        if plan["status"] == "applied" and claimed != {s["file"] for s in plan["skipped"]}:
            add("skipped-set-mismatch", f"brief.skipped {sorted(claimed)} != plan "
                                        f"{sorted(s['file'] for s in plan['skipped'])}")
    lows = len(_open_lows(doc))
    caps = []
    if plan["status"] == "applied":
        caps.append(plan["max_nits"])
    if bstatus == "applied" and _nonneg_int(brief.get("max_nits")):
        caps.append(brief["max_nits"])
    if caps and lows > min(caps):
        add("nit-cap-exceeded", f"{lows} open low findings > max_nits {min(caps)}")
    if v:
        return emit({"ok": False, "plan_status": plan["status"], "violations": v}, 1)
    return emit({"ok": True, "plan_status": plan["status"], "headline": lead}, 0)


def main(argv):
    ap = argparse.ArgumentParser(prog="review_brief.py")
    sub = ap.add_subparsers(dest="verb")
    for name in ("plan", "finalize", "check"):
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".")
        p.add_argument("--base", default=None)
        p.add_argument("--limits", default=None)
        p.add_argument("--changed-file", action="append", default=None)
        if name != "plan":
            p.add_argument("--review", required=True)
            p.add_argument("--report", default=None)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return emit({"error": "bad usage"}, 2)
    if not a.verb:
        return emit({"error": "bad usage: plan | finalize | check"}, 2)
    try:
        return {"plan": cmd_plan, "finalize": cmd_finalize, "check": cmd_check}[a.verb](a)
    except Fatal as e:
        return emit({"error": str(e)}, 2)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — never crash open
        print(json.dumps({"error": f"review_brief crashed: {e.__class__.__name__}: {e}"}))
        sys.exit(2)
