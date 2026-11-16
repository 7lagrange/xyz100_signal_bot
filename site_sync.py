"""Publish read-only market snapshots to this user's private ChatGPT Site."""

import json
import logging
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import requests

import bot
from dashboard import read_5m_candles, read_snapshot


class SiteSync:
    def __init__(self, state_dir, health_notifier=None):
        self.path = Path(state_dir) / 'signals.sqlite3'
        self.url, self.token = '', ''
        self.worker = None
        self.failures, self.retry_at = 0, 0
        self.last_5m_synced_slot = None
        self.health = bot.HealthMonitor('ChatGPT Site 同步', health_notifier) if health_notifier else None
        try:
            path = Path(state_dir) / 'config.json'
            config = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
            url = config.get('site_url', '').strip()
            token = config.get('site_service_token', '').strip()
            if not url and not token:
                return
            parsed = urlsplit(url)
            if (not token or '\r' in token or '\n' in token or parsed.scheme != 'https'
                    or not (parsed.hostname or '').endswith('.chatgpt.site')
                    or parsed.username or parsed.password or parsed.port not in (None, 443)
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
                raise ValueError('Invalid Site configuration')
            self.url, self.token = url.rstrip('/'), token
        except (ValueError, TypeError, AttributeError, OSError) as exc:
            logging.warning('Site sync disabled: check site_url and site_service_token in private configuration.')
            if self.health and not bot.market_closed_at(int(time.time() * 1000)):
                self.health.failure(f'Invalid Site configuration ({type(exc).__name__})',
                                    int(time.time() * 1000), immediate=True)

    def pause_health(self):
        if self.health:
            self.health.pause()

    def submit(self, now_ms):
        if (bot.market_closed_at(now_ms) or bot.market_closed_at(int(time.time() * 1000))
                or not self.url or time.monotonic() < self.retry_at
                or (self.worker and self.worker.is_alive())):
            return
        try:
            snapshot = read_snapshot(self.path, hours=720, now_ms=now_ms)
            if not snapshot['database_ready'] or snapshot['latest_minute'] is None:
                return
            five_minute_slot = now_ms // (5 * bot.STEPS['1m'])
            include_5m = five_minute_slot != self.last_5m_synced_slot
            if include_5m:
                snapshot['candles_5m'] = read_5m_candles(self.path, hours=72, now_ms=now_ms)
        except Exception as exc:
            logging.warning('Site snapshot could not be read (%s); collector continues.', type(exc).__name__)
            if self.health:
                self.health.failure(f'Snapshot read failed ({type(exc).__name__})',
                                    int(time.time() * 1000))
            return
        # One upload at a time, no backlog; the next cycle submits the newest complete snapshot.
        self.worker = threading.Thread(target=self.send_snapshot,
                                       args=(snapshot, five_minute_slot if include_5m else None), daemon=True)
        self.worker.start()

    def send_snapshot(self, snapshot, five_minute_slot=None):
        if bot.market_closed_at(int(time.time() * 1000)):
            logging.info('Site sync skipped during the scheduled weekend closure.')
            return False
        try:
            response = requests.post(self.url + '/api/snapshot',
                                     headers={'OAI-Sites-Authorization': f'Bearer {self.token}',
                                              'X-Snapshot-Token': self.token},
                                     json=snapshot, timeout=(3, 5), allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('Site did not accept snapshot')
            acknowledgement = response.json()
            if acknowledgement.get('ok') is not True or acknowledgement.get('latest_minute') != snapshot['latest_minute']:
                raise ValueError('Missing Site acknowledgement')
        except Exception as exc:
            response = getattr(exc, 'response', None)
            retry = bot.retry_after_seconds(response.headers.get('Retry-After')) if response is not None else 0
            self.failures += 1
            delay = bot.failure_delay(self.failures, bot.MarketDataError('Site sync failed', retry))
            self.retry_at = time.monotonic() + delay
            detail = f'HTTP {response.status_code}' if response is not None else type(exc).__name__
            logging.warning('Site sync failed (%s); retry in %ss. Collector and Slack continue.', detail, delay)
            if self.health and not bot.market_closed_at(int(time.time() * 1000)):
                status_code = response.status_code if response is not None else None
                self.health.failure(detail, int(time.time() * 1000),
                                    immediate=status_code in (401, 403, 429))
            return False
        self.failures, self.retry_at = 0, 0
        if five_minute_slot is not None:
            self.last_5m_synced_slot = five_minute_slot
        if self.health and not bot.market_closed_at(int(time.time() * 1000)):
            self.health.success(int(time.time() * 1000))
        logging.info('Site acknowledged minute %s.', bot.beijing(snapshot['latest_minute']))
        return True
