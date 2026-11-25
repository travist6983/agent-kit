#!/usr/bin/env python3
"""meta_monitor — the R8 loop-health guardrail: a deterministic trip / no-trip decision (no model).

Reads loop-health metrics through the qa-kb CLI (`kb metrics --breakdown --since <window>d`), compares
three signals against loop-limits.json `r8_meta_monitor`, and prints one decision document (contract
contracts/dev-loop-defect.meta-monitor.json):

  prod_regressed_rate     prod-regressed / (prod-confirmed + prod-regressed) prod_outcome rows;
                          inconclusive (and any other outcome) is excluded from both; evaluated only at
                          >= min_prod_verdicts decisive rows.
  escalation_rate         escalation rows / loop_outcome rows; evaluated only at >= min_loop_outcomes
                          loop_outcome rows. More escalations than loop outcomes means loop_outcome is
                          under-recorded: a warning, and this signal does not breach.
  repeat_failure_by_area  escalation rows per non-empty area (events, not distinct tickets); rows with no
                          area are reported as unattributed and never counted toward an area.

Every comparison is >=. decision: any breached signal = trip; else all three insufficient-data =
insufficient-data; else no-trip.

REPORT-ONLY BY DEFAULT. The one outward action is `kb pause --on --reason ... --by meta-monitor` (the R9
global pause), taken only when the decision is trip AND the limits section says trip_mode "armed" AND
armed_by names the human who armed it (both changed by PR in the tracked file) AND neither --report-only
nor --metrics-file was passed. There is no flag or environment variable that arms it. When the pause is
already on it is left alone (its reason is not rewritten). This script has no code path that turns the
pause off: unpausing is a human `kb pause` with the off switch, signed by that human.

Usage:
  meta_monitor.py [--limits PATH] [--metrics-file PATH] [--report-only] [--out PATH]

  --limits        the limits file (default: stack.config `limits_file`, else .claude/loops/loop-limits.json,
                  resolved the same way recipe_router.py resolves it)
  --metrics-file  read a JSON object carrying `breakdown` (the `kb metrics --breakdown` reply shape)
                  instead of calling kb; forces report-only and makes no kb call at all
  --report-only   never pause, whatever the limits say
  --out           also write the decision through contracts/emit.py (a contract violation writes nothing)

Exit codes:
  0  a decision was produced and its intended action done (reported, paused or already-paused)
  1  the decision was trip and armed, but the pause could not be set (action pause-failed)
  2  bad usage or bad limits (missing section, bool / non-number / out-of-range value, unknown trip_mode,
     armed with an empty armed_by): one JSON error line, no decision, no kb call
  3  metrics unreadable (kb failed, timed out, replied ok:false or without `breakdown`, or the metrics
     file is unreadable): one JSON error line, no pause call, nothing written to --out
  4  the decision document violated its own contract: nothing written to --out

Stdlib only: no database driver, no network. The brain is reached only through the kb CLI (KB_CLI
overrides its path, the same override pr_feed.py honours).
"""
import argparse
import json
import os
import signal
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.realpath(os.path.join(HERE, "..", "..", "..", ".."))
CONTRACT = os.path.join(ROOT, "contracts", "dev-loop-defect.meta-monitor.json")
EMIT = os.path.join(ROOT, "contracts", "emit.py")
KB_CLI = os.environ.get("KB_CLI", os.path.join(ROOT, ".claude", "skills", "qa-kb", "scripts", "kb.py"))

# Bounds one kb subprocess (process-group kill, as pr_feed.py does). It bounds a wait; it gates nothing.
KB_TIMEOUT_S = 60

SECTION = "r8_meta_monitor"
TRIP_MODES = ("report-only", "armed")
PAUSE_BY = "meta-monitor"

# The metric names today's writers use. Not config keys: renaming one is a change to its writer.
M_PROD = "prod_outcome"      # infra-prod-verify: `kb metric --name prod_outcome --metadata '{"outcome": ...}'`
M_ESCALATION = "escalation"  # dev-escalate: `kb metric --name escalation --value 1 --area <area>` (prose, optional)
M_LOOP = "loop_outcome"      # the recipe: one loop_outcome per finished loop (recipe-engine.md, prose)
PROD_CONFIRMED = "prod-confirmed"
PROD_REGRESSED = "prod-regressed"

INT_KEYS = ("window_days", "min_loop_outcomes", "min_prod_verdicts", "area_escalations_trip")
RATE_KEYS = ("escalation_rate_trip", "prod_regressed_rate_trip")
EPS = 1e-12


def err(msg, code):
    print(json.dumps({"ok": False, "error": msg}))
    return code


def default_limits_path():
    try:
        sys.path.insert(0, HERE)
        from recipe_router import _limits_path  # same resolution as the router
        return _limits_path()
    except Exception:
        return os.path.join(ROOT, ".claude", "loops", "loop-limits.json")


def load_limits(path):
    """(section, None) or (None, error message). Fails closed on every malformed value."""
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        return None, "limits unreadable: %s" % e
    sec = doc.get(SECTION) if isinstance(doc, dict) else None
    if not isinstance(sec, dict):
        return None, "%s section missing from %s" % (SECTION, path)
    for k in INT_KEYS:
        v = sec.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            return None, "%s.%s must be an integer >= 1, got %r" % (SECTION, k, v)
    if sec["area_escalations_trip"] < 2:
        return None, "%s.area_escalations_trip must be >= 2 (one escalation is not a repeat)" % SECTION
    for k in RATE_KEYS:
        v = sec.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 < v <= 1):
            return None, "%s.%s must be a number in (0, 1], got %r" % (SECTION, k, v)
    if sec.get("trip_mode") not in TRIP_MODES:
        return None, "%s.trip_mode must be one of %s, got %r" % (SECTION, list(TRIP_MODES), sec.get("trip_mode"))
    armed_by = sec.get("armed_by", "")
    if not isinstance(armed_by, str):
        return None, "%s.armed_by must be a string" % SECTION
    if sec["trip_mode"] == "armed" and not armed_by.strip():
        return None, "%s.trip_mode is armed but armed_by is empty: a human must sign the arming" % SECTION
    return sec, None


def run_bounded(cmd, timeout):
    """Run a command under a hard wall-clock bound (kill the whole process group); None if it overran."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    except OSError:
        return None
    try:
        o, e = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, o, e)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        return None


def kb(*args):
    """(reply dict, None) or (None, why). A non-zero exit or a non-JSON reply is a failure."""
    if not os.path.exists(KB_CLI):
        return None, "kb CLI not found at %s" % KB_CLI
    p = run_bounded([sys.executable, KB_CLI] + list(args), KB_TIMEOUT_S)
    if p is None:
        return None, "kb %s timed out after %ss" % (args[0], KB_TIMEOUT_S)
    try:
        rec = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None, "kb %s replied non-JSON (exit %s)" % (args[0], p.returncode)
    if p.returncode != 0 or not isinstance(rec, dict):
        return None, "kb %s failed (exit %s): %s" % (args[0], p.returncode, str(rec)[:200])
    return rec, None


def breakdown_rows(reply):
    """The validated breakdown rows, or None when the reply is not a usable breakdown."""
    if not isinstance(reply, dict) or reply.get("ok") is not True or not isinstance(reply.get("breakdown"), list):
        return None
    rows = []
    for r in reply["breakdown"]:
        if not isinstance(r, dict) or not isinstance(r.get("metric"), str):
            return None
        n = r.get("n")
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            return None
        rows.append({"metric": r["metric"], "area": str(r.get("area") or "").strip(),
                     "outcome": str(r.get("outcome") or "").strip(), "n": n})
    return rows


def hit(value, threshold):
    return value >= threshold - EPS


def fmt(x):
    return ("%.4f" % x).rstrip("0").rstrip(".") if isinstance(x, float) else str(x)


def compute(rows, lim):
    signals, breaches, warnings = [], [], []

    # prod_regressed_rate
    conf = sum(r["n"] for r in rows if r["metric"] == M_PROD and r["outcome"] == PROD_CONFIRMED)
    regr = sum(r["n"] for r in rows if r["metric"] == M_PROD and r["outcome"] == PROD_REGRESSED)
    other = sum(r["n"] for r in rows if r["metric"] == M_PROD) - conf - regr
    dec, thr, mn = conf + regr, lim["prod_regressed_rate_trip"], lim["min_prod_verdicts"]
    s = {"signal": "prod_regressed_rate", "numerator": regr, "denominator": dec, "threshold": thr,
         "min_samples": mn, "detail": "%d prod-regressed of %d decisive verdicts; %d excluded "
                                      "(inconclusive or other)" % (regr, dec, other)}
    if dec < mn:
        s.update(status="insufficient-data", value=None)
    else:
        v = regr / dec
        s.update(value=v, status="breached" if hit(v, thr) else "ok")
    signals.append(s)
    if s["status"] == "breached":
        breaches.append("prod_regressed_rate")

    # escalation_rate
    esc_rows = [r for r in rows if r["metric"] == M_ESCALATION]
    esc = sum(r["n"] for r in esc_rows)
    loops = sum(r["n"] for r in rows if r["metric"] == M_LOOP)
    thr, mn = lim["escalation_rate_trip"], lim["min_loop_outcomes"]
    s = {"signal": "escalation_rate", "numerator": esc, "denominator": loops, "threshold": thr,
         "min_samples": mn, "detail": "%d escalations over %d loop outcomes" % (esc, loops)}
    if loops < mn:
        s.update(status="insufficient-data", value=None)
    elif esc > loops:
        s.update(status="insufficient-data", value=None)
        warnings.append("escalation count %d exceeds loop_outcome count %d: loop_outcome is under-recorded; "
                        "escalation_rate not evaluated" % (esc, loops))
    else:
        v = esc / loops
        s.update(value=v, status="breached" if hit(v, thr) else "ok")
    signals.append(s)
    if s["status"] == "breached":
        breaches.append("escalation_rate")

    # repeat_failure_by_area
    by_area = {}
    for r in esc_rows:
        if r["area"]:
            by_area[r["area"]] = by_area.get(r["area"], 0) + r["n"]
    unattributed = sum(r["n"] for r in esc_rows if not r["area"])
    if unattributed:
        warnings.append("%d escalation(s) carry no area: reported as unattributed, never counted toward an area"
                        % unattributed)
    thr = lim["area_escalations_trip"]
    tail = "; %d unattributed escalation(s)" % unattributed
    base = {"signal": "repeat_failure_by_area", "denominator": None, "threshold": thr, "min_samples": 1}
    if not by_area:
        signals.append(dict(base, status="insufficient-data", value=None, numerator=0,
                            detail="no escalation carries an area" + tail))
    else:
        hot = sorted((a for a, n in by_area.items() if n >= thr), key=lambda a: (-by_area[a], a))
        if hot:
            for a in hot:
                signals.append(dict(base, status="breached", value=by_area[a], numerator=by_area[a], area=a,
                                    detail="%d escalations in area %s%s" % (by_area[a], a, tail)))
                breaches.append("repeat_failure_by_area:%s" % a)
        else:
            a = sorted(by_area, key=lambda x: (-by_area[x], x))[0]
            signals.append(dict(base, status="ok", value=by_area[a], numerator=by_area[a], area=a,
                                detail="highest area %s with %d escalations%s" % (a, by_area[a], tail)))

    order = ("signal", "status", "value", "numerator", "denominator", "threshold", "min_samples", "area", "detail")
    signals = [{k: x[k] for k in order if k in x} for x in signals]
    statuses = [x["status"] for x in signals]
    if "breached" in statuses:
        decision = "trip"
    elif "ok" not in statuses:
        decision = "insufficient-data"
    else:
        decision = "no-trip"
    return decision, signals, breaches, warnings


def pause_reason(signals, breaches, window, armed_by):
    parts = []
    for s in signals:
        if s["status"] != "breached":
            continue
        name = s["signal"] + (":" + s["area"] if s.get("area") else "")
        parts.append("%s %s>=%s" % (name, fmt(s["value"]), fmt(s["threshold"])))
    return "R8 meta-monitor: %s over %dd; armed by %s" % (", ".join(parts or breaches), window, armed_by)


def rel(path):
    p = os.path.realpath(path)
    return os.path.relpath(p, ROOT) if p.startswith(ROOT + os.sep) else p


def main():
    ap = argparse.ArgumentParser(description="R8 meta-monitor: loop-health trip / no-trip decision")
    ap.add_argument("--limits")
    ap.add_argument("--metrics-file")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--out")
    try:
        a = ap.parse_args()
    except SystemExit as e:
        return 2 if e.code else 0

    limits_path = a.limits or default_limits_path()
    lim, why = load_limits(limits_path)
    if lim is None:
        return err(why, 2)

    # 1) metrics
    if a.metrics_file:
        source = "file"
        try:
            with open(a.metrics_file) as fh:
                reply = json.load(fh)
        except (OSError, ValueError) as e:
            return err("metrics file unreadable: %s" % e, 3)
        if isinstance(reply, dict) and "ok" not in reply:
            reply = dict(reply, ok=True)
    else:
        source = "kb"
        reply, why = kb("metrics", "--breakdown", "--since", "%dd" % lim["window_days"])
        if reply is None:
            return err("metrics unreadable: " + why, 3)
    rows = breakdown_rows(reply)
    if rows is None:
        return err("metrics unreadable: the reply is not ok:true with a `breakdown` list of {metric, area, "
                   "outcome, n} rows (an older kb.py has no --breakdown)", 3)

    # 2) decision
    decision, signals, breaches, warnings = compute(rows, lim)

    # 3) armed?
    armed_by = lim.get("armed_by", "").strip()
    report_only_reason = None
    if lim["trip_mode"] != "armed":
        report_only_reason = "trip-mode-report-only"
    elif a.report_only:
        report_only_reason = "flag-report-only"
    elif source != "kb":
        report_only_reason = "metrics-file"
    armed = report_only_reason is None

    doc = {"decision": decision, "armed": armed, "action": "reported", "window_days": lim["window_days"],
           "metrics_source": source, "signals": signals, "breaches": breaches, "warnings": warnings}
    if lim["trip_mode"] == "armed":
        doc["armed_by"] = armed_by
    if report_only_reason:
        doc["report_only_reason"] = report_only_reason
    doc["pause"] = None
    doc["limits_file"] = rel(limits_path)

    # 4) the one outward action: pause ON, only when tripped and armed
    code = 0
    if decision == "trip" and armed:
        status, _ = kb("pause", "--status")
        if status is not None and status.get("ok") is True and status.get("paused") is True:
            doc["action"] = "already-paused"
        else:
            reason = pause_reason(signals, breaches, lim["window_days"], armed_by)
            res, _ = kb("pause", "--on", "--reason", reason, "--by", PAUSE_BY)
            receipt = res.get("receipt") if isinstance(res, dict) else None
            doc["pause"] = {"reason": reason, "by": PAUSE_BY,
                            "receipt": receipt if isinstance(receipt, dict) else None}
            if res is not None and res.get("ok") is True and res.get("paused") is True:
                doc["action"] = "paused"
            else:
                doc["action"] = "pause-failed"
                code = 1

    # 5) evidence
    if a.out:
        p = subprocess.run([sys.executable, EMIT, "--contract", CONTRACT, "--out", a.out,
                            "--from-json", "-", "--no-timing"],
                           input=json.dumps(doc), text=True, capture_output=True)
        if p.returncode != 0:
            sys.stderr.write(p.stderr or p.stdout)
            print(json.dumps(doc))
            return 4
    print(json.dumps(doc))
    return code


if __name__ == "__main__":
    sys.exit(main())
