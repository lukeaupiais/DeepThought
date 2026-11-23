// Independent TypeScript AST check of stored bodies and selected-declaration ownership.
// Reads no evaluation answers and executes no project modules.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const [preparedPath, outputPath, compilerPackage] = process.argv.slice(2);
const ts = require(path.resolve(compilerPackage));
const prepared = JSON.parse(fs.readFileSync(preparedPath, "utf8"));
const root = prepared.provenance.source.workspace;
const nodes = prepared.private_tool_output.nodes
  .filter((n) => n.properties.source_context_schema === "1")
  .map((n) => n.properties);
const key = (file, range) => JSON.stringify([file, ...range]);
const expected = new Map(
  nodes.map((n) => [key(n.file_path, n.name_ranges["utf-16"]), n]),
);
const matched = new Map();
const verifiedOwners = new Map();
const diagnostics = [];
const failures = [];
const sha = (value) => crypto.createHash("sha256").update(value).digest("hex");
for (const file of [...new Set(nodes.map((n) => n.file_path))]) {
  const bytes = fs.readFileSync(path.join(root, file));
  const source = ts.createSourceFile(
    file,
    bytes.toString("utf8"),
    ts.ScriptTarget.Latest,
    true,
    file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const lines = source.text.split("\n");
  for (const diagnostic of source.parseDiagnostics)
    diagnostics.push({ file, code: diagnostic.code });
  const nameRange = (name) => {
    const a = source.getLineAndCharacterOfPosition(name.getStart(source));
    const b = source.getLineAndCharacterOfPosition(name.end);
    return [a.line + 1, a.character, b.line + 1, b.character];
  };
  function declarationName(node) {
    if (node.name) return node.name;
    if (ts.isConstructorDeclaration(node))
      return node
        .getChildren(source)
        .find((n) => n.kind === ts.SyntaxKind.ConstructorKeyword);
    return undefined;
  }
  function position(line, column) {
    const prefix = Buffer.from(lines[line - 1], "utf8")
      .subarray(0, column)
      .toString("utf8");
    return source.getPositionOfLineAndCharacter(line - 1, prefix.length);
  }
  function walk(node) {
    const name = declarationName(node);
    const identity = name ? key(file, nameRange(name)) : undefined;
    const stored = expected.get(identity);
    if (stored) {
      if (matched.has(stored.definition_id))
        failures.push({
          file,
          id: stored.definition_id,
          reason: "ambiguous_ast_name",
        });
      matched.set(stored.definition_id, true);
      const start = position(
        stored.source_start_line,
        stored.source_start_column,
      );
      const end = position(stored.source_end_line, stored.source_end_column);
      const code = source.text.slice(start, end);
      const permittedEnd =
        end === node.end || (end === node.end - 1 && source.text[end] === ";");
      if (
        start > node.getStart(source) ||
        !permittedEnd ||
        code !== stored.source_code ||
        sha(code) !== stored.source_sha256 ||
        sha(bytes) !== stored.file_sha256
      )
        failures.push({
          file,
          id: stored.definition_id,
          reason: "body_or_range_mismatch",
        });
      let parent = node.parent;
      let owner;
      while (parent && !owner) {
        const parentName = declarationName(parent);
        owner = parentName
          ? expected.get(key(file, nameRange(parentName)))
          : undefined;
        parent = parent.parent;
      }
      verifiedOwners.set(stored.definition_id, owner?.definition_id ?? null);
      if (
        (owner?.definition_id ?? null) !== stored.parent_definition_id ||
        stored.lod !== (owner ? owner.lod + 1 : 0)
      )
        failures.push({
          file,
          id: stored.definition_id,
          reason: "parent_or_depth_mismatch",
        });
    }
    ts.forEachChild(node, walk);
  }
  walk(source);
}
for (const node of nodes)
  if (!matched.has(node.definition_id))
    failures.push({
      file: node.file_path,
      id: node.definition_id,
      reason: "missing_compiler_declaration",
    });
const byId = new Map(nodes.map((n) => [n.definition_id, n]));
const files = new Map(
  prepared.private_tool_output.nodes
    .filter((n) => n.labels.includes("File"))
    .map((n) => [n.properties.path, n.properties.qualified_name]),
);
const actualOwnership = new Set(
  prepared.private_tool_output.relationships
    .filter((r) => r.type === "CONTAINS" && r.properties.ownership === "ast")
    .map((r) =>
      JSON.stringify([
        r.source_match.qualified_name,
        r.target_match.qualified_name,
      ]),
    ),
);
const expectedOwnership = new Set(
  nodes.map((n) => {
    const parent = verifiedOwners.get(n.definition_id);
    return JSON.stringify([
      parent ? byId.get(parent).qualified_name : files.get(n.file_path),
      n.qualified_name,
    ]);
  }),
);
for (const edge of expectedOwnership)
  if (!actualOwnership.has(edge))
    failures.push({ reason: "missing_ownership_edge", edge });
for (const edge of actualOwnership)
  if (!expectedOwnership.has(edge))
    failures.push({ reason: "unexpected_ownership_edge", edge });
const report = {
  compiler: ts.version,
  files: new Set(nodes.map((n) => n.file_path)).size,
  selected_declarations: nodes.length,
  verified_ownership_edges: actualOwnership.size,
  matched_declarations: matched.size,
  source_body_and_ownership_pass: !failures.length && !diagnostics.length,
  failures,
  parse_diagnostics: diagnostics,
  scope:
    "All stored selected AST declarations, compared with independently parsed TypeScript name ranges, body boundaries, source hashes and nearest selected declaration ancestry.",
};
fs.writeFileSync(outputPath, JSON.stringify(report, null, 2) + "\n");
console.log(
  JSON.stringify({
    pass: report.source_body_and_ownership_pass,
    files: report.files,
    declarations: nodes.length,
    failures: failures.length,
  }),
);
if (!report.source_body_and_ownership_pass) process.exitCode = 1;
