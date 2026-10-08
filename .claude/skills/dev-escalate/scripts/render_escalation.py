#!/usr/bin/env python3
"""Render an escalation handoff comment from a loop's artifacts — tracker-neutral, dry-run by default.

Assembles the FULL loop memory (intake/repro/fix/verify/regression) into the configurable
comment template so a human resumes warm, not cold. It does NOT post anywhere: posting is
`post_escalation.py` (through the issue_tracker + notifications adapters) and stays gated as an
outward action. This renderer is the deterministic, testable core.

Usage: render_escalation.py --task-dir DIR [--config CONFIG] [--reason REASON]
Prints JSON: {key, area, assignee, assignee_configured, transition, notification_channel,
              dry_run, posted, comment}.

Fails LOUDLY — never a silent empty handoff:
  exit 2 — config missing/unparsable, empty comment_template, corrupt artifact JSON,
           or a template placeholder error (escape literal braces as {{ }})
  exit 3 — no loop artifacts found in --task-dir (refusing to emit an empty handoff)
A MISSING artifact file is fine (loops stop early); a PRESENT-but-unparsable one is fatal.

Reads the ratified artifact shapes (contracts/qa-repro.json: status/surface/symptom_quote/
app_map_writes/replay_spec + optional media[{kind,url,label}]) and tolerates the legacy
pre-contract shapes (before_media string, media/video2 dicts). Also scans <task-dir>/media/
so on-disk before/after recordings always surface.

Config (assets/escalate.config.json, skill-local, no tenant values):
  assignees.default / assignees.by_area[area]   opaque tracker user ids or handles, or null
  transition_on_escalate                        tracker status name, or null to leave it
  notification_channel                          channel for the notifications adapter
  comment_template                              the handoff body, {placeholders} filled here
"""
import argparse
import json
import os
import re
import sys

MEDIA_EXTS = (".gif", ".mp4", ".webm", ".png", ".jpg", ".jpeg")


def die(msg, code=2):
    print(f"render_escalation: ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def load(path, required=False):
    """Missing file -> None (absent artifacts are expected). Unreadable/corrupt -> fatal."""
    if not os.path.exists(path):
        if required:
            die(f"required file missing: {path}")
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception as e:
        die(f"cannot parse {path}: {e}")


def collect_media(task_dir, repro, verify):
    """Every recording the loop produced: the contract's media[] arrays, legacy keys, and a
    scan of <task-dir>/media/."""
    media, seen = [], set()

    def add(item):
        if item and item not in seen:
            seen.add(item)
            media.append(item)

    for art in (repro, verify):
        m = art.get("media")
        if isinstance(m, list):  # ratified shape: [{kind, url, label?}]
            for entry in m:
                if isinstance(entry, dict) and entry.get("url"):
                    label = entry.get("label") or entry.get("kind") or "media"
                    add(f"{label}: {entry['url']}")
        elif isinstance(m, dict):  # legacy pre-contract {gif, mp4}
            for k in ("gif", "mp4"):
                if m.get(k):
                    add(m[k])

    for art, key in ((repro, "before_media"), (verify, "after_media")):
        v = art.get(key)
        if isinstance(v, str) and v:
            add(v)

    v2 = verify.get("video2")
    if isinstance(v2, dict):
        for k in ("gif", "mp4"):
            if v2.get(k):
                add(v2[k])
    elif isinstance(v2, str) and v2:
        add(v2)

    mdir = os.path.join(task_dir, "media")
    if os.path.isdir(mdir):
        for fn in sorted(os.listdir(mdir)):
            if fn.lower().endswith(MEDIA_EXTS):
                add(os.path.join("media", fn))

    return media


def render_design_decisions(fix):
    """fix.json design_decisions entries are dicts per dev-fix's contract, but real runs
    also emit plain strings — render both.

    Entries marked must_read (the PR thread bar) need the human's decision and render in full
    with a "please confirm". Everything else — strings, older dict shapes, and the
    plan-synthesis runner-up — is context for whoever takes over and renders as one line: a
    "please confirm" on every call trains the reader to skip the one that matters."""
    act, context = [], []
    for dd in fix.get("design_decisions") or []:
        if isinstance(dd, str):
            context.append(f"- {dd}")
            continue
        if not isinstance(dd, dict):
            context.append(f"- {dd!r}")
            continue
        alts = dd.get("alternatives_considered") or []
        if isinstance(alts, str):
            alts = [alts]
        title = dd.get("title", "(untitled)")
        if dd.get("must_read") is True:
            reason = dd.get("must_read_reason") or dd.get("kind") or "must-read"
            act.append(
                f"- **{title}** [{reason}]\n"
                f"  - What the loop did: {dd.get('decision', '')}\n"
                + (f"  - Alternative(s) considered: {'; '.join(alts)}\n" if alts else "")
                + f"  - 👉 Please confirm: {dd.get('reviewer_must_confirm', '')}"
            )
            continue
        line = f"- {title}"
        if dd.get("decision"):
            line += f" — {dd['decision']}"
        if alts:
            line += f" (also considered: {'; '.join(alts)})"
        context.append(line)
    for alt in (fix.get("plan_synthesis") or {}).get("alternatives") or []:
        if isinstance(alt, dict) and alt.get("strategy"):
            why_not = f" — not chosen: {alt['why_not']}" if alt.get("why_not") else ""
            context.append(f"- Runner-up strategy (re-fix candidate): {alt['strategy']}{why_not}")
    if not act and not context:
        return "(none flagged)"
    parts = []
    if act:
        parts.append("👉 **Needs your decision**\n" + "\n".join(act))
    if context:
        parts.append("Other calls the loop made — context, no action needed:\n" + "\n".join(context))
    return "\n\n".join(parts)


def render_noticed(fix, task_dir):
    """Pre-existing issues the loop saw but did not touch: fix.json noticed_not_changed + the
    '## Noticed, not changed' bullets of dev-code-review's review.md. They never become
    PR threads, and feature-prerelease's STAGE path never runs infra-ship — so on that path this block
    is the only place a human meets them. Uncapped here: a handoff is read once, in full."""
    items = [" ".join(x.split()) for x in (fix.get("noticed_not_changed") or []) if isinstance(x, str) and x.strip()]
    review_md = os.path.join(task_dir, "review.md")
    if os.path.isfile(review_md):
        inside = False
        with open(review_md) as fh:
            for line in fh:
                if re.match(r"^#{1,6}\s", line):
                    inside = bool(re.match(r"^#{1,6}\s+Noticed, not changed\s*$", line.strip(), re.I))
                    continue
                if inside and re.match(r"^\s*[-*]\s+\S", line):
                    items.append(" ".join(re.sub(r"^\s*[-*]\s+", "", line).split()))
    seen, out = set(), []
    for item in items:
        if item.lower() not in seen:
            seen.add(item.lower())
            out.append(f"- {item}")
    return "\n".join(out) or "(none)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True)
    ap.add_argument("--config")
    ap.add_argument("--reason", default="")
    a = ap.parse_args()

    d = a.task_dir
    if not os.path.isdir(d):
        die(f"--task-dir does not exist: {d}")

    arts = {
        name: load(os.path.join(d, f"{name}.json"))
        for name in ("intake", "brief", "repro", "fix", "verify", "judge", "regression")
    }
    if all(v is None for v in arts.values()):
        die(
            f"no loop artifacts (intake/brief/repro/fix/verify/regression.json) in {d} — "
            "refusing to emit an empty handoff",
            code=3,
        )
    intake = arts["intake"] or {}
    brief = arts["brief"] or {}
    repro = arts["repro"] or {}
    fix = arts["fix"] or {}
    # fix-verify writes verify.json; repro-judge writes judge.json. On a judge-only
    # run (intake→repro→judge slice) the judge verdict is the verify-summary the human needs.
    verify = arts["verify"] or arts["judge"] or {}
    regression = arts["regression"] or {}

    cfg_path = a.config or os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "assets", "escalate.config.json")
    cfg = load(cfg_path, required=True)
    tmpl = cfg.get("comment_template", "")
    if not tmpl.strip():
        die(f"config {cfg_path} has no comment_template — refusing to render an empty handoff")

    key = intake.get("key") or repro.get("key") or brief.get("key") or fix.get("key") or "?"
    if key == "?":
        print(f"render_escalation: WARNING: no ticket key in any artifact in {d}", file=sys.stderr)
    area = intake.get("area") or brief.get("surface") or ""
    failure_class = a.reason or fix.get("failure_class") or regression.get("status") or repro.get("status") or "unknown"
    root_cause = fix.get("root_cause") or "(not diagnosed)"
    expected = brief.get("expected_verbatim") or "(n/a)"
    actual = brief.get("actual_verbatim") or repro.get("symptom_quote") or "(n/a)"
    replay_spec = repro.get("replay_spec") or ""
    repro_summary = (
        "; ".join(brief.get("repro_steps", []))
        or (f"replay oracle: {replay_spec}" if replay_spec else "")
        or repro.get("oracle")
        or "(n/a)"
    )
    verify_summary = (f"verify verdict={verify.get('verdict')} confidence={verify.get('confidence')} "
                      f"masking={verify.get('masking_suspected')}") if verify else "(no verify run)"
    failed_specs = regression.get("failed_specs", []) if regression else []
    failed_str = ", ".join(f"{f.get('spec')} [{f.get('class')}]" for f in failed_specs)
    regression_summary = ((f"regression status={regression.get('status')} next_action={regression.get('next_action')}"
                           + (f" — failing: {failed_str}" if failed_str else ""))
                          if regression else "(no regression run)")

    attempts = []
    for name, art in [("intake", intake), ("repro", repro), ("fix", fix), ("verify", verify), ("regression", regression)]:
        if art:
            keys = {k: art[k] for k in ("status", "verdict", "recipe", "next_action", "is_root_cause", "confidence") if k in art}
            attempts.append(f"- {name}: {json.dumps(keys)}")
    attempt_log = "\n".join(attempts) or "(no artifacts found)"

    media = collect_media(d, repro, verify)
    media_str = "\n".join(f"- {m}" for m in media) or "(none)"

    # The calls the loop made on its own. On feature-prerelease's STAGE path this bundle is the human's
    # only view of them (that path skips infra-ship), so the whole record renders here — must_read
    # items as actions, the rest (and the plan-synthesis runner-up) as one-line context.
    decisions_str = render_design_decisions(fix)
    noticed_str = render_noticed(fix, d)

    try:
        comment = tmpl.format(
            reason=failure_class, failure_class=failure_class, root_cause=root_cause,
            attempt_log=attempt_log, repro_summary=repro_summary, expected=expected, actual=actual,
            verify_summary=verify_summary, regression_summary=regression_summary, media=media_str,
            design_decisions=decisions_str, noticed=noticed_str, key=key, area=area,
        )
    except (KeyError, ValueError, IndexError) as e:
        die(f"comment_template in {cfg_path} failed to render ({e!r}) — "
            "unknown placeholder or unescaped literal brace (escape literal braces as {{ and }})")

    # Routing: per-area first, then the default. Values are opaque to this skill (a tracker user
    # id, handle or email the issue_tracker adapter understands). A null result is SURFACED
    # (assignee_configured=false) — never silently dropped.
    assignees = cfg.get("assignees") or {}
    by_area = assignees.get("by_area") or {}
    assignee = by_area.get(area) or assignees.get("default")
    if not assignee:
        print(f"render_escalation: WARNING: no assignee configured for area {area!r} "
              f"(assignees.by_area / assignees.default) — handoff will be unassigned", file=sys.stderr)
    out = {
        "key": key,
        "area": area,
        "assignee": assignee,
        "assignee_configured": bool(assignee),
        "transition": cfg.get("transition_on_escalate"),
        "notification_channel": cfg.get("notification_channel") or "escalations",
        "dry_run": True,
        "posted": False,
        "comment": comment,
    }
    print(json.dumps(out, default=str, indent=2))


if __name__ == "__main__":
    main()
