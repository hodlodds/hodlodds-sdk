// Minimal types for the Node test runner (the local @types/node is v12, from the kit's dependency tree).
declare module "node:test" {
  export function test(name: string, fn: () => unknown): Promise<void>;
}

declare module "assert" {
  namespace assert {
    function match(value: string, regExp: RegExp, message?: string | Error): void;
    function throws(block: () => unknown, error?: RegExp | Function | Error | object, message?: string | Error): void;
  }
}
