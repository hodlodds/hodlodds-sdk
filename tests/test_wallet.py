"""Offline wallet-file safety checks; the SDK and network are never invoked."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "hodlodds_wallet_test", Path(__file__).resolve().parents[1] / "hodlodds" / "wallet.py")
wallet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wallet)


class FakeKeypair:
    def __bytes__(self):
        return bytes(range(64))

    def pubkey(self):
        return "ExamplePublicAddress"


class WalletTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "wallet.json"

    def call(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wallet.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_create_prints_public_only_with_exact_private_mode(self):
        previous = os.umask(0o777)
        try:
            with mock.patch.object(wallet, "_keypair", return_value=FakeKeypair()):
                code, out, err = self.call(["new", str(self.path)])
        finally:
            os.umask(previous)
        self.assertEqual((code, out, err), (0, "ExamplePublicAddress\n", ""))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_text()), list(range(64)))

    def test_existing_file_unchanged_and_no_key_generated(self):
        self.path.write_text("existing wallet")
        with mock.patch.object(wallet, "_keypair") as key:
            code, out, err = self.call(["new", str(self.path)])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("refusing to overwrite", err)
        self.assertEqual(self.path.read_text(), "existing wallet")
        key.assert_not_called()

    def test_dangling_symlink_never_followed(self):
        destination = Path(self.tmp.name) / "destination.json"
        self.path.symlink_to(destination)
        with mock.patch.object(wallet, "_keypair") as key:
            code, out, _ = self.call(["new", str(self.path)])
        self.assertEqual((code, out), (1, ""))
        self.assertFalse(destination.exists())
        self.assertTrue(self.path.is_symlink())
        key.assert_not_called()

    def test_sdk_exception_is_redacted_and_partial_file_removed(self):
        secret = "DO_NOT_PRINT_PRIVATE_KEY_MATERIAL"
        with mock.patch.object(wallet, "_keypair", side_effect=ValueError(secret)):
            code, out, err = self.call(["new", str(self.path)])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn(secret, err)
        self.assertFalse(self.path.exists())

    def test_write_exception_is_redacted_and_partial_file_removed(self):
        with mock.patch.object(wallet, "_keypair", return_value=FakeKeypair()), mock.patch.object(
                wallet.json, "dump", side_effect=ValueError("DO_NOT_PRINT_SECRET")):
            code, out, err = self.call(["new", str(self.path)])
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn("DO_NOT_PRINT_SECRET", err)
        self.assertFalse(self.path.exists())

    def test_fsync_failure_does_not_announce_saved_wallet(self):
        with mock.patch.object(wallet, "_keypair", return_value=FakeKeypair()), mock.patch.object(
                wallet.os, "fsync", side_effect=OSError("disk full")):
            code, out, _ = self.call(["new", str(self.path)])
        self.assertEqual((code, out), (1, ""))
        self.assertFalse(self.path.exists())

    def test_invalid_usage_has_exit_two_without_key_generation(self):
        with mock.patch.object(wallet, "_keypair") as key:
            for args in ([], ["new"], ["other", str(self.path)], ["new", "a", "b"]):
                with self.subTest(args=args):
                    code, out, err = self.call(args)
                    self.assertEqual((code, out), (2, ""))
                    self.assertIn("usage:", err)
        key.assert_not_called()

    def test_base_sepolia_wallet_prints_address_only_and_refuses_mainnet(self):
        from types import SimpleNamespace
        public = "0x1111111111111111111111111111111111111111"
        account = SimpleNamespace(key=bytes(range(32)), address=public)
        with mock.patch.object(wallet, "_evm_account", return_value=account), mock.patch.object(wallet, "_keypair") as sol:
            code, out, err = self.call(["new", "--network", "base-sepolia", str(self.path)])
            self.assertEqual((code, out, err), (0, public + "\n", "")); sol.assert_not_called()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_text()), {"private_key": "0x" + bytes(range(32)).hex()})
        with mock.patch.object(wallet, "_evm_account") as create:
            for network in ("base", "solana", "mainnet"):
                self.assertEqual(self.call(["new", "--network", network, "new.json"])[0], 2)
            create.assert_not_called()

    def test_base_failure_redacts_secret_and_cleans_partial_file(self):
        secret = "EVM_SECRET_MUST_NOT_ESCAPE"
        with mock.patch.object(wallet, "_evm_account", side_effect=RuntimeError(secret)):
            code, out, err = self.call(["new", str(self.path), "--network", "base-sepolia"])
        self.assertEqual((code, out), (1, "")); self.assertNotIn(secret, err); self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
