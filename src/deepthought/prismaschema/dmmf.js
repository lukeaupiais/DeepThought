// Read Prisma schemas with Prisma's own schema engine (prisma-schema-wasm).
//
// argv[2]: the directory of the @prisma/prisma-schema-wasm package.
// stdin: {"op": "config" | "dmmf", "schemas": [[[path, text], ...], ...]}.
// stdout: one JSON result per schema, in order:
//   config: {"datasources": n, "errors": [message, ...]}
//   dmmf:   {"models": [{"name", "dbName", "schema"}]} or {"error": message}
"use strict";
const wasm = require(process.argv[2]);

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  const { op, schemas } = JSON.parse(input);
  // Builds before multi-file schemas (Prisma 5.15) take one schema text.
  const call = (name, params, files) => {
    try {
      return wasm[name](JSON.stringify({ ...params, prismaSchema: files }));
    } catch (error) {
      if (files.length !== 1) throw error;
      return wasm[name](JSON.stringify({ ...params, prismaSchema: files[0][1] }));
    }
  };
  const results = schemas.map((files) => {
    try {
      if (op === "config") {
        const out = JSON.parse(
          call(
            "get_config",
            { datasourceOverrides: {}, ignoreEnvVarErrors: true, env: {} },
            files
          )
        );
        return {
          datasources: out.config.datasources.length,
          errors: (out.errors || []).map((e) => e.message),
        };
      }
      const dmmf = JSON.parse(
        call("get_dmmf", { noColor: true }, files)
      );
      return {
        models: dmmf.datamodel.models.map((m) => ({
          name: m.name,
          dbName: m.dbName,
          schema: m.schema || null,
        })),
      };
    } catch (error) {
      let message = String(error && error.message ? error.message : error);
      try {
        message = JSON.parse(message).message || message;
      } catch (_) {}
      return { error: message };
    }
  });
  process.stdout.write(JSON.stringify(results));
});
