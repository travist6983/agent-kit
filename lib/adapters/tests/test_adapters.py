"""Adapter layer tests. Stdlib unittest, no network, no shared state.

    python3 -m unittest discover -s lib/adapters/tests
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.adapters import (  # noqa: E402
    OPS, AdapterError, AdapterUnavailable, UnknownProviderError, Credentials, Ticket,
    get, known_providers, load_config,
)
from lib.adapters.config import StackConfig  # noqa: E402
from lib.adapters.providers.none import NoneAdapter  # noqa: E402

CLI = REPO_ROOT / "lib" / "adapters" / "cli.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo)] + list(args), check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout


def make_git_repo(root: Path, name: str = "app") -> Path:
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "loops@example.com")
    _git(repo, "config", "user.name", "Loops Test")
    (repo / "README.md").write_text("# app\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def base_config(root: Path, repo_dir: str = "../app", **adapters) -> dict:
    cfg = {
        "version": "1.0.0",
        "project": {"name": "app", "ticket_key_pattern": "^[A-Z][A-Z0-9]+-[0-9]+$", "default_branch": "main",
                    "branch_prefixes": {"fix": "fix/", "feature": "feature/"}},
        "app": {"repo_dir": repo_dir, "url": "http://localhost:3000", "api_url": "http://localhost:3001"},
        "adapters": {
            "issue_tracker": {"provider": "file", "config": {"dir": "runs/tickets"}},
            "vcs_host": {"provider": "git-local", "config": {}},
            "test_management": {"provider": "none", "config": {}},
            "analytics": {"provider": "none", "config": {}},
            "auth_provider": {"provider": "env-file", "config": {"env_file": ".env.e2e", "local_env_file": ".env.e2e.local"}},
            "ci_cd": {"provider": "none", "config": {}},
            "deploy": {"provider": "none", "config": {}},
            "notifications": {"provider": "file", "config": {"dir": "runs/notifications"}},
            "sandbox": {"provider": "git-worktree", "config": {"port_base": 3000, "port_stride": 10, "max_concurrent": 2}},
            "evidence_store": {"provider": "local", "config": {}},
        },
        "evidence": {"root": "runs"},
    }
    for role, spec in adapters.items():
        cfg["adapters"][role] = spec
    return cfg


class TempRepoCase(unittest.TestCase):
    """A temp 'framework root' with .claude/loops/stack.config.json and a sibling git repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "framework"
        (self.root / ".claude" / "loops").mkdir(parents=True)
        self.app = make_git_repo(Path(self.tmp.name), "app")
        self.config_path = self.root / ".claude" / "loops" / "stack.config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, **adapters) -> StackConfig:
        self.config_path.write_text(json.dumps(base_config(self.root, **adapters), indent=2))
        return load_config(self.config_path, root=self.root)


# --------------------------------------------------------------------------------------
class ConfigTests(TempRepoCase):
    def test_loads_and_resolves_paths(self):
        stack = self.write_config()
        self.assertEqual(stack.get("app.url"), "http://localhost:3000")
        self.assertEqual(stack.root, self.root)
        self.assertEqual(stack.evidence_root(), (self.root / "runs").resolve())
        self.assertEqual(stack.repo_dir(), self.app.resolve())
        self.assertIsNone(stack.get("nope.missing"))
        self.assertEqual(stack.get("nope.missing", 7), 7)

    def test_env_override(self):
        stack = self.write_config()
        with mock.patch.dict(os.environ, {"LOOPS_CFG__APP__URL": "http://localhost:4000",
                                          "LOOPS_CFG__ADAPTERS__SANDBOX__CONFIG__PORT_BASE": "4000",
                                          "LOOPS_CFG__APP__API_URL": "null"}):
            self.assertEqual(stack.get("app.url"), "http://localhost:4000")
            self.assertEqual(stack.get("adapters.sandbox.config.port_base"), 4000)
            self.assertIsNone(stack.get("app.api_url"))
        self.assertEqual(stack.get("app.url"), "http://localhost:3000")

    def test_comment_keys_are_stripped_and_example_loads(self):
        example = REPO_ROOT / ".claude" / "loops" / "stack.config.example.json"
        stack = load_config(example, root=self.root)
        self.assertNotIn("_comment", stack.data)
        self.assertNotIn("_comment_project", stack.data)
        self.assertEqual(stack.adapter_spec("test_management")["provider"], "none")
        self.assertIn(":", stack.adapter_spec("issue_tracker")["provider"])

    def test_default_config_has_every_role_and_no_secrets(self):
        stack = load_config(REPO_ROOT / ".claude" / "loops" / "stack.config.json", root=REPO_ROOT)
        for role in OPS:
            self.assertIn("provider", stack.adapter_spec(role))
        # No secret VALUES: a key may be named password_selector or api_key_env, but no key
        # that names a credential may carry a non-empty string value.
        import re
        text = (REPO_ROOT / ".claude" / "loops" / "stack.config.json").read_text()
        leaks = re.findall(r'"(?:[a-z_]*(?:password|secret|token|api_key))"\s*:\s*"[^"]+"', text, re.I)
        self.assertEqual(leaks, [], f"credential-looking values in stack.config.json: {leaks}")

    def test_missing_file_is_a_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            load_config(self.root / "nope.json")


# --------------------------------------------------------------------------------------
class RegistryTests(TempRepoCase):
    def test_every_role_resolves(self):
        stack = self.write_config()
        for role in OPS:
            adapter = get(role, stack=stack)
            self.assertIsNotNone(adapter, role)
            for op in OPS[role]:
                self.assertTrue(callable(getattr(adapter, op)), "%s.%s" % (role, op))

    def test_none_raises_for_every_op(self):
        for role, ops in OPS.items():
            adapter = NoneAdapter(role, {}, None)
            for op in ops:
                with self.assertRaises(AdapterUnavailable) as cm:
                    getattr(adapter, op)()
                self.assertEqual(cm.exception.role, role)
                self.assertEqual(cm.exception.op, op)
                self.assertEqual(cm.exception.to_dict()["error"], "adapter-unavailable:%s" % role)

    def test_bad_provider_name_lists_known(self):
        stack = self.write_config(issue_tracker={"provider": "acme-tracker", "config": {}})
        with self.assertRaises(UnknownProviderError) as cm:
            get("issue_tracker", stack=stack)
        self.assertIn("file", str(cm.exception))
        self.assertIn("none", str(cm.exception))

    def test_alias_and_wrong_role(self):
        stack = self.write_config(notifications={"provider": "file_notifications", "config": {"dir": "runs/n"}})
        self.assertEqual(get("notifications", stack=stack).provider_name, "file")
        stack = self.write_config(notifications={"provider": "file_issue_tracker", "config": {}})
        with self.assertRaises(UnknownProviderError):
            get("notifications", stack=stack)

    def test_third_party_provider_path(self):
        mod_dir = Path(self.tmp.name) / "thirdparty" / "my_adapters"
        mod_dir.mkdir(parents=True)
        (mod_dir / "__init__.py").write_text("")
        (mod_dir / "linear.py").write_text(
            "from lib.adapters.base import BaseAdapter, Ticket\n"
            "class LinearTracker(BaseAdapter):\n"
            "    provider_name = 'linear'\n"
            "    def fetch_ticket(self, key):\n"
            "        return Ticket(key=key, title='from linear', raw={'team': self.config.get('team')})\n")
        sys.path.insert(0, str(mod_dir.parent))
        try:
            stack = self.write_config(issue_tracker={"provider": "my_adapters.linear:LinearTracker", "config": {"team": "SHOP"}})
            t = get("issue_tracker", stack=stack).fetch_ticket("SHOP-1")
            self.assertEqual(t.title, "from linear")
            self.assertEqual(t.raw["team"], "SHOP")
        finally:
            sys.path.remove(str(mod_dir.parent))
            sys.modules.pop("my_adapters.linear", None)
            sys.modules.pop("my_adapters", None)

    def test_known_providers_shape(self):
        kp = known_providers()
        self.assertEqual(set(kp), set(OPS))
        self.assertEqual(kp["test_management"], ["none"])


# --------------------------------------------------------------------------------------
class FileIssueTrackerTests(TempRepoCase):
    def test_round_trip(self):
        stack = self.write_config()
        tr = get("issue_tracker", stack=stack)
        created = tr.create_ticket({"title": "Login button unresponsive", "body": "Clicking Login does nothing", "type": "bug"})
        self.assertEqual(created.key, "PROJ-1")
        self.assertEqual(created.status, "open")
        self.assertTrue((self.root / "runs" / "tickets" / "PROJ-1.json").exists())

        fetched = tr.fetch_ticket("PROJ-1")
        self.assertEqual(fetched.title, "Login button unresponsive")
        self.assertEqual(fetched.comments, [])

        tr.add_comment("PROJ-1", "Reproduced on main.\nSee video.", author="qa-repro")
        tr.add_comment("PROJ-1", "second")
        fetched = tr.fetch_ticket("PROJ-1")
        self.assertEqual(len(fetched.comments), 2)
        self.assertEqual(fetched.comments[0]["author"], "qa-repro")
        self.assertEqual(fetched.comments[0]["body"], "Reproduced on main.\nSee video.")
        self.assertTrue(fetched.comments[0]["at"].endswith("+00:00"))

        moved = tr.transition("PROJ-1", "in-progress")
        self.assertEqual(moved.status, "in-progress")
        self.assertEqual(tr.fetch_ticket("PROJ-1").status, "in-progress")
        # comments are not duplicated into the json on rewrite
        data = json.loads((self.root / "runs" / "tickets" / "PROJ-1.json").read_text())
        self.assertEqual(data["comments"], [])
        self.assertEqual(len(tr.fetch_ticket("PROJ-1").comments), 2)

        tr.assign("PROJ-1", "owner@example.com")
        self.assertEqual(tr.fetch_ticket("PROJ-1").assignee, "owner@example.com")

        second = tr.create_ticket({"title": "Checkout total wrong", "body": "Off by tax"})
        self.assertEqual(second.key, "PROJ-2")
        hits = tr.search("LOGIN")
        self.assertEqual([t.key for t in hits], ["PROJ-1"])
        self.assertEqual([t.key for t in tr.search("off by")], ["PROJ-2"])
        self.assertEqual(len(tr.search("")), 2)

        with self.assertRaises(AdapterError):
            tr.fetch_ticket("PROJ-999")

    def test_markdown_ticket_and_attach(self):
        stack = self.write_config()
        tdir = self.root / "runs" / "tickets"
        tdir.mkdir(parents=True)
        (tdir / "PROJ-7.md").write_text("# Scorecard blank\n\nSteps:\n1. open\n2. see nothing\n")
        tr = get("issue_tracker", stack=stack)
        t = tr.fetch_ticket("PROJ-7")
        self.assertEqual(t.title, "Scorecard blank")
        self.assertTrue(t.body.startswith("Steps:"))
        shot = Path(self.tmp.name) / "shot.png"
        shot.write_bytes(b"\x89PNG")
        att = tr.attach("PROJ-7", str(shot))
        self.assertTrue(att["url"].startswith("file://"))
        self.assertEqual(tr.fetch_ticket("PROJ-7").attachments[0]["name"], "shot.png")
        self.assertEqual(tr.create_ticket({"title": "n"}).key, "PROJ-8")

    def test_prefix_from_pattern(self):
        cfg = base_config(self.root)
        cfg["project"]["ticket_key_pattern"] = "^SHOP-[0-9]+$"
        self.config_path.write_text(json.dumps(cfg))
        tr = get("issue_tracker", stack=load_config(self.config_path, root=self.root))
        self.assertEqual(tr.create_ticket({"title": "x"}).key, "SHOP-1")


# --------------------------------------------------------------------------------------
class GitLocalVcsTests(TempRepoCase):
    def test_open_list_get_diff_threads(self):
        stack = self.write_config()
        _git(self.app, "checkout", "-q", "-b", "fix/PROJ-1")
        (self.app / "a.txt").write_text("hello\n")
        _git(self.app, "add", "a.txt")
        _git(self.app, "commit", "-q", "-m", "fix: add a")
        _git(self.app, "checkout", "-q", "main")

        vcs = get("vcs_host", stack=stack)
        self.assertEqual(vcs.default_branch(), "main")
        res = vcs.open_pr("fix/PROJ-1", title="Fix PROJ-1", body="Root cause: …", labels=["bug"], draft=True)
        self.assertEqual(res["id"], "fix/PROJ-1")
        self.assertTrue(res["dry_run"])
        self.assertTrue(res["url"].startswith("file://"))
        md = self.root / "runs" / "prs" / "fix-PROJ-1.md"
        self.assertTrue(md.exists())
        text = md.read_text()
        for needle in ("# Fix PROJ-1", "base: `main`", "draft: true", "bug", "fix: add a", "Root cause"):
            self.assertIn(needle, text)

        prs = vcs.list_prs()
        self.assertEqual([p.id for p in prs], ["fix/PROJ-1"])
        self.assertEqual(vcs.list_prs({"draft": False}), [])
        pr = vcs.get_pr("fix/PROJ-1")
        self.assertEqual(pr.base, "main")
        self.assertEqual(pr.labels, ["bug"])
        diff = vcs.get_pr_diff("fix/PROJ-1")
        self.assertIn("+hello", diff)
        self.assertIn("a.txt", diff)

        c = vcs.post_review_comment("fix/PROJ-1", "a.txt", 1, "nit: trailing newline")
        self.assertEqual(c["thread_id"], "t1")
        vcs.post_review_comment("fix/PROJ-1", None, None, "general remark")
        threads = vcs.list_review_threads("fix/PROJ-1")
        self.assertEqual(len(threads), 2)
        self.assertEqual(threads[0]["path"], "a.txt")
        self.assertEqual(threads[0]["line"], 1)
        self.assertEqual(threads[1]["path"], None)
        self.assertEqual(threads[1]["body"], "general remark")
        with self.assertRaises(AdapterError):
            vcs.get_pr("nope")


# --------------------------------------------------------------------------------------
class EnvFileAuthTests(TempRepoCase):
    def test_two_file_precedence_and_extras(self):
        stack = self.write_config()
        (self.root / ".env.e2e").write_text(
            "# tracked\nE2E_ADMIN_USERNAME=admin@example.com\nE2E_ADMIN_PASSWORD=placeholder\n"
            "E2E_ADMIN_ORG=\"Acme Inc\"\nexport E2E_MEMBER_USERNAME=member@example.com\n")
        (self.root / ".env.e2e.local").write_text("E2E_ADMIN_PASSWORD='s3cr3t' # real\nE2E_MEMBER_PASSWORD=pw2\n")
        auth = get("auth_provider", stack=stack)
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in [k for k in os.environ if k.startswith("E2E_")]:
                os.environ.pop(k)
            c = auth.credentials("admin")
            self.assertIsInstance(c, Credentials)
            self.assertEqual(c.username, "admin@example.com")
            self.assertEqual(c.password, "s3cr3t")
            self.assertEqual(c.extra, {"org": "Acme Inc"})
            self.assertNotIn("s3cr3t", repr(c))
            self.assertEqual(auth.credentials("member").password, "pw2")
            with self.assertRaises(AdapterError) as cm:
                auth.credentials("site-owner")
            self.assertIn("E2E_SITE_OWNER_USERNAME", str(cm.exception))
            self.assertNotIn("s3cr3t", str(cm.exception))
            os.environ["E2E_MEMBER_PASSWORD"] = "from-env"
            self.assertEqual(auth.credentials("member").password, "from-env")
        self.assertIn("E2E_ADMIN_USERNAME", auth.login_hint("admin"))
        stack2 = self.write_config(auth_provider={"provider": "env-file", "config": {"login_hint": {"default": "use the form"}}})
        self.assertEqual(get("auth_provider", stack=stack2).login_hint("admin"), "use the form")


# --------------------------------------------------------------------------------------
class SandboxTests(TempRepoCase):
    def test_provision_teardown_idempotent_and_cap(self):
        stack = self.write_config()
        sb = get("sandbox", stack=stack)
        one = sb.provision("PROJ-1")
        self.assertEqual(one.branch, "fix/PROJ-1")
        self.assertEqual(one.slot, 1)
        self.assertEqual(one.ports["app"], 3010)
        self.assertEqual(one.ports["api"], 3011)
        self.assertEqual(one.urls["app"], "http://localhost:3010")
        self.assertFalse(one.started)
        self.assertTrue(Path(one.dir).is_dir())
        self.assertEqual(Path(one.dir).parent.name, "app-sandboxes")
        self.assertEqual(_git(Path(one.dir), "rev-parse", "--abbrev-ref", "HEAD").strip(), "fix/PROJ-1")
        self.assertTrue((self.root / "runs" / "sandboxes.json").exists())
        self.assertTrue(one.next_steps)

        again = sb.provision("PROJ-1")  # idempotent
        self.assertEqual(again.slot, 1)
        self.assertEqual(again.dir, one.dir)

        two = sb.provision("PROJ-2", ticket_type="feature")
        self.assertEqual(two.branch, "feature/PROJ-2")
        self.assertEqual(two.slot, 2)
        self.assertEqual(two.ports["app"], 3020)
        self.assertEqual(len(sb.list()), 2)

        with self.assertRaises(AdapterError) as cm:
            sb.provision("PROJ-3")
        self.assertIn("max_concurrent", str(cm.exception))

        res = sb.teardown("PROJ-1")
        self.assertTrue(res["removed_worktree"])
        self.assertTrue(res["removed_registry"])
        self.assertFalse(Path(one.dir).exists())
        res2 = sb.teardown("PROJ-1")  # idempotent
        self.assertFalse(res2["removed_worktree"])
        self.assertFalse(res2["removed_registry"])
        self.assertEqual(sb.teardown("never-existed")["removed_registry"], False)
        three = sb.provision("PROJ-3")  # slot 1 freed and reused
        self.assertEqual(three.slot, 1)
        sb.teardown("PROJ-2")
        sb.teardown("PROJ-3")
        self.assertEqual(sb.list(), [])
        wt = _git(self.app, "worktree", "list")
        self.assertNotIn("sandboxes", wt)

    def test_crash_safe_teardown_when_dir_vanished(self):
        stack = self.write_config()
        sb = get("sandbox", stack=stack)
        one = sb.provision("PROJ-9")
        import shutil
        shutil.rmtree(one.dir)
        res = sb.teardown("PROJ-9")
        self.assertTrue(res["removed_registry"])
        self.assertFalse(res["removed_worktree"])
        self.assertEqual(sb.list(), [])

    def test_db_clone_command_template(self):
        marker = Path(self.tmp.name) / "db.log"
        stack = self.write_config(sandbox={"provider": "git-worktree", "config": {
            "port_stride": 10, "max_concurrent": 1,
            "db_clone_command": "echo clone {key} slot={slot} db={db} > %s" % marker}})
        sb = get("sandbox", stack=stack)
        one = sb.provision("PROJ-4")
        self.assertEqual(one.db["returncode"], 0)
        self.assertIn("clone PROJ-4 slot=1 db=app_proj_4", marker.read_text())
        sb.teardown("PROJ-4")


# --------------------------------------------------------------------------------------
class SmallProvidersTests(TempRepoCase):
    def test_notifications_and_evidence(self):
        stack = self.write_config()
        n = get("notifications", stack=stack).post("#loop-alerts", "PROJ-1 escalated")
        text = (self.root / "runs" / "notifications" / "loop-alerts.md").read_text()
        self.assertIn("PROJ-1 escalated", text)
        self.assertIn(n["at"], text)
        src = Path(self.tmp.name) / "after.webm"
        src.write_bytes(b"vid")
        up = get("evidence_store", stack=stack).upload(str(src), "PROJ-1")
        self.assertTrue(up["url"].startswith("file://"))
        self.assertTrue(Path(up["local_path"]).exists())
        self.assertIn(str(self.root / "runs" / "PROJ-1"), up["local_path"])


# --------------------------------------------------------------------------------------
class CliTests(TempRepoCase):
    def run_cli(self, *args: str, env_extra=None):
        env = dict(os.environ, LOOPS_STACK_CONFIG=str(self.config_path), LOOPS_REPO_ROOT=str(self.root))
        env.update(env_extra or {})
        proc = subprocess.run([sys.executable, str(CLI)] + list(args), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, "expected exactly one JSON line, got: %r / stderr=%r" % (proc.stdout, proc.stderr))
        return proc.returncode, json.loads(lines[0])

    def test_happy_path_usage_and_unavailable(self):
        self.write_config()
        tdir = self.root / "runs" / "tickets"
        tdir.mkdir(parents=True)
        (tdir / "PROJ-1.md").write_text("# CLI ticket\n\nbody text\n")
        code, out = self.run_cli("issue_tracker", "fetch_ticket", "PROJ-1")
        self.assertEqual(code, 0, out)
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"]["title"], "CLI ticket")

        code, out = self.run_cli("issue_tracker", "add_comment", "key=PROJ-1", "--json", '{"body": "via cli", "author": "t"}')
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("issue_tracker", "search", "query=cli")
        self.assertEqual([t["key"] for t in out["result"]], ["PROJ-1"])

        code, out = self.run_cli("issue_tracker")
        self.assertEqual(code, 2)
        self.assertFalse(out["ok"])
        code, out = self.run_cli("issue_tracker", "explode")
        self.assertEqual(code, 2)
        code, out = self.run_cli("nope_role", "x")
        self.assertEqual(code, 2)
        code, out = self.run_cli("issue_tracker", "fetch_ticket", "--json", "{not json")
        self.assertEqual(code, 2)

        code, out = self.run_cli("test_management", "list_cases")
        self.assertEqual(code, 3, out)
        self.assertEqual(out["error"], "adapter-unavailable:test_management")
        self.assertEqual(out["op"], "list_cases")

        code, out = self.run_cli("issue_tracker", "fetch_ticket", "PROJ-404")
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])

        code, out = self.run_cli("--list")
        self.assertEqual(code, 0)
        self.assertEqual(set(out["roles"]), set(OPS))
        self.assertEqual(out["roles"]["issue_tracker"]["provider"], "file")
        self.assertEqual(out["roles"]["sandbox"]["ops"], OPS["sandbox"])
        self.assertEqual(out["providers"]["test_management"], ["none"])

    def test_env_override_reaches_cli(self):
        self.write_config()
        code, out = self.run_cli("vcs_host", "default_branch", env_extra={"LOOPS_CFG__PROJECT__DEFAULT_BRANCH": "trunk"})
        self.assertEqual(code, 0, out)
        self.assertEqual(out["result"], "trunk")


class ImportHygieneTests(unittest.TestCase):
    def test_import_creates_nothing_under_runs(self):
        code = ("import sys; sys.path.insert(0, %r); import lib.adapters, lib.adapters.cli; "
                "import lib.adapters.providers.file_issue_tracker, lib.adapters.providers.git_worktree_sandbox; "
                "print('ok')" % str(REPO_ROOT))
        with tempfile.TemporaryDirectory() as d:
            proc = subprocess.run([sys.executable, "-c", code], cwd=d, env=dict(os.environ, LOOPS_REPO_ROOT=d),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((Path(d) / "runs").exists())


if __name__ == "__main__":
    unittest.main()
