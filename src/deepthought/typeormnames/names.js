// Default TypeORM table names, computed by TypeORM's own naming strategy.
//
// argv[2]: the directory of the installed typeorm package (the version the
// project pins). stdin: a JSON array of entity class names. stdout: a JSON
// object mapping each name to DefaultNamingStrategy.tableName(name).
"use strict";
const path = require("path");
const { DefaultNamingStrategy } = require(
  path.join(process.argv[2], "naming-strategy", "DefaultNamingStrategy")
);

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  const naming = new DefaultNamingStrategy();
  const tables = {};
  for (const name of JSON.parse(input)) {
    tables[name] = naming.tableName(name, undefined);
  }
  process.stdout.write(JSON.stringify(tables));
});
