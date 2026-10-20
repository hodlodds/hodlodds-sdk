import base64
import hashlib
import json
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

from hodlodds_mcp.server import Config, DISCLAIMER, FRESHNESS, MEANING, create_server, main, summary

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
                            started=threading.Event(), release=threading.Event())
    net = sdk._network_config("devnet")
    monkeypatch.setenv("HODLODDS_BASE_URL", "http://localhost:8010")
    monkeypatch.setenv("HODLODDS_NETWORK", "devnet")
    monkeypatch.setenv("HODLODDS_PAY_TO", PAYTO)
    monkeypatch.setenv("HODLODDS_KEYPAIR", str(key))
    monkeypatch.setenv("HODLODDS_SESSION_BUDGET_USDC", "0.02")

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
        if state.behavior == "free":
            return httpx.Response(404, json={"error": SECRET + SIGNED})
        if is_board and state.behavior == "empty":
            return httpx.Response(200, json={"rows": [], "not_delivered": [
                {"mint": MINT, "stage": "unknown", "reason": "Not observed"}]})
        amount = state.rows * 5000 if is_board else state.amount
        if "payment-signature" not in request.headers:
            extra = {"paymentFlow": "authorization", "feePayer": "11111111111111111111111111111111"}
            if is_board:
                params = dict(request.url.params)
                mints = params.pop("mints", None)
                markets = {k + "s": params.pop(k).split(",") for k in ("chain", "platform") if k in params}
                query = {"limit": int(params.pop("limit")), "sort": params.pop("sort"),
                         "filters": {k: v if k == "min_confidence" else float(v) for k, v in params.items()}}
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
            return httpx.Response(409, headers=headers, json={"error": SECRET + SIGNED + str(key), "charged": False})
        row = {"mint": MINT, "p_crash_60m": 0.04, "confidence": {"level": "high"}}
        if state.behavior == "extended":
            row.update(chain=sdk.AVAILABLE_CHAINS["solana"], platform="pumpfun", future_field={"value": 1})
        if state.behavior == "unexpected":
            row.pop("p_crash_60m")
        if state.summary:
            row["summary"] = summary(0.04)
        data = {"as_of": 1800000000, "horizon_end": 1800003600, "issued_at": 1800000020, "expires_at": 1800000140}
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
        assert names == {"status", "coverage", "meaning", "markets"} | ({"crash_odds", "board"} if keyed else set())
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
        for name, args in (("crash_odds", {"mint": MINT}), ("board", {"mints": [MINT]})):
            assert (await call(client, name, **args))["payment"] == data["payment"]
        assert len(api.requests) == before
        assert (await call(client, "status"))["state"] == "live"
        assert (await call(client, "coverage", mint=MINT))["stage"] == "unknown"
        await call(client, "meaning")
    assert api.signs == api.loads == 1
    assert str(api.key) not in json.dumps(data)
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err and SIGNED not in captured.out + captured.err


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
        assert (await call(client, "board", limit=10))["error"] == "paid_request_failed"
        api.rows, api.invalid_binding = 1, True
        assert (await call(client, "board", mints=[MINT]))["error"] == "paid_request_failed"
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
        assert data["error"] == "paid_request_failed"
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
