"""vcs_host: plain git, no remote host. PRs are dry-run files.

    open_pr   → <evidence.root>/prs/<branch-slug>.md (+ .json sidecar) and
                {"url": "file://…", "id": "<branch>", "dry_run": true}
    list_prs / get_pr        read that directory
    get_pr_diff              `git diff <base>...<branch>`
    post_review_comment      appends to <evidence.root>/prs/<slug>.threads.md
    list_review_threads      parses that file back

The git repo is `config.repo_dir` → `app.repo_dir` → the current directory.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import AdapterError, BaseAdapter, PullRequest, now_iso

_THREAD_HEADER = re.compile(r"^## (?P<tid>t\d+) (?P<at>\S+)(?: (?P<loc>\S+))?$")


def slugify(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(branch)).strip("-") or "pr"


class GitLocalVcs(BaseAdapter):
    provider_name = "git-local"

    # -- plumbing ----------------------------------------------------------------------
    @property
    def repo_dir(self) -> Path:
        raw = self.config.get("repo_dir")
        if raw:
            p = self.stack.resolve(raw) if self.stack is not None else Path(raw).expanduser().resolve()
        elif self.stack is not None and self.stack.get("app.repo_dir"):
            p = self.stack.repo_dir()
        else:
            p = Path.cwd()
        p = Path(p)
        if not p.is_dir():
            raise AdapterError("app.repo_dir %s does not exist" % p, repo_dir=str(p))
        return p

    @property
    def prs_dir(self) -> Path:
        raw = self.config.get("prs_dir")
        if raw:
            return self.stack.resolve(raw) if self.stack is not None else Path(raw).expanduser().resolve()  # type: ignore[return-value]
        root = self.stack.evidence_root() if self.stack is not None else (Path.cwd() / "runs")
        return Path(root) / "prs"

    def git(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(["git", "-C", str(self.repo_dir)] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and proc.returncode != 0:
            raise AdapterError("git %s failed: %s" % (" ".join(args), proc.stderr.strip()),
                               returncode=proc.returncode)
        return proc.stdout

    def _paths(self, id: str):
        slug = slugify(id)
        return self.prs_dir / (slug + ".md"), self.prs_dir / (slug + ".json"), self.prs_dir / (slug + ".threads.md")

    # -- ops ---------------------------------------------------------------------------------
    def default_branch(self) -> str:
        configured = self.config.get("default_branch") or (self.stack.get("project.default_branch") if self.stack is not None else None)
        if configured:
            return str(configured)
        head = self.git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False).strip()
        if head.startswith("origin/"):
            return head[len("origin/"):]
        for cand in ("main", "master"):
            if subprocess.run(["git", "-C", str(self.repo_dir), "rev-parse", "--verify", "--quiet", cand],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return cand
        return "main"

    def open_pr(self, branch: str, base: Optional[str] = None, title: str = "", body: str = "",
                draft: bool = False, labels: Optional[List[str]] = None) -> Dict[str, Any]:
        if not branch:
            raise AdapterError("open_pr needs a branch")
        base = base or self.default_branch()
        if isinstance(labels, str):
            labels = [l.strip() for l in labels.split(",") if l.strip()]
        labels = list(labels or [])
        draft = _as_bool(draft)
        log = self.git("log", "--oneline", "%s..%s" % (base, branch), check=False).strip()
        author = self.git("config", "user.name", check=False).strip() or os.environ.get("USER")
        md_path, json_path, _ = self._paths(branch)
        self.prs_dir.mkdir(parents=True, exist_ok=True)
        at = now_iso()
        pr = PullRequest(id=branch, url=md_path.resolve().as_uri(), branch=branch, base=base,
                         title=title or branch, body=body or "", state="open", draft=draft,
                         labels=labels, author=author, created_at=at, dry_run=True,
                         raw={"commits": log.splitlines(), "repo_dir": str(self.repo_dir)})
        lines = [
            "# %s" % pr.title, "",
            "- branch: `%s`" % branch,
            "- base: `%s`" % base,
            "- draft: %s" % ("true" if draft else "false"),
            "- labels: %s" % (", ".join(labels) if labels else "(none)"),
            "- opened: %s" % at,
            "- dry_run: true (git-local provider — no remote host configured)",
            "", "## Body", "", pr.body or "(empty)", "",
            "## Commits (`git log %s..%s --oneline`)" % (base, branch), "",
            "```", log or "(no commits ahead of %s)" % base, "```", "",
        ]
        md_path.write_text("\n".join(lines), encoding="utf-8")
        json_path.write_text(json.dumps(pr.to_dict(), indent=2) + "\n", encoding="utf-8")
        return {"url": pr.url, "id": branch, "dry_run": True, "path": str(md_path), "base": base,
                "title": pr.title, "draft": draft, "labels": labels, "commits": len(pr.raw["commits"])}

    def _read_pr(self, json_path: Path) -> PullRequest:
        with open(json_path, "r", encoding="utf-8") as fh:
            return PullRequest.from_dict(json.load(fh))

    def list_prs(self, filter: Optional[Dict[str, Any]] = None) -> List[PullRequest]:
        if isinstance(filter, str):
            try:
                filter = json.loads(filter)
            except ValueError:
                filter = {"state": filter}
        flt = dict(filter or {})
        out: List[PullRequest] = []
        if not self.prs_dir.exists():
            return out
        for jp in sorted(self.prs_dir.glob("*.json")):
            try:
                pr = self._read_pr(jp)
            except (ValueError, TypeError):
                continue
            if flt.get("state") and flt["state"] != "all" and pr.state != flt["state"]:
                continue
            if flt.get("base") and pr.base != flt["base"]:
                continue
            if flt.get("branch") and pr.branch != flt["branch"]:
                continue
            if "draft" in flt and _as_bool(flt["draft"]) != pr.draft:
                continue
            if flt.get("label") and flt["label"] not in pr.labels:
                continue
            out.append(pr)
        return out

    def get_pr(self, id: str) -> PullRequest:
        _, json_path, _ = self._paths(id)
        if not json_path.exists():
            raise AdapterError("no PR %r under %s" % (id, self.prs_dir), id=id)
        return self._read_pr(json_path)

    def get_pr_diff(self, id: str) -> str:
        pr = self.get_pr(id)
        return self.git("diff", "%s...%s" % (pr.base, pr.branch))

    def post_review_comment(self, id: str, path: Optional[str], line: Optional[int], body: str) -> Dict[str, Any]:
        pr = self.get_pr(id)
        if not body:
            raise AdapterError("post_review_comment needs a body")
        _, _, threads_path = self._paths(pr.id)
        existing = self.list_review_threads(pr.id)
        tid = "t%d" % (len(existing) + 1)
        at = now_iso()
        loc = ("%s:%s" % (path, line)) if path and line not in (None, "") else (path or "")
        with open(threads_path, "a", encoding="utf-8") as fh:
            fh.write("## %s %s%s\n\n%s\n\n" % (tid, at, (" " + loc) if loc else "", str(body).rstrip()))
        return {"id": pr.id, "thread_id": tid, "path": path, "line": line, "at": at,
                "file": str(threads_path), "dry_run": True}

    def list_review_threads(self, id: str) -> List[Dict[str, Any]]:
        _, _, threads_path = self._paths(id)
        if not threads_path.exists():
            return []
        threads: List[Dict[str, Any]] = []
        cur: Optional[Dict[str, Any]] = None
        for raw in threads_path.read_text(encoding="utf-8").splitlines():
            m = _THREAD_HEADER.match(raw)
            if m:
                if cur:
                    cur["body"] = cur["body"].strip()
                    threads.append(cur)
                loc = m.group("loc") or ""
                path, _, line = loc.rpartition(":")
                if not path:
                    path, line = loc, ""
                cur = {"thread_id": m.group("tid"), "at": m.group("at"), "path": path or None,
                       "line": int(line) if line.isdigit() else None, "body": "", "status": "active"}
            elif cur is not None:
                cur["body"] += raw + "\n"
        if cur:
            cur["body"] = cur["body"].strip()
            threads.append(cur)
        return threads


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


__all__ = ["GitLocalVcs", "slugify"]
