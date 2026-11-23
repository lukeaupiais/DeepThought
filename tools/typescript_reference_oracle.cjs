// Independent TypeScript compiler oracle for SCIP reference checks.
// Reads only the workspace's tsconfig.json and sources, never SCIP or
// DeepThought output. For every identifier in a project source file whose
// symbol (aliases followed) is declared in project source, it records the
// identifier's range and the ranges of the declaration names.
//
// Usage: node tools/typescript_reference_oracle.cjs WORKSPACE TYPESCRIPT_DIR OUTPUT.json
const fs = require("node:fs");
const path = require("node:path");
const [workspace, typescriptDir, output] = process.argv.slice(2);
if (!output) throw Error("usage: WORKSPACE TYPESCRIPT_DIR OUTPUT.json");
const root = fs.realpathSync(workspace);
const ts = require(path.resolve(typescriptDir));
const raw = ts.readConfigFile(path.join(root, "tsconfig.json"), ts.sys.readFile);
if (raw.error) throw Error(ts.flattenDiagnosticMessageText(raw.error.messageText, " "));
const config = ts.parseJsonConfigFileContent(raw.config, ts.sys, root);
const program = ts.createProgram(config.fileNames, config.options);
const checker = program.getTypeChecker();
const own = (file) =>
  file.startsWith(root + "/") && !file.includes("/node_modules/") && !file.endsWith(".d.ts");
const sources = program.getSourceFiles().filter((f) => own(f.fileName));
const relative = (source) => path.relative(root, source.fileName).split(path.sep).join("/");
function range(node, source) {
  const a = source.getLineAndCharacterOfPosition(node.getStart(source));
  const b = source.getLineAndCharacterOfPosition(node.getEnd());
  return [relative(source), a.line + 1, a.character, b.line + 1, b.character];
}
function unalias(symbol) {
  const seen = new Set();
  while (symbol && symbol.flags & ts.SymbolFlags.Alias && !seen.has(symbol)) {
    seen.add(symbol);
    symbol = checker.getAliasedSymbol(symbol);
  }
  return symbol;
}
const occurrences = [];
let identifiers = 0;
for (const source of sources) {
  (function walk(node) {
    if (ts.isIdentifier(node)) {
      identifiers += 1;
      const symbol = unalias(checker.getSymbolAtLocation(node));
      const declarations = (symbol?.declarations || [])
        .filter((d) => own(d.getSourceFile().fileName))
        .map((d) => ts.getNameOfDeclaration(d) || d)
        .map((n) => range(n, n.getSourceFile()));
      const here = JSON.stringify(range(node, source));
      if (declarations.length && !declarations.some((d) => JSON.stringify(d) === here)) {
        occurrences.push({ range: range(node, source), declarations });
      }
    }
    ts.forEachChild(node, walk);
  })(source);
}
fs.writeFileSync(
  output,
  JSON.stringify({ compiler: ts.version, files: sources.length, identifiers, occurrences }) + "\n",
);
console.log(JSON.stringify({ compiler: ts.version, files: sources.length, identifiers, occurrences: occurrences.length }));
