// Independent source/compiler oracle. Reads no SCIP or DeepThought output.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const root = path.resolve(process.argv[2] || ".");
const ts = require(path.join(root, "node_modules/typescript"));
process.chdir(root);
const configPath = path.join(root, "tsconfig.json");
const raw = ts.readConfigFile(configPath, ts.sys.readFile);
if (raw.error)
  throw Error(ts.flattenDiagnosticMessageText(raw.error.messageText, " "));
const config = ts.parseJsonConfigFileContent(raw.config, ts.sys, root);
if (config.errors.length) throw Error(JSON.stringify(config.errors));
const program = ts.createProgram(config.fileNames, config.options);
const checker = program.getTypeChecker();
const sourceFiles = program.getSourceFiles().filter(
  (f) =>
    f.fileName.startsWith(root + "/src/") &&
    !f.fileName
      .slice(root.length + 1)
      .split("/")
      .some((part) => part.startsWith("__")),
);
function location(node, source) {
  const a = source.getLineAndCharacterOfPosition(node.getStart(source));
  const b = source.getLineAndCharacterOfPosition(node.getEnd());
  return {
    file: path.relative(root, source.fileName).split(path.sep).join("/"),
    line: a.line + 1,
    column: a.character,
    end_line: b.line + 1,
    end_column: b.character,
  };
}
function unalias(symbol) {
  const seen = new Set();
  while (symbol && symbol.flags & ts.SymbolFlags.Alias && !seen.has(symbol)) {
    seen.add(symbol);
    symbol = checker.getAliasedSymbol(symbol);
  }
  return symbol;
}
const result = {
  compiler: ts.version,
  source_files: sourceFiles.length,
  source_sha256: Object.fromEntries(
    sourceFiles.map((f) => [
      path.relative(root, f.fileName),
      crypto.createHash("sha256").update(f.text).digest("hex"),
    ]),
  ),
  targets: {},
  method:
    "Source-authored target definitions and TypeScript checker symbol identity, before inspecting SCIP results. Original declaration name excluded. Re-export and import alias references included. No inferred runtime calls.",
};
for (const name of ["getNamedType", "typeFromAST"]) {
  let declarations = [];
  for (const source of sourceFiles) {
    function walk(n) {
      if (ts.isFunctionDeclaration(n) && n.name?.text === name)
        declarations.push({
          node: n.name,
          source,
          symbol: checker.getSymbolAtLocation(n.name),
        });
      ts.forEachChild(n, walk);
    }
    walk(source);
  }
  if (!declarations.length) throw Error("Missing target " + name);
  const entry = program.getSourceFile(path.join(root, "src/index.ts"));
  const exports = checker.getExportsOfModule(
    checker.getSymbolAtLocation(entry),
  );
  const publicSymbol = exports.find((s) => s.name === name);
  if (!publicSymbol) throw Error("No public export " + name);
  const target = unalias(publicSymbol);
  declarations = declarations.filter((d) => d.symbol === target);
  if (!declarations.length)
    throw Error("Public export missing function declarations");
  const defs = new Set(
    declarations.map((d) => JSON.stringify(location(d.node, d.source))),
  );
  const references = [];
  for (const source of sourceFiles) {
    function walk(n) {
      if (ts.isIdentifier(n) || ts.isStringLiteralLike(n)) {
        const symbol = unalias(checker.getSymbolAtLocation(n));
        if (symbol === target && !defs.has(JSON.stringify(location(n, source))))
          references.push({
            ...location(n, source),
            source_text: n.getText(source),
          });
      }
      ts.forEachChild(n, walk);
    }
    walk(source);
  }
  result.targets[name] = {
    definitions: declarations.map((d) => location(d.node, d.source)),
    references: references.sort(
      (a, b) =>
        a.file.localeCompare(b.file) || a.line - b.line || a.column - b.column,
    ),
  };
}
fs.writeFileSync(
  path.resolve(process.argv[3] || "source-reference-oracle.json"),
  JSON.stringify(result, null, 2) + "\n",
);
console.log(
  JSON.stringify({
    compiler: ts.version,
    files: sourceFiles.length,
    targets: Object.fromEntries(
      Object.entries(result.targets).map(([n, v]) => [
        n,
        { definitions: v.definitions.length, references: v.references.length },
      ]),
    ),
  }),
);
