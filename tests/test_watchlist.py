"""Offline watchlist validation, query/price binding, CLI files and watch budgets."""
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlparse
import unittest
from unittest.mock import patch

import test_client as f
import test_watch as w
client = f.client


def mints(n):
    # Valid decoded 32-byte addresses, each 44 characters.
    return ['B' + '1' * 41 + client.BASE58[i // 58] + client.BASE58[i % 58] for i in range(n)]


class OfflineNetworks:
    def setUp(self):
        patcher = patch.object(client, '_network_config', side_effect=lambda name: f.FIXTURE_NETWORKS[name])
        patcher.start()
        self.addCleanup(patcher.stop)


class WatchlistClientTests(OfflineNetworks, unittest.TestCase):
    make = f.ClientTests.make

    def test_default_limits_aliases_and_sorted_list_without_mutating_input(self):
        requested = [f.PAYER, f.MINT]
        before = requested[:]
        query = client._board_query(None, 'safest', {}, requested)
        self.assertEqual(query, dict(limit=2, sort='safest', filters={}, mints=sorted(requested)))
        self.assertEqual(requested, before)
        self.assertEqual(client._board_query(None, 'lowest_risk', {}),
                         dict(limit=50, sort='lowest_risk', filters={}))
        self.assertEqual(client._board_query(100, 'lowest_risk', {}, requested)['limit'], 100)

    def test_validation_before_any_http_for_board_and_watch(self):
        bad = [[], [f.MINT, f.MINT], ['bad'], [None], [123], f.MINT, set([f.MINT]), mints(101)]
        for requested in bad:
            for method in ('board', 'watch_board'):
                c = self.make()
                kwargs = {'max_spend_usdc': '1'} if method == 'watch_board' else {}
                with self.subTest(mints=requested, method=method), self.assertRaises(client.HodlOddsError):
                    getattr(c, method)(mints=requested, **kwargs)
                self.assertEqual(self.http.calls, [])
                self.assertEqual(self.sdk.loads, 0)
        for limit in (0, 101, True, 1.0, '1'):
            c = self.make()
            with self.assertRaises(client.HodlOddsError):
                c.board(mints=[f.MINT], limit=limit)
            self.assertEqual(self.http.calls, [])

    def test_paid_partial_delivered_count_and_identical_sorted_retry(self):
        c = self.make()
        f.board_offer(self.sdk, n=1, limit=None, sort='safest', mints=[f.PAYER, f.MINT])
        answer = c.board(mints=[f.MINT, f.PAYER], sort='safest', max_price_usdc='.005')
        self.assertEqual(answer.payment['amount_usdc'], '0.005')
        self.assertEqual(self.http.calls[0][1], self.http.calls[1][1])
        query = parse_qs(urlparse(self.http.calls[0][1]).query)
        self.assertEqual(query, {'mints': [','.join(sorted([f.MINT, f.PAYER]))],
                                 'limit': ['2'], 'sort': ['safest']})

    def test_changed_list_and_bad_count_amount_memo_rejected_before_signing(self):
        for change in ('list', 'count', 'amount', 'memo'):
            c = self.make()
            f.board_offer(self.sdk, n=1, limit=None, mints=[f.MINT])
            option = self.sdk.offer['accepts'][0]
            if change == 'list':
                option['extra']['hodloddsBoard']['query']['mints_sha256'] = client._bound_query({'mints': [f.PAYER]})['mints_sha256']
            elif change == 'count':
                option['extra']['hodloddsBoard']['priced_n'] = 2
            elif change == 'amount':
                option['amount'] = '4999'
            else:
                option['extra']['memo'] = 'a' * 32
            with self.assertRaises(client.HodlOddsError):
                c.board(mints=[f.MINT], max_price_usdc='.01')
            self.assertEqual(self.sdk.signs, 0)
            self.assertEqual(self.sdk.loads, 0)

    def test_maximum_list_pending_file_redeems_exact_original_query_without_signing(self):
        requested = mints(100)
        c = self.make([f.challenge(), f.Response(503)])
        f.board_offer(self.sdk, n=2, limit=None, mints=requested)
        with self.assertRaises(client.HodlOddsError) as caught:
            c.board(mints=list(reversed(requested)), max_price_usdc='.01')
        with tempfile.TemporaryDirectory() as tmp:
            filename = client.save_pending(caught.exception.payment, Path(tmp).resolve() / 'pending')
            payment = client.load_pending(filename)
            self.http.responses.append(f.Response(200, {'HodlOdds-Redeemed': f.TX}))
            answer = c.redeem(payment)
        self.assertTrue(answer.payment['redeemed'])
        self.assertEqual(self.sdk.signs, 1)
        self.assertEqual(self.http.calls[1][1], self.http.calls[2][1])
        self.assertEqual(len(parse_qs(urlparse(self.http.calls[2][1]).query)['mints'][0].split(',')), 100)
        self.assertEqual(self.http.calls[2][2]['HodlOdds-Redeem'], '1')

    def test_maximum_encoded_url_and_free_empty_omissions(self):
        body = {'rows': [], 'not_delivered': [{'mint': f.MINT, 'stage': 'unknown', 'reason': 'Not observed'}]}
        c = self.make([f.Response(200, body=body)])
        answer = c.board(mints=list(reversed(mints(100))))
        self.assertFalse(answer.paid)
        self.assertEqual(answer.data, body)
        url = self.http.calls[0][1]
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query['limit'], ['100'])
        self.assertEqual(query['mints'][0].split(','), sorted(mints(100)))
        self.assertLess(len(('GET ' + url + ' HTTP/1.1\r\n').encode()), 8192)
        self.assertEqual(self.sdk.signs, 0)


class WatchlistWatchTests(w.OfflineWatch, unittest.TestCase):
    def test_watch_uses_delivered_price_and_estimates_requested_list(self):
        c = self.make([w.status(), f.challenge(), w.paid(rows=[{'mint': f.MINT}])])
        f.board_offer(self.sdk, n=1, limit=None, mints=[f.MINT, f.PAYER])
        watch = c.watch_board(1, mints=[f.PAYER, f.MINT], max_spend_usdc='.005',
                              max_price_usdc='.005', max_calls=1)
        self.assertEqual(watch.estimate['per_hour_usdc'], '0.6')
        answer = next(watch)
        self.assertEqual(answer.watch['spent_usdc'], '0.005')
        self.assertEqual(watch.spent_atomic, 5000)
        self.assertEqual(self.sdk.signs, 1)
        watch.close()

    def test_ten_mint_minute_cost_and_validation_before_status(self):
        c = self.make()
        watch = c.watch_board(1, mints=mints(10), max_spend_usdc='3', max_price_usdc='.05')
        self.assertEqual(watch.estimate['price_per_call_usdc'], '0.05')
        self.assertEqual(watch.estimate['per_hour_usdc'], '3')
        watch.close()
        with self.assertRaises(client.HodlOddsError):
            c.watch_board(mints=[f.MINT, f.MINT], max_spend_usdc='1')
        self.assertEqual(self.http.calls, [])


class WatchlistCLITests(OfflineNetworks, unittest.TestCase):
    run_cli = f.CLITests.run_cli

    def test_comma_list_and_file_are_validated_before_http(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'mints.txt'
            for contents in ('', f.MINT + '\n' + f.MINT, 'bad\n', '\n', '\n'.join(mints(101))):
                path.write_text(contents)
                for command in (['board'], ['watch', '--board', '--max-spend', '1']):
                    code, _, _, http, sdk = self.run_cli(command + ['--mints-file', str(path)], [])
                    self.assertEqual(code, 1)
                    self.assertEqual(http.calls, [])
                    self.assertEqual(sdk.signs, 0)
            for args in (['board', '--mints', f.MINT + ',' + f.MINT],
                         ['board', '--mints-file', str(path / 'missing')],
                         ['board', '--mints', ''], ['risk', f.MINT, '--mints', f.MINT],
                         ['board', '--mints', f.MINT, '--mints-file', str(path)]):
                code, _, _, http, _ = self.run_cli(args, [])
                self.assertNotEqual(code, 0)
                self.assertEqual(http.calls, [])

    def test_board_file_maximum_and_comma_list_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'mints.txt'
            path.write_text('\n'.join(reversed(mints(100))) + '\n')
            for flags, count in ((['--mints', f.MINT], 1), (['--mints-file', str(path)], 100)):
                code, _, err, http, _ = self.run_cli(['board'] + flags, [f.Response(200, body={'rows': []})])
                self.assertEqual(code, 0, err)
                query = parse_qs(urlparse(http.calls[0][1]).query)
                self.assertEqual(query['limit'], [str(count)])
                self.assertEqual(len(query['mints'][0].split(',')), count)

    def test_cli_watch_file_forwards_list(self):
        clock = w.Clock()
        with tempfile.TemporaryDirectory() as temp, patch.object(client.time, 'time', side_effect=lambda: clock.now), \
                patch.object(client.time, 'sleep', side_effect=clock.sleep), patch.object(client.random, 'uniform', return_value=0):
            path = Path(temp) / 'mints.txt'; path.write_text(f.MINT + '\n')
            sdk = f.FakeSDK(); f.board_offer(sdk, n=1, limit=None, mints=[f.MINT])
            args = ['watch', '--board', '--mints-file', str(path), '--max-spend', '.005',
                    '--max-calls', '1', '--pay-to', f.PAYTO, '--every', '1']
            code, out, err, http, sdk = self.run_cli(args, [w.status(), f.challenge(), w.paid(rows=[])], sdk)
            self.assertEqual(code, 0, err)
            self.assertIn('mints=' + f.MINT, http.calls[1][1])
            self.assertIn('0.005', err)
