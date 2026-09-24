"""Offline shell integration tests: isolated HOME, fake cron/git/send commands."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.install = self.home / 'agent'
        (self.install / 'scripts').mkdir(parents=True)
        shutil.copy2(ROOT / 'run_telemetry.sh', self.install)
        for file in (ROOT / 'scripts').glob('*'):
            if file.is_file():
                shutil.copy2(file, self.install / 'scripts')
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('AIKUB_', 'HERMES_', 'ENV_FILE', 'CODEX_'))}
        self.env.update(HOME=str(self.home), HERMES_HOME=str(self.home / '.hermes'),
                        PATH=str(self.bin) + ':' + os.environ['PATH'],
                        TEST_HOME=str(self.home), AIKUB_TELEMETRY_AGENT_DIR=str(self.install),
                        AIKUB_TELEMETRY_BASE_URL='https://invalid.example',
                        AIKUB_TELEMETRY_BOTOPS_TOKEN='fixture-not-a-real-token')
        self.stub('git', 'import sys\nsys.exit(1)\n')
        self.stub('crontab', '''import os, pathlib, sys
p = pathlib.Path(os.environ['TEST_HOME']) / 'crontab'
if sys.argv[1:] == ['-l']:
    print(p.read_text() if p.exists() else '', end='')
else:
    p.write_text(sys.stdin.read())
''')
        self.stub('python3', '''import json, os, pathlib, sys
p = pathlib.Path(os.environ['TEST_HOME']) / 'calls'
with p.open('a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')
if 'logs_incremental' in sys.argv[1] and os.environ.get('FAIL_LOGS'): sys.exit(1)
''')

    def stub(self, name, body):
        p = self.bin / name
        p.write_text('#!' + sys.executable + '\n' + body)
        p.chmod(0o755)

    def run_script(self, mode):
        return subprocess.run(['bash', str(self.install / 'run_telemetry.sh'), mode],
                              env=self.env, text=True, capture_output=True)

    def calls(self):
        p = self.home / 'calls'
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def test_install_cron_idempotent_no_collection_no_credentials(self):
        self.env.pop('AIKUB_TELEMETRY_BASE_URL')
        self.env.pop('AIKUB_TELEMETRY_BOTOPS_TOKEN')
        cron = self.home / 'crontab'
        cron.write_text('MAILTO=test@example.invalid\n15 8 * * * /bin/true\n')
        for _ in range(2):
            result = self.run_script('install-cron')
            self.assertEqual(result.returncode, 0, result.stderr)
        text = cron.read_text()
        self.assertIn('15 8 * * * /bin/true', text)
        self.assertEqual(text.count('0 */2 * * *'), 1)
        self.assertEqual(text.count('30 3 * * *'), 1)
        self.assertNotIn('hermes cron', text)
        self.assertEqual(self.calls(), [])

    def test_sessions_offline_fallback_and_valid_arguments(self):
        result = self.run_script('sessions')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [['aikub_telemetry_sessions_snapshot.py', '--chunk-size', '50']])
        self.assertIn('using existing local', result.stderr)
        # Real argparse validation, --help exits before DB/network access.
        checked = subprocess.run([sys.executable, str(ROOT / 'scripts/aikub_telemetry_sessions_snapshot.py'),
                                  *self.calls()[0][1:], '--help'], capture_output=True)
        self.assertEqual(checked.returncode, 0)

    def test_light_no_sessions_and_logs_failure_nonfatal(self):
        self.env['FAIL_LOGS'] = '1'
        result = self.run_script('light')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c[0] for c in self.calls()],
                         ['aikub_telemetry_logger.py', 'aikub_telemetry_logs_incremental.py'])
        self.assertIn('logs snapshot failed', result.stderr)

    def test_log_timezone_loaded_from_dotenv_with_environment_precedence(self):
        (self.install / '.env').write_text('AIKUB_LOG_TIMEZONE="America/Toronto"\n')
        self.stub('python3', '''import os, pathlib
(pathlib.Path(os.environ['TEST_HOME']) / 'timezone').write_text(os.environ.get('AIKUB_LOG_TIMEZONE', 'missing'))
''')
        result = self.run_script('light')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / 'timezone').read_text(), 'America/Toronto')
        self.env['AIKUB_LOG_TIMEZONE'] = 'UTC'
        result = self.run_script('light')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / 'timezone').read_text(), 'UTC')

    def test_hermes_home_resolution_does_not_override_configured_profile(self):
        self.stub('python3', '''import os, pathlib
(pathlib.Path(os.environ['TEST_HOME']) / 'selected-home').write_text(os.environ['HERMES_HOME'])
''')
        cases = [
            ({}, 'HERMES_HOME="/fixture/profile"\n', '/fixture/profile'),
            ({}, 'HERMES_REAL_HOME="/fixture/real"\n', '/fixture/real'),
            ({'HERMES_REAL_HOME': '/fixture/env-real'}, '', '/fixture/env-real'),
            ({'HERMES_HOME': '/fixture/env'}, 'HERMES_HOME=/fixture/file\n', '/fixture/env'),
            ({}, '', str(self.home / '.hermes')),
        ]
        for environment, dotenv, expected in cases:
            with self.subTest(environment=environment, dotenv=dotenv):
                self.env.pop('HERMES_HOME', None)
                self.env.pop('HERMES_REAL_HOME', None)
                self.env.update(environment)
                (self.install / '.env').write_text(dotenv)
                result = self.run_script('light')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.home / 'selected-home').read_text(), expected)

    def test_invalid_mode_has_no_side_effects(self):
        result = self.run_script('typo')
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.home / 'crontab').exists())
        self.assertEqual(self.calls(), [])

    def run_helper(self):
        return subprocess.run(['bash', str(ROOT / 'scripts/aikub_telemetry_self_update.sh')],
                              env=self.env, capture_output=True, text=True)

    def test_failed_repair_preserves_original_directory(self):
        (self.install / '.env').write_text('fixture configuration\n')
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.install / '.env').read_text(), 'fixture configuration\n')
        self.assertEqual(list(self.home.glob('agent.old_*')), [])
        self.assertEqual(list(self.home.glob('agent.update_*')), [])
        self.assertFalse((self.home / 'crontab').exists())

    def test_failed_fetch_uses_existing_checkout(self):
        (self.install / '.git').mkdir()
        self.stub('git', "import sys\nsys.exit(0 if 'set-url' in sys.argv else 1)\n")
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('existing local', result.stderr)
        self.assertFalse((self.home / 'crontab').exists())

    def test_successful_repair_preserves_runtime_files_without_cron_changes(self):
        origin = self.home / 'origin'
        shutil.copytree(self.install, origin)
        real_git = shutil.which('git')
        def git(*args):
            result = subprocess.run([real_git, '-C', str(origin), *args],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        git('init', '-b', 'main')
        git('add', '.')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.test',
            'commit', '-m', 'fixture')
        (self.install / '.env').write_text('TEST=retained\n')
        (self.install / 'state').mkdir()
        (self.install / 'state' / 'cursor.json').write_text('{"offset":12}')
        self.env['AIKUB_TELEMETRY_REPO_URL'] = str(origin)
        self.stub('git', f'import os, sys\nos.execv({real_git!r}, [{real_git!r}] + sys.argv[1:])\n')
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.install / '.git').is_dir())
        self.assertEqual((self.install / '.env').read_text(), 'TEST=retained\n')
        self.assertEqual((self.install / 'state' / 'cursor.json').read_text(), '{"offset":12}')
        self.assertFalse((self.home / 'crontab').exists())

    def test_fresh_clone_failure_stops_cleanly(self):
        self.env['AIKUB_TELEMETRY_AGENT_DIR'] = str(self.home / 'missing')
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / 'missing').exists())
        self.assertIn('no usable local copy', result.stderr)

    def test_incomplete_local_copy_does_not_send(self):
        (self.install / 'scripts/aikub_telemetry_logger.py').unlink()
        result = self.run_script('sessions')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
