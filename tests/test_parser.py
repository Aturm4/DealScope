from decimal import Decimal
import unittest

from dealradar.parser import extract_capacities, extract_price, parse_post


class PriceTests(unittest.TestCase):
    def test_supported_currencies_and_separators(self):
        cases = {
            "Цена 50$": ("50", "USD"), "$ 49.99": ("49.99", "USD"),
            "USD 49,50": ("49.50", "USD"), "50 долларов": ("50", "USD"),
            "1 200 000 сум": ("1200000", "UZS"), "1.200.000 UZS": ("1200000", "UZS"),
            "600 000 so‘m": ("600000", "UZS"), "600000 soʻm": ("600000", "UZS"),
            "600000 so'm": ("600000", "UZS"), "600000 сўм": ("600000", "UZS"),
            "1,200.50 USD": ("1200.50", "USD"), "1.200,50 USD": ("1200.50", "USD"),
        }
        for text, (amount, currency) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(extract_price(text), (Decimal(amount), currency, "full", ()))

    def test_partial_and_non_full_prices(self):
        for text, kind in (("от 40$", "from"), ("от $40", "from"), ("от USD 40", "from"), ("from 40 USD", "from"),
                           ("5$ в месяц", "installment"), ("oyiga 50000 so'm", "installment"),
                           ("Предоплата 10$", "deposit"), ("Аванс 100000 сум", "deposit")):
            with self.subTest(text=text):
                self.assertEqual(extract_price(text)[2], kind)

    def test_multiple_and_ranges_are_ambiguous(self):
        for text in ("50–70$", "50/70 USD", "50$ или 600000 UZS", "40$ и 50$"):
            with self.subTest(text=text):
                self.assertEqual(extract_price(text)[2], "ambiguous")
                self.assertIsNone(extract_price(text)[0])

    def test_missing_currency_is_unknown(self):
        amount, currency, kind, _ = extract_price("SSD 512ГБ, цена: 500000")
        self.assertEqual(amount, Decimal("500000"))
        self.assertIsNone(currency)
        self.assertEqual(kind, "unknown")

    def test_malformed_grouping_is_not_a_cheaper_suffix(self):
        self.assertEqual(extract_price("Цена 12 34 000 сум")[2], "ambiguous")

    def test_negative_price_is_not_positive_price(self):
        for text in ("Цена -40$", "Цена −40 USD", "Цена -$40"):
            with self.subTest(text=text):
                self.assertEqual(extract_price(text)[2], "ambiguous")

    def test_capacity_is_not_price(self):
        self.assertEqual(extract_price("SSD 512 GB M.2 PCIe 4.0"), (None, None, "unknown", ()))


class CapacityTests(unittest.TestCase):
    def test_units_and_decimal_tb(self):
        for text, amount in (("512GB", 512), ("512 ГБ", 512), ("1TB", 1000),
                             ("1 ТБ", 1000), ("0,5 TB", 500), ("1.024TB", 1024)):
            with self.subTest(text=text):
                self.assertEqual(extract_capacities(text)[0][0], amount)

    def test_multiple_capacities_are_not_guessed(self):
        offer = parse_post("SSD NVMe 512GB / 1TB цена 50$").offers[0]
        self.assertIsNone(offer.capacity_gb)
        self.assertIn("multiple_capacities", offer.issues)


class ParsingTests(unittest.TestCase):
    def test_screenshot_style_list_has_per_item_availability(self):
        parsed = parse_post("SSD NVME диски\n🟠 Kingfast 128GB 15$❌\n🟠 Biostar 128GB 15$❌\n"
                            "🟠 Axle 128GB 15$❌\n🟠 Western Digital 512GB 50$\n🟠 Hikvision 512GB 50$")
        self.assertEqual(len(parsed.offers), 5)
        self.assertEqual([item.availability for item in parsed.offers], ["sold"] * 3 + ["unmarked"] * 2)
        self.assertTrue(all(item.drive_type == "ssd" and item.interface == "nvme" for item in parsed.offers))
        self.assertTrue(all(item.model is None for item in parsed.offers))

    def test_whole_post_sold_marker(self):
        for marker in ("ПРОДАНО❌ | SOTILDI❌", "ВСЁ ПРОДАНО", "sotildi"):
            with self.subTest(marker=marker):
                parsed = parse_post("SSD NVMe\nSamsung 512GB 40$\nCrucial 1TB 45$\n" + marker)
                self.assertTrue(all(item.availability == "sold" for item in parsed.offers))

    def test_negative_sold_and_pcie_x_are_not_sold(self):
        for tail in ("Ещё не продано", "not sold", "PCIe x4", "❌ Доставки нет"):
            with self.subTest(tail=tail):
                self.assertEqual(parse_post("SSD NVMe 512GB 40$\n" + tail).offers[0].availability, "unmarked")

    def test_negation_with_contradictory_cross_needs_review(self):
        item = parse_post("SSD NVMe 512GB 40$ не продано❌").offers[0]
        self.assertEqual(item.availability, "ambiguous")

    def test_m2_does_not_imply_nvme(self):
        item = parse_post("SSD Kingston 512GB M.2 40$").offers[0]
        self.assertEqual(item.form_factor, "m2")
        self.assertIsNone(item.interface)
        self.assertIsNone(item.condition)
        self.assertIsNone(item.model)

    def test_explicit_model_and_condition(self):
        item = parse_post("SSD Samsung 980 PRO 1ТБ NVMe, новый, 49$").offers[0]
        self.assertEqual((item.brand, item.model, item.capacity_gb, item.condition), ("Samsung", "980 PRO", 1000, "new"))

    def test_price_is_assigned_to_multiline_product_blocks(self):
        parsed = parse_post("SSD NVMe\nSamsung 512GB\nЦена: 40$\nCrucial 1TB\nЦена: 45$")
        self.assertEqual([item.price for item in parsed.offers], [Decimal("40"), Decimal("45")])
        self.assertTrue(all(not item.issues for item in parsed.offers))

    def test_shared_price_is_not_assigned_to_last_product(self):
        parsed = parse_post("SSD NVMe\nSamsung 512GB\nCrucial 1TB\nЦена: 40$")
        self.assertTrue(all("ambiguous_product_price" in item.issues for item in parsed.offers))

    def test_media_only_is_unsupported(self):
        result = parse_post("", has_media=True)
        self.assertEqual(result.status, "unsupported_media")
        self.assertEqual(result.offers, ())

    def test_caption_with_media_is_parsed(self):
        self.assertEqual(len(parse_post("SSD NVMe 512GB 40$", has_media=True).offers), 1)

    def test_conflicting_interfaces_remain_unknown(self):
        result = parse_post("SSD 512GB NVMe SATA 40$").offers[0]
        self.assertIsNone(result.interface)
        self.assertIn("conflicting_interfaces", result.issues)

    def test_other_product_cannot_inherit_ssd_from_heading(self):
        item = parse_post("SSD NVMe\nВидеокарта RTX 3080 10GB 40$").offers[0]
        self.assertEqual(item.drive_type, "other")

    def test_cross_before_interface_does_not_confirm_nvme(self):
        item = parse_post("SSD 512GB 40$\n❌ NVMe").offers[0]
        self.assertIsNone(item.interface)

    def test_two_brands_in_one_block_need_review(self):
        item = parse_post("SSD NVMe Samsung или Crucial 512GB 40$").offers[0]
        self.assertIn("multiple_products_in_block", item.issues)


if __name__ == "__main__":
    unittest.main()
