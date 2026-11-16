import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from site_sync import SiteSync


class SiteSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'slack_bot_token': 'xoxb-dummy',
                       'site_url': 'https://xyz100.test.chatgpt.site',
                       'site_service_token': 'dummy-machine-token'}
        self.snapshot = {'database_ready': True, 'latest_minute': 1767225600000}

    def uploader(self):
        (self.root / 'config.json').write_text(json.dumps(self.config))
        return SiteSync(self.root)

    def test_missing_optional_configuration_makes_no_requests(self):
        sync = SiteSync(self.root)
        with patch('site_sync.requests.post') as post, patch('site_sync.read_snapshot') as read:
            sync.submit(1767225600000)
        post.assert_not_called()
        read.assert_not_called()
        configured = self.uploader()
        with patch('site_sync.read_snapshot', return_value={'database_ready': True, 'latest_minute': None}), \
             patch('site_sync.requests.post') as post:
            configured.submit(1767225600000)
        post.assert_not_called()

    def test_secret_goes_only_to_valid_https_site_and_never_in_payload(self):
        sync = self.uploader()
        response = Mock(status_code=200)
        response.json.return_value = {'ok': True, 'latest_minute': self.snapshot['latest_minute']}
        with patch('site_sync.requests.post', return_value=response) as post:
            self.assertTrue(sync.send_snapshot(self.snapshot))
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
        self.assertNotIn('xoxb-', json.dumps(post.call_args.kwargs['json']))
        self.assertEqual(post.call_args.kwargs['headers']['X-Snapshot-Token'], 'dummy-machine-token')
        for url in ('http://xyz100.test.chatgpt.site', 'https://other.example',
                    'https://xyz100.test.chatgpt.site/path', 'https://xyz100.test.chatgpt.site/?key=1'):
            with self.subTest(url=url), patch('site_sync.logging.warning'):
                self.config['site_url'] = url
                self.assertEqual(self.uploader().url, '')

    def test_site_cooldown_is_independent_and_honours_retry_after(self):
        sync = self.uploader()
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = '180'
        with patch('site_sync.requests.post', return_value=response), \
             patch('site_sync.time.monotonic', return_value=100), \
             patch('site_sync.bot.random.uniform', return_value=0), patch('site_sync.logging.warning'):
            self.assertFalse(sync.send_snapshot(self.snapshot))
            self.assertEqual(sync.retry_at, 280)
            with patch('site_sync.read_snapshot') as read:
                sync.submit(1767225600000)
            read.assert_not_called()

    def test_timeout_is_contained_and_success_resets_backoff(self):
        sync = self.uploader()
        with patch('site_sync.requests.post', side_effect=requests.Timeout()), patch('site_sync.logging.warning'):
            self.assertFalse(sync.send_snapshot(self.snapshot))
        self.assertEqual(sync.failures, 1)
        response = Mock(status_code=200)
        response.json.return_value = {'ok': True, 'latest_minute': self.snapshot['latest_minute']}
        with patch('site_sync.requests.post', return_value=response):
            self.assertTrue(sync.send_snapshot(self.snapshot))
        self.assertEqual((sync.failures, sync.retry_at), (0, 0))

    def test_slow_upload_does_not_block_collector_or_build_a_backlog(self):
        sync = self.uploader()
        entered, release = threading.Event(), threading.Event()
        response = Mock(status_code=200)
        response.json.return_value = {'ok': True, 'latest_minute': self.snapshot['latest_minute']}

        def slow_post(*args, **kwargs):
            entered.set()
            release.wait(2)
            return response

        with patch('site_sync.read_snapshot', return_value=self.snapshot), \
             patch('site_sync.requests.post', side_effect=slow_post) as post:
            try:
                sync.submit(1767225600000)
                self.assertTrue(entered.wait(1))
                self.assertTrue(sync.worker.is_alive())
                sync.submit(1767225660000)
                self.assertEqual(post.call_count, 1)
            finally:
                release.set()
                sync.worker.join(2)

    def test_redirects_and_unconfirmed_writes_are_not_success(self):
        sync = self.uploader()
        for status, acknowledgement in ((302, {}), (200, {'ok': True, 'latest_minute': 1})):
            with self.subTest(status=status), patch('site_sync.requests.post') as post, patch('site_sync.logging.warning'):
                response = post.return_value
                response.status_code = status
                response.json.return_value = acknowledgement
                self.assertFalse(sync.send_snapshot(self.snapshot))


if __name__ == '__main__':
    unittest.main()
