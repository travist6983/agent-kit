"""evidence_store: keep evidence on the local disk under <evidence.root>/<key>/uploads/."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict

from ..base import AdapterError, BaseAdapter


class LocalEvidence(BaseAdapter):
    provider_name = "local"

    @property
    def root(self) -> Path:
        raw = self.config.get("root")
        if self.stack is not None:
            return self.stack.resolve(raw) if raw else self.stack.evidence_root()  # type: ignore[return-value]
        p = Path(raw or "runs").expanduser()
        return p if p.is_absolute() else (Path.cwd() / p).resolve()

    def upload(self, path: str, key: str) -> Dict[str, Any]:
        src = Path(path).expanduser().resolve()
        if not src.exists():
            raise AdapterError("evidence file %s does not exist" % src, path=str(src))
        key = str(key or "unkeyed").replace("/", "_")
        root = self.root.resolve()
        try:
            src.relative_to(root)
            inside = True
        except ValueError:
            inside = False
        if inside and not self.config.get("always_copy"):
            dest = src  # already under the evidence root — don't duplicate
        else:
            dest_dir = root / key / "uploads"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            if src.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(str(src), str(dest))
            else:
                shutil.copy2(str(src), str(dest))
        return {"url": dest.as_uri(), "local_path": str(dest), "key": key, "copied": dest != src}


__all__ = ["LocalEvidence"]
