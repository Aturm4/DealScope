"""Outbox recovery, authorization and complete synthetic collection → send flow."""

import asyncio
from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dealradar.bot_api import BotFailure
from dealradar.cli import main
from dealradar.models import IncomingPost, Offer, SearchRule
from dealradar.notifications import Dispatcher, format_notification
from dealradar.storage import Store, _LIVE_SCHEMA, _SCHEMA
from dealradar.telegram_source import Channel, Monitor, SourceFailure, TEST_USERNAME

BOT, OWNER, CHANNEL = 123456, 700001, 90001
SOURCE = 'telegram:' + TEST_USERNAME
PUBLISHED = datetime(2026, 10, 19, 12, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 20, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds=120):
        self.value += timedelta(seconds=seconds)

    async def sleep(self, seconds):
        self.advance(seconds)


class FakeReader:
    def __init__(self):
        self.channel = Channel(TEST_USERNAME, CHANNEL)
        self.posts = {}
        self.failure = None
        self.reads = 0

    async def latest_id(self):
        return max([10, *self.posts])

    async def new_posts(self, after, upper):
        return [post for item, post in sorted(self.posts.items()) if after < item <= upper and post is not None]

    async def existing_posts(self, ids):
        self.reads += 1
        if self.failure:
            raise self.failure
        return {item: self.posts.get(item) for item in ids}

    async def sponsored(self):
        return []


class FakeBot:
    bot_id, owner_id = BOT, OWNER

    def __init__(self):
        self.calls = []
        self.failure = None
        self.forgotten = False

    def forget(self):
        self.forgotten = True

    def send(self, text):
        self.calls.append(text)
        if self.failure:
            raise self.failure
        return len(self.calls) + 100


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / 'synthetic.sqlite3'
        self.store = Store(self.path)
        self.store.bind_telegram(TEST_USERNAME, CHANNEL, 10)
        self.rule = SearchRule('synthetic', 'Учебный пресет', 'USD', Decimal('50'))
        self.store.put_rule(self.rule)
        self.clock = Clock()
        self.store.link_notifier(BOT, OWNER, now=self.clock)
        self.reader, self.bot = FakeReader(), FakeBot()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def post(self, item=11, text='SSD NVMe 512GB 40$', edit=None):
        return IncomingPost(item, text, PUBLISHED, edited_at=edit)

    def add(self, post=None):
        post = post or self.post()
        self.reader.posts[post.message_id] = post
        return self.store.process(SOURCE, post, origin='telegram', detected_at=self.clock())

    def tick(self):
        return asyncio.run(Dispatcher(self.store, self.bot, SOURCE, now=self.clock, sleep=self.clock.sleep).tick(self.reader))

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)

    def test_collection_to_delivery_duplicate_and_restart(self):
        self.reader.posts[11] = self.post()
        monitor = Monitor(self.store, self.reader, username=TEST_USERNAME, now=self.clock)
        self.assertEqual(asyncio.run(monitor.poll())['new'], 1)
        self.assertEqual(self.tick()['sent'], 1)
        self.assertEqual(asyncio.run(monitor.poll())['new'], 0)
        self.assertEqual(self.tick()['sent'], 0)
        self.reopen()
        self.assertEqual(self.store.process(SOURCE, self.post(), origin='telegram').status, 'duplicate')
        self.assertEqual(self.tick()['sent'], 0)
        job = self.store.jobs()[0]
        self.assertEqual((job['status'], job['telegram_message_id'], job['attempts']), ('sent', 101, 1))
        self.assertIn('СИНТЕТИЧЕСКИЙ ТЕСТ', self.bot.calls[0])
        self.assertEqual(len(self.bot.calls), 1)

    def test_overlapping_presets_produce_one_card_for_the_post(self):
        self.store.put_rule(replace(self.rule, key='second', name='Второй учебный пресет'))
        self.add()
        self.assertEqual(self.tick()['sent'], 1)
        jobs = self.store.jobs()
        self.assertEqual(len(jobs), 2)
        self.assertEqual({row['status'] for row in jobs}, {'sent'})
        self.assertEqual({row['telegram_message_id'] for row in jobs}, {101})
        self.assertEqual(len(self.bot.calls), 1)

    def test_pre_enable_history_and_recheck_never_flush(self):
        self.store.disable_notifier()
        self.add()
        self.assertEqual(self.store.jobs()[0]['status'], 'held')
        self.store.link_notifier(BOT, OWNER, now=self.clock)
        self.store.recheck()
        self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(self.store.jobs()[0]['reason'], 'before_enable')
        self.add(self.post(12))
        self.assertEqual(self.tick()['sent'], 1)

    def test_edit_to_sold_before_send_cancels_the_pending_notification(self):
        self.add()
        self.reader.posts[11] = self.post(text='SSD NVMe 512GB 40$\nПРОДАНО ❌', edit=PUBLISHED + timedelta(minutes=1))
        self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(self.store.offers()[0]['offer']['availability'], 'sold')
        self.assertEqual(self.store.jobs()[0]['status'], 'cancelled')
        self.assertEqual(self.bot.calls, [])

    def test_partial_sale_and_changed_price_use_the_current_offer_only(self):
        initial = 'SSD NVMe\nSamsung 512GB 40$\nCrucial 1TB 49$'
        self.add(self.post(text=initial))
        self.reader.posts[11] = self.post(text=initial.replace('40$', '40$❌').replace('49$', '45$'), edit=PUBLISHED + timedelta(minutes=1))
        self.assertEqual(self.tick()['sent'], 1)
        self.assertNotIn('512 ГБ', self.bot.calls[0])
        self.assertIn('1000 ГБ', self.bot.calls[0])
        self.assertIn('45 USD', self.bot.calls[0])
        self.assertEqual(self.store.jobs()[0]['target_revision'], 2)

    def test_deleted_post_is_not_sent(self):
        self.add()
        self.reader.posts[11] = None
        self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(self.store.jobs()[0]['reason'], 'unavailable')

    def test_source_cannot_be_checked_is_not_empty_or_safe_to_send(self):
        self.add()
        self.reader.failure = SourceFailure('timeout')
        result = self.tick()
        self.assertEqual((result['state'], result['code']), ('source_error', 'timeout'))
        self.assertEqual(self.store.jobs()[0]['status'], 'pending')
        self.assertEqual(self.store.telegram_status(SOURCE)['last_error_code'], 'timeout')
        self.assertEqual(self.bot.calls, [])
        reads = self.reader.reads
        self.assertEqual(self.tick()['state'], 'source_paused')
        self.assertEqual(self.reader.reads, reads)

    def test_source_floodwait_is_saved_without_sending(self):
        self.add()
        self.reader.failure = SourceFailure('rate_limited', 90)
        self.assertEqual(self.tick()['state'], 'source_error')
        self.assertIsNotNone(self.store.auth_pause())
        self.assertEqual(self.bot.calls, [])

    def test_missing_refresh_results_fail_closed(self):
        self.add()
        async def missing(_):
            return {}
        self.reader.existing_posts = missing
        self.assertEqual(self.tick()['code'], 'protocol_error')
        self.assertEqual(self.bot.calls, [])

    def test_uncertain_send_is_not_retried_even_after_reopen(self):
        self.add()
        self.bot.failure = BotFailure('network_error', ambiguous=True)
        self.assertTrue(self.tick()['delivery_unknown'])
        self.assertEqual(self.store.jobs()[0]['status'], 'unknown')
        self.reopen()
        self.bot.failure = None
        self.clock.advance()
        self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(len(self.bot.calls), 1)
        self.store.resolve_notification(self.store.jobs()[0]['id'], 'sent')
        self.assertEqual(self.store.jobs()[0]['reason'], 'owner_confirmed')
        self.assertEqual(self.tick()['sent'], 0)

    def test_restart_after_claim_without_response_becomes_unknown(self):
        self.add()
        job = self.store.claim_notification(SOURCE, 11, BOT, OWNER, now=self.clock)
        self.assertIsNotNone(job)
        self.reopen()
        self.assertEqual(self.store.recover_notifications(now=self.clock), 1)
        self.assertEqual(self.store.jobs()[0]['status'], 'unknown')
        self.assertEqual(self.tick()['sent'], 0)
        self.store.resolve_notification(self.store.jobs()[0]['id'], 'skip')
        self.assertEqual(self.store.jobs()[0]['reason'], 'owner_skipped')

    def test_429_pause_survives_restart_and_retries_only_after_due(self):
        self.add()
        self.bot.failure = BotFailure('rate_limited', retry_after=90)
        self.assertEqual(self.tick()['code'], 'rate_limited')
        self.assertEqual(self.store.jobs()[0]['status'], 'pending')
        self.reopen()
        self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(len(self.bot.calls), 1)
        self.bot.failure = None
        self.clock.advance(91)
        self.assertEqual(self.tick()['sent'], 1)
        self.assertEqual(self.store.jobs()[0]['attempts'], 2)

    def test_retries_are_limited_to_three_server_confirmed_rejections(self):
        self.add()
        self.bot.failure = BotFailure('rate_limited', retry_after=30)
        for _ in range(3):
            self.tick()
            self.clock.advance(31)
        self.assertEqual((self.store.jobs()[0]['status'], self.store.jobs()[0]['attempts']), ('failed', 3))
        self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(len(self.bot.calls), 3)

    def test_token_revocation_disables_delivery_and_reenable_skips_old_queue(self):
        self.add()
        self.add(self.post(12))
        self.bot.failure = BotFailure('token_rejected')
        self.assertEqual(self.tick()['code'], 'token_rejected')
        self.assertFalse(self.store.notifier()['enabled'])
        self.store.link_notifier(BOT, OWNER, now=self.clock)
        self.bot.failure = None
        self.clock.advance()
        self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(len(self.bot.calls), 1)

    def test_other_notifications_can_continue_after_a_single_bad_request(self):
        self.add()
        self.bot.failure = BotFailure('bad_request')
        self.tick()
        self.add(self.post(12))
        self.bot.failure = None
        self.clock.advance()
        self.assertEqual(self.tick()['sent'], 1)
        self.assertTrue(self.store.notifier()['enabled'])

    def test_disabling_source_or_changing_rule_cancels_pending(self):
        self.add()
        self.store.set_source_enabled(SOURCE, False)
        self.assertEqual(self.store.jobs()[0]['reason'], 'source_disabled')
        self.assertEqual(self.tick()['sent'], 0)
        self.store.set_source_enabled(SOURCE, True)
        self.add(self.post(12))
        self.store.put_rule(replace(self.rule, max_price=Decimal('30')))
        self.assertEqual(self.store.jobs()[1]['reason'], 'rule_changed')
        self.store.recheck()
        self.assertEqual(self.tick()['sent'], 0)

    def test_edit_after_confirmed_delivery_never_changes_history_or_resends(self):
        self.add()
        self.tick()
        before = self.store.jobs()[0]
        self.reader.posts[11] = self.post(text='SSD NVMe 512GB 35$', edit=PUBLISHED + timedelta(minutes=1))
        self.store.process(SOURCE, self.reader.posts[11], origin='telegram')
        self.clock.advance()
        self.tick()
        after = self.store.jobs()[0]
        self.assertEqual((after['target_revision'], after['delivery_text']), (before['target_revision'], before['delivery_text']))
        self.assertEqual(len(self.bot.calls), 1)

    def test_notification_enable_does_not_allow_offline_import_delivery(self):
        path = Path(self.temp.name) / 'offline.sqlite3'
        with Store(path) as store:
            store.add_test_source('test:synthetic', 'Только офлайн')
            store.put_rule(self.rule)
            store.link_notifier(BOT, OWNER, now=self.clock)
            store.process('test:synthetic', self.post())
            self.assertEqual(store.jobs()[0]['status'], 'held')
            self.assertEqual(store.notification_candidates(now=self.clock), [])

    def test_owner_bot_and_channel_cannot_be_swapped(self):
        self.add()
        for bot, owner in [(BOT + 1, OWNER), (BOT, OWNER + 1)]:
            with self.assertRaises(ValueError):
                self.store.link_notifier(bot, owner)
            self.assertIsNone(self.store.claim_notification(SOURCE, 11, bot, owner, now=self.clock))
        self.bot.owner_id = OWNER + 1
        with self.assertRaises(BotFailure):
            self.tick()
        self.assertEqual(self.reader.reads, 0)
        self.bot.owner_id = OWNER
        self.reader.channel = Channel(TEST_USERNAME, CHANNEL + 1)
        with self.assertRaises(SourceFailure):
            self.tick()
        self.assertEqual(self.bot.calls, [])

    def test_disable_bot_stops_future_attempts(self):
        self.add()
        self.store.disable_notifier()
        self.assertEqual(self.tick()['state'], 'disabled')
        self.assertEqual(self.store.jobs()[0]['reason'], 'notifications_disabled')

    def test_html_and_links_from_ads_are_not_interpreted(self):
        post = {'source_key': SOURCE, 'name': '<script>bad</script>\u202e', 'message_id': 11,
                'original_url': 'http://127.0.0.1/private'}
        offer = Offer(0, '<a href="http://127.0.0.1">run</a>', '', brand='<b>brand</b>', model='&model',
                      capacity_gb=512, price=Decimal('40'), currency='USD', interface='nvme')
        text = format_notification(post, [offer], [replace(self.rule, name='<b>presет</b>')])
        self.assertNotIn('<script>', text)
        self.assertNotIn('127.0.0.1', text)
        self.assertNotIn('\u202e', text)
        self.assertIn('&lt;b&gt;brand&lt;/b&gt;', text)
        self.assertIn('https://t.me/testirovk4/11', text)

    def test_many_offers_fit_one_bounded_card_and_unknowns_are_visible(self):
        post = {'source_key': SOURCE, 'name': 'testirovk4', 'message_id': 11}
        offers = [Offer(index, '', '', capacity_gb=512, interface='nvme', price=Decimal('40'), currency='USD') for index in range(100)]
        text = format_notification(post, offers, [self.rule])
        self.assertIn('модель не указана', text)
        self.assertIn('состояние не указано', text)
        self.assertIn('Ещё подходящих позиций: 95', text)
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)

    def test_maximum_escaped_labels_fit_and_cannot_supply_markup(self):
        post = {'source_key': SOURCE, 'name': '&' * 100, 'message_id': 11}
        offers = [Offer(index, '', '', brand='"' * 100, model='"' * 100,
                        capacity_gb=100000, price=Decimal('1000000000000'), currency='USD', interface='nvme') for index in range(100)]
        rules = [replace(self.rule, name='"' * 100, max_price=Decimal('1000000000000'))] * 30
        text = format_notification(post, offers, rules)
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)

    def test_multiple_pending_posts_are_rate_spaced(self):
        for item in range(11, 14):
            self.add(self.post(item))
        before = self.clock()
        self.assertEqual(self.tick()['sent'], 3)
        self.assertGreaterEqual((self.clock() - before).total_seconds(), 4)
        self.assertEqual(len(self.bot.calls), 3)

    def run_console(self, authenticated_owner=OWNER, setup_failure=None, notify=True):
        from dealradar.telegram_commands import run_monitor
        args = SimpleNamespace(db=self.path, channel=TEST_USERNAME, interval=60,
                               start_now=False, watch=False, notify=notify)
        events = []
        setup = AsyncMock(return_value=self.bot, side_effect=setup_failure)
        login = AsyncMock(return_value=authenticated_owner)
        close = AsyncMock(return_value=True)
        with patch('dealradar.telegram_commands.sys.stdin.isatty', return_value=True), \
             patch('dealradar.telegram_commands.sys.stdout.isatty', return_value=True), \
             patch('dealradar.telegram_commands.sys.stderr.isatty', return_value=True), \
             patch('dealradar.telegram_client.check_dependencies', return_value={}), \
             patch('dealradar.telegram_client.prompt_credentials', return_value=object()) as credentials, \
             patch('dealradar.telegram_client.build_client', return_value=object()), \
             patch('dealradar.telegram_client.login', login), patch('dealradar.telegram_client.close_client', close), \
             patch('dealradar.telegram_client.TelethonReader.resolve', AsyncMock(return_value=self.reader)), \
             patch('dealradar.bot_commands.prepare_notifier', setup):
            try:
                result = asyncio.run(run_monitor(args, events.append))
            except BotFailure as exc:
                result = exc.code
        return result, events, setup, login, close, credentials

    def test_console_notify_flag_runs_collection_delivery_and_logout(self):
        self.reader.posts[11] = self.post()
        result, events, setup, login, close, credentials = self.run_console()
        self.assertEqual(result, 0)
        self.assertEqual(len(self.bot.calls), 1)
        self.assertEqual(self.store.jobs()[0]['status'], 'sent')
        close.assert_awaited_once()
        self.assertTrue(self.bot.forgotten)
        self.assertTrue(events[-1]['logout_confirmed'])

    def test_account_owner_must_equal_the_explicit_bot_recipient(self):
        self.reader.posts[11] = self.post()
        result, events, setup, login, close, credentials = self.run_console(authenticated_owner=OWNER + 1)
        self.assertEqual(result, 'identity_changed')
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.store.stats()['counts']['posts'], 0)
        close.assert_awaited_once()
        self.assertTrue(self.bot.forgotten)

    def test_bot_setup_failure_occurs_before_user_account_credentials_or_login(self):
        result, events, setup, login, close, credentials = self.run_console(setup_failure=BotFailure('token_rejected'))
        self.assertEqual(result, 'token_rejected')
        credentials.assert_not_called()
        login.assert_not_awaited()
        close.assert_not_awaited()

    def test_notifier_failure_does_not_erase_a_successful_source_check(self):
        self.reader.posts[11] = self.post()
        self.bot.failure = BotFailure('network_error', ambiguous=True)
        result, events, setup, login, close, credentials = self.run_console()
        self.assertEqual(result, 0)
        self.assertEqual(self.store.telegram_status(SOURCE)['check_state'], 'ok')
        self.assertEqual(self.store.jobs()[0]['status'], 'unknown')
        self.assertTrue(any(event.get('notifications', {}).get('state') == 'notification_error' for event in events))
        self.assertTrue(events[-1]['logout_confirmed'])

    def test_collection_without_notify_flag_never_prompts_or_sends_bot(self):
        self.reader.posts[11] = self.post()
        result, events, setup, login, close, credentials = self.run_console(notify=False)
        self.assertEqual(result, 0)
        setup.assert_not_awaited()
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.store.jobs()[0]['status'], 'pending')


class MigrationAndPresetTests(unittest.TestCase):
    def legacy(self, path):
        with closing(sqlite3.connect(path)) as db:
            db.executescript(_SCHEMA.replace("CHECK (kind = 'test')", "CHECK (kind IN ('test','telegram'))")
                             + _LIVE_SCHEMA + '\nPRAGMA user_version=2;')
            db.execute("INSERT INTO sources(id,source_key,name,kind) VALUES (1,'test:old','Синтетический старый источник','test')")
            rule = SearchRule('old-rule', 'Старый учебный пресет', 'USD', Decimal('50'))
            db.execute('INSERT INTO rules(id,rule_key,version,enabled,config_json,updated_at) VALUES (1,?,1,1,?,?)',
                       (rule.key, json.dumps(rule.to_dict()), PUBLISHED.isoformat()))
            db.execute('INSERT INTO posts(id,source_id,message_id,published_at,first_detected_at,last_detected_at,revision,fingerprint) '
                       'VALUES (1,1,11,?,?,?,1,?)', (PUBLISHED.isoformat(), PUBLISHED.isoformat(), PUBLISHED.isoformat(), 'synthetic'))
            db.execute("INSERT INTO notification_jobs VALUES (7,1,1,1,'held','preview_only','[0]',?,?)", (PUBLISHED.isoformat(), PUBLISHED.isoformat()))
            db.commit()

    def test_schema2_history_and_jobs_migrate_without_enabling_or_sending(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'legacy.sqlite3'
            self.legacy(path)
            with Store(path) as store:
                self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0], 3)
                self.assertEqual(store.jobs()[0]['id'], 7)
                self.assertEqual(store.jobs()[0]['status'], 'held')
                self.assertIsNone(store.notifier())
                self.assertEqual(store.notification_candidates(), [])
                self.assertEqual(store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_failed_schema_upgrade_rolls_back_and_preserves_legacy(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'legacy.sqlite3'
            self.legacy(path)
            with patch('dealradar.storage.NOTIFICATION_SCHEMA', 'DROP TABLE notification_jobs; THIS_IS_NOT_SQL;'):
                with self.assertRaises(sqlite3.Error):
                    Store(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT id,status FROM notification_jobs').fetchone(), (7, 'held'))

    def test_named_preset_parameters_persist_and_can_be_changed_without_secrets(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'presets.sqlite3'
            common = ['--db', str(path), 'preset-save', 'my-choice', '--name', 'Мой пресет',
                      '--currency', 'UZS', '--capacity', '512', '--condition', 'used', '--exclude', 'адаптер']
            with redirect_stdout(StringIO()):
                self.assertEqual(main([*common, '--max-price', '123456']), 0)
                self.assertEqual(main([*common, '--max-price', '111111']), 0)
            with Store(path) as store:
                rules = store.rules()
                self.assertEqual(len(rules), 1)
                self.assertEqual(rules[0]['version'], 2)
                self.assertEqual(rules[0]['config']['max_price'], '111111')
                self.assertEqual(rules[0]['config']['capacities_gb'], [512])
                self.assertEqual(rules[0]['config']['conditions'], ['used'])
                self.assertIsNone(store.notifier())
