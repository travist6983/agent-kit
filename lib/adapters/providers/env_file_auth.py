"""auth_provider: test-account credentials from the two-file env split.

    env_file         (tracked, no secrets — usernames, hints)     e.g. .env.e2e
    local_env_file   (gitignored — passwords)                      e.g. .env.e2e.local

Later file wins; the process environment wins over both (CI injects secrets that way; disable
with `"process_env": false`). Variables per role, with ROLE upper-cased and `-` → `_`:

    E2E_<ROLE>_USERNAME   E2E_<ROLE>_PASSWORD   E2E_<ROLE>_<ANYTHING>  (→ extra[anything.lower()])

Errors name the *variable*, never a value.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from ..base import AdapterError, BaseAdapter, Credentials

_LINE = re.compile(r"""^\s*(?:export\s+)?(?P<k>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<v>.*)$""")


def parse_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path or not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        v = m.group("v").strip()
        if v[:1] in ("'", '"'):
            quote = v[0]
            end = v.find(quote, 1)
            v = v[1:end] if end > 0 else v[1:]          # quoted: keep as-is, drop anything after the close quote
        else:
            v = re.split(r"\s+#", v, 1)[0].rstrip()  # unquoted: strip a trailing comment
        out[m.group("k")] = v
    return out


class EnvFileAuth(BaseAdapter):
    provider_name = "env-file"

    @property
    def prefix(self) -> str:
        return str(self.config.get("prefix") or "E2E").rstrip("_")

    def _resolve(self, raw: Optional[str]) -> Optional[Path]:
        if not raw:
            return None
        if self.stack is not None:
            return self.stack.resolve(raw)
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (Path.cwd() / p).resolve()

    def _env(self) -> Dict[str, str]:
        merged: Dict[str, str] = {}
        for k in ("env_file", "local_env_file"):
            p = self._resolve(self.config.get(k))
            if p is not None:
                merged.update(parse_env_file(p))
        if self.config.get("process_env", True):
            prefix = self.prefix + "_"
            merged.update({k: v for k, v in os.environ.items() if k.startswith(prefix)})
        return merged

    @staticmethod
    def _norm_role(role: str) -> str:
        return re.sub(r"[^A-Z0-9_]", "_", str(role or "").strip().upper().replace("-", "_"))

    def var_names(self, role: str) -> Dict[str, str]:
        r = self._norm_role(role)
        return {"username": "%s_%s_USERNAME" % (self.prefix, r), "password": "%s_%s_PASSWORD" % (self.prefix, r)}

    def credentials(self, role: str) -> Credentials:
        if not role:
            raise AdapterError("credentials(role) needs a role name")
        env = self._env()
        names = self.var_names(role)
        missing = [n for n in names.values() if not env.get(n)]
        if missing:
            raise AdapterError(
                "no credentials for role %r: set %s in %s (local_env_file wins) or the environment"
                % (role, ", ".join(missing), " / ".join(str(self.config.get(k)) for k in ("env_file", "local_env_file") if self.config.get(k)) or "an env file"),
                role=role, missing=missing)
        r = self._norm_role(role)
        head = "%s_%s_" % (self.prefix, r)
        extra = {k[len(head):].lower(): v for k, v in env.items()
                 if k.startswith(head) and k not in names.values()}
        return Credentials(role=role, username=env[names["username"]], password=env[names["password"]], extra=extra)

    def login_hint(self, role: str) -> str:
        hint = self.config.get("login_hint")
        if isinstance(hint, dict):
            hint = hint.get(role) or hint.get("default")
        if hint:
            return str(hint)
        names = self.var_names(role)
        return ("Open the app at app.url, sign in with %s / %s via the login form, "
                "then wait for the authenticated landing page." % (names["username"], names["password"]))


__all__ = ["EnvFileAuth", "parse_env_file"]
