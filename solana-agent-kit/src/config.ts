import {
  DEFAULT_BASE_URL,
  DEFAULT_MAX_PRICE_USDC,
  FREE_TIMEOUT_MS,
  HARD_MAX_ATOMIC,
  MAX_BUDGET_ATOMIC,
  MIN_PAID_TIMEOUT_MS,
  NETWORKS,
  NetworkInfo,
  PAID_TIMEOUT_MS,
} from "./constants";
import { isAddress, usdcToAtomic } from "./encoding";
import { lookup } from "./util";

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface HodlOddsPluginOptions {
  /** API base URL. Default https://api.hodlodds.com (env HODLODDS_BASE_URL). */
  baseUrl?: string;
  /** "solana" (mainnet, the default) or "devnet" (env HODLODDS_NETWORK). */
  network?: string;
  /**
   * The HodlOdds USDC recipient, pinned (env HODLODDS_PAY_TO). Copy it from https://api.hodlodds.com/v1/meta
   * (payments.networks, payTo) and check it on hodlodds.com. Paid actions refuse without it.
   */
  payTo?: string;
  /** Highest price for one answer, in USDC. Default 0.01, at most 1 (env HODLODDS_MAX_PRICE_USDC). */
  maxPriceUsdc?: string | number;
  /**
   * Total USDC that paid calls may spend in this process (env HODLODDS_SESSION_BUDGET_USDC). The spend is shared by
   * every plugin instance in the process with the same base URL, network and payTo, whichever wallet pays, and each
   * instance compares that shared spend with its own budget. It is kept in memory: a restart, another worker process or another serverless
   * invocation starts again at zero, so the wallet's balance is the outer limit. Paid actions refuse without it.
   */
  sessionBudgetUsdc?: string | number;
  /** Free request timeout in milliseconds, 1000 to 300000 (default 20000). */
  freeTimeoutMs?: number;
  /** Paid request timeout in milliseconds, 30000 to 300000 (default 90000): leaving early can mean paying for nothing. */
  paidTimeoutMs?: number;
  /** Replaces global fetch (tests, proxies). */
  fetch?: FetchLike;
  /** Environment to read (default process.env). */
  env?: Record<string, string | undefined>;
}

export interface ResolvedConfig {
  baseUrl: string;
  network: NetworkInfo;
  payTo: string | null;
  maxAtomic: number;
  budgetAtomic: number | null;
  freeTimeoutMs: number;
  paidTimeoutMs: number;
  fetch: FetchLike;
}

function pick(option: unknown, envValue: string | undefined): unknown {
  if (option !== undefined && option !== null && option !== "") {
    return option;
  }
  return envValue !== undefined && envValue !== "" ? envValue : undefined;
}

function checkBaseUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("baseUrl must be an absolute URL");
  }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (!(url.protocol === "https:" || (url.protocol === "http:" && local))) {
    throw new Error("baseUrl must use https (http only for localhost)");
  }
  if (url.username || url.password || url.search || url.hash || value.includes("\\")) {
    throw new Error("baseUrl must not carry credentials, a query or a fragment");
  }
  return url.origin + url.pathname.replace(/\/+$/, "");
}

function checkTimeout(value: unknown, fallback: number, name: string, minimum: number): number {
  if (value === undefined) {
    return fallback;
  }
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum || value > 300_000) {
    throw new Error(`${name} must be an integer between ${minimum} and 300000`);
  }
  return value;
}

/** Options first, then the environment; throws on an invalid value (nothing is ever guessed). */
export function resolveConfig(options: HodlOddsPluginOptions = {}): ResolvedConfig {
  const env = options.env ?? (typeof process !== "undefined" ? process.env : {});
  const base = String(pick(options.baseUrl, env.HODLODDS_BASE_URL) ?? DEFAULT_BASE_URL);
  const networkName = String(pick(options.network, env.HODLODDS_NETWORK) ?? "solana").trim().toLowerCase();
  const network = networkName === "mainnet" ? NETWORKS.solana : lookup(NETWORKS, networkName);
  if (!network) {
    throw new Error("network must be solana (mainnet) or devnet");
  }
  const payToRaw = pick(options.payTo, env.HODLODDS_PAY_TO);
  if (payToRaw !== undefined && !isAddress(payToRaw)) {
    throw new Error("payTo must be a Solana address");
  }
  const maxAtomic = usdcToAtomic(pick(options.maxPriceUsdc, env.HODLODDS_MAX_PRICE_USDC) ?? DEFAULT_MAX_PRICE_USDC);
  if (maxAtomic === null || maxAtomic > HARD_MAX_ATOMIC) {
    throw new Error("maxPriceUsdc must be above 0 and at most 1 USDC, with at most 6 decimals");
  }
  const budgetRaw = pick(options.sessionBudgetUsdc, env.HODLODDS_SESSION_BUDGET_USDC);
  let budgetAtomic: number | null = null;
  if (budgetRaw !== undefined) {
    budgetAtomic = usdcToAtomic(budgetRaw);
    if (budgetAtomic === null || budgetAtomic > MAX_BUDGET_ATOMIC) {
      throw new Error("sessionBudgetUsdc must be above 0 and at most 1000 USDC, with at most 6 decimals");
    }
  }
  const fetchFn = options.fetch ?? (typeof fetch === "function" ? fetch.bind(globalThis) : undefined);
  if (!fetchFn) {
    throw new Error("global fetch is not available; use Node 22 or later, or pass options.fetch");
  }
  return {
    baseUrl: checkBaseUrl(base),
    network,
    payTo: (payToRaw as string | undefined) ?? null,
    maxAtomic,
    budgetAtomic,
    freeTimeoutMs: checkTimeout(options.freeTimeoutMs, FREE_TIMEOUT_MS, "freeTimeoutMs", 1_000),
    paidTimeoutMs: checkTimeout(options.paidTimeoutMs, PAID_TIMEOUT_MS, "paidTimeoutMs", MIN_PAID_TIMEOUT_MS),
    fetch: fetchFn as FetchLike,
  };
}
