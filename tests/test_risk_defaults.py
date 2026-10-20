"""Public 0.3.1 contract: raw query binding, additive answers and unsigned refresh."""
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_client as f
import test_watch as w

client = f.client


def changed(headers=None):
    return f.Response(409, headers or {}, {
        "error": "matches changed; request again", "code": "offer_changed", "charged": False})


class RiskDefaultsTests(w.OfflineWatch, unittest.TestCase):
    def test_explicit_filters_bind_without_implicit_defaults(self):
        for sort in ("lowest_risk", "safest", "riskiest"):
            for include in (False, True):
                with self.subTest(sort=sort, include=include):
                    c = self.make()
                    f.board_offer(self.sdk, n=10, sort=sort, min_trades_5=0, include_crashed=include)
                    c.board(sort=sort, min_trades_5=0, include_crashed=include, max_price_usdc=".05")
                    query = parse_qs(urlsplit(self.http.calls[0][1]).query)
                    self.assertEqual(query["include_crashed"], [str(include).lower()])
                    self.assertEqual(query["min_trades_5"], ["0"])
                    binding = self.sdk.signed_offer["accepts"][0]["extra"]["hodloddsBoard"]
                    self.assertEqual(set(binding), {"version", "query", "priced_n", "nonce"})
                    self.assertEqual(binding["query"]["filters"], {
                        "min_trades_5": 0, "include_crashed": include})
        # Golden canonical binding from the server with apply_defaults=False.
        query = client._bound_query(client._board_query(None, "lowest_risk", {}))
        self.assertEqual(json.dumps(query, sort_keys=True, separators=(",", ":")),
                         '{"filters":{},"limit":50,"sort":"lowest_risk"}')

    def test_watchlist_binding_has_no_defaults(self):
        c = self.make()
        f.board_offer(self.sdk, n=1, limit=None, mints=[f.MINT])
        c.board(mints=[f.MINT])
        self.assertEqual(self.sdk.signed_offer["accepts"][0]["extra"]["hodloddsBoard"]["query"]["filters"], {})
        self.assertNotIn("include_crashed", self.http.calls[0][1])
        self.assertNotIn("min_trades_5", self.http.calls[0][1])

    def test_include_crashed_rejects_non_booleans_before_http(self):
        for value in ("true", "false", 0, 1, None, [], {}):
            c = self.make()
            with self.subTest(value=value), self.assertRaisesRegex(client.HodlOddsError, "boolean"):
                c.board(include_crashed=value)
            self.assertEqual(self.http.calls, [])
            self.assertEqual(self.sdk.loads, 0)

    def test_new_row_and_top_level_fields_pass_through(self):
        row = {"mint": f.MINT, "p_crash_60m": .002, "dd_60": -.996,
               "already_crashed": True, "note": "This token already fell 90% or more within the last hour."}
        body = {"rows": [row], "filters": {"min_trades_5": 3, "include_crashed": False},
                "defaults_applied": {"min_trades_5": 3, "include_crashed": False, "reason": "example"},
                "total_matches": 10}
        c = self.make([f.challenge(), f.Response(200, {"PAYMENT-RESPONSE": "receipt"}, body)])
        f.board_offer(self.sdk)
        answer = c.board(max_price_usdc=".05")
        self.assertEqual(answer.data, body)

    def test_unsigned_offer_changed_refreshes_once_then_checks_and_signs(self):
        for headers in ({}, {"HodlOdds-Charged": "no"}):
            c = self.make([changed(headers), f.challenge(), w.paid(rows=[])])
            f.board_offer(self.sdk, include_crashed=True, min_trades_5=0)
            answer = c.board(include_crashed=True, min_trades_5=0, max_price_usdc=".05")
            self.assertTrue(answer.paid)
            self.assertEqual(self.sdk.signs, 1)
            self.assertEqual(len(self.http.calls), 3)
            self.assertEqual(self.http.calls[0][1], self.http.calls[1][1])
            self.assertEqual(self.http.calls[1][1], self.http.calls[2][1])
            self.assertEqual([bool(h) for _, _, h in self.http.calls], [False, False, True])

    def test_unsigned_offer_refresh_has_a_hard_one_retry_limit(self):
        c = self.make([changed(), changed(), f.challenge()])
        with self.assertRaises(client.HodlOddsError) as caught:
            c.board()
        self.assertEqual(caught.exception.status, 409)
        self.assertIs(caught.exception.payment["charged"], False)
        self.assertEqual(self.sdk.loads, 0)
        self.assertEqual(self.sdk.signs, 0)
        self.assertEqual(len(self.http.calls), 2)

    def test_other_codes_routes_and_conflicting_receipts_never_refresh(self):
        for response in (f.Response(409, body={"code": "replay"}),
                         changed({"PAYMENT-RESPONSE": "receipt", "HodlOdds-Charged": "no"}),
                         changed({"HodlOdds-Charged": "yes"}),
                         f.Response(503, body={"code": "offer_changed"})):
            c = self.make([response])
            with self.assertRaises(client.HodlOddsError):
                c.board()
            self.assertEqual(len(self.http.calls), 1)
            self.assertEqual(self.sdk.signs, 0)
        for method in ("risk", "last"):
            c = self.make([changed()])
            with self.assertRaises(client.HodlOddsError):
                getattr(c, method)(f.MINT)
            self.assertEqual(len(self.http.calls), 1)

    def test_refreshed_offer_still_checks_max_price_and_binding(self):
        for tamper in ("price", "binding"):
            c = self.make([changed(), f.challenge()])
            f.board_offer(self.sdk)
            if tamper == "binding":
                self.sdk.offer["accepts"][0]["extra"]["hodloddsBoard"]["query"]["filters"] = {"min_trades_5": 3}
            with self.assertRaises(client.HodlOddsError):
                c.board(max_price_usdc=".049999" if tamper == "price" else ".05")
            self.assertEqual(self.sdk.signs, 0)
            self.assertEqual(self.sdk.loads, 0)
            self.assertEqual(len(self.http.calls), 2)

    def test_signed_offer_changed_never_retries_even_if_marked_free(self):
        for headers in ({}, {"HodlOdds-Charged": "no"},
                        {"HodlOdds-Charged": "no", "PAYMENT-RESPONSE": "receipt"}):
            c = self.make([f.challenge(), changed(headers)])
            f.board_offer(self.sdk)
            with self.assertRaises(client.HodlOddsError) as caught:
                c.board(max_price_usdc=".05")
            self.assertEqual(len(self.http.calls), 2)
            self.assertEqual(self.sdk.signs, 1)
            if headers == {"HodlOdds-Charged": "no"}:
                self.assertIs(caught.exception.payment["charged"], False)
            else:
                self.assertIsInstance(caught.exception.payment, client.PendingPayment)

    def test_watch_refresh_budget_and_call_count(self):
        c = self.make([w.status(), changed(), f.challenge(), w.paid(rows=[])])
        f.board_offer(self.sdk)
        watching = c.watch_board(max_spend_usdc=".05", max_calls=1, max_price_usdc=".05")
        result = list(watching)
        self.assertEqual(len(result), 1)
        self.assertEqual(watching.calls, 1)
        self.assertEqual(watching.spent_atomic, 50000)
        self.assertEqual(self.sdk.signs, 1)

    def test_watch_refreshed_offer_cannot_exceed_session_budget(self):
        c = self.make([w.status(), changed(), f.challenge()])
        f.board_offer(self.sdk)
        watching = c.watch_board(max_spend_usdc=".049999", max_price_usdc=".05")
        self.assertEqual(list(watching)[0].watch["skipped_reason"], "spending cap reached")
        self.assertEqual(watching.spent_atomic, 0)
        self.assertEqual(self.sdk.loads, 0)

    def test_watch_repeated_unsigned_changes_skip_without_reserving_spend(self):
        c = self.make([w.status(100), changed(), changed(), w.status(160), f.challenge(), w.paid(160, rows=[])])
        f.board_offer(self.sdk)
        watching = c.watch_board(max_spend_usdc=".05", max_price_usdc=".05", max_calls=2)
        skipped, answer = list(watching)
        self.assertFalse(skipped.paid)
        self.assertEqual(skipped.watch["spent_usdc"], "0")
        self.assertTrue(answer.paid)
        self.assertEqual(watching.spent_atomic, 50000)
        self.assertEqual(self.sdk.signs, 1)
        self.assertEqual(watching.calls, 2)

    def test_last_keeps_labels_and_lower_price(self):
        body = {"stale": True, "age_s": 900, "freshness": "stale",
                "row": {"already_crashed": True, "note": "Already fell 90%."}}
        c = self.make([f.challenge(), f.Response(200, {"PAYMENT-RESPONSE": "receipt"}, body)])
        self.sdk.offer["accepts"][0]["amount"] = self.sdk.receipt["amount"] = "2000"
        answer = c.last(f.MINT, max_price_usdc=".002")
        self.assertEqual(answer.data, body)
        self.assertEqual(answer.payment["amount_usdc"], "0.002")
        self.assertTrue(self.http.calls[0][1].endswith("/last"))


class DefaultsCLITests(unittest.TestCase):
    run_cli = f.CLITests.run_cli

    def setUp(self):
        network = patch.object(client, "_network_config",
                               side_effect=lambda name: f.FIXTURE_NETWORKS[name])
        network.start()
        self.addCleanup(network.stop)

    def test_explicit_zero_and_crashed_flags_survive_cli(self):
        for flag, value in (("--include-crashed", "true"), ("--no-include-crashed", "false")):
            code, _, err, http, sdk = self.run_cli(
                ["board", "--min-trades", "0", flag, "--sort", "safest"],
                [f.Response(200, body={"rows": []})])
            self.assertEqual(code, 0, err)
            query = parse_qs(urlsplit(http.calls[0][1]).query)
            self.assertEqual(query["min_trades_5"], ["0"])
            self.assertEqual(query["include_crashed"], [value])
            self.assertEqual(query["sort"], ["safest"])
            self.assertEqual(sdk.signs, 0)

    def test_board_flags_cannot_silently_affect_single_routes(self):
        for flag in ("--include-crashed", "--no-include-crashed"):
            code, _, _, http, sdk = self.run_cli(["risk", f.MINT, flag], [])
            self.assertNotEqual(code, 0)
            self.assertEqual(http.calls, [])
            self.assertEqual(sdk.signs, 0)
