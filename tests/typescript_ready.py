"""Whether the TypeScript compiler, which reads every JavaScript/TypeScript
file of a service-contract run, can be used here: Node.js is found and the
pinned typescript package is installed or installable. The offline
acceptance sandbox has neither, so the tests that analyze scripts are
skipped there and run in the jobs that provision Node.js."""

import unittest

from deepthought import ts_facts


def _ready():
    try:
        ts_facts.node()
        ts_facts.typescript()
    except ts_facts.TypeScriptMissing:
        return False
    return True


TYPESCRIPT = _ready()
needs_typescript = unittest.skipUnless(
    TYPESCRIPT, "Node.js and the TypeScript compiler are not available"
)
