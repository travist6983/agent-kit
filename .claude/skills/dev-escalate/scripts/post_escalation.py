#!/usr/bin/env python3
"""Deliver a rendered escalation handoff — through the adapters, dry-run by default.

Reads <task-dir>/escalate.json (what render_escalation.py printed), always writes
<task-dir>/handoff.md (the handoff lands on disk even when nothing else is reachable), and —
ONLY with --authorize — sends it outward through `lib/adapters`:

  issue_tracker.add_comment(key, comment)        the tracker issue is the stored signal
  issue_tracker.assign(key, assignee)            when an assignee is configured
  issue_tracker.transition(key, status)          when transition_on_escalate is set
  notifications.post(channel, summary)           always, so a tracker-less stack still gets a warm handoff

Usage: post_escalation.py --task-dir DIR [--authorize] [--channel NAME] [--reason TEXT]

Writes escalate.json back with: dry_run, posted, delivered{issue_tracker, notifications},
tracker_results, warnings[], and (when nothing could be delivered) status=blocked + reason.

Exit codes:
  0 — dry-run, or authorized and the handoff reached at least one destination
  1 — authorized and an adapter operation failed (the handoff is still in handoff.md)
  2 — bad input: no escalate.json, empty comment, or (authorized) a ticket key that does not
      match project.ticket_key_pattern — `wrong-tracker-target`, never post to the wrong place
  3 — authorized and BOTH issue_tracker and notifications are unavailable
      (reason adapter-unavailable:<role>) — blocked, never a silent skip

The decision to escalate belongs to the recipe. Posting is an outward action on the user's
behalf: nothing leaves this machine without --authorize.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path


def _repo_root():
    env = os.environ.get("LOOPS_REPO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # <repo>/.claude/skills/dev-escalate/scripts/post_escalation.py → parents[4]
    return Path(__file__).resolve().parents[4]


sys.path.insert(0, str(_repo_root()))
try:
    from lib.adapters import AdapterError, AdapterUnavailable, cfg, get  # noqa: E402
except Exception as e:  # pragma: no cover - the adapter package is part of this repo
    print(f"post_escalation: ERROR: cannot import lib.adapters from {_repo_root()}: {e}", file=sys.stderr)
    sys.exit(2)


def die(msg, code=2):
    print(f"post_escalation: ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def summary_line(esc, delivered):
    key = esc.get("key") or "?"
    area = esc.get("area") or "-"
    reason = esc.get("reason") or "escalated"
    first = (esc.get("comment") or "").strip().splitlines()
    head = first[0].lstrip("# ").strip() if first else "handoff"
    who = esc.get("assignee") or "unassigned"
    where = "tracker comment" if delivered.get("issue_tracker") else "handoff.md only (tracker not reached)"
    return f"[{key}] {head} — area {area} · assignee {who} · {reason} · {where}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True)
    ap.add_argument("--authorize", action="store_true",
                    help="actually post/assign/transition/notify; without it nothing leaves this machine")
    ap.add_argument("--channel", help="override escalate.json notification_channel")
    ap.add_argument("--reason", default="", help="the stop reason, recorded in the summary line")
    a = ap.parse_args()

    d = Path(a.task_dir)
    esc_path = d / "escalate.json"
    if not esc_path.is_file():
        die(f"{esc_path} not found — run render_escalation.py first and save its output there")
    try:
        with open(esc_path, encoding="utf-8") as fh:
            esc = json.load(fh)
    except Exception as e:
        die(f"cannot parse {esc_path}: {e}")
    comment = (esc.get("comment") or "").strip()
    if not comment:
        die(f"{esc_path} has an empty comment — refusing to deliver an empty handoff")
    if a.reason:
        esc["reason"] = a.reason

    # The handoff always lands on disk, dry-run or not.
    handoff_md = d / "handoff.md"
    with open(handoff_md, "w", encoding="utf-8") as fh:
        fh.write(comment.rstrip() + "\n")
    esc["handoff_path"] = str(handoff_md)

    warnings = []  # recomputed on every run — a re-run must not accumulate stale warnings
    for stale in ("warnings", "would", "status", "errors", "tracker_results", "delivered"):
        esc.pop(stale, None)
    key = esc.get("key") or "?"
    pattern = cfg("project.ticket_key_pattern") or r"^[A-Z][A-Z0-9]+-[0-9]+$"
    key_ok = bool(re.match(pattern, str(key)))
    if not key_ok:
        warnings.append(f"wrong-tracker-target: key {key!r} does not match project.ticket_key_pattern {pattern!r}")
    if not esc.get("assignee"):
        warnings.append("no-assignee-configured: handoff is unassigned (set assignees.by_area / assignees.default)")

    channel = a.channel or esc.get("notification_channel") or "escalations"
    delivered = {"issue_tracker": False, "notifications": False}
    results = {}

    if not a.authorize:
        esc.update({"dry_run": True, "posted": False, "delivered": delivered, "warnings": warnings,
                    "would": {"issue_tracker": ["add_comment"] + (["assign"] if esc.get("assignee") else [])
                              + (["transition"] if esc.get("transition") else []),
                              "notifications": [f"post:{channel}"]}})
        write_json(esc_path, esc)
        print(json.dumps({k: esc[k] for k in ("key", "assignee", "transition", "dry_run", "posted", "would", "warnings")}, indent=2))
        return 0

    if not key_ok:
        write_json(esc_path, {**esc, "dry_run": False, "posted": False, "warnings": warnings,
                              "status": "failed", "reason": "wrong-tracker-target"})
        die(f"wrong-tracker-target: {key!r} does not match project.ticket_key_pattern {pattern!r}; not posting", code=2)

    unavailable = []
    failed = []

    # 1. The tracker issue is the stored signal.
    try:
        tracker = get("issue_tracker")
        results["add_comment"] = tracker.add_comment(key, comment)
        delivered["issue_tracker"] = True
        if esc.get("assignee"):
            try:
                tracker.assign(key, esc["assignee"])
                results["assign"] = {"user": esc["assignee"], "ok": True}
            except AdapterUnavailable as e:
                warnings.append(f"assign skipped: {e.to_dict()['error']}")
            except AdapterError as e:
                failed.append(f"assign: {e.message}")
        if esc.get("transition"):
            try:
                tracker.transition(key, esc["transition"])
                results["transition"] = {"status": esc["transition"], "ok": True}
            except AdapterUnavailable as e:
                warnings.append(f"transition skipped: {e.to_dict()['error']}")
            except AdapterError as e:
                failed.append(f"transition: {e.message}")
    except AdapterUnavailable as e:
        unavailable.append(e.to_dict()["error"])
    except AdapterError as e:
        failed.append(f"add_comment: {e.message}")

    # 2. Notify — the only destination a tracker-less stack has.
    try:
        results["notify"] = get("notifications").post(channel, summary_line(esc, delivered))
        delivered["notifications"] = True
    except AdapterUnavailable as e:
        unavailable.append(e.to_dict()["error"])
    except AdapterError as e:
        failed.append(f"notifications.post: {e.message}")

    posted = delivered["issue_tracker"] or delivered["notifications"]
    esc.update({"dry_run": False, "posted": posted, "delivered": delivered,
                "tracker_results": results, "warnings": warnings + unavailable})
    if not posted and unavailable and not failed:
        esc.update({"status": "blocked", "reason": unavailable[0].split(" ")[0]})
        write_json(esc_path, esc)
        print(json.dumps({"status": "blocked", "reason": esc["reason"], "handoff_path": str(handoff_md),
                          "unavailable": unavailable}, indent=2))
        return 3
    if failed:
        esc.update({"status": "failed" if not posted else "posted-partial", "errors": failed})
        write_json(esc_path, esc)
        print(json.dumps({"status": esc["status"], "errors": failed, "delivered": delivered,
                          "handoff_path": str(handoff_md)}, indent=2))
        return 1
    esc["status"] = "posted" if delivered["issue_tracker"] else "notified-only"
    write_json(esc_path, esc)
    print(json.dumps({"status": esc["status"], "key": key, "assignee": esc.get("assignee"),
                      "transition": esc.get("transition"), "delivered": delivered,
                      "warnings": esc["warnings"], "handoff_path": str(handoff_md)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
