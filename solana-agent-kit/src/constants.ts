export const VERSION = "0.1.0";
export const USER_AGENT = `hodlodds-agent-kit/${VERSION}`;
export const DEFAULT_BASE_URL = "https://api.hodlodds.com";

export type NetworkName = "solana" | "devnet";

export interface NetworkInfo {
  readonly name: NetworkName;
  /** CAIP-2 id the API uses in its x402 offers. */
  readonly caip2: string;
  /** USDC mint on that network: the only asset this plugin pays with. */
  readonly usdc: string;
  readonly explorerSuffix: string;
}

export const NETWORKS: Readonly<Record<NetworkName, NetworkInfo>> = {
  solana: {
    name: "solana",
    caip2: "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    usdc: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    explorerSuffix: "",
  },
  devnet: {
    name: "devnet",
    caip2: "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
    usdc: "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
    explorerSuffix: "?cluster=devnet",
  },
};

export const EXPLORER_TX = "https://explorer.solana.com/tx/";

// Programs used by the x402 "exact" Solana payment (the same layout as the official x402 client).
export const TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
export const ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
export const MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr";
export const COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111";
export const USDC_DECIMALS = 6;
export const COMPUTE_UNIT_LIMIT = 20_000;
export const COMPUTE_UNIT_PRICE_MICROLAMPORTS = 1;
/** Seller memos longer than this are refused (the HodlOdds API sends at most 32 bytes). */
export const MAX_MEMO_BYTES = 32;

/** No single answer is ever paid above 1 USDC, whatever the configuration says. */
export const HARD_MAX_ATOMIC = 1_000_000;
/** Highest session budget the plugin accepts (1,000 USDC), to catch a mistyped value. */
export const MAX_BUDGET_ATOMIC = 1_000_000_000;
export const DEFAULT_MAX_PRICE_USDC = "0.01";

export const MAX_PAYMENT_HEADER = 16_384;
export const MAX_SIGNATURE_HEADER = 65_536;
export const MAX_HEADERS_BYTES = 65_536;
export const MAX_BODY_BYTES = 1_048_576;
export const FREE_TIMEOUT_MS = 20_000;
export const PAID_TIMEOUT_MS = 90_000;
/** Leaving a paid request early can mean paying without receiving the answer (the Python SDK's floor). */
export const MIN_PAID_TIMEOUT_MS = 30_000;
/** The wallet's signing time, also capped by the offer's validity, well inside a blockhash's lifetime. */
export const MAX_SIGNING_MS = 30_000;

export const DISCLAIMER = "Odds, not guarantees. Not financial advice.";
// Verbatim from the API's llms.txt (also bundled in hodlodds-mcp).
export const MEANING =
  "0.04 means about 4 in 100 tokens like this one lose 90% or more of their price within the next 60 minutes. " +
  "Higher means riskier. A low number is not a safety signal: smaller or slower drops are not counted, " +
  "and the odds change minute by minute.";
// Read by the agent: it asks for no action beyond reporting (the restart procedure is in the README, for the operator).
export const RECONCILE =
  "Payments are paused. Do not retry this call and do not try to clear the pause. Report these payment details " +
  "to your human operator, who checks the wallet's finalized history and the HodlOdds payment records before paid " +
  "answers resume.";

// The API's own texts (cloud/summary.py, schema.py, last_known.py, air/confidence.py). The plugin rebuilds the summary
// and the crash note from the row's checked fields, and passes confidence reasons only from this list.
export const CRASHED_NOTE =
  "This token already fell 90% or more from its high of the last 60 minutes. " +
  "The odds cover a further 90% drop from the current price; they are not a recovery or safety signal.";
export const CONFIDENCE_RULE = "hodlodds.confidence.v1";
export const CONFIDENCE_REASONS: ReadonlySet<string> = new Set([
  "Historical band evidence is missing or invalid.",
  "Fewer than 100 positive landmarks support the high level.",
  "Absolute calibration error exceeds half the tolerance.",
  "Relative calibration error exceeds 25%.",
  "Fewer than 20 positive landmarks support the medium level.",
  "Absolute calibration error exceeds the tolerance.",
  "Relative calibration error exceeds 50%.",
  "Token age is unknown or invalid; confidence cannot exceed medium.",
  "Token age is below 120 minutes; confidence cannot exceed medium.",
]);
/** Added by the last-known route to a stale answer's reasons (cloud/last_known.py). */
export const STALE_REASON = /^stale data: \d{1,7} min old$/;
export const CHAIN_NAMES: ReadonlySet<string> = new Set(["Solana"]);

// Market vocabulary (the API's /v1/markets stays authoritative when it adds markets).
export const AVAILABLE_CHAINS: Readonly<Record<string, string>> = {
  solana: "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
};
export const AVAILABLE_PLATFORMS: Readonly<Record<string, string>> = {
  pumpfun: "pumpfun",
  pump: "pumpfun",
  "pump.fun": "pumpfun",
};
