"""GitHub vcs_host provider: offline, the gh CLI is replaced by a fake.

    python3 -m unittest discover -s lib/adapters/tests
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.adapters import AdapterError  # noqa: E402
from lib.adapters.registry import build  # noqa: E402

PR = {"number": 7, "url": "https://github.com/o/r/pull/7", "headRefName": "feature/X-1-thing",
      "baseRefName": "main", "title": "feat: X-1", "body": "b", "state": "OPEN", "isDraft": False,
      "labels": [{"name": "agent-loop"}], "author": {"login": "bot"},
      "createdAt": "2026-11-27T00:00:00Z", "headRefOid": "abc123"}


class FakeGh:
    """Stands in for subprocess.run(["gh", ...]); answers by argv."""

    def __init__(self, open_prs=None, labels=None, fail=None):
        self.calls = []
        self.open_prs = list(open_prs or [])
        self.labels = list(labels or [])
        self.fail = fail

    def __call__(self, cmd, cwd=None, input=None, env=None, **kw):
        self.calls.append({"cmd": cmd, "input": input, "env": env})
        a = cmd[1:]
        out = ""
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", self.fail)
        if a[:2] == ["pr", "list"]:
            out = json.dumps(self.open_prs if "--head" not in a or self.open_prs else [])
            if "--json" in a and a[a.index("--json") + 1] == "number":
                out = json.dumps([{"number": 7}])
        elif a[:2] == ["pr", "view"]:
            out = json.dumps(PR)
        elif a[:2] == ["pr", "create"]:
            out = "Creating pull request\nhttps://github.com/o/r/pull/7\n"
        elif a[:2] == ["pr", "diff"]:
            out = "diff --git a/x b/x\n"
        elif a[:2] == ["label", "list"]:
            out = json.dumps([{"name": n} for n in self.labels])
        elif a[:2] == ["label", "create"]:
            out = ""
        elif a[:2] == ["repo", "view"]:
            out = json.dumps({"nameWithOwner": "o/r", "defaultBranchRef": {"name": "trunk"}})
        elif a[:2] == ["api", "graphql"]:
            out = json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"id": "T1", "isResolved": True, "isOutdated": False, "path": "a.py", "line": 3,
                           "comments": {"nodes": [{"databaseId": 11, "body": "fix", "createdAt": "t",
                                                   "author": {"login": "rev"}}]}}]}}}}})
        elif a[0] == "api" and "--paginate" in a:
            out = json.dumps([{"id": 22, "created_at": "t2", "body": "hello", "user": {"login": "u"}}])
        elif a[0] == "api":
            out = json.dumps({"id": 33, "created_at": "t3", "html_url": "https://github.com/o/r/pull/7#c"})
        return subprocess.CompletedProcess(cmd, 0, out, "")


class GitHubVcsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vcs = build("vcs_host", "github", {"repo_dir": self.tmp.name, "repo": "o/r"})

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, fake, fn, *a, **kw):
        with mock.patch("subprocess.run", fake):
            return fn(*a, **kw)

    def argv(self, fake, verb):
        return [c["cmd"] for c in fake.calls if c["cmd"][1:3] == verb]

    def test_open_pr_is_published_with_body_on_stdin_and_creates_missing_label(self):
        fake = FakeGh(labels=[])
        res = self.run_with(fake, self.vcs.open_pr, branch="feature/X-1-thing", base="main",
                            title="feat: X-1", body="# Body\nline", labels=["agent-loop"])
        self.assertEqual(res["url"], "https://github.com/o/r/pull/7")
        self.assertEqual(res["id"], "7")
        self.assertFalse(res["dry_run"])
        self.assertFalse(res["draft"])
        (create,) = self.argv(fake, ["pr", "create"])
        self.assertNotIn("--draft", create)
        self.assertEqual(create[create.index("--head") + 1], "feature/X-1-thing")
        self.assertEqual(create[create.index("--body-file") + 1], "-")
        body_call = [c for c in fake.calls if c["cmd"][1:3] == ["pr", "create"]][0]
        self.assertEqual(body_call["input"], "# Body\nline")
        self.assertEqual(len(self.argv(fake, ["label", "create"])), 1)

    def test_open_pr_draft_only_when_asked(self):
        fake = FakeGh(labels=["agent-loop"])
        self.run_with(fake, self.vcs.open_pr, branch="b", base="main", title="t", body="", draft="true",
                      labels=["agent-loop"])
        (create,) = self.argv(fake, ["pr", "create"])
        self.assertIn("--draft", create)
        self.assertEqual(self.argv(fake, ["label", "create"]), [])

    def test_open_pr_refuses_a_second_open_pr_for_the_branch(self):
        fake = FakeGh(open_prs=[PR])
        with self.assertRaises(AdapterError):
            self.run_with(fake, self.vcs.open_pr, branch="feature/X-1-thing", base="main", title="t")
        self.assertEqual(self.argv(fake, ["pr", "create"]), [])

    def test_get_pr_normalizes(self):
        pr = self.run_with(FakeGh(), self.vcs.get_pr, "7")
        self.assertEqual((pr.id, pr.branch, pr.base, pr.state, pr.draft, pr.labels, pr.author),
                         ("7", "feature/X-1-thing", "main", "open", False, ["agent-loop"], "bot"))
        self.assertFalse(pr.dry_run)

    def test_pr_ids_accept_urls_and_branches(self):
        fake = FakeGh()
        self.run_with(fake, self.vcs.get_pr, "https://github.com/o/r/pull/7")
        self.run_with(fake, self.vcs.get_pr, "feature/X-1-thing")
        views = self.argv(fake, ["pr", "view"])
        self.assertEqual([v[3] for v in views], ["7", "7"])

    def test_list_prs_passes_filters(self):
        fake = FakeGh(open_prs=[PR])
        prs = self.run_with(fake, self.vcs.list_prs, {"state": "open", "base": "main", "label": "agent-loop"})
        self.assertEqual([p.id for p in prs], ["7"])
        (call,) = self.argv(fake, ["pr", "list"])
        for flag, val in (("--state", "open"), ("--base", "main"), ("--label", "agent-loop")):
            self.assertEqual(call[call.index(flag) + 1], val)

    def test_line_comment_targets_the_head_commit(self):
        fake = FakeGh()
        res = self.run_with(fake, self.vcs.post_review_comment, "7", "a.py", 3, "note")
        api = [c for c in fake.calls if c["cmd"][1] == "api"][-1]
        self.assertIn("repos/o/r/pulls/7/comments", api["cmd"])
        self.assertEqual(json.loads(api["input"]),
                         {"body": "note", "path": "a.py", "line": 3, "side": "RIGHT", "commit_id": "abc123"})
        self.assertEqual(res["thread_id"], "33")

    def test_general_comment_goes_to_the_conversation(self):
        fake = FakeGh()
        self.run_with(fake, self.vcs.post_review_comment, "7", None, None, "note")
        api = [c for c in fake.calls if c["cmd"][1] == "api"][-1]
        self.assertIn("repos/o/r/issues/7/comments", api["cmd"])

    def test_list_review_threads_merges_review_and_conversation(self):
        threads = self.run_with(FakeGh(), self.vcs.list_review_threads, "7")
        self.assertEqual([(t["thread_id"], t["status"], t["path"]) for t in threads],
                         [("11", "resolved", "a.py"), ("22", "active", None)])

    def test_default_branch_prefers_config_then_github(self):
        self.assertEqual(self.run_with(FakeGh(), self.vcs.default_branch), "trunk")
        vcs = build("vcs_host", "github", {"repo_dir": self.tmp.name, "default_branch": "dev"})
        self.assertEqual(vcs.default_branch(), "dev")

    def test_auth_failures_are_labelled(self):
        fake = FakeGh(fail="HTTP 401: Bad credentials")
        with self.assertRaises(AdapterError) as ctx:
            self.run_with(fake, self.vcs.get_pr_diff, "7")
        self.assertTrue(ctx.exception.message.startswith("auth-failure:"))
        self.assertTrue(ctx.exception.details["auth"])

    def test_token_env_is_passed_as_gh_token_and_never_echoed(self):
        vcs = build("vcs_host", "github", {"repo_dir": self.tmp.name, "repo": "o/r", "token_env": "MY_TOK"})
        fake = FakeGh()
        with mock.patch.dict(os.environ, {"MY_TOK": "s3cret-value"}):
            self.run_with(fake, vcs.get_pr_diff, "7")
        self.assertEqual(fake.calls[-1]["env"]["GH_TOKEN"], "s3cret-value")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MY_TOK", None)
            with self.assertRaises(AdapterError) as ctx:
                vcs.get_pr_diff("7")
        self.assertIn("MY_TOK", ctx.exception.message)
        self.assertNotIn("s3cret", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
