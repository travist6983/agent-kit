"""Linear issue_tracker provider: offline, urlopen patched.

    python3 -m unittest discover -s lib/adapters/tests
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.adapters import AdapterError, AdapterUnavailable  # noqa: E402
from lib.adapters.registry import build  # noqa: E402

ISSUE = {
    "id": "uuid-1", "identifier": "VEU-7", "title": "Fix login", "description": "It breaks",
    "priority": 2, "priorityLabel": "High", "url": "https://linear.app/x/issue/VEU-7",
    "state": {"name": "Todo", "type": "unstarted"}, "team": {"id": "team-1", "key": "VEU"},
    "labels": {"nodes": [{"name": "bug"}]}, "assignee": {"name": "T", "email": "t@example.com"},
    "creator": None, "project": {"name": "Core"}, "attachments": {"nodes": []},
    "relations": {"nodes": [{"type": "blocks", "relatedIssue": {"identifier": "VEU-9"}}]},
    "inverseRelations": {"nodes": [{"type": "blocks", "issue": {"identifier": "VEU-5"}}]},
    "comments": {"nodes": [{"body": "hi", "createdAt": "2026-01-01T00:00:00Z", "user": {"name": "A", "email": None}}]},
}


class FakeLinear:
    """Records requests; answers by looking at the query text."""

    def __init__(self):
        self.calls = []
        self.relations = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        self.calls.append((req.get_header("Authorization"), body))
        q, v = body["query"], body["variables"]
        if "searchIssues" in q:
            data = {"searchIssues": {"nodes": [ISSUE]}}
        elif "issueUpdate" in q:
            data = {"issueUpdate": {"success": True, "issue": dict(ISSUE, state={"name": "Done", "type": "completed"})}}
        elif "commentCreate" in q:
            data = {"commentCreate": {"success": True, "comment": {"id": "c1", "createdAt": "2026-01-02T00:00:00Z"}}}
        elif "team(id" in q:
            data = {"team": {"states": {"nodes": [{"id": "s1", "name": "Todo"}, {"id": "s2", "name": "Done"}]}}}
        elif "teams(filter" in q:
            data = {"teams": {"nodes": [{"id": "team-1", "key": "VEU", "states": {"nodes": []},
                                         "labels": {"nodes": [{"id": "l1", "name": "bug"}]}}]}}
        elif "issueRelationCreate" in q:
            self.relations.append(v["in"])
            data = {"issueRelationCreate": {"success": True}}
        elif "issueCreate" in q:
            data = {"issueCreate": {"success": True, "issue": ISSUE}}
        elif "users(" in q:
            data = {"users": {"nodes": [{"id": "u1", "name": "T", "displayName": "t", "email": "t@example.com"}]}}
        elif "issue(id" in q:
            other = dict(ISSUE, id="uuid-2", identifier="VEU-2", relations={"nodes": []},
                         inverseRelations={"nodes": []})
            data = {"issue": ISSUE if v["id"] == "VEU-7" else other if v["id"] == "VEU-2" else None}
        else:
            raise AssertionError("unexpected query: " + q)
        return io.BytesIO(json.dumps({"data": data}).encode())


class LinearTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLinear()
        p = mock.patch("urllib.request.urlopen", self.fake)
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {"LINEAR_API_KEY": "lin_api_secret"})
        e.start()
        self.addCleanup(e.stop)
        self.t = build("issue_tracker", "linear", {"team": "VEU"})

    def test_fetch_normalizes(self):
        t = self.t.fetch_ticket("VEU-7")
        self.assertEqual((t.key, t.title, t.status, t.priority), ("VEU-7", "Fix login", "Todo", "High"))
        self.assertEqual(t.labels, ["bug"])
        self.assertEqual(t.assignee, "t@example.com")
        self.assertEqual(t.comments[0]["author"], "A")
        self.assertEqual(self.fake.calls[0][0], "lin_api_secret")  # personal key: no Bearer
        self.assertEqual(t.links, [{"type": "blocks", "key": "VEU-9"}, {"type": "blocked_by", "key": "VEU-5"}])
        self.assertEqual(t.type, "Bug")  # from the label, not the workflow state ("unstarted")

    def test_type_from_labels(self):
        from lib.adapters.providers.linear_issue_tracker import _type_from_labels
        self.assertEqual(_type_from_labels(["story", "phase-1a", "Feature"]), "Story")
        self.assertEqual(_type_from_labels(["Bug"]), "Bug")
        self.assertIsNone(_type_from_labels(["phase-1a"]))
        self.assertIsNone(_type_from_labels(["bug", "feature"]))  # conflicting: intake decides

    def test_oauth_token_uses_bearer(self):
        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "lin_oauth_x"}):
            self.t.fetch_ticket("VEU-7")
        self.assertEqual(self.fake.calls[0][0], "Bearer lin_oauth_x")

    def test_missing_issue_is_error(self):
        with self.assertRaises(AdapterError):
            self.t.fetch_ticket("VEU-99")

    def test_missing_key_fails_closed_without_leaking(self):
        with mock.patch.dict(os.environ, clear=True):
            with self.assertRaises(AdapterUnavailable) as cm:
                self.t.fetch_ticket("VEU-7")
        self.assertIn("LINEAR_API_KEY", str(cm.exception))

    def test_search_scoped_to_team(self):
        out = self.t.search("login", limit=5)
        self.assertEqual([x.key for x in out], ["VEU-7"])
        self.assertEqual(self.fake.calls[0][1]["variables"]["f"], {"team": {"key": {"eq": "VEU"}}})

    def test_comment_transition_assign(self):
        self.assertEqual(self.t.add_comment("VEU-7", "note", author="loop")["id"], "c1")
        self.assertEqual(self.t.transition("VEU-7", "done").status, "Done")
        sent = [c[1]["variables"] for c in self.fake.calls if "issueUpdate" in c[1]["query"]][0]
        self.assertEqual(sent["in"], {"stateId": "s2"})
        self.t.assign("VEU-7", "T@example.com")
        self.t.assign("VEU-7", None)
        with self.assertRaises(AdapterError):
            self.t.transition("VEU-7", "Nonsense")
        with self.assertRaises(AdapterError):
            self.t.assign("VEU-7", "nobody")

    def test_create_ticket(self):
        t = self.t.create_ticket({"title": "New", "body": "b", "priority": "high", "labels": ["bug"]})
        self.assertEqual(t.key, "VEU-7")
        inp = [c[1]["variables"]["in"] for c in self.fake.calls if "issueCreate" in c[1]["query"]][0]
        self.assertEqual((inp["priority"], inp["labelIds"], inp["teamId"]), (2, ["l1"], "team-1"))

    def test_add_link_blocked_by_creates_the_relation_on_the_blocker(self):
        self.t.add_link("VEU-7", "blocked_by", "VEU-2")
        self.assertEqual(self.fake.relations,
                         [{"issueId": "uuid-2", "relatedIssueId": "uuid-1", "type": "blocks"}])

    def test_add_link_blocks_and_related_create_on_the_issue(self):
        self.t.add_link("VEU-7", "related", "VEU-2")
        self.assertEqual(self.fake.relations[0], {"issueId": "uuid-1", "relatedIssueId": "uuid-2", "type": "related"})

    def test_add_link_is_idempotent(self):
        self.t.add_link("VEU-7", "blocks", "VEU-9")  # ISSUE already has blocks VEU-9
        self.assertEqual(self.fake.relations, [])

    def test_add_link_refuses_unknown_types_and_self_links(self):
        with self.assertRaises(AdapterError):
            self.t.add_link("VEU-7", "depends", "VEU-2")
        with self.assertRaises(AdapterError):
            self.t.add_link("VEU-7", "blocks", "VEU-7")

    def test_create_ticket_with_links(self):
        self.t.create_ticket({"title": "New", "links": [{"type": "blocked_by", "key": "VEU-2"}]})
        self.assertEqual(self.fake.relations[0]["type"], "blocks")
        self.assertEqual(self.fake.relations[0]["issueId"], "uuid-2")

    def test_attach_unavailable(self):
        with self.assertRaises(AdapterUnavailable):
            self.t.attach("VEU-7", "/tmp/x")


if __name__ == "__main__":
    unittest.main()
