"""Manual Windows launcher and an explicitly paired, loopback-only Sites bridge."""

from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path
import re
import threading
import webbrowser


def checked_site_origin(value: str) -> str:
    if (type(value) is not str or len(value) > 250
            or not re.fullmatch(r"https://[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*\.chatgpt\.site", value)):
        raise ValueError("Нужен точный HTTPS-адрес созданного приватного Sites без пути и параметров.")
    return value


def site_settings() -> dict:
    path = Path(__file__).resolve().parent.parent / "site" / "site.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    if type(settings) is not dict or set(settings) != {"url", "port"}:
        raise ValueError("Проверьте настройки сайта.")
    checked_site_origin(settings["url"])
    if type(settings["port"]) is not int or not 1024 <= settings["port"] <= 65535:
        raise ValueError("Неверный порт подключения.")
    return settings


def run_manual_monitor(database, server, emit) -> int:
    from . import bot_commands, telegram_commands
    from .storage import Store
    with Store(database) as store:
        config = store.notifier()
        if not store.telegram_usernames(enabled_only=True):
            emit({"state": "no_sources", "notice": "Добавь и включи канал на сайте. Вход в Telegram не выполнялся."})
            return 0
    if not config or not config["enabled"]:
        emit({"notice": "Сначала привяжем твоего бота. Токен вводится только в этом окне."})
        if result := bot_commands.command(Namespace(db=database, command="bot-link"), emit):
            return result

    def status(data, **kwargs):
        if data.get("source") and data.get("state") in {"ok", "initialized", "catching_up"}:
            server.monitor_state = "running"
        if "logout_confirmed" in data:
            server.monitor_state = "stopped" if data["logout_confirmed"] else "logout_unconfirmed"
        emit(data, **kwargs)

    server.monitor_state = "login"
    try:
        return telegram_commands.command(Namespace(db=database, command="telegram-run", all_sources=True,
            channel=None, start_now=True, watch=True, interval=60, notify=True), status)
    finally:
        if server.monitor_state != "logout_unconfirmed":
            server.monitor_state = "stopped"


def command(args, emit) -> int:
    from .bot_api import require_console
    from .telegram_client import LocalSetupError, check_dependencies
    from .web_app import LocalUI
    try:
        settings = site_settings()
        check_dependencies()
        if args.check:
            emit({"state": "launcher_ready", "site": settings["url"], "port": settings["port"],
                  "network_used": False, "login_performed": False, "database_opened": False})
            return 0
        require_console()
        with LocalUI(args.db, settings["port"], site_origin=settings["url"]) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
            thread.start()
            try:
                local_url = server.origin + "/connect"
                webbrowser.open(local_url, new=2)
                emit({"state": "pc_ready", "connect": local_url, "site": settings["url"],
                      "notice": "В локальном окне скопируй временный код, открой сайт и подключи ПК. "
                                "Добавь источник и сохрани поиск. Реквизиты Telegram вводятся только здесь. "
                                "Не закрывай это окно во время мониторинга."})
                while thread.is_alive():
                    try:
                        answer = input("Enter — ручной вход и мониторинг; Q — закрыть программу: ").strip().lower()
                    except KeyboardInterrupt:
                        break
                    if answer == "q":
                        break
                    if answer:
                        continue
                    if server.monitor_state == "logout_unconfirmed":
                        confirmation = input("Сначала отзови Personal Deal Radar в Устройствах Telegram. После этого введи ОТОЗВАНО: ").strip()
                        if confirmation != "ОТОЗВАНО":
                            continue
                    result = run_manual_monitor(args.db, server, emit)
                    emit({"state": "monitor_stopped", "exit_code": result,
                          "notice": "Мониторинг завершён. Сайт подключён, пока открыто это окно. "
                                    "При logout_confirmed=false отзови Personal Deal Radar в Устройствах Telegram."})
            finally:
                if thread.is_alive():
                    server.shutdown()
                thread.join(timeout=10)
                server.link_token = None
        emit({"state": "pc_disconnected", "notice": "Локальная программа закрыта; временный код больше не действует."})
        return 0
    except KeyboardInterrupt:
        emit({"state": "stopped", "notice": "Проверь подтверждение выхода; при его отсутствии отзови сессию в Telegram."})
        return 130
    except LocalSetupError as exc:
        emit({"state": "error", "code": exc.code, "notice": exc.notice})
        return 1
    except Exception:
        emit({"state": "error", "code": "launcher_setup_error",
              "notice": "Проверь настройки site/site.json, свободный порт 8767 и локальную базу. "
                        "Закрой прежний запуск этого ярлыка. Реквизиты не сохранялись."})
        return 1
