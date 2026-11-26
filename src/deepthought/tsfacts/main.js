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
// file read; {"file", "failures"} for names the checker failed on; or
// {"error"} for a config the compiler rejects; and {"file", "start",
// "end", "types"} for a parameter or return type written on a decorated
// method, with the repository's declarations of the named types it
// carries (declaredTypes).
//
// {"file", "proto": {...}} lines name the protobuf types generated code
// declares (protoFacts): protoc-gen-js's goog.exportSymbol, protobuf-es's
// fileDesc/messageDesc/enumDesc descriptors, Message<"name"> types and
// typeName properties, and ts-proto's protobufPackage.
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

// All of standard input. readFileSync(0) fails with EAGAIN when the pipe is
// non-blocking and momentarily empty (a writer slower than this reader), so
// read in chunks and wait briefly whenever no data is ready yet.
function readStdin() {
  const chunks = [];
  const buffer = Buffer.alloc(1 << 16);
  const pause = new Int32Array(new SharedArrayBuffer(4));
  for (;;) {
    let read;
    try {
      read = fs.readSync(0, buffer, 0, buffer.length, null);
    } catch (error) {
      if (error.code === "EAGAIN") {
        Atomics.wait(pause, 0, 0, 2);
        continue;
      }
      if (error.code === "EOF") break;
      throw error;
    }
    if (read === 0) break;
    chunks.push(Buffer.from(buffer.subarray(0, read)));
  }
  return Buffer.concat(chunks).toString("utf8");
}
const root = path.resolve(process.argv[3]);
const wanted = new Set(
  readStdin()
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
  let failures = 0;
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
        let value = null;
        try {
          value = named(node, 0);
        } catch (error) {
          // The checker gave up on this name (a stack overflow on a
          // bundled library, say): it has no value.
          failures += 1;
        }
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
  declaredTypes(checker, file, offset, out);
  if (failures > 0) emit({ file: out, failures });
}

// Types that only wrap the type a value carries: Promise<T>, T[] and the
// like give T. With noLib the checker has no lib types, so wrappers are
// read from the type's written form.
// Array methods whose callback's first parameter is an element.
const ARRAY_CALLBACKS = new Set([
  "map",
  "flatMap",
  "forEach",
  "filter",
  "find",
  "some",
  "every",
]);

const WRAPPERS = new Set([
  "Promise",
  "PromiseLike",
  "Observable",
  "Array",
  "ReadonlyArray",
  "Readonly",
  "Partial",
  "Required",
]);

function declaredTypes(checker, file, offset, out) {
  // For each parameter type and return type written on a decorated method
  // (a NestJS handler, say), the declarations in the repository of the
  // named types it carries: {file, start, end, types: [{file, name, line,
  // column}], unresolved}, line 1-based and column in UTF-8 bytes of the
  // name; unresolved counts the written names the compiler resolves to no
  // declaration at all.
  let unresolved = 0;
  function named(node, depth, found) {
    if (!node || depth > MAX_DEPTH) return;
    if (ts.isParenthesizedTypeNode(node) || ts.isTypeOperatorNode(node)) {
      named(node.type, depth + 1, found);
    } else if (ts.isArrayTypeNode(node)) {
      named(node.elementType, depth + 1, found);
    } else if (ts.isUnionTypeNode(node) || ts.isIntersectionTypeNode(node)) {
      node.types.forEach((t) => named(t, depth + 1, found));
    } else if (ts.isTypeLiteralNode(node)) {
      // An inline object type carries the types of its members.
      node.members.forEach((m) => m.type && named(m.type, depth + 1, found));
    } else if (ts.isTypeReferenceNode(node)) {
      const written = ts.isIdentifier(node.typeName)
        ? node.typeName.text
        : node.typeName.right.text;
      let symbol = checker.getSymbolAtLocation(node.typeName);
      if (symbol && symbol.flags & ts.SymbolFlags.Alias) {
        try {
          symbol = checker.getAliasedSymbol(symbol);
        } catch (error) {
          symbol = undefined;
        }
      }
      const declarations = ((symbol && symbol.declarations) || []).filter(
        (d) =>
          (ts.isClassDeclaration(d) ||
            ts.isInterfaceDeclaration(d) ||
            ts.isTypeAliasDeclaration(d) ||
            ts.isEnumDeclaration(d)) &&
          d.name,
      );
      const local = declarations.filter((d) => {
        const where = relative(d.getSourceFile().fileName);
        return !where.startsWith("..") && !where.split("/").includes("node_modules");
      });
      if (local.length > 0) {
        for (const d of local) {
          const source = d.getSourceFile();
          const at = source.getLineAndCharacterOfPosition(d.name.getStart(source));
          const lineStart = source.getPositionOfLineAndCharacter(at.line, 0);
          const bytes = byteOffsets(source.text, source.fileName);
          found.push({
            file: relative(source.fileName),
            name: d.name.text,
            line: at.line + 1,
            column: bytes(d.name.getStart(source)) - bytes(lineStart),
          });
        }
      } else if (declarations.length === 0 && !WRAPPERS.has(written)) {
        // Neither the repository's nor a library's: a name the compiler
        // cannot resolve (a workspace package that is not installed, say).
        unresolved += 1;
      }
      // Type arguments are carried too: Promise<T>, Page<T>, Map<K, V>.
      (node.typeArguments || []).forEach((t) => named(t, depth + 1, found));
    }
  }
  // The repository's declarations of a symbol's class, interface, type
  // alias or enum, as named() records them.
  function declared(symbol, found) {
    if (symbol && symbol.flags & ts.SymbolFlags.Alias) {
      try {
        symbol = checker.getAliasedSymbol(symbol);
      } catch (error) {
        return false;
      }
    }
    const local = ((symbol && symbol.declarations) || []).filter((d) => {
      if (
        !(
          ts.isClassDeclaration(d) ||
          ts.isInterfaceDeclaration(d) ||
          ts.isTypeAliasDeclaration(d) ||
          ts.isEnumDeclaration(d)
        ) ||
        !d.name
      ) {
        return false;
      }
      const where = relative(d.getSourceFile().fileName);
      return !where.startsWith("..") && !where.split("/").includes("node_modules");
    });
    for (const d of local) {
      const source = d.getSourceFile();
      const at = source.getLineAndCharacterOfPosition(d.name.getStart(source));
      const lineStart = source.getPositionOfLineAndCharacter(at.line, 0);
      const bytes = byteOffsets(source.text, source.fileName);
      found.push({
        file: relative(source.fileName),
        name: d.name.text,
        line: at.line + 1,
        column: bytes(d.name.getStart(source)) - bytes(lineStart),
      });
    }
    return local.length > 0;
  }
  // The types a value carries: the type written where its name or member
  // is declared, else the checker's type of it (a class instance, a value
  // inferred from a call); object and array literals carry the types of
  // their members and elements, `x as T` carries T.
  function carried(expression, depth, found) {
    let node = expression;
    if (!node || depth > MAX_DEPTH) return;
    while (ts.isParenthesizedExpression(node) || ts.isNonNullExpression(node)) {
      node = node.expression;
    }
    if (ts.isAsExpression(node) || ts.isSatisfiesExpression(node)) {
      named(node.type, depth + 1, found);
      return;
    }
    if (ts.isObjectLiteralExpression(node)) {
      for (const p of node.properties) {
        if (ts.isPropertyAssignment(p)) carried(p.initializer, depth + 1, found);
        else if (ts.isShorthandPropertyAssignment(p)) carried(p.name, depth + 1, found);
        else if (ts.isSpreadAssignment(p)) carried(p.expression, depth + 1, found);
      }
      return;
    }
    if (ts.isArrayLiteralExpression(node)) {
      node.elements.forEach((e) => carried(e, depth + 1, found));
      return;
    }
    if (ts.isElementAccessExpression(node)) {
      // rs[0] carries what rs's elements carry.
      carried(node.expression, depth + 1, found);
      return;
    }
    if (ts.isIdentifier(node)) {
      // r in rs.map((r) => ...): with noLib the checker cannot type an
      // array method's callback parameter, so it carries what rs carries.
      const symbol = checker.getSymbolAtLocation(node);
      const parameter = symbol && symbol.valueDeclaration;
      if (
        parameter &&
        ts.isParameter(parameter) &&
        !parameter.type &&
        parameter.parent.parameters[0] === parameter &&
        (ts.isArrowFunction(parameter.parent) ||
          ts.isFunctionExpression(parameter.parent)) &&
        ts.isCallExpression(parameter.parent.parent) &&
        parameter.parent.parent.arguments[0] === parameter.parent &&
        ts.isPropertyAccessExpression(parameter.parent.parent.expression) &&
        ARRAY_CALLBACKS.has(parameter.parent.parent.expression.name.text)
      ) {
        carried(parameter.parent.parent.expression.expression, depth + 1, found);
        return;
      }
    }
    if (ts.isIdentifier(node) || ts.isPropertyAccessExpression(node)) {
      const parent = node.parent;
      const symbol =
        parent && ts.isShorthandPropertyAssignment(parent) && parent.name === node
          ? checker.getShorthandAssignmentValueSymbol(parent)
          : checker.getSymbolAtLocation(node);
      const declaration = symbol && symbol.valueDeclaration;
      if (declaration && declaration.type) {
        const kind = declaration.type.kind;
        if (kind === ts.SyntaxKind.AnyKeyword || kind === ts.SyntaxKind.UnknownKeyword) {
          // Written as any: what it carries is not known.
          unresolved += 1;
          return;
        }
        named(declaration.type, depth + 1, found);
        return;
      }
    }
    if (ts.isNewExpression(node)) {
      declared(checker.getSymbolAtLocation(node.expression), found);
      return;
    }
    const type = checker.getTypeAtLocation(node);
    if (type.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown)) {
      // The checker cannot type it (any, or a name it cannot resolve).
      unresolved += 1;
      return;
    }
    (type.isUnion() ? type.types : [type]).forEach((t) => {
      if (declared(t.aliasSymbol || t.getSymbol(), found)) return;
      if (t.flags & ts.TypeFlags.Object && t.objectFlags & ts.ObjectFlags.Reference) {
        checker.getTypeArguments(t).forEach((a) => {
          declared(a.aliasSymbol || a.getSymbol(), found);
        });
      }
    });
  }
  // What a call sends: the data of `.emit(pattern, data)` and
  // `.send(pattern, data)` (a NestJS ClientProxy's), and what
  // `JSON.stringify(value)` serializes (a kafkajs message's value). Which
  // calls are a broker's is told where the sites are read (messaging.py).
  function sent(node) {
    if (!ts.isCallExpression(node) || !ts.isPropertyAccessExpression(node.expression)) {
      return undefined;
    }
    const name = node.expression.name.text;
    if ((name === "emit" || name === "send") && node.arguments.length >= 2) {
      return node.arguments[1];
    }
    const owner = node.expression.expression;
    if (
      name === "stringify" &&
      ts.isIdentifier(owner) &&
      owner.text === "JSON" &&
      node.arguments.length >= 1
    ) {
      return node.arguments[0];
    }
    return undefined;
  }
  // The 1-based line of a node's start.
  function lineOf(node) {
    return file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1;
  }
  // The fields of a class, an interface or an object type alias: for each
  // property with a written type (a class's parameter properties too), the
  // repository's declarations of the named types it carries, as named()
  // reads them; and the declarations of the classes and interfaces it
  // extends or implements (name null).
  function fields(node) {
    if (!node.name) return;
    const members = [];
    const field = (name, at, type) => {
      const found = [];
      unresolved = 0;
      try {
        named(type, 0, found);
      } catch (error) {
        unresolved += 1;
      }
      if (found.length > 0 || unresolved > 0) {
        members.push({ name, line: lineOf(at), types: found, unresolved });
      }
    };
    for (const clause of node.heritageClauses || []) {
      for (const base of clause.types) {
        const found = [];
        try {
          declared(checker.getSymbolAtLocation(base.expression), found);
        } catch (error) {
          // not resolved: no base
        }
        (base.typeArguments || []).forEach((t) => named(t, 0, found));
        if (found.length > 0) {
          members.push({ name: null, line: lineOf(base), types: found, unresolved: 0 });
        }
      }
    }
    const list = ts.isTypeAliasDeclaration(node)
      ? ts.isTypeLiteralNode(node.type)
        ? node.type.members
        : []
      : node.members;
    for (const member of list) {
      if (
        (ts.isPropertyDeclaration(member) || ts.isPropertySignature(member)) &&
        member.type &&
        member.name &&
        (ts.isIdentifier(member.name) || ts.isStringLiteral(member.name))
      ) {
        const static_ = (ts.getModifiers(member) || []).some(
          (m) => m.kind === ts.SyntaxKind.StaticKeyword,
        );
        if (!static_) field(member.name.text, member, member.type);
      } else if (ts.isConstructorDeclaration(member)) {
        for (const p of member.parameters) {
          if (
            ts.isParameterPropertyDeclaration(p, member) &&
            p.type &&
            ts.isIdentifier(p.name)
          ) {
            field(p.name.text, p, p.type);
          }
        }
      }
    }
    if (members.length > 0) {
      emit({ file: out, name: node.name.text, line: lineOf(node.name), fields: members });
    }
  }
  function visit(node) {
    if (
      ts.isClassDeclaration(node) ||
      ts.isInterfaceDeclaration(node) ||
      ts.isTypeAliasDeclaration(node)
    ) {
      fields(node);
    }
    const value = sent(node);
    if (value) {
      const found = [];
      unresolved = 0;
      try {
        carried(value, 0, found);
      } catch (error) {
        unresolved += 1;
      }
      if (found.length > 0 || unresolved > 0) {
        emit({
          file: out,
          start: offset(value.getStart(file)),
          end: offset(value.end),
          types: found,
          unresolved,
        });
      }
    }
    if (
      (ts.isMethodDeclaration(node) || ts.isConstructorDeclaration(node)) &&
      ts.canHaveDecorators(node)
    ) {
      const decorated =
        (ts.getDecorators(node) || []).length > 0 ||
        node.parameters.some((p) => (ts.getDecorators(p) || []).length > 0);
      if (decorated) {
        const written = node.parameters.map((p) => p.type);
        if (node.type) written.push(node.type);
        for (const type of written) {
          if (!type) continue;
          const found = [];
          unresolved = 0;
          try {
            named(type, 0, found);
          } catch (error) {
            unresolved += 1;
          }
          if (found.length > 0 || unresolved > 0) {
            emit({
              file: out,
              start: offset(type.getStart(file)),
              end: offset(type.end),
              types: found,
              unresolved,
            });
          }
        }
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(file);
}

// The module an identifier is bound to, as the checker resolves it: an
// import (namespace, default or named) or a const/var initialized with
// require("m"), through local aliases. {module, name}: name is the
// imported name, null for the module itself; null when not bound to one.
function moduleOf(checker, node, depth) {
  if (!node || depth > 6) return null;
  if (ts.isPropertyAccessExpression(node)) {
    const base = moduleOf(checker, node.expression, depth + 1);
    return base && base.name === null ? { module: base.module, name: node.name.text } : null;
  }
  if (!ts.isIdentifier(node)) return null;
  const symbol = checker.getSymbolAtLocation(node);
  const declaration = symbol && symbol.declarations && symbol.declarations[0];
  if (!declaration) return null;
  if (ts.isNamespaceImport(declaration) || ts.isImportClause(declaration)) {
    const spec = declaration.parent.parent.moduleSpecifier || (declaration.parent && declaration.parent.moduleSpecifier);
    const literal = ts.isImportClause(declaration) ? declaration.parent.moduleSpecifier : spec;
    if (!literal || !ts.isStringLiteral(literal)) return null;
    return { module: literal.text, name: ts.isImportClause(declaration) ? "default" : null };
  }
  if (ts.isImportSpecifier(declaration)) {
    const literal = declaration.parent.parent.parent.moduleSpecifier;
    if (!literal || !ts.isStringLiteral(literal)) return null;
    return { module: literal.text, name: (declaration.propertyName || declaration.name).text };
  }
  if (ts.isVariableDeclaration(declaration) && declaration.initializer) {
    const init = declaration.initializer;
    if (
      ts.isCallExpression(init) &&
      ts.isIdentifier(init.expression) &&
      init.expression.text === "require" &&
      init.arguments.length === 1 &&
      ts.isStringLiteral(init.arguments[0])
    ) {
      return ts.isIdentifier(declaration.name) ? { module: init.arguments[0].text, name: null } : null;
    }
    if (ts.isIdentifier(init) || ts.isPropertyAccessExpression(init)) {
      return moduleOf(checker, init, depth + 1);
    }
  }
  return null;
}

const PROTOBUF_ES = "@bufbuild/protobuf";
const CODEGEN = new Set(["@bufbuild/protobuf/codegenv1", "@bufbuild/protobuf/codegenv2"]);

// The protobuf types generated code declares, from the generators' own
// registrations:
//   - protoc-gen-js (google-protobuf; grpc-web and grpc-tools use it):
//     goog.exportSymbol("proto.<full name>", ...), goog bound to
//     require("google-protobuf");
//   - protobuf-es 2: const f = fileDesc("<base64 FileDescriptorProto>"),
//     and const XSchema = messageDesc(f, i, ...) / enumDesc(f, i, ...),
//     whose indexes walk the descriptor; type X = Message<"<full name>">;
//   - protobuf-es 1: a class extending Message with
//     static readonly typeName = "<full name>", and
//     proto3.util.setEnumType(E, "<full name>", ...);
//   - ts-proto: export const protobufPackage = "<package>".
function protoFacts(checker, file, out) {
  function line(node) {
    return file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1;
  }
  function declared(node) {
    // The declaration an identifier or type reference names: (file, name, line).
    const symbol = node && checker.getSymbolAtLocation(node);
    let target = symbol;
    if (target && target.flags & ts.SymbolFlags.Alias) target = checker.getAliasedSymbol(target);
    const declaration = target && target.declarations && target.declarations[0];
    if (!declaration || !declaration.name) return null;
    const source = declaration.getSourceFile();
    return {
      file: relative(source.fileName),
      name: declaration.name.getText(source),
      line: source.getLineAndCharacterOfPosition(declaration.name.getStart(source)).line + 1,
    };
  }
  function emitProto(item) {
    emit({ file: out, proto: item });
  }
  function indexes(args) {
    const path = [];
    for (const arg of args) {
      if (!ts.isNumericLiteral(arg)) return null;
      path.push(Number(arg.text));
    }
    return path;
  }
  function visit(node) {
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)) {
      const method = node.expression.name.text;
      const base = moduleOf(checker, node.expression.expression, 0);
      const first = node.arguments[0];
      if (
        method === "exportSymbol" &&
        base &&
        base.module === "google-protobuf" &&
        base.name === null &&
        first &&
        ts.isStringLiteral(first) &&
        first.text.startsWith("proto.")
      ) {
        emitProto({ generator: "protoc-gen-js", full: first.text.slice(6), line: line(node) });
      }
      if (method === "setEnumType" && node.arguments.length >= 2 && ts.isStringLiteral(node.arguments[1])) {
        const util = node.expression.expression;
        const owner = ts.isPropertyAccessExpression(util) ? moduleOf(checker, util.expression, 0) : null;
        const target = declared(first);
        if (owner && owner.module === PROTOBUF_ES && target) {
          emitProto({ generator: "protobuf-es", full: node.arguments[1].text, kind: "enum", ...target });
        }
      }
    }
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      const init = node.initializer;
      if (ts.isCallExpression(init)) {
        const callee = moduleOf(checker, init.expression, 0);
        if (callee && CODEGEN.has(callee.module)) {
          const [first, ...rest] = init.arguments;
          if (callee.name === "fileDesc" && first && ts.isStringLiteral(first)) {
            emitProto({ generator: "protobuf-es", descriptor: first.text, name: node.name.text, line: line(node.name) });
          }
          if ((callee.name === "messageDesc" || callee.name === "enumDesc") && first && ts.isIdentifier(first)) {
            const path = indexes(rest);
            const owner = declared(first);
            if (path && owner) {
              const kind = callee.name === "messageDesc" ? "message" : "enum";
              const item = { generator: "protobuf-es", kind, owner, path, name: node.name.text, line: line(node.name) };
              // GenMessage<X> / GenEnum<X>: the generated type X too.
              const type = node.type;
              if (type && ts.isTypeReferenceNode(type) && type.typeArguments && type.typeArguments.length >= 1) {
                const argument = type.typeArguments[0];
                const named = ts.isTypeReferenceNode(argument) ? declared(argument.typeName) : null;
                if (named) item.type = named;
              }
              emitProto(item);
            }
          }
        }
      }
      if (
        ts.isStringLiteral(init) &&
        node.name.text === "protobufPackage" &&
        ts.isVariableDeclarationList(node.parent) &&
        node.parent.flags & ts.NodeFlags.Const &&
        ts.isVariableStatement(node.parent.parent) &&
        (node.parent.parent.modifiers || []).some((m) => m.kind === ts.SyntaxKind.ExportKeyword)
      ) {
        emitProto({ generator: "ts-proto", package: init.text, line: line(node.name) });
      }
    }
    if (ts.isTypeAliasDeclaration(node)) {
      const parts = ts.isIntersectionTypeNode(node.type) ? node.type.types : [node.type];
      for (const part of parts) {
        if (!ts.isTypeReferenceNode(part) || !part.typeArguments || part.typeArguments.length !== 1) continue;
        const ref = moduleOf(checker, part.typeName, 0);
        const argument = part.typeArguments[0];
        if (
          ref &&
          ref.module === PROTOBUF_ES &&
          ref.name === "Message" &&
          ts.isLiteralTypeNode(argument) &&
          ts.isStringLiteral(argument.literal)
        ) {
          emitProto({ generator: "protobuf-es", kind: "message", full: argument.literal.text, name: node.name.text, line: line(node.name), file: out });
        }
      }
    }
    if (ts.isClassDeclaration(node) && node.name) {
      const extendsMessage = (node.heritageClauses || []).some(
        (clause) =>
          clause.token === ts.SyntaxKind.ExtendsKeyword &&
          clause.types.some((t) => {
            const ref = moduleOf(checker, t.expression, 0);
            return ref && ref.module === PROTOBUF_ES && ref.name === "Message";
          }),
      );
      if (extendsMessage) {
        for (const member of node.members) {
          if (
            ts.isPropertyDeclaration(member) &&
            member.name &&
            ts.isIdentifier(member.name) &&
            member.name.text === "typeName" &&
            (member.modifiers || []).some((m) => m.kind === ts.SyntaxKind.StaticKeyword) &&
            member.initializer &&
            ts.isStringLiteral(member.initializer)
          ) {
            emitProto({ generator: "protobuf-es", kind: "message", full: member.initializer.text, name: node.name.text, line: line(node.name), file: out });
          }
        }
      }
    }
    ts.forEachChild(node, visit);
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
    protoFacts(checker, file, rel);
  }
}
