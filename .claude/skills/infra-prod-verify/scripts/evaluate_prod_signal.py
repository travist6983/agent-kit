#!/usr/bin/env python3
"""Decide prod-confirmed / prod-regressed / inconclusive from the post-deploy signal.

The deterministic, testable core of infra-prod-verify (R2). Given the smoke result, the before/after
error rates from the analytics adapter, and whether a NEW error signature appeared, it returns the
verdict + whether to roll back (R9). The analytics query and the deploy/rollback are the adapters'
job (see scripts/prod_signal.py); this is the judgment. It has no vendor client and makes no
network call.

Usage (rates already computed):
  evaluate_prod_signal.py --smoke pass|fail|blocked|skipped-mutating \
      --before-rate F --after-rate F [--new-signature true|false] [--min-drop 0.5]

Usage (adapter output, as scripts/prod_signal.py prints it or as two ErrorSignal objects):
  evaluate_prod_signal.py --smoke pass --signal prod-signal.json
  evaluate_prod_signal.py --smoke pass --before-signal before.json --after-signal after.json \
      [--before-window 24h] [--after-window 2h] [--new-signal new.json]

The two explicit non-autonomous paths (§ settled decisions):
  --analytics-unavailable REASON   no analytics adapter / query failed -> inconclusive, never confirmed
  --human-confirmed-by NAME        a human confirmed in production -> prod-confirmed with provenance
                                   (still prod-regressed when the smoke is RED: a red oracle is evidence)

Decision (unchanged rules):
  smoke fail                                  -> prod-regressed
  smoke blocked / skipped-mutating            -> inconclusive (never roll back on a blocked oracle)
  new signature                               -> prod-regressed
  after_rate >= before_rate                   -> prod-regressed
  after_rate <= before_rate * (1 - min_drop)  -> prod-confirmed   (boundary inclusive)
  otherwise                                   -> inconclusive
rollback_recommended is true only on prod-regressed; escalate on prod-regressed and inconclusive.

Prints one JSON object. Exit 0 always when it could decide; 2 on bad usage.
"""
import argparse
import json
import re
import sys

WINDOW_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.I)
WINDOW_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_window_hours(text):
    """'24h' | '90m' | '7d' -> hours (float). Raises ValueError on anything else."""
    m = WINDOW_RE.match(str(text))
    if not m:
        raise ValueError("bad window %r (use e.g. 24h, 90m, 7d)" % (text,))
    return float(m.group(1)) * WINDOW_SECONDS[m.group(2).lower()] / 3600.0


def load_json_arg(value):
    """A path, '-' for stdin, or an inline JSON literal."""
    if value is None:
        return None
    if value == "-":
        return json.load(sys.stdin)
    text = value.strip()
    if text[:1] in "{[":
        return json.loads(text)
    with open(value, encoding="utf-8") as fh:
        return json.load(fh)


def unwrap_signal(obj):
    """Accept an ErrorSignal dict, or the adapter CLI envelope {ok, result}."""
    if obj is None:
        return None
    if isinstance(obj, dict) and "result" in obj and "ok" in obj:
        if not obj.get("ok"):
            return None
        return obj["result"]
    return obj


def signal_rate(signal, window_hours):
    """Rate per hour for one window. A provider-supplied `rate` wins; else count / window."""
    if signal is None:
        return None
    if isinstance(signal, dict) and signal.get("rate") is not None:
        return float(signal["rate"])
    count = (signal or {}).get("count", 0) or 0
    if window_hours <= 0:
        raise ValueError("window must be > 0 hours")
    return float(count) / window_hours


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--smoke", choices=["pass", "fail", "blocked", "skipped-mutating"], required=True)
    ap.add_argument("--before-rate", type=float)
    ap.add_argument("--after-rate", type=float)
    ap.add_argument("--new-signature", choices=["true", "false"], default=None)
    ap.add_argument("--min-drop", type=float, default=0.5,
                    help="fraction the rate must drop for prod-confirmed (default 0.5)")
    # adapter output
    ap.add_argument("--signal", help="prod-signal.json from scripts/prod_signal.py (path, '-' or inline JSON)")
    ap.add_argument("--before-signal", help="ErrorSignal JSON for the before window")
    ap.add_argument("--after-signal", help="ErrorSignal JSON for the after window")
    ap.add_argument("--new-signal", help="ErrorSignal JSON for the new-signature probe (count > 0 = new signature)")
    ap.add_argument("--before-window", default="24h")
    ap.add_argument("--after-window", default="2h")
    # explicit non-autonomous paths
    ap.add_argument("--analytics-unavailable", metavar="REASON",
                    help="no analytics adapter (adapter-unavailable:analytics) or the query failed")
    ap.add_argument("--human-confirmed-by", metavar="NAME",
                    help="a named human confirmed the fix in production; recorded as provenance")
    # passthrough evidence
    ap.add_argument("--deployed-version")
    ap.add_argument("--env", default="prod")
    ap.add_argument("--ticket")
    a = ap.parse_args()

    before, after = a.before_rate, a.after_rate
    new_sig = None if a.new_signature is None else a.new_signature == "true"
    windows = {"before": a.before_window, "after": a.after_window}
    deployed_version = a.deployed_version
    analytics_unavailable = a.analytics_unavailable
    source = "flags"

    try:
        bundle = load_json_arg(a.signal) if a.signal else None
        if bundle is not None:
            source = "prod_signal"
            if bundle.get("ok") is False:
                analytics_unavailable = analytics_unavailable or bundle.get("error") or "analytics query failed"
            windows = bundle.get("windows") or windows
            deployed_version = deployed_version or bundle.get("deployed_version")
            b_sig = unwrap_signal(bundle.get("before"))
            a_sig = unwrap_signal(bundle.get("after"))
            n_sig = unwrap_signal(bundle.get("new"))
            if bundle.get("before_rate") is not None and before is None:
                before = float(bundle["before_rate"])
            if bundle.get("after_rate") is not None and after is None:
                after = float(bundle["after_rate"])
            if bundle.get("new_signature") is not None and new_sig is None:
                new_sig = bool(bundle["new_signature"])
        else:
            b_sig = unwrap_signal(load_json_arg(a.before_signal)) if a.before_signal else None
            a_sig = unwrap_signal(load_json_arg(a.after_signal)) if a.after_signal else None
            n_sig = unwrap_signal(load_json_arg(a.new_signal)) if a.new_signal else None
            if b_sig is not None or a_sig is not None:
                source = "signals"
        if before is None and b_sig is not None:
            before = signal_rate(b_sig, parse_window_hours(windows["before"]))
        if after is None and a_sig is not None:
            after = signal_rate(a_sig, parse_window_hours(windows["after"]))
        if new_sig is None and n_sig is not None:
            new_sig = (n_sig.get("count", 0) or 0) > 0
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(json.dumps({"ok": False, "error": "bad signal input: %s" % e}), file=sys.stderr)
        sys.exit(2)

    if new_sig is None:
        new_sig = False
    min_drop = a.min_drop
    human = a.human_confirmed_by
    provenance = {"mode": "human", "confirmed_by": human} if human else {"mode": "autonomous", "confirmed_by": None}

    # --- the decision ----------------------------------------------------------------------
    if a.smoke == "fail":
        verdict, reason = "prod-regressed", "post-deploy smoke failed (the oracle went RED on the deployed build)"
    elif human:
        verdict, reason = "prod-confirmed", "confirmed in production by %s (human provenance; smoke=%s)" % (human, a.smoke)
    elif a.smoke in ("blocked", "skipped-mutating"):
        verdict, reason = "inconclusive", "post-deploy smoke %s — neither pass nor fail; never roll back on a blocked oracle" % a.smoke
    elif analytics_unavailable:
        verdict, reason = "inconclusive", "no error-signal evidence: %s — autonomous prod-confirmed requires an analytics adapter" % analytics_unavailable
    elif before is None or after is None:
        verdict, reason = "inconclusive", "missing before/after error rate — autonomous prod-confirmed requires both windows"
    elif new_sig:
        verdict, reason = "prod-regressed", "a new error signature appeared after deploy"
    elif before == 0 and after == 0:
        verdict, reason = "inconclusive", "no baseline signal (before=0, after=0) — the error reference never fired in either window"
    elif after >= before:
        verdict, reason = "prod-regressed", "error rate did not drop (before=%s, after=%s)" % (before, after)
    elif after <= before * (1 - min_drop):
        verdict, reason = "prod-confirmed", "error rate dropped >= %d%% (before=%s, after=%s)" % (int(min_drop * 100), before, after)
    else:
        verdict, reason = "inconclusive", "error rate dropped but < %d%% (before=%s, after=%s)" % (int(min_drop * 100), before, after)

    drop_pct = None
    if before is not None and after is not None and before > 0:
        drop_pct = round((1 - after / before) * 100, 1)

    out = {
        "verdict": verdict,
        "reason": reason,
        "rollback_recommended": verdict == "prod-regressed",
        "escalate": verdict in ("prod-regressed", "inconclusive"),
        "provenance": provenance,
        "evidence": {
            "smoke": a.smoke,
            "before_rate": before,
            "after_rate": after,
            "drop_pct": drop_pct,
            "new_signature": new_sig,
            "min_drop": min_drop,
            "windows": windows,
            "env": a.env,
            "deployed_version": deployed_version,
            "analytics_unavailable": analytics_unavailable,
            "source": source,
        },
    }
    if a.ticket:
        out["ticket"] = a.ticket
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
