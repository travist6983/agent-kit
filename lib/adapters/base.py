"""Adapter contract: role Protocols, normalized dataclasses, the OPS table and the error types.

This module is the single source of truth for *what* each role can do. `cli.py`, `registry.py`,
the `none` provider and the test-suite all read `OPS` instead of carrying their own list.

Stdlib only. Nothing here touches the filesystem, the network or the config.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:  # Python 3.8+
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - 3.7 fallback
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

# --------------------------------------------------------------------------------------
# The role → operations table (spec §3). Keep in sync with the Protocols below.
# --------------------------------------------------------------------------------------

OPS: Dict[str, List[str]] = {
    "issue_tracker": [
        "fetch_ticket", "search", "add_comment", "transition", "assign", "attach", "create_ticket",
    ],
    "vcs_host": [
        "default_branch", "open_pr", "list_prs", "get_pr", "get_pr_diff",
        "post_review_comment", "list_review_threads",
    ],
    "test_management": ["list_cases", "get_case", "create_run", "record_result", "get_run"],
    "analytics": ["query_error_signal", "deployed_version"],
    "auth_provider": ["credentials", "login_hint"],
    "ci_cd": ["build_status", "test_results", "deploy_status"],
    "deploy": ["deployed_version", "rollback_hint"],
    "notifications": ["post"],
    "sandbox": ["provision", "teardown", "list"],
    "evidence_store": ["upload"],
}

ROLES: List[str] = list(OPS)


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------

class AdapterError(Exception):
    """An operation was attempted and failed (CLI exit 1). The message is safe to print."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"ok": False, "error": self.message}
        d.update(self.details)
        return d


class AdapterUnavailable(AdapterError):
    """The role is absent (`provider: none`) or the provider cannot serve this op (CLI exit 3).

    Skills must fail *closed* on this: emit a structured `blocked` with reason
    `adapter-unavailable:<role>` — never crash, never skip silently, never guess.
    """

    def __init__(self, role: str, op: Optional[str] = None, reason: Optional[str] = None) -> None:
        self.role = role
        self.op = op
        self.reason = reason or "provider is 'none' or does not implement this operation"
        msg = "adapter-unavailable:%s" % role
        if op:
            msg += " (%s)" % op
        msg += ": " + self.reason
        super().__init__(msg)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": "adapter-unavailable:%s" % self.role,
            "role": self.role,
            "op": self.op,
            "reason": self.reason,
        }


class UnknownProviderError(AdapterError):
    """A provider name in stack.config.json is not registered and not a `module:Class` path."""


# --------------------------------------------------------------------------------------
# Normalized dataclasses. Every one has to_dict() / from_dict() and tolerates extra keys.
# --------------------------------------------------------------------------------------

class _Normalized:
    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)  # type: ignore[call-overload]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        names = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in (data or {}).items() if k in names})  # type: ignore[call-arg]


@dataclass
class Ticket(_Normalized):
    key: str
    type: Optional[str] = None
    title: str = ""
    body: str = ""
    status: Optional[str] = None
    priority: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    components: List[str] = field(default_factory=list)
    assignee: Optional[str] = None
    reporter: Optional[str] = None
    comments: List[Dict[str, Any]] = field(default_factory=list)      # {author, at, body}
    attachments: List[Dict[str, Any]] = field(default_factory=list)   # {name, url, local_path}
    links: List[Any] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PullRequest(_Normalized):
    id: str
    url: Optional[str] = None
    branch: Optional[str] = None
    base: Optional[str] = None
    title: str = ""
    body: str = ""
    state: str = "open"
    draft: bool = False
    labels: List[str] = field(default_factory=list)
    author: Optional[str] = None
    created_at: Optional[str] = None
    dry_run: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TestCase(_Normalized):
    id: str
    title: str = ""
    suite_id: Optional[str] = None
    section_id: Optional[str] = None
    refs: Optional[str] = None
    steps: Any = None
    expected: Any = None
    priority: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TestRun(_Normalized):
    id: str
    name: str = ""
    project_id: Optional[str] = None
    suite_id: Optional[str] = None
    url: Optional[str] = None
    case_ids: List[Any] = field(default_factory=list)
    status_counts: Dict[str, int] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ErrorSignal(_Normalized):
    signature: str
    count: int = 0
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    version: Optional[str] = None
    samples: List[Any] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BuildStatus(_Normalized):
    ref: str
    id: Optional[str] = None
    status: Optional[str] = None     # queued | running | completed | unknown
    result: Optional[str] = None     # succeeded | failed | canceled | None
    url: Optional[str] = None
    finished_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DeployStatus(_Normalized):
    env: str
    version: Optional[str] = None
    status: Optional[str] = None
    deployed_at: Optional[str] = None
    url: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Sandbox(_Normalized):
    key: str
    dir: str
    branch: str
    slot: int = 0
    ports: Dict[str, int] = field(default_factory=dict)
    urls: Dict[str, Optional[str]] = field(default_factory=dict)
    db: Optional[Dict[str, Any]] = None
    started: bool = False
    created_at: Optional[str] = None
    next_steps: List[str] = field(default_factory=list)


@dataclass
class Credentials(_Normalized):
    role: str
    username: str
    password: str
    extra: Dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:  # never leak the password through logging/tracebacks
        return "Credentials(role=%r, username=%r, password='***', extra_keys=%r)" % (
            self.role, self.username, sorted(self.extra))

    def redacted(self) -> Dict[str, Any]:
        return {"role": self.role, "username": self.username, "password": "***",
                "extra": {k: "***" for k in self.extra}}


# --------------------------------------------------------------------------------------
# Role Protocols. One method per entry in OPS[role]; argument names are the contract.
# --------------------------------------------------------------------------------------

@runtime_checkable
class IssueTracker(Protocol):
    def fetch_ticket(self, key: str) -> Ticket: ...
    def search(self, query: str, limit: int = 50) -> List[Ticket]: ...
    def add_comment(self, key: str, body: str, author: Optional[str] = None) -> Dict[str, Any]: ...
    def transition(self, key: str, status: str) -> Ticket: ...
    def assign(self, key: str, user: Optional[str]) -> Ticket: ...
    def attach(self, key: str, path: str) -> Dict[str, Any]: ...
    def create_ticket(self, draft: Dict[str, Any]) -> Ticket: ...


@runtime_checkable
class VcsHost(Protocol):
    def default_branch(self) -> str: ...
    def open_pr(self, branch: str, base: Optional[str] = None, title: str = "", body: str = "",
                draft: bool = False, labels: Optional[List[str]] = None) -> Dict[str, Any]: ...
    def list_prs(self, filter: Optional[Dict[str, Any]] = None) -> List[PullRequest]: ...
    def get_pr(self, id: str) -> PullRequest: ...
    def get_pr_diff(self, id: str) -> str: ...
    def post_review_comment(self, id: str, path: Optional[str], line: Optional[int],
                            body: str) -> Dict[str, Any]: ...
    def list_review_threads(self, id: str) -> List[Dict[str, Any]]: ...


@runtime_checkable
class TestManagement(Protocol):
    def list_cases(self, scope: Optional[Dict[str, Any]] = None) -> List[TestCase]: ...
    def get_case(self, id: Any) -> TestCase: ...
    def create_run(self, scope: Optional[Dict[str, Any]], name: str,
                   case_ids: Optional[List[Any]] = None) -> TestRun: ...
    def record_result(self, run_id: Any, case_id: Any, status: str, comment: str = "",
                      attachments: Optional[List[str]] = None) -> Dict[str, Any]: ...
    def get_run(self, run_id: Any) -> TestRun: ...


@runtime_checkable
class Analytics(Protocol):
    def query_error_signal(self, signature: str, since: Optional[str] = None,
                           version: Optional[str] = None) -> ErrorSignal: ...
    def deployed_version(self, env: str) -> Optional[str]: ...


@runtime_checkable
class AuthProvider(Protocol):
    def credentials(self, role: str) -> Credentials: ...
    def login_hint(self, role: str) -> str: ...


@runtime_checkable
class CiCd(Protocol):
    def build_status(self, ref: str) -> BuildStatus: ...
    def test_results(self, build_id: str) -> Dict[str, Any]: ...
    def deploy_status(self, env: str) -> DeployStatus: ...


@runtime_checkable
class Deploy(Protocol):
    def deployed_version(self, env: str) -> Optional[str]: ...
    def rollback_hint(self, env: str) -> str: ...


@runtime_checkable
class Notifications(Protocol):
    def post(self, channel: str, message: str) -> Dict[str, Any]: ...


@runtime_checkable
class SandboxProvider(Protocol):
    def provision(self, key: str, ticket_type: str = "fix", base: Optional[str] = None) -> Sandbox: ...
    def teardown(self, key: str) -> Dict[str, Any]: ...
    def list(self) -> List[Dict[str, Any]]: ...


@runtime_checkable
class EvidenceStore(Protocol):
    def upload(self, path: str, key: str) -> Dict[str, Any]: ...


PROTOCOLS: Dict[str, type] = {
    "issue_tracker": IssueTracker,
    "vcs_host": VcsHost,
    "test_management": TestManagement,
    "analytics": Analytics,
    "auth_provider": AuthProvider,
    "ci_cd": CiCd,
    "deploy": Deploy,
    "notifications": Notifications,
    "sandbox": SandboxProvider,
    "evidence_store": EvidenceStore,
}


# --------------------------------------------------------------------------------------
# Base class every shipped provider extends.
# --------------------------------------------------------------------------------------

class BaseAdapter:
    """Holds the role, the provider's `config` block and the loaded stack config.

    Subclasses implement the ops of their role. Ops the provider cannot serve call
    `self.unavailable(op, reason)` instead of returning a guess.
    """

    role: str = ""
    provider_name: str = ""

    def __init__(self, role: str, config: Optional[Dict[str, Any]] = None, stack: Any = None) -> None:
        self.role = role
        self.config = dict(config or {})
        self.stack = stack

    def unavailable(self, op: str, reason: Optional[str] = None) -> "AdapterUnavailable":
        return AdapterUnavailable(self.role, op, reason)

    def describe(self) -> Dict[str, Any]:
        return {"role": self.role, "provider": self.provider_name or type(self).__name__,
                "ops": list(OPS.get(self.role, []))}


def now_iso() -> str:
    """UTC timestamp in the one format every adapter writes."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "OPS", "ROLES", "PROTOCOLS",
    "AdapterError", "AdapterUnavailable", "UnknownProviderError",
    "Ticket", "PullRequest", "TestCase", "TestRun", "ErrorSignal", "BuildStatus",
    "DeployStatus", "Sandbox", "Credentials",
    "IssueTracker", "VcsHost", "TestManagement", "Analytics", "AuthProvider", "CiCd",
    "Deploy", "Notifications", "SandboxProvider", "EvidenceStore",
    "BaseAdapter", "now_iso",
]
