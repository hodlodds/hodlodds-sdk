# hodlodds — Python client

Pay-per-answer **crash odds for Solana memecoins**, paid per request with
[x402](https://www.x402.org) (USDC on Solana or Base). No accounts, no API keys.

> **Status: pre-launch.** The API at `https://api.hodlodds.com` is not yet open for
> payments. Its model is being evaluated against a pre-registered forward test.
> A limited paid beta opens only if it passes. Until then this client is published
> so you can read the code and test the payment flow on **Solana devnet** or **Base Sepolia**.

## What it answers

> What are the odds that this token's price falls 90% or more within the next 60 minutes?

A crash is a completed one-minute close, priced in SOL, at least 90% below the
reference price within the next 60 minutes. Coverage: SOL-quoted pump.fun
graduates. Every answer carries a confidence level (high / medium / low) based on
how well past predictions in that probability range matched reality.

| Route | Price (planned) | Notes |
|---|---|---|
| `GET /v1/status` | free | Feed state (`live` / `stale` / `down`), age, incidents, uptime |
| `GET /v1/meta` | free | Definition, prices, payment details, model card |
| `GET /v1/risk/{mint}` | $0.01 | Fresh odds for one token |
| `GET /v1/board?limit=N` | $0.005 per coin delivered | Ranked, filtered board of 10–500 covered tokens; pay only for coins returned, empty is free |
| `GET /v1/risk/{mint}/last` | $0.002 | Last known odds during a feed interruption, at most 30 minutes old, labelled stale, confidence lowered one level |

You only pay for fresh, covered answers. "Not covered", "feed interrupted" and
"too old" answers are free, and this client never signs for them.

## Install

```sh
pip install "git+https://github.com/hodlodds/hodlodds-sdk"
```

Python 3.10+. Dependencies: `httpx` and the official `x402[svm,evm]==2.25.0` SDK (pinned).

## Use

Command line:

```sh
hodlodds status
hodlodds risk <MINT> --keypair ~/.config/hodlodds/wallet.json --pay-to <RECIPIENT> --network devnet
hodlodds risk <MINT> --keypair ~/.config/hodlodds/base-wallet.json --pay-to <BASE_RECIPIENT> --network base-sepolia
hodlodds board --limit 100 --sort safest --max-risk 0.05 --min-liquidity 50 --max-price 0.5 \
  --keypair ... --pay-to ... --network devnet
```

Python:

```python
from hodlodds import HodlOdds, HodlOddsError

with HodlOdds("https://api.hodlodds.com", keypair="~/.config/hodlodds/wallet.json",
              network="devnet", pay_to="<RECIPIENT>", max_price_usdc="0.01") as client:
    print(client.status()["state"])
    try:
        answer = client.risk("<MINT>")
        row = answer.data["row"]
        print(row["p_crash_60m"], row["confidence"]["level"], answer.payment["explorer"])
    except HodlOddsError as e:
        print(e, e.status, e.body)   # free refusals land here; e.payment is set if a payment may have happened
```

`<RECIPIENT>` is the payment address published in `GET /v1/meta` (and in this
README at launch). Pin it in your code: the client refuses any offer that pays
someone else.

Networks: `devnet` and `solana` (mainnet; `mainnet` is an alias) take a Solana
JSON key file; `base-sepolia` and `base` take an EVM key file
(`{"private_key": "0x..."}`, mode 0600). Prices are the same on both chains, and the
API offers both; the client signs only on the network you chose.

```python
base = HodlOdds("https://api.hodlodds.com", keypair="~/.config/hodlodds/base-wallet.json",
                network="base", pay_to="<BASE_RECIPIENT>", max_price_usdc="0.01")
```

Board options: `limit` 10–500 (default 50); `sort` = `safest` (default), `riskiest`,
`liquidity`, `newest`, `active`; filters `max_risk`, `min_risk`, `min_liquidity_sol`,
`min_age_min`, `max_age_min`, `min_trades_5`, `min_confidence` (`high`/`medium`/`low`).
The total price is `0.005 × coins returned`; pass a `max_price` at least that high
(the client caps a single board payment at 2.5 USDC).

```python
board = client.board(limit=100, sort="safest", max_risk=0.05, min_liquidity_sol=50, max_price_usdc="0.5")
for row in board.data["rows"]:
    print(row["mint"], row["p_crash_60m"], row["confidence"]["level"])
```

### If an answer is lost after you paid

The paid request waits up to 90 seconds (`paid_timeout`, 30–600): verification and
on-chain settlement usually take one to three seconds. If the connection still ends
after the payment settled, nothing is lost:

- The server keeps that payment redeemable **once** for 24 hours.
- In Python, the error carries it: `client.redeem(error.payment)` re-sends the same
  authorization with `HodlOdds-Redeem: 1`. It never signs a new payment and never
  settles a second time.
- The CLI saves it to `~/.config/hodlodds/pending/<id>.json` (mode 0600) and prints
  the path; run `hodlodds redeem <file>` after about two minutes.
- While the server is still confirming the payment on-chain, a redeem answers
  `409` with `Retry-After`; if the payment never settled, it says so (nothing was
  charged).

Environment variables for the CLI: `HODLODDS_BASE_URL`, `HODLODDS_NETWORK`,
`HODLODDS_KEYPAIR`, `HODLODDS_PAY_TO`, `HODLODDS_MAX_PRICE`.
Exit status: `0` success, `1` refusal/API/transport failure, `2` invalid usage.

## Safety rules built into the client

- A payment is signed only after the 402 offer passes every check: x402 v2, the
  chosen network, exact USDC, authorization flow, the pinned `payTo`, a valid
  facilitator fee payer distinct from you, a bounded timeout, and a price at or
  below your limit (default 0.01 USDC, hard cap 1 USDC; exact atomic arithmetic).
- Payments are **never retried automatically**. If anything goes wrong after an
  authorization was sent, the error says so and carries the details
  (`HodlOddsError.payment`) so you can reconcile against your wallet history or
  redeem the answer once (see above).
- The key file must be a regular, non-symlink file with mode 0600. It is read
  locally and never printed or sent; only the SDK-signed USDC transfer
  authorization leaves your machine. Use a dedicated low-balance wallet.
- HTTPS only (plain HTTP only for a local test server). No redirects, no
  environment proxies, bounded headers and bodies.
- Mainnets require an explicit `network="solana"` (or `"base"`) and a pinned
  `pay_to`; the CLI requires `--network solana|base --pay-to ...` on the command line.

## Test wallet (devnet / Base Sepolia)

```sh
hodlodds-wallet new ~/.config/hodlodds/my-devnet.json   # prints the public address only
hodlodds-wallet new --network base-sepolia ~/.config/hodlodds/base-sepolia.json
```

Fund it with test USDC at https://faucet.circle.com (network: Solana Devnet or Base Sepolia).
Never put real funds in a test wallet.

## Tests

```sh
python -m unittest discover -s tests -v
```

The tests run offline with a fake server and a fake SDK seam; with the pinned SDK
installed, one more test builds a real signature with all RPC calls mocked.

## Disclaimer

Odds, not guarantees. Not financial advice. Low odds do not mean a token is
safe; collapses can happen without warning. The operator also runs an
independent trading system that may trade tokens HodlOdds scores; it never
receives customer requests or purchases, and nobody can pay to change a score.

## License

MIT
