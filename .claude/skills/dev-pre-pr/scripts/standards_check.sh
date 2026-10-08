#!/usr/bin/env bash
# Conventions scan — the DETERMINISTIC half of dev-pre-pr's readiness gate.
# Rules live in the target repo's conventions file (stack.config `stack.conventions_file`,
# default .claude/loops/conventions.json — its `rules` array; format documented in
# ../references/violation-rules.md). It reads ONLY the lines this branch ADDED, so it can
# never blame the author for pre-existing code — the single biggest source of false
# positives in a whole-file scan, and the thing that makes developers stop reading a report.
#
# Usage:
#   standards_check.sh                       scan working tree vs the merge-base with project.default_branch
#   standards_check.sh --base release/2      use a different base branch
#   standards_check.sh --files a.ext b.ext   scan an explicit list (all lines treated as added)
#   [--rules FILE] [--repo DIR]              override the conventions file / the repo to scan
#
# Config (read through lib/adapters/config.py; every value has an env override LOOPS_CFG__…):
#   project.default_branch      default for --base            (no config + no --base = exit 2)
#   stack.conventions_file      default for --rules           (fallback: .claude/loops/conventions.json)
#   project.ticket_key_pattern  expands `{{ticket_key_pattern}}` inside a rule's regexes
#
# Prints one JSON object:
#   {"ok":bool,"base":"...","rules_file":"...","scanned":N,"added_lines":N,"counts":{...},
#    "violations":[{file,line,severity,rule_id,rule,fix,snippet,heuristic,confirm_question?}],"notes":[...]}
# Exit codes: 0 = no blocking violations · 3 = at least one non-heuristic critical/high
#             2 = cannot scan (bad repo / missing or invalid rules) — FAIL CLOSED
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRAMEWORK_ROOT="${LOOPS_REPO_ROOT:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"
RULES=""; BASE=""; REPO="."; MODE="diff"; FILES=()
while [ $# -gt 0 ]; do case "$1" in
  --base)  BASE="$2"; shift 2;;
  --rules) RULES="$2"; shift 2;;
  --repo)  REPO="$2"; shift 2;;
  --files) MODE="files"; shift; while [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; do FILES+=("$1"); shift; done;;
  *) printf '{"ok":false,"error":"unknown arg %s"}\n' "$1"; exit 2;;
esac; done

if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  printf '{"ok":false,"error":"not a git repo: %s — FAIL CLOSED"}\n' "$REPO"; exit 2
fi

FRAMEWORK_ROOT="$FRAMEWORK_ROOT" RULES="$RULES" BASE="$BASE" REPO="$REPO" MODE="$MODE" \
FILELIST="$(printf '%s\n' "${FILES[@]:-}")" python3 - <<'PY'
import json, os, re, subprocess, sys

REPO  = os.environ["REPO"]
MODE  = os.environ["MODE"]
notes = []

# ---------------------------------------------------------------- resolve config defaults
# stack.config.json is the source of the base branch, the conventions file and the ticket-key
# pattern. A missing/unreadable stack.config is NOT fatal for an explicit --rules/--base run
# (the external caller may be scanning an arbitrary checkout); it only removes the defaults.
sys.path.insert(0, os.environ["FRAMEWORK_ROOT"])
default_branch, rules_default, ticket_key_pattern = None, None, None
try:
    from lib.adapters.config import load_config
    _stack = load_config()
    default_branch = _stack.get("project.default_branch")
    ticket_key_pattern = _stack.get("project.ticket_key_pattern")
    rules_default = _stack.resolve(_stack.get("stack.conventions_file"), ".claude/loops/conventions.json")
except Exception as e:                                        # noqa: BLE001 — defaults only
    notes.append(f"stack.config.json not loaded ({e.__class__.__name__}); using built-in defaults")
    rules_default = os.path.join(os.environ["FRAMEWORK_ROOT"], ".claude", "loops", "conventions.json")

BASE  = os.environ["BASE"] or default_branch
RULES = os.environ["RULES"] or str(rules_default)
if MODE != "files" and not BASE:
    print(json.dumps({"ok": False, "error": "cannot determine the base branch: pass --base or set "
                      "project.default_branch in stack.config.json — FAIL CLOSED", "notes": notes}))
    sys.exit(2)

def git(*args, ok_fail=False):
    p = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)
    if p.returncode != 0 and not ok_fail:
        return None
    return p.stdout

# ---------------------------------------------------------------- load the ruleset
if not os.path.isfile(RULES):
    print(json.dumps({"ok": False, "error": f"rules file not found: {RULES} — FAIL CLOSED"}))
    sys.exit(2)
try:
    spec = json.load(open(RULES, encoding="utf-8"))
except Exception as e:                                        # a broken rule file must not
    print(json.dumps({"ok": False, "error": f"invalid rules json: {e}"}))  # silently pass the gate
    sys.exit(2)
if not isinstance(spec, dict) or not isinstance(spec.get("rules"), list):
    print(json.dumps({"ok": False, "error": f"{RULES} has no `rules` array — FAIL CLOSED"}))
    sys.exit(2)

MARKER = spec.get("suppress_marker") or "standards:allow"
TESTRE = re.compile(spec.get("test_path_regex") or r"(?!x)x")

# `{{ticket_key_pattern}}` inside a regex expands to project.ticket_key_pattern with its
# anchors stripped, so a rule can say "a TODO without a ticket key" without hard-coding
# any tracker's key shape.
_tk = (ticket_key_pattern or r"[A-Z][A-Z0-9]+-[0-9]+").strip()
_tk = _tk[1:] if _tk.startswith("^") else _tk
_tk = _tk[:-1] if _tk.endswith("$") else _tk
def expand(rx):
    return rx.replace("{{ticket_key_pattern}}", f"(?:{_tk})")

rules = []
for r in spec["rules"]:
    if not isinstance(r, dict):
        notes.append(f"rule entry {r!r} is not an object and was skipped")
        continue
    if not r.get("include_path"):
        notes.append(f"rule {r.get('id')} has no include_path and was skipped")
        continue
    try:
        c = dict(r)
        c["_include"] = re.compile(expand(r["include_path"]))
        c["_exclude"] = re.compile(expand(r["exclude_path"])) if r.get("exclude_path") else None
        c["_pat"]     = re.compile(expand(r["pattern"])) if r.get("pattern") else None
        c["_absent"]  = re.compile(expand(r["absent_pattern"])) if r.get("absent_pattern") else None
        rules.append(c)
    except re.error as e:
        notes.append(f"rule {r.get('id')} has an invalid regex and was skipped: {e}")

# ---------------------------------------------------------------- collect added lines
# added[path] = [(lineno, text), ...] — ONLY lines this branch introduced.
added, base_desc = {}, BASE

if MODE == "files":
    base_desc = "explicit file list"
    for f in [l.strip() for l in os.environ["FILELIST"].splitlines() if l.strip()]:
        try:
            with open(os.path.join(REPO, f), encoding="utf-8", errors="replace") as fh:
                added[f] = [(i, l.rstrip("\n")) for i, l in enumerate(fh, 1)]
        except OSError as e:
            notes.append(f"could not read {f}: {e}")
else:
    mb = git("merge-base", BASE, "HEAD", ok_fail=True)
    ref = (mb or "").strip() or BASE
    base_desc = f"{BASE} (merge-base {ref[:12]})" if mb and mb.strip() else BASE
    diff = git("diff", "-U0", "--no-color", ref)
    if diff is None:
        print(json.dumps({"ok": False, "error": f"git diff against '{ref}' failed — FAIL CLOSED"}))
        sys.exit(2)
    path, lineno = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            path = None if p == "/dev/null" else re.sub(r"^b/", "", p)
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++") and path:
            added.setdefault(path, []).append((lineno, line[1:]))
            lineno += 1
    # untracked files are new work too — every line counts as added
    for f in (git("ls-files", "--others", "--exclude-standard") or "").splitlines():
        f = f.strip()
        if not f:
            continue
        try:
            with open(os.path.join(REPO, f), encoding="utf-8", errors="replace") as fh:
                added.setdefault(f, []).extend((i, l.rstrip("\n")) for i, l in enumerate(fh, 1))
        except OSError:
            pass

# ---------------------------------------------------------------- apply the rules
def full_text(path):
    try:
        with open(os.path.join(REPO, path), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return git("show", f"HEAD:{path}", ok_fail=True) or ""

tracked = set((git("ls-files") or "").splitlines())
violations, added_lines = [], sum(len(v) for v in added.values())

def applies(rule, path):
    if not rule["_include"].search(path):
        return False
    if rule["_exclude"] and rule["_exclude"].search(path):
        return False
    if rule.get("skip_tests") and TESTRE.search(path):
        return False
    return True

def record(rule, path, line, snippet):
    v = {
        "file": path, "line": line, "severity": rule.get("severity", "medium"),
        "rule_id": rule.get("id", "unnamed-rule"), "rule": rule.get("rule", rule.get("id", "")),
        "fix": rule.get("fix", ""), "snippet": snippet.strip()[:200],
        "heuristic": bool(rule.get("heuristic")),
    }
    if rule.get("confirm_question"):
        v["confirm_question"] = rule["confirm_question"]
    violations.append(v)

def sibling_name(rule, path):
    """The cross-file check: `{stem}` is the basename minus extension and minus `strip_suffix`;
    `{ext}` keeps the dot; `{dir}` is the file's directory; `{name}` the full basename."""
    d, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    suffix = rule.get("strip_suffix") or ""
    if suffix and stem.endswith(suffix):
        stem = stem[: -len(suffix)]
    tmpl = rule.get("sibling_template") or "{stem}{ext}"
    return tmpl.format(stem=stem, ext=ext, dir=d, name=name)

for path, lines in sorted(added.items()):
    for rule in rules:
        if not applies(rule, path):
            continue
        kind = rule.get("type", "line")

        if kind == "sibling_file":
            want = sibling_name(rule, path)
            want_base = os.path.basename(want)
            if "/" in want:                                      # a path template: match the relative path
                found = any(t == want or t.endswith("/" + want) for t in tracked) \
                     or any(p == want or p.endswith("/" + want) for p in added)
            else:                                                # a bare name: anywhere in the repo or the change set
                found = any(os.path.basename(t) == want_base for t in tracked) \
                     or any(os.path.basename(p) == want_base for p in added)
            if not found:
                record(rule, path, 1, f"no {want} found for {os.path.basename(path)}")
            continue

        if rule["_pat"] is None:
            continue
        if kind == "file_absent" and rule["_absent"] and rule["_absent"].search(full_text(path)):
            continue                                            # the guard IS present — not a violation

        for lineno, text in lines:
            if MARKER in text:
                continue
            if rule["_pat"].search(text):
                record(rule, path, lineno, text)
                if kind == "file_absent":
                    break                                       # one finding per file is enough

SEV = ("critical", "high", "medium", "low")
for v in violations:
    if v["severity"] not in SEV:
        notes.append(f"rule {v['rule_id']} has severity {v['severity']!r}; treated as medium")
        v["severity"] = "medium"
counts = {s: sum(1 for v in violations if v["severity"] == s) for s in SEV}
blocking = [v for v in violations if v["severity"] in ("critical", "high") and not v["heuristic"]]
sev_rank = {s: i for i, s in enumerate(SEV)}
violations.sort(key=lambda v: (sev_rank[v["severity"]], v["heuristic"], v["file"], v["line"]))

print(json.dumps({
    "ok": not blocking,
    "base": base_desc,
    "rules_file": RULES,
    "scanned": len(added),
    "added_lines": added_lines,
    "counts": counts,
    "blocking": len(blocking),
    "heuristic": sum(1 for v in violations if v["heuristic"]),
    "violations": violations,
    "notes": notes,
}, indent=2))
sys.exit(3 if blocking else 0)
PY
