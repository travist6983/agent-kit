#!/usr/bin/env python3
"""parse_test_report.py — the runner's own counts, from the report it wrote. Never "looks fine".

    python3 parse_test_report.py --format junit --glob 'web/test-results/*.xml'
    python3 parse_test_report.py --format json  --glob 'tmp/report.json' --area-regex 'src/tasks/'
    python3 parse_test_report.py --project web            # format + glob from conventions.projects[web].test_report
    python3 parse_test_report.py --project other          # repo-wide stack.test_report

Supported `test_report.format` values:
  junit   JUnit XML (<testsuites>/<testsuite>/<testcase> with <failure>/<error>/<skipped>) — what
          almost every runner can emit with one flag.
  json    a generic JSON report; the accepted shapes are in references/test-report-formats.md.

Prints ONE JSON object:

  {
    "format": "junit", "files_read": [...],
    "results": {"passed": N, "failed": N, "skipped": N, "total": N},   # total = passed + failed (skipped excluded)
    "tests":  [{"spec": "<file or suite> > <name>", "file": "...", "status": "passed|failed|skipped",
                "message": "..."}],
    "files_run": [...],                     # distinct test files/suites that executed at least one test
    "area": {"regex": "...", "files": [...], "results": {...}} | null,   # with --area-regex
    "kb_flakiness_args": [["--spec", "<spec>", "--area", "<tag>", "--source", "qa-regression", "--failed"?], ...]
                                            # one per executed test, passes included — feed each to `kb write flakiness`
  }

Exit 0 = report parsed and at least one test executed; 1 = report parsed but zero tests executed
(QA-REGRESSION-C1: that is blocked-slice-empty / blocked-environment, never green); 2 = usage or no
report file matched the glob (treat as the runner not having produced a report — investigate before
trusting any count); 3 = a report file was unreadable / malformed.

Stdlib only. It runs no tests and calls no brain; `kb_flakiness_args` is for the caller to feed
`kb write flakiness` with — one data point per test RUN, passes included, so flake_rate has a denominator.
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PASS, FAIL, SKIP = "passed", "failed", "skipped"
_STATUS_WORDS = {
    PASS: {"passed", "pass", "ok", "success", "succeeded", "passing"},
    FAIL: {"failed", "fail", "error", "errored", "broken", "failure", "failing", "timedout", "timeout"},
    SKIP: {"skipped", "skip", "pending", "todo", "disabled", "ignored", "notrun", "not_run", "excluded"},
}


def _norm_status(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip().lower().replace("-", "").replace(" ", "")
    for k, words in _STATUS_WORDS.items():
        if s in words:
            return k
    return None


# ---------------------------------------------------------------- junit
def _parse_junit(path: Path) -> List[Dict[str, Any]]:
    tree = ET.parse(str(path))
    root = tree.getroot()
    tests: List[Dict[str, Any]] = []
    for case in root.iter("testcase"):
        name = case.get("name") or "?"
        classname = case.get("classname") or ""
        file_ = case.get("file") or _suite_file(case, root) or classname or path.name
        status, message = PASS, ""
        for child in case:
            tag = child.tag.lower()
            if tag in ("failure", "error"):
                status = FAIL
                first_line = ((child.text or "").strip().splitlines() or [""])[0]
                message = (child.get("message") or first_line)[:400]
                break
            if tag == "skipped":
                status = SKIP
                message = child.get("message") or ""
                break
        prefix = file_ if file_ else classname
        spec = "%s > %s" % (prefix, name) if prefix else name
        tests.append({"spec": spec, "file": file_, "status": status, "message": message})
    return tests


def _suite_file(case: ET.Element, root: ET.Element) -> Optional[str]:
    # ElementTree has no parent pointers; find the enclosing <testsuite> by identity.
    for suite in root.iter("testsuite"):
        for child in suite.iter("testcase"):
            if child is case:
                return suite.get("file") or suite.get("name")
    return None


# ---------------------------------------------------------------- generic json
_NAME_KEYS = ("fullName", "full_name", "fullTitle", "full_title", "full_description", "title", "name",
              "description", "test", "spec", "id")
_FILE_KEYS = ("file", "path", "filePath", "file_path", "testFilePath", "location", "suite", "classname", "class")
_STATUS_KEYS = ("status", "state", "outcome", "result", "verdict")
_MSG_KEYS = ("message", "failureMessage", "failure_message", "error", "reason", "failureMessages", "exception")
_LIST_KEYS = ("tests", "testResults", "test_results", "results", "cases", "examples", "assertionResults", "items")


def _first(d: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _walk_json(node: Any, inherited_file: Optional[str], out: List[Dict[str, Any]]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_json(item, inherited_file, out)
        return
    if not isinstance(node, dict):
        return
    file_ = _first(node, _FILE_KEYS)
    file_ = str(file_) if file_ is not None else inherited_file
    nested = [node[k] for k in _LIST_KEYS if isinstance(node.get(k), list)]
    status = _norm_status(_first(node, _STATUS_KEYS))
    name = _first(node, _NAME_KEYS)
    if status is not None and name is not None and not nested:
        msg = _first(node, _MSG_KEYS)
        if isinstance(msg, list):
            msg = msg[0] if msg else ""
        if isinstance(msg, dict):  # e.g. {"exception": {"class": ..., "message": ...}}
            msg = _first(msg, ("message", "msg", "text")) or ""
        prefix = file_ or ""
        out.append({"spec": ("%s > %s" % (prefix, name)) if prefix else str(name),
                    "file": file_, "status": status, "message": str(msg or "")[:400]})
        return
    for lst in nested:
        _walk_json(lst, file_, out)


def _parse_json(path: Path) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, int]]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    tests: List[Dict[str, Any]] = []
    _walk_json(data, None, tests)
    summary = None
    if isinstance(data, dict):
        src = data.get("summary") if isinstance(data.get("summary"), dict) else data
        cand = {
            "passed": _first(src, ("passed", "numPassedTests", "num_passed", "pass")),
            "failed": _first(src, ("failed", "numFailedTests", "num_failed", "fail", "failures")),
            "total": _first(src, ("total", "numTotalTests", "num_total", "tests_total")),
        }
        if all(isinstance(v, int) for v in cand.values()):
            summary = cand  # type: ignore[assignment]
    return tests, summary


# ---------------------------------------------------------------- resolve format/glob from config
def _from_config(project: Optional[str], config_path: Optional[str]) -> Tuple[str, Optional[str], Path]:
    root_env = os.environ.get("LOOPS_REPO_ROOT")
    root = Path(root_env).expanduser().resolve() if root_env else Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root))
    from lib.adapters.config import load_config  # noqa: E402
    stack = load_config(config_path) if config_path else load_config()
    fmt, pattern = None, None
    if project and project != "other":
        conv_path = stack.resolve(stack.get("stack.conventions_file", ".claude/loops/conventions.json"))
        with open(conv_path, "r", encoding="utf-8") as fh:
            conv = json.load(fh)
        for p in conv.get("projects") or []:
            if isinstance(p, dict) and p.get("name") == project:
                rep = p.get("test_report") or {}
                fmt, pattern = rep.get("format"), rep.get("glob")
                break
        else:
            raise KeyError("no project named %r in conventions.projects[]" % project)
    rep = stack.get("stack.test_report") or {}
    fmt = fmt or rep.get("format") or "junit"
    pattern = pattern or rep.get("glob")
    cwd = stack.repo_dir() or Path.cwd()
    return str(fmt), pattern, cwd


def _summarize(tests: List[Dict[str, Any]]) -> Dict[str, int]:
    passed = sum(1 for t in tests if t["status"] == PASS)
    failed = sum(1 for t in tests if t["status"] == FAIL)
    skipped = sum(1 for t in tests if t["status"] == SKIP)
    return {"passed": passed, "failed": failed, "skipped": skipped, "total": passed + failed}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--format", choices=["junit", "json"], help="report format (default: from config, else junit)")
    ap.add_argument("--glob", dest="pattern", help="report file glob, relative to --cwd")
    ap.add_argument("--project", help="read format + glob from conventions.projects[<name>].test_report ('other' = stack.test_report)")
    ap.add_argument("--cwd", help="directory the glob is relative to (default: app.repo_dir from stack.config, else .)")
    ap.add_argument("--config", help="an alternate stack.config.json")
    ap.add_argument("--area-regex", help="regex over test file/spec; matches form the area slice")
    ap.add_argument("--area-tag", default=None, help="--area value to include in kb_flakiness_args")
    ap.add_argument("--source", default="qa-regression", help="--source value for kb_flakiness_args (provenance; default qa-regression)")
    args = ap.parse_args(argv)

    fmt, pattern, cwd = args.format, args.pattern, Path(args.cwd) if args.cwd else None
    if args.project or not (fmt and pattern):
        try:
            cfg_fmt, cfg_glob, cfg_cwd = _from_config(args.project, args.config)
        except Exception as e:  # config problems are usage errors here
            print(json.dumps({"ok": False, "error": "config: %s" % e}))
            return 2
        fmt = fmt or cfg_fmt
        pattern = pattern or cfg_glob
        cwd = cwd or cfg_cwd
    cwd = cwd or Path.cwd()
    if not pattern:
        print(json.dumps({"ok": False, "error": "no report glob: pass --glob or set test_report.glob in config"}))
        return 2

    files = sorted(globmod.glob(str(Path(cwd) / pattern), recursive=True))
    if not files:
        print(json.dumps({"ok": False, "error": "no report matched %s under %s — the runner wrote no report; do not trust any count" % (pattern, cwd),
                          "format": fmt, "glob": pattern, "cwd": str(cwd)}))
        return 2

    tests: List[Dict[str, Any]] = []
    summary_hint: Optional[Dict[str, int]] = None
    for f in files:
        try:
            if fmt == "junit":
                tests += _parse_junit(Path(f))
            else:
                t, s = _parse_json(Path(f))
                tests += t
                summary_hint = summary_hint or s
        except (ET.ParseError, json.JSONDecodeError, OSError, ValueError) as e:
            print(json.dumps({"ok": False, "error": "unreadable report %s: %s" % (f, e), "format": fmt}))
            return 3

    results = _summarize(tests)
    if not tests and summary_hint:
        # A summary-only JSON report: counts are the runner's, but no per-test rows exist.
        results = {"passed": summary_hint["passed"], "failed": summary_hint["failed"], "skipped": 0,
                   "total": summary_hint["total"]}
    files_run = sorted({t["file"] for t in tests if t["file"] and t["status"] != SKIP})

    area = None
    if args.area_regex:
        rx = re.compile(args.area_regex)
        area_tests = [t for t in tests if rx.search(t["file"] or "") or rx.search(t["spec"])]
        area = {"regex": args.area_regex,
                "files": sorted({t["file"] for t in area_tests if t["file"] and t["status"] != SKIP}),
                "results": _summarize(area_tests)}

    kb_args: List[List[str]] = []
    for t in tests:
        if t["status"] == SKIP:
            continue
        a = ["--spec", t["spec"]]
        if args.area_tag:
            a += ["--area", args.area_tag]
        a += ["--source", args.source]
        if t["status"] == FAIL:
            a.append("--failed")
        kb_args.append(a)

    out = {
        "ok": True, "format": fmt, "files_read": files, "results": results,
        "tests": tests, "files_run": files_run, "area": area,
        "summary_only": bool(not tests and summary_hint), "kb_flakiness_args": kb_args,
    }
    print(json.dumps(out, indent=2))
    return 0 if results["total"] >= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
