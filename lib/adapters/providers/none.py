"""`provider: none` — the role is absent. Every op raises AdapterUnavailable(role, op).

A skill that reaches this must fail closed: structured `blocked`, reason
`adapter-unavailable:<role>`. The CLI maps it to exit 3.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..base import OPS, AdapterUnavailable, BaseAdapter


class NoneAdapter(BaseAdapter):
    provider_name = "none"

    def __init__(self, role: str, config: Optional[Dict[str, Any]] = None, stack: Any = None) -> None:
        if role not in OPS:
            raise AdapterUnavailable(role, None, "unknown role")
        super().__init__(role, config, stack)

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not set on the instance/class — i.e. the role's ops.
        if name in OPS.get(self.__dict__.get("role", ""), []):
            role = self.role

            def _unavailable(*args: Any, **kwargs: Any) -> Any:
                raise AdapterUnavailable(role, name, "adapters.%s.provider is 'none' in stack.config.json" % role)

            _unavailable.__name__ = name
            return _unavailable
        raise AttributeError(name)
