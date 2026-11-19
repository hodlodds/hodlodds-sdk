const ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
const INDEX: Record<string, number> = Object.fromEntries([...ALPHABET].map((c, i) => [c, i]));

/** Base58 decode; null for anything that is not plain base58. */
export function base58Decode(value: string): Uint8Array | null {
  if (typeof value !== "string" || value.length === 0 || value.length > 128) {
    return null;
  }
  const bytes: number[] = [];
  for (const char of value) {
    const digit = INDEX[char];
    if (digit === undefined) {
      return null;
    }
    let carry = digit;
    for (let i = 0; i < bytes.length; i++) {
      carry += (bytes[i] as number) * 58;
      bytes[i] = carry & 0xff;
      carry >>= 8;
    }
    while (carry > 0) {
      bytes.push(carry & 0xff);
      carry >>= 8;
    }
  }
  for (const char of value) {
    if (char !== "1") {
      break;
    }
    bytes.push(0);
  }
  return Uint8Array.from(bytes.reverse());
}

/** A Solana address: 32 bytes of base58. */
export function isAddress(value: unknown): value is string {
  return typeof value === "string" && /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(value) && base58Decode(value)?.length === 32;
}

/** A Solana transaction signature: 64 bytes of base58. */
export function isSignature(value: unknown): value is string {
  return typeof value === "string" && /^[1-9A-HJ-NP-Za-km-z]{64,88}$/.test(value) && base58Decode(value)?.length === 64;
}

/** Strict standard base64 (with padding) to bytes; null when the text is not canonical base64. */
export function base64Decode(value: string): Buffer | null {
  if (typeof value !== "string" || value.length === 0 || value.length % 4 !== 0 || !/^[A-Za-z0-9+/]+={0,2}$/.test(value)) {
    return null;
  }
  const bytes = Buffer.from(value, "base64");
  return bytes.toString("base64") === value ? bytes : null;
}

/** USDC text ("0.01") to atomic units (6 decimals); null when invalid, zero or negative. */
export function usdcToAtomic(value: unknown): number | null {
  const text = typeof value === "number" ? String(value) : value;
  if (typeof text !== "string") {
    return null;
  }
  const match = /^(0|[1-9][0-9]{0,8})(?:\.([0-9]{1,6}))?$/.exec(text.trim());
  if (!match) {
    return null;
  }
  const atomic = Number(match[1]) * 1_000_000 + Number((match[2] ?? "").padEnd(6, "0") || "0");
  return atomic > 0 ? atomic : null;
}

/** Atomic USDC units to plain text ("0.01"). */
export function atomicToUsdc(atomic: number): string {
  const whole = Math.floor(atomic / 1_000_000);
  const fraction = atomic % 1_000_000;
  return fraction ? `${whole}.${String(fraction).padStart(6, "0").replace(/0+$/, "")}` : String(whole);
}
