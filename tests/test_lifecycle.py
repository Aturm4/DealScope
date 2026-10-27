"""Synthetic restart/cancellation/privacy checks; no account or Telegram network."""

import asyncio
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import sqlite3
import subprocess
import sys
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dealradar.bot_api import BotFailure
from dealradar.models import IncomingPost, SearchRule
from dealradar.notifications import Dispatcher
from dealradar.storage import MaintenanceConflict, Store
from dealradar.process_lock import MonitorBusy, monitor_lock
from dealradar.models import utc_now, timestamp
from dealradar.telegram_source import APPROVED_USERNAME, Channel, Monitor, SourceFailure, TEST_USERNAME
from tests.test_notifications import BOT, OWNER, CHANNEL, SOURCE, PUBLISHED, Clock, FakeBot, FakeReader


class CancellationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / 'synthetic.sqlite3'
        self.store = Store(self.path)
        self.clock = Clock()
        self.store.bind_telegram(TEST_USERNAME, CHANNEL, 10)
        self.rule = SearchRule('synthetic', 'СИНТЕТИЧЕСКИЙ пресет', 'USD', Decimal('50'))
        self.store.put_rule(self.rule)
        self.store.link_notifier(BOT, OWNER, now=self.clock)
        self.reader, self.bot = FakeReader(), FakeBot()
        self.post = IncomingPost(11, 'СИНТЕТИЧЕСКИЙ ТЕСТ SSD NVMe 512GB 40$', PUBLISHED)
        self.reader.posts[11] = self.post
        self.store.process(SOURCE, self.post, origin='telegram')
        self.claimed = self.store.claim_notification(SOURCE, 11, BOT, OWNER, now=self.clock)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def finish_rejected(self):
        self.store.finish_notification(self.claimed['group_id'],
                                       failure=BotFailure('rate_limited', retry_after=90), now=self.clock)
        self.store.close()
        self.store = Store(self.path)
        self.clock.advance()
        self.assertEqual(asyncio.run(Dispatcher(self.store, self.bot, SOURCE, now=self.clock,
                                               sleep=self.clock.sleep).tick(self.reader))['sent'], 0)
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.store.jobs()[0]['status'], 'cancelled')

    def test_disabled_source_cannot_revive_after_429_or_restart(self):
        self.store.set_source_enabled(SOURCE, False)
        self.store.set_source_enabled(SOURCE, True)
        self.finish_rejected()
        self.assertEqual(self.store.jobs()[0]['reason'], 'source_disabled')

    def test_disabled_notifier_cannot_revive_after_relink_and_429(self):
        self.store.disable_notifier()
        self.store.link_notifier(BOT, OWNER, now=self.clock)
        self.finish_rejected()

    def test_changed_preset_cannot_revive_an_old_request_after_429(self):
        self.store.put_rule(replace(self.rule, max_price=Decimal('45')))
        self.finish_rejected()
        self.assertEqual(self.store.jobs()[0]['reason'], 'rule_changed')

    def test_sale_while_sending_cannot_be_retried_after_429(self):
        self.store.process(SOURCE, replace(self.post, text=self.post.text + '\nПРОДАНО ❌',
                                          edited_at=self.clock()), origin='telegram')
        self.finish_rejected()
        self.assertEqual(self.store.jobs()[0]['reason'], 'no_current_match')

    def test_confirmed_success_after_pause_is_still_recorded_as_delivered(self):
        self.store.set_source_enabled(SOURCE, False)
        self.store.finish_notification(self.claimed['group_id'], message_id=123, now=self.clock)
        job = self.store.jobs()[0]
        self.assertEqual((job['status'], job['reason'], job['telegram_message_id']),
                         ('sent', 'telegram_confirmed', 123))

    def test_ambiguous_delivery_after_pause_stays_unknown(self):
        self.store.disable_notifier()
        self.store.finish_notification(self.claimed['group_id'],
                                       failure=BotFailure('network_error', ambiguous=True), now=self.clock)
        self.assertEqual(self.store.jobs()[0]['status'], 'unknown')
        self.assertEqual(self.store.jobs()[0]['reason'], 'delivery_unconfirmed')


class PrivacyLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / 'synthetic.sqlite3'
        self.store = Store(self.path)
        self.store.bind_telegram(TEST_USERNAME, CHANNEL, 10)
        self.rule = SearchRule('synthetic', 'СИНТЕТИЧЕСКИЙ пресет', 'USD', Decimal('50'))
        self.store.put_rule(self.rule)
        self.clock = Clock()
        self.store.link_notifier(BOT, OWNER, now=self.clock)
        self.reader = FakeReader()
        for item in (11, 12):
            post = IncomingPost(item, 'СИНТЕТИЧЕСКИЙ ТЕСТ SSD NVMe 512GB 40$', PUBLISHED)
            self.reader.posts[item] = post
            self.store.process(SOURCE, post, origin='telegram')
        self.store.checked_telegram(SOURCE, 12)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_clear_removes_all_text_and_delivery_history_preserving_settings_and_cursor(self):
        claimed = self.store.claim_notification(SOURCE, 11, BOT, OWNER, now=self.clock)
        self.store.finish_notification(claimed['group_id'], message_id=123, now=self.clock)
        self.store.advance_edit_scan(SOURCE, 11)
        pause = utc_now() + timedelta(seconds=600)
        self.store.pause_auth(pause, 'rate_limited')
        self.store.telegram_retry(SOURCE, 'rate_limited', pause)
        before_rule, before_notifier = self.store.rules(), self.store.notifier()
        result = self.store.clear_history('УДАЛИТЬ')
        self.assertEqual(result['deleted']['posts'], 2)
        for table in ('posts', 'post_versions', 'offers', 'evaluations', 'notification_jobs'):
            self.assertEqual(self.store.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        self.assertEqual(self.store.rules(), before_rule)
        self.assertEqual(self.store.notifier()['owner_id'], before_notifier['owner_id'])
        self.assertEqual(self.store.notifier()['not_before'], before_notifier['not_before'])
        self.assertIsNone(self.store.notifier()['last_successful_send_at'])
        self.store.close()
        self.store = Store(self.path)
        status = self.store.telegram_status(SOURCE)
        self.assertEqual((status['last_message_id'], status['boundary_id'], status['edit_before_id']), (12, 12, None))
        self.assertEqual(status['retry_not_before'], timestamp(pause))
        self.assertEqual(self.store.auth_pause()['retry_not_before'], timestamp(pause))
        self.assertEqual(self.store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_clear_then_restart_skips_old_edits_and_delivers_only_a_new_post_once(self):
        self.store.clear_history('УДАЛИТЬ')
        self.store.close()
        self.store = Store(self.path)
        self.reader.posts[11] = replace(self.reader.posts[11], text='ПРОДАНО ❌', edited_at=self.clock())
        monitor = Monitor(self.store, self.reader, username=TEST_USERNAME, now=self.clock)
        self.assertEqual(asyncio.run(monitor.poll())['new'], 0)
        with self.assertRaises(ValueError):
            self.store.process(SOURCE, self.reader.posts[11], origin='telegram')
        self.reader.posts[13] = IncomingPost(13, 'СИНТЕТИЧЕСКИЙ ТЕСТ SSD NVMe 1TB 49$', PUBLISHED)
        self.assertEqual(asyncio.run(monitor.poll())['new'], 1)
        self.assertEqual(asyncio.run(monitor.poll())['new'], 0)
        bot = FakeBot()
        dispatcher = Dispatcher(self.store, bot, SOURCE, now=self.clock, sleep=self.clock.sleep)
        self.assertEqual(asyncio.run(dispatcher.tick(self.reader))['sent'], 1)
        self.clock.advance()
        self.assertEqual(asyncio.run(dispatcher.tick(self.reader))['sent'], 0)
        self.assertEqual(len(bot.calls), 1)

    def test_clear_and_unlink_require_typed_confirmation_and_stopped_monitor(self):
        for value in ('', 'удалить', True, None):
            with self.assertRaises(ValueError):
                self.store.clear_history(value)
        with self.assertRaises(ValueError):
            self.store.forget_notifier('yes')
        with monitor_lock(self.path):
            with self.assertRaises(MonitorBusy):
                self.store.clear_history('УДАЛИТЬ')
            with self.assertRaises(MonitorBusy):
                self.store.forget_notifier('ОТВЯЗАТЬ')
        self.assertEqual(self.store.stats()['counts']['posts'], 2)

    def test_failed_delete_rolls_back_history_and_lower_boundary_together(self):
        self.store.connection.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON posts BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.Error):
            self.store.clear_history('УДАЛИТЬ')
        self.assertEqual(self.store.stats()['counts']['posts'], 2)
        self.assertEqual(self.store.telegram_status(SOURCE)['boundary_id'], 10)
        self.assertEqual(len(self.store.jobs()), 2)

    def test_busy_wal_is_reported_without_claiming_that_committed_deletion_failed(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as reader:
            reader.execute('BEGIN')
            self.assertEqual(reader.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 2)
            self.store.connection.execute('PRAGMA busy_timeout=5')
            result = self.store.clear_history('УДАЛИТЬ')
            self.assertTrue(result['cleared'])
            self.assertFalse(result['checkpoint_complete'])
            self.assertEqual(self.store.stats()['counts']['posts'], 0)
            reader.rollback()

    def test_unlink_removes_binding_and_delivery_records_but_not_offers_or_rules(self):
        result = self.store.forget_notifier('ОТВЯЗАТЬ')
        self.assertTrue(result['forgotten'])
        self.store.close()
        self.store = Store(self.path)
        self.assertIsNone(self.store.notifier())
        self.assertEqual(self.store.jobs(), [])
        self.assertEqual(self.store.stats()['counts']['posts'], 2)
        self.assertEqual(self.store.rules()[0]['config']['key'], self.rule.key)
        self.store.recheck()
        self.assertEqual(self.store.jobs(), [])

    def test_unlink_cannot_discard_an_active_platform_pause(self):
        self.store.connection.execute('UPDATE notifier_settings SET not_before=?', (timestamp(utc_now() + timedelta(minutes=5)),))
        with self.assertRaises(MaintenanceConflict):
            self.store.forget_notifier('ОТВЯЗАТЬ')
        self.assertIsNotNone(self.store.notifier())
        self.assertEqual(len(self.store.jobs()), 2)

    def test_stale_ui_cannot_resolve_a_reused_job_id_after_clear(self):
        self.store.clear_history('УДАЛИТЬ')
        post = IncomingPost(13, 'СИНТЕТИЧЕСКИЙ ТЕСТ SSD NVMe 512GB 40$', PUBLISHED)
        self.store.process(SOURCE, post, origin='telegram')
        claim = self.store.claim_notification(SOURCE, 13, BOT, OWNER, now=self.clock)
        self.store.finish_notification(claim['group_id'], failure=BotFailure('network_error', ambiguous=True), now=self.clock)
        with self.assertRaises(ValueError):
            self.store.resolve_notification(claim['group_id'], 'sent', expected_source=SOURCE, expected_message_id=11)
        self.assertEqual(self.store.jobs()[0]['status'], 'unknown')
        self.store.resolve_notification(claim['group_id'], 'skip', expected_source=SOURCE, expected_message_id=13)
        self.assertEqual(self.store.jobs()[0]['status'], 'cancelled')

    def test_process_crash_releases_monitor_lock_without_credential_files(self):
        child_code = 'import os,sys; from dealradar.process_lock import monitor_lock; lock=monitor_lock(sys.argv[1]); lock.__enter__(); print("locked",flush=True); os._exit(17)'
        result = subprocess.run([sys.executable, '-B', '-c', child_code, str(self.path)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(result.stdout.strip(), 'locked')
        with monitor_lock(self.path):
            pass

    def test_failure_of_one_source_does_not_change_another_database_or_cursor(self):
        self.reader.latest_id = AsyncMock(side_effect=SourceFailure('network_error'))
        first = asyncio.run(Monitor(self.store, self.reader, username=TEST_USERNAME).poll())
        self.assertEqual((first['state'], first['code']), ('error', 'network_error'))
        with Store(Path(self.temp.name) / 'other-synthetic.sqlite3') as other:
            other.bind_telegram(APPROVED_USERNAME, CHANNEL + 1, 10)
            reader = FakeReader()
            reader.channel = Channel(APPROVED_USERNAME, CHANNEL + 1)
            reader.posts[11] = IncomingPost(11, 'СИНТЕТИЧЕСКИЙ ТЕСТ SSD NVMe 512GB 40$', PUBLISHED)
            result = asyncio.run(Monitor(other, reader, username=APPROVED_USERNAME).poll())
            self.assertEqual((result['state'], result['new'], result['cursor']), ('ok', 1, 11))
            self.assertEqual(self.store.telegram_status(SOURCE)['last_error_code'], 'network_error')

    def test_successful_checkpoint_removes_synthetic_contact_from_database_and_wal(self):
        marker = 'SYNTHETIC_CONTACT_ERASURE_284edb5d'
        post = IncomingPost(13, 'SSD NVMe 512GB 40$ ' + marker, PUBLISHED)
        self.store.process(SOURCE, post, origin='telegram')
        self.assertTrue(self.store.clear_history('УДАЛИТЬ')['checkpoint_complete'])
        for path in (self.path, Path(str(self.path) + '-wal')):
            if path.exists():
                self.assertNotIn(marker.encode(), path.read_bytes())


class WatchRecoveryTests(unittest.TestCase):
    def run_watch(self, results, *, watch=True, pause_on_sleep=False):
        from dealradar.telegram_commands import run_monitor
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'synthetic-watch.sqlite3'
            with Store(path) as store:
                store.bind_telegram(TEST_USERNAME, CHANNEL, 10)
            args = SimpleNamespace(db=path, channel=TEST_USERNAME, interval=60,
                                   start_now=False, watch=watch, notify=False)
            events = []
            monitor = SimpleNamespace(poll=AsyncMock(side_effect=results))
            login, close = AsyncMock(return_value=OWNER), AsyncMock(return_value=True)
            async def fake_sleep(seconds):
                if pause_on_sleep:
                    with Store(path) as store:
                        store.set_source_enabled(SOURCE, False)
            sleep = AsyncMock(side_effect=fake_sleep)
            with patch('dealradar.telegram_commands.sys.stdin.isatty', return_value=True), \
                 patch('dealradar.telegram_commands.sys.stdout.isatty', return_value=True), \
                 patch('dealradar.telegram_commands.sys.stderr.isatty', return_value=True), \
                 patch('dealradar.telegram_client.check_dependencies', return_value={}), \
                 patch('dealradar.telegram_client.prompt_credentials', return_value=object()) as credentials, \
                 patch('dealradar.telegram_client.build_client', return_value=object()), \
                 patch('dealradar.telegram_client.login', login), \
                 patch('dealradar.telegram_client.close_client', close), \
                 patch('dealradar.telegram_client.TelethonReader.resolve', AsyncMock(return_value=FakeReader())), \
                 patch('dealradar.telegram_commands.Monitor', return_value=monitor), \
                 patch('dealradar.telegram_commands.asyncio.sleep', sleep):
                result = asyncio.run(run_monitor(args, events.append))
            login.assert_awaited_once()
            credentials.assert_called_once()
            close.assert_awaited_once()
            self.assertTrue(events[-1]['logout_confirmed'])
            with monitor_lock(path):
                pass
            return result, events, monitor.poll.await_count, sleep

    def test_three_consecutive_transient_failures_stop_without_a_second_login(self):
        result, events, polls, _ = self.run_watch([{'state': 'error', 'code': 'network_error'}] * 3)
        self.assertEqual((result, polls), (2, 3))
        self.assertEqual([event['retry'] for event in events if event.get('state') == 'retry_scheduled'], [1, 2])

    def test_success_resets_the_bounded_transient_failure_counter(self):
        error = {'state': 'error', 'code': 'timeout'}
        result, events, polls, _ = self.run_watch([error, {'state': 'ok'}, error, error, error])
        self.assertEqual((result, polls), (2, 5))
        self.assertEqual([event['retry'] for event in events if event.get('state') == 'retry_scheduled'], [1, 1, 2])

    def test_access_and_protocol_failures_are_never_retried(self):
        for code in ('access_denied', 'identity_changed', 'auth_required', 'protocol_error'):
            with self.subTest(code=code):
                result, _, polls, sleep = self.run_watch([{'state': 'error', 'code': code}])
                self.assertEqual((result, polls), (2, 1))
                sleep.assert_not_awaited()

    def test_source_pause_interrupts_a_long_wait_and_closes_the_session(self):
        result, events, polls, sleep = self.run_watch([{'state': 'ok'}], pause_on_sleep=True)
        self.assertEqual((result, polls), (0, 1))
        sleep.assert_awaited_once_with(5)
        self.assertTrue(any(event.get('state') == 'disabled' for event in events))

    def test_one_shot_mode_keeps_the_original_no_retry_behavior(self):
        result, _, polls, sleep = self.run_watch([{'state': 'error', 'code': 'network_error'}], watch=False)
        self.assertEqual((result, polls), (2, 1))
        sleep.assert_not_awaited()
