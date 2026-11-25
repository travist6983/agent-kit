#!/usr/bin/env python3
"""Turn review MISSES into harden-queue entries. Deterministic, zero-LLM, offline, read-only unless --append.

A review miss is a bug the review let through that something later confirmed. When a review
misses something, the question is what the reviewer did not have in front of it, and the answer
belongs in a check, the context, an expectation or an instruction, never only in the moment.
This script does the mechanical half: it finds the misses and queues them, carrying the facts of
what the reviewer HAD and an empty `lacked` slot that dev-harden fills by judgment (validate.py
DEV-HARDEN-M1 refuses a harden.json for a miss until it does). Two sources:

  1. Prod: every <evid_root>/<KEY>/prod-verify.json with verdict `prod-regressed` (written by
     infra-prod-verify). One entry per run, whatever the review said, and also when no review ran
     (then miss.review_verdict is null and had.reviewed is false). Other verdicts, `inconclusive`
     included, are not misses. An unreadable prod-verify.json is listed under `skipped`.
  2. Eval: each --eval-score FILE, the output of dev-code-review's `reviewer_eval.py score --out`.
     It must validate against contracts/dev-code-review.eval-score.json; a missing, unreadable or
     invalid file is exit 2, never skipped (an input someone named that cannot be trusted). Each
     bad case with result `missed` or `partial` is one entry whose miss.locations are the bugs it
     did not locate. `no-review` / `invalid-review` cases go to `not_scored` (the reviewer never sat
     them; that is not a miss) and false-flagged clean cases to `false_flags` (reported, not queued).

Each entry is one harden-queue line: {fingerprint, source: "review-miss", ref, statement, miss}.
Fingerprints are `review-miss-prod-<key>` and `review-miss-eval-<case id>`, slugged with the same
slug() collect_candidates.py uses, so the two scripts cannot drift. Dedupe: two refs on one
fingerprint in a run -> the second is `skipped` (fingerprint-collision); a fingerprint already in
the queue -> `already_queued`; an eval case whose <evid_root>/harden/<fp>/harden.json is decided ->
`still_missed_after_decision` for a human, never re-queued.

Usage:
  collect_misses.py [--evid-root DIR] [--eval-score FILE ...] [--queue FILE] [--append]

Prints one JSON object: {ok, evid_root, queue, appended, entries:[...], already_queued:[...],
still_missed_after_decision:[...], false_flags:[...], not_scored:[...], skipped:[...]}. Without
--append nothing is written. With --append each new entry is appended to the queue as one
sorted-key JSON line, in a single write (idempotent: a second run appends nothing).
Exit 0 on a readable input set; 2 on bad arguments or an invalid --eval-score.
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KIT_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(KIT_ROOT, "contracts"))
from collect_candidates import DECIDED, DEFAULT_QUEUE, evid_root_default, slug  # noqa: E402
import validate  # noqa: E402  (contracts/validate.py, stdlib only)

SCORE_CONTRACT = os.path.join(KIT_ROOT, "contracts", "dev-code-review.eval-score.json")
REASON_MAX = 200


def die(msg):
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(2)


def read_json(path):
    """(doc, None) or (None, reason). Never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except (OSError, ValueError) as e:
        return None, f"unreadable: {e.__class__.__name__}"


def as_data(text):
    """Script output from another skill, still treated as data: one line, bounded."""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:REASON_MAX]


def prod_entries(root, skipped):
    out = []
    for path in sorted(glob.glob(os.path.join(root, "*", "prod-verify.json"))):
        run = os.path.dirname(path)
        key = os.path.basename(run)
        pv, err = read_json(path)
        if err or not isinstance(pv, dict):
            skipped.append({"file": path, "reason": err or "not a JSON object"})
            continue
        if pv.get("verdict") != "prod-regressed":
            continue
        review, _ = read_json(os.path.join(run, "review.json"))
        review = review if isinstance(review, dict) else None
        verdict = review.get("verdict") if review else None
        findings = review.get("findings") if review else None
        brief = review.get("brief") if review else None
        ref = f"{key}/prod-verify.json"
        out.append({
            "fingerprint": slug("review-miss-prod-" + key),
            "source": "review-miss",
            "ref": ref,
            "statement": f"{key} regressed in prod after review verdict {verdict or 'no review ran'}: "
                         f"{as_data(pv.get('reason'))}",
            "miss": {
                "origin": "prod-regressed",
                "ref": ref,
                "review_verdict": verdict if isinstance(verdict, str) else None,
                "locations": [],
                "had": {
                    "reviewed": review is not None,
                    "review_findings": len(findings) if isinstance(findings, list) else 0,
                    "masking": review.get("masking") if review else None,
                    "context_pack": os.path.isfile(os.path.join(run, "context.json")),
                    "brief_status": brief.get("status") if isinstance(brief, dict) else None,
                    "evidence": sorted(n for n in os.listdir(run) if os.path.isfile(os.path.join(run, n))),
                },
                "lacked": None,
            },
        })
    return out


def eval_entries(path, schema, false_flags, not_scored):
    if not os.path.isfile(path):
        die(f"--eval-score not found: {path}")
    doc, err = read_json(path)
    if err:
        die(f"--eval-score {path} is {err}")
    errs = validate.validate(schema, doc)
    if errs:
        die(f"--eval-score {path} fails contracts/dev-code-review.eval-score.json: {errs[:5]}")
    sha = str(doc["case_set"]["sha256"])
    out = []
    for c in doc["cases"]:
        cid, kind, result = c["id"], c["kind"], c["result"]
        ref = f"eval:{cid}@{sha[:12]}"
        if result in ("no-review", "invalid-review"):
            not_scored.append({"id": cid, "kind": kind, "result": result, "ref": ref})
            continue
        if kind == "clean" and result == "false-flagged":
            false_flags.append({"id": cid, "ref": ref, "verdict": c.get("verdict")})
            continue
        if kind != "bad" or result not in ("missed", "partial"):
            continue
        bugs = [b for b in (c.get("bugs") or []) if isinstance(b, dict)]
        uncaught = [b for b in bugs if not b.get("caught")]
        classes = ", ".join(sorted({str(b.get("class")) for b in uncaught})) or "no class"
        how = "missed" if result == "missed" else "partially caught"
        out.append({
            "fingerprint": slug("review-miss-eval-" + cid),
            "source": "review-miss",
            "ref": ref,
            "statement": f"Reviewer eval case {cid} was {how}: {len(uncaught)} of {len(bugs)} "
                         f"confirmed bug(s) not located ({classes})",
            "miss": {
                "origin": "eval-missed" if result == "missed" else "eval-partial",
                "ref": ref,
                "review_verdict": c.get("verdict"),
                "locations": [{"file": b["file"], "line_start": b["line_start"],
                               "line_end": b["line_end"], "class": b["class"]} for b in uncaught],
                "had": {
                    "masking": c.get("masking"),
                    "unmatched_findings": c.get("unmatched_findings", 0),
                    "unlocatable_findings": c.get("unlocatable_findings", 0),
                    "case_set_sha256": sha,
                    "score_status": doc["status"],
                },
                "lacked": None,
            },
        })
    return out


def queued_fingerprints(queue):
    seen = set()
    if not os.path.isfile(queue):
        return seen
    with open(queue, encoding="utf-8") as fh:
        for line in fh:
            try:
                q = json.loads(line)
            except ValueError:
                continue
            if isinstance(q, dict) and q.get("fingerprint"):
                seen.add(slug(str(q["fingerprint"])))
    return seen


def decided_status(root, fp):
    doc, _ = read_json(os.path.join(root, "harden", fp, "harden.json"))
    status = doc.get("status") if isinstance(doc, dict) else None
    return status if status in DECIDED else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--evid-root", default=None)
    ap.add_argument("--eval-score", action="append", default=[])
    ap.add_argument("--queue", default=DEFAULT_QUEUE)
    ap.add_argument("--append", action="store_true")
    a = ap.parse_args()
    root = os.path.abspath(a.evid_root or evid_root_default())
    queue = os.path.abspath(a.queue)
    skipped, false_flags, not_scored = [], [], []

    found = prod_entries(root, skipped)
    if a.eval_score:
        schema, err = read_json(SCORE_CONTRACT)
        if err:
            die(f"cannot read {SCORE_CONTRACT}: {err}")
        for path in a.eval_score:
            found += eval_entries(path, schema, false_flags, not_scored)

    queued = queued_fingerprints(queue)
    by_fp, entries, already, still = {}, [], [], []
    for e in found:
        fp = e["fingerprint"]
        if fp in by_fp:
            if by_fp[fp] != e["ref"]:
                skipped.append({"ref": e["ref"], "fingerprint": fp, "reason": "fingerprint-collision",
                                "kept": by_fp[fp]})
            continue
        by_fp[fp] = e["ref"]
        if e["miss"]["origin"].startswith("eval-"):
            status = decided_status(root, fp)
            if status:
                still.append({"fingerprint": fp, "status": status,
                              "artifact": os.path.join(root, "harden", fp, "harden.json")})
                continue
        if fp in queued:
            already.append({"fingerprint": fp, "ref": e["ref"]})
            continue
        entries.append(e)
    entries.sort(key=lambda e: e["fingerprint"])

    appended = False
    if a.append and entries:
        os.makedirs(os.path.dirname(queue) or ".", exist_ok=True)
        lead = ""
        if os.path.isfile(queue) and os.path.getsize(queue) > 0:
            with open(queue, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                lead = "" if fh.read(1) == b"\n" else "\n"
        with open(queue, "a", encoding="utf-8") as fh:
            fh.write(lead + "".join(json.dumps(e, sort_keys=True) + "\n" for e in entries))
        appended = True

    print(json.dumps({"ok": True, "evid_root": root, "queue": queue, "appended": appended,
                      "entries": entries,
                      "already_queued": sorted(already, key=lambda d: d["fingerprint"]),
                      "still_missed_after_decision": sorted(still, key=lambda d: d["fingerprint"]),
                      "false_flags": false_flags, "not_scored": not_scored, "skipped": skipped},
                     indent=2))


if __name__ == "__main__":
    main()
