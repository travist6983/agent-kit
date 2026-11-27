#!/usr/bin/env python3
"""recipe_router — the retry/escalate decision engine shared by every dev-loop-* recipe (deterministic, tested).

The recipes own the counters/budget; this encodes the routing rules so every retry CHANGES strategy by
failure class — never a blind repeat. Given a stage's result + counters + budget + flags, it returns the
next action. It reads nothing but its arguments, the tracked limits file and the review-rounds file
named by --review-rounds: no adapters, no brain, no network — the recipe supplies every fact.

Review convergence (--review-rounds, code-review stage only): the file is the per-round history
review_rounds.py records after every review. When one finding fingerprint (file + bug_class) is open in
the last review_convergence.stagnation_fix_cycles + 1 consecutive rounds, the router escalates with
failure_class review-stagnation BEFORE the retry cap: step back and reconsider, do not re-fix. A
request-changes WITHOUT the flag escalates (failure_class review-rounds-missing): fail closed, since a
re-fix with no round history cannot be shown to converge.

Usage:
  recipe_router.py --stage repro|fix|verify|regression|code-review|pre-pr|prod-verify|ship --result R
                   --attempt N --max 3 [--confidence C] [--min-confidence 80]
                   [--budget-spent X] [--budget-cap Y] [--paused true|false] [--sensitive true|false]
                   [--review-rounds <evid>/<KEY>/review-rounds.json]
Prints {"action": ..., "reason": ...} (plus failure_class + stagnant on a review-stagnation escalate,
and failure_class on a ship stale-readiness / readiness-unbound escalate).
action ∈ {halt-paused, stop-budget, escalate, re-fix, re-repro, reject, rollback, advance}
"""
import argparse
import json
import os
import sys

_TRUE, _FALSE = {"true", "1", "yes", "on"}, {"false", "0", "no", "off", ""}

# <repo>/.claude/skills/dev-loop-defect/scripts/recipe_router.py → parents[4] is the repo root.
_REPO_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", ".."))
_DEFAULT_LIMITS_RELPATH = os.path.join(".claude", "loops", "loop-limits.json")


def _limits_path():
    """Where the decided gate numbers live: `limits_file` in stack.config.json (env override
    LOOPS_CFG__LIMITS_FILE), resolved against the repo root; else the tracked default path."""
    try:
        sys.path.insert(0, _REPO_ROOT)
        from lib.adapters.config import load_config  # stdlib-only loader
        stack = load_config()
        p = stack.resolve(stack.get("limits_file"), _DEFAULT_LIMITS_RELPATH)
        if p is not None:
            return str(p)
    except Exception:
        pass
    root = os.environ.get("LOOPS_REPO_ROOT") or _REPO_ROOT
    return os.path.join(root, _DEFAULT_LIMITS_RELPATH)


def _limits():
    """The decided gate numbers (.claude/loops/loop-limits.json by default)."""
    try:
        with open(_limits_path()) as fh:
            return json.load(fh)
    except Exception:
        return {}

def _review_stagnation(path):
    """None when the review is still converging, else (reason, extra) for an escalate.
    Fails CLOSED: no decided number or an unreadable rounds file escalates."""
    n = (_limits().get("review_convergence") or {}).get("stagnation_fix_cycles")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        return ("review_convergence.stagnation_fix_cycles missing or invalid in loop-limits.json — "
                "--review-rounds was passed but there is no decided number; failing CLOSED", {})
    try:
        with open(path) as fh:
            doc = json.load(fh)
        rounds = doc["rounds"]
        if not isinstance(rounds, list) or not all(
                isinstance(x, dict) and isinstance(x.get("open"), list) for x in rounds):
            raise ValueError("bad shape")
        window = rounds[-(n + 1):]
        sets = []
        for x in window:
            sets.append({o["fingerprint"]: o for o in x["open"]
                         if isinstance(o, dict) and isinstance(o.get("fingerprint"), str)})
    except (OSError, ValueError, KeyError, TypeError):
        return ("review-rounds unreadable - cannot prove the review is converging", {})
    if len(window) < n + 1:
        return None
    common = set(sets[0])
    for s in sets[1:]:
        common &= set(s)
    if not common:
        return None
    last = sets[-1]
    stagnant = []
    for fp in sorted(common):
        o = last[fp]
        stagnant.append({"fingerprint": fp, "file": o.get("file"), "bug_class": o.get("bug_class"),
                         "basis": o.get("basis")})
    named = ", ".join("%s %s" % (x["file"], x["bug_class"] if x["basis"] == "class" and x["bug_class"] else "text")
                      for x in stagnant)
    reason = ("review-stagnation: %d finding(s) open across %d fix cycles (%s) - the loop is not converging; "
              "step back and reconsider the approach, do not re-fix" % (len(stagnant), n, named))
    return (reason, {"failure_class": "review-stagnation", "stagnant": stagnant})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--result", required=True)
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--max", type=int, default=3)
    ap.add_argument("--confidence", type=int, default=100)
    ap.add_argument("--min-confidence", type=int, default=80)
    ap.add_argument("--budget-spent", type=float, default=0)
    ap.add_argument("--budget-cap", type=float, default=None)  # unset → loop-limits.json per_loop_usd_cap
    ap.add_argument("--paused", default="false")
    ap.add_argument("--sensitive", default="false")
    ap.add_argument("--review-rounds", default=None)
    a = ap.parse_args()

    def out(action, reason, **extra):
        doc = {"action": action, "reason": reason}
        doc.update(extra)
        print(json.dumps(doc)); return

    # Safety flags FAIL CLOSED on malformed input: '--paused True' (Python-repr capitals),
    # 'TRUE', a stray value — anything not recognizably false is treated as the safe
    # direction, never silently ignored.
    pv, sv = a.paused.strip().lower(), a.sensitive.strip().lower()
    if pv not in _TRUE and pv not in _FALSE:
        return out("halt-paused", f"malformed --paused value {a.paused!r} — cannot prove un-paused, failing CLOSED (R9)")
    paused = pv in _TRUE
    sensitive = sv in _TRUE or (sv not in _FALSE)  # malformed sensitive → treat as sensitive

    # R9: a global pause halts everything — checked before any retry or ship.
    if paused:
        return out("halt-paused", "qa-kb global_pause is set (R9) — halt the loop")
    # R5: budget circuit breaker — never retry into unbounded spend. The cap defaults from
    # the limits file (a passed --budget-cap overrides; 0 explicitly disables). Spend is
    # supplied by the RUNNER's own usage accounting — see loop-limits.json r5_budget honesty note.
    cap = a.budget_cap if a.budget_cap is not None else float(
        (_limits().get("r5_budget") or {}).get("per_loop_usd_cap") or 0)
    if cap and a.budget_spent >= cap:
        return out("stop-budget", f"per-loop budget cap reached ({a.budget_spent} >= {cap}, R5) — stop + escalate")

    s, r = a.stage, a.result
    cap_hit = a.attempt >= a.max
    def retry_or_escalate(act, why):
        return out("escalate", f"{why}; retry cap reached ({a.attempt}/{a.max})") if cap_hit else out(act, why)

    if s == "repro":
        # contracts/qa-repro.json: reject-vs-retry is triage-critical.
        if r == "reproduced": return out("advance", "bug reproduced")
        if r == "not_reproduced":
            return out("reject", "repro ran and the bug did NOT occur — a real negative: triage MAY reject/close the ticket (human confirms); never auto-fix")
        if r == "inconclusive":
            return retry_or_escalate("re-repro", "could not determine (tooling/needs-info/env broke) — must NOT reject; retry the repro")
        if r in ("not-reproduced", "needs-info", "tooling-failed"):
            return out("escalate", f"repro={r} — STALE retired vocabulary (contracts/qa-repro.json invalidates it): emit not_reproduced | inconclusive")
        return out("escalate", f"repro={r} — cannot fix what isn't reproduced")

    if s == "fix":
        if r == "fixed": return out("advance", "candidate fix produced")
        if r in ("blocked-blast-radius", "could-not-fix", "symptom-only"): return out("escalate", f"fix={r}")
        return out("escalate", f"fix={r}")

    if s == "verify":
        if r == "pass":
            if a.confidence < a.min_confidence and sensitive:
                return out("escalate", f"green but confidence {a.confidence} < {a.min_confidence} on a sensitive area — escalate even if green")
            return out("advance", "verify pass")
        if r in ("fail", "partial"):
            return retry_or_escalate("re-fix", f"verify={r} — refine the fix with the failure as new context")
        return out("escalate", f"verify={r} (blocked/no-oracle)")

    if s == "regression":
        if r in ("green", "flaky"): return out("advance", f"regression {r} (flake quarantined, not a gate fail)")
        if r == "same-area-regression": return retry_or_escalate("re-fix", "same-area regression — refine the fix")
        if r == "unrelated-break": return out("escalate", "unrelated-area break — fix exceeded blast radius; escalate immediately, no retry")
        if r == "blocked-build": return retry_or_escalate("re-fix", "build/lint regressed — refine the fix")
        if r == "blocked-environment":
            return out("escalate", "environment failure (test runner / dependencies broke) — re-provision or a human decides; NOT blast radius")
        if r == "blocked-slice-empty":
            return out("escalate", "zero tests ran for the area — the gate did not run; never ship-ok on an empty run")
        return out("escalate", f"regression={r}")

    if s == "code-review":
        if r == "approve": return out("advance", "review clean")
        if r == "needs-human":
            # contracts/dev-code-review.json v1.8.0: a masking or critical finding the validators
            # split on. Only a unanimous reject drops one, so it is kept for a human. A re-fix cannot
            # settle a disagreement about whether the fix masks the symptom: escalate, at any attempt,
            # before stagnation or the retry cap are even looked at.
            return out("escalate", "code-review=needs-human — the validators split on a masking or "
                       "critical finding; a human decides, a re-fix cannot",
                       failure_class="review-needs-human")
        if r != "request-changes":
            # e.g. `validation-unavailable` / `blocked`: the review did not produce a verdict, so there is
            # no evidence about the fix. Like verify=blocked: escalate, never burn a re-fix on it.
            return out("escalate", f"code-review={r} — the review produced no verdict (not a finding about the fix); a re-fix cannot help")
        if a.review_rounds is None:
            # Fail CLOSED: a re-fix without the round history cannot prove the review is converging,
            # which is the exact blind repeat the stagnation rule exists to stop.
            return out("escalate", "review request-changes without --review-rounds — record the round "
                       "(review_rounds.py record) and pass the history; failing CLOSED",
                       failure_class="review-rounds-missing")
        stuck = _review_stagnation(a.review_rounds)
        if stuck is not None:
            return out("escalate", stuck[0], **stuck[1])
        return retry_or_escalate("re-fix", "review request-changes (incl. masking) — refine the fix")

    if s == "pre-pr":
        # The readiness gate before ship. `warnings` ADVANCES on purpose: a MEDIUM violation that is
        # not allowed to block would otherwise spin the loop forever — it rides along in the report
        # and the PR body where a human sees it.
        if r in ("ready", "warnings"): return out("advance", f"pre-pr {r} — clear to ship")
        if r == "blocked": return retry_or_escalate("re-fix", "pre-pr blockers (build/lint/test, confirmed conventions violation, or unresolved review finding) — refine the fix")
        if r == "scan-unavailable":
            return out("escalate", "conventions scan could not run (exit 2) — FAIL CLOSED; a re-fix cannot help, a human checks the repo state")
        return out("escalate", f"pre-pr={r}")

    if s == "prod-verify":
        if r == "prod-confirmed": return out("advance", "fix confirmed in prod")
        if r == "prod-regressed": return out("rollback", "prod regression — roll back per the deploy adapter's rollback_hint (prefer a feature-flag flip where one exists) + escalate with prod evidence (R2/R9)")
        return out("escalate", f"prod-verify={r} (inconclusive, or no analytics adapter) — a human decides")

    if s == "ship":
        if r == "ok": return out("advance", "shipped")
        if r in ("global-pause", "pause-check-failed"): return out("halt-paused", f"ship preflight {r} (R9)")
        # commit_binding.py check: the commit is not the content dev-pre-pr judged, or there is no
        # usable readiness verdict. Deterministic — re-shipping the same commit cannot change it, and
        # there is no fix to make — so it escalates; it is never retried or re-fixed.
        if r == "stale-readiness":
            return out("escalate", "ship abort=stale-readiness — the commit is not the content the readiness "
                                   "verdict judged; a deterministic failure, never re-shipped on the old verdict",
                       failure_class=r)
        if r == "readiness-unbound":
            return out("escalate", "ship abort=readiness-unbound — no usable readiness verdict bound to this "
                                   "commit; a deterministic failure, never re-shipped", failure_class=r)
        return out("escalate", f"ship abort={r}")

    return out("escalate", f"unknown stage {s}")

if __name__ == "__main__":
    main()
