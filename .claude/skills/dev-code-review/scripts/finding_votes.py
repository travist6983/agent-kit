#!/usr/bin/env python3
"""dev-code-review's independent finding validation: deterministic counting, zero model calls,
no network, no brain.

WHY THIS EXISTS. A producer never grades its own work. The specialists PRODUCE candidate findings;
deciding which of them are real used to happen in the same context that writes the verdict, so the
context that wanted a clean (or a damning) review also filtered the evidence. Now N fresh Agent
validators vote on every candidate, each in its own context, and THIS script does the counting. The
orchestrating reviewer merges specialist output into candidates.json without judging it, runs
`ballot`, spawns the validators, and runs `tally`. It never votes.

The numbers live in .claude/loops/loop-limits.json `review_validation` {validators, quorum,
confidence_floor}. There is no default here: a missing or invalid block is exit 2 and no review.

Working files (intra-skill run evidence, validated by this script, not seam contracts), in a
caller-chosen dir, by convention <EVID_ROOT>/<KEY>/review-validation/:
  candidates.json  {"findings": [<specialist finding objects, unvalidated>]}  written by the reviewer
  ballot.json      what the validators see (written by `ballot`)
  votes/v<i>.json  one per validator: {"validator": "v<i>", "votes": [{"finding_id",
                   "verdict": confirm|reject|pre-existing, "confidence": <int 0-100>, "reason",
                   "serious"?: <bool>}]}
Nothing reads these to suppress a later finding: they are evidence of one run, not a store.

Verbs (each prints ONE JSON object on stdout):
  ballot --dir DIR [--limits PATH]
         ids (F-<sha1[:10]> of dimension|file|line|bug_class|normalized issue), dedup (same id;
         or same file + integer line + bug_class other than `other`: highest severity, then
         confidence, then smallest id), rewrites candidates.json with finding_id, writes ballot.json
         holding only candidates at or above the floor, with the specialist's confidence,
         suggestion, fingerprint and harden stripped so a validator forms its own number.
         exit 0 · exit 2 bad input / limits
  tally  --dir DIR --review DRAFT [--limits PATH] [--derive-verdict]
         counts the returned ballots. An ordinary finding is kept iff the specialist confidence >=
         floor AND >= quorum returned ballots vote confirm at confidence >= floor. A PROTECTED
         finding (see below) is dropped only by a unanimous reject; a unanimous confirm keeps it as
         an ordinary finding; anything else keeps it with human_gate: true. Rewrites DRAFT
         atomically: findings (kept, plus earlier resolved:true ones), critical, high, masking,
         human_gate, validation; pops headline + brief (review_brief.py finalize rewrites them).
         Fewer than quorum returned ballots on a non-empty ballot: exit 2
         `validation-unavailable`, DRAFT untouched. --derive-verdict (eval replay only) also sets
         the strict verdict: needs-human when a finding is human-gated, else request-changes on a
         kept masking / critical / high finding, else approve.
         exit 0 · exit 2 bad input / limits / validation-unavailable
  check  --dir DIR --review review.json [--limits PATH]
         read-only re-derivation: exit 0 consistent · 1 {"ok": false, "violations": [...]} ·
         2 uncheckable (the same conditions as tally)

--limits defaults to stack.config `limits_file`, else .claude/loops/loop-limits.json.

PROTECTED FINDINGS (owner decision, not a setting). A masking-class finding (the R3 judge's
symptom-masking finding: bug_class symptom-masking or dimension masking) and any finding with
severity critical cannot be voted down by the quorum. They go on the ballot whatever the
specialist's confidence, and among the returned ballots:
  every ballot rejects it                        -> dropped (the only way it leaves)
  every ballot confirms it at >= the floor       -> kept as today (masking true = hard block)
  anything else (one confirm, a pre-existing vote,
  a below-floor confirm, an abstain or an
  unusable vote on it)                           -> kept with human_gate: true + human_gate_reason,
                                                    review human_gate: true; the verdict is
                                                    needs-human and the recipe escalates to a
                                                    human, never re-fixes.
A gated masking finding does not set masking true: on a split the masking call is undecided, and
undecided is a human's to make. There is deliberately no loop-limits key for this rule: a number
that could be turned off would reopen the hole it closes. The quorum, validator count and floor
still apply to every other finding and are still tunable by replay.

HONEST LIMIT: the reviewer's own `tally` call writes the result, and two gates re-derive it with
`check`: dev-pre-pr's review_gate.py blocks a review.json that fails it (or lacks `validation`), and
the PR-review posting guard (dev-loop-pr-review/scripts/pr_review.sh) refuses to post or preview one
that fails it. check_convergence.py only warns (`unvalidated-findings`) on a review with open
findings and no independent validation. The
script proves the count of distinct returned ballots and the arithmetic. That each validator really
ran in a fresh context, and read only what it was allowed to, is self-reported: the same limit
graph-engineering.md states for `judged_blind`.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SEV_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
VERDICTS = {"confirm", "reject", "pre-existing"}
BALLOT_KEYS = ("finding_id", "dimension", "severity", "file", "line", "bug_class", "instances",
               "issue", "rule_id")
VALIDATION_FIELDS = ("status", "validators", "validators_returned", "quorum", "confidence_floor",
                     "candidates", "kept", "rejected", "split", "human_gated")


class Fatal(Exception):
    """bad input / limits / usage: exit 2, nothing written."""

    def __init__(self, msg, doc=None):
        super().__init__(msg)
        self.doc = doc


def emit(doc, code):
    print(json.dumps(doc, indent=1, sort_keys=True))
    return code


def _rb():
    spec = importlib.util.spec_from_file_location("review_brief", os.path.join(HERE, "review_brief.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_limits(arg):
    if arg:
        return arg
    rb = _rb()
    return rb.limits_path(rb._config(), None)


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def read_limits(path):
    try:
        with open(path) as fh:
            block = json.load(fh).get("review_validation")
    except Exception as e:  # noqa: BLE001
        raise Fatal(f"limits file unreadable ({path}): {e.__class__.__name__}: {e}")
    if not isinstance(block, dict):
        raise Fatal(f"no review_validation block in {path}: no decided validator count, quorum or "
                    "floor, so no review")
    out = {}
    for k in ("validators", "quorum", "confidence_floor"):
        v = block.get(k)
        if not _int(v):
            raise Fatal(f"review_validation.{k}={v!r} in {path} is not an integer")
        out[k] = v
    if not 1 <= out["quorum"] <= out["validators"]:
        raise Fatal(f"review_validation in {path}: need 1 <= quorum ({out['quorum']}) <= validators "
                    f"({out['validators']})")
    if not 0 <= out["confidence_floor"] <= 100:
        raise Fatal(f"review_validation.confidence_floor={out['confidence_floor']} in {path} is "
                    "outside 0-100")
    return out


def atomic_write(path, text):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".finding_votes.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def dump(doc):
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:  # noqa: BLE001
        raise Fatal(f"{what} unreadable ({path}): {e.__class__.__name__}: {e}")


# ---- candidates ---------------------------------------------------------------------------------
def norm_file(f):
    f = str(f or "").strip().replace("\\", "/")
    while f.startswith("./"):
        f = f[2:]
    return f


def norm_issue(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def finding_id(c):
    key = "|".join([str(c.get("dimension") or ""), norm_file(c.get("file")), str(c.get("line")),
                    str(c.get("bug_class") or ""), norm_issue(c.get("issue"))])
    return "F-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def is_protected(c):
    """Masking-class or critical: dropped only by a unanimous reject (see the module docstring)."""
    return c.get("bug_class") == "symptom-masking" or c.get("dimension") == "masking" \
        or str(c.get("severity") or "").lower() == "critical"


def is_masking(c):
    return c.get("bug_class") == "symptom-masking" or c.get("dimension") == "masking"


def on_ballot(c, floor):
    conf = conf_of(c)
    return is_protected(c) or (conf is not None and conf >= floor)


def conf_of(c):
    v = c.get("confidence")
    return v if _int(v) else None


def read_candidates(d):
    doc = read_json(os.path.join(d, "candidates.json"), "candidates.json")
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list) \
            or not all(isinstance(x, dict) for x in doc["findings"]):
        raise Fatal("candidates.json must be {\"findings\": [<finding objects>]}")
    return doc


def _rank(c):
    conf = conf_of(c)
    return (-SEV_RANK.get(str(c.get("severity") or "").lower(), 0),
            -(conf if conf is not None else -1), c["finding_id"])


def dedup(findings):
    """(survivors in input order, removed count). Ids are recomputed every run (idempotent)."""
    items = []
    for c in findings:
        c = dict(c)
        c["finding_id"] = finding_id(c)
        items.append(c)
    by_id = {}
    for c in items:
        cur = by_id.get(c["finding_id"])
        if cur is None or _rank(c) < _rank(cur):
            by_id[c["finding_id"]] = c
    seen, step1 = set(), []
    for c in items:
        if c["finding_id"] not in seen and by_id[c["finding_id"]] is c:
            seen.add(c["finding_id"])
            step1.append(c)
    groups = {}
    for c in step1:
        bc, ln = c.get("bug_class"), c.get("line")
        if isinstance(bc, str) and bc and bc != "other" and _int(ln):
            key = (norm_file(c.get("file")), ln, bc)
            cur = groups.get(key)
            if cur is None or _rank(c) < _rank(cur):
                groups[key] = c
    out = []
    for c in step1:
        bc, ln = c.get("bug_class"), c.get("line")
        if isinstance(bc, str) and bc and bc != "other" and _int(ln):
            if groups[(norm_file(c.get("file")), ln, bc)] is not c:
                continue
        out.append(c)
    return out, len(findings) - len(out)


# ---- ballot -------------------------------------------------------------------------------------
def cmd_ballot(a):
    lim = read_limits(resolve_limits(a.limits))
    doc = read_candidates(a.dir)
    survivors, removed = dedup(doc["findings"])
    floor = lim["confidence_floor"]
    on = [c for c in survivors if on_ballot(c, floor)]
    n = lim["validators"] if on else 0
    vote_files = [f"votes/v{i}.json" for i in range(1, n + 1)]
    ballot = {"validators": n, "quorum": lim["quorum"], "confidence_floor": floor,
              "vote_files": vote_files,
              "findings": [{k: c[k] for k in BALLOT_KEYS if k in c} for c in on]}
    doc = dict(doc)
    doc["findings"] = survivors
    os.makedirs(os.path.join(a.dir, "votes"), exist_ok=True)
    atomic_write(os.path.join(a.dir, "candidates.json"), dump(doc))
    atomic_write(os.path.join(a.dir, "ballot.json"), dump(ballot))
    return emit({"status": "ballot-ready" if on else "no-candidates", "validators": n,
                 "quorum": lim["quorum"], "confidence_floor": floor, "candidates": len(survivors),
                 "on_ballot": len(on), "below_floor": len(survivors) - len(on),
                 "protected": sum(1 for c in on if is_protected(c)), "deduped": removed,
                 "ballot": os.path.join(a.dir, "ballot.json"), "vote_files": vote_files}, 0)


# ---- tally --------------------------------------------------------------------------------------
def read_ballot(d, cands):
    b = read_json(os.path.join(d, "ballot.json"), "ballot.json (run `ballot` first)")
    if not isinstance(b, dict) or not _int(b.get("validators")) or b["validators"] < 0 \
            or not _int(b.get("confidence_floor")) or not isinstance(b.get("vote_files"), list) \
            or not isinstance(b.get("findings"), list) \
            or not all(isinstance(x, dict) for x in b["findings"]) \
            or len(b["vote_files"]) != b["validators"] \
            or not all(isinstance(x, str) for x in b["vote_files"]):
        raise Fatal("ballot.json is malformed (run `ballot` again)")
    ids = [c.get("finding_id") for c in cands]
    if any(not isinstance(i, str) or i != finding_id(c) for i, c in zip(ids, cands)) \
            or len(set(ids)) != len(ids):
        raise Fatal("candidates.json carries no or stale finding_id values (run `ballot` first)")
    want = {c["finding_id"] for c in cands if on_ballot(c, b["confidence_floor"])}
    have = [f.get("finding_id") for f in b["findings"]]
    if set(have) != want or len(have) != len(set(have)):
        raise Fatal("ballot.json does not match candidates.json (run `ballot` again)")
    if bool(have) != bool(b["validators"]):
        raise Fatal("ballot.json has findings but no validators, or validators but no findings")
    return b


def read_votes(d, ballot):
    """(returned ballots [{validator, votes: {id: vote}}], missing [vote_file], warnings)."""
    ids = {f["finding_id"] for f in ballot["findings"]}
    returned, missing, warnings, seen = [], [], [], set()
    for vf in ballot["vote_files"]:
        path = os.path.join(d, vf)
        if not os.path.isfile(path):
            missing.append(vf)
            warnings.append(f"{vf}: absent (the validator returned nothing)")
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception as e:  # noqa: BLE001
            missing.append(vf)
            warnings.append(f"{vf}: unparsable ({e.__class__.__name__}), not counted")
            continue
        vid = doc.get("validator") if isinstance(doc, dict) else None
        if not isinstance(doc, dict) or not isinstance(vid, str) or not vid.strip() \
                or not isinstance(doc.get("votes"), list):
            missing.append(vf)
            warnings.append(f"{vf}: not a ballot (needs a validator id and a votes list), not counted")
            continue
        if vid in seen:
            missing.append(vf)
            warnings.append(f"{vf}: duplicate validator id {vid!r}, not counted")
            continue
        seen.add(vid)
        votes = {}
        doubled = set()
        for i, v in enumerate(doc["votes"]):
            fid = v.get("finding_id") if isinstance(v, dict) else None
            where = f"{vf} votes[{i}]"
            if fid not in ids:
                warnings.append(f"{where}: unknown finding_id {fid!r}, ignored")
                continue
            if fid in votes or fid in doubled:
                votes.pop(fid, None)
                doubled.add(fid)
                warnings.append(f"{where}: second vote on {fid}, both are an abstain")
                continue
            if v.get("verdict") not in VERDICTS:
                warnings.append(f"{where}: verdict {v.get('verdict')!r} off the enum, an abstain on {fid}")
                doubled.add(fid)
                continue
            c = v.get("confidence")
            if not _int(c) or not 0 <= c <= 100:
                warnings.append(f"{where}: confidence {c!r} is not an integer 0-100, an abstain on {fid}")
                doubled.add(fid)
                continue
            votes[fid] = v
        returned.append({"validator": vid, "votes": votes})
    return returned, missing, warnings


def derive(d, lim, limits_src, draft_findings):
    """The pure tally: (result, kept findings, carried resolved findings). Raises Fatal."""
    doc = read_candidates(d)
    cands = doc["findings"]
    ballot = read_ballot(d, cands)
    quorum, floor = lim["quorum"], lim["confidence_floor"]
    n = ballot["validators"]
    if n and quorum > n:
        raise Fatal(f"quorum {quorum} > the {n} validators this ballot was run with")
    returned, missing, warnings = read_votes(d, ballot)
    if ballot["findings"] and len(returned) < quorum:
        raise Fatal("validation-unavailable", {
            "error": "validation-unavailable", "validators": n, "validators_returned": len(returned),
            "quorum": quorum, "missing": missing, "warnings": warnings})
    kept, rejected_split, noticed = [], 0, []
    for c in cands:
        fid = c["finding_id"]
        vs = [r["votes"][fid] for r in returned if fid in r["votes"]]
        confirm = sum(1 for v in vs if v["verdict"] == "confirm" and v["confidence"] >= floor)
        votes = {"confirm": confirm, "reject": sum(1 for v in vs if v["verdict"] == "reject"),
                 "pre_existing": sum(1 for v in vs if v["verdict"] == "pre-existing"),
                 "cast": len(returned)}
        sc = conf_of(c)
        if is_protected(c):
            cast = len(returned)
            if votes["reject"] == cast:
                continue  # a unanimous reject: the only way a protected finding leaves
            f = dict(c)
            f["votes"] = votes
            f.pop("human_gate", None)
            f.pop("human_gate_reason", None)
            if confirm != cast:
                abstain = cast - len(vs)
                low = sum(1 for v in vs if v["verdict"] == "confirm" and v["confidence"] < floor)
                kind = "masking" if is_masking(c) else "critical"
                f["human_gate"] = True
                f["human_gate_reason"] = (
                    f"validators split on a protected ({kind}) finding: {confirm} confirm"
                    f"{f' (+{low} below the floor {floor})' if low else ''}, {votes['reject']} reject, "
                    f"{votes['pre_existing']} pre-existing, {abstain} abstain or unusable of {cast} "
                    "returned ballot(s). Only a unanimous reject drops a masking or critical finding "
                    "and only a unanimous confirm makes it a plain block, so a human decides.")
            kept.append(f)
        elif sc is not None and sc >= floor and confirm >= quorum:
            f = dict(c)
            f["votes"] = votes
            kept.append(f)
        else:
            if confirm >= 1:
                rejected_split += 1
            serious = sum(1 for v in vs if v["verdict"] == "pre-existing" and v.get("serious") is True)
            if serious >= quorum:
                noticed.append({"finding_id": fid, "file": c.get("file"), "line": c.get("line"),
                                "issue": c.get("issue")})
    kept_ids = {f["finding_id"] for f in kept}
    carried = [f for f in draft_findings if isinstance(f, dict) and f.get("resolved") is True
               and f.get("finding_id") not in kept_ids]
    masking = any(is_masking(f) and f.get("human_gate") is not True for f in kept)
    gated = sum(1 for f in kept if f.get("human_gate") is True)
    validation = {"status": "independent" if ballot["findings"] else "no-candidates",
                  "validators": n, "validators_returned": len(returned), "quorum": quorum,
                  "confidence_floor": floor, "candidates": len(cands), "kept": len(kept),
                  "rejected": len(cands) - len(kept), "split": rejected_split,
                  "human_gated": gated,
                  "source": f"{limits_src}#review_validation"}
    res = {"validation": validation, "masking": masking, "human_gate": gated > 0,
           "critical": sum(1 for f in kept if f.get("severity") == "critical"),
           "high": sum(1 for f in kept if f.get("severity") == "high"),
           "missing": missing, "warnings": warnings, "noticed": noticed}
    return res, kept, carried


def read_draft(path, create):
    if not os.path.exists(path):
        if create:
            return {}
        raise Fatal(f"review file {path} does not exist")
    doc = read_json(path, "review file")
    if not isinstance(doc, dict):
        raise Fatal(f"review file {path} is not a JSON object")
    return doc


def cmd_tally(a):
    src = resolve_limits(a.limits)
    lim = read_limits(src)
    draft = read_draft(a.review, create=True)
    res, kept, carried = derive(a.dir, lim, src,
                                draft.get("findings") if isinstance(draft.get("findings"), list) else [])
    draft["findings"] = kept + carried
    draft["critical"] = res["critical"]
    draft["high"] = res["high"]
    draft["masking"] = res["masking"]
    draft["human_gate"] = res["human_gate"]
    draft["validation"] = res["validation"]
    draft.pop("headline", None)
    draft.pop("brief", None)
    if a.derive_verdict:
        blocking = res["masking"] or res["critical"] or res["high"]
        draft["verdict"] = "needs-human" if res["human_gate"] else (
            "request-changes" if blocking else "approve")
        if not isinstance(draft.get("summary"), str) or not draft["summary"]:
            draft["summary"] = "Verdict derived by finding_votes.py tally --derive-verdict (eval replay)."
    atomic_write(a.review, dump(draft))
    v = res["validation"]
    return emit({"status": v["status"], "validators": v["validators"],
                 "validators_returned": v["validators_returned"], "quorum": v["quorum"],
                 "confidence_floor": v["confidence_floor"], "candidates": v["candidates"],
                 "kept": v["kept"], "rejected": v["rejected"], "split": v["split"],
                 "human_gated": v["human_gated"], "human_gate": res["human_gate"],
                 "masking": res["masking"], "critical": res["critical"], "high": res["high"],
                 "missing_validators": res["missing"], "warnings": res["warnings"],
                 "noticed_candidates": res["noticed"]}, 0)


# ---- check --------------------------------------------------------------------------------------
def cmd_check(a):
    src = resolve_limits(a.limits)
    lim = read_limits(src)
    doc = read_draft(a.review, create=False)
    findings = [f for f in doc.get("findings") or [] if isinstance(f, dict)] \
        if isinstance(doc.get("findings"), list) else []
    res, kept, _ = derive(a.dir, lim, src, findings)
    v = []

    def add(cls, detail):
        v.append({"class": cls, "detail": detail})

    have = doc.get("validation")
    want = res["validation"]
    if not isinstance(have, dict):
        add("validation-missing", "review.json has no validation block (run finding_votes.py tally)")
    else:
        bad = sorted(k for k in VALIDATION_FIELDS if have.get(k) != want[k])
        if bad:
            add("validation-counts-wrong", "; ".join(f"{k}: {have.get(k)!r} != {want[k]!r}" for k in bad))
    kept_by_id = {f["finding_id"]: f for f in kept}
    opened = [f for f in findings if f.get("resolved") is not True]
    present = set()
    for f in opened:
        fid = f.get("finding_id")
        if not isinstance(fid, str) or fid not in kept_by_id:
            add("finding-without-quorum", f"finding {fid!r} at {f.get('file')}:{f.get('line')} is "
                                          "not in the re-derived kept set")
            continue
        present.add(fid)
        if f.get("votes") != kept_by_id[fid]["votes"]:
            add("votes-tampered", f"{fid}: votes {f.get('votes')!r} != {kept_by_id[fid]['votes']!r}")
        want_gate = kept_by_id[fid].get("human_gate") is True
        if (f.get("human_gate") is True) != want_gate:
            add("human-gate-mismatch", f"{fid}: human_gate {f.get('human_gate')!r} but the ballots say "
                                       f"{want_gate!r}")
        if want_gate and f.get("withheld") is True:
            add("human-gate-withheld", f"{fid} needs a human decision and is withheld")
    for fid in sorted(set(kept_by_id) - present):
        add("validated-finding-missing", f"{fid} reached quorum but is not in findings")
    if doc.get("masking") != res["masking"]:
        add("masking-mismatch", f"masking {doc.get('masking')!r} but the kept findings say {res['masking']!r}")
    for sev in ("critical", "high"):
        n = sum(1 for f in opened if f.get("severity") == sev)
        if doc.get(sev) != n:
            add("counts-wrong", f"{sev} {doc.get(sev)!r} but {n} open {sev} finding(s)")
    if doc.get("human_gate") is not res["human_gate"]:
        add("human-gate-mismatch", f"review human_gate {doc.get('human_gate')!r} but the ballots say "
                                   f"{res['human_gate']!r}")
    if doc.get("verdict") == "approve" and (res["masking"] or res["critical"] or res["human_gate"]):
        add("approve-with-blocking", "verdict approve while a kept finding is masking, critical or "
                                     "needs a human")
    if res["human_gate"] and doc.get("verdict") != "needs-human":
        add("human-gate-not-routed", f"a finding needs a human decision but verdict is "
                                     f"{doc.get('verdict')!r} (needs-human)")
    if v:
        return emit({"ok": False, "violations": v}, 1)
    return emit({"ok": True, "status": want["status"], "kept": want["kept"]}, 0)


def main(argv):
    ap = argparse.ArgumentParser(prog="finding_votes.py")
    sub = ap.add_subparsers(dest="verb")
    for name in ("ballot", "tally", "check"):
        p = sub.add_parser(name)
        p.add_argument("--dir", required=True)
        p.add_argument("--limits", default=None)
        if name != "ballot":
            p.add_argument("--review", required=True)
        if name == "tally":
            p.add_argument("--derive-verdict", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return emit({"error": "bad usage"}, 2)
    if not a.verb:
        return emit({"error": "bad usage: ballot | tally | check"}, 2)
    try:
        return {"ballot": cmd_ballot, "tally": cmd_tally, "check": cmd_check}[a.verb](a)
    except Fatal as e:
        return emit(e.doc if e.doc is not None else {"error": str(e)}, 2)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — never crash open
        print(json.dumps({"error": f"finding_votes crashed: {e.__class__.__name__}: {e}"}))
        sys.exit(2)
