#!/usr/bin/env python3
"""Bind dev-pre-pr's readiness verdict to the commit that ships — deterministic, git only.

WHY THIS EXISTS. An approval covers one specific commit. dev-pre-pr judges a working tree that is
usually NOT committed yet (dev-fix never commits; infra-ship formats and commits afterwards), so a
bare HEAD sha cannot bind the verdict: the commit does not exist when the verdict is written. What
can bind it is the CONTENT: a fingerprint of the change set that is identical before and after the
commit when nothing changed in between.

  fingerprint = "sha256:" + sha256 over the sorted lines "<repo-relative path>\\0<git blob id|deleted>"
                joined with "\\n", over every file that differs from merge-base(base, HEAD).
  File mode is not part of it; a symlink is hashed as its link text (what git stores).

  stamp   (dev-pre-pr, FIRST)   fingerprints the worktree vs the merge-base: committed + staged +
          unstaged + untracked, minus report artifacts (the ship_preflight.sh section 7 rule) and,
          in a linked worktree only, the SHIP_SCAFFOLD_PATHS sandbox files. Prints {"judged": {...}}.
  check   (infra-ship, AFTER its commit, BEFORE the push) recomputes the fingerprint FROM THE COMMIT
          (merge-base..HEAD, blob ids from the HEAD tree, nothing excluded — an artifact committed by
          mistake is a mismatch) and compares it with pre-pr.json `judged.change_fingerprint`.

Usage:
  commit_binding.py stamp --repo DIR [--base B]
  commit_binding.py check --pre-pr PATH --repo DIR [--base B] [--key KEY] [--loop | --no-loop]

check exit codes:
  0  bound: the commit is exactly the judged content and the verdict is ready|warnings
     (or --no-loop with no pre-pr.json: bound false, reason not-judged — the ticketless draft ship)
  1  abort "readiness-unbound": pre-pr.json missing (in a loop), unreadable, contract-invalid, not
     ready|warnings, without `judged.change_fingerprint` or `risk.human_approvals_required`, or for
     another key; abort "stale-readiness": the commit differs from what was judged
  2  bad usage or git cannot answer — callers treat 2 as an abort too
--loop is the default (the stricter rule), exactly as verify_gate.py. Stdout is one JSON object.
`pr_body_block` is markdown for the PR body, used verbatim.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

FRAMEWORK_ROOT = os.environ.get("LOOPS_REPO_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
CONTRACT = os.path.join(FRAMEWORK_ROOT, "contracts", "dev-pre-pr.json")
ARTIFACT_RE = re.compile(r"^(plan|TASK|code-review-report|pre-pr-report)\.md$|-report\.md$")


class GitError(Exception):
    pass


def emit(doc, code):
    print(json.dumps(doc, indent=1))
    return code


def default_branch():
    try:
        sys.path.insert(0, FRAMEWORK_ROOT)
        from lib.adapters.config import cfg
        return cfg("project.default_branch") or None
    except Exception:  # noqa: BLE001
        return None


def git(repo, *args, data=None):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=120,
                       input=data)
    if r.returncode != 0:
        raise GitError("git %s failed: %s" % (" ".join(args[:3]), r.stderr.decode(errors="replace").strip()))
    return r.stdout


def zsplit(raw):
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]


def fingerprint(entries):
    lines = sorted(f"{p}\0{b}" for p, b in entries.items())
    return "sha256:" + hashlib.sha256("\n".join(lines).encode("utf-8", "surrogateescape")).hexdigest()


def merge_base(repo, base):
    return git(repo, "merge-base", base, "HEAD").decode().strip()


def head_tree(repo, paths=None):
    """{path: blob id} for HEAD (ls-tree -r)."""
    out = {}
    raw = git(repo, "ls-tree", "-r", "-z", "HEAD")
    for rec in zsplit(raw):
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) >= 3:
            out[path] = parts[2]
    return out if paths is None else {p: out[p] for p in paths if p in out}


def is_linked_worktree(repo):
    g = os.path.join(repo, ".git")
    if not os.path.isfile(g):
        return False
    try:
        with open(g) as fh:
            return fh.read().startswith("gitdir:")
    except OSError:
        return False


def scaffold_paths():
    raw = os.environ.get("SHIP_SCAFFOLD_PATHS", "")
    return {p for p in re.split(r"[\n:]", raw) if p.strip()}


def blob_of_worktree(repo, path):
    full = os.path.join(repo, path)
    if os.path.islink(full):
        return git(repo, "hash-object", "--no-filters", "--stdin",
                   data=os.readlink(full).encode("utf-8", "surrogateescape")).decode().strip()
    if os.path.isdir(full):
        # a gitlink (submodule): the checked-out commit is what would be committed
        return git(full, "rev-parse", "HEAD").decode().strip()
    return None  # hashed in one batch below


def stamp(repo, base):
    mb = merge_base(repo, base)
    names = set(zsplit(git(repo, "diff", "--no-renames", "--name-only", "-z", mb)))
    untracked = set(zsplit(git(repo, "ls-files", "--others", "--exclude-standard", "-z")))
    names |= untracked
    names = {n for n in names if not ARTIFACT_RE.search(n)}
    if is_linked_worktree(repo):
        names -= scaffold_paths()
    entries, batch = {}, []
    for n in sorted(names):
        full = os.path.join(repo, n)
        if not os.path.lexists(full):
            entries[n] = "deleted"
            continue
        b = blob_of_worktree(repo, n)
        if b is None:
            batch.append(n)
        else:
            entries[n] = b
    if batch:
        ids = git(repo, "hash-object", "--stdin-paths",
                  data=("\n".join(batch) + "\n").encode("utf-8", "surrogateescape")).decode().split()
        if len(ids) != len(batch):
            raise GitError("hash-object returned %d ids for %d paths" % (len(ids), len(batch)))
        entries.update(zip(batch, ids))
    head = git(repo, "rev-parse", "HEAD").decode().strip()
    tree = head_tree(repo)
    def differs_from_head(n, b):
        if b == "deleted":
            return n in tree
        return n in untracked or tree.get(n) != b
    dirty = any(differs_from_head(n, b) for n, b in entries.items())
    return {"judged": {"head_commit": head, "merge_base": mb, "dirty": dirty,
                       "change_fingerprint": fingerprint(entries), "files": len(entries)}}


def committed(repo, base):
    mb = merge_base(repo, base)
    names = zsplit(git(repo, "diff", "--no-renames", "--name-only", "-z", mb, "HEAD"))
    tree = head_tree(repo)
    entries = {n: tree.get(n, "deleted") for n in names}
    return mb, entries


def body_block(status, commit, fp, risk, bound):
    lines = ["## Review requirements", ""]
    if not bound:
        lines += ["No readiness verdict covers this commit: it was shipped without a dev-pre-pr run "
                  "(a ticketless draft). Review it as unjudged.",
                  "",
                  "Any approval by the loop's own review is an agent verdict and counts as no human approval.",
                  "",
                  "The number of human approvals is enforced by the host's branch protection, not by the loop."]
        return "\n".join(lines) + "\n"
    tier_line = f"Risk tier: **{risk.get('tier')}**"
    if risk.get("escalated"):
        rs = risk.get("reasons") or []
        tier_line += (f" (raised from triage {risk.get('triage_tier') or 'untiered (T1)'} on the actual diff"
                      + (f": {rs[0]}" if rs else "") + ")")
    lines.append(tier_line)
    lines.append("")
    n = risk.get("human_approvals_required")
    gate = risk.get("human_gate") or {}
    appr = f"Human approvals required before merge: **{n}**"
    if gate.get("demanded"):
        appr += f" (human gate: {', '.join(gate.get('triggers') or [])})"
    lines.append(appr)
    lines.append("")
    lines.append("The loop's own review verdict (approve) is an agent verdict and fills none of them.")
    lines.append("")
    lines.append(f"Readiness verdict ({status}) covers commit `{commit[:12]}` only "
                 f"(change fingerprint `{fp.split(':', 1)[-1][:12]}`). A later push is not covered "
                 "until the gate runs again.")
    lines.append("")
    lines.append("The count is enforced by the host's branch protection, not by the loop.")
    return "\n".join(lines) + "\n"


def unbound(reason, **extra):
    doc = {"ok": False, "abort": "readiness-unbound", "reason": reason}
    doc.update(extra)
    return emit(doc, 1)


def contract_errors(doc):
    try:
        sys.path.insert(0, os.path.join(FRAMEWORK_ROOT, "contracts"))
        import validate  # noqa: E402
        with open(CONTRACT) as fh:
            schema = json.load(fh)
        return validate.validate(schema, doc)
    except Exception as e:  # noqa: BLE001 — cannot validate: fail closed
        return [f"cannot load the dev-pre-pr contract: {e}"]


def check(a):
    head = git(a.repo, "rev-parse", "HEAD").decode().strip()
    in_loop = True if a.loop is None else a.loop
    if not os.path.isfile(a.pre_pr):
        if in_loop:
            return unbound(f"no pre-pr.json at {a.pre_pr}: inside a loop every ship needs a readiness "
                           "verdict bound to its commit", commit=head)
        return emit({"ok": True, "bound": False, "reason": "not-judged", "commit": head,
                     "pr_body_block": body_block(None, head, "", {}, False)}, 0)
    try:
        with open(a.pre_pr) as fh:
            doc = json.load(fh)
    except Exception as e:  # noqa: BLE001
        return unbound(f"pre-pr.json unreadable ({e.__class__.__name__}: {e})", commit=head)
    if not isinstance(doc, dict):
        return unbound(f"pre-pr.json parsed to {type(doc).__name__}, expected object", commit=head)
    status = doc.get("status")
    if status not in ("ready", "warnings"):
        return unbound(f"pre-pr.json status is {status!r}: only ready|warnings may ship", commit=head)
    if a.key and doc.get("key") not in (None, a.key):
        return unbound(f"pre-pr.json is for key {doc.get('key')!r}, not {a.key!r}", commit=head)
    judged = doc.get("judged") if isinstance(doc.get("judged"), dict) else {}
    risk = doc.get("risk") if isinstance(doc.get("risk"), dict) else {}
    fp = judged.get("change_fingerprint")
    if not isinstance(fp, str) or not fp.startswith("sha256:"):
        return unbound("pre-pr.json has no judged.change_fingerprint: the verdict is not bound to any "
                       "content (run commit_binding.py stamp in dev-pre-pr step 1)", commit=head)
    n = risk.get("human_approvals_required")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        return unbound("pre-pr.json has no risk.human_approvals_required: nobody stated how many humans "
                       "must approve (run risk_reclass.py in dev-pre-pr)", commit=head)
    errs = contract_errors(doc)
    if errs:
        return unbound("pre-pr.json violates contracts/dev-pre-pr.json: " + "; ".join(errs[:3]), commit=head)

    mb, entries = committed(a.repo, a.base)
    now = fingerprint(entries)
    if now != fp:
        return emit({"ok": False, "abort": "stale-readiness",
                     "reason": "the commit is not the content the readiness verdict judged: a file changed, "
                               "was added or was left out after dev-pre-pr ran. Re-run the gates on this "
                               "commit; never ship it on the old verdict.",
                     "commit": head, "judged_head_commit": judged.get("head_commit"),
                     "judged_fingerprint": fp, "commit_fingerprint": now,
                     "judged_files": judged.get("files"), "committed_files": sorted(entries)}, 1)
    return emit({"ok": True, "bound": True, "commit": head, "merge_base": mb,
                 "change_fingerprint": fp, "status": status, "tier": risk.get("tier"),
                 "human_approvals_required": n, "human_gate": risk.get("human_gate"),
                 "pr_body_block": body_block(status, head, fp, risk, True)}, 0)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="verb")
    s = sub.add_parser("stamp")
    s.add_argument("--repo", default=".")
    s.add_argument("--base", default=None)
    c = sub.add_parser("check")
    c.add_argument("--pre-pr", required=True)
    c.add_argument("--repo", default=".")
    c.add_argument("--base", default=None)
    c.add_argument("--key", default=None)
    c.add_argument("--loop", dest="loop", action="store_true", default=None)
    c.add_argument("--no-loop", dest="loop", action="store_false")
    try:
        a = ap.parse_args()
    except SystemExit:
        return emit({"error": "bad usage"}, 2)
    if a.verb not in ("stamp", "check"):
        return emit({"error": "bad usage: stamp | check"}, 2)
    a.base = a.base or default_branch()
    if not a.base:
        return emit({"error": "cannot determine the base branch (no --base and no project.default_branch)"}, 2)
    try:
        if a.verb == "stamp":
            return emit(stamp(a.repo, a.base), 0)
        return check(a)
    except GitError as e:
        return emit({"error": str(e)}, 2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — never crash open
        print(json.dumps({"error": f"commit_binding crashed: {e.__class__.__name__}: {e}"}))
        sys.exit(2)
