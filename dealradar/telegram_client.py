"""Pinned Telethon transport, interactive secrets and memory-only authorization.

The RPC guard constrains our client code, NOT the server permissions of its key.
No callers should print a client, request, session or underlying exception.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import getpass
from importlib.metadata import PackageNotFoundError, version
import logging
import re
import sys
import warnings

from .models import IncomingPost, checked_url
from .telegram_source import APPROVED_USERNAME, PAGE_SIZE, Channel, SourceFailure, checked_username

EXPECTED_VERSIONS = {'Telethon': '1.45.0', 'rsa': '4.9.1', 'pyasn1': '0.6.4', 'pyaes': '1.6.1'}

_SETUP_MESSAGES = {
    'invalid_api_id': 'API ID должен быть положительным целым числом из API development tools, не токеном бота.',
    'invalid_api_hash': 'API hash должен содержать ровно 32 символа: цифры 0–9 и буквы a–f. Нужен API hash, не токен бота.',
    'invalid_phone': 'Нужен полный номер с + и кодом страны. Пробелы, скобки и дефисы допустимы; код страны не угадывается.',
    'interactive_console_required': 'Нужна самостоятельная интерактивная консоль пользователя без перенаправления ввода/вывода.',
    'hidden_input_unavailable': 'Скрытый ввод недоступен; ввод отменён без отображения значения.',
    'empty_input': 'Поле оставлено пустым. Введите значение только в своей локальной консоли.',
    'input_too_long': 'Введённое значение превышает допустимую длину; проверьте, что скопировано только нужное поле.',
    'client_initialization_error': 'Не удалось подготовить локальный клиент. Запустите telegram-preflight; вход не начинался.',
    'local_setup_error': 'Не удалось подготовить локальный ввод. Значения реквизитов в чат не присылайте.',
}


class LocalSetupError(ValueError):
    """Only application-owned codes and fixed text, never a supplied value."""

    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in _SETUP_MESSAGES else 'local_setup_error'
        self.notice = _SETUP_MESSAGES[self.code]
        super().__init__(self.notice)


def _checked_api_id(value):
    if type(value) is not int or not 0 < value < 2**31:
        raise LocalSetupError('invalid_api_id')
    return value


def _checked_api_hash(value):
    if not isinstance(value, str) or not re.fullmatch('[a-fA-F0-9]{32}', value.strip()):
        raise LocalSetupError('invalid_api_hash')
    return value.strip()


def _checked_phone(value):
    if not isinstance(value, str):
        raise LocalSetupError('invalid_phone')
    phone = re.sub(r'[ ()-]', '', value.strip())
    if not re.fullmatch(r'\+[1-9][0-9]{7,14}', phone):
        raise LocalSetupError('invalid_phone')
    return phone


def check_dependencies() -> dict[str, str]:
    try:
        installed = {name: version(name) for name in EXPECTED_VERSIONS}
    except PackageNotFoundError:
        raise ValueError('Установите проверенные зависимости в .venv по README.') from None
    if installed != EXPECTED_VERSIONS:
        raise ValueError('Версии зависимостей отличаются от проверенного набора; вход заблокирован.')
    return installed


def hidden(prompt: str) -> str:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise LocalSetupError('interactive_console_required')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            value = getpass.getpass(prompt, stream=sys.stderr)
        except (getpass.GetPassWarning, EOFError):
            raise LocalSetupError('hidden_input_unavailable') from None
    if not value:
        raise LocalSetupError('empty_input')
    if len(value) > 1024:
        raise LocalSetupError('input_too_long')
    return value


@dataclass(repr=False)
class Credentials:
    api_id: int = field(repr=False)
    api_hash: str = field(repr=False)
    phone: str = field(repr=False)

    def __post_init__(self):
        self.api_id = _checked_api_id(self.api_id)
        self.api_hash = _checked_api_hash(self.api_hash)
        self.phone = _checked_phone(self.phone)


def prompt_credentials() -> Credentials:
    raw_id = hidden('Ваш API ID (скрытый ввод): ').strip()
    if not re.fullmatch('[0-9]{1,10}', raw_id):
        raise LocalSetupError('invalid_api_id')
    api_id = _checked_api_id(int(raw_id))
    api_hash = _checked_api_hash(hidden('Ваш API hash (скрытый ввод): '))
    phone = _checked_phone(hidden('Ваш номер с + (скрытый ввод): '))
    return Credentials(api_id, api_hash, phone)


def build_client(credentials: Credentials, *, username=APPROVED_USERNAME, approved_usernames=None):
    from .telegram_source import APPROVED_USERNAMES, MAX_SOURCES, public_username
    approved = tuple(dict.fromkeys(public_username(value) for value in approved_usernames)) if approved_usernames is not None else APPROVED_USERNAMES
    if not approved or len(approved) > MAX_SOURCES:
        raise ValueError('Допускается до пяти явно согласованных каналов.')
    username = checked_username(username, approved=approved)
    check_dependencies()
    from telethon import TelegramClient, errors, functions, types, utils
    from telethon.sessions import MemorySession
    from .handshake_safety import install_bounds

    class MinimalSession(MemorySession):
        def process_entities(self, _):
            pass

        def set_update_state(self, *_):
            pass

        def cache_file(self, *_):
            pass

    class DiscardUpdates(asyncio.Queue):
        def put_nowait(self, _):
            pass

        async def put(self, _):
            pass

    class ChannelClient(TelegramClient):
        bound_channel = None
        sponsored_ids = None
        _selected_username = username
        _approved_usernames = approved

        def configure_sources(self, usernames):
            values = tuple(public_username(value) for value in usernames)
            if not values or len(values) > MAX_SOURCES or len(set(values)) != len(values):
                raise SourceFailure('access_denied')
            self._approved_usernames = values

        def select_source(self, value):
            value = checked_username(value, approved=self._approved_usernames)
            if value != self._selected_username:
                self.bound_channel = None
                self.sponsored_ids = None
            self._selected_username = value

        @property
        def source_username(self):
            return self._selected_username

        def approve_channel(self, channel):
            if not isinstance(channel, types.Channel) or not channel.broadcast or channel.megagroup:
                raise SourceFailure('access_denied')
            if (channel.username or '').casefold() != self.source_username or not channel.access_hash:
                raise SourceFailure('identity_changed')
            if self.bound_channel is not None and self.bound_channel != (channel.id, channel.access_hash):
                raise SourceFailure('identity_changed')
            self.bound_channel = (channel.id, channel.access_hash)

        def same_channel(self, peer):
            return isinstance(peer, (types.InputPeerChannel, types.InputChannel)) and self.bound_channel == (peer.channel_id, peer.access_hash)

        def check_request(self, request):
            # No batch/wrapper input: it could hide an unrelated RPC.
            kind = type(request)
            auth = functions.auth
            if kind in (auth.SendCodeRequest, auth.SignInRequest, auth.CheckPasswordRequest,
                        auth.LogOutRequest, functions.account.GetPasswordRequest):
                return
            if kind is functions.help.GetConfigRequest:
                return  # Native DC routing metadata, not account content.
            if kind is functions.updates.GetStateRequest and self._authorized is not True:
                return  # Native pre-login migration authorization check only.
            if kind is functions.users.GetUsersRequest and request.id and all(type(user) is types.InputUserSelf for user in request.id):
                return
            if self.source_username not in self._approved_usernames:
                raise SourceFailure('access_denied')
            if kind is functions.contacts.ResolveUsernameRequest and request.username.casefold() == self.source_username and not request.referer:
                return
            if kind is functions.channels.GetParticipantRequest and self.same_channel(request.channel) and type(request.participant) is types.InputPeerSelf:
                return
            if kind is functions.messages.GetHistoryRequest and self.same_channel(request.peer) and 0 < request.limit <= PAGE_SIZE:
                return
            if kind is functions.channels.GetMessagesRequest and self.same_channel(request.channel) and 0 < len(request.id) <= PAGE_SIZE:
                return
            if kind is functions.messages.GetSponsoredMessagesRequest and self.same_channel(request.peer):
                return
            if kind is functions.messages.ViewSponsoredMessageRequest and request.random_id in (self.sponsored_ids or set()):
                return
            raise SourceFailure('access_denied')

        async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
            self.check_request(request)
            return await super().__call__(request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold)

        async def _on_login(self, user):
            # Pinned 1.45.0 normally calls account-wide GetDifference here.
            # With polling/no updates, retain only self identity in RAM.
            if not self._no_updates or user.bot:
                raise SourceFailure('access_denied')
            self._mb_entity_cache.set_self_user(user.id, user.bot, user.access_hash)
            self._authorized = True
            return user

        async def _update_loop(self):
            # Do not process/cache unsolicited account updates or request differences.
            await asyncio.shield(self.disconnected)

        async def send_code_request(self, phone):
            # Native sign_in looks up phone_code_hash under parse_phone(phone).
            # Use that same key when requesting/storing the code response.
            phone = utils.parse_phone(phone)
            if not phone:
                raise SourceFailure('invalid_input')
            request = functions.auth.SendCodeRequest(phone, self.api_id, self.api_hash, types.CodeSettings())
            old_dc = self.session.dc_id
            try:
                result = await self(request)
            except (errors.PhoneMigrateError, errors.NetworkMigrateError, errors.UserMigrateError):
                # Pinned _call switches DC, then rethrows the migration error
                # when its single attempt is exhausted. One retry after switch.
                if self.session.dc_id == old_dc or self._authorized is True:
                    raise
                result = await self(request)
            if not isinstance(result, types.auth.SentCode) or not result.phone_code_hash:
                raise SourceFailure('auth_required')
            self._phone = phone
            self._phone_code_hash[phone] = result.phone_code_hash
            return result

    logger = logging.getLogger('dealradar.transport')
    logger.setLevel(logging.CRITICAL + 1)
    logger.propagate = False
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    client = ChannelClient(
        MinimalSession(), credentials.api_id, credentials.api_hash,
        connection=install_bounds(),
        receive_updates=False, catch_up=False, request_retries=0,
        connection_retries=0, auto_reconnect=False, flood_sleep_threshold=0,
        raise_last_call_error=True, timeout=10, base_logger=logger,
        device_model='Personal Deal Radar', app_version='0.2.0',
        system_version='Windows', lang_code='ru', system_lang_code='ru',
    )
    # Pinned sender could otherwise retain unsolicited updates in an unbounded
    # queue even though this application never processes account updates.
    discarded = DiscardUpdates()
    client._updates_queue = discarded
    client._sender._updates_queue = discarded
    return client


def sanitized_failure(exc: Exception) -> SourceFailure:
    from telethon import errors
    if isinstance(exc, SourceFailure):
        return exc
    if isinstance(exc, errors.FloodError):
        seconds = getattr(exc, 'seconds', None)
        if type(seconds) is int and 0 <= seconds <= 2**31:
            return SourceFailure('rate_limited', max(1, seconds))
        return SourceFailure('access_denied')
    if isinstance(exc, (asyncio.TimeoutError, errors.TimedOutError)):
        return SourceFailure('timeout')
    if isinstance(exc, (OSError, ConnectionError, errors.ServerError)):
        return SourceFailure('network_error')
    if isinstance(exc, errors.UnauthorizedError):
        return SourceFailure('auth_required')
    if isinstance(exc, (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError,
                        errors.PasswordHashInvalidError, errors.ApiIdInvalidError)):
        return SourceFailure('auth_required')
    if isinstance(exc, (errors.ChannelPrivateError, errors.UserNotParticipantError, errors.ChatAdminRequiredError)):
        return SourceFailure('access_denied')
    return SourceFailure('protocol_error')


async def bounded_call(factory, *, attempts: int = 2):
    for attempt in range(attempts):
        try:
            return await asyncio.wait_for(factory(), timeout=15)
        except Exception as exc:
            failure = sanitized_failure(exc)
            if failure.code not in ('timeout', 'network_error') or attempt + 1 == attempts:
                raise failure from None
            await asyncio.sleep(2)


async def login(client, credentials: Credentials, *, prompt=hidden):
    from telethon import errors, functions
    await bounded_call(client.connect, attempts=1)
    # A single code request: no resend loop, signup, QR URL or forced SMS.
    await bounded_call(lambda: client.send_code_request(credentials.phone), attempts=1)
    code = prompt('Код входа из Telegram (скрытый ввод): ')
    if not re.fullmatch('[0-9]{4,8}', code):
        raise SourceFailure('auth_required')
    try:
        try:
            user = await asyncio.wait_for(client.sign_in(phone=credentials.phone, code=code), timeout=20)
        except errors.SessionPasswordNeededError:
            password = prompt('Ваш пароль двухэтапной защиты (скрытый ввод): ')
            user = await asyncio.wait_for(client.sign_in(password=password), timeout=20)
            password = None
    except Exception as exc:
        raise sanitized_failure(exc) from None
    finally:
        code = None
    if not user or user.bot:
        raise SourceFailure('access_denied')
    protection = await bounded_call(lambda: client(functions.account.GetPasswordRequest()), attempts=1)
    if not protection.has_password:
        raise ValueError('Сначала включите двухэтапную защиту в официальном Telegram; эта сессия будет закрыта.')
    return user.id


async def close_client(client) -> bool:
    confirmed = False
    try:
        confirmed = await asyncio.wait_for(client.log_out(), timeout=15) is True
    except (Exception, asyncio.CancelledError):
        pass
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5)
        except (Exception, asyncio.CancelledError):
            pass
        # Drop application references; Python/OS do not guarantee RAM zeroization.
        if client.session is not None:
            client.session.auth_key = None
    return confirmed


class TelethonReader:
    def __init__(self, client, entity):
        from telethon import types
        client.approve_channel(entity)
        self.client = client
        self.peer = types.InputPeerChannel(entity.id, entity.access_hash)
        self.input_channel = types.InputChannel(entity.id, entity.access_hash)
        self.channel = Channel(client.source_username, entity.id)

    @classmethod
    async def resolve(cls, client, expected_id=None):
        from telethon import functions, types
        resolved = await bounded_call(lambda: client(functions.contacts.ResolveUsernameRequest(client.source_username)))
        channels = [chat for chat in resolved.chats if isinstance(chat, types.Channel)
                    and isinstance(resolved.peer, types.PeerChannel) and chat.id == resolved.peer.channel_id]
        if len(channels) != 1:
            raise SourceFailure('access_denied')
        if expected_id is not None and channels[0].id != expected_id:
            raise SourceFailure('identity_changed')
        reader = cls(client, channels[0])
        membership = await bounded_call(lambda: client(functions.channels.GetParticipantRequest(reader.input_channel, types.InputPeerSelf())))
        if isinstance(membership.participant, (types.ChannelParticipantBanned, types.ChannelParticipantLeft)):
            raise SourceFailure('access_denied')
        return reader

    def post(self, message):
        from telethon import types
        if (not isinstance(message, (types.Message, types.MessageService)) or not isinstance(message.peer_id, types.PeerChannel)
                or message.peer_id.channel_id != self.channel.channel_id):
            raise SourceFailure('protocol_error')
        try:
            return IncomingPost(message.id, getattr(message, 'message', '') or '', message.date,
                                original_url=f'https://t.me/c/{self.channel.channel_id}/{message.id}',
                                edited_at=getattr(message, 'edit_date', None), has_media=getattr(message, 'media', None) is not None)
        except (ValueError, TypeError):
            raise SourceFailure('invalid_input') from None

    async def latest_id(self):
        from telethon import functions
        result = await bounded_call(lambda: self.client(functions.messages.GetHistoryRequest(
            self.peer, offset_id=0, offset_date=None, add_offset=0, limit=1, max_id=0, min_id=0, hash=0)))
        return max((message.id for message in result.messages), default=0)

    async def new_posts(self, after, upper):
        async def fetch():
            return await self.client.get_messages(self.peer, min_id=after, max_id=upper + 1,
                                                  reverse=True, limit=PAGE_SIZE, wait_time=0)
        messages = await bounded_call(fetch)
        from telethon import types
        return [self.post(message) for message in messages
                if isinstance(message, (types.Message, types.MessageService))]

    async def existing_posts(self, ids):
        messages = await bounded_call(lambda: self.client.get_messages(self.peer, ids=ids))
        if len(messages) != len(ids):
            raise SourceFailure('protocol_error')
        return {item: self.post(message) if message is not None else None for item, message in zip(ids, messages)}

    async def sponsored(self):
        from telethon import functions
        result = await bounded_call(lambda: self.client(functions.messages.GetSponsoredMessagesRequest(self.peer)))
        messages = getattr(result, 'messages', [])
        self.client.sponsored_ids = {message.random_id for message in messages[:10]}
        ads = []
        for message in messages[:10]:
            try:
                url = checked_url(message.url)
            except ValueError:
                url = None
            ads.append({'label': 'Реклама — официальное спонсируемое сообщение', 'text': message.message,
                        'url': url, 'random_id': message.random_id})
        return ads

    async def sponsored_viewed(self, random_id):
        from telethon import functions
        await bounded_call(lambda: self.client(functions.messages.ViewSponsoredMessageRequest(random_id)), attempts=1)
