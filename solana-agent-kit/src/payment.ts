import { createPublicKey, randomBytes, verify } from "crypto";
import { MessageV0, PublicKey, VersionedTransaction } from "@solana/web3.js";
import {
  ASSOCIATED_TOKEN_PROGRAM,
  COMPUTE_BUDGET_PROGRAM,
  COMPUTE_UNIT_LIMIT,
  COMPUTE_UNIT_PRICE_MICROLAMPORTS,
  MAX_MEMO_BYTES,
  MAX_SIGNATURE_HEADER,
  MEMO_PROGRAM,
  NetworkInfo,
  TOKEN_PROGRAM,
  USDC_DECIMALS,
} from "./constants";
import { base64Decode, isAddress } from "./encoding";
import { ClientRefusal, HodlOddsError, PriceLimitError } from "./errors";
import { isObject, Json, parseJsonObject } from "./util";

const TOKEN_PROGRAM_ID = new PublicKey(TOKEN_PROGRAM);
const ASSOCIATED_TOKEN_PROGRAM_ID = new PublicKey(ASSOCIATED_TOKEN_PROGRAM);
const COMPUTE_BUDGET_PROGRAM_ID = new PublicKey(COMPUTE_BUDGET_PROGRAM);
const MEMO_PROGRAM_ID = new PublicKey(MEMO_PROGRAM);

export interface Requirement {
  scheme: string;
  network: string;
  asset: string;
  amount: string;
  payTo: string;
  maxTimeoutSeconds: number;
  extra: Json;
}

/** Decodes a base64 JSON header into an object; null when it is not one. */
export function decodeHeaderObject(header: string | undefined): Json | null {
  const bytes = header === undefined ? null : base64Decode(header);
  return bytes ? parseJsonObject(bytes) : null;
}

function isRequirementShape(row: unknown): row is Requirement {
  return isObject(row) && typeof row.scheme === "string" && typeof row.network === "string" && typeof row.asset === "string" &&
    typeof row.amount === "string" && typeof row.payTo === "string" && typeof row.maxTimeoutSeconds === "number" &&
    Number.isInteger(row.maxTimeoutSeconds) && (row.extra === undefined || isObject(row.extra));
}

/**
 * Exactly one x402 v2 option on the configured network paying the pinned recipient (the Python SDK's rule). Like the
 * SDK's typed decoding, an offer with any malformed option, resource or extensions is refused as a whole.
 */
export function selectOffer(data: Json, network: NetworkInfo, payTo: string): Requirement {
  if (typeof data.x402Version !== "number" || data.x402Version !== 2) {
    throw new ClientRefusal("only x402 v2 offers are accepted");
  }
  const accepts = data.accepts;
  if (!Array.isArray(accepts) || accepts.length < 1 || accepts.length > 16) {
    throw new ClientRefusal("invalid payment options");
  }
  if (!accepts.every(isRequirementShape) ||
      (data.resource !== undefined && data.resource !== null && !(isObject(data.resource) && typeof data.resource.url === "string")) ||
      (data.extensions !== undefined && data.extensions !== null && !isObject(data.extensions))) {
    throw new ClientRefusal("invalid PAYMENT-REQUIRED offer; nothing signed");
  }
  const matches = accepts.filter((row) => row.network === network.caip2 && row.payTo === payTo);
  if (matches.length !== 1) {
    throw new ClientRefusal("expected one payment option matching the configured network and pinned pay_to");
  }
  const row = matches[0] as Requirement;
  return { ...row, extra: row.extra ?? {} };
}

/** Every check before signing; returns the price in atomic USDC. The limit is compared last. */
export function checkOffer(row: Requirement, network: NetworkInfo, payTo: string, limitAtomic: number): number {
  if (row.scheme !== "exact" || row.asset !== network.usdc) {
    throw new ClientRefusal("only exact USDC payments are accepted");
  }
  if (!isAddress(row.payTo) || row.payTo !== payTo) {
    throw new ClientRefusal("server payTo differs from the pinned pay_to address");
  }
  if (!/^[1-9][0-9]{0,6}$/.test(row.amount)) {
    throw new ClientRefusal("price is invalid or above your USDC limit");
  }
  const extra = row.extra;
  if (extra.paymentFlow !== "authorization") {
    throw new ClientRefusal("only authorization payment flow is accepted");
  }
  if (row.maxTimeoutSeconds < 1 || row.maxTimeoutSeconds > 300) {
    throw new ClientRefusal("payment timeout must be between 1 and 300 seconds");
  }
  if (!isAddress(extra.feePayer)) {
    throw new ClientRefusal("a valid facilitator feePayer is required");
  }
  if ("memo" in extra && (typeof extra.memo !== "string" || Buffer.byteLength(extra.memo, "utf8") > MAX_MEMO_BYTES)) {
    throw new ClientRefusal("invalid or oversized payment memo (maximum 32 bytes)");
  }
  if ("recentBlockhash" in extra && !isAddress(extra.recentBlockhash)) {
    throw new ClientRefusal("invalid payment blockhash");
  }
  if ("hodloddsBoard" in extra || "hodloddsBoardSignature" in extra) {
    throw new ClientRefusal("board offers are not supported by this plugin version; nothing signed");
  }
  const amount = Number(row.amount);
  if (amount > limitAtomic) {
    throw new PriceLimitError(amount, limitAtomic);
  }
  return amount;
}

function associatedTokenAddress(owner: PublicKey, mint: PublicKey): PublicKey {
  return PublicKey.findProgramAddressSync([owner.toBuffer(), TOKEN_PROGRAM_ID.toBuffer(), mint.toBuffer()], ASSOCIATED_TOKEN_PROGRAM_ID)[0];
}

export function payerTokenAccount(payer: PublicKey, network: NetworkInfo): PublicKey {
  return associatedTokenAddress(payer, new PublicKey(network.usdc));
}

/**
 * The x402 "exact" Solana payment, byte-identical to what the official x402 client 2.25 builds (checked by a test
 * against the Python client): SetComputeUnitLimit(20000), SetComputeUnitPrice(1), TransferChecked(amount, 6 decimals),
 * Memo (the seller's memo, or 16 random bytes as 32 hex characters). The facilitator pays the fee and signs at index 0;
 * the payer signs at index 1 as the transfer authority. Accounts are ordered as Solana's Rust message compiler orders
 * them: signers first, then writable, then read-only accounts, each group sorted by key bytes.
 * `source` is the payer's USDC token account (payerTokenAccount), derived once by the caller.
 */
export function buildPayment(row: Requirement, payer: PublicKey, source: PublicKey, blockhash: string): VersionedTransaction {
  const extra = row.extra;
  const feePayer = new PublicKey(extra.feePayer as string);
  const mint = new PublicKey(row.asset);
  const destination = associatedTokenAddress(new PublicKey(row.payTo), mint);
  const byBytes = (a: PublicKey, b: PublicKey) => Buffer.compare(a.toBuffer(), b.toBuffer());
  const keys = [
    feePayer,
    payer,
    ...[source, destination].sort(byBytes),
    ...[COMPUTE_BUDGET_PROGRAM_ID, TOKEN_PROGRAM_ID, MEMO_PROGRAM_ID, mint].sort(byBytes),
  ];
  if (new Set(keys.map((key) => key.toBase58())).size !== keys.length) {
    throw new ClientRefusal("unexpected account overlap in the payment offer; nothing signed");
  }
  const at = (key: PublicKey) => keys.findIndex((k) => k.equals(key));
  const limit = Buffer.alloc(5);
  limit.writeUInt8(2, 0);
  limit.writeUInt32LE(COMPUTE_UNIT_LIMIT, 1);
  const price = Buffer.alloc(9);
  price.writeUInt8(3, 0);
  price.writeBigUInt64LE(BigInt(COMPUTE_UNIT_PRICE_MICROLAMPORTS), 1);
  const transfer = Buffer.alloc(10);
  transfer.writeUInt8(12, 0);
  transfer.writeBigUInt64LE(BigInt(row.amount), 1);
  transfer.writeUInt8(USDC_DECIMALS, 9);
  const memo = typeof extra.memo === "string" && extra.memo ? extra.memo : randomBytes(16).toString("hex");
  const message = new MessageV0({
    header: { numRequiredSignatures: 2, numReadonlySignedAccounts: 1, numReadonlyUnsignedAccounts: 4 },
    staticAccountKeys: keys,
    recentBlockhash: blockhash,
    compiledInstructions: [
      { programIdIndex: at(COMPUTE_BUDGET_PROGRAM_ID), accountKeyIndexes: [], data: limit },
      { programIdIndex: at(COMPUTE_BUDGET_PROGRAM_ID), accountKeyIndexes: [], data: price },
      { programIdIndex: at(TOKEN_PROGRAM_ID), accountKeyIndexes: [at(source), at(mint), at(destination), at(payer)], data: transfer },
      { programIdIndex: at(MEMO_PROGRAM_ID), accountKeyIndexes: [], data: Buffer.from(memo, "utf8") },
    ],
    addressTableLookups: [],
  });
  return new VersionedTransaction(message);
}

const ED25519_SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");

/**
 * The wallet's output, checked: the message is byte-identical to the one built (serialized before the wallet saw it,
 * so a wallet that edits the object in place is caught), the fee payer's slot is empty, and the payer's signature
 * verifies. Returns the wire bytes to send.
 */
export function checkSigned(expectedMessage: Uint8Array, signed: unknown, payer: PublicKey): Uint8Array {
  const fail = (why: string): never => {
    throw new ClientRefusal(`${why}; nothing sent`);
  };
  if (!signed || typeof (signed as { serialize?: unknown }).serialize !== "function") {
    fail("the wallet returned no transaction");
  }
  let bytes: Uint8Array;
  let tx: VersionedTransaction;
  try {
    bytes = (signed as VersionedTransaction).serialize();
    tx = VersionedTransaction.deserialize(bytes);
  } catch {
    return fail("the wallet returned an unreadable transaction");
  }
  const message = Buffer.from(tx.message.serialize());
  if (!message.equals(Buffer.from(expectedMessage))) {
    fail("the wallet changed the payment transaction");
  }
  const index = tx.message.staticAccountKeys.findIndex((key) => key.equals(payer));
  if (tx.message.header.numRequiredSignatures !== 2 || index !== 1 || tx.signatures.length !== 2) {
    fail("unexpected signer layout");
  }
  if (!(tx.signatures[0] as Uint8Array).every((byte) => byte === 0)) {
    fail("unexpected fee payer signature");
  }
  const publicKey = createPublicKey({ key: Buffer.concat([ED25519_SPKI_PREFIX, payer.toBuffer()]), format: "der", type: "spki" });
  if (!verify(null, message, publicKey, Buffer.from(tx.signatures[index] as Uint8Array))) {
    fail("the wallet signature does not verify");
  }
  return bytes!;
}

/**
 * The x402 v2 PaymentPayload around a transaction, as the official client encodes it (resource and extensions echoed
 * from the offer, which selectOffer already checked). The caller builds it once before signing with the unsigned
 * transaction, which has the same length as the signed one, so an oversized payload is refused before the wallet is
 * asked.
 */
export function paymentSignatureHeader(offer: Json, row: Requirement, transaction: Uint8Array): string {
  const payload: Json = {
    x402Version: 2,
    payload: { transaction: Buffer.from(transaction).toString("base64") },
    accepted: {
      scheme: row.scheme,
      network: row.network,
      asset: row.asset,
      amount: row.amount,
      payTo: row.payTo,
      maxTimeoutSeconds: row.maxTimeoutSeconds,
      extra: row.extra,
    },
  };
  if (isObject(offer.resource)) {
    const resource: Json = {};
    for (const key of ["url", "description", "mimeType", "serviceName", "tags", "iconUrl"]) {
      if (offer.resource[key] !== undefined && offer.resource[key] !== null) {
        resource[key] = offer.resource[key];
      }
    }
    payload.resource = resource;
  }
  if (isObject(offer.extensions) && Object.keys(offer.extensions).length > 0) {
    payload.extensions = offer.extensions;
  }
  const header = Buffer.from(JSON.stringify(payload), "utf8").toString("base64");
  if (header.length > MAX_SIGNATURE_HEADER) {
    throw new ClientRefusal("authorization construction failed: the payment header would be too large; nothing signed");
  }
  return header;
}

export interface Settlement {
  success: boolean;
  transaction: string;
  network: string;
  payer?: string | null;
  amount?: string | null;
  errorReason?: string | null;
}

/** The PAYMENT-RESPONSE header (x402 SettleResponse); throws when it is not one. */
export function decodeSettlement(header: string): Settlement {
  const value = decodeHeaderObject(header);
  if (!value || typeof value.success !== "boolean" || typeof value.transaction !== "string" || typeof value.network !== "string") {
    throw new HodlOddsError("invalid payment response");
  }
  for (const key of ["payer", "amount", "errorReason"]) {
    if (key in value && value[key] !== null && typeof value[key] !== "string") {
      throw new HodlOddsError("invalid payment response");
    }
  }
  return value as unknown as Settlement;
}
