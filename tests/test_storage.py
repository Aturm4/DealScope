from dataclasses import replace
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dealradar.models import IncomingPost, SearchRule
from dealradar.storage import Store

PUBLISHED = datetime(2026, 10, 1, 5, tzinfo=UTC)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.store = Store(self.path)
        self.store.add_test_source("test:a", "Искусственный источник A")
        self.rule = SearchRule("test-rule", "Искусственное правило", "USD", Decimal("50"))
        self.store.put_rule(self.rule)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def post(self, text="SSD NVMe 512GB 40$", message_id=1, edited_at=None):
        return IncomingPost(message_id, text, PUBLISHED, edited_at=edited_at)

    def test_original_and_extracted_results_are_saved(self):
        detected = PUBLISHED + timedelta(minutes=1)
        post = replace(self.post(), original_url="https://example.invalid/test/1")
        result = self.store.process("test:a", post, detected_at=detected)
        self.assertEqual((result.status, result.matched), ("created", 1))
        history = self.store.history("test:a", 1)
        self.assertEqual(history[0]["raw_text"], post.text)
        self.assertEqual(history[0]["published_at"], PUBLISHED.isoformat())
        self.assertEqual(history[0]["detected_at"], detected.isoformat())
        offer = self.store.offers()[0]
        self.assertEqual(offer["original_url"], post.original_url)
        self.assertEqual(offer["offer"]["price"], "40")
        self.assertEqual(offer["evaluations"][0]["decision"], "matched")

    def test_duplicate_same_source_id_does_not_create_versions_or_jobs(self):
        first = self.store.process("test:a", self.post())
        second = self.store.process("test:a", self.post())
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(first.post_id, second.post_id)
        self.assertEqual(len(self.store.history("test:a", 1)), 1)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_same_post_in_different_sources_is_not_deduplicated(self):
        self.store.add_test_source("test:b", "Искусственный источник B")
        self.store.process("test:a", self.post())
        self.store.process("test:b", self.post())
        self.assertEqual(self.store.stats()["counts"]["posts"], 2)
        self.assertEqual(len(self.store.jobs()), 2)

    def test_edit_to_sold_updates_existing_post_and_cancels_job(self):
        initial = self.store.process("test:a", self.post())
        edited = self.store.process("test:a", self.post("SSD NVMe 512GB 40$❌", edited_at=PUBLISHED + timedelta(hours=1)))
        self.assertEqual(initial.post_id, edited.post_id)
        self.assertEqual((edited.status, edited.revision), ("edited", 2))
        self.assertEqual(self.store.stats()["counts"]["posts"], 1)
        self.assertEqual(len(self.store.history("test:a", 1)), 2)
        self.assertEqual(self.store.offers()[0]["offer"]["availability"], "sold")
        self.assertEqual(self.store.jobs()[0]["status"], "cancelled")

    def test_edit_to_ambiguous_price_cancels_job(self):
        self.store.process("test:a", self.post())
        self.store.process("test:a", self.post("SSD NVMe 512GB от 40$", edited_at=PUBLISHED + timedelta(minutes=5)))
        self.assertEqual(self.store.jobs()[0]["status"], "cancelled")

    def test_partial_sale_updates_preview_to_remaining_items(self):
        text = "SSD NVMe\nSamsung 512GB 40$\nCrucial 1TB 45$"
        self.store.process("test:a", self.post(text))
        self.assertEqual(self.store.jobs()[0]["offer_ordinals"], [0, 1])
        self.store.process("test:a", self.post(text.replace("40$", "40$❌"), edited_at=PUBLISHED + timedelta(minutes=5)))
        self.assertEqual(self.store.jobs()[0]["offer_ordinals"], [1])
        self.assertEqual(self.store.jobs()[0]["target_revision"], 2)

    def test_no_new_notification_when_edit_becomes_matching(self):
        self.store.process("test:a", self.post("SSD NVMe 512GB 70$"))
        result = self.store.process("test:a", self.post(edited_at=PUBLISHED + timedelta(minutes=5)))
        self.assertEqual(result.matched, 1)
        self.assertEqual(self.store.jobs(), [])

    def test_cancelled_preview_is_not_revived_after_later_edit(self):
        self.store.process("test:a", self.post())
        self.store.process("test:a", self.post("SSD NVMe 512GB 40$❌", edited_at=PUBLISHED + timedelta(minutes=1)))
        self.store.process("test:a", self.post(edited_at=PUBLISHED + timedelta(minutes=2)))
        self.assertEqual(self.store.jobs()[0]["status"], "cancelled")

    def test_out_of_order_edit_cannot_restore_old_offer(self):
        self.store.process("test:a", self.post())
        self.store.process("test:a", self.post("SSD NVMe 512GB 40$❌", edited_at=PUBLISHED + timedelta(minutes=5)))
        result = self.store.process("test:a", self.post(edited_at=PUBLISHED + timedelta(minutes=1)))
        self.assertEqual(result.status, "stale_ignored")
        self.assertEqual(len(self.store.history("test:a", 1)), 2)
        self.assertEqual(self.store.offers()[0]["offer"]["availability"], "sold")

    def test_restart_keeps_rules_cursor_history_and_deduplication(self):
        self.store.process("test:a", self.post(message_id=22))
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.rules()[0]["version"], 1)
        self.assertEqual(self.store.sources()[0]["last_message_id"], 22)
        self.assertEqual(len(self.store.history("test:a", 22)), 1)
        self.assertEqual(self.store.process("test:a", self.post(message_id=22)).status, "duplicate")
        self.assertEqual(len(self.store.jobs()), 1)

    def test_processing_failure_rolls_back_post_cursor_and_job(self):
        with patch("dealradar.storage.evaluate", side_effect=RuntimeError("TEST failure")):
            with self.assertRaises(RuntimeError):
                self.store.process("test:a", self.post())
        self.assertEqual(self.store.sources()[0]["last_message_id"], 0)
        self.assertIsNone(self.store.sources()[0]["last_successful_ingest_at"])
        self.assertEqual(self.store.stats()["counts"]["posts"], 0)
        self.assertEqual(self.store.jobs(), [])
        self.assertEqual(self.store.process("test:a", self.post()).status, "created")

    def test_failure_on_one_source_does_not_block_other_source(self):
        self.store.add_test_source("test:b", "Искусственный источник B")
        self.store.source_error("test:a", "timeout")
        self.store.process("test:b", self.post())
        sources = {row["source_key"]: row for row in self.store.sources()}
        self.assertEqual(sources["test:a"]["last_error_code"], "timeout")
        self.assertIsNone(sources["test:a"]["last_successful_ingest_at"])
        self.assertIsNone(sources["test:b"]["last_error_code"])
        self.assertIsNotNone(sources["test:b"]["last_successful_ingest_at"])

    def test_cursor_does_not_move_backward_on_old_post_or_edit(self):
        self.store.process("test:a", self.post(message_id=22))
        self.store.process("test:a", self.post(message_id=3))
        self.assertEqual(self.store.sources()[0]["last_message_id"], 22)

    def test_disable_source_cancels_preview_and_rejects_import(self):
        self.store.process("test:a", self.post())
        self.store.set_source_enabled("test:a", False)
        self.assertEqual(self.store.jobs()[0]["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.store.process("test:a", self.post(message_id=2))

    def test_rules_recheck_does_not_notify_history(self):
        self.store.put_rule(replace(self.rule, max_price=Decimal("20")))
        self.store.process("test:a", self.post())
        self.store.put_rule(self.rule)
        self.assertEqual(self.store.recheck()["matched"], 1)
        self.assertEqual(self.store.jobs(), [])

    def test_rule_change_cancels_old_preview_and_keeps_evidence(self):
        self.store.process("test:a", self.post())
        self.store.put_rule(replace(self.rule, max_price=Decimal("20")))
        self.assertEqual(self.store.jobs()[0]["reason"], "rule_changed")
        self.assertEqual(self.store.offers()[0]["evaluations"], [])
        self.store.recheck()
        self.assertEqual(self.store.offers()[0]["evaluations"][0]["decision"], "rejected")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0], 2)

    def test_same_rule_does_not_create_new_version(self):
        self.assertEqual(self.store.put_rule(self.rule), 1)

    def test_media_only_has_original_but_no_items_or_jobs(self):
        self.store.process("test:a", replace(self.post(""), has_media=True))
        self.assertEqual(self.store.history("test:a", 1)[0]["parse_status"], "unsupported_media")
        self.assertEqual(self.store.offers(), [])
        self.assertEqual(self.store.jobs(), [])

    def test_sql_and_markup_are_preserved_only_as_data(self):
        text = "SSD NVMe 512GB 40$\n'); DROP TABLE sources; --\n<script>alert('TEST')</script>"
        self.store.process("test:a", self.post(text))
        self.assertEqual(self.store.history("test:a", 1)[0]["raw_text"], text)
        self.assertEqual(len(self.store.sources()), 1)
        self.assertEqual(self.store.history("test:a' OR 1=1 --", 1), [])

    def test_foreign_keys_and_integrity(self):
        self.store.process("test:a", self.post())
        self.assertEqual(self.store.connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(self.store.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unknown_schema_and_unrelated_db_are_not_overwritten(self):
        for kind in ("newer", "unrelated"):
            with self.subTest(kind=kind):
                path = Path(self.temp.name) / f"{kind}.sqlite3"
                with closing(sqlite3.connect(path)) as db:
                    db.execute("CREATE TABLE keep_me(value TEXT)")
                    db.execute("INSERT INTO keep_me VALUES ('TEST DATA')")
                    if kind == "newer":
                        db.execute("PRAGMA user_version=99")
                    db.commit()
                with self.assertRaises(ValueError):
                    Store(path)
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("SELECT value FROM keep_me").fetchone()[0], "TEST DATA")

    def test_changed_text_requires_edit_timestamp(self):
        self.store.process("test:a", self.post())
        with self.assertRaises(ValueError):
            self.store.process("test:a", self.post("SSD NVMe 512GB 35$"))
        self.assertEqual(len(self.store.history("test:a", 1)), 1)


if __name__ == "__main__":
    unittest.main()
