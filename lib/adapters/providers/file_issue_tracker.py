"""issue_tracker: tickets as files.

    <dir>/<KEY>.json          a normalized Ticket (see base.Ticket) — preferred; what writes produce
    <dir>/<KEY>.md            markdown ticket: the first `# ` line is the title, the rest is the body
    <dir>/<KEY>.comments.md   append-only comment log (one `## <at> — <author>` section per comment)
    <dir>/<KEY>.attachments/  copies of attached files

`transition` / `assign` / `attach` rewrite `<KEY>.json` (a markdown ticket is normalized into a json
sibling on first write; json wins on read from then on). Ticket text is returned verbatim — it is
DATA (R6); sanitization is dev-intake's job.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import AdapterError, BaseAdapter, Ticket, now_iso

_COMMENT_HEADER = re.compile(r"^## (?P<at>\S+) — (?P<author>.*)$")
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$")


class TicketNotFound(AdapterError):
    pass


class FileIssueTracker(BaseAdapter):
    provider_name = "file"

    # -- paths -------------------------------------------------------------------------
    @property
    def dir(self) -> Path:
        raw = self.config.get("dir") or "runs/tickets"
        if self.stack is not None:
            return self.stack.resolve(raw)  # type: ignore[return-value]
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (Path.cwd() / p).resolve()

    def _json_path(self, key: str) -> Path:
        return self.dir / ("%s.json" % key)

    def _md_path(self, key: str) -> Path:
        return self.dir / ("%s.md" % key)

    def _comments_path(self, key: str) -> Path:
        return self.dir / ("%s.comments.md" % key)

    def _check_key(self, key: str) -> str:
        key = str(key).strip()
        if not key or "/" in key or key.startswith("."):
            raise AdapterError("invalid ticket key %r" % key, key=key)
        return key

    # -- read ----------------------------------------------------------------------------
    def _load(self, key: str) -> Ticket:
        jp, mp = self._json_path(key), self._md_path(key)
        if jp.exists():
            with open(jp, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data.setdefault("key", key)
            ticket = Ticket.from_dict(data)
        elif mp.exists():
            text = mp.read_text(encoding="utf-8")
            title, body = _split_markdown(text)
            ticket = Ticket(key=key, title=title, body=body, raw={"source": str(mp)})
        else:
            raise TicketNotFound("ticket %s not found under %s" % (key, self.dir), key=key, dir=str(self.dir))
        file_comments = self._read_comments(key)
        if file_comments:
            ticket.comments = list(ticket.comments) + file_comments
        return ticket

    def _read_comments(self, key: str) -> List[Dict[str, Any]]:
        cp = self._comments_path(key)
        if not cp.exists():
            return []
        comments: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        for line in cp.read_text(encoding="utf-8").splitlines():
            m = _COMMENT_HEADER.match(line)
            if m:
                if current is not None:
                    current["body"] = current["body"].strip()
                    comments.append(current)
                current = {"author": m.group("author").strip(), "at": m.group("at"), "body": ""}
            elif current is not None:
                current["body"] += line + "\n"
        if current is not None:
            current["body"] = current["body"].strip()
            comments.append(current)
        return comments

    def fetch_ticket(self, key: str) -> Ticket:
        return self._load(self._check_key(key))

    def search(self, query: str, limit: int = 50) -> List[Ticket]:
        q = (query or "").lower()
        found: List[Ticket] = []
        if not self.dir.exists():
            return found
        seen = set()
        for p in sorted(self.dir.iterdir()):
            if p.name.endswith(".comments.md") or p.is_dir() or p.name.startswith("."):
                continue
            if p.suffix not in (".json", ".md"):
                continue
            key = p.stem
            if key in seen:
                continue
            seen.add(key)
            try:
                t = self._load(key)
            except (TicketNotFound, ValueError):
                continue
            if not q or q in (t.title or "").lower() or q in (t.body or "").lower() or q in key.lower():
                found.append(t)
            if len(found) >= int(limit or 50):
                break
        return found

    # -- write -------------------------------------------------------------------------
    def _save(self, ticket: Ticket) -> Ticket:
        self.dir.mkdir(parents=True, exist_ok=True)
        data = ticket.to_dict()
        # comments live in the .comments.md log, not in the json, so they are not duplicated on read
        logged = self._read_comments(ticket.key)
        data["comments"] = [c for c in data.get("comments", []) if c not in logged]
        tmp = self._json_path(ticket.key).with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, self._json_path(ticket.key))
        return ticket

    def add_comment(self, key: str, body: str, author: Optional[str] = None) -> Dict[str, Any]:
        key = self._check_key(key)
        self._load(key)  # must exist
        at = now_iso()
        author = author or self.config.get("author") or os.environ.get("LOOP_ID") or "loop"
        cp = self._comments_path(key)
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(cp, "a", encoding="utf-8") as fh:
            fh.write("## %s — %s\n\n%s\n\n" % (at, author, (body or "").rstrip()))
        return {"key": key, "at": at, "author": author, "path": str(cp)}

    def transition(self, key: str, status: str) -> Ticket:
        key = self._check_key(key)
        t = self._load(key)
        previous = t.status
        t.status = status
        t.raw = dict(t.raw or {})
        t.raw.setdefault("history", []).append({"at": now_iso(), "op": "transition", "from": previous, "to": status})
        return self._save(t)

    def assign(self, key: str, user: Optional[str]) -> Ticket:
        key = self._check_key(key)
        t = self._load(key)
        previous = t.assignee
        t.assignee = user
        t.raw = dict(t.raw or {})
        t.raw.setdefault("history", []).append({"at": now_iso(), "op": "assign", "from": previous, "to": user})
        return self._save(t)

    def attach(self, key: str, path: str) -> Dict[str, Any]:
        key = self._check_key(key)
        t = self._load(key)
        src = Path(path).expanduser()
        if not src.is_file():
            raise AdapterError("attachment %s does not exist" % src, path=str(src))
        dest_dir = self.dir / ("%s.attachments" % key)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        shutil.copy2(str(src), str(dest))
        entry = {"name": src.name, "url": dest.resolve().as_uri(), "local_path": str(dest.resolve())}
        t.attachments = [a for a in t.attachments if a.get("name") != src.name] + [entry]
        self._save(t)
        return entry

    def create_ticket(self, draft: Dict[str, Any]) -> Ticket:
        if isinstance(draft, str):
            try:
                draft = json.loads(draft)
            except ValueError:
                draft = {"title": draft}
        draft = dict(draft or {})
        if not draft.get("title"):
            raise AdapterError("create_ticket needs a draft with at least a title")
        self.dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.dir / ".create.lock"
        with open(lock_path, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                key = draft.get("key") or self._allocate_key()
                if self._json_path(key).exists() or self._md_path(key).exists():
                    raise AdapterError("ticket %s already exists" % key, key=key)
                ticket = Ticket.from_dict(dict(draft, key=key))
                ticket.status = ticket.status or self.config.get("initial_status") or "open"
                ticket.type = ticket.type or "bug"
                ticket.raw = dict(ticket.raw or {})
                ticket.raw.setdefault("created_at", now_iso())
                self._save(ticket)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return ticket

    # -- key allocation --------------------------------------------------------------------
    def key_prefix(self) -> str:
        explicit = self.config.get("key_prefix")
        if explicit:
            return str(explicit)
        pattern = self.stack.get("project.ticket_key_pattern") if self.stack is not None else None
        prefix = _literal_prefix(pattern or "")
        return prefix or "PROJ"

    def _allocate_key(self) -> str:
        prefix = self.key_prefix()
        highest = 0
        if self.dir.exists():
            for p in self.dir.iterdir():
                stem = p.name.split(".", 1)[0]
                if stem.startswith(prefix + "-") and stem[len(prefix) + 1:].isdigit():
                    highest = max(highest, int(stem[len(prefix) + 1:]))
        return "%s-%d" % (prefix, highest + 1)


def _split_markdown(text: str):
    lines = text.splitlines()
    title = ""
    body_lines: List[str] = []
    for i, line in enumerate(lines):
        if not title and line.startswith("# "):
            title = line[2:].strip()
            body_lines = lines[i + 1:]
            break
    else:
        body_lines = lines
    return title, "\n".join(body_lines).strip("\n")


def _literal_prefix(pattern: str) -> Optional[str]:
    """`^PROJ-[0-9]+$` → PROJ. A class like `^[A-Z]+-...` has no literal prefix → None."""
    m = re.match(r"^\^?(?P<lit>[A-Z][A-Z0-9]*)-", pattern or "")
    return m.group("lit") if m else None


__all__ = ["FileIssueTracker", "TicketNotFound"]
