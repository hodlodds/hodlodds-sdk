"""Create a throwaway Solana devnet or Base Sepolia wallet, mode 0600.

    hodlodds-wallet new ~/.config/hodlodds/my-devnet.json
    hodlodds-wallet new --network base-sepolia ~/.config/hodlodds/base-sepolia.json

Prints only the public address. Fund it with devnet USDC at https://faucet.circle.com
(network: Solana Devnet or Base Sepolia). Never put real funds in a test wallet.
"""
import argparse
import json
import os
import sys
from pathlib import Path


class WalletError(Exception):
    """A key-generation failure with no SDK exception or secret material attached."""


def _keypair():
    # lazy import keeps offline safety tests local.
    from solders.keypair import Keypair
    return Keypair()


def _evm_account():
    from eth_account import Account
    return Account.create()


def new(path, network="devnet"):
    fd = None
    identity = None
    p = None
    try:
        if network not in ("devnet", "base-sepolia"):
            raise ValueError("test wallet network required")
        p = Path(path).expanduser()
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # O_EXCL, including for dangling symlinks, is the overwrite guard.
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        st = os.fstat(fd)
        identity = (st.st_dev, st.st_ino)
        # enforce exactly 0600 even under a strict
        # umask; never print a public address until its private file is durable.
        os.fchmod(fd, 0o600)
        if network == "devnet":
            kp = _keypair(); secret = bytes(kp); address = str(kp.pubkey())
            if len(secret) != 64:
                raise ValueError("invalid keypair")
            stored = list(secret)
        else:
            account = _evm_account(); secret = bytes(account.key); address = str(account.address)
            if len(secret) != 32:
                raise ValueError("invalid EVM key")
            stored = {"private_key": "0x" + secret.hex()}
        f = os.fdopen(fd, "w")
        fd = None
        with f:
            json.dump(stored, f)
            f.flush()
            os.fsync(f.fileno())
    except Exception as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        # Only unlink the file we created, never a replaced path or old wallet.
        if identity is not None:
            try:
                st = p.lstat()
                if (st.st_dev, st.st_ino) == identity:
                    p.unlink()
            except OSError:
                pass
        message = "refusing to overwrite an existing wallet path" if isinstance(exc, FileExistsError) else (
            "could not create the wallet; check the destination and installed x402 dependencies")
        # SDK/JSON exceptions are not safe to expose: they may contain key bytes.
        raise WalletError(message) from None
    print(address)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(description="Create a local test wallet; prints only its public address")
    p.add_argument("command", choices=("new",))
    p.add_argument("path")
    p.add_argument("--network", choices=("devnet", "base-sepolia"), default="devnet")
    try:
        a = p.parse_args(args)
    except SystemExit as exc:
        return exc.code
    try:
        new(a.path, a.network)
    except WalletError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
