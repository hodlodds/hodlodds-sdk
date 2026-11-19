export type Json = Record<string, unknown>;

export function isObject(value: unknown): value is Json {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** UTF-8 JSON bytes to an object; null for anything else. */
export function parseJsonObject(bytes: Uint8Array): Json | null {
  try {
    const value: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    return isObject(value) ? value : null;
  } catch {
    return null;
  }
}

/** An own property of a lookup table (never an inherited one such as "constructor" or "__proto__"). */
export function lookup<T>(table: Readonly<Record<string, T>>, key: string): T | undefined {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

// Token metadata written by a token's creator, never passed to the agent: a name or a link can carry instructions.
const CREATOR_TEXT = new Set(["name", "image_uri", "image", "description", "uri", "website", "twitter", "telegram"]);
const TICKER = /^[A-Za-z0-9]{1,12}$/;

/** Removes creator-written fields at any depth; a symbol is kept only in plain ticker form. */
export function scrub(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(scrub);
  }
  if (!isObject(value)) {
    return value;
  }
  const out: Json = {};
  for (const [key, inner] of Object.entries(value)) {
    if (CREATOR_TEXT.has(key)) {
      continue;
    }
    if ((key === "symbol" || key === "quote_symbol") && inner !== null && !(typeof inner === "string" && TICKER.test(inner))) {
      continue;
    }
    out[key] = scrub(inner);
  }
  return out;
}
