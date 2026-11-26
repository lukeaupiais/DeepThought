// Anonymous callbacks outside every named callable, for the impact oracle.
//
// usage: node tools/ts_callback_regions.cjs TYPESCRIPT_JS ROOT < FILES > regions.tsv
//
// A test framework registers its cases as anonymous functions passed to a
// call: `describe('x', () => { it('y', () => { f() }) })`. scip-typescript
// gives such a function no symbol, so the impact oracle has no callable to
// attribute `f()` to and drops the call. This lists each function or arrow
// that is an argument of a call and is not inside any other function, except
// another such callback, as a region: calls inside it belong to it (the
// innermost one), and it is a caller only, never a call target or a seed.
// tools/impact_truth.py derive takes the rows with --initializers. Whether a
// callback runs the callbacks inside it (`act(() => ...)` in an `it`) is not
// ruled on: a dependent reached only through one is unjudged.
//
// Only callbacks outside named callables are listed, so a callback inside a
// function (`items.map((x) => f(x))` in `g`) still belongs to `g` and no
// existing dependent changes.
//
// Reads workspace-relative paths (one per line) on stdin and prints
//   file  owner_line  first_line  last_line  name
// where owner_line is the line the call starts on (`it(` or `describe(`),
// first and last span the callback, and name is the callee and its first
// argument when that is a string literal (`it("adds")`).
"use strict";

const fs = require("fs");
const path = require("path");

const [typescriptJs, rootArg] = process.argv.slice(2);
const ts = require(typescriptJs);
const root = path.resolve(rootArg);

const files = fs
  .readFileSync(0, "utf8")
  .split("\n")
  .map((f) => f.trim())
  .filter(Boolean);

function kind(file) {
  if (file.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (file.endsWith(".jsx")) return ts.ScriptKind.JSX;
  if (/\.[cm]?ts$/.test(file)) return ts.ScriptKind.TS;
  return ts.ScriptKind.JS;
}

function calleeText(sourceFile, call) {
  const text = call.expression.getText(sourceFile).replace(/\s+/g, "");
  const first = call.arguments[0];
  return first && ts.isStringLiteralLike(first)
    ? `${text}(${JSON.stringify(first.text)})`
    : `${text}(…)`;
}

const rows = [];
for (const file of files) {
  const text = fs.readFileSync(path.join(root, file), "utf8");
  const sourceFile = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind(file));
  const line = (pos) => sourceFile.getLineAndCharacterOfPosition(pos).line + 1;
  const visit = (node) => {
    if (ts.isFunctionLike(node)) {
      const parent = node.parent;
      const callback =
        (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) &&
        parent &&
        ts.isCallExpression(parent) &&
        parent.arguments.includes(node);
      if (!callback) return; // a named or other callable: the index owns it
      rows.push(
        [
          file,
          line(parent.getStart(sourceFile)),
          line(node.getStart(sourceFile)),
          line(node.end),
          calleeText(sourceFile, parent),
        ].join("\t")
      );
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
}
process.stdout.write(
  "# file\towner_line\tfirst_line\tlast_line\tname\n" + rows.map((r) => r + "\n").join("")
);
