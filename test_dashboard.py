from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import bot
import dashboard


BASE = 1767225600000
STEP = bot.STEPS['30m']
CHANNEL = '#dashboard-test'


def candles(prices, interval='30m', offset=0):
    step = bot.STEPS[interval]
    return [{'interval': interval, 'start': BASE + (i + offset) * step,
             'end': BASE + (i + offset + 1) * step - 1,
             'open': str(price), 'high': str(price), 'low': str(price),
             'close': str(price), 'volume': '1', 'count': 1}
            for i, price in enumerate(prices)]


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'signals.sqlite3'
        self.now = BASE + 130 * STEP + 12 * 60000 + 10000

    def populate(self):
        db = bot.open_database(self.path)
        self.addCleanup(db.close)
        bot.save_candles(db, candles([100] * 116 + [90] * 14))
        bot.save_candles(db, candles([1000] * 13, '1m', offset=130 * 30))
        bot.process_signals(db, CHANNEL, self.now, {'start': BASE + 130 * STEP, 'close': '90'})
        bot.process_signals(db, CHANNEL, self.now, {'start': BASE + 130 * STEP, 'close': '1000'})
        return db

    def test_missing_database_is_empty_and_never_created(self):
        result = dashboard.read_snapshot(self.path, now_ms=self.now)
        self.assertFalse(result['database_ready'])
        self.assertEqual(result['signals'], [])
        self.assertFalse(self.path.exists())

    def test_chart_matches_strategy_and_read_is_not_a_strategy_run(self):
        db = self.populate()
        before = self.path.read_bytes()
        result = dashboard.read_snapshot(self.path, hours=6, now_ms=self.now)
        self.assertEqual(len(result['candles']), 13)
        self.assertTrue(result['strategy_started'])
        self.assertTrue(result['candles'][-1]['forming'])
        state = db.execute('SELECT * FROM strategy_state').fetchone()
        self.assertEqual(result['candles'][-1]['ma14'], state['ma14'])
        self.assertEqual(result['candles'][-1]['ma99'], state['ma99'])
        self.assertEqual(result['candles'][-1]['relation'], state['relation'])
        self.assertEqual(result['signals'][0]['action'], 'BUY')
        self.assertEqual(result['signals'][0]['observed_at'], self.now)
        self.assertEqual(result['pending'], 1)
        self.assertNotIn('stream', result['signals'][0])
        self.assertNotIn('slack_ts', result['signals'][0])
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

    def test_history_gaps_do_not_get_connected_by_moving_averages(self):
        db = self.populate()
        with db:
            db.execute("DELETE FROM candles WHERE interval='30m' AND start=?", (BASE + 125 * STEP,))
        result = dashboard.read_snapshot(self.path, now_ms=self.now)
        self.assertIsNone(result['candles'][-1]['ma14'])
        self.assertIsNone(result['candles'][-1]['ma99'])

    def test_stopped_collector_keeps_original_forming_time(self):
        self.populate()
        result = dashboard.read_snapshot(self.path, now_ms=self.now + 2 * 3600000)
        self.assertEqual(result['candles'][-1]['start'], BASE + 130 * STEP)
        self.assertGreater(result['server_time'] - result['latest_minute'], 150000)

    def test_http_routes_are_bounded_and_do_not_serve_configuration(self):
        self.populate()
        server = dashboard.make_server(self.path, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def get(path, headers=None):
            with closing(http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)) as client:
                client.request('GET', path, headers=headers or {})
                response = client.getresponse()
                return response.status, dict(response.getheaders()), response.read()

        status, headers, body = get('/')
        self.assertEqual(status, 200)
        self.assertIn('XYZ100'.encode(), body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        status, _, body = get('/api/data?hours=6')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['signals'][0]['action'], 'BUY')
        self.assertEqual(get('/api/data?hours=9999')[0], 400)
        self.assertEqual(get('/config.json')[0], 404)
        self.assertEqual(get('/../config.json')[0], 404)
        self.assertEqual(get('/signals.sqlite3')[0], 404)
        self.assertEqual(get('/api/data', {'Host': 'foreign.example'})[0], 403)
        self.assertEqual(get('/api/data', {'Origin': 'https://foreign.example'})[0], 403)

    def test_dashboard_entry_does_not_load_credentials_or_start_collector(self):
        with patch('sys.argv', ['bot.py', '--dashboard']), \
             patch('dashboard.main', return_value=0) as entry, \
             patch('bot.load_config') as config, patch('bot.run_cycle') as cycle:
            self.assertEqual(bot.main(), 0)
        entry.assert_called_once()
        config.assert_not_called()
        cycle.assert_not_called()


if __name__ == '__main__':
    unittest.main()
