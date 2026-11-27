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

// All of standard input. readFileSync(0) fails with EAGAIN when the pipe is
// non-blocking and momentarily empty (a writer slower than this reader), so
// read in chunks and wait briefly whenever no data is ready yet.
function readStdin() {
  const chunks = [];
  const buffer = Buffer.alloc(1 << 16);
  const pause = new Int32Array(new SharedArrayBuffer(4));
  for (;;) {
    let read;
    try {
      read = fs.readSync(0, buffer, 0, buffer.length, null);
    } catch (error) {
      if (error.code === "EAGAIN") {
        Atomics.wait(pause, 0, 0, 2);
        continue;
      }
      if (error.code === "EOF") break;
      throw error;
    }
    if (read === 0) break;
    chunks.push(Buffer.from(buffer.subarray(0, read)));
  }
  return Buffer.concat(chunks).toString("utf8");
}

const input = JSON.parse(readStdin());
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
