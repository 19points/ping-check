import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DB_PATH = os.environ.get('DB_PATH', 'data/ping.db')


@contextmanager
def database():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY, address TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending', last_checked REAL, latency REAL
        );
        CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
            timestamp REAL NOT NULL, latency REAL
        );
        CREATE INDEX IF NOT EXISTS samples_target_time ON samples(target_id, timestamp);
        CREATE TABLE IF NOT EXISTS outages (
            id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
            started REAL NOT NULL, ended REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_outage ON outages(target_id) WHERE ended IS NULL;
        ''')


def ping(address):
    """Return round-trip milliseconds, or None when no echo reply arrives."""
    try:
        result = subprocess.run(['ping', '-n', '-c', '1', '-W', '1', address],
                                capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r'time[=<]([\d.]+)', result.stdout)
    if not match:
        raise RuntimeError('Could not parse successful ping output')
    return float(match.group(1))


def record_result(target_id, latency, timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    with database() as db:
        if not db.execute('SELECT id FROM targets WHERE id=?', (target_id,)).fetchone():
            return
        db.execute('INSERT INTO samples(target_id,timestamp,latency) VALUES(?,?,?)',
                   (target_id, timestamp, latency))
        if latency is None:
            db.execute('INSERT OR IGNORE INTO outages(target_id,started) VALUES(?,?)', (target_id, timestamp))
        else:
            db.execute('UPDATE outages SET ended=? WHERE target_id=? AND ended IS NULL', (timestamp, target_id))
        db.execute('UPDATE targets SET status=?,last_checked=?,latency=? WHERE id=?',
                   ('down' if latency is None else 'up', timestamp, latency, target_id))


def monitor(stop):
    # One in-flight probe per target; slow hosts never block checks of other hosts.
    active = {}
    next_cleanup = 0
    retention = int(os.environ.get('RETENTION_DAYS', '30')) * 86400
    with ThreadPoolExecutor(max_workers=32) as pool:
        while not stop.is_set():
            now = time.time()
            for target_id, future in list(active.items()):
                if future.done():
                    try:
                        future.result()
                    except Exception as error:
                        print(f'Probe error for target {target_id}: {error}', flush=True)
                    del active[target_id]
            with database() as db:
                targets = db.execute('SELECT * FROM targets').fetchall()
                if now >= next_cleanup:
                    db.execute('DELETE FROM samples WHERE timestamp < ?', (now - retention,))
                    next_cleanup = now + 3600
            for target in targets:
                interval = 1 if target['status'] == 'down' else 10
                if target['id'] not in active and (target['last_checked'] is None or now - target['last_checked'] >= interval):
                    active[target['id']] = pool.submit(check_target, target['id'], target['address'])
            stop.wait(0.1)


def check_target(target_id, address):
    started = time.time()
    latency = ping(address)
    record_result(target_id, latency, started)


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body, content_type='application/json'):
        data = json.dumps(body).encode() if content_type == 'application/json' else body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/':
            return self.respond(200, Path(__file__).with_name('index.html').read_bytes(), 'text/html; charset=utf-8')
        with database() as db:
            if path == '/api/targets':
                return self.respond(200, [dict(row) for row in db.execute('SELECT * FROM targets ORDER BY id')])
            match = re.fullmatch(r'/api/targets/(\d+)/history', path)
            if match:
                target_id = int(match.group(1))
                if not db.execute('SELECT id FROM targets WHERE id=?', (target_id,)).fetchone():
                    return self.respond(404, {'error': 'Target not found'})
                ranges = {'1h': 3600, '3h': 10800, '8h': 28800, '1d': 86400, '2d': 172800, '7d': 604800}
                period = parse_qs(urlsplit(self.path).query).get('range', ['1h'])[0]
                if period not in ranges:
                    return self.respond(400, {'error': 'Invalid range'})
                end = time.time()
                start = end - ranges[period]
                # At most 1,000 buckets; mixed success/failure buckets remain gaps.
                width = ranges[period] / 1000
                samples = db.execute('''
                    SELECT AVG(timestamp) AS timestamp,
                           CASE WHEN COUNT(latency)=COUNT(*) THEN AVG(latency) END AS latency
                    FROM samples WHERE target_id=? AND timestamp>=? AND timestamp<=?
                    GROUP BY CAST((timestamp-?)/? AS INTEGER) ORDER BY timestamp
                ''', (target_id, start, end, start, width)).fetchall()
                outages = db.execute('''SELECT started,ended FROM outages
                    WHERE target_id=? AND started<=? AND (ended IS NULL OR ended>=?)
                    ORDER BY started DESC''', (target_id, end, start)).fetchall()
                return self.respond(200, {'start': start, 'end': end,
                    'samples': [dict(row) for row in samples], 'outages': [dict(row) for row in outages]})
        self.respond(404, {'error': 'Not found'})

    def do_POST(self):
        if self.path != '/api/targets':
            return self.respond(404, {'error': 'Not found'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('Invalid request size')
            body = json.loads(self.rfile.read(length))
            address = str(ipaddress.ip_address(body['address'].strip()))
        except (ValueError, KeyError, TypeError, AttributeError):
            return self.respond(400, {'error': 'Provide a valid IPv4 or IPv6 address'})
        try:
            with database() as db:
                cursor = db.execute('INSERT INTO targets(address) VALUES(?)', (address,))
                target_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            return self.respond(409, {'error': 'Address already monitored'})
        self.respond(201, {'id': target_id, 'address': address})

    def do_DELETE(self):
        match = re.fullmatch(r'/api/targets/(\d+)', self.path)
        if not match:
            return self.respond(404, {'error': 'Not found'})
        with database() as db:
            deleted = db.execute('DELETE FROM targets WHERE id=?', (int(match.group(1)),)).rowcount
        self.respond(200 if deleted else 404, {'deleted': bool(deleted)})


if __name__ == '__main__':
    initialize()
    stop = threading.Event()
    worker = threading.Thread(target=monitor, args=(stop,), daemon=True)
    worker.start()
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8088'))), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        worker.join(timeout=5)
