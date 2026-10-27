"""Availability regressions: run adversarial bounded inputs in a disposable child."""

from pathlib import Path
import subprocess
import sys
import unittest

from dealradar.parser import extract_capacities, extract_price


class ParserLimitTests(unittest.TestCase):
    def test_long_accepted_inputs_complete_within_process_deadline(self):
        # Code is fixed test source; no imported post contributes executable text.
        script = """
from dealradar.parser import parse_post
cases = [
    'SSD NVMe 512GB 40$\\n1-1' + ' ' * 31900 + 'X',
    'SSD NVMe 512GB 40$\\n' + '1' * 15000 + ' ' * 15000 + 'X',
    'SSD NVMe 512GB 40$\\nб' + ' ' * 31900 + 'X',
    'SSD NVMe 512GB\\n' + '1 000 ' * 5000 + 'X',
]
for text in cases:
    assert len(text) <= 32000
    parsed = parse_post(text)
    assert parsed.status in ('parsed', 'needs_review')
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, timeout=8, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_extractors_reject_oversized_input(self):
        for extractor in (extract_price, extract_capacities):
            with self.subTest(extractor=extractor.__name__):
                with self.assertRaises(ValueError):
                    extractor("x" * 32001)

    def test_excessive_price_tokens_require_review(self):
        self.assertEqual(extract_price("1$ " * 201)[2:], ("ambiguous", ("too_many_price_tokens",)))

    def test_ranges_with_currency_on_either_end(self):
        for text in ("$50–70", "50$–70", "50–$70", "50$–50$", "50 / 70 USD"):
            with self.subTest(text=text):
                self.assertEqual(extract_price(text)[2], "ambiguous")


if __name__ == "__main__":
    unittest.main()
