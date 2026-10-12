import tempfile
import unittest
from pathlib import Path
from scripts.build_source import patch_iq_requirements


class SourceBuildTests(unittest.TestCase):
    def test_override_preserves_other_pins_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.txt"
            path.write_text("--extra-index-url https://test.pypi.org/simple/\nchromadb==1.5.10.dev239\npyheavydb==10.0.0\nfastapi==0.136.3\n")
            patch_iq_requirements(path)
            first = path.read_text()
            patch_iq_requirements(path)
            self.assertEqual(first, path.read_text())
            self.assertIn("chromadb==1.5.9", first)
            self.assertIn("pyheavydb==10.0.0", first)
            self.assertIn("fastapi==0.136.3", first)
            self.assertNotIn("test.pypi", first)

    def test_unexpected_dependency_version_requires_review(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.txt"
            path.write_text("chromadb==2.0.0\n")
            with self.assertRaises(ValueError):
                patch_iq_requirements(path)
