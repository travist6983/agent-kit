"""notifications: append a timestamped entry to <dir>/<channel>.md."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

from ..base import AdapterError, BaseAdapter, now_iso


class FileNotifications(BaseAdapter):
    provider_name = "file"

    @property
    def dir(self) -> Path:
        raw = self.config.get("dir") or "runs/notifications"
        if self.stack is not None:
            return self.stack.resolve(raw)  # type: ignore[return-value]
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (Path.cwd() / p).resolve()

    def post(self, channel: str, message: str) -> Dict[str, Any]:
        channel = re.sub(r"[^A-Za-z0-9._-]+", "-", str(channel or "general").lstrip("#")).strip("-") or "general"
        if not message:
            raise AdapterError("post needs a message")
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / ("%s.md" % channel)
        at = now_iso()
        text = str(message).rstrip()
        with open(path, "a", encoding="utf-8") as fh:
            if "\n" in text:
                fh.write("- %s —\n" % at)
                for line in text.splitlines():
                    fh.write("    %s\n" % line)
            else:
                fh.write("- %s — %s\n" % (at, text))
        return {"channel": channel, "at": at, "path": str(path), "dry_run": True}


__all__ = ["FileNotifications"]
