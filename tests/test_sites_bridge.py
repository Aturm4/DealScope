"""Synthetic cross-origin companion authorization; never authenticates Telegram."""

from argparse import Namespace
from contextlib import redirect_stdout
from http.client import HTTPConnection
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from dealradar.cli import main
from dealradar.desktop import checked_site_origin, run_manual_monitor
from dealradar.storage import Store
from dealradar.web_app import LocalUI

SITE = "https://synthetic-radar.synthetic-owner.chatgpt.site"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "synthetic.sqlite3"
        self.server = LocalUI(self.path, 0, site_origin=SITE)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, method="GET", path="/api/bootstrap", body=None, **headers):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def auth(self, **extra):
        return {"Origin": SITE, "Sec-Fetch-Site": "cross-site", "X-DealRadar-UI": "1",
                "X-DealRadar-Link": self.server.link_token, **extra}

    def test_anonymous_and_wrong_capability_cannot_read_any_api(self):
        for path in ("/api/bootstrap", "/api/overview", "/api/feed", "/api/presets", "/api/notifications", "/api/monitor"):
            for token in ("", "invalid", self.server.link_token + "x"):
                with self.subTest(path=path, supplied=bool(token)):
                    status, headers, _ = self.request(path=path, **self.auth(**{"X-DealRadar-Link": token}))
                    self.assertEqual(status, 403)
                    self.assertNotIn("Access-Control-Allow-Credentials", headers)

    def test_exact_origin_and_capability_allow_reads_without_cookies(self):
        code, headers, body = self.request(**self.auth())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["csrf"], self.server.csrf)
        self.assertEqual(headers["Access-Control-Allow-Origin"], SITE)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.request(path="/api/overview", **self.auth())[0], 200)

    def test_other_origins_even_with_capability_cannot_read_or_write(self):
        for origin in ("null", SITE + ".evil.example", "https://evil.example", SITE.replace("https:", "http:"), SITE + "/"):
            with self.subTest(origin=origin):
                status, headers, _ = self.request(**self.auth(Origin=origin))
                self.assertEqual(status, 403)
                self.assertNotIn("Access-Control-Allow-Origin", headers)
                self.assertEqual(self.request("POST", "/api/sources/add", "{}", **self.auth(Origin=origin))[0], 403)

    def test_remote_writes_still_require_current_csrf_and_source_consent(self):
        headers = self.auth(**{"Content-Type": "application/json"})
        body = json.dumps({"address": "https://t.me/synthetic_channel", "already_joined": True, "start_from": "connection"})
        self.assertEqual(self.request("POST", "/api/sources/add", body, **headers)[0], 403)
        headers["X-CSRF-Token"] = self.server.csrf
        bad = json.dumps({"address": "@synthetic_channel", "already_joined": False, "start_from": "connection"})
        self.assertEqual(self.request("POST", "/api/sources/add", bad, **headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/sources/add", body, **headers)[0], 200)
        with Store(self.path) as store:
            self.assertEqual(store.telegram_usernames(), ("synthetic_channel",))
            self.assertIsNone(store.telegram_status("telegram:synthetic_channel")["channel_id"])

    def test_preflight_is_limited_to_one_origin_and_known_headers(self):
        headers = {"Origin": SITE, "Access-Control-Request-Method": "GET",
                   "Access-Control-Request-Headers": "x-dealradar-link,x-dealradar-ui"}
        code, response, body = self.request("OPTIONS", **headers)
        self.assertEqual((code, body), (204, b""))
        self.assertEqual(response["Access-Control-Allow-Origin"], SITE)
        self.assertNotIn("Access-Control-Allow-Credentials", response)
        for changes in ({"Origin": "https://evil.example"}, {"Access-Control-Request-Method": "DELETE"},
                        {"Access-Control-Request-Headers": "authorization"}, {"Host": "evil.example"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.request("OPTIONS", **{**headers, **changes})[0], 403)
        self.assertEqual(self.request("OPTIONS", "/connect", **headers)[0], 403)

    def test_local_pairing_code_requires_local_cookie_and_is_never_in_html(self):
        code, headers, html = self.request(path="/connect")
        self.assertEqual(code, 200)
        self.assertNotIn(self.server.link_token.encode(), html)
        self.assertEqual(self.request(path="/api/local-link", **{"X-DealRadar-UI": "1"})[0], 403)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        code, _, body = self.request(path="/api/local-link", **{"Cookie": cookie, "X-DealRadar-UI": "1"})
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data, {"token": self.server.link_token, "site_url": SITE})
        self.assertEqual(self.request(path="/api/local-link", **self.auth())[0], 403)
        for path in ("/", "/connect", "/connect.js", "/app.js"):
            self.assertEqual(self.request(path=path, **self.auth())[0], 403)

    def test_restarting_invalidates_capability_without_losing_settings(self):
        with Store(self.path) as store:
            store.add_telegram_source("@synthetic_channel")
        old = self.server.link_token
        with LocalUI(self.path, 0, site_origin=SITE) as second:
            self.assertNotEqual(old, second.link_token)
            self.assertNotEqual(self.server.csrf, second.csrf)
        with Store(self.path) as store:
            self.assertEqual(store.telegram_usernames(), ("synthetic_channel",))

    def test_normal_ui_does_not_enable_hosted_access(self):
        with LocalUI(self.path, 0) as server:
            self.assertIsNone(server.link_token)
            self.assertNotIn("/connect", server.assets)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True); thread.start()
            try:
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.request("GET", "/api/bootstrap", headers=self.auth())
                response = conn.getresponse(); self.assertEqual(response.status, 403); response.read(); conn.close()
            finally:
                server.shutdown(); thread.join(timeout=5)


class ManualLauncherTests(unittest.TestCase):
    def test_config_check_never_opens_db_browser_or_prompts(self):
        with TemporaryDirectory() as temp, patch("builtins.input", side_effect=AssertionError("input forbidden")), \
                patch("dealradar.desktop.site_settings", return_value={"url": SITE, "port": 8767}), \
                patch("webbrowser.open", side_effect=AssertionError("browser forbidden")), redirect_stdout(io.StringIO()) as out:
            path = Path(temp) / "must-not-exist.sqlite3"
            self.assertEqual(main(["--db", str(path), "desktop", "--check"]), 0)
            self.assertFalse(path.exists())
            data = json.loads(out.getvalue())
            self.assertFalse(data["network_used"]); self.assertFalse(data["login_performed"])

    def test_no_sources_does_not_pair_or_start_account_login(self):
        with TemporaryDirectory() as temp, patch("dealradar.bot_commands.command") as bot, \
                patch("dealradar.telegram_commands.command") as monitor:
            self.assertEqual(run_manual_monitor(Path(temp) / "synthetic.sqlite3", Namespace(monitor_state="waiting"), lambda *_: None), 0)
            bot.assert_not_called(); monitor.assert_not_called()

    def test_unconfirmed_logout_is_preserved_in_visible_status(self):
        with TemporaryDirectory() as temp, Store(Path(temp) / "synthetic.sqlite3") as store:
            store.add_telegram_source("@synthetic_channel")
            store.link_notifier(123456789, 123456789)
            server = Namespace(monitor_state="waiting")
            def fake(args, emit):
                self.assertTrue(args.all_sources and args.notify and args.watch)
                emit({"logout_confirmed": False})
                return 2
            with patch("dealradar.telegram_commands.command", side_effect=fake):
                self.assertEqual(run_manual_monitor(store.path, server, lambda *_: None), 2)
            self.assertEqual(server.monitor_state, "logout_unconfirmed")

    def test_untrusted_site_addresses_are_rejected(self):
        for value in (SITE + "/", SITE + "?token=x", SITE + "#x", SITE + ":444", "https://127.0.0.1", "file:///x", "https://evil.example", "https://a@b.c.chatgpt.site"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checked_site_origin(value)
        self.assertEqual(checked_site_origin(SITE), SITE)
