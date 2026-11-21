#!/usr/bin/env python3
"""Prove a candidate conventions rule before it is allowed anywhere near the gate.

dev-harden turns a recurring correction into a check that FAILS, instead of one more line of
prose. When the cheapest check that can catch the mistake is a conventions rule (the
`rules` array dev-pre-pr's standards scan reads), this script is the deterministic half of
that step. It never decides WHETHER a correction deserves a rule; it decides whether a given
rule is well formed, does what its author claims, and how many copies of the mistake already
sit in the target repo.

It reuses the real scanner (dev-pre-pr/scripts/standards_check.sh in --files mode) for every
match, so a rule proven here behaves exactly the same at the gate. Two sources of matching
logic would disagree within a month.

Usage:
  rule_probe.py --candidate cand.json [--target-repo DIR] [--conventions FILE] [--max-hits N]
  rule_probe.py --candidate cand.json --apply --by NAME [--artifact harden.json] [--conventions FILE] [--target-repo DIR]
  rule_probe.py --audit [--conventions FILE]

cand.json (a collect_candidates.py candidate works as is: `candidate_rule` is read as `rule`):
  { "rule": { ...one conventions rule, format in dev-pre-pr/references/violation-rules.md... },
    "fixtures": { "must_trip": [ {"path": "src/menu.tsx", "text": "setTimeout(close, 0)"} ],
                  "must_pass": [ {"path": "src/menu.tsx", "text": "close()"} ] } }

Checks, each a failure CLASS (principle 3: classify before adapting):
  invalid-rule      a required field is missing or off-enum, or a regex does not compile
  weak-message      `fix` is missing, shorter than 40 chars, or just repeats the `rule` label.
                    The message is the part the next agent actually reads; it must say what
                    to do instead.
  id-collision      the rule id already exists in the conventions file
  no-fixtures       fewer than one must_trip and one must_pass fixture
  misses-positive   a must_trip fixture did not fire
  false-positive    a must_pass fixture fired
  scan-error        the scanner could not run (its exit 2)

Inventory (only with --target-repo): every tracked file the rule applies to is scanned as if
all its lines were added. The hits are the broken windows: copies an agent will find and
imitate even after the rule exists, because the gate only reads ADDED lines. They are
reported, never fixed here.

--audit runs the static checks (invalid-rule, weak-message, duplicate ids) over EVERY rule
already in the conventions file, so a rule whose message does not tell the agent what to do
instead is caught even if it predates this skill. No fixtures, no scan.

--apply appends the proven rule to the conventions file. It is the human step, the same way
`kb bless --by` is: it refuses without --by, re-runs the probe first, refuses on any failure
class, and stamps a `_comment_provenance` key (keys starting with `_comment` are ignored by
every reader of the file). With --artifact it also flips that harden.json to rule-applied and
fills `applied: {by, at}`, so collect_candidates.py never offers the correction again.

Prints one JSON object. Exit 0 = probe passed (and applied, with --apply) ·
3 = probe failed (see `failures`) · 2 = could not run (bad args, unreadable input).
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.abspath(os.path.join(HERE, "..", ".."))
FRAMEWORK_ROOT = os.environ.get("LOOPS_REPO_ROOT") or os.path.abspath(os.path.join(SKILLS, "..", ".."))
SCAN = os.path.join(SKILLS, "dev-pre-pr", "scripts", "standards_check.sh")
DEFAULT_CONVENTIONS = os.path.join(FRAMEWORK_ROOT, ".claude", "loops", "conventions.json")

SEVERITIES = {"critical", "high", "medium", "low"}
TYPES = {"line", "file_absent", "sibling_file"}
MIN_FIX_CHARS = 40
CHUNK = 400  # files per scanner call, keeps the argv well under any OS limit


def out(obj, code):
    print(json.dumps(obj, indent=2))
    sys.exit(code)


def load_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        out({"ok": False, "error": f"cannot read {what} {path}: {e}"}, 2)


def ticket_pattern():
    try:
        sys.path.insert(0, FRAMEWORK_ROOT)
        from lib.adapters.config import load_config  # noqa: E402
        return load_config().get("project.ticket_key_pattern")
    except Exception:  # noqa: BLE001 (default only)
        return None


def expand(rx, tk):
    return rx.replace("{{ticket_key_pattern}}", f"(?:{(tk or r'[A-Z][A-Z0-9]+-[0-9]+').strip().strip('^$')})")


def check_shape(rule, existing_ids, tk):
    """Static checks. Returns a list of {class, detail}."""
    f = []
    if not isinstance(rule, dict):
        return [{"class": "invalid-rule", "detail": "rule is not an object"}]
    rtype = rule.get("type", "line")
    for key in ("id", "severity", "include_path", "rule"):
        if not rule.get(key):
            f.append({"class": "invalid-rule", "detail": f"missing `{key}`"})
    if rule.get("severity") and rule["severity"] not in SEVERITIES:
        f.append({"class": "invalid-rule", "detail": f"severity {rule['severity']!r} not in {sorted(SEVERITIES)}"})
    if rtype not in TYPES:
        f.append({"class": "invalid-rule", "detail": f"type {rtype!r} not in {sorted(TYPES)}"})
    if rtype in ("line", "file_absent") and not rule.get("pattern"):
        f.append({"class": "invalid-rule", "detail": f"type {rtype} needs `pattern`"})
    if rtype == "file_absent" and not rule.get("absent_pattern"):
        f.append({"class": "invalid-rule", "detail": "type file_absent needs `absent_pattern`"})
    if rtype == "sibling_file" and not rule.get("sibling_template"):
        f.append({"class": "invalid-rule", "detail": "type sibling_file needs `sibling_template`"})
    if rule.get("heuristic") and not rule.get("confirm_question"):
        f.append({"class": "invalid-rule", "detail": "heuristic:true needs a `confirm_question`"})
    for key in ("include_path", "exclude_path", "pattern", "absent_pattern"):
        if rule.get(key):
            try:
                re.compile(expand(rule[key], tk))
            except re.error as e:
                f.append({"class": "invalid-rule", "detail": f"`{key}` does not compile: {e}"})
    fix = (rule.get("fix") or "").strip()
    if len(fix) < MIN_FIX_CHARS:
        f.append({"class": "weak-message",
                  "detail": f"`fix` is {len(fix)} chars; it must tell the next agent what to do instead "
                            f"(at least {MIN_FIX_CHARS})"})
    elif fix.lower().rstrip(".") == (rule.get("rule") or "").strip().lower().rstrip("."):
        f.append({"class": "weak-message", "detail": "`fix` only repeats the `rule` label"})
    if rule.get("id") and rule["id"] in existing_ids:
        f.append({"class": "id-collision", "detail": f"id {rule['id']!r} already exists in the conventions file"})
    return f


def write_rules(path, base_spec, rule):
    spec = {k: v for k, v in base_spec.items() if k != "rules"}
    spec.setdefault("suppress_marker", "standards:allow")
    spec["rules"] = [rule]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh)


def scan_files(repo, rules_file, files):
    """Run the real scanner in --files mode. Returns (violations, error)."""
    hits = []
    for i in range(0, len(files), CHUNK):
        chunk = files[i:i + CHUNK]
        p = subprocess.run(["bash", SCAN, "--rules", rules_file, "--repo", repo, "--files", *chunk],
                           capture_output=True, text=True)
        if p.returncode == 2:
            return hits, (p.stdout.strip() or p.stderr.strip() or "scanner exit 2")[:500]
        try:
            hits.extend(json.loads(p.stdout).get("violations", []))
        except ValueError:
            return hits, f"scanner printed non-JSON: {p.stdout[:200]!r}"
    return hits, None


def run_fixtures(rule, fixtures, base_spec, rules_file):
    f = []
    trip, ok = fixtures.get("must_trip") or [], fixtures.get("must_pass") or []
    if not trip or not ok:
        return [{"class": "no-fixtures",
                 "detail": "need at least one must_trip and one must_pass fixture: a rule tested "
                           "only positive is a false-positive generator"}], {"must_trip": 0, "must_pass": 0}
    tmp = tempfile.mkdtemp(prefix="harden-fx-")
    try:
        subprocess.run(["git", "init", "-q", tmp], check=True)
        for kind, items in (("must_trip", trip), ("must_pass", ok)):
            for n, fx in enumerate(items):
                rel = fx.get("path") or f"fixture_{n}.txt"
                if os.path.isabs(rel) or ".." in rel.split("/"):
                    f.append({"class": "invalid-rule", "detail": f"fixture path {rel!r} must be relative"})
                    continue
                case = os.path.join(tmp, kind, str(n))
                full = os.path.join(case, rel)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write(fx.get("text", ""))
                # scan each fixture alone, from its own root, so its path is what the rule sees
                subprocess.run(["git", "init", "-q", case], check=True)
                hits, err = scan_files(case, rules_file, [rel])
                if err:
                    f.append({"class": "scan-error", "detail": err})
                    continue
                fired = any(h.get("rule_id") == rule.get("id") for h in hits)
                if kind == "must_trip" and not fired:
                    f.append({"class": "misses-positive", "detail": f"must_trip fixture {n} ({rel}) did not fire"})
                if kind == "must_pass" and fired:
                    f.append({"class": "false-positive", "detail": f"must_pass fixture {n} ({rel}) fired"})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return f, {"must_trip": len(trip), "must_pass": len(ok)}


def inventory(rule, target, rules_file, test_rx, tk, max_hits):
    p = subprocess.run(["git", "-C", target, "ls-files"], capture_output=True, text=True)
    if p.returncode != 0:
        return None, f"not a git repo: {target}"
    inc = re.compile(expand(rule["include_path"], tk))
    exc = re.compile(expand(rule["exclude_path"], tk)) if rule.get("exclude_path") else None
    tre = re.compile(test_rx) if (rule.get("skip_tests") and test_rx) else None
    files = [x for x in p.stdout.splitlines()
             if x and inc.search(x) and not (exc and exc.search(x)) and not (tre and tre.search(x))
             and os.path.isfile(os.path.join(target, x))]
    hits, err = scan_files(target, rules_file, files)
    if err:
        return None, err
    hits = [h for h in hits if h.get("rule_id") == rule.get("id")]
    return {"files_scanned": len(files), "count": len(hits),
            "files_with_hits": len({h["file"] for h in hits}),
            "sample": [{"file": h["file"], "line": h.get("line"), "snippet": h.get("snippet", "")}
                       for h in hits[:max_hits]]}, None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--candidate", default=None)
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--conventions", default=None)
    ap.add_argument("--target-repo", default=None)
    ap.add_argument("--max-hits", type=int, default=25)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--artifact", default=None, help="with --apply: the harden.json to mark rule-applied")
    ap.add_argument("--by", default=None)
    ap.add_argument("--lesson-ref", default=None, help="where the correction came from (provenance)")
    a = ap.parse_args()

    if not os.path.isfile(SCAN):
        out({"ok": False, "error": f"scanner not found at {SCAN}"}, 2)
    if a.apply and not (a.by or "").strip():
        out({"ok": False, "error": "--apply needs --by NAME: adding a rule to the gate is a human step"}, 2)

    conv_path = a.conventions or DEFAULT_CONVENTIONS
    spec = load_json(conv_path, "conventions file")
    if not isinstance(spec.get("rules"), list):
        out({"ok": False, "error": f"{conv_path} has no `rules` array"}, 2)
    if a.audit:
        tk = ticket_pattern()
        report, seen = [], set()
        for n, r in enumerate(spec["rules"]):
            fails = check_shape(r, set(), tk)
            rid = r.get("id") if isinstance(r, dict) else None
            if rid in seen:
                fails.append({"class": "id-collision", "detail": f"id {rid!r} appears more than once"})
            seen.add(rid)
            if fails:
                report.append({"index": n, "rule_id": rid, "failures": fails})
        out({"ok": not report, "status": "audit-pass" if not report else "audit-fail",
             "conventions": conv_path, "rules": len(spec["rules"]), "failing": report}, 0 if not report else 3)
    if not a.candidate:
        out({"ok": False, "error": "--candidate is required (or pass --audit)"}, 2)
    cand = load_json(a.candidate, "candidate")
    # Accept a collect_candidates.py candidate as is (candidate_rule), so nothing is hand-copied
    # between collecting and probing.
    rule = cand.get("rule") if cand.get("rule") is not None else cand.get("candidate_rule")
    fixtures = cand.get("fixtures") or {}
    tk = ticket_pattern()
    existing = {r.get("id") for r in spec["rules"] if isinstance(r, dict)}

    failures = check_shape(rule, existing, tk)
    result = {"ok": False, "status": "probe-fail", "rule_id": (rule or {}).get("id") if isinstance(rule, dict) else None,
              "conventions": conv_path, "failures": failures, "fixtures": None, "inventory": None,
              "would_block": False, "applied": None}

    if not any(x["class"] == "invalid-rule" for x in failures):
        work = tempfile.mkdtemp(prefix="harden-rules-")
        try:
            rules_file = os.path.join(work, "rules.json")
            write_rules(rules_file, spec, rule)
            fx_fail, counts = run_fixtures(rule, fixtures, spec, rules_file)
            failures.extend(fx_fail)
            result["fixtures"] = counts
            if a.target_repo:
                inv, err = inventory(rule, os.path.abspath(a.target_repo), rules_file,
                                     spec.get("test_path_regex"), tk, a.max_hits)
                if err:
                    failures.append({"class": "scan-error", "detail": f"inventory: {err}"})
                result["inventory"] = inv
        finally:
            shutil.rmtree(work, ignore_errors=True)

    result["would_block"] = bool(isinstance(rule, dict) and not rule.get("heuristic")
                                 and rule.get("severity") in ("critical", "high"))
    if failures:
        out(result, 3)
    result["ok"], result["status"] = True, "probe-pass"

    if a.apply:
        stamped = dict(rule)
        stamped["_comment_provenance"] = (
            f"added by dev-harden; approved by {a.by.strip()} on "
            f"{datetime.date.today().isoformat()}" + (f"; from {a.lesson_ref}" if a.lesson_ref else ""))
        spec["rules"].append(stamped)
        tmp = conv_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, conv_path)
        result["applied"] = {"by": a.by.strip(), "file": conv_path}
        if a.artifact:
            art = load_json(a.artifact, "harden artifact")
            art["status"] = "rule-applied"
            art["applied"] = {"by": a.by.strip(),
                              "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            art["rule"] = rule
            tmp = a.artifact + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(art, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, a.artifact)
            result["applied"]["artifact"] = a.artifact
    out(result, 0)


if __name__ == "__main__":
    main()
