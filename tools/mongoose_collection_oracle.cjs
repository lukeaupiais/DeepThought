// Independent oracle for the collection each Mongoose model maps to.
//
// Finds model declarations with the TypeScript compiler's parser and names
// their collections with Mongoose's own rules. It never runs the app or
// Mongoose: the pluralize rules and uncountable words are read from the
// source text of the pluralize module of the Mongoose version the app's
// lockfile pins (lib/helpers/pluralize.js from Mongoose 6 on, the
// mongoose-legacy-pluralize package before), and applied here.
//
// A declaration is `model('Name', schema[, collection])` called on the
// mongoose module (default, namespace or `require` binding, or a parameter
// typed `typeof import('mongoose')`), on a connection made from it
// (`createConnection`, `connection`, `useDb`), or the `model` named import.
// Its collection is the literal third argument, else the schema's literal
// `collection` option (the schema written inline, held in a variable, or
// imported from a repository file), else the model name through Mongoose's
// toCollectionName. A discriminator (`Base.discriminator('Name', schema)`)
// shares the collection of a base declared in the same file. Anything the
// oracle cannot fix statically is listed under `not_counted` with a reason.
//
// Usage: node tools/mongoose_collection_oracle.cjs ROOT TYPESCRIPT_DIR \
//            PLURALIZE_JS MONGOOSE_VERSION OUTPUT.json
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const [workspace, typescriptDir, pluralizeFile, version, output] = process.argv.slice(2);
if (!output) throw Error("usage: ROOT TYPESCRIPT_DIR PLURALIZE_JS MONGOOSE_VERSION OUTPUT.json");
const root = fs.realpathSync(workspace);
const ts = require(path.resolve(typescriptDir));

// --- Mongoose's naming rules, read from its source text -----------------
function readRules(file) {
  const text = fs.readFileSync(file, "utf8");
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
  const lists = {};
  (function walk(node) {
    if (
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
      ts.isPropertyAccessExpression(node.left) &&
      node.left.expression.getText(source) === "exports" &&
      ts.isArrayLiteralExpression(node.right)
    ) {
      lists[node.left.name.text] = node.right.elements;
    }
    ts.forEachChild(node, walk);
  })(source);
  if (!lists.pluralization || !lists.uncountables) throw Error(`no rules in ${file}`);
  const rules = lists.pluralization.map((pair) => {
    const [pattern, replacement] = pair.elements;
    const literal = pattern.getText(source);
    const cut = literal.lastIndexOf("/");
    return [new RegExp(literal.slice(1, cut), literal.slice(cut + 1)), replacement.text];
  });
  const uncountables = lists.uncountables.map((e) => e.text);
  // The function must be the one the rules were written for.
  for (const part of ["toLowerCase()", "uncountables.indexOf(str)", "found[0][0], found[0][1]"]) {
    if (!text.includes(part)) throw Error(`unexpected pluralize body in ${file}: ${part}`);
  }
  return { rules, uncountables };
}
const { rules, uncountables } = readRules(pluralizeFile);
function pluralize(str) {
  str = str.toLowerCase();
  if (uncountables.includes(str)) return str;
  const found = rules.filter((rule) => str.match(rule[0]));
  return found[0] ? str.replace(found[0][0], found[0][1]) : str;
}
function toCollectionName(name) {
  if (name === "system.profile" || name === "system.indexes") return name;
  return pluralize(name);
}

// --- Files ---------------------------------------------------------------
const EXTENSIONS = [".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"];
const listed = execFileSync("git", ["-C", root, "ls-files", "-z"], { maxBuffer: 1 << 28 })
  .toString("utf8")
  .split("\0")
  .filter(
    (f) =>
      f &&
      EXTENSIONS.includes(path.extname(f)) &&
      !f.endsWith(".d.ts") &&
      !f.split("/").includes("node_modules")
  );
const isTest = (f) =>
  f.split("/").some((part) => ["test", "tests", "__tests__", "spec"].includes(part)) ||
  /\.(test|spec)\.[^/]+$/.test(f);
const parsed = new Map();
function parse(file) {
  if (!parsed.has(file)) {
    const text = fs.readFileSync(path.join(root, file), "utf8");
    const kind = /\.[mc]?tsx?$/.test(file) ? ts.ScriptKind.TSX : ts.ScriptKind.JSX;
    const scriptKind = file.endsWith(".ts") || file.endsWith(".mts") || file.endsWith(".cts")
      ? ts.ScriptKind.TS
      : kind;
    parsed.set(file, ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, scriptKind));
  }
  return parsed.get(file);
}
const strip = (node) => {
  while (
    node &&
    (ts.isParenthesizedExpression(node) ||
      ts.isAsExpression(node) ||
      ts.isNonNullExpression(node) ||
      ts.isAwaitExpression(node) ||
      (ts.isSatisfiesExpression && ts.isSatisfiesExpression(node)) ||
      ts.isTypeAssertionExpression(node))
  ) {
    node = node.expression;
  }
  return node;
};
const literal = (node) => {
  node = strip(node);
  return node && (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node))
    ? node.text
    : undefined;
};
// A string literal, or a const bound to one in scope.
const fixed = (node) => {
  const value = literal(node);
  if (value !== undefined) return value;
  node = strip(node);
  if (node && ts.isIdentifier(node)) {
    const b = bindingOf(node);
    if (b && b.kind === "init" && ts.isVariableDeclarationList(b.decl.parent) &&
        b.decl.parent.flags & ts.NodeFlags.Const) {
      return literal(b.node);
    }
  }
  return undefined;
};
const lineOf = (node) =>
  node.getSourceFile().getLineAndCharacterOfPosition(node.getStart()).line + 1;

// --- Module resolution: relative paths and tsconfig paths ---------------
const configs = new Map();
function tsconfigFor(dir) {
  if (configs.has(dir)) return configs.get(dir);
  let found = null;
  const file = path.join(root, dir, "tsconfig.json");
  if (fs.existsSync(file)) {
    const raw = ts.readConfigFile(file, ts.sys.readFile);
    if (!raw.error) found = ts.parseJsonConfigFileContent(raw.config, ts.sys, path.dirname(file)).options;
  } else if (dir && dir !== ".") {
    found = tsconfigFor(path.dirname(dir) === "." ? "" : path.dirname(dir));
  }
  configs.set(dir, found);
  return found;
}
const known = new Set(listed);
function candidates(base) {
  const out = [base];
  const ext = path.extname(base);
  if ([".js", ".mjs", ".cjs", ".jsx"].includes(ext)) {
    const stem = base.slice(0, -ext.length);
    out.push(stem + ".ts", stem + ".tsx", stem + ".mts", stem + ".cts");
  }
  for (const e of EXTENSIONS) out.push(base + e);
  for (const e of EXTENSIONS) out.push(base + "/index" + e);
  return out.map((p) => path.normalize(p).split(path.sep).join("/"));
}
function resolveModule(from, specifier) {
  let bases = [];
  if (specifier.startsWith(".")) {
    bases = [path.join(path.dirname(from), specifier)];
  } else {
    const options = tsconfigFor(path.dirname(from));
    if (options && options.paths) {
      const baseUrl = options.baseUrl || options.pathsBasePath || root;
      for (const [pattern, targets] of Object.entries(options.paths)) {
        const star = pattern.indexOf("*");
        let rest = null;
        if (star < 0 && pattern === specifier) rest = "";
        else if (
          star >= 0 &&
          specifier.startsWith(pattern.slice(0, star)) &&
          specifier.endsWith(pattern.slice(star + 1))
        ) {
          rest = specifier.slice(star, specifier.length - (pattern.length - star - 1));
        }
        if (rest === null) continue;
        for (const target of targets) {
          bases.push(path.relative(root, path.resolve(baseUrl, target.replace("*", rest))));
        }
      }
    }
  }
  for (const base of bases) {
    for (const c of candidates(base)) if (known.has(c)) return c;
  }
  return null;
}

// --- Bindings ------------------------------------------------------------
// A binding is {kind: "mongoose"} (the module), {kind: "named", name} (a
// named export of mongoose), {kind: "init", node} (a variable initializer),
// {kind: "import", file, name} (a repository module's export), or null.
const requireOf = (node) => {
  node = strip(node);
  return node &&
    ts.isCallExpression(node) &&
    ts.isIdentifier(node.expression) &&
    node.expression.text === "require" &&
    node.arguments.length === 1
    ? literal(node.arguments[0])
    : undefined;
};
function moduleBinding(file, specifier, name) {
  if (specifier === "mongoose") return name === "*" || name === "default" ? { kind: "mongoose" } : { kind: "named", name };
  const target = resolveModule(file, specifier);
  return target ? { kind: "import", file: target, name } : null;
}
function bindingInDeclarations(file, statements, name) {
  for (const statement of statements) {
    if (ts.isImportDeclaration(statement) && statement.importClause) {
      const specifier = literal(statement.moduleSpecifier);
      const clause = statement.importClause;
      if (clause.name && clause.name.text === name) return moduleBinding(file, specifier, "default");
      const bindings = clause.namedBindings;
      if (bindings && ts.isNamespaceImport(bindings) && bindings.name.text === name) {
        return moduleBinding(file, specifier, "*");
      }
      if (bindings && ts.isNamedImports(bindings)) {
        for (const element of bindings.elements) {
          if (element.name.text === name) {
            return moduleBinding(file, specifier, (element.propertyName || element.name).text);
          }
        }
      }
    }
    if (ts.isImportEqualsDeclaration(statement) && statement.name.text === name) {
      const ref = statement.moduleReference;
      if (ts.isExternalModuleReference(ref)) return moduleBinding(file, literal(ref.expression), "default");
    }
    const declarations = ts.isVariableStatement(statement)
      ? statement.declarationList.declarations
      : [];
    for (const declaration of declarations) {
      const found = fromVariable(file, declaration, name);
      if (found !== undefined) return found;
    }
    if (
      (ts.isFunctionDeclaration(statement) || ts.isClassDeclaration(statement)) &&
      statement.name &&
      statement.name.text === name
    ) {
      return null;
    }
  }
  return undefined;
}
function fromVariable(file, declaration, name) {
  if (ts.isIdentifier(declaration.name)) {
    if (declaration.name.text !== name) return undefined;
    const specifier = requireOf(declaration.initializer);
    if (specifier !== undefined) return moduleBinding(file, specifier, "default");
    return declaration.initializer
      ? { kind: "init", node: declaration.initializer, file, decl: declaration }
      : null;
  }
  if (ts.isObjectBindingPattern(declaration.name)) {
    for (const element of declaration.name.elements) {
      if (!ts.isIdentifier(element.name) || element.name.text !== name || element.dotDotDotToken) continue;
      const key = (element.propertyName || element.name).getText();
      const specifier = requireOf(declaration.initializer);
      if (specifier !== undefined) return moduleBinding(file, specifier, key);
      const base = valueOf(file, declaration.initializer);
      if (base && base.kind === "mongoose") return { kind: "named", name: key };
      return null;
    }
  }
  return undefined;
}
// The binding of identifier `id`, searched from its scope outward.
function bindingOf(id) {
  const file = id.getSourceFile().fileName;
  const name = id.text;
  for (let node = id.parent; node; node = node.parent) {
    if (ts.isFunctionLike(node)) {
      for (const parameter of node.parameters || []) {
        if (ts.isIdentifier(parameter.name) && parameter.name.text === name) {
          return parameterBinding(parameter);
        }
      }
    }
    const statements = ts.isSourceFile(node) || ts.isBlock(node) || ts.isModuleBlock(node)
      ? node.statements
      : null;
    if (statements) {
      const found = bindingInDeclarations(file, statements, name);
      if (found !== undefined) return found;
    }
  }
  return null;
}
function parameterBinding(parameter) {
  const type = parameter.type;
  if (type && ts.isTypeQueryNode(type)) {
    // typeof mongoose
    const id = type.exprName;
    if (ts.isIdentifier(id)) {
      const b = bindingOf(id);
      if (b && b.kind === "mongoose") return b;
    }
  }
  if (type && ts.isImportTypeNode(type) && type.isTypeOf) {
    const arg = type.argument;
    if (ts.isLiteralTypeNode(arg) && arg.literal.text === "mongoose" && !type.qualifier) {
      return { kind: "mongoose" };
    }
  }
  if (type && ts.isTypeReferenceNode(type) && ts.isIdentifier(type.typeName)) {
    const b = bindingOf(type.typeName);
    if (b && b.kind === "named" && b.name === "Mongoose") return { kind: "mongoose" };
    if (b && b.kind === "named" && b.name === "Connection") return { kind: "connection" };
  }
  return null;
}
// What an expression evaluates to, as far as this oracle follows it.
function valueOf(file, node, depth = 0) {
  node = strip(node);
  if (!node || depth > 20) return null;
  if (ts.isIdentifier(node)) {
    const b = bindingOf(node);
    if (!b) return null;
    if (b.kind === "init") return valueOf(b.file, b.node, depth + 1);
    if (b.kind === "import") return exported(b.file, b.name, depth + 1);
    if (b.kind === "named" && b.name === "connection") return { kind: "connection" };
    return b;
  }
  if (ts.isPropertyAccessExpression(node)) {
    const base = valueOf(file, node.expression, depth + 1);
    if (base && base.kind === "mongoose") {
      if (node.name.text === "connection") return { kind: "connection" };
      if (node.name.text === "default" || node.name.text === "mongoose") return base;
      return { kind: "named", name: node.name.text };
    }
    return null;
  }
  if (ts.isCallExpression(node)) {
    const callee = strip(node.expression);
    if (ts.isPropertyAccessExpression(callee) && callee.name.text === "asPromise") {
      return valueOf(file, callee.expression, depth + 1);
    }
    const f = valueOf(file, callee, depth + 1);
    if (f && f.kind === "named" && f.name === "createConnection") return { kind: "connection" };
    if (ts.isPropertyAccessExpression(callee) && callee.name.text === "useDb") {
      const base = valueOf(file, callee.expression, depth + 1);
      if (base && (base.kind === "connection" || base.kind === "mongoose")) return { kind: "connection" };
    }
    const declared = declaration(node, depth + 1);
    if (declared) return { kind: "model", declared };
    return null;
  }
  return { kind: "expression", node, file };
}
// The value a repository module exports under `name`.
function exported(file, name, depth) {
  const source = parse(file);
  for (const statement of source.statements) {
    if (ts.isExportAssignment(statement) && name === "default") {
      return valueOf(file, statement.expression, depth + 1);
    }
    if (ts.isVariableStatement(statement) && statement.modifiers?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword)) {
      for (const d of statement.declarationList.declarations) {
        if (ts.isIdentifier(d.name) && d.name.text === name) return valueOf(file, d.initializer, depth + 1);
      }
    }
    if (ts.isExportDeclaration(statement) && statement.exportClause && ts.isNamedExports(statement.exportClause)) {
      for (const element of statement.exportClause.elements) {
        if (element.name.text !== name) continue;
        const local = (element.propertyName || element.name).text;
        if (statement.moduleSpecifier) {
          const target = resolveModule(file, literal(statement.moduleSpecifier));
          return target ? exported(target, local, depth + 1) : null;
        }
        const b = bindingInDeclarations(file, source.statements, local);
        if (!b) return null;
        if (b.kind === "init") return valueOf(b.file, b.node, depth + 1);
        if (b.kind === "import") return exported(b.file, b.name, depth + 1);
        return b;
      }
    }
    // CommonJS: module.exports = X, module.exports.name = X, exports.name = X
    if (ts.isExpressionStatement(statement) && ts.isBinaryExpression(statement.expression)) {
      const { left, right, operatorToken } = statement.expression;
      if (operatorToken.kind !== ts.SyntaxKind.EqualsToken) continue;
      const target = left.getText(source);
      if (target === "module.exports" && name === "default") return valueOf(file, right, depth + 1);
      if (target === `module.exports.${name}` || target === `exports.${name}`) {
        return valueOf(file, right, depth + 1);
      }
      if (target === "module.exports" && ts.isObjectLiteralExpression(right)) {
        for (const p of right.properties) {
          if (p.name && p.name.getText(source) === name) {
            if (ts.isShorthandPropertyAssignment(p)) return valueOf(file, p.name, depth + 1);
            if (ts.isPropertyAssignment(p)) return valueOf(file, p.initializer, depth + 1);
          }
        }
      }
    }
  }
  return null;
}

// --- Declarations --------------------------------------------------------
// A model declaration call, as {name, collection | reason}, or null.
function declaration(call, depth = 0) {
  const callee = strip(call.expression);
  const file = call.getSourceFile().fileName;
  let receiver = null;
  if (ts.isPropertyAccessExpression(callee) && callee.name.text === "model") {
    receiver = valueOf(file, callee.expression, depth + 1);
    if (!receiver || !["mongoose", "connection"].includes(receiver.kind)) {
      receiver = { kind: "unknown" };
    }
  } else if (ts.isIdentifier(callee)) {
    const b = bindingOf(callee);
    if (b && b.kind === "named" && b.name === "model") receiver = { kind: "mongoose" };
  }
  if (!receiver || call.arguments.length < 2) return null;
  const name = literal(call.arguments[0]);
  if (receiver.kind === "unknown") {
    return name === undefined ? null : { name, reason: "receiver is not a known mongoose binding" };
  }
  if (name === undefined) return { name: null, reason: "model name is not literal" };
  if (call.arguments.length >= 3) {
    const collection = fixed(call.arguments[2]);
    return collection === undefined
      ? { name, reason: "collection argument is not literal" }
      : { name, collection, how: "argument" };
  }
  const schema = schemaOptions(file, call.arguments[1], depth + 1);
  if (schema.reason) return { name, reason: schema.reason };
  if (schema.collection !== undefined) return { name, collection: schema.collection, how: "schema option" };
  return { name, collection: toCollectionName(name), how: "default" };
}
// The schema's options: {collection} when fixed, {} when it has none, or
// {reason} when the oracle cannot tell.
function schemaOptions(file, node, depth) {
  node = strip(node);
  if (ts.isIdentifier(node)) {
    const b = bindingOf(node);
    if (!b) return { reason: "schema binding not found" };
    let found;
    if (b.kind === "init") found = schemaOptions(b.file, b.node, depth + 1);
    else if (b.kind === "import") {
      const source = exportedNode(b.file, b.name, depth + 1);
      found = source ? schemaOptions(source.file, source.node, depth + 1) : { reason: "schema import not followed" };
    } else found = { reason: "schema is not a Schema" };
    if (!found.reason && b.kind === "init" && setsCollection(b.file, node.text)) {
      return { reason: "schema collection set after construction" };
    }
    return found;
  }
  // Schema(...) without `new` builds a schema too (it calls itself with new).
  if (ts.isNewExpression(node) || ts.isCallExpression(node)) {
    const ctor = valueOf(file, node.expression, depth + 1);
    if (!ctor || ctor.kind !== "named" || ctor.name !== "Schema") return { reason: "schema is not new Schema" };
    return collectionOption(file, node.arguments && node.arguments[1], depth + 1);
  }
  return { reason: "schema expression not read" };
}
function exportedNode(file, name, depth) {
  // Like exported(), but returns the initializer node (to read a Schema).
  const source = parse(file);
  const local = (n) => {
    const b = bindingInDeclarations(file, source.statements, n);
    if (b && b.kind === "init") return { file, node: b.node, name: n };
    if (b && b.kind === "import") return exportedNode(b.file, b.name, depth + 1);
    return null;
  };
  for (const statement of source.statements) {
    if (ts.isExportAssignment(statement) && name === "default") {
      const e = strip(statement.expression);
      return ts.isIdentifier(e) ? local(e.text) : { file, node: e };
    }
    if (ts.isVariableStatement(statement) && statement.modifiers?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword)) {
      for (const d of statement.declarationList.declarations) {
        if (ts.isIdentifier(d.name) && d.name.text === name) return { file, node: d.initializer, name };
      }
    }
    if (ts.isExportDeclaration(statement) && statement.exportClause && ts.isNamedExports(statement.exportClause)) {
      for (const element of statement.exportClause.elements) {
        if (element.name.text !== name) continue;
        const inner = (element.propertyName || element.name).text;
        if (statement.moduleSpecifier) {
          const target = resolveModule(file, literal(statement.moduleSpecifier));
          return target ? exportedNode(target, inner, depth + 1) : null;
        }
        return local(inner);
      }
    }
    if (ts.isExpressionStatement(statement) && ts.isBinaryExpression(statement.expression)) {
      const { left, right } = statement.expression;
      const target = left.getText(source);
      const r = strip(right);
      if (
        (target === "module.exports" && name === "default") ||
        target === `module.exports.${name}` ||
        target === `exports.${name}`
      ) {
        return ts.isIdentifier(r) ? local(r.text) : { file, node: r };
      }
    }
  }
  return null;
}
function setsCollection(file, name) {
  const text = parse(file).text;
  const escaped = name.replace(/[$]/g, "\\$");
  return new RegExp(`\\b${escaped}\\s*\\.\\s*set\\(\\s*['"\`]collection['"\`]`).test(text);
}
function collectionOption(file, node, depth) {
  node = strip(node);
  if (!node) return {};
  if (ts.isIdentifier(node)) {
    const b = bindingOf(node);
    return b && b.kind === "init" ? collectionOption(b.file, b.node, depth + 1) : { reason: "schema options not literal" };
  }
  if (!ts.isObjectLiteralExpression(node)) return { reason: "schema options not literal" };
  let result = {};
  for (const p of node.properties) {
    if (ts.isSpreadAssignment(p)) return { reason: "schema options spread" };
    if (!p.name || p.name.getText().replace(/['"]/g, "") !== "collection") continue;
    if (!ts.isPropertyAssignment(p)) return { reason: "collection option not literal" };
    const value = fixed(p.initializer);
    if (value === undefined) return { reason: "collection option not literal" };
    result = { collection: value };
  }
  return result;
}

// --- Walk ----------------------------------------------------------------
const models = [];
const notCounted = [];
const overrides = [];
for (const file of listed) {
  let source;
  try {
    source = parse(file);
  } catch (error) {
    notCounted.push({ site: file, reason: `unreadable: ${error.message}` });
    continue;
  }
  const local = new Map(); // variable name -> model entry, for discriminators
  (function walk(node) {
    if (ts.isCallExpression(node)) {
      const callee = strip(node.expression);
      if (ts.isPropertyAccessExpression(callee) && ["pluralize", "set"].includes(callee.name.text)) {
        const base = valueOf(file, callee.expression);
        const arg = node.arguments[0] && literal(node.arguments[0]);
        if (base && base.kind === "mongoose" && (callee.name.text === "pluralize" || arg === "pluralization")) {
          overrides.push(`${file}:${lineOf(node)}`);
        }
      }
      const found = declaration(node);
      let entry = null;
      if (found) {
        entry = { found, line: lineOf(node) };
      } else if (
        ts.isPropertyAccessExpression(callee) &&
        callee.name.text === "discriminator" &&
        node.arguments.length >= 2
      ) {
        const name = literal(node.arguments[0]);
        const base = strip(callee.expression);
        const parent = ts.isIdentifier(base) ? local.get(base.text) : undefined;
        if (name === undefined) entry = null;
        else if (parent && parent.found.collection !== undefined) {
          entry = { found: { name, collection: parent.found.collection, how: "discriminator" }, line: lineOf(node) };
        } else {
          entry = { found: { name, reason: "discriminator base not declared in the file" }, line: lineOf(node) };
        }
      }
      if (entry) {
        const key = `${file}::${entry.found.name}`;
        if (isTest(file)) notCounted.push({ class: key, line: entry.line, reason: "test source" });
        else if (entry.found.reason) notCounted.push({ class: key, line: entry.line, reason: entry.found.reason });
        else models.push({ class: key, table: entry.found.collection, line: entry.line, how: entry.found.how });
        let holder = node.parent;
        while (holder && (ts.isParenthesizedExpression(holder) || ts.isAsExpression(holder) || ts.isBinaryExpression(holder))) {
          holder = holder.parent;
        }
        if (holder && ts.isVariableDeclaration(holder) && ts.isIdentifier(holder.name)) {
          local.set(holder.name.text, entry);
        }
      }
    }
    ts.forEachChild(node, walk);
  })(source);
}
if (overrides.length) {
  // A global pluralize override changes every default name.
  for (const m of models.splice(0)) {
    if (m.how === "default" || m.how === "discriminator") {
      notCounted.push({ class: m.class, line: m.line, reason: "global pluralize override" });
    } else models.push(m);
  }
}
const byKey = (a, b) => (a.class || a.site).localeCompare(b.class || b.site) || a.line - b.line;
fs.writeFileSync(
  output,
  JSON.stringify(
    {
      source:
        `tools/mongoose_collection_oracle.cjs: model declarations read with the TypeScript parser, ` +
        `collections named by the pluralize rules of mongoose ${version} (${path.basename(path.dirname(pluralizeFile))}/${path.basename(pluralizeFile)}).`,
      mongoose: version,
      pluralize_overrides: overrides,
      models: models.sort(byKey),
      not_counted: notCounted.sort(byKey),
    },
    null,
    2
  ) + "\n"
);
console.log(`${models.length} models, ${notCounted.length} not counted, ${overrides.length} overrides`);
