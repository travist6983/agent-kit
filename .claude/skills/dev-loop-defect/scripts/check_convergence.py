#!/usr/bin/env python3
"""The checker node — guards the convergence point of the verify ∥ regression ∥ review diamond.

Running the three judges in parallel removes the natural checkpoints where a bad output
would have been noticed. A hallucinated, empty, stale-vocabulary or contradictory artifact
now flows into dev-pre-pr beside two good ones, and dev-pre-pr reconciles by FRESHNESS — it
asks "is this newer than the diff", never "is this sane". This script asks the second
question, and it is the only thing standing between a diamond and a confident synthesis
built on bad material.

The five failure modes it catches (and nothing else — it is not a reviewer):
  1. EMPTY      — a node returned nothing usable, or null, or an empty object
  2. CONTRADICTORY — two artifacts that cannot both be true
  3. OFF-TOPIC  — an artifact about a different ticket
  4. LOW-CONFIDENCE — below the floor for the run's tier
  5. MALFORMED  — unparseable, or a shape the next node cannot read

Usage:
  python3 check_convergence.py --evid <EVID_ROOT>/<KEY> --key <KEY>
        [--producer-model <model id or family>] [--min-confidence 80]
        [--require verify,regression,review] [--tier T1] [--json]

Model strength (the judge-strength invariant) comes from `models.rank` in
.claude/loops/stack.config.json; a family name ("sonnet") and a full id ("claude-sonnet-5-5")
rank the same.

Exit codes:
  0  clean      — every artifact present, sane and mutually consistent
  1  FLAGGED    — at least one problem; DO NOT merge. Route by `next_action`.
  2  UNCHECKABLE — the evidence directory is unreadable. Fail closed; never read as clean.

A `null` agent result is a REPORTED FAILURE, never something to filter away quietly. That is
the whole point: the "bad node goes undetected" failure of a parallel layer is silence, not noise.
"""
import argparse
import json
import os
import sys

# Model strength ordering for the judge-strength invariant. The authoritative order is
# `models.rank` in .claude/loops/stack.config.json (weakest → strongest, full model ids); this
# family-name ladder is the documented default when the config cannot be read.
_DEFAULT_MODEL_RANK = {"haiku": 1, "sonnet": 2, "opus": 3, "fable": 4}
# <repo>/.claude/skills/dev-loop-defect/scripts/check_convergence.py → parents[4] is the repo root.
_REPO_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", ".."))


def _family(model_id):
    """'claude-sonnet-5-5' → 'sonnet'; 'sonnet' → 'sonnet'. Vendor prefixes and version suffixes are
    not strength, so a judge named by family or by full id ranks the same."""
    parts = [p for p in str(model_id).strip().lower().replace("_", "-").split("-") if p]
    if not parts:
        return ""
    if parts[0] == "claude" and len(parts) > 1:
        parts = parts[1:]
    return parts[0]


def load_model_rank():
    """{family_or_id: rank} from stack.config `models.rank`, else the default ladder."""
    try:
        sys.path.insert(0, _REPO_ROOT)
        from lib.adapters.config import cfg
        rank = cfg("models.rank")
        if isinstance(rank, list) and rank:
            table = {}
            for i, mid in enumerate(rank, 1):
                mid_s = str(mid).strip().lower()
                table[mid_s] = i
                table.setdefault(_family(mid_s), i)
            return table
    except Exception:
        pass
    return dict(_DEFAULT_MODEL_RANK)


MODEL_RANK = load_model_rank()


def model_rank(model_id):
    m = str(model_id).strip().lower()
    return MODEL_RANK.get(m) if m in MODEL_RANK else MODEL_RANK.get(_family(m))

# Confidence floor by tier. T0 verifies LESS, so it may not also believe itself more loosely.
TIER_FLOOR = {"T0": 80, "T1": 80, "T2": 85}

ARTIFACTS = {
    "verify": "verify.json",
    "regression": "regression.json",
    "review": "review.json",
}


class Flags:
    def __init__(self):
        self.items = []

    def add(self, kind, artifact, detail, next_action="re-fix"):
        self.items.append({
            "kind": kind, "artifact": artifact,
            "detail": detail, "next_action": next_action,
        })

    def __bool__(self):
        return bool(self.items)


def load(path, name, flags):
    """Returns (data, ok). A missing REQUIRED artifact is the caller's problem; here we only
    classify what a present-but-bad file is."""
    if not os.path.isfile(path):
        return None, False
    try:
        raw = open(path).read()
    except Exception as e:
        flags.add("malformed", name, f"unreadable: {e}", "escalate")
        return None, False
    if not raw.strip():
        flags.add("empty", name, "file is zero-length — the agent wrote nothing", "escalate")
        return None, False
    try:
        d = json.loads(raw)
    except Exception as e:
        flags.add("malformed", name, f"unparseable JSON: {e}", "escalate")
        return None, False
    if d is None:
        # A null result is a reported failure, never filtered away silently.
        flags.add("empty", name, "artifact is literally `null` — the agent returned nothing. "
                                 "This is a failure to report, not a value to skip.", "escalate")
        return None, False
    if not isinstance(d, dict):
        flags.add("malformed", name, f"parsed to {type(d).__name__}, expected object", "escalate")
        return None, False
    if not d:
        flags.add("empty", name, "artifact is an empty object", "escalate")
        return None, False
    return d, True


def check_off_topic(d, name, key, flags):
    """An artifact naming a different ticket is the parallel layer's cross-wiring failure —
    two runs sharing an EVID_ROOT, or an agent handed the wrong path."""
    if not key:
        return
    for field in ("key", "ticket", "ticket_key"):
        v = d.get(field)
        if isinstance(v, str) and v.strip() and v.strip().upper() != key.upper():
            flags.add("off-topic", name,
                      f"names ticket {v!r} but this run is {key} — wrong artifact, or two "
                      "runs are sharing an evidence directory", "escalate")
            return


def check_confidence(d, name, floor, flags):
    c = d.get("confidence")
    if c is None:
        return
    if not isinstance(c, (int, float)):
        flags.add("malformed", name, f"confidence is {type(c).__name__}, expected number", "escalate")
        return
    if c < floor:
        flags.add("low-confidence", name,
                  f"confidence {c} is below the floor of {floor} for this tier — "
                  "too low to merge into a readiness verdict", "escalate")


def check_judge_strength(d, name, producer_model, flags):
    """A judge weaker than its producer does not give a weaker verdict; it gives an invalid
    one. Blocking, not advisory."""
    if not producer_model:
        return
    jm = d.get("model") or d.get("judge_model")
    if not isinstance(jm, str):
        return
    jr = model_rank(jm)
    pr = model_rank(producer_model)
    if jr is None or pr is None:
        return
    if jr < pr:
        flags.add("weak-judge", name,
                  f"judged by {jm} but produced by {producer_model} — a judge must be at "
                  "least as strong as the producer it grades; this verdict is invalid",
                  "escalate")


def check_contradictions(verify, regression, review, flags):
    """Two artifacts that cannot both be true. Kept deliberately narrow — a checker that
    guesses generates false blockers, and after two of those nobody trusts the gate."""
    # 1. A fix cannot be verified working on a build that does not compile.
    if verify and regression:
        vv = verify.get("verdict")
        rs = regression.get("status")
        if vv == "pass" and rs in ("blocked-build",):
            flags.add("contradictory", "verify+regression",
                      f"verify says the fix works (verdict={vv}) but regression says the "
                      f"build is broken (status={rs}) — both cannot be true", "escalate")
    # 2. An approving review that simultaneously reports unresolved findings.
    #
    # CALIBRATED AGAINST REAL RUNS, and deliberately split by severity. Replayed over a corpus of
    # historical runs, a strict version of this check flagged several — but in at least one the
    # HIGH finding *had* been addressed in a re-fix cycle; the artifact simply never set
    # `resolved`. Blocking on that is a material false-blocker rate, and dev-pre-pr's own
    # failure list says it plainly: after two false blockers nobody trusts the gate.
    #
    # So: an unresolved CRITICAL under `approve` blocks (those two cannot both be true), an
    # unresolved HIGH warns and names the bookkeeping gap. The real fix is upstream —
    # dev-code-review should set `resolved` when a re-fix cycle closes a finding, which makes
    # the claim falsifiable instead of inferred.
    if review:
        verdict = review.get("verdict")
        findings = review.get("findings") or []
        if verdict == "approve" and isinstance(findings, list):
            def unresolved(sev):
                return [f for f in findings
                        if isinstance(f, dict)
                        and str(f.get("severity", "")).lower() == sev
                        and not f.get("resolved")]
            crit = unresolved("critical")
            high = unresolved("high")
            if crit:
                flags.add("contradictory", "review",
                          f"verdict=approve while carrying {len(crit)} unresolved CRITICAL "
                          "finding(s) — dev-pre-pr treats `approve` as clearing those, so this "
                          "would ship them silently. Approve and unresolved-critical cannot "
                          "both be true.", "re-fix")
            elif high:
                flags.add("unverifiable-claim", "review",
                          f"verdict=approve with {len(high)} HIGH finding(s) carrying no "
                          "`resolved` flag. Probably a bookkeeping gap rather than a live "
                          "risk — but from this artifact alone it is impossible to tell "
                          "whether a re-fix closed them. Set `resolved` on findings a cycle "
                          "addressed.", "warn")
    # 3. Masking suspected but the review approved — the R3 defence-in-depth disagreeing.
    if verify and review:
        if verify.get("masking_suspected") is True and review.get("verdict") == "approve":
            flags.add("contradictory", "verify+review",
                      "verify suspects symptom masking (R3) but review approved — the two "
                      "masking detectors disagree; a human decides, not the loop", "escalate")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evid", required=True, help="the run's evidence dir, <EVID_ROOT>/<KEY>")
    ap.add_argument("--key", default="", help="ticket key, for the off-topic check")
    ap.add_argument("--producer-model", default="", help="the model dev-fix ran on")
    ap.add_argument("--min-confidence", type=int, default=None)
    ap.add_argument("--tier", default="T1")
    ap.add_argument("--require", default="verify,regression",
                    help="comma list that MUST be present. review is optional by default: "
                         "the standard defect recipe has no review stage.")
    a = ap.parse_args()

    if not os.path.isdir(a.evid):
        print(json.dumps({"status": "uncheckable", "next_action": "escalate", "flags": [],
                          "reason": f"evidence dir not found: {a.evid}"}, indent=1))
        return 2

    floor = a.min_confidence if a.min_confidence is not None else TIER_FLOOR.get(a.tier.upper(), 80)
    flags = Flags()
    required = [s.strip() for s in a.require.split(",") if s.strip()]
    data = {}

    for name, fn in ARTIFACTS.items():
        path = os.path.join(a.evid, fn)
        d, ok = load(path, name, flags)
        if ok:
            data[name] = d
            check_off_topic(d, name, a.key, flags)
            check_confidence(d, name, floor, flags)
            check_judge_strength(d, name, a.producer_model, flags)
        elif d is None and not os.path.isfile(path):
            if name in required:
                flags.add("empty", name,
                          f"{fn} is required for this run and is absent — the agent that "
                          "should have written it produced nothing", "escalate")

    check_contradictions(data.get("verify"), data.get("regression"), data.get("review"), flags)

    # Severity resolution. escalate > re-fix > warn: the stricter route wins on a mixed bag.
    # A warn-only result does NOT block — it rides along in the report so a reader sees it,
    # exactly as dev-pre-pr's `warnings` status advances. A checker that blocks on everything
    # it notices becomes a gate the recipe learns to route around.
    blocking = [f for f in flags.items if f["next_action"] in ("escalate", "re-fix")]
    warned = [f for f in flags.items if f["next_action"] == "warn"]

    if blocking:
        status = "flagged"
        next_action = "escalate" if any(f["next_action"] == "escalate" for f in blocking) else "re-fix"
    elif warned:
        status = "warnings"
        next_action = "continue"
    else:
        status = "clean"
        next_action = "continue"

    print(json.dumps({
        "status": status,
        "next_action": next_action,
        "tier": a.tier,
        "confidence_floor": floor,
        "checked": sorted(data.keys()),
        "flags": flags.items,
        "blocking_count": len(blocking),
        "warning_count": len(warned),
    }, indent=1))
    return 1 if blocking else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # never crash open
        print(json.dumps({"status": "uncheckable", "next_action": "escalate", "flags": [],
                          "reason": f"check_convergence crashed: {e}"}, indent=1))
        sys.exit(2)
