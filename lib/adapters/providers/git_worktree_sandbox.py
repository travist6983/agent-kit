"""sandbox: one git worktree + port offset per ticket key.

    provision(key, ticket_type="fix")
        git worktree add <repo_dir>/../<project.name>-sandboxes/<key> -b <branch_prefix><key>
        slot allocated under an fcntl lock in <evidence.root>/sandboxes.json (slot 1..max_concurrent)
        ports/urls = app.url / app.api_url with (port + slot*port_stride); port_base when a url has no port
        db_clone_command (template vars {key} {slot} {dir} {db}) runs in the new worktree if set
        → Sandbox{dir, branch, slot, ports, urls, db, started: False, next_steps}
    teardown(key)   idempotent, crash-safe: a missing dir or registry entry is not an error
    list()          the registry entries

The sandbox is NOT started: the recipe runs `app.start_command` in `dir` with the ports and
proves reachability itself.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlsplit, urlunsplit

from ..base import AdapterError, BaseAdapter, Sandbox, now_iso


class GitWorktreeSandbox(BaseAdapter):
    provider_name = "git-worktree"

    # -- config ------------------------------------------------------------------------
    def _resolve(self, raw: Optional[str]) -> Optional[Path]:
        if not raw:
            return None
        return self.stack.resolve(raw) if self.stack is not None else Path(raw).expanduser().resolve()

    @property
    def repo_dir(self) -> Path:
        p = self._resolve(self.config.get("repo_dir")) or (self.stack.repo_dir() if self.stack is not None else None) or Path.cwd()
        p = Path(p)
        if not p.is_dir():
            raise AdapterError("app.repo_dir %s does not exist" % p, repo_dir=str(p))
        return p

    @property
    def project_name(self) -> str:
        name = (self.stack.get("project.name") if self.stack is not None else None) or self.repo_dir.name
        return str(name)

    @property
    def sandboxes_dir(self) -> Path:
        explicit = self._resolve(self.config.get("sandboxes_dir"))
        return explicit or (self.repo_dir.parent / ("%s-sandboxes" % self.project_name))

    @property
    def registry_path(self) -> Path:
        explicit = self._resolve(self.config.get("registry_file"))
        if explicit:
            return explicit
        root = self.stack.evidence_root() if self.stack is not None else Path.cwd() / "runs"
        return Path(root) / "sandboxes.json"

    @property
    def port_base(self) -> int:
        return int(self.config.get("port_base") or 3000)

    @property
    def port_stride(self) -> int:
        return int(self.config.get("port_stride") or 10)

    @property
    def max_concurrent(self) -> int:
        return int(self.config.get("max_concurrent") or 3)

    def branch_for(self, key: str, ticket_type: str) -> str:
        prefixes = (self.stack.get("project.branch_prefixes") if self.stack is not None else None) or {}
        prefix = prefixes.get(ticket_type or "fix")
        if prefix is None:
            prefix = prefixes.get("fix", "fix/")
        return "%s%s" % (prefix, key)

    # -- registry --------------------------------------------------------------------------
    @contextmanager
    def _locked_registry(self) -> Iterator[Dict[str, Any]]:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.registry_path.with_suffix(self.registry_path.suffix + ".lock")
        with open(lock_path, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                reg = self._read_registry()
                yield reg
                self._write_registry(reg)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read_registry(self) -> Dict[str, Any]:
        if not self.registry_path.exists():
            return {}
        try:
            with open(self.registry_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (ValueError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_registry(self, reg: Dict[str, Any]) -> None:
        tmp = self.registry_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(reg, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.registry_path)

    # -- ports / urls ------------------------------------------------------------------------
    def _shift(self, url: Optional[str], slot: int) -> Optional[Dict[str, Any]]:
        if not url:
            return None
        parts = urlsplit(str(url))
        port = parts.port or self.port_base
        new_port = port + slot * self.port_stride
        host = parts.hostname or "localhost"
        if ":" in host:
            host = "[%s]" % host
        netloc = "%s:%d" % (host, new_port)
        if parts.username:
            netloc = "%s%s@%s" % (parts.username, (":" + parts.password) if parts.password else "", netloc)
        return {"port": new_port, "url": urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))}

    def _ports_and_urls(self, slot: int):
        app = self._shift(self.stack.get("app.url") if self.stack is not None else None, slot)
        api = self._shift(self.stack.get("app.api_url") if self.stack is not None else None, slot)
        ports: Dict[str, int] = {"offset": slot * self.port_stride}
        urls: Dict[str, Optional[str]] = {}
        if app:
            ports["app"], urls["app"] = app["port"], app["url"]
        else:
            ports["app"] = self.port_base + slot * self.port_stride
            urls["app"] = None
        if api:
            ports["api"], urls["api"] = api["port"], api["url"]
        return ports, urls

    # -- ops ---------------------------------------------------------------------------------
    def provision(self, key: str, ticket_type: str = "fix", base: Optional[str] = None) -> Sandbox:
        key = str(key or "").strip()
        if not key or "/" in key or key.startswith("."):
            raise AdapterError("invalid sandbox key %r" % key, key=key)
        branch = self.branch_for(key, ticket_type)
        wt_dir = self.sandboxes_dir / key
        with self._locked_registry() as reg:
            if key in reg and Path(reg[key].get("dir", "")).is_dir():
                existing = Sandbox.from_dict(reg[key])
                existing.next_steps = self._next_steps(existing)
                return existing  # idempotent re-provision
            reg.pop(key, None)  # stale entry whose dir vanished
            if len(reg) >= self.max_concurrent:
                raise AdapterError(
                    "sandbox cap reached: %d active (max_concurrent=%d); tear one down first"
                    % (len(reg), self.max_concurrent), active=sorted(reg), max_concurrent=self.max_concurrent)
            used = {int(v.get("slot", 0)) for v in reg.values()}
            slot = next(s for s in range(1, self.max_concurrent + 1) if s not in used)
            self._git_worktree_add(wt_dir, branch, base)
            ports, urls = self._ports_and_urls(slot)
            db = None
            cmd = self.config.get("db_clone_command")
            if cmd:
                try:
                    db = self._run_template(cmd, key, slot, wt_dir)
                except AdapterError:
                    self._git_worktree_remove(wt_dir)
                    raise
            sb = Sandbox(key=key, dir=str(wt_dir), branch=branch, slot=slot, ports=ports, urls=urls,
                         db=db, started=False, created_at=now_iso())
            entry = sb.to_dict()
            entry.pop("next_steps", None)
            reg[key] = entry
        sb.next_steps = self._next_steps(sb)
        return sb

    def teardown(self, key: str) -> Dict[str, Any]:
        key = str(key or "").strip()
        result: Dict[str, Any] = {"key": key, "removed_worktree": False, "removed_registry": False, "db": None}
        with self._locked_registry() as reg:
            entry = reg.pop(key, None)
            result["removed_registry"] = entry is not None
            wt_dir = Path(entry["dir"]) if entry and entry.get("dir") else self.sandboxes_dir / key
            result["dir"] = str(wt_dir)
            result["branch"] = (entry or {}).get("branch")
            drop = self.config.get("db_drop_command")
            if drop and entry:
                try:
                    result["db"] = self._run_template(drop, key, int(entry.get("slot", 0)), wt_dir)
                except AdapterError as e:
                    result["db"] = {"error": e.message}
            if wt_dir.exists():
                self._git_worktree_remove(wt_dir)
                result["removed_worktree"] = True
            subprocess.run(["git", "-C", str(self.repo_dir), "worktree", "prune"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if entry and self.config.get("delete_branch_on_teardown") and entry.get("branch"):
                subprocess.run(["git", "-C", str(self.repo_dir), "branch", "-D", entry["branch"]],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                result["deleted_branch"] = True
        return result

    def list(self) -> List[Dict[str, Any]]:
        reg = self._read_registry()
        out = []
        for key in sorted(reg):
            entry = dict(reg[key])
            entry["exists"] = Path(entry.get("dir", "")).is_dir()
            out.append(entry)
        return out

    # -- helpers -----------------------------------------------------------------------------
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.repo_dir)] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _git_worktree_add(self, wt_dir: Path, branch: str, base: Optional[str]) -> None:
        wt_dir.parent.mkdir(parents=True, exist_ok=True)
        if wt_dir.exists():
            # a dir left behind by a crash — remove its worktree registration and start clean
            self._git_worktree_remove(wt_dir)
        exists = self._git("rev-parse", "--verify", "--quiet", "refs/heads/" + branch).returncode == 0
        if exists:
            proc = self._git("worktree", "add", str(wt_dir), branch)
        else:
            args = ["worktree", "add", str(wt_dir), "-b", branch]
            if base:
                args.append(base)
            proc = self._git(*args)
        if proc.returncode != 0:
            raise AdapterError("git worktree add failed: %s" % proc.stderr.strip(), branch=branch, dir=str(wt_dir))

    def _git_worktree_remove(self, wt_dir: Path) -> None:
        proc = self._git("worktree", "remove", "--force", str(wt_dir))
        if proc.returncode != 0 and wt_dir.exists():
            shutil.rmtree(str(wt_dir), ignore_errors=True)
        self._git("worktree", "prune")

    def _run_template(self, cmd: Any, key: str, slot: int, wt_dir: Path) -> Dict[str, Any]:
        db_name = ("%s_%s" % (self.project_name, key)).replace("-", "_").lower()
        vars_ = {"key": key, "slot": str(slot), "dir": str(wt_dir), "db": db_name}
        if isinstance(cmd, list):
            argv = [str(c).format(**vars_) for c in cmd]
            shell = False
            rendered = " ".join(argv)
        else:
            rendered = str(cmd).format(**vars_)
            argv = rendered
            shell = True
        proc = subprocess.run(argv, shell=shell, cwd=str(wt_dir) if wt_dir.exists() else None,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        tail = (proc.stdout or "")[-2000:]
        if proc.returncode != 0:
            raise AdapterError("db command failed (%d): %s" % (proc.returncode, (proc.stderr or "").strip()[-2000:]),
                               command=rendered, returncode=proc.returncode)
        return {"command": rendered, "returncode": proc.returncode, "name": db_name, "stdout_tail": tail}

    def _next_steps(self, sb: Sandbox) -> List[str]:
        start = (self.stack.get("app.start_command") if self.stack is not None else None) or "<app.start_command>"
        steps = ["cd %s" % sb.dir,
                 "start the app on port %s: %s" % (sb.ports.get("app"), start)]
        if sb.urls.get("app"):
            health = (self.stack.get("app.health_path") if self.stack is not None else None) or "/"
            steps.append("prove reachability: curl -fsS %s%s" % (sb.urls["app"].rstrip("/"), health))
        steps.append("teardown in a finally: sandbox teardown %s" % sb.key)
        return steps


__all__ = ["GitWorktreeSandbox"]
