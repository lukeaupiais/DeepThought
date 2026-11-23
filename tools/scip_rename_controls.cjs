// In-memory actual compiler checks, independent of SCIP graph output.
const fs = require("node:fs"),
  path = require("node:path");
const root = path.resolve(process.argv[2] || ".");
const ts = require(root + "/node_modules/typescript");
process.chdir(root);
const raw = ts.readConfigFile(root + "/tsconfig.json", ts.sys.readFile);
const cfg = ts.parseJsonConfigFileContent(raw.config, ts.sys, root);
const original = ts.createProgram(cfg.fileNames, cfg.options);
const checker = original.getTypeChecker();
const baseline = ts.getPreEmitDiagnostics(original);
if (baseline.length) {
  console.log(
    baseline.map((d) => ({
      code: d.code,
      file: d.file?.fileName,
      message: ts.flattenDiagnosticMessageText(d.messageText, " "),
    })),
  );
  throw Error("Baseline compiler diagnostics " + baseline.length);
}
function unalias(s) {
  const seen = new Set();
  while (s && s.flags & ts.SymbolFlags.Alias && !seen.has(s)) {
    seen.add(s);
    s = checker.getAliasedSymbol(s);
  }
  return s;
}
const entry = original.getSourceFile(root + "/src/index.ts");
const publicExports = checker.getExportsOfModule(
  checker.getSymbolAtLocation(entry),
);
const reports = {};
for (const name of ["getNamedType", "typeFromAST"]) {
  const target = unalias(publicExports.find((s) => s.name === name));
  const changes = new Map();
  let omitted = null;
  for (const source of original
    .getSourceFiles()
    .filter(
      (s) =>
        s.fileName.startsWith(root + "/") &&
        !s.fileName.includes("/node_modules/"),
    )) {
    const edits = [];
    function walk(n) {
      if (
        ts.isIdentifier(n) &&
        unalias(checker.getSymbolAtLocation(n)) === target
      ) {
        const e = {
          start: n.getStart(source),
          end: n.getEnd(),
          text: name + "RenamedForValidation",
        };
        edits.push(e);
        if (
          !omitted &&
          ts.isCallExpression(n.parent) &&
          n.parent.expression === n
        )
          omitted = { file: source.fileName, start: e.start };
      }
      ts.forEachChild(n, walk);
    }
    walk(source);
    if (edits.length)
      changes.set(source.fileName, { text: source.text, edits });
  }
  if (!omitted) throw Error("No negative call-site control");
  function evaluate(breakOne) {
    const texts = new Map();
    for (const [file, value] of changes) {
      let text = value.text;
      for (const e of value.edits.sort((a, b) => b.start - a.start)) {
        if (breakOne && file === omitted.file && e.start === omitted.start)
          continue;
        text = text.slice(0, e.start) + e.text + text.slice(e.end);
      }
      texts.set(file, text);
    }
    const host = ts.createCompilerHost(cfg.options);
    const read = host.readFile.bind(host);
    host.readFile = (file) => (texts.has(file) ? texts.get(file) : read(file));
    const p = ts.createProgram(cfg.fileNames, cfg.options, host);
    return ts
      .getPreEmitDiagnostics(p)
      .map((d) => ({
        code: d.code,
        file: d.file ? path.relative(root, d.file.fileName) : null,
        message: ts.flattenDiagnosticMessageText(d.messageText, " "),
      }));
  }
  const good = evaluate(false),
    bad = evaluate(true);
  if (good.length || !bad.length)
    throw Error("Rename controls did not separate " + name);
  reports[name] = {
    positive_all_project_rename_diagnostics: good,
    negative_missing_call_diagnostics: bad,
    changed_files: changes.size,
    omitted_call: {
      file: path.relative(root, omitted.file),
      offset: omitted.start,
    },
    source_mutations: 0,
  };
}
fs.writeFileSync(
  path.resolve(process.argv[3] || "rename-qualification.json"),
  JSON.stringify(
    {
      compiler: ts.version,
      baseline_diagnostics: 0,
      method:
        "Actual compiler programs on in-memory modified project source; positive full rename and deliberately omitted true call. No SCIP data read.",
      targets: reports,
    },
    null,
    2,
  ) + "\n",
);
console.log(JSON.stringify(reports));
