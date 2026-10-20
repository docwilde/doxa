"""Setup boundaries only; no npm, renderer or browser is launched."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("prepare", Path(__file__).with_name("prepare.py"))
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class SetupBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.browser, self.cache = self.base / "browser", self.base / "cache"
        self.browser.mkdir()
        self.cache.mkdir()
        (self.browser / "chrome").write_text("unused fixture")
        (self.browser / "chrome").chmod(0o700)

    def inputs(self, root=None):
        with patch.object(prepare.platform, "system", return_value="Linux"), \
             patch.object(prepare.platform, "machine", return_value="x86_64"), \
             patch.object(prepare.subprocess, "check_output", return_value=prepare.BROWSER_VERSION):
            return prepare.reviewed_inputs(root or self.base / "new", self.browser, self.cache)

    def test_plan_accepts_pinned_inputs_without_creating_root(self):
        root, _, _, count = self.inputs()
        self.assertGreater(count, 100)
        self.assertFalse(root.exists())

    def test_existing_and_symlink_destinations_are_refused(self):
        for root in (self.browser, self.base / "alias"):
            if not root.exists():
                root.symlink_to(self.base / "missing")
            with self.assertRaisesRegex(ValueError, "never overwritten"):
                self.inputs(root)

    def test_browser_symlink_cannot_import_outside_files(self):
        (self.browser / "escape").symlink_to(self.cache)
        with self.assertRaisesRegex(ValueError, "ordinary files"):
            self.inputs()

    def test_repository_and_state_destinations_are_refused(self):
        repo = self.base / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        with self.assertRaisesRegex(ValueError, "session repositories"):
            self.inputs(repo / "renderer")
        with patch.dict(prepare.os.environ, {"DOXA_HOME": str(self.base / "state")}):
            with self.assertRaisesRegex(ValueError, "DOXA state"):
                self.inputs(self.base / "state/renderer")


if __name__ == "__main__":
    unittest.main()
