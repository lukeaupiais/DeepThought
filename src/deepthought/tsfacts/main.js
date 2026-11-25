// tsfacts reads JavaScript/TypeScript files with the TypeScript compiler
// and prints, as JSON lines, the string values the compiler's checker can
// give names and member accesses, for the service-contracts tool.
//
// Programs: one per tsconfig.json/jsconfig.json of the repository (outside
// node_modules), parsed by the compiler itself (extends, paths, baseUrl,
// include and exclude), and one inferred program (allowJs) for the files no
// config holds. Name resolution is the checker's: imports through relative
// paths, tsconfig paths and package exports, re-exports, aliases and
// namespace imports.
//
// A name or member access has a value when the symbol the checker resolves
// it to is
//   - an enum member with a string constant value (checker.getConstantValue),
//   - a const variable whose initializer has a value,
//   - a property of an object literal that initializes a const variable, or
//   - anything whose type is a single string literal type;
// and an initializer has a value when it is a string literal, a template or
// a + join with at least one part that has a value (the others are written
// as Placeholder), or a name or member access with one.
//
// Each output line is {"file", "start", "end", "value"} with UTF-8 byte
// offsets into the file as read from disk; {"file", "module", "resolved"}
// for each import or re-export specifier the compiler resolves to a file
// of the repository (outside node_modules); {"file", "program"} for each
// file read; or {"error"} for a config the compiler rejects.
//
// Globals a build defines (the Angular CLI "define" option) are given as
// DEFINES, a JSON list of [directory, {name: value}]: an ambient
// `declare const NAME` without initializer takes the value the innermost
// project around the file using it defines.
//
// Usage: node main.js TYPESCRIPT_MODULE ROOT DEFINES < files (one relative
// path a line)
"use strict";

const fs = require("fs");
const path = require("path");

const ts = require(process.argv[2]);
const root = path.resolve(process.argv[3]);
const wanted = new Set(
  fs
    .readFileSync(0, "utf8")
    .split("\n")
    .filter((line) => line.length > 0),
);
const defines = JSON.parse(process.argv[4] || "[]");
const PLACEHOLDER = "\u0000";

function defined(file, name) {
  let best = null;
  for (const [directory, values] of defines) {
    if ((directory === "" || file.startsWith(directory + "/")) && (best === null || directory.length > best[0].length)) {
      best = [directory, values];
    }
  }
  const value = best && best[1][name];
  return typeof value === "string" ? value : null;
}
const MAX_DEPTH = 12;

function emit(item) {
  process.stdout.write(JSON.stringify(item) + "\n");
}

function relative(file) {
  return path.relative(root, file).split(path.sep).join("/");
}

function configs() {
  const found = [];
  const walk = (directory) => {
    let entries;
    try {
      entries = fs.readdirSync(directory, { withFileTypes: true });
    } catch (error) {
      return;
    }
    entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    for (const entry of entries) {
      if (entry.isDirectory()) {
        if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
        walk(path.join(directory, entry.name));
      } else if (entry.name === "tsconfig.json" || entry.name === "jsconfig.json") {
        found.push(path.join(directory, entry.name));
      }
    }
  };
  walk(root);
  return found;
}

// Only names resolve; nothing is emitted or type-checked in full, and no
// default library or @types package is loaded.
const QUIET = { noLib: true, types: [], noEmit: true, skipLibCheck: true };

function programs() {
  const made = [];
  const covered = new Set();
  for (const config of configs()) {
    const parsed = ts.getParsedCommandLineOfConfigFile(
      config,
      { allowJs: true },
      {
        ...ts.sys,
        onUnRecoverableConfigFileDiagnostic: () => {},
      },
    );
    if (!parsed) {
      emit({ error: relative(config) });
      continue;
    }
    const names = parsed.fileNames.filter((f) => wanted.has(relative(f)));
    if (names.length === 0) continue;
    names.forEach((f) => covered.add(relative(f)));
    made.push([relative(config), names, { ...parsed.options, ...QUIET }]);
  }
  const rest = [...wanted]
    .filter((f) => !covered.has(f))
    .map((f) => path.join(root, f));
  if (rest.length > 0) {
    made.push([
      "",
      rest,
      {
        allowJs: true,
        jsx: ts.JsxEmit.Preserve,
        module: ts.ModuleKind.ESNext,
        moduleResolution: ts.ModuleResolutionKind.Bundler,
        target: ts.ScriptTarget.ESNext,
        ...QUIET,
      },
    ]);
  }
  return made;
}

function byteOffsets(text, fileName) {
  // UTF-16 position -> UTF-8 byte offset; the compiler drops a BOM.
  let bom = false;
  try {
    const head = fs.readFileSync(fileName).subarray(0, 3);
    bom = head.length === 3 && head[0] === 0xef && head[1] === 0xbb && head[2] === 0xbf;
  } catch (error) {
    bom = false;
  }
  // eslint-disable-next-line no-control-regex
  if (/^[\u0000-\u007f]*$/.test(text)) return bom ? (pos) => pos + 3 : (pos) => pos;
  const table = new Uint32Array(text.length + 1);
  let bytes = 0;
  for (let i = 0; i < text.length; i++) {
    table[i] = bytes;
    const code = text.charCodeAt(i);
    if (code < 0x80) bytes += 1;
    else if (code < 0x800) bytes += 2;
    else if (code >= 0xd800 && code <= 0xdbff) {
      bytes += 4;
      table[++i] = bytes; // the low surrogate
      continue;
    } else bytes += 3;
  }
  table[text.length] = bytes;
  return bom ? (pos) => table[pos] + 3 : (pos) => table[pos];
}

// Names a module's `export *` declarations give more than once: the
// compiler reports them (TS2308) and ECMAScript exports none of them.
const AMBIGUOUS = new Map();
function ambiguous(program, file) {
  if (!AMBIGUOUS.has(file.fileName)) {
    const names = new Set();
    if (file.statements.some((s) => ts.isExportDeclaration(s) && !s.exportClause)) {
      for (const diagnostic of program.getSemanticDiagnostics(file)) {
        if (diagnostic.code !== 2308) continue;
        const text = ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n");
        const match = /member named '([^']+)'/.exec(text);
        if (match) names.add(match[1]);
      }
    }
    AMBIGUOUS.set(file.fileName, names);
  }
  return AMBIGUOUS.get(file.fileName);
}

function analyze(program, checker, file, seen) {
  const offset = byteOffsets(file.text, file.fileName);
  const out = relative(file.fileName);
  function unwrap(node) {
    while (
      ts.isParenthesizedExpression(node) ||
      ts.isAsExpression(node) ||
      ts.isSatisfiesExpression(node) ||
      ts.isTypeAssertionExpression(node) ||
      ts.isNonNullExpression(node)
    ) {
      node = node.expression;
    }
    return node;
  }
  function join(parts) {
    if (!parts.some((p) => p !== null)) return null;
    return parts.map((p) => (p === null ? PLACEHOLDER : p)).join("");
  }
  function valueOf(node, depth) {
    if (!node || depth > MAX_DEPTH) return null;
    node = unwrap(node);
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      return node.text;
    }
    if (ts.isTemplateExpression(node)) {
      const parts = [node.head.text];
      for (const span of node.templateSpans) {
        parts.push(valueOf(span.expression, depth + 1), span.literal.text);
      }
      return join(parts);
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
      return join([valueOf(node.left, depth + 1), valueOf(node.right, depth + 1)]);
    }
    if (
      ts.isIdentifier(node) ||
      ts.isPropertyAccessExpression(node) ||
      (ts.isElementAccessExpression(node) && ts.isStringLiteral(node.argumentExpression))
    ) {
      return named(node, depth);
    }
    return null;
  }
  function named(node, depth) {
    let symbol = checker.getSymbolAtLocation(
      ts.isElementAccessExpression(node) ? node.argumentExpression : node,
    );
    if (symbol && symbol.flags & ts.SymbolFlags.Alias) {
      // Each import or re-export on the way: a name that a module's
      // export * gives ambiguously has no value.
      let step = symbol;
      for (let i = 0; step && step.flags & ts.SymbolFlags.Alias && i < MAX_DEPTH; i++) {
        for (const declaration of step.declarations || []) {
          const holder = ts.findAncestor(
            declaration,
            (n) => ts.isImportDeclaration(n) || ts.isExportDeclaration(n),
          );
          const specifier = holder && holder.moduleSpecifier;
          const target = specifier && checker.getSymbolAtLocation(specifier);
          const source = target && target.valueDeclaration;
          const name = (declaration.propertyName || declaration.name || {}).text;
          if (source && ts.isSourceFile(source) && name && ambiguous(program, source).has(name)) {
            return null;
          }
        }
        try {
          step = checker.getImmediateAliasedSymbol(step);
        } catch (error) {
          step = undefined;
        }
      }
      try {
        symbol = checker.getAliasedSymbol(symbol);
      } catch (error) {
        symbol = undefined;
      }
    }
    const declaration = symbol && (symbol.valueDeclaration || (symbol.declarations || [])[0]);
    if (declaration) {
      if (ts.isEnumMember(declaration)) {
        const value = checker.getConstantValue(declaration);
        return typeof value === "string" ? value : null;
      }
      if (
        ts.isVariableDeclaration(declaration) &&
        ts.getCombinedNodeFlags(declaration) & ts.NodeFlags.Const &&
        declaration.initializer &&
        ts.isIdentifier(declaration.name)
      ) {
        return valueOf(declaration.initializer, depth + 1);
      }
      if (
        ts.isVariableDeclaration(declaration) &&
        !declaration.initializer &&
        ts.isIdentifier(declaration.name) &&
        ts.getCombinedModifierFlags(declaration) & ts.ModifierFlags.Ambient
      ) {
        const value = defined(relative(node.getSourceFile().fileName), declaration.name.text);
        if (value !== null) return value;
      }
      if (ts.isPropertyAssignment(declaration) && constObject(declaration.parent)) {
        return valueOf(declaration.initializer, depth + 1);
      }
    }
    const type = checker.getTypeAtLocation(node);
    return type && type.isStringLiteral() ? type.value : null;
  }
  function constObject(literal) {
    // An object literal initializing a const (nested literals too).
    let node = literal;
    while (ts.isObjectLiteralExpression(node)) {
      let parent = node.parent;
      while (
        ts.isParenthesizedExpression(parent) ||
        ts.isAsExpression(parent) ||
        ts.isSatisfiesExpression(parent)
      ) {
        parent = parent.parent;
      }
      if (ts.isPropertyAssignment(parent)) {
        node = parent.parent;
        continue;
      }
      return (
        ts.isVariableDeclaration(parent) &&
        (ts.getCombinedNodeFlags(parent) & ts.NodeFlags.Const) !== 0
      );
    }
    return false;
  }
  function visit(node) {
    if (
      (ts.isIdentifier(node) && isValue(node)) ||
      ts.isPropertyAccessExpression(node) ||
      ts.isElementAccessExpression(node)
    ) {
      const key = `${node.getStart(file)}:${node.end}`;
      if (!seen.has(key)) {
        seen.add(key);
        const value = named(node, 0);
        if (value !== null) {
          emit({ file: out, start: offset(node.getStart(file)), end: offset(node.end), value });
        }
      }
      if (ts.isPropertyAccessExpression(node)) {
        visit(node.expression);
        return;
      }
    }
    ts.forEachChild(node, visit);
  }
  function isValue(node) {
    const parent = node.parent;
    if (!parent) return false;
    if (ts.isPropertyAccessExpression(parent) && parent.name === node) return false;
    if ((ts.isImportSpecifier(parent) || ts.isExportSpecifier(parent)) && parent.propertyName === node) {
      return false;
    }
    if (
      (ts.isVariableDeclaration(parent) ||
        ts.isParameter(parent) ||
        ts.isFunctionDeclaration(parent) ||
        ts.isClassDeclaration(parent) ||
        ts.isPropertyAssignment(parent) ||
        ts.isPropertyDeclaration(parent) ||
        ts.isMethodDeclaration(parent) ||
        ts.isEnumMember(parent) ||
        ts.isBindingElement(parent) ||
        ts.isImportSpecifier(parent) ||
        ts.isExportSpecifier(parent) ||
        ts.isImportClause(parent) ||
        ts.isNamespaceImport(parent)) &&
      parent.name === node
    ) {
      return false;
    }
    return !ts.isTypeNode(parent) && !ts.isTypeReferenceNode(parent);
  }
  visit(file);
}

const done = new Set();
for (const [config, names, options] of programs()) {
  const program = ts.createProgram({ rootNames: names, options });
  const checker = program.getTypeChecker();
  for (const file of program.getSourceFiles()) {
    const rel = relative(file.fileName);
    if (!wanted.has(rel) || done.has(rel)) continue;
    done.add(rel);
    emit({ file: rel, program: config });
    for (const statement of file.statements) {
      const specifier = statement.moduleSpecifier;
      if (!specifier || !ts.isStringLiteral(specifier)) continue;
      const found = ts.resolveModuleName(specifier.text, file.fileName, options, ts.sys)
        .resolvedModule;
      if (!found || found.isExternalLibraryImport) continue;
      const target = relative(found.resolvedFileName);
      if (!target.startsWith("..") && !target.split("/").includes("node_modules")) {
        emit({ file: rel, module: specifier.text, resolved: target });
      }
    }
    analyze(program, checker, file, new Set());
  }
}
