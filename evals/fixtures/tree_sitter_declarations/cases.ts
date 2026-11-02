// SPDX-License-Identifier: Apache-2.0

export interface Port {
  read(): string;
}

export class Reader {
  read(): string {
    return "reader";
  }
}

export const read = (): string => "standalone";

export namespace Nested {
  export function read(): string {
    return "nested";
  }
}

export type Token = string;
const text = "function imaginary() {}";
// function commentedOut() {}
