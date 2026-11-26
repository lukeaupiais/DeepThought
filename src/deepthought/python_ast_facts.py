"""What the pytest fixture and FastAPI dependency rules read from Python
sources with ``ast``, as plain data.

Python's ``ast`` parses only the syntax of the interpreter it runs in, so a
project written for a newer Python is read by that newer interpreter
(``deepthought.python_runtime``). This module therefore uses the standard
library only and also runs as a script: it reads ``{path: text}`` as JSON on
standard input and writes the facts as JSON on standard output.
"""

import ast
from dataclasses import asdict
import json
import os
import sys

try:
    from .fastapi_dependencies import dependency_facts
    from .pytest_fixtures import fixture_uses
except ImportError:  # Run as a script by another interpreter.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from fastapi_dependencies import dependency_facts
    from pytest_fixtures import fixture_uses

# The modules a script run needs beside this one.
MODULES = ("python_ast_facts.py", "pytest_fixtures.py", "fastapi_dependencies.py")


def ast_facts(sources):
    """``python`` (the interpreter's version), ``unparsed`` (path to the
    error of each file it cannot parse), ``fixtures`` (``FixtureUse`` fields)
    and ``dependencies`` (path to ``dependency_facts``) of ``sources``."""
    trees, unparsed = {}, {}
    for path in sorted(sources):
        try:
            trees[path] = ast.parse(sources[path])
        except (SyntaxError, ValueError) as error:
            unparsed[path] = str(error)
    return {
        "python": "{}.{}.{}".format(*sys.version_info[:3]),
        "unparsed": unparsed,
        "fixtures": [asdict(use) for use in fixture_uses(sources)],
        "dependencies": {path: dependency_facts(tree) for path, tree in trees.items()},
    }


if __name__ == "__main__":
    json.dump(ast_facts(json.load(sys.stdin)), sys.stdout)
