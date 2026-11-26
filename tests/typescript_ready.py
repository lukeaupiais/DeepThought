"""Whether the TypeScript compiler, which reads every JavaScript/TypeScript
file of a service-contract run, can be used here: Node.js is found and the
pinned typescript package is installed or installable. The tools are
required, so this skip is TEMPORARY: it exists only because the offline
acceptance sandbox does not yet provision Node.js. Remove it (and every
``needs_typescript``) once the acceptance jobs install Node.js and the sandbox
PATH carries it (#323, #344)."""

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
