"""Current service compatibility and confirmed free refusal money safety."""
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch
from hodlodds import __version__
import test_client as f
import test_watch as w
from test_watchlist import OfflineNetworks
client = f.client


class FreeRefusalTests(w.OfflineWatch, unittest.TestCase):
    def test_free_431_after_signing_has_no_pending_or_automatic_retry(self):
        for code in (400, 404, 409, 410, 422, 429, 431, 503):
            with self.subTest(status=code):
                c = self.make([f.challenge(), f.Response(code, {'HodlOdds-Charged': 'no'},
                    {'charged': False, 'error': 'Payment header too large'})])
                with self.assertRaises(client.HodlOddsError) as caught:
                    c.risk(f.MINT)
                self.assertFalse(caught.exception.payment['charged'])
                self.assertNotIsInstance(caught.exception.payment, client.PendingPayment)
                self.assertEqual(caught.exception.status, code)
                self.assertEqual(self.sdk.signs, 1)
                self.assertEqual(len(self.http.calls), 2)

    def test_431_before_challenge_never_loads_a_key(self):
        c = self.make([f.Response(431, {'HodlOdds-Charged': 'no'}, {'charged': False})])
        with self.assertRaises(client.HodlOddsError) as caught:
            c.risk(f.MINT)
        self.assertIsNone(caught.exception.payment)
        self.assertEqual(self.sdk.loads, 0)
        self.assertEqual(self.sdk.signs, 0)
        self.assertEqual(len(self.http.calls), 1)

    def test_431_free_refusal_watch_continues(self):
        c = self.make([w.status(100), f.challenge(), f.Response(431, {'HodlOdds-Charged': 'no'},
            {'charged': False, 'error': 'Payment header too large'}),
            w.status(160), f.challenge(), w.paid(160)])
        watching = c.watch(f.MINT, max_spend_usdc='.01', max_calls=2)
        skipped, paid = list(watching)
        self.assertFalse(skipped.paid)
        self.assertEqual(skipped.watch['spent_usdc'], '0')
        self.assertTrue(paid.paid)
        self.assertEqual(watching.spent_atomic, 10000)
        self.assertEqual(self.sdk.signs, 2)

    def test_free_refusal_no_pending_then_watch_continues_without_spend(self):
        refusal = f.Response(503, {'HodlOdds-Charged': 'no'}, {'charged': False, 'error': 'Snapshot refresh in progress'})
        c = self.make([w.status(100), f.challenge(), refusal, w.status(160), f.challenge(), w.paid(160)])
        watch = c.watch(f.MINT, max_spend_usdc='.01', max_calls=2)
        skipped = next(watch)
        self.assertEqual(skipped.watch['skipped_reason'], 'refused before settlement: Snapshot refresh in progress')
        self.assertEqual(watch.spent_atomic, 0)
        self.assertFalse(skipped.paid)
        self.assertTrue(next(watch).paid)
        self.assertEqual(watch.spent_atomic, 10000)
        self.assertEqual(self.sdk.signs, 2)
        with self.assertRaises(StopIteration):
            next(watch)

    def test_free_refusal_is_plain_error_not_a_pending_payment(self):
        for receipt in (False, True):
            headers = {'HodlOdds-Charged': 'no'}
            if receipt:
                headers['PAYMENT-RESPONSE'] = 'receipt'
            c = self.make([f.challenge(), f.Response(409, headers, {'charged': False, 'error': 'Latest snapshot changed; request again'})])
            self.sdk.receipt.update(success=False, transaction='')
            with self.assertRaises(client.HodlOddsError) as caught:
                c.risk(f.MINT)
            error = caught.exception
            self.assertIs(error.payment['charged'], False)
            self.assertNotIsInstance(error.payment, client.PendingPayment)
            self.assertNotIn('Settlement may have occurred', str(error))

    def test_proxy_header_cannot_override_success_transaction_or_malformed_receipt(self):
        for success, tx, malformed in ((True, '', False), (False, f.TX, False), (True, f.TX, False), (False, '', True)):
            c = self.make([w.status(100), f.challenge(), f.Response(503,
                {'HodlOdds-Charged': 'no', 'PAYMENT-RESPONSE': 'receipt'}, {'charged': False, 'error': 'proxy'})])
            self.sdk.receipt.update(success=success, transaction=tx)
            if malformed:
                self.sdk.receipt_error = ValueError('bad receipt')
            watch = c.watch(f.MINT, max_spend_usdc='.01')
            with self.assertRaises(client.HodlOddsError) as caught:
                next(watch)
            self.assertIn('Settlement may have occurred', str(caught.exception))
            self.assertIsInstance(caught.exception.payment, client.PendingPayment)
            self.assertEqual(watch.spent_atomic, 10000)
            with self.assertRaises(StopIteration):
                next(watch)


class ServiceCompatibilityTests(OfflineNetworks, unittest.TestCase):
    make = f.ClientTests.make

    def test_release_version_and_user_agent_agree(self):
        self.assertEqual(__version__, '0.3.4')
        project = (Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text()
        self.assertEqual(re.search(r'^version = "([^"]+)"$', project, re.MULTILINE).group(1), __version__)
        with patch.object(client, '_new_http', return_value=f.FakeHTTP([])) as factory:
            c = client.HodlOdds('http://127.0.0.1:8010', pay_to=f.PAYTO)
        self.assertEqual(factory.call_args[0][1]['User-Agent'], 'hodlodds-python/' + __version__)
        c.close()

    def test_bound_query_matches_server_golden_bytes(self):
        # Frozen from cloud.board.parse_query()/bound_query() at the live
        # service revision 56cfff5. Keep this suite standalone on PyPI/GitHub:
        # no private checkout import is required to compare canonical bytes.
        query = client._board_query(None, 'lowest_risk', {
            'min_risk': .05, 'max_risk': .8, 'min_trades_5': 9223372036854775807},
            [f.PAYER, f.MINT], chain='solana', platform='pump.fun')
        expected = (
            '{"chains":["solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"],'
            '"filters":{"max_risk":0.8,"min_risk":0.05,"min_trades_5":9223372036854775807},'
            '"limit":2,"mints_count":2,'
            '"mints_sha256":"252b7b4a05649559112a5ef2bb0c888f62bfa21d45a1ad8f8af739fde359db44",'
            '"platforms":["pumpfun"],"sort":"lowest_risk"}')
        actual = json.dumps(client._bound_query(query), sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode('utf-8')
        self.assertEqual(actual, expected.encode('utf-8'))
        self.assertEqual(query['mints'], sorted([f.PAYER, f.MINT]))

    def test_default_board_binding_and_old_sort_alias(self):
        self.assertEqual(json.dumps(client._bound_query(client._board_query(None, 'lowest_risk', {})),
                                    sort_keys=True, separators=(',', ':')),
                         '{"filters":{},"limit":50,"sort":"lowest_risk"}')
        for method in ('board', 'watch_board'):
            c = self.make([f.Response(200, body={'rows': []})])
            if method == 'board':
                c.board()
                self.assertTrue(self.http.calls[0][1].endswith('/v1/board?limit=50&sort=lowest_risk'))
            else:
                watching = c.watch_board(max_spend_usdc='1')
                self.assertEqual(watching.board_query['sort'], 'lowest_risk')
                watching.close()
        c = self.make([f.Response(200, body={'rows': []})])
        c.board(sort='safest')
        self.assertIn('sort=safest', self.http.calls[0][1])

    def test_min_trades_bound_checked_before_http_or_signature(self):
        c = self.make([f.Response(200, body={'rows': []})])
        c.board(min_trades_5=2**63 - 1)
        self.assertIn('min_trades_5=9223372036854775807', self.http.calls[0][1])
        for value in (2**63, -1, True, 1.5):
            for method in ('board', 'watch_board'):
                c = self.make()
                kwargs = {'max_spend_usdc': '1'} if method == 'watch_board' else {}
                with self.subTest(value=value, method=method), self.assertRaises(client.HodlOddsError):
                    getattr(c, method)(min_trades_5=value, **kwargs)
                self.assertEqual(self.http.calls, [])
                self.assertEqual(self.sdk.loads, 0)
                self.assertEqual(self.sdk.signs, 0)

    def test_public_https_offer_resource_does_not_have_to_match_request_url(self):
        c = self.make()
        self.sdk.offer['resource'] = {
            'url': 'https://api.hodlodds.com/v1/risk/' + f.MINT,
            'description': '60-minute crash odds', 'mimeType': 'application/json'}
        self.assertTrue(c.risk(f.MINT).paid)
        self.assertEqual(self.sdk.signs, 1)
        self.assertTrue(self.http.calls[0][1].startswith('http://127.0.0.1:8010/'))
