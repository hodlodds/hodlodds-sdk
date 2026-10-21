import base64
import hashlib
import json
import stat
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import pytest
from hodlodds import client as sdk
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from hodlodds_mcp.server import Config, DISCLAIMER, FRESHNESS, MEANING, create_server, error_details, main, odds_row, summary

MINT = "CY8tK7zPvoRAL5tugvxBEgFEtrgAXiikTAKVgh61Siq5"
OTHER = "GrbVMxp8NHbeMq4WhpD1gwBX4ftCB21ZvfdB5viZhSry"
PAYTO = "Bga5h69rmgYbxZgLxVQGsevn4aBLPnN5vQEnZRQmXGkn"
PAYER = "So11111111111111111111111111111111111111112"
TX = "2" + "1" * 87
SECRET = "KEY_FILE_CONTENT_MUST_NEVER_ESCAPE"
SIGNED = "SIGNED_PAYLOAD_MUST_NEVER_ESCAPE"


def encoded(data):
    return base64.b64encode(json.dumps(data).encode()).decode()


@asynccontextmanager
async def session(server):
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=10) as client:
            await client.initialize()
            yield client


async def call(client, name, **args):
    answer = await client.call_tool(name, args)
    data = answer.structured_content
    assert data["disclaimer"] == DISCLAIMER
    assert answer.is_error == bool(data.get("error"))
    wire = answer.model_dump_json()
    assert SECRET not in wire and SIGNED not in wire
    return data


@pytest.fixture
def api(monkeypatch, tmp_path):
    key = tmp_path / "PRIVATE_KEY_PATH.json"
    key.write_text(SECRET)
    key.chmod(0o600)
    state = SimpleNamespace(loads=0, signs=0, requests=[], behavior="ok", amount=10000,
                            rows=1, summary=True, invalid_binding=False, key=key,
                            row_fields={}, answer_fields={}, error_body=None, initial_changes=0,
                            started=threading.Event(), release=threading.Event())
    net = sdk._network_config("devnet")
    monkeypatch.setenv("HODLODDS_BASE_URL", "http://localhost:8010")
    monkeypatch.setenv("HODLODDS_NETWORK", "devnet")
    monkeypatch.setenv("HODLODDS_PAY_TO", PAYTO)
    monkeypatch.setenv("HODLODDS_KEYPAIR", str(key))
    monkeypatch.setenv("HODLODDS_SESSION_BUDGET_USDC", "0.02")
    # 0.2.3: a paused payment writes its pending file under HOME; never the developer's real home
    state.home = tmp_path / "home"
    state.home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(state.home))

    def handler(request):
        state.requests.append(request)
        path = request.url.path
        if path == "/v1/markets":
            return httpx.Response(200, json={"markets": [{"platform": "pumpfun", "status": "available"}]})
        if path == "/v1/status":
            return httpx.Response(200, json={"state": "live", "age_s": 20, "as_of": 1800000000})
        if path.startswith("/v1/coverage/"):
            return httpx.Response(200, json={"mint": path.rsplit("/", 1)[-1], "stage": "unknown",
                                             "reason": "Not observed", "as_of": 1800000000})
        is_board = path == "/v1/board"
        is_last = path.endswith("/last")
        if state.behavior == "free":
            return httpx.Response(404, json=state.error_body or {"error": SECRET + SIGNED})
        if is_board and state.behavior == "empty":
            return httpx.Response(200, json={"rows": [], "not_delivered": [
                {"mint": MINT, "stage": "unknown", "reason": "Not observed"}]})
        amount = state.rows * 5000 if is_board else 2000 if is_last else state.amount
        if "payment-signature" not in request.headers:
            if is_board and state.initial_changes:
                state.initial_changes -= 1
                return httpx.Response(409, headers={"HodlOdds-Charged": "no"},
                                      json={"error": "matches changed; request again", "code": "offer_changed",
                                            "charged": False})
            extra = {"paymentFlow": "authorization", "feePayer": "11111111111111111111111111111111"}
            if is_board:
                params = dict(request.url.params)
                mints = params.pop("mints", None)
                markets = {k + "s": params.pop(k).split(",") for k in ("chain", "platform") if k in params}
                query = {"limit": int(params.pop("limit")), "sort": params.pop("sort"),
                         "filters": {k: v if k == "min_confidence" else v == "true" if k == "include_crashed"
                                     else int(v) if k == "min_trades_5" else float(v) for k, v in params.items()}}
                query.update(markets)
                if mints:
                    query.update(mints_sha256=hashlib.sha256(mints.encode()).hexdigest(), mints_count=len(mints.split(",")))
                if state.invalid_binding:
                    query["mints_sha256"] = "0" * 64
                binding = {"version": 1, "query": query, "priced_n": state.rows, "nonce": "a" * 32}
                extra.update(hodloddsBoard=binding, hodloddsBoardSignature="b" * 64,
                             memo=hashlib.sha256(b"hodlodds.board.v1\0" + json.dumps(
                                 binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32])
            offer = {"x402Version": 2, "accepts": [{"scheme": "exact", "network": net["caip2"],
                     "asset": net["usdc"], "amount": str(amount), "payTo": PAYTO,
                     "maxTimeoutSeconds": 60, "extra": extra}]}
            return httpx.Response(402, headers={"PAYMENT-REQUIRED": encoded(offer)})
        assert request.headers["payment-signature"] == SIGNED
        assert request.extensions["timeout"]["read"] == 90.0
        if state.behavior == "wait":
            state.started.set()
            assert state.release.wait(5)
        if state.behavior == "lost":
            raise httpx.ReadTimeout(SECRET + SIGNED + str(key))
        if state.behavior in ("refused", "conflict", "malformed"):
            headers = {"HodlOdds-Charged": "no"}
            if state.behavior == "conflict":
                headers["PAYMENT-RESPONSE"] = encoded({"success": True, "network": net["caip2"], "transaction": TX})
            if state.behavior == "malformed":
                headers["PAYMENT-RESPONSE"] = "not-base64"
            return httpx.Response(409, headers=headers, json=state.error_body or {
                "error": SECRET + SIGNED + str(key), "charged": False})
        row = {"mint": MINT, "p_crash_60m": 0.04, "confidence": {"level": "high"}}
        if state.behavior == "extended":
            row.update(chain=sdk.AVAILABLE_CHAINS["solana"], platform="pumpfun", future_field={"value": 1})
        if state.behavior == "unexpected":
            row.pop("p_crash_60m")
        if state.summary:
            row["summary"] = summary(0.04)
        row.update(state.row_fields)
        data = {"as_of": 1800000000, "horizon_end": 1800003600, "issued_at": 1800000020, "expires_at": 1800000140}
        if is_last:
            data.update(stale=True, age_s=300, freshness="stale", horizon_remaining_s=3280)
        data.update(state.answer_fields)
        if is_board:
            data.update(rows=[dict(row) for _ in range(state.rows)], not_delivered=[
                {"mint": OTHER, "stage": "unknown", "reason": "Not observed"}])
        else:
            data["row"] = row
        receipt = {"success": True, "network": net["caip2"], "transaction": TX, "payer": PAYER, "amount": str(amount)}
        headers = {} if state.behavior == "no_receipt" else {"PAYMENT-RESPONSE": encoded(receipt)}
        return httpx.Response(200, headers=headers, json=data)

    monkeypatch.setattr(sdk, "_new_http", lambda timeout, headers: httpx.Client(
        transport=httpx.MockTransport(handler), headers=headers, timeout=timeout))

    class Signer:
        def set_spend_controls(self, value):
            pass

        def create_payment_payload(self, offer):
            state.signs += 1
            return object()

    def load(client):
        state.loads += 1
        assert sdk._read_key_file(client._keypair_path).decode() == SECRET
        client._protocol()["encode"] = lambda payload: SIGNED
        return {"payer": PAYER, "client": Signer()}

    monkeypatch.setattr(sdk.HodlOdds, "_sdk_client", load)
    return state


@pytest.mark.anyio
@pytest.mark.parametrize("keyed", [False, True])
async def test_listing_and_free_tools(api, monkeypatch, keyed):
    if not keyed:
        monkeypatch.delenv("HODLODDS_KEYPAIR")
        monkeypatch.delenv("HODLODDS_SESSION_BUDGET_USDC")
    async with session(create_server()) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == {"status", "coverage", "meaning", "markets"} | ({"crash_odds", "last_known", "board"} if keyed else set())
        assert (await call(client, "status"))["age_s"] == 20
        assert (await call(client, "markets"))["markets"][0]["platform"] == "pumpfun"
        assert (await call(client, "coverage", mint=MINT))["stage"] == "unknown"
        assert (await call(client, "meaning"))["meaning"] == MEANING
        if not keyed:
            assert (await call(client, "crash_odds", mint=MINT))["error"] == "invalid_tool_call"
    assert api.loads == api.signs == 0


@pytest.mark.anyio
@pytest.mark.parametrize("has_summary", [False, True])
async def test_paid_crash_odds(api, has_summary):
    api.summary = has_summary
    async with session(create_server()) as client:
        data = await call(client, "crash_odds", mint=MINT)
    assert data["mint"] == MINT and data["p_crash_60m"] == 0.04
    assert data["summary"] == summary(0.04) and data["confidence"] == "high"
    assert data["confidence_detail"] == {"level": "high"}
    assert data["horizon_end"] - data["as_of"] == 3600
    assert data["payment"]["transaction"] == TX and data["payment"]["network"] == "devnet"
    assert data["payment"]["amount_usdc"] == "0.01"
    assert data["spent_atomic"] == 10000 and data["budget_atomic"] == 20000
    assert api.signs == api.loads == 1 and len(api.requests) == 2


@pytest.mark.anyio
async def test_budget_before_key_access(api, monkeypatch):
    monkeypatch.setenv("HODLODDS_SESSION_BUDGET_USDC", "0.009999")
    async with session(create_server()) as client:
        data = await call(client, "crash_odds", mint=MINT)
    assert data["error"] == "budget_exhausted" and data["spent_atomic"] == 0
    assert api.signs == api.loads == 0 and len(api.requests) == 1


@pytest.mark.anyio
async def test_shared_budget_exact_exhaustion(api, monkeypatch):
    monkeypatch.setenv("HODLODDS_SESSION_BUDGET_USDC", "0.015")
    async with session(create_server()) as client:
        await call(client, "crash_odds", mint=MINT)
        data = await call(client, "board", mints=[MINT, OTHER])
        assert data["spent_atomic"] == data["budget_atomic"] == 15000
        assert (await call(client, "crash_odds", mint=MINT))["error"] == "budget_exhausted"
    assert api.signs == api.loads == 2


@pytest.mark.anyio
@pytest.mark.parametrize("behavior", ["lost", "no_receipt", "conflict", "malformed"])
async def test_ambiguous_pauses_only_paid_calls(api, behavior, capsys):
    api.behavior = behavior
    async with session(create_server()) as client:
        data = await call(client, "crash_odds", mint=MINT)
        assert data["error"] == "payments_paused" and data["spent_atomic"] == 10000
        assert "Restart only after checking the wallet" in data["message"]
        assert data["payment"]["payer"] == PAYER and data["payment"]["pay_to"] == PAYTO
        before = len(api.requests)
        for name, args in (("crash_odds", {"mint": MINT}), ("last_known", {"mint": MINT}),
                           ("board", {"mints": [MINT]})):
            assert (await call(client, name, **args))["payment"] == data["payment"]
        assert len(api.requests) == before
        assert (await call(client, "status"))["state"] == "live"
        assert (await call(client, "coverage", mint=MINT))["stage"] == "unknown"
        await call(client, "meaning")
    assert api.signs == api.loads == 1
    assert str(api.key) not in json.dumps(data)
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err and SIGNED not in captured.out + captured.err
    # 0.2.3 (Fable audit 21): the redeem credential is kept in a private file, never in the result
    pending = Path(data["pending_file"])
    assert pending.parent == api.home / ".config" / "hodlodds" / "pending"
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600 and SIGNED not in json.dumps(data)
    assert "hodlodds redeem <pending_file>" in data["message"]


@pytest.mark.anyio
async def test_confirmed_free_refusal_refunds_and_continues(api, monkeypatch):
    monkeypatch.setenv("HODLODDS_SESSION_BUDGET_USDC", "0.01")
    api.behavior = "refused"
    async with session(create_server()) as client:
        data = await call(client, "crash_odds", mint=MINT)
        assert data["error"] == "paid_request_failed" and data["spent_atomic"] == 0
        assert data["payment"]["charged"] is False
        api.behavior = "ok"
        assert (await call(client, "crash_odds", mint=MINT))["spent_atomic"] == 10000
    assert api.signs == 2


@pytest.mark.anyio
async def test_watchlist_and_filters(api):
    async with session(create_server()) as client:
        data = await call(client, "board", mints=[OTHER, MINT], sort="riskiest", max_risk=0.2, min_confidence="high")
    assert data["rows"][0]["odds_percent"] == 4.0
    assert data["rows"][0]["summary"] == summary(0.04)
    assert data["not_delivered"] == [{"mint": OTHER, "stage": "unknown", "reason": "Not observed"}]
    assert data["spent_atomic"] == 5000
    assert api.requests[0].url == api.requests[1].url
    assert api.requests[0].url.params["mints"] == ",".join(sorted([MINT, OTHER]))


@pytest.mark.anyio
async def test_board_cap_and_binding_before_signing(api):
    api.rows = 3
    async with session(create_server()) as client:
        data = await call(client, "board", limit=10)
        assert data["error"] == "client_refused" and data["http_status"] is None
        assert data["message"] == ("This board costs 0.015 USDC but HODLODDS_MAX_PRICE_USDC is 0.01; "
                                   "raise it to at least 0.015.")
        assert data["payment"] is None and data["spent_atomic"] == 0
        api.rows, api.invalid_binding = 1, True
        data = await call(client, "board", mints=[MINT])
        assert data["error"] == "client_refused"
        assert data["message"] == "board offer does not match the requested query and per-coin price"
    assert api.signs == api.loads == 0


@pytest.mark.anyio
async def test_board_large_explicit_cap(api, monkeypatch):
    monkeypatch.setenv("HODLODDS_MAX_PRICE_USDC", "2.5")
    monkeypatch.setenv("HODLODDS_SESSION_BUDGET_USDC", "2.5")
    api.rows = 500
    async with session(create_server()) as client:
        data = await call(client, "board", limit=500)
    assert data["spent_atomic"] == 2500000 and len(data["rows"]) == 500


@pytest.mark.anyio
async def test_free_empty_and_refusal(api):
    async with session(create_server()) as client:
        api.behavior = "empty"
        data = await call(client, "board", mints=[MINT])
        assert data["rows"] == [] and data["not_delivered"][0]["mint"] == MINT
        assert data["spent_atomic"] == 0 and data["payment"] is None
        api.behavior = "free"
        assert (await call(client, "crash_odds", mint=MINT))["error"] == "paid_request_failed"
    assert api.signs == api.loads == 0


@pytest.mark.anyio
async def test_free_tool_reports_a_local_refusal_as_such(api):
    # 0.2.3 (Fable audit 21): an invalid mint is refused locally, not a "free request failed" outage
    async with session(create_server()) as client:
        data = await call(client, "coverage", mint="not-a-mint")
        assert data["error"] == "client_refused" and data["http_status"] is None
    assert api.requests == []


@pytest.mark.anyio
async def test_bad_arguments_are_sanitized(api):
    async with session(create_server()) as client:
        for name, args in (("coverage", {"mint": SIGNED}), ("crash_odds", {}),
                           ("board", {"limit": SECRET}), ("board", {"mints": [MINT, MINT]})):
            assert (await call(client, name, **args))["error"]
    assert api.signs == api.loads == 0 and api.requests == []


@pytest.mark.anyio
async def test_concurrent_calls_cannot_overspend(api, monkeypatch):
    monkeypatch.setenv("HODLODDS_SESSION_BUDGET_USDC", "0.01")
    data = []
    async with session(create_server()) as client:
        async def buy():
            data.append(await call(client, "crash_odds", mint=MINT))
        async with anyio.create_task_group() as group:
            group.start_soon(buy)
            group.start_soon(buy)
    assert sum("error" not in row for row in data) == 1
    assert api.signs == api.loads == 1


@pytest.mark.anyio
async def test_cancellation_keeps_inflight_spend_and_free_tools_available(api, monkeypatch):
    monkeypatch.setenv("HODLODDS_SESSION_BUDGET_USDC", "0.01")
    api.behavior = "wait"
    scope = anyio.CancelScope()
    async with session(create_server()) as client:
        async def buy():
            with scope:
                await client.call_tool("crash_odds", {"mint": MINT})

        async with anyio.create_task_group() as group:
            group.start_soon(buy)
            assert await anyio.to_thread.run_sync(api.started.wait, 5)
            try:
                assert (await call(client, "status"))["state"] == "live"
                scope.cancel()
            finally:
                api.release.set()
        data = await call(client, "crash_odds", mint=MINT)
        assert data["error"] == "budget_exhausted" and data["spent_atomic"] == 10000
    assert api.signs == api.loads == 1


@pytest.mark.parametrize("network", ["solana", "base"])
def test_mainnet_requires_recipient(monkeypatch, network):
    monkeypatch.setenv("HODLODDS_NETWORK", network)
    with pytest.raises(ValueError, match="HODLODDS_PAY_TO"):
        create_server()


def test_key_requires_budget(api, monkeypatch):
    monkeypatch.delenv("HODLODDS_SESSION_BUDGET_USDC")
    with pytest.raises(ValueError, match="HODLODDS_SESSION_BUDGET_USDC"):
        create_server()
    assert api.loads == 0


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-1", "0.0000001", "1.00000000000000000000001"])
def test_exact_budget_configuration(api, monkeypatch, value):
    monkeypatch.setenv("HODLODDS_SESSION_BUDGET_USDC", value)
    with pytest.raises(ValueError):
        create_server()
    assert api.loads == 0


def test_defaults_and_startup_error_sanitized(monkeypatch, capsys):
    config = Config.from_env()
    assert config.network == "devnet" and config.base_url == "https://api.hodlodds.com"
    assert config.max_atomic == 10000 and config.keypair is None and config.pay_to is None
    monkeypatch.setenv("HODLODDS_NETWORK", SECRET)
    assert main() == 2
    captured = capsys.readouterr()
    assert captured.out == "" and SECRET not in captured.err


def test_meaning_freshness_and_summary_match_published_contract():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert MEANING in readme and FRESHNESS in readme
    # Frozen API wording at release; no private server checkout is needed.
    cases = [
        (0, 'under 0.1% chance of a 90%+ price drop within 60 minutes (fewer than 1 in 1,000). Low odds are not a safety signal.'),
        (0.0001, 'under 0.1% chance of a 90%+ price drop within 60 minutes (fewer than 1 in 1,000). Low odds are not a safety signal.'),
        (0.001, '0.1% chance of a 90%+ price drop within 60 minutes (about 1 in 1,000). Low odds are not a safety signal.'),
        (0.0095, '0.9% chance of a 90%+ price drop within 60 minutes (about 10 in 1,000). Low odds are not a safety signal.'),
        (0.04, '4% chance of a 90%+ price drop within 60 minutes (about 4 in 100). Low odds are not a safety signal.'),
        (0.0995, '10% chance of a 90%+ price drop within 60 minutes (about 10 in 100). Odds, not guarantees.'),
        (0.25, '25% chance of a 90%+ price drop within 60 minutes (about 25 in 100). Odds, not guarantees.'),
        (0.99, '99% chance of a 90%+ price drop within 60 minutes (about 99 in 100). Odds, not guarantees.'),
        (1, 'over 99% chance of a 90%+ price drop within 60 minutes (more than 99 in 100). Odds, not guarantees.'),
    ]
    for probability, expected in cases:
        assert summary(probability) == expected
        assert "safe" not in summary(probability).split()


@pytest.mark.anyio
@pytest.mark.parametrize("tool,args", [("crash_odds", {"mint": MINT}), ("board", {"limit": 10})])
async def test_market_filters_pass_through_and_validate(api, tool, args):
    async with session(create_server()) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert {"chain", "platform"} <= set(tools[tool].input_schema["properties"])
        before = len(api.requests)
        data = await call(client, tool, **args, chain="robinhood")
        assert data["error"] == "client_refused"
        assert data["message"] == "chain not available; see /v1/markets"
        assert len(api.requests) == before and api.signs == 0
        data = await call(client, tool, **args, chain=["SOLANA"], platform="PUMP.FUN")
        assert "error" not in data
        for request in api.requests:
            assert request.url.params["chain"] == sdk.AVAILABLE_CHAINS["solana"]
            assert request.url.params["platform"] == "pumpfun"


@pytest.mark.anyio
@pytest.mark.parametrize("tool,args", [("crash_odds", {"mint": MINT}), ("board", {"limit": 10})])
@pytest.mark.parametrize("shape", ["extended", "unexpected"])
async def test_settled_row_shape_never_pauses_payments(api, tool, args, shape):
    api.behavior = shape
    async with session(create_server()) as client:
        data = await call(client, tool, **args)
        assert "error" not in data
        assert data["payment"]["success"] is True and data["spent_atomic"] > 0
        if shape == "unexpected":
            assert "warning" in data and "data" in data
        else:
            row = data["rows"][0] if tool == "board" else data
            assert row["future_field"] == {"value": 1}
            assert row["platform"] == "pumpfun"
            assert "warning" in row
        api.behavior = "ok"
        second = await call(client, tool, **args)
        assert "error" not in second
        assert api.signs == 2


@pytest.mark.anyio
async def test_last_known_freshness_and_shared_budget(api, monkeypatch):
    monkeypatch.setenv("HODLODDS_SESSION_BUDGET_USDC", "0.017")
    async with session(create_server()) as client:
        await call(client, "crash_odds", mint=MINT)
        data = await call(client, "last_known", mint=MINT, chain="solana", platform="pumpfun")
        assert data["stale"] is True and data["freshness"] == "stale"
        assert data["age_s"] == 300 and data["horizon_remaining_s"] == 3280
        assert data["payment"]["amount_usdc"] == "0.002"
        assert data["spent_atomic"] == 12000
        assert api.requests[-1].url.path == "/v1/risk/" + MINT + "/last"
        assert api.requests[-1].url.params["platform"] == "pumpfun"
        assert (await call(client, "board", mints=[MINT]))["spent_atomic"] == 17000
        assert (await call(client, "last_known", mint=MINT))["error"] == "budget_exhausted"
    assert api.signs == api.loads == 3


@pytest.mark.anyio
@pytest.mark.parametrize("variable,value,expected", [
    ("HODLODDS_MAX_PRICE_USDC", "0.001999", "client_refused"),
    ("HODLODDS_SESSION_BUDGET_USDC", "0.001999", "budget_exhausted"),
])
async def test_last_known_limits_before_key_access(api, monkeypatch, variable, value, expected):
    monkeypatch.setenv(variable, value)
    async with session(create_server()) as client:
        data = await call(client, "last_known", mint=MINT)
    assert data["error"] == expected and data["spent_atomic"] == 0
    assert api.signs == api.loads == 0


@pytest.mark.anyio
async def test_last_known_uncertainty_pauses_every_paid_tool(api):
    api.behavior = "lost"
    async with session(create_server()) as client:
        data = await call(client, "last_known", mint=MINT)
        assert data["error"] == "payments_paused" and data["spent_atomic"] == 2000
        before = len(api.requests)
        for tool, args in (("crash_odds", {"mint": MINT}), ("last_known", {"mint": MINT}),
                           ("board", {"mints": [MINT]})):
            assert (await call(client, tool, **args))["error"] == "payments_paused"
        assert len(api.requests) == before
    assert api.signs == 1


@pytest.mark.anyio
async def test_last_known_confirmed_free_refusal_keeps_budget(api, monkeypatch):
    monkeypatch.setenv("HODLODDS_SESSION_BUDGET_USDC", "0.002")
    api.behavior = "refused"
    async with session(create_server()) as client:
        data = await call(client, "last_known", mint=MINT)
        assert data["error"] == "paid_request_failed" and data["spent_atomic"] == 0
        assert data["payment"]["charged"] is False
        api.behavior = "ok"
        assert (await call(client, "last_known", mint=MINT))["spent_atomic"] == 2000
    assert api.signs == 2


@pytest.mark.anyio
@pytest.mark.parametrize("tool,args", [
    ("crash_odds", {"mint": MINT}), ("last_known", {"mint": MINT}), ("board", {"mints": [MINT]})
])
async def test_all_documented_evidence_survives_without_unknown_warning(api, tool, args):
    band = {"lo": .005, "hi": .01, "n": 33234, "pred": .0073, "obs": .0133,
            "historical_observed": .0133, "basis": "Historical weekly refits"}
    confidence = {"level": "low", "reasons": ["historical under-prediction"], "band": band,
                  "basis": "Historical weekly refits", "direction": "historically under-predicted"}
    note = ("This token already fell 90% or more from its high of the last 60 minutes. The odds cover "
            "a further 90% drop from the current price; they are not a recovery or safety signal.")
    api.row_fields = {"confidence": confidence, "band": band, "already_crashed": True,
                      "crashed_within_24h": True, "last_crash_as_of": 1799999940,
                      "note": note, "alert": False, "dd_60": -.99, "symbol": "TEST",
                      "name": "Example token", "resq_sol": 120, "age_min": 1440,
                      "ret_5": -.95, "ret_60": -.99, "trades_5": 3, "kind": "sol",
                      "chain": sdk.AVAILABLE_CHAINS["solana"], "chain_name": "Solana",
                      "platform": "pumpfun", "market_status": "available", "asset": "SOL"}
    api.answer_fields = {"model": {"id": "example-model", "hashes": {"model": "a" * 64}},
                         "definition": "Example event definition", "covered": True,
                         "coverage": {"universe": "SOL-quoted pump.fun graduates"}}
    if tool == "board":
        api.answer_fields.update(sort="lowest_risk", total_matches=37, returned=1, limit=1,
                                 filters={"include_crashed": True}, defaults_applied={},
                                 excluded_by_defaults=0, unit_price_usdc="0.005", price_usdc="0.005")
    async with session(create_server()) as client:
        data = await call(client, tool, **args)
    row = data["rows"][0] if tool == "board" else data
    assert row["confidence"] == "low" and row["confidence_detail"] == confidence
    for name, value in api.row_fields.items():
        assert row["confidence_detail" if name == "confidence" else name] == value
    for name, value in api.answer_fields.items():
        assert data[name] == value
    assert "warning" not in row and "warning" not in data


@pytest.mark.parametrize("confidence,level", [
    ({"level": "high", "reasons": ["historical evidence"]}, "high"),
    ({"level": "medium", "reasons": ["young token"]}, "medium"),
    ({"level": "low", "reasons": ["historical under-prediction"]}, "low"),
    ({"level": "unknown", "reasons": ["unvalidated"]}, "missing"),
    ({}, "missing"), (None, "missing"),
])
def test_0_1_row_contract_preserved_with_additive_confidence_detail(confidence, level):
    source = {"mint": MINT, "p_crash_60m": .04, "confidence": confidence,
              "dd_60": -.1, "band": {"lo": .02, "hi": .05, "historical_observed": .038}}
    # Golden 0.1.0 output for the same source, including its string confidence.
    legacy = {**source, "odds_percent": 4., "summary": summary(.04), "confidence": level}
    actual = odds_row(source)
    assert isinstance(actual["confidence"], str)
    assert actual["confidence_detail"] == (confidence or {})
    assert {key: value for key, value in actual.items() if key != "confidence_detail"} == legacy
    assert "confidence_level" not in actual


@pytest.mark.anyio
@pytest.mark.parametrize("include", [None, False, True])
async def test_board_new_filter_alias_and_explicit_zero(api, include):
    api.answer_fields = {"filters": {"min_trades_5": 0}, "defaults_applied": {}, "total_matches": 1}
    args = {"min_trades_5": 0, "sort": "safest", "mints": [MINT]}
    if include is not None:
        args["include_crashed"] = include
        api.answer_fields["filters"]["include_crashed"] = include
    async with session(create_server()) as client:
        tools = {item.name: item for item in (await client.list_tools()).tools}
        assert "include_crashed" in tools["board"].input_schema["properties"]
        data = await call(client, "board", **args)
    assert "error" not in data
    assert api.requests[0].url.params["min_trades_5"] == "0"
    assert api.requests[0].url.params["sort"] == "safest"
    if include is None:
        assert "include_crashed" not in api.requests[0].url.params
    else:
        assert api.requests[0].url.params["include_crashed"] == str(include).lower()
    assert data["filters"] == api.answer_fields["filters"]
    assert data["defaults_applied"] == {} and data["total_matches"] == 1


@pytest.mark.anyio
async def test_board_effective_defaults_are_preserved(api):
    api.answer_fields = {"filters": {"min_trades_5": 3, "include_crashed": False},
                         "defaults_applied": {"min_trades_5": 3, "include_crashed": False,
                                              "reason": "Low-risk defaults applied"}, "total_matches": 17}
    async with session(create_server()) as client:
        data = await call(client, "board", limit=10)
    assert "error" not in data
    assert "min_trades_5" not in api.requests[0].url.params
    assert "include_crashed" not in api.requests[0].url.params
    for key in api.answer_fields:
        assert data[key] == api.answer_fields[key]


@pytest.mark.anyio
async def test_offer_changed_refusal_is_structured_and_never_retried_after_signing(api):
    api.behavior = "refused"
    api.error_body = {"error": "matches changed; request again", "code": "offer_changed", "charged": False,
                      "untrusted_debug": SECRET + SIGNED}
    async with session(create_server()) as client:
        data = await call(client, "board", mints=[MINT])
    assert data["error"] == "paid_request_failed" and data["http_status"] == 409
    assert data["server_code"] == "offer_changed"
    assert data["server_error"] == "matches changed; request again"
    assert data["spent_atomic"] == 0 and api.signs == 1 and len(api.requests) == 2


@pytest.mark.anyio
async def test_unsigned_board_offer_refresh_is_shared_sdk_path_and_single_charge(api, monkeypatch):
    monkeypatch.setenv("HODLODDS_SESSION_BUDGET_USDC", "0.005")
    api.initial_changes = 1
    async with session(create_server()) as client:
        data = await call(client, "board", mints=[MINT])
        assert "error" not in data
        assert data["spent_atomic"] == data["budget_atomic"] == 5000
        assert api.signs == api.loads == 1 and len(api.requests) == 3
        assert all("payment-signature" not in request.headers for request in api.requests[:2])
        assert api.requests[0].url == api.requests[1].url == api.requests[2].url
        assert (await call(client, "last_known", mint=MINT))["error"] == "budget_exhausted"
    assert api.signs == api.loads == 1


@pytest.mark.anyio
async def test_unsigned_board_offer_refresh_stops_once_without_spending_or_pausing(api):
    api.initial_changes = 2
    async with session(create_server()) as client:
        data = await call(client, "board", mints=[MINT])
        assert data["error"] == "paid_request_failed" and data["http_status"] == 409
        assert data["server_code"] == "offer_changed" and data["spent_atomic"] == 0
        assert len(api.requests) == 2 and api.signs == api.loads == 0
        assert "error" not in await call(client, "board", mints=[MINT])
    assert api.signs == api.loads == 1


@pytest.mark.parametrize("body,expected", [
    ({"error": "sort must be one of: lowest_risk, safest, riskiest, liquidity, newest, active"},
     {"server_error": "sort must be one of: lowest_risk, safest, riskiest, liquidity, newest, active"}),
    ({"error": "min_trades_5 must be an integer"}, {"server_error": "min_trades_5 must be an integer"}),
    ({"error": SECRET + SIGNED, "code": SECRET}, {}),
    ({"error": "sort must be one of: " + SECRET}, {}),
    ({"error": {"key": SECRET}, "code": "offer_changed"}, {"server_code": "offer_changed"}),
])
def test_server_error_allowlist_never_echoes_rejected_secrets(body, expected):
    exc = sdk.HodlOddsError(SECRET + SIGNED, status=400, body=body)
    assert error_details(exc) == {"http_status": 400, **expected}


@pytest.mark.anyio
async def test_price_cap_refusal_names_the_variable_and_amount(api):
    api.rows = 10
    async with session(create_server()) as client:
        data = await call(client, "board", limit=10)
    assert data["error"] == "client_refused" and data["http_status"] is None
    assert data["message"] == "This board costs 0.05 USDC but HODLODDS_MAX_PRICE_USDC is 0.01; raise it to at least 0.05."
    assert data["detail"] == "Refused locally before any payment was sent; nothing was sent or charged."
    assert data["spent_atomic"] == 0 and data["payment"] is None
    assert len(api.requests) == 1 and api.signs == api.loads == 0


@pytest.mark.anyio
async def test_invalid_arguments_are_refused_with_fixed_sdk_text_before_any_request(api):
    async with session(create_server()) as client:
        data = await call(client, "board", limit=5)
        assert data["error"] == "client_refused" and data["message"] == "limit must be an integer between 10 and 500"
        data = await call(client, "board", mints=[MINT, MINT])
        assert data["message"] == "duplicate mints are not allowed"
        data = await call(client, "board", min_risk=0.5, max_risk=0.1)
        assert data["message"] == "min_risk must not exceed max_risk"
        data = await call(client, "crash_odds", mint=SECRET)
        assert data["error"] == "client_refused" and data["message"] == "mint must be a Solana address"
    assert api.requests == [] and api.signs == api.loads == 0


@pytest.mark.anyio
async def test_defaults_explanations_and_unknown_memory_pass_through(api):
    api.row_fields = {"already_crashed": False, "crashed_within_24h": None, "last_crash_as_of": None}
    api.answer_fields = {"filters": {"min_trades_5": 3, "min_liquidity_sol": 5, "include_crashed": False},
                         "defaults_applied": {"min_trades_5": 3, "min_liquidity_sol": 5, "include_crashed": False,
                                              "reason": "Low-risk discovery applies the omitted filters shown here; "
                                                        "recent-crash memory unavailable."},
                         "excluded_by_defaults": 12, "total_matches": 1}
    async with session(create_server()) as client:
        data = await call(client, "board", limit=10)
    for name, value in api.answer_fields.items():
        assert data[name] == value
    row = data["rows"][0]
    assert row["crashed_within_24h"] is None and row["last_crash_as_of"] is None
    assert "warning" not in row and "warning" not in data
