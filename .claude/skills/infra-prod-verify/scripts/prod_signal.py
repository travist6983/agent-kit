#!/usr/bin/env python3
"""Fetch the R2 tie-back evidence through the adapter layer and print ONE JSON object.

    prod_signal.py --signature "<error reference>" --deployed-at 2026-01-01T12:00:00Z \
        [--env prod] [--before-window 24h] [--after-window 2h] \
        [--version <deployed release>] [--previous-version <release before it>] \
        [--new-signature-query "<provider query for errors first seen after deploy>"]

Roles used (from .claude/loops/stack.config.json):
  analytics.query_error_signal(signature, since, version)  — the before window, the after window and,
                                                             when --new-signature-query is given, the
                                                             new-signature probe
  deploy.deployed_version(env)                             — the release the after window is scoped to
                                                             (falls back to analytics.deployed_version,
                                                             then --version)

Output (feed it to evaluate_prod_signal.py --signal):
  { "ok": true, "signature", "env", "deployed_at", "deployed_version",
    "windows": {"before": "24h", "after": "2h"},
    "before": <ErrorSignal>, "after": <ErrorSignal>, "new": <ErrorSignal>|null,
    "before_rate": <per hour>, "after_rate": <per hour>, "new_signature": bool|null }

Exit codes: 0 ok · 1 query error · 2 usage · 3 adapter unavailable (analytics is `none` or cannot serve
the op). On 1 and 3 the object is {"ok": false, "error": "...", ...} — the caller passes it straight to
evaluate_prod_signal.py --signal, which turns it into `inconclusive`. Never crashes, never guesses.

No vendor client lives here: whatever the analytics provider is, it is reached through the adapter.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = Path(os.environ.get("LOOPS_REPO_ROOT") or HERE.parents[3])
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(1, str(REPO_ROOT / "lib"))   # third-party providers dropped under lib/, as cli.py does

from lib.adapters import get, AdapterUnavailable, AdapterError  # noqa: E402

WINDOW_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.I)
WINDOW_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def window_seconds(text):
    m = WINDOW_RE.match(str(text))
    if not m:
        raise ValueError("bad window %r (use e.g. 24h, 90m, 7d)" % (text,))
    return float(m.group(1)) * WINDOW_SECONDS[m.group(2).lower()]


def parse_iso(text):
    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    d = dt.datetime.fromisoformat(t)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_dict(obj):
    if obj is None:
        return None
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return obj
    return {"raw": str(obj)}


def rate_per_hour(signal, seconds):
    if signal is None:
        return None
    if signal.get("rate") is not None:
        return float(signal["rate"])
    return float(signal.get("count", 0) or 0) / (seconds / 3600.0)


def emit(obj, code):
    print(json.dumps(obj, indent=2, default=str))
    sys.exit(code)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signature", required=True, help="the ticket's error reference (carried from dev-intake)")
    ap.add_argument("--deployed-at", required=True, help="ISO-8601 deploy time; the before window ends here")
    ap.add_argument("--env", default="prod", help="an app.environments name (default prod)")
    ap.add_argument("--before-window", default="24h")
    ap.add_argument("--after-window", default="2h")
    ap.add_argument("--version", help="the deployed release, when the deploy adapter cannot tell")
    ap.add_argument("--previous-version", help="the release that served the before window, when known")
    ap.add_argument("--new-signature-query",
                    help="a provider query whose count > 0 means an error signature first appeared after deploy")
    a = ap.parse_args()

    try:
        deployed_at = parse_iso(a.deployed_at)
        before_s = window_seconds(a.before_window)
        after_s = window_seconds(a.after_window)
    except ValueError as e:
        emit({"ok": False, "error": "usage: %s" % e}, 2)

    base = {"signature": a.signature, "env": a.env, "deployed_at": iso(deployed_at),
            "windows": {"before": a.before_window, "after": a.after_window}}

    # 1. Which release is live? deploy adapter first, analytics second, the flag last. Optional.
    deployed_version = None
    version_source = None
    for role in ("deploy", "analytics"):
        try:
            v = get(role).deployed_version(a.env)
        except (AdapterUnavailable, AdapterError, Exception):  # optional: a missing deploy role is not a stop
            continue
        if v:
            deployed_version, version_source = str(v), role
            break
    if not deployed_version and a.version:
        deployed_version, version_source = a.version, "flag"
    base.update({"deployed_version": deployed_version, "deployed_version_source": version_source})

    # 2. The signal itself — REQUIRED. `none` -> exit 3 with the structured reason.
    try:
        analytics = get("analytics")
        before = to_dict(analytics.query_error_signal(
            a.signature, since=iso(deployed_at - dt.timedelta(seconds=before_s)), version=a.previous_version))
        after = to_dict(analytics.query_error_signal(
            a.signature, since=iso(deployed_at), version=deployed_version))
        new = None
        if a.new_signature_query:
            new = to_dict(analytics.query_error_signal(
                a.new_signature_query, since=iso(deployed_at), version=deployed_version))
    except AdapterUnavailable as e:
        err = e.to_dict() if hasattr(e, "to_dict") else {"error": "adapter-unavailable:analytics", "reason": str(e)}
        out = dict(base)
        out.update(err)
        out["ok"] = False
        out.setdefault("error", "adapter-unavailable:analytics")
        emit(out, 3)
    except AdapterError as e:
        emit(dict(base, ok=False, error="analytics query failed: %s" % e), 1)
    except Exception as e:  # a provider bug is still a structured failure, never a traceback
        emit(dict(base, ok=False, error="analytics query raised %s: %s" % (type(e).__name__, e)), 1)

    out = dict(base, ok=True, before=before, after=after, new=new,
               before_rate=rate_per_hour(before, before_s), after_rate=rate_per_hour(after, after_s),
               new_signature=(None if new is None else (new.get("count", 0) or 0) > 0),
               note="rates are events per hour over each window; the after window is scoped to deployed_version "
                    "when the provider supports a version filter")
    emit(out, 0)


if __name__ == "__main__":
    main()
