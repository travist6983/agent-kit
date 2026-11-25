#!/usr/bin/env python3
"""Gather harden candidates into one ranked list. Deterministic, zero-LLM, read-only.

A harden candidate is a correction that might deserve a check that fails instead of one more
line of prose. They arrive by three paths, all written by other skills or scripts, never by this
one:

  1. dev-code-review findings that carry a `harden` block (the reviewer saw a mistake a check
     could catch and drafted the check). Read from <evid_root>/*/review.json.
  2. The harden queue, .claude/loops/harden-queue.jsonl: one JSON object per line, appended by
     qa-dream when a fix lesson recurs, or by a human. Fields: fingerprint, source, ref,
     statement, and optionally band and candidate_rule.
  3. Review misses, also through the queue: collect_misses.py (this skill's own collector, run
     with --append) queues each prod-regressed run and each reviewer-eval case the reviewer missed
     as a line with source `review-miss` and a `miss` block {origin, ref, review_verdict,
     locations, had, lacked}. A review-miss line without a miss block (origin and ref strings) is
     listed under `skipped`; the block is passed through on the candidate as `miss`.

Candidates are grouped by fingerprint (the drafted rule's id, else a slug of the statement), so
the same mistake seen on three tickets is ONE candidate with occurrences=3. A candidate is
`ready` when it recurred (occurrences >= --min-occurrences, default 2), a human queued it, or it
is a review miss: one diff cannot show that a mistake keeps coming back, and a rule written for a
one-off is noise at the gate. A miss is ready at one occurrence because its evidence is not a
hunch that needs recurrence: it is a confirmed prod regression or a human-confirmed answer key. Fingerprints that already have a decided <evid_root>/harden/<fingerprint>/harden.json
are listed under `decided` and left out of `candidates`.

Usage:
  collect_candidates.py [--evid-root DIR] [--queue FILE] [--min-occurrences N]

Prints one JSON object: {ok, evid_root, queue, candidates:[...], decided:[...], skipped:[...]}.
Exit 0 always on a readable input set; 2 on bad arguments.
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FRAMEWORK_ROOT = os.environ.get("LOOPS_REPO_ROOT") or os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
DEFAULT_QUEUE = os.path.join(FRAMEWORK_ROOT, ".claude", "loops", "harden-queue.jsonl")
DECIDED = {"rule-proposed", "rule-applied", "ticket-drafted", "expectation-proposed", "guideline-proposed", "declined"}


def evid_root_default():
    try:
        sys.path.insert(0, FRAMEWORK_ROOT)
        from lib.adapters.config import load_config  # noqa: E402
        cfg = load_config()
        return str(cfg.resolve(cfg.get("evidence.root"), "runs"))
    except Exception:  # noqa: BLE001 (default only)
        return os.path.join(FRAMEWORK_ROOT, "runs")


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "unnamed"


def fingerprint(rule, statement):
    if isinstance(rule, dict) and rule.get("id"):
        return slug(rule["id"])
    return slug(statement)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--evid-root", default=None)
    ap.add_argument("--queue", default=DEFAULT_QUEUE)
    ap.add_argument("--min-occurrences", type=int, default=2)
    a = ap.parse_args()
    if a.min_occurrences < 1:
        print(json.dumps({"ok": False, "error": "--min-occurrences must be >= 1"}))
        sys.exit(2)
    root = os.path.abspath(a.evid_root or evid_root_default())
    groups, skipped = {}, []

    def add(fp, source, ref, statement, band, rule, fixtures, miss=None):
        g = groups.setdefault(fp, {"fingerprint": fp, "occurrences": 0, "sources": [], "refs": [],
                                   "statement": statement, "band": band, "candidate_rule": None,
                                   "fixtures": None, "human": False, "miss": None})
        g["occurrences"] += 1
        if source not in g["sources"]:
            g["sources"].append(source)
        if ref and ref not in g["refs"]:
            g["refs"].append(ref)
        g["human"] = g["human"] or source == "human"
        g["band"] = g["band"] or band
        if rule and not g["candidate_rule"]:
            g["candidate_rule"] = rule
        if fixtures and not g["fixtures"]:
            g["fixtures"] = fixtures
        if miss and not g["miss"]:
            g["miss"] = miss

    # 1. review.json harden blocks, one per finding
    for path in sorted(glob.glob(os.path.join(root, "*", "review.json"))):
        ticket = os.path.basename(os.path.dirname(path))
        try:
            with open(path, encoding="utf-8") as fh:
                review = json.load(fh)
        except (OSError, ValueError) as e:
            skipped.append({"file": path, "reason": f"unreadable: {e.__class__.__name__}"})
            continue
        for i, f in enumerate(review.get("findings") or []):
            h = f.get("harden") if isinstance(f, dict) else None
            if not isinstance(h, dict):
                continue
            rule = h.get("candidate_rule")
            statement = h.get("why") or f.get("issue", "")
            fixtures = None
            if h.get("must_trip") is not None and h.get("must_pass") is not None:
                p = f.get("file") or "fixture.txt"
                fixtures = {"must_trip": [{"path": p, "text": h["must_trip"]}],
                            "must_pass": [{"path": p, "text": h["must_pass"]}]}
            add(fingerprint(rule, statement), "review", f"{ticket}/review.json#findings[{i}]",
                statement, h.get("band"), rule, fixtures)

    # 2. the queue
    if os.path.isfile(a.queue):
        with open(a.queue, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    q = json.loads(line)
                except ValueError:
                    skipped.append({"file": a.queue, "line": n, "reason": "not JSON"})
                    continue
                if not isinstance(q, dict) or not (q.get("statement") or q.get("candidate_rule")):
                    skipped.append({"file": a.queue, "line": n, "reason": "no statement or candidate_rule"})
                    continue
                miss = q.get("miss")
                if q.get("source") == "review-miss" and not (
                        isinstance(miss, dict) and isinstance(miss.get("origin"), str)
                        and isinstance(miss.get("ref"), str)):
                    skipped.append({"file": a.queue, "line": n, "reason": "review-miss without a miss block"})
                    continue
                rule = q.get("candidate_rule")
                fp = slug(q["fingerprint"]) if q.get("fingerprint") else fingerprint(rule, q.get("statement"))
                add(fp, q.get("source") or "queue", q.get("ref"), q.get("statement", ""), q.get("band"), rule, None,
                    miss if isinstance(miss, dict) else None)

    # 3. drop what is already decided
    decided, candidates = [], []
    for fp, g in groups.items():
        hp = os.path.join(root, "harden", fp, "harden.json")
        status = None
        if os.path.isfile(hp):
            try:
                with open(hp, encoding="utf-8") as fh:
                    status = json.load(fh).get("status")
            except (OSError, ValueError):
                status = None
        if status in DECIDED:
            decided.append({"fingerprint": fp, "status": status, "artifact": hp})
            continue
        g["ready"] = g["human"] or "review-miss" in g["sources"] or g["occurrences"] >= a.min_occurrences
        candidates.append(g)
    candidates.sort(key=lambda g: (not g["ready"], -g["occurrences"], g["fingerprint"]))
    print(json.dumps({"ok": True, "evid_root": root, "queue": a.queue,
                      "min_occurrences": a.min_occurrences, "candidates": candidates,
                      "decided": sorted(decided, key=lambda d: d["fingerprint"]), "skipped": skipped}, indent=2))


if __name__ == "__main__":
    main()
