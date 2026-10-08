#!/usr/bin/env python3
"""
sweep.py — the deterministic half of qa-ui-parity-sweep.

The model drives the live browser (navigate, inject probe.js, read the console). Everything that
can be decided without a browser lives here, so it is the same on every run and testable offline:
planning + the hard budget, a local collector for probe dumps, coverage accounting that refuses
silent skips, matching by role + text, High/Medium/Low grading, likely-source location, the
report, and brain write-back.

Two environments take part, named in `.claude/loops/stack.config.json`:
  baseline   integrations.ui_parity.baseline_env  — the design reference (the source of truth)
  candidate  integrations.ui_parity.candidate_env — the deployment being measured against it
Both names must exist under app.environments with a url; `init` can override either by flag.
Internally the two sides are always "baseline" and "candidate" (dump paths, --env values, finding
keys); the configured environment NAMES are the labels the report prints.

Verbs (every verb prints one JSON object; exit 0 ok · 1 usage · 2 failure · 3 budget phase
closed / cap reached · 4 coverage gap / silent skip · 5 preflight: stop the run):
  init       [--baseline-env N] [--candidate-env N] [--baseline-url U] [--candidate-url U]
             [--routes F] [--only id,id] [--run DIR] [--budget-min M] [--port P] [--rehearsal]
  preflight  --run DIR                                    screen unlocked? collector port free? (exit 5 = stop)
  serve      --run DIR [--port P] [--background]          collector for probe.js dumps (+ /config.json)
  stop       --run DIR                                    stop a --background collector
  budget     --run DIR --phase capture|fixes|report       exit 3 once the phase is closed
  console    --run DIR --route R --env baseline|candidate [--file F] [--since-ms MS]   console text (stdin ok)
  mark       --run DIR --route R [--env E] --status covered|partial|failed|skipped --reason TEXT [--what W]
  diff       --run DIR                                    → findings.json
  locate     --run DIR [--candidate-repo DIR] [--baseline-repo DIR] [--remote NAME] [--no-brain]
  candidates --run DIR                                    Medium findings with an obvious one-file fix
  fixed      --run DIR [--finding ID] (--pr N --url U --branch B [--draft] | --skipped REASON)
  report     --run DIR [--regrade]                        → parity-report.md (exit 4 on a silent skip)
  writeback  --run DIR [--dry-run]                        metric + source-map rows via qa-kb
"""

import argparse
import datetime
import difflib
import http.server
import json
import os
import re
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
REPO = os.path.abspath(os.path.join(SKILL, "..", "..", ".."))
ASSETS = os.path.join(SKILL, "assets")
KB = os.environ.get("KB_BIN", os.path.join(REPO, ".claude", "skills", "qa-kb", "scripts", "kb.py"))
R6_SCAN = os.path.join(REPO, ".claude", "skills", "dev-fix", "scripts", "r6_denylist_scan.sh")
PORT = 5098                    # the collector's default port; `init --port` / `serve --port` override it
ENVS = ("baseline", "candidate")
# Fallbacks only: the decided numbers live in loop-limits.json → ui_parity_sweep (one copy).
LIMIT_FALLBACK = {"budget_min": 45, "max_fix_prs": 2, "max_dialogs_per_route": 3}

# The probe's per-stack knobs and the source-location knobs, with their defaults. A routes file
# may override any of them under `probe` / `source` (see assets/routes.default.json). None of
# these carries a tenant value; they describe the UI toolkit, not the app.
PROBE_DEFAULTS = {
    "guard_allow_hosts": [],          # regexes of hosts whose non-GET traffic the read-only guard lets through (an identity provider)
    "settle_selectors": ['[class*="-enter-active"]', '[class*="-leave-active"]', '[class*="-enter-from"]'],
    "buttons_extra": [],              # selectors the toolkit uses for button-like controls beyond native + ARIA
    "dialogs_extra": [],              # selectors the toolkit uses for open dialogs beyond [role=dialog] / dialog[open]
    "dialog_close_extra": [],         # selectors of a dialog's close control beyond [aria-label=Close]
    "utility_class_prefixes": [],     # regexes of utility/state class prefixes to drop from the class chain
    "icon_class_patterns": [],        # regexes that name an icon from an <svg> class (e.g. an icon library's prefix)
}
SOURCE_DEFAULTS = {
    "candidate_paths": [],            # git pathspecs to grep in the candidate repo ([] = the whole tree)
    "baseline_repo_dir": None,        # the design-reference repo, when it is one (else only the brain's map is used)
    "baseline_paths": [],
    "extensions": [],                 # file extensions that can render an element ([] = any non-test file)
    "component_extensions": [],       # markup components: +1 when ranking (a control's markup lives there)
    "style_extensions": [".scss", ".css", ".less", ".sass"],   # files a one-file style fix may touch
    "logic_dirs": ["services", "stores", "hooks", "utils"],   # -1 when ranking: logic, rarely markup or style
}


def stack():
    """The framework's stack.config (lib/adapters/config.py). Missing/invalid → a usage failure."""
    try:
        sys.path.insert(0, REPO)
        from lib.adapters.config import load_config
        return load_config()
    except Exception as e:  # FileNotFoundError, ValueError, ImportError
        fail("usage", f"cannot load stack.config.json: {e}", code=1)


def limits():
    try:
        c = stack()
        path = str(c.resolve(c.get("limits_file"), ".claude/loops/loop-limits.json"))
        o = json.load(open(path))["ui_parity_sweep"]
        return {k: (o[k] if isinstance(o.get(k), (int, float)) and o[k] > 0 else v) for k, v in LIMIT_FALLBACK.items()}
    except Exception:
        return dict(LIMIT_FALLBACK, _fallback=True)

# Phase deadlines as fractions of the budget: capture closes with room left for the fix phase,
# fixes close with room left to locate, report and write back. 45 min → 33 / 41 / 45.
PHASES = {"capture": 0.733, "fixes": 0.911, "report": 1.0}


# --- plumbing ------------------------------------------------------------------------------

def out(payload, code=0):
    json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    sys.exit(code)


def fail(cls, msg, code=2, **extra):
    out({"status": "failed", "failure_class": cls, "message": msg, **extra}, code)


def now():
    return time.time()


def iso(t=None):
    return datetime.datetime.fromtimestamp(t or now(), datetime.timezone.utc).isoformat(timespec="seconds")


def rjson(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def wjson(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True, default=str)
    os.replace(tmp, path)


def jl_read(path):
    rows = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return rows


def jl_append(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def plan_of(run):
    p = rjson(os.path.join(run, "plan.json"))
    if not p:
        fail("usage", f"no plan.json in {run} — run `sweep.py init` first", code=1)
    set_labels(p)
    return p


def slug(s, n=48):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:n] or "x"


# The two sides' display labels (the configured environment names). Set from the plan.
LABELS = {"baseline": "baseline", "candidate": "candidate"}


def set_labels(plan):
    for env in ENVS:
        LABELS[env] = ((plan.get("envs") or {}).get(env) or {}).get("name") or env


def B():
    return LABELS["baseline"]


def C():
    return LABELS["candidate"]


# --- init / budget -------------------------------------------------------------------------

def resolve_envs(a, c):
    """Which two environments take part, and their URLs. Flags beat stack.config; a URL flag
    without a name flag keeps the configured name (or the side's own name when none is set)."""
    envs = {}
    for side in ENVS:
        name = getattr(a, f"{side}_env") or c.get(f"integrations.ui_parity.{side}_env") or side
        url = getattr(a, f"{side}_url")
        if not url:
            url = c.get(f"app.environments.{name}.url")
            if not url:
                fail("usage", f"the {side} environment '{name}' has no url under app.environments in stack.config.json "
                              f"(set integrations.ui_parity.{side}_env to an environment that has one, or pass --{side}-url)", code=1)
        envs[side] = {"name": name, "url": str(url).rstrip("/")}
    if envs["baseline"]["url"] == envs["candidate"]["url"]:
        fail("usage", "baseline and candidate resolve to the same url — nothing to compare", code=1)
    return envs


def load_routes(a, c):
    """`--routes F` beats integrations.ui_parity.routes; the shipped assets/routes.default.json is a
    documented template, never a silent default against a real app."""
    if a.routes:
        cfg = rjson(a.routes)
        if not cfg or not isinstance(cfg.get("routes"), list) or not cfg["routes"]:
            fail("usage", f"routes file {a.routes} has no routes", code=1)
        return cfg, a.routes
    inline = c.get("integrations.ui_parity.routes") or []
    if isinstance(inline, list) and inline:
        return {"routes": inline, "roots": c.get("integrations.ui_parity.roots") or {}}, "stack.config.json#integrations.ui_parity.routes"
    fail("usage", "no routes: pass --routes FILE (shape: assets/routes.default.json) or fill "
                  "integrations.ui_parity.routes in stack.config.json", code=1)


def cmd_init(a):
    c = stack()
    cfg, routes_file = load_routes(a, c)
    routes = cfg["routes"]
    if a.only:
        want = [x.strip() for x in a.only.split(",") if x.strip()]
        unknown = [w for w in want if w not in {r.get("id") for r in routes}]
        if unknown:
            fail("usage", f"--only names unknown route ids: {unknown}", code=1)
        routes = [r for r in routes if r["id"] in want]
    ids = [r.get("id") for r in routes]
    if len(set(ids)) != len(ids):
        fail("usage", "route ids must be unique", code=1)
    for r in routes:
        if not r.get("id"):
            fail("usage", f"route {r} lacks 'id'", code=1)
        for side in ENVS:
            if not (r.get(side) or r.get("path")):
                fail("usage", f"route {r['id']} lacks '{side}' (or a shared 'path')", code=1)
    lim = limits()
    if a.budget_min is None:
        a.budget_min = lim["budget_min"]
    if a.budget_min <= 0:
        fail("usage", "--budget-min must be positive", code=1)
    envs = resolve_envs(a, c)
    t0 = now()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = os.path.abspath(a.run or os.path.join(str(c.evidence_root()), "ui-parity-sweep", stamp))
    if os.path.exists(os.path.join(run, "plan.json")):
        fail("usage", f"{run} already holds a run — pick a new --run", code=1)
    join = lambda base, path: base.rstrip("/") + "/" + path.lstrip("/")
    # `main` is the HTML landmark for a page's primary content — the neutral default when the
    # routes file does not name the content column on each side.
    roots = {**{"baseline": "main", "candidate": "main"}, **(cfg.get("roots") or {})}
    probe = {**PROBE_DEFAULTS, **(cfg.get("probe") or {})}
    source = {**SOURCE_DEFAULTS, **(cfg.get("source") or {})}
    plan = {
        "run_id": f"ui-parity-{stamp}", "run": run, "started_at": t0, "started_iso": iso(t0),
        "budget_min": a.budget_min,
        "deadlines": {ph: t0 + frac * a.budget_min * 60 for ph, frac in PHASES.items()},
        "envs": envs, "roots": roots, "routes_file": routes_file,
        "routes": [{**r, "baseline": r.get("baseline") or r.get("path"), "candidate": r.get("candidate") or r.get("path"),
                    "baseline_url": join(envs["baseline"]["url"], r.get("baseline") or r.get("path")),
                    "candidate_url": join(envs["candidate"]["url"], r.get("candidate") or r.get("path")),
                    "roots": {**roots, **(r.get("roots") or {})}} for r in routes],
        "collector": f"http://127.0.0.1:{a.port}",
        "limits": lim,
        "probe": probe,
        "source": source,
        "console_noise": cfg.get("console_noise") or [],
        "rehearsal": bool(a.rehearsal),
    }
    wjson(os.path.join(run, "plan.json"), plan)
    set_labels(plan)
    out({"status": "ok", "run": run, "run_id": plan["run_id"], "envs": envs, "routes": [
        {"id": r["id"], "baseline_url": r["baseline_url"], "candidate_url": r["candidate_url"], "roots": r["roots"],
         "modals": r.get("modals") or []} for r in plan["routes"]],
        "deadlines": {k: iso(v) for k, v in plan["deadlines"].items()}, "collector": plan["collector"],
        "budget_min": plan["budget_min"], "max_fix_prs": lim["max_fix_prs"], "max_dialogs_per_route": lim["max_dialogs_per_route"],
        "rehearsal": plan["rehearsal"]})


def cmd_budget(a):
    plan = plan_of(a.run)
    t = now()
    dl = plan["deadlines"]
    res = {"phase": a.phase, "elapsed_s": round(t - plan["started_at"]),
           "phase_remaining_s": round(dl[a.phase] - t), "hard_remaining_s": round(dl["report"] - t)}
    if t >= dl[a.phase]:
        out({"status": "closed", **res, "message": f"the {a.phase} phase is closed — move to the next phase"
             if a.phase != "report" else "the hard budget is spent — write the report NOW with what you have"}, 3)
    out({"status": "open", **res})


# --- preflight -----------------------------------------------------------------------------

def screen_locked():
    """True / False on macOS, None when it cannot be told (any other platform, or the query fails).
    A locked screen hides every tab, and a hidden tab freezes the app's transitions and clamps
    timers: nothing measured then is evidence."""
    forced = os.environ.get("LOOP_PARITY_SCREEN_LOCKED")   # tests only: 1 / 0
    if forced in ("0", "1"):
        return forced == "1"
    if sys.platform != "darwin":
        return None
    try:
        raw = subprocess.run(["ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, timeout=10).stdout
        import plistlib
        users = plistlib.loads(raw).get("IOConsoleUsers") or []
        return any(u.get("CGSSessionScreenIsLocked") for u in users)
    except Exception:
        return None


def cmd_preflight(a):
    plan = plan_of(a.run)
    lock = screen_locked()
    port = int(plan["collector"].rsplit(":", 1)[1])
    import socket
    sk = socket.socket()
    try:
        sk.settimeout(1)
        in_use = sk.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sk.close()
    ours = bool(rjson(os.path.join(a.run, "collector.pid")))
    res = {"screen_locked": lock, "collector_port": port, "port_in_use": in_use, "collector_is_ours": ours}
    if lock:
        out({"status": "stop", "failure_class": "screen-locked", **res,
             "message": "the screen is locked: every tab reads hidden, so nothing can be measured. Mark every route "
                        "`failed --reason 'screen locked'`, write the report, and tell the human to unlock and re-run."}, 5)
    if in_use and not ours:
        out({"status": "stop", "failure_class": "collector-blocked", **res,
             "message": f"port {port} is taken by something else (another sweep?) — stop it or init with --port"}, 5)
    out({"status": "ok", **res})


# --- collector -----------------------------------------------------------------------------

def make_handler(run):
    dumps = os.path.join(run, "dumps")

    class H(http.server.BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Allow-Local-Network", "true")

        def do_OPTIONS(self):
            self.send_response(204); self._cors(); self.end_headers()

        def do_GET(self):
            p = self.path.split("?")[0].lstrip("/")
            if p == "health":
                body = json.dumps({"ok": True, "run": run}).encode()
                ctype = "application/json"
            elif p == "probe.js":
                body = open(os.path.join(HERE, "probe.js"), "rb").read()
                ctype = "text/javascript"
            elif p == "config.json":
                # The probe's per-stack knobs, from the plan — so probe.js carries no toolkit names.
                plan = rjson(os.path.join(run, "plan.json"), {}) or {}
                body = json.dumps({**PROBE_DEFAULTS, **(plan.get("probe") or {})}).encode()
                ctype = "application/json"
            else:
                self.send_response(404); self._cors(); self.end_headers(); return
            self.send_response(200); self._cors()
            self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_POST(self):
            m = re.search(r"name=([\w./\-]+)", self.path)
            rel = m.group(1) if m else ""
            if not rel or ".." in rel or rel.startswith("/") or not rel.endswith(".json"):
                self.send_response(400); self._cors(); self.end_headers(); return
            n = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(n)
            try:
                json.loads(data)
            except ValueError:
                self.send_response(422); self._cors(); self.end_headers(); return
            dest = os.path.join(dumps, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".tmp", "wb") as fh:
                fh.write(data)
            os.replace(dest + ".tmp", dest)
            self.send_response(200); self._cors(); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"saved": rel, "bytes": n}).encode())

        def log_message(self, *_):
            pass
    return H


def cmd_serve(a):
    plan_of(a.run)
    pidf = os.path.join(a.run, "collector.pid")
    if a.background:
        old = rjson(pidf)
        if old and _alive(old.get("pid")):
            out({"status": "ok", "already_running": True, **old})
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve", "--run", a.run, "--port", str(a.port)],
                             stdout=subprocess.DEVNULL, stderr=open(os.path.join(a.run, "collector.log"), "a"),
                             start_new_session=True)
        for _ in range(40):
            time.sleep(0.1)
            try:
                import urllib.request
                urllib.request.urlopen(f"http://127.0.0.1:{a.port}/health", timeout=1).read()
                wjson(pidf, {"pid": p.pid, "port": a.port})
                out({"status": "ok", "pid": p.pid, "port": a.port, "probe": f"http://127.0.0.1:{a.port}/probe.js"})
            except Exception:
                if p.poll() is not None:
                    break
        fail("collector-down", f"the collector did not come up on port {a.port} (see {a.run}/collector.log) — "
             "is another sweep's collector already bound to it?")
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.run))
    except OSError as e:
        fail("collector-down", f"cannot bind 127.0.0.1:{a.port}: {e}")
    srv.serve_forever()


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def cmd_stop(a):
    pidf = os.path.join(a.run, "collector.pid")
    rec = rjson(pidf)
    if not rec:
        out({"status": "ok", "stopped": False, "message": "no collector pid recorded"})
    try:
        os.kill(int(rec["pid"]), signal.SIGTERM)
    except (OSError, ValueError, KeyError):
        pass
    try:
        os.remove(pidf)
    except OSError:
        pass
    out({"status": "ok", "stopped": True, "pid": rec.get("pid")})


# --- console + coverage marks ---------------------------------------------------------------

LEVEL_RE = re.compile(r"\b(error|exception|uncaught|warn(?:ing)?)\b", re.I)


def parse_console(raw):
    """Accept a JSON array of {level,text} OR the text the browser tool's console reader prints."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("messages") or []
        if isinstance(data, list):
            rows = []
            for m in data:
                if isinstance(m, dict):
                    lvl = str(m.get("level") or m.get("type") or "").lower()
                    rows.append({"level": "error" if lvl.startswith(("err", "exc")) else "warn" if lvl.startswith("warn") else lvl or "log",
                                 "text": str(m.get("text") or m.get("message") or "")[:600]})
                elif isinstance(m, str):
                    rows.append(_line_row(m))
            return [r for r in rows if r["text"]]
    except ValueError:
        pass
    # The console reader's own shape: "[n] [time] [LEVEL] (source)" then the message on the
    # following line(s), until the next "[n]". One entry = one message, never two half-rows.
    entry = re.compile(r"^\[(\d+)\]\s*\[([^\]]*)\]\s*\[(\w+)\]\s*(?:\((.*)\))?\s*$")
    lines = raw.splitlines()
    if any(entry.match(l.strip()) for l in lines):
        rows, cur = [], None
        for l in lines:
            m = entry.match(l.strip())
            if m:
                if cur:
                    rows.append(cur)
                lvl = m.group(3).lower()
                cur = {"level": "error" if lvl.startswith(("err", "exc")) else "warn" if lvl.startswith("warn") else lvl,
                       "text": "", "source": (m.group(4) or "")[:200], "clock": m.group(2)}
            elif cur is not None and l.strip() and not l.startswith("Tab Context") and not l.strip().startswith(("•", "- Executed", "- Available")):
                cur["text"] = (cur["text"] + " " + l.strip()).strip()[:600]
        if cur:
            rows.append(cur)
        for r in rows:
            if not r["text"]:
                r["text"] = r["source"]
        return [r for r in rows if r["text"]]
    if re.match(r"^\s*no console messages found", raw, re.I):
        return []
    return [r for r in (_line_row(line) for line in lines) if r["text"]]


def _line_row(line):
    line = line.strip()
    m = re.match(r"^\[?(error|warning|warn|log|info|debug|exception)\]?[:\s]+(.*)$", line, re.I)
    if m:
        lvl = m.group(1).lower()
        return {"level": "error" if lvl in ("error", "exception") else "warn" if lvl.startswith("warn") else lvl, "text": m.group(2)[:600]}
    lvl = LEVEL_RE.search(line)
    level = "log"
    if lvl:
        w = lvl.group(1).lower()
        level = "warn" if w.startswith("warn") else "error"
    return {"level": level, "text": line[:600]}


def entry_epoch_ms(clock, ref_ms):
    """The console reader stamps entries with a local wall-clock time ("5:11:55 PM", no date).
    Place it on the same local day as ref_ms (the page's navigation start); None if unparseable."""
    try:
        t = datetime.datetime.strptime(clock.strip().upper(), "%I:%M:%S %p").time()
    except ValueError:
        try:
            t = datetime.datetime.strptime(clock.strip(), "%H:%M:%S").time()
        except ValueError:
            return None
    ref = datetime.datetime.fromtimestamp(ref_ms / 1000)
    return int(datetime.datetime.combine(ref.date(), t).timestamp() * 1000)


def cmd_console(a):
    plan = plan_of(a.run)
    if a.route not in {r["id"] for r in plan["routes"]}:
        fail("usage", f"unknown route {a.route}", code=1)
    raw = open(a.file).read() if a.file else sys.stdin.read()
    rows = parse_console(raw)
    dropped = 0
    if a.since_ms:
        # The console buffer is per tab and `clear` has proven unreliable: an entry logged before
        # THIS page's navigation started belongs to an earlier page and must not be graded here.
        kept = []
        for r in rows:
            at = entry_epoch_ms(r.get("clock") or "", a.since_ms)
            if at is not None and at < a.since_ms - 1000:
                dropped += 1
                continue
            kept.append(r)
        rows = kept
    wjson(os.path.join(a.run, "console", f"{a.route}.{a.env}.json"), {"route": a.route, "env": a.env, "at": iso(), "messages": rows})
    out({"status": "ok", "saved": len(rows), "errors": sum(r["level"] == "error" for r in rows),
         "warnings": sum(r["level"] == "warn" for r in rows), "dropped_from_earlier_pages": dropped})


def cmd_mark(a):
    plan = plan_of(a.run)
    if a.route not in {r["id"] for r in plan["routes"]}:
        fail("usage", f"unknown route {a.route}", code=1)
    if a.status != "covered" and not (a.reason or "").strip():
        fail("usage", "a non-covered mark needs --reason: a skip without a reason is the silent skip this skill exists to prevent", code=1)
    row = {"route": a.route, "env": a.env, "what": a.what, "status": a.status, "reason": a.reason, "at": iso()}
    jl_append(os.path.join(a.run, "coverage.jsonl"), row)
    out({"status": "ok", **row})


# --- diff: matching + grading ----------------------------------------------------------------

PX_PROPS = {"font-size", "line-height", "letter-spacing", "padding-top", "padding-right", "padding-bottom", "padding-left",
            "margin-top", "margin-right", "margin-bottom", "margin-left", "column-gap", "row-gap",
            "border-top-width", "border-right-width", "border-bottom-width", "border-left-width", "outline-width",
            "border-top-left-radius", "border-top-right-radius", "border-bottom-right-radius", "border-bottom-left-radius"}
COLOR_PROPS = {"color", "background-color", "border-top-color", "border-right-color", "border-bottom-color",
               "border-left-color", "outline-color"}
EXACT_PROPS = {"font-family", "font-weight", "font-style", "text-transform", "text-decoration-line", "text-align",
               "white-space", "border-top-style", "outline-style", "cursor", "box-shadow", "background-image",
               "display", "justify-content", "align-items"}
NOISE_FLOOR = 0.5   # sub-pixel rounding between two browsers' layouts is not drift
LOW_MAX = 2.0       # the line: cosmetic drift UNDER 2px is Low, 2px and up is Medium
SEV_RANK = {"high": 0, "medium": 1, "low": 2}
CONTROL_ROLES = {"button", "menuitem", "tab", "switch", "link", "menuitemradio", "menuitemcheckbox"}


def group(role):
    return "heading" if role == "heading" else "dialog" if role in ("dialog", "alertdialog") else "control"


def loose(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def label_of(it):
    return it.get("name") or (f"[icon:{it['icon']}]" if it.get("icon") else f"[{it.get('tag', '?')}]")


def nameable(it):
    """A label that identifies the element. An unnamed control with no icon identity ("[button]",
    "[icon:svg]") says nothing about WHICH control it is, so it may only pair structurally."""
    lab = label_of(it)
    return not (lab.startswith("[") and (lab.startswith("[icon:svg") or not lab.startswith("[icon:")))


def label_differs(l, u):
    """Text differs as a reader sees it. The browser's innerText applies text-transform, so a
    case-only difference where either side transforms case is a style difference (reported as
    text-transform), not label drift."""
    a, b = label_of(l), label_of(u)
    if a == b:
        return False
    if a.lower() == b.lower() and any(((x.get("s") or {}).get("text-transform") or "none") != "none" for x in (l, u)):
        return False
    return True


def sig(it):
    cls = it.get("cls") or []
    own = ".".join(sorted(cls[0])) if cls else ""
    parent = ".".join(sorted(cls[1][:2])) if len(cls) > 1 else ""
    return f"{group(it.get('role'))}|{it.get('tag')}|{own}|{parent}"


def match(base, cand):
    """Pair baseline items with candidate items. Returns (pairs[(b,c,how)], baseline_only, candidate_only, data_pairs)."""
    lu, uu = list(base), list(cand)
    pairs = []

    def take(pred, how):
        nonlocal lu, uu
        used_u = set()
        keep_l = []
        for l in lu:
            hit = next((u for u in uu if id(u) not in used_u and pred(l, u)), None)
            if hit is None:
                keep_l.append(l)
            else:
                used_u.add(id(hit)); pairs.append((l, hit, how))
        lu = keep_l
        uu = [u for u in uu if id(u) not in used_u]

    both = lambda l, u: group(l["role"]) == group(u["role"]) and nameable(l) and nameable(u)
    take(lambda l, u: both(l, u) and label_of(l) == label_of(u), "exact")
    take(lambda l, u: both(l, u) and loose(label_of(l)) and loose(label_of(l)) == loose(label_of(u)), "loose")
    take(lambda l, u: (both(l, u)
                       and difflib.SequenceMatcher(None, loose(label_of(l)), loose(label_of(u))).ratio() >= 0.75
                       and abs(l["box"]["y"] - u["box"]["y"]) <= 48), "near")
    # Same component, different DATA (a record's title, a person's name): pair by structural
    # signature in order. These are compared for style, never reported as missing/extra.
    data_pairs = []
    by_sig_u = {}
    for u in uu:
        by_sig_u.setdefault(sig(u), []).append(u)
    keep_l = []
    for l in lu:
        pool = by_sig_u.get(sig(l)) if (l.get("cls") or [[]])[0] else None
        if pool:
            data_pairs.append((l, pool.pop(0), "data"))
        else:
            keep_l.append(l)
    used = {id(u) for _, u, _ in data_pairs}
    uu = [u for u in uu if id(u) not in used]
    return pairs + data_pairs, keep_l, uu, len(data_pairs)


def px(v):
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)px\s*", str(v or ""))
    return float(m.group(1)) if m else None


def rgb(v):
    m = re.fullmatch(r"rgba?\((\d+),(\d+),(\d+)(?:,([\d.]+))?\)", str(v or "").replace(" ", ""))
    return tuple(float(x) for x in m.groups(default="1")) if m else None


def family(v):
    return (str(v or "").split(",")[0].strip().strip("'\"").lower())


def compare_styles(l, u, compare_size=True):
    """Return [(prop, baseline, candidate, severity, delta)] for one matched pair."""
    deltas = []
    ls, us = l.get("s") or {}, u.get("s") or {}
    for p in sorted(set(ls) | set(us)):
        a, b = ls.get(p), us.get(p)
        if a == b:
            continue
        if p in PX_PROPS:
            x, y = px(a), px(b)
            if x is None or y is None:
                deltas.append((p, a, b, "medium", None))
                continue
            d = abs(x - y)
            if d < NOISE_FLOOR:
                continue
            deltas.append((p, a, b, "low" if d < LOW_MAX else "medium", round(d, 2)))
        elif p in COLOR_PROPS:
            x, y = rgb(a), rgb(b)
            if x and y and max(abs(i - j) for i, j in zip(x[:3], y[:3])) <= 3 and abs(x[3] - y[3]) < 0.05:
                deltas.append((p, a, b, "low", 0))
            else:
                deltas.append((p, a, b, "medium", None))
        elif p == "font-family":
            if family(a) != family(b):
                deltas.append((p, a, b, "medium", None))
        elif p == "opacity":
            try:
                if abs(float(a) - float(b)) >= 0.05:
                    deltas.append((p, a, b, "medium", None))
            except (TypeError, ValueError):
                deltas.append((p, a, b, "medium", None))
        elif p in EXACT_PROPS:
            deltas.append((p, a, b, "medium", None))
        # visibility / margins of layout wrappers etc. are covered by the visible flag and PX_PROPS
    if compare_size and l.get("box") and u.get("box"):
        dims = ("h",) if l.get("role") == "heading" else ("w", "h")
        for dim in dims:
            d = abs(float(l["box"][dim]) - float(u["box"][dim]))
            if d >= NOISE_FLOOR:
                deltas.append((f"box.{dim}", f"{l['box'][dim]}px", f"{u['box'][dim]}px", "low" if d < LOW_MAX else "medium", round(d, 2)))
    return deltas


def load_noise(plan=None):
    """The framework's neutral noise list (assets/console-noise.json) plus the routes file's own
    `console_noise` entries (third-party widgets, an identity provider's dev banner, the toolkit's
    dev-mode notices — whatever this stack emits that is not the app)."""
    cfg = rjson(os.path.join(ASSETS, "console-noise.json"), {}) or {}
    pats = []
    for n in (cfg.get("noise") or []) + list((plan or {}).get("console_noise") or []):
        try:
            pats.append((re.compile(n["pattern"], re.I), n.get("why", "")))
        except (re.error, KeyError, TypeError):
            pass
    return pats


def norm_msg(t):
    t = re.sub(r"https?://\S+", "<url>", t or "")
    t = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<id>", t, flags=re.I)
    t = re.sub(r"\d+", "<n>", t)
    return re.sub(r"\s+", " ", t).strip()[:240]


def console_rows(run, route, env, base):
    rows = []
    c = rjson(os.path.join(run, "console", f"{route}.{env}.json"))
    if c:
        rows += [{**m, "src": "console-reader"} for m in c.get("messages") or []]  # keeps m["source"]
    for d in [base] + [rjson(os.path.join(run, "dumps", route, env, f)) for f in _modal_files(run, route, env)]:
        for m in ((d or {}).get("meta") or {}).get("console") or []:
            rows.append({"level": m.get("level"), "text": m.get("text"), "src": "in-page"})
    return rows, c is not None


def _modal_files(run, route, env):
    d = os.path.join(run, "dumps", route, env)
    try:
        return sorted(f for f in os.listdir(d) if f.startswith("modal-") and f.endswith(".json"))
    except OSError:
        return []


def describe(it, deltas=None, side="candidate"):
    kind = "heading" if it.get("role") == "heading" else "dialog" if group(it.get("role")) == "dialog" else it.get("role", "control")
    head = f"{kind} “{label_of(it)}”"
    if not deltas:
        return head
    vals = ", ".join(f"{p} {(a if side == 'baseline' else b)}" for p, a, b, _, _ in deltas[:6])
    return f"{head}: {vals}" + (" …" if len(deltas) > 6 else "")


class Findings:
    def __init__(self):
        self.rows = []

    def add(self, sev, route, category, title, before, after, **ev):
        self.rows.append({"severity": sev, "route": route, "category": category, "title": title,
                          "before": before, "after": after, **ev})


WHOLESALE_MATCH = 0.4    # under 40% of the baseline's visible elements matched → a different design, not drift
WHOLESALE_MIN = 8       # ...and only when the baseline shows enough elements for the ratio to mean something


def diff_items(F, route, where, base_items, cand_items, base_hidden=False):
    pairs, base_only, cand_only, data_n = match(base_items, cand_items)
    base_vis = [x for x in base_items if x.get("visible")]
    matched_vis = sum(1 for l, _, _ in pairs if l.get("visible"))
    base_only_vis = [x for x in base_only if x.get("visible")]
    cand_only_vis = [x for x in cand_only if x.get("visible")]
    wholesale = len(base_vis) >= WHOLESALE_MIN and matched_vis / max(len(base_vis), 1) < WHOLESALE_MATCH
    cand_vis = [x for x in cand_items if x.get("visible")]
    # The reverse case: the BASELINE page is a placeholder (a title and an empty body) while the
    # candidate has the real page. There is no design to compare against, so the candidate's
    # elements are not "extra": say so once, as a coverage note, and grade nothing else.
    base_placeholder = (len(base_vis) < WHOLESALE_MIN and len(cand_vis) >= 15
                        and len(cand_vis) >= 3 * max(len(base_vis), 1))
    if wholesale:
        # One finding, not thirty: when most of the baseline's page has no counterpart, the
        # candidate is showing a different design (typically the previous page, with the redesign
        # behind a feature flag). Per-element "missing"/"extra" rows would bury that fact under noise.
        names = lambda xs: ", ".join(dict.fromkeys(f"“{label_of(x)}”" for x in xs if label_of(x) and not label_of(x).startswith("[")))
        F.add("high", route, "different-design",
              f"{C()} does not implement {B()}'s design for {where if where != 'page' else 'this page'} "
              f"({matched_vis} of {len(base_vis)} {B()} elements have a counterpart)",
              f"{C()} shows a different layout: {len(cand_only_vis)} controls/headings with no {B()} counterpart — {names(cand_only_vis)[:400]}",
              f"{B()}'s design: {len(base_only_vis)} controls/headings missing from {C()} — {names(base_only_vis)[:400]}. "
              "Check first whether a feature flag gates the redesign in this environment.",
              element={"role": "page", "name": next((label_of(x) for x in base_only_vis if nameable(x)), where), "where": where,
                       "candidate_name": next((label_of(x) for x in cand_only_vis if nameable(x)), None),
                       "baseline_cls": next((x.get("cls") for x in base_only_vis if nameable(x)), None),
                       "candidate_cls": next((x.get("cls") for x in cand_only_vis if nameable(x)), None)},
              missing=[label_of(x) for x in base_only_vis], extra=[label_of(x) for x in cand_only_vis])
    for l, u, how in pairs:
        el = {"role": l.get("role"), "name": label_of(l), "candidate_name": label_of(u), "match": how, "where": where,
              "baseline_cls": l.get("cls"), "candidate_cls": u.get("cls"), "baseline_box": l.get("box"), "candidate_box": u.get("box")}
        is_control = group(l.get("role")) == "control"
        if l.get("visible") and not u.get("visible"):
            F.add("high" if is_control else "medium", route, "hidden-in-candidate",
                  f"{describe(l)} is hidden in {C()}", f"{describe(u)} is present but not visible",
                  f"visible, as in {B()}", element=el)
            continue
        if u.get("visible") and not l.get("visible"):
            F.add("medium", route, "visible-in-candidate", f"{describe(u)} is visible in {C()} but hidden in {B()}",
                  f"{describe(u)} is visible", f"hidden, as in {B()}", element=el)
            continue
        if not (l.get("visible") and u.get("visible")):
            continue
        if is_control and not l.get("disabled") and u.get("disabled"):
            F.add("high", route, "disabled-in-candidate", f"{describe(l)} is disabled in {C()}",
                  f"{describe(u)} is disabled", f"enabled, as in {B()}", element=el)
        elif is_control and l.get("disabled") and not u.get("disabled"):
            F.add("medium", route, "enabled-in-candidate", f"{describe(l)} is enabled in {C()} but disabled in {B()}",
                  f"{describe(u)} is enabled", f"disabled, as in {B()}", element=el)
        if how in ("loose", "near") and label_differs(l, u):
            F.add("medium", route, "label-drift", f"label drift: “{label_of(u)}” should read “{label_of(l)}”",
                  f"reads “{label_of(u)}”", f"reads “{label_of(l)}”", element=el)
        if (l.get("icon") or "") != (u.get("icon") or "") and how != "data":
            F.add("medium", route, "icon-drift", f"{describe(l)} icon differs",
                  f"icon {u.get('icon') or 'none'}", f"icon {l.get('icon') or 'none'}", element=el)
        deltas = compare_styles(l, u, compare_size=(how != "data"))
        if base_hidden or not deltas:
            continue
        sev = min((d[3] for d in deltas), key=SEV_RANK.get)
        cat = "style-drift" if sev == "medium" else "subpixel-drift"
        F.add(sev, route, cat, f"{describe(l)} — {len(deltas)} style difference{'s' if len(deltas) > 1 else ''}",
              describe(u, [d for d in deltas if d[3] == sev] or deltas, "candidate"),
              describe(l, [d for d in deltas if d[3] == sev] or deltas, "baseline"),
              element=el, deltas=[{"prop": p, "baseline": a, "candidate": b, "severity": s, "delta_px": dd} for p, a, b, s, dd in deltas])
    for l in ([] if wholesale else base_only):
        if not l.get("visible"):
            continue
        is_control = group(l.get("role")) == "control"
        F.add("high" if is_control else "medium", route, "missing-in-candidate",
              f"{describe(l)} is missing in {C()}", "not present on the page",
              f"a visible {describe(l)} as in {B()} (≈{int(l['box']['y'])}px down the content column)",
              element={"role": l.get("role"), "name": label_of(l), "where": where, "baseline_cls": l.get("cls"), "baseline_box": l.get("box")})
    for u in ([] if (wholesale or base_placeholder) else cand_only):
        if not u.get("visible"):
            continue
        F.add("medium", route, "extra-in-candidate", f"{describe(u)} is in {C()} but not in {B()}",
              f"{describe(u)} is shown", f"not shown ({B()} has no counterpart)",
              element={"role": u.get("role"), "name": label_of(u), "where": where, "candidate_cls": u.get("cls"), "candidate_box": u.get("box")})
    return {"pairs": len(pairs), "baseline_only": len(base_only_vis), "candidate_only": len(cand_only_vis), "data_pairs": data_n,
            "wholesale": wholesale, "baseline_placeholder": base_placeholder, "baseline_visible": len(base_vis), "candidate_visible": len(cand_vis)}


def cmd_diff(a):
    plan = plan_of(a.run)
    noise = load_noise(plan)
    F = Findings()
    routes_meta = {}
    info = {"baseline_console_errors": [], "warnings": [], "noise": {}, "blocked_writes": []}
    for r in plan["routes"]:
        rid = r["id"]
        base = {env: rjson(os.path.join(a.run, "dumps", rid, env, "base.json")) for env in ENVS}
        meta = {"baseline_base": bool(base["baseline"]), "candidate_base": bool(base["candidate"]), "hidden": [], "modals": {}}
        routes_meta[rid] = meta
        for env in ENVS:
            b = base[env]
            if b and (b.get("meta") or {}).get("visibility") == "hidden":
                meta["hidden"].append(env)
            for w in ((b or {}).get("meta") or {}).get("blocked") or []:
                info["blocked_writes"].append({"route": rid, "env": env, **w})
        # console (graded even if a dump is missing — an error on load is a finding by itself)
        for env in ENVS:
            rows, read = console_rows(a.run, rid, env, base[env])
            meta[f"{env}_console"] = read
            seen = set()
            for m in rows:
                text = m.get("text") or ""
                lvl = (m.get("level") or "").lower()
                if lvl not in ("error", "warn"):
                    continue
                n = next((why for pat, why in noise if pat.search(text) or pat.search(m.get("source") or "")), None)
                if n is not None:
                    info["noise"][n] = info["noise"].get(n, 0) + 1
                    continue
                key = (lvl, norm_msg(text))
                if key in seen:
                    continue
                seen.add(key)
                if lvl == "warn":
                    info["warnings"].append({"route": rid, "env": env, "text": text[:300]})
                elif env == "baseline":
                    info["baseline_console_errors"].append({"route": rid, "text": text[:300]})
                else:
                    F.add("high", rid, "console-error", f"console error in {C()}: {text[:120]}",
                          f"{C()} logs `{text[:300]}`", f"no error (not known noise; {B()}'s counterpart page does not log it)"
                          if not any(norm_msg(x['text']) == norm_msg(text) for x in info['baseline_console_errors'] if x['route'] == rid)
                          else f"no error ({B()} logs the same message — shared cause, still not known noise)",
                          console={"text": text[:600], "source": m.get("src")})
        if not (base["baseline"] and base["candidate"]):
            continue
        if base["candidate"]["meta"].get("rootFound") is False and base["baseline"]["meta"].get("rootFound") is not False:
            meta["not_rendered"] = True
            F.add("high", rid, "page-not-rendered", f"{C()} did not render the page's content column",
                  f"no `{r['roots']['candidate']}` on {base['candidate']['meta'].get('url')} (error page, redirect or crash)",
                  f"the page renders as in {B()}")
            continue
        if meta["hidden"]:
            continue  # a hidden tab freezes transitions and timers: its measurements are not evidence
        meta["base"] = diff_items(F, rid, "page", base["baseline"].get("items") or [], base["candidate"].get("items") or [])
        if meta["base"].get("baseline_placeholder"):
            meta.setdefault("modal_notes", []).append(
                f"the {B()} page is a placeholder ({meta['base']['baseline_visible']} {B()} elements vs {meta['base']['candidate_visible']} in {C()}) — "
                f"no design to compare against; {C()}'s elements are not graded as extra")
        # dialogs open at load
        ld = {d["name"]: d for d in base["baseline"].get("dialogs") or []}
        ud = {d["name"]: d for d in base["candidate"].get("dialogs") or []}
        for name in sorted(set(ld) | set(ud)):
            if name in ld and name in ud:
                diff_items(F, rid, f"dialog “{name}”", ld[name]["items"], ud[name]["items"])
        # modals opened by triggers
        names = sorted({f[6:-5] for env in ENVS for f in _modal_files(a.run, rid, env)})
        for name in names:
            lm = rjson(os.path.join(a.run, "dumps", rid, "baseline", f"modal-{name}.json"))
            um = rjson(os.path.join(a.run, "dumps", rid, "candidate", f"modal-{name}.json"))
            lok, uok = bool(lm and not lm.get("error")), bool(um and not um.get("error"))
            meta["modals"][name] = {"baseline": "opened" if lok else (lm or {}).get("error", "not attempted"),
                                    "candidate": "opened" if uok else (um or {}).get("error", "not attempted")}
            if lok and um is not None and (um or {}).get("error") == "guard-blocked":
                meta.setdefault("modal_notes", []).append(f"dialog “{name}”: {C()} did not open it while the read-only guard was refusing a request — not graded; check by hand")
            elif lok and um is not None and (um or {}).get("error") == "trigger-not-found":
                # the trigger's label differs (or it is missing) in the candidate — the button diff
                # already reports that; claiming "the dialog does not open" here would be a false High.
                meta.setdefault("modal_notes", []).append(f"dialog “{name}”: {C()} trigger not found by {B()}'s label — see the control findings")
            elif lok and not uok and um is not None:
                F.add("high", rid, "dialog-does-not-open", f"dialog “{name}” opens in {B()} but not in {C()}",
                      f"clicking “{(um.get('meta') or {}).get('label') or name}” in {C()}: {um.get('error')}",
                      f"the “{name}” dialog opens, as in {B()}",
                      trigger_label=(um.get("meta") or {}).get("label") or (lm.get("meta") or {}).get("label") or name, dialog=name)
            elif lok and uok:
                diff_items(F, rid, f"dialog “{name}”", lm.get("items") or [], um.get("items") or [])
                ls, us = lm.get("shell") or {}, um.get("shell") or {}
                d = compare_styles({"s": ls.get("s"), "box": ls.get("box"), "role": "dialog"},
                                   {"s": us.get("s"), "box": us.get("box"), "role": "dialog"}, compare_size=False)
                for dim in ("w", "h"):
                    x, y = (ls.get("box") or {}).get(dim), (us.get("box") or {}).get(dim)
                    if x is not None and y is not None and abs(x - y) >= NOISE_FLOOR:
                        dd = abs(x - y)
                        d.append((f"box.{dim}", f"{x}px", f"{y}px", "low" if dd < LOW_MAX else "medium", round(dd, 2)))
                if d:
                    sev = min((x[3] for x in d), key=SEV_RANK.get)
                    F.add(sev, rid, "style-drift" if sev == "medium" else "subpixel-drift",
                          f"dialog “{name}” shell — {len(d)} style difference{'s' if len(d) > 1 else ''}",
                          describe({"role": "dialog", "name": name}, d, "candidate"), describe({"role": "dialog", "name": name}, d, "baseline"),
                          element={"role": "dialog", "name": name, "where": f"dialog “{name}”"},
                          deltas=[{"prop": p, "baseline": x, "candidate": y, "severity": s, "delta_px": dd} for p, x, y, s, dd in d])
    # One cause, one High: a dialog that "does not open" because its trigger is disabled, hidden or
    # missing in the candidate is the trigger finding — fold it in rather than count the same defect twice.
    trig = {(f["route"], loose((f.get("element") or {}).get("name"))): f for f in F.rows
            if f["category"] in ("disabled-in-candidate", "hidden-in-candidate", "missing-in-candidate")}
    kept = []
    for f in F.rows:
        if f["category"] == "dialog-does-not-open":
            hit = trig.get((f["route"], loose(f.get("trigger_label") or "")))
            if hit:
                hit.setdefault("also", []).append(f["title"])
                hit["after"] += f" (it opens the “{f.get('dialog')}” dialog in {B()})"
                continue
        kept.append(f)
    F.rows = kept
    F.rows.sort(key=lambda f: (SEV_RANK[f["severity"]], [x["id"] for x in plan["routes"]].index(f["route"]), f["category"], f["title"]))
    counters = {"high": 0, "medium": 0, "low": 0}
    for f in F.rows:
        counters[f["severity"]] += 1
        f["id"] = f"{f['severity'][0].upper()}-{counters[f['severity']]:02d}"
    wjson(os.path.join(a.run, "findings.json"), {"at": iso(), "counts": counters, "routes": routes_meta, "info": info,
                                                   "envs": plan.get("envs"), "findings": F.rows})
    out({"status": "ok", "counts": counters, "routes": {k: {kk: v for kk, v in m.items() if kk != "modals"} for k, m in routes_meta.items()}})


# --- locate --------------------------------------------------------------------------------

_grep_cache = {}


def git_grep(repo, ref, needle, paths, source):
    key = (repo, ref, needle, tuple(paths))
    if key in _grep_cache:
        return _grep_cache[key]
    try:
        p = subprocess.run(["git", "-C", repo, "grep", "-l", "-F", "-e", needle, ref, "--", *(paths or ["."])],
                           capture_output=True, text=True, timeout=30)
        files = [line.split(":", 1)[1] for line in p.stdout.splitlines() if ":" in line] if p.returncode in (0, 1) else []
    except (OSError, subprocess.TimeoutExpired):
        files = []
    # Where an element RENDERS: components and their styles, never tests, stories or fixtures.
    exts = tuple(source.get("extensions") or [])
    files = [f for f in files if (not exts or f.endswith(exts))
             and not re.search(r"(__tests__|__mocks__|\.(test|spec|stories)\.|/tests?/|/fixtures?/)", f)]
    _grep_cache[key] = files
    return files


def rank_files(repo, ref, paths, cls_chain, text, area, source):
    scores = {}
    comp = tuple(source.get("component_extensions") or [])
    logic = [f"/{d.strip('/')}/" for d in source.get("logic_dirs") or []]
    for depth, level in enumerate(cls_chain or []):
        for c in (level or [])[:3]:
            if len(c) < 4:
                continue
            hits = git_grep(repo, ref, c, paths, source)
            if len(hits) > 12:        # a utility/shared class says nothing about where THIS element lives
                continue
            for f in hits:
                scores[f] = scores.get(f, 0) + (3 if depth == 0 else 1)
    if text and 3 <= len(text) <= 60 and not text.startswith("["):
        for f in git_grep(repo, ref, text, paths, source):
            scores[f] = scores.get(f, 0) + 2
    for f in list(scores):
        if area and f"/{area}/" in f:
            scores[f] += 1
        if comp and f.endswith(comp):
            scores[f] += 1          # markup lives in components
        elif any(d in f for d in logic):
            scores[f] -= 1          # logic, rarely where a control's markup or style is
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:3]


def brain_source_map(area):
    try:
        p = subprocess.run([sys.executable, KB, "read", "app_map", "--kind", "source-map", "--area", area],
                           capture_output=True, text=True, timeout=120)
        data = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
    except Exception:
        return None
    rows = data.get("rows") or data.get("results") or (data if isinstance(data, list) else [])
    outm = {}
    for r in rows if isinstance(rows, list) else []:
        v = r.get("value")
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                v = None
        if isinstance(v, dict) and r.get("key"):
            outm[r["key"]] = {**v, "id": r.get("id")}
    return outm


def source_key(f):
    el = f.get("element") or {}
    return f"parity:{f['route']}:{el.get('role', 'page')}:{slug(el.get('name') or f['category'])}"


def cmd_locate(a):
    plan = plan_of(a.run)
    fpath = os.path.join(a.run, "findings.json")
    data = rjson(fpath)
    if not data:
        fail("usage", "no findings.json — run `sweep.py diff` first", code=1)
    c = stack()
    source = {**SOURCE_DEFAULTS, **(plan.get("source") or {})}
    branch = str(c.get("project.default_branch") or "main")
    ref = f"{a.remote}/{branch}"
    cand_repo = a.candidate_repo or (str(c.repo_dir()) if c.repo_dir() else None)
    base_repo = a.baseline_repo or (str(c.resolve(source.get("baseline_repo_dir"))) if source.get("baseline_repo_dir") else None)
    if not cand_repo or not os.path.isdir(cand_repo):
        fail("usage", f"no candidate repo to search: pass --candidate-repo DIR or set app.repo_dir in stack.config.json (got {cand_repo!r})", code=1)
    # Update remote refs only — never a working tree. The baseline repo (when there is one) stays read-only.
    for repo in [r for r in (cand_repo, base_repo) if r]:
        subprocess.run(["git", "-C", repo, "fetch", "-q", a.remote, branch], capture_output=True, timeout=120)
    areas = {r["id"]: r.get("area") or "" for r in plan["routes"]}
    brain = {}
    if not a.no_brain:
        for area in sorted(set(areas.values())):
            m = brain_source_map(area)
            if m is None:
                brain = None
                break
            brain.update(m)
    located = 0
    for f in data["findings"]:
        if f["severity"] == "low":
            continue
        el = f.get("element") or {}
        area = areas.get(f["route"], "")
        prior = (brain or {}).get(source_key(f))
        if f["category"] == "console-error":
            f["source"] = {"candidate": [], "baseline": [], "note": "a console error — the stack trace, not the DOM, names the file"}
            continue
        cand = rank_files(cand_repo, ref, source.get("candidate_paths") or [], el.get("candidate_cls") or el.get("baseline_cls"),
                          el.get("candidate_name") or el.get("name"), area, source)
        basef = rank_files(base_repo, ref, source.get("baseline_paths") or [], el.get("baseline_cls"), el.get("name"), area, source) if base_repo else []
        if prior and prior.get("candidate_file"):
            cand = [(prior["candidate_file"], 99)] + [x for x in cand if x[0] != prior["candidate_file"]][:2]
        if prior and prior.get("baseline_file") and not basef:
            basef = [(prior["baseline_file"], 99)]
        f["source"] = {"candidate": [{"file": p, "score": s} for p, s in cand], "baseline": [{"file": p, "score": s} for p, s in basef],
                       "from_brain": bool(prior), "brain_id": (prior or {}).get("id")}
        located += bool(cand)
    data["located_at"] = iso()
    data["brain"] = "unreachable" if brain is None else "read"
    data["source_ref"] = ref
    wjson(fpath, data)
    out({"status": "ok", "located": located, "brain": data["brain"], "ref": ref,
         "candidate_repo": cand_repo, "baseline_repo": base_repo})


# --- fix candidates + fix records -------------------------------------------------------------

def r6_denylisted(path):
    """The R6 blast-radius denylist, from dev-fix's scanner (one copy of the patterns: the target
    repo's conventions file, else the framework floor). Unscannable counts as denylisted — fail closed."""
    try:
        p = subprocess.run(["bash", R6_SCAN, "--files", path], capture_output=True, text=True, timeout=30)
        return p.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        return True


def cmd_candidates(a):
    plan = plan_of(a.run)
    data = rjson(os.path.join(a.run, "findings.json")) or {}
    source = {**SOURCE_DEFAULTS, **(plan.get("source") or {})}
    fixable = tuple((source.get("style_extensions") or []) + (source.get("component_extensions") or []))
    cands = []
    for f in data.get("findings") or []:
        if f["severity"] != "medium" or f["category"] != "style-drift" or f.get("element", {}).get("match") == "data":
            continue
        src = (f.get("source") or {}).get("candidate") or []
        if not src or not (f.get("source") or {}).get("baseline"):
            continue
        top = src[0]
        second = src[1]["score"] if len(src) > 1 else 0
        if top["score"] < 3 or top["score"] < 2 * second:
            continue  # not "obvious": the element's class does not point at one file
        if not (fixable and top["file"].endswith(fixable)) or r6_denylisted(top["file"]):
            continue
        props = [d for d in f.get("deltas") or [] if d["severity"] == "medium" and not d["prop"].startswith("box.")]
        if not props or len(props) > 4:
            continue  # a box-size-only drift has no single rule to port; >4 props is not a one-line fix
        cands.append({"id": f["id"], "route": f["route"], "title": f["title"], "file": top["file"],
                      "baseline_file": f["source"]["baseline"][0]["file"], "props": props})
    cands.sort(key=lambda c: (len(c["props"]), c["id"]))
    lim = plan.get("limits") or limits()
    out({"status": "ok", "count": len(cands), "candidates": cands[:6], "limit": lim["max_fix_prs"]})


def cmd_fixed(a):
    if not a.skipped and not (a.pr and a.url and a.branch):
        fail("usage", "record either --pr/--url/--branch or --skipped REASON", code=1)
    if not a.skipped and not a.finding:
        fail("usage", "a PR record needs --finding", code=1)
    if not a.skipped and (rjson(os.path.join(a.run, "plan.json"), {}) or {}).get("rehearsal"):
        fail("rehearsal", "this run was initialised with --rehearsal: no PR may be recorded (or opened)", code=3)
    rows = [r for r in jl_read(os.path.join(a.run, "fixes.jsonl")) if r.get("pr")]
    cap = (rjson(os.path.join(a.run, "plan.json"), {}).get("limits") or limits())["max_fix_prs"]
    if not a.skipped and len(rows) >= cap:
        fail("fix-cap", f"{len(rows)} fix PRs are already recorded for this run — the cap is {cap} (loop-limits.json ui_parity_sweep.max_fix_prs)", code=3)
    row = {"finding": a.finding or "(run)", "at": iso()}
    row.update({"skipped": a.skipped} if a.skipped else {"pr": a.pr, "url": a.url, "branch": a.branch, "draft": bool(a.draft)})
    jl_append(os.path.join(a.run, "fixes.jsonl"), row)
    out({"status": "ok", **row})


# --- coverage + report -----------------------------------------------------------------------

def coverage(run, plan, data):
    """Per route AND per environment: each env either has its page dump and its console read, or a
    mark (for that env, or for the whole route) that says why not. Anything else is a silent skip."""
    marks = jl_read(os.path.join(run, "coverage.jsonl"))
    rows, silent = [], []
    for r in plan["routes"]:
        rid = r["id"]
        m = (data.get("routes") or {}).get(rid) or {}
        mk = [x for x in marks if x["route"] == rid]
        have = {env: bool(m.get(f"{env}_base")) for env in ENVS}
        cons = {env: bool(m.get(f"{env}_console")) for env in ENVS}
        notes = [f"{LABELS.get(x.get('env'), x.get('env')) if x.get('env') else 'both'}{(' ' + x['what']) if x.get('what') else ''}: {x['status']} — {x['reason']}"
                 for x in mk if x["status"] != "covered"]
        if m.get("hidden"):
            notes.append(f"measured while the tab was hidden ({', '.join(LABELS[e] for e in m['hidden'])}) — style comparison skipped")
        if m.get("not_rendered"):
            notes.append(f"{C()} did not render the page's content column — nothing to compare (see the High finding)")
        notes += m.get("modal_notes") or []
        explained = lambda env: any(x["status"] != "covered" and x.get("env") in (env, None) and (x.get("reason") or "").strip() for x in mk)
        unexplained = [env for env in ENVS if not (have[env] and cons[env]) and not explained(env)]
        anything = any(have.values()) or any(cons.values())
        if all(have.values()) and all(cons.values()) and not m.get("hidden") and not m.get("not_rendered"):
            status = "covered"
        elif not anything:
            worst = next((x["status"] for x in mk if x["status"] in ("failed", "skipped")), None)
            status = f"not covered ({worst})" if worst else "not covered"
        else:
            status = "partial"
        missing = [f"{LABELS[env]} {what}" for env in ENVS for what, ok in (("page", have[env]), ("console", cons[env])) if not ok]
        if unexplained:
            silent.append(rid if len(unexplained) == 2 else f"{rid} ({LABELS[unexplained[0]]})")
            notes.append(f"SILENT SKIP — {' and '.join(LABELS[e] for e in unexplained)}: nothing captured and no reason recorded")
        base = m.get("base") or {}
        rows.append({"route": rid, "status": status, "missing": missing, "notes": notes, "modals": m.get("modals") or {},
                     "matched": base.get("pairs"), "baseline_only": base.get("baseline_only"), "candidate_only": base.get("candidate_only"),
                     "data_pairs": base.get("data_pairs")})
    return rows, silent


def md_escape(s):
    return str(s).replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")


def cmd_report(a):
    plan = plan_of(a.run)
    data = rjson(os.path.join(a.run, "findings.json"))
    if not data:
        fail("usage", "no findings.json — run `sweep.py diff` (and `locate`) first", code=1)
    t = now()
    if a.regrade:
        # Grading a finished capture later: the budget was spent by the CAPTURE, so measure to its
        # last artifact, not to now — a regrade next week is not a budget overrun.
        # The artifacts' OWN timestamps (meta.at / at), not file mtimes: a copied run directory
        # (cp -R) restamps every file with the copy time.
        stamps = []
        for sub in ("dumps", "console"):
            for dp, _, fs in os.walk(os.path.join(a.run, sub)):
                for f in fs:
                    d = rjson(os.path.join(dp, f), {}) or {}
                    at = (d.get("meta") or {}).get("at") or d.get("at")
                    try:
                        stamps.append(datetime.datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp())
                    except (TypeError, ValueError):
                        pass
        t = max(stamps) if stamps else plan["started_at"]
    elapsed = max(0.0, t - plan["started_at"])
    over = t > plan["deadlines"]["report"]
    cov, silent = coverage(a.run, plan, data)
    fixes = jl_read(os.path.join(a.run, "fixes.jsonl"))
    F = data["findings"]
    by = {s: [f for f in F if f["severity"] == s] for s in ("high", "medium", "low")}
    envs = plan.get("envs") or {}
    ref = data.get("source_ref") or "the default branch"
    L = []
    L.append(f"# UI parity sweep — {B()} vs {C()} · {plan['started_iso'][:10]}")
    L.append("")
    L.append(f"Run `{plan['run_id']}` · {B()} {envs.get('baseline', {}).get('url')} · {C()} {envs.get('candidate', {}).get('url')} · started {plan['started_iso']}")
    L.append(f"Elapsed **{int(elapsed // 60)}m {int(elapsed % 60)}s** of a **{plan['budget_min']}-minute** budget"
             + (" — **BUDGET EXCEEDED** (the report was written late; see coverage)" if over else " — within budget"))
    blocked = (data.get("info") or {}).get("blocked_writes") or []
    L.append(f"Posture: read-only. The in-page guard refused {len(blocked)} write request(s); no data was created."
             + (" **Rehearsal:** no PRs, no brain writes." if plan.get("rehearsal") else ""))
    if a.regrade:
        L.append(f"Regraded {iso()} from the capture on disk; elapsed is measured to the capture's last artifact.")
    L.append("")
    covered = sum(1 for c in cov if c["status"] == "covered")
    L.append(f"**{len(by['high'])} High · {len(by['medium'])} Medium · {len(by['low'])} Low** · routes covered {covered}/{len(cov)}"
             + (f" · **{len(silent)} SILENTLY SKIPPED**" if silent else ""))
    L.append("")
    L.append("## Coverage by route")
    L.append("")
    L.append(f"| Route | Status | Matched | {B()} only | {C()} only | Same-component data pairs | Dialogs ({B()} / {C()}) | Notes |")
    L.append("|---|---|---|---|---|---|---|---|")
    for c in cov:
        dl = "; ".join(f"{n}: {v['baseline']} / {v['candidate']}" for n, v in c["modals"].items()) or "—"
        notes = "; ".join(c["notes"] + ([f"missing: {', '.join(c['missing'])}"] if c["missing"] else [])) or "—"
        L.append(f"| {c['route']} | {c['status']} | {c['matched'] if c['matched'] is not None else '—'} | "
                 f"{c['baseline_only'] if c['baseline_only'] is not None else '—'} | {c['candidate_only'] if c['candidate_only'] is not None else '—'} | "
                 f"{c['data_pairs'] if c['data_pairs'] is not None else '—'} | {md_escape(dl)} | {md_escape(notes)} |")
    L.append("")
    for sev, title in (("high", "High — broken functionality or console errors"), ("medium", "Medium — visible style or label drift")):
        L.append(f"## {title} ({len(by[sev])})")
        L.append("")
        if not by[sev]:
            L.append("None.")
            L.append("")
        for f in by[sev]:
            L.append(f"### {f['id']} · {f['route']} · {md_escape(f['title'])}")
            L.append("")
            L.append(f"- **Before ({C()} now):** {md_escape(f['before'])}")
            L.append(f"- **After (match {B()}):** {md_escape(f['after'])}")
            src = f.get("source") or {}
            if src.get("candidate"):
                tie = len(src["candidate"]) > 1 and src["candidate"][0]["score"] == src["candidate"][1]["score"]
                L.append(f"- **Likely source:** `{src['candidate'][0]['file']}`" + (" (tied — check the next one too)" if tie else "")
                         + (f" (also {', '.join('`' + x['file'] + '`' for x in src['candidate'][1:])})" if len(src["candidate"]) > 1 else "")
                         + (" — from the brain's source map" if src.get("from_brain") else ""))
            elif src.get("note"):
                L.append(f"- **Likely source:** {src['note']}")
            else:
                L.append("- **Likely source:** not located (no distinctive class or label to search for)")
            if src.get("baseline"):
                L.append(f"- **{B()} reference:** `{src['baseline'][0]['file']}` ({ref})")
            if f.get("deltas"):
                shown = [d for d in f["deltas"] if d["severity"] == sev] or f["deltas"]
                L.append("- **Differences:** " + "; ".join(f"`{d['prop']}` {md_escape(d['candidate'])} → {md_escape(d['baseline'])}" for d in shown[:8])
                         + (" …" if len(shown) > 8 else ""))
            for also in f.get("also") or []:
                L.append(f"- **Also:** {md_escape(also)} — the same cause, counted once")
            where = (f.get("element") or {}).get("where")
            if where and where != "page":
                L.append(f"- **Where:** {md_escape(where)}")
            L.append("")
    L.append(f"## Low — cosmetic drift under 2px ({len(by['low'])})")
    L.append("")
    if by["low"]:
        L.append(f"| ID | Route | Element | Differences ({C()} → {B()}) |")
        L.append("|---|---|---|---|")
        for f in by["low"]:
            d = "; ".join(f"{x['prop']} {x['candidate']}→{x['baseline']}" for x in f.get("deltas") or [])
            L.append(f"| {f['id']} | {f['route']} | {md_escape((f.get('element') or {}).get('name', ''))} | {md_escape(d[:200])} |")
    else:
        L.append("None.")
    L.append("")
    L.append("## Fix PRs")
    L.append("")
    prs = [x for x in fixes if x.get("pr")]
    if prs:
        for x in prs:
            L.append(f"- **{x['finding']}** → PR [{x['pr']}]({x['url']}) on `{x['branch']}`"
                     + (" — **DRAFT: nobody is notified until it is published**" if x.get("draft") else ""))
    for x in fixes:
        if x.get("skipped"):
            who = "Fix phase" if x["finding"] == "(run)" else x["finding"]
            L.append(f"- {who}: not opened — {md_escape(x['skipped'])}")
    if not fixes:
        L.append("None opened.")
    L.append("")
    info = data.get("info") or {}
    L.append("## Other observations (not graded)")
    L.append("")
    if info.get("baseline_console_errors"):
        L.append(f"- **{B()}-side console errors** (the reference's, not {C()}'s): " + "; ".join(
            f"{x['route']}: `{md_escape(x['text'][:120])}`" for x in info["baseline_console_errors"][:10]))
    if info.get("warnings"):
        L.append(f"- **Console warnings** ({len(info['warnings'])}): " + "; ".join(
            f"{x['route']}/{LABELS.get(x['env'], x['env'])}: `{md_escape(x['text'][:100])}`" for x in info["warnings"][:10]))
    if info.get("noise"):
        L.append("- **Known console noise ignored:** " + ", ".join(f"{k} ×{v}" for k, v in sorted(info["noise"].items())))
    if blocked:
        L.append("- **Writes the guard refused** (the page tried to write while only being viewed): " + "; ".join(
            f"{b['route']}/{LABELS.get(b['env'], b['env'])} {b.get('method')} {md_escape(b.get('url', '')[:80])}" for b in blocked[:10]))
    if not any(info.get(k) for k in ("baseline_console_errors", "warnings", "noise")) and not blocked:
        L.append("None.")
    L.append("")
    L.append(f"_Grading: High = broken functionality (a {B()} control missing, hidden or disabled in {C()}; a dialog that does not "
             f"open; a page that does not render) or a {C()} console error that is not known noise. Medium = visible style or "
             "label drift (≥2px, any colour/font/shadow change, text). Low = drift under 2px. Elements are matched by role "
             "and text; same-component rows with different data are compared for style only._")
    rpath = os.path.join(a.run, "parity-report.md")
    with open(rpath, "w") as fh:
        fh.write("\n".join(L) + "\n")
    res = {"status": "ok" if not silent else "coverage-gap", "report": rpath, "counts": data.get("counts"),
           "covered": covered, "routes": len(cov), "silent_skips": silent, "over_budget": over,
           "elapsed_s": round(elapsed)}
    out(res, 4 if silent else 0)


# --- write-back --------------------------------------------------------------------------------

def kb(args, dry):
    cmd = [sys.executable, KB] + args
    if dry:
        return {"dry_run": True, "cmd": cmd[2:]}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        last = (p.stdout.strip().splitlines() or ["{}"])[-1]
        return json.loads(last) if last.startswith("{") else {"ok": False, "raw": last[:200], "rc": p.returncode}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def cmd_writeback(a):
    plan = plan_of(a.run)
    if plan.get("rehearsal") and not a.dry_run:
        a.dry_run = True   # a rehearsal writes nothing to the shared brain
    data = rjson(os.path.join(a.run, "findings.json")) or {}
    cov, silent = coverage(a.run, plan, data)
    counts = data.get("counts") or {}
    areas = {r["id"]: r.get("area") or "" for r in plan["routes"]}
    meta = {"counts": counts, "routes": len(cov), "covered": sum(c["status"] == "covered" for c in cov),
            "silent_skips": silent, "elapsed_s": round(now() - plan["started_at"]), "budget_min": plan["budget_min"],
            "envs": plan.get("envs"),
            "fix_prs": [x for x in jl_read(os.path.join(a.run, "fixes.jsonl")) if x.get("pr")]}
    results = [{"metric": kb(["metric", "--name", "ui-parity-sweep", "--value", str(counts.get("high", 0)), "--unit", "high_findings",
                              "--area", "ui-parity", "--source", "qa-ui-parity-sweep", "--run-id", plan["run_id"],
                              "--metadata", json.dumps(meta)], a.dry_run)}]
    written = 0
    for f in sorted(data.get("findings") or [], key=lambda f: SEV_RANK[f["severity"]]):
        src = f.get("source") or {}
        if f["severity"] == "low" or not src.get("candidate") or src.get("from_brain") or written >= 25:
            continue
        el = f.get("element") or {}
        val = {"candidate_file": src["candidate"][0]["file"], "baseline_file": (src.get("baseline") or [{}])[0].get("file"),
               "candidate_classes": (el.get("candidate_cls") or [[]])[0], "baseline_classes": (el.get("baseline_cls") or [[]])[0],
               "route": f["route"], "seen": f["id"]}
        results.append({"app_map": kb(["write", "app_map", "--kind", "source-map", "--area", areas.get(f["route"]) or "ui",
                                       "--key", source_key(f), "--value", json.dumps(val), "--source", "qa-ui-parity-sweep",
                                       "--confidence", "0.6", "--run-id", plan["run_id"]], a.dry_run)})
        written += 1
    ok = all((list(r.values())[0] or {}).get("ok", a.dry_run) for r in results)
    out({"status": "ok" if ok else "partial", "writes": len(results), "results": results[:8]}, 0 if ok else 2)


# --- main ----------------------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(prog="sweep.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("init")
    s.add_argument("--baseline-env", help="environment name under app.environments (default: integrations.ui_parity.baseline_env)")
    s.add_argument("--candidate-env", help="environment name under app.environments (default: integrations.ui_parity.candidate_env)")
    s.add_argument("--baseline-url", help="override the baseline url (the name still labels the report)")
    s.add_argument("--candidate-url", help="override the candidate url")
    s.add_argument("--routes"); s.add_argument("--only"); s.add_argument("--run"); s.add_argument("--budget-min", type=float, default=None)
    s.add_argument("--port", type=int, default=PORT); s.add_argument("--rehearsal", action="store_true")
    s = sub.add_parser("preflight"); s.add_argument("--run", required=True)
    s = sub.add_parser("serve"); s.add_argument("--run", required=True); s.add_argument("--port", type=int, default=PORT); s.add_argument("--background", action="store_true", help="run the collector detached; `stop` ends it")
    s = sub.add_parser("stop"); s.add_argument("--run", required=True)
    s = sub.add_parser("budget"); s.add_argument("--run", required=True); s.add_argument("--phase", choices=list(PHASES), required=True)
    s = sub.add_parser("console"); s.add_argument("--run", required=True); s.add_argument("--route", required=True)
    s.add_argument("--env", choices=ENVS, required=True); s.add_argument("--file")
    s.add_argument("--since-ms", type=int, help="the capture's loadedAtMs — drop entries logged before this page loaded")
    s = sub.add_parser("mark"); s.add_argument("--run", required=True); s.add_argument("--route", required=True)
    s.add_argument("--env", choices=ENVS); s.add_argument("--status", choices=["covered", "partial", "failed", "skipped"], required=True)
    s.add_argument("--reason", default=""); s.add_argument("--what")
    s = sub.add_parser("diff"); s.add_argument("--run", required=True)
    s = sub.add_parser("locate"); s.add_argument("--run", required=True)
    s.add_argument("--candidate-repo", help="the candidate's source repo (default: app.repo_dir)")
    s.add_argument("--baseline-repo", help="the design reference's repo, when it has one (default: routes file source.baseline_repo_dir)")
    s.add_argument("--remote", default="origin", help="git remote whose <project.default_branch> is searched")
    s.add_argument("--no-brain", action="store_true")
    s = sub.add_parser("candidates"); s.add_argument("--run", required=True)
    s = sub.add_parser("fixed"); s.add_argument("--run", required=True); s.add_argument("--finding")
    s.add_argument("--pr"); s.add_argument("--url"); s.add_argument("--branch"); s.add_argument("--draft", action="store_true"); s.add_argument("--skipped")
    s = sub.add_parser("report"); s.add_argument("--run", required=True); s.add_argument("--regrade", action="store_true")
    s = sub.add_parser("writeback"); s.add_argument("--run", required=True); s.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    if getattr(a, "run", None):
        a.run = os.path.abspath(a.run)
    return {"init": cmd_init, "preflight": cmd_preflight, "serve": cmd_serve, "stop": cmd_stop, "budget": cmd_budget, "console": cmd_console,
            "mark": cmd_mark, "diff": cmd_diff, "locate": cmd_locate, "candidates": cmd_candidates, "fixed": cmd_fixed,
            "report": cmd_report, "writeback": cmd_writeback}[a.verb](a)


if __name__ == "__main__":
    main()
