"""Interactive local pairing. Never auto-enroll the first stranger to /start."""

from __future__ import annotations

import asyncio
import hmac
import math
import secrets
import time

from .bot_api import BotAPI, BotFailure, checked_user_id, prompt_token, require_console
from .storage import Store
from .models import read_timestamp, utc_now
from .telegram_commands import monitor_lock


def check_bot_pause(config):
    if config and config['not_before']:
        remaining = (read_timestamp(config['not_before']) - utc_now()).total_seconds()
        if remaining > 0:
            raise BotFailure('rate_limited', retry_after=math.ceil(remaining))


def paired_user(update: dict, challenge: str) -> int | None:
    message = update.get('message')
    if not isinstance(message, dict):
        return None
    sender, chat = message.get('from'), message.get('chat')
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    text = message.get('text')
    if (not isinstance(text, str) or len(text) > 100 or not hmac.compare_digest(text.encode(), ('/start ' + challenge).encode())
            or sender.get('is_bot') is not False or chat.get('type') != 'private'
            or type(sender.get('id')) is not int or type(chat.get('id')) is not int
            or sender['id'] != chat['id'] or not 0 < sender['id'] < 2**52
            or any(key in message for key in ('forward_origin', 'forward_from', 'forward_sender_name', 'via_bot', 'sender_chat'))):
        return None
    return sender['id']


async def link_bot(args, emit):
    require_console()
    with Store(args.db) as store, monitor_lock(args.db):
        # A bot cannot turn offline imports into real notifications.
        if any(source['kind'] != 'telegram' for source in store.sources()):
            raise ValueError('Используйте отдельную базу реального или согласованного тестового Telegram-канала.')
        config = store.notifier()
        check_bot_pause(config)
        token = prompt_token()
        api = BotAPI(token)
        token = None
        try:
            identity = await asyncio.to_thread(api.identity)
            if config and identity['bot_id'] != config['bot_id']:
                raise BotFailure('identity_changed')
            await asyncio.to_thread(api.ensure_no_webhook)
            challenge = secrets.token_urlsafe(24)
            expires = time.monotonic() + 300
            emit({'state': 'pairing', 'bot_username': identity['username'],
                  'command_for_your_bot': '/start ' + challenge,
                  'notice': 'Откройте именно своего бота и отправьте эту команду ему в личный чат. '
                  'Одноразовую команду не публикуйте и не присылайте в переписку с разработчиком. '
                  'Срок действия — 5 минут. Затем подтвердите свой числовой ID здесь, в консоли.'})
            offset = 0
            while time.monotonic() < expires:
                updates = await asyncio.to_thread(api.updates, offset)
                candidates = {owner for update in updates if (owner := paired_user(update, challenge)) is not None}
                if updates:
                    offset = max(update['update_id'] for update in updates) + 1
                if len(candidates) > 1:
                    raise BotFailure('pairing_not_confirmed')
                if not candidates:
                    continue
                owner = checked_user_id(candidates.pop())
                if config and owner != config['owner_id']:
                    raise BotFailure('identity_changed')
                emit({'state': 'owner_confirmation_required', 'owner_id': owner,
                      'notice': 'Получатель ещё не сохранён. Подтвердите ID только если команду отправили вы со своего аккаунта.'})
                typed = input(f'Для привязки введите этот ID целиком ({owner}); Enter отменяет: ').strip()
                if time.monotonic() >= expires:
                    raise BotFailure('pairing_expired')
                if typed != str(owner):
                    raise BotFailure('pairing_not_confirmed')
                store.recover_notifications()
                store.link_notifier(api.bot_id, owner)
                emit({'state': 'linked', 'bot_id': api.bot_id, 'owner_id': owner,
                      'notice': 'Получатель подтверждён локально. Старая очередь не рассылается. '
                      'Токен не сохранён; для отправки запустите telegram-run --notify. '
                      'Управление из Telegram пока отсутствует.'})
                return 0
            raise BotFailure('pairing_expired')
        finally:
            api.forget()


async def prepare_notifier(store, emit):
    config = store.notifier()
    if not config or not config['enabled']:
        raise ValueError('Сначала выполните bot-link в этой же базе; выключенный бот не отправляет уведомления.')
    check_bot_pause(config)
    api = BotAPI(prompt_token(), owner_id=config['owner_id'])
    try:
        identity = await asyncio.to_thread(api.identity)
        if identity['bot_id'] != config['bot_id']:
            raise BotFailure('identity_changed')
        await asyncio.to_thread(api.ensure_no_webhook)
        recovered = store.recover_notifications()
        emit({'notifications': 'enabled', 'owner_id': config['owner_id'], 'unconfirmed_after_restart': recovered,
              'notice': 'Только личный чат подтверждённого получателя. Неизвестные результаты отправки не повторяются автоматически.'})
        return api
    except BaseException:
        api.forget()
        raise


def command(args, emit):
    try:
        return asyncio.run(link_bot(args, emit))
    except KeyboardInterrupt:
        emit({'state': 'stopped', 'notice': 'Привязка остановлена; пользовательский аккаунт не авторизовывался.'})
        return 130
    except BotFailure as exc:
        emit({'state': 'error', 'code': exc.code, 'retry_after': exc.retry_after,
              'notice': 'Токен не сохранён. При webhook_present используйте отдельного нового бота; чужой webhook не изменялся.'})
        return 2
    except Exception:
        emit({'state': 'error', 'code': 'local_setup_or_storage_error',
              'notice': 'Проверьте отдельную базу Telegram, права на папку и остановите второй монитор этой базы. '
              'Привязка получателя выполняется только в своей интерактивной консоли.'})
        return 1
