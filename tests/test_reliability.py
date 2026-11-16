"""Offline regressions shared with collector assets; only synthetic homes/DBs."""
import importlib.util
import inspect
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.agent = self.root / 'agent'
        shutil.copytree(ROOT, self.agent, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.cron = self.root / 'crontab'
        self.calls = self.root / 'calls'
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('AIKUB_', 'HERMES_', 'CODEX_', 'OPENCLAW_'))}
        self.env.update(HOME=str(self.root), HERMES_HOME=str(self.root / 'hermes'),
                        PATH=str(self.bin) + ':' + self.env['PATH'], CRON=str(self.cron), CALLS=str(self.calls),
                        AIKUB_TELEMETRY_CRON_SLOT='0', AIKUB_TELEMETRY_CRON_STEP_MINUTES='4',
                        AIKUB_TELEMETRY_URL='https://offline.invalid', AIKUB_TELEMETRY_API_KEY='synthetic')
        self.stub('crontab', 'if [ "$1" = -l ]; then /bin/cat "$CRON" 2>/dev/null; else /bin/cat > "$CRON"; fi')
        self.stub('git', 'exit 1')
        self.stub('curl', 'exit 1')
        self.stub('python3', 'printf "%s\\n" "$*" >> "$CALLS"; case "$1" in *logger.py) exit "${FAIL_LIGHT:-0}";; *sessions_snapshot.py) exit "${FAIL_SESSIONS:-0}";; esac')

    def stub(self, name, body):
        p = self.bin / name
        p.write_text('#!/bin/bash\n' + body + '\n')
        p.chmod(0o755)

    def run_agent(self, mode='install-cron', **env):
        return subprocess.run(['bash', str(self.agent / 'run_telemetry.sh'), mode],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=15)

    def test_new_install_split_stagger_and_idempotence(self):
        self.cron.write_text('12 7 * * * /bin/true\n')
        result = self.run_agent()
        self.assertEqual(result.returncode, 0, result.stderr)
        first = self.cron.read_text()
        self.assertIn('0 0-23/2 * * * flock -n ', first)
        self.assertIn('0 3 * * * flock -n ', first)
        self.assertEqual(first.count('run_telemetry.sh light'), 1)
        self.assertEqual(first.count('run_telemetry.sh sessions'), 1)
        self.assertIn('12 7 * * * /bin/true', first)
        self.assertEqual(self.run_agent().returncode, 0)
        self.assertEqual(self.cron.read_text(), first)
        self.assertFalse(self.calls.exists())
        self.cron.unlink()
        self.assertEqual(self.run_agent(AIKUB_TELEMETRY_CRON_SLOT='1').returncode, 0)
        self.assertIn('4 0-23/2 * * *', self.cron.read_text())
        self.cron.unlink()
        self.assertEqual(self.run_agent(AIKUB_TELEMETRY_CRON_SLOT='1', AIKUB_TELEMETRY_CRON_STEP_MINUTES='3').returncode, 0)
        self.assertIn('3 0-23/2 * * *', self.cron.read_text())

    def test_automatic_slot_is_stable(self):
        self.env.pop('AIKUB_TELEMETRY_CRON_SLOT')
        self.assertEqual(self.run_agent().returncode, 0)
        first = self.cron.read_text()
        self.cron.unlink()
        self.assertEqual(self.run_agent().returncode, 0)
        self.assertEqual(first, self.cron.read_text())

    def test_existing_implicit_full_and_staggered_commands_preserved_byte_for_byte(self):
        for command in [
            f'7 */2 * * * flock -n /custom/full.lock {self.agent}/run_telemetry.sh >> /custom/log 2>&1\n',
            f'11 0-23/2 * * * flock -n /custom/light.lock {self.agent}/run_telemetry.sh light\n'
            f'19 5 * * * flock -n /custom/sessions.lock {self.agent}/run_telemetry.sh sessions\n',
            f'27 */2 * * * {self.agent}/run_telemetry.sh all\n',
        ]:
            with self.subTest(command=command):
                self.cron.write_text('12 7 * * * /bin/true\n' + command)
                before = self.cron.read_bytes()
                result = self.run_agent()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.cron.read_bytes(), before)

    def test_invalid_slot_does_not_modify_crontab(self):
        self.cron.write_text('12 7 * * * /bin/true\n')
        before = self.cron.read_bytes()
        self.assertNotEqual(self.run_agent(AIKUB_TELEMETRY_CRON_SLOT='99').returncode, 0)
        self.assertEqual(self.cron.read_bytes(), before)

    def test_all_attempts_sessions_even_when_light_fails_and_aggregates_status(self):
        for light, sessions in [('1', '0'), ('0', '1'), ('0', '0')]:
            with self.subTest(light=light, sessions=sessions):
                self.calls.unlink(missing_ok=True)
                result = self.run_agent('all', FAIL_LIGHT=light, FAIL_SESSIONS=sessions)
                self.assertEqual(result.returncode, int(light != '0' or sessions != '0'), result.stderr)
                calls = self.calls.read_text()
                self.assertIn('aikub_telemetry_logger.py', calls)
                self.assertIn('aikub_telemetry_logs_incremental.py', calls)
                self.assertIn('aikub_telemetry_sessions_snapshot.py', calls)


class CompatibilityTests(unittest.TestCase):
    def test_old_and_modern_database_without_source_mutation(self):
        mod = load('aikub_telemetry_sessions_snapshot')
        for modern in [False, True]:
            with self.subTest(modern=modern), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                db = home / 'state.db'
                now = time.time()
                with sqlite3.connect(db) as con:
                    con.execute('CREATE TABLE sessions (id TEXT, title TEXT, source TEXT, model TEXT, '
                                'started_at REAL, ended_at REAL, message_count INTEGER, tool_call_count INTEGER, '
                                'input_tokens INTEGER, output_tokens INTEGER, system_prompt TEXT, model_config TEXT'
                                + (', chat_id TEXT, chat_type TEXT' if modern else '') + ')')
                    con.execute('INSERT INTO sessions (id,title,source,started_at) VALUES (?,?,?,?)', ('s','synthetic','cli',now))
                    con.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, '
                                'timestamp REAL, content TEXT, active INTEGER' + (', compacted INTEGER' if modern else '') + ')')
                    con.execute('INSERT INTO messages (session_id,role,timestamp,content,active) VALUES (?,?,?,?,?)', ('s','user',now,'synthetic',1))
                    if modern:
                        con.execute('INSERT INTO messages VALUES (2,?,?,?,?,?,?)', ('s','user',now,'compacted',1,1))
                before = db.read_bytes()
                args = [home, 2000]
                if 'first_run_days' in inspect.signature(mod.collect_sessions_and_messages).parameters:
                    args.append(3)
                result = mod.collect_sessions_and_messages(*args)
                self.assertEqual(len(result[1]), 1)
                self.assertEqual(len(result[2]), 1)
                self.assertEqual(result[2][0]['content'], 'synthetic')
                self.assertEqual(db.read_bytes(), before)

    def test_modern_oauth_account_never_exports_credentials(self):
        logger = load('aikub_telemetry_logger')
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / 'auth.json'
            auth.write_text(json.dumps({'email':'synthetic@example.invalid', 'access_token':'secret-token'}))
            with patch.object(logger.urllib.request, 'urlopen', side_effect=AssertionError('network forbidden')) as http:
                account = logger.read_codex_oauth_account_from_file(auth)
            http.assert_not_called()
            self.assertEqual(account['email'], 'synthetic@example.invalid')
            self.assertNotIn('secret-token', json.dumps(account))

    def test_openclaw_skill_readiness_metadata(self):
        logger = load('aikub_telemetry_logger')
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'hermes'
            oc = Path(tmp) / 'openclaw'
            skill = oc / 'workspace/skills/demo/SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_text('---\nname: demo\ndescription: synthetic modern skill\n---\n')
            with patch.dict(os.environ, {'OPENCLAW_HOME': str(oc), 'AIKUB_TELEMETRY_DISABLE_OPENCLAW_CLI': '1'}):
                found = logger.discover_skills(home)
            self.assertEqual(found[0]['name'], 'demo')
            self.assertIn('readinessStatus', found[0])
            self.assertIn('usableByHermes', found[0])
