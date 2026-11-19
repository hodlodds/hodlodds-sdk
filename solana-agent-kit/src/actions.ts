import type { Action } from "solana-agent-kit";
import { z } from "zod";
import { asPayingAgent } from "./client";
import { MarketOptions, Service } from "./service";

const EXAMPLE_MINT = "TokenMintAddressExamp1e1111111111111111pump";

const marketFields = {
  chain: z.string().optional().describe("Optional chain, e.g. \"solana\" (the default market)"),
  platform: z.string().optional().describe("Optional platform, e.g. \"pumpfun\" (the default market)"),
};

const mintInput = z.object({
  mint: z.string().describe("Mint address of the Solana token (base58)"),
  ...marketFields,
});

function options(input: Record<string, unknown>): MarketOptions {
  const out: MarketOptions = {};
  if (typeof input.chain === "string") {
    out.chain = input.chain;
  }
  if (typeof input.platform === "string") {
    out.platform = input.platform;
  }
  return out;
}

export function createActions(service: () => Service): Action[] {
  return [
    {
      name: "HODLODDS_STATUS",
      similes: ["hodlodds status", "crash odds feed status", "is the hodlodds feed live"],
      description:
        "Free. State of the HodlOdds crash-odds feed (live or not, age of the latest scores, model) and this agent's " +
        "HodlOdds payment settings and spending so far. No payment is made.",
      examples: [[{
        input: {},
        output: { status: "success", state: "live", age_s: 41, payments: { configured: true, spent_usdc: "0.01" } },
        explanation: "The feed is live; one paid answer was bought in this session.",
      }]],
      schema: z.object({}),
      handler: async () => service().status(),
    },
    {
      name: "HODLODDS_MARKETS",
      similes: ["hodlodds markets", "hodlodds prices", "which tokens does hodlodds cover"],
      description:
        "Free. The markets HodlOdds scores (chains, platforms, filter syntax), the model identities and the prices of " +
        "paid answers. No payment is made.",
      examples: [[{
        input: {},
        output: { status: "success", markets: [{ chain: "solana", platform: "pumpfun" }] },
        explanation: "HodlOdds covers pump.fun tokens on Solana.",
      }]],
      schema: z.object({}),
      handler: async () => service().markets(),
    },
    {
      name: "HODLODDS_COVERAGE",
      similes: ["is this token covered by hodlodds", "hodlodds coverage", "can hodlodds score this token"],
      description:
        "Free. Whether HodlOdds currently scores a Solana token (its coverage stage and the reason). Use it before a " +
        "paid answer: an uncovered token has no odds and is never charged. No payment is made.",
      examples: [[{
        input: { mint: EXAMPLE_MINT },
        output: { status: "success", stage: "covered", reason: "This mint has a SOL risk score in the latest snapshot." },
        explanation: "The token is scored now, so its crash odds can be paid for.",
      }]],
      schema: mintInput,
      handler: async (_agent, input) => service().coverage(input.mint, options(input)),
    },
    {
      name: "HODLODDS_CRASH_ODDS",
      similes: [
        "crash odds for a solana token",
        "hodlodds crash risk",
        "pre-trade crash check",
        "chance this token crashes in the next hour",
      ],
      description:
        "Paid, 0.01 USDC from the agent's wallet through x402 (exact USDC on Solana, under the configured price limit " +
        "and session budget). Fresh 60-minute crash odds for one covered Solana token: the probability that its price " +
        "falls 90% or more within the next 60 minutes, with a confidence level, timestamps and the payment receipt. " +
        "Higher means riskier; a low number does not mean the price will hold. Refresh right before acting: the odds " +
        "change minute by minute. Uncovered tokens and refusals are not charged.",
      examples: [[{
        input: { mint: EXAMPLE_MINT },
        output: {
          status: "success",
          mint: EXAMPLE_MINT,
          p_crash_60m: 0.031,
          summary: "3.1% chance of a 90%+ price drop within 60 minutes (about 3.1 in 100). Low odds are not a safety signal.",
          confidence: "medium",
          payment: { amount_usdc: "0.01", success: true },
        },
        explanation: "About 3 in 100 tokens like this one lost 90% or more within an hour.",
      }]],
      schema: mintInput,
      handler: async (agent, input) => service().crashOdds(asPayingAgent(agent), input.mint, options(input)),
    },
    {
      name: "HODLODDS_LAST_KNOWN_ODDS",
      similes: ["last known crash odds", "cheaper hodlodds odds", "recent crash odds for a token"],
      description:
        "Paid, 0.002 USDC through x402. The last-known crash odds for one covered Solana token, at most 30 minutes old, " +
        "labelled with their age and a lower confidence when stale. The forecast window is not refreshed by the request. " +
        "Uncovered tokens and refusals are not charged.",
      examples: [[{
        input: { mint: EXAMPLE_MINT },
        output: { status: "success", p_crash_60m: 0.05, stale: true, age_s: 600, confidence: "low" },
        explanation: "Odds from 10 minutes ago, labelled stale, with confidence lowered.",
      }]],
      schema: mintInput,
      handler: async (agent, input) => service().lastKnownOdds(asPayingAgent(agent), input.mint, options(input)),
    },
  ];
}
