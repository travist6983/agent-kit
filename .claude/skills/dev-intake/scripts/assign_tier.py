#!/usr/bin/env python3
"""Assign a complexity TIER and a per-step MODEL plan from evidence the ticket already carries.

WHY THIS EXISTS. Tier was being judged by the ticket TITLE. The title is the one part of a
defect report that is written before anyone understands the defect, so a one-file, few-line
fix and a many-file shared-module change get the same path when both have a one-line title.

Triage cannot be made perfect, which is why escalate-never-de-escalate is load-bearing rather
than decorative: the design makes being wrong recoverable instead of pretending it will not
happen. A run may move UP a tier mid-flight; it never moves down.

  T0  trivial   ALL of: defect-type · single layer · <=3 files · <=2 scenarios · 1 page · 1 role
                · area flake_rate < 0.30 · risk standard · no prior failed attempt
  T2  complex   ANY of: feature-type · >=8 files or layer both+data · >=5 scenarios or
                multi-role · sensitive risk · migration/shared-core · prior_attempts >= 1
  T1  standard  everything else — the tier you land in when unsure

Issue types are a BUILT-IN DEFAULT vocabulary, not config: defect-type = Bug | Defect,
feature-type = Story | Task | Epic, compared case-insensitively against --issue-type. A tracker
whose vocabulary differs (a Feature/Improvement type, or labels instead of a type field) is
normalized by dev-intake before this script runs: pass Bug for a defect-type ticket and Story
for a feature-type one. Any other string is "not a bug", which is never T0.
Sensitive risk classes default to billing / signup / auth / payments plus the meta-values
`sensitive` and `prerelease`; any other non-`standard` --risk value is also sensitive for the
T0 rule, so an adopter extends the list by passing its own class name.

TARGET_MINUTES is a starting band inherited from the framework's own runs — a baseline to
re-measure on your stack, not a measurement of it. Tune it here once you have runs of your own.

Usage:
  python3 assign_tier.py --issue-type Bug --layer BE --files 2 --scenarios 1 \\
      [--pages 1] [--roles 1] [--flake-rate 0.0] [--risk standard] [--prior-attempts 0] \\
      [--confidence 85] [--shared-core] [--migration] [--json]
  python3 assign_tier.py --from-json intake.json   # reads the fields it needs
  # e.g. PROJ-123 "Invoice total rounds the wrong way": --issue-type Bug --layer backend --files 1 --scenarios 1 → T0

Exit 0 always (a tier is always assigned); the decision is on stdout.
"""
import argparse
import json
import sys

SENSITIVE = ("billing", "signup", "auth", "payments", "sensitive", "prerelease")

# Confidence floors. Below the first, start one tier up; below the second, start at T2.
CONF_T1_FLOOR = 70
CONF_T2_FLOOR = 50


def decide(f):
    """f is a dict of normalised facts. Returns (tier, reasons, escalations)."""
    reasons = []
    issue_type = (f.get("issue_type") or "").strip().lower()
    layer = (f.get("layer") or "").strip().lower()
    files = f.get("files")
    scenarios = f.get("scenarios")
    pages = f.get("pages")
    roles = f.get("roles")
    flake = f.get("flake_rate")
    risk = (f.get("risk_class") or "standard").strip().lower()
    prior = f.get("prior_attempts") or 0
    conf = f.get("confidence")

    # ---- T2 triggers (ANY) ---------------------------------------------------------
    t2 = []
    if issue_type in ("story", "task", "epic"):
        t2.append(f"issue type {issue_type!r} — a feature is never T0: there is no bug to "
                  "reproduce and the solution space is open")
    if isinstance(files, int) and files >= 8:
        t2.append(f"{files} files likely touched (>= 8)")
    if layer in ("both+data", "data+both", "all"):
        t2.append(f"layer {layer!r} spans code and data")
    if isinstance(scenarios, int) and scenarios >= 5:
        t2.append(f"{scenarios} Gherkin scenarios (>= 5)")
    if isinstance(roles, int) and roles > 1:
        t2.append(f"{roles} roles involved — multi-role paths are not single-surface")
    if risk in SENSITIVE or f.get("sensitive"):
        t2.append(f"risk_class {risk!r} — sensitive areas never auto-ship")
    if f.get("migration"):
        t2.append("migration-adjacent (R6 blast radius)")
    if f.get("shared_core"):
        t2.append("touches a shared/core module (conventions.shared_core_globs)")
    if isinstance(prior, int) and prior >= 1:
        t2.append(f"{prior} prior loop attempt(s) — the identical strategy must not re-run")
    if t2:
        return "T2", t2, []

    # ---- T0 requires ALL --------------------------------------------------------------
    t0_fails = []
    if issue_type not in ("bug", "defect"):
        t0_fails.append(f"issue type {issue_type!r} is not a bug")
    if layer not in ("fe", "be", "frontend", "backend"):
        t0_fails.append(f"layer {layer!r} is not a single layer")
    if not isinstance(files, int) or files > 3:
        t0_fails.append(f"files={files} (T0 needs <= 3)")
    if not isinstance(scenarios, int) or scenarios > 2:
        t0_fails.append(f"scenarios={scenarios} (T0 needs <= 2)")
    if isinstance(pages, int) and pages > 1:
        t0_fails.append(f"{pages} pages — T0 verifies one surface")
    if isinstance(roles, int) and roles > 1:
        t0_fails.append(f"{roles} roles")
    if isinstance(flake, (int, float)) and flake >= 0.30:
        t0_fails.append(f"area flake_rate {flake} >= 0.30 (R7) — a flaky area needs the "
                        "retry headroom T0 does not have")
    if risk != "standard":
        t0_fails.append(f"risk_class {risk!r}")

    if not t0_fails:
        tier = "T0"
        reasons = ["Bug, single layer, small, single-surface, non-sensitive, clean area, "
                   "no prior attempt — every T0 condition met"]
    else:
        tier = "T1"
        reasons = ["not T0: " + "; ".join(t0_fails)]

    # ---- confidence floors: being unsure moves UP, never down -----------------------------
    esc = []
    if isinstance(conf, int):
        if conf < CONF_T2_FLOOR:
            esc.append(f"triage confidence {conf} < {CONF_T2_FLOOR} — starting at T2")
            tier = "T2"
        elif conf < CONF_T1_FLOOR and tier == "T0":
            esc.append(f"triage confidence {conf} < {CONF_T1_FLOOR} — starting at T1, not T0")
            tier = "T1"
    return tier, reasons, esc


# Model plan per tier. T0 is cheap because it has FEWER, SHORTER nodes — not because it
# thinks less about the code. The fix stays on the strong model at every tier, and the judges
# follow it: a judge weaker than its producer yields an invalid verdict, not merely a weaker one.
# Values are role ALIASES, not model ids: "sonnet" stands for the cheap role (stack.config
# models.cheap) and "opus" for the strong role (models.strong). The recipe resolves each alias
# through `models` in .claude/loops/stack.config.json; the alias string never reaches a runner.
MODELS = {
    "T0": {"intake": "sonnet", "context": "sonnet", "repro": "sonnet", "fix": "opus",
           "verify": "opus", "regression": "sonnet", "review": "opus",
           "pre_pr": "sonnet", "ship": "sonnet"},
    "T1": {"intake": "sonnet", "context": "sonnet", "repro": "opus", "fix": "opus",
           "verify": "opus", "regression": "sonnet", "review": "opus",
           "pre_pr": "sonnet", "ship": "sonnet"},
    "T2": {"intake": "sonnet", "context": "opus", "repro": "opus", "fix": "opus",
           "verify": "opus", "regression": "sonnet", "review": "opus",
           "pre_pr": "sonnet", "ship": "sonnet"},
}

CAPS = {
    "T0": {"repro": 1, "verify": 1, "fix": 2, "loop_total": 3},
    "T1": {"repro": 1, "verify": 2, "fix": 3, "loop_total": 5},
    "T2": {"repro": 2, "verify": 2, "fix": 3, "loop_total": 6},
}

# Starting bands, not a measurement of the adopter's stack (see the module docstring).
TARGET_MINUTES = {"T0": [8, 12], "T1": [14, 18], "T2": [25, 45]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-json", help="read facts from an intake.json-shaped file")
    ap.add_argument("--issue-type")
    ap.add_argument("--layer", help="a single layer name (frontend|backend, or fe|be) | both | data | both+data. "
                                    "A repo with no frontend/backend split is a single layer.")
    ap.add_argument("--files", type=int)
    ap.add_argument("--scenarios", type=int)
    ap.add_argument("--pages", type=int)
    ap.add_argument("--roles", type=int)
    ap.add_argument("--flake-rate", type=float)
    ap.add_argument("--risk", dest="risk_class", default="standard")
    ap.add_argument("--prior-attempts", type=int, default=0)
    ap.add_argument("--confidence", type=int)
    ap.add_argument("--shared-core", action="store_true")
    ap.add_argument("--migration", action="store_true")
    a = ap.parse_args()

    f = {}
    if a.from_json:
        try:
            d = json.load(open(a.from_json))
        except Exception as e:
            print(json.dumps({"error": f"cannot read {a.from_json}: {e}"}), file=sys.stderr)
            return 2
        f.update({
            "issue_type": d.get("issue_type"),
            "risk_class": d.get("risk_class", "standard"),
            "confidence": d.get("confidence"),
            "prior_attempts": (d.get("prior_attempts") or {}).get("attempt_count", 0),
        })
    for k in ("issue_type", "layer", "files", "scenarios", "pages", "roles",
              "flake_rate", "confidence"):
        v = getattr(a, k, None)
        if v is not None:
            f[k] = v
    if a.risk_class:
        f["risk_class"] = a.risk_class
    if a.prior_attempts:
        f["prior_attempts"] = a.prior_attempts
    f["shared_core"] = a.shared_core
    f["migration"] = a.migration

    tier, reasons, esc = decide(f)
    print(json.dumps({
        "tier": tier,
        "reasons": reasons,
        "auto_escalations": esc,
        "models": MODELS[tier],
        "retry_caps": CAPS[tier],
        "target_wall_clock_minutes": TARGET_MINUTES[tier],
        "facts": f,
        "rule": "Escalate, never de-escalate. A run may move UP a tier or model mid-flight "
                "(scope surprise, cross-layer impact, failed fix); it never moves down. "
                "Downgrades happen only here, at triage.",
    }, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
