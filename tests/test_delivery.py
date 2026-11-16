"""Offline delivery regressions: HTTP success is not an ERP acknowledgement."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

logger = load('aikub_telemetry_logger')
logs = load('aikub_telemetry_logs_incremental')
sessions = load('aikub_telemetry_sessions_snapshot')

def response(body):
    obj = Mock(status=201)
    obj.read.return_value = json.dumps(body).encode()
    obj.__enter__ = Mock(return_value=obj)
    obj.__exit__ = Mock(return_value=False)
    return obj

class DeliveryTests(unittest.TestCase):
    def test_logger_rejects_nested_erp_failure(self):
        with patch.object(logger.urllib.request, 'urlopen', return_value=response({'erpImport': {'ok': False, 'status': 500}})):
            self.assertFalse(logger.post_event('https://test/events','test','bot',{})['ok'])

    def test_logger_rejects_missing_erp_auth(self):
        with patch.object(logger.urllib.request, 'urlopen', return_value=response({'erpImport': {'ok': True, 'skipped': True, 'reason': 'ERP_AUTH_MISSING'}})):
            self.assertFalse(logger.post_event('https://test/events','test','bot',{})['ok'])

    def test_transient_errors_never_quarantine_or_advance_logs(self):
        for status in [None, 201, 401, 403, 429, 500, 503]:
            with self.subTest(status=status):
                base = SimpleNamespace(build_payload=lambda *_: {'payload': {}}, optional_env=lambda *_: 'test', build_endpoint=lambda _: 'https://test', require_env=lambda _: 'https://test', require_token=lambda: 'test', post_event=Mock(return_value={'ok':False,'status':status,'body':{}}))
                with patch.object(logs, 'load_base_logger', return_value=base), patch.object(logs, 'append_quarantine') as quarantine, patch.object(logs.time,'sleep'):
                    result=logs.send([{'line':1},{'line':2}], {'entriesInThisRun':2}, 250, False, 25)
                self.assertFalse(result['allOk'])
                self.assertEqual(result['quarantinedCount'],0)
                quarantine.assert_not_called()

    def test_sessions_retry_failed_erp_ack_same_payload(self):
        payload={'payload':{'sessions':{'chunkIndex':1,'chunkCount':1}}}
        failed=response({'batch':{'status':'COMPLETE'},'erpImport':{'ok':False,'status':500}})
        succeeded=response({'batch':{'status':'COMPLETE'},'erpImport':{'ok':True,'status':200}})
        with patch.object(sessions.urllib.request,'urlopen',side_effect=[failed,succeeded]) as post, patch('time.sleep'):
            result=sessions.send_payload(payload,'https://test/events','test','bot',1)
        self.assertEqual(result['status'],201)
        self.assertEqual(post.call_count,2)
        self.assertEqual(post.call_args_list[0].args[0].data,post.call_args_list[1].args[0].data)

    def test_sessions_never_accept_final_incomplete_batch(self):
        payload={'payload':{'sessions':{'chunkIndex':2,'chunkCount':2}}}
        body={'batch':{'status':'PARTIAL'},'erpImport':{'ok':True,'skipped':True,'reason':'SESSION_BATCH_INCOMPLETE'}}
        with patch.object(sessions.urllib.request,'urlopen',return_value=response(body)), patch('time.sleep'):
            with self.assertRaises(RuntimeError):
                sessions.send_payload(payload,'https://test/events','test','bot',1)

    def test_partial_log_line_is_held_until_newline(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'agent.log'; state_path=Path(temp)/'state.json'
            path.write_text('2026-11-16T12:00:00Z INFO test: first\n2026-11-16T12:01:00Z INFO test: par')
            with patch.object(logs,'LOG_PATH',path), patch.object(logs,'STATE_PATH',state_path), patch.dict(logs.os.environ,{'AIKUB_LOG_TIMEZONE':'UTC'}):
                entries,state=logs.collect_lines(3)
                self.assertEqual(len(entries),1)
                logs.write_state(state)
                with path.open('a') as handle: handle.write('tial\n')
                entries,state=logs.collect_lines(3)
                self.assertEqual(len(entries),1)
                self.assertEqual(entries[0]['message'],'partial')
                self.assertEqual(entries[0]['line'],2)

    def test_partial_session_chunk_can_wait_for_remainder(self):
        payload={'payload':{'sessions':{'chunkIndex':1,'chunkCount':2}}}
        body={'batch':{'status':'PARTIAL'},'erpImport':{'ok':True,'skipped':True,'reason':'SESSION_BATCH_INCOMPLETE'}}
        with patch.object(sessions.urllib.request,'urlopen',return_value=response(body)):
            self.assertEqual(sessions.send_payload(payload,'https://test/events','test','bot',1)['status'],201)
