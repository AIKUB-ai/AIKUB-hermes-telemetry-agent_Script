"""Synthetic SQLite regressions: no backfill, role leaks or cursor replay."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from unittest.mock import Mock

SPEC = importlib.util.spec_from_file_location(
    'incremental_sessions', Path(__file__).resolve().parents[1] / 'scripts/aikub_telemetry_sessions_snapshot.py')
sessions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sessions)


class IncrementalSessionsTests(unittest.TestCase):
    def cli(self, *args):
        with patch.dict(sessions.os.environ, {'HERMES_HOME': str(self.home),
                'AIKUB_TELEMETRY_URL': 'https://offline.invalid',
                'AIKUB_TELEMETRY_BOTOPS_TOKEN': 'synthetic'}, clear=True), \
                patch('sys.argv', ['sessions', *args]), patch('builtins.print'):
            return sessions.main()

    def test_default_migration_baselines_history_and_keeps_future_messages(self):
        self.add(1)
        with patch.object(sessions, 'send_payload') as send:
            self.assertEqual(self.cli(), 0)
            send.assert_not_called()
        state = sessions.read_state(self.home)
        self.assertGreater(state['lastMessageTimestamp'], self.now)
        self.add(2, timestamp=state['lastMessageTimestamp'] + 1)
        self.assertEqual([m['messageId'] for m in self.collect()[2]], [2])

    def test_dry_run_never_initializes_or_advances_cursor(self):
        self.add(1)
        self.assertEqual(self.cli('--dry-run'), 0)
        self.assertFalse(sessions.state_path(self.home).exists())

    def test_corrupt_cursor_fails_closed_without_replaying(self):
        sessions.state_path(self.home).parent.mkdir()
        for raw in ('broken', '{}', '[]', '{"lastMessageTimestamp":NaN,"messageIdsAtLastTimestamp":[]}'):
            sessions.state_path(self.home).write_text(raw)
            with self.assertRaises(SystemExit):
                self.collect()

    def test_final_chunk_failure_keeps_entire_pending_cursor(self):
        self.add(1)
        self.add(2)
        sessions.write_state(self.home, {'lastMessageTimestamp': self.now - 1, 'messageIdsAtLastTimestamp': []})
        previous = sessions.state_path(self.home).read_bytes()
        with patch.object(sessions, 'send_payload', side_effect=[
                {'ok': True, 'status': 201}, {'ok': False, 'status': 503}]):
            self.assertEqual(self.cli('--chunk-size', '1'), 1)
        self.assertEqual(sessions.state_path(self.home).read_bytes(), previous)
        self.assertEqual(len(self.collect()[2]), 2)

    def test_missing_erp_ack_never_counts_as_success(self):
        payload = {'payload': {'sessions': {'chunkIndex': 1, 'chunkCount': 1}}}
        for body in ({'ok': True}, [], {'erpImport': {'ok': True, 'skipped': True, 'reason': 'ERP_AUTH_MISSING'}}):
            response = Mock(status=201)
            response.read.return_value = json.dumps(body).encode()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            with patch.object(sessions.urllib.request, 'urlopen', return_value=response):
                self.assertFalse(sessions.send_payload(payload, 'https://offline.invalid', 'synthetic', 'bot', 180, 0)['ok'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.now = time.time()
        with sqlite3.connect(self.home / 'state.db') as con:
            con.execute('CREATE TABLE sessions (id TEXT, source TEXT, started_at REAL)')
            con.execute('INSERT INTO sessions VALUES (?,?,?)', ('s', 'cli', self.now))
            con.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL, content TEXT, active INTEGER, compacted INTEGER)')

    def add(self, ident, role='user', timestamp=None, content='synthetic', active=1, compacted=0):
        with sqlite3.connect(self.home / 'state.db') as con:
            con.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                        (ident, 's', role, self.now if timestamp is None else timestamp, content, active, compacted))

    def collect(self):
        return sessions.collect_sessions_and_messages(self.home, 2000, 1)

    def test_initial_window_roles_and_repeat_run_do_not_backfill(self):
        self.add(1, timestamp=self.now - 10 * 86400)
        self.add(2)
        self.add(3, 'assistant')
        for ident, role in enumerate(('tool', 'system', 'developer'), 4):
            self.add(ident, role)
        self.add(7, content='')
        self.add(8, active=0)
        self.add(9, compacted=1)
        _, _, messages, state = self.collect()
        self.assertEqual([m['messageId'] for m in messages], [2, 3])
        sessions.write_state(self.home, state)
        self.assertEqual(self.collect()[2], [])
        self.add(10, 'assistant', self.now + 1)
        self.assertEqual([m['messageId'] for m in self.collect()[2]], [10])

    def test_same_timestamp_new_ids_remain_acknowledged_on_later_runs(self):
        self.add(1)
        sessions.write_state(self.home, self.collect()[3])
        for ident in (2, 3):
            self.add(ident)
            _, _, messages, state = self.collect()
            self.assertEqual([m['messageId'] for m in messages], [ident])
            sessions.write_state(self.home, state)
            self.assertEqual(self.collect()[2], [])

    def test_failed_final_ack_keeps_cursor_then_success_advances_it(self):
        self.add(1)
        sessions.write_state(self.home, {'lastMessageTimestamp': self.now - 1, 'messageIdsAtLastTimestamp': []})
        previous = sessions.state_path(self.home).read_bytes()
        with patch.dict(sessions.os.environ, {'HERMES_HOME': str(self.home),
                'AIKUB_TELEMETRY_URL': 'https://offline.invalid',
                'AIKUB_TELEMETRY_BOTOPS_TOKEN': 'synthetic'}, clear=True), \
                patch('sys.argv', ['sessions']), patch('builtins.print'):
            with patch.object(sessions, 'send_payload', return_value={'ok': False, 'status': 503}):
                self.assertEqual(sessions.main(), 1)
            self.assertEqual(sessions.state_path(self.home).read_bytes(), previous)
            self.assertEqual(len(self.collect()[2]), 1)
            with patch.object(sessions, 'send_payload', return_value={'ok': True, 'status': 201}):
                self.assertEqual(sessions.main(), 0)
            self.assertEqual(self.collect()[2], [])
