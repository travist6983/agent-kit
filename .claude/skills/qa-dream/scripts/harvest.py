#!/usr/bin/env python3
"""harvest — build the qa-dream "dream brief": a COMPACT, deterministic, zero-LLM digest of
what the day's/week's Claude sessions and loop runs actually learned.

    python3 harvest.py --since 7d [--project SLUG]... [--all-projects]
                       [--out brief.json] [--max-sessions N] [--max-bytes N]
                       [--watermark ISO] [--now ISO] [--no-brain] [--compact]

Reads, WITHOUT an LLM and without any model API:
  1. Session transcripts  <config_dir>/projects/<slug>/*.jsonl  and the per-session
     subagent/workflow transcripts under  <slug>/<sessionId>/**/ *.jsonl, where
     <config_dir> is `runner.config_dir` from stack.config, else $CLAUDE_CONFIG_DIR, else
     ~/.claude. (Claude Code is the runner by design; the transcript format is its own.)
  2. Run artifacts        <repo>/<evidence.root>/**  AND every live git worktree's
     <worktree>/<evidence.root>/**  (`runs` by default; repro/fix/verify/regression/review/pre-pr/ship/brief/
     intake/results json + pr-followups.md / learnings.md, plus pr-body*.md / plan.md as
     BOUNDED prose evidence). The loops run inside `infra-worktree` sandboxes, so a scan of
     the repo roots alone is blind to exactly the runs that produce fix_lessons. Every dir
     actually scanned is named in `artifact_sources`, and everything discovered-but-unusable
     in `artifact_sources_skipped`, so a `tickets_seen: 0` is never ambiguous between
     "no tickets" and "did not look".
  3. Brain state          `kb receipt --ticket KEY` (READ-ONLY) so every candidate can be
     marked already_in_brain — the dream must never re-propose what a run already wrote.

Emits ONE JSON object. It is a BRIEF, not a transcript pile: every extracted string is
truncated, every category is capped per session, and a global byte budget trims the
lowest-value evidence last-in-first-out until the brief fits.

DETERMINISM: given a fixed window (--watermark/--now or an unchanged clock second) and
unchanged inputs, two runs emit byte-identical output. File lists, session order and every
candidate list are explicitly sorted; nothing iterates an unordered set. The only
clock-dependent fields are `window.now` and `watermark_from` (derived from --since);
`watermark_to` is DATA-derived — never the wall clock — so advancing the watermark can
never skip a record written while the scan was running.

THE WATERMARK CONTRACT — a capped run is a PARTIAL CATCH-UP, never a skip.
  * `--max-sessions` keeps the OLDEST transcripts in the window and defers the newest to
    the next pass (chronological catch-up). Keeping the newest instead — while deriving
    the watermark from what was kept — would advance the watermark over sessions that were
    never read; transcripts rotate in ~3-4 weeks, so that material would be gone for good.
  * `watermark_to` never passes ground the pass actually covered:
      - uncapped  → the newest RECORD consumed (`data_max`), the race guard above;
      - capped    → the newest SCANNED FILE's mtime, hard-clamped to the oldest DEFERRED
        file's mtime. A file's mtime is the newest instant it can contain and is stat'ed
        BEFORE the read, so everything appended mid-scan is stamped later and survives.
        It is also the furthest SAFE point: a deferred session's records run back from its
        mtime to an unknown start, so a watermark set any later would step over a session
        this pass never opened.
    Because the next pass selects files with `mtime >= watermark`, the newest scanned file
    is re-read once (it yields nothing) — one cap slot in forty. If even the oldest scanned
    file survives that filter, no progress is possible (cap of 1, or a whole batch sharing
    one second) and the brief says so in `warnings` instead of looping quietly.
  * PROCESSED is decided by the FILE being read, not by what it yielded. A scanned session
    that is empty, out-of-cwd or unreadable still counts as processed — otherwise it would
    refill the cap on every pass and a backlog could never drain. A session DEFERRED by the
    cap never counts as processed, whatever its timestamps.
  * `sessions_remaining` / `catch_up_needed` tell the runner a backlog is waiting: re-run
    from the new watermark until `catch_up_needed` is false (`sessions_skipped_by_cap` is
    kept as the compatibility alias of `sessions_remaining`).
  * --max-bytes trimming happens AFTER the watermark is fixed and never moves it: the
    budget drops raw session EVIDENCE from the brief, not sessions from the pass, and the
    distilled candidates it protects are exactly what the watermark is being advanced over.
    (Pulling the watermark back when the budget bites would re-harvest the same window into
    the same budget forever.)

CONFIG: the few stack-specific facts come from `.claude/loops/stack.config.json` through
`lib.adapters.config` (stdlib, no network) — `project.ticket_key_pattern` (how a run dir is
named after its ticket), `app.repo_dir` (the target app checkout whose sessions count),
`evidence.root` (the run-artifact dir name) and `runner.config_dir` (where the transcripts
live). Every one of them has a safe default, so the harvest still runs when the config is
absent, and every one can be overridden by a flag.

Python 3.9 compatible. Standard library only.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))


def _stack_cfg(key, default=None):
    """`stack.config.json` lookup through lib.adapters.config (dotted key, LOOPS_CFG__ env
    override). Missing config file, missing key or an import problem all yield `default`:
    the harvest must run — and stay deterministic — on a checkout with no stack.config."""
    try:
        if REPO_ROOT not in sys.path:
            sys.path.insert(0, REPO_ROOT)
        from lib.adapters.config import load_config
        v = load_config().get(key, None)
        return default if v is None else v
    except Exception:
        return default


def _ticket_re():
    """The ticket-key regex, compiled from `project.ticket_key_pattern`. The config pattern is
    anchored (it validates ONE key); here it is searched inside a directory basename or a
    JSON blob, so the anchors come off and word boundaries go on. An unusable pattern falls
    back to the generic `<PREFIX>-<number>` shape rather than crashing the harvest."""
    pat = str(_stack_cfg("project.ticket_key_pattern", "") or "").strip()
    pat = re.sub(r"^\^", "", pat)
    pat = re.sub(r"\$$", "", pat)
    for cand in ([pat] if pat else []) + [r"[A-Z][A-Z0-9]+-[0-9]+"]:
        try:
            return re.compile(r"\b(" + cand + r")\b")
        except re.error:
            continue
    return re.compile(r"\b([A-Z][A-Z0-9]+-[0-9]+)\b")


def _artifacts_dirname():
    """The run-artifact directory name under each repo root / worktree (`evidence.root`,
    default `runs`). Only the basename is used here — a worktree has its own copy."""
    v = str(_stack_cfg("evidence.root", "runs") or "runs").strip().rstrip("/")
    return os.path.basename(v) or "runs"


def _default_projects_root():
    """<config_dir>/projects — `runner.config_dir` from stack.config, else the
    $CLAUDE_CONFIG_DIR the runner exports, else ~/.claude."""
    d = _stack_cfg("runner.config_dir", None) or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return os.path.join(os.path.expanduser(str(d)), "projects")


def _default_app_repos():
    """The target app checkout(s) whose sessions and run artifacts count, from
    `app.repo_dir` (relative paths resolve against this repo's root). Only existing
    directories are returned; no sibling-directory guessing."""
    out = []
    v = _stack_cfg("app.repo_dir", None)
    for item in (v if isinstance(v, list) else [v]):
        if not item or not isinstance(item, str):
            continue
        path = os.path.expanduser(item)
        if not os.path.isabs(path):
            path = os.path.join(REPO_ROOT, path)
        path = os.path.abspath(path)
        if os.path.isdir(path):
            out.append(path)
    return out

# ── caps (the whole point: a brief, never a transcript pile) ──────────────────
CAPS = OrderedDict([
    ("human_turn_chars", 200),      # the human's own words — the single best signal
    ("human_turns", 8),
    ("failed_cmd_chars", 160),
    ("failed_cmd_err_chars", 160),
    ("failed_cmds", 6),
    ("kb_call_chars", 160),
    ("kb_calls", 8),
    ("files_edited", 20),
    ("path_chars", 90),
    ("recap_chars", 320),           # the session's closing assistant message
    ("skill_signal_chars", 200),
    ("skill_signals", 6),
    ("subagent_recaps", 8),
    ("subagent_recap_chars", 200),
    ("artifact_field_chars", 400),  # root_cause / expected_verbatim etc.
    ("artifact_list_items", 8),
    ("prose_file_chars", 2000),     # pr-body/plan prose: per FILE
    ("prose_files", 2),             # pr-body/plan prose: per TICKET
])
DEFAULT_MAX_BYTES = 180_000         # "well under 200KB for a week of sessions"
DEFAULT_MAX_SESSIONS = 40
MAX_LINE_BYTES = 2_000_000          # skip pathological lines (embedded images/base64)

ARTIFACT_JSON = [
    "intake.json", "brief.json", "repro.json", "fix.json", "verify.json", "judge.json",
    "regression.json", "review.json", "pre-pr.json", "ship.json",
    "results.json", "escalation.json", "fix-metadata.json", "task-metadata.json",
]
ARTIFACT_MD = ["pr-followups.md", "learnings.md", "decisions.md", "FINAL-REPORT.md"]
# PROSE artifacts — a loop's own writing, read as BOUNDED EVIDENCE, never as a candidate.
# A feature loop's root cause and fix shape are written up for a human in the PR body (and
# the plan it worked from) and nowhere else, which is exactly fix_lesson material — but an
# uncapped PR body is a transcript pile, the one thing this brief exists not to be. So:
# one pr-body variant + plan.md per ticket, boilerplate stripped, hard-capped per file, and
# every dropped character COUNTED in `filtered` (see read_prose / pick_prose).
PROSE_PLAN = "plan.md"
PROSE_BODY_PREFIX = "pr-body"
# pr-body.initial.md / pr-body.md / pr-body.final.md are drafts of ONE document, not three.
PROSE_VARIANT_RANK = {"pr-body.final.md": 0, "pr-body.md": 1, "pr-body.initial.md": 2}
PROSE_READ_CHARS = 200_000      # read bound; the real cap is CAPS["prose_file_chars"]

# Compiled from `project.ticket_key_pattern` (stack.config) — never a literal project key.
TICKET_RE = _ticket_re()

KB_VERB_RE = re.compile(
    r"\bkb(?:\.py)?\s+(write|read|propose|search|bless|reject|quarantine|forget|decay|"
    r"pause|flag|metric|metrics|receipt|history|confirm|state|outbox|reembed)\b"
    r"(?:\s+(app_map|expectations?|flakiness|fix_lessons?))?"
)
# These are the RUNNER'S OWN markers (Claude Code's transcript format), not a project
# convention: the harvest reads the runner's transcripts by design.
# A tool_result is a FAILURE only on the authoritative signals: the harness's own
# `is_error` flag, its "Exit code <nonzero>" preamble, or a <tool_use_error> envelope.
# A loose "contains the word error" test false-positives on successful output that merely
# mentions an error, and a brief built on false failures teaches the brain lies.
FAIL_TEXT_RE = re.compile(
    r"\A\s*(?:<tool_use_error>|Exit code [1-9])", re.IGNORECASE)
# Error-class buckets, checked in order, for the env-lesson signature.
ERROR_CLASSES = [
    ("command-not-found", "command not found"),
    ("no-such-file", "no such file or directory"),
    ("permission-denied", "permission denied"),
    ("module-not-found", "module not found"),
    ("python-traceback", "traceback (most recent call last)"),
    ("connection-refused", "connection refused"),
    ("input-validation", "inputvalidationerror"),
    ("harness-blocked", "<tool_use_error>blocked"),  # e.g. a foreground `sleep N; <cmd>`
    ("git-fatal", "fatal:"),
]

# A tool call the HUMAN declined is not an environment trap — it is a person exercising
# judgment. Harvesting it would teach the brain that its own operator is a broken dependency.
USER_REJECTED_RE = re.compile(
    r"(the user (doesn't|does not|didn't) want to proceed|tool use was rejected|"
    r"user (rejected|denied|cancelled|canceled))", re.IGNORECASE)

# Shell keywords / wrappers that are never the command that failed. Without this, a
# `for f in …; do …; done` collapses into a `for:` bucket and a loop variable shows up as
# a "command" named `S`.
SHELL_NOISE_HEADS = frozenset([
    "do", "done", "then", "else", "elif", "fi", "esac", "in", "{", "}", "(", ")", "[", "[[",
    "time", "nohup", "exec", "eval", "command", "builtin", "sudo", "env", "xargs",
])
# These START a compound whose remainder is a header/condition, not the failing command.
SHELL_BLOCK_HEADS = frozenset(["for", "while", "until", "select", "case", "if"])
# Reporting/no-op commands: their exit status belongs to something else in the pipeline.
REPORTING_HEADS = frozenset(["echo", "printf", "true", "false", ":", "cat", "cd", "export",
                             "set", "unset", "local", "declare", "readonly", "source", "."])
# For these, exit 1 is an ANSWER ("no match" / "not found"), not a failure.
QUERY_HEADS = frozenset(["grep", "egrep", "fgrep", "rg", "ag", "ls", "find", "test", "[",
                         "diff", "cmp", "which", "type", "pgrep", "jq", "git"])
STMT_SPLIT_RE = re.compile(r"(?:\|\||&&|[;\n|])")

# Skill-defect signals: a documented command that fails, a reference that is wrong,
# a classifier that misses a real case. Small, auditable pattern set — each match
# carries its pattern name so the dream can route by CLASS, not by vibe.
SKILL_SIGNAL_PATTERNS = [
    ("doc-wrong", re.compile(
        r"(the skill|SKILL\.md|the cookbook|the reference|the runbook|the docs?)\b[^.\n]{0,60}"
        r"\b(is|are|was|were)\b[^.\n]{0,30}\b(wrong|incorrect|stale|out of date|outdated|broken|lying)",
        re.IGNORECASE)),
    ("documented-command-fails", re.compile(
        r"(documented|as documented|the documented|per the skill|the skill says|SKILL\.md says)"
        r"[^.\n]{0,80}\b(fail|fails|failed|doesn't work|does not work|is broken|errors? out)",
        re.IGNORECASE)),
    ("classifier-gap", re.compile(
        r"(classifier|classification|taxonomy|the table|the allowlist|keyword table)"
        r"[^.\n]{0,60}\b(gap|misses|missed|has no|doesn't cover|does not cover|wrong)",
        re.IGNORECASE)),
    ("skill-should", re.compile(
        r"\b(the skill|this skill|qa-\w+|dev-\w+|infra-\w+)\s+should\s+(have\s+)?"
        r"(not\s+)?\w+", re.IGNORECASE)),
    ("update-the-skill", re.compile(
        r"\b(update|fix|correct|amend)\s+the\s+(skill|SKILL\.md|reference|cookbook|runbook)\b",
        re.IGNORECASE)),
    ("skill-bug", re.compile(
        r"\b(that'?s|this is|it'?s)\s+a\s+skill\s+(bug|defect|gap|problem)\b", re.IGNORECASE)),
]
# Which of those an ASSISTANT turn may raise. The human's own words are the best signal and
# every pattern applies to them; assistant prose is restricted to the two CONCRETE, checkable
# claims. The loose ones ("classifier gap", "the skill should…") match an agent *discussing*
# skill defects — including its own design notes — which produced a self-referential followup
# built from the agent's own planning text. An agent grading its own homework is not evidence.
ASSISTANT_SIGNAL_PATTERNS = frozenset(["doc-wrong", "documented-command-fails"])


# ── small helpers ────────────────────────────────────────────────────────────

def iso(dt):
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    """Parse an ISO-8601 timestamp from a transcript line. Returns aware UTC datetime or None."""
    if not s or not isinstance(s, str):
        return None
    t = s.strip().replace("Z", "+00:00")
    # python3.9 fromisoformat can't do fractional seconds with >6 digits; trim.
    m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", t)
    if m:
        frac = (m.group(2) or "")[:7]
        t = m.group(1) + frac + (m.group(3) or "")
    try:
        d = datetime.fromisoformat(t)
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


DUR_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)


def parse_since(s, now):
    """'7d' | '36h' | '2w' | an ISO timestamp → aware UTC datetime."""
    m = DUR_RE.match((s or "").strip())
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
        return now - timedelta(seconds=n * mult)
    d = parse_ts(s)
    if d is None:
        raise ValueError("bad --since %r (want 7d / 36h / 2w / an ISO timestamp)" % s)
    return d


WS_RE = re.compile(r"\s+")


def trunc(s, n):
    """Collapse whitespace and hard-truncate. Never returns None."""
    if s is None:
        return ""
    if not isinstance(s, str):
        try:
            s = json.dumps(s, sort_keys=True)
        except Exception:
            s = str(s)
    s = WS_RE.sub(" ", s).strip()
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "…"


def cut(s, n):
    """Hard-truncate WITHOUT collapsing whitespace — prose keeps its line structure, which
    is half of why a PR body is readable at all. `trunc` is for one-line fields."""
    if not isinstance(s, str):
        return ""
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "…"


def snippet(text, start, end, before=60, after=100, cap=200):
    """A readable window around a regex hit, ellipsis-marked when it is a fragment."""
    lo = max(0, start - before)
    hi = min(len(text), end + after)
    s = trunc(text[lo:hi], cap)
    if lo > 0:
        s = "…" + s
    return s


def norm_area(s):
    """Mirror the kb CLI's area normalization so candidates arrive pre-normalized.
    'Design System' → design-system ; 'Billing/Invoices' → billing-invoices."""
    if not s:
        return ""
    s = str(s).strip().lower()
    s = re.sub(r"[\s_/\\]+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


def rel(path, roots):
    """Shorten an absolute path against the known repo roots so paths stay cheap."""
    if not path or not isinstance(path, str):
        return ""
    for name, root in roots:
        if path == root:
            return name
        if path.startswith(root.rstrip("/") + "/"):
            return name + "/" + path[len(root.rstrip("/")) + 1:]
    return path


def under(path, root):
    if not path or not root:
        return False
    p = os.path.normpath(path)
    r = os.path.normpath(root)
    return p == r or p.startswith(r + os.sep)


def slug_for(path):
    """<config_dir>/projects/<slug> — the slug is the abs path with every
    non-alphanumeric character replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(path))


def jload(path):
    try:
        with open(path, "r", errors="replace") as fh:
            return json.load(fh)
    except Exception:
        return None


# ── transcript scanning ──────────────────────────────────────────────────────

def line_type(raw):
    """Cheap type sniff so we never json.loads an attachment/base64 line we don't need."""
    head = raw[:64]
    m = re.match(r'^\{\s*"type"\s*:\s*"([a-zA-Z_-]+)"', head)
    if m:
        return m.group(1)
    return None  # unknown position → caller decides whether to parse


WANTED_TYPES = {"user", "assistant", "ai-title", "summary"}


def blocks_of(obj):
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return []
    ct = msg.get("content")
    if isinstance(ct, str):
        return [{"type": "text", "text": ct}]
    if isinstance(ct, list):
        return [b for b in ct if isinstance(b, dict)]
    return []


def text_of(blocks):
    out = []
    for b in blocks:
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            out.append(b["text"])
    return "\n".join(out).strip()


def result_text(block):
    """tool_result content is a string OR a list of blocks. Never returns base64/image."""
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                parts.append(b["text"])
        return "\n".join(parts)
    return ""


def scan_transcript(path, w_from, w_to, is_subagent, roots, stats):
    """Stream one .jsonl transcript. Returns a per-file extraction dict (uncapped strings
    are already truncated at extraction time; counts are capped by the caller)."""
    rec = {
        "session_id": None, "cwd": None, "title": None,
        "started_at": None, "ended_at": None, "max_in_window": None,
        "human_turns": [], "failed_commands": [], "kb_calls": [],
        "files_edited": [], "recap": "", "skill_signals": [],
        "lines": 0, "malformed": 0,
    }
    pending = {}          # tool_use_id -> {"kind","cmd","at"} awaiting its tool_result
    bash_order = []       # ordered tool_use_ids of Bash calls
    bash_cmds = {}        # tool_use_id -> truncated command text
    ok_after = {}         # tool_use_id -> bool (result came back clean)
    files = []
    seen_signals = set()

    try:
        fh = open(path, "r", errors="replace")
    except Exception:
        stats["unreadable"] += 1
        return None
    with fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or raw[0] != "{":
                continue
            if len(raw) > MAX_LINE_BYTES:
                stats["huge_lines"] += 1
                continue
            t = line_type(raw)
            if t is not None and t not in WANTED_TYPES:
                continue
            try:
                o = json.loads(raw)
            except Exception:
                rec["malformed"] += 1
                stats["malformed"] += 1
                continue
            t = o.get("type")
            if t not in WANTED_TYPES:
                continue
            rec["lines"] += 1

            if t == "ai-title":
                if isinstance(o.get("aiTitle"), str) and not rec["title"]:
                    rec["title"] = trunc(o["aiTitle"], 120)
                continue
            if t == "summary":
                if isinstance(o.get("summary"), str) and not rec["title"]:
                    rec["title"] = trunc(o["summary"], 120)
                continue

            if rec["session_id"] is None and o.get("sessionId"):
                rec["session_id"] = o["sessionId"]
            if rec["cwd"] is None and o.get("cwd"):
                rec["cwd"] = o["cwd"]

            ts = parse_ts(o.get("timestamp"))
            if ts is not None:
                if rec["started_at"] is None or ts < rec["started_at"]:
                    rec["started_at"] = ts
                if rec["ended_at"] is None or ts > rec["ended_at"]:
                    rec["ended_at"] = ts
                if ts < w_from or ts > w_to:
                    # outside the window: still useful for session bounds, but do not extract
                    continue
                # newest record we actually CONSUMED — this, not `ended_at`, is what the
                # watermark may advance to. A session still being appended to can carry a
                # line stamped after our clock; advancing past it would lose it forever.
                if rec["max_in_window"] is None or ts > rec["max_in_window"]:
                    rec["max_in_window"] = ts

            blocks = blocks_of(o)

            if t == "user":
                origin = o.get("origin")
                if isinstance(origin, dict) and origin.get("kind") == "human":
                    txt = text_of(blocks)          # image blocks dropped by text_of
                    if txt:
                        rec["human_turns"].append({
                            "at": iso(ts), "text": trunc(txt, CAPS["human_turn_chars"])})
                        for name, pat in SKILL_SIGNAL_PATTERNS:
                            m = pat.search(txt)
                            if m:
                                ev = snippet(txt, m.start(), m.end(),
                                             cap=CAPS["skill_signal_chars"])
                                k = (name, ev)
                                if k not in seen_signals:
                                    seen_signals.add(k)
                                    rec["skill_signals"].append(
                                        {"pattern": name, "evidence": ev,
                                         "voice": "human", "at": iso(ts)})
                    continue
                # tool_result-bearing user line (NOT human input)
                for b in blocks:
                    if b.get("type") != "tool_result":
                        continue
                    tid = b.get("tool_use_id")
                    p = pending.pop(tid, None)
                    body = result_text(b)
                    bad = bool(b.get("is_error")) or bool(FAIL_TEXT_RE.search(body[:1500]))
                    if tid is not None:
                        ok_after[tid] = not bad
                    if p is None:
                        continue
                    if p["kind"] == "kb":
                        p["entry"]["ok"] = not bad
                        if bad:
                            p["entry"]["error"] = trunc(body, CAPS["failed_cmd_err_chars"])
                    elif p["kind"] == "bash" and bad:
                        # A call the HUMAN declined is a person exercising judgment, not a
                        # property of the environment — harvesting it would teach the brain
                        # that its own operator is a broken dependency.
                        if not USER_REJECTED_RE.search(body[:400]):
                            rec["failed_commands"].append({
                                "at": p["at"], "id": tid,
                                "command": p["cmd"],
                                "error": trunc(body, CAPS["failed_cmd_err_chars"]),
                                "recovered_by": None,
                            })
                continue

            # assistant
            txt = text_of(blocks)
            if txt:
                rec["recap"] = trunc(txt, CAPS["recap_chars"])   # last non-empty wins
                for name, pat in SKILL_SIGNAL_PATTERNS:
                    if name not in ASSISTANT_SIGNAL_PATTERNS:
                        continue  # see ASSISTANT_SIGNAL_PATTERNS — human voice only
                    m = pat.search(txt)
                    if m:
                        ev = snippet(txt, m.start(), m.end(),
                                     cap=CAPS["skill_signal_chars"])
                        k = (name, ev)
                        if k not in seen_signals:
                            seen_signals.add(k)
                            rec["skill_signals"].append(
                                {"pattern": name, "evidence": ev,
                                 "voice": "assistant", "at": iso(ts)})
            for b in blocks:
                if b.get("type") != "tool_use":
                    continue
                name = b.get("name") or ""
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                tid = b.get("id")
                if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if isinstance(fp, str) and fp:
                        files.append(rel(fp, roots)[: CAPS["path_chars"]])
                elif name == "Bash":
                    cmd = inp.get("command")
                    if not isinstance(cmd, str):
                        continue
                    short = trunc(cmd, CAPS["failed_cmd_chars"])
                    bash_cmds[tid] = short
                    km = KB_VERB_RE.search(cmd)
                    if km:
                        entry = {"at": iso(ts), "verb": km.group(1),
                                 "store": km.group(2) or "",
                                 "command": trunc(cmd, CAPS["kb_call_chars"]),
                                 "ok": None}
                        rec["kb_calls"].append(entry)
                        pending[tid] = {"kind": "kb", "entry": entry, "at": iso(ts)}
                    else:
                        pending[tid] = {"kind": "bash", "at": iso(ts), "cmd": short}
                    bash_order.append(tid)

    # Recovery: the recovery command is the next Bash call that came back CLEAN within the
    # 3 calls after the failure. `bash_cmds` keeps the (truncated) command text so the
    # lesson carries "this failed → this worked", which is the actually reusable half.
    pos = {tid: i for i, tid in enumerate(bash_order)}
    for f in rec["failed_commands"]:
        i = pos.get(f["id"])
        f["recovered_by"] = None
        if i is not None:
            fallback = None
            for j in range(i + 1, min(i + 4, len(bash_order))):
                nxt = bash_order[j]
                if not ok_after.get(nxt):
                    continue
                cand = bash_cmds.get(nxt)
                if cand and cand != f["command"]:
                    f["recovered_by"] = cand     # a DIFFERENT command that worked
                    break
                if fallback is None:
                    fallback = cand              # identical retry — weaker evidence
            if f["recovered_by"] is None:
                f["recovered_by"] = fallback
        f["recovered"] = f["recovered_by"] is not None
        f.pop("id", None)

    # de-dup + cap files, preserving first-seen order
    seen = set()
    uniq = []
    for p in files:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    rec["files_edited"] = uniq

    rec["is_subagent"] = is_subagent
    return rec


def cap_session(rec):
    """Apply the per-session item caps. Newest-last ordering is preserved; we keep the
    LAST N of the time-ordered lists (the end of a session is where the lessons land)."""
    rec["human_turns"] = rec["human_turns"][-CAPS["human_turns"]:]
    rec["failed_commands"] = rec["failed_commands"][-CAPS["failed_cmds"]:]
    rec["kb_calls"] = rec["kb_calls"][-CAPS["kb_calls"]:]
    rec["files_edited"] = rec["files_edited"][: CAPS["files_edited"]]
    rec["skill_signals"] = rec["skill_signals"][: CAPS["skill_signals"]]
    return rec


def subagent_files(session_dir):
    out = []
    if not os.path.isdir(session_dir):
        return out
    for dirpath, dirnames, filenames in os.walk(session_dir):
        dirnames.sort()
        for fn in sorted(filenames):
            if fn.endswith(".jsonl"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


# ── run artifacts ────────────────────────────────────────────────────────────

def _s(obj, *keys):
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _list(obj, key, n, chars):
    v = obj.get(key)
    if not isinstance(v, list):
        return []
    out = []
    for item in v[:n]:
        if isinstance(item, dict):
            lbl = _s(item, "title", "spec", "name", "kind", "summary")
            det = _s(item, "decision", "detail", "class", "rationale", "note", "description")
            out.append(trunc((lbl + " — " + det).strip(" —"), chars))
        else:
            out.append(trunc(item, chars))
    return [x for x in out if x]


# Boilerplate every loop's prose carries and no brain ever needs: the trailers the runner
# appends to commits and PR bodies (these are the runner's own markers, not a project PR
# convention), embedded media, and inlined blobs. Stripped BEFORE the cap, so the cap spends
# its budget on the argument rather than on a signature.
PROSE_BOILERPLATE_RE = re.compile(
    r"^\s*!\["                                   # ![alt](media.png)
    r"|^\s*\[!\["                                # [![badge](…)](…)
    r"|^\s*<img\b"
    r"|^\s*(?:🤖\s*)?Generated with\b"            # 🤖 Generated with [Claude Code](…)
    r"|^\s*Co-Authored-By\s*:"
    r"|^\s*Claude-Session\s*:"
    r"|^\s*<?https?://\S+>?\s*$"                 # a bare session/media link line
    r"|;base64,"                                 # an inlined blob, anywhere on the line
    r"|data:[a-z.+-]+/[a-z0-9.+-]+;",
    re.IGNORECASE)


def is_prose_artifact(name):
    """`plan.md` or any `pr-body*.md` — the two places a loop writes prose for a human."""
    return name == PROSE_PLAN or (
        name.startswith(PROSE_BODY_PREFIX) and name.endswith(".md"))


def pick_prose(names):
    """Which prose files a ticket contributes → (chosen, skipped_count).

    At most CAPS["prose_files"]: ONE pr-body — the FINAL variant when several exist, because
    initial/plain/final are drafts of the same document — plus plan.md. Sorted, so a
    directory listing's order can never move a byte in the brief."""
    bodies = sorted(n for n in names if n != PROSE_PLAN and is_prose_artifact(n))
    chosen = []
    if bodies:
        chosen.append(sorted(bodies, key=lambda n: (PROSE_VARIANT_RANK.get(n, 3), n))[0])
    if PROSE_PLAN in names:
        chosen.append(PROSE_PLAN)
    keep = chosen[: CAPS["prose_files"]]
    available = len(bodies) + (1 if PROSE_PLAN in names else 0)
    return keep, max(0, available - len(keep))


def read_prose(path, cap):
    """ONE prose artifact as bounded evidence → (text, chars_dropped, lines_stripped), or
    None if it could not be read. Boilerplate out first, then a hard character cap: what is
    dropped is RETURNED, never silently swallowed — a silent truncation reads as
    "nothing there", the exact failure mode this whole fix is about."""
    try:
        with open(path, "r", errors="replace") as fh:
            raw = fh.read(PROSE_READ_CHARS)
    except Exception:
        return None
    kept, stripped = [], 0
    for line in raw.splitlines():
        if PROSE_BOILERPLATE_RE.search(line):
            stripped += 1
            continue
        kept.append(line.rstrip())
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    out = cut(body, cap)
    return out, max(0, len(body) - len(out)), stripped


def scan_task_dir(d, w_from, w_to, roots, source_dir=""):
    """Read ONE runs run directory into a compact ticket record. Read-only."""
    art = {}
    newest = None
    for name in ARTIFACT_JSON:
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
        except Exception:
            continue
        mt = datetime.fromtimestamp(st.st_mtime, timezone.utc)
        if newest is None or mt > newest:
            newest = mt
        o = jload(p)
        if isinstance(o, dict):
            art[name] = o
    md = {}
    for name in ARTIFACT_MD:
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
            mt = datetime.fromtimestamp(st.st_mtime, timezone.utc)
            if newest is None or mt > newest:
                newest = mt
            with open(p, "r", errors="replace") as fh:
                md[name] = fh.read(20000)
        except Exception:
            continue
    # prose evidence: what a FEATURE loop actually learned lives here and nowhere else
    prose = []
    try:
        entries = sorted(os.listdir(d))
    except Exception:
        entries = []
    picked, prose_skipped = pick_prose([n for n in entries
                                        if os.path.isfile(os.path.join(d, n))])
    for name in picked:
        p = os.path.join(d, name)
        got = read_prose(p, CAPS["prose_file_chars"])
        if not got or not got[0]:
            continue
        text, chars_dropped, lines_stripped = got
        try:
            mt = datetime.fromtimestamp(os.stat(p).st_mtime, timezone.utc)
        except Exception:
            continue
        if newest is None or mt > newest:
            newest = mt
        prose.append(OrderedDict([
            ("file", name),
            ("chars", len(text)),
            ("chars_dropped", chars_dropped),
            ("boilerplate_lines_dropped", lines_stripped),
            ("text", text),
        ]))
    if not art and not md and not prose:
        return None
    if newest is None or newest < w_from or newest > w_to:
        return None

    base = os.path.basename(d.rstrip("/"))
    key = ""
    for src in (art.get("intake.json", {}), art.get("brief.json", {}),
                art.get("fix.json", {}), art.get("ship.json", {}),
                art.get("regression.json", {}), art.get("pre-pr.json", {})):
        if isinstance(src, dict):
            k = _s(src, "key", "ticket")
            if k:
                key = k
                break
    if not key:
        m = TICKET_RE.search(base) or TICKET_RE.search(json.dumps(art)[:4000])
        if m:
            key = m.group(1)
    if not key:
        fm = art.get("fix-metadata.json") or {}
        bugs = fm.get("bugs") if isinstance(fm, dict) else None
        if isinstance(bugs, list) and bugs and isinstance(bugs[0], str):
            key = bugs[0]
    if not key:
        # No ticket key anywhere (a tracker-less run, or a dir named by hand): name it by
        # its directory. `dir:` keys are never looked up in the brain.
        key = "dir:" + base

    intake = art.get("intake.json", {}) or {}
    brief = art.get("brief.json", {}) or {}
    fix = art.get("fix.json", {}) or {}
    verify = art.get("verify.json", {}) or {}
    regr = art.get("regression.json", {}) or {}
    review = art.get("review.json", {}) or {}
    prepr = art.get("pre-pr.json", {}) or {}
    ship = art.get("ship.json", {}) or {}
    fmeta = art.get("fix-metadata.json", {}) or {}
    tmeta = art.get("task-metadata.json", {}) or {}

    area = norm_area(_s(intake, "area") or _s(regr, "area") or _s(fmeta, "strategy"))
    rec = OrderedDict()
    rec["key"] = key
    rec["area"] = area
    rec["dir"] = rel(os.path.abspath(d), roots)
    # which runs root this came from — a repo root or an infra-worktree sandbox.
    # Provenance, so a reader can join a ticket back to the brief's `artifact_sources`.
    rec["source_dir"] = rel(source_dir, roots) if source_dir else ""
    rec["last_artifact_at"] = iso(newest)
    rec["artifacts"] = sorted(list(art.keys()) + list(md.keys())
                              + [x["file"] for x in prose])
    rec["recipe"] = _s(intake, "recipe")
    rec["risk_class"] = _s(intake, "risk_class")
    rec["statuses"] = OrderedDict([
        ("repro", _s(art.get("repro.json", {}) or {}, "status", "outcome")),
        ("fix", _s(fix, "status")),
        ("verify", _s(verify, "verdict")),
        ("regression", _s(regr, "status")),
        ("review", _s(review, "verdict")),
        ("pre_pr", _s(prepr, "status")),
        ("ship", _s(ship, "status")),
    ])
    rec["symptom"] = trunc(
        _s(brief, "actual_verbatim") or _s(fmeta, "actual_result")
        or _s(art.get("repro.json", {}) or {}, "symptom_quote"),
        CAPS["artifact_field_chars"])
    rec["expected"] = trunc(
        _s(brief, "expected_verbatim") or _s(fmeta, "expected_result")
        or _s(verify, "expected_verbatim"),
        CAPS["artifact_field_chars"])
    rec["root_cause"] = trunc(
        _s(fix, "root_cause") or _s(brief, "root_cause_hypothesis"),
        CAPS["artifact_field_chars"])
    rec["is_root_cause"] = fix.get("is_root_cause") if isinstance(fix.get("is_root_cause"), bool) else None
    rec["masking_suspected"] = bool(fix.get("masking_suspected") or verify.get("masking_suspected")
                                    or review.get("masking"))
    rec["files_changed"] = [rel(p, roots)[: CAPS["path_chars"]]
                            for p in (fix.get("files_changed") or [])[: CAPS["artifact_list_items"]]
                            if isinstance(p, str)]
    rec["design_decisions"] = _list(fix, "design_decisions",
                                    CAPS["artifact_list_items"], CAPS["artifact_field_chars"])
    rec["failure_classes"] = sorted({
        str(f.get("class")) for f in (regr.get("failed_specs") or [])
        if isinstance(f, dict) and f.get("class")})
    rec["failed_specs"] = [
        {"spec": trunc(f.get("spec"), 120), "class": str(f.get("class") or ""),
         "detail": trunc(f.get("detail"), 160)}
        for f in (regr.get("failed_specs") or [])[: CAPS["artifact_list_items"]]
        if isinstance(f, dict)]
    rec["quarantined"] = [trunc(x, 120) for x in (regr.get("flaky_quarantined") or [])
                          [: CAPS["artifact_list_items"]]]
    rec["app_map_writes"] = [trunc(x, 120) for x in
                             ((art.get("repro.json", {}) or {}).get("app_map_writes") or [])
                             [: CAPS["artifact_list_items"]]]
    rec["warnings"] = _list(prepr, "warnings", CAPS["artifact_list_items"], 160)
    rec["blockers"] = _list(prepr, "blockers", CAPS["artifact_list_items"], 160)
    rec["review_findings"] = _list(review, "findings", CAPS["artifact_list_items"], 160)

    follow = []
    for name in ("pr-followups.md", "learnings.md", "decisions.md"):
        body = md.get(name)
        if not body:
            continue
        for line in body.splitlines():
            line = line.strip()
            if line.startswith(("- ", "* ")) and len(line) > 4:
                follow.append(trunc(line[2:], 160))
            if len(follow) >= CAPS["artifact_list_items"]:
                break
        if len(follow) >= CAPS["artifact_list_items"]:
            break
    rec["followups"] = follow
    rec["task_description"] = trunc(_s(tmeta, "task_description"), CAPS["artifact_field_chars"])
    # CANDIDATE EVIDENCE, not candidates: the agent may reason from this prose, but the
    # harvest never synthesises a fix_lesson out of it — it stays deterministic and zero-LLM.
    rec["prose_evidence"] = prose
    rec["prose_files_skipped"] = prose_skipped
    return rec


def find_task_dirs(tasks_root, max_depth=3):
    out = []
    if not os.path.isdir(tasks_root):
        return out
    root_depth = tasks_root.rstrip("/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(tasks_root):
        dirnames.sort()
        if dirpath.rstrip("/").count(os.sep) - root_depth >= max_depth:
            dirnames[:] = []
        names = set(filenames)
        if (names & set(ARTIFACT_JSON) or names & set(ARTIFACT_MD)
                or any(is_prose_artifact(n) for n in names)):
            out.append(dirpath)
    return sorted(out)


# ── where the loops actually run: repo roots AND live worktrees ──────────────

def git_worktrees(repo, timeout=15):
    """Every worktree `git` knows about for `repo`, sorted → list, or None if discovery
    itself failed (git missing, not a repo, non-zero exit, timeout, unreadable output).

    DEGRADE, NEVER CRASH: a harvest that dies because a worktree was pruned is worse than
    one that misses it. Sorted because `git worktree list` makes no ordering promise and
    the brief promises byte-identical output."""
    try:
        p = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    try:
        out = (p.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return None
    paths = set()
    for line in out.splitlines():
        if line.startswith("worktree "):
            v = line[len("worktree "):].strip()
            if v:
                paths.add(v)
    return sorted(paths)


def resolve_task_dirs(repos, explicit=None):
    """The runs dirs this pass will scan → (sources, skipped).

    An explicit --tasks-dir overrides entirely. Otherwise: every repo root's runs
    PLUS every live git worktree's — `qa-dream/SKILL.md` says it outright ("a loop's cwd is
    normally an `infra-worktree` sandbox, not this repo"), and a scan of the repo roots
    alone is blind to exactly the runs that would fill `fix_lessons`.

    DEDUPE IS BY os.path.realpath, never by the literal string. `git worktree list` reports
    the MAIN worktree too — that is the repo root a second time — and it prints RESOLVED
    paths, so on macOS a checkout under /tmp or /var comes back as /private/… . A string
    compare scans the same directory twice and doubles its ticket dirs.

    The winning spelling is the first one CONSIDERED (repo roots before worktrees), so a
    repo-root source keeps the caller's own spelling and stays short under `rel()`."""
    sources, skipped, seen = OrderedDict(), OrderedDict(), set()

    def note_skip(path, reason):
        if path not in skipped:
            skipped[path] = reason

    def consider(path, missing_reason):
        real = os.path.realpath(path)
        if real in seen:
            return                       # already scanning this exact directory
        seen.add(real)
        if os.path.isdir(path):
            sources[path] = True
        else:
            note_skip(path, missing_reason)

    if explicit:
        for t in sorted(set(os.path.abspath(x) for x in explicit)):
            consider(t, "not_a_directory")
    else:
        artifacts = _artifacts_dirname()
        for r in repos:
            consider(os.path.join(r, artifacts), "no_runs_dir")
        for r in repos:
            wts = git_worktrees(r)
            if wts is None:
                note_skip(r, "git_worktree_list_failed")
                continue
            for w in wts:
                if not os.path.isdir(w):
                    note_skip(w, "worktree_missing")   # pruned, but still listed
                    continue
                consider(os.path.join(w, artifacts), "no_runs_dir")
    return (sorted(sources),
            [OrderedDict([("path", p), ("reason", r)]) for p, r in sorted(skipped.items())])


# ── brain state (READ-ONLY) ──────────────────────────────────────────────────

def kb_receipt(kb_path, key, since, timeout=30):
    """`kb receipt --ticket KEY` — read-only. Returns (payload|None, note)."""
    if not kb_path or not os.path.exists(kb_path):
        return None, "kb_cli_missing"
    cmd = [sys.executable, kb_path, "receipt", "--ticket", key]
    if since:
        cmd += ["--since", since]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, "exec_error: " + trunc(str(e), 80)
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    err = (p.stderr or b"").decode("utf-8", "replace")
    if not out:
        if "invalid choice" in err or err.lstrip().startswith("usage:"):
            # this brain CLI has no `receipt` verb — unknown, not "absent".
            return None, "verb_unsupported"
        return None, trunc(err, 120) or "no_output"
    try:
        o = json.loads(out.splitlines()[-1])
    except Exception:
        return None, "non_json_response"
    if not isinstance(o, dict) or not o.get("ok"):
        return None, trunc(str((o or {}).get("error", "not_ok")), 120)
    return o, "ok"


def receipt_stores(payload):
    """Best-effort extraction of which stores this ticket already has writes in.
    Tolerates several shapes so a later kb change can't break the harvest."""
    stores = set()
    if not isinstance(payload, dict):
        return stores
    by = (payload.get("writes_by_store") or payload.get("counts_by_store")
          or payload.get("by_store") or payload.get("stores"))
    if isinstance(by, dict):
        for k, v in by.items():
            try:
                n = v if isinstance(v, int) else (len(v) if isinstance(v, list) else int(v))
            except Exception:
                n = 1
            if n:
                stores.add(str(k))
    elif isinstance(by, list):
        for k in by:
            if isinstance(k, str):
                stores.add(k)
    for k in ("writes", "rows", "receipts"):
        v = payload.get(k)
        if isinstance(v, list):
            for r in v:
                if isinstance(r, dict) and r.get("store"):
                    stores.add(str(r["store"]))
    return stores


# ── candidates (deterministic pre-merge; the agent writes the prose) ─────────

ENVASSIGN_RE = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)+")
CD_PREFIX_RE = re.compile(r"^\s*cd\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*(?:&&|;|\|\|)\s*")
CMD_HEAD_RE = re.compile(r"^\s*([\w./-]+)")


def command_head(cmd):
    """The command that actually ran — leading `cd <dir> &&` hops, env assignments and
    wrappers (`time`, `sudo`, `env`, `nohup`, …) peeled off the FRONT.

    Deliberately front-only and deliberately willing to answer `?`. Real Bash calls are
    multi-statement scripts with heredocs and quoted newlines; picking "the" command out of
    one is guesswork, and a guessed head is a loop variable or a mid-string fragment posing
    as a tool. When the front is a shell keyword, a loop header, or a pure reporting command,
    `?` is the honest answer and the ERROR CLASS carries the meaning instead."""
    s = (cmd or "").strip()
    if not s:
        return "?"
    for _ in range(4):
        s2 = CD_PREFIX_RE.sub("", ENVASSIGN_RE.sub("", s))
        if s2 == s:
            break
        s = s2
    for _ in range(4):
        m = CMD_HEAD_RE.match(s)
        if not m:
            return "?"
        head = os.path.basename(m.group(1))
        if head in SHELL_NOISE_HEADS:          # `time foo`, `sudo foo`, `do foo`
            s = s[m.end():].strip()
            continue
        if head in SHELL_BLOCK_HEADS or head in REPORTING_HEADS:
            return "?"                          # a compound script, not one command
        return head or "?"
    return "?"


def failure_signature(f):
    tool = command_head(f.get("command"))
    err = (f.get("error") or "").lower()
    for cls, pat in ERROR_CLASSES:
        if pat in err:
            return tool + ":" + cls
    m = re.search(r"exit code (\d+)", err)
    if m:
        return tool + ":exit-" + m.group(1)
    return tool + ":error"


# Every named class EXCEPT python-traceback (see env_lesson_worthy for why).
WORTHY_CLASSES = frozenset(c for c, _ in ERROR_CLASSES) - {"python-traceback"}


def env_lesson_worthy(sig, recovered):
    """Is this failure a reusable ENVIRONMENT LESSON, or just a command that exited non-zero?

    Quality beats completeness here: a dream brief full of junk produces a brain full of junk.
    The bar is a NAMED diagnostic class — a trap a future run can recognise and route around:
    a missing binary, a path that isn't there, a permission wall, a blocked harness pattern,
    a refused connection, an argument the tool layer rejects, a git fatal.

    Deliberately NOT part of the bar:
      * `recovered` — it fires on ~every failure by construction (the
        heuristic pairs the next differing Bash call), so it separates nothing. It is still
        REPORTED, because the recovery command is the most useful thing in the example.
      * a bare `exit-N` or an unclassifiable `:error` — a one-off script bug, a `grep` that
        found nothing, or a 10-minute timeout (exit 143). None of those is a property of the
        environment, and none is actionable next week.
      * `python-traceback` — a traceback from inline code written seconds earlier (a
        `python3 - <<PY` heredoc with a KeyError) is a bug in that code, not an environment
        lesson. A traceback that IS environmental (`ModuleNotFoundError`) classifies as
        `module-not-found` first and survives.
    """
    _head, _, cls = sig.partition(":")
    return cls in WORTHY_CLASSES


def build_candidates(sessions, tickets, brain, sessions_remaining=0):
    app_map, fix_lessons, expectations, flakiness = [], [], [], []
    env_lessons, skill_followups, metrics = [], [], []

    # ── env_lessons: the same trap hit in N sessions collapses into ONE lesson ──
    buckets = OrderedDict()
    for s in sessions:
        for f in s.get("failed_commands", []):
            sig = failure_signature(f)
            b = buckets.setdefault(sig, {
                "signature": sig, "occurrences": 0, "sessions": [],
                "examples": [], "recovered": 0})
            b["occurrences"] += 1
            if s["session_id"] not in b["sessions"]:
                b["sessions"].append(s["session_id"])
            if f.get("recovered"):
                b["recovered"] += 1
            if len(b["examples"]) < 3:
                b["examples"].append(OrderedDict([
                    ("command", f.get("command", "")),
                    ("error", f.get("error", "")),
                    ("recovered_by", f.get("recovered_by")),
                ]))
    env_dropped = OrderedDict()
    for sig in sorted(buckets):
        b = buckets[sig]
        if not env_lesson_worthy(sig, b["recovered"] > 0):
            env_dropped[sig] = b["occurrences"]     # counted, never silently discarded
            continue
        env_lessons.append(OrderedDict([
            ("signature", b["signature"]),
            ("occurrences", b["occurrences"]),
            ("distinct_sessions", len(b["sessions"])),
            ("sessions", sorted(b["sessions"])[:6]),
            ("recovered_count", b["recovered"]),
            ("examples", b["examples"]),
            ("suggested_store", "app_map"),
            ("suggested_kind", "lesson"),
            ("already_in_brain", None),
        ]))

    # ── skill_followups: run-discovered skill defects ──
    sig_buckets = OrderedDict()
    for s in sessions:
        for g in s.get("skill_signals", []):
            k = (g["pattern"], g["evidence"])
            b = sig_buckets.setdefault(k, {
                "pattern": g["pattern"], "evidence": g["evidence"],
                "voice": g.get("voice", ""), "sessions": []})
            if s["session_id"] not in b["sessions"]:
                b["sessions"].append(s["session_id"])
    for k in sorted(sig_buckets):
        b = sig_buckets[k]
        skill_followups.append(OrderedDict([
            ("pattern", b["pattern"]),
            ("voice", b["voice"]),
            ("evidence", b["evidence"]),
            ("distinct_sessions", len(b["sessions"])),
            ("sessions", sorted(b["sessions"])[:6]),
        ]))

    # ── ticket-derived candidates ──
    for t in tickets:
        key = t["key"]
        stores = brain.get(key, {}).get("stores", set())
        known = brain.get(key, {}).get("note", "unknown") == "ok"

        def mark(store):
            if not known:
                return None
            return store in stores

        if t.get("root_cause") and t["statuses"].get("verify") in ("pass", "partial"):
            fix_lessons.append(OrderedDict([
                ("ticket", key), ("area", t["area"]),
                ("symptom", t["symptom"]), ("root_cause", t["root_cause"]),
                ("fix_shape", trunc("; ".join(t.get("design_decisions") or [])
                                    or ", ".join(t.get("files_changed") or []),
                                    CAPS["artifact_field_chars"])),
                ("masking_suspected", t["masking_suspected"]),
                ("evidence", [t["dir"] + "/fix.json", t["dir"] + "/verify.json"]),
                ("already_in_brain", mark("fix_lessons")),
            ]))
        if t.get("expected"):
            expectations.append(OrderedDict([
                ("ticket", key), ("area", t["area"]),
                ("statement", t["expected"]),
                ("evidence", [t["dir"] + "/brief.json"]),
                ("propose_only", True),
                ("already_in_brain", mark("expectations")),
            ]))
        for w in t.get("app_map_writes", []):
            app_map.append(OrderedDict([
                ("ticket", key), ("area", t["area"]),
                ("kind", "selector"), ("value", w),
                ("evidence", [t["dir"] + "/repro.json"]),
                ("already_in_brain", mark("app_map")),
            ]))
        for f in t.get("failed_specs", []):
            if f.get("class") in ("flaky", "environment"):
                flakiness.append(OrderedDict([
                    ("ticket", key), ("area", t["area"]),
                    ("spec", f.get("spec", "")), ("class", f.get("class", "")),
                    ("detail", f.get("detail", "")),
                    ("evidence", [t["dir"] + "/regression.json"]),
                    ("already_in_brain", mark("flakiness")),
                ]))
        for q in t.get("quarantined", []):
            flakiness.append(OrderedDict([
                ("ticket", key), ("area", t["area"]), ("spec", q),
                ("class", "quarantined"), ("detail", ""),
                ("evidence", [t["dir"] + "/regression.json"]),
                ("already_in_brain", mark("flakiness")),
            ]))

    # ── metrics: what the window itself measured (R5/R8 fuel) ──
    shipped = sum(1 for t in tickets if (t["statuses"].get("ship") or "").startswith("shipped"))
    verified = sum(1 for t in tickets if t["statuses"].get("verify") == "pass")
    blocked = sum(1 for t in tickets
                  if (t["statuses"].get("regression") or "").startswith("blocked"))
    metrics = [
        OrderedDict([("name", "dream_sessions_scanned"), ("value", len(sessions)),
                     ("unit", "count")]),
        # the backlog the cap deferred — visible in the brain, so a runner that never
        # drains (or a --max-sessions set too low) shows up as a trend, not a surprise.
        OrderedDict([("name", "dream_sessions_remaining"), ("value", sessions_remaining),
                     ("unit", "count")]),
        OrderedDict([("name", "dream_tickets_seen"), ("value", len(tickets)), ("unit", "count")]),
        OrderedDict([("name", "dream_loops_verified_pass"), ("value", verified),
                     ("unit", "count")]),
        OrderedDict([("name", "dream_loops_shipped"), ("value", shipped), ("unit", "count")]),
        OrderedDict([("name", "dream_regression_blocked"), ("value", blocked), ("unit", "count")]),
        OrderedDict([("name", "dream_env_traps"), ("value", len(env_lessons)), ("unit", "count")]),
        OrderedDict([("name", "dream_skill_followups"), ("value", len(skill_followups)),
                     ("unit", "count")]),
    ]

    fix_lessons.sort(key=lambda x: (x["ticket"], x["root_cause"][:60]))
    expectations.sort(key=lambda x: (x["ticket"], x["statement"][:60]))
    app_map.sort(key=lambda x: (x["ticket"], x["value"][:60]))
    flakiness.sort(key=lambda x: (x["ticket"], x["spec"][:60], x["class"]))
    env_lessons.sort(key=lambda x: (-x["occurrences"], x["signature"]))
    skill_followups.sort(key=lambda x: (-x["distinct_sessions"], x["pattern"], x["evidence"][:40]))

    return OrderedDict([
        ("app_map", app_map),
        ("fix_lessons", fix_lessons),
        ("expectations", expectations),
        ("flakiness", flakiness),
        ("env_lessons", env_lessons),
        ("skill_followups", skill_followups),
        ("metrics", metrics),
    ]), prose_filtered(tickets, OrderedDict([
        # What env_lesson_worthy() refused, and how often — a filter whose rejects are
        # invisible is indistinguishable from a filter that is silently eating real signal.
        ("env_lessons_filtered", OrderedDict(sorted(env_dropped.items()))),
        ("env_lessons_filtered_total", sum(env_dropped.values())),
    ]))


def prose_filtered(tickets, filtered):
    """Fold the prose caps into `filtered`, the same discipline env_lessons_filtered runs
    on: a cap whose drops are invisible reads as "nothing there". Recomputed from the live
    tickets, so a later budget trim of the same prose stays consistent with the counts."""
    by_file, chars, lines, files_skipped = OrderedDict(), 0, 0, 0
    for t in tickets:                       # already sorted → the dict is deterministic
        files_skipped += int(t.get("prose_files_skipped") or 0)
        for p in t.get("prose_evidence") or []:
            n = int(p.get("chars_dropped") or 0)
            lines += int(p.get("boilerplate_lines_dropped") or 0)
            chars += n
            if n:
                by_file[str(t.get("key", "")) + "/" + str(p.get("file", ""))] = n
    filtered["prose_chars_dropped"] = by_file
    filtered["prose_chars_dropped_total"] = chars
    filtered["prose_boilerplate_lines_dropped_total"] = lines
    filtered["prose_files_skipped_total"] = files_skipped
    return filtered


# ── budget ───────────────────────────────────────────────────────────────────

def serialize(brief, compact):
    if compact:
        return json.dumps(brief, separators=(",", ":"))
    return json.dumps(brief, indent=2)


def enforce_budget(brief, max_bytes, compact):
    """Trim the lowest-value evidence until the brief fits. Deterministic: always trims
    the same things in the same order. Candidates and tickets are NEVER dropped — they
    are the product; only the raw session evidence is thinned.

    Runs AFTER the watermark is fixed and never moves it: this trims what the brief CARRIES,
    not what the pass READ (see the watermark contract at the top of this file)."""
    dropped = OrderedDict([("session_fields", []), ("sessions_dropped", 0),
                           ("prose_thinned", False),
                           ("evidence_examples_trimmed", False)])
    # Bind the live `dropped` block into the brief up front, so every measurement below
    # sizes the object this function actually returns — the trim log and its own warnings
    # are part of the payload, and a budget that does not count them is not a budget.
    brief["dropped"] = dropped
    s = serialize(brief, compact)
    if len(s.encode("utf-8")) <= max_bytes:
        brief["truncated"] = False
        brief["budget_exceeded"] = False
        return s

    order = [
        ("files_edited", lambda r: r.__setitem__("files_edited", r["files_edited"][:5])),
        ("kb_calls", lambda r: r.__setitem__("kb_calls", r["kb_calls"][-3:])),
        ("recap", lambda r: r.__setitem__("recap", trunc(r["recap"], 160))),
        ("human_turns", lambda r: r.__setitem__("human_turns", r["human_turns"][-4:])),
        ("subagent_recaps", lambda r: r.__setitem__("subagent_recaps", r.get("subagent_recaps", [])[:2])),
        ("failed_commands", lambda r: r.__setitem__("failed_commands", r["failed_commands"][-3:])),
        ("files_edited_all", lambda r: r.__setitem__("files_edited", [])),
        ("human_turns_2", lambda r: r.__setitem__("human_turns", r["human_turns"][-2:])),
        ("recap_short", lambda r: r.__setitem__("recap", trunc(r["recap"], 80))),
    ]
    for label, fn in order:
        for r in brief["sessions"]:
            try:
                fn(r)
            except Exception:
                pass
        dropped["session_fields"].append(label)
        brief["truncated"] = True
        s = serialize(brief, compact)
        if len(s.encode("utf-8")) <= max_bytes:
            brief["budget_exceeded"] = False
            return serialize(brief, compact)

    # next: thin the pr-body/plan prose. It is EVIDENCE, not a candidate, so it trims —
    # but never to zero: a 200-char gist of what a loop told a human survives a trim far
    # better than the tail of a transcript does, which is why it goes before whole sessions.
    for limit in (600, 200):
        if len(s.encode("utf-8")) <= max_bytes:
            break
        touched = False
        for t in brief.get("tickets", []):
            for p in t.get("prose_evidence") or []:
                if len(p.get("text") or "") > limit:
                    before = len(p["text"])
                    p["text"] = cut(p["text"], limit)
                    p["chars"] = len(p["text"])
                    p["chars_dropped"] = int(p.get("chars_dropped") or 0) + (
                        before - p["chars"])
                    touched = True
        if not touched:
            continue
        dropped["prose_thinned"] = True
        brief["truncated"] = True
        # keep `filtered` honest about the trim it just made, or the per-file counts and
        # the totals would disagree inside one brief.
        prose_filtered(brief.get("tickets", []), brief["filtered"])
        s = serialize(brief, compact)
        if len(s.encode("utf-8")) <= max_bytes:
            brief["budget_exceeded"] = False
            return s

    # next: drop whole sessions, oldest first (the candidates already hold their signal)
    while brief["sessions"] and len(s.encode("utf-8")) > max_bytes:
        brief["sessions"].pop(0)
        dropped["sessions_dropped"] += 1
        if dropped["sessions_dropped"] == 1:
            # The watermark was fixed BEFORE this trim and is deliberately NOT walked back:
            # the pass really did read these sessions and their distilled candidates survive
            # right here. Walking it back would re-harvest the same window into the same
            # budget forever. But raw evidence the dream will never get another look at is
            # not something to drop quietly — say it once, in the payload being measured.
            brief["warnings"].append(
                "--max-bytes dropped whole sessions of raw evidence (count in "
                "dropped.sessions_dropped); their candidates are kept and the watermark "
                "still advances past them. Lower --max-sessions or raise --max-bytes.")
        s = serialize(brief, compact)

    # last resort: thin env-lesson evidence to ONE example each. Never to zero — the skill
    # may not write a lesson it cannot cite evidence for.
    if len(s.encode("utf-8")) > max_bytes:
        for e in brief["candidates"].get("env_lessons", []):
            if len(e.get("examples", [])) > 1:
                e["examples"] = e["examples"][:1]
                dropped["evidence_examples_trimmed"] = True
        s = serialize(brief, compact)

    brief["truncated"] = True
    over = len(s.encode("utf-8")) > max_bytes
    brief["budget_exceeded"] = over
    if over:
        brief["warnings"].append(
            "over --max-bytes %d after every trim: candidates + tickets are the product and "
            "are never dropped. Narrow the window or raise --max-bytes." % max_bytes)
    return serialize(brief, compact)   # `dropped` is already bound into `brief`


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv=None):
    repo_root = REPO_ROOT
    default_kb = os.environ.get("KB_BIN") or os.path.join(
        repo_root, ".claude", "skills", "qa-kb", "scripts", "kb.py")
    app_repos = _default_app_repos()
    default_projects_root = _default_projects_root()

    ap = argparse.ArgumentParser(
        prog="harvest.py",
        description="Build the qa-dream brief (deterministic, zero-LLM).")
    ap.add_argument("--since", default="1d",
                    help="window size (7d/36h/2w) or an ISO timestamp. Default 1d.")
    ap.add_argument("--watermark", default=None,
                    help="explicit window START (ISO). Overrides --since. This is what the "
                         "skill passes from `kb state --key dream.watermark`.")
    ap.add_argument("--now", default=None,
                    help="pin the clock (ISO) — makes the whole brief byte-reproducible.")
    ap.add_argument("--project", action="append", default=[],
                    help="project slug under --projects-root (repeatable). Slugs start with "
                         "'-', so use the =form: --project=-home-me-src-my-app")
    ap.add_argument("--all-projects", action="store_true",
                    help="scan every project slug, not just the ones that map to this repo "
                         "and app.repo_dir.")
    ap.add_argument("--projects-root",
                    default=default_projects_root,
                    help="Claude Code projects dir (default %s — runner.config_dir, else "
                         "$CLAUDE_CONFIG_DIR, else ~/.claude, plus /projects)."
                         % default_projects_root)
    ap.add_argument("--repo", action="append", default=[],
                    help="repo root whose sessions/artifacts count (repeatable). "
                         "Default: this repo + app.repo_dir from stack.config.")
    ap.add_argument("--tasks-dir", action="append", default=[],
                    help="runs dir to scan (repeatable). OVERRIDES the default "
                         "entirely. Default: <repo>/runs for every --repo PLUS "
                         "<worktree>/runs for every live git worktree of those "
                         "repos (that is where the loops actually run).")
    ap.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS)
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    ap.add_argument("--no-subagents", action="store_true")
    ap.add_argument("--no-brain", action="store_true",
                    help="skip the READ-ONLY `kb receipt` lookups (offline harvest).")
    ap.add_argument("--kb", default=default_kb, help="path to kb.py")
    ap.add_argument("--compact", action="store_true", help="minified JSON")
    ap.add_argument("--out", default=None, help="write here instead of stdout")
    a = ap.parse_args(argv)

    now = parse_ts(a.now) if a.now else datetime.now(timezone.utc)
    if now is None:
        print(json.dumps({"ok": False, "error": "bad --now"}), file=sys.stderr)
        return 2
    now = now.replace(microsecond=0)
    try:
        w_from = parse_ts(a.watermark) if a.watermark else parse_since(a.since, now)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 2
    if w_from is None:
        print(json.dumps({"ok": False, "error": "bad --watermark"}), file=sys.stderr)
        return 2
    w_from = w_from.replace(microsecond=0)
    w_to_limit = now

    repos = [os.path.abspath(r) for r in a.repo] or ([repo_root] + app_repos)
    repos = sorted(set(repos))
    roots = sorted(((os.path.basename(r.rstrip("/")), r) for r in repos),
                   key=lambda x: -len(x[1]))

    warnings = []
    stats = {"malformed": 0, "huge_lines": 0, "unreadable": 0}

    # ── choose project slug dirs ──
    proots = a.projects_root
    slugs = []
    if a.project:
        slugs = sorted(set(a.project))
    elif os.path.isdir(proots):
        allslugs = sorted(d for d in os.listdir(proots)
                          if os.path.isdir(os.path.join(proots, d)))
        if a.all_projects:
            slugs = allslugs
        else:
            want = set()
            for r in repos:
                base = slug_for(r)
                want.add(base)
                for s in allslugs:
                    if s.startswith(base + "-"):
                        want.add(s)
            slugs = sorted(want & set(allslugs))
    if not slugs:
        warnings.append("no project slugs matched under %s" % proots)

    # ── scan transcripts ──
    filter_by_cwd = not (a.project or a.all_projects)
    files = []
    for slug in slugs:
        d = os.path.join(proots, slug)
        if not os.path.isdir(d):
            warnings.append("missing project dir: " + slug)
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(d, fn)
            try:
                mt = datetime.fromtimestamp(os.stat(p).st_mtime, timezone.utc)
            except Exception:
                continue
            if mt < w_from:
                continue     # nothing in this file can be inside the window
            files.append((mt, slug, p))
    files.sort(key=lambda x: (x[0], x[2]))
    # CHRONOLOGICAL CATCH-UP: when the cap bites, keep the OLDEST sessions and defer the
    # newest to the next pass. `files[-N:]` (keep the newest) combined with a watermark
    # derived from what was kept advances the watermark over sessions that were never read
    # — and transcripts rotate in ~3-4 weeks, so that material is lost permanently.
    scanned = files[: a.max_sessions] if a.max_sessions > 0 else files
    deferred = files[len(scanned):]
    sessions_remaining = len(deferred)
    # PROCESSED vs DEFERRED — the distinction the watermark is built on:
    #   * scanned  → PROCESSED, even if it yields nothing (empty window, wrong cwd,
    #     unreadable). Its whole span was read. If an empty scan did not count, the same
    #     file would refill the cap on every pass and the backlog would never drain.
    #   * deferred → NEVER processed. Nothing in it was read, so the watermark must stay
    #     behind it even though it is the NEWER file by mtime.
    # mtime is the newest instant a file can contain (records are appended carrying their
    # own timestamp) and is stat'ed above, BEFORE the read — so anything appended while the
    # scan is running is stamped later than `scanned_through` and survives for the next pass.
    scanned_through = max([mt for mt, _sl, _p in scanned]) if scanned else None
    deferred_floor = deferred[0][0] if deferred else None

    sessions = []
    data_max = None
    empty_sessions = 0
    for mt, slug, p in scanned:
        rec = scan_transcript(p, w_from, w_to_limit, False, roots, stats)
        if rec is None:
            continue
        sid = rec["session_id"] or os.path.splitext(os.path.basename(p))[0]
        rec["session_id"] = sid
        rec["project"] = slug
        if filter_by_cwd and rec.get("cwd"):
            if not any(under(rec["cwd"], r) for r in repos):
                continue
        # subagents / workflow agents for this session
        subs = []
        if not a.no_subagents:
            sdir = os.path.join(os.path.dirname(p), sid)
            for sp in subagent_files(sdir):
                if len(subs) >= CAPS["subagent_recaps"]:
                    break
                try:
                    if datetime.fromtimestamp(os.stat(sp).st_mtime, timezone.utc) < w_from:
                        continue
                except Exception:
                    continue
                srec = scan_transcript(sp, w_from, w_to_limit, True, roots, stats)
                if not srec:
                    continue
                if srec["max_in_window"] and (data_max is None
                                              or srec["max_in_window"] > data_max):
                    data_max = srec["max_in_window"]
                for g in srec.get("skill_signals", []):
                    if len(rec["skill_signals"]) < CAPS["skill_signals"] * 2:
                        rec["skill_signals"].append(g)
                for f in srec.get("failed_commands", [])[-2:]:
                    rec["failed_commands"].append(f)
                for k in srec.get("kb_calls", []):
                    rec["kb_calls"].append(k)
                if srec.get("recap"):
                    subs.append(OrderedDict([
                        ("agent", os.path.splitext(os.path.basename(sp))[0]),
                        ("recap", trunc(srec["recap"], CAPS["subagent_recap_chars"]))]))
        rec["subagent_recaps"] = subs
        rec = cap_session(rec)
        if rec["max_in_window"] and (data_max is None or rec["max_in_window"] > data_max):
            data_max = rec["max_in_window"]
        if not any((rec["human_turns"], rec["failed_commands"], rec["kb_calls"],
                    rec["files_edited"], rec["skill_signals"], subs, rec["recap"])):
            empty_sessions += 1   # touched inside the window but contributed no signal
            continue
        out = OrderedDict([
            ("session_id", sid),
            ("project", slug),
            ("cwd", rel(rec.get("cwd") or "", roots)),
            ("title", rec.get("title") or ""),
            ("started_at", iso(rec["started_at"])),
            ("ended_at", iso(rec["ended_at"])),
            ("lines_read", rec["lines"]),
            ("human_turns", rec["human_turns"]),
            ("failed_commands", rec["failed_commands"]),
            ("kb_calls", rec["kb_calls"]),
            ("files_edited", rec["files_edited"]),
            ("skill_signals", rec["skill_signals"][: CAPS["skill_signals"]]),
            ("subagent_recaps", subs),
            ("recap", rec["recap"]),
        ])
        sessions.append(out)
    sessions.sort(key=lambda r: (r["ended_at"] or "", r["session_id"]))

    # ── scan run artifacts ──
    # Repo roots AND live worktrees: the loops run in infra-worktree sandboxes, so the repo
    # roots alone miss the very runs that produce fix_lessons. --tasks-dir overrides both.
    tdirs, tskipped = resolve_task_dirs(repos, a.tasks_dir)
    if not tdirs:
        warnings.append(
            "no run-artifact dir to scan — every candidate class fed by run artifacts will "
            "be empty for reasons that have nothing to do with the window "
            "(see artifact_sources_skipped)")
    tickets_by_key = OrderedDict()
    for tr in tdirs:
        for d in find_task_dirs(tr):
            rec = scan_task_dir(d, w_from, w_to_limit, roots, tr)
            if not rec:
                continue
            ts = parse_ts(rec["last_artifact_at"])
            if ts and (data_max is None or ts > data_max):
                data_max = ts
            prev = tickets_by_key.get(rec["key"])
            if prev is None or (rec["last_artifact_at"] or "") > (prev["last_artifact_at"] or ""):
                tickets_by_key[rec["key"]] = rec
    tickets = sorted(tickets_by_key.values(),
                     key=lambda t: (t["last_artifact_at"] or "", t["key"]))

    # ── brain state (read-only) ──
    brain = OrderedDict()
    if not a.no_brain:
        span_days = max(1, int((w_to_limit - w_from).total_seconds() // 86400) + 1)
        since_arg = "%dd" % span_days
        for t in tickets:
            key = t["key"]
            if key.startswith("dir:") or key in brain:
                continue
            payload, note = kb_receipt(a.kb, key, since_arg)
            brain[key] = {"note": note, "stores": receipt_stores(payload)}
            if note != "ok" and ("brain lookup unavailable: " + note) not in warnings:
                warnings.append("brain lookup unavailable: " + note)
    else:
        warnings.append("brain lookup skipped (--no-brain)")
    for t in tickets:
        b = brain.get(t["key"], {})
        t["brain"] = OrderedDict([
            ("lookup", b.get("note", "skipped")),
            ("stores_written", sorted(b.get("stores", set()))),
        ])

    candidates, filtered = build_candidates(sessions, tickets, brain, sessions_remaining)

    # ── the watermark: only over ground this pass actually covered ───────────
    # Uncapped: the newest RECORD consumed. data_max is already clamped to the window at
    # extraction time, but belt-and-braces.
    w_to = min(data_max, w_to_limit) if data_max else w_from
    if sessions_remaining:
        # Capped = partial catch-up. Advance to the newest SCANNED file's mtime: every
        # record those files hold was read — including the ones that yielded nothing — so
        # the next pass can move on to the deferred backlog instead of re-reading this
        # batch forever. It is also the FURTHEST safe point: a deferred file's records run
        # back from its mtime to an unknown start, so anything later would step over
        # material in a session this pass never opened.
        catch_up_to = scanned_through if scanned_through is not None else w_from
        # Ticket artifacts are scanned uncapped, so data_max can be NEWER than the session
        # boundary — the boundary still rules. Re-reading a task dir next pass is
        # idempotent (`already_in_brain` marks it); a rotated-away transcript is not
        # recoverable.
        if deferred_floor is not None and catch_up_to > deferred_floor:
            # Unreachable while `files` stays mtime-sorted — kept as the executable
            # statement of the invariant: a session DEFERRED by the cap is never counted as
            # processed, however new its mtime is.
            catch_up_to = deferred_floor
        w_to = min(catch_up_to, w_to_limit)
    if w_to < w_from:
        w_to = w_from
    if sessions_remaining and scanned and scanned[0][0] >= w_to:
        # The next pass selects files with `mtime >= watermark`, so the newest scanned file
        # is always re-read once (it yields nothing and costs one cap slot — 1 in 40 at the
        # default). If even the OLDEST scanned file survives that filter, the next pass
        # would re-read this exact batch and the backlog can never drain: the cap is 1, or
        # every file in it shares one second, and a second-resolution watermark cannot split
        # them. Say so — a silent no-op nightly is the failure mode this warning exists to remove.
        warnings.append(
            "catch-up cannot advance: --max-sessions %d leaves %d session(s) waiting but the "
            "watermark (%s) does not clear the oldest scanned transcript, so the next pass "
            "would re-read the same batch. Raise --max-sessions."
            % (a.max_sessions, sessions_remaining, iso(w_to)))

    brief = OrderedDict([
        ("schema", "qa-dream.brief/1"),
        ("watermark_from", iso(w_from)),
        ("watermark_to", iso(w_to)),
        ("window", OrderedDict([("since", a.watermark or a.since), ("now", iso(now))])),
        ("repos", [rel(r, roots) or r for r in repos]),
        ("projects", slugs),
        # WHAT THE SCAN LOOKED AT. A `tickets_seen: 0` is ambiguous on its own — it can mean
        # "no tickets" or "did not look". These two lists make the two cases distinguishable
        # without re-deriving the defaults.
        ("artifact_sources", tdirs),
        ("artifact_sources_skipped", tskipped),
        ("sessions_scanned", len(sessions)),
        ("sessions_available", len(files)),
        ("session_files_read", len(scanned)),
        ("sessions_empty_in_window", empty_sessions),
        # sessions_skipped_by_cap is the compatibility alias of sessions_remaining: both
        # mean "deferred to the next pass", never "dropped".
        ("sessions_skipped_by_cap", sessions_remaining),
        ("sessions_remaining", sessions_remaining),
        ("catch_up_needed", sessions_remaining > 0),
        ("tickets_seen", len(tickets)),
        ("candidate_counts", OrderedDict((k, len(v)) for k, v in candidates.items())),
        ("caps", OrderedDict(CAPS)),
        ("parse_stats", OrderedDict(sorted(stats.items()))),
        ("filtered", filtered),
        ("warnings", warnings),
        ("candidates", candidates),
        ("tickets", tickets),
        ("sessions", sessions),
        ("truncated", False),
        ("budget_exceeded", False),
        ("dropped", OrderedDict([("session_fields", []), ("sessions_dropped", 0),
                                 ("prose_thinned", False),
                                 ("evidence_examples_trimmed", False)])),
    ])

    text = enforce_budget(brief, a.max_bytes, a.compact)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text + "\n")
        print(json.dumps({
            "ok": True, "out": a.out, "bytes": len(text.encode("utf-8")),
            "sessions_scanned": brief["sessions_scanned"],
            "tickets_seen": brief["tickets_seen"],
            "candidates": brief["candidate_counts"],
            "watermark_from": brief["watermark_from"],
            "watermark_to": brief["watermark_to"],
            "truncated": brief["truncated"],
            # the runner's catch-up signal: re-run from watermark_to while this is true.
            "sessions_remaining": brief["sessions_remaining"],
            "catch_up_needed": brief["catch_up_needed"],
        }, indent=2))
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
