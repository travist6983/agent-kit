#!/usr/bin/env python3
"""Survey a codebase deterministically: the onboarding loop's first look, with no model in it.

What it writes (contracts/dev-onboard-survey.json): languages by line count, build and package
manifests, the commands the repo itself declares (each with the file and line it came from),
entry points, tests, docs and decision records, churn hotspots from git history, and a first
cut of AREAS sized for one reader each, with the import edges between them where they can be
parsed. Every value is counted or read from a file. Nothing is guessed, so two runs on the same
commit write the same survey apart from timing.

Usage:
  survey.py --repo <checkout> --out <evid_root>/onboard/<project>/survey.json
            [--project NAME] [--max-area-lines 3000] [--history 300] [--started-at ISO]

Exit codes: 0 written and contract-valid · 1 contract violation (nothing written) · 2 bad usage.
Stdout: one JSON line {ok, areas, code_files, code_lines, out}.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
CONTRACT = os.path.join(ROOT, "contracts", "dev-onboard-survey.json")
EMIT = os.path.join(ROOT, "contracts", "emit.py")

LANG = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".vue": "Vue",
    ".svelte": "Svelte", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".scala": "Scala", ".sh": "Shell",
    ".sql": "SQL", ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir",
}
EXCLUDE_DIRS = {".git", "node_modules", "vendor", "dist", "build", "out", "target", ".venv",
                "venv", "__pycache__", ".next", ".nuxt", "coverage", ".tox", ".mypy_cache",
                ".pytest_cache", ".idea", ".vscode", "obj"}
GENERATED = re.compile(r"(\.min\.(js|css)$|_pb2(_grpc)?\.py$|\.pb\.go$|\.generated\.|\.g\.dart$)")
MAX_FILE_BYTES = 1_000_000
TEST_DIR = {"test", "tests", "__tests__", "spec", "specs", "e2e", "testing"}
TEST_FILE = re.compile(r"(^test_.*\.py$|_test\.(py|go)$|\.(test|spec)\.[jt]sx?$|Tests?\.(cs|java|kt)$)")
DOC_DIRS = {"docs", "doc", "documentation"}
DECISION_PARTS = {"adr", "adrs", "decisions", "rfc", "rfcs"}
WRAPPERS = {"src", "lib", "libs", "packages", "apps", "services", "internal", "pkg", "cmd",
            "modules", "crates"}
CONVENTIONAL_ENTRY = re.compile(
    r"^(main\.\w+|__main__\.py|index\.[jt]sx?|server\.\w+|app\.(py|[jt]s)|cli\.\w+|"
    r"manage\.py|wsgi\.py|asgi\.py|Program\.cs)$")
PURPOSE = [
    ("typecheck", re.compile(r"^(typecheck|type-check|types|mypy|tsc)$")),
    ("test", re.compile(r"^(test|tests|check|unit|e2e|coverage|test:.*)$")),
    ("lint", re.compile(r"^(lint|vet|lint:.*|eslint|ruff|flake8)$")),
    ("format", re.compile(r"^(fmt|format|prettier|black)$")),
    ("build", re.compile(r"^(build|compile|all|dist|bundle|build:.*)$")),
    ("run", re.compile(r"^(run|serve|start|dev|up|watch)$")),
]
MAX_COMMANDS = 40
MAX_ENTRY = 20


def purpose_of(name):
    for p, rx in PURPOSE:
        if rx.match(name):
            return p
    return "other"


def git(repo, *args):
    p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def is_git_root(repo):
    top = git(repo, "rev-parse", "--show-toplevel")
    return bool(top) and os.path.realpath(top.strip()) == os.path.realpath(repo)


def list_files(repo, use_git):
    if use_git:
        out = git(repo, "ls-files", "-z") or ""
        files = [f for f in out.split("\0") if f]
    else:
        files = []
        for d, dirs, names in os.walk(repo):
            dirs[:] = sorted(x for x in dirs if x not in EXCLUDE_DIRS and not x.startswith("."))
            for n in names:
                files.append(os.path.relpath(os.path.join(d, n), repo).replace(os.sep, "/"))
    keep = []
    for f in files:
        parts = f.split("/")
        if any(p in EXCLUDE_DIRS for p in parts[:-1]):
            continue
        if os.path.isfile(os.path.join(repo, f)) and not os.path.islink(os.path.join(repo, f)):
            keep.append(f)
    return sorted(keep)


def read_text(repo, rel):
    """Text of a file, or None if binary, generated or too large to read."""
    p = os.path.join(repo, rel)
    try:
        if os.path.getsize(p) > MAX_FILE_BYTES:
            return None
        with open(p, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="replace")


def count_lines(text):
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def is_test(rel):
    parts = rel.split("/")
    return any(p in TEST_DIR for p in parts[:-1]) or bool(TEST_FILE.search(parts[-1]))


def is_doc(rel):
    parts = rel.split("/")
    name = parts[-1].lower()
    if not (name.endswith(".md") or name.endswith(".rst") or name.endswith(".adoc")):
        return False
    return (len(parts) == 1 or parts[0].lower() in DOC_DIRS or name.startswith("readme")
            or name.startswith("architecture") or name.startswith("contributing"))


def is_decision(rel):
    parts = [p.lower() for p in rel.split("/")]
    return is_doc(rel) and any(p in DECISION_PARTS for p in parts[:-1])


def line_of(text, needle):
    for i, l in enumerate(text.splitlines(), 1):
        if needle in l:
            return i
    return None


# ---------------------------------------------------------------- commands + entry points

def from_makefile(rel, text, out):
    for i, l in enumerate(text.splitlines(), 1):
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)", l)
        if m and not l.startswith("\t"):
            name = m.group(1)
            d = os.path.dirname(rel)
            run = ("make -C %s %s" % (d, name)) if d else ("make %s" % name)
            out.append({"purpose": purpose_of(name), "name": name, "run": run, "path": rel,
                        "line": i, "source": "declared"})


def from_justfile(rel, text, out):
    for i, l in enumerate(text.splitlines(), 1):
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_-]*)(\s+[^:=]*)?:(?!=)", l)
        if m:
            name = m.group(1)
            out.append({"purpose": purpose_of(name), "name": name, "run": "just %s" % name,
                        "path": rel, "line": i, "source": "declared"})


def from_package_json(repo, rel, text, files, out, entries):
    try:
        pkg = json.loads(text)
    except ValueError:
        return
    if not isinstance(pkg, dict):
        return
    d = os.path.dirname(rel)
    siblings = set(files)
    tool = "npm"
    for lock, t in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun")):
        if os.path.join(d, lock).lstrip("/") in siblings or lock in siblings:
            tool = t
            break
    prefix = ("cd %s && " % d) if d else ""
    for name in sorted((pkg.get("scripts") or {}).keys()):
        run = "%s %s" % (tool, name) if (tool != "npm" or name in ("test", "start")) \
            else "npm run %s" % name
        out.append({"purpose": purpose_of(name), "name": name, "run": prefix + run, "path": rel,
                    "line": line_of(text, '"%s"' % name), "source": "declared"})
    targets = []
    if isinstance(pkg.get("main"), str):
        targets.append(pkg["main"])
    b = pkg.get("bin")
    if isinstance(b, str):
        targets.append(b)
    elif isinstance(b, dict):
        targets += [v for k, v in sorted(b.items()) if isinstance(v, str)]
    for t in targets:
        p = os.path.normpath(os.path.join(d, t)).replace(os.sep, "/")
        if p in siblings:
            entries.append({"path": p, "line": None, "reason": "manifest"})


def from_pyproject(rel, text, files, out, entries):
    if tomllib is None:
        return
    try:
        data = tomllib.loads(text)
    except Exception:  # noqa: BLE001  a broken manifest is a note, not a crash
        return
    tool = data.get("tool") or {}
    if "pytest" in tool:
        out.append({"purpose": "test", "name": "pytest", "run": "pytest", "path": rel,
                    "line": line_of(text, "[tool.pytest"), "source": "convention"})
    if "ruff" in tool:
        out.append({"purpose": "lint", "name": "ruff", "run": "ruff check .", "path": rel,
                    "line": line_of(text, "[tool.ruff"), "source": "convention"})
    if "mypy" in tool:
        out.append({"purpose": "typecheck", "name": "mypy", "run": "mypy .", "path": rel,
                    "line": line_of(text, "[tool.mypy"), "source": "convention"})
    scripts = dict((data.get("project") or {}).get("scripts") or {})
    scripts.update((tool.get("poetry") or {}).get("scripts") or {})
    d = os.path.dirname(rel)
    fs = set(files)
    for name in sorted(scripts):
        target = scripts[name]
        if not isinstance(target, str) or ":" not in target:
            continue
        mod = target.split(":")[0].replace(".", "/")
        for cand in (mod + ".py", mod + "/__init__.py", "src/" + mod + ".py"):
            p = os.path.normpath(os.path.join(d, cand)).replace(os.sep, "/")
            if p in fs:
                entries.append({"path": p, "line": None, "reason": "manifest"})
                break


CONVENTION = {
    "go.mod": [("build", "go build ./..."), ("test", "go test ./..."), ("lint", "go vet ./...")],
    "Cargo.toml": [("build", "cargo build"), ("test", "cargo test"), ("lint", "cargo clippy")],
}


def commands_and_entries(repo, files, texts):
    cmds, entries, manifests = [], [], []
    by_depth = sorted(files, key=lambda f: (f.count("/"), f))
    for rel in by_depth:
        if rel.count("/") > 2:
            continue
        name = rel.split("/")[-1]
        text = texts.get(rel)
        if text is None and name in ("Makefile", "GNUmakefile", "justfile", "package.json",
                                     "pyproject.toml", "go.mod", "Cargo.toml"):
            text = read_text(repo, rel)
        if name in ("Makefile", "GNUmakefile", "makefile") and text is not None:
            manifests.append(rel)
            from_makefile(rel, text, cmds)
        elif name in ("justfile", "Justfile") and text is not None:
            manifests.append(rel)
            from_justfile(rel, text, cmds)
        elif name == "package.json" and text is not None:
            manifests.append(rel)
            from_package_json(repo, rel, text, files, cmds, entries)
        elif name == "pyproject.toml" and text is not None:
            manifests.append(rel)
            from_pyproject(rel, text, files, cmds, entries)
        elif name in CONVENTION:
            manifests.append(rel)
            d = os.path.dirname(rel)
            for purpose, run in CONVENTION[name]:
                cmds.append({"purpose": purpose, "name": run.split()[1], "run":
                             (("cd %s && " % d) if d else "") + run, "path": rel, "line": None,
                             "source": "convention"})
        elif name.endswith((".sln", ".csproj")) and rel.count("/") <= 1:
            manifests.append(rel)
            for purpose, verb in (("build", "build"), ("test", "test")):
                cmds.append({"purpose": purpose, "name": verb, "run": "dotnet %s %s" % (verb, rel),
                             "path": rel, "line": None, "source": "convention"})
        elif name in ("requirements.txt", "setup.py", "setup.cfg", "Gemfile", "pom.xml",
                      "build.gradle", "build.gradle.kts", "composer.json", "mix.exs"):
            manifests.append(rel)
    # declared before convention, then by file order; one entry per (run)
    seen, uniq = set(), []
    for c in sorted(cmds, key=lambda c: (c["source"] != "declared", c["path"].count("/"),
                                          c["path"], c["line"] or 0)):
        if c["run"] in seen:
            continue
        seen.add(c["run"])
        uniq.append(c)
    return uniq[:MAX_COMMANDS], entries, sorted(set(manifests))


def code_entries(code, texts):
    out = []
    for rel in code:
        name = rel.split("/")[-1]
        text = texts.get(rel) or ""
        if rel.endswith(".py"):
            ln = None
            for i, l in enumerate(text.splitlines(), 1):
                if re.match(r"""^if\s+__name__\s*==\s*['"]__main__['"]\s*:""", l):
                    ln = i
                    break
            if ln:
                out.append({"path": rel, "line": ln, "reason": "main-guard"})
                continue
        if rel.endswith(".go") and re.search(r"^package\s+main\b", text, re.M):
            m = re.search(r"^func\s+main\s*\(", text, re.M)
            if m:
                out.append({"path": rel, "line": text[:m.start()].count("\n") + 1,
                            "reason": "main-func"})
                continue
        if CONVENTIONAL_ENTRY.match(name) and rel.count("/") <= 3:
            out.append({"path": rel, "line": None, "reason": "conventional-name"})
    return out


def merge_entries(*lists):
    rank = {"manifest": 0, "main-guard": 1, "main-func": 1, "conventional-name": 2}
    best = {}
    for lst in lists:
        for e in lst:
            cur = best.get(e["path"])
            if cur is None:
                best[e["path"]] = dict(e)
            elif rank[e["reason"]] < rank[cur["reason"]]:
                best[e["path"]] = dict(e, line=e["line"] or cur["line"])
            elif not cur["line"] and e["line"]:
                cur["line"] = e["line"]
    return sorted(best.values(), key=lambda e: (rank[e["reason"]], e["path"]))[:MAX_ENTRY]


# ---------------------------------------------------------------- areas

def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "root"


def build_areas(code_lines, max_area_lines, notes):
    """code_lines: {relpath: lines} for non-test source files. Returns [(paths, files)]."""
    total = sum(code_lines.values()) or 1

    def children(d):
        direct, subs = [], defaultdict(list)
        pre = d + "/" if d else ""
        for f in code_lines:
            if not f.startswith(pre):
                continue
            rest = f[len(pre):]
            if "/" in rest:
                subs[pre + rest.split("/")[0]].append(f)
            else:
                direct.append(f)
        return sorted(direct), dict(sorted(subs.items()))

    def lines(fs):
        return sum(code_lines[f] for f in fs)

    out = []

    def visit(d, depth):
        direct, subs = children(d)
        if direct and lines(direct) > 0:
            out.append(([d + "/"] if not subs and d else direct, direct))
        for sub, fs in subs.items():
            name = sub.split("/")[-1]
            n = lines(fs)
            _, grand = children(sub)
            dominant = len(subs) == 1 and not d and n >= 0.8 * total
            wrapper = name in WRAPPERS and len(grand) >= 1
            oversized = n > max_area_lines and len(grand) >= 1
            if depth < 6 and grand and (dominant or wrapper or oversized):
                visit(sub, depth + 1)
            else:
                if n > max_area_lines:
                    notes.append("%s has %d lines with no subdirectory to split on; the how "
                                 "stage should split it by file" % (sub, n))
                if n > 0:
                    out.append(([sub + "/"], sorted(fs)))
    visit("", 0)
    # a dir whose own loose files became an area keeps its subdirs as separate areas: loose
    # files are listed by path so the prefix does not swallow the subdirs.
    return out


def name_areas(raw):
    """Short unique ids: the last path part, widened with parents until unique."""
    def base(paths):
        p = paths[0].rstrip("/")
        if len(paths) > 1 or not paths[0].endswith("/"):
            p = os.path.dirname(paths[0]) or "root"
        return p.split("/")
    parts = [base(p) for p, _ in raw]
    width = [1] * len(raw)
    while True:
        ids = [slug("-".join(pp[-w:])) for pp, w in zip(parts, width)]
        dup = {i for i, x in enumerate(ids) if ids.count(x) > 1}
        if not dup or all(width[i] >= len(parts[i]) for i in dup):
            break
        for i in dup:
            width[i] = min(width[i] + 1, len(parts[i]))
    seen = Counter()
    final = []
    for x in ids:
        seen[x] += 1
        final.append(x if seen[x] == 1 else "%s-%d" % (x, seen[x]))
    return final


# ---------------------------------------------------------------- import edges

PY_IMPORT = re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+([\w*, ()]+)|import\s+([\w.]+))", re.M)
JS_IMPORT = re.compile(r"""(?:import\s[^'"]*?from\s*|import\s*\(\s*|require\s*\(\s*|import\s+)['"](\.{1,2}/[^'"]+)['"]""")
JS_EXT = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".vue", "/index.ts", "/index.tsx", "/index.js")


def py_targets(rel, text, fs):
    pkg = os.path.dirname(rel)
    hits = set()
    for m in PY_IMPORT.finditer(text):
        mod, names, plain = m.group(1), m.group(2), m.group(3)
        mods = []
        if plain:
            mods.append(plain)
        elif mod is not None:
            dots = len(mod) - len(mod.lstrip("."))
            base = mod.lstrip(".")
            if dots:
                anchor = pkg
                for _ in range(dots - 1):
                    anchor = os.path.dirname(anchor)
                prefix = anchor.replace("/", ".")
                full = ".".join(x for x in (prefix, base) if x)
            else:
                full = base
            mods.append(full)
            for n in re.split(r"[,\s()]+", names or ""):
                if n and n != "*":
                    mods.append(full + "." + n if full else n)
        for mname in mods:
            p = mname.replace(".", "/")
            for root in ("", "src/"):
                for cand in (root + p + ".py", root + p + "/__init__.py"):
                    if cand in fs:
                        hits.add(cand)
    return hits


def js_targets(rel, text, fs):
    d = os.path.dirname(rel)
    hits = set()
    for m in JS_IMPORT.finditer(text):
        base = os.path.normpath(os.path.join(d, m.group(1))).replace(os.sep, "/")
        for ext in JS_EXT:
            if base + ext in fs:
                hits.add(base + ext)
                break
    return hits


def imports_of(rel, text, fs):
    if rel.endswith(".py"):
        return py_targets(rel, text, fs)
    if rel.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte")):
        return js_targets(rel, text, fs)
    return set()


# ---------------------------------------------------------------- main

def survey(a):
    repo = os.path.abspath(a.repo)
    use_git = is_git_root(repo)
    files = list_files(repo, use_git)
    notes = []
    if not use_git:
        notes.append("not a git checkout: no commit, no hotspots, no history for the why stage")
    texts, skipped = {}, 0
    code_lines, test_files, lang_files, lang_lines = {}, [], Counter(), Counter()
    for rel in files:
        ext = os.path.splitext(rel)[1].lower()
        if ext not in LANG:
            continue
        if GENERATED.search(rel):
            skipped += 1
            continue
        text = read_text(repo, rel)
        if text is None:
            skipped += 1
            continue
        texts[rel] = text
        n = count_lines(text)
        lang_files[LANG[ext]] += 1
        lang_lines[LANG[ext]] += n
        if is_test(rel):
            test_files.append(rel)
        elif not rel.split("/")[0].lower() in DOC_DIRS:
            code_lines[rel] = n
    fs = set(files)

    commands, manifest_entries, manifests = commands_and_entries(repo, files, texts)
    entries = merge_entries(manifest_entries, code_entries(sorted(code_lines), texts))

    churn = Counter()
    history = {"commits_scanned": 0, "shallow": False}
    commit = None
    if use_git:
        commit = (git(repo, "rev-parse", "HEAD") or "").strip() or None
        log = git(repo, "log", "-n", str(a.history), "--name-only", "--format=tformat:@@commit")
        if log:
            history["commits_scanned"] = log.count("@@commit")
            for l in log.splitlines():
                l = l.strip()
                if l and l != "@@commit" and l in fs:
                    churn[l] += 1
        history["shallow"] = (git(repo, "rev-parse", "--is-shallow-repository") or "").strip() == "true"
        if history["shallow"]:
            notes.append("shallow clone: history is cut off, so hotspots and the why stage see less")
        if commit is None:
            notes.append("git checkout with no commits yet")

    raw = build_areas(code_lines, a.max_area_lines, notes)
    ids = name_areas(raw)
    file_area = {}
    for (paths, afiles), aid in zip(raw, ids):
        for f in afiles:
            file_area[f] = aid
    edges = defaultdict(set)
    for f, aid in file_area.items():
        for t in imports_of(f, texts[f], fs):
            other = file_area.get(t)
            if other and other != aid:
                edges[aid].add(other)
    test_hits = defaultdict(set)
    for t in test_files:
        for tgt in imports_of(t, texts[t], fs):
            if tgt in file_area:
                test_hits[file_area[tgt]].add(t)
    for t in test_files:  # a test named for an area tests it, whatever it imports
        stem = re.sub(r"(^test_|_test$|\.(test|spec)$|Tests?$)", "",
                      os.path.splitext(t.split("/")[-1])[0])
        for aid in ids:
            if len(aid) >= 3 and slug(stem) in (aid, aid.rstrip("s"), aid + "s"):
                test_hits[aid].add(t)
    areas = []
    for (paths, afiles), aid in zip(raw, ids):
        langs = sorted({LANG[os.path.splitext(f)[1].lower()] for f in afiles})
        area = {"id": aid, "paths": paths, "files": len(afiles),
                "lines": sum(code_lines[f] for f in afiles), "languages": langs,
                "depends_on": sorted(edges.get(aid, ())), "tests": sorted(test_hits.get(aid, ()))[:10]}
        if use_git:
            area["hotspot_commits"] = sum(churn[f] for f in afiles)
        areas.append(area)
    if len(areas) > 24:
        notes.append("%d areas is a lot of fan-out; dev-onboard-map should merge the smallest"
                     % len(areas))
    if not areas:
        notes.append("no source files found in a language the survey counts")

    test_dirs = sorted({"/".join(t.split("/")[:next((i for i, p in enumerate(t.split("/"))
                                                     if p in TEST_DIR), 0) + 1])
                        for t in test_files if any(p in TEST_DIR for p in t.split("/")[:-1])})
    return {
        "project": a.project or os.path.basename(repo.rstrip("/")),
        "commit": commit,
        "source": "git" if use_git else "tree",
        "totals": {"files": len(files), "code_files": len(code_lines),
                   "code_lines": sum(code_lines.values()), "skipped": skipped},
        "languages": [{"name": n, "files": lang_files[n], "lines": lang_lines[n]}
                      for n in sorted(lang_lines, key=lambda n: (-lang_lines[n], n))],
        "manifests": manifests,
        "commands": commands,
        "entry_points": entries,
        "tests": {"files": len(test_files), "dirs": test_dirs},
        "docs": [f for f in files if is_doc(f)][:60],
        "decisions": [f for f in files if is_decision(f)][:60],
        "hotspots": [{"path": p, "commits": c}
                     for p, c in sorted(churn.items(), key=lambda kv: (-kv[1], kv[0]))[:15]],
        "history": history,
        "areas": areas,
        "notes": notes,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--project")
    ap.add_argument("--max-area-lines", type=int, default=3000)
    ap.add_argument("--history", type=int, default=300)
    ap.add_argument("--started-at")
    ap.add_argument("--dry-run", action="store_true", help="print the survey, write nothing")
    a = ap.parse_args()
    if not os.path.isdir(a.repo):
        print(json.dumps({"ok": False, "error": "repo not found"}))
        return 2
    result = survey(a)
    if a.dry_run:
        print(json.dumps(result, indent=2))
        return 0
    cmd = [sys.executable, EMIT, "--contract", CONTRACT, "--out", a.out, "--from-json", "-"]
    if a.started_at:
        cmd += ["--started-at", a.started_at]
    p = subprocess.run(cmd, input=json.dumps(result), text=True, capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr or p.stdout)
        return 1
    print(json.dumps({"ok": True, "areas": len(result["areas"]),
                      "code_files": result["totals"]["code_files"],
                      "code_lines": result["totals"]["code_lines"], "out": a.out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
