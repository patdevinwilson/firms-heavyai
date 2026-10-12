import os
import tempfile
import unittest
from pathlib import Path
from scripts.build_progress import snapshot


class ProgressTests(unittest.TestCase):
    def test_component_progress_and_no_raw_log_disclosure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "var/log/firms-runtime-install.log"
            log.parent.mkdir(parents=True)
            log.write_text("private-token-do-not-output\n[ 28%] Building CXX object LLVMThing/file.cpp.o\n")
            os.utime(log, (100, 100))
            report = snapshot(root, now=110)
            self.assertEqual(report["stage"], "Dependencies: LLVM")
            self.assertEqual(report["component_percent"], 28)
            self.assertEqual(report["last_output_seconds_ago"], 10)
            self.assertNotIn("private-token", str(report))

    def test_later_stage_clears_earlier_compile_percentage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "var/log/firms-runtime-install.log"
            log.parent.mkdir(parents=True)
            log.write_text("[100%] Built target LLVM\n[firms-progress] iq_evaluation\n")
            report = snapshot(root)
            self.assertEqual(report["stage"], "Native HeavyIQ SQL evaluation")
            self.assertIsNone(report["component_percent"])

    def test_missing_log_is_reported_without_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            report = snapshot(Path(directory))
            self.assertEqual(report["stage"], "Runtime setup")
            self.assertIsNone(report["last_output_seconds_ago"])
