"""HodlOdds Python client: x402 v2 exact USDC on Solana or Base.

    from hodlodds import HodlOdds
    client = HodlOdds("https://api.hodlodds.com", keypair="~/.config/hodlodds/payer.json",
                      network="solana", pay_to="<RECIPIENT FROM /v1/meta>")
    answer = client.risk("<MINT>")
    print(answer.data["row"], answer.payment)

Free routes (status, meta, "not covered", "feed interrupted", "too old") never sign
anything. A payment is signed only after the server's 402 offer passes every check:
x402 v2, the chosen network, exact USDC, the pinned payTo address and a price
no higher than your limit. A payment is never retried automatically. The key file
(Solana JSON or EVM hex/JSON, mode 0600) is read locally and never printed or sent anywhere;
only the SDK-signed USDC transfer authorization leaves the machine.
"""
import argparse
import hashlib
import json
import math
import os
import random
import re
import stat
import sys
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from importlib.metadata import version
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

SDK_VERSION = "2.25.0"
NETWORKS = ("devnet", "solana", "mainnet", "base", "base-sepolia")
ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
USDC_ATOMIC = Decimal(10) ** 6
HARD_MAX_USDC = Decimal("1")
BOARD_MAX_USDC = Decimal("2.5")
BOARD_ATOMIC_PER_COIN = 5000
BOARD_SORTS = ("lowest_risk", "safest", "riskiest", "liquidity", "newest", "active")
BOARD_FILTERS = ("max_risk", "min_risk", "min_liquidity_sol", "min_age_min", "max_age_min",
                 "min_trades_5", "min_confidence", "include_crashed")
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


class PendingPayment(dict):
    """Public receipt fields; redeem credentials stay out of repr/JSON/logs."""
    def __init__(self, fields, *, signature, url, base):
        super().__init__(fields)
        self._signature, self._url, self._base = signature, url, base


def save_pending(payment, directory=None):
    if not isinstance(payment, PendingPayment):
        raise HodlOddsError("no pending payment to save")
    directory = Path(directory or '~/.config/hodlodds/pending').expanduser().absolute()
    try:
        # Never traverse a symlink, including one on a parent directory.
        if any(p.is_symlink() for p in (directory, *directory.parents)):
            raise ValueError()
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(dfd).st_uid != os.geteuid():
                raise ValueError()
            os.fchmod(dfd, 0o700)
            name = secrets.token_hex(16) + '.json'
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            with os.fdopen(fd, 'w') as target:
                os.fchmod(target.fileno(), 0o600)
                json.dump(dict(schema='hodlodds.pending.v1', payment=dict(payment),
                    signature=payment._signature, url=payment._url, base=payment._base), target)
                target.flush(); os.fsync(target.fileno())
        finally:
            os.close(dfd)
        return directory / name
    except Exception:
        raise HodlOddsError("cannot save pending payment securely") from None


def load_pending(filename):
    try:
        path = Path(filename).expanduser().absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid() or info.st_size > 100000):
                raise ValueError()
            value = json.load(source)
        if (value['schema'] != 'hodlodds.pending.v1'
                or not isinstance(value['payment'], dict)
                or value['payment'].get('network') not in NETWORKS
                or not isinstance(value['payment'].get('pay_to'), str)
                or not isinstance(value['url'], str) or not isinstance(value['base'], str)
                or not isinstance(value['signature'], str) or not value['signature']
                or len(value['signature']) > MAX_SIGNATURE_HEADER
                or not value['signature'].isascii() or any(c.isspace() for c in value['signature'])):
            raise ValueError()
        return PendingPayment(value['payment'], signature=value['signature'], url=value['url'], base=value['base'])
    except Exception:
        raise HodlOddsError("cannot load pending payment: use its private 0600 non-symlink file") from None


class Answer:
    def __init__(self, status, data, payment=None):
        self.status, self.data, self.payment = status, data, payment

    @property
    def paid(self):
        return self.payment is not None

    def __repr__(self):
        return "Answer(status=%r, paid=%r)" % (self.status, self.paid)


def _network_config(network):
    # never silently fall back to a remembered
    # token address or a different chain. Missing pinned SDK constants is fatal.
    try:
        if version("x402") != SDK_VERSION:
            raise ValueError()
        if network in ("devnet", "solana", "mainnet"):
            from x402.mechanisms.svm import constants as svm
            test = network == "devnet"
            return {"family": "svm", "testnet": test,
                    "caip2": svm.SOLANA_DEVNET_CAIP2 if test else svm.SOLANA_MAINNET_CAIP2,
                    "usdc": svm.USDC_DEVNET_ADDRESS if test else svm.USDC_MAINNET_ADDRESS,
                    "rpc": svm.DEVNET_RPC_URL if test else svm.MAINNET_RPC_URL,
                    "explorer": "https://explorer.solana.com/tx/", "explorer_suffix": "?cluster=devnet" if test else ""}
        from x402.mechanisms.evm.v1.constants import V1_NETWORK_CHAIN_IDS
        from x402.mechanisms.evm.default_assets import DEFAULT_ASSETS
        caip2 = "eip155:" + str(V1_NETWORK_CHAIN_IDS[network])
        choices = [a for a in DEFAULT_ASSETS[caip2] if a["symbol"] == "USDC" and a["decimals"] == 6]
        if len(choices) != 1 or choices[0].get("asset_transfer_method", "eip3009") != "eip3009":
            raise ValueError()
        asset = choices[0]; test = network == "base-sepolia"
        return {"family": "evm", "testnet": test, "caip2": caip2, "usdc": asset["asset"],
                "name": asset["name"], "version": asset["version"], "rpc": None,
                "explorer": "https://sepolia.basescan.org/tx/" if test else "https://basescan.org/tx/", "explorer_suffix": ""}
    except Exception:
        raise HodlOddsError("pinned SDK network/USDC constants unavailable; install x402[svm,evm]==" + SDK_VERSION) from None


def _evm_address(value):
    try:
        from eth_utils import is_checksum_address
        return isinstance(value, str) and bool(is_checksum_address(value)) and int(value, 16) != 0
    except Exception:
        return False


def _usdc(value, name, maximum=HARD_MAX_USDC):
    message = name + " must be above 0, at most %s USDC, with at most 6 decimals" % maximum
    try:
        text = str(value)
        if len(text) > 128:
            raise ValueError()
        amount = Decimal(text)
        if not amount.is_finite() or not 0 < amount <= maximum:
            raise ValueError()
        # Decimal multiplication can round away
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
        if not 0 < atomic <= int(maximum * USDC_ATOMIC):
            raise ValueError()
        return atomic
    except (DecimalException, ValueError, TypeError, OverflowError):
        pass
    raise HodlOddsError(message)


# Standalone client vocabulary; checked against cloud.markets by offline tests.
# /v1/markets remains authoritative when the server adds markets.
AVAILABLE_CHAINS = {"solana": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"}
AVAILABLE_PLATFORMS = {"pumpfun": "pumpfun", "pump": "pumpfun", "pump.fun": "pumpfun"}


def _market_query(chain=None, platform=None):
    result = {}
    chains = {**AVAILABLE_CHAINS, **{v.lower(): v for v in AVAILABLE_CHAINS.values()}}
    for name, raw, allowed in (("chain", chain, chains), ("platform", platform, AVAILABLE_PLATFORMS)):
        if raw is None:
            continue
        parts = raw.split(",") if isinstance(raw, str) else raw
        if (not isinstance(parts, (list, tuple)) or not 1 <= len(parts) <= 10
                or any(not isinstance(p, str) or not p.strip() for p in parts)):
            raise HodlOddsError(name + " must contain 1..10 non-empty values")
        values = [allowed.get(p.strip().lower()) for p in parts]
        if None in values:
            raise HodlOddsError(name + " not available; see /v1/markets")
        if len(set(values)) != len(values):
            raise HodlOddsError("duplicate " + name + " value")
        result[name + "s"] = sorted(values)
    return result


def _market_path(path, chain=None, platform=None):
    query = _market_query(chain, platform)
    return path + ("?" + urlencode({k[:-1]: ",".join(v) for k, v in query.items()}) if query else "")


def _board_query(limit, sort, filters, mints=None, chain=None, platform=None):
    # validate before HTTP/key access and send a
    # canonical query; the returned offer must describe this same selection.
    if mints is not None:
        if (not isinstance(mints, (list, tuple)) or not 1 <= len(mints) <= 100
                or any(not _address(mint) for mint in mints)):
            raise HodlOddsError("mints must contain 1..100 valid Solana mint addresses")
        if len(set(mints)) != len(mints):
            raise HodlOddsError("duplicate mints are not allowed")
        mints = sorted(mints)
    if limit is None:
        limit = len(mints) if mints else 50
    minimum, maximum = (1, 100) if mints else (10, 500)
    if type(limit) is not int or not minimum <= limit <= maximum:
        raise HodlOddsError("limit must be an integer between %d and %d" % (minimum, maximum))
    if sort not in BOARD_SORTS:
        raise HodlOddsError("sort must be one of: " + ", ".join(BOARD_SORTS))
    unknown = set(filters) - set(BOARD_FILTERS)
    if unknown:
        raise HodlOddsError("unknown board filter: " + ", ".join(sorted(unknown)))
    normalized = {}
    for name in BOARD_FILTERS:
        if name not in filters:
            continue
        value = filters[name]
        if name == "include_crashed":
            if type(value) is not bool:
                raise HodlOddsError("include_crashed must be a boolean")
        elif name == "min_confidence":
            if value not in ("high", "medium", "low"):
                raise HodlOddsError("min_confidence must be high, medium or low")
        elif name == "min_trades_5":
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise HodlOddsError("min_trades_5 must be an integer between 0 and 9223372036854775807")
        else:
            try:
                if isinstance(value, bool) or value is None:
                    raise ValueError()
                value = float(value)
                if not math.isfinite(value) or value < 0 or (name in ("min_risk", "max_risk") and value > 1):
                    raise ValueError()
                value = 0.0 if value == 0 else value
            except (ValueError, TypeError, OverflowError):
                raise HodlOddsError(name + " must be a finite number " +
                                    ("between 0 and 1" if name in ("min_risk", "max_risk") else "at least 0")) from None
        normalized[name] = value
    for lo, hi in (("min_risk", "max_risk"), ("min_age_min", "max_age_min")):
        if lo in normalized and hi in normalized and normalized[lo] > normalized[hi]:
            raise HodlOddsError(lo + " must not exceed " + hi)
    query = {"limit": limit, "sort": sort, "filters": normalized, **_market_query(chain, platform)}
    if mints is not None:
        query["mints"] = mints
    return query


def _bound_query(query):
    """Compact payment binding; selection still uses the original URL list."""
    result = dict(query)
    if 'mints' in result:
        mints = result.pop('mints')
        result['mints_sha256'] = hashlib.sha256(','.join(sorted(mints)).encode('ascii')).hexdigest()
        result['mints_count'] = len(mints)
    return result


def _board_params(query):
    params = {"limit": query["limit"], "sort": query["sort"], **query["filters"]}
    if "include_crashed" in params:
        params["include_crashed"] = "true" if params["include_crashed"] else "false"
    if "mints" in query:
        params["mints"] = ",".join(query["mints"])
    for name in ("chains", "platforms"):
        if name in query:
            params[name[:-1]] = ",".join(query[name])
    return params


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


def _read_key_file(path):
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
        return raw
    except Exception:
        pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    raise HodlOddsError("cannot load key: use a regular non-symlink file, mode 0600, at most 4096 bytes")


def _load_keypair(path):
    try:
        data = json.loads(_read_key_file(path))
        if not (isinstance(data, list) and len(data) == 64 and all(type(v) is int and 0 <= v <= 255 for v in data)):
            raise ValueError()
        from solders.keypair import Keypair
        return Keypair.from_bytes(bytes(data))
    except Exception:
        # No parser/SDK error, filename, JSON doc or key bytes escapes this seam.
        pass
    raise HodlOddsError("cannot load key: use a regular non-symlink solana-keygen file, mode 0600, and installed SDK dependencies")


def _load_evm_account(path):
    try:
        raw = _read_key_file(path).decode("ascii").strip()
        # Accept a hex file, JSON hex string, {"private_key": "0x..."}, or a
        # JSON array of exactly 32 bytes. No mnemonics or encrypted-keystore guess.
        if raw.startswith(("[", "{", '"')):
            raw = json.loads(raw)
        if isinstance(raw, dict):
            if set(raw) != {"private_key"}:
                raise ValueError()
            raw = raw["private_key"]
        if isinstance(raw, list):
            if len(raw) != 32 or any(type(v) is not int or not 0 <= v <= 255 for v in raw):
                raise ValueError()
            key = bytes(raw)
        elif isinstance(raw, str) and re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", raw):
            key = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
        else:
            raise ValueError()
        from eth_account import Account
        return Account.from_key(key)
    except Exception:
        pass
    raise HodlOddsError("cannot load EVM key: use a regular non-symlink 0600 hex/JSON private-key file and installed x402[evm] dependencies")


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
                 rpc_url=None, timeout=30.0, access_headers=None, paid_timeout=90.0):
        if network not in NETWORKS:
            raise HodlOddsError("network must be solana, devnet, base or base-sepolia (mainnet is a Solana alias)")
        if network in ("mainnet", "solana", "base") and pay_to is None:
            raise HodlOddsError("mainnet requires an explicit pinned pay_to address")
        self.base = _base_url(base_url)
        self.network, self.net = network, _network_config(network)
        self._address_check = _evm_address if self.net["family"] == "evm" else _address
        if pay_to is not None and not self._address_check(pay_to):
            raise HodlOddsError("pay_to must be a nonzero EIP-55 checksum address" if self.net["family"] == "evm" else "pay_to must be a Solana address")
        self.pay_to, self.max_atomic = pay_to, _usdc(max_price_usdc, "max_price_usdc")
        # The paid request waits for verification and on-chain settlement; in rare
        # facilitator trouble the server needs up to ~70 s (retry + on-chain
        # confirmation). Leaving earlier means paying without receiving the answer.
        if type(paid_timeout) not in (int, float) or not 30 <= paid_timeout <= 600:
            raise HodlOddsError("paid_timeout must be between 30 and 600 seconds")
        self.paid_timeout = float(paid_timeout)
        rpc = rpc_url or self.net["rpc"]
        self.rpc_url, self._keypair_path = _base_url(rpc, rpc=True) if rpc else None, keypair
        self._sdk, self._codecs = None, None
        headers = dict(access_headers or {})
        for k, v in headers.items():
            if (not isinstance(k, str) or not isinstance(v, str) or len(k) + len(v) > MAX_PAYMENT_HEADER
                    or any(c in k + v for c in "\r\n") or k.lower().startswith(("payment-", "x-payment"))
                    or k.lower() in ("host", "content-length", "transfer-encoding")):
                raise HodlOddsError("access_headers contains an invalid or reserved payment/transport header")
        headers["User-Agent"] = "hodlodds-python/0.3.1"
        try:
            self.http = _new_http(timeout, headers)
        except Exception:
            raise HodlOddsError("cannot initialize HTTP client; install httpx and check configuration") from None

    # Free routes
    def status(self):
        return self._get_json("/v1/status")

    def meta(self):
        return self._get_json("/v1/meta")

    def markets(self):
        return self._get_json("/v1/markets")

    def coverage(self, mint, chain=None, platform=None):
        return self._get_json(_market_path("/v1/coverage/" + self._mint(mint), chain, platform))

    # Paid routes (free when the API cannot sell a fresh, covered answer)
    def risk(self, mint, max_price_usdc=None, *, chain=None, platform=None):
        return self._paid(_market_path("/v1/risk/" + self._mint(mint), chain, platform), max_price_usdc)

    def last(self, mint, max_price_usdc=None, *, chain=None, platform=None):
        """Labelled last-known odds (<= 30 min); stale confidence is downgraded; 0.002 USDC."""
        return self._paid(_market_path("/v1/risk/" + self._mint(mint) + "/last", chain, platform), max_price_usdc)

    def board(self, limit=None, sort="lowest_risk", max_price_usdc=None, *, mints=None, chain=None, platform=None, **filters):
        """Current covered SOL ranking; 0.005 USDC per delivered coin, at most 500.

        Pass a per-call maximum covering the dynamic total (e.g. 0.5 for 100
        coins). With mints, limit defaults to list size (1..100); otherwise 50
        (10..500). An empty match set is free. A changed unsigned offer may be
        refreshed once; an authorized request is never automatically retried.
        Low-risk defaults are applied by the server, never added to our binding.
        """
        query = _board_query(limit, sort, filters, mints, chain, platform)
        params = _board_params(query)
        return self._paid("/v1/board?" + urlencode(params), max_price_usdc, board_query=query)

    def watch(self, mint, every_minutes=5, *, max_spend_usdc, max_calls=None,
              alert_above=None, stop_on_stale=False, chain=None, platform=None):
        """Closeable generator of Answers; estimate and spent_atomic live on the iterator."""
        return Watch(self, _market_path("/v1/risk/" + self._mint(mint), chain, platform), every_minutes,
                     max_spend_usdc, max_calls, alert_above, stop_on_stale)

    def watch_board(self, every_minutes=5, *, max_spend_usdc, max_calls=None,
                    alert_above=None, stop_on_stale=False, limit=None, sort="lowest_risk",
                    max_price_usdc=None, mints=None, chain=None, platform=None, **filters):
        """Watch a board with the same query binding and price checks as board()."""
        query = _board_query(limit, sort, filters, mints, chain, platform)
        params = _board_params(query)
        return Watch(self, "/v1/board?" + urlencode(params), every_minutes,
                     max_spend_usdc, max_calls, alert_above, stop_on_stale,
                     max_price_usdc=max_price_usdc, board_query=query)

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

    def _request(self, url, headers=None, timeout=None):
        # bounded streaming also limits malicious
        # decoded gzip bodies. No redirects, environment proxy or automatic retry.
        try:
            with self.http.stream("GET", url, headers=headers or {}, **({"timeout": timeout} if timeout else {})) as r:
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
                raise HodlOddsError("install the pinned x402[svm,evm]==%s SDK" % SDK_VERSION) from None
        return self._codecs

    def _sdk_client(self):
        if self._sdk is None:
            if self._keypair_path is None:
                raise HodlOddsError("a keypair file is required for paid answers")
            self._protocol()
            try:
                from x402 import x402ClientSync
                if _network_config(self.network) != self.net:
                    raise HodlOddsError("pinned SDK network constants changed")
                if self.net["family"] == "svm":
                    from x402.mechanisms.svm.exact.client import ExactSvmScheme
                    from x402.mechanisms.svm.signers import KeypairSigner
                    signer = KeypairSigner(_load_keypair(self._keypair_path))
                    scheme = ExactSvmScheme(signer, rpc_url=self.rpc_url)
                else:
                    from x402.mechanisms.evm.exact.client import ExactEvmScheme
                    from x402.mechanisms.evm.signers import EthAccountSigner
                    signer = EthAccountSigner(_load_evm_account(self._keypair_path))
                    scheme = ExactEvmScheme(signer)
                client = x402ClientSync().register(self.net["caip2"], scheme)
                self._sdk = dict(self._codecs, payer=signer.address, client=client)
            except HodlOddsError:
                raise
            except Exception:
                raise HodlOddsError("cannot initialize payment SDK; no authorization was sent") from None
        return self._sdk

    def _same_address(self, left, right):
        return (isinstance(left, str) and isinstance(right, str) and
                (left.lower() == right.lower() if self.net["family"] == "evm" else left == right))

    def _select_offer(self, data):
        if not isinstance(data, dict) or type(data.get("x402Version")) is not int or data.get("x402Version") != 2:
            raise HodlOddsError("only x402 v2 offers are accepted")
        accepts = data.get("accepts")
        if not isinstance(accepts, list) or not 1 <= len(accepts) <= 16:
            raise HodlOddsError("invalid payment options")
        matches = [(i, row) for i, row in enumerate(accepts) if isinstance(row, dict)
                   and row.get("network") == self.net["caip2"] and self.pay_to is not None
                   and self._same_address(row.get("payTo"), self.pay_to)]
        if len(matches) != 1:
            raise HodlOddsError("expected one payment option matching the configured network and pinned pay_to")
        return matches[0]

    def _check_offer(self, data, limit, board_query=None):
        _, row = self._select_offer(data)
        if row.get("scheme") != "exact" or not self._same_address(row.get("asset"), self.net["usdc"]):
            raise HodlOddsError("only exact USDC payments are accepted")
        if not self._address_check(row.get("payTo")) or not self._same_address(row.get("payTo"), self.pay_to):
            raise HodlOddsError("server payTo differs from the pinned pay_to address")
        amount = row.get("amount")
        if not (isinstance(amount, str) and re.fullmatch(r"[1-9][0-9]{0,6}", amount) and int(amount) <= limit):
            raise HodlOddsError("price is invalid or above your USDC limit")
        extra = row.get("extra")
        if not isinstance(extra, dict) or extra.get("paymentFlow") != "authorization":
            raise HodlOddsError("only authorization payment flow is accepted")
        timeout = row.get("maxTimeoutSeconds")
        if type(timeout) is not int or not 0 < timeout <= 300:
            raise HodlOddsError("payment timeout must be between 1 and 300 seconds")
        if self.net["family"] == "svm":
            if not _address(extra.get("feePayer")):
                raise HodlOddsError("a valid facilitator feePayer is required")
            if "memo" in extra and (not isinstance(extra["memo"], str) or len(extra["memo"].encode()) > 32):
                raise HodlOddsError("invalid or oversized payment memo (maximum 32 bytes)")
            if "recentBlockhash" in extra and not _address(extra["recentBlockhash"]):
                raise HodlOddsError("invalid payment blockhash")
        else:
            if extra.get("assetTransferMethod", "eip3009") != "eip3009":
                raise HodlOddsError("only EIP-3009 exact USDC authorizations are accepted; Permit2 is disabled")
            if extra.get("name") != self.net["name"] or extra.get("version") != self.net["version"]:
                raise HodlOddsError("USDC signing domain differs from the pinned SDK")
            if any(name in extra for name in ("memo", "feePayer", "recentBlockhash")):
                raise HodlOddsError("Solana transaction fields are not accepted in a Base offer")
        if board_query is not None:
            board = extra.get("hodloddsBoard")
            if (not isinstance(board, dict) or type(board.get("version")) is not int or board["version"] != 1
                    or set(board) != {"version", "query", "priced_n", "nonce"}
                    or json.dumps(board.get("query"), sort_keys=True, allow_nan=False)
                    != json.dumps(_bound_query(board_query), sort_keys=True, allow_nan=False)
                    or type(board.get("priced_n")) is not int
                    or not 1 <= board["priced_n"] <= board_query["limit"]
                    or not isinstance(board.get("nonce"), str) or not re.fullmatch("[0-9a-f]{32}", board["nonce"])
                    or int(amount) != board["priced_n"] * BOARD_ATOMIC_PER_COIN):
                raise HodlOddsError("board offer does not match the requested query and per-coin price")
            binding_signature = extra.get("hodloddsBoardSignature")
            if not isinstance(binding_signature, str) or not re.fullmatch("[0-9a-f]{64}", binding_signature):
                raise HodlOddsError("board offer is missing its server binding signature")
            # 32 hex chars: the SPL Memo instruction must fit the SDK's 20,000 CU limit.
            expected_memo = hashlib.sha256(b"hodlodds.board.v1\0" + json.dumps(
                board, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()[:32]
            if self.net["family"] == "svm" and extra.get("memo") != expected_memo:
                raise HodlOddsError("board payment memo does not bind the requested query and count")
        return int(amount)

    def redeem(self, payment):
        """One explicit resend of the original authorization, without signing."""
        if (not isinstance(payment, PendingPayment) or payment._base != self.base
                or payment.get('network') != self.network or payment.get('pay_to') != self.pay_to
                or not isinstance(payment._url, str) or not payment._url.startswith(self.base + '/v1/')
                or not isinstance(payment._signature, str) or not payment._signature
                or len(payment._signature) > MAX_SIGNATURE_HEADER
                or not payment._signature.isascii() or any(c.isspace() for c in payment._signature)):
            raise HodlOddsError("redeem requires the original payment, endpoint, network and recipient")
        parsed = urlparse(payment._url)
        if (parsed.fragment or parsed.username or parsed.password or '\\' in payment._url
                or not (parsed.path == urlparse(self.base).path + '/v1/board'
                    or parsed.path.startswith(urlparse(self.base).path + '/v1/risk/'))):
            raise HodlOddsError("invalid redeem route")
        response = None
        try:
            response = self._request(payment._url, headers={'PAYMENT-SIGNATURE': payment._signature, 'HodlOdds-Redeem': '1'})
            tx = response.headers.get('hodlodds-redeemed')
            if response.status_code != 200 or not tx:
                raise HodlOddsError("redeem returned HTTP %d; no new payment was signed; keep the pending payment" % response.status_code)
            if not (_base58_size(tx) == 64 if self.net['family'] == 'svm' else
                    bool(re.fullmatch('0x[0-9a-fA-F]{64}', tx))):
                raise HodlOddsError("invalid redeem receipt")
            receipt = dict(payment, success=True, transaction=tx,
                           explorer=self.net['explorer'] + tx + self.net['explorer_suffix'], redeemed=True)
            return Answer(200, _json_object(response), receipt)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, HodlOddsError) else 'redeem response unavailable; not retried'
            raise HodlOddsError(reason, response.status_code if response else None, payment=payment) from None

    def _paid(self, path, max_price_usdc, board_query=None, *, _watch=None):
        maximum = BOARD_MAX_USDC if board_query is not None else HARD_MAX_USDC
        limit = self.max_atomic if max_price_usdc is None else _usdc(max_price_usdc, "max_price_usdc", maximum)
        url = self.base + path
        first = self._request(url)
        # Only refresh an UNSIGNED board request. In particular, this code is
        # outside the authorized-request block: even a charged:no 409 after
        # signing must return to the caller, never create another authorization.
        if board_query is not None and _offer_changed_unsigned(first):
            first = self._request(url)
        if first.status_code == 200:
            return Answer(200, _json_object(first))
        if first.status_code != 402:
            # Free refusals: not covered (404), feed interrupted (503), too old (410), ...
            # A second changed offer is a free watch skip, not an endless retry.
            free_change = board_query is not None and _offer_changed_unsigned(first)
            raise HodlOddsError("free answer HTTP %d (nothing paid)" % first.status_code,
                                first.status_code, _body(first),
                                payment={"charged": False, "success": False} if free_change else None)
        header = first.headers.get("payment-required")
        if not header:
            raise HodlOddsError("402 without a valid PAYMENT-REQUIRED header; nothing paid", 402)
        protocol = self._protocol()
        try:
            parsed = protocol["decode"](header)
            offer = parsed.model_dump(by_alias=True, exclude_none=True)
            amount = self._check_offer(offer, limit, board_query)
            index, selected = self._select_offer(offer)
            # never let the SDK's default selector
            # choose an unchecked alternative, including another same-chain offer.
            parsed = parsed.model_copy(update={"accepts": [parsed.accepts[index]]}, deep=True)
        except HodlOddsError:
            raise
        except Exception:
            raise HodlOddsError("invalid PAYMENT-REQUIRED offer; nothing signed", 402) from None
        if _watch is not None:
            _watch.check_budget(amount)
        # Read the key only after all checks that do not need the payer address.
        sdk = self._sdk_client()
        if not self._address_check(sdk["payer"]) or self._same_address(sdk["payer"], self.pay_to):
            raise HodlOddsError("payer and payTo must be different wallets")
        if self.net["family"] == "svm" and selected["extra"]["feePayer"] == sdk["payer"]:
            raise HodlOddsError("payer must differ from the facilitator fee payer")
        try:
            # SDK 2.25.0 itself defaults to $1.
            # Permit only this already-validated total; do not disable controls
            # or leave a board's larger allowance on the cached SDK instance.
            sdk["client"].set_spend_controls({"max_amount_per_payment": "$" + _fmt(str(amount))})
            try:
                payload = sdk["client"].create_payment_payload(parsed)
            finally:
                sdk["client"].set_spend_controls({"max_amount_per_payment": "$1"})
            signature = protocol["encode"](payload)
            if (not isinstance(signature, str) or not signature or len(signature) > MAX_SIGNATURE_HEADER
                    or not signature.isascii() or any(c.isspace() for c in signature)):
                raise ValueError()
        except Exception:
            raise HodlOddsError("authorization construction failed; nothing sent and not retried") from None
        payment = {"amount_usdc": _fmt(str(amount)), "network": self.network, "success": None,
                   "transaction": None, "payer": sdk["payer"], "pay_to": self.pay_to, "explorer": None}
        payment = PendingPayment(payment, signature=signature, url=url, base=self.base)
        paid = None
        try:
            if _watch is not None:
                # Reserve the accepted amount before transport. An uncertain
                # attempt is never refunded or retried by a watch.
                _watch.spent_atomic += amount
            # Exactly one authorized HTTP attempt. A lost/error response does
            # not establish absence of settlement, even on normally free errors.
            paid = self._request(url, headers={"PAYMENT-SIGNATURE": signature}, timeout=self.paid_timeout)
            receipt = paid.headers.get("payment-response")
            info = protocol["settled"](receipt).model_dump(by_alias=True, exclude_none=True) if receipt else {}
            if (paid.status_code != 200 and paid.headers.get('hodlodds-charged') == 'no'
                    and info.get('success') is not True and not info.get('transaction')):
                payment = dict(payment, charged=False, success=False)
                if _watch is not None:
                    _watch.spent_atomic -= amount
                body = _body(paid)
                reason = body.get('error', 'HTTP %d' % paid.status_code) if isinstance(body, dict) else 'HTTP %d' % paid.status_code
                raise HodlOddsError('refused before settlement: ' + str(reason), paid.status_code,
                                    body, payment=payment)
            if receipt:
                tx = info.get("transaction")
                if (_base58_size(tx) == 64 if self.net["family"] == "svm" else
                        isinstance(tx, str) and bool(re.fullmatch("0x[0-9a-fA-F]{64}", tx))):
                    payment["transaction"] = tx
                    payment["explorer"] = self.net["explorer"] + tx + self.net["explorer_suffix"]
                # Only a 402 carrying a reason is the facilitator declaring
                # failure; any other status leaves the outcome unknown.
                if info.get("success") is False and not tx and paid.status_code == 402 and info.get("errorReason"):
                    reason = info.get('errorReason')
                    reason = reason if isinstance(reason, str) and re.fullmatch('[a-zA-Z0-9_]{1,100}', reason) else 'unknown'
                    payment['success'] = False
                    raise HodlOddsError("the facilitator reported settlement failed: %s; usually nothing was charged — if your wallet shows the payment, redeem after ~2 minutes" % reason)
                if (info.get("success") is not True or info.get("network") != self.net["caip2"]
                        or payment["transaction"] is None
                        or ("payer" in info and not self._same_address(info["payer"], sdk["payer"]))
                        or ("amount" in info and info["amount"] != str(amount))):
                    raise HodlOddsError("invalid or unsuccessful PAYMENT-RESPONSE")
                payment["success"] = True  # Server receipt, not independent RPC finality proof.
            if paid.status_code != 200:
                raise HodlOddsError("authorized request returned HTTP %d" % paid.status_code)
            if not receipt:
                raise HodlOddsError("200 without PAYMENT-RESPONSE")
            return Answer(200, _json_object(paid), dict(payment))
        except (Exception, KeyboardInterrupt, GeneratorExit) as exc:
            reason = (str(exc) if isinstance(exc, HodlOddsError) else
                      "authorized request interrupted" if isinstance(exc, (KeyboardInterrupt, GeneratorExit))
                      else "invalid payment response")
            if payment.get('charged') is False:
                raise HodlOddsError(reason, paid.status_code, _body(paid), payment=payment) from None
            if payment["success"] is False:
                # Facilitator-declared failure: say so plainly, keep the reconcile facts.
                raise HodlOddsError(reason + ". Not retried. Check %(amount_usdc)s USDC on %(network)s from %(payer)s "
                                    "to %(pay_to)s in your wallet history." % payment, paid.status_code if paid else None,
                                    _body(paid) if paid else None, payment=payment) from None
            message = (reason + ". Settlement may have occurred; not retried. Reconcile %(amount_usdc)s USDC "
                       "on %(network)s from %(payer)s to %(pay_to)s against finalized wallet history and "
                       "the server payment records before another paid call. Transaction: %(transaction)s." % payment)
            raise HodlOddsError(message, paid.status_code if paid else getattr(exc, "status", None),
                               _body(paid) if paid else None, payment=payment) from None


def _fmt(atomic):
    try:
        amount = int(atomic)
        whole, fraction = divmod(abs(amount), 1000000)
        return ("-" if amount < 0 else "") + str(whole) + ((".%06d" % fraction).rstrip("0") if fraction else "")
    except (TypeError, ValueError, DecimalException):
        return "?"


class _WatchCapReached(Exception):
    pass


def _watch_wake(now, every_minutes):
    period = every_minutes * 60
    return (math.floor((now - 25) / period) + 1) * period + 25 + random.uniform(0, 5)


class Watch:
    """Iterator backed by a generator, with an estimate available before iteration.

    max_calls counts paid-route calls (including free empty boards), not skipped
    status checks. close() releases the generator; the caller owns the client.
    """
    def __init__(self, client, path, every_minutes, max_spend_usdc, max_calls,
                 alert_above, stop_on_stale, max_price_usdc=None, board_query=None):
        if type(every_minutes) is not int or not 1 <= every_minutes <= 60:
            raise HodlOddsError("every_minutes must be an integer between 1 and 60")
        if max_calls is not None and (type(max_calls) is not int or max_calls < 1):
            raise HodlOddsError("max_calls must be a positive integer")
        if type(stop_on_stale) is not bool:
            raise HodlOddsError("stop_on_stale must be a boolean")
        if alert_above is not None:
            try:
                if isinstance(alert_above, bool):
                    raise ValueError()
                alert_above = float(alert_above)
                if not math.isfinite(alert_above) or not 0 <= alert_above <= 1:
                    raise ValueError()
            except (ValueError, TypeError, OverflowError):
                raise HodlOddsError("alert_above must be a finite number between 0 and 1") from None
        self.cap_atomic = _usdc(max_spend_usdc, "max_spend_usdc", Decimal("1e100"))
        if max_price_usdc is not None:
            _usdc(max_price_usdc, "max_price_usdc", BOARD_MAX_USDC if board_query else HARD_MAX_USDC)
        self.client, self.path, self.every_minutes = client, path, every_minutes
        self.max_calls, self.alert_above, self.stop_on_stale = max_calls, alert_above, stop_on_stale
        self.max_price_usdc, self.board_query = max_price_usdc, board_query
        self.spent_atomic, self.calls, self.cycle = 0, 0, 0
        self.last_as_of, self.stop_reason = None, None
        price = board_query["limit"] * BOARD_ATOMIC_PER_COIN if board_query else 10000
        hourly = Decimal(price) * 60 / (1000000 * every_minutes)
        self.estimate = {"price_per_call_usdc": _fmt(price),
                         "calls_per_hour": format(Decimal(60) / every_minutes, "f"),
                         "per_hour_usdc": format(hourly, "f"),
                         "per_24h_usdc": format(hourly * 24, "f"),
                         "cap_usdc": _fmt(self.cap_atomic)}
        self._generator = self._run()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._generator)

    def close(self):
        self._generator.close()

    def check_budget(self, amount):
        if self.spent_atomic + amount > self.cap_atomic:
            raise _WatchCapReached()

    def _result(self, answer, reason=None):
        answer.watch = {"cycle": self.cycle, "spent_usdc": _fmt(self.spent_atomic),
                        "cap_usdc": _fmt(self.cap_atomic), "skipped_reason": reason,
                        "utc": datetime.fromtimestamp(time.time(), timezone.utc).isoformat()}
        rows = answer.data.get("rows", [answer.data.get("row", {})]) if reason is None else []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise HodlOddsError("invalid watch answer rows; watch stopped", answer.status,
                                answer.data, answer.payment)
        answer.alerts = [row for row in rows
                         if self.alert_above is not None
                         and isinstance(row.get("p_crash_60m"), (int, float))
                         and row["p_crash_60m"] >= self.alert_above]
        answer.watch["alert"] = bool(answer.alerts)
        return answer

    @staticmethod
    def _skip_reason(status, last_as_of):
        if status.get("state") != "live":
            return "feed " + str(status.get("state", "unknown"))
        as_of = status.get("as_of")
        if type(as_of) is not int or as_of < 0:
            raise HodlOddsError("live status has no valid as_of; nothing paid")
        if last_as_of is not None and as_of <= last_as_of:
            return "unchanged as_of" if as_of == last_as_of else "older as_of"
        return None

    def _run(self):
        try:
            while True:
                if self.max_calls is not None and self.calls >= self.max_calls:
                    self.stop_reason = "max_calls reached"
                    return
                if self.spent_atomic >= self.cap_atomic:
                    self.stop_reason = "spending cap reached"
                    return
                wake = _watch_wake(time.time(), self.every_minutes)
                remaining = wake - time.time()
                while remaining > 0:
                    time.sleep(min(60, remaining))
                    remaining = wake - time.time()
                self.cycle += 1
                status = self.client.status()
                reason = self._skip_reason(status, self.last_as_of)
                if reason in ("unchanged as_of", "older as_of"):
                    time.sleep(10)
                    status = self.client.status()
                    reason = self._skip_reason(status, self.last_as_of)
                if reason:
                    yield self._result(Answer(None, status), reason)
                    if self.stop_on_stale and status.get("state") != "live":
                        self.stop_reason = reason
                        return
                    continue
                try:
                    answer = self.client._paid(self.path, self.max_price_usdc,
                                               board_query=self.board_query, _watch=self)
                except _WatchCapReached:
                    self.stop_reason = "spending cap reached"
                    yield self._result(Answer(None, status), self.stop_reason)
                    return
                except HodlOddsError as exc:
                    if not exc.payment or exc.payment.get('charged') is not False:
                        raise
                    self.calls += 1
                    yield self._result(Answer(exc.status, exc.body if isinstance(exc.body, dict) else {}), str(exc))
                    continue
                self.calls += 1
                if answer.paid:
                    as_of = answer.data.get("as_of")
                    if type(as_of) is not int or as_of < 0:
                        raise HodlOddsError("paid answer has no valid as_of; watch stopped",
                                            answer.status, answer.data, answer.payment)
                    self.last_as_of = as_of
                yield self._result(answer)
        except KeyboardInterrupt:
            # Interrupts after authorization are converted to payment errors by
            # _paid. Only unsigned requests and between-cycle waits end here.
            self.stop_reason = "interrupted"
            return


def _offer_changed_unsigned(response):
    """No authorization has been sent; contradictory receipt metadata forbids refresh."""
    if (response.status_code != 409 or response.headers.get("payment-response")
            or response.headers.get("hodlodds-charged") not in (None, "no")):
        return False
    body = _body(response)
    return isinstance(body, dict) and body.get("code") == "offer_changed"


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


def _watch_print(answer, as_json=False, board=False):
    if as_json:
        print(json.dumps(dict(answer.data, watch=answer.watch)), flush=True)
        return
    watch = answer.watch
    prefix = "%s as_of=%s" % (watch["utc"], answer.data.get("as_of"))
    spend = "%s / %s USDC" % (watch["spent_usdc"], watch["cap_usdc"])
    if watch["skipped_reason"]:
        detail = "skipped: " + watch["skipped_reason"]
    elif board:
        rows = answer.data.get("rows", [])
        detail = "board %d rows, %d alerts" % (len(rows), len(answer.alerts))
        if answer.alerts:
            detail += " ALERT"
    else:
        detail = _watch_row(answer.data.get("row", {}))
        if answer.alerts:
            detail += " ALERT"
    print("%s %s spent %s" % (prefix, detail, spend), flush=True)
    if board and not watch["skipped_reason"]:
        for row in answer.alerts:
            print("  %s %s ALERT" % (row.get("mint", "?"), _watch_row(row)), flush=True)


def _watch_row(row):
    probability = row.get("p_crash_60m")
    if not isinstance(probability, (int, float)):
        odds = "?"
    elif probability < 0.001:
        odds = "<0.1%"  # never print a low score as 0.0%: low is not zero
    else:
        odds = "%.1f%%" % (probability * 100)
    confidence = row.get("confidence")
    level = confidence.get("level", "unknown") if isinstance(confidence, dict) else "unknown"
    return "odds=%s confidence=%s" % (odds, level)


def main(argv=None):
    p = argparse.ArgumentParser(description="HodlOdds x402 client")
    p.add_argument("command", choices=["status", "meta", "markets", "coverage", "risk", "last", "board", "redeem", "watch"])
    p.add_argument("mint", nargs="?")
    p.add_argument("--base-url", default=os.environ.get("HODLODDS_BASE_URL", "https://api.hodlodds.com"))
    p.add_argument("--network", default=os.environ.get("HODLODDS_NETWORK", "devnet"), choices=sorted(NETWORKS))
    p.add_argument("--keypair", default=os.environ.get("HODLODDS_KEYPAIR"))
    p.add_argument("--pay-to", default=os.environ.get("HODLODDS_PAY_TO"))
    p.add_argument("--max-price", default=os.environ.get("HODLODDS_MAX_PRICE", "0.01"),
                   help="USDC spending limit (default 0.01; hard cap 2.5 for board, 1 otherwise)")
    p.add_argument("--chain", help="data chain alias or CAIP-2 ID; comma-separated values")
    p.add_argument("--platform", help="launch platform; comma-separated values")
    p.add_argument("--rpc-url")
    p.add_argument("--limit", type=int, help="board coins: 10..500 (default 50); with mints: 1..100 (default list size)")
    lists = p.add_mutually_exclusive_group()
    lists.add_argument("--mints", help="comma-separated watchlist, 1..100 distinct mints")
    lists.add_argument("--mints-file", help="watchlist file, one mint per line, max 100")
    p.add_argument("--sort", default="lowest_risk", choices=BOARD_SORTS)
    p.add_argument("--max-risk")
    p.add_argument("--min-risk")
    p.add_argument("--min-liquidity", dest="min_liquidity_sol")
    p.add_argument("--min-age", dest="min_age_min")
    p.add_argument("--max-age", dest="max_age_min")
    p.add_argument("--min-trades", dest="min_trades_5", type=int)
    p.add_argument("--min-confidence", choices=("high", "medium", "low"))
    p.add_argument("--include-crashed", action=argparse.BooleanOptionalAction, default=None,
                   help="include already-crashed board rows; --no-include-crashed filters them")
    p.add_argument("--board", action="store_true", help="watch a board instead of a mint")
    p.add_argument("--every", type=int, default=5, help="watch interval in minutes, 1..60")
    p.add_argument("--max-spend", help="required watch spending cap in USDC")
    p.add_argument("--max-calls", type=int, help="maximum paid-route calls; skips do not count")
    p.add_argument("--alert-above", help="alert at or above these odds, 0..1")
    p.add_argument("--stop-on-stale", action="store_true")
    p.add_argument("--json", action="store_true", help="watch results as JSON Lines")
    args = list(sys.argv[1:] if argv is None else argv)
    a = p.parse_args(args)
    if a.command in ("risk", "last", "coverage") and not a.mint:
        p.error(a.command + " needs a mint address")
    if a.command == "redeem" and not a.mint:
        p.error("redeem needs a pending payment file")
    if a.command not in ("risk", "last", "coverage", "redeem", "watch") and a.mint:
        p.error(a.command + " does not take a mint")
    explicit = lambda flag: any(v == flag or v.startswith(flag + "=") for v in args)
    if a.command == "watch":
        if a.max_spend is None:
            p.error("watch requires --max-spend")
        if bool(a.mint) == a.board:
            p.error("watch requires either a mint address or --board")
    elif any(explicit(flag) for flag in ("--board", "--every", "--max-spend", "--max-calls",
                                        "--alert-above", "--stop-on-stale", "--json")):
        p.error("watch options can be used only with watch")
    if a.command not in ("risk", "last", "coverage", "board", "watch") and (a.chain is not None or a.platform is not None):
        p.error("chain/platform filters require a data command")
    is_board = a.command == "board" or (a.command == "watch" and a.board)
    board_flags = ("--mints", "--mints-file", "--limit", "--sort", "--max-risk", "--min-risk", "--min-liquidity", "--min-age",
                   "--max-age", "--min-trades", "--min-confidence", "--include-crashed", "--no-include-crashed")
    if not is_board and any(explicit(flag) for flag in board_flags):
        p.error("board filters and sorting can be used only with board")
    if a.command != "redeem" and a.network in ("mainnet", "solana", "base") and not (explicit("--network") and explicit("--pay-to")):
        p.error("mainnet requires explicit --network and --pay-to; environment defaults are not enough")
    try:
        _market_query(a.chain, a.platform)
        market_options = {k: v for k, v in (("chain", a.chain), ("platform", a.platform)) if v is not None}
        mints = a.mints.split(",") if a.mints is not None else None
        if a.mints_file is not None:
            try:
                with open(a.mints_file, encoding="utf-8") as source:
                    mints = []
                    for line in source:
                        line = line.split("#", 1)[0].strip()  # blank lines and # comments are ignored
                        if line:
                            mints.append(line)
                        if len(mints) > 100:
                            break
            except (OSError, UnicodeError):
                raise HodlOddsError("cannot read mints file") from None
        if is_board:
            filters = {name: getattr(a, name) for name in BOARD_FILTERS if getattr(a, name) is not None}
            _board_query(a.limit, a.sort, filters, mints, a.chain, a.platform)
        pending = load_pending(a.mint) if a.command == 'redeem' else None
        if pending is not None:
            a.base_url, a.network, a.pay_to = pending._base, pending['network'], pending['pay_to']
        with HodlOdds(a.base_url, keypair=a.keypair, network=a.network, pay_to=a.pay_to,
                      max_price_usdc="0.01" if is_board else a.max_price, rpc_url=a.rpc_url) as client:
            if a.command == "watch":
                options = dict(every_minutes=a.every, max_spend_usdc=a.max_spend, max_calls=a.max_calls,
                               alert_above=a.alert_above, stop_on_stale=a.stop_on_stale)
                if a.board:
                    filters = {name: getattr(a, name) for name in BOARD_FILTERS if getattr(a, name) is not None}
                    watching = client.watch_board(limit=a.limit, sort=a.sort, max_price_usdc=a.max_price, mints=mints,
                                                  **options, **market_options, **filters)
                else:
                    watching = client.watch(a.mint, **options, **market_options)
                print("Watch estimate: %(price_per_call_usdc)s USDC/call × %(calls_per_hour)s calls/h = "
                      "%(per_hour_usdc)s USDC/h, %(per_24h_usdc)s USDC/24h; cap %(cap_usdc)s USDC"
                      % watching.estimate, file=sys.stderr, flush=True)
                try:
                    for answer in watching:
                        _watch_print(answer, a.json, a.board)
                finally:
                    watching.close()
                if watching.stop_reason:
                    print("Watch stopped: " + watching.stop_reason, file=sys.stderr)
                return 0
            if a.command in ("status", "meta", "markets"):
                print(json.dumps(getattr(client, a.command)(), indent=2))
                return 0
            if a.command == "coverage":
                print(json.dumps(client.coverage(a.mint, **market_options), indent=2))
                return 0
            if a.command == "redeem":
                answer = client.redeem(pending)
            elif a.command == "board":
                filters = {name: getattr(a, name) for name in BOARD_FILTERS if getattr(a, name) is not None}
                answer = client.board(limit=a.limit, sort=a.sort, max_price_usdc=a.max_price, mints=mints, **market_options, **filters)
            else:
                answer = getattr(client, a.command)(a.mint, **market_options)
            print(json.dumps(answer.data, indent=2))
            if answer.payment:
                verb = "Redeemed (no new payment; paid earlier)" if answer.payment.get("redeemed") else "Paid"
                print("\n" + verb + " %(amount_usdc)s USDC on %(network)s from %(payer)s\nTransaction: %(explorer)s" % answer.payment)
            return 0
    except HodlOddsError as e:
        print("HodlOdds: " + str(e), file=sys.stderr)
        if isinstance(e.payment, PendingPayment) and a.command != 'redeem':
            try:
                print("Pending payment saved: " + str(save_pending(e.payment)), file=sys.stderr)
            except HodlOddsError as save_error:
                print(str(save_error), file=sys.stderr)
        if e.body is not None and e.payment is None:
            print(json.dumps(e.body, indent=2), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
