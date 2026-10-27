"""One explicitly selected approved source per database and client.

The transport is injected; tests use synthetic records, never a real account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
import re

from .models import IncomingPost, read_timestamp, utc_now
from .storage import Store

APPROVED_USERNAME = 'sborkipctashkent'
TEST_USERNAME = 'testirovk4'
APPROVED_USERNAMES = (APPROVED_USERNAME, TEST_USERNAME)
SOURCE_KEY = 'telegram:' + APPROVED_USERNAME
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_SOURCES = 5


def public_username(value: str) -> str:
    """Parse an address, never fetch it. Invite/post URLs are deliberately unsupported."""
    if type(value) is not str or len(value) > 100:
        raise ValueError('Укажите публичный канал: https://t.me/username или @username.')
    value = value.strip()
    if value.startswith('https://t.me/'):
        value = value[len('https://t.me/'):].removesuffix('/')
    elif value.startswith('@'):
        value = value[1:]
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', value, flags=re.ASCII):
        raise ValueError('Нужен публичный username канала, без ссылки на пост, приглашения и параметров.')
    return value.lower()


def checked_username(username: str, *, approved=APPROVED_USERNAMES) -> str:
    username = public_username(username)
    if username not in approved:
        raise ValueError('Канал не входит в согласованный список.')
    return username


class SourceFailure(Exception):
    """Only a fixed code/delay, never a library exception or secret-bearing text."""

    def __init__(self, code: str, retry_after: int = 0):
        if code not in {'timeout', 'network_error', 'access_denied', 'rate_limited',
                        'auth_required', 'identity_changed', 'protocol_error', 'invalid_input'}:
            code = 'protocol_error'
        self.code = code
        self.retry_after = retry_after if type(retry_after) is int and 0 <= retry_after <= 2**31 else 0
        super().__init__(code)


@dataclass(frozen=True)
class Channel:
    username: str
    channel_id: int


class Reader(Protocol):
    channel: Channel

    async def latest_id(self) -> int: ...
    async def new_posts(self, after: int, upper: int) -> list[IncomingPost]: ...
    async def existing_posts(self, ids: list[int]) -> dict[int, IncomingPost | None]: ...


class Monitor:
    def __init__(self, store: Store, reader: Reader, *, username=APPROVED_USERNAME, now=utc_now):
        self.store, self.reader, self.now = store, reader, now
        self.username = checked_username(username, approved=(*APPROVED_USERNAMES, *store.telegram_usernames()))
        self.source_key = 'telegram:' + self.username
        channel = reader.channel
        if channel.username != self.username or type(channel.channel_id) is not int or not 0 < channel.channel_id < 2**63:
            raise SourceFailure('access_denied')

    async def poll(self, *, start_now: bool = False) -> dict:
        self.store.check_telegram_scope(self.username)
        status = self.store.telegram_status(self.source_key)
        if status and not status['enabled']:
            return {'state': 'disabled', 'new': 0, 'edits': 0}
        initialized = bool(status and status['channel_id'] is not None)
        if initialized and status['channel_id'] != self.reader.channel.channel_id:
            self.store.telegram_retry(self.source_key, 'identity_changed', None)
            return {'state': 'error', 'code': 'identity_changed'}
        if status and status['retry_not_before'] and read_timestamp(status['retry_not_before']) > self.now():
            return {'state': 'paused', 'retry_not_before': status['retry_not_before']}
        if not initialized and not start_now:
            raise ValueError('Для первого подключения требуется явно выбранная точка --start-now.')
        created = edits = missing = 0
        try:
            if status:
                self.store.telegram_check_state(self.source_key, 'checking')
            head = await self.reader.latest_id()
            if type(head) is not int or not 0 <= head < 2**31:
                raise SourceFailure('protocol_error')
            if not initialized:
                self.store.bind_telegram(self.username, self.reader.channel.channel_id, head)
                self.store.checked_telegram(self.source_key, head)
                return {'state': 'initialized', 'boundary_id': head, 'new': 0, 'edits': 0,
                        'notice': 'Старые публикации не импортированы.'}
            cursor = status['last_message_id']
            upper = max(cursor, head)
            finished = cursor >= upper
            for _ in range(MAX_PAGES):
                if finished:
                    break
                posts = await self.reader.new_posts(cursor, upper)
                if not isinstance(posts, list) or len(posts) > PAGE_SIZE:
                    raise SourceFailure('protocol_error')
                ids = [post.message_id for post in posts]
                if len(set(ids)) != len(ids) or any(not cursor < item <= upper for item in ids):
                    raise SourceFailure('protocol_error')
                if not posts:
                    finished = True
                    break
                for post in sorted(posts, key=lambda item: item.message_id):
                    result = self.store.process(self.source_key, post, origin='telegram')
                    created += result.status == 'created'
                    cursor = post.message_id
                finished = cursor >= upper or len(posts) < PAGE_SIZE
            ids = self.store.edit_ids(self.source_key, status['edit_before_id'])
            if ids:
                fetched = await self.reader.existing_posts(ids)
                if set(fetched) != set(ids):
                    raise SourceFailure('protocol_error')
                for item in ids:
                    post = fetched[item]
                    if post is None:
                        self.store.unavailable(self.source_key, item)
                        missing += 1
                    else:
                        if post.message_id != item:
                            raise SourceFailure('protocol_error')
                        result = self.store.process(self.source_key, post, origin='telegram')
                        edits += result.status == 'edited'
            self.store.advance_edit_scan(self.source_key, min(ids) if len(ids) == PAGE_SIZE else None)
            if finished:
                self.store.checked_telegram(self.source_key, upper)
            else:
                self.store.telegram_check_state(self.source_key, 'catching_up')
            return {'state': 'ok' if finished else 'catching_up', 'new': created, 'edits': edits,
                    'unavailable': missing, 'cursor': upper if finished else cursor}
        except SourceFailure as exc:
            delay = exc.retry_after if exc.code == 'rate_limited' else 30
            until = self.now() + timedelta(seconds=max(1, delay)) if exc.code in {'rate_limited', 'timeout', 'network_error'} else None
            if self.store.telegram_status(self.source_key):
                self.store.telegram_retry(self.source_key, exc.code, until)
            if exc.code == 'rate_limited' and until:
                self.store.pause_auth(until, 'rate_limited')
            return {'state': 'error', 'code': exc.code, 'new': created, 'edits': edits,
                    'retry_not_before': until.isoformat() if until else None}

