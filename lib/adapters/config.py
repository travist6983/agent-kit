"""stack.config.json loader.

    from lib.adapters.config import cfg, repo_root, load_config
    cfg("app.url")                      # dotted lookup, env override LOOPS_CFG__APP__URL
    cfg("adapters.sandbox.config.port_base", 3000)
    repo_root()                         # the directory that holds .claude/loops/

Resolution order for the config file: explicit `path` arg → `$LOOPS_STACK_CONFIG` →
`$AGENT_KIT_OVERLAY/projects/<project>/stack.config.json` → `$AGENT_KIT_OVERLAY/stack.config.json`
→ `<repo_root>/.claude/loops/stack.config.json` (the kit's example).

An overlay is a folder outside the kit that holds one owner's config (see docs/setups.md).
`<project>` is `$AGENT_KIT_PROJECT`, else the name of the git repo the process runs in.

Relative paths inside a kit config resolve against the repo root (`$LOOPS_REPO_ROOT` when set,
else the checkout this file lives in). Relative paths inside an overlay config resolve against
that config's own folder first and fall back to the kit, so an overlay only carries the files
it overrides. An overlay project config with no `app.repo_dir` targets the project's git root.

`_comment_*` keys (used by stack.config.example.json) are stripped on load so the example is
itself a valid config.

Stdlib only; nothing is created on disk at import time.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

CONFIG_RELPATH = Path(".claude") / "loops" / "stack.config.json"
ENV_CONFIG_PATH = "LOOPS_STACK_CONFIG"
ENV_REPO_ROOT = "LOOPS_REPO_ROOT"
ENV_OVERRIDE_PREFIX = "LOOPS_CFG__"
ENV_OVERLAY = "AGENT_KIT_OVERLAY"
ENV_PROJECT = "AGENT_KIT_PROJECT"
GLOBAL_PROJECT = "global"

_MISSING = object()


def repo_root() -> Path:
    """The repo root: `$LOOPS_REPO_ROOT`, else the checkout containing this package (lib/adapters/..)."""
    env = os.environ.get(ENV_REPO_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def overlay_dir() -> Optional[Path]:
    """The overlay folder from `$AGENT_KIT_OVERLAY`, or None when no overlay is set."""
    env = os.environ.get(ENV_OVERLAY)
    return Path(env).expanduser().resolve() if env else None


def _git_toplevel(cwd: Optional[os.PathLike] = None) -> Optional[Path]:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd or os.getcwd(),
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()).resolve() if out.returncode == 0 and out.stdout.strip() else None


def project_root() -> Optional[Path]:
    """The git repo the process runs in, unless that repo is the kit itself."""
    top = _git_toplevel()
    return None if top is None or top == repo_root() else top


def project_name() -> str:
    """`$AGENT_KIT_PROJECT`, else the name of the git repo the process runs in, else `global`."""
    env = os.environ.get(ENV_PROJECT)
    if env:
        return env
    top = project_root()
    return top.name if top is not None else GLOBAL_PROJECT


def _overlay_config_path() -> Optional[Path]:
    overlay = overlay_dir()
    if overlay is None:
        return None
    project = project_name()
    candidates = [overlay / "stack.config.json"]
    if project != GLOBAL_PROJECT:
        candidates.insert(0, overlay / "projects" / project / "stack.config.json")
    return next((c for c in candidates if c.exists()), None)


def default_config_path() -> Path:
    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        return Path(env).expanduser().resolve()
    return _overlay_config_path() or repo_root() / CONFIG_RELPATH


def _is_in_overlay(path: Path) -> bool:
    overlay = overlay_dir()
    return overlay is not None and (path == overlay or overlay in path.parents)


def _strip_comments(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_comments(v) for k, v in node.items() if not str(k).startswith("_comment")}
    if isinstance(node, list):
        return [_strip_comments(v) for v in node]
    return node


def _env_key(dotted: str) -> str:
    return ENV_OVERRIDE_PREFIX + dotted.upper().replace(".", "__").replace("-", "_")


def _parse_env_value(raw: str) -> Any:
    """Env overrides are JSON when they parse as JSON (null, true, 3000, [..], {..}) else strings."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _dig(data: Any, parts: Iterable[str]) -> Any:
    cur = data
    for part in parts:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING
    return cur


class StackConfig:
    """A loaded stack.config.json plus the root its relative paths resolve against."""

    def __init__(self, data: Dict[str, Any], path: Optional[Path] = None, root: Optional[Path] = None,
                 is_overlay: bool = False) -> None:
        self.data = _strip_comments(data or {})
        self.path = Path(path) if path else None
        self.root = Path(root) if root else repo_root()
        self.is_overlay = is_overlay

    # -- lookup ------------------------------------------------------------------------
    def get(self, dotted: str, default: Any = None, env_override: bool = True) -> Any:
        """Dotted lookup with env override. `LOOPS_CFG__APP__URL` beats `app.url` in the file."""
        if env_override:
            env_val = os.environ.get(_env_key(dotted))
            if env_val is not None:
                return _parse_env_value(env_val)
        found = _dig(self.data, dotted.split("."))
        return default if found is _MISSING else found

    def require(self, dotted: str) -> Any:
        found = self.get(dotted, _MISSING)
        if found is _MISSING or found is None:
            raise KeyError("stack.config.json is missing required key %r (or set %s)" % (dotted, _env_key(dotted)))
        return found

    # -- paths -------------------------------------------------------------------------
    def resolve(self, rel: Optional[str], default: Optional[str] = None) -> Optional[Path]:
        """Resolve a path from the config against the repo root. None stays None (unless default)."""
        value = rel if rel not in (None, "") else default
        if value in (None, ""):
            return None
        p = Path(str(value)).expanduser()
        if p.is_absolute():
            return p
        resolved = (self.root / p).resolve()
        if self.is_overlay and not resolved.exists() and (repo_root() / p).exists():
            # An overlay carries only what it overrides; everything else is the kit's.
            return (repo_root() / p).resolve()
        return resolved

    def evidence_root(self) -> Path:
        return self.resolve(self.get("evidence.root"), "runs")  # type: ignore[return-value]

    def repo_dir(self) -> Optional[Path]:
        configured = self.resolve(self.get("app.repo_dir"))
        if configured is None and self.is_overlay:
            return project_root()
        return configured

    # -- adapters ----------------------------------------------------------------------
    def adapter_spec(self, role: str) -> Dict[str, Any]:
        spec = self.get("adapters.%s" % role, None)
        if not isinstance(spec, dict):
            spec = {}
        provider = self.get("adapters.%s.provider" % role, spec.get("provider", "none"))
        config = self.get("adapters.%s.config" % role, spec.get("config") or {})
        return {"provider": provider or "none", "config": dict(config or {})}

    def to_dict(self) -> Dict[str, Any]:
        return {"path": str(self.path) if self.path else None, "root": str(self.root),
                "is_overlay": self.is_overlay, "data": self.data}


# -- module-level API -----------------------------------------------------------------------

_cached: Optional[StackConfig] = None


def load_config(path: Optional[os.PathLike] = None, root: Optional[os.PathLike] = None,
                reload: bool = False) -> StackConfig:
    """Load stack.config.json. The default (no args) is cached per process."""
    global _cached
    if path is None and root is None and _cached is not None and not reload:
        return _cached
    cfg_path = Path(path).expanduser().resolve() if path else default_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(
            "stack.config.json not found at %s (set %s or create it from "
            ".claude/loops/stack.config.example.json)" % (cfg_path, ENV_CONFIG_PATH))
    with open(cfg_path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as e:
            raise ValueError("%s is not valid JSON: %s" % (cfg_path, e)) from e
    is_overlay = _is_in_overlay(cfg_path)
    if root is None and os.environ.get(ENV_REPO_ROOT):
        root = repo_root()
    if root is None and is_overlay:
        root = cfg_path.parent
    if root is None:
        # <root>/.claude/loops/stack.config.json → root is three levels up; otherwise the package's checkout.
        parents = cfg_path.parents
        root = parents[2] if len(parents) > 2 and parents[0].name == "loops" and parents[1].name == ".claude" else repo_root()
    loaded = StackConfig(data, cfg_path, root, is_overlay)
    if path is None:
        _cached = loaded
    return loaded


def cfg(dotted: str, default: Any = None) -> Any:
    """Shortcut: dotted lookup on the default config with env override."""
    return load_config().get(dotted, default)


__all__ = ["StackConfig", "load_config", "cfg", "repo_root", "default_config_path", "overlay_dir",
           "project_root", "project_name", "CONFIG_RELPATH", "ENV_CONFIG_PATH", "ENV_REPO_ROOT",
           "ENV_OVERRIDE_PREFIX", "ENV_OVERLAY", "ENV_PROJECT", "GLOBAL_PROJECT"]
