"""Legacy hosted wrappers must migrate without changing cron modes or dry runs."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location("migration_logger", Path(__file__).resolve().parents[1] / "scripts/aikub_telemetry_logger.py")
logger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(logger)
NEW = b'#!/usr/bin/env bash\nset -euo pipefail\nexport AIKUB_TELEMETRY_RUNNER_VERSION="2026.11.17.1"\n# AIKUB_TELEMETRY_SELF_UPDATED\nexit 0\n'

class RunnerMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runner = self.root / "run_telemetry.sh"
        self.old = '#!/usr/bin/env bash\nexport AIKUB_TELEMETRY_RUNNER_VERSION="2026.09.02.1"\nMODE="${1:-light}"\n'
        self.runner.write_text(self.old)
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.read.return_value = NEW

    def test_preserves_light_default_and_installs_atomically(self):
        with patch.object(logger.urllib.request, "urlopen", return_value=self.response):
            self.assertTrue(logger.upgrade_legacy_runner("https://collector.test", self.root))
        self.assertEqual(self.runner.read_bytes(), NEW)
        self.assertEqual((self.root / ".runner-default-mode").read_text(), "light\n")
        self.assertEqual((self.root / ".run_telemetry.legacy.sh").read_text(), self.old)
        self.assertTrue(self.runner.stat().st_mode & 0o100)

    def test_preserves_existing_implicit_all(self):
        self.runner.write_text(self.old.replace(':-light', ':-all'))
        with patch.object(logger.urllib.request, "urlopen", return_value=self.response):
            self.assertTrue(logger.upgrade_legacy_runner("https://collector.test", self.root))
        self.assertEqual((self.root / ".runner-default-mode").read_text(), "all\n")

    def test_invalid_download_does_not_replace_runner(self):
        self.response.read.return_value = NEW + b'if then\n'
        with patch.object(logger.urllib.request, "urlopen", return_value=self.response):
            self.assertFalse(logger.upgrade_legacy_runner("https://collector.test", self.root))
        self.assertEqual(self.runner.read_text(), self.old)
        self.assertFalse((self.root / ".runner-default-mode").exists())

    def test_modern_wrapper_is_not_downloaded_again(self):
        self.runner.write_bytes(NEW)
        with patch.object(logger.urllib.request, "urlopen") as fetch:
            self.assertFalse(logger.upgrade_legacy_runner("https://collector.test", self.root))
        fetch.assert_not_called()

    def test_network_failure_keeps_old_runner(self):
        with patch.object(logger.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertFalse(logger.upgrade_legacy_runner("https://collector.test", self.root))
        self.assertEqual(self.runner.read_text(), self.old)

    def test_unknown_default_does_not_guess(self):
        self.runner.write_text(self.old.replace(':-light', ':-custom'))
        with patch.object(logger.urllib.request, "urlopen") as fetch:
            self.assertFalse(logger.upgrade_legacy_runner("https://collector.test", self.root))
        fetch.assert_not_called()

if __name__ == "__main__":
    unittest.main()
