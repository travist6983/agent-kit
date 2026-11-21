#!/usr/bin/env python3
"""Cite checks shared by the onboarding loop: does a file-and-line cite point at real code?

Used by grade_quiz.py (answer keys and answers) and by dev-onboard-map's check_map.py (map
claims). Stdlib only. A cite is {path, line_start, line_end, symbol?} for code or {ref} for a
record pointer (commit:<sha>, pr:<n>, doc:<path>). Record refs are not resolved here: a commit
or pull request lives behind the vcs_host adapter, and a doc ref is checked as a path.

check_cite returns one of: ok, bad-path, bad-line, symbol-absent, none.
"""
import os

SYMBOL_SLACK = 3   # a symbol may sit this many lines outside the cited range (a moved decorator, a docstring)


def _safe_join(repo, rel):
    """Resolve rel under repo, refusing absolute paths and anything that climbs out."""
    if not rel or os.path.isabs(rel):
        return None
    full = os.path.realpath(os.path.join(repo, rel))
    root = os.path.realpath(repo)
    if full != root and not full.startswith(root + os.sep):
        return None
    return full


def read_lines(repo, rel, _cache={}):
    key = (os.path.realpath(repo), rel)
    if key not in _cache:
        full = _safe_join(repo, rel)
        if full is None or not os.path.isfile(full):
            _cache[key] = None
        else:
            with open(full, encoding="utf-8", errors="replace") as fh:
                _cache[key] = fh.read().splitlines()
    return _cache[key]


def check_cite(repo, cite):
    """One cite -> a check result string."""
    if not isinstance(cite, dict):
        return "bad-path"
    path = cite.get("path")
    if not path:
        ref = cite.get("ref", "")
        if ref.startswith("doc:"):
            return "ok" if read_lines(repo, ref[4:]) is not None else "bad-path"
        return "ok" if ref else "none"
    lines = read_lines(repo, path)
    if lines is None:
        return "bad-path"
    start = cite.get("line_start")
    end = cite.get("line_end", start)
    if start is None:
        return "ok" if not cite.get("symbol") else _symbol_in(lines, 1, len(lines), cite["symbol"])
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start or end > len(lines):
        return "bad-line"
    if cite.get("symbol"):
        return _symbol_in(lines, start, end, cite["symbol"])
    return "ok"


def _symbol_in(lines, start, end, symbol):
    lo = max(1, start - SYMBOL_SLACK)
    hi = min(len(lines), end + SYMBOL_SLACK)
    window = "\n".join(lines[lo - 1:hi])
    return "ok" if symbol in window else "symbol-absent"


def check_all(repo, cites):
    """Worst result over a list of cites. 'none' when the list is empty."""
    if not cites:
        return "none"
    order = ["bad-path", "bad-line", "symbol-absent", "ok"]
    results = [check_cite(repo, c) for c in cites]
    for r in order:
        if r in results:
            return r
    return "none"


def cite_key(cite):
    """A comparable identity for a code cite: (path, start, end). Record cites key on ref."""
    if cite.get("path"):
        s = cite.get("line_start")
        return (cite["path"], s, cite.get("line_end", s))
    return ("ref", cite.get("ref"), None)


def covered_by(answer_cite, claim_cites):
    """An answer cite is covered when a named claim cites the same file and overlapping lines.

    This is the blindness check: the examiner may only repeat what the map already says. A cite
    to a file no claim names, or to lines no claim covers, means it read the code.
    """
    p = answer_cite.get("path")
    if not p:
        return any(answer_cite.get("ref") and answer_cite.get("ref") == c.get("ref") for c in claim_cites)
    a0 = answer_cite.get("line_start")
    a1 = answer_cite.get("line_end", a0)
    for c in claim_cites:
        if c.get("path") != p:
            continue
        c0 = c.get("line_start")
        c1 = c.get("line_end", c0)
        if a0 is None or c0 is None:
            return True
        if a0 <= c1 and c0 <= a1:
            return True
    return False
