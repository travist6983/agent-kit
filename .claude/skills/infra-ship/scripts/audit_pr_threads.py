#!/usr/bin/env python3
"""Read-back audit for the PR thread bar: did anything reach the PR outside select_pr_threads.py?

The funnel decides what MAY be posted; this proves what WAS. It compares the PR's threads before and
after the ship step's posting. A new thread the funnel did not post — an improvised caveat, a review
note, anything — is a `noise-thread`, and a PR holding more loop threads than the cap is `over-cap`.
The calibration case that motivated this had three caveat threads of exactly the first kind: no
skill prescribed them and nothing noticed. The result goes into ship.json `thread_audit` and the
`pr_threads_*` metrics.

Usage:
  audit_pr_threads.py --before <threads-before.json> --after <threads-after.json>
                      [--posted-ids t1,t2] [--runner <identity>] [--limits <loop-limits.json>]

  --before / --after  the vcs_host adapter's `list_review_threads` output (a JSON array of
                      {thread_id|id, body, path, line, status, author?, is_system?, deleted?}, or the
                      CLI envelope {ok, result:[...]}), taken before the first post and after the last.
  --posted-ids        the thread ids the funnel's posts returned (comma-separated, compared as strings;
                      empty when nothing posted).
  --runner            the identity the loop posts as — whatever `author` your vcs_host provider records
                      for the loop's own threads. With it, a colleague commenting mid-ship is never
                      mistaken for the loop.

Prints {ok, new_threads, via_funnel, outside_funnel:[{id, author, first_line}], loop_threads_on_pr,
        cap, noise_thread, over_cap_on_pr}.
Exit codes: 0 = clean · 3 = a violation (noise_thread or over_cap_on_pr) — record it, don't hide it ·
            2 = bad input.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from select_pr_threads import (DEFAULT_LIMITS, LOOP_HEADING, fail, is_system, resolve_cap,  # noqa: E402
                               thread_body, thread_id, threads_from_file)


def first_line(thread):
    body = thread_body(thread).strip()
    return body.splitlines()[0][:160] if body else ""


def author_of(thread):
    author = thread.get("author")
    if isinstance(author, dict):   # a provider that returns {name, email, id}: any of them identifies
        return str(author.get("email") or author.get("name") or author.get("id") or "")
    return "" if author is None else str(author)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--posted-ids", default="")
    ap.add_argument("--runner", help="the identity the loop posts as; only its new threads can be noise")
    ap.add_argument("--limits", default=DEFAULT_LIMITS)
    args = ap.parse_args()

    posted = {x.strip() for x in args.posted_ids.split(",") if x.strip()}
    cap, _source = resolve_cap(None, args.limits)
    before_ids = {thread_id(t) for t in threads_from_file(args.before, "--before")}
    after = threads_from_file(args.after, "--after")

    new = [t for t in after if thread_id(t) not in before_ids and not is_system(t) and not t.get("deleted")]
    via_funnel = [t for t in new if thread_id(t) in posted]
    outside = []
    for t in new:
        if thread_id(t) in posted:
            continue
        who = author_of(t)
        if args.runner and who.lower() != args.runner.lower():
            continue   # a colleague commented while the loop was shipping — not the loop's noise
        outside.append({"id": thread_id(t), "author": who, "first_line": first_line(t)})

    loop_on_pr = sum(1 for t in after if not t.get("deleted") and not is_system(t)
                     and LOOP_HEADING.match(first_line(t)))
    result = {
        "ok": True,
        "new_threads": len(new),
        "via_funnel": len(via_funnel),
        "outside_funnel": outside,
        "loop_threads_on_pr": loop_on_pr,
        "cap": cap,
        "noise_thread": bool(outside),
        "over_cap_on_pr": loop_on_pr > cap,
    }
    print(json.dumps(result, indent=2))
    sys.exit(3 if (result["noise_thread"] or result["over_cap_on_pr"]) else 0)


if __name__ == "__main__":
    main()
