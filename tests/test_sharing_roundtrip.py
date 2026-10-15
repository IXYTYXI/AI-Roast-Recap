"""Local receiver round trips with synthetic events and mocked account reads."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import scold

try:
    import sharing
except ImportError:
    sharing = None


class SharingRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(sharing)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'ledger'
        self.received = []
        self.remote = {}
        self.fail_once = False
        owner = self

        class Receiver(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                envelope = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.received.append(envelope)
                for item in envelope['events']:
                    old = owner.remote.get(item['event_id'])
                    if old is None or item['revision'] > old['revision']:
                        owner.remote[item['event_id']] = item
                if owner.fail_once:
                    owner.fail_once = False
                    self.send_response(503)
                    self.end_headers()
                    return
                body = json.dumps({'accepted': [
                    {'event_id': item['event_id'], 'revision': item['revision']}
                    for item in envelope['events']]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.identity = {'available': True,
                         'account': {'source': 'codex', 'email': 'reviewer@example.test'},
                         'observed_at': '2026-05-01T00:00:00Z'}
        self.reader = patch.object(sharing, '_read_identity', return_value=self.identity)
        self.reader.start()
        self.addCleanup(self.reader.stop)
        scold.initialize(self.data, 'codex', Path(self.tmp.name) / 'native',
                         since='2026-05-01T00:00:00Z')
        endpoint = 'http://127.0.0.1:%s/events' % self.server.server_port
        with patch.object(scold, '_now', return_value=datetime(2026, 5, 1, tzinfo=timezone.utc)):
            sharing.configure(self.data, endpoint, consent=True)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def event(self):
        scold.ingest_messages(self.data, [{
            'platform': 'codex', 'conversation_id': 'private-conversation',
            'message_id': 'private-message', 'occurred_at': '2026-05-02T08:00:00Z',
            'text': 'DO_NOT_UPLOAD_raw_dialogue', 'source_ref': '/synthetic/private/path'}])
        ident = scold.event_id_for('codex', 'private-conversation', 'private-message')
        scold.review(self.data, [{'event_id': ident, 'status': 'scolding', 'note': 'private note'}])
        return ident

    def test_review_sync_correction_and_no_private_dialogue_in_request(self):
        ident = self.event()
        self.assertEqual(1, sharing.sync(self.data)['acknowledged'])
        self.assertEqual(1, len(self.remote))
        remote_id, first = next(iter(self.remote.items()))
        self.assertNotEqual(ident, remote_id)
        self.assertTrue(first['active'])
        wire = json.dumps(self.received)
        for private in ['DO_NOT_UPLOAD', '/synthetic/private', 'private-conversation',
                        'private-message', 'private note']:
            self.assertNotIn(private, wire)
        sharing.disable(self.data)
        scold.review(self.data, [{'event_id': ident, 'status': 'not_scolding', 'note': 'correction'}])
        self.assertFalse(sharing.sync(self.data)['enabled'])
        self.assertEqual(1, len(self.received))
        sharing.configure(self.data, 'http://127.0.0.1:%s/events' % self.server.server_port,
                          consent=True)
        self.assertEqual(1, sharing.sync(self.data)['acknowledged'])
        self.assertFalse(self.remote[remote_id]['active'])
        self.assertGreater(self.remote[remote_id]['revision'], first['revision'])
        sharing.sync(self.data)
        self.assertEqual(2, len(self.received))

    def test_retry_after_receiver_saved_but_failed_response_is_idempotent(self):
        self.event()
        self.fail_once = True
        failed = sharing.sync(self.data)
        self.assertEqual(1, failed['remaining'])
        self.assertEqual(1, len(self.remote))
        self.assertEqual(1, sharing.sync(self.data)['acknowledged'])
        self.assertEqual(1, len(self.remote))
        first, retry = [envelope['events'][0] for envelope in self.received]
        self.assertEqual(first, retry)


if __name__ == '__main__':
    unittest.main()
