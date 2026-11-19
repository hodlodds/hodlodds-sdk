# hodlodds-agent-kit

HodlOdds crash odds as a [Solana Agent Kit](https://github.com/sendaifun/solana-agent-kit) plugin.

[HodlOdds](https://hodlodds.com) publishes 60-minute crash odds for SOL-quoted pump.fun graduates on Solana: the
probability that a token's price falls 90% or more within the next 60 minutes. A crash is a completed one-minute close,
priced in SOL, at least 90% below the reference price within the next 60 minutes.

The plugin gives an agent the free status, market and coverage answers, and paid odds through
[x402](https://www.x402.org/): each answer is paid separately in USDC from the agent's own wallet, with no account and
no API key, under a price limit and a session budget set in the agent's configuration.

## Install

```sh
npm install hodlodds-agent-kit
```

Peer dependencies: `solana-agent-kit` 2.x, `@solana/web3.js` 1.98 or later 1.x, `zod` 3.24 or later 3.x.
Node.js 22 or later (the Node.js runtime: not an Edge runtime or a browser).

## Use

Create the plugin once, at module scope, and give the same instance to every agent:

```ts
import { KeypairWallet, SolanaAgentKit } from "solana-agent-kit";
import { createHodlOddsPlugin } from "hodlodds-agent-kit";

const hodlodds = createHodlOddsPlugin({
  payTo: "<the HodlOdds recipient, see Payments>",
  sessionBudgetUsdc: "0.10",
});

const agent = new SolanaAgentKit(new KeypairWallet(keypair, rpcUrl), rpcUrl, {}).use(hodlodds);

const odds = await agent.methods.hodloddsCrashOdds(agent, "<token mint>");
console.log(odds.summary, odds.payment);
```

`HodlOddsPlugin` is a ready-made instance configured from the environment. Import it by name, also from ES modules:
`import { HodlOddsPlugin } from "hodlodds-agent-kit"` (the package is CommonJS; a default import under native
ES modules gives the whole module).

Without `payTo` and `sessionBudgetUsdc` the free actions work and the paid ones refuse before anything is signed.

## Actions

| Action | Price now | Returns |
|---|---|---|
| `HODLODDS_STATUS` | free | feed state, age of the latest scores, model, and this agent's payment settings and spending |
| `HODLODDS_MARKETS` | free | the markets scored, filter syntax, model identities and prices |
| `HODLODDS_COVERAGE` | free | whether a token is scored now (coverage stage and reason) |
| `HODLODDS_CRASH_ODDS` | 0.01 USDC | fresh 60-minute crash odds for one covered token, with confidence, timestamps and the payment receipt |
| `HODLODDS_LAST_KNOWN_ODDS` | 0.002 USDC | the last-known odds for one token, at most 30 minutes old, labelled with their age |

The prices are the API's current prices, stated in each offer. The plugin pays any offer up to `maxPriceUsdc`.

Methods: `hodloddsStatus`, `hodloddsMarkets`, `hodloddsCoverage`, `hodloddsCrashOdds`, `hodloddsLastKnownOdds`,
`hodloddsMeaning`, `hodloddsSpend`, each called as `agent.methods.<name>(agent, ...)`. `mint` and the optional market
(`{ chain: "solana", platform: "pumpfun" }`) are the arguments where they apply. Every answer has `status`
(`"success"` or `"error"`) and a fixed disclaimer: odds, not guarantees; not financial advice.

Uncovered tokens and refusals are answered without a payment.

## What the agent sees

Paid answers carry the row's computed fields (the probability, its band and confidence, returns, drawdown, trades,
pool reserve, the scored close in SOL, cost inputs, crash flags, market) only with the types the API's schema gives
them. The summary and the crash note are rebuilt from those fields in the API's own wording; a server text that
differs is not shown. The token's name, image and metadata link are written by its creator and are left out on
purpose; the symbol is kept only as a plain ticker. A row for another token than the one asked for is not shown. When
a field cannot be checked, the answer says how many were left out (`warning`). The answer's other parts (its
timestamps, model, coverage and freshness, and the free status, market and coverage answers) are passed as the API
sends them, with only the creator-written fields removed.

## Payments

- The agent's wallet pays exact USDC on Solana mainnet (or devnet for tests) through x402 v2. The transaction is the
  one the official x402 client builds: compute limit, compute price, one USDC `TransferChecked`, a memo. The
  facilitator named in the offer pays the network fee, so the wallet must sign a partially signed v0 transaction and
  return it unchanged: the plugin checks that the message is the one it built, the payer's signature is valid and the
  fee payer's slot is empty.
- `payTo` pins the recipient. Copy it from `https://api.hodlodds.com/v1/meta` (`payments.networks`, the Solana entry's
  `payTo`) and compare it with the address shown on hodlodds.com. An offer for any other recipient, asset, network or
  scheme is refused before signing.
- `maxPriceUsdc` (default 0.01, at most 1) limits one answer. `sessionBudgetUsdc` limits the total paid in this
  process: the spend is shared by every plugin instance with the same base URL, network and `payTo`, whichever wallet
  pays, and each instance compares it with its own budget. A back end that runs agents for several users' wallets
  therefore shares one budget, one pause and one queue among them: one wallet's uncertain payment (which its owner can
  cause, for example by moving the USDC away during settlement) stops paid answers for every wallet until a restart.
  The other wallets' agents are told only that another wallet's payment paused them; the details stay in
  `hodloddsSpend` for the operator. Size the budget for all wallets, or give each wallet its own process.
- The budget and the pause are kept in memory. A restart, another worker process or another serverless invocation
  starts again from zero, so keep only a small USDC balance in a wallet used for nothing else: it is the outer limit.
- A wallet without a USDC account is refused before signing. The wallet has at most 30 seconds to sign.
- Paid calls run one at a time. The amount is reserved before the request is sent and released only when the server
  states that nothing was charged.
- One authorized request per answer, never retried by the plugin. (Node's fetch, following the Fetch standard, sends a
  request once more on a new connection when a server answers 421 Misdirected Request; the same signed transaction
  cannot settle twice.) The authorization is not kept, so an answer lost after settlement cannot be fetched again.
- If the outcome is anything other than a stated "nothing charged" (a timeout, an error after the authorization, a
  missing or inconsistent receipt, a settled answer the plugin cannot show), paid calls pause for the rest of the
  process. The answer has `error: "payments_paused"`, a `reason` and the payment details: payer, recipient, amount, and
  the transaction when known.

### After a pause

The agent is told to report and not to retry. The operator then checks the payer's finalized history for a USDC
transfer of that amount to `payTo` (the transaction in the receipt when there is one), compares it with the HodlOdds
payment records, and only then restarts the process. A restart starts a new budget.

## Configuration

| Option | Environment | Default |
|---|---|---|
| `baseUrl` | `HODLODDS_BASE_URL` | `https://api.hodlodds.com` |
| `network` | `HODLODDS_NETWORK` | `solana` (`devnet` for tests) |
| `payTo` | `HODLODDS_PAY_TO` | none: paid actions refuse |
| `maxPriceUsdc` | `HODLODDS_MAX_PRICE_USDC` | `0.01` |
| `sessionBudgetUsdc` | `HODLODDS_SESSION_BUDGET_USDC` | none: paid actions refuse |
| `freeTimeoutMs` (1000 to 300000) | | 20000 |
| `paidTimeoutMs` (30000 to 300000) | | 90000 |

Options take precedence over the environment. An invalid value throws when the agent loads the plugin.

## Reading the odds

0.04 means about 4 in 100 tokens like this one lose 90% or more of their price within the next 60 minutes. Higher means
riskier. A low number does not mean the price will hold: smaller or slower drops are not counted, and the odds change
minute by minute, so refresh right before acting. `confidence` describes how closely historical predictions in that
range matched what happened.

## License

MIT
