import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import app


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = app.DB_PATH
        app.DB_PATH = self.temp.name + '/test.db'
        app.initialize()
        clock = patch('app.time.time', return_value=130)
        clock.start()
        self.addCleanup(clock.stop)
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        app.DB_PATH = self.old_path
        self.temp.cleanup()

    def request(self, path, method='GET', data=None):
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.server.server_port}{path}',
            data=json.dumps(data).encode() if data is not None else None,
            method=method, headers={'Content-Type': 'application/json'})
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_outage_lifecycle_and_delete(self):
        status, target = self.request('/api/targets', 'POST', {'address': '127.0.0.1'})
        self.assertEqual(status, 201)
        target_id = target['id']
        for timestamp, latency in [(100, 2.5), (110, None), (111, None), (112, 1.2), (122, None)]:
            app.record_result(target_id, latency, timestamp)
        status, history = self.request(f'/api/targets/{target_id}/history')
        self.assertEqual(status, 200)
        self.assertTrue(any(s['latency'] is None for s in history['samples']))
        self.assertEqual(history['end'] - history['start'], 3600)
        self.assertEqual(history['outages'], [{'started': 122, 'ended': None}, {'started': 110, 'ended': 112}])
        self.assertEqual(self.request('/api/targets')[1][0]['status'], 'down')
        self.assertEqual(self.request(f'/api/targets/{target_id}', 'DELETE')[0], 200)
        app.record_result(target_id, None, 123)  # In-flight probes after deletion are harmless.
        with app.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM outages').fetchone()[0], 0)

    def test_validation_duplicates_and_ipv6(self):
        self.assertEqual(self.request('/api/targets', 'POST', {'address': '; echo bad'})[0], 400)
        self.assertEqual(self.request('/api/targets', 'POST', {'address': '::1'})[0], 201)
        self.assertEqual(self.request('/api/targets', 'POST', {'address': '0:0:0:0:0:0:0:1'})[0], 409)
        self.assertEqual(self.request('/api/targets/999/history')[0], 404)

    def test_history_ranges(self):
        target = self.request('/api/targets', 'POST', {'address': '127.0.0.1'})[1]['id']
        app.record_result(target, None, 130 - 7200)
        app.record_result(target, 2, 130 - 5400)
        app.record_result(target, 3, 120)
        path = f'/api/targets/{target}/history'
        recent = self.request(path)[1]
        self.assertEqual(len(recent['samples']), 1)
        self.assertEqual(recent['outages'], [])
        for period, seconds in [('1h', 3600), ('3h', 10800), ('8h', 28800),
                                ('1d', 86400), ('2d', 172800), ('7d', 604800)]:
            status, history = self.request(f'{path}?range={period}')
            self.assertEqual(status, 200)
            self.assertEqual(history['end'] - history['start'], seconds)
            self.assertLessEqual(len(history['samples']), 1001)
            if period != '1h':
                self.assertEqual(len(history['outages']), 1)
        self.assertEqual(self.request(f'{path}?range=invalid')[0], 400)

    def test_ping(self):
        with patch('app.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '64 bytes from ::1: icmp_seq=1 ttl=64 time=0.031 ms'
            self.assertEqual(app.ping('::1'), 0.031)
            run.return_value.returncode = 1
            self.assertIsNone(app.ping('::1'))

    def test_open_outage_survives_reinitialization(self):
        target = self.request('/api/targets', 'POST', {'address': '127.0.0.1'})[1]['id']
        app.record_result(target, None, 100)
        app.initialize()
        app.record_result(target, None, 101)
        app.record_result(target, 1, 102)
        history = self.request(f'/api/targets/{target}/history')[1]
        self.assertEqual(history['outages'], [{'started': 100, 'ended': 102}])


if __name__ == '__main__':
    unittest.main()
