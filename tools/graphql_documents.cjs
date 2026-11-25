// GraphQL documents in source files, read by graphql-js and the TypeScript
// compiler, for tools/graphql_client_oracle.py.
//
//   NODE_PATH=DIR node tools/graphql_documents.cjs < files.json > documents.json
//
// DIR holds graphql and typescript (npm install graphql@16.9.0
// typescript@5.6.3). The input is a JSON list of file paths. A .graphql or
// .gql file is one document. In any other file, the TypeScript compiler
// finds each template literal tagged `gql` or `graphql` (also as the
// argument of a `gql(...)` call); its interpolations (`${FRAGMENT}`) are
// blanked to spaces so that lines stay those of the file.
//
// Output, per file: {file, documents: [{line, error} | {line, operations,
// schema_roots, object_types}]}, where line is the 0-based file line the
// document starts on and every other line is 1-based in the document.

"use strict";

const fs = require("fs");
const { parse, GraphQLError } = require("graphql");
const ts = require("typescript");

const TAGS = new Set(["gql", "graphql"]);

function tagName(node) {
  if (ts.isIdentifier(node)) return node.text;
  if (ts.isPropertyAccessExpression(node)) return node.name.text;
  return null;
}

function scriptKind(file) {
  if (/\.tsx$/.test(file)) return ts.ScriptKind.TSX;
  if (/\.ts$/.test(file)) return ts.ScriptKind.TS;
  if (/\.jsx$/.test(file)) return ts.ScriptKind.JSX;
  return ts.ScriptKind.JS;
}

// The body of a template literal, with each `${...}` blanked and each
// escape read as the character it escapes.
function body(source, template) {
  const text = source.text;
  const start = template.getStart(source) + 1; // after the backtick
  const end = template.getEnd() - 1; // before the closing backtick
  const chars = text.slice(start, end).split("");
  if (ts.isTemplateExpression(template)) {
    for (const span of template.templateSpans) {
      // From "${" to its "}": the expression and its braces.
      const from = span.expression.getFullStart() - 2 - start;
      const to = span.literal.getStart(source) + 1 - start;
      for (let i = from; i < to; i++) {
        if (chars[i] !== "\n") chars[i] = " ";
      }
    }
  }
  let out = "";
  for (let i = 0; i < chars.length; i++) {
    if (chars[i] === "\\" && i + 1 < chars.length) {
      out += chars[i + 1];
      i++;
    } else {
      out += chars[i];
    }
  }
  return { line: source.getLineAndCharacterOfPosition(start - 1).line, text: out };
}

function templates(file, text) {
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, scriptKind(file));
  const found = [];
  const visit = (node) => {
    let template = null;
    if (ts.isTaggedTemplateExpression(node) && TAGS.has(tagName(node.tag))) {
      template = node.template;
    } else if (
      ts.isCallExpression(node) &&
      TAGS.has(tagName(node.expression)) &&
      node.arguments.length > 0 &&
      (ts.isNoSubstitutionTemplateLiteral(node.arguments[0]) ||
        ts.isTemplateExpression(node.arguments[0]))
    ) {
      template = node.arguments[0];
    }
    if (template) found.push(body(source, template));
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

function lineOf(node) {
  return node.loc.source.locationOffset.line - 1 + lineAt(node.loc.source.body, node.loc.start);
}

function lineAt(text, offset) {
  let line = 1;
  for (let i = 0; i < offset; i++) if (text.charCodeAt(i) === 10) line++;
  return line;
}

function summary(doc) {
  const operations = [];
  const schemaRoots = {};
  const objectTypes = [];
  for (const d of doc.definitions) {
    if (d.kind === "OperationDefinition") {
      const fields = [];
      for (const s of d.selectionSet.selections) {
        if (!s.name) continue; // inline fragments and spreads name no field
        fields.push({
          name: s.name.value,
          line: lineOf(s.name),
          client: (s.directives || []).some((x) => x.name.value === "client"),
        });
      }
      operations.push({ type: d.operation, name: d.name ? d.name.value : null, fields });
    } else if (d.kind === "SchemaDefinition" || d.kind === "SchemaExtension") {
      for (const op of d.operationTypes || []) schemaRoots[op.operation] = op.type.name.value;
    } else if (d.kind === "ObjectTypeDefinition" || d.kind === "ObjectTypeExtension") {
      objectTypes.push({
        name: d.name.value,
        fields: (d.fields || []).map((f) => ({ name: f.name.value, line: lineOf(f.name) })),
      });
    }
  }
  return { operations, schema_roots: schemaRoots, object_types: objectTypes };
}

function documents(file) {
  const text = fs.readFileSync(file, "utf8");
  const pieces = /\.(graphql|gql)$/.test(file) ? [{ line: 0, text }] : templates(file, text);
  return pieces.map((piece) => {
    try {
      return { line: piece.line, ...summary(parse(piece.text)) };
    } catch (error) {
      if (!(error instanceof GraphQLError)) throw error;
      return { line: piece.line, error: error.message };
    }
  });
}

const files = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(files.map((file) => ({ file, documents: documents(file) }))));
