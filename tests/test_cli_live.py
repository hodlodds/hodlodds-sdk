"""Public CLI coverage for live watchlists and data-market filters; no network."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_client as f
import test_watch as w

client = f.client


class LiveCLITests(unittest.TestCase):
    run_cli = f.CLITests.run_cli

    def setUp(self):
        network = patch.object(client, "_network_config",
                               side_effect=lambda name: f.FIXTURE_NETWORKS[name])
        network.start()
        self.addCleanup(network.stop)

    def test_public_url_and_lowest_risk_default(self):
        code, _, err, http, signer = self.run_cli(["board"], [f.Response(200, body={"rows": []})])
        self.assertEqual(code, 0, err)
        self.assertEqual(http.calls[0][1],
                         "https://api.hodlodds.com/v1/board?limit=50&sort=lowest_risk")
        self.assertEqual(signer.signs, 0)

    def test_watchlist_list_and_commented_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mints.txt"
            path.write_text("# Watchlist\n\n" + f.PAYER + " # token\n" + f.MINT + "\n")
            for flags in (["--mints", f.PAYER + "," + f.MINT], ["--mints-file", str(path)]):
                code, _, err, http, signer = self.run_cli(["board"] + flags,
                                                         [f.Response(200, body={"rows": []})])
                self.assertEqual(code, 0, err)
                self.assertEqual(parse_qs(urlsplit(http.calls[0][1]).query), {
                    "limit": ["2"], "sort": ["lowest_risk"],
                    "mints": [",".join(sorted([f.MINT, f.PAYER]))]})
                self.assertEqual(signer.signs, 0)

    def test_watchlist_invalid_or_unreadable_before_http(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mints.txt"
            for text in ("", f.MINT + "\n" + f.MINT, "invalid\n", "\xff"):
                path.write_text(text)
                code, _, _, http, signer = self.run_cli(["board", "--mints-file", str(path)], [])
                self.assertEqual(code, 1)
                self.assertEqual(http.calls, [])
                self.assertEqual(signer.signs, 0)
            for args in (["board", "--mints", f.MINT + "," + f.MINT],
                         ["board", "--mints-file", str(path / "missing")],
                         ["board", "--mints", f.MINT, "--mints-file", str(path)],
                         ["risk", f.MINT, "--mints", f.MINT]):
                code, _, _, http, signer = self.run_cli(args, [])
                self.assertNotEqual(code, 0)
                self.assertEqual(http.calls, [])
                self.assertEqual(signer.signs, 0)

    def test_data_commands_market_filters_and_free_markets(self):
        for command in ("risk", "last", "coverage", "board"):
            args = [command] + ([] if command == "board" else [f.MINT])
            code, _, err, http, signer = self.run_cli(
                args + ["--chain", "SOLANA", "--platform", "pump.fun"],
                [f.Response(200, body={})])
            self.assertEqual(code, 0, err)
            query = parse_qs(urlsplit(http.calls[0][1]).query)
            self.assertEqual(query["chain"], [client.AVAILABLE_CHAINS["solana"]])
            self.assertEqual(query["platform"], ["pumpfun"])
            self.assertEqual(signer.signs, 0)
        code, out, err, http, signer = self.run_cli(["markets"], [f.Response(200, body={"markets": []})])
        self.assertEqual(code, 0, err)
        self.assertTrue(http.calls[0][1].endswith("/v1/markets"))
        self.assertIn('"markets": []', out)
        self.assertEqual(signer.signs, 0)

    def test_invalid_market_flags_before_http(self):
        for args in (["markets", "--chain", "solana"], ["status", "--platform", "pumpfun"],
                     ["board", "--chain", "solana,solana"],
                     ["risk", f.MINT, "--platform", "meteora"]):
            code, _, _, http, signer = self.run_cli(args, [])
            self.assertNotEqual(code, 0)
            self.assertEqual(http.calls, [])
            self.assertEqual(signer.signs, 0)

    def test_watch_preserves_market_filters_and_watchlist(self):
        clock = w.Clock()
        with patch.object(client.time, "time", side_effect=lambda: clock.now), \
                patch.object(client.time, "sleep", side_effect=clock.sleep), \
                patch.object(client.random, "uniform", return_value=0):
            for target in ([f.MINT], ["--board", "--mints", f.MINT]):
                args = ["watch"] + target + ["--chain", "solana", "--platform", "pumpfun",
                                             "--max-spend", "1", "--max-calls", "1"]
                code, _, err, http, signer = self.run_cli(
                    args, [w.status(), f.Response(200, body={"as_of": 3540, "rows": []})])
                self.assertEqual(code, 0, err)
                query = parse_qs(urlsplit(http.calls[1][1]).query)
                self.assertEqual(query["chain"], [client.AVAILABLE_CHAINS["solana"]])
                self.assertEqual(query["platform"], ["pumpfun"])
                if "--board" in target:
                    self.assertEqual(query["mints"], [f.MINT])
                    self.assertEqual(query["limit"], ["1"])
                self.assertEqual(signer.signs, 0)

    def test_board_and_board_watch_accept_their_own_2_5_cap(self):
        clock = w.Clock()
        with patch.object(client.time, "time", side_effect=lambda: clock.now), \
                patch.object(client.time, "sleep", side_effect=clock.sleep), \
                patch.object(client.random, "uniform", return_value=0):
            for prefix in (["board"], ["watch", "--board", "--max-spend", "2.5", "--max-calls", "1"]):
                sdk = f.FakeSDK()
                f.board_offer(sdk, n=500, limit=500)
                responses = ([w.status()] if prefix[0] == "watch" else [])
                responses += [f.challenge(), w.paid(rows=[])]
                code, out, err, _, signer = self.run_cli(
                    prefix + ["--limit", "500", "--max-price", "2.5", "--pay-to", f.PAYTO],
                    responses, sdk)
                self.assertEqual(code, 0, err)
                self.assertEqual(signer.signs, 1)
                self.assertIn("2.5", out)

    def test_board_cap_does_not_raise_single_route_cap(self):
        for args in (["board", "--max-price", "2.500001"],
                     ["watch", "--board", "--max-spend", "3", "--max-price", "2.500001"],
                     ["risk", f.MINT, "--max-price", "1.000001"],
                     ["last", f.MINT, "--max-price", "2.5"]):
            code, _, _, http, signer = self.run_cli(args, [])
            self.assertEqual(code, 1)
            self.assertEqual(http.calls, [])
            self.assertEqual(signer.signs, 0)
