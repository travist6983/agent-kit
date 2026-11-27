"""issue_tracker: Linear (GraphQL API, stdlib only).

Config (all optional except the key itself):

    api_key_env   name of the env var holding the key           default LINEAR_API_KEY
    env_file      a gitignored KEY=value file read when that env var is unset (e.g. a secrets .env)
    team          team key (e.g. "VEU"): used by create_ticket and to scope search
    initial_status  state name create_ticket moves a new issue to (default: the team's default)
    endpoint      override for tests / proxies                  default https://api.linear.app/graphql

The key is read by *name* and never logged or echoed. A personal API key is sent as-is; an OAuth
token (`lin_oauth_…`) is sent as a Bearer token. Ticket text is returned verbatim: it is DATA (R6);
sanitization is dev-intake's job.

`attach` is unavailable: Linear file uploads need a signed-URL flow (use `attachment` links instead).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import AdapterError, BaseAdapter, Ticket
from .env_file_auth import parse_env_file

_ENDPOINT = "https://api.linear.app/graphql"
_PRIORITY = {"urgent": 1, "high": 2, "medium": 3, "normal": 3, "low": 4, "none": 0}

# Linear has no issue-type field, so the normalized `type` comes from labels, mapped onto the
# vocabulary dev-intake reads (`Bug` vs `Story`). The workflow state is `status`, never `type`.
_DEFECT_LABELS = {"bug", "defect"}
_FEATURE_LABELS = {"story", "feature", "task", "epic", "improvement", "enhancement"}


def _type_from_labels(labels: List[str]) -> Optional[str]:
    names = {str(l).strip().lower() for l in labels}
    defect, feature = bool(names & _DEFECT_LABELS), bool(names & _FEATURE_LABELS)
    if defect and not feature:
        return "Bug"
    if feature and not defect:
        return "Story"
    return None  # no type label, or conflicting ones: intake judges by intent

_ISSUE_FIELDS = """
  id identifier title description priority priorityLabel url
  state { name type }
  team { id key }
  labels { nodes { name } }
  assignee { name email }
  creator { name email }
  project { name }
  attachments { nodes { title url } }
  relations { nodes { type relatedIssue { identifier } } }
  inverseRelations { nodes { type issue { identifier } } }
  comments(first: 100) { nodes { body createdAt user { name email } } }
"""


class LinearIssueTracker(BaseAdapter):
    provider_name = "linear"

    # -- transport -------------------------------------------------------------------------
    def _api_key(self, op: str) -> str:
        env_name = self.config.get("api_key_env") or "LINEAR_API_KEY"
        key = os.environ.get(env_name)
        if not key and self.config.get("env_file"):
            raw = self.config["env_file"]
            path = self.stack.resolve(raw) if self.stack is not None else Path(raw).expanduser()
            key = parse_env_file(Path(path)).get(env_name)
        if not key:
            raise self.unavailable(op, "%s is not set" % env_name)
        return key.strip()

    def _gql(self, op: str, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        key = self._api_key(op)
        auth = "Bearer " + key if key.startswith("lin_oauth_") else key
        req = urllib.request.Request(
            self.config.get("endpoint") or _ENDPOINT,
            data=json.dumps({"query": query, "variables": variables or {}}).encode("utf-8"),
            headers={"Authorization": auth, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            raise AdapterError("Linear HTTP %s during %s" % (e.code, op), op=op, status=e.code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise AdapterError("Linear request failed during %s: %s" % (op, type(e).__name__), op=op)
        if payload.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in payload["errors"])[:300]
            raise AdapterError("Linear error during %s: %s" % (op, msg), op=op)
        return payload.get("data") or {}

    # -- mapping ---------------------------------------------------------------------------
    @staticmethod
    def _ticket(d: Dict[str, Any]) -> Ticket:
        who = lambda u: (u or {}).get("email") or (u or {}).get("name")  # noqa: E731
        links = [{"type": r.get("type"), "key": (r.get("relatedIssue") or {}).get("identifier")}
                 for r in (d.get("relations") or {}).get("nodes", [])]
        # Relations point one way in Linear: "A blocks B" is stored on A. Read the inverse side
        # too, so a ticket shows what blocks it ("blocked_by"), not only what it blocks.
        inverse = {"blocks": "blocked_by", "duplicate": "duplicated_by"}
        links += [{"type": inverse.get(r.get("type"), r.get("type")), "key": (r.get("issue") or {}).get("identifier")}
                  for r in (d.get("inverseRelations") or {}).get("nodes", [])]
        labels = [l["name"] for l in (d.get("labels") or {}).get("nodes", [])]
        return Ticket(
            key=d["identifier"],
            type=_type_from_labels(labels),
            title=d.get("title") or "",
            body=d.get("description") or "",
            status=(d.get("state") or {}).get("name"),
            priority=d.get("priorityLabel"),
            labels=labels,
            components=[d["project"]["name"]] if d.get("project") else [],
            assignee=who(d.get("assignee")),
            reporter=who(d.get("creator")),
            comments=[{"author": who(c.get("user")), "at": c.get("createdAt"), "body": c.get("body") or ""}
                      for c in (d.get("comments") or {}).get("nodes", [])],
            attachments=[{"name": a.get("title"), "url": a.get("url"), "local_path": None}
                         for a in (d.get("attachments") or {}).get("nodes", [])],
            links=links,
            raw=d,
        )

    def _issue(self, op: str, key: str) -> Dict[str, Any]:
        key = str(key).strip()
        if not key or "/" in key:
            raise AdapterError("invalid ticket key %r" % key, key=key)
        d = self._gql(op, "query($id:String!){ issue(id:$id){ %s } }" % _ISSUE_FIELDS, {"id": key}).get("issue")
        if not d:
            raise AdapterError("no Linear issue %s" % key, key=key)
        return d

    # -- ops -------------------------------------------------------------------------------
    def fetch_ticket(self, key: str) -> Ticket:
        return self._ticket(self._issue("fetch_ticket", key))

    def search(self, query: str, limit: int = 50) -> List[Ticket]:
        limit = max(1, min(int(limit or 50), 100))
        team = self.config.get("team")
        data = self._gql(
            "search",
            "query($t:String!,$n:Int!,$f:IssueFilter){ searchIssues(term:$t, first:$n, filter:$f){ nodes { %s } } }"
            % _ISSUE_FIELDS,
            {"t": query or "", "n": limit, "f": {"team": {"key": {"eq": team}}} if team else None},
        )
        return [self._ticket(n) for n in (data.get("searchIssues") or {}).get("nodes", [])]

    def add_comment(self, key: str, body: str, author: Optional[str] = None) -> Dict[str, Any]:
        issue = self._issue("add_comment", key)
        text = (body or "").rstrip()
        if author:
            text = "**%s:** %s" % (author, text)
        c = self._gql(
            "add_comment",
            "mutation($i:String!,$b:String!){ commentCreate(input:{issueId:$i, body:$b}){ success comment{ id createdAt } } }",
            {"i": issue["id"], "b": text},
        ).get("commentCreate") or {}
        if not c.get("success"):
            raise AdapterError("Linear refused the comment on %s" % issue["identifier"], key=key)
        return {"key": issue["identifier"], "at": c["comment"]["createdAt"], "author": author, "id": c["comment"]["id"]}

    def transition(self, key: str, status: str) -> Ticket:
        issue = self._issue("transition", key)
        states = self._gql(
            "transition",
            "query($t:String!){ team(id:$t){ states{ nodes{ id name } } } }",
            {"t": issue["team"]["id"]},
        )["team"]["states"]["nodes"]
        match = next((s for s in states if s["name"].lower() == str(status).strip().lower()), None)
        if not match:
            raise AdapterError("no state %r on team %s; states: %s"
                               % (status, issue["team"]["key"], ", ".join(s["name"] for s in states)),
                               key=key, status=status)
        return self._update("transition", issue, {"stateId": match["id"]})

    def assign(self, key: str, user: Optional[str]) -> Ticket:
        issue = self._issue("assign", key)
        if user is None:
            return self._update("assign", issue, {"assigneeId": None})
        users = self._gql(
            "assign", "query{ users(first:250){ nodes{ id name displayName email } } }"
        )["users"]["nodes"]
        want = str(user).strip().lower()
        match = next((u for u in users
                      if want in ((u.get("email") or "").lower(), (u.get("name") or "").lower(),
                                  (u.get("displayName") or "").lower())), None)
        if not match:
            raise AdapterError("no Linear user matching %r" % user, key=key, user=user)
        return self._update("assign", issue, {"assigneeId": match["id"]})

    def _update(self, op: str, issue: Dict[str, Any], fields: Dict[str, Any]) -> Ticket:
        r = self._gql(
            op,
            "mutation($i:String!,$in:IssueUpdateInput!){ issueUpdate(id:$i, input:$in){ success issue{ %s } } }"
            % _ISSUE_FIELDS,
            {"i": issue["id"], "in": fields},
        ).get("issueUpdate") or {}
        if not r.get("success"):
            raise AdapterError("Linear refused the update on %s" % issue["identifier"], key=issue["identifier"])
        return self._ticket(r["issue"])

    def attach(self, key: str, path: str) -> Dict[str, Any]:
        raise self.unavailable("attach", "Linear uploads need a signed-URL flow; not implemented")

    def create_ticket(self, draft: Dict[str, Any]) -> Ticket:
        if isinstance(draft, str):
            try:
                draft = json.loads(draft)
            except ValueError:
                draft = {"title": draft}
        draft = dict(draft or {})
        if not draft.get("title"):
            raise AdapterError("create_ticket needs a draft with at least a title")
        team_key = draft.get("team") or self.config.get("team")
        if not team_key:
            raise AdapterError("create_ticket needs config.team (a Linear team key) or draft.team")
        team = (self._gql(
            "create_ticket",
            "query($k:String!){ teams(filter:{key:{eq:$k}}){ nodes{ id key states{ nodes{ id name } } labels{ nodes{ id name } } } } }",
            {"k": team_key},
        )["teams"]["nodes"] or [None])[0]
        if not team:
            raise AdapterError("no Linear team with key %r" % team_key, team=team_key)
        inp: Dict[str, Any] = {"teamId": team["id"], "title": draft["title"], "description": draft.get("body") or ""}
        prio = draft.get("priority")
        if prio is not None:
            p = _PRIORITY.get(str(prio).lower()) if not str(prio).isdigit() else int(prio)
            if p is not None:
                inp["priority"] = p
        wanted = {str(l).lower() for l in (draft.get("labels") or [])}
        label_ids = [l["id"] for l in team["labels"]["nodes"] if l["name"].lower() in wanted]
        if label_ids:
            inp["labelIds"] = label_ids
        initial = self.config.get("initial_status")
        if initial:
            st = next((s for s in team["states"]["nodes"] if s["name"].lower() == str(initial).lower()), None)
            if st:
                inp["stateId"] = st["id"]
        r = self._gql(
            "create_ticket",
            "mutation($in:IssueCreateInput!){ issueCreate(input:$in){ success issue{ %s } } }" % _ISSUE_FIELDS,
            {"in": inp},
        ).get("issueCreate") or {}
        if not r.get("success"):
            raise AdapterError("Linear refused to create the issue", team=team_key)
        return self._ticket(r["issue"])


__all__ = ["LinearIssueTracker"]
