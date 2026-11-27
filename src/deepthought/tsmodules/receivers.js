// The TypeScript checker's view of method calls (deepthought.ts_receivers):
// for each call, the declaration the call resolves to and the type
// arguments of its receiver's type. Nothing is emitted.
//
// argv: path of the pinned typescript package's lib/typescript.js.
// stdin: {"root": absolute workspace, "calls": [{"file", "line",
// "character"}]}, each the position (0-based, UTF-16) where a call's method
// name is written in a workspace-relative file.
// stdout: one JSON object per call the checker resolves:
//   {"file", "line", "character",
//    "owner": the name of the class or interface declaring the method,
//    "owner_file": absolute path of the file declaring it, symbolic links
//      resolved,
//    "receiver": [declaration sites of the receiver type's type arguments],
//    "argument": [declaration sites of the first argument's type],
//    "project": workspace-relative tsconfig.json or jsconfig.json, or null}
// A declaration site is {"file": absolute path, "line", "character"} of the
// declaration's name; a type argument that is no named class or interface
// gives none. Files are grouped into programs by project as
// tsmodules/resolve.js groups them.
"use strict";
const fs = require("fs");
const path = require("path");
const ts = require(process.argv[2]);

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const root = path.resolve(input.root);
const canonical = (p) => path.resolve(p);
const parsed = new Map();

function parse(config) {
  if (!parsed.has(config)) {
    const host = { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => {} };
    const result = ts.getParsedCommandLineOfConfigFile(config, {}, host);
    parsed.set(config, {
      config,
      options: result.options,
      references: result.projectReferences || [],
      files: new Set(result.fileNames.map(canonical)),
    });
  }
  return parsed.get(config);
}

function including(config, file, seen) {
  if (seen.has(config) || !fs.existsSync(config)) return null;
  seen.add(config);
  const project = parse(config);
  if (project.files.has(file)) return project;
  for (const reference of project.references) {
    let target = reference.path;
    if (!target.endsWith(".json")) target = path.join(target, "tsconfig.json");
    const found = including(canonical(target), file, seen);
    if (found) return found;
  }
  return null;
}

function projectOf(file) {
  let directory = path.dirname(file);
  for (;;) {
    for (const name of ["tsconfig.json", "jsconfig.json"]) {
      const found = including(path.join(directory, name), file, new Set());
      if (found) return found;
    }
    if (directory === root || path.dirname(directory) === directory) break;
    directory = path.dirname(directory);
  }
  return null;
}

const INFERRED = {
  target: ts.ScriptTarget.Latest,
  jsx: ts.JsxEmit.Preserve,
  allowJs: true,
  allowNonTsExtensions: true,
};

function site(declaration) {
  const name = ts.getNameOfDeclaration(declaration) || declaration;
  const source = declaration.getSourceFile();
  const start = source.getLineAndCharacterOfPosition(name.getStart(source));
  return {
    file: fs.realpathSync(source.fileName),
    line: start.line,
    character: start.character,
  };
}

function classSites(type) {
  // The named classes or interfaces a type stands for: the type itself, or
  // the instance type of ``typeof C``.
  if (!type) return [];
  const symbol = type.aliasSymbol || type.getSymbol();
  if (!symbol || !symbol.declarations) return [];
  return symbol.declarations
    .filter((d) => ts.isClassLike(d) || ts.isInterfaceDeclaration(d))
    .map(site);
}

function ownerArguments(checker, type, owner, seen) {
  // The type arguments the receiver's type gives the class or interface
  // declaring the method: its own (``Repository<User>``), or those of the
  // base type it extends (``class UserRepo extends Repository<User>``).
  if (!type || seen.has(type) || seen.size > 16) return [];
  seen.add(type);
  if (type.isUnion && type.isUnion()) {
    return type.types.flatMap((t) => ownerArguments(checker, t, owner, seen));
  }
  const symbol = type.getSymbol();
  if (
    symbol === owner &&
    type.flags & ts.TypeFlags.Object &&
    type.objectFlags & ts.ObjectFlags.Reference
  ) {
    return checker.getTypeArguments(type);
  }
  if (!(type.flags & ts.TypeFlags.Object)) return [];
  const target = type.objectFlags & ts.ObjectFlags.Reference ? type.target : type;
  if (!(target.objectFlags & (ts.ObjectFlags.Class | ts.ObjectFlags.Interface)))
    return [];
  return (checker.getBaseTypes(target) || []).flatMap((base) =>
    ownerArguments(checker, base, owner, seen),
  );
}

function innermost(source, position) {
  let found = source;
  (function visit(node) {
    if (node.getStart(source) <= position && position < node.getEnd()) {
      found = node;
      ts.forEachChild(node, visit);
    }
  })(source);
  return found;
}

const groups = new Map();
for (const call of input.calls) {
  const file = canonical(path.join(root, call.file));
  const project = projectOf(file);
  const key = project ? project.config : "";
  if (!groups.has(key)) groups.set(key, { project, calls: [] });
  groups.get(key).calls.push({ ...call, absolute: file });
}

const out = [];
for (const key of [...groups.keys()].sort()) {
  const { project, calls } = groups.get(key);
  const options = project ? project.options : INFERRED;
  const files = [...new Set(calls.map((c) => c.absolute))].sort();
  const program = ts.createProgram({ rootNames: files, options });
  const checker = program.getTypeChecker();
  for (const call of calls) {
    const source = program.getSourceFile(call.absolute);
    if (!source) continue;
    let position;
    try {
      position = source.getPositionOfLineAndCharacter(call.line, call.character);
    } catch (error) {
      continue;
    }
    const name = innermost(source, position);
    const access = name.parent;
    if (!access || !ts.isPropertyAccessExpression(access) || access.name !== name)
      continue;
    const expression = access.parent;
    if (!expression || !ts.isCallExpression(expression) || expression.expression !== access)
      continue;
    const signature = checker.getResolvedSignature(expression);
    const declaration = signature && signature.getDeclaration();
    if (!declaration || !declaration.parent) continue;
    const owner = declaration.parent;
    const ownerName = owner.name ? owner.name.getText(owner.getSourceFile()) : null;
    if (!ownerName) continue;
    const ownerSymbol = checker.getSymbolAtLocation(owner.name);
    const typeArguments = ownerArguments(
      checker,
      // ``this`` is a type parameter; its apparent type is the class.
      checker.getApparentType(checker.getTypeAtLocation(access.expression)),
      ownerSymbol,
      new Set(),
    );
    const first = expression.arguments[0];
    let argument = [];
    if (first) {
      const symbol = checker.getSymbolAtLocation(first);
      const target =
        symbol && symbol.flags & ts.SymbolFlags.Alias
          ? checker.getAliasedSymbol(symbol)
          : symbol;
      argument = ((target && target.declarations) || [])
        .filter((d) => ts.isClassLike(d))
        .map(site);
    }
    out.push(
      JSON.stringify({
        file: call.file,
        line: call.line,
        character: call.character,
        owner: ownerName,
        owner_file: fs.realpathSync(owner.getSourceFile().fileName),
        receiver: typeArguments.flatMap(classSites),
        argument,
        project: project
          ? path.relative(root, project.config).split(path.sep).join("/")
          : null,
      }),
    );
  }
}
process.stdout.write(out.join("\n") + (out.length ? "\n" : ""));
