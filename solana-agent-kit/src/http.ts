import { MAX_BODY_BYTES, MAX_HEADERS_BYTES, MAX_PAYMENT_HEADER, USER_AGENT } from "./constants";
import { FetchLike } from "./config";
import { HodlOddsError } from "./errors";
import { Json, parseJsonObject } from "./util";

export interface HttpAnswer {
  status: number;
  /** Lower-case header names; payment headers already checked for size and form. */
  headers: Map<string, string>;
  body: Uint8Array;
}

/**
 * One GET, never retried by the plugin, never redirected, with a timeout and bounded headers and body. (With Node's
 * fetch the one exception is the Fetch standard's single resend after a 421 Misdirected Request; see client.ts.)
 * Payment headers (payment-*, x-payment*) must be single, ASCII, without spaces or commas, and at most 16 KiB.
 * Every refusal after the response arrived aborts it, so its connection is not held.
 */
export async function get(fetchFn: FetchLike, url: string, timeoutMs: number, extra: Record<string, string> = {}): Promise<HttpAnswer> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const refuse = (message: string, status: number): never => {
    controller.abort();
    throw new HodlOddsError(message, status);
  };
  try {
    let response: Response;
    try {
      response = await fetchFn(url, {
        method: "GET",
        headers: { Accept: "application/json", "User-Agent": USER_AGENT, ...extra },
        redirect: "manual",
        signal: controller.signal,
      });
    } catch {
      throw new HodlOddsError(controller.signal.aborted ? "request timed out" : "request failed (network)");
    }
    const headers = new Map<string, string>();
    let size = 0;
    response.headers.forEach((value, key) => {
      size += key.length + value.length;
      headers.set(key.toLowerCase(), value);
    });
    if (size > MAX_HEADERS_BYTES) {
      refuse("response headers exceed the client size limit", response.status);
    }
    for (const [key, value] of headers) {
      if ((key.startsWith("payment-") || key.startsWith("x-payment")) &&
          (value.length > MAX_PAYMENT_HEADER || !/^[\x21-\x7e]*$/.test(value) || value.includes(","))) {
        refuse("duplicate, invalid or oversized payment header", response.status);
      }
    }
    if (response.status >= 300 && response.status < 400) {
      refuse("redirect refused; use the final endpoint", response.status);
    }
    const chunks: Uint8Array[] = [];
    let total = 0;
    if (response.body) {
      const reader = response.body.getReader();
      for (;;) {
        let part: ReadableStreamReadResult<Uint8Array>;
        try {
          part = await reader.read();
        } catch {
          throw new HodlOddsError(controller.signal.aborted ? "request timed out" : "response body failed", response.status);
        }
        if (part.done) {
          break;
        }
        total += part.value.byteLength;
        if (total > MAX_BODY_BYTES) {
          refuse("response body exceeds the client size limit", response.status);
        }
        chunks.push(part.value);
      }
    }
    const body = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      body.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return { status: response.status, headers, body };
  } finally {
    clearTimeout(timer);
  }
}

/** The body as a JSON object, or null. */
export function jsonObject(answer: HttpAnswer): Json | null {
  return parseJsonObject(answer.body);
}
