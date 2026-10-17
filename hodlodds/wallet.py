"""Create a throwaway Solana DEVNET test wallet (solana-keygen JSON, mode 0600).

    hodlodds-wallet new ~/.config/hodlodds/my-devnet.json

Prints only the public address. Fund it with devnet USDC at https://faucet.circle.com
(network: Solana Devnet). Never put real funds in a test wallet.
"""
import json
import os
import sys
from pathlib import Path


class WalletError(Exception):
    """A key-generation failure with no SDK exception or secret material attached."""


def _keypair():
    # local (2026-10-17, Codex r54): lazy import keeps offline safety tests local.
    from solders.keypair import Keypair
    return Keypair()


def new(path):
    fd = None
    identity = None
    p = None
    try:
        p = Path(path).expanduser()
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # O_EXCL, including for dangling symlinks, is the overwrite guard.
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        st = os.fstat(fd)
        identity = (st.st_dev, st.st_ino)
        # local (2026-10-17, Codex r54): enforce exactly 0600 even under a strict
        # umask; never print a public address until its private file is durable.
        os.fchmod(fd, 0o600)
        kp = _keypair()
        secret = bytes(kp)
        address = str(kp.pubkey())
        if len(secret) != 64:
            raise ValueError("invalid keypair")
        f = os.fdopen(fd, "w")
        fd = None
        with f:
            json.dump(list(secret), f)
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
    if len(args) != 2 or args[0] != "new":
        print("usage: hodlodds-wallet new <path>", file=sys.stderr)
        return 2
    try:
        new(args[1])
    except WalletError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
