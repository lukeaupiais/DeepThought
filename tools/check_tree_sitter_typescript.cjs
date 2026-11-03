#!/usr/bin/env node
/* Independently compare TreeSitterTool declarations with the TypeScript compiler.
 *
 * Run with an existing, externally installed TypeScript package. This checker
 * never imports DeepThought's extractor or installs source dependencies.
 *
 * node tools/check_tree_sitter_typescript.cjs \
 *   prepared.private.json independent-conformance.json /path/to/typescript
 */
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const [preparedPath, outputPath, compilerPackage] = process.argv.slice(2);
if (!preparedPath || !outputPath || !compilerPackage || process.argv.length !== 5) {
  throw new Error('Expected prepared.private.json output.json TypeScript-package-root');
}
const ts = require(path.resolve(compilerPackage));
const prepared = JSON.parse(fs.readFileSync(preparedPath, 'utf8'));
const workspace = prepared.provenance.source.workspace;
const compilerFacts = [];
const parseDiagnostics = [];
const unsupportedNames = [];

function declarationStart(node, source) {
  // Export and ambient wrappers are separate declaration nodes in Tree-sitter.
  // Retain declaration modifiers such as async and class-member visibility.
  const excluded = new Set([
    ts.SyntaxKind.ExportKeyword,
    ts.SyntaxKind.DefaultKeyword,
    ts.SyntaxKind.DeclareKeyword,
  ]);
  let start = node.getStart(source);
  for (const child of node.getChildren(source)) {
    if (child.kind === ts.SyntaxKind.SyntaxList && node.modifiers) {
      const modifiers = child.getChildren(source);
      if (modifiers.some((modifier) => ts.isModifier(modifier))) {
        const retained = modifiers.find((modifier) => !excluded.has(modifier.kind));
        if (retained) {
          return retained.getStart(source);
        }
        start = child.end;
        continue;
      }
    }
    if (child.getStart(source) >= start && !excluded.has(child.kind)) {
      return child.getStart(source);
    }
  }
  return start;
}

function declarationName(node, source) {
  const name = node.name;
  if (!name) {
    return null;
  }
  if (ts.isComputedPropertyName(name)) {
    // Preserve the complete syntactic key. This records a declaration name,
    // without evaluating the expression or resolving its runtime property key.
    return name.getText(source);
  }
  if (
    ts.isIdentifier(name) ||
    ts.isStringLiteral(name) ||
    ts.isNumericLiteral(name) ||
    ts.isPrivateIdentifier(name)
  ) {
    return name.getText(source);
  }
  unsupportedNames.push({
    file: source.fileName,
    line: source.getLineAndCharacterOfPosition(name.getStart(source)).line + 1,
    reason: 'Computed or non-simple declaration name',
    text: name.getText(source),
  });
  return null;
}

for (const file of Object.keys(prepared.scenario.source.files).sort()) {
  if (!/\.tsx?$/.test(file)) {
    continue;
  }
  const content = fs.readFileSync(path.join(workspace, file));
  const actualHash = crypto.createHash('sha256').update(content).digest('hex');
  if (actualHash !== prepared.scenario.source.files[file]) {
    throw new Error('Manifest source fingerprint mismatch: ' + file);
  }
  const source = ts.createSourceFile(
    file,
    content.toString('utf8'),
    ts.ScriptTarget.Latest,
    true,
    file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  for (const diagnostic of source.parseDiagnostics) {
    parseDiagnostics.push({
      file,
      start: diagnostic.start,
      code: diagnostic.code,
      message: ts.flattenDiagnosticMessageText(diagnostic.messageText, ' '),
    });
  }

  function walk(node, scopes) {
    let kind = null;
    let name = null;
    if (ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node)) {
      kind = 'Function';
      name = declarationName(node, source);
    } else if (
      ts.isVariableDeclaration(node) &&
      node.initializer &&
      ts.isArrowFunction(node.initializer) &&
      ts.isIdentifier(node.name)
    ) {
      kind = 'Function';
      name = node.name.getText(source);
    } else if (ts.isClassDeclaration(node) || ts.isClassExpression(node)) {
      kind = 'Class';
      name = declarationName(node, source);
    } else if (ts.isInterfaceDeclaration(node)) {
      kind = 'Interface';
      name = declarationName(node, source);
    } else if (ts.isTypeAliasDeclaration(node)) {
      kind = 'TypeAlias';
      name = declarationName(node, source);
    } else if (ts.isEnumDeclaration(node)) {
      kind = 'Enum';
      name = declarationName(node, source);
    } else if (ts.isModuleDeclaration(node)) {
      kind = 'Namespace';
      name = declarationName(node, source);
    } else if (ts.isConstructorDeclaration(node)) {
      kind = 'Method';
      name = 'constructor';
    } else if (
      ts.isMethodDeclaration(node) ||
      ts.isMethodSignature(node) ||
      ts.isGetAccessorDeclaration(node) ||
      ts.isSetAccessorDeclaration(node)
    ) {
      kind = 'Method';
      name = declarationName(node, source);
    }
    if (kind && name) {
      const position = ts.isVariableDeclaration(node)
        ? node.getStart(source)
        : declarationStart(node, source);
      compilerFacts.push({
        file,
        symbol: [...scopes, name].join('.'),
        kind,
        line: source.getLineAndCharacterOfPosition(position).line + 1,
      });
      scopes = [...scopes, name];
    }
    ts.forEachChild(node, (child) => walk(child, scopes));
  }
  walk(source, []);
}

const graphFacts = prepared.private_tool_output.nodes
  .filter((node) => node.labels.includes('Symbol'))
  .map((node) => ({
    file: node.properties.file_path,
    symbol: node.properties.name,
    kind: node.properties.kind,
    line: node.properties.start_line,
  }));
const key = (fact) => JSON.stringify([fact.file, fact.symbol, fact.kind, fact.line]);
const identity = (fact) => JSON.stringify([fact.file, fact.symbol, fact.kind]);
function factCounts(facts) {
  const counts = new Map();
  for (const fact of facts) {
    const factKey = key(fact);
    counts.set(factKey, (counts.get(factKey) || 0) + 1);
  }
  return counts;
}

function unmatchedFacts(facts, oppositeCounts) {
  const remaining = new Map(oppositeCounts);
  return facts.filter((fact) => {
    const factKey = key(fact);
    const count = remaining.get(factKey) || 0;
    if (count > 0) {
      remaining.set(factKey, count - 1);
      return false;
    }
    return true;
  });
}

function duplicates(counts) {
  return [...counts.entries()]
    .filter(([, count]) => count > 1)
    .map(([factKey, count]) => ({ tuple: JSON.parse(factKey), count }));
}

const compilerCounts = factCounts(compilerFacts);
const graphCounts = factCounts(graphFacts);
const missing = unmatchedFacts(compilerFacts, graphCounts);
const extra = unmatchedFacts(graphFacts, compilerCounts);
const lineMismatches = [];
const missingFromGraph = [];
const pairedExtraIndexes = new Set();
for (const compilerFact of missing) {
  const extraIndex = extra.findIndex(
    (fact, index) =>
      identity(fact) === identity(compilerFact) && !pairedExtraIndexes.has(index),
  );
  if (extraIndex !== -1) {
    lineMismatches.push({ compiler: compilerFact, graph: extra[extraIndex] });
    pairedExtraIndexes.add(extraIndex);
  } else {
    missingFromGraph.push(compilerFact);
  }
}
const result = {
  compiler: {
    name: 'typescript',
    version: ts.version,
    script_sha256: crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex'),
  },
  scope: 'Every production TypeScript/TSX source file in the prepared manifest',
  methodology: 'Independent compiler AST traversal. No DeepThought parser imports or graph-derived oracle. The graph output is read only after extraction for final multiset comparison, retaining repeated identical tuples.',
  start_policy: 'Exclude export/default/declare wrappers, retain async and member modifiers. Identifier-bound arrow starts at variable declarator. All positions are 1-based syntax start lines without documentation comments.',
  accessor_policy: 'Get/set accessors and constructor members are Method declarations. Constructor AST lacks a name property but its explicit syntax names the member constructor. Simple literal/identifier names preserve exact source spelling, including quotes and identifier escapes. Computed members preserve the complete bracketed syntax via the compiler AST, without evaluating or resolving the property expression.',
  anonymous_scope_policy: 'Anonymous functions, callbacks and classes introduce no named scope component. Named descendants retain named lexical ancestors. Model differences must be reviewed separately from parser omissions.',
  source_files: Object.keys(prepared.scenario.source.files).length,
  source_hashes_verified: true,
  compiler_fact_count: compilerFacts.length,
  graph_fact_count: graphFacts.length,
  exact_matched_count: compilerFacts.length - missing.length,
  duplicate_tuple_counts: { compiler: duplicates(compilerCounts), graph: duplicates(graphCounts) },
  compiler_parse_diagnostics: parseDiagnostics,
  unhandled_name_notes: unsupportedNames,
  line_mismatches: lineMismatches,
  missing_from_graph: missingFromGraph,
  extra_in_graph: extra.filter((fact, index) => !pairedExtraIndexes.has(index)),
  compiler_facts: compilerFacts,
};
fs.writeFileSync(outputPath, JSON.stringify(result, null, 2) + '\n', { flag: 'wx' });
console.log(JSON.stringify({
  compiler_version: ts.version,
  files: result.source_files,
  compiler_facts: result.compiler_fact_count,
  graph_facts: result.graph_fact_count,
  matched: result.exact_matched_count,
  line_mismatches: lineMismatches.length,
  missing: missingFromGraph.length,
  extra: result.extra_in_graph.length,
  diagnostics: parseDiagnostics.length,
}));
