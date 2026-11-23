// Independent TypeScript compiler oracle for SCIP implementation checks.
// Reads only the workspace's tsconfig.json and sources, never SCIP or
// DeepThought output. For every class and interface declared in project
// source it records each supertype reached through extends or implements
// clauses, transitively, and for each member the same-named members of those
// supertypes. Positions are declaration-name starts; `source_interface` says
// whether the source type, or the member's owner, is an interface.
//
// Usage: node tools/typescript_supertype_oracle.cjs WORKSPACE TYPESCRIPT_DIR OUTPUT.json
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
function at(declaration) {
  const name = ts.getNameOfDeclaration(declaration) || declaration;
  const source = name.getSourceFile();
  const a = source.getLineAndCharacterOfPosition(name.getStart(source));
  return [relative(source), a.line + 1, a.character];
}
function unalias(symbol) {
  const seen = new Set();
  while (symbol && symbol.flags & ts.SymbolFlags.Alias && !seen.has(symbol)) {
    seen.add(symbol);
    symbol = checker.getAliasedSymbol(symbol);
  }
  return symbol;
}
const isType = (d) => ts.isClassDeclaration(d) || ts.isInterfaceDeclaration(d);
function direct(declaration) {
  const found = [];
  for (const clause of declaration.heritageClauses || []) {
    for (const type of clause.types) {
      const symbol = unalias(checker.getSymbolAtLocation(type.expression));
      if (symbol) found.push(symbol);
    }
  }
  return found;
}
const pairs = new Map();
function add(kind, source, target, sourceInterface) {
  const item = { kind, source, target, source_interface: sourceInterface };
  pairs.set(JSON.stringify(item), item);
}
let types = 0;
for (const source of sources) {
  (function walk(node) {
    if (isType(node) && node.name) {
      types += 1;
      const seen = new Set();
      const queue = direct(node);
      while (queue.length) {
        const symbol = queue.shift();
        if (seen.has(symbol)) continue;
        seen.add(symbol);
        for (const d of symbol.declarations || []) if (isType(d)) queue.push(...direct(d));
      }
      const sourceInterface = ts.isInterfaceDeclaration(node);
      for (const symbol of seen) {
        for (const target of (symbol.declarations || []).filter((d) => isType(d) && own(d.getSourceFile().fileName))) {
          add("type", at(node), at(target), sourceInterface);
          for (const member of node.members) {
            const name = member.name && ts.getPropertyNameForPropertyNameNode(member.name);
            if (name === undefined || ts.isConstructorDeclaration(member)) continue;
            for (const other of target.members) {
              const otherName = other.name && ts.getPropertyNameForPropertyNameNode(other.name);
              if (otherName === name && !ts.isConstructorDeclaration(other)) {
                add("member", at(member), at(other), sourceInterface);
              }
            }
          }
        }
      }
    }
    ts.forEachChild(node, walk);
  })(source);
}
const result = [...pairs.values()].sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
fs.writeFileSync(output, JSON.stringify({ compiler: ts.version, files: sources.length, types, pairs: result }) + "\n");
console.log(JSON.stringify({ compiler: ts.version, files: sources.length, types, pairs: result.length }));
