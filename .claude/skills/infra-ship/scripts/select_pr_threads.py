#!/usr/bin/env python3
"""Select which PR comment threads the loop may post — the MECHANICAL half of the PR thread bar
(../references/pr-thread-bar.md).

A PR thread is an interrupt, and the expected count is zero. This script is the one funnel every
thread goes through. It reads fix.json `design_decisions` (plus an optional candidates file for
must-read items about THIS diff that did not come from dev-fix), keeps only entries marked
`must_read: true` with a recognised `must_read_reason` and a `reviewer_must_confirm` ask, ranks
them, caps them at loop-limits.json `defect_pr_threads.max_per_pr`, and renders ready-to-POST
thread bodies.

The cap is PER PR, not per run: with --existing (the PR's current threads), every loop-authored
thread already on the PR uses up a slot, and a selected item already on the PR — same heading, or
same reason on the same file after a reworded re-fix — is never posted again. Items past the cap
are NOT dropped: they become bullets in a "Before merging" section at the top of the description.
Everything that is not must-read stays in fix.json (dev-escalate's handoff still reads it) and never
reaches the PR.

It also assembles the description's "## Noticed, not changed" block — pre-existing issues seen along
the way, from fix.json `noticed_not_changed` plus the "## Noticed, not changed" bullets of
dev-code-review's report — deduped and capped at 3 lines. The rest go to `noticed_overflow` for the run
report. A pre-existing issue never becomes a thread.

Usage:
  select_pr_threads.py --fix <fix.json> [--candidates <extra.json>] [--existing <threads.json>]
                       [--review-report <review.md>] [--limits <loop-limits.json>] [--max N]
                       [--out-dir DIR]

Prints one JSON object:
  {ok, cap, cap_source, counts:{candidates, must_read, threads, already_posted, existing_loop_threads,
   before_merging, over_cap, not_posted}, threads:[{rank, reason, title, source, index, file,
   anchored, body_chars}], already_posted:[{rank, title}], before_merging:[{reason, title, ask}],
   not_posted:[{source, index, title, why}], description_top, noticed_block, noticed_overflow, warnings}
With --out-dir, the directory's old thread-*.json files are removed and one thread-<rank>.json is
written per thread in `threads` — a vendor-neutral thread spec `{path, line, body}` that is exactly
the kwargs of the vcs_host adapter's `post_review_comment(id, path, line, body)`. Post exactly those
(`python3 lib/adapters/cli.py vcs_host post_review_comment id=<pr> --json "$(cat thread-<rank>.json)"`).
A thread opens unresolved; the adapter renders whatever its host needs.

--existing takes the vcs_host adapter's `list_review_threads` output: a JSON array of
`{thread_id|id, body, path, line, status, author?, is_system?, deleted?}` (the CLI envelope
`{"ok": true, "result": [...]}` is accepted as is). No vendor payload is ever read here.

--max may only LOWER the cap (tests; the body-fallback's --max 0). Raising it is a change to the
tracked loop-limits.json, never a flag a loop passes to itself.

Exit codes (always with one JSON object on stdout):
            0 = selected (zero threads is the normal result)
            2 = bad input: an unreadable or malformed file, a bad argument, --max above the cap
            4 = unclassified must-read: an entry marked must_read:true without a recognised
                must_read_reason or a reviewer_must_confirm ask (or with a non-boolean must_read).
                That is dev-fix's artifact to fix, not the ship step's — stop, post nothing, and
                report ship.json abort "unclassified-must-read". Never post around this script.
"""
import argparse
import glob
import json
import os
import re
import sys

# Priority order: when the cap binds, the lower number is posted first.
REASONS = {
    "ticket-not-fully-fixed": (0, "Not fully fixed"),
    "data-or-security-risk": (1, "Data or security risk"),
    "breaking-shared-contract": (2, "Breaking change for other consumers"),
    "human-action-required": (3, "Needs a manual step"),
    "behavior-change-beyond-ticket": (4, "Behavior change beyond the ticket"),
    "product-call": (5, "Your call"),
}
LABEL_TO_REASON = {label: reason for reason, (_prio, label) in REASONS.items()}
# The first line of every thread this script renders. It is how a loop thread is recognised on the
# PR (a human's thread or a system thread never matches, so it is never counted or deduped against).
LOOP_HEADING = re.compile(r"^\*\*⚠️ (" + "|".join(re.escape(l) for l in LABEL_TO_REASON) + r") — (.+)\*\*$")
ACTIVE = {None, "", "active", "open", "pending", "unresolved"}   # normalized statuses that still ask for attention
DEFAULT_MAX = 2
NOTICED_MAX = 3   # "Noticed, not changed" lines on the PR; the rest belong in the run report
LONG_BODY_CHARS = 900  # a must-read thread says what to act on; past this it has become an essay
# A worktree/filesystem path is not a repo anchor: anchors are repo-relative paths with no leading
# slash; the vcs_host provider adds whatever its host wants (a leading "/", a side, an offset).
FS_ABSOLUTE = re.compile(r"^(/(Users|home|private|tmp|var|opt|Volumes|mnt)/|[A-Za-z]:[\\/])")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LIMITS = os.path.normpath(os.path.join(HERE, "..", "..", "..", "loops", "loop-limits.json"))


def fail(msg, code=2):
    print(json.dumps({"ok": False, "error": msg, "exit": code}))
    sys.exit(code)


def unclassified(msg):
    fail(msg + " (exit 4: unclassified-must-read — dev-fix's artifact to fix; post nothing)", code=4)


def load_json(path, what):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        fail(f"cannot read {what} {path}: {exc}")


def norm(text):
    """One line, single-spaced: headings must match themselves on a re-run, and a newline inside a
    title would otherwise break both the bold markup and the --existing dedupe."""
    return " ".join(str(text).split())


def resolve_cap(cli_max, limits_path):
    if not os.path.exists(limits_path):
        cap, source = DEFAULT_MAX, f"default {DEFAULT_MAX} (no {limits_path})"
    else:
        limits = load_json(limits_path, "limits file")
        block = limits.get("defect_pr_threads") if isinstance(limits, dict) else None
        value = block.get("max_per_pr") if isinstance(block, dict) else None
        if value is None:
            cap, source = DEFAULT_MAX, f"default {DEFAULT_MAX} (defect_pr_threads.max_per_pr absent)"
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            cap, source = DEFAULT_MAX, f"default {DEFAULT_MAX} (defect_pr_threads.max_per_pr={value!r} is not an int >= 0)"
        else:
            cap, source = value, "loop-limits.json defect_pr_threads.max_per_pr"
    if cli_max is not None:
        if cli_max < 0:
            fail("--max must be >= 0")
        if cli_max > cap:
            fail(f"--max {cli_max} is above the cap {cap} ({source}). --max may only lower it; raising the "
                 f"cap is a change to the tracked loop-limits.json, never a flag a loop passes itself.")
        cap, source = cli_max, f"--max (lowered from {cap})"
    return cap, source


def as_list(data, what):
    if data is None:
        return []
    if isinstance(data, dict) and "candidates" in data:
        data = data["candidates"]
    if not isinstance(data, list):
        fail(f"{what} must be a JSON array")
    return data


def parse_anchor(files, warnings, title):
    """fix.json carries files:["path:line"]. Returns (repo_path|None, line|None) for the first entry;
    a filesystem path, or a line that isn't a positive integer, leaves the thread PR-level."""
    if not isinstance(files, list) or not files or not isinstance(files[0], str) or not files[0].strip():
        return None, None
    ref = files[0].strip()
    m = re.fullmatch(r"(.+?):(\d+)(?::\d+)?", ref, re.ASCII)   # path:line and path:line:col
    path, line = (m.group(1), int(m.group(2))) if m else (ref, None)
    if FS_ABSOLUTE.match(path) or "\\" in path:
        warnings.append(f"'{title}': {ref!r} is a filesystem path, not a repo path — posted PR-level")
        return None, None
    if path.startswith("./"):
        path = path[2:]
    return path.lstrip("/"), (line if line and line > 0 else None)


def threads_from_file(path, what):
    """The vcs_host adapter's `list_review_threads` output: a JSON array of normalized threads, or the
    adapter CLI's envelope {"ok": true, "result": [...]} around it. Never a vendor payload."""
    data = load_json(path, what)
    if isinstance(data, dict) and isinstance(data.get("result"), list):
        data = data["result"]
    if not isinstance(data, list):
        fail(f"{what} must be the vcs_host list_review_threads output (a JSON array, or the CLI envelope "
             f"{{ok, result:[...]}})")
    return [t for t in data if isinstance(t, dict)]


def thread_id(thread):
    tid = thread.get("thread_id", thread.get("id"))
    return None if tid is None else str(tid)


def thread_body(thread):
    body = thread.get("body")
    return body if isinstance(body, str) else ""


def is_system(thread):
    return bool(thread.get("is_system")) or not isinstance(thread.get("body"), str)


def existing_loop_threads(path):
    """The loop-authored threads already on the PR, from the adapter's list_review_threads output."""
    found = []
    for t in threads_from_file(path, "existing threads file"):
        if t.get("deleted") or is_system(t):
            continue
        content = thread_body(t)
        if not content.strip():
            continue
        m = LOOP_HEADING.match(content.strip().splitlines()[0].strip())
        if not m:
            continue
        file_ = t.get("path") or None
        if isinstance(file_, str):
            file_ = file_.lstrip("/") or None
        found.append({"heading": m.group(0), "reason": LABEL_TO_REASON[m.group(1)],
                      "file": file_, "status": t.get("status")})
    return found


def noticed_items(fix, review_report):
    """fix.json noticed_not_changed + the bullets under dev-code-review's '## Noticed, not changed'."""
    items = [norm(x) for x in (fix.get("noticed_not_changed") or []) if isinstance(x, str) and x.strip()]
    if review_report:
        try:
            with open(review_report) as fh:
                lines = fh.read().splitlines()
        except OSError as exc:
            fail(f"cannot read review report {review_report}: {exc}")
        inside = False
        for line in lines:
            if re.match(r"^#{1,6}\s", line):
                inside = bool(re.match(r"^#{1,6}\s+Noticed, not changed\s*$", line.strip(), re.I))
                continue
            if inside and re.match(r"^\s*[-*]\s+\S", line):
                items.append(norm(re.sub(r"^\s*[-*]\s+", "", line)))
    unique, seen = [], set()
    for item in items:
        if item.lower() not in seen:
            seen.add(item.lower())
            unique.append(item)
    return unique


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fix", required=True, help="the loop's fix.json")
    ap.add_argument("--candidates", help="optional JSON array of extra must-read candidates about THIS diff")
    ap.add_argument("--existing", help="the PR's current threads (vcs_host list_review_threads output); required on a re-ship")
    ap.add_argument("--review-report", help="dev-code-review's markdown report (its '## Noticed, not changed' bullets)")
    ap.add_argument("--limits", default=DEFAULT_LIMITS, help="loop-limits.json (default: the tracked one)")
    ap.add_argument("--max", type=int, help="LOWER the cap (tests, and the body-fallback's --max 0) — never raise it")
    ap.add_argument("--out-dir", help="write thread-<rank>.json POST bodies here (stale ones are removed)")
    args = ap.parse_args()

    cap, cap_source = resolve_cap(args.max, args.limits)
    existing = existing_loop_threads(args.existing) if args.existing else []
    fix = load_json(args.fix, "fix.json")
    if not isinstance(fix, dict):
        fail("fix.json must be a JSON object")
    pool = [("fix.json", i, e) for i, e in enumerate(as_list(fix.get("design_decisions"), "fix.json design_decisions"))]
    if args.candidates:
        extra = as_list(load_json(args.candidates, "candidates file"), "candidates file")
        pool += [("candidates", i, e) for i, e in enumerate(extra)]

    must_read, not_posted, warnings, seen = [], [], [], {}
    for source, index, entry in pool:
        where = f"{source} entry {index}"
        if isinstance(entry, str):
            not_posted.append({"source": source, "index": index, "title": norm(entry)[:80],
                               "why": "string-form entry: cannot carry must_read, stays in fix.json"})
            continue
        if not isinstance(entry, dict):
            fail(f"{where} is neither an object nor a string")
        flag = entry.get("must_read", False)
        if not isinstance(flag, bool):
            unclassified(f"{where}: must_read must be true or false, got {flag!r}")
        raw_title = entry.get("title") or entry.get("decision") or "(untitled)"
        if not flag:
            not_posted.append({"source": source, "index": index, "title": norm(raw_title)[:120],
                               "why": "not must-read: stays in fix.json for the record / handoff"})
            continue
        for key in ("title", "decision"):
            if entry.get(key) is not None and not isinstance(entry.get(key), str):
                unclassified(f"{where}: {key} must be a string, got {type(entry.get(key)).__name__}")
        title = norm(raw_title)[:120]
        reason = entry.get("must_read_reason")
        if not isinstance(reason, str) or reason not in REASONS:
            unclassified(f"{where} ('{title}') is must_read:true but must_read_reason={reason!r} is not one of "
                 f"{sorted(REASONS)}. If none fits, it is not a thread: set must_read:false.")
        ask = entry.get("reviewer_must_confirm")
        if not isinstance(ask, str) or not ask.strip():
            unclassified(f"{where} ('{title}') is must_read:true but has no reviewer_must_confirm — a thread must "
                 f"say what the reviewer has to do or decide before merging. No ask means it is not a thread.")
        key = (reason, title.lower())
        if key in seen:
            not_posted.append({"source": source, "index": index, "title": title,
                               "why": f"duplicate of {seen[key]}"})
            continue
        seen[key] = where
        must_read.append((REASONS[reason][0], len(must_read), source, index,
                          dict(entry, title=title, reviewer_must_confirm=norm(ask))))

    must_read.sort(key=lambda t: (t[0], t[1]))
    on_pr_headings = {e["heading"] for e in existing}
    on_pr_reason_file = {(e["reason"], e["file"]) for e in existing if e["file"]}
    slots = max(0, cap - len(existing))   # the cap is per PR: every loop thread already there uses one
    threads, already_posted, before_merging = [], [], []
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        for stale in glob.glob(os.path.join(args.out_dir, "thread-*.json")):
            os.remove(stale)   # a leftover body from an earlier run must never be posted again
    for rank, (_prio, _order, source, index, entry) in enumerate(must_read, start=1):
        reason = entry["must_read_reason"]
        label = REASONS[reason][1]
        heading = f"**⚠️ {label} — {entry['title']}**"
        path, line = parse_anchor(entry.get("files"), warnings, entry["title"])
        if heading in on_pr_headings or (path and (reason, path) in on_pr_reason_file):
            already_posted.append({"rank": rank, "title": entry["title"]})
            continue
        if slots == 0:
            before_merging.append({"reason": reason, "title": entry["title"], "ask": entry["reviewer_must_confirm"]})
            continue
        slots -= 1
        parts = [heading]
        decision = norm(entry.get("decision") or "")
        if decision:
            parts.append(decision)
        parts.append(f"**Before merging:** {entry['reviewer_must_confirm']}")
        body = "\n\n".join(parts)
        # The spec is the adapter call's kwargs — nothing vendor-shaped. An anchor is path + line;
        # a file with no line (or no file) leaves the thread PR-level.
        anchored = bool(path and line)
        post = {"path": path if anchored else None, "line": line if anchored else None, "body": body}
        if len(body) > LONG_BODY_CHARS:
            warnings.append(f"thread {rank} ('{entry['title']}') is {len(body)} chars: cut it to what the "
                            f"reviewer must act on")
        if args.out_dir:
            with open(os.path.join(args.out_dir, f"thread-{rank}.json"), "w") as fh:
                json.dump(post, fh, indent=2)
        threads.append({"rank": rank, "reason": reason, "title": entry["title"], "source": source,
                        "index": index, "file": path, "anchored": anchored,
                        "body_chars": len(body)})

    over_cap = max(0, len(must_read) - cap)
    if over_cap:
        warnings.append(f"{len(must_read)} must-read items on one PR against a cap of {cap}: merge related items "
                        f"into one, and say in the run report whether this change should have been staged for "
                        f"a human instead of shipped")

    # The banner counts what needs the reviewer: active loop threads (new or already there) plus any
    # Before-merging bullets — and says where each lives, so "2 items" never sits above 2 other bullets.
    n_threads = len(threads) + sum(1 for e in existing if e["status"] in ACTIVE)
    n_bullets = len(before_merging)
    total = n_threads + n_bullets
    top = []
    if total:
        items = f"{total} item{'s' if total > 1 else ''} need{'' if total > 1 else 's'} your eyes before merge"
        if n_threads and n_bullets:
            where = f"{n_threads} as active thread{'s' if n_threads > 1 else ''} on this PR, {n_bullets} listed below"
        elif n_threads:
            where = f"see the active thread{'s' if n_threads > 1 else ''} on this PR"
        else:
            where = "listed below"
        top.append(f"> ⚠️ **{items}** — {where}.")
    if before_merging:
        top.append("## ⚠️ Before merging\n" + "\n".join(f"- **{b['title']}** — {b['ask']}" for b in before_merging))

    noticed = noticed_items(fix, args.review_report)
    noticed_block = ("## Noticed, not changed\n" + "\n".join(f"- {n}" for n in noticed[:NOTICED_MAX])) if noticed else ""

    print(json.dumps({
        "ok": True,
        "cap": cap,
        "cap_source": cap_source,
        "counts": {"candidates": len(pool), "must_read": len(must_read), "threads": len(threads),
                   "already_posted": len(already_posted), "existing_loop_threads": len(existing),
                   "before_merging": len(before_merging), "over_cap": over_cap, "not_posted": len(not_posted)},
        "threads": threads,
        "already_posted": already_posted,
        "before_merging": before_merging,
        "not_posted": not_posted,
        "description_top": "\n\n".join(top),
        "noticed_block": noticed_block,
        "noticed_overflow": noticed[NOTICED_MAX:],
        "warnings": warnings,
    }, indent=2))


if __name__ == "__main__":
    main()
