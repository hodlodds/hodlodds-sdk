"""Fixed refusal texts: SDK refusals before signing and recognized server messages."""
import ast
import json
from pathlib import Path

import pytest
from hodlodds import PriceLimitError
from hodlodds import client as sdk

from hodlodds_mcp.server import (CLIENT_REFUSALS, SERVER_ERROR_TEMPLATES, SERVER_ERRORS, client_refusal,
                                 error_details)

CATALOG = json.loads((Path(__file__).with_name("server_messages.json")).read_text())
SECRET = "KEY_FILE_CONTENT_MUST_NEVER_ESCAPE"

# SDK messages that are not refusals before signing: transport and response
# failures, post-signing outcomes, redeem/pending-file handling and settings
# the MCP fixes at startup. They keep the generic error path.
NOT_CLIENT_REFUSALS = {
    "200 without PAYMENT-RESPONSE",
    "402 without a valid PAYMENT-REQUIRED header; nothing paid",
    "HTTP cleanup failed; reconcile any previous paid result before repeating it",
    "HTTP request failed; request details are omitted",
    "access_headers contains an invalid or reserved payment/transport header",
    "alert_above must be a finite number between 0 and 1",
    "cannot initialize HTTP client; install httpx and check configuration",
    "cannot load pending payment: use its private 0600 non-symlink file",
    "cannot read mints file",
    "cannot save pending payment securely",
    "duplicate, invalid or oversized payment header",
    "every_minutes must be an integer between 1 and 60",
    "invalid PAYMENT-REQUIRED offer; nothing signed",
    "invalid or unsuccessful PAYMENT-RESPONSE",
    "invalid redeem receipt",
    "invalid redeem route",
    "invalid watch answer rows; watch stopped",
    "live status has no valid as_of; nothing paid",
    "mainnet requires an explicit pinned pay_to address",
    "max_calls must be a positive integer",
    "network must be solana, devnet, base or base-sepolia (mainnet is a Solana alias)",
    "no pending payment to save",
    "paid answer has no valid as_of; watch stopped",
    "paid_timeout must be between 30 and 600 seconds",
    "redeem requires the original payment, endpoint, network and recipient",
    "redirect refused; use the final endpoint",
    "response body exceeds the client size limit",
    "response headers exceed the client size limit",
    "response is not a JSON object",
    "stop_on_stale must be a boolean",
    "use a valid HTTPS endpoint with no credentials/fragment (HTTP only on loopback; base URL has no query)",
}


def sdk_literals():
    """String literals the SDK passes to HodlOddsError or its subclasses."""
    tree = ast.parse(Path(sdk.__file__).read_text())
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = getattr(node.func, "id", getattr(node.func, "attr", None))
        if name in ("HodlOddsError", "PriceLimitError", "__init__"):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
    return found


def sdk_generated():
    """Messages the SDK builds from fixed vocabularies, collected by calling it."""
    found = {"install the pinned x402[svm,evm]==%s SDK" % sdk.SDK_VERSION,
             "pinned SDK network/USDC constants unavailable; install x402[svm,evm]==" + sdk.SDK_VERSION}
    cases = [dict(limit=5), dict(limit=600), dict(limit=101, mints=["CY8tK7zPvoRAL5tugvxBEgFEtrgAXiikTAKVgh61Siq5"]), dict(sort="zz")]
    cases += [{"filters": {name: -1}} for name in ("min_risk", "max_risk", "min_liquidity_sol",
                                                   "min_age_min", "max_age_min")]
    cases += [{"filters": {"min_risk": .5, "max_risk": .1}}, {"filters": {"min_age_min": 5, "max_age_min": 1}}]
    for name in ("chain", "platform"):
        cases += [{name: ""}, {name: "zz"}, {name: ["solana", "solana"] if name == "chain" else ["pump", "pumpfun"]}]
    for case in cases:
        try:
            sdk._board_query(case.get("limit"), case.get("sort", "lowest_risk"), case.get("filters", {}),
                             case.get("mints"), case.get("chain"), case.get("platform"))
        except sdk.HodlOddsError as exc:
            found.add(str(exc))
    return found


def test_client_refusals_are_the_sdks_own_fixed_texts():
    produced = sdk_literals() | sdk_generated()
    assert CLIENT_REFUSALS <= produced, sorted(CLIENT_REFUSALS - produced)


def test_every_fixed_sdk_message_is_classified():
    # A new SDK refusal must be added to CLIENT_REFUSALS or listed above.
    unclassified = sdk_literals() - CLIENT_REFUSALS - NOT_CLIENT_REFUSALS
    assert not unclassified, sorted(unclassified)
    assert not CLIENT_REFUSALS & NOT_CLIENT_REFUSALS


def test_price_limit_names_the_variable_and_the_amount():
    cases = [
        (PriceLimitError(50_000, 10_000), True, 10_000,
         "This board costs 0.05 USDC but HODLODDS_MAX_PRICE_USDC is 0.01; raise it to at least 0.05."),
        (PriceLimitError(2_000, 1_999), False, 1_999,
         "This answer costs 0.002 USDC but HODLODDS_MAX_PRICE_USDC is 0.001999; raise it to at least 0.002."),
        (PriceLimitError(2_500_000, 500_000), True, 500_000,
         "This board costs 2.5 USDC but HODLODDS_MAX_PRICE_USDC is 0.5; raise it to at least 2.5."),
        # Above what HODLODDS_MAX_PRICE_USDC may be set to: never suggest raising it.
        (PriceLimitError(3_000_000, 2_500_000), True, 2_500_000,
         "This board costs more than the client hard cap of 2.5 USDC; nothing was signed."),
        (PriceLimitError(1_500_000, 1_000_000), False, 2_000_000,
         "This answer costs more than the client hard cap of 1 USDC; nothing was signed."),
    ]
    for exc, board, configured, expected in cases:
        assert str(exc) == "price is invalid or above your USDC limit"
        assert client_refusal(exc, board=board, max_atomic=configured) == expected


@pytest.mark.parametrize("exc", [
    sdk.HodlOddsError("unknown board filter: " + SECRET),
    sdk.HodlOddsError(SECRET),
    sdk.HodlOddsError("limit must be an integer between 10 and 500", status=400),
    sdk.HodlOddsError("limit must be an integer between 10 and 500", payment={"charged": False}),
    ValueError("limit must be an integer between 10 and 500"),
])
def test_other_failures_are_not_client_refusals(exc):
    assert client_refusal(exc, board=True, max_atomic=10_000) is None


def test_server_allowlist_equals_the_vendored_server_catalog():
    assert SERVER_ERRORS == frozenset(CATALOG["messages"])
    assert SERVER_ERROR_TEMPLATES == tuple(CATALOG["templates"])


def test_current_server_refusals_are_recognized():
    for message in ("No redeemable payment found; no settlement attempted",
                    "Snapshot refresh in progress; request again",
                    "Latest snapshot changed; request again",
                    "Board payment does not match the normalized query and priced count; request again",
                    "Board answer exceeds 4 MiB; use a smaller limit",
                    "Public beta is not enabled", "Feed unavailable",
                    "payment is being verified on-chain", "this payment never settled; request a new offer",
                    "Payment already redeemed or redeem expired"):
        exc = sdk.HodlOddsError("free answer HTTP 409 (nothing paid)", status=409, body={"error": message})
        assert error_details(exc) == {"http_status": 409, "server_error": message}


@pytest.mark.parametrize("message,shown", [
    ("Unknown board parameter: " + SECRET, "Unknown board parameter"),
    ("Unknown parameter: " + SECRET, "Unknown parameter"),
    ("chain not available; see /v1/markets. Valid values: solana", "chain not available; see /v1/markets"),
    ("platform not available; see /v1/markets. Valid values: " + SECRET, "platform not available; see /v1/markets"),
])
def test_templates_show_only_the_fixed_prefix(message, shown):
    exc = sdk.HodlOddsError("free answer HTTP 400 (nothing paid)", status=400, body={"error": message})
    assert error_details(exc) == {"http_status": 400, "server_error": shown}


def test_stale_refusal_says_a_last_known_answer_exists():
    # 0.2.3 (Fable audit 21): the server's age of the labelled last-known answer, never its hint text
    from hodlodds import HodlOddsError
    from hodlodds_mcp.server import error_details
    details = error_details(HodlOddsError("x", 503, {"error": "Feed interrupted", "last_known_age_s": 120,
                                                     "hint": "/v1/risk/ANY/last"}))
    assert details["last_known_available"] is True and details["last_known_age_s"] == 120
    assert "hint" not in details
    assert "last_known_available" not in error_details(HodlOddsError("x", 503, {"error": "Feed interrupted"}))
