"""Overlay config lookup: an owner's config lives outside the kit and wins over the kit's example."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from lib.adapters import config as C  # noqa: E402

KIT = Path(__file__).resolve().parents[3]


class OverlayLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.overlay = base / "overlay"
        (self.overlay / "projects" / "demo-app").mkdir(parents=True)
        self.repo = base / "demo-app"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.env = dict(os.environ)
        for key in (C.ENV_CONFIG_PATH, C.ENV_REPO_ROOT, C.ENV_OVERLAY, C.ENV_PROJECT):
            os.environ.pop(key, None)
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        os.environ.clear()
        os.environ.update(self.env)
        self.tmp.cleanup()

    def write(self, path, data):
        path.write_text(json.dumps(data))
        return path

    def test_no_overlay_uses_kit_example(self):
        self.assertEqual(C.default_config_path(), KIT / C.CONFIG_RELPATH)

    def test_project_config_beats_overlay_default(self):
        self.write(self.overlay / "stack.config.json", {"project": {"name": "overlay-default"}})
        proj = self.write(self.overlay / "projects" / "demo-app" / "stack.config.json", {"project": {"name": "demo"}})
        os.environ[C.ENV_OVERLAY] = str(self.overlay)
        os.chdir(self.repo)
        self.assertEqual(C.project_name(), "demo-app")
        self.assertEqual(C.default_config_path(), proj)
        loaded = C.load_config(reload=True)
        self.assertTrue(loaded.is_overlay)
        self.assertEqual(loaded.get("project.name"), "demo")

    def test_overlay_default_when_project_has_none(self):
        default = self.write(self.overlay / "stack.config.json", {"project": {"name": "overlay-default"}})
        os.environ[C.ENV_OVERLAY] = str(self.overlay)
        os.environ[C.ENV_PROJECT] = "unknown-project"
        self.assertEqual(C.default_config_path(), default)

    def test_explicit_path_still_wins(self):
        self.write(self.overlay / "stack.config.json", {})
        explicit = self.write(Path(self.tmp.name) / "explicit.json", {})
        os.environ[C.ENV_OVERLAY] = str(self.overlay)
        os.environ[C.ENV_CONFIG_PATH] = str(explicit)
        self.assertEqual(C.default_config_path(), explicit.resolve())

    def test_overlay_paths_resolve_locally_then_fall_back_to_kit(self):
        proj_dir = self.overlay / "projects" / "demo-app"
        self.write(proj_dir / "conventions.json", {})
        self.write(proj_dir / "stack.config.json", {"stack": {"conventions_file": "conventions.json"}})
        os.environ[C.ENV_OVERLAY] = str(self.overlay)
        os.chdir(self.repo)
        loaded = C.load_config(reload=True)
        self.assertEqual(loaded.resolve(loaded.get("stack.conventions_file")), proj_dir / "conventions.json")
        self.assertEqual(loaded.resolve(".claude/loops/loop-limits.json"), KIT / ".claude/loops/loop-limits.json")

    def test_overlay_project_targets_its_git_root(self):
        self.write(self.overlay / "projects" / "demo-app" / "stack.config.json", {})
        os.environ[C.ENV_OVERLAY] = str(self.overlay)
        os.chdir(self.repo)
        self.assertEqual(C.load_config(reload=True).repo_dir(), self.repo)

    def test_kit_checkout_is_the_global_project(self):
        os.chdir(KIT)
        self.assertEqual(C.project_name(), C.GLOBAL_PROJECT)


if __name__ == "__main__":
    unittest.main()
