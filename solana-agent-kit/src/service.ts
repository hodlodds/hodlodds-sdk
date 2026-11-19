import { Answer, HodlOddsClient, PayingAgent } from "./client";
import { ResolvedConfig } from "./config";
import {
  AVAILABLE_CHAINS,
  AVAILABLE_PLATFORMS,
  CHAIN_NAMES,
  CONFIDENCE_REASONS,
  CONFIDENCE_RULE,
  CRASHED_NOTE,
  DISCLAIMER,
  HARD_MAX_ATOMIC,
  MEANING,
  RECONCILE,
  STALE_REASON,
} from "./constants";
import { atomicToUsdc, isAddress } from "./encoding";
import { BudgetExceeded, ClientRefusal, HodlOddsError, PaymentRecord, PriceLimitError } from "./errors";
import { Ledger, ledgerKey, PauseReason, receipt } from "./ledger";
import { SERVER_ERRORS, SERVER_ERROR_TEMPLATES } from "./serverMessages";
import { isObject, Json, lookup, scrub } from "./util";

export interface MarketOptions {
  chain?: string | string[];
  platform?: string | string[];
}

/** Every answer: status "success" or "error" (as Solana Agent Kit's executeAction reports them) and the disclaimer. */
export function result(fields: Json): Json {
  return { ...fields, status: "error" in fields ? "error" : "success", disclaimer: DISCLAIMER };
}

/** "?chain=...&platform=..." for the API, validated and normalized as the Python SDK does. */
export function marketQuery(options: MarketOptions = {}): string {
  const chains: Record<string, string> = { ...AVAILABLE_CHAINS };
  for (const value of Object.values(AVAILABLE_CHAINS)) {
    chains[value.toLowerCase()] = value;
  }
  const params = new URLSearchParams();
  for (const [name, raw, allowed] of [["chain", options.chain, chains], ["platform", options.platform, AVAILABLE_PLATFORMS]] as const) {
    if (raw === undefined || raw === null) {
      continue;
    }
    const parts = typeof raw === "string" ? raw.split(",") : raw;
    if (!Array.isArray(parts) || parts.length < 1 || parts.length > 10 || parts.some((p) => typeof p !== "string" || !p.trim())) {
      throw new ClientRefusal(`${name} must contain 1..10 non-empty values`);
    }
    const values = parts.map((p) => lookup(allowed, p.trim().toLowerCase()));
    if (values.some((v) => v === undefined)) {
      throw new ClientRefusal(`${name} not available; see /v1/markets`);
    }
    if (new Set(values).size !== values.length) {
      throw new ClientRefusal(`duplicate ${name} value`);
    }
    params.set(name, (values as string[]).sort().join(","));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

function checkedMint(mint: unknown): string {
  if (!isAddress(mint)) {
    throw new ClientRefusal("mint must be a Solana address");
  }
  return mint;
}

function mintPath(prefix: string, mint: unknown, suffix: string, options?: MarketOptions): string {
  return `${prefix}${checkedMint(mint)}${suffix}${marketQuery(options)}`;
}

/** Python round(x, 1): the exact binary value rounded to one decimal, ties to even. */
function roundOneDecimal(x: number): number {
  const exact = x.toFixed(100);
  const [whole, fraction = ""] = exact.split(".");
  const first = Number(fraction[0] ?? "0");
  const rest = fraction.slice(1);
  let tenths = Number(whole) * 10 + first;
  const half = "5".padEnd(rest.length, "0");
  if (rest > half || (rest === half && tenths % 2 === 1)) {
    tenths += 1;
  }
  return tenths / 10;
}

/** The odds and frequency words of the API's summary for p (cloud/summary.py; also hodlodds-mcp). */
function summaryParts(p: number): { odds: string; frequency: string; low: boolean } {
  const percent = p * 100;
  const halfUp = (value: number) => Math.floor(value + 0.5);
  if (percent < 0.1) {
    return { odds: "under 0.1%", frequency: "fewer than 1 in 1,000", low: true };
  }
  if (percent > 99) {
    return { odds: "over 99%", frequency: "more than 99 in 100", low: false };
  }
  const shown = roundOneDecimal(percent);
  if (shown < 10) {
    return { odds: `${shown}%`, frequency: shown < 1 ? `about ${halfUp(p * 1000)} in 1,000` : `about ${shown} in 100`, low: true };
  }
  return { odds: `${halfUp(percent)}%`, frequency: `about ${halfUp(percent)} in 100`, low: false };
}

/** The same fresh-row wording and rounding as the API's own summary (and hodlodds-mcp). */
export function summary(p: number): string {
  const { odds, frequency, low } = summaryParts(p);
  const tail = low ? "Low odds are not a safety signal." : "Odds, not guarantees.";
  return `${odds} chance of a 90%+ price drop within 60 minutes (${frequency}). ${tail}`;
}

type Check = (value: unknown) => boolean;
const isNumber: Check = (v) => typeof v === "number" && Number.isFinite(v);
const atLeastZero: Check = (v) => isNumber(v) && (v as number) >= 0;
const aboveZero: Check = (v) => isNumber(v) && (v as number) > 0;
const probability: Check = (v) => isNumber(v) && (v as number) >= 0 && (v as number) <= 1;
const isBoolean: Check = (v) => typeof v === "boolean";
const epoch: Check = (v) => Number.isSafeInteger(v) && (v as number) >= 0;
const orNull = (check: Check): Check => (v) => v === null || check(v);
const word = (allowed: ReadonlySet<string>): Check => (v) => typeof v === "string" && allowed.has(v);
const PLATFORMS: ReadonlySet<string> = new Set(Object.values(AVAILABLE_PLATFORMS));

function recentCrashNote(asOf: number): string {
  const seen = new Date(asOf * 1000).toISOString().slice(0, 19).replace("T", " ");
  return `This token fell 90% or more from a recent high within the last 24 hours (last seen ${seen} UTC); ` +
    "low odds now are not a safety signal.";
}

/**
 * The API's summary and crash note for a row (cloud/summary.py response_row), rebuilt from the row's checked fields:
 * the "Last known (N min old): " prefix only on the last-known route, the crash note and "a further 90%+ drop" only
 * with the row's crash flags. The agent sees this text; a server text that differs is counted, not shown.
 */
export function rowTexts(p: number, crashed: unknown, recent: unknown, lastCrash: unknown, lastAgeS: number | null):
    { summary: string; note: string | null } {
  const { odds, frequency, low } = summaryParts(p);
  const prefix = lastAgeS === null ? "" : `Last known (${Math.floor(lastAgeS / 60)} min old): `;
  let note: string | null = null;
  if (crashed === true) {
    note = CRASHED_NOTE;
  } else if (recent === true && epoch(lastCrash)) {
    note = recentCrashNote(lastCrash as number);
  }
  let tail: string;
  if (note !== null) {
    tail = p < 0.1 ? note : crashed === true ? "It already fell 90%+ from its trailing-hour high."
      : "It already fell 90%+ from a recent high within the last 24 hours.";
  } else {
    tail = low ? "Low odds are not a safety signal." : "Odds, not guarantees.";
  }
  const event = note !== null ? "a further 90%+ drop" : "a 90%+ price drop";
  return { summary: `${prefix}${odds} chance of ${event} within 60 minutes (${frequency}). ${tail}`, note };
}

// Row fields passed to the agent, each only with the type the API's schema gives it (cloud/schema.py, the live
// /openapi.json RiskRow): numbers, booleans and known words. The summary and the note are rebuilt (rowTexts). The
// token's name, image and metadata link (written by its creator) are left out and the symbol is kept only as a plain
// ticker. A field or value that fails its check, or that this version does not know, is counted in a warning.
const ROW_CHECKS: Readonly<Record<string, Check>> = {
  resq_sol: orNull(atLeastZero),
  age_min: orNull(atLeastZero),
  ret_5: orNull(isNumber),
  ret_60: orNull(isNumber),
  dd_60: orNull(isNumber),
  trades_5: orNull(atLeastZero),
  close_sol: orNull(aboveZero),
  alert: orNull(isBoolean),
  already_crashed: orNull(isBoolean),
  crashed_within_24h: orNull(isBoolean),
  last_crash_as_of: orNull(epoch),
  chain: (v) => typeof v === "string" && /^[-a-z0-9]{3,8}:[-_a-zA-Z0-9]{1,32}$/.test(v),
  chain_name: word(CHAIN_NAMES),
  platform: word(PLATFORMS),
  market_status: word(new Set(["available", "experimental"])),
};
const ROW_HANDLED = new Set(["mint", "p_crash_60m", "kind", "band", "asset", "cost", "summary", "note", "confidence", "symbol"]);
const TICKER = /^[A-Za-z0-9]{1,12}$/;
const ROW_NOT_SHOWN = new Set(["name", "image_uri", "metadata_uri"]);
const BAND_CHECKS: Readonly<Record<string, Check>> = {
  lo: probability, hi: probability, historical_observed: orNull(probability), n: orNull(atLeastZero),
};
const COST_CHECKS: Readonly<Record<string, Check>> = {
  fee_rate: (v) => isNumber(v) && (v as number) >= 0 && (v as number) < 1, reserve_min_5: aboveZero, reserve_max_5: aboveZero,
};
const CONFIDENCE_BAND_CHECKS: Readonly<Record<string, Check>> = {
  lo: probability, hi: probability, n: orNull(atLeastZero), pred: orNull(probability), obs: orNull(probability),
  positives: orNull(atLeastZero),
};
const CONFIDENCE_CHECKS: Readonly<Record<string, Check>> = {
  rule_version: (v) => v === CONFIDENCE_RULE,
  level: word(new Set(["high", "medium", "low"])),
  direction: (v) => v === null || v === "historically under-predicted" || v === "historically over-predicted",
};
const LEVELS: ReadonlySet<string> = new Set(["high", "medium", "low"]);

interface Count {
  n: number;
}

/** The fields of an object that pass their checks; `dropped` names known texts left out without counting them. */
function checked(value: unknown, checks: Readonly<Record<string, Check>>, dropped: ReadonlySet<string>, count: Count): Json | null {
  if (!isObject(value)) {
    count.n += 1;
    return null;
  }
  const out: Json = {};
  for (const [key, field] of Object.entries(value)) {
    const check = lookup(checks, key);
    if (check !== undefined && check(field)) {
      out[key] = field;
    } else if (!dropped.has(key)) {
      count.n += 1;
    }
  }
  return out;
}

const NONE: ReadonlySet<string> = new Set();
const BASIS: ReadonlySet<string> = new Set(["basis"]);
const CONFIDENCE_SPECIAL: ReadonlySet<string> = new Set(["basis", "band", "reasons"]);

function confidenceDetail(value: unknown, count: Count): Json {
  if (value === undefined || value === null) {
    return {};
  }
  const out = checked(value, CONFIDENCE_CHECKS, CONFIDENCE_SPECIAL, count);
  if (out === null || !isObject(value)) {
    return {};
  }
  if ("band" in value) {
    const band = checked(value.band, CONFIDENCE_BAND_CHECKS, NONE, count);
    if (band !== null) {
      out.band = band;
    }
  }
  if ("reasons" in value) {
    const reasons = Array.isArray(value.reasons) ? value.reasons : [];
    const known = reasons.filter((r): r is string => typeof r === "string" && (CONFIDENCE_REASONS.has(r) || STALE_REASON.test(r)));
    count.n += Array.isArray(value.reasons) ? reasons.length - known.length : 1;
    out.reasons = known;
  }
  return out;
}

/**
 * The paid row for `mint`, checked field by field; throws when it is not a SOL probability row for that mint.
 * lastAgeS: the answer's age in seconds on the last-known route, null on the fresh route.
 */
export function oddsRow(row: unknown, mint: string, lastAgeS: number | null = null): Json {
  if (!isObject(row)) {
    throw new Error("invalid probability row");
  }
  const p = row.p_crash_60m;
  if (!probability(p) || (row.kind ?? "sol") !== "sol" || "experimental_score" in row) {
    throw new Error("invalid probability row");
  }
  if (row.mint !== mint) {
    throw new Error("the row is for another token");
  }
  const count: Count = { n: 0 };
  const out: Json = { mint };
  if ("kind" in row) {
    out.kind = "sol";
  }
  for (const [key, value] of Object.entries(row)) {
    if (ROW_HANDLED.has(key) || ROW_NOT_SHOWN.has(key)) {
      continue;
    }
    const check = lookup(ROW_CHECKS, key);
    if (check !== undefined && check(value)) {
      out[key] = value;
    } else {
      count.n += 1;
    }
  }
  // The symbol is creator-written: shown only as a plain ticker, otherwise left out without a warning, as the name is.
  if (typeof row.symbol === "string" && TICKER.test(row.symbol)) {
    out.symbol = row.symbol;
  }
  if ("band" in row) {
    const band = checked(row.band, BAND_CHECKS, BASIS, count);
    if (band !== null) {
      out.band = band;
    }
  }
  if ("cost" in row && row.cost !== null) {
    const cost = checked(row.cost, COST_CHECKS, NONE, count);
    if (cost !== null && Object.keys(cost).length === 3 && (cost.reserve_min_5 as number) <= (cost.reserve_max_5 as number)) {
      out.cost = cost;
    } else if (cost !== null) {
      count.n += 1;
    }
  }
  if ("asset" in row) {
    if (typeof out.chain === "string" && row.asset === `${out.chain}/token:${mint}`) {
      out.asset = row.asset;
    } else {
      count.n += 1;
    }
  }
  const probabilityValue = p as number;
  const texts = rowTexts(probabilityValue, out.already_crashed, out.crashed_within_24h, out.last_crash_as_of, lastAgeS);
  if (row.summary !== undefined && row.summary !== texts.summary) {
    count.n += 1;
  }
  if (row.note !== undefined && row.note !== texts.note) {
    count.n += 1;
  }
  const rule = isObject(row.confidence) && row.confidence.rule_version === CONFIDENCE_RULE;
  const level = rule && isObject(row.confidence) ? row.confidence.level : undefined;
  Object.assign(out, {
    p_crash_60m: probabilityValue,
    odds_percent: probabilityValue * 100,
    summary: texts.summary,
    confidence: typeof level === "string" && LEVELS.has(level) ? level : "missing",
    confidence_detail: confidenceDetail(row.confidence, count),
  });
  if (texts.note !== null) {
    out.note = texts.note;
  }
  const shown = scrub(out) as Json;
  if (count.n > 0) {
    shown.warning = `The source row has ${count.n} field(s) or value(s) this plugin version does not know or could not ` +
      "check; they are not shown.";
  }
  return shown;
}

export function errorDetails(exc: unknown): Json {
  if (!(exc instanceof HodlOddsError)) {
    return { http_status: null };
  }
  const out: Json = { http_status: exc.status };
  const body = isObject(exc.body) ? exc.body : {};
  if (body.code === "offer_changed") {
    out.server_code = "offer_changed";
  }
  const age = body.last_known_age_s;
  if (typeof age === "number" && Number.isInteger(age) && age >= 0) {
    out.last_known_available = true;
    out.last_known_age_s = age;
  }
  const message = body.error;
  if (typeof message === "string") {
    if (SERVER_ERRORS.has(message)) {
      out.server_error = message;
    } else {
      const prefix = SERVER_ERROR_TEMPLATES.find((t) => message.startsWith(t));
      if (prefix !== undefined) {
        out.server_error = prefix.replace(/: $/, "").replace(/\. Valid values$/, "");
      }
    }
  }
  return out;
}

/** Fixed text for a refusal made before signing; null for anything else. */
export function clientRefusal(exc: unknown, maxAtomic: number): string | null {
  if (exc instanceof PriceLimitError) {
    const { priceAtomic: price, limitAtomic: limit } = exc;
    if (limit === maxAtomic && price <= HARD_MAX_ATOMIC) {
      return `This answer costs ${atomicToUsdc(price)} USDC but maxPriceUsdc is ${atomicToUsdc(limit)}; raise it to at least ${atomicToUsdc(price)}.`;
    }
    return `This answer costs more than the client hard cap of ${atomicToUsdc(HARD_MAX_ATOMIC)} USDC; nothing was signed.`;
  }
  return exc instanceof ClientRefusal ? exc.message : null;
}

function walletAddress(agent: PayingAgent): string | null {
  try {
    return agent.wallet.publicKey.toBase58();
  } catch {
    return null;
  }
}

const LEDGER_UNUSABLE = "This process's HodlOdds spending record cannot be used (another value holds its global slot, " +
  "or the global object cannot take it); paid actions refuse. Nothing was signed.";

const COMMON_ALWAYS = ["as_of", "horizon_end", "issued_at", "expires_at"];
const COMMON_IF_PRESENT = [
  "schema", "covered", "definition", "coverage", "model", "model_id", "model_hashes", "stale", "age_s", "freshness",
  "horizon_remaining_s", "price_usdc",
];

export class Service {
  readonly config: ResolvedConfig;
  readonly ledger: Ledger;
  readonly client: HodlOddsClient;

  constructor(config: ResolvedConfig) {
    this.config = config;
    this.ledger = new Ledger(config.budgetAtomic, ledgerKey(config.baseUrl, config.network.caip2, config.payTo));
    this.client = new HodlOddsClient(config, this.ledger);
  }

  private async free(operation: () => Promise<Json>): Promise<Json> {
    try {
      return result(scrub(await operation()) as Json);
    } catch (exc) {
      const refusal = clientRefusal(exc, this.config.maxAtomic);
      if (refusal !== null) {
        return result({ error: "client_refused", message: refusal,
          detail: "Refused locally before any request was sent; nothing was signed.", http_status: null });
      }
      return result({ error: "free_request_failed", message: "Free request failed; nothing signed.", ...errorDetails(exc) });
    }
  }

  status(): Promise<Json> {
    return this.free(async () => ({ ...(await this.client.free("/v1/status")), payments: this.paymentState() }));
  }

  markets(): Promise<Json> {
    return this.free(() => this.client.free("/v1/markets"));
  }

  coverage(mint: unknown, options?: MarketOptions): Promise<Json> {
    return this.free(() => this.client.free(mintPath("/v1/coverage/", mint, "", options)));
  }

  meaning(): Json {
    return result({ meaning: MEANING });
  }

  spend(): Json {
    return result({ ...this.paymentState(), paused: this.ledger.paused, pause_reason: this.ledger.pauseReason });
  }

  private paymentState(): Json {
    return {
      configured: this.config.payTo !== null && this.config.budgetAtomic !== null,
      network: this.config.network.name,
      pay_to: this.config.payTo,
      max_price_usdc: atomicToUsdc(this.config.maxAtomic),
      ...this.ledger.fields(),
    };
  }

  crashOdds(agent: PayingAgent, mint: unknown, options?: MarketOptions): Promise<Json> {
    return this.paid(agent, mint, false, () => mintPath("/v1/risk/", mint, "", options));
  }

  lastKnownOdds(agent: PayingAgent, mint: unknown, options?: MarketOptions): Promise<Json> {
    return this.paid(agent, mint, true, () => mintPath("/v1/risk/", mint, "/last", options));
  }

  private paid(agent: PayingAgent, mint: unknown, last: boolean, path: () => string): Promise<Json> {
    if (!this.ledger.usable) {
      return Promise.resolve(result({ error: "client_refused", message: LEDGER_UNUSABLE, http_status: null, payment: null }));
    }
    return this.ledger.exclusive(async () => {
      const paused = this.ledger.paused;
      if (paused !== null) {
        // Another wallet's payment details stay with the operator (hodloddsSpend); this agent learns only the pause.
        const own = walletAddress(agent) === paused.payer;
        return result({ error: "payments_paused", message: RECONCILE, reason: this.ledger.pauseReason,
          payment: own ? paused : null, ...(own ? {} : { paused_by: "another wallet" }), ...this.ledger.fields() });
      }
      let answer: Answer | null = null;
      try {
        const route = path();
        answer = await this.client.paid(agent, route);
        const data = answer.data;
        const common: Json = {};
        for (const key of COMMON_ALWAYS) {
          common[key] = data[key] ?? null;
        }
        for (const key of COMMON_IF_PRESENT) {
          if (key in data) {
            common[key] = data[key];
          }
        }
        if (last && !atLeastZero(data.age_s)) {
          throw new Error("a last-known answer without its age"); // shown as fresh otherwise: unusable
        }
        const lastAgeS = last ? (data.age_s as number) : null;
        return result({ ...oddsRow(data.row, checkedMint(mint), lastAgeS), ...(scrub(common) as Json),
          payment: receipt(answer.payment), ...this.ledger.fields() });
      } catch (exc) {
        if (exc instanceof BudgetExceeded) {
          return result({ error: "budget_exhausted", message: "Accepted offer exceeds remaining session budget; nothing signed.",
            ...this.ledger.fields() });
        }
        let payment: PaymentRecord | null = exc instanceof HodlOddsError ? exc.payment : null;
        if (payment === null && answer !== null) {
          payment = answer.payment;
        }
        // The raw answer is never passed on (it may carry creator-written text); only its plain top-level field names.
        const keys = answer !== null ? Object.keys(answer.data).filter((k) => /^[a-z0-9_]{1,40}$/.test(k)).slice(0, 30) : [];
        if (answer !== null && payment === null) {
          return result({ error: "unexpected_answer", warning: "The answer has an unexpected shape and is not shown; nothing was paid.",
            data_keys: keys, payment: null, ...this.ledger.fields() });
        }
        if (payment !== null && payment.charged !== false) {
          // Every outcome other than a stated "charged: no" stops paid calls for this key in the process. A
          // facilitator-reported failure usually means nothing was charged, but the plugin does not take it as proof:
          // the amount stays reserved (as hodlodds-mcp does). A settled payment whose answer cannot be shown pauses too,
          // so an agent that retries does not pay again for answers it cannot use.
          const reason: PauseReason = payment.success === true ? "settled_answer_unusable"
            : payment.success === false ? "facilitator_reported_failure" : "outcome_unknown";
          this.ledger.pause(receipt(payment), reason);
          const paused: Json = { error: "payments_paused", message: RECONCILE, reason, payment: this.ledger.paused,
            ...errorDetails(exc), ...this.ledger.fields() };
          if (answer !== null) {
            paused.warning = "Payment settled; the answer has an unexpected shape and is not shown.";
            paused.data_keys = keys;
          }
          return result(paused);
        }
        const refusal = answer === null ? clientRefusal(exc, this.config.maxAtomic) : null;
        if (refusal !== null) {
          return result({ error: "client_refused", message: refusal,
            detail: "Refused locally before any payment was sent; nothing was sent or charged.", http_status: null, payment: null,
            ...this.ledger.fields() });
        }
        return result({ error: "paid_request_failed", message: "Request refused or unavailable; no payment charged.",
          ...errorDetails(exc), payment: receipt(payment), ...this.ledger.fields() });
      }
    });
  }
}
