"""vcs_host: GitHub, through the `gh` CLI.

The adapter is the only place in the kit allowed to call a vendor CLI; skills reach GitHub
through `cli.py vcs_host <op>` like any other provider.

    open_pr              gh pr create (published unless draft=true); labels are created if missing
    list_prs / get_pr    gh pr list / gh pr view, normalized to PullRequest
    get_pr_diff          gh pr diff
    post_review_comment  a line comment on the PR head when path+line are given, else a PR comment
    list_review_threads  review threads (GraphQL, with resolved state) + PR conversation comments

Auth is gh's own login (`gh auth login`). `config.token_env` may name an env var whose value is
passed to gh as GH_TOKEN (for headless runners); the value is never logged or echoed.

Config (all optional):
    repo        "owner/name"; default: the repo `gh` resolves from the git checkout
    repo_dir    the git checkout gh runs in; default app.repo_dir, else cwd
    gh_bin      the gh executable; default "gh"
    token_env   NAME of an env var holding a token for GH_TOKEN
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import AdapterError, BaseAdapter, PullRequest

_PR_FIELDS = "number,url,headRefName,baseRefName,title,body,state,isDraft,labels,author,createdAt,headRefOid"
_AUTH_HINTS = ("gh auth login", "authentication", "HTTP 401", "Bad credentials", "not logged")

_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line
          comments(first: 1) { nodes { databaseId body createdAt author { login } } }
        }
      }
    }
  }
}
"""


class GitHubVcs(BaseAdapter):
    provider_name = "github"

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
            raise AdapterError("repo_dir %s does not exist" % p, repo_dir=str(p))
        return p

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        name = self.config.get("token_env")
        if name:
            token = os.environ.get(str(name))
            if not token:
                raise AdapterError("vcs_host github: env var %s is not set" % name, auth=True)
            env["GH_TOKEN"] = token
        env.setdefault("GH_PROMPT_DISABLED", "1")
        env.setdefault("NO_COLOR", "1")
        return env

    def gh(self, *args: str, input: Optional[str] = None) -> str:
        cmd = [str(self.config.get("gh_bin") or "gh")] + list(args)
        try:
            proc = subprocess.run(cmd, cwd=str(self.repo_dir), input=input, env=self._env(),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError:
            raise AdapterError("vcs_host github: the gh CLI is not installed or not on PATH")
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            auth = any(h.lower() in err.lower() for h in _AUTH_HINTS)
            prefix = "auth-failure: " if auth else ""
            raise AdapterError("%sgh %s failed: %s" % (prefix, args[0] if args else "", err[:500]),
                               returncode=proc.returncode, auth=auth)
        return proc.stdout

    def gh_json(self, *args: str, input: Optional[str] = None) -> Any:
        out = self.gh(*args, input=input)
        try:
            return json.loads(out) if out.strip() else None
        except ValueError:
            raise AdapterError("gh %s returned non-JSON output" % (args[0] if args else ""))

    def _repo_args(self) -> List[str]:
        return ["--repo", str(self.config["repo"])] if self.config.get("repo") else []

    @property
    def repo(self) -> str:
        if self.config.get("repo"):
            return str(self.config["repo"])
        if not hasattr(self, "_repo_cache"):
            data = self.gh_json("repo", "view", "--json", "nameWithOwner")
            self._repo_cache = str(data["nameWithOwner"])
        return self._repo_cache

    def _number(self, id: Any) -> str:
        """A PR number from a number, '#12', a PR URL, or a branch name."""
        s = str(id).strip().lstrip("#")
        if s.isdigit():
            return s
        if "/pull/" in s:
            tail = s.split("/pull/", 1)[1].split("/", 1)[0]
            if tail.isdigit():
                return tail
        prs = self.gh_json("pr", "list", *self._repo_args(), "--head", s, "--state", "all",
                           "--json", "number", "--limit", "1")
        if not prs:
            raise AdapterError("no GitHub PR for %r" % id, id=str(id))
        return str(prs[0]["number"])

    @staticmethod
    def _normalize(d: Dict[str, Any]) -> PullRequest:
        state = str(d.get("state") or "open").lower()
        return PullRequest(
            id=str(d.get("number")), url=d.get("url"), branch=d.get("headRefName"),
            base=d.get("baseRefName"), title=d.get("title") or "", body=d.get("body") or "",
            state=state, draft=bool(d.get("isDraft")),
            labels=[l.get("name") for l in (d.get("labels") or []) if l.get("name")],
            author=(d.get("author") or {}).get("login"), created_at=d.get("createdAt"),
            dry_run=False, raw={"head_sha": d.get("headRefOid")})

    def _ensure_labels(self, labels: List[str]) -> None:
        existing = self.gh_json("label", "list", *self._repo_args(), "--json", "name", "--limit", "500") or []
        have = {str(l.get("name", "")).lower() for l in existing}
        for name in labels:
            if name.lower() not in have:
                self.gh("label", "create", name, *self._repo_args(),
                        "--description", "Opened by an agent-kit loop", "--color", "5319e7")

    # -- ops ---------------------------------------------------------------------------------
    def default_branch(self) -> str:
        configured = self.config.get("default_branch") or (
            self.stack.get("project.default_branch") if self.stack is not None else None)
        if configured:
            return str(configured)
        data = self.gh_json("repo", "view", *([self.config["repo"]] if self.config.get("repo") else []),
                            "--json", "defaultBranchRef")
        return str((data.get("defaultBranchRef") or {}).get("name") or "main")

    def open_pr(self, branch: str, base: Optional[str] = None, title: str = "", body: str = "",
                draft: bool = False, labels: Optional[List[str]] = None) -> Dict[str, Any]:
        if not branch:
            raise AdapterError("open_pr needs a branch")
        if isinstance(labels, str):
            labels = [l.strip() for l in labels.split(",") if l.strip()]
        labels = list(labels or [])
        draft = _as_bool(draft)
        base = base or self.default_branch()
        open_now = self.list_prs({"state": "open", "branch": branch})
        if open_now:
            raise AdapterError("an open PR already exists for %s: %s" % (branch, open_now[0].url),
                               id=open_now[0].id, url=open_now[0].url)
        if labels:
            self._ensure_labels(labels)
        args = ["pr", "create", *self._repo_args(), "--head", branch, "--base", base,
                "--title", title or branch, "--body-file", "-"]
        if draft:
            args.append("--draft")
        for name in labels:
            args += ["--label", name]
        url = self.gh(*args, input=body or "").strip().splitlines()[-1].strip()
        pr = self.get_pr(url)
        return {"url": pr.url, "id": pr.id, "dry_run": False, "base": pr.base, "title": pr.title,
                "draft": pr.draft, "labels": pr.labels, "branch": pr.branch}

    def list_prs(self, filter: Optional[Dict[str, Any]] = None) -> List[PullRequest]:
        if isinstance(filter, str):
            try:
                filter = json.loads(filter)
            except ValueError:
                filter = {"state": filter}
        flt = dict(filter or {})
        state = str(flt.get("state") or "open").lower()
        args = ["pr", "list", *self._repo_args(), "--json", _PR_FIELDS, "--limit", str(flt.get("limit") or 100),
                "--state", state if state in ("open", "closed", "merged", "all") else "open"]
        if flt.get("base"):
            args += ["--base", str(flt["base"])]
        if flt.get("branch"):
            args += ["--head", str(flt["branch"])]
        if flt.get("label"):
            args += ["--label", str(flt["label"])]
        prs = [self._normalize(d) for d in (self.gh_json(*args) or [])]
        if "draft" in flt:
            prs = [p for p in prs if p.draft == _as_bool(flt["draft"])]
        return prs

    def get_pr(self, id: str) -> PullRequest:
        return self._normalize(self.gh_json("pr", "view", self._number(id), *self._repo_args(),
                                            "--json", _PR_FIELDS))

    def get_pr_diff(self, id: str) -> str:
        return self.gh("pr", "diff", self._number(id), *self._repo_args())

    def post_review_comment(self, id: str, path: Optional[str], line: Optional[int], body: str) -> Dict[str, Any]:
        if not body:
            raise AdapterError("post_review_comment needs a body")
        number = self._number(id)
        if path and line not in (None, ""):
            pr = self.get_pr(number)
            payload = {"body": body, "path": path, "line": int(line), "side": "RIGHT",
                       "commit_id": pr.raw.get("head_sha")}
            data = self.gh_json("api", "--method", "POST", "repos/%s/pulls/%s/comments" % (self.repo, number),
                                "--input", "-", input=json.dumps(payload))
        else:
            data = self.gh_json("api", "--method", "POST", "repos/%s/issues/%s/comments" % (self.repo, number),
                                "--input", "-", input=json.dumps({"body": body}))
        return {"id": number, "thread_id": str(data.get("id")), "path": path, "line": line,
                "at": data.get("created_at"), "url": data.get("html_url"), "dry_run": False}

    def list_review_threads(self, id: str) -> List[Dict[str, Any]]:
        number = self._number(id)
        owner, name = self.repo.split("/", 1)
        threads: List[Dict[str, Any]] = []
        after: Optional[str] = None
        while True:
            args = ["api", "graphql", "-f", "query=" + _THREADS_QUERY, "-F", "owner=" + owner,
                    "-F", "name=" + name, "-F", "number=" + number]
            if after:
                args += ["-F", "after=" + after]
            page = self.gh_json(*args)["data"]["repository"]["pullRequest"]["reviewThreads"]
            for t in page["nodes"]:
                first = ((t.get("comments") or {}).get("nodes") or [{}])[0]
                threads.append({"thread_id": str(first.get("databaseId") or t.get("id")),
                                "at": first.get("createdAt"), "path": t.get("path"), "line": t.get("line"),
                                "body": first.get("body") or "",
                                "author": (first.get("author") or {}).get("login"),
                                "status": "resolved" if t.get("isResolved") else "active"})
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        comments = self.gh_json("api", "--paginate", "repos/%s/issues/%s/comments" % (self.repo, number)) or []
        for c in comments:
            threads.append({"thread_id": str(c.get("id")), "at": c.get("created_at"), "path": None,
                            "line": None, "body": c.get("body") or "",
                            "author": (c.get("user") or {}).get("login"), "status": "active"})
        return threads


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


__all__ = ["GitHubVcs"]
