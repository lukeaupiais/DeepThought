// Independent TypeScript parser oracle for stored declaration text.
// Reads only the listed source files, never DeepThought output. For every
// named node it records the declaration text from its first token (export
// keyword, decorators and modifiers included, JSDoc excluded) to its end, and
// the name's position with UTF-16 columns, as the compiler counts them. A
// constructor's name is its `constructor` keyword.
//
// Usage: node tools/typescript_declaration_source_oracle.cjs ROOT TYPESCRIPT_DIR FILES.json OUTPUT.json
const fs = require("node:fs");
const path = require("node:path");
const [root, typescriptDir, filesPath, output] = process.argv.slice(2);
if (!output) throw Error("usage: ROOT TYPESCRIPT_DIR FILES.json OUTPUT.json");
const ts = require(path.resolve(typescriptDir));
const found = [];
for (const file of JSON.parse(fs.readFileSync(filesPath, "utf8"))) {
  const text = fs.readFileSync(path.join(root, file), "utf8");
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  const visit = (node) => {
    // A constructor's name is its keyword; other declarations name themselves.
    const name = ts.isConstructorDeclaration(node)
      ? node.getChildren(source).find((c) => c.kind === ts.SyntaxKind.ConstructorKeyword)
      : node.name;
    if (
      name &&
      (ts.isIdentifier(name) ||
        ts.isPrivateIdentifier(name) ||
        ts.isStringLiteral(name) ||
        ts.isComputedPropertyName(name) ||
        name.kind === ts.SyntaxKind.ConstructorKeyword)
    ) {
      const start = source.getLineAndCharacterOfPosition(name.getStart(source));
      const end = source.getLineAndCharacterOfPosition(name.end);
      const first = source.getLineAndCharacterOfPosition(node.getStart(source));
      found.push({
        file,
        kind: ts.SyntaxKind[node.kind],
        name: [start.line + 1, start.character, end.line + 1, end.character],
        start_line: first.line + 1,
        text: text.slice(node.getStart(source), node.end),
      });
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
}
fs.writeFileSync(output, JSON.stringify(found));
