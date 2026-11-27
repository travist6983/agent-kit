#!/usr/bin/env python3
"""reviewer_eval.py: the dev-code-review eval set, checked, materialized and scored with no model.

Past incidents are an eval the reviewer is scored against; known-clean diffs count the harmful
false flags. A case lives at evals/cases/<id>/ as:

  case.json    the answer key (contracts/dev-code-review.eval-case.json). NEVER shown to the reviewer.
  change.diff  the candidate diff (a real `git diff` output).
  base/        the files the diff modifies, before the change.
  fix.json     optional dev-fix receipt the reviewer reads (contracts/dev-fix.json).

Subcommands (one JSON document on stdout per invocation; stdlib only; offline):

  check        [--cases DIR ...]
      The set is well-formed: every case validates (incl. DEV-REVIEW-EVAL-K1), id == dir name, the
      diff parses and adds a line, every modified file exists under base/, every confirmed bug range
      sits on a line the diff ADDS, fix.json validates; the set has >= 1 bad and >= 1 clean case.
      exit 0 ok · 1 invalid · 2 usage.

  materialize  --case ID --out DIR [--cases DIR ...]
      Build a throwaway git repo at DIR/repo (branch main = base/, branch eval-<id> = base + diff)
      and DIR/runs/<ticket>/fix.json, so the reviewer can be run on it. The answer key is never
      copied. exit 0 · 2 (unknown case, non-empty DIR, DIR inside a cases dir, diff does not apply).

  score        --reviews DIR [--cases DIR ...] [--limits FILE] [--out FILE]
      Read DIR/<id>/review.json per case and score: recall over confirmed bugs, false-flag rate over
      clean cases, against loop-limits.json `reviewer_eval`. Output validates against
      contracts/dev-code-review.eval-score.json and is byte-identical across runs.
      exit 0 pass · 1 below a bar · 2 incomplete (a case without a valid review) or bad input.

  candidates   --evidence-root DIR [--cases DIR ...]
      Read-only: list run keys that are candidates for new cases (prod-regressed, escalated,
      re-fixed) and are not already a case's source.ref. Writes nothing, promotes nothing.

--cases is repeatable and, when given, REPLACES the default (<skill>/evals/cases): an adopter
passes the kit's directory plus their own.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(SKILL)))
CONTRACTS = os.path.join(REPO, "contracts")
DEFAULT_CASES = os.path.join(SKILL, "evals", "cases")
DEFAULT_LIMITS = os.path.join(REPO, ".claude", "loops", "loop-limits.json")

sys.path.insert(0, CONTRACTS)
import validate  # noqa: E402  (contracts/validate.py, stdlib only)

SEVERE = ("critical", "high")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def emit(obj, code, out=None):
    text = json.dumps(obj, indent=2, sort_keys=True) + "\n"
    if out:
        with open(out, "w") as f:
            f.write(text)
    sys.stdout.write(text)
    return code


def load_json(path):
    with open(path) as f:
        return json.load(f)


def schema(name):
    return load_json(os.path.join(CONTRACTS, name))


def norm_path(p):
    p = str(p or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _strip_side(p):
    p = p.split("\t", 1)[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return norm_path(p)


def parse_diff(diff_text):
    """-> (added {path: set(new-file line numbers)}, files [{"old": path|None, "new": path|None}]).

    Hunk bodies are consumed by their header counts, so a removed line that starts with '--- ' is
    never mistaken for a file header."""
    added, files = {}, []
    lines = diff_text.splitlines()
    i, cur = 0, None
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old, new = _strip_side(ln[4:]), _strip_side(lines[i + 1][4:])
            cur = {"old": old, "new": new}
            files.append(cur)
            i += 2
            continue
        m = _HUNK.match(ln)
        if m and cur is not None:
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            n = int(m.group(3))
            i += 1
            while i < len(lines) and (old_left > 0 or new_left > 0):
                body = lines[i]
                if body.startswith("\\"):
                    i += 1
                    continue
                tag = body[:1]
                if tag == "+":
                    if cur["new"]:
                        added.setdefault(cur["new"], set()).add(n)
                    n += 1
                    new_left -= 1
                elif tag == "-":
                    old_left -= 1
                elif tag == " " or body == "":
                    n += 1
                    new_left -= 1
                    old_left -= 1
                else:
                    break
                i += 1
            continue
        i += 1
    return added, files


def case_dirs(dirs):
    return [os.path.abspath(d) for d in (dirs or [DEFAULT_CASES])]


def load_cases(dirs):
    """-> (cases [{id, dir, doc|None, load_error|None}], set-level errors). Sorted by id."""
    out, errs, seen = [], [], {}
    for d in case_dirs(dirs):
        if not os.path.isdir(d):
            errs.append("<set>: cases dir not found: %s" % d)
            continue
        for name in sorted(os.listdir(d)):
            cdir = os.path.join(d, name)
            if not os.path.isdir(cdir):
                continue
            if name in seen:
                errs.append("%s: duplicate case id (in %s and %s)" % (name, seen[name], d))
                continue
            seen[name] = d
            entry = {"id": name, "dir": cdir, "doc": None, "load_error": None}
            p = os.path.join(cdir, "case.json")
            if not os.path.isfile(p):
                entry["load_error"] = "missing case.json"
            else:
                try:
                    entry["doc"] = load_json(p)
                except (OSError, ValueError) as e:
                    entry["load_error"] = "case.json unreadable: %s" % e
            out.append(entry)
    out.sort(key=lambda c: c["id"])
    return out, errs


def check_case(c, case_schema, fix_schema):
    """Every problem with one case, as '<id>: <what>' strings."""
    cid, errs = c["id"], []
    if c["load_error"]:
        return ["%s: %s" % (cid, c["load_error"])]
    doc = c["doc"]
    for e in validate.validate(case_schema, doc):
        errs.append("%s: case.json %s" % (cid, e))
    if isinstance(doc, dict) and doc.get("id") != cid:
        errs.append("%s: case.json id %r != directory name" % (cid, doc.get("id")))
    dpath = os.path.join(c["dir"], "change.diff")
    if not os.path.isfile(dpath):
        errs.append("%s: missing change.diff" % cid)
        return errs
    try:
        with open(dpath) as f:
            added, files = parse_diff(f.read())
    except (OSError, UnicodeDecodeError) as e:
        errs.append("%s: change.diff unreadable: %s" % (cid, e))
        return errs
    if not files or not any(added.values()):
        errs.append("%s: change.diff adds no line" % cid)
    base = os.path.join(c["dir"], "base")
    for f in files:
        if f["old"] and not os.path.isfile(os.path.join(base, f["old"])):
            errs.append("%s: diff modifies %s but base/%s does not exist" % (cid, f["old"], f["old"]))
    if isinstance(doc, dict) and doc.get("kind") == "bad":
        for i, b in enumerate(doc.get("bugs") or []):
            if not isinstance(b, dict):
                continue
            bf = norm_path(b.get("file"))
            ls, le = b.get("line_start"), b.get("line_end")
            if bf not in added:
                errs.append("%s: bugs[%d] file %s is not added to by change.diff" % (cid, i, bf))
            elif isinstance(ls, int) and isinstance(le, int) and \
                    not any(ls <= n <= le for n in added[bf]):
                errs.append("%s: bugs[%d] %s:%s-%s is not on a line change.diff adds"
                            % (cid, i, bf, ls, le))
    fpath = os.path.join(c["dir"], "fix.json")
    if os.path.exists(fpath):
        try:
            for e in validate.validate(fix_schema, load_json(fpath)):
                errs.append("%s: fix.json %s" % (cid, e))
        except (OSError, ValueError) as e:
            errs.append("%s: fix.json unreadable: %s" % (cid, e))
    return errs


def check_set(dirs):
    cases, errs = load_cases(dirs)
    cs, fs = schema("dev-code-review.eval-case.json"), schema("dev-fix.json")
    for c in cases:
        errs.extend(check_case(c, cs, fs))
    kinds = [c["doc"].get("kind") for c in cases if isinstance(c["doc"], dict)]
    if kinds.count("bad") < 1:
        errs.append("<set>: needs at least one bad case")
    if kinds.count("clean") < 1:
        errs.append("<set>: needs at least one clean case")
    return cases, errs, kinds


# ── check ────────────────────────────────────────────────────────────────────
def cmd_check(a):
    cases, errs, kinds = check_set(a.cases)
    return emit({"status": "invalid" if errs else "ok", "cases": len(cases),
                 "bad": kinds.count("bad"), "clean": kinds.count("clean"), "errors": errs},
                1 if errs else 0)


# ── materialize ──────────────────────────────────────────────────────────────
def _inside(path, parent):
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    return path == parent or path.startswith(parent + os.sep)


def _git(repo, *args):
    cmd = ["git", "-C", repo, "-c", "user.name=reviewer-eval",
           "-c", "user.email=reviewer-eval@example.com", "-c", "commit.gpgsign=false",
           "-c", "core.hooksPath=/dev/null"] + list(args)
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def cmd_materialize(a):
    def fail(msg):
        return emit({"status": "error", "error": msg}, 2)
    cases, _ = load_cases(a.cases)
    c = next((x for x in cases if x["id"] == a.case), None)
    if c is None or not isinstance(c["doc"], dict):
        return fail("unknown case: %s" % a.case)
    out = os.path.abspath(a.out)
    for d in case_dirs(a.cases):
        if _inside(out, d):
            return fail("--out must not be inside a cases dir (the answer keys): %s" % d)
    if os.path.exists(out) and (not os.path.isdir(out) or os.listdir(out)):
        return fail("--out exists and is not an empty directory: %s" % out)
    diff = os.path.join(c["dir"], "change.diff")
    if not os.path.isfile(diff):
        return fail("%s: missing change.diff" % a.case)
    ticket = str(c["doc"].get("ticket") or "")
    if not ticket or "/" in ticket or ticket.startswith("."):
        return fail("%s: case.json ticket is not a usable key: %r" % (a.case, ticket))
    repo, runs = os.path.join(out, "repo"), os.path.join(out, "runs")
    os.makedirs(repo)
    r = _git(repo, "init", "-q", "-b", "main")
    if r.returncode != 0:
        return fail("git init failed: %s" % r.stderr.decode("utf-8", "replace").strip())
    base = os.path.join(c["dir"], "base")
    if os.path.isdir(base):
        shutil.copytree(base, repo, dirs_exist_ok=True)
    head = "eval-%s" % a.case
    for args in (("add", "-A"), ("commit", "-q", "--allow-empty", "-m", "base"),
                 ("checkout", "-q", "-b", head),
                 ("apply", "--whitespace=nowarn", os.path.abspath(diff)),
                 ("add", "-A"), ("commit", "-q", "-m", "candidate")):
        r = _git(repo, *args)
        if r.returncode != 0:
            return fail("git %s failed: %s" % (args[0], r.stderr.decode("utf-8", "replace").strip()))
    os.makedirs(os.path.join(runs, ticket))
    fix = os.path.join(c["dir"], "fix.json")
    if os.path.isfile(fix):
        shutil.copyfile(fix, os.path.join(runs, ticket, "fix.json"))
    return emit({"case": a.case, "repo": repo, "base": "main", "head": head,
                 "evid_root": runs, "key": ticket}, 0)


# ── score ────────────────────────────────────────────────────────────────────
def read_limits(path):
    try:
        d = load_json(path)
    except (OSError, ValueError) as e:
        return None, "limits file unreadable: %s" % e
    blk = d.get("reviewer_eval") if isinstance(d, dict) else None
    if not isinstance(blk, dict):
        return None, "limits file has no reviewer_eval block: %s" % path
    out = {}
    for k in ("min_recall", "max_false_flag_rate"):
        v = blk.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None, "reviewer_eval.%s is not a number: %r" % (k, v)
        out[k] = float(v)
    v = blk.get("line_tolerance")
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        return None, "reviewer_eval.line_tolerance is not a non-negative integer: %r" % (v,)
    out["line_tolerance"] = v
    return out, None


def _is_line(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _r4(x):
    return None if x is None else round(float(x), 4)


def score_bad(doc, review, tol):
    bugs_out, findings = [], [f for f in (review.get("findings") or []) if isinstance(f, dict)]
    located = set()
    reasons = []
    for b in doc.get("bugs") or []:
        bf, ls, le = norm_path(b["file"]), b["line_start"], b["line_end"]
        hits, gated_hit = [], False
        for idx, f in enumerate(findings):
            ln = f.get("line")
            if norm_path(f.get("file")) == bf and _is_line(ln) and ls - tol <= ln <= le + tol:
                hits.append(ln)
                located.add(idx)
                gated_hit = gated_hit or f.get("human_gate") is True
        # a masking bug is caught when the judge called masking OR (contract v1.8.0) the located
        # finding is human-gated: the validators split, the loop routed it to a human, nothing
        # approved it. A split is not a miss; a dropped or approved mask is.
        caught = bool(hits) and (b["class"] != "masking" or review.get("masking") is True
                                 or (gated_hit and review.get("verdict") == "needs-human"))
        if hits and not caught:
            reasons.append("masking-bug-located-but-masking-false: %s:%d" % (bf, ls))
        matched = None
        if caught:
            dist = lambda n: 0 if ls <= n <= le else (ls - n if n < ls else n - le)  # noqa: E731
            matched = sorted(hits, key=lambda n: (dist(n), n))[0]
        bugs_out.append({"file": bf, "line_start": ls, "line_end": le, "class": b["class"],
                         "caught": caught, "matched_line": matched})
    unlocatable = sum(1 for f in findings if not _is_line(f.get("line")))
    unmatched = sum(1 for i, f in enumerate(findings) if _is_line(f.get("line")) and i not in located)
    n = sum(1 for b in bugs_out if b["caught"])
    result = "caught" if n == len(bugs_out) and n else ("partial" if n else "missed")
    if n and review.get("verdict") == "approve":
        reasons.append("caught-but-approved")
    return result, bugs_out, unmatched, unlocatable, reasons


def score_clean(review):
    findings = [f for f in (review.get("findings") or []) if isinstance(f, dict)]
    reasons = []
    if review.get("verdict") == "request-changes":
        reasons.append("verdict request-changes on a clean diff")
    if review.get("verdict") == "needs-human":
        # a human gate on a clean diff spends a person's time on nothing: a false flag
        reasons.append("verdict needs-human on a clean diff")
    if review.get("masking") is True:
        reasons.append("masking=true on a clean diff")
    severe = [f for f in findings if f.get("severity") in SEVERE and f.get("resolved") is not True]
    if severe:
        reasons.append("%d unresolved critical/high finding(s) on a clean diff" % len(severe))
    unlocatable = sum(1 for f in findings if not _is_line(f.get("line")))
    return ("false-flagged" if reasons else "clean"), len(findings), unlocatable, reasons


def case_set_hash(cases):
    h = hashlib.sha256()
    for c in cases:
        h.update(c["id"].encode() + b"\0")
        with open(os.path.join(c["dir"], "case.json"), "rb") as f:
            h.update(f.read())
        h.update(b"\0")
        with open(os.path.join(c["dir"], "change.diff"), "rb") as f:
            h.update(f.read())
    return h.hexdigest()


def cmd_score(a):
    def fail(obj):
        return emit(obj, 2)
    limits, err = read_limits(a.limits or DEFAULT_LIMITS)
    if err:
        return fail({"status": "error", "error": err})
    if not os.path.isdir(a.reviews):
        return fail({"status": "error", "error": "--reviews is not a directory: %s" % a.reviews})
    cases, errs, _ = check_set(a.cases)
    if errs:
        return fail({"status": "error", "error": "invalid case set", "errors": errs})
    tol = limits["line_tolerance"]
    review_schema = schema("dev-code-review.json")
    rows, reasons = [], []
    bad = {"cases": 0, "bugs": 0, "caught": 0, "missed": 0}
    clean = {"cases": 0, "false_flagged": 0, "noise_findings": 0}
    per_class, clean_scored = {}, 0
    for c in cases:
        doc, cid = c["doc"], c["id"]
        kind = doc["kind"]
        row = {"id": cid, "kind": kind, "verdict": None, "masking": None,
               "unlocatable_findings": 0, "reasons": []}
        rpath = os.path.join(a.reviews, cid, "review.json")
        review = None
        if not os.path.isfile(rpath):
            row["result"] = "no-review"
            row["reasons"].append("no review.json at %s/review.json" % cid)
        else:
            try:
                review = load_json(rpath)
                verr = validate.validate(review_schema, review)
            except (OSError, ValueError) as e:
                review, verr = None, ["unparsable: %s" % e]
            if verr:
                review = None
                row["result"] = "invalid-review"
                row["reasons"].extend(verr)
        if review is not None:
            row["verdict"], row["masking"] = review.get("verdict"), review.get("masking")
        if kind == "bad":
            bad["cases"] += 1
            if review is not None:
                res, bugs_out, unm, unl, rs = score_bad(doc, review, tol)
                row.update(result=res, bugs=bugs_out, unmatched_findings=unm,
                           unlocatable_findings=unl)
                row["reasons"].extend(rs)
            else:
                row["bugs"] = [{"file": norm_path(b["file"]), "line_start": b["line_start"],
                                "line_end": b["line_end"], "class": b["class"], "caught": False,
                                "matched_line": None} for b in doc["bugs"]]
                row["unmatched_findings"] = 0
            for b in row["bugs"]:
                bad["bugs"] += 1
                pc = per_class.setdefault(b["class"], {"class": b["class"], "bugs": 0, "caught": 0})
                pc["bugs"] += 1
                if b["caught"]:
                    bad["caught"] += 1
                    pc["caught"] += 1
                else:
                    bad["missed"] += 1
        else:
            clean["cases"] += 1
            if review is not None:
                res, nf, unl, rs = score_clean(review)
                row.update(result=res, unlocatable_findings=unl)
                row["reasons"].extend(rs)
                clean["noise_findings"] += nf
                clean_scored += 1
                if res == "false-flagged":
                    clean["false_flagged"] += 1
        if row["result"] in ("no-review", "invalid-review"):
            reasons.append("%s: %s" % (row["result"], cid))
        rows.append(row)
    recall = _r4(bad["caught"] / bad["bugs"]) if bad["bugs"] else None
    ffr = _r4(clean["false_flagged"] / clean_scored) if clean_scored else None
    if not bad["bugs"]:
        reasons.append("no confirmed bugs to score recall on")
    if not clean_scored:
        reasons.append("no clean case with a valid review to score false flags on")
    if reasons:
        status = "incomplete"
    else:
        if recall < limits["min_recall"]:
            reasons.append("recall %s < min_recall %s" % (recall, limits["min_recall"]))
        if ffr > limits["max_false_flag_rate"]:
            reasons.append("false_flag_rate %s > max_false_flag_rate %s"
                           % (ffr, limits["max_false_flag_rate"]))
        status = "fail" if reasons else "pass"
    obj = {"status": status, "recall": recall, "false_flag_rate": ffr, "bad": bad, "clean": clean,
           "thresholds": {"min_recall": limits["min_recall"],
                          "max_false_flag_rate": limits["max_false_flag_rate"],
                          "line_tolerance": tol, "source": "loop-limits.json reviewer_eval"},
           "case_set": {"count": len(cases), "sha256": case_set_hash(cases)},
           "per_class": [per_class[k] for k in sorted(per_class)],
           "cases": rows, "reasons": reasons}
    verr = validate.validate(schema("dev-code-review.eval-score.json"), obj)
    if verr:
        return fail({"status": "error", "error": "scorer produced a contract-invalid score",
                     "errors": verr})
    return emit(obj, {"pass": 0, "fail": 1, "incomplete": 2}[status], a.out)


# ── candidates ───────────────────────────────────────────────────────────────
def _try_json(path):
    try:
        d = load_json(path)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _attempt_gt1(d):
    return isinstance(d, dict) and _is_line(d.get("attempt")) and d["attempt"] > 1


def cmd_candidates(a):
    root = a.evidence_root
    if not os.path.isdir(root):
        return emit({"status": "error", "error": "--evidence-root is not a directory: %s" % root}, 2)
    cases, _ = load_cases(a.cases)
    refs = {str((c["doc"].get("source") or {}).get("ref") or "")
            for c in cases if isinstance(c["doc"], dict)} - {""}
    cands, already = [], []
    for key in sorted(os.listdir(root)):
        kdir = os.path.join(root, key)
        if not os.path.isdir(kdir):
            continue
        origins = []
        pv = _try_json(os.path.join(kdir, "prod-verify.json"))
        if pv and pv.get("verdict") == "prod-regressed":
            origins.append("prod-regressed")
        if os.path.isfile(os.path.join(kdir, "escalate.json")):
            origins.append("escalation")
        rv = _try_json(os.path.join(kdir, "review.json"))
        fx = _try_json(os.path.join(kdir, "fix.json"))
        refix = _attempt_gt1(rv) or _attempt_gt1(fx) or any(
            isinstance(f, dict) and f.get("resolved") is True for f in ((rv or {}).get("findings") or []))
        if refix:
            origins.append("re-fix")
        if not origins:
            continue
        if key in refs:
            already.append(key)
            continue
        cands.append({"key": key, "origins": origins,
                      "review_verdict": (rv or {}).get("verdict") if rv else None,
                      "evidence": sorted(n for n in os.listdir(kdir)
                                         if os.path.isfile(os.path.join(kdir, n)))})
    return emit({"candidates": cands, "already_cases": already}, 0)


def main(argv=None):
    p = argparse.ArgumentParser(prog="reviewer_eval.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd")
    for name in ("check", "materialize", "score", "candidates"):
        s = sub.add_parser(name)
        s.add_argument("--cases", action="append", default=None, metavar="DIR")
        if name == "materialize":
            s.add_argument("--case", required=True)
            s.add_argument("--out", required=True)
        if name == "score":
            s.add_argument("--reviews", required=True)
            s.add_argument("--limits", default=None)
            s.add_argument("--out", default=None)
        if name == "candidates":
            s.add_argument("--evidence-root", required=True)
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if not a.cmd:
        p.print_usage(sys.stderr)
        return 2
    return {"check": cmd_check, "materialize": cmd_materialize, "score": cmd_score,
            "candidates": cmd_candidates}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
