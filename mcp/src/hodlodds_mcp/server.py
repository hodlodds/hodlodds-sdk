"""Environment-configured stdio server. No wallet material enters MCP results."""

import json
import math
import os
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from threading import Lock
from typing import Any, Literal

import anyio
from hodlodds import HodlOdds, HodlOddsError, PriceLimitError
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from . import __version__

DISCLAIMER = "Odds, not guarantees. Not financial advice."
# Verbatim meaning and freshness from cloud/static/llms.txt, bundled for offline use.
MEANING = (
    "0.04 means about 4 in 100 tokens like this one lose 90% or more of their price within the next 60 minutes. "
    "Higher means riskier, but a low number does not mean safe: smaller or slower drops are not counted, "
    "and the odds change minute by minute."
)
FRESHNESS = (
    "Scores are recomputed every 60 seconds, about 20 seconds after each minute closes, from completed "
    "one-minute bars up to `as_of`. Each score covers the 60 minutes after its `as_of` time and is valid "
    "only until the next update; the odds for the same token can rise sharply from one minute to the next. "
    "Scoring starts around :20; computation and publication can delay availability. For decisions, refresh "
    "immediately before acting and once per minute for continuous monitoring, checking `as_of` for a new "
    "minute. The API freshness cutoff is separate: by default, data becomes `stale` more than 120 seconds "
    "after `issued_at` or after `expires_at`. A new request does not restart the forecast window."
)
RECONCILE = (
    "Payments paused. Check finalized wallet history and the operator's payment records using these "
    "reconciliation details. Restart only after checking the wallet and reconciling this payment. "
    "A restart starts a new process budget."
)

# Fixed JSON `error` texts the HodlOdds API can return to a client. Kept equal to
# tests/server_messages.json, which is exported from the server source; a test
# fails when the two differ. Anything else is never echoed.
SERVER_ERRORS = frozenset((
    "Answer freshness changed; request again",
    "Board answer exceeds 4 MiB; use a smaller limit",
    "Board payment amount changed; request again",
    "Board payment does not match the normalized query and priced count; request again",
    "Duplicate board parameter: chain",
    "Duplicate board parameter: include_crashed",
    "Duplicate board parameter: limit",
    "Duplicate board parameter: max_age_min",
    "Duplicate board parameter: max_risk",
    "Duplicate board parameter: min_age_min",
    "Duplicate board parameter: min_confidence",
    "Duplicate board parameter: min_liquidity_sol",
    "Duplicate board parameter: min_risk",
    "Duplicate board parameter: min_trades_5",
    "Duplicate board parameter: mints",
    "Duplicate board parameter: platform",
    "Duplicate board parameter: sort",
    "Duplicate chain value; see /v1/markets",
    "Duplicate mints are not allowed",
    "Duplicate parameter: chain",
    "Duplicate parameter: platform",
    "Duplicate platform value; see /v1/markets",
    "Feed interrupted",
    "Feed unavailable",
    "Final freshness check unavailable",
    "Invalid mint",
    "Invalid priced board count",
    "Last known odds too old",
    "Latest snapshot changed; request again",
    "Mint is not currently covered",
    "No redeemable payment found; no settlement attempted",
    "Outside validated SOL coverage",
    "Paid service is not available",
    "Payment already delivered or redeem expired",
    "Payment already redeemed",
    "Payment already redeemed or redeem expired",
    "Payment already submitted; reconcile the original payment before retrying",
    "Payment capacity check unavailable",
    "Payment header too large",
    "Payment recovery check unavailable",
    "Payment replay check unavailable",
    "Payment required",
    "Payment service unavailable",
    "Payment service unavailable; reconcile any submitted authorization",
    "Payment settled; delivery persistence unavailable; redeem after reconciliation",
    "Public beta is not enabled",
    "Rate limit exceeded",
    "Redeem requires the original board query",
    "Request refused before settlement",
    "Request too large",
    "Service temporarily unavailable",
    "Snapshot refresh in progress; request again",
    "Template paths provide discovery only",
    "Too many concurrent paid requests",
    "Too many invalid payments; slow down",
    "Use a real mint to pay for an answer",
    "busy",
    "chain must contain 1..10 non-empty values; see /v1/markets",
    "include_crashed must be true or false",
    "limit is too large",
    "limit must be 1..100",
    "limit must be 10..500",
    "limit must be an integer",
    "matches changed; request again",
    "max_age_min must be a finite non-negative number",
    "max_age_min must be a finite number",
    "max_risk must be a finite non-negative number",
    "max_risk must be a finite number",
    "max_risk must be between 0 and 1",
    "min_age_min must be a finite non-negative number",
    "min_age_min must be a finite number",
    "min_age_min must not exceed max_age_min",
    "min_confidence must be high, medium or low",
    "min_liquidity_sol must be a finite non-negative number",
    "min_liquidity_sol must be a finite number",
    "min_risk must be a finite non-negative number",
    "min_risk must be a finite number",
    "min_risk must be between 0 and 1",
    "min_risk must not exceed max_risk",
    "min_trades_5 is too large",
    "min_trades_5 must be 0..9223372036854775807",
    "min_trades_5 must be an integer",
    "mints must contain 1..100 valid Solana mint addresses",
    "payment is being verified on-chain",
    "platform must contain 1..10 non-empty values; see /v1/markets",
    "platform outside the given chains; see /v1/markets",
    "sort must be one of: lowest_risk, safest, riskiest, liquidity, newest, active",
    "this payment never settled; request a new offer",
))
# Texts whose tail names a rejected input or a server list: only this prefix is shown.
SERVER_ERROR_TEMPLATES = (
    "Unknown board parameter: ",
    "Unknown parameter: ",
    "chain not available; see /v1/markets. Valid values: ",
    "platform not available; see /v1/markets. Valid values: ",
)
# The SDK's own fixed refusals, raised before any authorization is signed
# (argument checks, offer checks, key loading). Kept in sync with the SDK
# source by tests/test_refusals.py.
CLIENT_REFUSALS = frozenset((
    "mint must be a Solana address",
    "mints must contain 1..100 valid Solana mint addresses",
    "duplicate mints are not allowed",
    "limit must be an integer between 10 and 500",
    "limit must be an integer between 1 and 100",
    "sort must be one of: lowest_risk, safest, riskiest, liquidity, newest, active",
    "include_crashed must be a boolean",
    "min_confidence must be high, medium or low",
    "min_trades_5 must be an integer between 0 and 9223372036854775807",
    "min_risk must be a finite number between 0 and 1",
    "max_risk must be a finite number between 0 and 1",
    "min_liquidity_sol must be a finite number at least 0",
    "min_age_min must be a finite number at least 0",
    "max_age_min must be a finite number at least 0",
    "min_risk must not exceed max_risk",
    "min_age_min must not exceed max_age_min",
    "chain must contain 1..10 non-empty values",
    "chain not available; see /v1/markets",
    "duplicate chain value",
    "platform must contain 1..10 non-empty values",
    "platform not available; see /v1/markets",
    "duplicate platform value",
    "only x402 v2 offers are accepted",
    "invalid payment options",
    "expected one payment option matching the configured network and pinned pay_to",
    "only exact USDC payments are accepted",
    "server payTo differs from the pinned pay_to address",
    "price is invalid or above your USDC limit",
    "only authorization payment flow is accepted",
    "payment timeout must be between 1 and 300 seconds",
    "a valid facilitator feePayer is required",
    "invalid or oversized payment memo (maximum 32 bytes)",
    "invalid payment blockhash",
    "only EIP-3009 exact USDC authorizations are accepted; Permit2 is disabled",
    "USDC signing domain differs from the pinned SDK",
    "Solana transaction fields are not accepted in a Base offer",
    "board offer does not match the requested query and per-coin price",
    "board offer is missing its server binding signature",
    "board payment memo does not bind the requested query and count",
    "install the pinned x402[svm,evm]==2.25.0 SDK",
    "pinned SDK network/USDC constants unavailable; install x402[svm,evm]==2.25.0",
    "a keypair file is required for paid answers",
    "pinned SDK network constants changed",
    "cannot load key: use a regular non-symlink file, mode 0600, at most 4096 bytes",
    "cannot load key: use a regular non-symlink solana-keygen file, mode 0600, and installed SDK dependencies",
    "cannot load EVM key: use a regular non-symlink 0600 hex/JSON private-key file and installed x402[evm] dependencies",
    "cannot initialize payment SDK; no authorization was sent",
    "payer and payTo must be different wallets",
    "payer must differ from the facilitator fee payer",
    "authorization construction failed; nothing sent and not retried",
))


def atomic(value, name):
    """Parse decimal strings without float or Decimal-context rounding."""
    if not isinstance(value, str) or len(value) > 128 or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?", value):
        raise ValueError(name + " must be a positive decimal with at most six decimal places")
    whole, _, fraction = value.partition(".")
    result = int(whole) * 1_000_000 + int(fraction.ljust(6, "0"))
    if result <= 0:
        raise ValueError(name + " must be positive")
    return result


def usdc(value):
    whole, fraction = divmod(value, 1_000_000)
    return f"{whole}.{fraction:06d}"


def plain(value):
    """0.05 rather than 0.050000; for messages only."""
    return usdc(value).rstrip("0").rstrip(".")


@dataclass(frozen=True)
class Config:
    base_url: str
    network: str
    pay_to: str | None
    keypair: str | None = field(repr=False)
    max_atomic: int
    budget_atomic: int | None

    @classmethod
    def from_env(cls):
        network = os.environ.get("HODLODDS_NETWORK", "devnet")
        if network not in ("solana", "base", "devnet", "base-sepolia"):
            raise ValueError("HODLODDS_NETWORK must be solana, base, devnet or base-sepolia")
        pay_to = os.environ.get("HODLODDS_PAY_TO") or None
        keypair = os.environ.get("HODLODDS_KEYPAIR") or None
        if (network in ("solana", "base") or keypair) and not pay_to:
            raise ValueError("HODLODDS_PAY_TO is required for mainnet and for keyed test networks")
        maximum = atomic(os.environ.get("HODLODDS_MAX_PRICE_USDC", "0.01"), "HODLODDS_MAX_PRICE_USDC")
        if maximum > 2_500_000:
            raise ValueError("HODLODDS_MAX_PRICE_USDC must not exceed 2.5")
        budget = os.environ.get("HODLODDS_SESSION_BUDGET_USDC")
        if keypair and not budget:
            raise ValueError("HODLODDS_SESSION_BUDGET_USDC is required when HODLODDS_KEYPAIR is set")
        return cls(os.environ.get("HODLODDS_BASE_URL", "https://api.hodlodds.com"), network,
                   pay_to, keypair, maximum, atomic(budget, "HODLODDS_SESSION_BUDGET_USDC") if budget else None)


class BudgetExceeded(HodlOddsError):
    pass


class Ledger:
    def __init__(self, cap):
        self.cap_atomic = cap
        self.spent_atomic = 0
        self.paused = None
        self.lock = Lock()

    def check_budget(self, amount):
        if self.cap_atomic is None or self.spent_atomic + amount > self.cap_atomic:
            raise BudgetExceeded("Session budget exceeded before key access")

    def fields(self):
        return {"spent_atomic": self.spent_atomic, "spent_usdc": usdc(self.spent_atomic),
                "budget_atomic": self.cap_atomic,
                "budget_usdc": usdc(self.cap_atomic) if self.cap_atomic is not None else None}


class BudgetClient(HodlOdds):
    """Adapter to the SDK 0.3 watch hook; keep all offer/receipt checks in the SDK.

    _paid invokes check_budget(accepted_amount) before _sdk_client/key loading,
    reserves spent_atomic before transport, and refunds only confirmed free refusals.
    The caller holds the ledger lock for the complete paid operation.
    """

    def __init__(self, config, ledger):
        self.ledger = ledger
        super().__init__(config.base_url, network=config.network, pay_to=config.pay_to,
                         keypair=config.keypair, max_price_usdc=usdc(min(config.max_atomic, 1_000_000)),
                         paid_timeout=90.0)

    def _paid(self, path, max_price_usdc, board_query=None, *, _watch=None):
        return super()._paid(path, max_price_usdc, board_query, _watch=self.ledger)


def receipt(payment):
    # In particular, never serialize PendingPayment attributes, body or exception text.
    if payment is None:
        return None
    return {key: payment.get(key) for key in
            ("network", "amount_usdc", "transaction", "payer", "pay_to", "success", "charged")}


def error_details(exc):
    """Expose fixed public refusal text, never arbitrary echoed inputs or SDK exceptions."""
    if not isinstance(exc, HodlOddsError):
        return {"http_status": None}
    result = {"http_status": exc.status}
    body = exc.body if isinstance(exc.body, dict) else {}
    if body.get("code") == "offer_changed":
        result["server_code"] = "offer_changed"
    message = body.get("error")
    if isinstance(message, str):
        if message in SERVER_ERRORS:
            result["server_error"] = message
        else:
            prefix = next((t for t in SERVER_ERROR_TEMPLATES if message.startswith(t)), None)
            if prefix is not None:
                result["server_error"] = prefix.removesuffix(": ").removesuffix(". Valid values")
    return result


def client_refusal(exc, *, board, max_atomic):
    """Fixed text for a refusal the SDK made before signing; None for anything else."""
    if not isinstance(exc, HodlOddsError) or exc.status is not None or exc.payment is not None:
        return None
    if isinstance(exc, PriceLimitError):
        price, limit = exc.price_atomic, exc.limit_atomic
        what = "This board" if board else "This answer"
        hard_cap = 2_500_000 if board else 1_000_000
        if limit == max_atomic and price <= hard_cap:
            return (f"{what} costs {plain(price)} USDC but HODLODDS_MAX_PRICE_USDC is {plain(limit)}; "
                    f"raise it to at least {plain(price)}.")
        return f"{what} costs more than the client hard cap of {plain(hard_cap)} USDC; nothing was signed."
    message = str(exc)
    return message if message in CLIENT_REFUSALS else None


def summary(p):
    """Same fresh-row wording/rounding as cloud/summary.py, without a server dependency."""
    percent = p * 100
    half_up = lambda value: int(math.floor(value + 0.5))
    if percent < 0.1:
        odds, frequency, low = "under 0.1%", "fewer than 1 in 1,000", True
    elif percent > 99:
        odds, frequency, low = "over 99%", "more than 99 in 100", False
    else:
        shown = round(percent, 1)
        if shown < 10:
            odds = "%g%%" % shown
            frequency = "about %d in 1,000" % half_up(p * 1000) if shown < 1 else "about %g in 100" % shown
            low = True
        else:
            odds = "%d%%" % half_up(percent)
            frequency, low = "about %d in 100" % half_up(percent), False
    tail = "Low odds are not a safety signal." if low else "Odds, not guarantees."
    return odds + " chance of a 90%+ price drop within 60 minutes (" + frequency + "). " + tail


def odds_row(row):
    p = row.get("p_crash_60m")
    if (type(p) not in (float, int) or not math.isfinite(p) or not 0 <= p <= 1
            or row.get("kind", "sol") != "sol" or "experimental_score" in row):
        raise ValueError("Invalid probability row")
    confidence = row.get("confidence") or {}
    level = confidence.get("level")
    output = {**row, "mint": row["mint"], "p_crash_60m": p, "odds_percent": p * 100,
            "summary": row.get("summary") or summary(p),
            # Preserve the 0.1.0 string contract; full evidence is additive.
            "confidence": level if level in ("high", "medium", "low") else "missing",
            "confidence_detail": confidence}
    known = {"mint", "p_crash_60m", "summary", "confidence", "kind", "band", "resq_sol", "age_min",
             "ret_5", "ret_60", "dd_60", "trades_5", "symbol", "name", "image_uri",
             "chain", "chain_name", "platform", "market_status", "asset",
             "already_crashed", "crashed_within_24h", "last_crash_as_of", "note", "alert"}
    if row.keys() - known:
        output["warning"] = "Additional source row fields are included; their meaning has not been interpreted."
    return output


def result(**fields):
    return {**fields, "disclaimer": DISCLAIMER}


class LocalMCPServer(MCPServer):
    async def call_tool(self, name, arguments, context=None):
        # Also sanitize SDK argument/unknown-tool failures; never echo rejected values.
        try:
            answer = await super().call_tool(name, arguments, context)
            if isinstance(answer, CallToolResult) and answer.structured_content and answer.structured_content.get("error"):
                answer.is_error = True
            return answer
        except Exception:
            data = result(error="invalid_tool_call", message="Tool call failed; check the tool name and arguments.")
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(data))],
                                  structured_content=data, is_error=True)


class Service:
    def __init__(self, config):
        self.config = config
        self.ledger = Ledger(config.budget_atomic)
        self.client = BudgetClient(config, self.ledger)

    def free(self, operation):
        try:
            return result(**operation())
        except Exception as exc:
            return result(error="free_request_failed", message="Free request failed; nothing signed.",
                          **error_details(exc))

    def paid(self, operation, *, board=False):
        with self.ledger.lock:
            if self.ledger.paused is not None:
                return result(error="payments_paused", message=RECONCILE,
                              payment=self.ledger.paused, **self.ledger.fields())
            answer = None
            try:
                answer = operation()
                data = answer.data
                common = {key: data.get(key) for key in ("as_of", "horizon_end", "issued_at", "expires_at")}
                # Keep public evidence and last-known freshness; never turn a
                # historical answer into an apparently current one by omission.
                common.update({key: data[key] for key in (
                    "schema", "covered", "definition", "coverage", "model", "model_id", "model_hashes",
                    "stale", "age_s", "freshness", "horizon_remaining_s",
                    "sort", "filters", "defaults_applied", "excluded_by_defaults", "limit", "returned", "total_matches",
                    "price_usdc", "unit_price_usdc") if key in data})
                common.update(payment=receipt(answer.payment), **self.ledger.fields())
                if board:
                    rows = [odds_row(row) for row in data["rows"]]
                    omissions = [{key: row.get(key) for key in ("mint", "stage", "reason")}
                                 for row in data.get("not_delivered", [])]
                    return result(rows=rows, not_delivered=omissions, **common)
                return result(**odds_row(data["row"]), **common)
            except BudgetExceeded:
                return result(error="budget_exhausted", message="Accepted offer exceeds remaining session budget; nothing signed.",
                              **self.ledger.fields())
            except Exception as exc:
                payment = exc.payment if isinstance(exc, HodlOddsError) else None
                if payment is None and answer is not None:
                    payment = answer.payment
                if answer is not None and payment is not None and payment.get("success") is True and payment.get("transaction"):
                    return result(data=answer.data,
                                  warning="Payment settled; answer has an unexpected shape. Raw data is included.",
                                  payment=receipt(payment), **self.ledger.fields())
                if answer is not None and payment is None:
                    return result(data=answer.data, warning="Answer has an unexpected shape. Raw data is included.",
                                  payment=None, **self.ledger.fields())
                if payment is not None and payment.get("charged") is not False:
                    self.ledger.paused = receipt(payment)
                    return result(error="payments_paused", message=RECONCILE,
                                  payment=self.ledger.paused, **error_details(exc), **self.ledger.fields())
                refusal = client_refusal(exc, board=board, max_atomic=self.config.max_atomic) if answer is None else None
                if refusal is not None:
                    return result(error="client_refused", message=refusal,
                                  detail="Refused locally before signing; nothing was signed or charged.",
                                  http_status=None, payment=None, **self.ledger.fields())
                return result(error="paid_request_failed", message="Request refused or unavailable; no payment charged.",
                              **error_details(exc),
                              payment=receipt(payment), **self.ledger.fields())


def create_server():
    service = Service(Config.from_env())

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield service
        finally:
            service.client.close()

    server = LocalMCPServer("hodlodds-mcp", version=__version__, lifespan=lifespan, log_level="ERROR")

    @server.tool(description="Free feed state, issuance age and source timestamp.")
    async def status() -> dict[str, Any]:
        return await anyio.to_thread.run_sync(partial(service.free, service.client.status))

    @server.tool(description="Free available and experimental data markets, filter syntax, model identities and prices.")
    async def markets() -> dict[str, Any]:
        return await anyio.to_thread.run_sync(partial(service.free, service.client.markets))

    @server.tool(description="Free observed coverage stage and reason for a Solana mint.")
    async def coverage(mint: str, chain: str | list[str] | None = None, platform: str | list[str] | None = None) -> dict[str, Any]:
        return await anyio.to_thread.run_sync(partial(service.free, partial(service.client.coverage, mint, chain=chain, platform=platform)))

    @server.tool(description="Crash-odds definition, confidence interpretation and freshness rules.")
    def meaning() -> dict[str, Any]:
        return result(meaning=MEANING, freshness=FRESHNESS,
                      confidence="High/medium/low is a calibration/evidence label, not a token-specific interval or guarantee. Missing confidence is reported as missing.",
                      coverage="A missing score, an uncovered token or stale data is not a safety signal.",
                      crash="A crash is a completed one-minute close in SOL at least 90% below the reference price within the displayed 60-minute window.")

    if service.config.keypair:
        @server.tool(description="Paid 60-minute crash odds for one covered Solana token (0.01 USDC), with confidence, timestamps, receipt and process spend.")
        async def crash_odds(mint: str, chain: str | list[str] | None = None, platform: str | list[str] | None = None) -> dict[str, Any]:
            return await anyio.to_thread.run_sync(partial(service.paid, partial(service.client.risk, mint, chain=chain, platform=platform)))

        @server.tool(description="Last-known crash odds for one covered Solana token (0.002 USDC), at most 30 minutes old, labelled with freshness/age and a reduced confidence level when stale. The original forecast window is unchanged; shares the session budget and payment pause rules.")
        async def last_known(mint: str, chain: str | list[str] | None = None, platform: str | list[str] | None = None) -> dict[str, Any]:
            return await anyio.to_thread.run_sync(partial(service.paid, partial(service.client.last, mint, chain=chain, platform=platform)))

        @server.tool(description="Paid ranked covered Solana tokens or a watchlist of 1..100 distinct mints, with omitted-mint reasons, receipt and process spend. Price is 0.005 USDC per delivered row; HODLODDS_MAX_PRICE_USDC must cover the total. lowest_risk (alias safest) defaults to min_trades_5=3, min_liquidity_sol=5 and include_crashed=false (excludes crashes in the last hour or the last 24 hours); explicit filters override these defaults, and watchlists get none of them.")
        async def board(
            limit: int | None = None,
            sort: Literal["lowest_risk", "safest", "riskiest", "liquidity", "newest", "active"] = "lowest_risk",
            max_risk: float | None = None, min_risk: float | None = None,
            min_liquidity_sol: float | None = None,
            min_age_min: float | None = None, max_age_min: float | None = None,
            min_trades_5: int | None = None,
            min_confidence: Literal["high", "medium", "low"] | None = None,
            include_crashed: bool | None = None,
            mints: list[str] | None = None,
            chain: str | list[str] | None = None, platform: str | list[str] | None = None,
        ) -> dict[str, Any]:
            filters = {key: value for key, value in locals().items()
                       if key in ("max_risk", "min_risk", "min_liquidity_sol", "min_age_min", "max_age_min",
                                  "min_trades_5", "min_confidence", "include_crashed") and value is not None}
            operation = partial(service.client.board, limit=limit, sort=sort, mints=mints,
                                max_price_usdc=usdc(service.config.max_atomic), chain=chain, platform=platform, **filters)
            # Default run_sync cancellation shielding keeps settlement accounting alive
            # if the MCP caller cancels while the authorized HTTP request is in flight.
            return await anyio.to_thread.run_sync(partial(service.paid, operation, board=True))

    return server


def main():
    try:
        server = create_server()
    except (ValueError, HodlOddsError):
        # No environment values, file paths, key bytes or arbitrary exception text.
        print("hodlodds-mcp: invalid configuration; check network, recipient, key path and USDC caps. " + DISCLAIMER,
              file=sys.stderr)
        return 2
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
