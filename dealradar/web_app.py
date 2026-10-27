"""Loopback-only UI with fixed routes, same-origin checks and per-run browser secrets."""

from __future__ import annotations

from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit

from .storage import MaintenanceConflict, RuleConflict, Store
from .process_lock import MonitorBusy
from .web_store import UIStore

MAX_BODY = 64 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'")


class UIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def _equal(value: str | None, expected: str) -> bool:
    return bool(value and value.isascii() and len(value) == len(expected) and secrets.compare_digest(value, expected))


def _unique(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повторяющиеся поля JSON запрещены.")
        result[key] = value
    return result


def _constant(_: str):
    raise ValueError("Недопустимое число JSON.")


class LocalUI(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, database: Path, port: int = 8765, *, site_origin: str | None = None):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Порт должен быть числом от 1 до 65535.")
        self.database = Path(database).resolve()
        self.session = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.cookie_name = "radar_" + secrets.token_hex(8)
        if site_origin is not None:
            from .desktop import checked_site_origin
            site_origin = checked_site_origin(site_origin)
        self.site_origin = site_origin
        self.link_token = secrets.token_urlsafe(32) if site_origin else None
        self.monitor_state = "waiting"
        self.slots = threading.BoundedSemaphore(12)
        web = Path(__file__).with_name("web")
        self.assets = {"/": ("text/html; charset=utf-8", (web / "index.html").read_bytes()),
                       "/app.js": ("text/javascript; charset=utf-8", (web / "app.js").read_bytes()),
                       "/app.css": ("text/css; charset=utf-8", (web / "app.css").read_bytes()),
                       "/icon.svg": ("image/svg+xml", (web / "icon.svg").read_bytes())}
        if site_origin:
            self.assets.update({"/connect": ("text/html; charset=utf-8", (web / "connect.html").read_bytes()),
                                "/connect.js": ("text/javascript; charset=utf-8", (web / "connect.js").read_bytes())})
        super().__init__(("127.0.0.1", port), Handler)
        self.authority = f"127.0.0.1:{self.server_port}"
        self.origin = "http://" + self.authority
        try:
            with Store(self.database):
                pass
        except BaseException:
            self.server_close()
            raise

    def verify_request(self, request, client_address):
        return client_address[0] == "127.0.0.1"

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Never log paths, headers, request bodies, cookies or exception messages.
        pass


class Handler(BaseHTTPRequestHandler):
    server: LocalUI
    server_version = "DealRadar"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.request.settimeout(8)
        super().setup()

    def log_message(self, *_):
        pass

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"error": "http_error", "message": "HTTP-запрос отклонён."})

    def _one(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise UIError(400, "duplicate_header", "Повторяющийся заголовок запрещён.")
        return values[0] if values else None

    def _guard(self, *, api: bool = False, write: bool = False):
        if self._one("Host") != self.server.authority:
            raise UIError(403, "host_denied", "Открывайте интерфейс по адресу 127.0.0.1 из своей консоли.")
        origin = self._one("Origin")
        if self.server.site_origin and origin == self.server.site_origin:
            # Only the chosen hosted origin, explicit API routes and a per-run
            # capability may cross the loopback boundary. No cross-site cookies.
            if not api or self._one("X-DealRadar-UI") != "1" or not _equal(
                    self._one("X-DealRadar-Link"), self.server.link_token):
                raise UIError(403, "link_required", "Подключи этот ПК временным кодом из локального окна.")
            if write and not _equal(self._one("X-CSRF-Token"), self.server.csrf):
                raise UIError(403, "csrf_denied", "Подключи этот ПК заново.")
            return
        site = self._one("Sec-Fetch-Site")
        if site not in (None, "none", "same-origin"):
            raise UIError(403, "site_denied", "Запрос с другого сайта запрещён.")
        if (origin is not None and origin != self.server.origin) or (write and origin != self.server.origin):
            raise UIError(403, "origin_denied", "Запрос с другого сайта запрещён.")
        if api:
            raw = self._one("Cookie") or ""
            if len(raw) > 8192:
                raise UIError(403, "session_required", "Обновите страницу интерфейса.")
            cookie = SimpleCookie()
            try:
                cookie.load(raw)
            except CookieError:
                raise UIError(403, "session_required", "Обновите страницу интерфейса.") from None
            session = cookie.get(self.server.cookie_name)
            if not session or not _equal(session.value, self.server.session) or self._one("X-DealRadar-UI") != "1":
                raise UIError(403, "session_required", "Обновите страницу интерфейса.")
        if write and not _equal(self._one("X-CSRF-Token"), self.server.csrf):
            raise UIError(403, "csrf_denied", "Защита запроса: обновите страницу и повторите действие.")

    def _path(self) -> tuple[str, dict]:
        if len(self.path) > 2048 or not self.path.startswith("/") or self.path.startswith("//"):
            raise ValueError("Некорректный путь.")
        parts = urlsplit(self.path)
        if parts.scheme or parts.netloc or parts.fragment:
            raise ValueError("Некорректный путь.")
        query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=8, strict_parsing=True)
        if any(len(value) != 1 for value in query.values()):
            raise ValueError("Повторяющиеся параметры запрещены.")
        return parts.path, {key: value[0] for key, value in query.items()}

    def _respond(self, code: int, body: bytes, content_type: str, *, cookie: bool = False):
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self.server.site_origin and self.headers.get("Origin") == self.server.site_origin:
            self.send_header("Access-Control-Allow-Origin", self.server.site_origin)
            self.send_header("Vary", "Origin")
        if cookie:
            self.send_header("Set-Cookie", f"{self.server.cookie_name}={self.server.session}; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_RESPONSE:
            raise UIError(413, "response_limit", "Слишком много данных; сузьте выборку.")
        self._respond(code, body, "application/json; charset=utf-8")

    def _run(self, action):
        try:
            action()
        except UIError as exc:
            self._json(exc.status, {"error": exc.code, "message": exc.message})
        except RuleConflict as exc:
            self._json(409, {"error": "preset_conflict", "message": str(exc)})
        except (MonitorBusy, MaintenanceConflict) as exc:
            self._json(409, {"error": "maintenance_conflict", "message": str(exc)})
        except (ValueError, TypeError, KeyError, RecursionError):
            self._json(400, {"error": "invalid_input", "message": "Проверьте поля: название, цену, валюту, объёмы и формат списков."})
        except (sqlite3.Error, OSError):
            self._json(503, {"error": "storage_unavailable", "message": "База недоступна или занята. Повторите позже; проверьте её командой check."})
        except Exception:
            self._json(500, {"error": "internal_error", "message": "Не удалось выполнить действие. Повторите после обновления страницы."})

    def do_GET(self):
        self._run(self._get)

    def do_OPTIONS(self):
        self._run(self._options)

    def _options(self):
        path, query = self._path()
        if (self._one("Host") != self.server.authority or not self.server.site_origin
                or self._one("Origin") != self.server.site_origin or not path.startswith("/api/")
                or self._one("Access-Control-Request-Method") not in ("GET", "POST")):
            raise UIError(403, "origin_denied", "Подключение с этого сайта запрещено.")
        headers = {item.strip().lower() for item in (self._one("Access-Control-Request-Headers") or "").split(",")}
        if not headers <= {"content-type", "x-dealradar-ui", "x-dealradar-link", "x-csrf-token"}:
            raise UIError(403, "headers_denied", "Заголовки запроса запрещены.")
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.server.site_origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-DealRadar-UI, X-DealRadar-Link, X-CSRF-Token")
        self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _get(self):
        path, query = self._path()
        self._guard(api=path.startswith("/api/"))
        if path in self.server.assets:
            if query:
                raise ValueError("Параметры не поддерживаются.")
            content_type, body = self.server.assets[path]
            self._respond(200, body, content_type, cookie=path in ("/", "/connect"))
            return
        if path == "/api/bootstrap" and not query:
            self._json(200, {"csrf": self.server.csrf})
            return
        if path == "/api/local-link" and not query and self.server.site_origin:
            if self._one("Origin") not in (None, self.server.origin):
                raise UIError(403, "local_only", "Открой локальное окно подключения на этом ПК.")
            self._json(200, {"token": self.server.link_token, "site_url": self.server.site_origin})
            return
        if path == "/api/monitor" and not query:
            self._json(200, {"state": self.server.monitor_state})
            return
        with UIStore(self.server.database) as store:
            if path == "/api/overview" and not query:
                result = store.overview()
            elif path == "/api/feed" and not set(query) - {"decision", "source", "before", "preset"}:
                result = store.feed(decision=query.get("decision", "matched"), source=query.get("source", ""),
                                    before=int(query.get("before", "0")), preset=query.get("preset", ""))
            elif path == "/api/presets" and not query:
                result = store.presets()
            elif path == "/api/post" and set(query) == {"source", "message_id"}:
                result = store.post(query["source"], int(query["message_id"]))
            elif path == "/api/notifications" and not set(query) - {"status", "before"}:
                result = store.notification_page(status=query.get("status", "all"), before=int(query.get("before", "0")))
            else:
                raise UIError(404, "not_found", "Страница или действие не найдены.")
            self._json(200, result)

    def do_POST(self):
        self._run(self._post)

    def _post(self):
        self._guard(api=True, write=True)
        path, query = self._path()
        if query or self._one("Transfer-Encoding") or self._one("Expect"):
            raise ValueError("Этот формат запроса не поддерживается.")
        if (self._one("Content-Type") or "").lower() not in ("application/json", "application/json; charset=utf-8"):
            raise UIError(415, "json_required", "Ожидается JSON.")
        size = self._one("Content-Length")
        if not size or not size.isascii() or not size.isdecimal() or len(size) > 8:
            raise ValueError("Некорректная длина запроса.")
        size = int(size)
        if not 2 <= size <= MAX_BODY:
            raise UIError(413, "body_limit", "Запрос слишком большой или пустой.")
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError("Неполный запрос.")
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
        if type(body) is not dict:
            raise ValueError("Ожидается объект.")
        if path == "/api/shutdown" and not body:
            self._json(200, {"stopped": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        with UIStore(self.server.database) as store:
            if path == "/api/presets/save" and set(body) == {"config", "expected_version"}:
                result = {"version": store.save_preset(body["config"], body["expected_version"])}
            elif path == "/api/sources/state" and set(body) == {"key", "enabled"}:
                store.set_source_enabled(body["key"], body["enabled"])
                result = {"saved": True}
            elif path == "/api/sources/add" and set(body) == {"address", "already_joined", "start_from"}:
                if body['already_joined'] is not True or body['start_from'] != 'connection':
                    raise UIError(400, 'source_consent_required', 'Подтвердите подписку и начало с первого подключения.')
                try:
                    key = store.add_telegram_source(body['address'])
                except ValueError as exc:
                    raise UIError(400, 'invalid_source', str(exc)) from None
                result = {'saved': True, 'source': key, 'state': 'awaiting_connection'}
            elif path == "/api/recheck" and set(body) == {"after"}:
                result = store.recheck_slice(body["after"])
            elif path == "/api/notifier/disable" and not body:
                store.disable_notifier()
                result = {"saved": True}
            elif path == "/api/history/clear" and set(body) == {"confirmation"}:
                result = store.clear_history(body["confirmation"])
            elif path == "/api/notifier/forget" and set(body) == {"confirmation"}:
                result = store.forget_notifier(body["confirmation"])
            elif path == "/api/notifications/resolve" and set(body) == {"id", "outcome", "source", "message_id"}:
                if type(body['source']) is not str or type(body['message_id']) is not int:
                    raise ValueError('Некорректная ссылка на отправку.')
                store.resolve_notification(body["id"], body["outcome"],
                                           expected_source=body['source'], expected_message_id=body['message_id'])
                result = {"saved": True}
            else:
                raise UIError(404, "not_found", "Действие не найдено.")
            self._json(200, result)


def command(args, output) -> int:
    if not 1 <= args.port <= 65535:
        output({"state": "error", "code": "invalid_ui_port", "notice": "Порт: 1–65535."}, error=True)
        return 1
    try:
        with LocalUI(args.db, args.port) as server:
            output({"state": "ui_ready", "url": server.origin, "notice": "Локальный интерфейс. Сбор запускается отдельно; секреты здесь не запрашиваются. Остановка: кнопка «Остановить интерфейс» или Ctrl+C."})
            try:
                server.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                pass
        output({"state": "ui_stopped", "notice": "Интерфейс остановлен. Отдельный мониторинг этим не остановлен."})
        return 0
    except (OSError, ValueError, sqlite3.Error):
        output({"state": "error", "code": "ui_start_failed", "notice": "Проверьте базу и свободный порт; например --port 8766. Подробности реквизитов не выводятся."}, error=True)
        return 1
