# hodlodds — Python client

Pay-per-answer **crash odds for Solana memecoins**, paid per request with
[x402](https://www.x402.org) (USDC on Solana). No accounts, no API keys.

> **Status: pre-launch.** The API at `https://api.hodlodds.com` is not yet open for
> payments. Its model is being evaluated against a pre-registered forward test.
> A limited paid beta opens only if it passes. Until then this client is published
> so you can read the code and test the payment flow on **Solana devnet**.

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
| `GET /v1/board` | $0.05 | Ranked board of covered tokens |
| `GET /v1/risk/{mint}/last` | $0.002 | Last known odds during a feed interruption, at most 30 minutes old, labelled stale, confidence lowered one level |

You only pay for fresh, covered answers. "Not covered", "feed interrupted" and
"too old" answers are free, and this client never signs for them.

## Install

```sh
pip install "git+https://github.com/hodlodds/hodlodds-sdk"
```

Python 3.10+. Dependencies: `httpx` and the official `x402[svm]==2.25.0` SDK (pinned).

## Use

Command line:

```sh
hodlodds status
hodlodds risk <MINT> --keypair ~/.config/hodlodds/wallet.json --pay-to <RECIPIENT> --network devnet
hodlodds board --max-price 0.05 --keypair ... --pay-to ... --network devnet
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
  (`HodlOddsError.payment`) so you can reconcile against your wallet history.
- The key file must be a regular, non-symlink file with mode 0600. It is read
  locally and never printed or sent; only the SDK-signed USDC transfer
  authorization leaves your machine. Use a dedicated low-balance wallet.
- HTTPS only (plain HTTP only for a local test server). No redirects, no
  environment proxies, bounded headers and bodies.
- Mainnet requires an explicit `network="mainnet"` and a pinned `pay_to`; the CLI
  requires `--network mainnet --pay-to ...` on the command line.

## Test wallet (devnet)

```sh
hodlodds-wallet new ~/.config/hodlodds/my-devnet.json   # prints the public address only
```

Fund it with devnet USDC at https://faucet.circle.com (network: Solana Devnet).
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
