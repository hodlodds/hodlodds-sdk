# hodlodds — Python client

Pay-per-answer **crash odds for Solana memecoins**, paid per request with
[x402](https://www.x402.org) (USDC on Solana or Base). No accounts, no API keys.

> **Status: pre-launch.** The API at `https://api.hodlodds.com` is not yet open for
> payments. Its model is being evaluated against a pre-registered forward test.
> A limited paid beta opens only if it passes. Until then this client is published
> so you can read the code and test the payment flow on **Solana devnet** or **Base Sepolia**.

## What it answers

> What are the odds that this token's price falls 90% or more within the next 60 minutes?

0.04 means about 4 in 100 tokens like this one lose 90% or more of their price within the next 60 minutes. Higher means riskier, but a low number does not mean safe: smaller or slower drops are not counted, and the odds change minute by minute.

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
| `GET /v1/risk/{mint}/last` | $0.002 | Last known odds, at most 30 minutes old (also while live); when stale it is labelled stale and confidence drops one level |

You only pay for fresh, covered answers. "Not covered", "feed interrupted" and
"too old" answers are free, and this client never signs for them.

### Freshness

Scores are recomputed every 60 seconds, about 20 seconds after each minute closes, from completed one-minute bars up to `as_of`. Each score covers the 60 minutes after its `as_of` time and is valid only until the next update; the odds for the same token can rise sharply from one minute to the next. Scoring starts around :20; computation and publication can delay availability. For decisions, refresh immediately before acting and once per minute for continuous monitoring, checking `as_of` for a new minute. The API freshness cutoff is separate: by default, data becomes `stale` more than 120 seconds after `issued_at` or after `expires_at`. A new request does not restart the forecast window.

The opt-in `/v1/risk/{mint}/last` route returns stored odds at most 30 minutes after issuance, including exactly 1800 seconds. It also works while live. When stale, confidence drops high to medium or medium to low; low stays low and missing confidence stays missing. Its original horizon and expiry stay unchanged.

## Install

```sh
pip install hodlodds                                        # from PyPI
pip install "git+https://github.com/hodlodds/hodlodds-sdk"  # or the latest main
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

Board options: `limit` 10–500 (default 50); `sort` = `safest` (default; lowest predicted odds first, a ranking, not a safety claim), `riskiest`,
`liquidity`, `newest`, `active`; filters `max_risk`, `min_risk`, `min_liquidity_sol`,
`min_age_min`, `max_age_min`, `min_trades_5`, `min_confidence` (`high`/`medium`/`low`).
The total price is `0.005 × coins returned`; pass a `max_price` at least that high
(the client caps a single board payment at 2.5 USDC).

```python
board = client.board(limit=100, sort="safest", max_risk=0.05, min_liquidity_sol=50, max_price_usdc="0.5")
for row in board.data["rows"]:
    print(row["mint"], row["p_crash_60m"], row["confidence"]["level"])
```

### Watching a token

The watch loop runs entirely in the client. Choose an integer interval from 1 to 60 minutes;
**`--max-spend` is required** and caps the whole watch in USDC. `--max-price`
still limits each offer. Examples (devnet; add your wallet flags as in the commands above):

```sh
hodlodds watch <MINT> --every 1 --max-spend 1.00 \
  --keypair ~/.config/hodlodds/wallet.json --pay-to <RECIPIENT> --network devnet
hodlodds watch <MINT> --every 5 --max-spend 1.00 --max-calls 20 --alert-above 0.20 \
  --keypair ~/.config/hodlodds/wallet.json --pay-to <RECIPIENT> --network devnet
hodlodds watch --board --limit 100 --sort riskiest --max-price 0.50 --every 5 --max-spend 2.00 --alert-above 0.20 --json \
  --keypair ~/.config/hodlodds/wallet.json --pay-to <RECIPIENT> --network devnet
```

Mainnet still requires explicit `--network solana` or `--network base` and
`--pay-to <recipient>`. All existing board filters are supported with `--board`.
The default per-call maximum remains 0.01; raise it explicitly for a larger board.

Estimated continuous costs, before skips or the total cap:

| Target | Every | Price/call (USDC) | Calls/hour | USDC/hour | USDC/24 h |
|---|---|---|---|---|---|
| Token | 1 minute | 0.01 | 60 | 0.60 | 14.40 |
| Token | 5 minutes | 0.01 | 12 | 0.12 | 2.88 |
| Board, limit 100 | 1 minute | at most 0.50 | 60 | at most 30.00 | at most 720.00 |
| Board, limit 100 | 5 minutes | at most 0.50 | 12 | at most 6.00 | at most 144.00 |

The token estimate uses the advertised 0.01 price. Board estimates use the full
requested limit at 0.005 per coin, even when fewer coins match or `--max-price`
is lower. Actual spending uses the exact atomic USDC amount in each checked offer.
The loop stops before signing an offer that would exceed the remaining cap.
Caps must be positive with at most six decimal places; pass decimal strings to
avoid float arithmetic before calling the SDK. There is no default spending cap.

Cycles start on UTC minute boundaries at **:25 plus 0–5 seconds of jitter**;
five-minute watches use :00, :05, :10, and so on. The first call waits for the
next scheduled boundary. Slow requests or consumers skip missed slots rather
than catching up. Every cycle checks the free `/v1/status` first:

- A feed whose `state` is not `live` produces a skipped result without payment.
  `--stop-on-stale` ends after that result; otherwise monitoring continues.
- If `as_of` equals the last paid answer's minute (or is older), the loop waits
  ten seconds and checks status once more. If still unchanged, it skips without
  payment. It compares against the paid answer's `as_of`, since publication may
  advance during payment processing.
- A new live minute makes one paid-route call through the existing payment
  checks. The server also rechecks freshness before serving a paid answer.
  The watch never uses the stale `/last` route.

**A score is valid only until the next minute's update.** Watching every five
minutes saves money but leaves gaps between current scores. Feed interruptions
and repeated minutes cost nothing. An empty board is also free. `--max-calls`
counts paid-route calls, including free empty boards; skipped status checks do
not count and a watch can keep waiting during an outage.

Text output shows UTC time, `as_of`, odds to one decimal percent (below 0.1% it prints `<0.1%`, never 0.0%), confidence,
and total spend/cap. `ALERT` marks odds at or above the threshold (0 to 1).
Boards print a summary and each row meeting the threshold. `--json` emits one
JSON object per cycle: all answer fields plus `watch` containing `cycle`,
`spent_usdc`, `cap_usdc`, `skipped_reason`, `utc` and `alert`. Skipped cycles
contain the status fields instead of a paid answer. Amounts are exact decimal
strings; `skipped_reason` is null for answers. Estimates and stop/error messages
go to stderr, keeping stdout valid JSON Lines.

```python
# Use the configured client from the Python example above.
watch = client.watch(mint, every_minutes=5, max_spend_usdc="1.00",
                     max_calls=20, alert_above=0.20, stop_on_stale=False)
print(watch.estimate)  # available immediately, before any request
try:
    for answer in watch:
        print(answer.data, answer.watch, answer.payment)
finally:
    watch.close()

board_watch = client.watch_board(every_minutes=5, max_spend_usdc="2.00",
                                 limit=100, max_price_usdc="0.50", max_calls=4)
board_watch.close()
```

Both methods return a closeable iterator backed by a generator, yielding
`Answer` objects. `answer.alerts` holds matching rows; `watch.spent_atomic`
exposes the integer spend, and `watch.stop_reason` explains a normal limit or
interrupt stop. The caller owns and closes the client. Closing the iterator
or pressing Ctrl-C between cycles stops cleanly. An interrupt during an
authorized request is reported as ambiguous with reconciliation details.

Any payment error or non-200 authorized response stops the watch immediately;
there is no payment retry or automatic redemption. The failed attempt's amount
remains reserved in `spent_atomic`. Python receives `HodlOddsError.payment`;
the CLI saves the existing private pending-payment file for explicit redemption
as described below. Other request errors and rejected offers also stop the loop.
Do not start another watch after an ambiguous payment until it is reconciled.

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
