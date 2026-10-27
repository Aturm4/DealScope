"""Sequential polling of the explicitly saved sources in one temporary user session."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import timedelta
import sys

from .bot_api import BotFailure
from .models import read_timestamp, utc_now
from .notifications import Dispatcher
from .process_lock import monitor_lock
from .storage import Store
from .telegram_source import Monitor, SourceFailure, TEST_USERNAME


async def run_sources(args, emit):
    from .telegram_client import (LocalSetupError, TelethonReader, build_client, check_dependencies,
                                  close_client, login, prompt_credentials)
    from .bot_commands import prepare_notifier
    check_dependencies()
    if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        raise LocalSetupError('interactive_console_required')
    if not 60 <= args.interval <= 86400:
        raise ValueError('Интервал проверки от 60 до 86400 секунд.')
    def background_error(_loop, _context):
        try:
            emit({'state': 'background_error', 'code': 'transport_error'})
        except Exception:
            pass
    asyncio.get_running_loop().set_exception_handler(background_error)
    with Store(args.db) as store, monitor_lock(args.db), ExitStack() as resources:
        usernames = store.telegram_usernames(enabled_only=True)
        if not usernames:
            emit({'state': 'no_sources', 'notice': 'Добавьте и включите канал в локальном интерфейсе.', 'login_performed': False})
            return 0
        for username in usernames:
            store.check_telegram_scope(username)
        pause = store.auth_pause()
        if pause and read_timestamp(pause['retry_not_before']) > utc_now():
            emit({'state': 'paused', **pause, 'login_performed': False})
            return 2
        emit({'state': 'starting', 'sources': list(usernames),
              'notice': 'Сессия только в памяти. Секреты вводите в своей консоли. Читаются только добавленные каналы; '
                        'вступления нет. Для каждого нового источника начало — момент первого подключения. '
                        'При неподтверждённом выходе отзовите Personal Deal Radar в Устройствах Telegram.'})
        bot = None
        if args.notify:
            bot = await prepare_notifier(store, emit)
            resources.callback(bot.forget)
        if not any(rule['config']['enabled'] for rule in store.rules()):
            emit({'notice': 'Активных пресетов нет. Создайте поиск с ценовым потолком в интерфейсе.'})
        credentials = prompt_credentials()
        client = build_client(credentials, username=usernames[0], approved_usernames=usernames)
        failures, stopped = {}, set()
        try:
            store.pause_auth(utc_now() + timedelta(seconds=60), 'login_attempt')
            owner = await login(client, credentials)
            credentials = None
            if bot and (type(owner) is not int or owner != bot.owner_id):
                raise BotFailure('identity_changed')
            while True:
                usernames = store.telegram_usernames(enabled_only=True)
                if not usernames:
                    emit({'state': 'disabled', 'notice': 'Все источники приостановлены; сессия завершается.'})
                    return 0
                pause = store.auth_pause()
                # The short login-attempt throttle is for new logins, not polling in this session.
                account_paused = bool(pause and pause['last_error_code'] == 'rate_limited'
                                      and read_timestamp(pause['retry_not_before']) > utc_now())
                client.configure_sources(usernames)
                for username in usernames:
                    if account_paused:
                        break
                    key = 'telegram:' + username
                    if username in stopped:
                        continue
                    state = store.telegram_status(key)
                    if not state or not state['enabled']:
                        continue
                    if state['retry_not_before'] and read_timestamp(state['retry_not_before']) > utc_now():
                        emit({'source': key, 'state': 'paused', 'retry_not_before': state['retry_not_before']})
                        continue
                    try:
                        client.select_source(username)
                        reader = await TelethonReader.resolve(client, expected_id=state['channel_id'])
                        # Resolution may take time; recheck consent before reading history.
                        if not store.telegram_status(key)['enabled']:
                            continue
                        result = await Monitor(store, reader, username=username).poll(start_now=True)
                        emit({'source': key, 'test_channel': username == TEST_USERNAME, **result})
                        if result['state'] in ('initialized', 'ok', 'catching_up'):
                            for ad in await reader.sponsored():
                                random_id = ad.pop('random_id')
                                emit({'source': key, 'sponsored': ad})
                                await reader.sponsored_viewed(random_id)
                        if bot and result['state'] == 'ok':
                            delivery = await Dispatcher(store, bot, key).tick(reader)
                            emit({'source': key, 'notifications': delivery})
                            if delivery['state'] == 'source_error':
                                raise SourceFailure(delivery['code'])
                        if result['state'] == 'error':
                            raise SourceFailure(result['code'])
                        failures[username] = 0
                    except SourceFailure as exc:
                        if exc.code == 'auth_required':
                            raise  # Account-wide authorization loss cannot be isolated to a channel.
                        delay = exc.retry_after if exc.code == 'rate_limited' else 30
                        until = utc_now() + timedelta(seconds=max(1, delay)) if exc.code in {'rate_limited', 'network_error', 'timeout'} else None
                        # Monitor/Dispatcher may already have saved a longer FloodWait.
                        saved = store.telegram_status(key)
                        if saved and saved['retry_not_before']:
                            point = read_timestamp(saved['retry_not_before'])
                            if until is None or point > until:
                                until = point
                        store.telegram_retry(key, exc.code, until)
                        if exc.code == 'rate_limited':
                            store.pause_auth(until, 'rate_limited')
                            account_paused = True
                        failures[username] = failures.get(username, 0) + 1
                        if exc.code not in {'network_error', 'timeout', 'rate_limited'} or failures[username] >= 3:
                            stopped.add(username)
                        emit({'source': key, 'state': 'error', 'code': exc.code,
                              'source_stopped': username in stopped, 'retry_not_before': until.isoformat() if until else None})
                    except (ValueError, TypeError):
                        store.telegram_retry(key, 'identity_changed', None)
                        stopped.add(username)
                        emit({'source': key, 'state': 'error', 'code': 'identity_changed', 'source_stopped': True})
                if not args.watch:
                    return 2 if stopped or any(failures.values()) or account_paused else 0
                if all(name in stopped for name in store.telegram_usernames(enabled_only=True)):
                    emit({'state': 'sources_stopped', 'notice': 'Источники остановлены после ошибок. Исправьте причину и перезапустите.'})
                    return 2
                remaining = args.interval
                while remaining > 0:
                    if not store.telegram_usernames(enabled_only=True):
                        break
                    chunk = min(5, remaining)
                    await asyncio.sleep(chunk)
                    remaining -= chunk
        except SourceFailure as exc:
            # Login can be rate-limited before any source has been resolved.
            # Persist that full delay too, so a restart cannot shorten it to 60s.
            if exc.code == 'rate_limited':
                store.pause_auth(utc_now() + timedelta(seconds=max(1, exc.retry_after)), 'rate_limited')
            raise
        finally:
            credentials = None
            confirmed = await close_client(client)
            emit({'logout_confirmed': confirmed, 'notice': 'Сессия отозвана.' if confirmed else
                  'Выход не подтверждён. Отзовите Personal Deal Radar: Telegram → Настройки → Устройства.'})
