import type { Connection } from "@solana/web3.js";
import { PublicKey } from "@solana/web3.js";
import { ResolvedConfig } from "./config";
import { EXPLORER_TX, MAX_SIGNING_MS } from "./constants";
import { atomicToUsdc, isSignature } from "./encoding";
import { ClientRefusal, HodlOddsError, PaymentRecord } from "./errors";
import { get, HttpAnswer, jsonObject } from "./http";
import { Ledger } from "./ledger";
import {
  buildPayment,
  checkOffer,
  checkSigned,
  decodeHeaderObject,
  decodeSettlement,
  payerTokenAccount,
  paymentSignatureHeader,
  selectOffer,
  Settlement,
} from "./payment";
import { Json } from "./util";

/** The parts of a Solana Agent Kit agent that a paid answer uses. */
export interface PayingAgent {
  wallet: { publicKey: PublicKey; signTransaction<T>(transaction: T): Promise<T> };
  connection: Pick<Connection, "getLatestBlockhash" | "getTokenAccountBalance" | "getAccountInfo">;
}

export function asPayingAgent(agent: unknown): PayingAgent {
  return agent as PayingAgent;
}

export interface Answer {
  data: Json;
  payment: PaymentRecord | null;
}

const RPC_TIMEOUT_MS = 15_000;
/** Time kept between the end of signing and the offer's maxTimeoutSeconds, for the request to reach the server. */
const SEND_MARGIN_MS = 5_000;

class Timeout extends Error {}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  return Promise.race([
    promise,
    new Promise<T>((_, reject) => {
      timer = setTimeout(() => reject(new Timeout("timeout")), Math.max(0, ms));
    }),
  ]).finally(() => clearTimeout(timer));
}

export class HodlOddsClient {
  readonly config: ResolvedConfig;
  readonly ledger: Ledger;

  constructor(config: ResolvedConfig, ledger: Ledger) {
    this.config = config;
    this.ledger = ledger;
  }

  async free(path: string): Promise<Json> {
    const answer = await get(this.config.fetch, this.config.baseUrl + path, this.config.freeTimeoutMs);
    const data = jsonObject(answer);
    if (answer.status !== 200 || !data) {
      throw new HodlOddsError(`free answer HTTP ${answer.status}`, answer.status, data);
    }
    return data;
  }

  /**
   * One paid answer (a port of the HodlOdds Python SDK 0.3.5 `_paid`, single answers only): an unsigned request
   * first; on 402 every offer check, the price limit, the session budget and the payment header before the wallet is
   * asked; the wallet must sign within the offer's validity window and at most MAX_SIGNING_MS; then exactly one
   * authorized request, never retried by the plugin. The caller holds the ledger lock.
   */
  async paid(agent: PayingAgent, path: string): Promise<Answer> {
    const cfg = this.config;
    if (!cfg.payTo || cfg.budgetAtomic === null) {
      throw new ClientRefusal("payments are not configured: set payTo and sessionBudgetUsdc");
    }
    const url = cfg.baseUrl + path;
    const first = await get(cfg.fetch, url, cfg.freeTimeoutMs);
    if (first.status === 200) {
      const data = jsonObject(first);
      if (!data) {
        throw new HodlOddsError("free answer is not a JSON object", 200);
      }
      return { data, payment: null };
    }
    if (first.status !== 402) {
      // Free refusals: not covered (404), feed interrupted (503), too old (410), ...
      throw new HodlOddsError(`free answer HTTP ${first.status} (nothing paid)`, first.status, jsonObject(first));
    }
    const offered = Date.now();
    const offer = decodeHeaderObject(first.headers.get("payment-required"));
    if (!offer) {
      throw new ClientRefusal("402 without a valid PAYMENT-REQUIRED header; nothing paid");
    }
    const row = selectOffer(offer, cfg.network, cfg.payTo);
    const amount = checkOffer(row, cfg.network, cfg.payTo, cfg.maxAtomic);
    this.ledger.checkBudget(amount);
    const deadline = offered + row.maxTimeoutSeconds * 1000 - SEND_MARGIN_MS;

    let payer: PublicKey;
    try {
      payer = new PublicKey(agent.wallet.publicKey.toBase58());
    } catch {
      throw new ClientRefusal("the agent has no usable wallet; nothing signed");
    }
    if (payer.toBase58() === cfg.payTo) {
      throw new ClientRefusal("payer and payTo must be different wallets");
    }
    if (payer.toBase58() === row.extra.feePayer) {
      throw new ClientRefusal("payer must differ from the facilitator fee payer");
    }
    let blockhash: string;
    if (typeof row.extra.recentBlockhash === "string") {
      blockhash = row.extra.recentBlockhash;
    } else {
      try {
        blockhash = (await withTimeout(agent.connection.getLatestBlockhash("confirmed"), RPC_TIMEOUT_MS)).blockhash;
      } catch {
        throw new ClientRefusal("could not read a recent blockhash from the agent's RPC; nothing signed");
      }
    }
    const source = payerTokenAccount(payer, cfg.network);
    let balance: bigint | null = null;
    try {
      const read = await withTimeout(agent.connection.getTokenAccountBalance(source, "confirmed"), RPC_TIMEOUT_MS);
      balance = BigInt(read.value.amount);
    } catch {
      // An RPC error or no USDC account. Only an RPC that answers "no such account" is refused here; anything else is
      // left to the facilitator's simulation, which charges nothing when the transfer cannot succeed.
      let missing = false;
      try {
        missing = (await withTimeout(agent.connection.getAccountInfo(source, "confirmed"), RPC_TIMEOUT_MS)) === null;
      } catch {
        missing = false;
      }
      if (missing) {
        throw new ClientRefusal(`the wallet has no USDC account on ${cfg.network.name}: send USDC to it first; nothing signed`);
      }
    }
    if (balance !== null && balance < BigInt(amount)) {
      throw new ClientRefusal("the wallet's USDC balance is below the price; nothing signed");
    }
    const built = buildPayment(row, payer, source, blockhash);
    const expectedMessage = Uint8Array.from(built.message.serialize());
    paymentSignatureHeader(offer, row, built.serialize()); // same length as the signed transaction: refuses before signing
    // A slow wallet (remote or human-approved) must not use up the blockhash's lifetime of roughly 60 to 90 seconds.
    const window = Math.min(deadline - Date.now(), MAX_SIGNING_MS);
    if (window < 1_000) {
      throw new ClientRefusal("the offer expired before signing; nothing signed");
    }
    let signed: unknown;
    try {
      signed = await withTimeout(agent.wallet.signTransaction(built), window);
    } catch (exc) {
      throw new ClientRefusal(exc instanceof Timeout ? "the wallet did not sign in time; nothing sent" : "the wallet did not sign; nothing sent");
    }
    const header = paymentSignatureHeader(offer, row, checkSigned(expectedMessage, signed, payer));
    if (Date.now() > deadline) {
      throw new ClientRefusal("the offer expired while the wallet was signing; nothing sent");
    }

    const payment: PaymentRecord = {
      amount_usdc: atomicToUsdc(amount),
      network: cfg.network.name,
      success: null,
      transaction: null,
      payer: payer.toBase58(),
      pay_to: cfg.payTo,
      explorer: null,
    };
    // Reserved before transport; an uncertain attempt is never refunded or retried.
    this.ledger.reserve(amount);
    let paid: HttpAnswer | null = null;
    try {
      // Exactly one authorized attempt by the plugin. A lost or failed response does not prove that nothing settled.
      // One exception sits below the plugin: Node's fetch follows the Fetch standard and sends the request once more,
      // on a new connection, when a server answers 421 Misdirected Request (a reply from a server that cannot answer
      // for this host). The second copy carries the same signed transaction, which cannot settle twice.
      paid = await get(cfg.fetch, url, cfg.paidTimeoutMs, { "PAYMENT-SIGNATURE": header });
      const receiptHeader = paid.headers.get("payment-response");
      const info: Settlement | null = receiptHeader ? decodeSettlement(receiptHeader) : null;
      if (paid.status !== 200 && paid.headers.get("hodlodds-charged") === "no" && info?.success !== true && !info?.transaction) {
        payment.charged = false;
        payment.success = false;
        this.ledger.refund(amount);
        throw new HodlOddsError("refused before settlement");
      }
      if (info) {
        if (isSignature(info.transaction)) {
          payment.transaction = info.transaction;
          payment.explorer = EXPLORER_TX + info.transaction + cfg.network.explorerSuffix;
        }
        // Only a 402 carrying a reason is the facilitator declaring failure; anything else leaves the outcome unknown.
        if (info.success === false && !info.transaction && paid.status === 402 && info.errorReason) {
          const reason = /^[a-zA-Z0-9_]{1,100}$/.test(info.errorReason) ? info.errorReason : "unknown";
          payment.success = false;
          throw new HodlOddsError(`the facilitator reported settlement failed: ${reason}; usually nothing was charged`);
        }
        if (info.success !== true || info.network !== cfg.network.caip2 || payment.transaction === null ||
            (info.payer != null && info.payer !== payment.payer) || (info.amount != null && info.amount !== String(amount))) {
          throw new HodlOddsError("invalid or unsuccessful PAYMENT-RESPONSE");
        }
        payment.success = true; // the server's receipt, not an independent proof of finality
      }
      if (paid.status !== 200) {
        throw new HodlOddsError(`authorized request returned HTTP ${paid.status}`);
      }
      if (!info) {
        throw new HodlOddsError("200 without PAYMENT-RESPONSE");
      }
      const data = jsonObject(paid);
      if (!data) {
        throw new HodlOddsError("paid answer is not a JSON object");
      }
      return { data, payment: { ...payment } };
    } catch (exc) {
      const reason = exc instanceof HodlOddsError ? exc.message : "invalid payment response";
      const status = paid ? paid.status : exc instanceof HodlOddsError ? exc.status : null;
      const body = paid ? jsonObject(paid) : null;
      if (payment.charged === false) {
        throw new HodlOddsError(reason, status, body, payment);
      }
      if (payment.success === false) {
        throw new HodlOddsError(
          `${reason}. Not retried. Check ${payment.amount_usdc} USDC on ${payment.network} from ${payment.payer} ` +
            `to ${payment.pay_to} in your wallet history.`, status, body, payment);
      }
      throw new HodlOddsError(
        `${reason}. Settlement may have occurred; not retried. Reconcile ${payment.amount_usdc} USDC on ` +
          `${payment.network} from ${payment.payer} to ${payment.pay_to} against finalized wallet history and the ` +
          `server payment records before another paid call. Transaction: ${payment.transaction}.`, status, body, payment);
    }
  }
}
