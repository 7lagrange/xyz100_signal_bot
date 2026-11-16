from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from decimal import Decimal
from email.utils import formatdate
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

import requests

import bot
from install_task import task_xml, NS


BASE = 1767225600000
STEP = bot.STEPS['30m']
CHANNEL = '#fut-trading-30mins'


def payload(prices, interval='30m', offset=0):
    step = bot.STEPS[interval]
    return [{'t': BASE + (i + offset) * step, 'T': BASE + (i + offset + 1) * step - 1,
             's': bot.COIN, 'i': interval, 'o': str(p), 'h': str(p), 'l': str(p),
             'c': str(p), 'v': '1', 'n': 1} for i, p in enumerate(prices)]


def clock(count):
    return BASE + count * STEP + bot.GRACE_MS


def minute_rows(prices, now_ms, live=90):
    raw = payload([p for p in prices for _ in range(30)] + [live], '1m')
    return bot.normalize_candles(raw, '1m', now_ms, include_open=True)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'signals.sqlite3'
        self.db = bot.open_database(self.path)
        self.addCleanup(self.db.close)
        self.prices = [100] * 86 + [90] * 14
        self.now = clock(100)
        self.save(self.prices)
        self.sleep = patch('bot.time.sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def save(self, prices):
        self.now = clock(len(prices))
        rows = bot.normalize_candles(payload(prices), '30m', self.now)
        bot.save_candles(self.db, rows)
        self.current = {'start': BASE + len(prices) * STEP, 'close': str(prices[-1])}
        return rows

    def process(self, db=None, price=None, now=None):
        current = self.current | ({'close': str(price)} if price is not None else {})
        return bot.process_signals(db or self.db, CHANNEL, now or self.now, current)

    def baseline(self):
        self.assertEqual(self.process(), 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)

    def cross(self, include_sell=False):
        prices = self.prices + [200, 200] + ([1] if include_sell else [])
        self.save(prices)
        return self.process()

    def test_closed_bars_only_and_first_boot_skips_history(self):
        raw = payload(self.prices + [10000])
        normalized = bot.normalize_candles(raw, '30m', self.now)
        self.assertEqual(len(normalized), 100)
        self.baseline()
        state = self.db.execute('SELECT * FROM strategy_state').fetchone()
        self.assertEqual(state['relation'], -1)
        self.assertEqual(state['last_bar'], BASE + 100 * STEP)

    def test_restart_dedup_and_new_cross_are_persistent(self):
        self.baseline()
        self.assertEqual(self.cross(), 1)
        sender = Mock(return_value='123.456')
        self.assertEqual(bot.send_pending(self.db, CHANNEL, sender, self.now), 1)
        with closing(bot.open_database(self.path)) as restarted:
            self.assertEqual(self.process(db=restarted), 0)
            self.assertEqual(bot.send_pending(restarted, CHANNEL, sender, self.now), 0)
        sender.assert_called_once()
        self.assertIn('BUY', sender.call_args.args[1])
        self.assertIn(bot.COIN, sender.call_args.args[1])

    def test_failure_keeps_outbox_and_retries(self):
        self.baseline()
        self.cross()
        with self.assertRaises(requests.Timeout):
            bot.send_pending(self.db, CHANNEL, Mock(side_effect=requests.Timeout()), self.now)
        self.assertEqual(self.db.execute('SELECT status FROM outbox').fetchone()[0], 'pending')
        sender = Mock(return_value='ok.ts')
        self.assertEqual(bot.send_pending(self.db, CHANNEL, sender, self.now), 1)

    def test_partial_success_survives_later_rate_limit(self):
        self.baseline()
        self.assertEqual(self.cross(include_sell=True), 2)
        sender = Mock(side_effect=['first.ts', bot.DeliveryError('rate limit', 120), 'second.ts'])
        with self.assertRaises(bot.DeliveryError):
            bot.send_pending(self.db, CHANNEL, sender, self.now)
        statuses = [row[0] for row in self.db.execute('SELECT status FROM outbox ORDER BY bar_start')]
        self.assertEqual(statuses, ['sent', 'pending'])
        self.assertEqual(bot.send_pending(self.db, CHANNEL, sender, self.now + 60_000), 0)
        self.assertEqual(bot.send_pending(self.db, CHANNEL, sender, self.now + 121_000), 1)
        self.assertEqual(sender.call_count, 3)

    def test_history_gap_blocks_signals_and_then_recovers(self):
        self.baseline()
        rows = bot.normalize_candles(payload([200], offset=101), '30m', clock(102))
        bot.save_candles(self.db, rows)
        with self.assertRaises(bot.DataError):
            bot.process_signals(self.db, CHANNEL, clock(102), {'start': BASE + 102 * STEP, 'close': '200'})
        self.assertEqual(self.db.execute('SELECT last_bar FROM strategy_state').fetchone()[0], BASE + 100 * STEP)
        self.assertEqual(self.cross(), 1)

    def test_stale_data_does_not_dispatch_pending_signals(self):
        self.baseline()
        self.cross()
        rows = [dict(row) for row in self.db.execute("SELECT * FROM candles WHERE interval='30m' ORDER BY start")]
        sender = Mock(return_value='unexpected')
        with patch('bot.fetch_candles', return_value=rows), self.assertRaises(bot.DataError):
            bot.run_cycle(self.db, None, CHANNEL, sender, self.now + STEP)
        sender.assert_not_called()

    def test_equal_mas_preserve_previous_direction(self):
        self.baseline()
        rows = self.save(self.prices + [90] * 99)
        self.process()
        ma14, ma99, relation = bot.averages(rows[-99:])
        self.assertEqual(Decimal(ma14), Decimal(ma99))
        self.assertEqual(relation, 0)
        self.assertEqual(self.db.execute('SELECT relation FROM strategy_state').fetchone()[0], -1)
        self.save(self.prices + [90] * 99 + [91])
        self.assertEqual(self.process(), 1)

    def test_delayed_messages_are_labelled(self):
        self.baseline()
        self.cross()
        row = self.db.execute('SELECT * FROM outbox').fetchone()
        message = bot.message_text(row, self.now + 2 * STEP)
        self.assertIn('补发信号', message)
        self.assertIn(bot.beijing(row['bar_start'] + STEP), message)

    def test_minute_failure_blocks_signals_and_dispatch(self):
        self.baseline()
        sender = Mock(return_value='123.456')
        with patch('bot.fetch_candles', side_effect=requests.Timeout()), self.assertRaises(requests.Timeout):
            bot.run_cycle(self.db, None, CHANNEL, sender, self.now)
        sender.assert_not_called()

    def test_partial_startup_cache_recovers_full_warmup(self):
        rows = minute_rows(self.prices, self.now)
        sender = Mock(return_value='unexpected')

        def fetch(session, interval, start, now):
            self.assertEqual(interval, '1m')
            return [row for row in rows if row['start'] >= start]

        with closing(bot.open_database(Path(self.temp.name) / 'partial.sqlite3')) as db:
            bot.save_candles(db, rows[-1:])
            with patch('bot.fetch_candles', side_effect=fetch):
                bot.run_cycle(db, None, CHANNEL, sender, self.now)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM strategy_state').fetchone()[0], 1)
        sender.assert_not_called()

    def test_clock_skew_blocks_candle_processing(self):
        session = Mock()
        session.post.return_value.headers = {'Date': formatdate(self.now / 1000 - 180, usegmt=True)}
        with patch('bot.time.time', return_value=self.now / 1000), self.assertRaises(bot.DataError):
            bot.fetch_candles(session, '30m', BASE, self.now)

    def test_market_rate_limit_is_not_retried_inside_request(self):
        session = Mock()
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = '180'
        session.post.return_value = response
        with self.assertRaises(bot.MarketDataError) as caught:
            bot.fetch_candles(session, '1m', BASE, self.now)
        self.assertEqual(caught.exception.retry_after, 180)
        self.assertIn('HTTP 429', str(caught.exception))
        session.post.assert_called_once()

    def test_retry_after_supports_seconds_dates_and_invalid_headers(self):
        with patch('bot.time.time', return_value=self.now / 1000):
            self.assertEqual(bot.retry_after_seconds('120'), 120)
            self.assertEqual(bot.retry_after_seconds(formatdate((self.now + 180000) / 1000, usegmt=True)), 180)
            self.assertEqual(bot.retry_after_seconds(formatdate((self.now - 1000) / 1000, usegmt=True)), 0)
        for value in (None, '', 'invalid', '-10', 'NaN', 'Infinity'):
            with self.subTest(header=value):
                self.assertEqual(bot.retry_after_seconds(value), 0)

    def test_market_backoff_caps_but_never_shortens_server_cooldown(self):
        with patch('bot.random.uniform', return_value=0):
            self.assertEqual([bot.failure_delay(i, bot.MarketDataError('timeout')) for i in range(1, 8)],
                             [60, 120, 240, 480, 600, 600, 600])
            self.assertEqual(bot.failure_delay(5, bot.MarketDataError('429', 3600)), 3600)
        with patch('bot.random.uniform', return_value=3.5):
            self.assertEqual(bot.failure_delay(1, bot.MarketDataError('timeout')), 64)

    def test_timeout_and_bad_json_are_market_failures(self):
        session = Mock()
        session.post.side_effect = requests.Timeout()
        with self.assertRaises(bot.MarketDataError):
            bot.fetch_candles(session, '1m', BASE, self.now)
        session.post.assert_called_once()
        session.post.side_effect = None
        session.post.return_value.headers = {}
        session.post.return_value.json.side_effect = ValueError('not JSON')
        with self.assertRaises(bot.DataError):
            bot.fetch_candles(session, '1m', BASE, self.now)

    def test_complete_warmup_cache_is_reused_without_strategy_state(self):
        rows = minute_rows(self.prices, self.now)
        bot.save_candles(self.db, rows)
        sender = Mock(return_value='unused')
        with patch('bot.fetch_candles', return_value=rows[-2:]) as fetch:
            bot.run_cycle(self.db, None, CHANNEL, sender, self.now)
        self.assertEqual(fetch.call_args.args[2], rows[-1]['start'] - 60000)
        sender.assert_not_called()

    def run_mocked_collector(self, errors):
        elapsed, calls, sleeps = [0], [], []

        def cycle(*args):
            calls.append(elapsed[0])
            if len(calls) > len(errors):
                raise KeyboardInterrupt()
            error = errors[len(calls) - 1]
            if error is not None:
                raise error

        def sleep(seconds):
            sleeps.append(seconds)
            elapsed[0] += seconds

        with patch('sys.argv', ['bot.py']), patch.object(bot, 'STATE_DIR', Path(self.temp.name)), \
             patch('bot.load_config', return_value=('xoxb-fake', CHANNEL)), patch('bot.setup_logging'), \
             patch('bot.run_cycle', side_effect=cycle), patch('bot.logging.error'), \
             patch('bot.time.monotonic', side_effect=lambda: elapsed[0]), \
             patch('bot.time.sleep', side_effect=sleep), patch('bot.random.uniform', return_value=0):
            with self.assertRaises(KeyboardInterrupt):
                bot.main()
        self.assertLessEqual(max(sleeps), 60)
        return calls

    def test_collector_obeys_cooldown_and_success_resets_backoff(self):
        calls = self.run_mocked_collector([bot.MarketDataError('429', 180), bot.MarketDataError('503'), None])
        self.assertEqual(calls, [0, 180, 300, 360])

    def test_slack_failure_does_not_slow_market_sampling(self):
        calls = self.run_mocked_collector([bot.DeliveryError('Slack429', 300), requests.Timeout(), None])
        self.assertEqual(calls, [0, 60, 120, 180])

    def test_two_local_runners_dispatch_once(self):
        self.baseline()
        self.cross()
        sender = Mock(return_value='once.ts')

        def run(_):
            with closing(bot.open_database(self.path)) as db:
                return bot.send_pending(db, CHANNEL, sender, self.now)

        with ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(run, range(2)))
        self.assertEqual(sum(counts), 1)
        sender.assert_called_once()

    def test_malformed_market_data_rejected(self):
        for changes in [{'s': 'BTC'}, {'i': '1m'}, {'h': '1'}, {'c': 'NaN'},
                        {'v': '-1'}, {'n': -1}, {'t': BASE + 1}, {'T': BASE + STEP}]:
            with self.subTest(changes=changes), self.assertRaises(bot.DataError):
                item = payload([100])[0] | changes
                bot.normalize_candles([item], '30m', clock(1))
        with self.assertRaises(bot.DataError):
            item = payload([100])[0]
            bot.normalize_candles([item, item], '30m', clock(1))

    def test_slack_requires_ack_and_honours_rate_limit(self):
        session = Mock()
        response = session.post.return_value
        response.status_code = 200
        response.json.return_value = {'ok': False, 'error': 'not_in_channel'}
        with self.assertRaises(bot.DeliveryError):
            bot.post_slack(session, 'xoxb-fake', CHANNEL, 'test')
        response.status_code = 429
        response.headers = {'Retry-After': '120'}
        with self.assertRaises(bot.DeliveryError) as caught:
            bot.post_slack(session, 'xoxb-fake', CHANNEL, 'test')
        self.assertEqual(caught.exception.retry_after, 120)

    def test_private_config_is_not_created_inside_code_folder(self):
        path = Path(self.temp.name) / 'private' / 'config.json'
        with self.assertRaises(ValueError):
            bot.load_config(path)
        self.assertEqual(json.loads(path.read_text())['slack_bot_token'], '')
        path.write_text(json.dumps({'slack_bot_token': 'xoxb-fake', 'slack_channel': CHANNEL}), encoding='utf-8-sig')
        self.assertEqual(bot.load_config(path), ('xoxb-fake', CHANNEL))

    def test_source_check_never_writes_database_or_slack(self):
        rows = minute_rows(self.prices, self.now)
        with patch('bot.fetch_candles', return_value=rows), patch('bot.post_slack') as slack:
            with redirect_stdout(StringIO()):
                result = bot.check_source(None, self.now)
        self.assertFalse(result['database_changed'])
        slack.assert_not_called()

    def test_intrabar_buy_sell_buy_has_three_ids_and_survives_restart(self):
        self.baseline()
        self.assertEqual(self.process(price=1000), 1)
        self.assertEqual(self.process(price=1001), 0)
        with closing(bot.open_database(self.path)) as restarted:
            self.assertEqual(self.process(db=restarted, price=1001), 0)
            self.assertEqual(self.process(db=restarted, price=90), 1)
            self.assertEqual(self.process(db=restarted, price=1000), 1)
        rows = self.db.execute('SELECT * FROM outbox ORDER BY id').fetchall()
        self.assertEqual([r['action'] for r in rows], ['BUY', 'SELL', 'BUY'])
        self.assertEqual(len({r['id'] for r in rows}), 3)
        self.assertEqual(len({r['bar_start'] for r in rows}), 1)
        self.assertTrue(all(r['kind'] == 'intrabar' for r in rows))
        message = bot.message_text(rows[0], self.now)
        self.assertIn('未收盘', message)
        self.assertIn(bot.beijing(self.now), message)
        self.assertEqual(bot.send_pending(self.db, CHANNEL, Mock(return_value='ok.ts'), self.now), 3)

    def test_forming_price_replaces_one_close_in_each_sma(self):
        self.baseline()
        self.process(price=1000)
        state = self.db.execute('SELECT * FROM strategy_state').fetchone()
        self.assertEqual(Decimal(state['ma14']), (sum(map(Decimal, self.prices[-13:])) + 1000) / 14)
        self.assertEqual(Decimal(state['ma99']), (sum(map(Decimal, self.prices[-98:])) + 1000) / 99)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM candles WHERE interval='30m'").fetchone()[0], 100)

    def test_first_baseline_includes_live_price_without_sending(self):
        self.assertEqual(self.process(price=1000), 0)
        self.assertEqual(self.db.execute('SELECT relation FROM strategy_state').fetchone()[0], 1)
        self.assertEqual(self.process(price=1000), 0)
        self.assertEqual(self.process(price=90), 1)

    def test_final_close_does_not_repeat_observed_intrabar_cross(self):
        self.baseline()
        self.process(price=1000)
        self.save(self.prices + [1000])
        self.assertEqual(self.process(), 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

    def test_final_close_can_reverse_last_intrabar_signal(self):
        self.baseline()
        self.process(price=1000)
        self.save(self.prices + [90])
        self.assertEqual(self.process(), 1)
        rows = self.db.execute('SELECT action, kind FROM outbox ORDER BY id').fetchall()
        self.assertEqual([tuple(r) for r in rows], [('BUY', 'intrabar'), ('SELL', 'closed')])

    def test_minute_aggregation_ohlcv_and_closed_bar_is_frozen(self):
        minutes = bot.normalize_candles(payload(list(range(1, 31)), '1m'), '1m', clock(1))
        candles = bot.aggregate_30m(minutes, clock(1))
        self.assertEqual(len(candles), 1)
        row = candles[0]
        self.assertEqual([Decimal(row[k]) for k in ('open', 'high', 'low', 'close', 'volume')],
                         [1, 30, 1, 30, 30])
        self.assertEqual(row['count'], 30)
        with closing(bot.open_database(Path(self.temp.name) / 'aggregate.sqlite3')) as db:
            bot.save_candles(db, minutes)
            bot.finalize_30m_candles(db, clock(1))
            corrected = dict(minutes[-1]) | {'high': '500', 'close': '500'}
            bot.save_candles(db, [corrected])
            bot.finalize_30m_candles(db, clock(1))
            self.assertEqual(db.execute("SELECT close FROM candles WHERE interval='30m'").fetchone()[0], '30')
        with self.assertRaises(bot.DataError):
            bot.aggregate_30m(minutes[:12] + minutes[13:], clock(1))
        quiet = [dict(r) for r in minutes]
        quiet[0].update(open='1000', high='1000', low='1000', close='1000', count=0, volume='0')
        row = bot.aggregate_30m(quiet, clock(1))[0]
        self.assertEqual([row[k] for k in ('open', 'high', 'low', 'close')], ['2', '30', '2', '30'])
        silent = bot.normalize_candles(payload([100] * 30, '1m'), '1m', clock(1))
        for r in silent:
            r.update(count=0, volume='0')
        row = bot.aggregate_30m(silent, clock(1))[0]
        self.assertEqual([row[k] for k in ('open', 'high', 'low', 'close')], ['100'] * 4)

    def test_each_normal_poll_only_fetches_incremental_minutes(self):
        with closing(bot.open_database(Path(self.temp.name) / 'incremental.sqlite3')) as db:
            initial = minute_rows(self.prices, self.now)
            sender = Mock(return_value='unused.ts')
            with patch('bot.fetch_candles', return_value=initial) as fetch:
                bot.run_cycle(db, None, CHANNEL, sender, self.now)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[1], '1m')
            self.assertEqual(fetch.call_args.args[2], self.now - 3300 * bot.STEPS['1m'])
            next_minute = bot.normalize_candles(payload([90], '1m', offset=3001), '1m', self.now + 60000, include_open=True)
            with patch('bot.fetch_candles', return_value=initial[-2:] + next_minute) as fetch:
                bot.run_cycle(db, None, CHANNEL, sender, self.now + 60000)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[1:3], ('1m', BASE + 2999 * 60000))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candles WHERE interval='30m'").fetchone()[0], 100)
            # Finish the current half hour locally; no native 30m API request is needed.
            tail = bot.normalize_candles(payload([90] * 31, '1m', offset=3000), '1m', clock(101), include_open=True)
            with patch('bot.fetch_candles', return_value=tail) as fetch:
                bot.run_cycle(db, None, CHANNEL, sender, clock(101))
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[1], '1m')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candles WHERE interval='30m'").fetchone()[0], 101)
            sender.assert_not_called()

    def test_current_minute_must_be_fresh_but_can_have_no_new_trades(self):
        rows = minute_rows(self.prices, self.now)
        with self.assertRaises(bot.DataError):
            bot.current_candle(rows[:-1], self.now)
        rows[-1]['count'] = 0  # The provider explicitly emits empty minute candles.
        self.assertEqual(bot.current_candle(rows, self.now)['close'], '90')

    def test_old_database_upgrade_preserves_state_and_pending_messages(self):
        path = Path(self.temp.name) / 'legacy.sqlite3'
        old_stream = bot.stream_key(CHANNEL).replace('|intrabar-v2|', '|closed-v1|')
        with closing(sqlite3.connect(path)) as old:
            old.executescript('''
                CREATE TABLE strategy_state (stream TEXT PRIMARY KEY, last_bar INTEGER NOT NULL,
                    relation INTEGER NOT NULL, ma14 TEXT NOT NULL, ma99 TEXT NOT NULL);
                CREATE TABLE outbox (stream TEXT NOT NULL, bar_start INTEGER NOT NULL, action TEXT NOT NULL,
                    close TEXT NOT NULL, ma14 TEXT NOT NULL, ma99 TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', slack_ts TEXT, retry_at INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(stream, bar_start));
            ''')
            old.execute('INSERT INTO strategy_state VALUES (?, ?, -1, ?, ?)', (old_stream, BASE + 99 * STEP, '90', '99'))
            old.execute('INSERT INTO outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (old_stream, BASE + 98 * STEP, 'BUY', '100', '101', '99', 'sent', 'old.ts', 0))
            old.execute('INSERT INTO outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (old_stream, BASE + 99 * STEP, 'SELL', '90', '90', '99', 'pending', None, 0))
            old.commit()
        with closing(bot.open_database(path)) as upgraded:
            self.assertEqual(upgraded.execute('SELECT stream FROM strategy_state').fetchone()[0], bot.stream_key(CHANNEL))
            self.assertEqual(upgraded.execute('SELECT COUNT(*) FROM outbox_closed_v1').fetchone()[0], 2)
            sender = Mock(return_value='new.ts')
            self.assertEqual(bot.send_pending(upgraded, CHANNEL, sender, self.now), 1)
            self.assertIn('SELL', sender.call_args.args[1])
        with closing(bot.open_database(path)) as reopened:
            self.assertEqual(reopened.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 2)
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0], 0)

    def test_windows_task_configuration(self):
        tree = ET.fromstring(task_xml('S-1-5-21-1', r'C:\Python\pythonw.exe',
                                     r'C:\Trading Folder\bot.py', r'C:\Trading Folder'))
        namespace = {'t': NS}
        self.assertEqual(tree.find('.//t:LogonType', namespace).text, 'InteractiveToken')
        self.assertEqual(tree.find('.//t:ExecutionTimeLimit', namespace).text, 'PT0S')
        self.assertEqual(tree.find('.//t:MultipleInstancesPolicy', namespace).text, 'IgnoreNew')
        self.assertEqual(tree.find('.//t:RestartOnFailure/t:Interval', namespace).text, 'PT1M')
        self.assertLessEqual(int(tree.find('.//t:RestartOnFailure/t:Count', namespace).text), 255)
        self.assertIn('"C:\\Trading Folder\\bot.py"', tree.find('.//t:Arguments', namespace).text)


if __name__ == '__main__':
    unittest.main()
