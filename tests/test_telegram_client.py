"""Security tests with dummy credentials and a mocked transport; never log in."""

import asyncio
from datetime import UTC, datetime
import getpass
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dealradar.telegram_client import Credentials, LocalSetupError, bounded_call, build_client, close_client, hidden, login, prompt_credentials, sanitized_failure
from dealradar.telegram_commands import command, monitor_lock, run_monitor
from dealradar.telegram_source import APPROVED_USERNAME, TEST_USERNAME, Channel, SourceFailure

HAVE_TELETHON = importlib.util.find_spec('telethon') is not None


class SecretBoundaryTests(unittest.TestCase):
    def test_formatted_credentials_are_canonical_without_guessing_country(self):
        creds = Credentials(1, '  ' + '0' * 32 + '  ', '  +1 (000) 000-0000  ')
        self.assertEqual(creds.api_hash, '0' * 32)
        self.assertEqual(creds.phone, '+10000000000')
        with self.assertRaises(LocalSetupError) as error:
            Credentials(1, '0' * 32, '10000000000')
        self.assertEqual(error.exception.code, 'invalid_phone')

    def test_invalid_field_codes_and_messages_do_not_include_supplied_values(self):
        cases = ((Credentials, (0, '0' * 32, '+10000000000'), 'invalid_api_id'),
                 (Credentials, (1, 'SYNTHETIC_SECRET_HASH', '+10000000000'), 'invalid_api_hash'),
                 (Credentials, (1, '0' * 32, 'SYNTHETIC_SECRET_PHONE'), 'invalid_phone'))
        for factory, values, code in cases:
            with self.subTest(code=code), self.assertRaises(LocalSetupError) as error:
                factory(*values)
            self.assertEqual(error.exception.code, code)
            self.assertNotIn('SYNTHETIC_SECRET', str(error.exception))
        fallback = LocalSetupError('SYNTHETIC_SECRET_UNKNOWN_CODE')
        self.assertEqual(fallback.code, 'local_setup_error')
        self.assertNotIn('SYNTHETIC_SECRET', str(fallback))

    def test_prompt_validates_each_field_before_asking_for_the_next(self):
        for values, expected, calls in ((('0',), 'invalid_api_id', 1),
                                         (('1', 'SYNTHETIC_BAD_HASH'), 'invalid_api_hash', 2)):
            with self.subTest(expected=expected), \
                 patch('dealradar.telegram_client.hidden', side_effect=values) as prompt:
                with self.assertRaises(LocalSetupError) as error:
                    prompt_credentials()
            self.assertEqual(error.exception.code, expected)
            self.assertEqual(prompt.call_count, calls)

    def test_offline_input_command_uses_no_database_or_client(self):
        from contextlib import redirect_stdout, redirect_stderr
        import io
        from dealradar.cli import main
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'must_not_be_created.sqlite3'
            output, error_output = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error_output), \
                 patch('sys.stdin.isatty', return_value=True), \
                 patch('sys.stderr.isatty', return_value=True), \
                 patch('sys.stdout.isatty', return_value=True), \
                 patch('dealradar.telegram_client.hidden', side_effect=(' 1 ', '  ' + '0' * 32 + '  ', '+1 (000) 000-0000')), \
                 patch('dealradar.telegram_client.build_client', side_effect=AssertionError('No client')) as build, \
                 patch('sqlite3.connect', side_effect=AssertionError('No database')) as database:
                status = main(['--db', str(path), 'telegram-check-input'])
            result = json.loads(output.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(result['state'], 'input_format_valid')
            self.assertTrue(result['format_only'])
            self.assertFalse(result['network_used'])
            self.assertFalse(result['login_performed'])
            self.assertNotIn('10000000000', output.getvalue())
            self.assertNotIn('0' * 32, output.getvalue())
            self.assertEqual(error_output.getvalue(), '')
            self.assertFalse(path.exists())
            build.assert_not_called()
            database.assert_not_called()

    def test_offline_input_command_rejects_bad_field_without_echo(self):
        captured = []
        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stderr.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True), \
             patch('dealradar.telegram_client.hidden', side_effect=('1', 'SYNTHETIC_SECRET_BAD_HASH')):
            status = command(SimpleNamespace(command='telegram-check-input'), captured.append)
        self.assertEqual(status, 1)
        self.assertEqual(captured[0]['code'], 'invalid_api_hash')
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(captured))

    def test_offline_input_command_requires_a_private_console(self):
        captured = []
        with patch('sys.stdin.isatty', return_value=False), \
             patch('dealradar.telegram_client.prompt_credentials') as prompt:
            status = command(SimpleNamespace(command='telegram-check-input'), captured.append)
        self.assertEqual(status, 1)
        self.assertEqual(captured[0]['code'], 'interactive_console_required')
        prompt.assert_not_called()

    def test_runtime_setup_error_does_not_claim_no_login_attempt(self):
        captured = []
        async def failure(args, emit):
            emit({'logout_confirmed': True})  # Synthetic cleanup result only.
            raise LocalSetupError('hidden_input_unavailable')
        with patch('dealradar.telegram_commands.run_monitor', failure):
            status = command(SimpleNamespace(command='telegram-run'), captured.append)
        self.assertEqual(status, 1)
        self.assertEqual(captured[-1]['code'], 'hidden_input_unavailable')
        self.assertNotIn('login_attempted', captured[-1])

    def test_repr_has_no_dummy_secret_values(self):
        creds = Credentials(123, 'abcd' * 8, '+10000000000')
        self.assertNotIn(creds.api_hash, repr(creds))
        self.assertNotIn(creds.phone, repr(creds))

    def test_hidden_input_refuses_redirects_before_reading(self):
        with patch('sys.stdin.isatty', return_value=False), patch('getpass.getpass') as prompt:
            with self.assertRaises(ValueError):
                hidden('TEST')
            prompt.assert_not_called()

    def test_getpass_fallback_does_not_echo(self):
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stderr.isatty', return_value=True), \
             patch('getpass.getpass', side_effect=getpass.GetPassWarning('TEST')):
            with self.assertRaises(ValueError):
                hidden('TEST')

    def test_dependency_error_is_actionable_without_auth(self):
        from importlib.metadata import PackageNotFoundError
        from dealradar.telegram_client import check_dependencies
        with patch('dealradar.telegram_client.version', side_effect=PackageNotFoundError):
            with self.assertRaisesRegex(ValueError, 'README'):
                check_dependencies()

    def test_wrong_version_blocks_client_creation(self):
        with patch('dealradar.telegram_client.version', return_value='UNEXPECTED'):
            with self.assertRaises(ValueError):
                build_client(Credentials(1, '0' * 32, '+10000000000'))

    def test_third_party_exception_text_cannot_reach_cli(self):
        captured = []
        async def failure(*_):
            raise RuntimeError('DUMMY_SECRET_MUST_NOT_APPEAR')
        with patch('dealradar.telegram_commands.run_monitor', failure):
            status = command(SimpleNamespace(command='telegram-run'), captured.append)
        self.assertEqual(status, 1)
        self.assertNotIn('DUMMY_SECRET', json.dumps(captured))

    def test_only_one_process_can_hold_database_monitor_lock(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'synthetic.sqlite3'
            with monitor_lock(path):
                with self.assertRaises(OSError):
                    with monitor_lock(path):
                        self.fail('Second monitor acquired a held lock')
            with monitor_lock(path):
                pass


@unittest.skipUnless(HAVE_TELETHON, 'Optional pinned Telegram dependencies are not installed')
class TransportSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from telethon import types
        self.types = types
        self.creds = Credentials(1, '0' * 32, '+10000000000')
        self.client = build_client(self.creds)
        self.entity = types.Channel(id=123456, title='СИНТЕТИЧЕСКИЙ ТЕСТ', photo=types.ChatPhotoEmpty(),
                                    date=datetime(2026, 10, 19, tzinfo=UTC), broadcast=True,
                                    username=APPROVED_USERNAME, access_hash=987654)
        self.client.approve_channel(self.entity)
        self.client._authorized = True

    async def asyncTearDown(self):
        await self.client.disconnect()

    async def test_invalid_credentials_stop_before_client_or_login(self):
        captured = []
        with TemporaryDirectory() as temp:
            args = SimpleNamespace(db=Path(temp) / 'synthetic.sqlite3', interval=60, start_now=True, watch=False)
            with patch('sys.stdin.isatty', return_value=True), \
                 patch('sys.stderr.isatty', return_value=True), \
                 patch('sys.stdout.isatty', return_value=True), \
                 patch('dealradar.telegram_client.hidden', side_effect=('1', '0' * 32, '10000000000')), \
                 patch('dealradar.telegram_client.build_client') as build, \
                 patch('dealradar.telegram_client.login', new_callable=AsyncMock) as authenticate, \
                 patch('dealradar.telegram_client.close_client', new_callable=AsyncMock) as close:
                self.assertEqual(await run_monitor(args, captured.append), 1)
            self.assertEqual(captured[-1]['code'], 'invalid_phone')
            self.assertFalse(captured[-1]['login_attempted'])
            self.assertNotIn('10000000000', json.dumps(captured))
            build.assert_not_called()
            authenticate.assert_not_awaited()
            close.assert_not_awaited()

    async def test_client_setup_failure_is_sanitized_before_any_login(self):
        captured = []
        with TemporaryDirectory() as temp:
            args = SimpleNamespace(db=Path(temp) / 'synthetic.sqlite3', interval=60, start_now=True, watch=False)
            with patch('sys.stdin.isatty', return_value=True), \
                 patch('sys.stderr.isatty', return_value=True), \
                 patch('sys.stdout.isatty', return_value=True), \
                 patch('dealradar.telegram_client.prompt_credentials', return_value=self.creds), \
                 patch('dealradar.telegram_client.build_client', side_effect=RuntimeError('SYNTHETIC_SECRET_IN_EXCEPTION')), \
                 patch('dealradar.telegram_client.login', new_callable=AsyncMock) as authenticate, \
                 patch('dealradar.telegram_client.close_client', new_callable=AsyncMock) as close:
                self.assertEqual(await run_monitor(args, captured.append), 1)
            self.assertEqual(captured[-1]['code'], 'client_initialization_error')
            self.assertFalse(captured[-1]['login_attempted'])
            self.assertNotIn('SYNTHETIC_SECRET', json.dumps(captured))
            authenticate.assert_not_awaited()
            close.assert_not_awaited()

    async def test_only_selected_channel_and_self_can_be_read(self):
        from telethon import functions as f, types as t
        allowed = f.messages.GetHistoryRequest(t.InputPeerChannel(123456, 987654), 0, None, 0, 10, 0, 0, 0)
        self.client.check_request(allowed)
        denied = [f.contacts.GetContactsRequest(0),
                  f.contacts.ResolveUsernameRequest('unapproved'),
                  f.updates.GetStateRequest(),
                  f.updates.GetDifferenceRequest(1, datetime(2026, 10, 19, tzinfo=UTC), 0),
                  f.auth.ResetAuthorizationsRequest(),
                  f.channels.JoinChannelRequest(t.InputChannel(123456, 987654)),
                  f.messages.GetHistoryRequest(t.InputPeerChannel(999, 987654), 0, None, 0, 10, 0, 0, 0),
                  f.messages.GetHistoryRequest(t.InputPeerUser(123456, 987654), 0, None, 0, 10, 0, 0, 0),
                  f.users.GetUsersRequest([t.InputUser(123456, 987654)]),
                  f.messages.SendMessageRequest(t.InputPeerSelf(), 'TEST'),
                  f.messages.ViewSponsoredMessageRequest(b'UNAPPROVED'),
                  [allowed]]
        for request in denied:
            with self.subTest(request=type(request).__name__):
                with self.assertRaises(SourceFailure):
                    self.client.check_request(request)

    async def test_rpc_guard_runs_before_network_dispatch(self):
        from telethon import TelegramClient, functions
        with patch.object(TelegramClient, '__call__', new_callable=AsyncMock) as dispatch:
            with self.assertRaises(SourceFailure):
                await self.client(functions.contacts.GetContactsRequest(0))
            dispatch.assert_not_awaited()

    async def test_login_callback_does_not_request_account_differences(self):
        from telethon import types
        self.assertTrue(self.client._no_updates)
        self.assertFalse(self.client._catch_up)
        user = types.User(id=1, bot=False, access_hash=1)
        with patch.object(type(self.client), '__call__', new_callable=AsyncMock) as rpc:
            self.assertIs(await self.client._on_login(user), user)
            rpc.assert_not_awaited()

    async def test_pre_login_migration_metadata_is_allowed_but_not_account_differences(self):
        from telethon import functions
        self.client._authorized = False
        self.client.check_request(functions.updates.GetStateRequest())
        self.client.check_request(functions.help.GetConfigRequest())
        with self.assertRaises(SourceFailure):
            self.client.check_request(functions.updates.GetDifferenceRequest(1, datetime(2026,10,19,tzinfo=UTC), 0))

    async def test_send_code_retries_only_after_official_dc_switch(self):
        from telethon import errors, types
        self.client.session.set_dc(2, '149.154.167.51', 443)
        self.client._authorized = False
        calls = []
        def response(request, **_):
            calls.append(request)
            future = asyncio.get_running_loop().create_future()
            if len(calls) == 1:
                future.set_exception(errors.PhoneMigrateError(request, capture=4))
            else:
                future.set_result(types.auth.SentCode(type=types.auth.SentCodeTypeApp(5), phone_code_hash='SYNTHETIC_HASH'))
            return future
        async def switch(new_dc):
            self.assertEqual(new_dc, 4)
            self.client.session.set_dc(4, '149.154.167.91', 443)
        # Exercise the real pinned _call, including raise_last_call_error.
        with patch.object(self.client._sender, 'send', side_effect=response), \
             patch.object(self.client, '_switch_dc', side_effect=switch) as migration:
            await self.client.send_code_request(self.creds.phone)
        self.assertEqual(len(calls), 2)
        migration.assert_awaited_once()

    async def test_migration_error_without_dc_switch_cannot_resend_code(self):
        from telethon import errors
        self.client._authorized = False
        with patch.object(type(self.client), '__call__', new_callable=AsyncMock,
                          side_effect=errors.PhoneMigrateError(None, capture=4)) as rpc:
            with self.assertRaises(errors.PhoneMigrateError):
                await self.client.send_code_request(self.creds.phone)
        self.assertEqual(rpc.await_count, 1)

    async def test_auth_restart_does_not_resend_code(self):
        from telethon import errors
        with patch.object(type(self.client), '__call__', new_callable=AsyncMock, side_effect=errors.AuthRestartError(None)) as rpc:
            with self.assertRaises(errors.AuthRestartError):
                await self.client.send_code_request(self.creds.phone)
            self.assertEqual(rpc.await_count, 1)

    async def test_memory_session_does_not_cache_profiles_or_write_files(self):
        from telethon.crypto import AuthKey
        from telethon import types
        from telethon.sessions import MemorySession
        session = self.client.session
        self.assertIsInstance(session, MemorySession)
        session.auth_key = AuthKey(b'DUMMY_TEST_KEY__' * 16)
        session.process_entities([types.User(id=2, username='synthetic', phone='10000000000', access_hash=1)])
        self.assertEqual(session._entities, set())
        with patch('sqlite3.connect', side_effect=AssertionError('Disk session forbidden')):
            self.assertIsNone(session.save())
            self.assertIsNone(session.delete())

    async def test_reader_normalizes_caption_without_downloading(self):
        from telethon import types
        from dealradar.telegram_client import TelethonReader
        reader = TelethonReader(self.client, self.entity)
        message = types.Message(id=15, peer_id=types.PeerChannel(123456),
                                date=datetime(2026, 10, 19, tzinfo=UTC),
                                message='СИНТЕТИКА SSD NVMe 512ГБ 40$❌', media=types.MessageMediaPhoto())
        result = reader.post(message)
        self.assertEqual(result.message_id, 15)
        self.assertTrue(result.has_media)
        self.assertEqual(result.original_url, 'https://t.me/c/123456/15')
        message.peer_id = types.PeerChannel(999)
        with self.assertRaises(SourceFailure):
            reader.post(message)

    async def test_unsolicited_updates_are_not_retained_by_native_sender(self):
        queue = self.client._updates_queue
        self.assertIs(queue, self.client._sender._updates_queue)
        queue.put_nowait(SimpleNamespace(text='SYNTHETIC_UNRELATED_UPDATE'))
        await queue.put(SimpleNamespace(text='SYNTHETIC_UNRELATED_UPDATE'))
        self.assertEqual(queue.qsize(), 0)

    async def test_service_message_contains_no_profile_data(self):
        from telethon import types
        from dealradar.telegram_client import TelethonReader
        reader = TelethonReader(self.client, self.entity)
        service = types.MessageService(id=16, peer_id=types.PeerChannel(123456),
                    date=datetime(2026, 10, 19, tzinfo=UTC), action=types.MessageActionChatAddUser([999]))
        result = reader.post(service)
        self.assertEqual(result.text, '')
        self.assertNotIn('999', json.dumps(result.to_dict()))

    async def test_actual_library_pagination_is_scoped_and_ascending(self):
        from telethon import TelegramClient, functions, types
        from dealradar.telegram_client import TelethonReader
        reader = TelethonReader(self.client, self.entity)
        seen = []
        async def response(request, **kwargs):
            seen.append(request)
            self.assertIsInstance(request, functions.messages.GetHistoryRequest)
            records = [types.Message(id=item, peer_id=types.PeerChannel(123456), date=datetime(2026, 10, 19, tzinfo=UTC),
                                     message='СИНТЕТИКА SSD NVMe 512GB 40$') for item in (13, 12, 11)]
            return types.messages.Messages(messages=records, chats=[], users=[], topics=[])
        with patch.object(TelegramClient, '__call__', side_effect=response):
            posts = await reader.new_posts(10, 13)
        self.assertEqual([post.message_id for post in posts], [11, 12, 13])
        self.assertTrue(all(self.client.same_channel(request.peer) for request in seen))

    async def test_flood_wait_is_not_retried(self):
        from telethon import errors
        factory = AsyncMock(side_effect=errors.FloodWaitError(None, capture=300))
        with self.assertRaises(SourceFailure) as raised:
            await bounded_call(factory)
        self.assertEqual(raised.exception.retry_after, 300)
        self.assertEqual(factory.await_count, 1)

    async def test_first_head_flood_stops_watch_and_blocks_restart_before_secret_input(self):
        from dealradar.models import read_timestamp, utc_now
        from dealradar.storage import Store
        reader = SimpleNamespace(channel=Channel(APPROVED_USERNAME, 123456),
                                 latest_id=AsyncMock(side_effect=SourceFailure('rate_limited', 600)))
        captured = []
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'synthetic.sqlite3'
            args = SimpleNamespace(db=path, interval=60, start_now=True, watch=True)
            with patch('sys.stdin.isatty', return_value=True), \
                 patch('sys.stderr.isatty', return_value=True), \
                 patch('sys.stdout.isatty', return_value=True), \
                 patch('dealradar.telegram_client.prompt_credentials', return_value=self.creds) as prompt, \
                 patch('dealradar.telegram_client.build_client', return_value=self.client), \
                 patch('dealradar.telegram_client.login', new_callable=AsyncMock), \
                 patch('dealradar.telegram_client.TelethonReader.resolve', new_callable=AsyncMock, return_value=reader), \
                 patch('dealradar.telegram_client.close_client', new_callable=AsyncMock, return_value=True) as close, \
                 patch('dealradar.telegram_commands.asyncio.sleep', new_callable=AsyncMock,
                       side_effect=AssertionError('Must stop on first FloodWait')) as sleep:
                self.assertEqual(await run_monitor(args, captured.append), 2)
                # Fresh Store/process path must refuse login while pause persists.
                self.assertEqual(await run_monitor(args, captured.append), 2)
            prompt.assert_called_once()
            close.assert_awaited_once()
            sleep.assert_not_awaited()
            reader.latest_id.assert_awaited_once()
            self.assertTrue(any(item.get('state') == 'paused' for item in captured))
            with Store(path) as store:
                self.assertEqual(store.sources(), [])
                self.assertEqual(store.auth_pause()['last_error_code'], 'rate_limited')
                self.assertGreater(read_timestamp(store.auth_pause()['retry_not_before']), utc_now())

    async def test_network_retries_are_bounded(self):
        factory = AsyncMock(side_effect=OSError('DUMMY_SECRET'))
        with patch('dealradar.telegram_client.asyncio.sleep', new_callable=AsyncMock), self.assertRaises(SourceFailure) as raised:
            await bounded_call(factory)
        self.assertEqual(factory.await_count, 2)
        self.assertEqual(str(raised.exception), 'network_error')

    async def test_logout_failure_never_claims_success(self):
        fake = SimpleNamespace(log_out=AsyncMock(return_value=False), disconnect=AsyncMock(), session=SimpleNamespace(auth_key='DUMMY_KEY'))
        self.assertFalse(await close_client(fake))
        self.assertIsNone(fake.session.auth_key)
        fake.disconnect.assert_awaited_once()

    async def test_logout_success_and_disconnect(self):
        fake = SimpleNamespace(log_out=AsyncMock(return_value=True), disconnect=AsyncMock(), session=None)
        self.assertTrue(await close_client(fake))
        fake.disconnect.assert_awaited_once()

    async def test_login_without_two_step_protection_is_refused(self):
        fake = SimpleNamespace(connect=AsyncMock(), send_code_request=AsyncMock(),
                               sign_in=AsyncMock(return_value=SimpleNamespace(bot=False)))
        fake_call = AsyncMock(return_value=SimpleNamespace(has_password=False))
        # A callable class is needed for client(request).
        class Fake:
            connect, send_code_request, sign_in = fake.connect, fake.send_code_request, fake.sign_in
            async def __call__(self, request): return await fake_call(request)
        with self.assertRaisesRegex(ValueError, 'двухэтапную'):
            await login(Fake(), self.creds, prompt=lambda _: '12345')


@unittest.skipUnless(HAVE_TELETHON, 'Optional pinned Telegram dependencies are not installed')
class SelectedChannelSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.creds = Credentials(1, '0' * 32, '+10000000000')
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.disconnect()

    def client(self, username):
        client = build_client(self.creds, username=username)
        self.clients.append(client)
        return client

    def entity(self, username, channel_id=123456):
        from telethon import types
        return types.Channel(id=channel_id, title='СИНТЕТИЧЕСКИЙ ТЕСТ', photo=types.ChatPhotoEmpty(),
                             date=datetime(2026, 10, 19, tzinfo=UTC), broadcast=True,
                             username=username, access_hash=987654)

    async def test_selected_client_cannot_resolve_or_read_the_other_approved_channel(self):
        from telethon import functions as f, types as t
        for selected, other in ((APPROVED_USERNAME, TEST_USERNAME), (TEST_USERNAME, APPROVED_USERNAME)):
            with self.subTest(selected=selected):
                client = self.client(selected)
                client.approve_channel(self.entity(selected))
                client.check_request(f.contacts.ResolveUsernameRequest(selected))
                client.check_request(f.messages.GetHistoryRequest(t.InputPeerChannel(123456, 987654), 0, None, 0, 10, 0, 0, 0))
                denied = [f.contacts.ResolveUsernameRequest(other),
                          f.messages.GetHistoryRequest(t.InputPeerChannel(654321, 987654), 0, None, 0, 10, 0, 0, 0),
                          f.channels.JoinChannelRequest(t.InputChannel(123456, 987654)),
                          f.messages.SendMessageRequest(t.InputPeerChannel(123456, 987654), 'СИНТЕТИКА'),
                          f.messages.GetHistoryRequest(t.InputPeerUser(123456, 987654), 0, None, 0, 10, 0, 0, 0)]
                for request in denied:
                    with self.assertRaises(SourceFailure):
                        client.check_request(request)
                with self.assertRaises(SourceFailure):
                    client.approve_channel(self.entity(other))
                with self.assertRaises(SourceFailure):
                    client.approve_channel(self.entity(selected, 654321))

    async def test_unknown_channel_is_refused_before_client_construction(self):
        with patch('dealradar.telegram_client.check_dependencies') as dependencies:
            with self.assertRaises(ValueError):
                build_client(self.creds, username='unapproved')
            dependencies.assert_not_called()
        with patch('dealradar.telegram_commands.Store', side_effect=AssertionError('Database must not open')):
            with self.assertRaises(ValueError):
                await run_monitor(SimpleNamespace(channel='unapproved'), lambda _: None)

    async def test_test_reader_resolves_only_selected_source_and_checks_membership(self):
        from telethon import TelegramClient, functions as f, types as t
        from dealradar.telegram_client import TelethonReader
        client = self.client(TEST_USERNAME)
        entity = self.entity(TEST_USERNAME)
        seen = []
        async def response(request, **_):
            seen.append(request)
            if type(request) is f.contacts.ResolveUsernameRequest:
                return SimpleNamespace(peer=t.PeerChannel(entity.id), chats=[entity])
            if type(request) is f.channels.GetParticipantRequest:
                return SimpleNamespace(participant=SimpleNamespace())
            raise AssertionError('Unexpected synthetic RPC')
        with patch.object(TelegramClient, '__call__', side_effect=response):
            reader = await TelethonReader.resolve(client, expected_id=entity.id)
            self.assertEqual(reader.channel, Channel(TEST_USERNAME, entity.id))
            self.assertEqual(seen[0].username, TEST_USERNAME)
            self.assertIs(type(seen[1]), f.channels.GetParticipantRequest)
            self.assertIs(type(seen[1].participant), t.InputPeerSelf)
            # A renamed/reassigned username must stop before membership/history.
            seen.clear()
            with self.assertRaises(SourceFailure) as failure:
                await TelethonReader.resolve(client, expected_id=654321)
            self.assertEqual(failure.exception.code, 'identity_changed')
            self.assertEqual(len(seen), 1)

    async def test_wrong_database_refuses_both_modes_before_credentials_or_login(self):
        from dealradar.storage import Store
        with TemporaryDirectory() as temp:
            for stored, selected in ((APPROVED_USERNAME, TEST_USERNAME), (TEST_USERNAME, APPROVED_USERNAME)):
                path = Path(temp) / (stored + '.sqlite3')
                with Store(path) as store:
                    store.bind_telegram(stored, 123456, 0)
                args = SimpleNamespace(db=path, channel=selected, interval=60, start_now=True, watch=False)
                with patch('sys.stdin.isatty', return_value=True), \
                     patch('sys.stderr.isatty', return_value=True), \
                     patch('sys.stdout.isatty', return_value=True), \
                     patch('dealradar.telegram_client.prompt_credentials') as prompt, \
                     patch('dealradar.telegram_client.build_client') as build, \
                     patch('dealradar.telegram_client.login', new_callable=AsyncMock) as authenticate:
                    with self.assertRaises(ValueError):
                        await run_monitor(args, lambda _: None)
                prompt.assert_not_called()
                build.assert_not_called()
                authenticate.assert_not_awaited()

    async def test_console_path_passes_selected_source_and_labels_synthetic_channel(self):
        from dealradar.storage import Store
        client = self.client(TEST_USERNAME)
        reader = SimpleNamespace(channel=Channel(TEST_USERNAME, 123456), latest_id=AsyncMock(return_value=0),
                                 sponsored=AsyncMock(return_value=[]))
        captured = []
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'synthetic.sqlite3'
            args = SimpleNamespace(db=path, channel=TEST_USERNAME, interval=60, start_now=True, watch=False)
            with patch('sys.stdin.isatty', return_value=True), \
                 patch('sys.stderr.isatty', return_value=True), \
                 patch('sys.stdout.isatty', return_value=True), \
                 patch('dealradar.telegram_client.prompt_credentials', return_value=self.creds), \
                 patch('dealradar.telegram_client.build_client', return_value=client) as build, \
                 patch('dealradar.telegram_client.login', new_callable=AsyncMock), \
                 patch('dealradar.telegram_client.TelethonReader.resolve', new_callable=AsyncMock, return_value=reader), \
                 patch('dealradar.telegram_client.close_client', new_callable=AsyncMock, return_value=True) as close:
                self.assertEqual(await run_monitor(args, captured.append), 0)
            build.assert_called_once_with(self.creds, username=TEST_USERNAME)
            close.assert_awaited_once_with(client)
            result = next(item for item in captured if item.get('state') == 'initialized')
            self.assertTrue(result['test_channel'])
            self.assertEqual(result['source'], 'telegram:' + TEST_USERNAME)
            with Store(path) as store:
                self.assertEqual(store.sources()[0]['username'], TEST_USERNAME)
                self.assertEqual(store.stats()['counts']['posts'], 0)

    async def test_cli_source_choice_accepts_public_names_and_rejects_urls_outside_telegram(self):
        from contextlib import redirect_stderr
        import io
        from dealradar.cli import arguments
        self.assertEqual(arguments().parse_args(['telegram-run']).channel, APPROVED_USERNAME)
        self.assertEqual(arguments().parse_args(['telegram-run', '--channel', TEST_USERNAME]).channel, TEST_USERNAME)
        self.assertEqual(arguments().parse_args(['telegram-run', '--channel', 'https://t.me/synth_alpha']).channel, 'synth_alpha')
        self.assertTrue(arguments().parse_args(['telegram-run', '--all-sources']).all_sources)
        for value in ('https://evil.invalid/channel', 'https://t.me/testirovk4/1', '../data/live.sqlite3'):
            with self.subTest(value=value), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                arguments().parse_args(['telegram-run', '--channel', value])
            self.assertEqual(error.exception.code, 2)

    async def test_added_sources_can_be_selected_but_unapproved_and_disabled_peers_cannot(self):
        from telethon import functions as f, types as t
        client = build_client(self.creds, username='synth_alpha', approved_usernames=('synth_alpha', 'synth_beta'))
        self.clients.append(client)
        client.approve_channel(self.entity('synth_alpha', 123))
        client.check_request(f.messages.GetHistoryRequest(t.InputPeerChannel(123, 987654), 0, None, 0, 1, 0, 0, 0))
        with self.assertRaises(ValueError):
            client.select_source('unapproved')
        client.select_source('synth_beta')
        with self.assertRaises(SourceFailure):
            client.check_request(f.messages.GetHistoryRequest(t.InputPeerChannel(123, 987654), 0, None, 0, 1, 0, 0, 0))
        client.approve_channel(self.entity('synth_beta', 456))
        client.configure_sources(('synth_alpha',))
        with self.assertRaises(SourceFailure):
            client.check_request(f.contacts.ResolveUsernameRequest('synth_beta'))
        with self.assertRaises(ValueError):
            client.select_source('synth_beta')
        client.select_source('synth_alpha')
        with self.assertRaises(SourceFailure):
            client.check_request(f.channels.JoinChannelRequest(t.InputChannel(123, 987654)))


@unittest.skipUnless(HAVE_TELETHON, 'Optional pinned Telegram dependencies are not installed')
class NativeLoginFlowTests(unittest.IsolatedAsyncioTestCase):
    """Real SDK auth methods; only server replies, connection and SRP proof are fake."""

    async def asyncSetUp(self):
        from telethon import types
        self.creds = Credentials(1, '0' * 32, '+10000000000')
        self.client = build_client(self.creds)
        self.client._authorized = False
        self.user = types.User(id=1, bot=False, access_hash=1)
        self.proof = types.InputCheckPasswordSRP(1, b'SYNTHETIC_A', b'SYNTHETIC_M1')
        self.calls = []
        self.authorized = False
        self.requires_password = True
        self.has_password = True
        self.expired = False

    async def asyncTearDown(self):
        await self.client.disconnect()

    def server_reply(self, wrapped, **_):
        from telethon import errors, functions as f, types as t
        self.assertIs(type(wrapped), f.InvokeWithoutUpdatesRequest)
        request = wrapped.query
        kind = type(request)
        self.calls.append(kind)
        future = asyncio.get_running_loop().create_future()
        if kind is f.auth.SendCodeRequest:
            self.assertEqual(request.phone_number.lstrip('+'), '10000000000')
            result = t.auth.SentCode(t.auth.SentCodeTypeApp(5), 'SYNTHETIC_PHONE_CODE_HASH')
        elif kind is f.users.GetUsersRequest:
            self.assertTrue(all(type(user) is t.InputUserSelf for user in request.id))
            if not self.authorized:
                future.set_exception(errors.AuthKeyUnregisteredError(request))
                return future
            result = [self.user]
        elif kind is f.auth.SignInRequest:
            self.assertEqual(request.phone_number, '10000000000')
            self.assertEqual(request.phone_code_hash, 'SYNTHETIC_PHONE_CODE_HASH')
            self.assertEqual(request.phone_code, '12345')
            if self.expired:
                future.set_exception(errors.PhoneCodeExpiredError(request))
                return future
            if self.requires_password:
                future.set_exception(errors.SessionPasswordNeededError(request))
                return future
            self.authorized = True
            result = t.auth.Authorization(user=self.user)
        elif kind is f.account.GetPasswordRequest:
            result = SimpleNamespace(has_password=self.has_password)
        elif kind is f.auth.CheckPasswordRequest:
            self.assertIs(request.password, self.proof)
            self.authorized = True
            result = t.auth.Authorization(user=self.user)
        else:
            raise AssertionError('Unexpected RPC in synthetic login')
        future.set_result(result)
        return future

    async def test_native_send_code_and_sign_in_share_normalized_phone_hash(self):
        self.requires_password = False
        from telethon import functions as f
        with patch.object(self.client._sender, 'send', side_effect=self.server_reply), \
             patch('telethon.network.mtprotosender.MTProtoSender.connect',
                   side_effect=AssertionError('Real network forbidden')):
            await self.client.send_code_request(self.creds.phone)
            user = await self.client.sign_in(phone=self.creds.phone, code='12345')
        self.assertIs(user, self.user)
        self.assertEqual(self.client._phone, '10000000000')
        self.assertEqual(set(self.client._phone_code_hash), {'10000000000'})
        self.assertEqual(self.calls.count(f.auth.SendCodeRequest), 1)
        self.assertEqual(self.calls.count(f.auth.SignInRequest), 1)

    async def test_native_two_factor_login_without_connecting_or_logging_secrets(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        from telethon import functions as f
        output = io.StringIO()
        inputs = iter(('12345', 'SYNTHETIC_PASSWORD'))
        with patch.object(self.client, 'connect', new_callable=AsyncMock), \
             patch.object(self.client._sender, 'send', side_effect=self.server_reply), \
             patch('telethon.network.mtprotosender.MTProtoSender.connect',
                   side_effect=AssertionError('Real network forbidden')), \
             patch('telethon.client.auth.pwd_mod.compute_check', return_value=self.proof) as srp, \
             redirect_stdout(output), redirect_stderr(output):
            await login(self.client, self.creds, prompt=lambda _: next(inputs))
        self.assertTrue(self.client._authorized)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(self.calls, [f.auth.SendCodeRequest, f.users.GetUsersRequest,
                                     f.auth.SignInRequest, f.users.GetUsersRequest,
                                     f.account.GetPasswordRequest, f.auth.CheckPasswordRequest,
                                     f.account.GetPasswordRequest])
        srp.assert_called_once()

    async def test_expired_code_is_sanitized_and_not_resent_in_native_flow(self):
        from telethon import functions as f
        self.expired = True
        with patch.object(self.client, 'connect', new_callable=AsyncMock), \
             patch.object(self.client._sender, 'send', side_effect=self.server_reply), \
             patch('telethon.network.mtprotosender.MTProtoSender.connect',
                   side_effect=AssertionError('Real network forbidden')):
            with self.assertRaises(SourceFailure) as raised:
                await login(self.client, self.creds, prompt=lambda _: '12345')
        self.assertEqual(raised.exception.code, 'auth_required')
        self.assertEqual(self.client._phone_code_hash, {})
        self.assertEqual(self.calls.count(f.auth.SendCodeRequest), 1)
        self.assertEqual(self.calls.count(f.auth.SignInRequest), 1)

    async def test_native_login_without_two_factor_protection_is_still_refused(self):
        self.requires_password = False
        self.has_password = False
        with patch.object(self.client, 'connect', new_callable=AsyncMock), \
             patch.object(self.client._sender, 'send', side_effect=self.server_reply), \
             patch('telethon.network.mtprotosender.MTProtoSender.connect',
                   side_effect=AssertionError('Real network forbidden')):
            with self.assertRaisesRegex(ValueError, 'двухэтапную'):
                await login(self.client, self.creds, prompt=lambda _: '12345')


if __name__ == '__main__':
    unittest.main()
