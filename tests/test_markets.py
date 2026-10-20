"""Offline market arguments, binding, CLI, watch and old-server compatibility."""
import json
import hashlib
from urllib.parse import parse_qs, urlparse
import unittest

import test_client as f
import test_watch as w
from test_watchlist import OfflineNetworks

sdk = f.client
SOL = sdk.AVAILABLE_CHAINS['solana']


class MarketClientTests(OfflineNetworks, unittest.TestCase):
    make = f.ClientTests.make

    def test_strings_lists_aliases_and_case(self):
        for chain in ('solana', SOL.upper(), ['SOLANA']):
            for platform in ('pumpfun', 'PUMP.FUN', ['pump']):
                self.assertEqual(sdk._market_query(chain, platform), {'chains': [SOL], 'platforms': ['pumpfun']})

    def test_validation_before_http_or_key_access(self):
        for name, values in (('chain', ['', [], ['solana', SOL], 'solana,', ['bsc'], ['solana'] * 11, 1, [None]]),
                             ('platform', ['', [], 'pump,pumpfun', ',pump', 'meteora', ['pumpfun'] * 11, False, [1]])):
            for value in values:
                for method in ('risk', 'last', 'coverage', 'board', 'watch', 'watch_board'):
                    c = self.make()
                    args = () if method in ('board', 'watch_board') else (f.MINT,)
                    options = {'max_spend_usdc': '1'} if method.startswith('watch') else {}
                    with self.subTest(name=name, value=value, method=method), self.assertRaises(sdk.HodlOddsError):
                        getattr(c, method)(*args, **{name: value}, **options)
                    self.assertEqual(self.http.calls, [])
                    self.assertEqual(self.sdk.loads, 0)

    def test_data_route_defaults_send_no_new_parameters(self):
        for method, suffix in (('risk', '/risk/' + f.MINT), ('last', '/risk/' + f.MINT + '/last'),
                               ('coverage', '/coverage/' + f.MINT), ('board', '/board?limit=50&sort=lowest_risk')):
            c = self.make([f.Response(200, body={})])
            getattr(c, method)(*(() if method == 'board' else (f.MINT,)))
            self.assertEqual(self.http.calls[0][1], 'http://127.0.0.1:8010/v1' + suffix)
        self.assertEqual(sdk._bound_query(sdk._board_query(None, 'lowest_risk', {})),
                         {'limit': 50, 'sort': 'lowest_risk', 'filters': {}})

    def test_all_routes_send_canonical_explicit_filters(self):
        for method in ('risk', 'last', 'coverage', 'board'):
            c = self.make([f.Response(200, body={})])
            getattr(c, method)(*(() if method == 'board' else (f.MINT,)), chain=['SOLANA'], platform='PUMP.FUN')
            params = parse_qs(urlparse(self.http.calls[0][1]).query)
            self.assertEqual(params['chain'], [SOL])
            self.assertEqual(params['platform'], ['pumpfun'])

    def test_markets_is_free(self):
        doc = {'schema': 'hodlodds.markets.v1', 'markets': []}
        c = self.make([f.Response(200, body=doc)])
        self.assertEqual(c.markets(), doc)
        self.assertTrue(self.http.calls[0][1].endswith('/v1/markets'))
        self.assertEqual(self.sdk.loads, 0)

    def test_filtered_board_offer_and_mismatch(self):
        for mismatch in (False, True):
            c = self.make()
            f.board_offer(self.sdk, n=1)
            extra = self.sdk.offer['accepts'][0]['extra']
            extra['hodloddsBoard']['query'].update(chains=[SOL], platforms=['pumpfun'])
            if mismatch:
                del extra['hodloddsBoard']['query']['platforms']
            extra['memo'] = hashlib.sha256(b'hodlodds.board.v1\0' + json.dumps(
                extra['hodloddsBoard'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:32]
            if mismatch:
                with self.assertRaises(sdk.HodlOddsError):
                    c.board(chain='solana', platform='pumpfun')
                self.assertEqual(self.sdk.signs, 0)
            else:
                c.board(chain='solana', platform='pumpfun')
                self.assertEqual(self.sdk.signs, 1)
                self.assertEqual(self.http.calls[0][1], self.http.calls[1][1])


class MarketWatchTests(w.OfflineWatch, unittest.TestCase):
    run_cli = f.CLITests.run_cli

    def test_watch_cli_forwards_filters(self):
        for target in ([f.MINT], ['--board']):
            args = ['watch'] + target + ['--chain', 'solana', '--platform', 'pump.fun',
                                        '--max-spend', '1', '--max-calls', '1']
            code, _, err, http, signer = self.run_cli(args, [w.status(), f.Response(200, body={'as_of': 3540, 'rows': []})])
            self.assertEqual(code, 0, err)
            query = parse_qs(urlparse(http.calls[1][1]).query)
            self.assertEqual(query['chain'], [SOL])
            self.assertEqual(query['platform'], ['pumpfun'])
            self.assertEqual(signer.signs, 0)

    def test_watch_paths_and_binding(self):
        for method in ('watch', 'watch_board'):
            c = self.make([w.status(), f.Response(200, body={'as_of': 3540, 'rows': []})])
            args = () if method == 'watch_board' else (f.MINT,)
            watching = getattr(c, method)(*args, chain='SOLANA', platform=['pump'], max_spend_usdc='1', max_calls=1)
            list(watching)
            params = parse_qs(urlparse(self.http.calls[1][1]).query)
            self.assertEqual(params['chain'], [SOL])
            self.assertEqual(params['platform'], ['pumpfun'])
            if method == 'watch_board':
                self.assertEqual(watching.board_query['platforms'], ['pumpfun'])

    def test_watch_defaults_omit_filters(self):
        c = self.make()
        for watching in (c.watch(f.MINT, max_spend_usdc='1'), c.watch_board(max_spend_usdc='1')):
            params = parse_qs(urlparse(watching.path).query)
            self.assertNotIn('chain', params)
            self.assertNotIn('platform', params)
            watching.close()


class MarketCLITests(OfflineNetworks, unittest.TestCase):
    run_cli = f.CLITests.run_cli

    def test_flags_for_each_data_command_and_markets(self):
        for command in ('risk', 'last', 'coverage', 'board'):
            args = [command] + ([] if command == 'board' else [f.MINT]) + ['--chain', 'SOLANA', '--platform', 'pump.fun']
            code, _, err, http, signer = self.run_cli(args, [f.Response(200, body={})])
            self.assertEqual(code, 0, err)
            self.assertEqual(parse_qs(urlparse(http.calls[0][1]).query)['chain'], [SOL])
            self.assertEqual(parse_qs(urlparse(http.calls[0][1]).query)['platform'], ['pumpfun'])
            self.assertEqual(signer.signs, 0)
        code, _, err, http, signer = self.run_cli(['markets'], [f.Response(200, body={'markets': []})])
        self.assertEqual(code, 0, err)
        self.assertTrue(http.calls[0][1].endswith('/v1/markets'))
        self.assertEqual(signer.signs, 0)

    def test_invalid_flags_before_http(self):
        for args in (['board', '--chain', 'solana,solana'], ['risk', f.MINT, '--platform', 'meteora'],
                     ['markets', '--platform', 'pumpfun'], ['status', '--chain', 'solana']):
            code, _, _, http, signer = self.run_cli(args, [])
            self.assertNotEqual(code, 0)
            self.assertEqual(http.calls, [])
            self.assertEqual(signer.signs, 0)
