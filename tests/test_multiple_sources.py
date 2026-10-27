"""User-approved channel configuration and synthetic multi-channel monitoring. No real login."""

import asyncio
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from dealradar.bot_api import BotFailure
from dealradar.models import IncomingPost, SearchRule, utc_now
from dealradar.notifications import Dispatcher
from dealradar.storage import Store
from dealradar.telegram_source import Channel, Monitor, SourceFailure, public_username

ALPHA, BETA = 'synth_alpha', 'synth_beta'
NOW = datetime(2026, 10, 23, tzinfo=UTC)


class Reader:
    def __init__(self, username, channel_id):
        self.channel = Channel(username, channel_id)
        self.posts = {}
        self.error = None

    async def latest_id(self):
        if self.error:
            raise self.error
        return max(self.posts, default=0)

    async def new_posts(self, after, upper):
        return [value for key, value in sorted(self.posts.items()) if after < key <= upper]

    async def existing_posts(self, ids):
        return {item: self.posts.get(item) for item in ids}

    async def sponsored(self):
        return []


def post(item=1):
    return IncomingPost(item, 'СИНТЕТИЧЕСКИЙ ТЕСТ SSD NVMe 512GB 40$', NOW)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / 'SYNTHETIC-multiple.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_address_normalization_and_duplicate_do_not_reset_existing_source(self):
        self.assertEqual(public_username(' https://t.me/Synth_Alpha/ '), ALPHA)
        key = self.store.add_telegram_source('@Synth_Alpha')
        self.store.bind_telegram(ALPHA, 123, 17)
        self.store.set_source_enabled(key, False)
        with self.assertRaisesRegex(ValueError, 'уже добавлен'):
            self.store.add_telegram_source('https://t.me/synth_alpha')
        source = self.store.telegram_status(key)
        self.assertEqual((source['boundary_id'], source['enabled']), (17, 0))

    def test_untrusted_addresses_are_rejected_without_io(self):
        bad = ['http://t.me/synth_alpha', 'https://evil.invalid/synth_alpha', 'https://t.me@evil.invalid/synth_alpha',
               'https://t.me/+invite', 'https://t.me/c/123/1', 'https://t.me/synth_alpha/1',
               'https://t.me/synth_alpha?start=1', 'https://t.me/synth_alpha#x', 'https://t.me/synth_alpha%2f',
               'https://127.0.0.1/', 'javascript:alert(1)', '@synth\nalpha', '<svg/onload=alert(1)>',
               '//t.me/synth_alpha', 'аbcde', 'a' * 33, '', None, 1]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.add_telegram_source(value)
        self.assertFalse(self.store.sources())

    def test_limit_is_atomic_and_pending_sources_survive_restart(self):
        for item in range(5):
            self.store.add_telegram_source(f'synth_{item}')
        with self.assertRaisesRegex(ValueError, 'пяти'):
            self.store.add_telegram_source('synth_extra')
        with Store(self.path) as reopened:
            self.assertEqual(len(reopened.sources()), 5)
            self.assertTrue(all(row['channel_id'] is None for row in reopened.sources()))
            self.assertFalse(reopened.stats()['counts']['posts'])

    def test_offline_and_telegram_test_sources_remain_separate(self):
        self.store.add_telegram_source(ALPHA)
        with self.assertRaises(ValueError):
            self.store.add_telegram_source('testirovk4')
        with Store(Path(self.temp.name) / 'SYNTHETIC-offline.sqlite3') as other:
            other.add_test_source('test:demo', 'СИНТЕТИКА')
            with self.assertRaises(ValueError):
                other.add_telegram_source(ALPHA)

    def test_explicit_approval_precedes_any_reader_call(self):
        reader = Reader(ALPHA, 123)
        with self.assertRaises(ValueError):
            Monitor(self.store, reader, username=ALPHA)
        self.store.add_telegram_source(ALPHA)
        self.assertIsInstance(Monitor(self.store, reader, username=ALPHA), Monitor)

    def test_initial_boundary_and_restart_are_independent_for_each_source(self):
        readers = [Reader(ALPHA, 123), Reader(BETA, 456)]
        for reader in readers:
            self.store.add_telegram_source(reader.channel.username)
            reader.posts[1] = post()
            monitor = Monitor(self.store, reader, username=reader.channel.username)
            with self.assertRaises(ValueError):
                asyncio.run(monitor.poll())
            self.assertEqual(asyncio.run(monitor.poll(start_now=True))['new'], 0)
            reader.posts[2] = post(2)
            self.assertEqual(asyncio.run(monitor.poll())['new'], 1)
        self.assertEqual(self.store.stats()['counts']['posts'], 2)
        with Store(self.path) as reopened:
            for reader in readers:
                monitor = Monitor(reopened, reader, username=reader.channel.username)
                self.assertEqual(asyncio.run(monitor.poll())['new'], 0)
                self.assertEqual(reopened.telegram_status('telegram:' + reader.channel.username)['boundary_id'], 1)

    def test_same_numeric_channel_cannot_be_bound_under_two_names(self):
        self.store.add_telegram_source(ALPHA)
        self.store.add_telegram_source(BETA)
        self.store.bind_telegram(ALPHA, 123, 10)
        with self.assertRaises(ValueError):
            self.store.bind_telegram(BETA, 123, 20)
        self.assertIsNone(self.store.telegram_status('telegram:' + BETA)['channel_id'])
        self.assertEqual(self.store.telegram_status('telegram:' + ALPHA)['boundary_id'], 10)

    def test_outbox_is_filtered_by_source_and_has_no_cross_channel_send(self):
        self.store.put_rule(SearchRule('synthetic', 'СИНТЕТИКА', 'USD', '50'))
        self.store.link_notifier(1, 2)
        readers = [Reader(ALPHA, 123), Reader(BETA, 456)]
        for reader in readers:
            name = reader.channel.username
            self.store.add_telegram_source(name)
            self.store.bind_telegram(name, reader.channel.channel_id, 0)
            reader.posts[1] = post()
            self.store.process('telegram:' + name, post(), origin='telegram')
        sent = []
        bot = SimpleNamespace(bot_id=1, owner_id=2, send=lambda text: sent.append(text) or len(sent))
        for reader in readers:
            key = 'telegram:' + reader.channel.username
            self.assertEqual([row['source_key'] for row in self.store.notification_candidates(source_key=key)], [key])
            result = asyncio.run(Dispatcher(self.store, bot, key).tick(reader))
            self.assertEqual(result['sent'], 1)
            self.store.connection.execute('UPDATE notifier_settings SET not_before=NULL')
        self.assertIn('t.me/synth_alpha/1', sent[0])
        self.assertIn('t.me/synth_beta/1', sent[1])
        self.assertTrue(all(row['status'] == 'sent' for row in self.store.jobs()))


class MultiRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / 'SYNTHETIC-watch.sqlite3'
        self.readers = {ALPHA: Reader(ALPHA, 123), BETA: Reader(BETA, 456)}
        with Store(self.path) as store:
            for username, reader in self.readers.items():
                store.add_telegram_source(username)
                store.bind_telegram(username, reader.channel.channel_id, 0)

    def tearDown(self):
        self.temp.cleanup()

    def run_sources(self, *, watch=False, after_sleep=None, notify=False, owner=2, login_failure=None):
        from dealradar.telegram_multi import run_sources
        args = SimpleNamespace(db=self.path, interval=60, watch=watch, notify=notify)
        client = SimpleNamespace(configure_sources=Mock())
        client.select_source = lambda value: setattr(client, 'selected', value)
        events, sleeps, calls = [], [], []
        async def resolve(actual, expected_id=None):
            calls.append(actual.selected)
            reader = self.readers[actual.selected]
            self.assertEqual(expected_id, reader.channel.channel_id)
            return reader
        async def sleep(seconds):
            sleeps.append(seconds)
            if after_sleep:
                after_sleep(len(sleeps))
        with ExitStack() as stack:
            for target in ('stdin', 'stdout', 'stderr'):
                stack.enter_context(patch('sys.' + target + '.isatty', return_value=True))
            stack.enter_context(patch('dealradar.telegram_client.check_dependencies', return_value={}))
            prompt = stack.enter_context(patch('dealradar.telegram_client.prompt_credentials', return_value=object()))
            stack.enter_context(patch('dealradar.telegram_client.build_client', return_value=client))
            authenticate = stack.enter_context(patch('dealradar.telegram_client.login', AsyncMock(return_value=owner, side_effect=login_failure)))
            close = stack.enter_context(patch('dealradar.telegram_client.close_client', AsyncMock(return_value=True)))
            stack.enter_context(patch('dealradar.telegram_client.TelethonReader.resolve', side_effect=resolve))
            stack.enter_context(patch('dealradar.telegram_multi.asyncio.sleep', side_effect=sleep))
            bot = SimpleNamespace(owner_id=2, bot_id=1, forget=Mock())
            stack.enter_context(patch('dealradar.bot_commands.prepare_notifier', AsyncMock(return_value=bot)))
            try:
                result = asyncio.run(run_sources(args, events.append))
            except (BotFailure, SourceFailure) as exc:
                result = exc.code
        return result, events, calls, prompt, authenticate, close, bot

    def test_one_access_failure_does_not_stop_the_other_channel(self):
        self.readers[ALPHA].error = SourceFailure('access_denied')
        self.readers[BETA].posts[1] = post()
        result, events, calls, _, login, close, _ = self.run_sources()
        self.assertEqual(result, 2)
        self.assertEqual(calls, [ALPHA, BETA])
        self.assertTrue(any(item.get('source') == 'telegram:' + BETA and item.get('new') == 1 for item in events))
        login.assert_awaited_once()
        close.assert_awaited_once()

    def test_flood_wait_blocks_other_sources_and_survives_restart(self):
        self.readers[ALPHA].error = SourceFailure('rate_limited', 600)
        result, events, calls, *_ = self.run_sources()
        self.assertEqual((result, calls), (2, [ALPHA]))
        result, _, calls, prompt, login, close, _ = self.run_sources()
        self.assertEqual((result, calls), (2, []))
        prompt.assert_not_called()
        login.assert_not_awaited()
        close.assert_not_awaited()

    def test_all_sources_disabled_stops_before_asking_for_secrets(self):
        with Store(self.path) as store:
            for username in self.readers:
                store.set_source_enabled('telegram:' + username, False)
        result, _, calls, prompt, login, close, _ = self.run_sources()
        self.assertEqual((result, calls), (0, []))
        prompt.assert_not_called()
        login.assert_not_awaited()
        close.assert_not_awaited()

    def test_login_flood_wait_is_persisted_and_restart_does_not_ask_for_secrets(self):
        result, events, calls, _, login, close, _ = self.run_sources(login_failure=SourceFailure('rate_limited', 600))
        self.assertEqual((result, calls), ('rate_limited', []))
        login.assert_awaited_once()
        close.assert_awaited_once()
        self.assertTrue(events[-1]['logout_confirmed'])
        from dealradar.models import read_timestamp
        with Store(self.path) as store:
            pause = store.auth_pause()
            self.assertEqual(pause['last_error_code'], 'rate_limited')
            self.assertGreater((read_timestamp(pause['retry_not_before']) - utc_now()).total_seconds(), 550)
        result, _, calls, prompt, login, close, _ = self.run_sources()
        self.assertEqual((result, calls), (2, []))
        prompt.assert_not_called()
        login.assert_not_awaited()
        close.assert_not_awaited()

    def test_new_channel_added_while_running_is_seen_without_another_login(self):
        new = 'synth_gamma'
        self.readers[new] = Reader(new, 789)
        def changes(call):
            if call == 1:
                with Store(self.path) as store:
                    store.add_telegram_source(new)
                    store.bind_telegram(new, 789, 0)
            elif call == 13:
                with Store(self.path) as store:
                    for name in self.readers:
                        store.set_source_enabled('telegram:' + name, False)
        result, _, calls, prompt, login, close, _ = self.run_sources(watch=True, after_sleep=changes)
        self.assertEqual(result, 0)
        self.assertIn(new, calls)
        prompt.assert_called_once()
        login.assert_awaited_once()
        close.assert_awaited_once()

    def test_owner_mismatch_stops_before_any_channel_resolution(self):
        result, events, calls, _, login, close, bot = self.run_sources(notify=True, owner=3)
        self.assertEqual((result, calls), ('identity_changed', []))
        close.assert_awaited_once()
        bot.forget.assert_called_once()

    def test_single_source_mode_refuses_unregistered_channel_before_secret_input(self):
        from dealradar.telegram_commands import run_monitor
        args = SimpleNamespace(db=self.path, interval=60, channel='synth_unapproved',
                               start_now=True, watch=False, notify=False)
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), \
             patch('sys.stderr.isatty', return_value=True), \
             patch('dealradar.telegram_client.check_dependencies', return_value={}), \
             patch('dealradar.telegram_client.prompt_credentials') as prompt:
            with self.assertRaises(ValueError):
                asyncio.run(run_monitor(args, lambda _: None))
        prompt.assert_not_called()
