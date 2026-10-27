"""All channels/messages here are synthetic; no Telegram connection is made."""

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dealradar.models import IncomingPost, SearchRule
from dealradar.storage import Store, _SCHEMA
from dealradar.telegram_source import APPROVED_USERNAME, TEST_USERNAME, Channel, Monitor, SOURCE_KEY, SourceFailure

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def post(number, text='SSD NVMe 512GB 40$', edited=False):
    return IncomingPost(number, 'СИНТЕТИЧЕСКИЙ ТЕСТ\n' + text, NOW,
                        edited_at=NOW + timedelta(minutes=1) if edited else None)


class FakeReader:
    channel = Channel(APPROVED_USERNAME, 123456)

    def __init__(self):
        self.head = 10
        self.posts = {}
        self.failure = None
        self.calls = []

    async def latest_id(self):
        self.calls.append('latest')
        if self.failure:
            raise self.failure
        return self.head

    async def new_posts(self, after, upper):
        self.calls.append(('new', after, upper))
        return [self.posts[key] for key in sorted(self.posts) if after < key <= upper][:100]

    async def existing_posts(self, ids):
        self.calls.append(('edits', tuple(ids)))
        return {item: self.posts.get(item) for item in ids}


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.db = Path(self.temp.name) / 'synthetic.sqlite3'
        self.store = Store(self.db)
        self.reader = FakeReader()
        self.monitor = Monitor(self.store, self.reader, now=lambda: NOW)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def initialize(self):
        return await self.monitor.poll(start_now=True)

    async def test_explicit_boundary_no_history_import(self):
        self.reader.posts = {key: post(key) for key in range(1, 11)}
        with self.assertRaises(ValueError):
            await self.monitor.poll()
        self.assertEqual(self.reader.calls, [])
        result = await self.initialize()
        self.assertEqual(result['boundary_id'], 10)
        self.assertEqual(self.store.stats()['counts']['posts'], 0)
        self.assertEqual(self.store.telegram_status(SOURCE_KEY)['last_message_id'], 10)

    async def test_real_source_cannot_receive_manual_import(self):
        await self.initialize()
        with self.assertRaises(ValueError):
            self.store.process(SOURCE_KEY, post(11))
        with self.assertRaises(ValueError):
            self.store.process(SOURCE_KEY, post(10), origin='telegram')

    async def test_new_dedup_edit_sale_and_restart(self):
        await self.initialize()
        self.store.put_rule(SearchRule('synthetic', 'ТЕСТ', 'USD', '50'))
        self.reader.posts[11] = post(11)
        self.reader.head = 11
        first = await self.monitor.poll()
        self.assertEqual(first['new'], 1)
        self.assertEqual(len(self.store.jobs()), 1)
        self.reader.posts[11] = post(11, 'SSD NVMe 512GB 40$❌', True)
        second = await self.monitor.poll()
        self.assertEqual(second['edits'], 1)
        self.assertEqual(self.store.jobs()[0]['status'], 'cancelled')
        self.store.close()
        self.store = Store(self.db)
        monitor = Monitor(self.store, self.reader, now=lambda: NOW)
        await monitor.poll(start_now=True)
        self.assertEqual(self.store.stats()['counts']['posts'], 1)
        self.assertEqual(self.store.stats()['counts']['post_versions'], 2)
        self.assertEqual(self.store.telegram_status(SOURCE_KEY)['boundary_id'], 10)

    async def test_gap_is_recovered_in_ascending_pages(self):
        await self.initialize()
        self.reader.posts = {key: post(key) for key in range(11, 242)}
        self.reader.head = 241
        result = await self.monitor.poll()
        self.assertEqual((result['state'], result['new'], result['cursor']), ('ok', 231, 241))
        self.assertEqual([call[1] for call in self.reader.calls if isinstance(call, tuple) and call[0] == 'new'], [10, 110, 210])

    async def test_backlog_does_not_claim_complete_check(self):
        await self.initialize()
        checked = self.store.telegram_status(SOURCE_KEY)['last_successful_check_at']
        self.reader.posts = {key: post(key) for key in range(11, 161)}
        self.reader.head = 160
        with patch('dealradar.telegram_source.MAX_PAGES', 1):
            result = await self.monitor.poll()
        state = self.store.telegram_status(SOURCE_KEY)
        self.assertEqual(result['state'], 'catching_up')
        self.assertEqual(state['last_message_id'], 110)
        self.assertEqual(state['last_successful_check_at'], checked)
        self.assertEqual(state['check_state'], 'catching_up')
        self.assertEqual((await self.monitor.poll())['new'], 50)

    async def test_timeout_is_not_no_new_and_preserves_cursor(self):
        await self.initialize()
        checked = self.store.telegram_status(SOURCE_KEY)['last_successful_check_at']
        self.reader.failure = SourceFailure('timeout')
        result = await self.monitor.poll()
        state = self.store.telegram_status(SOURCE_KEY)
        self.assertEqual((result['state'], state['last_error_code']), ('error', 'timeout'))
        self.assertEqual(state['last_message_id'], 10)
        self.assertEqual(state['last_successful_check_at'], checked)
        self.reader.failure = None
        later = Monitor(self.store, self.reader, now=lambda: NOW + timedelta(seconds=31))
        self.assertEqual((await later.poll())['state'], 'ok')
        self.assertIsNone(self.store.telegram_status(SOURCE_KEY)['last_error_code'])

    async def test_flood_wait_survives_restart_without_network(self):
        await self.initialize()
        self.reader.failure = SourceFailure('rate_limited', 600)
        await self.monitor.poll()
        self.store.close()
        self.store = Store(self.db)
        self.reader.calls.clear()
        monitor = Monitor(self.store, self.reader, now=lambda: NOW + timedelta(seconds=100))
        self.assertEqual((await monitor.poll())['state'], 'paused')
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.store.auth_pause()['last_error_code'], 'rate_limited')

    async def test_auth_pause_is_monotonic_and_contains_no_account_data(self):
        self.store.pause_auth(NOW + timedelta(seconds=600), 'rate_limited')
        self.store.pause_auth(NOW + timedelta(seconds=60), 'login_attempt')
        pause = self.store.auth_pause()
        self.assertEqual(set(pause), {'retry_not_before', 'last_error_code'})
        self.assertEqual(pause['retry_not_before'], (NOW + timedelta(seconds=600)).isoformat())

    async def test_identity_change_and_disable_make_no_history_requests(self):
        await self.initialize()
        self.reader.calls.clear()
        self.reader.channel = Channel(APPROVED_USERNAME, 999999)
        self.assertEqual((await self.monitor.poll())['code'], 'identity_changed')
        self.assertEqual(self.reader.calls, [])
        self.reader.channel = Channel(APPROVED_USERNAME, 123456)
        self.store.set_source_enabled(SOURCE_KEY, False)
        self.assertEqual((await self.monitor.poll())['state'], 'disabled')
        self.assertEqual(self.reader.calls, [])

    async def test_inaccessible_post_hides_offer_preserving_original(self):
        await self.initialize()
        self.store.put_rule(SearchRule('synthetic', 'ТЕСТ', 'USD', '50'))
        self.reader.posts[11], self.reader.head = post(11), 11
        await self.monitor.poll()
        del self.reader.posts[11]
        result = await self.monitor.poll()
        self.assertEqual(result['unavailable'], 1)
        self.assertEqual(self.store.offers(decision='matched'), [])
        self.assertEqual(len(self.store.history(SOURCE_KEY, 11)), 1)
        self.assertEqual(self.store.jobs()[0]['status'], 'cancelled')

    async def test_partial_failure_does_not_skip_remaining_records(self):
        await self.initialize()
        self.reader.posts = {key: post(key) for key in (11, 12, 13)}
        self.reader.head = 13
        original = self.store.process
        def interrupt(key, value, **kwargs):
            if value.message_id == 12:
                raise SourceFailure('network_error')
            return original(key, value, **kwargs)
        with patch.object(self.store, 'process', side_effect=interrupt):
            await self.monitor.poll()
        self.assertEqual(self.store.telegram_status(SOURCE_KEY)['last_message_id'], 11)
        later = Monitor(self.store, self.reader, now=lambda: NOW + timedelta(seconds=31))
        self.assertEqual((await later.poll())['new'], 2)
        self.assertEqual(self.store.stats()['counts']['posts'], 3)

    async def test_bad_page_cannot_advance_cursor(self):
        await self.initialize()
        self.reader.head = 12
        async def malformed(*_): return [post(12), post(12)]
        self.reader.new_posts = malformed
        result = await self.monitor.poll()
        self.assertEqual(result['code'], 'protocol_error')
        self.assertEqual(self.store.telegram_status(SOURCE_KEY)['last_message_id'], 10)

    async def test_test_database_is_not_reused_as_live(self):
        self.store.add_test_source('test:fixture', 'Синтетика')
        with self.assertRaises(ValueError):
            await self.initialize()


class SeparateTestChannelTests(unittest.IsolatedAsyncioTestCase):
    """Synthetic messages and transport, including the approved test-channel path."""

    async def test_test_channel_new_edit_sale_dedup_and_offline_gap(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'synthetic-test-channel.sqlite3'
            key = 'telegram:' + TEST_USERNAME
            reader = FakeReader()
            reader.channel = Channel(TEST_USERNAME, 654321)
            with Store(path) as store:
                monitor = Monitor(store, reader, username=TEST_USERNAME, now=lambda: NOW)
                # An empty channel is a valid first boundary, not a fetch error.
                reader.head = 0
                self.assertEqual((await monitor.poll(start_now=True))['boundary_id'], 0)
                store.put_rule(SearchRule('synthetic', 'ТЕСТ', 'USD', '50'))
                reader.posts[1], reader.head = post(1), 1
                self.assertEqual((await monitor.poll())['new'], 1)
                self.assertEqual(len(store.offers(decision='matched')), 1)
                self.assertEqual((await monitor.poll())['new'], 0)
                self.assertEqual(len(store.history(key, 1)), 1)
                reader.posts[1] = post(1, 'SSD NVMe 512GB 45$', True)
                self.assertEqual((await monitor.poll())['edits'], 1)
                self.assertEqual(store.offers()[0]['offer']['price'], '45')
                reader.posts[1] = IncomingPost(1, 'СИНТЕТИЧЕСКИЙ ТЕСТ\nSSD NVMe 512GB 45$\nПРОДАНО ❌', NOW,
                                              edited_at=NOW + timedelta(minutes=2))
                self.assertEqual((await monitor.poll())['edits'], 1)
                self.assertEqual(store.offers(decision='matched'), [])
                self.assertEqual(store.jobs()[0]['status'], 'cancelled')
                self.assertIn('СИНТЕТИЧЕСКИЙ', store.sources()[0]['name'])
            # This message arrives while the database/monitor is closed.
            reader.posts[2], reader.head = post(2, 'SSD NVMe 1TB 49$'), 2
            with Store(path) as store:
                monitor = Monitor(store, reader, username=TEST_USERNAME, now=lambda: NOW)
                self.assertEqual((await monitor.poll(start_now=True))['new'], 1)
                self.assertEqual(store.telegram_status(key)['boundary_id'], 0)
                self.assertEqual(store.telegram_status(key)['last_message_id'], 2)
                self.assertEqual(store.stats()['counts']['posts'], 2)
                self.assertEqual(store.stats()['counts']['post_versions'], 4)
                self.assertEqual((await monitor.poll())['new'], 0)

    async def test_each_database_rejects_the_other_channel_before_reading(self):
        with TemporaryDirectory() as temp:
            for first, other in ((APPROVED_USERNAME, TEST_USERNAME), (TEST_USERNAME, APPROVED_USERNAME)):
                with self.subTest(first=first), Store(Path(temp) / (first + '.sqlite3')) as store:
                    store.bind_telegram(first, 123456, 10)
                    reader = FakeReader()
                    reader.channel = Channel(other, 654321)
                    monitor = Monitor(store, reader, username=other)
                    with self.assertRaises(ValueError):
                        await monitor.poll(start_now=True)
                    with self.assertRaises(ValueError):
                        store.bind_telegram(other, 654321, 0)
                    self.assertEqual(reader.calls, [])
                    self.assertEqual(len(store.sources()), 1)
                    self.assertEqual(store.telegram_status('telegram:' + first)['boundary_id'], 10)

    async def test_selected_channel_must_match_reader_and_pinned_numeric_id(self):
        with TemporaryDirectory() as temp, Store(Path(temp) / 'synthetic.sqlite3') as store:
            reader = FakeReader()
            with self.assertRaises(SourceFailure):
                Monitor(store, reader, username=TEST_USERNAME)
            with self.assertRaises(ValueError):
                Monitor(store, reader, username='unapproved')
            reader.channel = Channel(TEST_USERNAME, 654321)
            monitor = Monitor(store, reader, username=TEST_USERNAME)
            await monitor.poll(start_now=True)
            reader.calls.clear()
            reader.channel = Channel(TEST_USERNAME, 999999)
            self.assertEqual((await monitor.poll())['code'], 'identity_changed')
            self.assertEqual(reader.calls, [])


class MigrationTests(unittest.TestCase):
    def test_stage_one_history_survives_transactional_migration(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'old.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.executescript(_SCHEMA + '\nPRAGMA user_version=1;')
                db.execute("INSERT INTO sources(id,source_key,name,kind,last_message_id) VALUES (7,'test:old','ТЕСТ','test',42)")
                db.execute("INSERT INTO posts(id,source_id,message_id,published_at,first_detected_at,last_detected_at,revision,fingerprint) VALUES (9,7,42,'TEST','TEST','TEST',1,'TEST')")
                db.commit()
            with Store(path) as store:
                self.assertEqual(store.sources()[0]['last_message_id'], 42)
                self.assertEqual(store.stats()['counts']['posts'], 1)
                self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0], 3)
                self.assertEqual(store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
                self.assertEqual(store.connection.execute('PRAGMA foreign_keys').fetchone()[0], 1)

    def test_failed_migration_rolls_back_all_changes(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'old.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.executescript(_SCHEMA + '\nPRAGMA user_version=1;')
            with patch('dealradar.storage._LIVE_SCHEMA', 'THIS IS INVALID SQL;'):
                with self.assertRaises(sqlite3.Error):
                    Store(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='sources'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='sources_v2'").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
