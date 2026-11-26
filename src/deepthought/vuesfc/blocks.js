// The script blocks of Vue single-file components, as Vue's own parser
// (@vue/compiler-sfc) reads them (deepthought.vue_sfc).
//
// argv: path of the pinned @vue/compiler-sfc package directory.
// stdin: {"sources": [component source text, ...]}.
// stdout: {"version": the package's version, "results": [one entry per
// source, in order: {"blocks": [{"start", "end", "lang", "setup"}],
// "errors": [...]}]}.
// ``start`` and ``end`` are UTF-8 byte offsets of a block's content. A block
// written with ``src`` has no content here and is left out.
"use strict";
const fs = require("fs");
const sfc = require(process.argv[2]);

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const out = input.sources.map((source) => {
  const { descriptor, errors } = sfc.parse(source, {
    sourceMap: false,
    ignoreEmpty: false,
  });
  const bytes = (offset) => Buffer.byteLength(source.slice(0, offset), "utf8");
  const blocks = [];
  for (const block of [descriptor.script, descriptor.scriptSetup]) {
    if (!block || block.src) continue;
    blocks.push({
      start: bytes(block.loc.start.offset),
      end: bytes(block.loc.end.offset),
      lang: block.lang || null,
      setup: Boolean(block.setup),
    });
  }
  blocks.sort((a, b) => a.start - b.start);
  return {
    blocks,
    errors: errors.map((error) => String(error.message || error)),
  };
});
process.stdout.write(JSON.stringify({ version: sfc.version, results: out }));
