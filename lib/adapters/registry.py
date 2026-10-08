"""Provider name → class.

Built-in names are the short ones used in stack.config.json (`file`, `git-local`, `env-file`,
`git-worktree`, `local`, `none`); the module-style names (`file_issue_tracker`,
`git_local_vcs`, ...) are accepted as aliases. Anything containing a colon is a third-party
provider, `"module.path:ClassName"`, imported from `sys.path` (the repo root and `lib/` are both
on it when you go through `cli.py`).
"""
from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional, Type

from .base import OPS, BaseAdapter, UnknownProviderError

# role → {short name: relative module path + class}
BUILTIN: Dict[str, Dict[str, str]] = {
    "issue_tracker":   {"file": ".providers.file_issue_tracker:FileIssueTracker"},
    "vcs_host":        {"git-local": ".providers.git_local_vcs:GitLocalVcs"},
    "test_management": {},
    "analytics":       {},
    "auth_provider":   {"env-file": ".providers.env_file_auth:EnvFileAuth"},
    "ci_cd":           {},
    "deploy":          {},
    "notifications":   {"file": ".providers.file_notifications:FileNotifications"},
    "sandbox":         {"git-worktree": ".providers.git_worktree_sandbox:GitWorktreeSandbox"},
    "evidence_store":  {"local": ".providers.local_evidence:LocalEvidence"},
}

# role-independent aliases (module names / underscore spellings)
ALIASES: Dict[str, str] = {
    "file_issue_tracker": "issue_tracker:file",
    "git_local_vcs": "vcs_host:git-local",
    "git_local": "vcs_host:git-local",
    "git-local-vcs": "vcs_host:git-local",
    "env_file_auth": "auth_provider:env-file",
    "env_file": "auth_provider:env-file",
    "file_notifications": "notifications:file",
    "git_worktree_sandbox": "sandbox:git-worktree",
    "git_worktree": "sandbox:git-worktree",
    "local_evidence": "evidence_store:local",
}

NONE_PATH = ".providers.none:NoneAdapter"


def known_providers(role: Optional[str] = None) -> Dict[str, List[str]]:
    """{role: [provider names]} — what `--list` prints and what a bad-name error cites."""
    roles = [role] if role else list(OPS)
    out: Dict[str, List[str]] = {}
    for r in roles:
        names = ["none"] + sorted(BUILTIN.get(r, {}))
        names += sorted(a for a, target in ALIASES.items() if target.split(":", 1)[0] == r)
        out[r] = names
    return out


def _import_path(path: str) -> Type[Any]:
    mod_name, _, cls_name = path.partition(":")
    if not mod_name or not cls_name:
        raise UnknownProviderError("provider path %r must look like 'module.path:ClassName'" % path)
    if mod_name.startswith("."):
        module = importlib.import_module(mod_name, package=__package__)
    else:
        module = importlib.import_module(mod_name)
    try:
        return getattr(module, cls_name)
    except AttributeError:
        raise UnknownProviderError("module %r has no class %r" % (mod_name, cls_name))


def resolve_provider_class(role: str, name: str) -> Type[Any]:
    """Map (role, provider name) to a class. Raises UnknownProviderError with the known names."""
    if role not in OPS:
        raise UnknownProviderError("unknown adapter role %r; roles: %s" % (role, ", ".join(OPS)),
                                   role=role, known_roles=list(OPS))
    name = (name or "none").strip()
    if name == "none":
        return _import_path(NONE_PATH)
    if name in BUILTIN.get(role, {}):
        return _import_path(BUILTIN[role][name])
    if name in ALIASES:
        alias_role, short = ALIASES[name].split(":", 1)
        if alias_role != role:
            raise UnknownProviderError(
                "provider %r belongs to role %r, not %r; known providers for %s: %s"
                % (name, alias_role, role, role, ", ".join(known_providers(role)[role])),
                role=role, provider=name, known=known_providers(role)[role])
        return _import_path(BUILTIN[alias_role][short])
    if ":" in name:
        try:
            return _import_path(name)
        except ImportError as e:
            raise UnknownProviderError(
                "could not import third-party provider %r for role %s: %s" % (name, role, e),
                role=role, provider=name) from e
    raise UnknownProviderError(
        "unknown provider %r for role %s; known providers: %s (or use 'module.path:ClassName')"
        % (name, role, ", ".join(known_providers(role)[role])),
        role=role, provider=name, known=known_providers(role)[role])


def build(role: str, name: str, config: Optional[Dict[str, Any]] = None, stack: Any = None) -> Any:
    """Instantiate the provider for a role. Third-party classes get the same (role, config, stack) args."""
    cls = resolve_provider_class(role, name)
    instance = cls(role, dict(config or {}), stack) if issubclass(cls, BaseAdapter) else cls(role, dict(config or {}), stack)
    if not getattr(instance, "provider_name", None):
        try:
            instance.provider_name = name
        except Exception:  # pragma: no cover - frozen third-party objects
            pass
    return instance


__all__ = ["BUILTIN", "ALIASES", "known_providers", "resolve_provider_class", "build"]
