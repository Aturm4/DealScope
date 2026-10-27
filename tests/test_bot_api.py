"""Synthetic protocol fixtures. No live bot, token, recipient or user login."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import ssl
from tempfile import TemporaryDirectory
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dealradar.bot_api import BotAPI, BotFailure, BotToken, MAX_RESPONSE, checked_user_id, prompt_token
from dealradar.bot_commands import command, link_bot, paired_user, prepare_notifier
from dealradar.storage import Store

BOT_ID, OWNER = 123456, 700001


def dummy_token():
    return BotToken(str(BOT_ID) + ':' + 'A' * 35)


def pair_update(owner=OWNER, text='/start synthetic_challenge'):
    return {'update_id': 10, 'message': {'from': {'id': owner, 'is_bot': False},
            'chat': {'id': owner, 'type': 'private'}, 'text': text}}


class BotAPITests(unittest.TestCase):
    def setUp(self):
        self.api = BotAPI(dummy_token(), owner_id=OWNER)
        self.response = Mock(status=200)
        self.response.read.return_value = json.dumps({'ok': True, 'result': {
            'message_id': 42, 'chat': {'type': 'private', 'id': OWNER}}}).encode()
        self.connection = Mock()
        self.connection.getresponse.return_value = self.response
        self.factory = patch('dealradar.bot_api.http.client.HTTPSConnection', return_value=self.connection).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.api.forget)

    def test_fixed_endpoint_verified_tls_no_paid_send_or_link_fetch(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:9999', 'SSLKEYLOGFILE': 'not-used-test-file'}):
            self.assertEqual(self.api.send('<b>СИНТЕТИЧЕСКИЙ ТЕСТ</b>'), 42)
        args, kwargs = self.factory.call_args
        self.assertEqual(args, ('api.telegram.org', 443))
        self.assertEqual(kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kwargs['context'].check_hostname)
        self.assertIsNone(kwargs['context'].keylog_filename)
        call = self.connection.request.call_args
        self.assertEqual(call.args[:2], ('POST', '/bot' + dummy_token().value + '/sendMessage'))
        body = json.loads(call.kwargs['body'])
        self.assertEqual(body['chat_id'], OWNER)
        self.assertFalse(body['allow_paid_broadcast'])
        self.assertEqual(body['link_preview_options'], {'is_disabled': True})
        self.assertEqual(set(body), {'chat_id', 'text', 'parse_mode', 'allow_paid_broadcast', 'link_preview_options'})
        self.connection.close.assert_called_once()

    def test_redirect_is_rejected_without_following(self):
        self.response.status = 302
        self.response.getheader.return_value = 'http://127.0.0.1/private'
        with self.assertRaises(BotFailure) as result:
            self.api.send('test')
        self.assertTrue(result.exception.ambiguous)
        self.factory.assert_called_once()
        self.response.read.assert_not_called()

    def test_timeout_is_uncertain_and_never_retried_or_printed(self):
        secret = dummy_token().value
        self.connection.getresponse.side_effect = TimeoutError('SYNTHETIC ' + secret)
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output), self.assertRaises(BotFailure) as result:
            self.api.send('test')
        self.assertEqual((result.exception.code, result.exception.ambiguous), ('network_error', True))
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn(secret, ''.join(traceback.format_exception(result.exception)))
        self.connection.request.assert_called_once()

    def test_429_retains_pause_without_retry(self):
        self.response.status = 429
        self.response.read.return_value = json.dumps({'ok': False, 'error_code': 429,
                                                     'description': 'ignored server text', 'parameters': {'retry_after': 71}}).encode()
        with self.assertRaises(BotFailure) as result:
            self.api.send('test')
        self.assertEqual((result.exception.code, result.exception.retry_after, result.exception.ambiguous), ('rate_limited', 71, False))
        self.connection.request.assert_called_once()

    def test_definite_failures_and_5xx_have_different_delivery_semantics(self):
        for code, expected, ambiguous in [(400, 'bad_request', False), (401, 'token_rejected', False),
                                          (403, 'recipient_unavailable', False), (500, 'protocol_error', True)]:
            with self.subTest(code=code):
                self.response.status = code
                self.response.read.return_value = json.dumps({'ok': False, 'error_code': code, 'description': dummy_token().value}).encode()
                with self.assertRaises(BotFailure) as result:
                    self.api.send('test')
                self.assertEqual((result.exception.code, result.exception.ambiguous), (expected, ambiguous))
                self.assertNotIn(dummy_token().value, str(result.exception))

    def test_malformed_oversized_duplicate_and_recursive_response_fail_closed(self):
        for raw in [b'not-json', b'{"ok":true,"ok":false}', b'[]', b'{}',
                    b'[' * 1500 + b']' * 1500, b'X' * (MAX_RESPONSE + 1)]:
            with self.subTest(size=len(raw)), self.assertRaises(BotFailure) as result:
                self.response.read.return_value = raw
                self.api.send('test')
            self.assertTrue(result.exception.ambiguous)
        self.response.read.assert_called_with(MAX_RESPONSE + 1)

    def test_wrong_recipient_or_message_id_does_not_count_as_success(self):
        for result in [{'message_id': 1, 'chat': {'type': 'private', 'id': OWNER + 1}},
                       {'message_id': 1, 'chat': {'type': 'group', 'id': OWNER}},
                       {'message_id': True, 'chat': {'type': 'private', 'id': OWNER}}]:
            self.response.read.return_value = json.dumps({'ok': True, 'result': result}).encode()
            with self.assertRaises(BotFailure) as caught:
                self.api.send('test')
            self.assertTrue(caught.exception.ambiguous)

    def test_unapproved_recipient_and_methods_never_reach_network(self):
        api = BotAPI(dummy_token())
        with self.assertRaises(BotFailure):
            api.send('test')
        with self.assertRaises(BotFailure):
            self.api._request('sendMessage', {'chat_id': OWNER + 1, 'text': 'test'})
        with self.assertRaises(BotFailure):
            self.api._request('deleteWebhook', {})
        with self.assertRaises(BotFailure):
            self.api.updates()
        self.factory.assert_not_called()

    def test_token_format_and_repr_do_not_expose_values(self):
        self.assertNotIn(dummy_token().value, repr(dummy_token()))
        for value in [None, '', 'https://example.invalid/token', '123456:' + 'X' * 29,
                      '123456:' + 'X' * 35 + '\n', '123456:' + 'X' * 35 + '/sendMessage']:
            with self.subTest(value_type=type(value)), self.assertRaises(BotFailure):
                BotToken(value)

    def test_private_ids_only(self):
        for value in [True, False, None, 0, -1, '700001', 2**52]:
            with self.assertRaises(BotFailure):
                checked_user_id(value)

    def test_no_echo_fallback_in_noninteractive_input(self):
        with patch('dealradar.bot_api.sys.stdin.isatty', return_value=False), patch('getpass.getpass') as getpass:
            with self.assertRaises(BotFailure):
                prompt_token()
            getpass.assert_not_called()

    def test_getme_checks_real_bot_identity(self):
        for bot_id, is_bot in [(BOT_ID + 1, True), (BOT_ID, False), (True, True)]:
            self.response.read.return_value = json.dumps({'ok': True, 'result': {'id': bot_id, 'is_bot': is_bot, 'username': 'test_bot'}}).encode()
            with self.assertRaises(BotFailure):
                self.api.identity()

    def test_webhook_is_never_deleted(self):
        self.response.read.return_value = json.dumps({'ok': True, 'result': {'url': 'https://example.invalid/webhook'}}).encode()
        with self.assertRaises(BotFailure) as result:
            self.api.ensure_no_webhook()
        self.assertEqual(result.exception.code, 'webhook_present')
        self.assertTrue(self.connection.request.call_args.args[1].endswith('/getWebhookInfo'))
        self.connection.request.assert_called_once()

    def test_pairing_updates_are_bounded_and_reject_invalid_ids(self):
        api = BotAPI(dummy_token())
        self.response.read.return_value = json.dumps({'ok': True, 'result': [pair_update()]}).encode()
        self.assertEqual(api.updates()[0]['update_id'], 10)
        body = json.loads(self.connection.request.call_args.kwargs['body'])
        self.assertEqual((body['limit'], body['timeout'], body['allowed_updates']), (100, 20, ['message']))
        self.response.read.return_value = json.dumps({'ok': True, 'result': [{'update_id': True}]}).encode()
        with self.assertRaises(BotFailure):
            api.updates()

    def test_forget_drops_reference_and_disables_client(self):
        self.api.forget()
        self.assertIsNone(self.api._token)
        with self.assertRaises(BotFailure):
            self.api.send('test')
        self.factory.assert_not_called()


class PairingTests(unittest.TestCase):
    def test_challenge_and_private_actor_must_all_match(self):
        self.assertEqual(paired_user(pair_update(), 'synthetic_challenge'), OWNER)
        variants = []
        for path, value in [(('text',), '/start'), (('from', 'is_bot'), True), (('from', 'id'), True),
                            (('chat', 'id'), OWNER + 1), (('chat', 'type'), 'group'),
                            (('chat', 'id'), -123), (('text',), '/start synthetic_challenge\n')]:
            update = pair_update()
            target = update['message']
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            variants.append(update)
        for key in ('forward_origin', 'forward_from', 'forward_sender_name', 'via_bot', 'sender_chat'):
            update = pair_update()
            update['message'][key] = {}
            variants.append(update)
        variants += [{}, {'channel_post': pair_update()['message']}]
        for update in variants:
            self.assertIsNone(paired_user(update, 'synthetic_challenge'))

    def run_pair(self, path, confirm):
        api = Mock(bot_id=BOT_ID)
        api.identity.return_value = {'bot_id': BOT_ID, 'username': 'synthetic_test_bot'}
        api.updates.side_effect = [[pair_update(OWNER + 1, '/start')], [pair_update()]]
        outputs = []
        with patch('dealradar.bot_commands.require_console'), patch('dealradar.bot_commands.prompt_token', return_value=dummy_token()), \
             patch('dealradar.bot_commands.BotAPI', return_value=api), patch('dealradar.bot_commands.secrets.token_urlsafe', return_value='synthetic_challenge'), \
             patch('builtins.input', side_effect=confirm):
            result = asyncio.run(link_bot(SimpleNamespace(db=path), outputs.append))
        return result, outputs, api

    def test_stranger_cannot_enroll_and_local_confirmation_precedes_storage(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'synthetic.sqlite3'
            def confirm(_):
                with Store(path) as store:
                    self.assertIsNone(store.notifier())
                return str(OWNER)
            result, output, api = self.run_pair(path, confirm)
            self.assertEqual(result, 0)
            with Store(path) as store:
                self.assertEqual((store.notifier()['bot_id'], store.notifier()['owner_id']), (BOT_ID, OWNER))
            self.assertNotIn(dummy_token().value, path.read_bytes().decode('latin1'))
            self.assertNotIn('synthetic_challenge', path.read_bytes().decode('latin1'))
            api.send.assert_not_called()
            api.forget.assert_called_once()

    def test_wrong_local_confirmation_leaves_no_binding(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'synthetic.sqlite3'
            with self.assertRaises(BotFailure):
                self.run_pair(path, lambda _: str(OWNER + 1))
            with Store(path) as store:
                self.assertIsNone(store.notifier())

    def test_noninteractive_pairing_creates_no_files(self):
        with TemporaryDirectory() as temp, patch('dealradar.bot_api.sys.stdin.isatty', return_value=False):
            self.assertEqual(command(SimpleNamespace(db=Path(temp) / 'not-created.sqlite3'), lambda _: None), 2)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_preparing_wrong_bot_releases_token_before_user_login(self):
        with TemporaryDirectory() as temp, Store(Path(temp) / 'synthetic.sqlite3') as store:
            store.link_notifier(BOT_ID, OWNER)
            api = Mock()
            api.identity.return_value = {'bot_id': BOT_ID + 1, 'username': 'wrong_bot'}
            with patch('dealradar.bot_commands.prompt_token', return_value=dummy_token()), patch('dealradar.bot_commands.BotAPI', return_value=api):
                with self.assertRaises(BotFailure):
                    asyncio.run(prepare_notifier(store, lambda _: None))
            api.forget.assert_called_once()
            api.send.assert_not_called()

    def test_saved_bot_pause_is_checked_before_prompt_or_request(self):
        from datetime import timedelta
        from dealradar.models import timestamp, utc_now
        with TemporaryDirectory() as temp, Store(Path(temp) / 'synthetic.sqlite3') as store:
            store.link_notifier(BOT_ID, OWNER)
            store.connection.execute('UPDATE notifier_settings SET not_before=?', (timestamp(utc_now() + timedelta(seconds=90)),))
            with patch('dealradar.bot_commands.prompt_token') as prompt, patch('dealradar.bot_commands.BotAPI') as api:
                with self.assertRaises(BotFailure) as result:
                    asyncio.run(prepare_notifier(store, lambda _: None))
                self.assertEqual(result.exception.code, 'rate_limited')
                prompt.assert_not_called()
                api.assert_not_called()
