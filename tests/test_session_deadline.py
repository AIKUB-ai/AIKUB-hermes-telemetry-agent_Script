"""Session upload must outlive collector's bounded 120-second ERP relay."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch


class SessionDeadlineTest(unittest.TestCase):
    def test_default_upload_timeout_outlives_erp_relay(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "aikub_telemetry_sessions_snapshot.py"
        spec = importlib.util.spec_from_file_location("session_deadline_snapshot", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        env = {"AIKUB_TELEMETRY_BOT_ID": "test-bot"}
        with patch("sys.argv", [str(script)]), \
             patch.object(module, "optional_env", side_effect=lambda key, default="": env.get(key, default)), \
             patch.object(module, "require_env", return_value="https://collector.example.test"), \
             patch.object(module, "require_token", return_value="test-only-token"), \
             patch.object(module, "collect_sessions_and_messages", return_value=({}, [], [])), \
             patch.object(module, "send_payload", return_value={"status": 200}) as send, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(), 0)
        self.assertTrue(send.called)
        self.assertGreater(send.call_args.args[-1], 120)


if __name__ == "__main__":
    unittest.main()
