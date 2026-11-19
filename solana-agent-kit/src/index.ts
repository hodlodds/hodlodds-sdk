import type { Plugin, SolanaAgentKit } from "solana-agent-kit";
import { createActions } from "./actions";
import { asPayingAgent } from "./client";
import { HodlOddsPluginOptions, resolveConfig } from "./config";
import { MarketOptions, Service } from "./service";

export type { HodlOddsPluginOptions } from "./config";
export type { MarketOptions } from "./service";
export type { PaymentRecord } from "./errors";
export { VERSION, DISCLAIMER, MEANING } from "./constants";

/**
 * The HodlOdds plugin for Solana Agent Kit.
 *
 * Free actions work without configuration. Paid actions need a pinned recipient and a session budget, from the
 * options or the environment (HODLODDS_PAY_TO, HODLODDS_SESSION_BUDGET_USDC); the agent's own wallet pays in USDC.
 * The configuration is checked when the agent loads the plugin: an invalid value throws, nothing is guessed.
 * Spending and the pause after an uncertain payment are shared by every instance in the process with the same base
 * URL, network and payTo, so creating the plugin per request neither resets the budget nor forgets a pause.
 */
export function createHodlOddsPlugin(options: HodlOddsPluginOptions = {}) {
  let service: Service | null = null;
  const current = (): Service => {
    if (service === null) {
      service = new Service(resolveConfig(options));
    }
    return service;
  };
  const methods = {
    hodloddsStatus: (_agent: SolanaAgentKit) => current().status(),
    hodloddsMarkets: (_agent: SolanaAgentKit) => current().markets(),
    hodloddsCoverage: (_agent: SolanaAgentKit, mint: string, market?: MarketOptions) => current().coverage(mint, market),
    hodloddsCrashOdds: (agent: SolanaAgentKit, mint: string, market?: MarketOptions) =>
      current().crashOdds(asPayingAgent(agent), mint, market),
    hodloddsLastKnownOdds: (agent: SolanaAgentKit, mint: string, market?: MarketOptions) =>
      current().lastKnownOdds(asPayingAgent(agent), mint, market),
    hodloddsMeaning: (_agent: SolanaAgentKit) => current().meaning(),
    hodloddsSpend: (_agent: SolanaAgentKit) => current().spend(),
  };
  const plugin = {
    name: "hodlodds",
    methods,
    actions: createActions(current),
    initialize(_agent: SolanaAgentKit): void {
      current();
    },
  } satisfies Plugin;
  return plugin;
}

/**
 * Ready-made instance: free actions, plus paid ones when HODLODDS_PAY_TO and HODLODDS_SESSION_BUDGET_USDC are set.
 * Import it by name (`import { HodlOddsPlugin } from "hodlodds-agent-kit"`): the package is CommonJS, and under
 * native ES modules a default import gives the whole module, not the plugin.
 */
export const HodlOddsPlugin = createHodlOddsPlugin();
export default HodlOddsPlugin;
