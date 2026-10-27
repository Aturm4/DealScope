"""Local console entry points; never used by a bot or an HTTP endpoint."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import timedelta
import sys

from .models import read_timestamp, utc_now
from .bot_api import BotFailure
from .storage import Store
from .process_lock import monitor_lock
from .telegram_source import APPROVED_USERNAME, TEST_USERNAME, Monitor, SourceFailure, checked_username


async def wait_watch(store, source_key, seconds):
    """Keep the configured delay while noticing a source pause within five seconds."""
    remaining = seconds
    while remaining > 0:
        status = store.telegram_status(source_key)
        if not status or not status['enabled']:
            return False
        chunk = min(5, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk
    status = store.telegram_status(source_key)
    return bool(status and status['enabled'])


async def offline_preflight():
    from .telegram_client import Credentials, build_client, check_dependencies
    installed = check_dependencies()
    # Documented dummy credentials only; construction does not connect.
    client = build_client(Credentials(1, '0' * 32, '+10000000000'))
    if client.session.save() is not None or client.session._entities:
        raise ValueError('Проверка сессии в памяти не прошла.')
    return {'dependencies': installed, 'session_storage': 'memory_only',
            'network_used': False, 'login_performed': False}


async def run_monitor(args, emit):
    if getattr(args, 'all_sources', False):
        from .telegram_multi import run_sources
        return await run_sources(args, emit)
    from .telegram_client import LocalSetupError, TelethonReader, build_client, check_dependencies, close_client, login, prompt_credentials
    from .telegram_source import public_username
    username = public_username(getattr(args, 'channel', None) or APPROVED_USERNAME)
    source_key = 'telegram:' + username
    check_dependencies()
    if not sys.stdin.isatty() or not sys.stderr.isatty() or not sys.stdout.isatty():
        raise LocalSetupError('interactive_console_required')
    def sanitized_background_error(_loop, _context):
        try:
            emit({'state': 'background_error', 'code': 'transport_error',
                  'notice': 'Остановите радар и проверьте Устройства при неподтверждённом выходе.'})
        except Exception:
            pass
    asyncio.get_running_loop().set_exception_handler(sanitized_background_error)
    if not 60 <= args.interval <= 86400:
        raise ValueError('Интервал проверки от 60 до 86400 секунд.')
    with Store(args.db) as store, monitor_lock(args.db), ExitStack() as resources:
        store.check_telegram_scope(username)
        state = store.telegram_status(source_key)
        if state and not state['enabled']:
            emit({'state': 'disabled'})
            return 0
        if state and state['retry_not_before'] and read_timestamp(state['retry_not_before']) > utc_now():
            emit({'state': 'paused', 'retry_not_before': state['retry_not_before'], 'login_performed': False})
            return 2
        pause = store.auth_pause()
        if pause and read_timestamp(pause['retry_not_before']) > utc_now():
            emit({'state': 'paused', **pause, 'login_performed': False})
            return 2
        if (state is None or state['channel_id'] is None) and not args.start_now:
            raise ValueError('Для первого запуска явно укажите --start-now; история не импортируется.')
        emit({'source': source_key, 'test_channel': username == TEST_USERNAME,
              'notice': 'Неофициальный радар использует Telegram API. Сессия только в памяти; '
              'вводите секреты здесь, в своей консоли, а не в чате. При выходе выполняется log_out. '
              'При аварии отзовите Personal Deal Radar в Telegram → Настройки → Устройства. '
              'Ссылки и вложения не открываются. Официальная реклама при наличии показывается отдельно; '
              'Telegram получает отметку её показа.'})
        dispatcher = None
        if getattr(args, 'notify', False):
            from .bot_commands import prepare_notifier
            from .notifications import Dispatcher
            bot = await prepare_notifier(store, emit)
            resources.callback(bot.forget)
            dispatcher = Dispatcher(store, bot, source_key)
            if not any(rule['config']['enabled'] for rule in store.rules()):
                emit({'notice': 'Активных пресетов нет: объявления сохраняются, подходящие предложения пока не определяются.'})
        try:
            credentials = prompt_credentials()
            if username in (APPROVED_USERNAME, TEST_USERNAME):
                client = build_client(credentials, username=username)
            else:
                client = build_client(credentials, username=username, approved_usernames=store.telegram_usernames())
        except LocalSetupError as exc:
            emit({'state': 'error', 'code': exc.code, 'notice': exc.notice, 'login_attempted': False})
            return 1
        except Exception:
            failure = LocalSetupError('client_initialization_error')
            emit({'state': 'error', 'code': failure.code, 'notice': failure.notice, 'login_attempted': False})
            return 1
        try:
            store.pause_auth(utc_now() + timedelta(seconds=60), 'login_attempt')
            authenticated_user_id = await login(client, credentials)
            credentials = None
            if dispatcher and (type(authenticated_user_id) is not int or authenticated_user_id != bot.owner_id):
                raise BotFailure('identity_changed')
            reader = await TelethonReader.resolve(client, expected_id=state['channel_id'] if state else None)
            monitor = Monitor(store, reader, username=username)
            failures = 0
            while True:
                result = await monitor.poll(start_now=args.start_now)
                emit({'source': source_key, 'test_channel': username == TEST_USERNAME, **result})
                # A plain console renders the text/link without fetching ad media.
                # Sponsored records never enter the offers database or deal filters.
                if result['state'] in ('initialized', 'ok', 'catching_up'):
                    try:
                        for ad in await reader.sponsored():
                            random_id = ad.pop('random_id')
                            emit({'sponsored': ad})
                            await reader.sponsored_viewed(random_id)
                    except SourceFailure as exc:
                        # Ad retrieval is an API operation too: honor its flood wait.
                        delay = exc.retry_after if exc.code == 'rate_limited' else 30
                        until = utc_now() + timedelta(seconds=max(1, delay)) if exc.code in {'rate_limited', 'network_error', 'timeout'} else None
                        store.telegram_retry(source_key, exc.code, until)
                        if until and exc.code == 'rate_limited':
                            store.pause_auth(until, 'rate_limited')
                        emit({'state': 'error', 'code': exc.code, 'operation': 'sponsored'})
                        result = {'state': 'error', 'code': exc.code,
                                  'retry_not_before': until.isoformat() if until else None}
                if dispatcher and result['state'] == 'ok':
                    delivery = await dispatcher.tick(reader)
                    emit({'notifications': delivery})
                    if delivery['state'] == 'source_error':
                        result = {'state': 'error', 'code': delivery['code']}
                if result['state'] == 'error':
                    if (not args.watch or result['code'] not in {'network_error', 'timeout', 'rate_limited'}
                            or failures >= 2 or store.telegram_status(source_key) is None):
                        return 2
                    failures += 1
                    emit({'state': 'retry_scheduled', 'code': result['code'], 'retry': failures, 'retry_limit': 2,
                          'notice': 'Временный сбой. Следующая попытка в той же сессии после паузы; новый вход не выполняется.'})
                elif result['state'] in ('initialized', 'ok', 'catching_up'):
                    failures = 0
                if result['state'] == 'disabled':
                    return 0
                if not args.watch:
                    return 2 if result['state'] in ('error', 'paused') else 0
                if not await wait_watch(store, source_key, args.interval * (2 ** max(0, failures - 1))):
                    emit({'state': 'disabled', 'notice': 'Источник приостановлен; мониторинг завершает сессию.'})
                    return 0
        except SourceFailure as exc:
            until = utc_now() + timedelta(seconds=max(1, exc.retry_after)) if exc.code == 'rate_limited' else None
            if until:
                store.pause_auth(until, 'rate_limited')
            if store.telegram_status(source_key):
                store.telegram_retry(source_key, exc.code, until)
            raise
        finally:
            credentials = None
            confirmed = await close_client(client)
            emit({'logout_confirmed': confirmed,
                  'notice': 'Сессия радара отозвана.' if confirmed else
                  'Выход не подтверждён. Отзовите Personal Deal Radar с доверенного телефона: Настройки → Устройства.'})


def command(args, emit):
    from .telegram_client import LocalSetupError, prompt_credentials
    try:
        if args.command == 'telegram-check-input':
            if not sys.stdin.isatty() or not sys.stderr.isatty() or not sys.stdout.isatty():
                raise LocalSetupError('interactive_console_required')
            credentials = prompt_credentials()
            credentials = None
            emit({'state': 'input_format_valid', 'format_only': True, 'network_used': False,
                  'login_performed': False,
                  'notice': 'Проверен только формат ввода. Подлинность реквизитов и доступ к аккаунту не проверялись.'})
            return 0
        if args.command == 'telegram-preflight':
            emit(asyncio.run(offline_preflight()))
            return 0
        return asyncio.run(run_monitor(args, emit))
    except KeyboardInterrupt:
        emit({'state': 'stopped', 'notice': 'При отсутствии logout_confirmed=true проверьте Устройства с телефона.'})
        return 130
    except SourceFailure as exc:
        emit({'state': 'error', 'code': exc.code})
        return 2
    except BotFailure as exc:
        emit({'state': 'error', 'code': exc.code, 'operation': 'notification_bot',
              'notice': 'Токен бота не сохранён. Получатель и бот должны совпадать с локальной привязкой.'})
        return 2
    except LocalSetupError as exc:
        emit({'state': 'error', 'code': exc.code, 'notice': exc.notice})
        return 1
    except Exception:
        # Do not format third-party exceptions: they may embed requests/secrets.
        emit({'state': 'error', 'code': 'local_setup_or_storage_error',
              'notice': 'Проверьте версии, отдельную базу, --start-now и скрытый ввод по README. '
              'При начатом входе проверьте активные Устройства с телефона.'})
        return 1
