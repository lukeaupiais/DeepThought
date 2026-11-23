import { Widget } from "./models.js";
import { helper } from "./lib/helpers.js";
import { helper as alternate } from "./other/helpers.js";

export function constructed() {
  return new Widget();
}

export function run() {
  return helper(3) + alternate(3);
}

export function unresolved() {
  return missingFunction();
}
