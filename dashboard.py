"""Read-only localhost view of the signal bot's existing SQLite database."""

from contextlib import closing
import csv
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser
import zipfile

import bot


HOUR = 3_600_000
STEP = bot.STEPS['30m']
FIVE_MINUTES = 5 * bot.STEPS['1m']
HTML = Path(__file__).with_name('dashboard.html')
RANGE_HOURS = (6, 24, 72, 168, 720)


def read_snapshot(path, hours=24, now_ms=None):
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    result = {'database_ready': False, 'server_time': now_ms, 'hours': hours,
              'latest_minute': None, 'strategy_started': False, 'pending': 0,
              'candles': [], 'signals': [], 'warning': None,
              'market_closed': bot.market_closed_at(now_ms)}
    path = Path(path)
    if not path.exists():
        return result
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        recent_minutes = db.execute(
            "SELECT * FROM candles WHERE interval='1m' ORDER BY start DESC LIMIT 5000").fetchall()
        latest = next((row for row in recent_minutes if not bot.market_closed_at(row['start'])), None)
        recent_bars = db.execute(
            "SELECT start FROM candles WHERE interval='30m' ORDER BY start DESC LIMIT 1000").fetchall()
        last_closed = next((row['start'] for row in recent_bars
                            if not bot.market_closed_at(row['start'])), None)
        if latest is None and last_closed is None:
            return result
        result['database_ready'] = True
        result['latest_minute'] = latest['start'] if latest else None
        anchor = max(latest['start'] // STEP * STEP if latest else 0, last_closed or 0)
        left = anchor - hours * HOUR
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM candles WHERE interval='30m' AND start>=? AND start<=? ORDER BY start",
            (bot.market_history_start(left // STEP * STEP, bot.SLOW - 1), anchor))
            if not bot.market_closed_at(r['start'])]
        for row in rows:
            row['forming'] = False
        if latest and latest['start'] // STEP * STEP > (last_closed or 0):
            start = latest['start'] // STEP * STEP
            minutes = db.execute("SELECT * FROM candles WHERE interval='1m' AND start>=? AND start<=? ORDER BY start",
                                 (start, latest['start'])).fetchall()
            traded = [r for r in minutes if r['count'] > 0] or minutes
            rows.append({'start': start, 'open': traded[0]['open'],
                         'high': str(max(Decimal(r['high']) for r in traded)),
                         'low': str(min(Decimal(r['low']) for r in traded)),
                         'close': latest['close'], 'forming': True})
            if len(minutes) != (latest['start'] - start) // bot.STEPS['1m'] + 1:
                result['warning'] = '当前半小时的分钟记录不完整，K 线仅展示已保存的部分。'
        prefix = bot.stream_key('')
        result['strategy_started'] = db.execute(
            'SELECT 1 FROM strategy_state WHERE stream LIKE ? LIMIT 1', (prefix + '%',)).fetchone() is not None
        result['pending'] = db.execute(
            "SELECT COUNT(*) FROM outbox WHERE stream LIKE ? AND status='pending'", (prefix + '%',)).fetchone()[0]
        signals = [dict(r) for r in db.execute('''
            SELECT id, bar_start, observed_at, kind, action, close AS price, ma14, ma99, status
            FROM outbox WHERE stream LIKE ? AND observed_at>=? ORDER BY id DESC LIMIT 5000
        ''', (prefix + '%', left))]
        result['signals'] = [row for row in signals if not bot.market_closed_at(row['bar_start'])][:1000]
    run = []
    for row in rows:
        if run and bot.next_open_30m_start(run[-1]['start']) != row['start']:
            run = []
        run.append(row)
        run = run[-bot.SLOW:]
        if len(run) == bot.SLOW:
            row['ma14'], row['ma99'], row['relation'] = bot.averages(run)
        else:
            row['ma14'] = (str(sum(Decimal(r['close']) for r in run[-bot.FAST:]) / bot.FAST)
                           if len(run) >= bot.FAST else None)
            row['ma99'], row['relation'] = None, None
        if row['start'] >= left:
            result['candles'].append({k: row[k] for k in ('start', 'open', 'high', 'low', 'close', 'forming', 'ma14', 'ma99', 'relation')})
    return result


def next_open_5m_start(start):
    candidate = start + FIVE_MINUTES
    while bot.market_closed_at(candidate):
        candidate += FIVE_MINUTES
    return candidate


def read_5m_candles(path, hours=72, now_ms=None):
    """Aggregate saved one-minute candles into a read-only five-minute chart series."""
    path = Path(path)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        recent = db.execute(
            "SELECT * FROM candles WHERE interval='1m' ORDER BY start DESC LIMIT 5000").fetchall()
        latest = next((row for row in recent if not bot.market_closed_at(row['start'])), None)
        if latest is None:
            return []
        anchor = latest['start']
        first = db.execute("SELECT MIN(start) FROM candles WHERE interval='1m'").fetchone()[0]
        left = first if hours is None else anchor - hours * HOUR
        history_left = max(first, left - 12 * HOUR)
        minute_rows = db.execute(
            "SELECT * FROM candles WHERE interval='1m' AND start>=? AND start<=? ORDER BY start",
            (history_left, anchor)).fetchall()

    groups = {}
    for row in minute_rows:
        if bot.market_closed_at(row['start']):
            continue
        start = row['start'] // FIVE_MINUTES * FIVE_MINUTES
        groups.setdefault(start, []).append(row)

    bars = []
    ordered_groups = sorted(groups.items())
    last_group_start = ordered_groups[-1][0]
    for start, minutes in ordered_groups:
        traded = [row for row in minutes if row['count'] > 0] or minutes
        complete = (len(minutes) == FIVE_MINUTES // bot.STEPS['1m'] and
                    all(b['start'] - a['start'] == bot.STEPS['1m'] for a, b in zip(minutes, minutes[1:])))
        bars.append({
            'start': start,
            'open': traded[0]['open'],
            'high': str(max(Decimal(row['high']) for row in traded)),
            'low': str(min(Decimal(row['low']) for row in traded)),
            'close': minutes[-1]['close'],
            'forming': start == last_group_start and not complete,
            '_valid': complete or start == last_group_start,
        })

    result = []
    run = []
    for row in bars:
        if not row['_valid']:
            run = []
            row['ma14'], row['ma99'], row['relation'] = None, None, None
            if row['start'] >= left:
                result.append({key: row[key] for key in (
                    'start', 'open', 'high', 'low', 'close', 'forming', 'ma14', 'ma99', 'relation')})
            continue
        if run and next_open_5m_start(run[-1]['start']) != row['start']:
            run = []
        run.append(row)
        run = run[-bot.SLOW:]
        fast = None
        if len(run) >= bot.FAST:
            fast = sum(Decimal(item['close']) for item in run[-bot.FAST:]) / bot.FAST
            row['ma14'] = str(fast)
        else:
            row['ma14'] = None
        if len(run) == bot.SLOW:
            slow = sum(Decimal(item['close']) for item in run) / bot.SLOW
            row['ma99'] = str(slow)
            row['relation'] = (fast * bot.SLOW > slow * bot.FAST) - (fast * bot.SLOW < slow * bot.FAST)
        else:
            row['ma99'], row['relation'] = None, None
        if row['start'] >= left:
            result.append({key: row[key] for key in (
                'start', 'open', 'high', 'low', 'close', 'forming', 'ma14', 'ma99', 'relation')})
    return result


def export_all(path):
    """Return a ZIP containing a consistent SQLite backup and CSVs for every user table."""
    source_path = Path(path)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    archive = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix='xyz100-export-') as temp_dir:
        backup_path = Path(temp_dir) / 'signals.sqlite3'
        with closing(sqlite3.connect(source_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as source:
            source.execute('PRAGMA query_only=ON')
            with closing(sqlite3.connect(backup_path)) as backup:
                source.backup(backup)
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(backup_path, 'signals.sqlite3')
            with closing(sqlite3.connect(backup_path)) as snapshot:
                tables = [row[0] for row in snapshot.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                for table in tables:
                    quoted = '"' + table.replace('"', '""') + '"'
                    columns = [row[1] for row in snapshot.execute(f'PRAGMA table_info({quoted})')]
                    output = io.StringIO(newline='')
                    writer = csv.writer(output, lineterminator='\n')
                    writer.writerow(columns)
                    for row in snapshot.execute(f'SELECT * FROM {quoted}'):
                        writer.writerow(row)
                    bundle.writestr(f'{table}.csv', output.getvalue().encode('utf-8-sig'))
            candles_5m = read_5m_candles(backup_path, hours=None)
            output = io.StringIO(newline='')
            writer = csv.writer(output, lineterminator='\n')
            writer.writerow(('start', 'open', 'high', 'low', 'close', 'forming', 'ma14', 'ma99', 'relation'))
            for row in candles_5m:
                writer.writerow(row[key] for key in (
                    'start', 'open', 'high', 'low', 'close', 'forming', 'ma14', 'ma99', 'relation'))
            bundle.writestr('candles_5m.csv', output.getvalue().encode('utf-8-sig'))
            bundle.writestr('README.txt',
                'XYZ100 本机数据导出\n'
                'signals.sqlite3 是一致性备份，可用 SQLite 工具打开。\n'
                '同名 CSV 包含数据库中每张用户数据表的全部行；candles_5m.csv 从分钟线派生。\n'
                'Unix 时间戳单位为毫秒；CSV 使用 UTF-8（含 BOM）。\n')
    return archive.getvalue()


def make_server(path, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            authority = self.headers.get('Host', '')
            if authority not in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}:
                self.send_error(403)
                return
            origin = self.headers.get('Origin')
            if origin and origin != f'http://{authority}':
                self.send_error(403)
                return
            url = urlsplit(self.path)
            if url.path == '/':
                body, content_type, status = HTML.read_bytes(), 'text/html; charset=utf-8', 200
            elif url.path == '/api/data':
                try:
                    query = parse_qs(url.query)
                    hours = int(query.get('hours', ['24'])[0])
                    interval = query.get('interval', ['30m'])[0]
                    if hours not in RANGE_HOURS:
                        raise ValueError('Unsupported range')
                    if interval not in ('30m', '5m') or (interval == '5m' and hours > 72):
                        raise ValueError('Unsupported interval or range')
                    body, status = read_snapshot(path, hours), 200
                    body['interval'] = interval
                    if interval == '5m':
                        body['candles'] = read_5m_candles(path, hours, body['server_time'])
                except (ValueError, IndexError):
                    body, status = {'error': '5 分钟图请选择 6、24 或 72 小时；30 分钟图可选至 30 天。'}, 400
                except sqlite3.Error:
                    body, status = {'error': '暂时无法读取数据库，请确认新版采集程序已运行并稍后重试。'}, 503
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
                content_type = 'application/json; charset=utf-8'
            elif url.path == '/api/export':
                try:
                    body, status = export_all(path), 200
                    content_type = 'application/zip'
                except FileNotFoundError:
                    body, status = json.dumps({'error': '尚未找到本机信号数据库。'}, ensure_ascii=False).encode('utf-8'), 404
                    content_type = 'application/json; charset=utf-8'
                except sqlite3.Error:
                    body, status = json.dumps({'error': '暂时无法导出数据库，请稍后重试。'}, ensure_ascii=False).encode('utf-8'), 503
                    content_type = 'application/json; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if url.path == '/api/export' and status == 200:
                self.send_header('Content-Disposition', 'attachment; filename="xyz100-all-data.zip"')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    with make_server(bot.STATE_DIR / 'signals.sqlite3') as server:
        url = f'http://localhost:{server.server_port}'
        print(f'XYZ100 dashboard: {url}\nRead-only view. Keep the collector running separately. Ctrl+C to stop.', flush=True)
        webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == '__main__':
    main()
