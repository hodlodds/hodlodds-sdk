"""Offline client safety tests: fake HTTP transport + fake pinned-SDK seam.

No network, wallet funds, real key, or installed SDK is needed for these tests.
The optional real-SDK test mocks every RPC used for transaction construction.
"""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hodlodds import client

PAYTO = "Bga5h69rmgYbxZgLxVQGsevn4aBLPnN5vQEnZRQmXGkn"
PAYER = "So11111111111111111111111111111111111111112"
FEE = "11111111111111111111111111111111"
MINT = "CY8tK7zPvoRAL5tugvxBEgFEtrgAXiikTAKVgh61Siq5"
TX = "2" + "1" * 87  # 64-byte base58 signature shape; synthetic, never on chain.
BASE_PAYTO = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"  # EIP-55 example address; never funded by tests.
BASE_PAYER = "0x1111111111111111111111111111111111111111"
BASE_TX = "0x" + "a" * 64
FIXTURE_NETWORKS = {
    "devnet": {"family": "svm", "testnet": True, "caip2": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
               "usdc": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU", "rpc": "https://api.devnet.solana.com",
               "explorer": "https://explorer.solana.com/tx/", "explorer_suffix": "?cluster=devnet"},
    "solana": {"family": "svm", "testnet": False, "caip2": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
               "usdc": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "rpc": "https://api.mainnet-beta.solana.com",
               "explorer": "https://explorer.solana.com/tx/", "explorer_suffix": ""},
    "base": {"family": "evm", "testnet": False, "caip2": "eip155:8453", "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
             "name": "USD Coin", "version": "2", "rpc": None, "explorer": "https://basescan.org/tx/", "explorer_suffix": ""},
    "base-sepolia": {"family": "evm", "testnet": True, "caip2": "eip155:84532", "usdc": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                     "name": "USDC", "version": "2", "rpc": None, "explorer": "https://sepolia.basescan.org/tx/", "explorer_suffix": ""},
}
FIXTURE_NETWORKS["mainnet"] = FIXTURE_NETWORKS["solana"]
REAL_NETWORK_CONFIG = client._network_config


def setUpModule():
    # Offline seam: production obtains these from the installed pinned SDK,
    # never these test fixtures. No network or SDK installation is needed here.
    global _network_patch
    _network_patch = patch.object(client, "_network_config", side_effect=lambda name: copy.deepcopy(FIXTURE_NETWORKS[name]))
    _network_patch.start()


def tearDownModule():
    _network_patch.stop()


def offer():
    return {"x402Version": 2, "accepts": [{"scheme": "exact", "network": FIXTURE_NETWORKS["devnet"]["caip2"],
        "asset": FIXTURE_NETWORKS["devnet"]["usdc"], "amount": "10000", "payTo": PAYTO, "maxTimeoutSeconds": 60,
        "extra": {"paymentFlow": "authorization", "feePayer": FEE}}]}


def settlement():
    return {"success": True, "network": FIXTURE_NETWORKS["devnet"]["caip2"], "transaction": TX,
            "payer": PAYER, "amount": "10000"}


def board_offer(sdk, n=10, limit=50, sort="lowest_risk", mints=None, **filters):
    sdk.offer["accepts"][0]["amount"] = str(n * client.BOARD_ATOMIC_PER_COIN)
    sdk.offer["accepts"][0]["extra"]["hodloddsBoard"] = {
        "version": 1, "query": client._bound_query(client._board_query(limit, sort, filters, mints)), "priced_n": n, "nonce": "a" * 32}
    sdk.offer["accepts"][0]["extra"]["hodloddsBoardSignature"] = "b" * 64  # Server-HMAC shape; verified by server, not client.
    sdk.offer["accepts"][0]["extra"]["memo"] = board_memo(sdk.offer["accepts"][0]["extra"]["hodloddsBoard"])
    sdk.receipt["amount"] = sdk.offer["accepts"][0]["amount"]


def board_memo(metadata):
    return hashlib.sha256(b"hodlodds.board.v1\0" + json.dumps(
        metadata, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()[:32]


class Decoded:
    def __init__(self, data):
        self.data = data

    def model_dump(self, **kwargs):
        return copy.deepcopy(self.data)

    @property
    def accepts(self):
        return self.data["accepts"]

    def model_copy(self, update, **kwargs):
        return Decoded(dict(copy.deepcopy(self.data), **update))


class FakeSDK:
    def __init__(self):
        self.offer, self.receipt = offer(), settlement()
        self.signs, self.loads = 0, 0
        self.spend_caps = []
        self.payer = PAYER
        self.decode_error = self.receipt_error = self.sign_error = None
        self.encoded = "TEST_AUTHORIZATION_DO_NOT_LOG"
        self.signed_offer = None

    def decode(self, header):
        if self.decode_error:
            raise self.decode_error
        return Decoded(self.offer)

    def settled(self, header):
        if self.receipt_error:
            raise self.receipt_error
        return Decoded(self.receipt)

    def protocol(self):
        return {"decode": self.decode, "settled": self.settled, "encode": lambda payload: self.encoded}

    def load(self):
        self.loads += 1
        return dict(self.protocol(), payer=self.payer, client=self)

    def create_payment_payload(self, parsed):
        self.signs += 1
        self.signed_offer = parsed.model_dump()
        if self.sign_error:
            raise self.sign_error
        return {"authorization": "fixture"}

    def set_spend_controls(self, controls):
        self.spend_caps.append(controls["max_amount_per_payment"])


class Headers:
    def __init__(self, items):
        self.items = list(items.items()) if isinstance(items, dict) else list(items)

    def multi_items(self):
        return self.items


class Response:
    def __init__(self, status, headers=(), body=None):
        self.status_code, self.headers = status, Headers(headers)
        self.body = json.dumps({"fixture": True} if body is None else body).encode()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def iter_bytes(self):
        yield self.body


def challenge():
    return Response(402, {"PAYMENT-REQUIRED": "fixture"})


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls, self.closed = [], False

    def stream(self, method, url, headers, timeout=None):
        self.calls.append((method, url, headers))
        self.timeouts = getattr(self, "timeouts", []) + [timeout]
        if not self.responses:
            raise AssertionError("Unexpected automatic retry")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


class ClientTests(unittest.TestCase):
    def make(self, responses=None, **kwargs):
        self.sdk = FakeSDK()
        self.http = FakeHTTP(responses or [challenge(), Response(200, {"PAYMENT-RESPONSE": "receipt"})])
        with patch.object(client, "_new_http", return_value=self.http):
            c = client.HodlOdds("http://127.0.0.1:8010", pay_to=PAYTO, **kwargs)
        c._protocol = self.sdk.protocol
        c._sdk_client = self.sdk.load
        self.c = c
        return c

    def assert_no_payment(self, fn):
        with self.assertRaises(client.HodlOddsError):
            fn()
        self.assertEqual(self.sdk.signs, 0)
        self.assertTrue(all("PAYMENT-SIGNATURE" not in h for _, _, h in self.http.calls))

    def assert_uncertain_once(self, fn, status=None):
        with self.assertRaises(client.HodlOddsError) as result:
            fn()
        error = result.exception
        self.assertEqual(self.sdk.signs, 1)
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(error.status, status)
        self.assertIn("Reconcile", str(error))
        self.assertIn("not retried", str(error))
        self.assertNotIn("with no settlement", str(error))
        self.assertEqual(error.payment["amount_usdc"], "0.01")
        self.assertEqual(error.payment["pay_to"], PAYTO)
        return error

    def test_paid_request_waits_longer_than_free_ones(self):
        # Simulation r71: a 30 s client timeout is shorter than a slow settlement (~70 s).
        c = self.make()
        c.risk(MINT)
        self.assertEqual(self.http.timeouts, [None, 90.0])  # free challenge: client default; paid: 90 s
        for bad in (10, 601, "90", True):
            with self.subTest(bad=bad), self.assertRaisesRegex(client.HodlOddsError, "paid_timeout"):
                with patch.object(client, "_new_http", return_value=FakeHTTP([])):
                    client.HodlOdds("http://127.0.0.1:8010", pay_to=PAYTO, paid_timeout=bad)

    def test_every_invalid_offer_signs_nothing_and_does_not_load_key(self):
        changes = [lambda x: x.update(x402Version=1), lambda x: x.update(accepts=[]),
            lambda x: x["accepts"].append(copy.deepcopy(x["accepts"][0])), lambda x: x.update(accepts=[None])]
        for key, val in (("network", FIXTURE_NETWORKS["mainnet"]["caip2"]), ("asset", MINT), ("scheme", "upto"),
                         ("payTo", PAYER), ("amount", "10001"), ("amount", "1000001"), ("amount", "1.1"),
                         ("amount", "-1"), ("amount", "0"), ("amount", "01"), ("amount", "１００００"),
                         ("amount", "1" * 1000), ("amount", 10000), ("amount", True),
                         ("maxTimeoutSeconds", 0), ("maxTimeoutSeconds", 301), ("maxTimeoutSeconds", True),
                         ("extra", None)):
            changes.append(lambda x, k=key, v=val: x["accepts"][0].update({k: v}))
        for key, val in (("paymentFlow", "settlement"), ("feePayer", None), ("feePayer", "1" * 44),
                         ("memo", "x" * 257), ("memo", [1]), ("recentBlockhash", "bad")):
            changes.append(lambda x, k=key, v=val: x["accepts"][0]["extra"].update({k: v}))
        for change in changes:
            with self.subTest(change=change):
                c = self.make()
                change(self.sdk.offer)
                self.assert_no_payment(lambda: c.risk(MINT))
                self.assertEqual(self.sdk.loads, 0)

    def test_malformed_decode_is_redacted_before_signing(self):
        c = self.make(); self.sdk.decode_error = ValueError("DO_NOT_PRINT_PAYLOAD")
        with self.assertRaises(client.HodlOddsError) as result:
            c.risk(MINT)
        self.assertEqual(self.sdk.signs, 0)
        self.assertNotIn("DO_NOT_PRINT_PAYLOAD", repr(result.exception))

    def test_payer_must_differ_from_recipient_and_fee_payer(self):
        for payer in (PAYTO, FEE, "invalid"):
            c = self.make(); self.sdk.payer = payer
            self.assert_no_payment(lambda: c.risk(MINT))

    def test_free_answers_load_no_key_and_sign_nothing(self):
        for status in (404, 409, 410, 422, 429, 503):
            c = self.make([Response(status)])
            self.assert_no_payment(lambda: c.risk(MINT))
            self.assertEqual(self.sdk.loads, 0)
        c = self.make([Response(200)])
        self.assertFalse(c.risk(MINT).paid)
        self.assertEqual(self.sdk.signs, 0)

    def test_success_and_optional_receipt_fields(self):
        c = self.make()
        answer = c.risk(MINT)
        self.assertTrue(answer.paid)
        self.assertTrue(answer.payment["success"])
        self.assertEqual(answer.payment["transaction"], TX)
        self.assertEqual(self.sdk.signs, 1)
        self.assertEqual(len(self.http.calls), 2)
        self.assertNotIn(self.sdk.encoded, repr(answer))
        c = self.make(); self.sdk.receipt.pop("payer"); self.sdk.receipt.pop("amount")
        self.assertTrue(c.risk(MINT).paid)

    def test_paid_200_missing_receipt_is_ambiguous_not_free(self):
        c = self.make([challenge(), Response(200)])
        self.assert_uncertain_once(lambda: c.risk(MINT), 200)

    def test_paid_non200_with_or_without_receipt_never_retries(self):
        for status in (302, 402, 404, 409, 429, 500, 503):
            for receipt in (False, True):
                with self.subTest(status=status, receipt=receipt):
                    c = self.make([challenge(), Response(status, {"PAYMENT-RESPONSE": "receipt"} if receipt else {})])
                    error = self.assert_uncertain_once(lambda: c.risk(MINT), status)
                    if receipt and status != 302:
                        self.assertEqual(error.payment["transaction"], TX)

    def test_network_error_after_authorization_is_redacted_and_not_retried(self):
        c = self.make([challenge(), OSError("DO_NOT_PRINT_URL_OR_AUTH")])
        error = self.assert_uncertain_once(lambda: c.risk(MINT))
        self.assertNotIn("DO_NOT_PRINT", repr(error))

    def test_cleanup_failure_does_not_replace_reconciliation_error(self):
        c = self.make([challenge(), Response(200)])
        self.http.close = lambda: (_ for _ in ()).throw(OSError("DO_NOT_PRINT"))
        def request():
            with c:
                c.risk(MINT)
        error = self.assert_uncertain_once(request, 200)
        self.assertNotIn("DO_NOT_PRINT", repr(error))

    def test_malformed_or_failed_receipt_is_never_paid_success(self):
        for key, value in (("success", False), ("success", "true"), ("network", "wrong"),
                           ("payer", PAYTO), ("amount", "10001"), ("transaction", "bad")):
            c = self.make(); self.sdk.receipt[key] = value
            self.assert_uncertain_once(lambda: c.risk(MINT), 200)
        c = self.make(); self.sdk.receipt_error = ValueError("DO_NOT_PRINT_PAYLOAD")
        error = self.assert_uncertain_once(lambda: c.risk(MINT), 200)
        self.assertNotIn("DO_NOT_PRINT", repr(error))

    def test_bad_json_after_settlement_preserves_receipt(self):
        c = self.make([challenge(), Response(200, {"PAYMENT-RESPONSE": "receipt"}, body=[1, 2])])
        error = self.assert_uncertain_once(lambda: c.risk(MINT), 200)
        self.assertEqual(error.payment["transaction"], TX)
        nested = Response(200, {"PAYMENT-RESPONSE": "receipt"})
        nested.body = b"[" * 2000 + b"0" + b"]" * 2000
        c = self.make([challenge(), nested])
        self.assert_uncertain_once(lambda: c.risk(MINT), 200)

    def test_required_header_limits_duplicates_and_redirects_sign_nothing(self):
        for response in (Response(402), Response(302),
                Response(402, {"PAYMENT-REQUIRED": "x" * (client.MAX_PAYMENT_HEADER + 1)}),
                Response(402, [("PAYMENT-REQUIRED", "one"), ("Payment-Required", "two")]),
                Response(402, {"PAYMENT-REQUIRED": "x\ny"}),
                Response(402, {"X-Large": "x" * (client.MAX_HEADERS + 1)})):
            c = self.make([response])
            self.assert_no_payment(lambda: c.risk(MINT))
            self.assertTrue(response.closed)

    def test_response_size_limits_after_payment_require_reconciliation(self):
        large_body = Response(200); large_body.body = b"x" * (client.MAX_BODY + 1)
        for response in (Response(200, {"PAYMENT-RESPONSE": "x" * (client.MAX_PAYMENT_HEADER + 1)}),
                         Response(200, [("PAYMENT-RESPONSE", "a"), ("payment-response", "b")]), large_body):
            c = self.make([challenge(), response])
            self.assert_uncertain_once(lambda: c.risk(MINT), 200)

    def test_bad_generated_signature_or_sdk_error_is_never_sent(self):
        for signature in ("x" * (client.MAX_SIGNATURE_HEADER + 1), "header\ninjection", ""):
            c = self.make(); self.sdk.encoded = signature
            with self.assertRaisesRegex(client.HodlOddsError, "nothing sent"):
                c.risk(MINT)
            self.assertEqual(len(self.http.calls), 1)
        c = self.make(); self.sdk.sign_error = ValueError("DO_NOT_PRINT_KEY")
        with self.assertRaises(client.HodlOddsError) as error:
            c.risk(MINT)
        self.assertNotIn("DO_NOT_PRINT", str(error.exception))
        self.assertEqual(len(self.http.calls), 1)

    def test_last_and_board_keep_api_and_per_call_limits(self):
        c = self.make(); self.sdk.offer["accepts"][0]["amount"] = "2000"; self.sdk.receipt["amount"] = "2000"
        self.assertEqual(c.last(MINT).payment["amount_usdc"], "0.002")
        self.assertTrue(self.http.calls[0][1].endswith("/last"))
        c = self.make(); board_offer(self.sdk)
        self.assertEqual(c.board(max_price_usdc="0.05").payment["amount_usdc"], "0.05")
        c = self.make(); self.assert_no_payment(lambda: c.risk(MINT, max_price_usdc="0.0000001"))
        self.assertEqual(len(self.http.calls), 0)

    def test_board_normalizes_every_filter_and_sends_one_identical_paid_retry(self):
        filters = {"max_risk": "0.20", "min_risk": 0.01, "min_liquidity_sol": 100,
                   "min_age_min": 60, "max_age_min": 1440, "min_trades_5": 5, "min_confidence": "medium"}
        c = self.make(); board_offer(self.sdk, n=37, limit=100, sort="liquidity", **filters)
        answer = c.board(limit=100, sort="liquidity", max_price_usdc="0.5", **filters)
        self.assertEqual(answer.payment["amount_usdc"], "0.185")
        self.assertEqual(self.http.calls[0][1], self.http.calls[1][1])
        query = parse_qs(urlparse(self.http.calls[0][1]).query)
        self.assertEqual(query, {"limit": ["100"], "sort": ["liquidity"], "max_risk": ["0.2"],
            "min_risk": ["0.01"], "min_liquidity_sol": ["100.0"], "min_age_min": ["60.0"],
            "max_age_min": ["1440.0"], "min_trades_5": ["5"], "min_confidence": ["medium"]})
        self.assertEqual(self.sdk.signs, 1)

    def test_board_sort_and_filter_validation_precedes_http_and_key_access(self):
        bad = [{"limit": v} for v in (9, 501, True, 10.0, "50")]
        bad += [{"sort": v} for v in ("lowest", "highest", None, [])]
        bad += [{name: value} for name, value in (("page", 1), ("cursor", "anything"), ("n", 50),
            ("min_risk", -0.01), ("max_risk", 1.01), ("max_risk", "NaN"), ("min_age_min", -1),
            ("max_age_min", float("inf")), ("min_liquidity_sol", True), ("min_liquidity_sol", None),
            ("min_trades_5", 1.5), ("min_trades_5", -1), ("min_trades_5", True),
            ("min_confidence", "experimental"), ("min_confidence", None))]
        bad += [{"min_risk": 0.5, "max_risk": 0.2}, {"min_age_min": 120, "max_age_min": 60}]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs):
                c = self.make()
                self.assert_no_payment(lambda: c.board(**kwargs))
                self.assertEqual(self.http.calls, [])
                self.assertEqual(self.sdk.loads, 0)
        for sort in client.BOARD_SORTS:
            c = self.make([Response(200, body={"rows": []})])
            self.assertFalse(c.board(limit=10, sort=sort, max_risk=1, min_risk=0, min_age_min=0,
                                     min_liquidity_sol=0, max_age_min=0, min_trades_5=0,
                                     min_confidence="low").paid)

    def test_board_dynamic_max_price_and_board_only_cap(self):
        c = self.make(); board_offer(self.sdk, n=37, limit=100)
        self.assert_no_payment(lambda: c.board(limit=100, max_price_usdc="0.184999"))
        self.assertEqual(self.sdk.loads, 0)
        c = self.make(); board_offer(self.sdk, n=37, limit=100)
        self.assertEqual(c.board(limit=100, max_price_usdc=".185").payment["amount_usdc"], "0.185")
        c = self.make(); board_offer(self.sdk, n=500, limit=500)
        self.assertEqual(c.board(limit=500, max_price_usdc="2.500000").payment["amount_usdc"], "2.5")
        self.assertEqual(self.sdk.spend_caps, ["$2.5", "$1"])
        for cap in ("2.500001", "0.0000001", "NaN"):
            c = self.make(); board_offer(self.sdk)
            self.assert_no_payment(lambda: c.board(max_price_usdc=cap))
            self.assertEqual(self.http.calls, [])
        for method in ("risk", "last"):
            c = self.make()
            self.assert_no_payment(lambda: getattr(c, method)(MINT, max_price_usdc="1.000001"))
            self.assertEqual(self.http.calls, [])
        c = self.make(); board_offer(self.sdk)
        self.assert_no_payment(lambda: c.board())  # Default 0.01 never becomes permission for more.

    def test_board_offer_count_query_and_arithmetic_checked_before_key_loading(self):
        mutations = [lambda x: x.pop("hodloddsBoard"), lambda x: x.pop("memo"),
            lambda x: x.update(memo="unbound-query"),
            lambda x: x.pop("hodloddsBoardSignature"), lambda x: x.update(hodloddsBoardSignature="bad"),
            lambda x: x["hodloddsBoard"].update(nonce="short"),
            lambda x: x["hodloddsBoard"].update(version=2),
            lambda x: x["hodloddsBoard"].update(unrecognized=True),
            lambda x: x["hodloddsBoard"].update(priced_n=True),
            lambda x: x["hodloddsBoard"].update(priced_n=0),
            lambda x: x["hodloddsBoard"].update(priced_n=51),
            lambda x: x["hodloddsBoard"].update(priced_n=11),
            lambda x: x["hodloddsBoard"]["query"].update(limit=100),
            lambda x: x["hodloddsBoard"]["query"].update(limit=50.0),
            lambda x: x["hodloddsBoard"]["query"].update(sort="riskiest"),
            lambda x: x["hodloddsBoard"]["query"].update(filters={"min_risk": 0.5})]
        for mutate in mutations:
            c = self.make(); board_offer(self.sdk)
            mutate(self.sdk.offer["accepts"][0]["extra"])
            self.assert_no_payment(lambda: c.board(max_price_usdc=".5"))
            self.assertEqual(self.sdk.loads, 0)
        c = self.make(); board_offer(self.sdk)
        self.sdk.offer["accepts"][0]["amount"] = "50001"
        self.assert_no_payment(lambda: c.board(max_price_usdc=".5"))

    def test_board_empty_is_free_and_changed_matches_is_not_retried(self):
        c = self.make([Response(200, body={"schema": "hodlodds.board.v1", "rows": [], "returned": 0})])
        answer = c.board()
        self.assertFalse(answer.paid)
        self.assertEqual(answer.data["rows"], [])
        self.assertEqual(self.sdk.loads, 0)
        for status in (409, 503):
            c = self.make([challenge(), Response(status)])
            board_offer(self.sdk, n=2)
            self.assert_uncertain_once(lambda: c.board(), status)


class ConfigTests(unittest.TestCase):
    def test_missing_or_changed_sdk_network_constants_fail_closed(self):
        with patch.object(client, "version", return_value="wrong"):
            for network in client.NETWORKS:
                with self.assertRaisesRegex(client.HodlOddsError, "constants unavailable"):
                    REAL_NETWORK_CONFIG(network)

    def test_exact_decimal_parsing_and_rejection_without_rounding(self):
        for val, expected in ((".01", 10000), ("1", 1000000), ("1.000000", 1000000),
                              (".000001", 1), ("1e-6", 1), ("0.0100000", 10000)):
            self.assertEqual(client._usdc(val, "price"), expected)
        for value in ("0.0000001", "1.000001", "NaN", "sNaN", "Infinity", "-1", "0", True, None,
                      "0.0100000000000000000000000000001", "1e-999999999", "1e999999999", "1" * 129):
            with self.subTest(value=value), self.assertRaises(client.HodlOddsError):
                client._usdc(value, "price")

    def test_url_guards_base_and_rpc(self):
        for url in ("http://example.com", "https:///nohost", "https://user:password@example.com",
                    "https://example.com/#fragment", "https://example.com:99999", "http://127.0.0.1.example.com",
                    "https://example.com\n", "https://example.com\\@127.0.0.1"):
            for rpc in (False, True):
                with self.subTest(url=url, rpc=rpc), self.assertRaises(client.HodlOddsError):
                    client._base_url(url, rpc=rpc)
        for url in ("http://127.0.0.1:8010", "http://localhost:8010", "http://[::1]:8010", "https://example.com"):
            self.assertEqual(client._base_url(url), url)
        rpc = "https://example.com/?api-key=fixture/"
        self.assertEqual(client._base_url(rpc, rpc=True), rpc)

    def test_mainnet_missing_pin_and_payment_header_injection_are_refused(self):
        with patch.object(client, "_new_http") as new:
            with self.assertRaisesRegex(client.HodlOddsError, "mainnet"):
                client.HodlOdds("https://example.com", network="mainnet")
            for name in ("PAYMENT-SIGNATURE", "payment-required", "X-Payment", "Host"):
                with self.assertRaises(client.HodlOddsError):
                    client.HodlOdds("https://example.com", access_headers={name: "secret"})
            new.assert_not_called()

    def test_http_factory_disables_redirects_and_environment_proxies(self):
        seen = []
        with patch.dict(sys.modules, {"httpx": SimpleNamespace(Client=lambda **kw: seen.append(kw))}):
            client._new_http(30, {})
        self.assertIs(seen[0]["follow_redirects"], False)
        self.assertIs(seen[0]["trust_env"], False)

    def test_key_file_permissions_symlink_size_shape_and_error_redaction(self):
        sentinel = "PRIVATE_MATERIAL_MUST_NEVER_APPEAR"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "key.json"
            p.write_text(json.dumps(list(range(64)))); p.chmod(0o600)
            fake = SimpleNamespace(Keypair=SimpleNamespace(from_bytes=lambda data: "fixture-key"))
            with patch.dict(sys.modules, {"solders": SimpleNamespace(), "solders.keypair": fake}):
                self.assertEqual(client._load_keypair(p), "fixture-key")
                for mode in (0o644, 0o400, 0o700):
                    p.chmod(mode)
                    with self.assertRaises(client.HodlOddsError):
                        client._load_keypair(p)
                p.chmod(0o600)
                link = Path(tmp) / "link"; link.symlink_to(p)
                for bad in (link, Path(tmp), Path(tmp) / "missing"):
                    with self.assertRaises(client.HodlOddsError):
                        client._load_keypair(bad)
                for raw in (sentinel, json.dumps([True] * 64), json.dumps([1] * 63), "x" * (client.MAX_KEY_BYTES + 1)):
                    p.write_text(raw)
                    with self.assertRaises(client.HodlOddsError) as result:
                        client._load_keypair(p)
                    self.assertNotIn(sentinel, str(result.exception))
                    self.assertIsNone(result.exception.__context__)
                p.write_text(json.dumps(list(range(64))))
                fake.Keypair.from_bytes = lambda data: (_ for _ in ()).throw(ValueError(sentinel))
                with self.assertRaises(client.HodlOddsError) as result:
                    client._load_keypair(p)
                self.assertNotIn(sentinel, repr(result.exception))
                self.assertIsNone(result.exception.__context__)

    def test_evm_key_file_formats_permissions_and_redaction(self):
        secret = "1" * 64
        fake = SimpleNamespace(Account=SimpleNamespace(from_key=lambda key: ("account", len(key))))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"eth_account": fake}):
            p = Path(tmp) / "evm-key"; p.touch(mode=0o600); p.chmod(0o600)
            for value in (secret, "0x" + secret, json.dumps("0x" + secret), json.dumps({"private_key": "0x" + secret}), json.dumps([1] * 32)):
                p.write_text(value)
                self.assertEqual(client._load_evm_account(p), ("account", 32))
            for value in (json.dumps([True] * 32), json.dumps([1] * 31), json.dumps({"private_key": secret, "extra": "x"}), secret[:-1], "not a key"):
                p.write_text(value)
                with self.assertRaises(client.HodlOddsError) as caught:
                    client._load_evm_account(p)
                self.assertIsNone(caught.exception.__context__)
                self.assertNotIn(value, str(caught.exception))
            p.write_text(secret); p.chmod(0o644)
            with self.assertRaises(client.HodlOddsError):
                client._load_evm_account(p)
            p.chmod(0o600)
            link = Path(tmp) / "symlink"; link.symlink_to(p)
            with self.assertRaises(client.HodlOddsError):
                client._load_evm_account(link)
            fake.Account.from_key = lambda _: (_ for _ in ()).throw(ValueError(secret))
            with self.assertRaises(client.HodlOddsError) as caught:
                client._load_evm_account(p)
            self.assertNotIn(secret, repr(caught.exception)); self.assertIsNone(caught.exception.__context__)


class MultiNetworkTests(unittest.TestCase):
    def make(self, network="base-sepolia", responses=None):
        self.sdk = FakeSDK(); net = FIXTURE_NETWORKS[network]
        self.http = FakeHTTP(responses or [challenge(), Response(200, {"PAYMENT-RESPONSE": "receipt"})])
        extra = {"paymentFlow": "authorization", "name": net["name"], "version": net["version"]}
        self.evm = {"scheme": "exact", "network": net["caip2"], "asset": net["usdc"], "amount": "10000",
                    "payTo": BASE_PAYTO, "maxTimeoutSeconds": 60, "extra": extra}
        self.sdk.offer["accepts"].append(self.evm)
        self.sdk.payer = BASE_PAYER
        self.sdk.receipt.update(network=net["caip2"], transaction=BASE_TX, payer=BASE_PAYER)
        with patch.object(client, "_new_http", return_value=self.http), \
                patch.object(client, "_evm_address", side_effect=lambda value: value in (BASE_PAYTO, BASE_PAYER)):
            self.c = client.HodlOdds("http://127.0.0.1:8010", network=network, pay_to=BASE_PAYTO)
        self.c._protocol = self.sdk.protocol; self.c._sdk_client = self.sdk.load
        return self.c

    def refusal(self, fn):
        with self.assertRaises(client.HodlOddsError):
            fn()
        self.assertEqual(self.sdk.loads, 0); self.assertEqual(self.sdk.signs, 0)

    def test_selects_only_matching_option_for_each_network(self):
        for network in ("base", "base-sepolia"):
            c = self.make(network)
            result = c.risk(MINT)
            self.assertTrue(result.paid)
            self.assertEqual(self.sdk.signed_offer["accepts"], [self.evm])
            self.assertEqual(result.payment["transaction"], BASE_TX)
            self.assertIn("basescan.org", result.payment["explorer"])
        c = self.make()
        self.sdk.offer["accepts"].reverse()  # Solana still chooses its own offer in second place.
        with patch.object(client, "_new_http", return_value=self.http):
            solana = client.HodlOdds("http://127.0.0.1:8010", network="devnet", pay_to=PAYTO)
        solana._protocol = self.sdk.protocol; solana._sdk_client = self.sdk.load
        self.sdk.payer = PAYER; self.sdk.receipt = settlement()
        self.assertTrue(solana.risk(MINT).paid)
        self.assertEqual(self.sdk.signed_offer["accepts"], [self.sdk.offer["accepts"][1]])

    def test_every_base_offer_check_precedes_key_loading_or_signing(self):
        changes = [("network", "eip155:1"), ("payTo", BASE_PAYER), ("asset", BASE_PAYER), ("scheme", "upto"),
                   ("amount", "10001"), ("amount", "0"), ("maxTimeoutSeconds", 301)]
        for name, value in changes:
            c = self.make(); self.evm[name] = value
            self.refusal(lambda: c.risk(MINT))
        for name, value in (("assetTransferMethod", "permit2"), ("paymentFlow", "upfront"), ("name", "wrong domain"),
                            ("version", "1"), ("memo", "ignored?"), ("feePayer", FEE), ("recentBlockhash", FEE)):
            c = self.make(); self.evm["extra"][name] = value
            self.refusal(lambda: c.risk(MINT))
        c = self.make(); self.sdk.offer["accepts"].append(copy.deepcopy(self.evm))
        self.refusal(lambda: c.risk(MINT))

    def test_base_board_uses_hmac_extra_without_svm_memo(self):
        c = self.make(); helper = FakeSDK(); board_offer(helper, n=500, limit=500)
        bound = helper.offer["accepts"][0]
        self.evm["amount"] = "2500000"; self.sdk.receipt["amount"] = "2500000"
        self.evm["extra"].update({k: v for k, v in bound["extra"].items() if k.startswith("hodlodds")})
        answer = c.board(limit=500, max_price_usdc="2.5")
        self.assertEqual(answer.payment["amount_usdc"], "2.5")
        self.assertNotIn("memo", self.sdk.signed_offer["accepts"][0]["extra"])
        self.assertEqual(self.sdk.spend_caps, ["$2.5", "$1"])
        for field in ("hodloddsBoard", "hodloddsBoardSignature"):
            c = self.make(); self.evm["extra"].update({k: v for k, v in bound["extra"].items() if k.startswith("hodlodds")})
            self.evm["amount"] = "2500000"; self.evm["extra"].pop(field)
            self.refusal(lambda: c.board(limit=500, max_price_usdc="2.5"))

    def test_base_dynamic_cap_and_single_route_cap(self):
        c = self.make()
        self.refusal(lambda: c.risk(MINT, max_price_usdc="1.000001"))
        c = self.make()
        self.refusal(lambda: c.board(limit=500, max_price_usdc="2.500001"))
        c = self.make(); helper = FakeSDK(); board_offer(helper, n=100, limit=100)
        self.evm["amount"] = "500000"
        self.evm["extra"].update({k: v for k, v in helper.offer["accepts"][0]["extra"].items() if k.startswith("hodlodds")})
        self.refusal(lambda: c.board(limit=100, max_price_usdc=".499999"))

    def test_base_200_without_receipt_and_paid_errors_never_retry(self):
        for status, receipt in ((200, False), (409, False), (503, False), (409, True)):
            c = self.make(responses=[challenge(), Response(status, {"PAYMENT-RESPONSE": "receipt"} if receipt else {})])
            with self.assertRaises(client.HodlOddsError) as caught:
                c.risk(MINT)
            self.assertEqual(len(self.http.calls), 2); self.assertEqual(self.sdk.signs, 1)
            self.assertIn("Reconcile", str(caught.exception)); self.assertIn("base-sepolia", str(caught.exception))
            self.assertEqual(caught.exception.payment["transaction"], BASE_TX if receipt else None)

    def test_base_mainnet_pin_explicit_and_checksum_enforced(self):
        with patch.object(client, "_new_http") as http, patch.object(client, "_evm_address", return_value=False):
            for recipient in (None, BASE_PAYTO.lower(), "0x" + "0" * 40):
                with self.assertRaises(client.HodlOddsError):
                    client.HodlOdds("https://example.com", network="base", pay_to=recipient)
            http.assert_not_called()


class CLITests(unittest.TestCase):
    def setUp(self):
        # An ambiguous paid CLI call saves a pending file; never into the real
        # ~/.config/hodlodds/pending (reviewer, 2026-10-18: runs leaked there).
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        original = client.save_pending
        saver = patch.object(client, "save_pending",
                             lambda payment, directory=None: original(payment, Path(tmp.name).resolve() / "pending"))
        saver.start(); self.addCleanup(saver.stop)

    def run_cli(self, args, responses, sdk=None, env=None):
        http, sdk = FakeHTTP(responses), sdk or FakeSDK()
        out, err = io.StringIO(), io.StringIO()
        with patch.object(client, "_new_http", return_value=http), patch.object(client.HodlOdds, "_protocol", side_effect=sdk.protocol), \
                patch.object(client.HodlOdds, "_sdk_client", side_effect=sdk.load), \
                patch.dict(os.environ, env or {}, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = client.main(args)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue(), http, sdk

    def test_cli_success_free_error_paid_ambiguity_and_usage_codes(self):
        code, _, _, http, sdk = self.run_cli(["status"], [Response(200)])
        self.assertEqual(code, 0); self.assertTrue(http.closed); self.assertEqual(sdk.signs, 0)
        code, _, err, _, sdk = self.run_cli(["risk", MINT], [Response(503)])
        self.assertEqual(code, 1); self.assertIn("503", err); self.assertEqual(sdk.signs, 0)
        args = ["risk", MINT, "--pay-to", PAYTO]
        code, _, err, _, sdk = self.run_cli(args, [challenge(), Response(200)])
        self.assertEqual(code, 1); self.assertIn("Reconcile", err); self.assertEqual(sdk.signs, 1)
        code, out, _, _, sdk = self.run_cli(args, [challenge(), Response(200, {"PAYMENT-RESPONSE": "receipt"})])
        self.assertEqual(code, 0); self.assertIn("Paid 0.01", out); self.assertEqual(sdk.signs, 1)
        for args in (["risk"], ["status", MINT], ["unknown"]):
            self.assertEqual(self.run_cli(args, [])[0], 2)

    def test_mainnet_requires_flags_not_shell_wrapper_or_environment_defaults(self):
        env = {"HODLODDS_NETWORK": "mainnet", "HODLODDS_PAY_TO": PAYTO}
        for args in (["status"], ["status", "--network", "mainnet"], ["status", "--pay-to", PAYTO]):
            code, _, err, http, _ = self.run_cli(args, [], env=env)
            self.assertEqual(code, 2); self.assertIn("explicit", err); self.assertEqual(http.calls, [])
        args = ["status", "--network=mainnet", "--pay-to=" + PAYTO]
        self.assertEqual(self.run_cli(args, [Response(200)])[0], 0)

    def test_base_cli_requires_explicit_mainnet_pin_and_supports_testnet(self):
        for network in ("base", "solana"):
            code, _, _, http, _ = self.run_cli(["status"], [], env={"HODLODDS_NETWORK": network, "HODLODDS_PAY_TO": BASE_PAYTO})
            self.assertEqual(code, 2); self.assertEqual(http.calls, [])
        with patch.object(client, "_evm_address", return_value=True):
            code, _, err, _, _ = self.run_cli(["status", "--network", "base-sepolia", "--pay-to", BASE_PAYTO], [Response(200)])
        self.assertEqual(code, 0, err)

    def test_cli_invalid_amount_and_transport_errors_are_not_tracebacks(self):
        for args, replies in ((["status", "--max-price", "NaN"], []), (["status"], [OSError("DO_NOT_PRINT")])):
            code, _, err, _, sdk = self.run_cli(args, replies)
            self.assertEqual(code, 1); self.assertNotIn("Traceback", err); self.assertNotIn("DO_NOT_PRINT", err)
            self.assertEqual(sdk.signs, 0)

    def test_cli_board_dynamic_total_and_all_filter_flags(self):
        sdk = FakeSDK()
        board_offer(sdk, n=37, limit=100, sort="active", max_risk=.5, min_risk=.01,
                    min_liquidity_sol=100, min_age_min=60, max_age_min=1440, min_trades_5=2, min_confidence="high")
        args = ["board", "--pay-to", PAYTO, "--limit", "100", "--sort", "active", "--max-price", "0.5",
                "--max-risk", ".5", "--min-risk", ".01", "--min-liquidity", "100", "--min-age", "60",
                "--max-age", "1440", "--min-trades", "2", "--min-confidence", "high"]
        code, out, err, http, sdk = self.run_cli(args, [challenge(), Response(200, {"PAYMENT-RESPONSE": "receipt"})], sdk)
        self.assertEqual(code, 0, err)
        self.assertIn("Paid 0.185", out)
        self.assertIn("min_confidence=high", http.calls[0][1])
        sdk = FakeSDK(); board_offer(sdk, n=500, limit=500)
        code, out, err, _, _ = self.run_cli(["board", "--pay-to", PAYTO, "--limit", "500", "--max-price", "2.5"],
                                          [challenge(), Response(200, {"PAYMENT-RESPONSE": "receipt"})], sdk)
        self.assertEqual(code, 0, err)
        self.assertIn("Paid 2.5", out)

    def test_cli_board_only_flags_and_invalid_bounds_never_sign(self):
        for args, expected in ((["risk", MINT, "--limit", "10"], 2),
                               (["board", "--limit", "9"], 1), (["board", "--limit", "501"], 1),
                               (["board", "--min-trades", "1.5"], 2),
                               (["board", "--sort", "lowest"], 2),
                               (["board", "--max-price", "2.500001"], 1),
                               (["risk", MINT, "--max-price", "2.5"], 1)):
            code, _, err, http, sdk = self.run_cli(args, [])
            self.assertEqual(code, expected, err)
            self.assertEqual(http.calls, [])
            self.assertEqual(sdk.signs, 0)


class SDKIntegrationTests(unittest.TestCase):
    def test_real_evm_sdk_signs_only_selected_base_board_offline(self):
        try:
            from eth_account import Account
            from eth_account.messages import encode_typed_data
            from x402.http import decode_payment_signature_header, encode_payment_required_header, encode_payment_response_header
            from x402.schemas import PaymentRequired, SettleResponse
            from x402.mechanisms.evm.eip712 import build_typed_data_for_signing
            from x402.mechanisms.evm.types import ExactEIP3009Payload
            from x402.mechanisms.evm.signers import EthAccountSigner
        except ImportError:
            self.skipTest("install x402[svm,evm]==2.25.0 for the real-EVM offline signing test")
        payer = Account.create(); net = REAL_NETWORK_CONFIG("base-sepolia")
        helper = FakeSDK(); board_offer(helper, n=500, limit=500)
        selected = dict(helper.offer["accepts"][0], network=net["caip2"], asset=net["usdc"], payTo=BASE_PAYTO)
        selected["extra"] = {k: v for k, v in selected["extra"].items() if k.startswith("hodlodds")}
        selected["extra"].update(paymentFlow="authorization", name=net["name"], version=net["version"])
        requirement = offer(); requirement["accepts"].append(selected)
        receipt = {"success": True, "network": net["caip2"], "transaction": BASE_TX, "payer": payer.address, "amount": "2500000"}
        transport = FakeHTTP([Response(402, {"PAYMENT-REQUIRED": encode_payment_required_header(PaymentRequired.model_validate(requirement))}),
                              Response(200, {"PAYMENT-RESPONSE": encode_payment_response_header(SettleResponse.model_validate(receipt))})])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "throwaway.json"
            path.write_text(json.dumps({"private_key": "0x" + bytes(payer.key).hex()})); path.chmod(0o600)
            with patch.object(client, "_new_http", return_value=transport), patch.object(client, "_network_config", side_effect=REAL_NETWORK_CONFIG), \
                    patch("socket.create_connection", side_effect=AssertionError("unexpected RPC")):
                with client.HodlOdds("http://127.0.0.1:8010", network="base-sepolia", pay_to=BASE_PAYTO, keypair=path) as c:
                    answer = c.board(limit=500, max_price_usdc="2.5")
        self.assertTrue(answer.paid)
        payload = decode_payment_signature_header(transport.calls[1][2]["PAYMENT-SIGNATURE"])
        self.assertEqual(payload.accepted.network, net["caip2"])
        self.assertEqual(payload.accepted.extra["hodloddsBoard"], selected["extra"]["hodloddsBoard"])
        inner = ExactEIP3009Payload.from_dict(payload.payload)
        self.assertEqual(inner.authorization.value, "2500000"); self.assertEqual(inner.authorization.to, BASE_PAYTO)
        domain, types, _, message = build_typed_data_for_signing(inner.authorization, int(net["caip2"].split(":")[1]), net["usdc"], net["name"], net["version"])
        domain = {"name": domain.name, "version": domain.version, "chainId": domain.chain_id, "verifyingContract": domain.verifying_contract}
        signed = encode_typed_data(domain_data=domain, message_types=types, message_data=message)
        self.assertEqual(Account.recover_message(signed, signature=inner.signature), payer.address)

    def test_pinned_sdk_codec_and_signature_with_all_rpc_mocked(self):
        self.real_sdk_payment()

    def test_pinned_sdk_preserves_board_offer_binding_with_all_rpc_mocked(self):
        self.real_sdk_payment(board=True)

    def real_sdk_payment(self, board=False):
        try:
            import base64
            from solders.keypair import Keypair
            from solders.hash import Hash
            from solders.pubkey import Pubkey
            from solders.transaction import VersionedTransaction
            from x402.http import decode_payment_signature_header, encode_payment_required_header, encode_payment_response_header
            from x402.schemas import PaymentRequired, SettleResponse
            from x402.mechanisms.svm.exact.client import ExactSvmScheme
        except ImportError:
            self.skipTest("install x402[svm]==2.25.0 for the real-SDK offline check")
        payer, fee, recipient = Keypair(), Keypair(), Keypair()
        requirement = offer()
        requirement["accepts"][0].update(payTo=str(recipient.pubkey()))
        requirement["accepts"][0]["extra"]["feePayer"] = str(fee.pubkey())
        receipt = settlement(); receipt.update(payer=str(payer.pubkey()))
        if board:
            requirement["accepts"][0]["amount"] = receipt["amount"] = "2500000"
            requirement["accepts"][0]["extra"]["hodloddsBoard"] = {
                "version": 1, "query": client._bound_query(client._board_query(500, "lowest_risk", {"min_liquidity_sol": 100})),
                "priced_n": 500, "nonce": "a" * 32}
            requirement["accepts"][0]["extra"]["hodloddsBoardSignature"] = "b" * 64
            requirement["accepts"][0]["extra"]["memo"] = board_memo(requirement["accepts"][0]["extra"]["hodloddsBoard"])
        transport = FakeHTTP([
            Response(402, {"PAYMENT-REQUIRED": encode_payment_required_header(PaymentRequired.model_validate(requirement))}),
            Response(200, {"PAYMENT-RESPONSE": encode_payment_response_header(SettleResponse.model_validate(receipt))})])
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "throwaway.json"
            key.write_text(json.dumps(list(bytes(payer)))); key.chmod(0o600)
            with patch.object(client, "_new_http", return_value=transport), patch.object(client, "_network_config", side_effect=REAL_NETWORK_CONFIG), \
                    patch.object(ExactSvmScheme, "_get_client", return_value=object()), \
                    patch("x402.mechanisms.svm.exact.client.get_cached_mint_metadata",
                          return_value=SimpleNamespace(token_program=Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"), decimals=6)), \
                    patch("x402.mechanisms.svm.exact.client.resolve_blockhash", return_value=Hash.default()):
                with client.HodlOdds("http://127.0.0.1:8010", keypair=key, pay_to=str(recipient.pubkey())) as c:
                    answer = c.board(limit=500, min_liquidity_sol=100, max_price_usdc="2.5") if board else c.risk(MINT)
        self.assertTrue(answer.paid)
        self.assertEqual(len(transport.calls), 2)
        payload = decode_payment_signature_header(transport.calls[1][2]["PAYMENT-SIGNATURE"])
        tx = VersionedTransaction.from_bytes(base64.b64decode(payload.payload["transaction"]))
        self.assertEqual(payload.accepted.amount, "2500000" if board else "10000")
        if board:
            self.assertEqual(payload.accepted.extra["hodloddsBoard"], requirement["accepts"][0]["extra"]["hodloddsBoard"])
        self.assertTrue(tx.signatures[1].verify(payer.pubkey(), bytes([128]) + bytes(tx.message)))


if __name__ == "__main__":
    unittest.main()
