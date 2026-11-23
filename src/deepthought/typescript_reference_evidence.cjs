// Source-only compiler evidence. No SCIP results, expected answers or project code execution.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const ts = require(path.join(input.typescript, "lib/typescript.js"));
if (ts.version !== "6.0.3")
  throw Error("Evidence profile requires TypeScript 6.0.3");
const root = input.workspace;
process.chdir(root);
function captured(file) {
  const absolute = path.resolve(file);
  return (
    absolute === root ||
    absolute.startsWith(root + path.sep) ||
    absolute === input.typescript ||
    absolute.startsWith(input.typescript + path.sep)
  );
}
const selectedInputs = new Set(
  [...input.files, ...input.configuration_files].map((file) =>
    path.join(root, file),
  ),
);
function readable(file) {
  const absolute = path.resolve(file);
  return (
    selectedInputs.has(absolute) ||
    absolute.startsWith(path.join(root, "node_modules") + path.sep) ||
    absolute.startsWith(input.typescript + path.sep)
  );
}
const system = {
  ...ts.sys,
  readFile: (file) => (readable(file) ? ts.sys.readFile(file) : undefined),
  fileExists: (file) => readable(file) && ts.sys.fileExists(file),
  directoryExists: (file) => captured(file) && ts.sys.directoryExists(file),
  getDirectories: (file) => (captured(file) ? ts.sys.getDirectories(file) : []),
  readDirectory: (file, ...options) =>
    captured(file) ? ts.sys.readDirectory(file, ...options) : [],
};
const config = ts.readConfigFile(
  path.join(root, "tsconfig.json"),
  system.readFile,
);
if (config.error)
  throw Error(ts.flattenDiagnosticMessageText(config.error.messageText, " "));
const parsed = ts.parseJsonConfigFileContent(config.config, system, root);
if (parsed.errors.length)
  throw Error("Invalid TypeScript project configuration");
if (parsed.projectReferences?.length)
  throw Error("Project references require a separately captured profile");
const options = {
  ...parsed.options,
  noEmit: true,
};
const host = ts.createCompilerHost(options);
host.readFile = system.readFile;
host.fileExists = system.fileExists;
host.directoryExists = system.directoryExists;
host.getDirectories = system.getDirectories;
const program = ts.createProgram(
  input.files.map((file) => path.join(root, file)),
  options,
  host,
);
if (program.getSourceFiles().some((file) => !captured(file.fileName)))
  throw Error("Compiler program contains uncaptured source inputs");
const diagnostics = ts.getPreEmitDiagnostics(program);
if (diagnostics.length) {
  throw Error(
    JSON.stringify(
      diagnostics.slice(0, 10).map((d) => ({
        code: d.code,
        message: ts.flattenDiagnosticMessageText(d.messageText, " "),
      })),
    ),
  );
}
const checker = program.getTypeChecker();
const sourceFiles = input.files.map((file) => {
  const source = program.getSourceFile(path.join(root, file));
  if (!source)
    throw Error("Selected source missing from compiler program: " + file);
  return source;
});
const symbols = new Map();
function original(symbol) {
  const seen = new Set();
  while (symbol && symbol.flags & ts.SymbolFlags.Alias) {
    if (seen.has(symbol)) return undefined;
    seen.add(symbol);
    symbol = checker.getAliasedSymbol(symbol);
  }
  if (!symbol || !symbol.declarations?.length) return undefined;
  const roots = checker.getRootSymbols(symbol);
  return roots.length === 1 ? roots[0] : undefined;
}
function atDefinition(definition) {
  const source = program.getSourceFile(path.join(root, definition.file));
  if (!source) return undefined;
  if (
    definition.kind === "File" &&
    definition.column === 0 &&
    definition.end_column === 0
  ) {
    return original(checker.getSymbolAtLocation(source));
  }
  const start = source.getPositionOfLineAndCharacter(
    definition.line - 1,
    definition.column,
  );
  const end = source.getPositionOfLineAndCharacter(
    definition.end_line - 1,
    definition.end_column,
  );
  let found;
  function visit(node) {
    if (node.getStart(source) > start || node.getEnd() < end) return;
    if (
      (ts.isIdentifier(node) || ts.isStringLiteralLike(node)) &&
      node.getStart(source) === start &&
      node.getEnd() === end
    ) {
      found = original(checker.getSymbolAtLocation(node));
    } else ts.forEachChild(node, visit);
  }
  visit(source);
  return found;
}
const records = input.targets.map((target) => {
  const candidates = target.definitions.map(atDefinition);
  const roots = new Set(candidates);
  const symbol =
    roots.size === 1 && !roots.has(undefined) ? candidates[0] : undefined;
  const record = {
    key: target.key,
    status: symbol ? "checked" : "unavailable",
    references: [],
    exports: [],
    compiler_symbol_id: symbol
      ? crypto
          .createHash("sha256")
          .update(
            JSON.stringify(
              symbol.declarations
                .map((declaration) => {
                  const name = declaration.name || declaration;
                  return [
                    path.relative(root, name.getSourceFile().fileName),
                    name.getStart(),
                    name.getEnd(),
                  ];
                })
                .sort(),
            ),
          )
          .digest("hex")
      : null,
  };
  if (symbol) {
    if (!symbols.has(symbol)) symbols.set(symbol, []);
    symbols.get(symbol).push(record);
  }
  return record;
});
for (const source of sourceFiles) {
  const moduleSymbol = checker.getSymbolAtLocation(source);
  if (!moduleSymbol) continue;
  for (const exported of checker.getExportsOfModule(moduleSymbol)) {
    const matches = symbols.get(original(exported)) || [];
    for (const record of matches)
      record.exports.push({
        name: exported.name,
        file: path.relative(root, source.fileName).split(path.sep).join("/"),
      });
  }
}
const declarationNames = new Map();
for (const symbol of symbols.keys()) {
  declarationNames.set(
    symbol,
    new Set(
      (symbol.declarations || [])
        .map((declaration) => declaration.name)
        .filter(Boolean),
    ),
  );
}
for (const source of sourceFiles) {
  function visit(node) {
    if (ts.isIdentifier(node) || ts.isStringLiteralLike(node)) {
      const symbol = original(checker.getSymbolAtLocation(node));
      const matches = symbols.get(symbol);
      if (matches && !declarationNames.get(symbol).has(node)) {
        const start = source.getLineAndCharacterOfPosition(
          node.getStart(source),
        );
        const end = source.getLineAndCharacterOfPosition(node.getEnd());
        const reference = {
          file_path: path
            .relative(root, source.fileName)
            .split(path.sep)
            .join("/"),
          start_line: start.line + 1,
          column: start.character,
          end_line: end.line + 1,
          end_column: end.character,
        };
        for (const record of matches) record.references.push(reference);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
}
process.stdout.write(
  JSON.stringify({ compiler_version: ts.version, diagnostics: 0, records }),
);
