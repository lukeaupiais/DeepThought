// Default TypeORM table names, computed by TypeORM's own naming strategy.
//
// argv[2]: the directory of the installed typeorm package (the version the
// project pins). stdin: a JSON array of requests: an entity class name, for
// DefaultNamingStrategy.tableName(name); ["column", property, custom name or
// null, embedded prefixes], for columnName; or ["join", relation property,
// referenced column], for joinColumnName. stdout: a JSON object mapping each
// request (an array as its JSON text) to the name.
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
  for (const request of JSON.parse(input)) {
    if (typeof request === "string") {
      tables[request] = naming.tableName(request, undefined);
    } else if (request[0] === "column") {
      tables[JSON.stringify(request)] = naming.columnName(
        request[1],
        request[2] === null ? undefined : request[2],
        request[3]
      );
    } else if (request[0] === "join") {
      tables[JSON.stringify(request)] = naming.joinColumnName(
        request[1],
        request[2]
      );
    }
  }
  process.stdout.write(JSON.stringify(tables));
});
