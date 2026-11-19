import { atomicToUsdc } from "./encoding";
import { BudgetExceeded, PaymentRecord } from "./errors";

export type Receipt = Pick<PaymentRecord, "network" | "amount_usdc" | "transaction" | "payer" | "pay_to" | "success" | "charged">;

/** Why paid calls stopped: the facilitator said settlement failed, settled without a usable answer, or unknown. */
export type PauseReason = "facilitator_reported_failure" | "settled_answer_unusable" | "outcome_unknown";

interface SharedState {
  spentAtomic: number;
  paused: Receipt | null;
  reason: PauseReason | null;
  tail: Promise<void>;
}

// One record per (base URL, network, recipient) for the whole process, kept on globalThis under a registered symbol:
// every plugin instance with that key, including instances from another loaded copy of this package, shares the
// spend, the pause and the one-at-a-time lock. Bump the version suffix if SharedState ever changes shape.
const REGISTRY = Symbol.for("hodlodds-agent-kit/ledgers/v1");

/** The process's registry; null when another value holds its slot or globalThis cannot take it (paid actions refuse). */
function registry(): Map<string, SharedState> | null {
  const holder = globalThis as unknown as Record<symbol, unknown>;
  const existing = holder[REGISTRY];
  if (existing instanceof Map) {
    return existing as Map<string, SharedState>;
  }
  if (existing !== undefined) {
    return null;
  }
  const created = new Map<string, SharedState>();
  try {
    Object.defineProperty(globalThis, REGISTRY, { value: created }); // not writable, not configurable, not enumerable
  } catch {
    return null; // a global object that cannot be extended (a hardened runtime)
  }
  return created;
}

export function ledgerKey(baseUrl: string, caip2: string, payTo: string | null): string {
  return JSON.stringify([baseUrl, caip2, payTo]);
}

/**
 * The process's spending for one key: one paid call at a time, a budget checked before signing, the amount reserved
 * before the paid request is sent, refunded only when the server states nothing was charged, and a pause after any
 * payment whose outcome is not a stated "not charged" (every later paid call with this key is refused until the
 * process restarts). Each instance compares the shared spend with its own budget.
 */
export class Ledger {
  readonly budgetAtomic: number | null;
  private readonly shared: SharedState | null;

  constructor(budgetAtomic: number | null, key: string) {
    this.budgetAtomic = budgetAtomic;
    const all = registry();
    let shared = all?.get(key) ?? null;
    if (all !== null && shared === null) {
      shared = { spentAtomic: 0, paused: null, reason: null, tail: Promise.resolve() };
      all.set(key, shared);
    }
    this.shared = shared;
  }

  /** False when the process's registry cannot be used: free actions still work, paid ones refuse. */
  get usable(): boolean {
    return this.shared !== null;
  }

  private get state(): SharedState {
    if (this.shared === null) {
      throw new Error("the HodlOdds spending record of this process cannot be used");
    }
    return this.shared;
  }

  get spentAtomic(): number {
    return this.shared?.spentAtomic ?? 0;
  }

  get paused(): Receipt | null {
    return this.shared?.paused ?? null;
  }

  get pauseReason(): PauseReason | null {
    return this.shared?.reason ?? null;
  }

  pause(record: Receipt, reason: PauseReason): void {
    this.state.paused = record;
    this.state.reason = reason;
  }

  /** Runs paid operations one after another, across every instance with this key. */
  async exclusive<T>(operation: () => Promise<T>): Promise<T> {
    const state = this.state;
    const previous = state.tail;
    let release: () => void = () => undefined;
    state.tail = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous;
    try {
      return await operation();
    } finally {
      release();
    }
  }

  checkBudget(amountAtomic: number): void {
    if (this.budgetAtomic === null || this.state.spentAtomic + amountAtomic > this.budgetAtomic) {
      throw new BudgetExceeded();
    }
  }

  /** Before the authorized request is sent: never released for an uncertain outcome. */
  reserve(amountAtomic: number): void {
    this.checkBudget(amountAtomic);
    this.state.spentAtomic += amountAtomic;
  }

  /** Only when the server stated that nothing was charged. */
  refund(amountAtomic: number): void {
    this.state.spentAtomic = Math.max(0, this.state.spentAtomic - amountAtomic);
  }

  fields(): Record<string, unknown> {
    const spent = this.spentAtomic;
    return {
      spent_atomic: spent,
      spent_usdc: atomicToUsdc(spent),
      budget_atomic: this.budgetAtomic,
      budget_usdc: this.budgetAtomic === null ? null : atomicToUsdc(this.budgetAtomic),
    };
  }
}

export function receipt(payment: PaymentRecord): Receipt;
export function receipt(payment: PaymentRecord | null): Receipt | null;
export function receipt(payment: PaymentRecord | null): Receipt | null {
  if (!payment) {
    return null;
  }
  return {
    network: payment.network,
    amount_usdc: payment.amount_usdc,
    transaction: payment.transaction,
    payer: payment.payer,
    pay_to: payment.pay_to,
    success: payment.success,
    charged: payment.charged,
  };
}
