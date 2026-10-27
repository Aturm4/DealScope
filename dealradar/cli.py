"""Russian CLI for offline fixtures and an explicitly selected Telegram source."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

from .models import IncomingPost, SearchRule
from .storage import Store

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_BATCH_POSTS = 500


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повторяющиеся поля JSON не разрешены.")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("NaN и Infinity не разрешены в JSON.")


def read_json(path: Path) -> Any:
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Файл превышает лимит 2 МиБ.")
    try:
        text = raw.decode("utf-8-sig")
        depth = 0
        quoted = escaped = False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > 32:
                    raise ValueError("Вложенность JSON превышает лимит 32.")
            elif char in "]}":
                depth -= 1
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Ожидается корректный UTF-8 JSON без чрезмерной вложенности.") from exc


def _print(value: Any, *, error: bool = False) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    # JSON already escapes C0 controls. Also neutralize bidi/C1 terminal controls.
    rendered = re.sub(r"[\x7f-\x9f\u202a-\u202e\u2066-\u2069]", lambda match: f"\\u{ord(match[0]):04x}", rendered)
    print(rendered, file=sys.stderr if error else sys.stdout)


def import_posts(store: Store, source: str, data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list) or len(data) > MAX_BATCH_POSTS:
        raise ValueError("Ожидается список не более чем из 500 тестовых публикаций.")
    # Validate the entire file before the first mutation; each accepted post commits atomically.
    posts = [IncomingPost.from_dict(item) for item in data]
    return [asdict(store.process(source, post)) for post in posts]


def arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Личный радар: офлайн-данные и отдельное подключение согласованного канала.")
    parser.add_argument("--db", type=Path, default=Path("data/radar.sqlite3"), help="Локальная база (по умолчанию data/radar.sqlite3)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Создать или проверить структуру базы")
    commands.add_parser("demo", help="Загрузить явно синтетический пример; без Telegram и отправки")
    add_source = commands.add_parser("source-add", help="Добавить тестовый источник")
    add_source.add_argument("key", help="Идентификатор с префиксом test:")
    add_source.add_argument("name", help="Название")
    source_state = commands.add_parser("source-state", help="Включить или отключить сохранённый источник")
    source_state.add_argument("key")
    source_state.add_argument("state", choices=("on", "off"))
    commands.add_parser("sources", help="Сохранённые источники, ошибки и состояние проверок")
    rule = commands.add_parser("rule-load", help="Сохранить правило из локального JSON")
    rule.add_argument("path", type=Path)
    commands.add_parser("rules", help="Показать текущие правила")
    preset = commands.add_parser('preset-save', help='Сохранить или изменить именованный пресет')
    preset.add_argument('key')
    preset.add_argument('--name', required=True)
    preset.add_argument('--currency', choices=('USD', 'UZS'), required=True)
    preset.add_argument('--max-price', required=True)
    preset.add_argument('--capacity', type=int, action='append', required=True)
    preset.add_argument('--drive-type', choices=('ssd', 'hdd', 'any'), default='ssd')
    preset.add_argument('--interface', choices=('nvme', 'sata', 'any'), default='nvme')
    preset.add_argument('--brand', action='append', default=[])
    preset.add_argument('--model', action='append', default=[])
    preset.add_argument('--condition', choices=('new', 'used'), action='append', default=[])
    preset.add_argument('--keyword', action='append', default=[])
    preset.add_argument('--keyword-mode', choices=('any', 'all'), default='any')
    preset.add_argument('--exclude', action='append', default=[])
    preset.add_argument('--disabled', action='store_true')
    ingest = commands.add_parser("import-posts", help="Вручную обработать тестовый JSON, без получения данных из Telegram")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--source", required=True)
    listing = commands.add_parser("list", help="Последние версии товаров и причины решений")
    listing.add_argument("--decision", choices=("matched", "rejected", "needs_review"))
    listing.add_argument("--limit", type=int, default=50)
    history = commands.add_parser("history", help="Оригиналы сохранённых версий публикации")
    history.add_argument("--source", required=True)
    history.add_argument("--message-id", type=int, required=True)
    commands.add_parser("jobs", help="История очереди и результаты отправок; токенов здесь нет")
    commands.add_parser('bot-link', help='Скрытый ввод токена и явное локальное подтверждение своего ID')
    commands.add_parser('bot-status', help='Локальные настройки получателя, без сетевых запросов')
    commands.add_parser('bot-disable', help='Отключить будущие отправки и отменить ожидающие задания')
    resolve = commands.add_parser('notification-resolve', help='Вручную отметить неизвестный результат; автоматической повторной отправки нет')
    resolve.add_argument('--job-id', type=int, required=True)
    resolve.add_argument('--outcome', choices=('sent', 'skip'), required=True)
    commands.add_parser("recheck", help="Повторить фильтры без уведомлений по старой истории")
    commands.add_parser("status", help="Сводка базы и явно отключённой сети")
    commands.add_parser("check", help="Проверить целостность базы и внешние ключи")
    ui = commands.add_parser('ui', help='Русский локальный интерфейс на 127.0.0.1, без входа в Telegram')
    ui.add_argument('--port', type=int, default=8765, help='Локальный порт (по умолчанию 8765)')
    desktop = commands.add_parser('desktop', help='Приватный Sites-интерфейс и ручной запуск мониторинга на этом ПК')
    desktop.add_argument('--check', action='store_true', help='Проверить настройки без сети, базы и входа')
    commands.add_parser('telegram-preflight', help='Без сети проверить установленные версии и сессию только в памяти')
    commands.add_parser('telegram-check-input', help='Скрыто проверить только формат реквизитов, без сети и входа')
    from .telegram_source import APPROVED_USERNAME, public_username
    live = commands.add_parser('telegram-run', help='Чтение согласованных каналов; секреты вводятся локально')
    selection = live.add_mutually_exclusive_group()
    selection.add_argument('--channel', default=APPROVED_USERNAME, type=public_username,
                           help='Один добавленный канал; testirovk4 — отдельная учебная база')
    selection.add_argument('--all-sources', action='store_true', help='Все включённые каналы из интерфейса, до пяти в одной сессии')
    live.add_argument('--start-now', action='store_true', help='Первый запуск: зафиксировать конец истории, не импортируя её')
    live.add_argument('--watch', action='store_true', help='Продолжать проверки до Ctrl+C')
    live.add_argument('--interval', type=int, default=60, help='Интервал от 60 секунд')
    live.add_argument('--notify', action='store_true', help='Дополнительно отправлять новые совпадения отдельным привязанным ботом')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = arguments().parse_args(argv)
    if args.command == 'desktop':
        from .desktop import command
        return command(args, _print)
    if args.command == 'ui':
        from .web_app import command
        return command(args, _print)
    if args.command == 'bot-link':
        from .bot_commands import command
        return command(args, _print)
    if args.command in ('telegram-preflight', 'telegram-check-input', 'telegram-run'):
        from .telegram_commands import command
        return command(args, _print)
    try:
        # Read and validate local input before opening/creating a database.
        loaded = read_json(args.path) if args.command in ("rule-load", "import-posts") else None
        if args.command == "rule-load":
            loaded = SearchRule.from_dict(loaded)
        if args.command == 'preset-save':
            loaded = SearchRule(key=args.key, name=args.name, currency=args.currency, max_price=args.max_price,
                                capacities_gb=tuple(args.capacity),
                                drive_type=None if args.drive_type == 'any' else args.drive_type,
                                interface=None if args.interface == 'any' else args.interface,
                                brands=tuple(args.brand), models=tuple(args.model), conditions=tuple(args.condition),
                                keywords=tuple(args.keyword), keyword_mode=args.keyword_mode,
                                exclusions=tuple(args.exclude), enabled=not args.disabled)
        if args.command == "import-posts":
            if not isinstance(loaded, list) or len(loaded) > MAX_BATCH_POSTS:
                raise ValueError("Ожидается список не более 500 публикаций.")
            for item in loaded:
                IncomingPost.from_dict(item)
        with Store(args.db) as store:
            if args.command == "init":
                _print({"message": "Структура базы готова. Эта команда не подключает Telegram и не отправляет уведомлений.", **store.stats()})
            elif args.command == "demo":
                examples = Path(__file__).resolve().parent.parent / "examples"
                store.add_test_source("test:demo", "СИНТЕТИЧЕСКИЕ ДАННЫЕ — учебный источник")
                store.put_rule(SearchRule.from_dict(read_json(examples / "synthetic_rule.json")))
                results = import_posts(store, "test:demo", read_json(examples / "synthetic_posts.json"))
                _print({"notice": "Все объявления и ценовой потолок в демо синтетические. Это не ваши настройки и не реальные предложения.",
                        "results": results, **store.stats()})
            elif args.command == "source-add":
                store.add_test_source(args.key, args.name)
                _print({"message": "Тестовый источник сохранён. Telegram не подключён."})
            elif args.command == "source-state":
                store.set_source_enabled(args.key, args.state == "on")
                _print({"message": "Состояние источника сохранено."})
            elif args.command == "sources":
                _print(store.sources())
            elif args.command in ("rule-load", 'preset-save'):
                version = store.put_rule(loaded)
                _print({"message": "Правило сохранено. Для старых записей выполните recheck; новых уведомлений он не создаёт.", "version": version})
            elif args.command == "rules":
                _print(store.rules())
            elif args.command == "import-posts":
                _print({"mode": "manual_offline_import", "telegram_connected": False,
                        "results": import_posts(store, args.source, loaded)})
            elif args.command == "list":
                _print(store.offers(decision=args.decision, limit=args.limit))
            elif args.command == "history":
                _print(store.history(args.source, args.message_id))
            elif args.command == "jobs":
                _print({"sending_implemented": True, "jobs": store.jobs()})
            elif args.command == 'bot-status':
                _print({'network_used': False, 'notifier': store.notifier()})
            elif args.command == 'bot-disable':
                store.disable_notifier()
                _print({'message': 'Будущие отправки отключены. Уже начатый запрос может успеть доставиться. Токен не хранится в проекте.'})
            elif args.command == 'notification-resolve':
                store.resolve_notification(args.job_id, args.outcome)
                _print({'message': 'Результат отмечен по вашей ручной проверке. Повторной отправки нет.'})
            elif args.command == "recheck":
                _print(store.recheck())
            elif args.command == "status":
                _print(store.stats())
            elif args.command == "check":
                integrity = store.connection.execute("PRAGMA quick_check").fetchall()
                foreign = store.connection.execute("PRAGMA foreign_key_check").fetchall()
                ok = [row[0] for row in integrity] == ["ok"] and not foreign
                _print({"ok": ok, "foreign_key_errors": len(foreign), "schema_version": store.connection.execute("PRAGMA user_version").fetchone()[0]})
                return 0 if ok else 1
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        _print({"error": str(exc), "stage": 3}, error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
