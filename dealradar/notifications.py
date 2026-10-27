"""Escaped, bounded cards and one outbox worker, separate from user-account RPCs."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from html import escape
import re

from .bot_api import BotFailure
from .models import read_timestamp, utc_now
from .telegram_source import SourceFailure, TEST_USERNAME, public_username


def _label(value, limit=100):
    text = re.sub(r'[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]', ' ', str(value))
    return escape(text[:limit], quote=True)


def format_notification(post: dict, offers: list, rules: list) -> str:
    username = public_username(post['source_key'].removeprefix('telegram:'))
    message_id = post['message_id']
    if type(message_id) is not int or not 0 < message_id < 2**31:
        raise ValueError('Некорректный ID оригинала.')
    header = '<b>СИНТЕТИЧЕСКИЙ ТЕСТ — вымышленное объявление</b>' if username == TEST_USERNAME else '<b>Новое предложение по твоим правилам</b>'
    lines = [header, 'Источник: ' + _label(post['name'], 50)]
    for offer in sorted(offers, key=lambda item: item.ordinal)[:5]:
        model = ' '.join(value for value in (offer.brand, offer.model) if value) or 'модель не указана'
        state = {'new': 'новый', 'used': 'б/у'}.get(offer.condition, 'состояние не указано')
        lines.append(f'• {_label(model, 60)}; {_label(offer.capacity_gb)} ГБ; '
                     f'{_label(offer.interface).upper()}; {_label(offer.price)} {_label(offer.currency)}; {state}')
    if len(offers) > 5:
        lines.append(f'Ещё подходящих позиций: {len(offers) - 5}. Открой оригинал.')
    for rule in rules[:3]:
        lines.append(f'Пресет «{_label(rule.name, 40)}»: цена ≤ {_label(rule.max_price)} {_label(rule.currency)}.')
    if len(rules) > 3:
        lines.append(f'Также совпали пресеты: {len(rules) - 3}.')
    # Only a generated link to an approved Telegram source, never an ad-supplied URL.
    lines.append(f'<a href="https://t.me/{username}/{message_id}">Оригинал объявления</a>')
    lines.append('Наличие уточняй в оригинале: учтены пометки в последнем прочитанном тексте.')
    text = '\n'.join(lines)
    if len(text.encode('utf-16-le')) // 2 > 4096:
        raise ValueError('Уведомление превышает лимит Telegram.')
    return text


class Dispatcher:
    def __init__(self, store, bot, source_key: str, *, now=utc_now, sleep=asyncio.sleep):
        self.store, self.bot, self.source_key, self.now, self.sleep = store, bot, source_key, now, sleep
        self.username = public_username(source_key.removeprefix('telegram:'))
        store.check_telegram_scope(self.username)

    async def tick(self, reader) -> dict:
        config = self.store.notifier()
        state = self.store.telegram_status(self.source_key)
        if (not config or not config['enabled'] or not state or not state['enabled']):
            return {'state': 'disabled', 'sent': 0}
        if config['bot_id'] != self.bot.bot_id or config['owner_id'] != self.bot.owner_id:
            raise BotFailure('identity_changed')
        if reader.channel.username != self.username or reader.channel.channel_id != state['channel_id']:
            raise SourceFailure('identity_changed')
        if state['retry_not_before'] and read_timestamp(state['retry_not_before']) > self.now():
            return {'state': 'source_paused', 'sent': 0, 'retry_not_before': state['retry_not_before']}
        sent = 0
        candidates = self.store.notification_candidates(now=self.now, source_key=self.source_key)
        for candidate in candidates:
            if candidate['source_key'] != self.source_key:
                raise SourceFailure('access_denied')
            item = candidate['message_id']
            try:
                # Catch sold/edited/deleted posts that were waiting while the computer was off.
                posts = await reader.existing_posts([item])
                if set(posts) != {item} or (posts[item] is not None and posts[item].message_id != item):
                    raise SourceFailure('protocol_error')
                if posts[item] is None:
                    self.store.unavailable(self.source_key, item)
                else:
                    self.store.process(self.source_key, posts[item], origin='telegram')
            except SourceFailure as exc:
                delay = exc.retry_after if exc.code == 'rate_limited' else 30
                until = self.now() + timedelta(seconds=max(1, delay)) if exc.code in {'rate_limited', 'timeout', 'network_error'} else None
                self.store.telegram_retry(self.source_key, exc.code, until)
                if exc.code == 'rate_limited' and until:
                    self.store.pause_auth(until, 'rate_limited')
                return {'state': 'source_error', 'code': exc.code, 'sent': sent}
            job = self.store.claim_notification(self.source_key, item, config['bot_id'], config['owner_id'], now=self.now)
            if job is None:
                continue
            try:
                message_id = await asyncio.to_thread(self.bot.send, job['text'])
            except BotFailure as exc:
                self.store.finish_notification(job['group_id'], failure=exc, now=self.now)
                return {'state': 'notification_error', 'code': exc.code, 'sent': sent,
                        'delivery_unknown': exc.ambiguous}
            except BaseException:
                self.store.finish_notification(job['group_id'], failure=BotFailure('protocol_error', ambiguous=True), now=self.now)
                raise
            self.store.finish_notification(job['group_id'], message_id=message_id, now=self.now)
            sent += 1
            if len(candidates) > 1:
                await self.sleep(2)
        config = self.store.notifier()
        return {'state': 'ok', 'sent': sent, 'retry_not_before': config['not_before']}
