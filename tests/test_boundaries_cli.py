from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from dealradar.cli import MAX_INPUT_BYTES, _print, import_posts, main, read_json
from dealradar.models import IncomingPost, SearchRule
from dealradar.storage import Store


class InputBoundaryTests(unittest.TestCase):
    def test_reject_invalid_post_values(self):
        base = {"message_id": 1, "text": "TEST", "published_at": "2026-10-01T00:00:00Z"}
        for changes in ({"message_id": True}, {"message_id": -1}, {"message_id": 2**63},
                        {"text": "x" * 32001}, {"text": None}, {"published_at": "2026-10-01T00:00:00"},
                        {"has_media": "yes"}, {"edited_at": "2026-09-01T00:00:00Z"}, {"token": "TEST_PLACEHOLDER"}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ValueError):
                    IncomingPost.from_dict(base | changes)

    def test_reject_dangerous_reference_urls(self):
        for url in ("javascript:alert(1)", "file:///C:/TEST", "https://user:password@example.invalid/",
                    "https://example.invalid\n/", "https://example.invalid\\evil", "https://example.invalid:1234/"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    IncomingPost(1, "TEST", datetime(2026, 10, 1, tzinfo=UTC), original_url=url)

    def test_reject_invalid_rule_values(self):
        base = {"key": "test-rule", "name": "ТЕСТ", "currency": "USD", "max_price": "50"}
        for changes in ({"max_price": "NaN"}, {"max_price": "Infinity"}, {"max_price": "-1"},
                        {"max_price": "0.001"}, {"max_price": 50.0}, {"currency": "EUR"},
                        {"enabled": "true"}, {"capacities_gb": [True]}, {"keywords": "nvme"},
                        {"keyword_mode": "regex"}, {"extra": "ignored?"}, {"key": "../elsewhere"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    SearchRule.from_dict(base | changes)

    def test_json_limits_duplicates_and_nonfinite_numbers(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "input.json"
            for content in (b'{"key":1,"key":2}', b'{"value":NaN}', b'{"value":Infinity}',
                            b"[" * 2000 + b"]" * 2000, b" " * (MAX_INPUT_BYTES + 1)):
                with self.subTest(prefix=content[:30]):
                    path.write_bytes(content)
                    with self.assertRaises(ValueError):
                        read_json(path)

    def test_brackets_inside_json_string_do_not_count_as_nesting(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "input.json"
            text = '[' * 100 + '\\"' + ']' * 100
            path.write_text(json.dumps({"text": text}), encoding="utf-8")
            self.assertEqual(read_json(path)["text"], text)

    def test_input_validation_precedes_import_mutations(self):
        with TemporaryDirectory() as temp, Store(Path(temp) / "test.sqlite3") as store:
            store.add_test_source("test:a", "ТЕСТ")
            data = [{"message_id": 1, "text": "SSD NVMe 512GB 40$", "published_at": "2026-10-01T00:00:00Z"}, {"invalid": True}]
            with self.assertRaises(ValueError):
                import_posts(store, "test:a", data)
            self.assertEqual(store.stats()["counts"]["posts"], 0)

    def test_stage_one_refuses_real_source_names(self):
        with TemporaryDirectory() as temp, Store(Path(temp) / "test.sqlite3") as store:
            with self.assertRaises(ValueError):
                store.add_test_source("telegram:SborkiPcTashkent", "Это ещё не интеграция")

    def test_terminal_control_characters_are_not_executed(self):
        out = io.StringIO()
        with redirect_stdout(out):
            _print({"text": "TEST\x1b[2J\u202eevil\x9b"})
        rendered = out.getvalue()
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertNotIn("\x9b", rendered)
        self.assertIn("\\u202e", rendered)


class CliTests(unittest.TestCase):
    def call(self, db, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(["--db", str(db), *args])
        return status, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def test_demo_repeat_restart_and_integrity(self):
        with TemporaryDirectory() as temp:
            db = Path(temp) / "demo.sqlite3"
            code, first, _ = self.call(db, "demo")
            self.assertEqual(code, 0)
            self.assertIn("синтетические", first["notice"])
            self.assertFalse(first["telegram_connected"])
            self.assertEqual(first["counts"]["posts"], 7)
            self.assertEqual(first["counts"]["post_versions"], 8)
            code, second, _ = self.call(db, "demo")
            self.assertEqual(code, 0)
            self.assertEqual(second["counts"], first["counts"])
            self.assertTrue(all(result["status"] in ("duplicate", "stale_ignored") for result in second["results"]))
            code, items, _ = self.call(db, "list", "--decision", "matched")
            self.assertEqual(code, 0)
            self.assertEqual(len(items), 2)
            code, jobs, _ = self.call(db, "jobs")
            self.assertTrue(jobs["sending_implemented"])
            self.assertTrue(all(job['status'] == 'held' for job in jobs['jobs']))
            self.assertTrue(all(job["status"] == "held" for job in jobs["jobs"]))
            code, integrity, _ = self.call(db, "check")
            self.assertEqual(code, 0)
            self.assertTrue(integrity["ok"])

    def test_invalid_rule_does_not_even_create_database(self):
        with TemporaryDirectory() as temp:
            db, config = Path(temp) / "test.sqlite3", Path(temp) / "rule.json"
            config.write_text('{"key":"test","max_price":"NaN"}', encoding="utf-8")
            code, _, error = self.call(db, "rule-load", str(config))
            self.assertEqual(code, 1)
            self.assertFalse(db.exists())
            self.assertIn("error", error)


if __name__ == "__main__":
    unittest.main()
