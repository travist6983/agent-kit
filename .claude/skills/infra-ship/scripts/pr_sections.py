#!/usr/bin/env python3
"""The PR body's Testing / Risk / How to revert sections, rendered from the seam files — zero LLM.

WHY THIS EXISTS. A reviewer asks three things of every PR: how was it tested, how risky is it, and how
do I undo it. Written by hand at ship time those answers drift from the evidence (a "tested" line with
no verify.json behind it, a risk tier that disagrees with dev-pre-pr, a revert plan nobody checked). Here
every fact comes from an artifact the loop already wrote, or from the deploy adapter, and the body is
checked for the rendered blocks before the PR is created.

Sources, and nothing else:
  verify.json      qa-verify      verdict, replay, confidence, must_haves, judged_blind, caveats
  regression.json  qa-regression  status, results, area, slices, flaky quarantine, baseline
  pre-pr.json      dev-pre-pr     status, checks, warnings count, risk (tier, reasons, facts, gate)
  intake.json      dev-intake     risk_class, risk_reason, area, sensitive
  fix.json         dev-fix        is_root_cause, confidence, masking_suspected, feature_flag (v1.5.0),
                                  design_decisions with must_read_reason human-action-required
  git HEAD         the commit the blocks were rendered for
  deploy adapter   rollback_hint(env) per environment in app.environments (or each --env)

Usage:
  pr_sections.py render --evid-dir DIR --repo DIR [--base B] [--key KEY] [--env NAME ...]
                        [--no-loop] [--out FILE]
  pr_sections.py check  --sections FILE --body-file FILE --repo DIR [--require-block FILE ...]

render exit codes:
  0  {ok:true, commit, base, key, testing_block, risk_block, revert_block, sources, feature_flag,
     manual_steps, rollback}. An ABSENT seam file is stated as absent in its block, never guessed and
     never rendered as passing. A deploy adapter at `none` (the shipped default) or failing for one
     environment is reported in the revert block; it never fails the render.
  1  {ok:false, abort:"pr-sections-unsourced"}: loop mode (the default) and no pre-pr.json in the
     evidence directory — usually a wrong --evid-dir. --no-loop (the ticketless parity draft only)
     renders with every source absent instead.
  2  a seam file is present but unreadable, not JSON or not an object; an artifact's `key` is a
     different string from --key (cross-wired evidence); fix.json's feature_flag is malformed; git
     cannot answer; no base branch; bad usage. Fail closed — callers treat 2 as an abort.

check exit codes:
  0  {ok:true, body_check:"pass"}: each rendered block occurs verbatim in the body, each of the three
     headings is a line exactly once, the sections were rendered for the commit at HEAD, and every
     --require-block file's content occurs verbatim. Lines appended directly under a block are fine.
  1  {ok:false, abort:"pr-body-incomplete", missing, duplicated, stale, reason}
  2  unreadable or malformed sections file, unreadable body or required block, git failure, bad usage.

Artifact free text (reasons, titles, notes, paths, the flag name) is collapsed to one line, stripped of
control characters and backticks, truncated, and set inside a code span, so it can neither forge a
heading nor notify anyone on the host. A rollback hint is set inside a fenced block longer than any
backtick run it contains. Two renders on the same inputs are byte-identical (no timestamps).
Stdlib + lib.adapters only; no vendor name and no environment name lives here.
"""
import argparse
import contextlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FRAMEWORK_ROOT = os.environ.get("LOOPS_REPO_ROOT") or os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))

HEADINGS = {"testing_block": "## Testing", "risk_block": "## Risk", "revert_block": "## How to revert"}
SOURCES = ("verify", "regression", "pre_pr", "intake", "fix")
FILES = {"verify": "verify.json", "regression": "regression.json", "pre_pr": "pre-pr.json",
         "intake": "intake.json", "fix": "fix.json"}
# Formatting constants, not behaviour gates: they bound how much artifact text reaches the body.
MAX_TEXT = 200        # characters of one free-text value
MAX_ITEMS = 20        # entries of one rendered list before "and N more"
MAX_HINT_CHARS = 4000  # characters of one rollback hint
MAX_HINT_LINES = 40    # lines of one rollback hint
NOT_RUN = ("skipped", "unknown", "not-run")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏ -‮⁠-⁤﻿]")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:+/→-]{1,64}$")
NO_ROLLBACK = ("No rollback instructions are configured (the deploy adapter is not available): "
               "ask the deploy owner.")
HUMAN_ONLY = "Rolling back a deploy is a human action; the loop never does it."


class Fatal(Exception):
    """exit 2"""


def emit(doc, code):
    print(json.dumps(doc, indent=1, ensure_ascii=False))
    return code


# ---- hostile-text helpers --------------------------------------------------------------------------
def clean(value):
    """One line, no control characters, no backticks, bounded."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = CTRL_RE.sub(" ", text).replace("`", "'")
    text = " ".join(text.split())
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT - 3].rstrip() + "..."
    return text


def code(value):
    text = clean(value)
    return f"`{text}`" if text else "(blank)"


def tok(value):
    """An enum-like value printed bare when it is a plain token, else as a code span."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = clean(value)
    return text if TOKEN_RE.match(text) else code(value)


def bold(value):
    t = tok(value)
    return f"**{t}**" if not t.startswith("`") and t != "(blank)" else t


def items(values):
    vals = [v for v in values if v is not None] if isinstance(values, list) else []
    if not vals:
        return "none"
    shown = ", ".join(code(v) for v in vals[:MAX_ITEMS])
    if len(vals) > MAX_ITEMS:
        shown += f" and {len(vals) - MAX_ITEMS} more"
    return shown


def fenced(hint, indent):
    text = CTRL_RE.sub("", hint.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    "))
    if len(text) > MAX_HINT_CHARS:
        text = text[:MAX_HINT_CHARS] + "\n..."
    lines = [ln.rstrip() for ln in text.strip("\n").split("\n")]
    if len(lines) > MAX_HINT_LINES:
        lines = lines[:MAX_HINT_LINES] + ["..."]
    runs = [len(m) for m in re.findall(r"`+", text)]
    fence = "`" * max(3, (max(runs) + 1) if runs else 3)
    pad = " " * indent
    # every line is indented, so no hint line can start a heading or close the fence
    return [pad + fence] + [pad + ln if ln else "" for ln in lines] + [pad + fence]


# ---- inputs ----------------------------------------------------------------------------------------
def git_head(repo):
    try:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise Fatal(f"git rev-parse HEAD failed: {e.__class__.__name__}")
    if r.returncode != 0:
        raise Fatal("git rev-parse HEAD failed: " + r.stderr.decode(errors="replace").strip())
    return r.stdout.decode().strip()


def default_branch():
    try:
        sys.path.insert(0, FRAMEWORK_ROOT)
        from lib.adapters.config import cfg
        return cfg("project.default_branch") or None
    except Exception:  # noqa: BLE001
        return None


def load(evid, name):
    path = os.path.join(evid, FILES[name])
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as e:  # noqa: BLE001 — present but unusable: fail closed
        raise Fatal(f"{FILES[name]} is present but unreadable ({e.__class__.__name__}: {e})")
    if not isinstance(doc, dict):
        raise Fatal(f"{FILES[name]} parsed to {type(doc).__name__}, expected an object")
    return doc


def obj(doc, key):
    v = doc.get(key) if isinstance(doc, dict) else None
    return v if isinstance(v, dict) else {}


def feature_flag_of(fix):
    if not fix or fix.get("feature_flag") is None:
        return None
    ff = fix.get("feature_flag")
    if not isinstance(ff, dict):
        raise Fatal("fix.json feature_flag is not an object or null (contracts/dev-fix.json v1.5.0)")
    name = ff.get("name")
    if not isinstance(name, str) or not clean(name) or "\n" in name or "\r" in name:
        raise Fatal("fix.json feature_flag.name is not a non-blank single line (DEV-FIX-FF1)")
    if not isinstance(ff.get("off_restores_previous"), bool):
        raise Fatal("fix.json feature_flag.off_restores_previous is not a boolean")
    out = {"name": clean(name), "off_restores_previous": ff["off_restores_previous"]}
    how = ff.get("how_to_turn_off")
    if isinstance(how, str) and clean(how):
        out["how_to_turn_off"] = clean(how)
    return out


def environments(explicit):
    if explicit:
        return list(explicit)
    try:
        sys.path.insert(0, FRAMEWORK_ROOT)
        from lib.adapters.config import cfg
        envs = cfg("app.environments") or {}
    except Exception:  # noqa: BLE001
        envs = {}
    return list(envs) if isinstance(envs, dict) else []


def rollback_hints(envs):
    """[(env, hint|None, kind, reason)] with kind in hint|unavailable|error. Never raises."""
    sys.path.insert(0, FRAMEWORK_ROOT)
    sys.path.insert(1, os.path.join(FRAMEWORK_ROOT, "lib"))
    try:
        from lib.adapters import get, AdapterUnavailable, AdapterError
    except Exception as e:  # noqa: BLE001
        return [(env, None, "error", f"adapter layer unavailable: {e.__class__.__name__}") for env in envs]
    out = []
    try:
        with contextlib.redirect_stdout(sys.stderr):
            deploy = get("deploy", cache=False)
    except AdapterUnavailable:
        return [(env, None, "unavailable", "adapter-unavailable:deploy") for env in envs]
    except Exception as e:  # noqa: BLE001
        return [(env, None, "error", f"deploy adapter could not be loaded: {e.__class__.__name__}") for env in envs]
    for env in envs:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                hint = deploy.rollback_hint(env)
        except AdapterUnavailable:
            out.append((env, None, "unavailable", "adapter-unavailable:deploy"))
            continue
        except AdapterError as e:
            out.append((env, None, "error", e.__class__.__name__))
            continue
        except Exception as e:  # noqa: BLE001
            out.append((env, None, "error", e.__class__.__name__))
            continue
        if not isinstance(hint, str) or not hint.strip():
            out.append((env, None, "error", "blank or non-string hint"))
        else:
            out.append((env, hint, "hint", None))
    return out


# ---- blocks ----------------------------------------------------------------------------------------
def testing_block(verify, regression, pre_pr):
    lines = [HEADINGS["testing_block"], ""]
    caveats = []
    if verify is None:
        lines.append("- qa-verify: No verify.json in the evidence directory: fix verification is not "
                     "evidenced here.")
    else:
        parts = [f"verdict {bold(verify['verdict'])}" if "verdict" in verify else "verdict not recorded"]
        parts.append(f"replay {code(verify['replay'])}" if verify.get("replay") is not None
                     else "replay not recorded")
        parts.append(f"confidence {tok(verify['confidence'])}" if "confidence" in verify
                     else "confidence not recorded")
        mh = verify.get("must_haves")
        if isinstance(mh, list):
            ok = sum(1 for m in mh if isinstance(m, dict) and m.get("result") == "pass")
            parts.append(f"must-haves {ok}/{len(mh)} passed")
            for m in mh:
                if isinstance(m, dict) and m.get("result") in NOT_RUN:
                    caveats.append(f"qa-verify must-have {code(m.get('check'))} is {tok(m.get('result'))}.")
        else:
            parts.append("must-haves not recorded")
        jb = verify.get("judged_blind")
        parts.append("judged by a fresh context: " + ("yes" if jb is True else "no" if jb is False
                                                      else "not recorded"))
        lines.append("- qa-verify: " + ", ".join(parts))
        if verify.get("replay_note"):
            caveats.append(f"qa-verify replay note: {code(verify['replay_note'])}")
        if verify.get("masking_suspected") is True:
            caveats.append("qa-verify suspects the replay went green by masking the symptom "
                           "(masking_suspected: true).")
        if jb is False:
            caveats.append("qa-verify was not judged by a fresh context (judged_blind: false): the "
                           "producer graded its own fix.")
    if regression is None:
        lines.append("- qa-regression: No regression.json in the evidence directory: the build and "
                     "unit-test gate is not evidenced here.")
    else:
        parts = [f"status {bold(regression['status'])}" if "status" in regression else "status not recorded"]
        res = obj(regression, "results")
        if res:
            parts.append("results {} passed / {} failed / {} total".format(
                tok(res.get("passed", "?")), tok(res.get("failed", "?")), tok(res.get("total", "?"))))
        else:
            parts.append("results not recorded")
        parts.append(f"area {code(regression['area'])}" if regression.get("area") else "area not recorded")
        asr = regression.get("area_slice_run")
        parts.append(f"{len(asr)} area-slice file(s)" if isinstance(asr, list) else "area slice not recorded")
        ssr = regression.get("smoke_slice_run")
        parts.append(f"smoke slice: {items(ssr)}" if isinstance(ssr, list) else "smoke slice not recorded")
        fq = regression.get("flaky_quarantined")
        parts.append(f"{len(fq)} flaky test(s) quarantined" if isinstance(fq, list)
                     else "flaky quarantine not recorded")
        lines.append("- qa-regression: " + ", ".join(parts))
        if regression.get("baseline_missing") is True or obj(regression, "baseline").get("baseline_missing") is True:
            caveats.append("qa-regression recorded no lint baseline (baseline_missing: true): lint before "
                           "and after cannot be compared.")
    if pre_pr is None:
        lines.append("- dev-pre-pr: No pre-pr.json in the evidence directory: no readiness verdict is "
                     "evidenced here.")
    else:
        parts = [f"status {bold(pre_pr['status'])}" if "status" in pre_pr else "status not recorded"]
        checks = obj(pre_pr, "checks")
        for k in sorted(checks):
            parts.append(f"{tok(k)}={tok(checks[k])}")
            if checks[k] in NOT_RUN:
                caveats.append(f"dev-pre-pr check {tok(k)} is {tok(checks[k])}.")
        w = pre_pr.get("warnings")
        parts.append(f"warnings: {len(w)}" if isinstance(w, list) else "warnings: not recorded")
        lines.append("- dev-pre-pr: " + ", ".join(parts))
    if caveats:
        lines += ["", "Not covered / caveats:", ""] + [f"- {c}" for c in caveats]
    return "\n".join(lines) + "\n"


def risk_block(intake, pre_pr, fix):
    lines = [HEADINGS["risk_block"], ""]
    if intake is None:
        lines.append("- Intake: not recorded (no intake.json in the evidence directory).")
    else:
        s = f"- Intake: risk class {bold(intake['risk_class'])}" if intake.get("risk_class") else \
            "- Intake: risk class not recorded"
        if intake.get("risk_reason"):
            s += f" ({code(intake['risk_reason'])})"
        s += f", area {code(intake['area'])}" if intake.get("area") else ", area not recorded"
        sens = intake.get("sensitive")
        s += ", sensitive area: " + ("yes" if sens is True else "no" if sens is False else "not recorded")
        lines.append(s)
    risk = obj(pre_pr, "risk")
    if not risk:
        lines.append("- Tier: not recorded (no dev-pre-pr risk block).")
        lines.append("- Blast radius: not recorded.")
    else:
        triage = risk.get("triage_tier")
        tline = f"- Tier: {bold(risk.get('tier'))}" if risk.get("tier") else "- Tier: not recorded"
        tline += f" (triage {tok(triage) if triage else 'untiered (T1)'}"
        tline += ", raised on the actual diff)" if risk.get("escalated") is True else ", held)"
        lines.append(tline)
        for r in risk.get("reasons") or [] if isinstance(risk.get("reasons"), list) else []:
            lines.append(f"  - {code(r)}")
        facts = obj(risk, "facts")
        if facts:
            def n(k):
                return tok(facts[k]) if isinstance(facts.get(k), int) else "not recorded"
            lines.append(f"- Blast radius: {n('files')} source file(s), {n('test_files')} test file(s), "
                         f"projects {items(facts.get('projects'))}, shared-core hits "
                         f"{items(facts.get('shared_core_hits'))}, denylist hits "
                         f"{items(facts.get('denylist_hits'))}, tests weakened {items(facts.get('tests_weakened'))}")
        else:
            lines.append("- Blast radius: not recorded.")
    if fix is None:
        lines.append("- Fix: not recorded (no fix.json in the evidence directory).")
    else:
        irc = fix.get("is_root_cause")
        s = "- Fix: " + ("root cause" if irc is True else "**symptom-only patch**" if irc is False
                         else "root cause not recorded")
        s += f", confidence {tok(fix['confidence'])}" if "confidence" in fix else ", confidence not recorded"
        ms = fix.get("masking_suspected")
        s += ", masking suspected: " + ("yes" if ms is True else "no" if ms is False else "not recorded")
        lines.append(s)
    gate = obj(risk, "human_gate")
    if gate:
        if gate.get("demanded") is True:
            g = f"- Human gate: demanded ({items(gate.get('triggers'))})."
        else:
            g = "- Human gate: not demanded."
    else:
        g = "- Human gate: not recorded."
    lines.append(g + " Approvals: see Review requirements.")
    return "\n".join(lines) + "\n"


def revert_block(ff, base, commit, pre_pr, fix, hints):
    lines = [HEADINGS["revert_block"], ""]
    if ff is None:
        lines.append("- Feature flag: No feature flag is recorded for this change.")
    else:
        how = f" How: {code(ff['how_to_turn_off'])}" if ff.get("how_to_turn_off") else ""
        if ff["off_restores_previous"]:
            lines.append(f"- Feature flag: turn {code(ff['name'])} off: restores the previous behaviour "
                         f"without a deploy.{how}")
        else:
            lines.append(f"- Feature flag: turning {code(ff['name'])} off undoes only part of this change; "
                         f"also revert the code.{how}")
    s = (f"- Code: revert this PR's merge commit on {code(base)} (the change is commit "
         f"`{commit[:12]}`); ")
    facts = obj(obj(pre_pr, "risk"), "facts")
    hits = facts.get("denylist_hits") if "denylist_hits" in facts else None
    if not isinstance(hits, list):
        s += "the R6 scan of the change set is not recorded here."
    elif not hits:
        s += "no migration, secret, auth, infra or CI file is in the change set (R6 scan clean)."
    else:
        s += (f"**warning:** the change set touches {items(hits)}, which a code revert may not undo "
              "(R6 denylist).")
    lines.append(s)
    manual = []
    for d in (fix or {}).get("design_decisions") or [] if isinstance((fix or {}).get("design_decisions"), list) else []:
        if isinstance(d, dict) and d.get("must_read_reason") == "human-action-required":
            manual.append(clean(d.get("title") or d.get("decision") or "untitled human action") or
                          "untitled human action")
    if manual:
        lines.append("- Manual steps (not undone by a code revert):")
        lines += [f"  - `{m}`" for m in manual]
    else:
        lines.append("- Manual steps: none recorded.")
    rollback = []
    if not hints:
        lines.append("- Deployed build: no environments are configured (app.environments is empty and no "
                     "--env was given): ask the deploy owner.")
    elif all(k == "unavailable" for _, _, k, _ in hints):
        lines.append(f"- Deployed build: {NO_ROLLBACK}")
        rollback = [{"env": e, "source": "unavailable", "reason": r} for e, _, _, r in hints]
    else:
        lines.append("- Deployed build:")
        for env, hint, kind, reason in hints:
            if kind == "hint":
                lines.append(f"  - {code(env)}:")
                lines += fenced(hint, 4)
                rollback.append({"env": env, "source": "deploy.rollback_hint"})
            else:
                why = "the deploy adapter is not available" if kind == "unavailable" else \
                      f"the deploy adapter failed: {code(reason)}"
                lines.append(f"  - {code(env)}: no rollback instructions are available ({why}): ask the "
                             "deploy owner.")
                rollback.append({"env": env, "source": "unavailable", "reason": reason})
    lines += ["", HUMAN_ONLY]
    return "\n".join(lines) + "\n", manual, rollback


# ---- verbs -----------------------------------------------------------------------------------------
def render(a):
    if not os.path.isdir(a.evid_dir):
        raise Fatal(f"--evid-dir {a.evid_dir} is not a directory")
    base = a.base or default_branch()
    if not base:
        raise Fatal("cannot determine the base branch (no --base and no project.default_branch)")
    docs = {name: load(a.evid_dir, name) for name in SOURCES}
    if a.key:
        for name, doc in docs.items():
            if doc is not None and isinstance(doc.get("key"), str) and doc["key"] != a.key:
                raise Fatal(f"{FILES[name]} is for key {doc['key']!r}, not {a.key!r} (cross-wired evidence)")
    in_loop = not a.no_loop
    if in_loop and docs["pre_pr"] is None:
        return emit({"ok": False, "abort": "pr-sections-unsourced",
                     "reason": f"no pre-pr.json in {a.evid_dir}: inside a loop the PR sections are rendered "
                               "from the readiness evidence (a wrong --evid-dir is the usual cause)"}, 1)
    commit = git_head(a.repo)
    ff = feature_flag_of(docs["fix"])
    key = a.key or next((d["key"] for d in docs.values() if d and isinstance(d.get("key"), str)), None)
    hints = rollback_hints(environments(a.env))
    revert, manual, rollback = revert_block(ff, base, commit, docs["pre_pr"], docs["fix"], hints)
    out = {"ok": True, "commit": commit, "base": base, "key": key,
           "testing_block": testing_block(docs["verify"], docs["regression"], docs["pre_pr"]),
           "risk_block": risk_block(docs["intake"], docs["pre_pr"], docs["fix"]),
           "revert_block": revert,
           "sources": {n: ("present" if docs[n] is not None else "absent") for n in SOURCES},
           "feature_flag": ff, "manual_steps": manual, "rollback": rollback}
    if a.out:
        try:
            with open(a.out, "w", encoding="utf-8") as fh:
                json.dump(out, fh, indent=1, ensure_ascii=False)
                fh.write("\n")
        except OSError as e:
            raise Fatal(f"cannot write --out {a.out}: {e}")
    return emit(out, 0)


def occurs(block, body):
    """The block verbatim, ending at a line end (so no rendered line was extended)."""
    core = block.rstrip("\n")
    if not core:
        return False
    start = body.find(core)
    while start != -1:
        end = start + len(core)
        if (start == 0 or body[start - 1] == "\n") and (end == len(body) or body[end] in "\r\n"):
            return True
        start = body.find(core, start + 1)
    return False


def read_text(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except Exception as e:  # noqa: BLE001
        raise Fatal(f"cannot read {what} {path}: {e.__class__.__name__}")


def check(a):
    try:
        sec = json.loads(read_text(a.sections, "--sections"))
    except Fatal:
        raise
    except Exception as e:  # noqa: BLE001
        raise Fatal(f"--sections is not JSON: {e}")
    if not isinstance(sec, dict) or sec.get("ok") is not True or not isinstance(sec.get("commit"), str) \
            or not all(isinstance(sec.get(k), str) and sec[k] for k in HEADINGS):
        raise Fatal("--sections is not a pr_sections.py render result (ok, commit and the three blocks)")
    body = read_text(a.body_file, "--body-file").replace("\r\n", "\n")
    required = [(p, read_text(p, "--require-block")) for p in a.require_block or []]
    head = git_head(a.repo)
    missing, duplicated = [], []
    body_lines = [ln.rstrip() for ln in body.split("\n")]
    for k, heading in HEADINGS.items():
        if not occurs(sec[k].replace("\r\n", "\n"), body):
            missing.append(k)
        if body_lines.count(heading) > 1:
            duplicated.append(heading)
    for path, text in required:
        if not occurs(text.replace("\r\n", "\n"), body):
            missing.append(path)
    stale = sec["commit"] != head
    if missing or duplicated or stale:
        why = []
        if missing:
            why.append("not in the body verbatim: " + ", ".join(missing))
        if duplicated:
            why.append("heading written more than once: " + ", ".join(duplicated))
        if stale:
            why.append(f"rendered for {sec['commit'][:12]}, HEAD is {head[:12]}: re-render after the rebase")
        return emit({"ok": False, "abort": "pr-body-incomplete", "missing": missing, "duplicated": duplicated,
                     "stale": stale, "commit": head, "reason": "; ".join(why)}, 1)
    return emit({"ok": True, "body_check": "pass", "commit": head}, 0)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="verb")
    r = sub.add_parser("render")
    r.add_argument("--evid-dir", required=True)
    r.add_argument("--repo", default=".")
    r.add_argument("--base", default=None)
    r.add_argument("--key", default=None)
    r.add_argument("--env", action="append", default=None)
    r.add_argument("--no-loop", action="store_true")
    r.add_argument("--out", default=None)
    c = sub.add_parser("check")
    c.add_argument("--sections", required=True)
    c.add_argument("--body-file", required=True)
    c.add_argument("--repo", default=".")
    c.add_argument("--require-block", action="append", default=None)
    try:
        a = ap.parse_args()
    except SystemExit:
        return emit({"error": "bad usage"}, 2)
    if a.verb not in ("render", "check"):
        return emit({"error": "bad usage: render | check"}, 2)
    try:
        return render(a) if a.verb == "render" else check(a)
    except Fatal as e:
        return emit({"error": str(e)}, 2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — never crash open
        print(json.dumps({"error": f"pr_sections crashed: {e.__class__.__name__}: {e}"}))
        sys.exit(2)
