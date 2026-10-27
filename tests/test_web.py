"""Synthetic loopback UI acceptance and hostile browser-request boundaries."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from dealradar.bot_api import BotFailure
from dealradar.cli import main
from dealradar.models import IncomingPost, SearchRule
from dealradar.storage import RuleConflict, Store
from dealradar.web_app import LocalUI, MAX_BODY
from dealradar.web_store import PAGE_SIZE, UIStore, original_link, plain_card

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def rule(**changes):
    return SearchRule.from_dict({"key": "synthetic-ui", "name": "СИНТЕТИЧЕСКИЙ пресет UI", "currency": "USD",
                                 "max_price": "50", **changes})


def seed(store):
    store.add_test_source("test:ui", "СИНТЕТИЧЕСКИЕ предложения")
    store.put_rule(rule())
    for index, text in enumerate(("SSD NVMe 512GB 40$", "SSD NVMe 1TB 75$", "SSD M.2 512GB 30$", "SSD NVMe 1TB 45$ ПРОДАНО ❌"), 1):
        store.process("test:ui", IncomingPost(index, "СИНТЕТИЧЕСКИЙ ТЕСТ\n" + text, NOW))


class UIDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "synthetic.sqlite3"
        self.store = UIStore(self.path)
        seed(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_current_decisions_unknowns_and_sold(self):
        data = self.store.overview()
        self.assertTrue(data["synthetic"])
        self.assertEqual(data["counts"], {"matched": 1, "needs_review": 1, "rejected": 2, "unassessed": 0})
        self.assertIsNone(data["notifier"])
        item = self.store.feed()["items"][0]
        self.assertEqual(item["message_id"], 1)
        self.assertIsNone(item["offer"]["model"])
        self.assertNotIn("search_text", item["offer"])
        self.assertIsNone(item["original_url"])
        review = self.store.feed(decision="needs_review")["items"][0]
        self.assertIsNone(review["offer"]["interface"])
        self.assertIn("unknown_interface", review["evaluations"][0]["reasons"])

    def test_new_rule_version_and_chunked_recheck_do_not_create_sends(self):
        jobs_before = len(self.store.jobs())
        self.assertEqual(self.store.save_preset(rule(max_price="80").to_dict(), 1), 2)
        self.assertEqual(self.store.overview()["counts"]["unassessed"], 4)
        result = self.store.recheck_slice(0)
        self.assertEqual(result["posts"], 4)
        self.assertIsNone(result["next_after"])
        self.assertEqual(self.store.overview()["counts"]["matched"], 2)
        self.assertEqual(len(self.store.jobs()), jobs_before)
        self.assertTrue(all(job["status"] in ("held", "cancelled") for job in self.store.jobs()))

    def test_selected_preset_has_its_own_decision_and_reasons(self):
        self.store.put_rule(rule(key="synthetic-terabyte", name="СИНТЕТИЧЕСКИЙ 1 ТБ", max_price="80", capacities_gb=[1000]))
        self.store.recheck_slice(0)
        first = self.store.feed(preset="synthetic-ui")["items"]
        second = self.store.feed(preset="synthetic-terabyte")["items"]
        self.assertEqual([item["message_id"] for item in first], [1])
        self.assertEqual([item["message_id"] for item in second], [2])
        self.assertEqual([entry["name"] for entry in second[0]["evaluations"]], ["СИНТЕТИЧЕСКИЙ 1 ТБ"])
        rejected = self.store.feed(preset="synthetic-ui", decision="rejected")["items"]
        expensive = next(item for item in rejected if item["message_id"] == 2)
        self.assertEqual(expensive["decision"], "rejected")
        self.assertIn("price_above_limit", expensive["evaluations"][0]["reasons"])
        self.assertEqual({item["message_id"] for item in self.store.feed()["items"]}, {1, 2})

    def test_selected_preset_never_uses_an_old_or_other_rule_evaluation(self):
        self.store.put_rule(rule(key="synthetic-other", max_price="80"))
        self.store.recheck_slice(0)
        self.store.save_preset(rule(max_price="10").to_dict(), 1)
        self.assertFalse(self.store.feed(preset="synthetic-ui")["items"])
        stale = self.store.feed(preset="synthetic-ui", decision="unassessed")["items"]
        self.assertEqual(len(stale), 4)
        self.assertTrue(all(not item["evaluations"] for item in stale))
        self.assertEqual(len(self.store.feed()["items"]), 2)
        self.store.recheck_slice(0)
        self.assertEqual(len(self.store.feed(preset="synthetic-ui", decision="rejected")["items"]), 4)

    def test_rejected_feed_contains_only_the_selected_product(self):
        self.store.save_preset(rule(capacities_gb=[512]).to_dict(), 1)
        self.store.recheck_slice(0)
        texts = {10: "SSD NVMe 512GB 65$", 11: "SSD NVMe 512GB 25$ ПРОДАНО ❌",
                 12: "SSD NVMe 1TB 70$", 13: "SSD SATA 512GB 30$",
                 14: "HDD 512GB 15$ ПРОДАНО", 15: "Монитор 24 дюйма 50$ ПРОДАНО"}
        for message_id, text in texts.items():
            self.store.process("test:ui", IncomingPost(message_id, "СИНТЕТИЧЕСКИЙ ТЕСТ\n" + text, NOW))
        for preset in ("synthetic-ui", ""):
            result = self.store.feed(preset=preset, decision="rejected")
            self.assertEqual({item["message_id"] for item in result["items"]}, {10, 11})
            self.assertEqual(result["rejected_scope_version"], 1)
        saved = self.store.feed(preset="synthetic-ui", decision="all")["items"]
        self.assertTrue({12, 13, 14} <= {item["message_id"] for item in saved})

    def test_rejected_scope_applies_before_pagination(self):
        self.store.save_preset(rule(capacities_gb=[512]).to_dict(), 1)
        self.store.recheck_slice(0)
        for message_id in range(10, 10 + PAGE_SIZE + 3):
            self.store.process("test:ui", IncomingPost(message_id, "SSD NVMe 512GB 65$", NOW))
        for message_id in range(100, 100 + PAGE_SIZE + 3):
            self.store.process("test:ui", IncomingPost(message_id, "SSD NVMe 1TB 90$ ПРОДАНО", NOW))
        first = self.store.feed(preset="synthetic-ui", decision="rejected", source="test:ui")
        second = self.store.feed(preset="synthetic-ui", decision="rejected", source="test:ui", before=first["next_before"])
        self.assertEqual(len(first["items"]), PAGE_SIZE)
        self.assertEqual(len(second["items"]), 3)
        self.assertIsNone(second["next_before"])
        self.assertFalse({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
        self.assertTrue(all(item["offer"]["capacity_gb"] == 512 for item in first["items"] + second["items"]))

    def test_price_edit_recheck_moves_relevant_offer_but_never_resends(self):
        self.store.save_preset(rule(capacities_gb=[512]).to_dict(), 1)
        self.store.process("test:ui", IncomingPost(10, "SSD NVMe 512GB 65$", NOW))
        self.assertEqual([item["message_id"] for item in self.store.feed(preset="synthetic-ui", decision="rejected")["items"]], [10])
        jobs_before = len(self.store.jobs())
        self.store.save_preset(rule(capacities_gb=[512], max_price="70").to_dict(), 2)
        self.store.recheck_slice(0)
        self.assertFalse(self.store.feed(preset="synthetic-ui", decision="rejected")["items"])
        self.assertIn(10, {item["message_id"] for item in self.store.feed(preset="synthetic-ui")["items"]})
        self.assertEqual(len(self.store.jobs()), jobs_before)

    def test_rejected_scope_handles_unknowns_brands_and_keywords(self):
        self.store.save_preset(rule(capacities_gb=[512], brands=["Samsung"], keywords=["EVO"]).to_dict(), 1)
        for message_id, text in ((10, "SSD NVMe Samsung 970 EVO Plus 512GB 65$"),
                                 (11, "SSD NVMe Kingston NV2 512GB 65$"),
                                 (12, "SSD M.2 Samsung 970 EVO Plus 512GB 65$"),
                                 (13, "SSD NVMe Samsung 980 512GB 65$")):
            self.store.process("test:ui", IncomingPost(message_id, text, NOW))
        items = self.store.feed(preset="synthetic-ui", decision="rejected")["items"]
        self.assertEqual({item["message_id"] for item in items}, {10, 12})
        self.assertIsNone(next(item for item in items if item["message_id"] == 12)["offer"]["interface"])

    def test_all_rejected_uses_only_relevant_current_enabled_rules(self):
        self.store.save_preset(rule(capacities_gb=[512]).to_dict(), 1)
        self.store.put_rule(rule(key="synthetic-terabyte", capacities_gb=[1000]))
        self.store.recheck_slice(0)
        items = self.store.feed(decision="rejected")["items"]
        self.assertEqual({item["message_id"] for item in items}, {2, 4})
        self.assertTrue(all([entry["name"] for entry in item["evaluations"]] == ["СИНТЕТИЧЕСКИЙ пресет UI"] for item in items))
        self.store.save_preset(rule(key="synthetic-terabyte", capacities_gb=[1000], enabled=False).to_dict(), 1)
        self.assertFalse(self.store.feed(decision="rejected")["items"])

    def test_disabled_selected_preset_is_unassessed_and_survives_restart(self):
        self.store.save_preset(rule(enabled=False).to_dict(), 1)
        with UIStore(self.path) as restarted:
            self.assertFalse(restarted.feed(preset="synthetic-ui")["items"])
            items = restarted.feed(preset="synthetic-ui", decision="all")["items"]
            self.assertEqual(len(items), 4)
            self.assertTrue(all(item["decision"] == "unassessed" and not item["evaluations"] for item in items))

    def test_selected_preset_combines_source_and_cursor_without_duplicates(self):
        for index in range(10, 10 + PAGE_SIZE + 3):
            self.store.process("test:ui", IncomingPost(index, "SSD NVMe 512GB 40$", NOW))
        first = self.store.feed(preset="synthetic-ui", source="test:ui")
        second = self.store.feed(preset="synthetic-ui", source="test:ui", before=first["next_before"])
        self.assertEqual(len(first["items"]), PAGE_SIZE)
        self.assertEqual(len(first["items"]) + len(second["items"]), PAGE_SIZE + 4)
        self.assertFalse({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
        self.assertFalse(self.store.feed(preset="synthetic-ui", source="test:absent")["items"])

    def test_unknown_or_malformed_preset_filter_is_refused(self):
        for key in ("synthetic-missing", "' OR 1=1 --", "a" * 81):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.feed(preset=key)
        self.assertEqual(len(self.store.rules()), 1)

    def test_optimistic_edit_conflict_preserves_newer_preset(self):
        self.store.save_preset(rule(name="Новая версия").to_dict(), 1)
        with self.assertRaises(RuleConflict):
            self.store.save_preset(rule(name="Старая вкладка").to_dict(), 1)
        self.assertEqual(self.store.presets()["items"][0]["config"]["name"], "Новая версия")

    def test_empty_price_refused_without_using_synthetic_default(self):
        config = rule().to_dict() | {"key": "", "name": "Мой новый пресет", "max_price": ""}
        with self.assertRaises(ValueError):
            self.store.save_preset(config, 0)
        self.assertEqual(len(self.store.rules()), 1)
        config["max_price"] = "345678.90"
        config["currency"] = "UZS"
        self.store.save_preset(config, 0)
        created = self.store.presets()["items"][0]["config"]
        self.assertTrue(created["key"].startswith("preset:"))
        self.assertEqual(created["max_price"], "345678.90")
        self.assertEqual(created["currency"], "UZS")

    def test_presets_and_disabled_source_survive_restart(self):
        self.store.save_preset(rule(max_price="47").to_dict(), 1)
        self.store.set_source_enabled("test:ui", False)
        with UIStore(self.path) as restarted:
            self.assertEqual(restarted.rules()[0]["config"]["max_price"], "47")
            self.assertEqual(restarted.overview()["sources"][0]["enabled"], 0)
            self.assertEqual(restarted.connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertFalse(restarted.connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_cursor_pagination_is_bounded_and_has_no_duplicates(self):
        for index in range(10, 10 + PAGE_SIZE + 3):
            self.store.process("test:ui", IncomingPost(index, "SSD NVMe 512GB 40$", NOW))
        first = self.store.feed()
        self.assertEqual(len(first["items"]), PAGE_SIZE)
        second = self.store.feed(before=first["next_before"])
        self.assertTrue(second["items"])
        self.assertFalse({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
        self.assertIsNone(second["next_before"])

    def test_recheck_pages_and_updated_post_versions(self):
        for index in range(10, 67):
            self.store.process("test:ui", IncomingPost(index, "SSD NVMe 512GB 40$", NOW))
        first = self.store.recheck_slice(0)
        self.assertEqual(first["posts"], 50)
        self.assertIsNotNone(first["next_after"])
        second = self.store.recheck_slice(first["next_after"])
        self.assertEqual(second["posts"], 11)
        self.assertIsNone(second["next_after"])
        self.store.process("test:ui", IncomingPost(1, "SSD NVMe 512GB 40$ ПРОДАНО ❌", NOW, edited_at=NOW + timedelta(hours=1)))
        self.assertEqual(len(self.store.post("test:ui", 1)["versions"]), 2)
        self.assertFalse(any(item["message_id"] == 1 for item in self.store.feed()["items"]))

    def test_links_are_generated_only_for_approved_channels(self):
        self.assertEqual(original_link("telegram:testirovk4", 7), "https://t.me/testirovk4/7")
        for source in ("test:ui", "telegram:unapproved", "telegram:testirovk4@evil.invalid", "https://evil.invalid"):
            self.assertIsNone(original_link(source, 7))

    def test_saved_ad_url_and_markup_never_become_browser_links(self):
        self.store.process("test:ui", IncomingPost(8, 'SSD NVMe 512GB 40$ <img src="http://127.0.0.1:9" onerror="alert(1)">', NOW,
                                                 original_url="https://evil.invalid/download.exe"))
        item = self.store.feed()["items"][0]
        self.assertIsNone(item["original_url"])
        self.assertIn("<img", self.store.post("test:ui", 8)["versions"][0]["raw_text"])
        self.assertEqual(plain_card('<b>ТЕСТ</b> &lt;script&gt;<a href="http://127.0.0.1/">ссылка</a>'), "ТЕСТ <script>ссылка")

    def test_unknown_group_and_manual_outcome_are_not_api_confirmation(self):
        # Separate synthetic Telegram database. No client, session or Telegram connection.
        with UIStore(Path(self.temp.name) / "group.sqlite3") as store:
            key = store.bind_telegram("testirovk4", 12345, 0)
            store.put_rule(rule())
            store.put_rule(rule(key="synthetic-second", name="Второй синтетический пресет"))
            store.link_notifier(123, 456)
            store.process(key, IncomingPost(1, "СИНТЕТИЧЕСКИЙ ТЕСТ SSD NVMe 512GB 40$", NOW), origin="telegram")
            claimed = store.claim_notification(key, 1, 123, 456)
            store.finish_notification(claimed["group_id"], failure=BotFailure("network_error", ambiguous=True))
            items = store.notification_page(status="unknown")["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["preset_count"], 2)
            self.assertNotIn("<b>", items[0]["delivery_text"])
            store.resolve_notification(items[0]["id"], "sent")
            item = store.notification_page()["items"][0]
            self.assertEqual(item["reason"], "owner_confirmed")
            self.assertEqual(item["status"], "sent")


class UIHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "synthetic.sqlite3"
        with Store(self.path) as store:
            seed(store)
        self.server = LocalUI(self.path, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        code, headers, _ = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        code, _, body = self.request("GET", "/api/bootstrap", authenticated=True)
        self.csrf = json.loads(body)["csrf"]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None, authenticated=False):
        supplied = {}
        if authenticated:
            supplied = {"Cookie": self.cookie, "X-DealRadar-UI": "1"}
            if method == "POST":
                supplied |= {"Origin": self.server.origin, "X-CSRF-Token": self.csrf, "Content-Type": "application/json"}
        supplied.update(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body, supplied)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_only_loopback_is_bound_and_security_headers_are_present(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        _, headers, _ = self.request("GET", "/")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_missing_cookie_or_custom_header_cannot_read_saved_history(self):
        for headers in ({}, {"Cookie": self.cookie}, {"X-DealRadar-UI": "1"}, {"Cookie": "radar=wrong", "X-DealRadar-UI": "1"}):
            self.assertEqual(self.request("GET", "/api/overview", headers=headers)[0], 403)
        code, _, body = self.request("GET", "/api/overview", authenticated=True)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["synthetic"])
        self.assertNotIn("owner_id", body.decode())

    def test_wrong_host_origin_and_fetch_site_are_denied_for_reads(self):
        for headers in ({"Host": "evil.invalid"}, {"Host": "localhost:" + str(self.server.server_port)},
                        {"Origin": "https://evil.invalid"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("GET", "/api/feed", headers=headers, authenticated=True)[0], 403)
                self.assertEqual(self.request("GET", "/", headers=headers)[0], 403)

    def test_forged_write_cannot_change_source_or_preset(self):
        for headers in ({"X-CSRF-Token": "wrong"}, {"X-CSRF-Token": ""}, {"Origin": ""},
                        {"Origin": "http://127.0.0.1:1"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request("POST", "/api/sources/state", {"key": "test:ui", "enabled": False}, headers, True)[0], 403)
        with Store(self.path) as store:
            self.assertEqual(store.sources()[0]["enabled"], 1)

    def test_valid_save_disable_restart_and_recheck_flow(self):
        config = rule(max_price="70").to_dict()
        self.assertEqual(self.request("POST", "/api/presets/save", {"config": config, "expected_version": 1}, authenticated=True)[0], 200)
        self.assertEqual(self.request("POST", "/api/presets/save", {"config": config, "expected_version": 1}, authenticated=True)[0], 409)
        self.assertEqual(self.request("POST", "/api/recheck", {"after": 0}, authenticated=True)[0], 200)
        self.assertEqual(self.request("POST", "/api/sources/state", {"key": "test:ui", "enabled": False}, authenticated=True)[0], 200)
        with UIStore(self.path) as restarted:
            self.assertEqual(restarted.rules()[0]["version"], 2)
            self.assertEqual(restarted.sources()[0]["enabled"], 0)
            self.assertFalse(any(job["status"] in ("pending", "in_flight") for job in restarted.jobs()))

    def test_malformed_oversized_and_ambiguous_json_is_refused(self):
        for body in (b'{"after":0,"after":1}', b'{"after":NaN}', b"{\xff}", b"[" * 4000 + b"]" * 4000, b"{} " * (MAX_BODY // 3 + 1)):
            with self.subTest(prefix=body[:15]):
                self.assertIn(self.request("POST", "/api/recheck", body, authenticated=True)[0], (400, 413))
        self.assertEqual(self.request("POST", "/api/recheck", {"after": 0}, {"Content-Type": "text/plain"}, True)[0], 415)
        self.assertEqual(self.request("POST", "/api/recheck", {"after": 0}, {"Transfer-Encoding": "chunked"}, True)[0], 400)

    def test_duplicate_hosts_and_lengths_are_refused(self):
        for name, values in (("Host", [self.server.authority] * 2), ("Content-Length", ["2", "2"])):
            connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            try:
                connection.putrequest("POST", "/api/notifier/disable", skip_host=True)
                headers = {"Host": self.server.authority, "Cookie": self.cookie, "X-DealRadar-UI": "1",
                           "Origin": self.server.origin, "X-CSRF-Token": self.csrf, "Content-Type": "application/json", "Content-Length": "2"}
                for key, value in headers.items():
                    if key != name:
                        connection.putheader(key, value)
                for value in values:
                    connection.putheader(name, value)
                connection.endheaders(b"{}")
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            finally:
                connection.close()

    def test_no_file_server_traversal_or_database_selector_exists(self):
        for path in ("/README.md", "/data/anything.sqlite3", "/../pyproject.toml", "/%2e%2e/pyproject.toml",
                     "/api/overview?db=C:/private", "/api/overview?source=x&source=y"):
            code, _, body = self.request("GET", path, authenticated=True)
            self.assertIn(code, (400, 404))
            self.assertNotIn(b"Traceback", body)
        self.assertEqual(self.request("POST", "/api/login", {}, authenticated=True)[0], 404)

    def test_read_endpoints_do_not_offer_get_mutations(self):
        self.assertEqual(self.request("GET", "/api/sources/state?key=test:ui&enabled=0", authenticated=True)[0], 404)
        code, _, body = self.request("GET", "/api/post?source=test:ui&message_id=1", authenticated=True)
        self.assertEqual(code, 200)
        self.assertIn("СИНТЕТИЧЕСКИЙ", json.loads(body)["versions"][0]["raw_text"])

    def test_preset_feed_filter_uses_the_same_read_auth_and_validation(self):
        path = "/api/feed?preset=synthetic-ui"
        self.assertEqual(self.request("GET", path)[0], 403)
        code, _, body = self.request("GET", path, authenticated=True)
        self.assertEqual(code, 200)
        self.assertEqual([item["message_id"] for item in json.loads(body)["items"]], [1])
        for suffix in ("synthetic-missing", "%27%20OR%201%3D1--", "synthetic-ui&preset=synthetic-ui"):
            self.assertEqual(self.request("GET", "/api/feed?preset=" + suffix, authenticated=True)[0], 400)

    def test_server_restart_invalidates_old_session(self):
        with LocalUI(self.path, 0) as other:
            self.assertNotEqual(self.server.session, other.session)
            self.assertNotEqual(self.server.csrf, other.csrf)
            self.assertNotEqual(self.server.cookie_name, other.cookie_name)
            worker = threading.Thread(target=other.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            worker.start()
            connection = HTTPConnection("127.0.0.1", other.server_port, timeout=3)
            try:
                connection.request("GET", "/api/overview", headers={"Cookie": self.cookie, "X-DealRadar-UI": "1"})
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
            finally:
                connection.close()
                other.shutdown()
                worker.join(2)

    def test_clear_requires_auth_csrf_and_exact_confirmation(self):
        path = '/api/history/clear'
        self.assertEqual(self.request('POST', path, {'confirmation': 'УДАЛИТЬ'})[0], 403)
        self.assertEqual(self.request('POST', path, {'confirmation': 'УДАЛИТЬ'}, {'Origin': 'https://evil.invalid'}, True)[0], 403)
        self.assertEqual(self.request('POST', path, {'confirmation': 'УДАЛИТЬ'}, {'X-CSRF-Token': 'wrong'}, True)[0], 403)
        self.assertEqual(self.request('POST', path, {'confirmation': 'yes'}, authenticated=True)[0], 400)
        self.assertEqual(self.request('GET', path, authenticated=True)[0], 404)
        with Store(self.path) as store:
            self.assertEqual(store.stats()['counts']['posts'], 4)
        code, _, body = self.request('POST', path, {'confirmation': 'УДАЛИТЬ'}, authenticated=True)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)['cleared'])
        with Store(self.path) as store:
            self.assertEqual(store.stats()['counts']['posts'], 0)
            self.assertEqual(store.rules()[0]['config']['key'], 'synthetic-ui')
            self.assertEqual(store.sources()[0]['source_key'], 'test:ui')

    def test_maintenance_refuses_an_active_monitor_without_deleting(self):
        from dealradar.process_lock import monitor_lock
        with monitor_lock(self.path):
            for path, word in (('/api/history/clear', 'УДАЛИТЬ'), ('/api/notifier/forget', 'ОТВЯЗАТЬ')):
                code, _, body = self.request('POST', path, {'confirmation': word}, authenticated=True)
                self.assertEqual(code, 409)
                self.assertEqual(json.loads(body)['error'], 'maintenance_conflict')
        with Store(self.path) as store:
            self.assertEqual(store.stats()['counts']['posts'], 4)

    def test_notifier_unlink_is_local_authorized_and_preserves_posts(self):
        with Store(self.path) as store:
            store.link_notifier(123, 456)
        path = '/api/notifier/forget'
        self.assertEqual(self.request('POST', path, {'confirmation': 'ОТВЯЗАТЬ'})[0], 403)
        self.assertEqual(self.request('POST', path, {'confirmation': 'yes'}, authenticated=True)[0], 400)
        self.assertEqual(self.request('GET', path, authenticated=True)[0], 404)
        self.assertEqual(self.request('POST', path, {'confirmation': 'ОТВЯЗАТЬ'}, authenticated=True)[0], 200)
        with Store(self.path) as store:
            self.assertIsNone(store.notifier())
            self.assertEqual(store.stats()['counts']['posts'], 4)

    def test_ui_resolution_requires_the_displayed_source_and_publication(self):
        path = '/api/notifications/resolve'
        self.assertEqual(self.request('POST', path, {'id': 1, 'outcome': 'sent'}, authenticated=True)[0], 404)
        self.assertEqual(self.request('POST', path, {'id': 1, 'outcome': 'sent', 'source': 'test:ui', 'message_id': True}, authenticated=True)[0], 400)

    def test_shutdown_requires_authorized_post_and_stops_only_ui(self):
        self.assertEqual(self.request("GET", "/api/shutdown", authenticated=True)[0], 404)
        self.assertEqual(self.request("POST", "/api/shutdown", {}, {"Origin": "https://evil.invalid"}, True)[0], 403)
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(self.request("POST", "/api/shutdown", {}, authenticated=True)[0], 200)
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        with Store(self.path) as store:
            self.assertEqual(store.stats()["counts"]["posts"], 4)
            self.assertIsNone(store.notifier())

    def test_invalid_port_and_occupied_port_do_not_create_database(self):
        untouched = Path(self.temp.name) / "must-not-exist.sqlite3"
        for port in (0, 65536, self.server.server_port):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--db", str(untouched), "ui", "--port", str(port)]), 1)
            self.assertFalse(untouched.exists())

    def test_source_add_requires_csrf_and_explicit_consent_and_is_local_only(self):
        with Store(self.path) as store:
            store.connection.execute('DELETE FROM sources')
        body = {'address': 'https://t.me/synth_alpha', 'already_joined': True, 'start_from': 'connection'}
        self.assertEqual(self.request('POST', '/api/sources/add', body)[0], 403)
        self.assertEqual(self.request('POST', '/api/sources/add', body, {'Origin': 'https://evil.invalid'}, True)[0], 403)
        for key, value in (('already_joined', False), ('already_joined', 1), ('start_from', 'history')):
            self.assertEqual(self.request('POST', '/api/sources/add', {**body, key: value}, authenticated=True)[0], 400)
        with Store(self.path) as store:
            self.assertFalse(store.sources())
        code, _, result = self.request('POST', '/api/sources/add', body, authenticated=True)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(result)['state'], 'awaiting_connection')
        code, _, result = self.request('GET', '/api/overview', authenticated=True)
        self.assertIsNone(json.loads(result)['sources'][0]['channel_id'])
        duplicate = {**body, 'address': '@Synth_Alpha'}
        self.assertEqual(self.request('POST', '/api/sources/add', duplicate, authenticated=True)[0], 400)
        bad = {**body, 'address': 'https://t.me@127.0.0.1/synth_alpha'}
        self.assertEqual(self.request('POST', '/api/sources/add', bad, authenticated=True)[0], 400)
        with Store(self.path) as store:
            self.assertEqual(len(store.sources()), 1)
            self.assertFalse(store.stats()['counts']['posts'])


if __name__ == "__main__":
    unittest.main()
