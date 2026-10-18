"""Explicit redeem and private CLI handoff; no network or key reads."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import test_client as f
client = f.client


class RedeemTests(unittest.TestCase):
    make = f.ClientTests.make

    def setUp(self):
        self.net = patch.object(client, '_network_config', side_effect=lambda name: f.FIXTURE_NETWORKS[name])
        self.net.start(); self.addCleanup(self.net.stop)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name).resolve()/'pending'

    def uncertain(self):
        c = self.make([f.challenge(), f.Response(402, {'PAYMENT-RESPONSE':'receipt'})])
        self.sdk.receipt.update(success=False,transaction='',errorReason='invalid_exact_evm_transaction_failed')
        with self.assertRaises(client.HodlOddsError) as caught:
            c.risk(f.MINT)
        self.assertIn('usually nothing was charged',str(caught.exception))
        self.assertIn('redeem after ~2 minutes',str(caught.exception))
        return c,caught.exception

    def test_unknown_outcome_is_not_called_a_declared_failure(self):
        c = self.make([f.challenge(), f.Response(503, {'PAYMENT-RESPONSE':'receipt'})])
        self.sdk.receipt.update(success=False,transaction='')
        with self.assertRaises(client.HodlOddsError) as caught:
            c.risk(f.MINT)
        self.assertNotIn('usually nothing was charged',str(caught.exception))
        self.assertIn('Settlement may have occurred',str(caught.exception))

    def test_redeem_one_request_no_signature_or_secret_logging(self):
        c,error=self.uncertain()
        self.assertNotIn(self.sdk.encoded,repr(error.payment))
        self.assertNotIn(self.sdk.encoded,json.dumps(error.payment))
        self.assertNotIn(self.sdk.encoded,str(error))
        self.http.responses.append(f.Response(200,{'HodlOdds-Redeemed':f.TX}))
        answer=c.redeem(error.payment)
        self.assertTrue(answer.payment['redeemed'])
        self.assertEqual(len(self.http.calls),3)
        self.assertEqual(self.sdk.signs,1)
        self.assertEqual(self.http.calls[1][1],self.http.calls[2][1])
        self.assertEqual(self.http.calls[2][2]['PAYMENT-SIGNATURE'],self.sdk.encoded)
        self.assertEqual(self.http.calls[2][2]['HodlOdds-Redeem'],'1')

    def test_pending_and_transport_refusal_preserve_payment(self):
        c,error=self.uncertain()
        for outcome in (f.Response(409,{'Retry-After':'30'}),OSError('secret transport')):
            self.http.responses.append(outcome)
            count=len(self.http.calls)
            with self.assertRaises(client.HodlOddsError) as caught:
                c.redeem(error.payment)
            self.assertIs(caught.exception.payment,error.payment)
            self.assertNotIn('secret transport',str(caught.exception))
            self.assertEqual(len(self.http.calls),count+1)
        self.assertEqual(self.sdk.signs,1)

    def test_private_file_roundtrip_permissions_and_symlink_rejection(self):
        c,error=self.uncertain()
        file=client.save_pending(error.payment,self.directory)
        self.assertEqual(stat.S_IMODE(file.stat().st_mode),0o600)
        self.assertFalse(file.is_symlink())
        payment=client.load_pending(file)
        self.assertEqual(payment._signature,self.sdk.encoded)
        self.assertEqual(payment._url,error.payment._url)
        file.chmod(0o644)
        with self.assertRaises(client.HodlOddsError): client.load_pending(file)
        file.chmod(0o600)
        link=self.directory/'link'; link.symlink_to(file)
        with self.assertRaises(client.HodlOddsError): client.load_pending(link)
        linkdir=self.directory.parent/'alias'; linkdir.symlink_to(self.directory,target_is_directory=True)
        with self.assertRaises(client.HodlOddsError): client.save_pending(error.payment,linkdir)

    def test_cli_saves_and_prints_only_path_then_redeems(self):
        c,error=self.uncertain()
        c.risk=lambda *args,**kwargs: (_ for _ in ()).throw(error)
        output=io.StringIO()
        original=client.save_pending
        with patch.object(client,'HodlOdds',return_value=c), patch.object(client,'save_pending',
                side_effect=lambda payment: original(payment,self.directory)), contextlib.redirect_stderr(output):
            self.assertEqual(client.main(['risk',f.MINT]),1)
        self.assertNotIn(self.sdk.encoded,output.getvalue())
        files=list(self.directory.iterdir()); self.assertEqual(len(files),1)
        self.assertIn(str(files[0]),output.getvalue())
        self.http.responses.append(f.Response(200,{'HodlOdds-Redeemed':f.TX}))
        with patch.object(client,'HodlOdds',return_value=c), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(client.main(['redeem',str(files[0])]),0)
        self.assertEqual(self.sdk.signs,1)

    def test_wrong_endpoint_and_nonpayment_fail_before_http(self):
        c,error=self.uncertain()
        error.payment._base='https://other.test'
        for payment in (error.payment,dict(error.payment)):
            with self.assertRaises(client.HodlOddsError): c.redeem(payment)
        self.assertEqual(len(self.http.calls),2)
