"""HodlOdds Python client: pay-per-answer crash odds over x402 v2 (Solana USDC).

    from hodlodds import HodlOdds
    client = HodlOdds("https://api.hodlodds.com", keypair="~/.config/hodlodds/devnet-payer.json",
                      network="devnet", pay_to="<RECIPIENT FROM /v1/meta>")
    answer = client.risk("<MINT>")
    print(answer.data["row"], answer.payment)

Free routes (status, meta, "not covered", "feed interrupted", "too old") never sign
anything. A payment is signed only after the server's 402 offer passes every check:
x402 v2, the chosen Solana network, exact USDC, the pinned payTo address and a price
no higher than your limit. A payment is never retried automatically. The key file
(solana-keygen JSON, mode 0600) is read locally and never printed or sent anywhere;
only the SDK-signed USDC transfer authorization leaves the machine.
"""
import argparse
import json
import os
import re
import stat
import sys
from decimal import Decimal, DecimalException
from importlib.metadata import version
from pathlib import Path
from urllib.parse import quote, urlparse

SDK_VERSION = "2.25.0"
NETWORKS = {
    "devnet": {"caip2": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
               "usdc": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
               "rpc": "https://api.devnet.solana.com", "explorer": "?cluster=devnet"},
    "mainnet": {"caip2": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                "usdc": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "rpc": "https://api.mainnet-beta.solana.com", "explorer": ""},
}
ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
USDC_ATOMIC = Decimal(10) ** 6
HARD_MAX_USDC = Decimal("1")
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MAX_PAYMENT_HEADER = 16384
MAX_SIGNATURE_HEADER = 65536
MAX_HEADERS = 131072
MAX_BODY = 4 * 1024 * 1024
MAX_KEY_BYTES = 4096


class HodlOddsError(Exception):
    """Client/API error. payment may describe an uncertain submitted authorization."""
    def __init__(self, message, status=None, body=None, payment=None):
        super().__init__(message)
        self.status, self.body, self.payment = status, body, payment


class Answer:
    def __init__(self, status, data, payment=None):
        self.status, self.data, self.payment = status, data, payment

    @property
    def paid(self):
        return self.payment is not None

    def __repr__(self):
        return "Answer(status=%r, paid=%r)" % (self.status, self.paid)


def _usdc(value, name):
    message = name + " must be above 0, at most 1 USDC, with at most 6 decimals"
    try:
        text = str(value)
        if len(text) > 128:
            raise ValueError()
        amount = Decimal(text)
        if not amount.is_finite() or not 0 < amount <= HARD_MAX_USDC:
            raise ValueError()
        # local (2026-10-17, Codex r54): Decimal multiplication can round away
        # fractional atoms. Work on the exact coefficient/exponent instead.
        _, digits, exponent = amount.as_tuple()
        coefficient = int("".join(map(str, digits)))
        power = exponent + 6
        if power < 0:
            if -power > len(digits) or coefficient % (10 ** -power):
                raise ValueError()
            atomic = coefficient // (10 ** -power)
        else:
            atomic = coefficient * 10 ** power
        if not 0 < atomic <= 1000000:
            raise ValueError()
        return atomic
    except (DecimalException, ValueError, TypeError, OverflowError):
        pass
    raise HodlOddsError(message)


def _base58_size(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{1,88}", value):
        return 0
    number = 0
    for letter in value:
        number = number * 58 + BASE58.index(letter)
    return len(value) - len(value.lstrip("1")) + (number.bit_length() + 7) // 8


def _address(value):
    return isinstance(value, str) and bool(ADDRESS.fullmatch(value)) and _base58_size(value) == 32


def _base_url(value, *, rpc=False):
    try:
        if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
            raise ValueError()
        parsed = urlparse(value)
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment or (parsed.query and not rpc):
            raise ValueError()
        if parsed.port is not None and not 0 < parsed.port <= 65535:
            raise ValueError()
        loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback)):
            raise ValueError()
    except (ValueError, TypeError):
        raise HodlOddsError("use a valid HTTPS endpoint with no credentials/fragment (HTTP only on loopback; base URL has no query)") from None
    return value if rpc else value.rstrip("/")


def _load_keypair(path):
    fd = None
    try:
        p = Path(path).expanduser()
        before = p.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError()
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        current = os.fstat(fd)
        if ((current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(current.st_mode) or stat.S_IMODE(current.st_mode) != 0o600
                or current.st_size > MAX_KEY_BYTES):
            raise ValueError()
        f = os.fdopen(fd, "rb"); fd = None
        with f:
            raw = f.read(MAX_KEY_BYTES + 1)
        if len(raw) > MAX_KEY_BYTES:
            raise ValueError()
        data = json.loads(raw)
        if not (isinstance(data, list) and len(data) == 64 and all(type(v) is int and 0 <= v <= 255 for v in data)):
            raise ValueError()
        from solders.keypair import Keypair
        return Keypair.from_bytes(bytes(data))
    except Exception:
        # No parser/SDK error, filename, JSON doc or key bytes escapes this seam.
        pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    raise HodlOddsError("cannot load key: use a regular non-symlink solana-keygen file, mode 0600, and installed SDK dependencies")


def _new_http(timeout, headers):
    import httpx
    return httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False, headers=headers)


class _Reply:
    def __init__(self, status, headers, body):
        self.status_code, self.headers, self.content = status, headers, body

    def json(self):
        return json.loads(self.content)

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")


class HodlOdds:
    def __init__(self, base_url, *, keypair=None, network="devnet", pay_to=None, max_price_usdc="0.01",
                 rpc_url=None, timeout=30.0, access_headers=None):
        if network not in NETWORKS:
            raise HodlOddsError("network must be devnet or mainnet")
        if pay_to is not None and not _address(pay_to):
            raise HodlOddsError("pay_to must be a Solana address")
        if network == "mainnet" and pay_to is None:
            raise HodlOddsError("mainnet requires an explicit pinned pay_to address")
        self.base = _base_url(base_url)
        self.network, self.net = network, NETWORKS[network]
        self.pay_to, self.max_atomic = pay_to, _usdc(max_price_usdc, "max_price_usdc")
        self.rpc_url, self._keypair_path = _base_url(rpc_url or self.net["rpc"], rpc=True), keypair
        self._sdk, self._codecs = None, None
        headers = dict(access_headers or {})
        for k, v in headers.items():
            if (not isinstance(k, str) or not isinstance(v, str) or len(k) + len(v) > MAX_PAYMENT_HEADER
                    or any(c in k + v for c in "\r\n") or k.lower().startswith(("payment-", "x-payment"))
                    or k.lower() in ("host", "content-length", "transfer-encoding")):
                raise HodlOddsError("access_headers contains an invalid or reserved payment/transport header")
        headers["User-Agent"] = "hodlodds-python/0.1"
        try:
            self.http = _new_http(timeout, headers)
        except Exception:
            raise HodlOddsError("cannot initialize HTTP client; install httpx and check configuration") from None

    # Free routes
    def status(self):
        return self._get_json("/v1/status")

    def meta(self):
        return self._get_json("/v1/meta")

    # Paid routes (free when the API cannot sell a fresh, covered answer)
    def risk(self, mint, max_price_usdc=None):
        return self._paid("/v1/risk/" + self._mint(mint), max_price_usdc)

    def last(self, mint, max_price_usdc=None):
        """Last-known odds (<= 30 min old, labelled stale, confidence downgraded); 0.002 USDC."""
        return self._paid("/v1/risk/" + self._mint(mint) + "/last", max_price_usdc)

    def board(self, max_price_usdc=None):
        return self._paid("/v1/board", max_price_usdc)

    def close(self):
        try:
            self.http.close()
        except Exception:
            raise HodlOddsError("HTTP cleanup failed; reconcile any previous paid result before repeating it") from None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.close()
        except HodlOddsError:
            if exc[0] is None:
                raise
            # Preserve an existing payment/reconciliation error during cleanup.

    # Internals
    @staticmethod
    def _mint(mint):
        if not _address(mint):
            raise HodlOddsError("mint must be a Solana address")
        return quote(mint, safe="")

    def _get_json(self, path):
        r = self._request(self.base + path)
        if r.status_code != 200:
            raise HodlOddsError("HTTP %d on %s" % (r.status_code, path), r.status_code, _body(r))
        return _json_object(r)

    def _request(self, url, headers=None):
        # local (2026-10-17, Codex r54): bounded streaming also limits malicious
        # decoded gzip bodies. No redirects, environment proxy or automatic retry.
        try:
            with self.http.stream("GET", url, headers=headers or {}) as r:
                items = list(r.headers.multi_items())
                if sum(len(k.encode()) + len(v.encode()) for k, v in items) > MAX_HEADERS:
                    raise HodlOddsError("response headers exceed the client size limit", r.status_code)
                normalized = {}
                for k, v in items:
                    key = k.lower()
                    if key.startswith(("payment-", "x-payment")):
                        if key in normalized or len(v) > MAX_PAYMENT_HEADER or not v.isascii() or "\n" in v or "\r" in v:
                            raise HodlOddsError("duplicate, invalid or oversized payment header", r.status_code)
                    normalized[key] = v
                if 300 <= r.status_code < 400:
                    raise HodlOddsError("redirect refused; use the final endpoint", r.status_code)
                body = bytearray()
                for chunk in r.iter_bytes():
                    if len(body) + len(chunk) > MAX_BODY:
                        raise HodlOddsError("response body exceeds the client size limit", r.status_code)
                    body.extend(chunk)
                return _Reply(r.status_code, normalized, bytes(body))
        except HodlOddsError:
            raise
        except Exception:
            raise HodlOddsError("HTTP request failed; request details are omitted") from None

    def _protocol(self):
        if self._codecs is None:
            try:
                if version("x402") != SDK_VERSION:
                    raise ValueError()
                from x402.http import (decode_payment_required_header, decode_payment_response_header,
                                       encode_payment_signature_header)
                self._codecs = {"decode": decode_payment_required_header, "settled": decode_payment_response_header,
                                "encode": encode_payment_signature_header}
            except Exception:
                raise HodlOddsError("install the pinned x402[svm]==%s SDK" % SDK_VERSION) from None
        return self._codecs

    def _sdk_client(self):
        if self._sdk is None:
            if self._keypair_path is None:
                raise HodlOddsError("a keypair file is required for paid answers")
            self._protocol()
            try:
                from x402 import x402ClientSync
                from x402.mechanisms.svm.constants import (SOLANA_DEVNET_CAIP2, SOLANA_MAINNET_CAIP2,
                                                           USDC_DEVNET_ADDRESS, USDC_MAINNET_ADDRESS)
                from x402.mechanisms.svm.exact.client import ExactSvmScheme
                from x402.mechanisms.svm.signers import KeypairSigner
                pinned = {"devnet": (SOLANA_DEVNET_CAIP2, USDC_DEVNET_ADDRESS),
                          "mainnet": (SOLANA_MAINNET_CAIP2, USDC_MAINNET_ADDRESS)}[self.network]
                if pinned != (self.net["caip2"], self.net["usdc"]):
                    raise HodlOddsError("pinned SDK network constants changed")
                signer = KeypairSigner(_load_keypair(self._keypair_path))
                client = x402ClientSync().register(self.net["caip2"], ExactSvmScheme(signer, rpc_url=self.rpc_url))
                self._sdk = dict(self._codecs, payer=signer.address, client=client)
            except HodlOddsError:
                raise
            except Exception:
                raise HodlOddsError("cannot initialize payment SDK; no authorization was sent") from None
        return self._sdk

    def _check_offer(self, data, limit):
        if not isinstance(data, dict) or type(data.get("x402Version")) is not int or data.get("x402Version") != 2:
            raise HodlOddsError("only x402 v2 offers are accepted")
        accepts = data.get("accepts")
        if not isinstance(accepts, list) or len(accepts) != 1:
            raise HodlOddsError("expected exactly one payment option")
        row = accepts[0]
        if not isinstance(row, dict):
            raise HodlOddsError("invalid payment requirement")
        if row.get("network") != self.net["caip2"]:
            raise HodlOddsError("server asks for a different network than " + self.network)
        if row.get("scheme") != "exact" or row.get("asset") != self.net["usdc"]:
            raise HodlOddsError("only exact USDC payments are accepted")
        if self.pay_to is None or row.get("payTo") != self.pay_to:
            raise HodlOddsError("server payTo differs from the pinned pay_to address")
        amount = row.get("amount")
        if not (isinstance(amount, str) and re.fullmatch(r"[1-9][0-9]{0,6}", amount) and int(amount) <= limit):
            raise HodlOddsError("price is invalid or above your USDC limit")
        extra = row.get("extra")
        if not isinstance(extra, dict) or extra.get("paymentFlow") != "authorization":
            raise HodlOddsError("only authorization payment flow is accepted")
        if not _address(extra.get("feePayer")):
            raise HodlOddsError("a valid facilitator feePayer is required")
        timeout = row.get("maxTimeoutSeconds")
        if type(timeout) is not int or not 0 < timeout <= 300:
            raise HodlOddsError("payment timeout must be between 1 and 300 seconds")
        if "memo" in extra and (not isinstance(extra["memo"], str) or len(extra["memo"].encode()) > 256):
            raise HodlOddsError("invalid or oversized payment memo")
        if "recentBlockhash" in extra and not _address(extra["recentBlockhash"]):
            raise HodlOddsError("invalid payment blockhash")
        return int(amount)

    def _paid(self, path, max_price_usdc):
        limit = self.max_atomic if max_price_usdc is None else _usdc(max_price_usdc, "max_price_usdc")
        url = self.base + path
        first = self._request(url)
        if first.status_code == 200:
            return Answer(200, _json_object(first))
        if first.status_code != 402:
            # Free refusals: not covered (404), feed interrupted (503), too old (410), ...
            raise HodlOddsError("free answer HTTP %d (nothing paid)" % first.status_code, first.status_code, _body(first))
        header = first.headers.get("payment-required")
        if not header:
            raise HodlOddsError("402 without a valid PAYMENT-REQUIRED header; nothing paid", 402)
        protocol = self._protocol()
        try:
            parsed = protocol["decode"](header)
            offer = parsed.model_dump(by_alias=True, exclude_none=True)
            amount = self._check_offer(offer, limit)
        except HodlOddsError:
            raise
        except Exception:
            raise HodlOddsError("invalid PAYMENT-REQUIRED offer; nothing signed", 402) from None
        # Read the key only after all checks that do not need the payer address.
        sdk = self._sdk_client()
        if not _address(sdk["payer"]) or sdk["payer"] == self.pay_to:
            raise HodlOddsError("payer and payTo must be different wallets")
        if offer["accepts"][0]["extra"]["feePayer"] == sdk["payer"]:
            raise HodlOddsError("payer must differ from the facilitator fee payer")
        try:
            payload = sdk["client"].create_payment_payload(parsed)
            signature = protocol["encode"](payload)
            if (not isinstance(signature, str) or not signature or len(signature) > MAX_SIGNATURE_HEADER
                    or not signature.isascii() or any(c.isspace() for c in signature)):
                raise ValueError()
        except Exception:
            raise HodlOddsError("authorization construction failed; nothing sent and not retried") from None
        payment = {"amount_usdc": _fmt(str(amount)), "network": self.network, "success": None,
                   "transaction": None, "payer": sdk["payer"], "pay_to": self.pay_to, "explorer": None}
        paid = None
        try:
            # Exactly one authorized HTTP attempt. A lost/error response does
            # not establish absence of settlement, even on normally free errors.
            paid = self._request(url, headers={"PAYMENT-SIGNATURE": signature})
            receipt = paid.headers.get("payment-response")
            if receipt:
                info = protocol["settled"](receipt).model_dump(by_alias=True, exclude_none=True)
                tx = info.get("transaction")
                if _base58_size(tx) == 64:
                    payment["transaction"] = tx
                    payment["explorer"] = "https://explorer.solana.com/tx/%s%s" % (tx, self.net["explorer"])
                if (info.get("success") is not True or info.get("network") != self.net["caip2"]
                        or payment["transaction"] is None
                        or ("payer" in info and info["payer"] != sdk["payer"])
                        or ("amount" in info and info["amount"] != str(amount))):
                    raise HodlOddsError("invalid or unsuccessful PAYMENT-RESPONSE")
                payment["success"] = True  # Server receipt, not independent RPC finality proof.
            if paid.status_code != 200:
                raise HodlOddsError("authorized request returned HTTP %d" % paid.status_code)
            if not receipt:
                raise HodlOddsError("200 without PAYMENT-RESPONSE")
            return Answer(200, _json_object(paid), payment)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, HodlOddsError) else "invalid payment response"
            message = (reason + ". Settlement may have occurred; not retried. Reconcile %(amount_usdc)s USDC "
                       "on %(network)s from %(payer)s to %(pay_to)s against finalized wallet history and "
                       "the server payment records before another paid call. Transaction: %(transaction)s." % payment)
            raise HodlOddsError(message, paid.status_code if paid else getattr(exc, "status", None),
                               _body(paid) if paid else None, payment=payment) from None


def _fmt(atomic):
    try:
        return format((Decimal(int(atomic)) / USDC_ATOMIC).normalize(), "f")
    except (TypeError, ValueError, DecimalException):
        return "?"


def _body(response):
    try:
        return response.json()
    except Exception:
        return {"text": response.text[:500]}


def _json_object(response):
    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except Exception:
        raise HodlOddsError("response is not a JSON object", response.status_code) from None


def main(argv=None):
    p = argparse.ArgumentParser(description="HodlOdds x402 client")
    p.add_argument("command", choices=["status", "meta", "risk", "last", "board"])
    p.add_argument("mint", nargs="?")
    p.add_argument("--base-url", default=os.environ.get("HODLODDS_BASE_URL", "https://api.hodlodds.com"))
    p.add_argument("--network", default=os.environ.get("HODLODDS_NETWORK", "devnet"), choices=sorted(NETWORKS))
    p.add_argument("--keypair", default=os.environ.get("HODLODDS_KEYPAIR"))
    p.add_argument("--pay-to", default=os.environ.get("HODLODDS_PAY_TO"))
    p.add_argument("--max-price", default=os.environ.get("HODLODDS_MAX_PRICE", "0.01"), help="USDC, default 0.01")
    p.add_argument("--rpc-url")
    args = list(sys.argv[1:] if argv is None else argv)
    a = p.parse_args(args)
    if a.command in ("risk", "last") and not a.mint:
        p.error(a.command + " needs a mint address")
    if a.command not in ("risk", "last") and a.mint:
        p.error(a.command + " does not take a mint")
    explicit = lambda flag: any(v == flag or v.startswith(flag + "=") for v in args)
    if a.network == "mainnet" and not (explicit("--network") and explicit("--pay-to")):
        p.error("mainnet requires explicit --network mainnet and --pay-to; environment defaults are not enough")
    try:
        with HodlOdds(a.base_url, keypair=a.keypair, network=a.network, pay_to=a.pay_to,
                      max_price_usdc=a.max_price, rpc_url=a.rpc_url) as client:
            if a.command in ("status", "meta"):
                print(json.dumps(getattr(client, a.command)(), indent=2))
                return 0
            answer = getattr(client, a.command)(*([a.mint] if a.mint and a.command != "board" else []))
            print(json.dumps(answer.data, indent=2))
            if answer.payment:
                print("\nPaid %(amount_usdc)s USDC on %(network)s from %(payer)s\nTransaction: %(explorer)s" % answer.payment)
            return 0
    except HodlOddsError as e:
        print("HodlOdds: " + str(e), file=sys.stderr)
        if e.body is not None:
            print(json.dumps(e.body, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
