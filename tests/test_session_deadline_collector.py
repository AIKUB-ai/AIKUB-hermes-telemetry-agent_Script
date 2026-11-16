"""Session client deadline must exceed collector's bounded ERP relay."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


class DeadlineChecked(Exception):
    pass


class SessionDeadlineTests(unittest.TestCase):
    def test_cli_default_outlives_120_second_erp_relay(self):
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'aikub_telemetry_sessions_snapshot.py'
        spec = importlib.util.spec_from_file_location('asset_session_deadline', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = module.argparse.ArgumentParser.parse_args

        def check_default(parser, *args, **kwargs):
            parsed = original(parser, *args, **kwargs)
            self.assertGreater(parsed.timeout, 120)
            raise DeadlineChecked

        with patch('sys.argv', [str(path)]), patch.object(
            module.argparse.ArgumentParser, 'parse_args', check_default
        ):
            with self.assertRaises(DeadlineChecked):
                module.main()


if __name__ == '__main__':
    unittest.main()
