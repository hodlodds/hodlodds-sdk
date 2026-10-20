"""0.3.2 contract: price-limit details, TLS dependency floor, README and additive fields."""
import re
import unittest
from pathlib import Path

import test_client as f
import test_watch as w

client = f.client
ROOT = Path(__file__).resolve().parents[1]


def dependencies():
    text = (ROOT / "pyproject.toml").read_text()
    block = re.search(r"^dependencies = \[\n(.*?)^\]", text, re.MULTILINE | re.DOTALL).group(1)
    return re.findall(r'^\s*"([^"]+)",', block, re.MULTILINE)


class ReleaseMetadataTests(unittest.TestCase):
    def test_certifi_floor_authors_and_version(self):
        self.assertEqual(dependencies(), ["certifi>=2026.10.19", "httpx>=0.27,<1", "x402[svm,evm]==2.25.0"])
        text = (ROOT / "pyproject.toml").read_text()
        self.assertIn('authors = [{ name = "HodlOdds" }]', text)
        self.assertIn('version = "0.3.2"', text)
        self.assertIs(client.PriceLimitError, __import__("hodlodds").PriceLimitError)

    def test_readme_documents_new_fields_defaults_and_security(self):
        readme = (ROOT / "README.md").read_text()
        flat = " ".join(readme.split())
        for text in ("crashed_within_24h", "last_crash_as_of", "excluded_by_defaults",
                     "`min_liquidity_sol=5`", "`min_liquidity_sol=0`", "PriceLimitError",
                     "price_atomic", "limit_atomic", "## Security", "certifi>=2026.10.19",
                     'removes the "emSign ECC Root CA - G3" root'):
            self.assertIn(text, flat)
        self.assertIn("The lowest odds are usually the least-traded tokens; the default filters remove "
                      "most inactive or recently collapsed tokens, not all.", flat)
        self.assertIn("On the current API this 409 is returned only to a **signed request**", flat)
        self.assertIn("As a safeguard only", flat)
        self.assertNotIn("neither default", flat)


class PriceLimitTests(w.OfflineWatch, unittest.TestCase):
    def test_board_above_limit_reports_price_and_limit_before_signing(self):
        c = self.make()
        f.board_offer(self.sdk, n=10)
        with self.assertRaises(client.PriceLimitError) as caught:
            c.board(max_price_usdc=".04")
        error = caught.exception
        self.assertIsInstance(error, client.HodlOddsError)
        self.assertEqual(str(error), "price is invalid or above your USDC limit")
        self.assertEqual((error.price_atomic, error.limit_atomic), (50000, 40000))
        self.assertEqual((error.status, error.body, error.payment), (None, None, None))
        self.assertEqual((self.sdk.signs, self.sdk.loads, len(self.http.calls)), (0, 0, 1))

    def test_single_answer_above_default_limit(self):
        c = self.make()
        self.sdk.offer["accepts"][0]["amount"] = "20000"
        with self.assertRaises(client.PriceLimitError) as caught:
            c.risk(f.MINT)
        self.assertEqual((caught.exception.price_atomic, caught.exception.limit_atomic), (20000, 10000))
        self.assertEqual(self.sdk.signs, 0)

    def test_inconsistent_or_malformed_offer_is_not_a_price_limit(self):
        c = self.make()
        f.board_offer(self.sdk, n=10)
        self.sdk.offer["accepts"][0]["amount"] = "60000"  # not 10 x 0.005 USDC
        with self.assertRaises(client.HodlOddsError) as caught:
            c.board(max_price_usdc=".05")
        self.assertNotIsInstance(caught.exception, client.PriceLimitError)
        self.assertEqual(str(caught.exception), "board offer does not match the requested query and per-coin price")
        for amount in ("0", "1.5", "99999999", None):
            c = self.make()
            self.sdk.offer["accepts"][0]["amount"] = amount
            with self.subTest(amount=amount), self.assertRaises(client.HodlOddsError) as caught:
                c.risk(f.MINT)
            self.assertNotIsInstance(caught.exception, client.PriceLimitError)
            self.assertEqual(str(caught.exception), "price is invalid or above your USDC limit")
            self.assertEqual(self.sdk.signs, 0)


class AdditiveFieldTests(w.OfflineWatch, unittest.TestCase):
    def test_crash_memory_and_default_explanations_pass_through_unchanged(self):
        rows = [{"mint": f.MINT, "p_crash_60m": .002, "dd_60": -.2, "already_crashed": False,
                 "crashed_within_24h": True, "last_crash_as_of": 1799990000,
                 "note": "This token fell 90% or more from a recent high within the last 24 hours."},
                {"mint": f.MINT, "p_crash_60m": .003, "already_crashed": False,
                 "crashed_within_24h": None, "last_crash_as_of": None}]
        body = {"rows": rows, "filters": {"min_trades_5": 3, "min_liquidity_sol": 5, "include_crashed": False},
                "defaults_applied": {"min_trades_5": 3, "min_liquidity_sol": 5, "include_crashed": False,
                                     "reason": "example; recent-crash memory unavailable."},
                "excluded_by_defaults": 7, "total_matches": 2}
        c = self.make([f.challenge(), f.Response(200, {"PAYMENT-RESPONSE": "receipt"}, body)])
        f.board_offer(self.sdk, n=2)
        self.assertEqual(c.board(max_price_usdc=".05").data, body)


if __name__ == "__main__":
    unittest.main()
