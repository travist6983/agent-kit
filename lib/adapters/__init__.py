"""The adapter layer: every external system a loop touches, behind one role per concern.

    import sys; sys.path.insert(0, "<repo root>")
    from lib.adapters import get, AdapterUnavailable, cfg

    tracker = get("issue_tracker")
    try:
        ticket = tracker.fetch_ticket("PROJ-123")
    except AdapterUnavailable as e:
        emit_blocked(reason=e.to_dict()["error"])     # "adapter-unavailable:issue_tracker"

Roles and their operations live in `base.OPS`; providers are chosen by
`.claude/loops/stack.config.json` (see `config.py`, `registry.py`). Nothing here creates files
at import time.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .base import (  # noqa: F401
    OPS, ROLES, PROTOCOLS,
    AdapterError, AdapterUnavailable, UnknownProviderError,
    Ticket, PullRequest, TestCase, TestRun, ErrorSignal, BuildStatus, DeployStatus, Sandbox, Credentials,
    BaseAdapter, now_iso,
)
from .config import StackConfig, cfg, load_config, repo_root  # noqa: F401
from .registry import build, known_providers, resolve_provider_class  # noqa: F401

_instances: Dict[str, Any] = {}


def get(role: str, stack: Optional[StackConfig] = None, config_override: Optional[Dict[str, Any]] = None,
        provider: Optional[str] = None, cache: bool = True) -> Any:
    """Return the adapter instance for `role` per stack.config.json.

    `stack`           an explicitly loaded StackConfig (tests, alternate config files)
    `provider`        override the provider name from the file
    `config_override` merged over the role's `config` block
    Instances from the default config are cached per process; pass cache=False to bypass.
    """
    if role not in OPS:
        raise UnknownProviderError("unknown adapter role %r; roles: %s" % (role, ", ".join(OPS)),
                                   role=role, known_roles=list(OPS))
    use_cache = cache and stack is None and provider is None and not config_override
    if use_cache and role in _instances:
        return _instances[role]
    stack = stack or load_config()
    spec = stack.adapter_spec(role)
    name = provider or spec["provider"]
    config = dict(spec["config"])
    if config_override:
        config.update(config_override)
    instance = build(role, name, config, stack)
    if use_cache:
        _instances[role] = instance
    return instance


def reset_cache() -> None:
    _instances.clear()


__all__ = [
    "get", "reset_cache", "load_config", "cfg", "repo_root", "StackConfig",
    "OPS", "ROLES", "PROTOCOLS",
    "AdapterError", "AdapterUnavailable", "UnknownProviderError",
    "Ticket", "PullRequest", "TestCase", "TestRun", "ErrorSignal", "BuildStatus",
    "DeployStatus", "Sandbox", "Credentials", "BaseAdapter", "now_iso",
    "build", "known_providers", "resolve_provider_class",
]
