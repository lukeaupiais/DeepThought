// The TypeScript compiler's module resolution for each import of the given
// files (deepthought.ts_modules). Nothing is type-checked or emitted.
//
// argv: path of the pinned typescript package's lib/typescript.js.
// stdin: {"root": absolute workspace, "files": [workspace-relative paths],
//         "components": {workspace-relative .vue path: {"text", "jsx"}}}.
// A Vue single-file component is read as its script (``text``, every byte
// outside its script blocks blanked; deepthought.vue_sfc): the program holds
// it as the TypeScript (or, with ``jsx``, TSX) file ``X.vue.ts`` beside it,
// so the compiler's own resolver resolves ``./X.vue`` and aliases to it, as
// Vue's language tools do. Every path written out names the component.
// stdout: one JSON object per line, per import the compiler records in a
// file (import, export ... from, side-effect import, import(), require() in
// JavaScript, import x = require()), with the file's project:
//   {"file", "line", "character", "specifier", "resolved", "project",
//    "options_from"}
// ``line`` and ``character`` (0-based, UTF-16) are where the specifier's
// string literal starts. ``resolved`` is an absolute path, symbolic links
// resolved (a workspace package linked into node_modules is its own file),
// or null.
// ``project`` is the workspace-relative tsconfig.json or jsconfig.json that
// includes the file, or null for a file no project includes. Such a file gets
// the options of the nearest config above it (``options_from``), JavaScript
// allowed, or, with none, the options TypeScript's language service gives a
// file outside every project.
"use strict";
const fs = require("fs");
const path = require("path");
const ts = require(process.argv[2]);

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const root = path.resolve(input.root);
const canonical = (p) => path.resolve(p);
const parsed = new Map();
const out = [];

// The script of each component, by the path of the file the program reads
// it as (X.vue.ts or X.vue.tsx).
const scripts = new Map();
const scriptOf = new Map(); // component path -> script path
for (const [relative, component] of Object.entries(input.components || {})) {
  const file = canonical(path.join(root, relative));
  const script = file + (component.jsx ? ".tsx" : ".ts");
  scripts.set(script, component.text);
  scriptOf.set(file, script);
}
// The path a file the program reads names: a component for its script,
// else the file itself, symbolic links resolved.
const real = (file) =>
  scripts.has(file) ? file.slice(0, file.lastIndexOf(".")) : fs.realpathSync(file);
const named = (file) => (scripts.has(file) ? real(file) : file);
const relativeName = (file) =>
  path.relative(root, named(file)).split(path.sep).join("/");
// Config includes match components (as Vue's language tools declare them).
const EXTRA_EXTENSIONS = [
  { extension: ".vue", isMixedContent: true, scriptKind: ts.ScriptKind.Deferred },
];

function hostFor(options) {
  const host = ts.createCompilerHost(options);
  const { fileExists, readFile, realpath } = host;
  host.fileExists = (file) => scripts.has(canonical(file)) || fileExists(file);
  host.readFile = (file) => {
    const script = scripts.get(canonical(file));
    return script === undefined ? readFile(file) : script;
  };
  host.realpath = (file) =>
    scripts.has(canonical(file)) ? file : realpath ? realpath(file) : file;
  return host;
}

function parse(config) {
  if (!parsed.has(config)) {
    const host = {
      ...ts.sys,
      onUnRecoverableConfigFileDiagnostic: () => {},
    };
    const result = ts.getParsedCommandLineOfConfigFile(
      config,
      {},
      host,
      undefined,
      undefined,
      EXTRA_EXTENSIONS,
    );
    // An unreadable config fails the run.
    parsed.set(config, {
      config,
      options: result.options,
      references: result.projectReferences || [],
      files: new Set(
        result.fileNames.map(canonical).map((f) => scriptOf.get(f) || f),
      ),
    });
  }
  return parsed.get(config);
}

function including(config, file, seen) {
  if (seen.has(config) || !fs.existsSync(config)) return null;
  seen.add(config);
  const project = parse(config);
  if (project.files.has(file)) return project;
  // A solution-style config includes its files through references.
  for (const reference of project.references) {
    let target = reference.path;
    if (!target.endsWith(".json")) target = path.join(target, "tsconfig.json");
    const found = including(canonical(target), file, seen);
    if (found) return found;
  }
  return null;
}

function nearestConfig(file) {
  // The nearest tsconfig.json, then jsconfig.json, directory by directory.
  let directory = path.dirname(file);
  for (;;) {
    for (const name of ["tsconfig.json", "jsconfig.json"]) {
      const config = path.join(directory, name);
      if (fs.existsSync(config)) return parse(config);
    }
    if (directory === root || path.dirname(directory) === directory) return null;
    directory = path.dirname(directory);
  }
}

function projectOf(file) {
  // The nearest config that includes the file, as the language service
  // looks for one: tsconfig.json, then jsconfig.json, directory by directory.
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

const DECLARATIONS = [
  [".d.ts", ".js"],
  [".d.mts", ".mjs"],
  [".d.cts", ".cjs"],
];

function implementationOf(file) {
  // The implementation a declaration file describes: the file beside it
  // with the same stem.
  for (const [suffix, runtime] of DECLARATIONS) {
    if (file.endsWith(suffix)) {
      const candidate = file.slice(0, -suffix.length) + runtime;
      return fs.existsSync(candidate) ? candidate : null;
    }
  }
  return null;
}

function relativeTo(file) {
  return path.relative(root, real(file)).split(path.sep).join("/");
}

function exportsOf(checker, source) {
  // What the module exports under each name, as the checker binds it:
  // aliases (export ... from, export { x }, export =) followed to the
  // declarations they name. A step that reaches a declaration file with an
  // implementation beside it stops there: the export is that
  // implementation's export of the same name. Each step is an export of
  // the module that declares it (export * gives the other module's own
  // export symbols), so its name is the name that module exports it under.
  const module = checker.getSymbolAtLocation(source);
  if (!module) return;
  for (const exported of checker.getExportsOfModule(module)) {
    const row = {
      module: relativeName(source.fileName),
      name: ts.symbolName(exported),
    };
    let symbol = exported;
    let runtime = null;
    for (let step = 0; step < 64; step++) {
      const first = (symbol.declarations || [])[0];
      const file = first && first.getSourceFile().fileName;
      const implementation =
        file && file !== source.fileName ? implementationOf(file) : null;
      if (implementation) {
        runtime = [implementation, ts.symbolName(symbol)];
        break;
      }
      if (!(symbol.flags & ts.SymbolFlags.Alias)) break;
      const next = checker.getImmediateAliasedSymbol(symbol);
      if (!next) break;
      symbol = next;
    }
    if (runtime) {
      out.push(
        JSON.stringify({
          ...row,
          runtime: relativeTo(runtime[0]),
          runtime_name: runtime[1],
        }),
      );
      continue;
    }
    let target = exported;
    if (target.flags & ts.SymbolFlags.Alias) {
      target = checker.getAliasedSymbol(target);
    }
    for (const declaration of target.declarations || []) {
      const at = declaration.getSourceFile();
      const name = ts.getNameOfDeclaration(declaration) || declaration;
      const start = at.getLineAndCharacterOfPosition(name.getStart(at));
      out.push(
        JSON.stringify({
          ...row,
          declared: relativeTo(at.fileName),
          line: start.line,
          character: start.character,
        }),
      );
    }
  }
}

const INFERRED = {
  target: ts.ScriptTarget.Latest,
  jsx: ts.JsxEmit.Preserve,
  allowJs: true,
  allowNonTsExtensions: true,
};
const groups = new Map();
for (const relative of input.files) {
  const component = canonical(path.join(root, relative));
  const file = scriptOf.get(component) || component;
  const project = projectOf(file);
  // A file no config includes (excluded, or left out for a file of the same
  // name with another extension) gets the options of the nearest config,
  // JavaScript allowed, in one program per such config: as the compiler
  // would run there, its paths and baseUrl apply.
  const nearest = project ? null : nearestConfig(file);
  const key = project
    ? project.config
    : nearest
      ? "inferred:" + nearest.config
      : "";
  if (!groups.has(key)) groups.set(key, { project, nearest, files: [] });
  groups.get(key).files.push(file);
}

for (const key of [...groups.keys()].sort()) {
  const { project, nearest, files } = groups.get(key);
  const options = project
    ? project.options
    : nearest
      ? { ...nearest.options, allowJs: true, noEmit: true }
      : INFERRED;
  // References are not passed: they would redirect imports of a referenced
  // project to its built declarations, where the language service reads its
  // sources, as here.
  const program = ts.createProgram({
    rootNames: files,
    options,
    host: hostFor(options),
  });
  const checker = program.getTypeChecker();
  for (const file of files.slice().sort()) {
    const source = program.getSourceFile(file);
    if (!source) continue;
    exportsOf(checker, source);
    for (const literal of source.imports) {
      // Imports the compiler adds itself (a JSX runtime, tslib helpers)
      // are written nowhere.
      if (ts.nodeIsSynthesized(literal)) continue;
      const mode = ts.getModeForUsageLocation(source, literal, options);
      const resolution = program.getResolvedModule(source, literal.text, mode);
      const module = resolution && resolution.resolvedModule;
      const start = source.getLineAndCharacterOfPosition(literal.getStart(source));
      out.push(
        JSON.stringify({
          file: relativeName(file),
          line: start.line,
          character: start.character,
          specifier: literal.text,
          resolved: module ? real(canonical(module.resolvedFileName)) : null,
          project: project
            ? path.relative(root, project.config).split(path.sep).join("/")
            : null,
          options_from: nearest
            ? path.relative(root, nearest.config).split(path.sep).join("/")
            : null,
        }),
      );
    }
  }
}
process.stdout.write(out.join("\n") + (out.length ? "\n" : ""));
