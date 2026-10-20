# hodlodds-mcp

<!-- mcp-name: com.hodlodds/hodlodds-mcp -->

A local stdio MCP server for HodlOdds crash odds. Free tools explain token
coverage, data markets and feed status. With a local wallet configured, paid
tools use x402 USDC on Solana or Base with per-call and per-session spending
limits. Paying on Base does not change the data universe: current paid scores
cover active SOL-quoted pump.fun graduates.

Source: [HodlOdds SDK and MCP public repository](https://github.com/hodlodds/hodlodds-sdk/tree/main/mcp).

## Install

```sh
pip install hodlodds-mcp
hodlodds-mcp
# Or run in an isolated environment:
uvx hodlodds-mcp
```

Python 3.11+ is required. Dependencies are `hodlodds>=0.3.2,<0.4` and
`mcp>=2.3,<3`. The HodlOdds client supplies its pinned x402 dependencies. This
package starts a local process, not an HTTP MCP endpoint. Stdout is reserved
for MCP protocol messages.

## Tools and prices

| Tool | Cost in USDC | Result |
| --- | --- | --- |
| `status()` | Free | Feed state, age, source timestamp and API status fields |
| `markets()` | Free | Available and experimental data markets, prices and filter syntax |
| `coverage(mint)` | Free | Observed coverage stage, reason and source timestamp |
| `meaning()` | Free, offline | Crash definition, confidence interpretation and freshness rules |
| `crash_odds(mint)` | 0.01 per delivered answer | Crash probability, summary, confidence, forecast timestamps, receipt and budget |
| `last_known(mint)` | 0.002 per delivered answer | Labelled last-known odds, at most 30 minutes old, with freshness, age, receipt and budget |
| `board(...)` | 0.005 per delivered coin; empty results free | Ranked rows or a watchlist, omitted-mint facts, timestamps, receipt and budget |

`last_known` explicitly opts into the SDK's `last()` route. It can also be used
while the feed is live. Its `stale`, `freshness`, `age_s` and
`horizon_remaining_s` fields preserve the original forecast window; paying
does not restart it. When stale, confidence is downgraded one level. The same
per-call cap, session budget and uncertain-payment pause apply to all paid tools.

`board` accepts `limit`, `sort`, `max_risk`, `min_risk`, `min_liquidity_sol`,
`min_age_min`, `max_age_min`, `min_trades_5`, `min_confidence`, `include_crashed`,
`mints`, `chain` and `platform`. Sorts are `lowest_risk` (default), `safest`
(an alias for `lowest_risk`), `riskiest`, `liquidity`,
`newest` and `active`. Without `mints`, limits are 10–500 (default 50).
With a watchlist of 1–100 distinct Solana mint addresses, the default limit is
its length and explicit limits are 1–100. Only returned coins are charged.
`not_delivered` contains coverage/filter facts for omitted mints, not inferred
odds. Probability filters use 0–1; confidence is `high`, `medium` or `low`.

For `lowest_risk`/`safest`, omitted filters default server-side to
`min_trades_5=3`, `min_liquidity_sol=5` and `include_crashed=false`; the crash
filter excludes both `already_crashed` and `crashed_within_24h` rows. Explicit
values override them, including `min_trades_5=0`, `min_liquidity_sol=0` and
`include_crashed=true`. Watchlists get **none of these defaults**: your crashed
holdings stay visible and flagged unless explicitly filtered out. The offer
binds the client's parameters without these defaults; the server applies them
deterministically when selecting and pricing rows. Answers retain `filters`,
`defaults_applied`, `excluded_by_defaults` (rows removed only by the defaults,
before the limit) and `total_matches`.

The lowest odds are usually the least-traded tokens; the default filters
remove most inactive or recently collapsed tokens, not all.

`crash_odds`, `last_known`, `board` and `coverage` accept optional `chain` and `platform`
strings, comma-separated strings or lists. Currently use `chain="solana"`
or its CAIP-2 ID and `platform="pumpfun"`; `pump` and `pump.fun` are aliases.
Values are case-insensitive; empty, duplicate and unsupported values are
refused before payment. Use `markets()` to check current availability. Data
markets and payment networks are separate choices.

Every result includes “Odds, not guarantees. Not financial advice.” Errors
have MCP `isError: true`, a structured error code and `http_status` when known.
`client_refused` means the client refused locally before any payment was
sent: an invalid argument, an offer that failed a check, a missing or
unreadable key, or a price above `HODLODDS_MAX_PRICE_USDC`. Its `message` is
the client's fixed refusal text, for example “This board costs 0.05 USDC but
HODLODDS_MAX_PRICE_USDC is 0.01; raise it to at least 0.05.” Its `http_status`
is null; nothing was sent or charged. Before 0.2.1 these refusals were
reported as `paid_request_failed`. Recognized public server messages are exposed as `server_error` and
`server_code` (such as `offer_changed`); arbitrary rejected content is not
echoed. As in 0.1.0, `confidence` remains the level string:
`high`, `medium`, `low`, or `missing`. The new `confidence_detail` field
retains the full server object, including `reasons` and its band evidence
(an empty object when absent). The row's `band` evidence also passes through.
Model IDs/hashes, `alert` when supplied, `already_crashed`,
`crashed_within_24h`, `last_crash_as_of` and `note` also pass through. Only undocumented row fields add an extra-fields warning.
Receipts contain public payment details, not signed
authorizations or key contents. Spending is tracked in integer atomic USDC;
displayed USDC amounts are exact decimal strings.

## Configuration

Tool arguments cannot change the wallet, recipient, payment network, endpoint
or spending limits. Set these in the MCP process environment:

| Variable | Default / requirement |
| --- | --- |
| `HODLODDS_BASE_URL` | `https://api.hodlodds.com`; HTTPS, or HTTP on loopback for local tests |
| `HODLODDS_NETWORK` | `devnet`; one of `solana`, `base`, `devnet`, `base-sepolia` |
| `HODLODDS_PAY_TO` | No default. Pin the recipient for the chosen payment network from `/v1/meta`, after independently verifying it. Required on mainnet even in read-only mode, and whenever a key is configured. |
| `HODLODDS_KEYPAIR` | Optional local key-file path. Omit or leave empty for read-only mode. Use an absolute path in desktop configurations. |
| `HODLODDS_MAX_PRICE_USDC` | `0.01` per call, maximum `2.5`; single-token calls retain the SDK's `1` USDC ceiling. Positive plain decimal with at most six fractional digits. |
| `HODLODDS_SESSION_BUDGET_USDC` | Required when a key is set; positive plain decimal with at most six fractional digits. No default budget. Resets on process restart. |

The default per-call cap can buy at most two delivered board rows. A larger
offer is refused before key access with `client_refused`, which names the
amount to set; the cap does not trim an answer down to two coins. Set `0.50` explicitly for up to 100 delivered rows, for example.
The accepted offer amount, not the requested limit, determines the budget debit.

**Read-only mode:** omit the key and session budget. The registered tools are
`status`, `markets`, `coverage` and `meaning`. No key is loaded at startup in
either mode. Key files must be regular, non-symlink files, mode `0600`, at most
4 KiB. Solana uses solana-keygen JSON; Base uses a local hex/JSON private-key
file. Use a dedicated low-balance wallet. Never put key contents in a client
configuration.

## Client configuration

Claude Desktop: merge this read-only setup into `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hodlodds": {
      "command": "uvx",
      "args": ["hodlodds-mcp"]
    }
  }
}
```

Use the executable's absolute path if the desktop app cannot find `uvx`.
To enable payments, add the environment values below to that server entry.
This is also a generic stdio process configuration for clients that accept
`command`, `args` and `env` (adapt the surrounding configuration to your client):

```json
{
  "command": "hodlodds-mcp",
  "args": [],
  "env": {
    "HODLODDS_BASE_URL": "https://api.hodlodds.com",
    "HODLODDS_NETWORK": "solana",
    "HODLODDS_PAY_TO": "REPLACE_WITH_VERIFIED_RECIPIENT_FROM_META",
    "HODLODDS_KEYPAIR": "/absolute/path/to/payer.json",
    "HODLODDS_MAX_PRICE_USDC": "0.01",
    "HODLODDS_SESSION_BUDGET_USDC": "0.10"
  }
}
```

Replace the recipient and file-path placeholders before starting. The example
uses real mainnet USDC. For Base payments, choose `base`, its recipient and an
EVM key file. Test networks require a test API that offers that network:
use `devnet` or `base-sepolia`, test USDC and the matching verified recipient.
These snippets are configuration templates; assistant applications were not
launched during offline package testing.

Restart the MCP process after changing its environment. Reconcile any uncertain
payment first: a restart also resets the session budget.

## Payment safety and reconciliation

Paid calls are serialized across tools. The SDK validates the offer's network,
asset, pinned recipient, query binding and amount before key access or signing.
The session budget checks that exact accepted amount, then reserves it before
the authorized request. A successful receipt retains the debit. Exhausting the
budget refuses the next payment before signing; free tools remain available.

A refusal marked `HodlOdds-Charged: no` is free only when the receipt does not
indicate success or a transaction. A conflicting or malformed receipt remains
uncertain. Confirmed free refusals refund the reservation, return an error and
leave payments enabled.

A board `409` with `code: "offer_changed"` is returned by the current API
only after signing. A signed request is never retried automatically. After
signing, a `409` with `code: "offer_changed"` and `HodlOdds-Charged: no`
(without a conflicting receipt) means nothing was charged, but the client must
call again; the SDK never re-signs on its own. A second automatic authorization
could be settled twice if a server misbehaved. The free refusal releases the
budget reservation; call `board` again for a new offer. As a safeguard only, the
SDK can request a new offer once **before signing only**, with the same query
and repeated price and budget checks.

After an uncertain payment—such as a lost response, missing receipt or
conflicting receipt—**all further paid calls pause**. The reserved amount stays
spent until the operator reconciles it against finalized wallet history and
payment records. The result includes public reconciliation details. There is
no tool to clear the pause, no automatic retry or redemption, and no pending
authorization file is written. Restart only after reconciliation and choose a
new budget that accounts for prior spending.

The budget is local to one process and is not shared or persistent. **Every
restart resets it.** The paid request timeout is 90 seconds. Cancellation does
not abandon in-flight payment accounting. A settled answer with an unexpected
shape returns raw data with a warning instead of authorizing another payment.
Receipts report server settlement; they are not independent proof of finality.

## Meaning and freshness

0.04 means about 4 in 100 tokens like this one lose 90% or more of their price within the next 60 minutes. Higher means riskier, but a low number does not mean safe: smaller or slower drops are not counted, and the odds change minute by minute.

Scores are recomputed every 60 seconds, about 20 seconds after each minute closes, from completed one-minute bars up to `as_of`. Each score covers the 60 minutes after its `as_of` time and is valid only until the next update; the odds for the same token can rise sharply from one minute to the next. Scoring starts around :20; computation and publication can delay availability. For decisions, refresh immediately before acting and once per minute for continuous monitoring, checking `as_of` for a new minute. The API freshness cutoff is separate: by default, data becomes `stale` more than 120 seconds after `issued_at` or after `expires_at`. A new request does not restart the forecast window.

A crash is a completed one-minute close in SOL at least 90% below the reference
price within the displayed 60-minute window. `as_of` and `horizon_end` are UTC
Unix seconds. Confidence describes historical calibration and evidence; it is
not a token-specific interval or guarantee. Missing odds, missing confidence,
stale data and uncovered tokens are not safety signals. Very low odds say
“under 0.1%”, never a rounded zero.

`dd_60` is close divided by the maximum close in the previous 60 minutes,
minus one. Zero is the hour's high. At `-0.90` or lower, `already_crashed`
is true; an older collapse reads false. `crashed_within_24h` and
`last_crash_as_of` say whether an API snapshot saw that threshold within the
last 24 hours and when (UTC Unix seconds). The memory only knows snapshots the
API received: a crash that happened entirely during an API outage is not seen.
Both fields are null while the memory is unavailable. Either flag
adds a `note`, and `summary` carries the same context: the odds then describe a
further 90% drop from the current price, not a recovery or safety signal.
Unknown drawdown gives a null flag. These are past-price facts, not new model
predictions. Below about 2%, historical crash rates have run up to
about twice the predicted value, under one percentage point in absolute
terms; those answers carry low confidence.

Odds, not guarantees. Not financial advice.

## Development

From the [public repository](https://github.com/hodlodds/hodlodds-sdk) root,
with the dependencies and pytest installed:

```sh
python -m pytest -q mcp/tests
python -m build mcp
```

Tests use this checkout's SDK and MCP sources, mocked HTTP and signing, and
an in-memory MCP session. Socket connections and DNS lookups are disabled.
They do not prove live settlement or desktop-client integration. The budget
adapter uses the SDK's watch accounting hook, so the dependency is bounded to
`hodlodds>=0.3.2,<0.4` and tested before upgrades.

`server.json` contains registry metadata for `com.hodlodds/hodlodds-mcp`.
Publishing to PyPI and publishing a registry listing are separate steps;
namespace ownership and registry acceptance must be verified by the publisher.
