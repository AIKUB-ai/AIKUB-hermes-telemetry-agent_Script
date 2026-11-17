"""A durable collector ACK transfers custody, not ERP import success."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


logs = load('aikub_telemetry_logs_incremental')
logger = load('aikub_telemetry_logger')
sessions = load('aikub_telemetry_sessions_snapshot')


def queued(status='PENDING'):
    return {'stored': True, 'delivery': {'durable': True, 'status': status},
            'erpImport': {'ok': False, 'skipped': True, 'reason': 'ERP_IMPORT_QUEUED'}}


def response(body, status=202):
    obj = Mock(status=status)
    obj.read.return_value = json.dumps(body).encode()
    obj.__enter__ = Mock(return_value=obj)
    obj.__exit__ = Mock(return_value=False)
    return obj


class DurableQueueTests(unittest.TestCase):
    def check_ack(self, body, status):
        payload = {'payload': {'sessions': {'chunkIndex': 1, 'chunkCount': 1}}}
        with patch.object(logger.urllib.request, 'urlopen', return_value=response(body, status)):
            logger_result = logger.post_event('https://test/events', 'test', 'bot', {})
        with patch.object(sessions.urllib.request, 'urlopen', return_value=response(body, status)):
            session_result = sessions.send_payload_once(payload, 'https://test/events', 'test', 'bot', 1)
        return logger_result['ok'], session_result['ok']

    def test_log_cursor_requires_durable_custody_for_every_chunk(self):
        for final_body, expected in ((queued(), 0), ({'stored': True}, 1)):
            with self.subTest(final_body=final_body), \
                    patch.object(logs, 'load_base_logger', return_value=logger), \
                    patch.object(logger, 'build_payload', return_value={'payload': {}}), \
                    patch.object(logger, 'require_base_url', return_value='https://test'), \
                    patch.object(logger, 'require_token', return_value='test'), \
                    patch.object(logs, 'collect_lines', return_value=([{'line': 1}, {'line': 2}], {'offset': 20})), \
                    patch.object(logger.urllib.request, 'urlopen', side_effect=[response(queued()), response(final_body)]), \
                    patch.object(logs, 'write_state') as save, \
                    patch.object(logs.time, 'sleep'), \
                    patch('sys.argv', ['logs', '--chunk-size', '1']), patch('builtins.print'):
                self.assertEqual(logs.main(), expected)
                if expected == 0:
                    save.assert_called_once_with({'offset': 20})
                else:
                    save.assert_not_called()

    def test_malformed_json_ack_is_rejected(self):
        for raw in (b'', b'not json', b'{'):
            for module in (logger, sessions):
                reply = response(None)
                reply.read.return_value = raw
                with self.subTest(raw=raw, module=module.__name__), patch.object(module.urllib.request, 'urlopen', return_value=reply):
                    if module is logger:
                        result = logger.post_event('https://test/events', 'test', 'bot', {})
                    else:
                        result = sessions.send_payload_once({}, 'https://test/events', 'test', 'bot', 1)
                    self.assertFalse(result['ok'])

    def test_missing_ack_is_rejected_even_for_synchronous_success(self):
        for body in ({}, {'ok': True}, {'stored': True}, [], None, {'erpImport': []}):
            for status in (200, 201, 202):
                with self.subTest(body=body, status=status):
                    self.assertEqual(self.check_ack(body, status), (False, False))

    def test_current_collector_contract_and_custody_are_authoritative(self):
        for status in ('PENDING', 'DELIVERED'):
            for relay in ({'ok': True, 'queued': True, 'skipped': True},
                          {'ok': False, 'reason': 'ERP_IMPORT_FAILED'}, None):
                body = queued(status)
                body['erpImport'] = relay
                with self.subTest(status=status, relay=relay):
                    self.assertEqual(self.check_ack(body, 202), (True, True))

    def test_waiting_chunks_ack_only_accepts_nonfinal_session_chunk(self):
        body = queued('WAITING_CHUNKS')
        body['erpImport'] = {'ok': True, 'queued': True, 'skipped': True,
                             'reason': 'SESSION_BATCH_INCOMPLETE'}
        self.assertEqual(self.check_ack(body, 202), (False, False))
        payload = {'payload': {'sessions': {'chunkIndex': 1, 'chunkCount': 2}}}
        with patch.object(sessions.urllib.request, 'urlopen', return_value=response(body)):
            self.assertTrue(sessions.send_payload_once(payload, 'https://test/events', 'test', 'bot', 1)['ok'])

    def test_explicit_modern_or_storage_failure_cannot_fall_back_to_legacy(self):
        for body in ({'stored': False, 'erpImport': {'ok': True}},
                     {'delivery': {'durable': False}, 'erpImport': {'ok': True}},
                     {'delivery': None, 'erpImport': {'ok': True}}):
            with self.subTest(body=body):
                self.assertEqual(self.check_ack(body, 200), (False, False))

    def test_queue_ack_requires_every_field_and_exact_types(self):
        mutations = [
            ('stored', None), ('stored', False), ('stored', 1), ('stored', 'true'),
            ('delivery', None), ('delivery', []), ('delivery', {}),
        ]
        bodies = []
        for key, value in mutations:
            body = queued()
            body[key] = value
            bodies.append(body)
        for key, values in {'durable': [None, False, 1, 'true'],
                            'status': [None, 'FAILED', 'pending', 'PROCESSING']}.items():
            for value in values:
                body = queued()
                body['delivery'][key] = value
                bodies.append(body)
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.check_ack(body, 202), (False, False))

    def test_delivered_modern_duplicate_ack_is_accepted_on_http_200(self):
        self.assertEqual(self.check_ack(queued("DELIVERED"), 200), (True, True))

    def test_queue_ack_is_not_accepted_on_other_http_statuses(self):
        for status in (200, 201, 400, 401, 429, 500, 503):
            with self.subTest(status=status):
                self.assertEqual(self.check_ack(queued(), status), (False, False))

    def test_synchronous_erp_success_is_preserved_but_not_as_202(self):
        body = {'erpImport': {'ok': True, 'status': 200}}
        for status in (200, 201):
            with self.subTest(status=status):
                self.assertEqual(self.check_ack(body, status), (True, True))
        self.assertEqual(self.check_ack(body, 202), (False, False))

    def test_sessions_accepts_final_and_partial_durable_custody(self):
        for status in ('PENDING', 'DELIVERED'):
            for index in (1, 2):
                body = queued(status)
                body['batch'] = {'status': 'PARTIAL' if index == 1 else 'COMPLETE'}
                payload = {'payload': {'sessions': {'chunkIndex': index, 'chunkCount': 2}}}
                with self.subTest(status=status, index=index), patch.object(sessions.urllib.request, 'urlopen', return_value=response(body)) as post:
                    result = sessions.send_payload(payload, 'https://test/events', 'test', 'bot', 1, retries=0)
                self.assertTrue(result['ok'])
                self.assertEqual(result['attempts'], 1)
                self.assertEqual(post.call_count, 1)
                self.assertEqual(result['response'], body)
                self.assertIs(result['response']['erpImport']['ok'], False)

    def test_logger_accepts_durable_custody_without_claiming_import(self):
        for status in ('PENDING', 'DELIVERED'):
            body = queued(status)
            with self.subTest(status=status), patch.object(logger.urllib.request, 'urlopen', return_value=response(body)):
                result = logger.post_event('https://test/events', 'test', 'bot', {})
            self.assertTrue(result['ok'])
            self.assertEqual(result['body'], body)
            self.assertIs(result['body']['erpImport']['ok'], False)
