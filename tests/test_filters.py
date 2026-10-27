from dataclasses import replace
from decimal import Decimal
import unittest

from dealradar.filters import evaluate
from dealradar.models import SearchRule
from dealradar.parser import parse_post


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.rule = SearchRule("test-usd", "ТЕСТОВОЕ правило", "USD", Decimal("50"))

    def decide(self, text, rule=None):
        return evaluate(parse_post(text).offers[0], rule or self.rule)

    def test_full_match_at_price_ceiling(self):
        result = self.decide("SSD NVMe 512GB 50$")
        self.assertEqual(result.status, "matched")
        self.assertIn("price_within_limit", result.reasons)

    def test_price_above_ceiling(self):
        result = self.decide("SSD NVMe 512GB 50.01$")
        self.assertEqual(result.status, "rejected")
        self.assertIn("price_above_limit", result.reasons)

    def test_wrong_capacity_interface_type_and_buy_request(self):
        for text, reason in (("SSD NVMe 128GB 15$", "wrong_capacity_gb"),
                             ("SSD SATA 512GB 30$", "wrong_interface"),
                             ("HDD 1TB 30$", "wrong_drive_type"),
                             ("Куплю SSD NVMe 512GB 40$", "buy_request")):
            with self.subTest(text=text):
                result = self.decide(text)
                self.assertEqual(result.status, "rejected")
                self.assertIn(reason, result.reasons)

    def test_unknown_required_interface_needs_review(self):
        result = self.decide("SSD M.2 512GB 30$")
        self.assertEqual(result.status, "needs_review")
        self.assertIn("unknown_interface", result.reasons)

    def test_unknown_optional_fields_do_not_block(self):
        self.assertEqual(self.decide("SSD NVMe 512GB 30$").status, "matched")

    def test_required_brand_model_and_condition(self):
        rule = replace(self.rule, brands=("Samsung",), models=("980 PRO",), conditions=("new",))
        self.assertEqual(self.decide("SSD Samsung 980 PRO 1TB NVMe новый 49$", rule).status, "matched")
        result = self.decide("SSD NVMe 1TB 49$", rule)
        self.assertEqual(result.status, "needs_review")
        self.assertTrue({"unknown_brand", "unknown_model", "unknown_condition"} <= set(result.reasons))

    def test_keyword_modes_and_exclusion(self):
        rule = replace(self.rule, keywords=("nvme", "samsung"), keyword_mode="all", exclusions=("адаптер",))
        self.assertEqual(self.decide("SSD Samsung 512GB NVMe 40$", rule).status, "matched")
        self.assertIn("keywords_not_matched", self.decide("SSD NVMe 512GB 40$", rule).reasons)
        self.assertIn("excluded_keyword", self.decide("Адаптер SSD Samsung NVMe 512GB 40$", rule).reasons)
        any_rule = replace(rule, keyword_mode="any")
        self.assertEqual(self.decide("SSD NVMe 512GB 40$", any_rule).status, "matched")

    def test_different_currency_is_not_compared_without_rate(self):
        result = self.decide("SSD NVMe 512GB 500000 so'm")
        self.assertEqual(result.status, "needs_review")
        self.assertIn("currency_not_comparable", result.reasons)
        self.assertNotIn("price_above_limit", result.reasons)

    def test_uzs_has_own_ceiling(self):
        rule = replace(self.rule, currency="UZS", max_price=Decimal("600000"))
        self.assertEqual(self.decide("ССД NVMe 512ГБ 600 000 сум", rule).status, "matched")

    def test_non_full_prices_never_match(self):
        for text in ("от 40$", "5$ в месяц", "предоплата 10$", "40–50$"):
            with self.subTest(text=text):
                self.assertEqual(self.decide("SSD NVMe 512GB " + text).status, "needs_review")

    def test_sold_never_matches(self):
        self.assertEqual(self.decide("SSD NVMe 512GB 40$❌").reasons, ("sold",))

    def test_accessory_is_not_the_drive_it_fits(self):
        self.assertEqual(self.decide("Переходник для SSD NVMe 512GB 10$").status, "rejected")

    def test_question_mark_on_interface_needs_review(self):
        self.assertEqual(self.decide("SSD NVMe? 512GB 40$").status, "needs_review")

    def test_500gb_is_not_512gb(self):
        self.assertEqual(self.decide("SSD NVMe 500GB 30$").status, "rejected")

    def test_keyword_exclusion_does_not_leak_from_sibling_product(self):
        rule = replace(self.rule, exclusions=("б/у",))
        items = parse_post("SSD NVMe\nSamsung 512GB 40$ б/у\nCrucial 1TB 45$ новый").offers
        self.assertEqual([evaluate(item, rule).status for item in items], ["rejected", "matched"])


if __name__ == "__main__":
    unittest.main()
