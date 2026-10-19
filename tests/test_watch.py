"""Watch scheduling and money safety using an advancing clock and fake HTTP/SDK.

No sockets, real sleeps, key files, facilitator calls or money.
"""
import json
import socket
import unittest
from unittest.mock import patch

import test_client as f

client = f.client


class Clock:
    def __init__(self, now=3590):
        self.now, self.sleeps = now, []

    def sleep(self, seconds):
        assert seconds > 0
        self.sleeps.append(seconds)
        self.now += seconds


def status(as_of=3540, state="live"):
    return f.Response(200, body=dict(state=state, as_of=as_of, last_issued_at=as_of + 21, age_s=4))


def paid(as_of=3540, rows=None):
    row = dict(mint=f.MINT, p_crash_60m=.2, confidence={"level": "high"})
    body = {"as_of": as_of, "row": row} if rows is None else {"as_of": as_of, "rows": rows, "returned": len(rows)}
    return f.Response(200, {"PAYMENT-RESPONSE": "receipt"}, body)


class OfflineWatch:
    make = f.ClientTests.make

    def setUp(self):
        self.clock = Clock()
        for patcher in (
            patch.object(client, "_network_config", side_effect=lambda name: f.FIXTURE_NETWORKS[name]),
            patch.object(client.time, "time", side_effect=lambda: self.clock.now),
            patch.object(client.time, "sleep", side_effect=self.clock.sleep),
            patch.object(client.random, "uniform", return_value=0),
            patch.object(socket, "socket", side_effect=AssertionError("network forbidden")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class WatchTests(OfflineWatch, unittest.TestCase):
    def test_schedule_one_and_five_minutes_across_hour_boundary(self):
        for every, expected in ((1, [3625, 3685, 3745]), (5, [3625, 3925, 4225])):
            with self.subTest(every=every):
                self.clock.now = 3590  # 00:59:50 UTC
                c = self.make([status(state="down") for _ in expected])
                watch = c.watch(f.MINT, every, max_spend_usdc="1")
                wakes = []
                for _ in expected:
                    next(watch)
                    wakes.append(self.clock.now)
                watch.close()
                self.assertEqual(wakes, expected)
                self.assertEqual(self.sdk.signs, 0)

    def test_jitter_bounds_and_no_catchup_after_slow_consumer(self):
        for jitter in (0, 5):
            with patch.object(client.random, "uniform", return_value=jitter) as random:
                self.assertEqual(client._watch_wake(3590, 5), 3625 + jitter)
                random.assert_called_once_with(0, 5)
        c = self.make([status(state="stale"), status(state="stale")])
        watch = c.watch(f.MINT, 1, max_spend_usdc="1")
        next(watch)
        self.clock.now += 126
        next(watch)
        self.assertEqual(self.clock.now, 3805)  # missed slots are not replayed
        watch.close()

    def test_nonlive_states_never_buy(self):
        c = self.make([status(state=s) for s in ("stale", "down", "empty", "unknown")])
        watch = c.watch(f.MINT, max_spend_usdc="1")
        for cycle, state in enumerate(("stale", "down", "empty", "unknown"), 1):
            result = next(watch)
            self.assertEqual(result.watch["skipped_reason"], "feed " + state)
            self.assertEqual(result.watch["cycle"], cycle)
            self.assertFalse(result.paid)
        watch.close()
        self.assertEqual(self.sdk.signs, 0)
        self.assertEqual(self.sdk.loads, 0)
        self.assertTrue(all(url.endswith("/v1/status") for _, url, _ in self.http.calls))

    def test_same_minute_rechecked_once_and_new_minutes_paid_once(self):
        c = self.make([status(100), f.challenge(), paid(100), status(100), status(100),
                       status(160), f.challenge(), paid(160)])
        watch = c.watch(f.MINT, 1, max_spend_usdc="1", max_calls=2)
        results = list(watch)
        self.assertEqual([r.watch["skipped_reason"] for r in results], [None, "unchanged as_of", None])
        self.assertEqual([r.watch["spent_usdc"] for r in results], ["0.01", "0.01", "0.02"])
        self.assertEqual(self.clock.sleeps.count(10), 1)
        self.assertEqual(self.sdk.signs, 2)
        self.assertEqual(watch.calls, 2)
        self.assertEqual(watch.stop_reason, "max_calls reached")

    def test_new_minute_during_recheck_is_bought_in_same_cycle(self):
        c = self.make([status(100), f.challenge(), paid(100), status(100), status(160), f.challenge(), paid(160)])
        watch = c.watch(f.MINT, 1, max_spend_usdc="1", max_calls=2)
        self.assertEqual(len(list(watch)), 2)
        self.assertEqual(self.sdk.signs, 2)
        self.assertEqual(self.clock.now, 3695)

    def test_dedup_uses_paid_answer_as_of_and_rejects_older_status(self):
        c = self.make([status(100), f.challenge(), paid(160), status(160), status(160),
                       status(100), status(100)])
        watch = c.watch(f.MINT, 1, max_spend_usdc="1")
        self.assertTrue(next(watch).paid)
        self.assertEqual(next(watch).watch["skipped_reason"], "unchanged as_of")
        self.assertEqual(next(watch).watch["skipped_reason"], "older as_of")
        self.assertEqual(self.sdk.signs, 1)
        watch.close()

    def test_stop_on_stale_including_recheck(self):
        for responses, count, signs in (([status(state="stale")], 1, 0),
                ([status(100), f.challenge(), paid(100), status(100), status(100, "down")], 2, 1)):
            c = self.make(responses)
            watch = c.watch(f.MINT, max_spend_usdc="1", stop_on_stale=True)
            self.assertEqual(len(list(watch)), count)
            self.assertEqual(self.sdk.signs, signs)
            self.assertTrue(watch.stop_reason.startswith("feed "))

    def test_exact_hundred_payments_total_one_usdc(self):
        responses = []
        for minute in range(100):
            responses.extend([status(minute * 60), f.challenge(), paid(minute * 60)])
        c = self.make(responses)
        watch = c.watch(f.MINT, 1, max_spend_usdc="1.00")
        results = list(watch)
        self.assertEqual(len(results), 100)
        self.assertEqual(self.sdk.signs, 100)
        self.assertEqual(watch.spent_atomic, 1000000)
        self.assertEqual(results[-1].watch["spent_usdc"], "1")
        self.assertEqual(len(self.http.calls), 300)
        self.assertEqual(watch.stop_reason, "spending cap reached")

    def test_offer_over_remaining_cap_stops_before_key_or_signature(self):
        c = self.make([status(100), f.challenge(), paid(100), status(160), f.challenge()])
        watch = c.watch(f.MINT, max_spend_usdc=".019999")
        results = list(watch)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[-1].watch["skipped_reason"], "spending cap reached")
        self.assertEqual(watch.spent_atomic, 10000)
        self.assertEqual(self.sdk.signs, 1)
        self.assertEqual(self.sdk.loads, 1)
        c = self.make([status(), f.challenge()])
        self.assertEqual(len(list(c.watch(f.MINT, max_spend_usdc=".009999"))), 1)
        self.assertEqual(self.sdk.loads, 0)

    def test_spend_uses_accepted_offer_not_estimated_token_price(self):
        c = self.make([status(100), f.challenge(), paid(100), status(160), f.challenge(), paid(160)])
        self.sdk.offer["accepts"][0]["amount"] = self.sdk.receipt["amount"] = "6000"
        watch = c.watch(f.MINT, max_spend_usdc=".012")
        self.assertEqual([r.watch["spent_usdc"] for r in watch], ["0.006", "0.012"])
        self.assertEqual(self.sdk.signs, 2)

    def test_price_limit_and_offer_checks_still_run_before_cap_check(self):
        for change in (lambda s: s.offer["accepts"][0].update(amount="10001"),
                       lambda s: s.offer["accepts"][0].update(asset=f.MINT)):
            c = self.make([status(), f.challenge()])
            change(self.sdk)
            watch = c.watch(f.MINT, max_spend_usdc=".001")
            with self.assertRaises(client.HodlOddsError):
                next(watch)
            self.assertEqual(list(watch), [])
            self.assertEqual(self.sdk.loads, 0)
            self.assertEqual(watch.spent_atomic, 0)

    def test_ambiguous_and_non200_paid_responses_stop_permanently(self):
        for outcome in (OSError("secret"), f.Response(200),
                        *(f.Response(code) for code in (302, 402, 404, 409, 429, 500, 503))):
            with self.subTest(outcome=outcome):
                c = self.make([status(), f.challenge(), outcome])
                watch = c.watch(f.MINT, max_spend_usdc="1")
                with self.assertRaises(client.HodlOddsError) as caught:
                    next(watch)
                error = caught.exception
                self.assertIsInstance(error.payment, client.PendingPayment)
                self.assertIn("Reconcile", str(error))
                self.assertNotIn(self.sdk.encoded, str(error))
                self.assertEqual(error.payment["amount_usdc"], "0.01")
                self.assertEqual(watch.spent_atomic, 10000)
                self.assertEqual(list(watch), [])
                self.assertEqual(len(self.http.calls), 3)
                self.assertEqual(self.sdk.signs, 1)

    def test_free_status_or_route_error_stops_without_payment(self):
        for responses in ([f.Response(503)], [status(), f.Response(503)],
                          [status(), f.Response(404)]):
            c = self.make(responses)
            watch = c.watch(f.MINT, max_spend_usdc="1")
            with self.assertRaises(client.HodlOddsError) as caught:
                next(watch)
            self.assertIsNone(caught.exception.payment)
            self.assertEqual(list(watch), [])
            self.assertEqual(self.sdk.signs, 0)

    def test_missing_status_as_of_is_rejected_without_payment(self):
        for value in (None, True, "123", -1):
            c = self.make([f.Response(200, body={"state": "live", "as_of": value})])
            with self.assertRaisesRegex(client.HodlOddsError, "as_of"):
                next(c.watch(f.MINT, max_spend_usdc="1"))
            self.assertEqual(self.sdk.signs, 0)

    def test_missing_paid_as_of_stops_and_preserves_receipt(self):
        c = self.make([status(), f.challenge(), paid(None)])
        watch = c.watch(f.MINT, max_spend_usdc="1")
        with self.assertRaises(client.HodlOddsError) as caught:
            next(watch)
        self.assertEqual(caught.exception.payment["transaction"], f.TX)
        self.assertEqual(list(watch), [])
        self.assertEqual(watch.spent_atomic, 10000)

    def test_interval_validation_is_eager_for_both_routes(self):
        c = self.make()
        for value in (0, 61, -1, True, False, 1.0, "5", None):
            for method, args in ((c.watch, (f.MINT,)), (c.watch_board, ())):
                with self.assertRaisesRegex(client.HodlOddsError, "every_minutes"):
                    method(*args, every_minutes=value, max_spend_usdc="1")
        for value in (1, 5, 60):
            c.watch(f.MINT, value, max_spend_usdc="1").close()
        self.assertEqual(self.http.calls, [])

    def test_required_cap_and_other_options_validate_before_http(self):
        c = self.make()
        with self.assertRaises(TypeError):
            c.watch(f.MINT)
        with self.assertRaises(TypeError):
            c.watch_board()
        for cap in (None, True, 0, -1, "NaN", "Infinity", ".0000001", "1.000000000000000000000000000001"):
            with self.assertRaisesRegex(client.HodlOddsError, "max_spend_usdc"):
                c.watch(f.MINT, max_spend_usdc=cap)
        for kwargs in ({"max_calls": 0}, {"max_calls": True}, {"max_calls": 1.5},
                       {"alert_above": -1}, {"alert_above": 1.1}, {"alert_above": "NaN"},
                       {"alert_above": True}, {"stop_on_stale": "yes"}):
            with self.assertRaises(client.HodlOddsError):
                c.watch(f.MINT, max_spend_usdc="1", **kwargs)
        for kwargs in ({"limit": 501}, {"max_price_usdc": "2.500001"}, {"typo": 1}):
            with self.assertRaises(client.HodlOddsError):
                c.watch_board(max_spend_usdc="2", **kwargs)
        self.assertEqual(self.http.calls, [])

    def test_estimates_available_before_iteration(self):
        c = self.make()
        for every, hourly, daily in ((1, "0.6", "14.4"), (5, "0.12", "2.88")):
            watch = c.watch(f.MINT, every, max_spend_usdc="1")
            self.assertEqual(watch.estimate["per_hour_usdc"], hourly)
            self.assertEqual(watch.estimate["per_24h_usdc"], daily)
            self.assertEqual(watch.estimate["price_per_call_usdc"], "0.01")
            self.assertEqual(watch.estimate["cap_usdc"], "1")
            watch.close()
        watch = c.watch_board(1, max_spend_usdc="20", limit=100, max_price_usdc=".1")
        self.assertEqual(watch.estimate["price_per_call_usdc"], "0.5")
        self.assertEqual(watch.estimate["per_hour_usdc"], "30")
        self.assertEqual(watch.estimate["per_24h_usdc"], "720")
        self.assertEqual(self.http.calls, [])
        watch.close()

    def test_board_uses_dynamic_offer_count_not_limit_for_actual_spend(self):
        c = self.make([status(), f.challenge(), paid(rows=[])])
        f.board_offer(self.sdk, n=37, limit=100, sort="active", min_confidence="high")
        watch = c.watch_board(max_spend_usdc=".185", limit=100, sort="active",
                              min_confidence="high", max_price_usdc=".5")
        self.assertEqual(len(list(watch)), 1)
        self.assertEqual(watch.spent_atomic, 185000)
        self.assertEqual(self.http.calls[1][1], self.http.calls[2][1])
        self.assertIn("min_confidence=high", self.http.calls[1][1])
        self.assertEqual(self.http.timeouts, [None, None, 90.0])

    def test_board_binding_and_price_checks_cannot_be_bypassed(self):
        for change in (lambda s: s.offer["accepts"][0]["extra"].pop("hodloddsBoardSignature"),
                       lambda s: s.offer["accepts"][0]["extra"]["hodloddsBoard"].update(priced_n=11)):
            c = self.make([status(), f.challenge()])
            f.board_offer(self.sdk)
            change(self.sdk)
            with self.assertRaises(client.HodlOddsError):
                next(c.watch_board(max_spend_usdc="1", max_price_usdc=".5"))
            self.assertEqual(self.sdk.loads, 0)
        c = self.make([status(), f.challenge()])
        f.board_offer(self.sdk)
        with self.assertRaisesRegex(client.HodlOddsError, "price"):
            next(c.watch_board(max_spend_usdc="1"))  # existing default stays .01
        self.assertEqual(self.sdk.loads, 0)

    def test_board_cap_checked_against_dynamic_price_before_signing(self):
        c = self.make([status(), f.challenge()])
        f.board_offer(self.sdk, n=37, limit=100)
        watch = c.watch_board(max_spend_usdc=".184999", limit=100, max_price_usdc=".5")
        answer, = list(watch)
        self.assertEqual(answer.watch["skipped_reason"], "spending cap reached")
        self.assertEqual(watch.spent_atomic, 0)
        self.assertEqual(self.sdk.loads, 0)

    def test_malformed_answer_rows_preserve_paid_receipt(self):
        for body in ({"as_of": 100, "row": None}, {"as_of": 100, "rows": [None]},
                     {"as_of": 100, "rows": None}):
            c = self.make([status(), f.challenge(), f.Response(200, {"PAYMENT-RESPONSE": "receipt"}, body)])
            watch = c.watch(f.MINT, max_spend_usdc="1", alert_above=.2)
            with self.assertRaises(client.HodlOddsError) as caught:
                next(watch)
            self.assertEqual(caught.exception.payment["transaction"], f.TX)
            self.assertEqual(watch.spent_atomic, 10000)
            self.assertEqual(list(watch), [])

    def test_empty_board_is_free_and_counts_towards_max_calls(self):
        c = self.make([status(), f.Response(200, body={"as_of": 3540, "rows": []})])
        watch = c.watch_board(max_spend_usdc="1", max_calls=1)
        answer, = list(watch)
        self.assertFalse(answer.paid)
        self.assertEqual(watch.spent_atomic, 0)
        self.assertEqual(self.sdk.signs, 0)

    def test_generator_close_before_start_and_between_cycles(self):
        c = self.make([status(), f.challenge(), paid()])
        watch = c.watch(f.MINT, max_spend_usdc="1")
        watch.close()
        self.assertEqual(list(watch), [])
        self.assertEqual(self.http.calls, [])
        watch = c.watch(f.MINT, max_spend_usdc="1")
        self.assertTrue(next(watch).paid)
        watch.close()
        self.assertEqual(list(watch), [])
        self.assertEqual(len(self.http.calls), 3)
        self.assertEqual(watch.spent_atomic, 10000)
        self.assertFalse(self.http.closed)  # the caller still owns the client

    def test_keyboardinterrupt_between_cycles_and_in_unsigned_request_is_clean(self):
        c = self.make()
        watch = c.watch(f.MINT, max_spend_usdc="1")
        with patch.object(client.time, "sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(list(watch), [])
        self.assertEqual(self.http.calls, [])
        self.assertEqual(watch.stop_reason, "interrupted")
        c = self.make([status(), f.challenge()])
        self.sdk.sign_error = KeyboardInterrupt()
        watch = c.watch(f.MINT, max_spend_usdc="1")
        self.assertEqual(list(watch), [])
        self.assertEqual(watch.spent_atomic, 0)
        self.assertEqual(len(self.http.calls), 2)

    def test_interrupt_in_authorized_request_preserves_pending_and_stops(self):
        for interrupt in (KeyboardInterrupt, GeneratorExit):
            c = self.make([status(), f.challenge()])
            original = self.http.stream

            def stream(method, url, headers, timeout=None):
                if "PAYMENT-SIGNATURE" in headers:
                    raise interrupt()
                return original(method, url, headers, timeout)

            self.http.stream = stream
            watch = c.watch(f.MINT, max_spend_usdc="1")
            with self.assertRaises(client.HodlOddsError) as caught:
                next(watch)
            self.assertIsInstance(caught.exception.payment, client.PendingPayment)
            self.assertIn("interrupted", str(caught.exception))
            self.assertIn("Reconcile", str(caught.exception))
            self.assertEqual(watch.spent_atomic, 10000)
            self.assertEqual(self.sdk.signs, 1)
            self.assertEqual(list(watch), [])


class WatchCLITests(OfflineWatch, unittest.TestCase):
    run_cli = f.CLITests.run_cli

    def setUp(self):
        super().setUp()
        f.CLITests.setUp(self)

    def test_cli_requires_cap_and_exclusive_target_before_http(self):
        for args in (["watch", f.MINT], ["watch", "--board"], ["watch", "--max-spend", "1"],
                     ["watch", f.MINT, "--board", "--max-spend", "1"],
                     ["watch", f.MINT, "--max-spend", "1", "--limit", "50"],
                     ["risk", f.MINT, "--max-spend", "1"]):
            code, _, err, http, sdk = self.run_cli(args, [])
            self.assertEqual(code, 2, err)
            self.assertEqual(http.calls, [])
            self.assertEqual(sdk.signs, 0)

    def test_cli_json_lines_full_answers_skips_metadata_and_stderr_estimate(self):
        args = ["watch", f.MINT, "--pay-to", f.PAYTO, "--every", "1", "--max-spend", ".02",
                "--max-calls", "2", "--alert-above", ".20", "--json"]
        replies = [status(100), f.challenge(), paid(100), status(100), status(100),
                   status(160), f.challenge(), paid(160)]
        code, out, err, http, sdk = self.run_cli(args, replies)
        self.assertEqual(code, 0, err)
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["row"]["p_crash_60m"], .2)
        self.assertEqual(lines[0]["watch"]["cycle"], 1)
        self.assertEqual(lines[0]["watch"]["spent_usdc"], "0.01")
        self.assertEqual(lines[0]["watch"]["cap_usdc"], "0.02")
        self.assertIsNone(lines[0]["watch"]["skipped_reason"])
        self.assertTrue(lines[0]["watch"]["alert"])
        self.assertEqual(lines[1]["watch"]["skipped_reason"], "unchanged as_of")
        self.assertEqual(lines[2]["watch"]["spent_usdc"], "0.02")
        self.assertIn("0.6 USDC/h", err)
        self.assertIn("14.4 USDC/24h", err)
        self.assertNotIn(sdk.encoded, out + err)
        self.assertTrue(http.closed)

    def test_tiny_odds_never_print_as_zero(self):
        self.assertIn("odds=<0.1%", f.client._watch_row({"p_crash_60m": 0.0004, "confidence": {"level": "medium"}}))
        self.assertIn("odds=<0.1%", f.client._watch_row({"p_crash_60m": 0.0}))
        self.assertIn("odds=0.1%", f.client._watch_row({"p_crash_60m": 0.001}))

    def test_text_output_odds_confidence_alert_and_spend(self):
        args = ["watch", f.MINT, "--pay-to", f.PAYTO, "--max-spend", ".01", "--alert-above", ".2"]
        code, out, err, _, _ = self.run_cli(args, [status(), f.challenge(), paid()])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 1)
        for expected in ("+00:00", "as_of=3540", "odds=20.0%", "confidence=high", "ALERT", "0.01 / 0.01 USDC"):
            self.assertIn(expected, out)

    def test_board_summary_alert_rows_flags_and_maximum_estimate(self):
        sdk = f.FakeSDK()
        f.board_offer(sdk, n=2, limit=100, sort="active", min_confidence="medium")
        rows = [{"mint": "above", "p_crash_60m": .2, "confidence": {"level": "high"}},
                {"mint": "below", "p_crash_60m": .1, "confidence": {"level": "medium"}}]
        args = ["watch", "--board", "--pay-to", f.PAYTO, "--max-spend", "2", "--max-price", ".5",
                "--limit", "100", "--sort", "active", "--min-confidence", "medium",
                "--max-calls", "1", "--alert-above", ".2"]
        code, out, err, http, _ = self.run_cli(args, [status(), f.challenge(), paid(rows=rows)], sdk)
        self.assertEqual(code, 0, err)
        self.assertIn("board 2 rows, 1 alerts ALERT", out)
        self.assertIn("above odds=20.0% confidence=high ALERT", out)
        self.assertNotIn("below", out)
        self.assertIn("0.5 USDC/call", err)
        self.assertIn("min_confidence=medium", http.calls[1][1])

    def test_board_json_preserves_all_rows_and_alert_is_false_below_threshold(self):
        sdk = f.FakeSDK()
        f.board_offer(sdk, n=1)
        rows = [{"mint": f.MINT, "p_crash_60m": .19, "confidence": None}]
        args = ["watch", "--board", "--pay-to", f.PAYTO, "--max-spend", "1",
                "--max-calls", "1", "--alert-above", ".2", "--json"]
        code, out, err, _, _ = self.run_cli(args, [status(), f.challenge(), paid(rows=rows)], sdk)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["rows"], rows)
        self.assertFalse(result["watch"]["alert"])
        self.assertEqual(result["watch"]["spent_usdc"], "0.005")

    def test_cli_invalid_intervals_never_send_requests(self):
        for every in ("0", "61", "1.5", "true"):
            code, _, err, http, sdk = self.run_cli(["watch", f.MINT, "--max-spend", "1", "--every", every], [])
            self.assertIn(code, (1, 2), err)
            self.assertEqual(http.calls, [])
            self.assertEqual(sdk.signs, 0)

    def test_cli_mainnet_still_requires_explicit_network_and_recipient(self):
        args = ["watch", f.MINT, "--max-spend", "1"]
        for network in ("solana", "base"):
            code, _, err, http, _ = self.run_cli(args, [], env={"HODLODDS_NETWORK": network, "HODLODDS_PAY_TO": f.PAYTO})
            self.assertEqual(code, 2)
            self.assertIn("explicit --network and --pay-to", err)
            self.assertEqual(http.calls, [])
        code, _, err, _, _ = self.run_cli(args + ["--network", "solana", "--pay-to", f.PAYTO, "--stop-on-stale"],
                                         [status(state="stale")])
        self.assertEqual(code, 0, err)

    def test_cli_price_rejection_and_pending_payment_error(self):
        args = ["watch", f.MINT, "--pay-to", f.PAYTO, "--max-spend", "1"]
        code, out, err, _, sdk = self.run_cli(args + ["--max-price", ".009"], [status(), f.challenge()])
        self.assertEqual(code, 1)
        self.assertEqual(sdk.signs, 0)
        self.assertIn("price", err)
        code, out, err, http, sdk = self.run_cli(args, [status(), f.challenge(), f.Response(503)])
        self.assertEqual(code, 1)
        self.assertIn("Reconcile", err)
        self.assertIn("Pending payment saved:", err)
        self.assertEqual(sdk.signs, 1)
        self.assertEqual(len(http.calls), 3)
        self.assertTrue(http.closed)

    def test_cli_interrupt_while_waiting_closes_client(self):
        with patch.object(client.time, "sleep", side_effect=KeyboardInterrupt):
            code, _, err, http, sdk = self.run_cli(["watch", f.MINT, "--max-spend", "1"], [])
        self.assertEqual(code, 0, err)
        self.assertIn("interrupted", err)
        self.assertEqual(http.calls, [])
        self.assertTrue(http.closed)
        self.assertEqual(sdk.signs, 0)


if __name__ == "__main__":
    unittest.main()
