"""Per-key env overrides for one adapter role's config block.

The config loader honours `LOOPS_CFG__ADAPTERS__<ROLE>__CONFIG__<KEY>` for a dotted lookup
(`stack.get("adapters.sandbox.config.port_base")`), but `stack.adapter_spec(role)` hands back the
file's `config` dict as a whole, so a per-key override never reaches the adapter instance that
`get(role)` builds. The scripts in this skill merge those overrides themselves and pass the result as
`config_override` — the same mechanism the flags use — so a test or a CI job can redirect the
registry, the sandboxes dir, the db hooks or the credential files for one process without editing
`stack.config.json`.

    from role_overrides import env_overrides
    sb = get("sandbox", config_override={**env_overrides("sandbox"), **flag_overrides})

Only the loader's public prefix is used; value parsing mirrors the loader (JSON when it parses, else
the raw string). When the adapter layer merges these itself this module becomes a no-op.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

try:
    from lib.adapters.config import ENV_OVERRIDE_PREFIX
except ImportError:  # pragma: no cover — the scripts put the repo root on sys.path first
    ENV_OVERRIDE_PREFIX = "LOOPS_CFG__"


def _parse(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def env_overrides(role: str) -> Dict[str, Any]:
    """{config_key: value} for every LOOPS_CFG__ADAPTERS__<ROLE>__CONFIG__<KEY> in the environment."""
    prefix = "%sADAPTERS__%s__CONFIG__" % (ENV_OVERRIDE_PREFIX, str(role).upper().replace("-", "_"))
    out: Dict[str, Any] = {}
    for name, raw in os.environ.items():
        if name.startswith(prefix) and len(name) > len(prefix):
            out[name[len(prefix):].lower()] = _parse(raw)
    return out


def role_config(stack, role: str) -> Dict[str, Any]:
    """The role's config block as the adapter will see it: file values with env overrides on top."""
    spec = stack.adapter_spec(role) if stack is not None else {}
    merged = dict((spec or {}).get("config") or {})
    merged.update(env_overrides(role))
    return merged


__all__ = ["env_overrides", "role_config"]
