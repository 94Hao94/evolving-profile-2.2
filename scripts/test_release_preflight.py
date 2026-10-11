import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("release-preflight.py")


class ReleasePreflightTest(unittest.TestCase):
    def run_preflight(self, root):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(root), "--json"],
            capture_output=True,
            text=True,
            check=False,
        )

    def fixture(self, root):
        (root / "VERSION").write_text("4.0.0\n", encoding="utf-8")
        (root / "README.md").write_text("# Evolving Profile 4.0\n", encoding="utf-8")
        (root / "NOTICE.md").write_text("# Attribution — Evolving Profile 4.0\n\nAuthor: CCY.\n", encoding="utf-8")
        (root / "CHANGELOG-4.0.md").write_text("# Evolving Profile 4.0.0\n", encoding="utf-8")

    def test_matching_release_metadata_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            result = self.run_preflight(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["version"], "4.0.0")
        self.assertEqual(report["issues"], [])

    def test_missing_changelog_blocks_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            (root / "CHANGELOG-4.0.md").unlink()
            result = self.run_preflight(root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing_changelog", json.loads(result.stdout)["issues"])

    def test_stale_readme_and_notice_block_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            (root / "README.md").write_text("# Evolving Profile 3.0\n", encoding="utf-8")
            (root / "NOTICE.md").write_text("# Attribution — Evolving Profile 3.0\n", encoding="utf-8")
            result = self.run_preflight(root)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stdout)["issues"],
            ["readme_version_mismatch", "notice_version_mismatch"],
        )

    def test_stale_flow_badge_blocks_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            flow = root / "console" / "src" / "components" / "flow-view.tsx"
            flow.parent.mkdir(parents=True)
            flow.write_text('<span>EP 2.2</span>\n', encoding="utf-8")
            result = self.run_preflight(root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("flow_badge_version_mismatch", json.loads(result.stdout)["issues"])

    def test_product_readme_title_can_be_version_neutral(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.mkdir(exist_ok=True)
            self.fixture(root)
            (root / "README.md").write_text("# Evolving Profile\n", encoding="utf-8")
            result = self.run_preflight(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("readme_version_mismatch", json.loads(result.stdout)["issues"])


if __name__ == "__main__":
    unittest.main()
