#!/usr/bin/env python3
"""measure_ticks.py — propose pr_review_loop.prs_per_tick from measured tick durations.

Reads pr-review.log (the runner's key=value lines), keeps the event=end lines of ticks that
made a model call and finished (no watchdog kill, no rate limit, no refusal), and prints:

  samples           ticks counted
  p90_tick_s        90th-percentile tick duration_s
  p90_per_pr_s      90th-percentile of duration_s / PRs reviewed in that tick
  proposed          the largest n with n * p90_per_pr_s + overhead_s <= timeout_s (at least 1)
  enough            samples >= pr_review_loop.timing_min_samples

It WRITES NOTHING. A person copies the numbers into loop-limits.json by hand (prs_per_tick and
the pr_review_loop.timing block, with measured_by naming themselves). pr_review.sh refuses a
--live tick until that is done.

PRs reviewed per tick: `marked=` on the end line (PRs marked reviewed this tick). A tick with
marked=0 reviewed nothing and is skipped — its duration says nothing about a review.

Exit: 0 printed a proposal (check `enough`), 2 unreadable log/limits or zero usable ticks.
"""
import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
SKIP_OUTCOMES = ("watchdog-kill", "killed", "anthropic-rate-limited", "no-trace")


def parse(line):
    out = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def p90(xs):
    xs = sorted(xs)
    return xs[max(0, math.ceil(0.9 * len(xs)) - 1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--limits", default=os.path.join(REPO, ".claude/loops/loop-limits.json"))
    ap.add_argument("--timeout-s", type=int, default=2400, help="the scheduler job's --timeout")
    ap.add_argument("--overhead-s", type=int, default=600,
                    help="runner work outside the model call plus report headroom")
    a = ap.parse_args()
    try:
        min_samples = json.load(open(a.limits))["pr_review_loop"]["timing_min_samples"]
        if type(min_samples) is not int or min_samples < 1:
            raise ValueError("timing_min_samples must be an integer >= 1")
        lines = open(a.log, encoding="utf-8", errors="replace").read().splitlines()
    except Exception as e:  # noqa: BLE001 — any unreadable input fails closed
        print(json.dumps({"ok": False, "error": f"unreadable input: {e}"}))
        return 2

    ticks, per_pr = [], []
    for line in lines:
        f = parse(line)
        if f.get("event") != "end" or f.get("model_call") == "false":
            continue
        if f.get("outcome", "") in SKIP_OUTCOMES or f.get("timed_out") == "true":
            continue
        try:
            dur, marked = int(f["duration_s"]), int(f.get("marked", "0"))
        except (KeyError, ValueError):
            continue
        if marked < 1 or dur < 1:
            continue
        ticks.append(dur)
        per_pr.append(dur / marked)

    if not ticks:
        print(json.dumps({"ok": False, "error": "no usable ticks (need event=end lines with a model call and marked>=1)"}))
        return 2
    pp = p90(per_pr)
    proposed = max(1, int((a.timeout_s - a.overhead_s) // pp)) if pp > 0 else 1
    print(json.dumps({
        "ok": True, "samples": len(ticks), "min_samples": min_samples,
        "enough": len(ticks) >= min_samples,
        "p90_tick_s": p90(ticks), "p90_per_pr_s": round(pp, 1),
        "timeout_s": a.timeout_s, "overhead_s": a.overhead_s,
        "proposed_prs_per_tick": proposed,
        "note": "writes nothing; a person records prs_per_tick and pr_review_loop.timing by hand",
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
