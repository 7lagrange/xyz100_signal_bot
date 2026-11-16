"""Hyperliquid XYZ100 intrabar SMA14/99 alerts. No trading/account API access."""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import random
import sqlite3
import sys
import time

import requests


COIN = 'xyz:XYZ100'
API_URL = 'https://api.hyperliquid.xyz/info'
SLACK_URL = 'https://slack.com/api/chat.postMessage'
STEPS = {'1m': 60_000, '30m': 1_800_000}
MAX_CANDLES_PER_REQUEST = 4_000
FAST, SLOW = 14, 99
GRACE_MS = 10_000
POLL_SECONDS = 60
BEIJING = timezone(timedelta(hours=8))
HERE = Path(__file__).resolve().parent
if os.name == 'nt':
    local_app_data = os.environ.get('LOCALAPPDATA')
    STATE_DIR = (Path(local_app_data) if local_app_data else Path.home() / 'AppData' / 'Local') / 'XYZ100SignalBot'
elif sys.platform == 'darwin':
    STATE_DIR = Path.home() / 'Library' / 'Application Support' / 'XYZ100SignalBot'
else:
    STATE_DIR = Path.home() / '.local' / 'state' / 'xyz100_signal_bot'


class DataError(RuntimeError):
    pass


class FreshnessLagError(DataError):
    """The API responded, but its latest candle has not caught up yet."""
    pass


class MarketDataError(DataError):
    def __init__(self, message, retry_after=0, status_code=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code


class DeliveryError(RuntimeError):
    def __init__(self, message, retry_after=60):
        super().__init__(message)
        self.retry_after = max(1, retry_after)


class HealthMonitor:
    def __init__(self, scope, notify, alert_after=3):
        self.scope = scope
        self.notify = notify
        self.alert_after = alert_after
        self.failures = 0
        self.started_at_ms = None
        self.last_error = ''
        self.alerted = False
        self.notice_due = False
        self.retry_notify_at_ms = 0

    def pause(self):
        self.failures = 0
        self.started_at_ms = None
        self.last_error = ''
        self.alerted = False
        self.notice_due = False
        self.retry_notify_at_ms = 0

    def failure(self, error, now_ms, immediate=False):
        if self.started_at_ms is None:
            self.started_at_ms = now_ms
        self.failures += 1
        self.last_error = str(error)[:160] or type(error).__name__
        if immediate or self.failures >= self.alert_after:
            self.notice_due = True
        if (self.notice_due and not self.alerted and now_ms >= self.retry_notify_at_ms):
            message = (f'[数据更新告警] {self.scope}连续失败 {self.failures} 次。\n'
                       f'开始时间：{beijing(self.started_at_ms)}\n'
                       f'最近错误：{self.last_error}\n程序会继续重试。')
            if self.notify(message):
                self.alerted = True
            self.retry_notify_at_ms = now_ms + 5 * 60_000

    def success(self, now_ms):
        if self.started_at_ms is None:
            self.failures = 0
            return
        if self.alerted and now_ms < self.retry_notify_at_ms:
            return
        if self.alerted or self.notice_due:
            seconds = max(0, (now_ms - self.started_at_ms) // 1000)
            duration = f'{seconds // 60} 分 {seconds % 60} 秒'
            if self.alerted:
                message = (f'[数据更新恢复] {self.scope}已恢复。\n'
                           f'故障持续：约 {duration}\n恢复时间：{beijing(now_ms)}')
            else:
                message = (f'[数据更新恢复] {self.scope}曾连续失败 {self.failures} 次，现已恢复。\n'
                           f'故障持续：约 {duration}\n最近错误：{self.last_error}\n'
                           f'恢复时间：{beijing(now_ms)}')
            if not self.notify(message):
                self.retry_notify_at_ms = now_ms + 5 * 60_000
                return
        self.failures = 0
        self.started_at_ms = None
        self.last_error = ''
        self.alerted = False
        self.notice_due = False
        self.retry_notify_at_ms = 0


def beijing(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, BEIJING).strftime('%Y-%m-%d %H:%M:%S +08:00')


def slack_time(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, BEIJING).strftime('%Y-%m-%d %H:%M:%S')


def slack_clock(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, BEIJING).strftime('%H:%M:%S')


def market_closed_at(milliseconds):
    local = datetime.fromtimestamp(milliseconds / 1000, BEIJING)
    if local.weekday() == 5:
        return local.hour >= 5
    if local.weekday() == 6:
        return True
    return local.weekday() == 0 and local.hour < 6


def next_market_open_ms(milliseconds):
    local = datetime.fromtimestamp(milliseconds / 1000, BEIJING)
    days = (0 - local.weekday()) % 7
    opening = local.replace(hour=6, minute=0, second=0, microsecond=0) + timedelta(days=days)
    if opening.timestamp() * 1000 <= milliseconds:
        opening += timedelta(days=7)
    return int(opening.timestamp() * 1000)


def next_market_close_ms(milliseconds):
    local = datetime.fromtimestamp(milliseconds / 1000, BEIJING)
    days = (5 - local.weekday()) % 7
    closing = local.replace(hour=5, minute=0, second=0, microsecond=0) + timedelta(days=days)
    if closing.timestamp() * 1000 <= milliseconds:
        closing += timedelta(days=7)
    return int(closing.timestamp() * 1000)


def next_open_30m_start(start_ms):
    candidate = start_ms + STEPS['30m']
    while market_closed_at(candidate):
        candidate += STEPS['30m']
    return candidate


def previous_open_30m_start(start_ms):
    candidate = start_ms - STEPS['30m']
    while market_closed_at(candidate):
        candidate -= STEPS['30m']
    return candidate


def next_open_minute_start(start_ms):
    candidate = start_ms + STEPS['1m']
    while market_closed_at(candidate):
        candidate += STEPS['1m']
    return candidate


def market_history_start(start_ms, prior_bars):
    candidate = start_ms
    for _ in range(prior_bars):
        candidate = previous_open_30m_start(candidate)
    return candidate


def latest_open_start(db, interval, limit):
    rows = db.execute('SELECT start FROM candles WHERE interval=? ORDER BY start DESC LIMIT ?',
                      (interval, limit)).fetchall()
    return next((row['start'] for row in rows if not market_closed_at(row['start'])), None)


def market_lookback_start(now_ms, minutes):
    candidate = now_ms // STEPS['1m'] * STEPS['1m']
    remaining = minutes
    while remaining:
        candidate -= STEPS['1m']
        if not market_closed_at(candidate):
            remaining -= 1
    return candidate


def retry_after_seconds(value):
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                return 0
            delay = deadline.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError, OSError, IndexError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0


def failure_delay(failures, error):
    if isinstance(error, FreshnessLagError):
        backoff = min(120, POLL_SECONDS * 2 ** min(max(0, failures - 1), 1))
        return math.ceil(max(backoff, getattr(error, 'retry_after', 0)) + random.uniform(0, 5))
    backoff = min(600, POLL_SECONDS * 2 ** min(max(0, failures - 1), 4))
    return math.ceil(max(backoff, getattr(error, 'retry_after', 0)) + random.uniform(0, 5))


def normalize_candles(payload, interval, now_ms, include_open=False):
    if not isinstance(payload, list) or len(payload) > 5000:
        raise DataError('Candle API must return a list with at most 5000 records.')
    step, result, seen = STEPS[interval], [], set()
    for item in payload:
        try:
            start, end, count = item['t'], item['T'], item['n']
            if item['s'] != COIN or item['i'] != interval:
                raise DataError('Wrong asset or candle interval returned by the API.')
            if (type(start) is not int or type(end) is not int or start <= 0
                    or start % step or end != start + step - 1
                    or type(count) is not int or count < 0):
                raise DataError('Invalid candle timestamps or trade count.')
            prices = [Decimal(str(item[key])) for key in ['o', 'h', 'l', 'c']]
            volume = Decimal(str(item['v']))
            if (not all(p.is_finite() and 0 < p < Decimal('1e12') for p in prices)
                    or not volume.is_finite() or volume < 0):
                raise DataError('Non-finite or invalid OHLC/volume.')
            opening, high, low, close = prices
            if high < max(prices) or low > min(prices):
                raise DataError('Inconsistent candle high/low.')
            if start in seen:
                raise DataError('Duplicate candle timestamp in one API response.')
            seen.add(start)
            if start > now_ms:
                raise DataError('API returned a candle from the future.')
            if include_open or end + 1 <= now_ms - GRACE_MS:
                result.append({'interval': interval, 'start': start, 'end': end,
                               'open': str(opening), 'high': str(high), 'low': str(low),
                               'close': str(close), 'volume': str(volume), 'count': count})
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise DataError('Malformed candle response.') from exc
    return sorted(result, key=lambda row: row['start'])


def fetch_candles(session, interval, start_ms, now_ms):
    if market_closed_at(now_ms):
        return []
    step = STEPS[interval]
    chunk_start = int(start_ms) // step * step
    result = []
    while chunk_start <= now_ms:
        if market_closed_at(chunk_start):
            chunk_start = next_market_open_ms(chunk_start)
            chunk_start = (chunk_start + step - 1) // step * step
            continue
        if market_closed_at(int(time.time() * 1000)):
            break
        market_close = next_market_close_ms(chunk_start)
        chunk_end = min(int(now_ms), chunk_start + MAX_CANDLES_PER_REQUEST * step - 1,
                        market_close - 1)
        try:
            response = session.post(API_URL, json={
                'type': 'candleSnapshot', 'req': {'coin': COIN, 'interval': interval,
                'startTime': chunk_start, 'endTime': chunk_end}}, timeout=(5, 20))
            response.raise_for_status()
        except requests.RequestException as exc:
            response = exc.response
            delay = retry_after_seconds(response.headers.get('Retry-After')) if response is not None else 0
            detail = f'HTTP {response.status_code}' if response is not None else type(exc).__name__
            status_code = response.status_code if response is not None else None
            raise MarketDataError(f'Hyperliquid request failed ({detail}).', delay, status_code) from exc
        server_date = response.headers.get('Date')
        if server_date:
            server_ms = parsedate_to_datetime(server_date).timestamp() * 1000
            if abs(server_ms - time.time() * 1000) > 120_000:
                raise DataError('Computer clock differs from the API by over 2 minutes; sync system time.')
        try:
            payload = response.json()
        except ValueError as exc:
            raise DataError('Hyperliquid returned invalid JSON; no prices were processed.') from exc
        result.extend(row for row in normalize_candles(
            payload, interval, now_ms, include_open=interval == '1m')
            if not market_closed_at(row['start']))
        if chunk_end >= now_ms:
            break
        chunk_start = chunk_end + 1
    return sorted(result, key=lambda row: row['start'])


def require_fresh(candles, now_ms):
    current_start = now_ms // STEPS['30m'] * STEPS['30m']
    expected = previous_open_30m_start(current_start)
    if not candles or candles[-1]['start'] != expected:
        raise FreshnessLagError('Latest completed 30m candle is missing/stale; no signals will be processed.')


def open_database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE IF NOT EXISTS candles (
            interval TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
            open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
            volume TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (interval, start)
        );
        CREATE TABLE IF NOT EXISTS strategy_state (
            stream TEXT PRIMARY KEY, last_bar INTEGER NOT NULL, relation INTEGER NOT NULL,
            ma14 TEXT NOT NULL, ma99 TEXT NOT NULL
        );
    ''')
    # Keep the old queue as a backup while migrating every sent/pending record atomically.
    with db:
        db.execute('BEGIN IMMEDIATE')
        columns = [row['name'] for row in db.execute('PRAGMA table_info(outbox)')]
        legacy = bool(columns) and 'id' not in columns
        if legacy:
            db.execute('ALTER TABLE outbox RENAME TO outbox_closed_v1')
        db.execute('''CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY, stream TEXT NOT NULL,
            bar_start INTEGER NOT NULL, observed_at INTEGER NOT NULL, kind TEXT NOT NULL,
            action TEXT NOT NULL,
            close TEXT NOT NULL, ma14 TEXT NOT NULL, ma99 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent')),
            slack_ts TEXT, retry_at INTEGER NOT NULL DEFAULT 0
        )''')
        if legacy:
            db.execute('''INSERT INTO outbox
                (stream, bar_start, observed_at, kind, action, close, ma14, ma99, status, slack_ts, retry_at)
                SELECT stream, bar_start, bar_start+?, 'closed', action, close, ma14, ma99,
                       status, slack_ts, retry_at FROM outbox_closed_v1 ORDER BY bar_start''', (STEPS['30m'],))
        for table in ('strategy_state', 'outbox'):
            db.execute(f"UPDATE {table} SET stream=REPLACE(stream, '|closed-v1|', '|intrabar-v2|') "
                       "WHERE INSTR(stream, '|closed-v1|')>0")
    return db


def save_candles(db, candles):
    with db:
        db.executemany('''INSERT INTO candles VALUES
            (:interval, :start, :end, :open, :high, :low, :close, :volume, :count)
            ON CONFLICT(interval, start) DO UPDATE SET
            end=excluded.end, open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume, count=excluded.count
            WHERE candles.interval='1m' ''', candles)


def aggregate_30m(minutes, now_ms):
    if not minutes:
        raise DataError('No minute data to build 30m candles.')
    step, minute = STEPS['30m'], STEPS['1m']
    first = (minutes[0]['start'] + step - 1) // step * step
    stop = (now_ms - GRACE_MS) // step * step
    by_start = {row['start']: row for row in minutes}
    result = []
    for start in range(first, stop, step):
        if market_closed_at(start):
            continue
        window = [by_start.get(t) for t in range(start, start + step, minute)]
        if any(row is None for row in window):
            raise DataError(f'Missing minute history at {beijing(start)}; cannot finalize a 30m candle.')
        # Empty minutes carry the previous price; they must not create a false open/high/low.
        traded = [row for row in window if row['count'] > 0] or window
        result.append({'interval': '30m', 'start': start, 'end': start + step - 1,
                       'open': traded[0]['open'], 'high': str(max(Decimal(r['high']) for r in traded)),
                       'low': str(min(Decimal(r['low']) for r in traded)), 'close': window[-1]['close'],
                       'volume': str(sum(Decimal(r['volume']) for r in window)),
                       'count': sum(r['count'] for r in window)})
    return result


def finalize_30m_candles(db, now_ms):
    maximum = latest_open_start(db, '30m', 1000)
    current_start = now_ms // STEPS['30m'] * STEPS['30m']
    expected = previous_open_30m_start(current_start)
    if maximum is not None and maximum >= expected:
        return
    lower = next_open_30m_start(maximum) if maximum is not None else 0
    minutes = db.execute("SELECT * FROM candles WHERE interval='1m' AND start>=? ORDER BY start",
                         (lower,)).fetchall()
    candles = aggregate_30m(minutes, now_ms)
    if maximum is not None and (not candles or candles[0]['start'] != lower):
        raise DataError('Minute history cannot bridge the last saved 30m candle; recover the missing data first.')
    require_fresh(candles, now_ms)
    save_candles(db, candles)


def stream_key(channel):
    return f'hyperliquid|{COIN}|30m|SMA14/99|intrabar-v2|{channel}'


def averages(window):
    if len(window) != SLOW or any(next_open_30m_start(a['start']) != b['start']
                                 for a, b in zip(window, window[1:])):
        raise DataError('Missing 30m history inside the SMA window; no synthetic candles are filled.')
    closes = [Decimal(row['close']) for row in window]
    fast_sum, slow_sum = sum(closes[-FAST:]), sum(closes)
    difference = fast_sum * SLOW - slow_sum * FAST
    relation = (difference > 0) - (difference < 0)
    return str(fast_sum / FAST), str(slow_sum / SLOW), relation


def process_signals(db, channel, now_ms, forming_candle):
    stream = stream_key(channel)
    with db:
        db.execute('BEGIN IMMEDIATE')
        state = db.execute('SELECT * FROM strategy_state WHERE stream=?', (stream,)).fetchone()
        stale_cursor = state is not None and market_closed_at(state['last_bar'])
        lower = market_history_start(state['last_bar'], SLOW if stale_cursor else SLOW - 1) if state else 0
        stored_rows = db.execute("SELECT * FROM candles WHERE interval='30m' AND start>=? AND end+1<=? ORDER BY start",
                                 (lower, now_ms - GRACE_MS)).fetchall()
        rows = [row for row in stored_rows if not market_closed_at(row['start'])]
        require_fresh(rows, now_ms)
        if len(rows) < (SLOW if stale_cursor else SLOW - 1):
            raise DataError(f'Need at least {SLOW - 1} consecutive closed 30m candles plus the forming candle.')
        if (forming_candle['start'] != now_ms // STEPS['30m'] * STEPS['30m']
                or forming_candle['start'] != next_open_30m_start(rows[-1]['start'])):
            raise DataError('Current price does not follow the latest closed 30m candle.')
        rows.append(forming_candle)
        if state is None:
            previous = 0
            for i in range(SLOW - 1, len(rows)):
                window = rows[i - SLOW + 1:i + 1]
                try:
                    ma14, ma99, current = averages(window)
                except DataError:
                    previous = 0
                    if i == len(rows) - 1:
                        raise
                    continue
                previous = current or previous
            db.execute('INSERT INTO strategy_state VALUES (?, ?, ?, ?, ?)',
                       (stream, rows[-1]['start'], previous, ma14, ma99))
            logging.info('Baseline initialized at %s, including the forming candle; no historical Slack messages.',
                         beijing(now_ms))
            return 0
        if stale_cursor:
            resume_start = next_open_30m_start(state['last_bar'])
            closed_rows = rows[:-1]
            anchor_index = next((i for i in range(len(closed_rows) - 1, -1, -1)
                                 if closed_rows[i]['start'] < resume_start), None)
            if anchor_index is None or anchor_index < SLOW - 1:
                raise DataError('Not enough valid history before the weekend to rebase the saved strategy state.')
            ma14, ma99, previous = averages(closed_rows[anchor_index - SLOW + 1:anchor_index + 1])
            cursor = closed_rows[anchor_index]['start']
            db.execute('UPDATE strategy_state SET last_bar=?, relation=?, ma14=?, ma99=? WHERE stream=?',
                       (cursor, previous, ma14, ma99, stream))
            logging.info('Rebased strategy from a previously stored weekend bar to the last open-market candle.')
        else:
            cursor, previous = state['last_bar'], state['relation']
        created = 0
        if cursor > forming_candle['start']:
            raise DataError('Strategy cursor is ahead of the current bar; check the computer clock.')
        for i, row in enumerate(rows):
            if row['start'] < cursor:
                continue
            if row['start'] > cursor and row['start'] != next_open_30m_start(cursor):
                raise DataError('Gap after the last processed bar; history must be recovered before alerts resume.')
            ma14, ma99, current = averages(rows[max(0, i - SLOW + 1):i + 1])
            if current and previous and current != previous:
                action = 'BUY' if current > 0 else 'SELL'
                forming = row['start'] == forming_candle['start']
                observed = now_ms if forming else row['start'] + STEPS['30m']
                db.execute('''INSERT INTO outbox
                    (stream, bar_start, observed_at, kind, action, close, ma14, ma99)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                           (stream, row['start'], observed, 'intrabar' if forming else 'closed',
                            action, row['close'], ma14, ma99))
                created += 1
            previous, cursor = current or previous, row['start']
            db.execute('UPDATE strategy_state SET last_bar=?, relation=?, ma14=?, ma99=? WHERE stream=?',
                       (cursor, previous, ma14, ma99, stream))
        return created


def message_text(row, now_ms):
    closed_at = row['bar_start'] + STEPS['30m']
    delayed = now_ms - row['observed_at'] > POLL_SECONDS * 1000
    forming = row['kind'] == 'intrabar'
    heading = '[补发信号]' if delayed else '[新信号]'
    stage = '盘中暂定，未收盘，可能反向' if forming else '收盘补算'
    timestamp_label = '检测时刻' if forming else 'K线收盘时刻'
    comparison = 'SMA14 上穿 SMA99' if row['action'] == 'BUY' else 'SMA14 下穿 SMA99'
    return (f"{heading} {row['action']} | XYZ100 | 30分钟 SMA14/99\n"
            f"状态：{stage}\n{timestamp_label}：{slack_time(row['observed_at'])}\n"
            f"30分钟区间：{slack_time(row['bar_start'])} → {slack_clock(closed_at)}\n"
            f"{'最新成交价快照' if forming else '收盘价'}：{row['close']} | "
            f"SMA14：{Decimal(row['ma14']):.4f} | SMA99：{Decimal(row['ma99']):.4f}\n"
            f"条件：{comparison}")


def post_slack(session, token, channel, text):
    response = session.post(SLACK_URL, headers={'Authorization': f'Bearer {token}'},
                            json={'channel': channel, 'text': text, 'unfurl_links': False,
                                  'unfurl_media': False}, timeout=(5, 20))
    if response.status_code == 429:
        delay = retry_after_seconds(response.headers.get('Retry-After')) or 60
        raise DeliveryError('Slack rate limited; message remains pending.', delay)
    response.raise_for_status()
    data = response.json()
    if data.get('ok') is not True or not isinstance(data.get('ts'), str) or not data['ts']:
        raise DeliveryError(f"Slack did not confirm delivery: {data.get('error', 'invalid_response')}")
    return data['ts']


def make_health_notifier(token, channel):
    def notify(message):
        try:
            with requests.Session() as session:
                session.headers['User-Agent'] = 'XYZ100SignalBot/2.0'
                post_slack(session, token, channel, message)
            logging.info('Slack data health notification acknowledged.')
            return True
        except Exception as exc:
            logging.warning('Slack data health notification failed (%s); will retry later.',
                            type(exc).__name__)
            return False
    return notify


def send_pending(db, channel, send, now_ms):
    sent = 0
    while True:
        retry = None
        with db:
            # Serialize sending across local runners, just as the original SQLite notifier does.
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM outbox WHERE stream=? AND status='pending' ORDER BY id LIMIT 1",
                             (stream_key(channel),)).fetchone()
            if row is None or row['retry_at'] > now_ms:
                return sent
            try:
                # shortcut: Slack/SQLite lack a shared transaction; use downstream idempotency if crash-window duplicates matter.
                slack_ts = send(channel, message_text(row, now_ms))
                if not isinstance(slack_ts, str) or not slack_ts:
                    raise DeliveryError('Missing Slack acknowledgement timestamp.')
            except DeliveryError as exc:
                db.execute('UPDATE outbox SET retry_at=? WHERE id=?',
                           (now_ms + exc.retry_after * 1000, row['id']))
                retry = exc
            else:
                db.execute("UPDATE outbox SET status='sent', slack_ts=? WHERE id=?",
                           (slack_ts, row['id']))
        if retry:
            raise retry
        sent += 1
        logging.info('Slack acknowledged %s %s', row['action'], beijing(row['bar_start'] + STEPS['30m']))
        time.sleep(1.1)


def run_cycle(db, session, channel, send, now_ms):
    if market_closed_at(now_ms):
        raise DataError('Scheduled market closure; no market request or signal calculation is due.')
    current_start = now_ms // STEPS['30m'] * STEPS['30m']
    if now_ms - current_start < GRACE_MS:
        raise DataError('Waiting 10 seconds for the previous 30m candle to settle; retry next poll.')
    state = db.execute('SELECT last_bar FROM strategy_state WHERE stream=?', (stream_key(channel),)).fetchone()
    last_minute = latest_open_start(db, '1m', 5000)
    warmup_ready = state is not None
    if not warmup_ready:
        stored_history = db.execute("SELECT start FROM candles WHERE interval='30m' ORDER BY start DESC LIMIT 1000").fetchall()
        history = [row for row in stored_history if not market_closed_at(row['start'])][:SLOW - 1]
        warmup_ready = (len(history) == SLOW - 1 and
                        all(next_open_30m_start(b['start']) == a['start']
                            for a, b in zip(history, history[1:])))
    start = (last_minute - STEPS['1m'] if warmup_ready and last_minute is not None
             else market_lookback_start(now_ms, 3300))
    minutes = fetch_candles(session, '1m', start, now_ms)
    if last_minute is not None and minutes and minutes[0]['start'] > next_open_minute_start(last_minute):
        logging.warning('1m archive has a gap; intrabar moves during downtime cannot be reconstructed.')
    save_candles(db, minutes)
    current = current_candle(minutes, now_ms)
    finalize_30m_candles(db, now_ms)
    created = process_signals(db, channel, now_ms, current)
    sent = send_pending(db, channel, send, now_ms)
    latest = db.execute('SELECT * FROM strategy_state WHERE stream=?', (stream_key(channel),)).fetchone()
    logging.info('Healthy: forming bar %s; latest price=%s SMA14=%s SMA99=%s; new=%d sent=%d',
                 beijing(latest['last_bar']), current['close'], latest['ma14'], latest['ma99'], created, sent)


def current_candle(minutes, now_ms):
    if not minutes or minutes[-1]['start'] != now_ms // STEPS['1m'] * STEPS['1m']:
        raise FreshnessLagError('Current minute candle is missing/stale; no signals will be processed.')
    return {'start': now_ms // STEPS['30m'] * STEPS['30m'], 'close': minutes[-1]['close']}


def load_config(path=None):
    path = Path(path) if path else STATE_DIR / 'config.json'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text((HERE / 'config.example.json').read_text(encoding='utf-8'), encoding='utf-8')
        if os.name != 'nt':
            path.chmod(0o600)
        raise ValueError(f'Created private configuration. Fill in Slack settings: {path}')
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    token = config.get('slack_bot_token', '').strip()
    channel = config.get('slack_channel', '').strip()
    if not token.startswith('xoxb-') or '\n' in token or '\r' in token or not channel:
        raise ValueError(f'Fill in slack_bot_token and slack_channel in {path}')
    return token, channel


def check_source(session, now_ms):
    if market_closed_at(now_ms):
        result = {'source': API_URL, 'coin': COIN, 'checked_at': beijing(now_ms),
                  'market_closed': True, 'next_open': beijing(next_market_open_ms(now_ms)),
                  'slack_sent': False, 'database_changed': False}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    minutes = fetch_candles(session, '1m', market_lookback_start(now_ms, 3300), now_ms)
    candles = aggregate_30m(minutes, now_ms)
    require_fresh(candles, now_ms)
    if len(candles) < SLOW - 1:
        raise DataError('Not enough 30m history to initialize SMA99.')
    current = current_candle(minutes, now_ms)
    ma14, ma99, relation = averages(candles[-(SLOW - 1):] + [current])
    result = {'source': API_URL, 'coin': COIN, 'checked_at': beijing(now_ms),
              'closed_30m_candles': len(candles), 'minute_candles': len(minutes),
              'latest_30m_close_time': beijing(candles[-1]['end'] + 1),
              'forming_30m_start': beijing(current['start']), 'forming_30m_close_time': beijing(current['start'] + STEPS['30m']),
              'price_minute': beijing(minutes[-1]['start']), 'latest_price': current['close'],
              'signal_mode': 'intrabar, polled every 60 seconds', 'sma14': ma14, 'sma99': ma99,
              'relation': relation, 'slack_sent': False, 'database_changed': False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def setup_logging():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [RotatingFileHandler(STATE_DIR / 'bot.log', maxBytes=5_000_000,
                                   backupCount=3, encoding='utf-8')]
    if sys.stderr is not None and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=handlers)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Read public market data only; no config/DB/Slack writes.')
    mode.add_argument('--once', action='store_true', help='One normal cycle, including real pending Slack delivery.')
    mode.add_argument('--test-slack', action='store_true', help='Send one explicitly labelled configuration test.')
    mode.add_argument('--dashboard', action='store_true', help='Open a read-only localhost chart; run the collector separately.')
    args = parser.parse_args()
    if args.dashboard:
        from dashboard import main as dashboard_main
        return dashboard_main()
    with requests.Session() as session:
        session.headers['User-Agent'] = 'XYZ100SignalBot/2.0'
        if args.check:
            check_source(session, int(time.time() * 1000))
            return 0
        if args.once and market_closed_at(int(time.time() * 1000)):
            print('Scheduled market closure in Asia/Shanghai; no request or signal calculation was run.')
            return 0
        setup_logging()
        try:
            token, channel = load_config()
        except Exception:
            logging.exception('Failed to load the private configuration.')
            raise
        send = lambda dest, text: post_slack(session, token, dest, text)
        notify_health = make_health_notifier(token, channel)
        if args.test_slack:
            ts = send(channel, '[配置测试] XYZ100 SMA14/99 通知程序已连接 Slack。这不是交易信号。')
            logging.info('Configuration test acknowledged: %s', ts)
            return 0
        with closing(open_database(STATE_DIR / 'signals.sqlite3')) as db:
            from site_sync import SiteSync
            site_sync = SiteSync(STATE_DIR, health_notifier=notify_health)
            market_health = HealthMonitor('Hyperliquid 行情请求', notify_health)
            failures, next_poll = 0, 0
            closure_logged = False
            while True:
                now_ms = int(time.time() * 1000)
                if market_closed_at(now_ms):
                    if not closure_logged:
                        logging.info('Scheduled weekend closure; market polling, calculations, and Site sync are paused until %s.',
                                     beijing(next_market_open_ms(now_ms)))
                        closure_logged = True
                    market_health.pause()
                    site_sync.pause_health()
                    next_poll = 0
                    time.sleep(min(300, max(1, (next_market_open_ms(now_ms) - now_ms) / 1000)))
                    continue
                if closure_logged:
                    logging.info('Scheduled market closure ended; resuming polling.')
                    closure_logged = False
                wait = next_poll - time.monotonic()
                if wait > 0:
                    time.sleep(min(wait, 60))
                    continue
                now_ms = int(time.time() * 1000)
                boundary_age = now_ms % STEPS['30m']
                if boundary_age < GRACE_MS:
                    settle_delay = (GRACE_MS - boundary_age) / 1000 + 0.05
                    logging.info('Waiting %.2fs for the new 30m candle to settle; this is not counted as a data failure.',
                                 settle_delay)
                    if args.once:
                        time.sleep(settle_delay)
                    else:
                        next_poll = time.monotonic() + settle_delay
                        continue
                started = time.monotonic()
                try:
                    run_cycle(db, session, channel, send, int(time.time() * 1000))
                except Exception as exc:
                    now_ms = int(time.time() * 1000)
                    if market_closed_at(now_ms):
                        market_health.pause()
                        site_sync.pause_health()
                        next_poll = 0
                        continue
                    if isinstance(exc, DataError):
                        failures += 1
                        delay = failure_delay(failures, exc)
                        market_health.failure(exc, now_ms,
                                              immediate=getattr(exc, 'status_code', None) in (401, 403, 429))
                    else:
                        # Slack failures retain their own queue retry time; market polling stays at 60s.
                        failures, delay = 0, POLL_SECONDS
                        market_health.success(now_ms)
                    logging.error('Cycle failed: %s: %s; next attempt in %ss.',
                                  type(exc).__name__, str(exc).replace(token, '<hidden>'), delay)
                    if args.once:
                        return 1
                else:
                    now_ms = int(time.time() * 1000)
                    if market_closed_at(now_ms):
                        market_health.pause()
                        site_sync.pause_health()
                        next_poll = 0
                        continue
                    failures = 0
                    delay = None
                    market_health.success(now_ms)
                site_sync.submit(int(time.time() * 1000))
                if delay is None:
                    delay = max(1, POLL_SECONDS - (time.monotonic() - started))
                if args.once:
                    return 0
                next_poll = time.monotonic() + delay


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        if logging.getLogger().handlers:
            logging.exception('Fatal startup failure.')
        elif sys.stderr is not None:
            print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
