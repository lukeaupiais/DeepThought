"""Express route prefixes composed across files from ``use`` mounts."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

READY = (
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_javascript")
    and importlib.util.find_spec("tree_sitter_typescript")
)

FILES = {
    "api/package.json": "{}",
    # CommonJS: app -> /office router required inline and through a name.
    "api/app.js": """
const express = require('express');
const users = require('./routes/users');
const VERSION = '/v2';

const app = express();
app.use(express.json());
app.use('/office', require('./routes/office'));
app.use(['/users', '/people'], auth, users);
app.use(`${process.env.BASE}/health`, require('./routes/health'));
app.use(VERSION, require('./routes/office'));

app.get('/', (req, res) => res.send('ok'));
""",
    "api/routes/office.js": """
const express = require('express');
const router = express.Router();

router.get('/getAll', (req, res) => res.json([]));
module.exports = router;
""",
    "api/routes/users.js": """
const express = require('express');
const router = express.Router();

router.get('/:id', (req, res) => res.json({}));
module.exports = router;
""",
    "api/routes/health.js": """
const express = require('express');
const router = express.Router();

router.get('/live', (req, res) => res.send('up'));
module.exports = router;
""",
    # ES modules in TypeScript: nested routers, a named export, an
    # unmounted router.
    "web/package.json": "{}",
    "web/src/index.ts": """
import express, { Router } from 'express';
import chat from './routes/chat';
import { orders } from './routes/orders';

const app = express();
const main = Router();
main.use('/chat', chat);
main.use('/orders/', orders);
app.use('/api/v1', main);
""",
    "web/src/routes/chat.ts": """
import { Router } from 'express';

const router = Router();
router.post('/send', (req, res) => res.end());
export default router;
""",
    "web/src/routes/orders.ts": """
import { Router } from 'express';

export const orders = Router();
orders.get('/:id', (req, res) => res.end());
""",
    "web/src/routes/unused.ts": """
import { Router } from 'express';

const router = Router();
router.get('/alone', (req, res) => res.end());
export default router;
""",
}


@unittest.skipUnless(READY, "tree-sitter JavaScript/TypeScript is not installed")
class ExpressMountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.tool = ServiceContractTool(root)
        asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_routes(self):
        _, routes, _, _, _ = self.tool.last_facts
        found = sorted(
            (r.file.rpartition("/")[2], " ".join(r.methods), r.path, r.base_unknown)
            for r in routes
        )
        self.assertEqual(
            found,
            [
                ("app.js", "GET", "/", False),
                ("chat.ts", "POST", "/api/v1/chat/send", False),
                # Mounted at a path that is not literal: matched as if the
                # mount added no segments.
                ("health.js", "GET", "/health/live", True),
                ("office.js", "GET", "/office/getAll", False),
                # A name bound to a path.
                ("office.js", "GET", "/v2/getAll", False),
                ("orders.ts", "GET", "/api/v1/orders/{var}", False),
                ("unused.ts", "GET", "/alone", False),
                # Mounted at each path of an array.
                ("users.js", "GET", "/people/{var}", False),
                ("users.js", "GET", "/users/{var}", False),
            ],
        )


if __name__ == "__main__":
    unittest.main()
