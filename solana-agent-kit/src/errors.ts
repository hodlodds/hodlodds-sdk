/** What is known about one paid request; never holds a key or the signed authorization. */
export interface PaymentRecord {
  amount_usdc: string;
  network: string;
  /** true: the server's receipt says settled; false: the facilitator or server says not settled; null: unknown. */
  success: boolean | null;
  transaction: string | null;
  payer: string;
  pay_to: string;
  explorer: string | null;
  /** false only when the server stated that nothing was charged. */
  charged?: boolean;
}

export class HodlOddsError extends Error {
  readonly status: number | null;
  readonly body: unknown;
  readonly payment: PaymentRecord | null;

  constructor(message: string, status: number | null = null, body: unknown = null, payment: PaymentRecord | null = null) {
    super(message);
    this.name = "HodlOddsError";
    this.status = status;
    this.body = body;
    this.payment = payment;
  }
}

/** A refusal made locally before anything was signed or sent; its message is fixed text. */
export class ClientRefusal extends HodlOddsError {
  constructor(message: string) {
    super(message);
    this.name = "ClientRefusal";
  }
}

/** The offer is valid but costs more than the per-answer limit; raised before anything is signed. */
export class PriceLimitError extends ClientRefusal {
  readonly priceAtomic: number;
  readonly limitAtomic: number;

  constructor(priceAtomic: number, limitAtomic: number) {
    super("price is invalid or above your USDC limit");
    this.name = "PriceLimitError";
    this.priceAtomic = priceAtomic;
    this.limitAtomic = limitAtomic;
  }
}

/** The accepted offer does not fit in the remaining session budget; raised before anything is signed. */
export class BudgetExceeded extends HodlOddsError {
  constructor() {
    super("session budget exceeded; nothing signed");
    this.name = "BudgetExceeded";
  }
}
