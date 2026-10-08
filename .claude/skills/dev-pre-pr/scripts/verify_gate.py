#!/usr/bin/env python3
"""dev-pre-pr's fix-verification gate — deterministic, zero model calls.

The invariant it enforces: **a tier may shrink HOW MUCH is verified; it may never remove
verification.** A recipe that advances one stage at a time upholds it by construction — and
stops upholding it the moment qa-verify, qa-regression and dev-code-review run in PARALLEL,
because a failed or crashed verify can then arrive at this gate alongside two green
artifacts and nothing would notice.

This script is the mechanical form of that rule, so it is not the agent's judgment call.

Usage:
  python3 verify_gate.py --verify <path/to/verify.json> [--changed-file F ...] [--loop] [--json]
  python3 verify_gate.py --verify <path> --changed-from-git --repo <dir> [--base <branch>]

`--base` defaults to `project.default_branch` from .claude/loops/stack.config.json (read
through lib/adapters/config.py; env override LOOPS_CFG__PROJECT__DEFAULT_BRANCH). With
--changed-from-git and no determinable base the gate fails closed (malformed → escalate).

Exit codes (and `fix_verified` / `sources.verify` for pre-pr.json):
  0  pass            verify.json present, fresh, verdict=pass, masking_suspected not true
  1  BLOCK           any other outcome inside a loop — the caller must emit status=blocked
  2  bad usage
Stdout is one JSON object: {fix_verified, source, status, next_action, reason}.

`--loop` (default when --evid-root/--key style paths are used) forbids `not-applicable`:
inside a loop there is always a fix to verify. Outside a loop (a standalone developer
pre-pr check on a docs-only branch) a missing verify.json is `not-applicable`, not a block.
"""
import argparse
import json
import os
import subprocess
import sys

FRAMEWORK_ROOT = os.environ.get("LOOPS_REPO_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))


def default_branch():
    """project.default_branch from stack.config.json; None when no config loads."""
    try:
        sys.path.insert(0, FRAMEWORK_ROOT)
        from lib.adapters.config import cfg
        return cfg("project.default_branch") or None
    except Exception:  # noqa: BLE001 — a missing stack.config removes the default, nothing else
        return None

PASS = "pass"
FAIL = "fail"
MISSING = "missing"
MALFORMED = "malformed"
NOT_APPLICABLE = "not-applicable"


def out(fix_verified, source, status, next_action, reason):
    print(json.dumps({
        "fix_verified": fix_verified,
        "source": source,
        "status": status,
        "next_action": next_action,
        "reason": reason,
    }, indent=1))
    return 0 if status == "ok" else 1


def changed_from_git(repo, base):
    """Files changed vs the merge-base. Never the base tip — that reports commits that
    landed on the base branch since you branched as if you deleted them."""
    try:
        mb = subprocess.run(["git", "-C", repo, "merge-base", base, "HEAD"],
                            capture_output=True, text=True, timeout=30)
        if mb.returncode != 0:
            return None
        names = subprocess.run(["git", "-C", repo, "diff", "--name-only", mb.stdout.strip()],
                               capture_output=True, text=True, timeout=30)
        if names.returncode != 0:
            return None
        return [os.path.join(repo, f) for f in names.stdout.split("\n") if f.strip()]
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", required=True, help="path to verify.json")
    ap.add_argument("--changed-file", action="append", default=[],
                    help="a changed source file; repeat. Staleness = any is newer than verify.json")
    ap.add_argument("--changed-from-git", action="store_true")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default=None,
                    help="base branch (default: project.default_branch from stack.config.json)")
    ap.add_argument("--loop", dest="loop", action="store_true", default=None,
                    help="inside a loop: not-applicable is forbidden (default: inferred)")
    ap.add_argument("--no-loop", dest="loop", action="store_false")
    a = ap.parse_args()
    if not a.base:
        a.base = default_branch()

    v = a.verify
    # Inside a loop unless explicitly told otherwise. Fail closed: if we cannot tell, assume
    # we ARE in a loop, because that is the posture with the stricter rule.
    in_loop = True if a.loop is None else a.loop

    # ---- present? -----------------------------------------------------------------
    if not os.path.isfile(v):
        if in_loop:
            return out(MISSING, MISSING, "blocked", "escalate",
                       f"no verify.json at {v} — inside a loop the fix must be verified. "
                       "A green build is not verification: it proves the code compiles, "
                       "not that the reported bug is gone.")
        return out(NOT_APPLICABLE, NOT_APPLICABLE, "ok", "continue",
                   "no verify.json and not inside a loop — standalone developer check")

    # ---- readable + contract-shaped? ----------------------------------------------
    try:
        with open(v) as fh:
            d = json.load(fh)
    except Exception as e:
        return out(MALFORMED, MALFORMED, "blocked", "escalate",
                   f"verify.json is present but unreadable ({e.__class__.__name__}: {e}). "
                   "Unreadable evidence is not evidence — fail closed.")
    if not isinstance(d, dict):
        return out(MALFORMED, MALFORMED, "blocked", "escalate",
                   f"verify.json parsed to {type(d).__name__}, expected object")

    verdict = d.get("verdict")
    if verdict is None:
        return out(MALFORMED, MALFORMED, "blocked", "escalate",
                   "verify.json has no `verdict` key — cannot tell whether the fix works")
    if not isinstance(verdict, str):
        return out(MALFORMED, MALFORMED, "blocked", "escalate",
                   f"verify.json `verdict` is {type(verdict).__name__}, expected string")

    # ---- fresh? --------------------------------------------------------------------
    # Staleness is judged BEFORE the verdict is trusted: a `pass` that predates the code it
    # graded is exactly the stale-evidence failure this skill exists to prevent.
    changed = list(a.changed_file)
    if a.changed_from_git:
        if not a.base:
            return out(MALFORMED, MALFORMED, "blocked", "escalate",
                       "cannot determine the base branch (no --base and no project.default_branch "
                       "in stack.config.json) — cannot date the verify. Fail closed.")
        got = changed_from_git(a.repo, a.base)
        if got is None:
            return out(MALFORMED, MALFORMED, "blocked", "escalate",
                       "could not determine changed files from git — cannot date the verify. "
                       "Fail closed rather than trust an undateable PASS.")
        changed += got

    vm = os.path.getmtime(v)
    newer = [f for f in changed if os.path.isfile(f) and os.path.getmtime(f) > vm]
    if newer:
        # Deliberately NOT ran-locally: dev-pre-pr has no Edit tool, drives no browser, and a
        # verify it ran itself would not be BLIND — which defeats the whole point.
        return out(FAIL, "stale", "blocked", "re-fix",
                   "verify.json is older than " + str(len(newer)) + " changed file(s) "
                   f"(e.g. {os.path.basename(newer[0])}) — re-verify. This gate may not "
                   "re-run it: a verify produced by the same agent that wrote the fix is "
                   "not a blind judge.")

    # ---- the verdict itself ---------------------------------------------------------
    if verdict == "pass":
        # A pass with masking_suspected is not a pass. The bug may be hidden, not fixed (R3).
        if d.get("masking_suspected") is True:
            return out(FAIL, "fresh", "blocked", "re-fix",
                       "verify.json verdict=pass but masking_suspected=true — R3: the "
                       "symptom may be masked rather than the root cause fixed. "
                       "A masked pass is a fail.")
        return out(PASS, "fresh", "ok", "continue",
                   f"fix verified (confidence {d.get('confidence', 'n/a')}, "
                   f"replay {d.get('replay', 'n/a')})")

    if verdict == "blocked":
        # The oracle produced NO evidence about the fix. Never route this to re-fix: there is
        # nothing to learn from, so a re-fix would be a blind retry.
        return out(FAIL, "fresh", "blocked", "escalate",
                   "verify.json verdict=blocked — the oracle gave no evidence about the fix "
                   "(app down, no oracle, replay exit 2). Not a failed fix; an absent test.")

    if verdict in ("fail", "partial"):
        return out(FAIL, "fresh", "blocked", "re-fix",
                   f"verify.json verdict={verdict} — the fix does not work. "
                   "Route back to dev-fix with the observation as new context.")

    return out(MALFORMED, MALFORMED, "blocked", "escalate",
               f"verify.json verdict={verdict!r} is not a recognised value "
               "(pass|fail|partial|blocked) — stale vocabulary or a contract violation.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # never let this gate crash open
        print(json.dumps({"fix_verified": MALFORMED, "source": MALFORMED,
                          "status": "blocked", "next_action": "escalate",
                          "reason": f"verify_gate crashed: {e}"}, indent=1))
        sys.exit(1)
