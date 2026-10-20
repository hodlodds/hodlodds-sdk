# hodlodds — Python client

Pay-per-answer **crash odds for Solana memecoins**, paid per request with
[x402](https://www.x402.org) (USDC on Solana or Base). No accounts, no API keys.

The API at `https://api.hodlodds.com` accepts payments. Check its free status,
markets and coverage endpoints before paying. Local test servers can use
**Solana devnet** or **Base Sepolia**.

## What it answers

> What are the odds that this token's price falls 90% or more within the next 60 minutes?

0.04 is an estimated 4% chance of the defined event within the forecast window.
Confidence describes historical calibration in that probability range, not a
guarantee for this token. Higher means riskier, but a low number is not a safety
signal: smaller or slower drops are not counted, and odds change minute by minute.

A crash is a completed one-minute close, priced in SOL, at least 90% below the
reference price within the next 60 minutes. Coverage: SOL-quoted pump.fun
graduates. Every answer carries a confidence level (high / medium / low) based on
how well past predictions in that probability range matched reality.

`dd_60` is the latest close divided by the maximum close in the previous
60 minutes, minus one. Zero means the token is at that hour's high;
`dd_60 <= -0.90` means it has already fallen at least 90% within the hour.
Rows expose `already_crashed` (`true`, `false`, or `null` when the drawdown
is unknown). A true value also carries a `note` and a warning in `summary`:
a low crash probability after a collapse is not a recovery or safety signal.
Below about 2%, observed crash rates have run up to about twice the predicted
value, under one percentage point in absolute terms; those answers carry
`confidence: low`.

| Route | Price | Notes |
|---|---|---|
| `GET /v1/status` | free | Feed state (`live` / `stale` / `down`), age, incidents, uptime |
| `GET /v1/meta` | free | Definition, prices, payment details, model card |
| `GET /v1/markets` | free | Available data chains, platforms and coverage |
| `GET /v1/coverage/{mint}` | free | Coverage stage and reason for one token |
| `GET /v1/risk/{mint}` | $0.01 | Fresh odds for one token |
| `GET /v1/board?limit=N` | $0.005 per coin delivered | Request 10–500 coins, or a watchlist of 1–100 distinct mints; pay only for coins returned, empty is free |
| `GET /v1/risk/{mint}/last` | $0.002 | Last known odds, at most 30 minutes old (also while live); when stale it is labelled stale and confidence drops one level |

The normal risk and board routes charge only for fresh, covered answers. The
opt-in `/last` route can charge for clearly labelled last-known odds within its
30-minute limit. Uncovered and ineligible answers are free. A refusal carrying
`HodlOdds-Charged: no` is treated as free unless a successful payment receipt
contradicts it; this includes an oversized-header `431`. If eligibility changes
after signing, that free refusal creates no pending payment.

### Freshness

Scores are recomputed every 60 seconds, about 20 seconds after each minute closes, from completed one-minute bars up to `as_of`. Each score covers the 60 minutes after its `as_of` time and is valid only until the next update; the odds for the same token can rise sharply from one minute to the next. Scoring starts around :20; computation and publication can delay availability. For decisions, refresh immediately before acting and once per minute for continuous monitoring, checking `as_of` for a new minute. The API freshness cutoff is separate: by default, data becomes `stale` more than 120 seconds after `issued_at` or after `expires_at`. A new request does not restart the forecast window.

The opt-in `/v1/risk/{mint}/last` route returns stored odds at most 30 minutes after issuance, including exactly 1800 seconds. It also works while live. When stale, confidence drops high to medium or medium to low; low stays low and missing confidence stays missing. Its original horizon and expiry stay unchanged.

## Install

```sh
pip install hodlodds                                        # from PyPI
pip install "git+https://github.com/hodlodds/hodlodds-sdk"  # or the latest main
```

Python 3.10+. Dependencies: `httpx` and the official `x402[svm,evm]==2.25.0` SDK (pinned).

## MCP server

The separate [hodlodds-mcp package](mcp/) provides local stdio tools for market
discovery and crash odds, with per-call and session payment limits.

`pip install hodlodds-mcp`
See [the MCP README](mcp/README.md) for client configuration and payment safety.

## Use

Command line:

```sh
hodlodds status
hodlodds markets
hodlodds coverage <MINT> --chain solana --platform pumpfun
hodlodds risk <MINT> --keypair ~/.config/hodlodds/wallet.json --pay-to <RECIPIENT> --network solana
hodlodds risk <MINT> --keypair ~/.config/hodlodds/base-wallet.json --pay-to <BASE_RECIPIENT> --network base
hodlodds board --limit 100 --sort lowest_risk --max-risk 0.05 --min-liquidity 50 --max-price 0.5 \
  --keypair ... --pay-to ... --network solana
hodlodds last <MINT> --keypair ... --pay-to ... --network solana
```

Python:

```python
from hodlodds import HodlOdds, HodlOddsError

with HodlOdds("https://api.hodlodds.com", keypair="~/.config/hodlodds/wallet.json",
              network="solana", pay_to="<RECIPIENT>", max_price_usdc="0.01") as client:
    print(client.status()["state"])
    print(client.markets())
    print(client.coverage("<MINT>", chain="solana", platform="pumpfun"))
    try:
        answer = client.risk("<MINT>")
        row = answer.data["row"]
        print(row["p_crash_60m"], row["confidence"]["level"], answer.payment["explorer"])
    except HodlOddsError as e:
        print(e, e.status, e.body)
        # e.payment["charged"] == False means a confirmed free refusal.
        # A PendingPayment needs reconciliation or explicit redemption.
```

`<RECIPIENT>` is the payment address for your chosen payment network, published
in `GET /v1/meta`. Verify and pin it in your code: the client refuses any offer
that pays someone else.

Networks: `devnet` and `solana` (mainnet; `mainnet` is an alias) take a Solana
JSON key file; `base-sepolia` and `base` take an EVM key file
(`{"private_key": "0x..."}`, mode 0600). Prices are the same on both chains, and the
API offers both; the client signs only on the network you chose.

```python
base = HodlOdds("https://api.hodlodds.com", keypair="~/.config/hodlodds/base-wallet.json",
                network="base", pay_to="<BASE_RECIPIENT>", max_price_usdc="0.01")
```

Board options: `limit` 10–500 (default 50); `sort` = `lowest_risk` (default), `riskiest`,
`liquidity`, `newest`, `active`; filters `max_risk`, `min_risk`, `min_liquidity_sol`,
`min_age_min`, `max_age_min`, `min_trades_5`, `min_confidence` (`high`/`medium`/`low`)
and `include_crashed` (`True`/`False`).
The total price is `0.005 × coins returned`; pass `max_price_usdc` in Python or
`--max-price` on the CLI at least that high. The default per-call maximum is
0.01 USDC, so boards normally need an explicit higher limit. The hard cap is
2.5 USDC **for board calls only** and 1 USDC for individual risk/last calls.
`min_trades_5` accepts integers from 0 through 9223372036854775807.

Compatibility: the 0.2.0 `safest` sort remains accepted as an alias for
`lowest_risk`, and the `mainnet` payment-network alias still means `solana`.
These older spellings are deprecated in examples; existing calls keep working.

For `lowest_risk` and its `safest` alias, the server applies
`min_trades_5=3` and `include_crashed=false` when those filters are omitted,
so barely traded or already-collapsed tokens do not dominate the list.
Explicit values override the defaults, including `min_trades_5=0` and
`include_crashed=True` (CLI: `--min-trades 0 --include-crashed`).
Other sorts keep their existing selection. An explicit `mints=` watchlist
gets **neither default**: crashed holdings remain visible and flagged unless
you explicitly request `include_crashed=False`.

Board answers return effective `filters` and `defaults_applied`, which is
empty when no default was added. The offer binds exactly the client's own
normalized parameters. The server applies its documented defaults
deterministically when selecting and pricing rows; the client does not add
those defaults to the signed query.

```python
board = client.board(limit=100, sort="lowest_risk", max_risk=0.05, min_liquidity_sol=50, max_price_usdc="0.5")
for row in board.data["rows"]:
    print(row["mint"], row["p_crash_60m"], row["confidence"]["level"])
# Explicitly include quiet and already-collapsed tokens:
all_rows = client.board(min_trades_5=0, include_crashed=True, max_price_usdc="0.25")
# The cheaper, opt-in answer preserves its original horizon and labels its age:
last_known = client.last("<MINT>")
print(last_known.data["age_s"], last_known.data["freshness"])
```

`last()` costs 0.002 USDC and can return last-known odds up to 30 minutes
old, including while the feed is live. Check `stale`, `age_s`, `freshness`
and `horizon_remaining_s`; buying the answer does not renew its forecast.

### When a board offer changes

A board `409` with `code: "offer_changed"` means the matching set changed
between the offer and the paid request; the server marks that refusal
`HodlOdds-Charged: no`. SDK 0.3.1 requests a new offer **once only if the
409 occurs before signing**, with no successful or uncertain receipt. It
uses the same query and repeats all price and budget checks. There is no
extra spend for this unsigned offer refresh.

A board `409` with `code: "offer_changed"` received after a **signed request**
is **never retried automatically**. With `HodlOdds-Charged: no` and no conflicting
receipt, nothing was charged, but the client must call again; the SDK never
re-signs on its own. A second automatic authorization could be settled twice
if a server misbehaved. Watch loops treat a
confirmed free refusal as a skip and continue on their next scheduled
cycle. Uncertain payments stop the watch and require reconciliation.

### Watchlists and market filters

Pass `mints=[mint_a, mint_b]` to restrict a board to 1–100 **distinct** Solana
mint addresses. Duplicates are rejected locally before any request (and are a
free 400 on the server). The watchlist default limit is its length; an explicit
watchlist limit is 1–100. Sorting and filters still apply. The offer is bound to
the exact normalized selection, including the watchlist digest, before signing.
Only delivered rows are charged. `answer.data["not_delivered"]` reports coverage
and filter facts for requested mints that were not returned; it is not a score.

```sh
hodlodds board --mints <MINT_A>,<MINT_B> --max-price 0.01 \
  --keypair ... --pay-to ... --network solana
hodlodds board --mints-file watchlist.txt --max-price 0.50 \
  --keypair ... --pay-to ... --network solana
```

Use one mint per line in `--mints-file`; blank lines and `#` comments are ignored.
The two watchlist flags are mutually exclusive and also work with `watch --board`.

```python
answer = client.board(mints=[mint_a, mint_b], max_price_usdc="0.01",
                      chain="solana", platform="pumpfun")
print(answer.data["rows"], answer.data.get("not_delivered", []))
```

`chain` and `platform` select the **data market**, independently of the network
used to pay. They work on `risk`, `last`, `coverage`, `board`, `watch` and
`watch_board`; CLI equivalents are `--chain` and `--platform`. Python accepts a
string, comma-separated string or list. Values are case-insensitive; chain
aliases or CAIP-2 IDs are accepted. `pump` and `pump.fun` are aliases for
`pumpfun`. Lists contain 1–10 distinct supported values, with OR within a list
and AND across the two filters. Unsupported or contradictory values are refused
before payment. Omitting the filters preserves the existing default selection.

Use `client.markets()` / `hodlodds markets` to see supported values;
`client.coverage(mint)` / `hodlodds coverage <MINT>` explains whether a token is
covered. Paying on Base does not select Base token data. Experimental non-SOL
rankings remain outside paid boards and can appear in free risk-route refusals.

### Watching a token

The watch loop runs entirely in the client. Choose an integer interval from 1 to 60 minutes;
**`--max-spend` is required** and caps the whole watch in USDC. `--max-price`
still limits each offer. Examples (mainnet; pin the published recipient):

```sh
hodlodds watch <MINT> --every 1 --max-spend 1.00 \
  --keypair ~/.config/hodlodds/wallet.json --pay-to <RECIPIENT> --network solana
hodlodds watch <MINT> --every 5 --max-spend 1.00 --max-calls 20 --alert-above 0.20 \
  --keypair ~/.config/hodlodds/wallet.json --pay-to <RECIPIENT> --network solana
hodlodds watch --board --limit 100 --sort riskiest --max-price 0.50 --every 5 --max-spend 2.00 --alert-above 0.20 --json \
  --keypair ~/.config/hodlodds/wallet.json --pay-to <RECIPIENT> --network solana
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

After signing, a refusal marked `HodlOdds-Charged: no` without a successful
payment receipt releases the reserved spend and yields a skipped result; the
watch continues. It counts toward `--max-calls`. An ambiguous payment, a
conflicting successful receipt, or another payment error stops the watch; there
is no payment retry or automatic redemption. An unresolved attempt remains
reserved in `spent_atomic`. Python receives `HodlOddsError.payment`; the CLI
saves a private pending-payment file for explicit redemption as described below.
Other request errors and rejected offers also stop the loop. Do not start
another watch after an ambiguous payment until it is reconciled.

### If an answer is lost after you paid

The paid request waits up to 90 seconds (`paid_timeout`, 30–600). If the
connection ends after authorization, keep the pending record and use explicit
recovery instead of paying again:

- An eligible settled payment whose answer was not delivered can be redeemed
  **once** within 24 hours; a redeem still checks the route and answer eligibility.
- In Python, the error carries it: `client.redeem(error.payment)` re-sends the same
  authorization with `HodlOdds-Redeem: 1`. It never signs a new payment and never
  settles a second time.
- The CLI saves it to `~/.config/hodlodds/pending/<id>.json` (mode 0600) and prints
  the path; run `hodlodds redeem <file>` after about two minutes.
- A `409` may mean confirmation is still pending, no redeemable payment was
  found, or it has already been redeemed or expired. Follow `Retry-After` when
  supplied and keep the pending record on refusal. A refused redeem signs and
  settles nothing new; it does not by itself prove the original payment failed.

Environment variables for the CLI: `HODLODDS_BASE_URL`, `HODLODDS_NETWORK`,
`HODLODDS_KEYPAIR`, `HODLODDS_PAY_TO`, `HODLODDS_MAX_PRICE`.
Exit status: `0` success, `1` refusal/API/transport failure, `2` invalid usage.

## Safety rules built into the client

- A payment is signed only after the 402 offer passes every check: x402 v2, the
  chosen network, exact USDC, authorization flow, the pinned `payTo`, a valid
  facilitator fee payer distinct from you on Solana, a bounded timeout, and a price at or
  below your limit (default 0.01 USDC; hard cap 1 USDC for risk/last and
  2.5 USDC for board; exact atomic arithmetic).
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
installed, additional tests build real signatures with all RPC calls mocked.

## Disclaimer

Odds, not guarantees. Not financial advice. Low odds do not mean a token is
safe; collapses can happen without warning. The operator also runs an
independent trading system that may trade tokens HodlOdds scores; it never
receives customer requests or purchases, and nobody can pay to change a score.

## License

MIT
