import { Widget } from "./models";
import { helper } from "./lib/helpers";
import { helper as alternate } from "./other/helpers";

export function annotated(value: Widget): Widget {
  return value;
}

export function constructed(): Widget {
  return new Widget();
}

export function run(): number {
  return helper(3) + alternate(3);
}

export function unresolved() {
  return missingFunction();
}
