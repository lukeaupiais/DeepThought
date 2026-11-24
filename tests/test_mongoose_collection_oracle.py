"""tools/mongoose_collection_oracle.cjs on a small app.

Skipped unless Node and the TypeScript package are available (set
TYPESCRIPT_DIR to its directory): the labels it produces are committed, so
CI does not run it. The pluralize rules come from a file in Mongoose's own
format, written here, so the test needs no Mongoose install.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ORACLE = (
    Path(__file__).resolve().parents[1] / "tools" / "mongoose_collection_oracle.cjs"
)
TYPESCRIPT = os.environ.get("TYPESCRIPT_DIR", "")

PLURALIZE = """'use strict';
module.exports = pluralize;
exports.pluralization = [
  [/(pe)rson$/gi, '$1ople'],
  [/([^aeiouy]|qu)y$/gi, '$1ies'],
  [/s$/gi, 's'],
  [/$/gi, 's']
];
const rules = exports.pluralization;
exports.uncountables = ['sheep'];
const uncountables = exports.uncountables;
function pluralize(str) {
  let found;
  str = str.toLowerCase();
  if (!~uncountables.indexOf(str)) {
    found = rules.filter(function(rule) { return str.match(rule[0]); });
    if (found[0]) { return str.replace(found[0][0], found[0][1]); }
  }
  return str;
}
"""

FILES = {
    "tsconfig.json": '{"compilerOptions":{"baseUrl":".","paths":{"~/*":["./src/*"]}}}',
    "src/schema/preset.ts": (
        "import { Schema } from 'mongoose';\n"
        "const presetSchema = new Schema({}, { collection: 'my_presets' });\n"
        "export default presetSchema;\n"
    ),
    "src/models/preset.ts": (
        "import mongoose from 'mongoose';\n"
        "import presetSchema from '~/schema/preset';\n"
        "export const P = mongoose.model('Preset', presetSchema);\n"
    ),
    "src/models/factory.ts": (
        "import { Schema } from 'mongoose';\n"
        "const s = new Schema({});\n"
        "export function make(mongoose: typeof import('mongoose')) {\n"
        "  return mongoose.model('Person', s);\n}\n"
    ),
    "src/models/legacy.js": (
        "const mongoose = require('mongoose');\n"
        "const s = mongoose.Schema({ name: String }, { collection: 'cs' });\n"
        "const C = 'fixed_sheep';\n"
        "module.exports = mongoose.model('Sheep', s);\n"
        "mongoose.model('Flock', new mongoose.Schema({}), C);\n"
    ),
    "src/models/kinds.js": (
        "const { model, Schema } = require('mongoose');\n"
        "const base = new Schema({});\n"
        "const Event = model('Event', base);\n"
        "const Click = Event.discriminator('Click', new Schema({}));\n"
        "const opts = { ...shared };\n"
        "model('Spread', new Schema({}, opts));\n"
        "model('Category', new Schema({}));\n"
        "const conn = mongoose.createConnection('x');\n"
        "other.model('Elsewhere', base);\n"
    ),
    "src/models/sheep.test.js": (
        "const mongoose = require('mongoose');\n"
        "mongoose.model('Fixture', new mongoose.Schema({}));\n"
    ),
}


@unittest.skipUnless(
    shutil.which("node")
    and shutil.which("git")
    and Path(TYPESCRIPT, "package.json").is_file(),
    "node, git and TYPESCRIPT_DIR are required",
)
class MongooseCollectionOracleTests(unittest.TestCase):
    def test_collections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            for name, text in FILES.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, "utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            rules = Path(directory) / "pluralize.js"
            rules.write_text(PLURALIZE, "utf-8")
            output = Path(directory) / "out.json"
            subprocess.run(
                [
                    "node",
                    str(ORACLE),
                    str(root),
                    TYPESCRIPT,
                    str(rules),
                    "0.0.0",
                    str(output),
                ],
                check=True,
                capture_output=True,
            )
            result = json.loads(output.read_text("utf-8"))
        models = {m["class"]: (m["table"], m["how"]) for m in result["models"]}
        self.assertEqual(
            models,
            {
                "src/models/preset.ts::Preset": ("my_presets", "schema option"),
                "src/models/factory.ts::Person": ("people", "default"),
                "src/models/legacy.js::Sheep": ("cs", "schema option"),
                "src/models/legacy.js::Flock": ("fixed_sheep", "argument"),
                "src/models/kinds.js::Event": ("events", "default"),
                "src/models/kinds.js::Click": ("events", "discriminator"),
                "src/models/kinds.js::Category": ("categories", "default"),
            },
        )
        reasons = {n["class"]: n["reason"] for n in result["not_counted"]}
        self.assertEqual(
            reasons,
            {
                "src/models/kinds.js::Spread": "schema options spread",
                "src/models/kinds.js::Elsewhere": "receiver is not a known mongoose binding",
                "src/models/sheep.test.js::Fixture": "test source",
            },
        )


if __name__ == "__main__":
    unittest.main()
