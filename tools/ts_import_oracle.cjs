// JS/TS import targets from webpack's resolver (enhanced-resolve), set up the
// way TypeScript resolves, with no code shared with the TypeScript compiler.
//
//   NODE_PATH=DIR node tools/ts_import_oracle.cjs ROOT < files.json > targets.json
//
// for tools/ts_import_truth.py.
//
// DIR holds enhanced-resolve and @babel/parser (npm install
// enhanced-resolve@5.18.1 @babel/parser@7.26.10). The input is a
// JSON list of repository-relative files. @babel/parser finds each module
// specifier (import, export ... from, import(), require(), import x =
// require()). Each specifier is resolved:
//   - through the `paths` and `baseUrl` of the nearest tsconfig.json or
//     jsconfig.json above the file (relative `extends` followed), first
//     match by TypeScript's longest-prefix rule;
//   - then by enhanced-resolve with TypeScript's extension order (.ts, .tsx,
//     .d.ts, then .js, .jsx), its .js -> .ts substitution, the types/typings
//     fields before main, and the types/import/require/default conditions.
// A .d.ts result also accepts the implementation file beside it with the
// same stem (.js, .mjs, .cjs, .jsx), since a graph links either.
// Output, per specifier: {file, line, spec, targets: [...]}, targets
// repository-relative, empty when nothing in the repository matches.

"use strict";

const fs = require("fs");
const path = require("path");
const { parse } = require("@babel/parser");
const resolve = require("enhanced-resolve");

const ROOT = path.resolve(process.argv[2]);

const resolver = resolve.create.sync({
  extensions: [".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".json"],
  extensionAlias: {
    ".js": [".ts", ".tsx", ".d.ts", ".js"],
    ".jsx": [".tsx", ".d.ts", ".jsx"],
    ".mjs": [".mts", ".d.mts", ".mjs"],
    ".cjs": [".cts", ".d.cts", ".cjs"],
  },
  mainFields: ["types", "typings", "main"],
  mainFiles: ["index"],
  conditionNames: ["types", "import", "require", "node", "default"],
  symlinks: true,
});

function stripJson(text) {
  // tsconfig allows comments and trailing commas.
  let out = "";
  let inString = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inString) {
      out += c;
      if (c === "\\") out += text[++i];
      else if (c === '"') inString = false;
    } else if (c === '"') {
      inString = true;
      out += c;
    } else if (c === "/" && text[i + 1] === "/") {
      while (i < text.length && text[i] !== "\n") i++;
      out += "\n";
    } else if (c === "/" && text[i + 1] === "*") {
      i += 2;
      while (i < text.length && !(text[i] === "*" && text[i + 1] === "/")) i++;
      i++;
    } else out += c;
  }
  return JSON.parse(out.replace(/,(\s*[}\]])/g, "$1"));
}

const configs = new Map();
function config(dir) {
  if (configs.has(dir)) return configs.get(dir);
  let found = null;
  for (const name of ["tsconfig.json", "jsconfig.json"]) {
    const file = path.join(dir, name);
    if (fs.existsSync(file)) {
      found = options(file);
      break;
    }
  }
  if (!found && dir !== ROOT && dir.startsWith(ROOT)) found = config(path.dirname(dir));
  configs.set(dir, found);
  return found;
}

// compilerOptions with relative `extends` followed; paths and baseUrl are
// kept with the directory of the config that set them.
function options(file) {
  const json = stripJson(fs.readFileSync(file, "utf8"));
  let base = { paths: null, pathsBase: null, baseUrl: null };
  const ext = [].concat(json.extends || []);
  for (const e of ext) {
    if (!e.startsWith(".")) continue; // a package's config is not installed
    let target = path.resolve(path.dirname(file), e);
    if (!target.endsWith(".json")) target += fs.existsSync(target + ".json") ? ".json" : "/tsconfig.json";
    if (fs.existsSync(target)) base = { ...base, ...pick(options(target)) };
  }
  const co = json.compilerOptions || {};
  const dir = path.dirname(file);
  if (co.baseUrl !== undefined) base.baseUrl = path.resolve(dir, co.baseUrl);
  if (co.paths) {
    base.paths = co.paths;
    base.pathsBase = co.baseUrl !== undefined ? path.resolve(dir, co.baseUrl) : dir;
  }
  return base;
}
function pick(o) {
  const out = {};
  for (const k of ["paths", "pathsBase", "baseUrl"]) if (o[k] !== null) out[k] = o[k];
  return out;
}

function inRepo(file) {
  const real = fs.realpathSync(file);
  const rel = path.relative(ROOT, real);
  return rel.startsWith("..") || rel.split(path.sep).includes("node_modules") ? null : rel.split(path.sep).join("/");
}

function tryResolve(from, request) {
  try {
    const r = resolver(from, request);
    return r ? r : null;
  } catch (e) {
    return null;
  }
}

function candidates(conf, spec) {
  // TypeScript's paths: the exact pattern, else the wildcard pattern with
  // the longest prefix.
  if (!conf || !conf.paths || spec.startsWith(".") || path.isAbsolute(spec)) return [];
  let best = null;
  for (const pattern of Object.keys(conf.paths)) {
    const star = pattern.indexOf("*");
    if (star < 0) {
      if (pattern === spec) return conf.paths[pattern].map((t) => path.resolve(conf.pathsBase, t));
      continue;
    }
    const prefix = pattern.slice(0, star);
    const suffix = pattern.slice(star + 1);
    if (spec.startsWith(prefix) && spec.endsWith(suffix) && spec.length >= prefix.length + suffix.length) {
      if (!best || prefix.length > best.prefix.length) best = { pattern, prefix, suffix };
    }
  }
  if (!best) return [];
  const middle = spec.slice(best.prefix.length, spec.length - best.suffix.length);
  return conf.paths[best.pattern].map((t) => path.resolve(conf.pathsBase, t.replace("*", middle)));
}

function targets(file, spec) {
  const abs = path.join(ROOT, file);
  const dir = path.dirname(abs);
  const conf = config(dir);
  let hit = null;
  for (const c of candidates(conf, spec)) {
    hit = tryResolve(dir, c);
    if (hit) break;
  }
  if (!hit && conf && conf.baseUrl && !spec.startsWith(".") && !path.isAbsolute(spec)) {
    hit = tryResolve(dir, path.join(conf.baseUrl, spec));
  }
  if (!hit) hit = tryResolve(dir, spec);
  if (!hit) return [];
  const out = new Set();
  const rel = inRepo(hit);
  if (rel) out.add(rel);
  const m = hit.match(/^(.*)\.d\.(m|c)?ts$/);
  if (m) {
    for (const e of [".js", ".mjs", ".cjs", ".jsx"]) {
      if (fs.existsSync(m[1] + e)) {
        const r = inRepo(m[1] + e);
        if (r) out.add(r);
      }
    }
  }
  return [...out].sort();
}

function specifiers(file) {
  const text = fs.readFileSync(path.join(ROOT, file), "utf8");
  const ts = /\.(m|c)?tsx?$/.test(file);
  const plugins = ts ? [["typescript", { dts: /\.d\.(m|c)?ts$/.test(file) }]] : ["flow"];
  if (!/\.(m|c)?ts$/.test(file) || /\.tsx$/.test(file)) plugins.push("jsx");
  let ast;
  try {
    ast = parse(text, {
      sourceType: "unambiguous",
      plugins: [...plugins, "decorators-legacy", "importAttributes", "topLevelAwait"],
      errorRecovery: true,
    });
  } catch (e) {
    return { error: e.message, found: [] };
  }
  const found = [];
  const add = (node) => {
    if (node && (node.type === "StringLiteral" || node.type === "Literal")) {
      found.push({ line: node.loc.start.line, spec: node.value });
    }
  };
  const visit = (node) => {
    if (!node || typeof node.type !== "string") return;
    switch (node.type) {
      case "ImportDeclaration":
      case "ExportAllDeclaration":
      case "ExportNamedDeclaration":
        add(node.source);
        break;
      case "TSImportEqualsDeclaration":
        if (node.moduleReference && node.moduleReference.type === "TSExternalModuleReference")
          add(node.moduleReference.expression);
        break;
      case "CallExpression":
        if (
          (node.callee.type === "Import" ||
            (node.callee.type === "Identifier" && node.callee.name === "require")) &&
          node.arguments.length > 0
        )
          add(node.arguments[0]);
        break;
      case "ImportExpression":
        add(node.source);
        break;
    }
    for (const key of Object.keys(node)) {
      if (key === "loc" || key === "start" || key === "end") continue;
      const v = node[key];
      if (Array.isArray(v)) v.forEach(visit);
      else if (v && typeof v.type === "string") visit(v);
    }
  };
  visit(ast.program);
  return { error: null, found };
}

const files = JSON.parse(fs.readFileSync(0, "utf8"));
const out = [];
for (const file of files) {
  const { error, found } = specifiers(file);
  if (error) out.push({ file, error });
  for (const s of found) out.push({ file, line: s.line, spec: s.spec, targets: targets(file, s.spec) });
}
process.stdout.write(JSON.stringify(out));
