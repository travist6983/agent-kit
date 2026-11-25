#!/usr/bin/env python3
"""dev-pre-pr's post-diff risk reclassification — deterministic, zero model calls, no network.

WHY THIS EXISTS. The tier is assigned at intake (dev-intake/scripts/assign_tier.py), before any
code exists, from an ESTIMATE of how many files a fix will touch. The change itself is the better
evidence. This script re-reads the tier from the actual diff, and it is escalate-only: the result is
never below the triage tier. It then runs a second, independent check that can demand a human
anyway, and states how many HUMAN approvals the pull request needs.

  tier = max(triage tier (T1 when there is none), decide(diff facts), scope-surprise tier)

  * decide() is assign_tier.py's OWN function, imported by path, so triage and reclassification
    apply one rule set and can never drift apart. Facts the diff cannot supply (issue type,
    scenarios, roles, prior attempts, sensitive) pass through from intake.json / context.json.
  * `files` counts changed NON-test files: a fix that adds tests is not pushed up a tier for it.
  * scope surprise: files > risk_reclass.scope_surprise_factor x len(context.likely_files) moves
    the tier one step above the triage tier (the old prose rule made mechanical).

  human gate (independent of the tier, closed vocabulary):
    symptom-only-fix  fix.json is_root_cause is exactly false (R3: flag for a human)
    tests-weakened    an EXISTING test file (conventions.test_path_regex) was deleted or lost lines
    blast-radius      the R6 denylist scan over the changed files hit (ship still refuses it)
    sensitive-area    intake.json sensitive is true

  human_approvals_required = risk_reclass.human_approvals_by_tier[tier], raised to
  risk_reclass.human_gate_min_approvals when the gate is demanded. The kit STATES this number;
  the host's branch protection ENFORCES it. An agent approval (dev-code-review `approve`) never
  counts as a human one: agent_approval_counts_as_human is always false.

Usage:
  python3 risk_reclass.py --evid <EVID_ROOT>/<KEY> --repo <dir> [--base BRANCH] [--limits PATH]
                          [--triage-tier T0|T1|T2] [--changed-file F ...]
  --base    defaults to project.default_branch (stack.config.json)
  --limits  defaults to stack.config `limits_file`, else .claude/loops/loop-limits.json
  --changed-file  replaces the git-derived file list (tests only; tests-weakened is then not read)

Exit 0 = classified (escalated or not — an escalation is not a block; there is no exit 1).
Exit 2 = could not classify: git or merge-base failure, no base, resolve_projects failure, the
denylist scan could not run, the limits file unreadable or a number invalid, bad usage. Fail closed.
Stdout is one JSON object: {"risk": {...}, "warnings": [...]} or {"error": "..."}.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys

FRAMEWORK_ROOT = os.environ.get("LOOPS_REPO_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
SKILLS = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
ASSIGN_TIER = os.path.join(SKILLS, "dev-intake", "scripts", "assign_tier.py")
RESOLVE = os.path.join(SKILLS, "qa-regression", "scripts", "resolve_projects.py")
R6SCAN = os.path.join(SKILLS, "dev-fix", "scripts", "r6_denylist_scan.sh")
DEFAULT_LIMITS = os.path.join(".claude", "loops", "loop-limits.json")

# The same report-artifact rule ship_preflight.sh section 7 applies: never part of a fix.
ARTIFACT_RE = re.compile(r"^(plan|TASK|code-review-report|pre-pr-report)\.md$|-report\.md$")
TIERS = ("T0", "T1", "T2")
RANK = {t: i for i, t in enumerate(TIERS)}
TRIGGERS = ("symptom-only-fix", "tests-weakened", "blast-radius", "sensitive-area")


class Unclassifiable(Exception):
    """Anything that stops an honest classification. Always exit 2."""


def emit(doc, code):
    print(json.dumps(doc, indent=1))
    return code


def _config():
    try:
        sys.path.insert(0, FRAMEWORK_ROOT)
        from lib.adapters.config import load_config
        return load_config()
    except Exception:  # noqa: BLE001 — no stack.config removes the defaults, nothing else
        return None


def default_branch(stack):
    try:
        return (stack.get("project.default_branch") if stack else None) or None
    except Exception:  # noqa: BLE001
        return None


def limits_path(stack):
    if stack is not None:
        try:
            p = stack.resolve(stack.get("limits_file"), DEFAULT_LIMITS)
            if p is not None:
                return str(p)
        except Exception:  # noqa: BLE001
            pass
    return os.path.join(FRAMEWORK_ROOT, DEFAULT_LIMITS)


def _pos_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def read_limits(path):
    try:
        with open(path) as fh:
            block = json.load(fh).get("risk_reclass")
    except Exception as e:  # noqa: BLE001
        raise Unclassifiable(f"limits file unreadable ({path}): {e}")
    if not isinstance(block, dict):
        raise Unclassifiable(f"no risk_reclass block in {path} — no decided numbers, failing closed")
    factor = block.get("scope_surprise_factor")
    by_tier = block.get("human_approvals_by_tier")
    gate_min = block.get("human_gate_min_approvals")
    if not _pos_int(factor):
        raise Unclassifiable(f"risk_reclass.scope_surprise_factor={factor!r} is not an integer >= 1")
    if not isinstance(by_tier, dict) or not all(_pos_int(by_tier.get(t)) for t in TIERS):
        raise Unclassifiable(f"risk_reclass.human_approvals_by_tier={by_tier!r} needs T0, T1 and T2 "
                             "as integers >= 1 (a loop PR is never merged without a human)")
    if not _pos_int(gate_min):
        raise Unclassifiable(f"risk_reclass.human_gate_min_approvals={gate_min!r} is not an integer >= 1")
    return factor, {t: by_tier[t] for t in TIERS}, gate_min


def load_json(path):
    """An optional seam file: None when absent; an unreadable one is a fact we cannot use."""
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path) as fh:
            d = json.load(fh)
        return (d if isinstance(d, dict) else None), (None if isinstance(d, dict) else
                                                       f"{os.path.basename(path)} is not an object — ignored")
    except Exception as e:  # noqa: BLE001
        return None, f"{os.path.basename(path)} unreadable ({e.__class__.__name__}) — ignored"


def load_decide():
    spec = importlib.util.spec_from_file_location("assign_tier", ASSIGN_TIER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.decide


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise Unclassifiable("git %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return r.stdout


def conventions(stack):
    try:
        rel = stack.get("stack.conventions_file", ".claude/loops/conventions.json")
        p = stack.resolve(rel)
        with open(p) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def resolve_projects(repo, base, files):
    cmd = [sys.executable, RESOLVE, "--base", base]
    if files is not None:
        cmd += list(files) if files else ["--stdin"]
    else:
        cmd += ["--cwd", repo]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, input="" if files == [] else None)
    try:
        d = json.loads(r.stdout)
    except Exception:  # noqa: BLE001
        d = None
    if r.returncode != 0 or not isinstance(d, dict) or "changed_files" not in d:
        raise Unclassifiable("resolve_projects.py failed (exit %s): %s" % (r.returncode, (r.stdout or r.stderr).strip()[:300]))
    return d


def tests_weakened(repo, merge_base, test_rx):
    """Existing test files deleted or with removed lines, worktree vs merge-base.
    A NEWLY added test file never counts: it does not exist at the merge-base."""
    if test_rx is None:
        return []
    out = git(repo, "diff", "--no-renames", "--numstat", merge_base)
    hits = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if not test_rx.search(path):
            continue
        existed = subprocess.run(["git", "-C", repo, "cat-file", "-e", f"{merge_base}:{path}"],
                                 capture_output=True).returncode == 0
        if not existed:
            continue
        # "-" = binary: removal cannot be ruled out, so it counts (err toward a human).
        if removed == "-" or (removed.isdigit() and int(removed) > 0):
            hits.append(path)
    return sorted(set(hits))


def denylist(files):
    r = subprocess.run(["bash", R6SCAN, "--stdin"], input="\n".join(files) + ("\n" if files else ""),
                       capture_output=True, text=True, timeout=60)
    if r.returncode == 2:
        raise Unclassifiable("R6 denylist scan could not run (exit 2): " + r.stdout.strip()[:300])
    if r.returncode not in (0, 3):
        raise Unclassifiable(f"R6 denylist scan exited {r.returncode}")
    try:
        hits = json.loads(r.stdout).get("denylisted") or []
    except Exception:  # noqa: BLE001
        raise Unclassifiable("R6 denylist scan printed no JSON")
    if r.returncode == 3 and not hits:
        raise Unclassifiable("R6 denylist scan reported a hit without naming it")
    return [h.get("file") for h in hits if isinstance(h, dict)]


def triage_tier_of(arg, context, intake):
    if arg:
        return arg
    seen = []
    for doc in (context, intake):
        if doc and doc.get("tier") in RANK:
            seen.append(doc["tier"])
    for h in (context or {}).get("tier_history") or []:
        if isinstance(h, dict) and h.get("tier") in RANK:
            seen.append(h["tier"])
    return max(seen, key=RANK.get) if seen else None


def prior_attempts(intake):
    pa = intake.get("prior_attempts")
    n = pa.get("attempt_count") if isinstance(pa, dict) else 0
    return n if isinstance(n, int) and not isinstance(n, bool) else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evid", required=True, help="<EVID_ROOT>/<KEY> — reads context/intake/fix.json")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default=None)
    ap.add_argument("--limits", default=None)
    ap.add_argument("--triage-tier", choices=TIERS, default=None)
    ap.add_argument("--changed-file", action="append", default=None)
    try:
        a = ap.parse_args()
    except SystemExit:
        return emit({"error": "bad usage"}, 2)

    stack = _config()
    try:
        factor, by_tier, gate_min = read_limits(a.limits or limits_path(stack))
        base = a.base or default_branch(stack)
        warnings = []
        context, w1 = load_json(os.path.join(a.evid, "context.json"))
        intake, w2 = load_json(os.path.join(a.evid, "intake.json"))
        fix, w3 = load_json(os.path.join(a.evid, "fix.json"))
        warnings += [w for w in (w1, w2, w3) if w]
        context, intake, fix = context or {}, intake or {}, fix or {}

        # ---- the diff --------------------------------------------------------------------
        if a.changed_file is not None:
            res = resolve_projects(a.repo, base or "HEAD", a.changed_file)
            merge_base = None
        else:
            if not base:
                raise Unclassifiable("cannot determine the base branch (no --base and no "
                                     "project.default_branch in stack.config.json)")
            if not os.path.isdir(a.repo):
                raise Unclassifiable(f"--repo {a.repo} is not a directory")
            merge_base = git(a.repo, "merge-base", base, "HEAD").strip()
            res = resolve_projects(a.repo, base, None)
        changed = [f for f in res["changed_files"] if not ARTIFACT_RE.search(f)]
        conv = conventions(stack) if stack else {}
        test_rx = re.compile(conv["test_path_regex"]) if conv.get("test_path_regex") else None
        tests = [f for f in changed if test_rx and test_rx.search(f)]
        source = [f for f in changed if f not in tests]
        projects = sorted({p["name"] for p in res.get("projects") or []
                           if any(not (test_rx and test_rx.search(f)) and not ARTIFACT_RE.search(f)
                                  for f in p.get("changed_files") or [])})
        shared_hits = [f for f in res.get("shared_core_hits") or [] if f in changed]

        # ---- the tier ----------------------------------------------------------------------
        triage = triage_tier_of(a.triage_tier, context, intake)
        baseline = triage or "T1"
        gherkin = [g for g in context.get("gherkin") or [] if isinstance(g, dict)]
        layer = str(context.get("layer") or "").strip().lower()
        if len(projects) >= 2 and layer not in ("both+data", "data+both", "all"):
            layer = "both"
        facts = {
            "issue_type": intake.get("issue_type"),
            "risk_class": intake.get("risk_class") or "standard",
            "sensitive": intake.get("sensitive") is True,
            "prior_attempts": prior_attempts(intake),
            "files": len(source),
            "layer": layer,
            "shared_core": bool(shared_hits),
            "migration": False,
        }
        if gherkin:
            facts["scenarios"] = len(gherkin)
            roles = {g.get("role") for g in gherkin if g.get("role")}
            pages = {g.get("page") for g in gherkin if g.get("page")}
            if roles:
                facts["roles"] = len(roles)
            if pages:
                facts["pages"] = len(pages)
        diff_tier, reasons, esc = load_decide()(facts)
        reasons = [f"diff: {r}" for r in list(reasons) + list(esc)]

        lead = reasons[0] if reasons else "diff facts"
        likely = [f for f in context.get("likely_files") or [] if isinstance(f, str)]
        surprise = None
        if likely and len(source) > factor * len(likely):
            surprise = TIERS[min(RANK[baseline] + 1, 2)]
            reasons.append(f"scope surprise: {len(source)} non-test files touched > {factor} x "
                           f"{len(likely)} likely_files estimated at triage")
            if RANK[surprise] > RANK[diff_tier]:
                lead = reasons[-1]
        if len(projects) >= 2:
            reasons.append(f"cross-layer: the diff spans {len(projects)} projects ({', '.join(projects)})")

        tier = max([baseline, diff_tier] + ([surprise] if surprise else []), key=RANK.get)
        escalated = RANK[tier] > RANK[baseline]
        if not escalated:
            reasons.insert(0, f"tier {tier} held: the diff does not exceed the "
                              f"{'triage' if triage else 'untiered (T1)'} tier")

        # ---- the second check: a human anyway -------------------------------------------------
        weakened = [] if merge_base is None else tests_weakened(a.repo, merge_base, test_rx)
        deny_hits = denylist(changed)
        triggers = []
        if fix.get("is_root_cause") is False:
            triggers.append("symptom-only-fix")
        if weakened:
            triggers.append("tests-weakened")
        if deny_hits:
            triggers.append("blast-radius")
        if intake.get("sensitive") is True:
            triggers.append("sensitive-area")
        demanded = bool(triggers)
        approvals = by_tier[tier]
        if demanded:
            approvals = max(approvals, gate_min)

        if escalated:
            warnings.append(f"Risk tier rose from {triage or 'untiered (T1)'} to {tier} on the actual diff "
                            f"({lead}); the PR needs {approvals} human approval(s). Verification "
                            "was not re-run at the higher tier's breadth.")
        if demanded:
            warnings.append(f"Human gate demanded ({', '.join(triggers)}): the PR needs at least "
                            f"{gate_min} human approvals whatever the tier.")
        risk = {
            "triage_tier": triage,
            "diff_tier": diff_tier,
            "tier": tier,
            "escalated": escalated,
            "reasons": reasons,
            "facts": {
                "files": len(source),
                "test_files": len(tests),
                "projects": projects,
                "likely_files": len(likely),
                "shared_core_hits": shared_hits,
                "denylist_hits": deny_hits,
                "tests_weakened": weakened,
                "merge_base": merge_base,
                "diff_source": "changed-file" if a.changed_file is not None else "git",
            },
            "human_gate": {"demanded": demanded, "triggers": triggers},
            "human_approvals_required": approvals,
            "agent_approval_counts_as_human": False,
        }
        return emit({"risk": risk, "warnings": warnings}, 0)
    except Unclassifiable as e:
        return emit({"error": str(e)}, 2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — never let the classifier crash open
        print(json.dumps({"error": f"risk_reclass crashed: {e.__class__.__name__}: {e}"}))
        sys.exit(2)
