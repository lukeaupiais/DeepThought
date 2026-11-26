// Calls through a member of a constant object literal, for the impact oracle.
//
// usage: node tools/ts_member_dispatch.cjs TYPESCRIPT_JS ROOT < FILES
//
// A call such as `detectors[name](c)`, or `const detector = detectors[name]`
// followed by `detector(c)`, runs one of the object's function members, and
// the key decides which. When `detectors` is a `const` whose initializer is an
// object literal (optionally `as const` or `satisfies T`), the TypeScript
// compiler resolves each member to its declaration, so the call can reach
// exactly those functions. The oracle cannot say which one runs, so
// tools/impact_truth.py takes these rows with --possible-calls: a dependent
// reached only through one is unjudged, neither expected nor wrong.
//
// Reads workspace-relative paths (one per line) on stdin and prints rows in
// the call-truth format (tools/ground_truth.py read_truth):
//   file  line  name  target_file  target_line  callable
// where name is the callee as the call truth names the site (the last name
// written before the arguments) and the target line is the line of the
// member's declared name. Nothing is printed for a key that is a string or
// number literal (the call truth binds those) or for an object that is not a
// constant literal.
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
  .filter(Boolean)
  .map((f) => path.join(root, f));

const program = ts.createProgram(files, {
  allowJs: true,
  checkJs: false,
  jsx: ts.JsxEmit.Preserve,
  noEmit: true,
  skipLibCheck: true,
  target: ts.ScriptTarget.ESNext,
  module: ts.ModuleKind.ESNext,
  moduleResolution: ts.ModuleResolutionKind.Bundler,
  allowImportingTsExtensions: true,
});
const checker = program.getTypeChecker();

function strip(node) {
  while (
    node &&
    (ts.isParenthesizedExpression(node) ||
      ts.isAsExpression(node) ||
      ts.isSatisfiesExpression(node) ||
      ts.isTypeAssertionExpression(node) ||
      ts.isNonNullExpression(node))
  ) {
    node = node.expression;
  }
  return node;
}

function resolved(symbol) {
  if (symbol && symbol.flags & ts.SymbolFlags.Alias) {
    symbol = checker.getAliasedSymbol(symbol);
  }
  return symbol;
}

function isConst(declaration) {
  return (
    ts.isVariableDeclaration(declaration) &&
    ts.isVariableDeclarationList(declaration.parent) &&
    (declaration.parent.flags & ts.NodeFlags.Const) !== 0
  );
}

function relative(sourceFile) {
  return path.relative(root, sourceFile.fileName).split(path.sep).join("/");
}

function lineOf(node) {
  const sourceFile = node.getSourceFile();
  return sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
}

// The declarations a constant object literal's members name, or null.
function members(objectExpression) {
  const expression = strip(objectExpression);
  if (!ts.isIdentifier(expression)) return null;
  const symbol = resolved(checker.getSymbolAtLocation(expression));
  const declaration = symbol && symbol.valueDeclaration;
  if (!declaration || !isConst(declaration) || !declaration.initializer) return null;
  const literal = strip(declaration.initializer);
  if (!ts.isObjectLiteralExpression(literal)) return null;
  const targets = [];
  for (const property of literal.properties) {
    let named = null;
    if (ts.isShorthandPropertyAssignment(property)) {
      named = resolved(checker.getShorthandAssignmentValueSymbol(property));
    } else if (ts.isPropertyAssignment(property)) {
      const value = strip(property.initializer);
      if (ts.isIdentifier(value)) {
        named = resolved(checker.getSymbolAtLocation(value));
      } else if (ts.isFunctionExpression(value) || ts.isArrowFunction(value)) {
        targets.push(property.name);
        continue;
      } else {
        return null; // a member the compiler cannot name: no claim at all
      }
    } else if (ts.isMethodDeclaration(property)) {
      targets.push(property.name);
      continue;
    } else {
      return null; // a spread or accessor
    }
    const target = named && named.valueDeclaration;
    if (!target || !target.name) return null;
    targets.push(target.name);
  }
  return targets;
}

// The member access a callee reads, when its key is not a literal.
function dynamicAccess(callee) {
  callee = strip(callee);
  if (ts.isElementAccessExpression(callee)) {
    const key = strip(callee.argumentExpression);
    if (ts.isStringLiteralLike(key) || ts.isNumericLiteral(key)) return null;
    return callee;
  }
  if (ts.isIdentifier(callee)) {
    const symbol = checker.getSymbolAtLocation(callee);
    const declaration = symbol && symbol.valueDeclaration;
    if (declaration && isConst(declaration) && declaration.initializer) {
      return dynamicAccess(declaration.initializer);
    }
  }
  return null;
}

function calleeName(callee) {
  callee = strip(callee);
  if (ts.isIdentifier(callee)) return callee.text;
  if (ts.isElementAccessExpression(callee)) return calleeName(callee.expression);
  if (ts.isPropertyAccessExpression(callee)) return callee.name.text;
  return null;
}

const rows = new Set();
for (const sourceFile of program.getSourceFiles()) {
  if (!files.includes(sourceFile.fileName)) continue;
  const visit = (node) => {
    if (ts.isCallExpression(node)) {
      const access = dynamicAccess(node.expression);
      const name = calleeName(node.expression);
      const targets = access && name && members(access.expression);
      if (targets) {
        for (const target of targets) {
          rows.add(
            [
              relative(sourceFile),
              lineOf(node.expression),
              name,
              relative(target.getSourceFile()),
              lineOf(target),
              "callable",
            ].join("\t")
          );
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
}
process.stdout.write(
  "# file\tline\tname\ttarget_file\ttarget_line\tkind\n" +
    [...rows].sort().map((row) => row + "\n").join("")
);
