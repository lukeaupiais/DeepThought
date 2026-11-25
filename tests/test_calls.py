"""Named call sites and their name-matched links to declarations."""

import importlib.util
import os
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import deepthought
from deepthought import analyze_repository
from deepthought.analysis_tools.evidence import registered_rules
from deepthought.calls import (
    CALL_IMPORTED_NAME,
    CALL_JS_MEMBER_CHAIN,
    CALL_JS_REEXPORT,
    CALL_JS_STATIC_METHOD,
    CALL_JS_TYPED_RECEIVER,
    CALL_LEXICAL_NAME,
    CALL_MODULE_ATTRIBUTE,
    CALL_PYTHON_REEXPORT,
    CALL_PYTHON_TYPED_RECEIVER,
    CALL_PRIVATE_NAME,
    CALL_SELF_METHOD,
    PYTHON_CALL_SELF_INHERITED,
    PYTHON_CALL_ATTRIBUTE_RECEIVER,
    PYTHON_CALL_RESULT_RECEIVER,
    PYTHON_CALL_SUPER,
    resolve_calls,
)
from deepthought.models import AnalysisResult, CallSite, FileCoverage

FIXTURES = Path(__file__).parent / "fixtures"
PYTHON_READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_python"
)
TYPESCRIPT_READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_typescript"
)


def analyze(files, language):
    with tempfile.TemporaryDirectory() as directory:
        for name, content in files.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return analyze_repository(
            directory, repository_id="fixture", languages=(language,)
        )


def links(result):
    """{(file, line, callee): (status, target 'file::symbol', rule, reason)}"""
    sites = {site.id: site for site in result.call_sites}
    definitions = {d.id: d for d in result.definitions}
    found = {}
    for r in resolve_calls(result):
        site = sites[r.call_id]
        target = definitions.get(r.target_id)
        found[(site.file, site.line, site.callee)] = (
            r.status,
            f"{target.file}::{target.symbol}" if target else None,
            r.rule_id,
            r.reason,
        )
    return found


@unittest.skipUnless(PYTHON_READY, "Provision the python extra")
class PythonCallTests(unittest.TestCase):
    def test_call_sites_record_callee_kind_and_innermost_caller(self):
        result = analyze(
            {
                "main.py": (
                    "def outer():\n"
                    "    def inner():\n"
                    "        return a.b.c()\n"
                    "    return inner()\n"
                    "setup()\n"
                    "f()()\n"
                )
            },
            "python",
        )
        symbols = {d.id: d.symbol for d in result.definitions}
        self.assertEqual(
            [
                (s.callee, s.kind, s.line, symbols.get(s.caller_id))
                for s in result.call_sites
            ],
            [
                ("a.b.c", "call", 3, "outer.inner"),
                ("inner", "call", 4, "outer"),
                ("setup", "call", 5, None),
                ("f", "call", 6, None),
            ],
        )

    def test_lexical_scopes_skip_class_bodies_and_report_ambiguity(self):
        result = analyze(
            {
                "main.py": (
                    "def helper(): pass\n"
                    "class C:\n"
                    "    def helper(self): pass\n"
                    "    def run(self):\n"
                    "        helper()\n"
                    "        self.helper()\n"
                    "        self.missing()\n"
                    "def twice(): pass\n"
                    "def twice(): pass\n"
                    "def go():\n"
                    "    twice()\n"
                    "    undefined()\n"
                    "helper()\n"
                    "self.outside()\n"
                )
            },
            "python",
        )
        found = links(result)
        self.assertEqual(
            found[("main.py", 5, "helper")],
            ("linked", "main.py::helper", CALL_LEXICAL_NAME, None),
        )
        self.assertEqual(
            found[("main.py", 6, "self.helper")],
            ("linked", "main.py::C.helper", CALL_SELF_METHOD, None),
        )
        self.assertEqual(
            found[("main.py", 7, "self.missing")],
            ("unresolved", None, None, "not_declared_in_class"),
        )
        self.assertEqual(
            found[("main.py", 11, "twice")],
            ("ambiguous", None, None, "multiple_declarations"),
        )
        self.assertEqual(
            found[("main.py", 12, "undefined")],
            ("unresolved", None, None, "unbound_name"),
        )
        self.assertEqual(
            found[("main.py", 13, "helper")],
            ("linked", "main.py::helper", CALL_LEXICAL_NAME, None),
        )
        self.assertEqual(
            found[("main.py", 14, "self.outside")],
            ("unresolved", None, None, "outside_class"),
        )

    def test_names_bound_in_functions_shadow_module_declarations(self):
        source = (
            "import json\n"  # 1
            "def f(): pass\n"  # 2
            "def loop(fs):\n"  # 3
            "    for f in fs:\n"  # 4
            "        f()\n"  # 5
            "def param(f, json):\n"  # 6
            "    f()\n"  # 7
            "    json.dumps()\n"  # 8
            "def assigned():\n"  # 9
            "    f()\n"  # 10 a name assigned anywhere is local throughout
            "    f = 1\n"  # 11
            "def declared_global():\n"  # 12
            "    global f\n"  # 13
            "    f = 2\n"  # 14
            "    f()\n"  # 15
            "def closure():\n"  # 16
            "    with open() as f:\n"  # 17
            "        def inner():\n"  # 18
            "            f()\n"  # 19
            "def other_scopes():\n"  # 20
            "    g = lambda f: f()\n"  # 21
            "    [f() for f in fs]\n"  # 22
            "    [x for x in f()]\n"  # 23 the first iterable is outside
            "    def nested(f): pass\n"  # 24 a parameter of another scope
            "    f()\n"  # 25
            "class C:\n"  # 26
            "    f = 1\n"  # 27
            "    def m(self):\n"  # 28
            "        f()\n"  # 29 a class body is not an enclosing scope
            "        self.m()\n"  # 30
            "def caught():\n"  # 31
            "    try: pass\n"  # 32
            "    except E as f: f()\n"  # 33
            "def walrus():\n"  # 34
            "    [(f := y) for y in ys]\n"  # 35
            "    f()\n"  # 36
            "def deleted():\n"  # 37
            "    del f\n"  # 38
            "    f()\n"  # 39
            "def unpacked():\n"  # 40
            "    a, (b, *f) = 1, (2, 3)\n"  # 41
            "    f()\n"  # 42
            "def attribute(o):\n"  # 43
            "    o.f = 1\n"  # 44
            "    f()\n"  # 45
            "def matched(v):\n"  # 46
            "    match v:\n"  # 47
            "        case f: f()\n"  # 48
            "def defaults(f=f()):\n"  # 49 defaults are evaluated outside
            "    pass\n"  # 50
            "def typed(*, f: int = 0, **json):\n"  # 51
            "    f()\n"  # 52
            "    json.dumps()\n"  # 53
            "def annotated(f: int):\n"  # 54
            "    f()\n"  # 55
            "def augmented():\n"  # 56
            "    f += 1\n"  # 57
            "    f()\n"  # 58
            "def leaks():\n"  # 59
            "    def inner():\n"  # 60
            "        f = 1\n"  # 61 binds in inner only
            "    f()\n"  # 62
            "def same_name(fs):\n"  # 63
            "    return [f for f in f()]\n"  # 64
            "def outer_bound():\n"  # 65
            "    f = 1\n"  # 66
            "    def inner():\n"  # 67
            "        global f\n"  # 68
            "        f()\n"  # 69
            "def value_pattern(v):\n"  # 70
            "    match v:\n"  # 71
            "        case f.x: f()\n"  # 72
            "def in_lambda():\n"  # 73
            "    return lambda: [(f := 1), f()]\n"  # 74
            "def nested_capture(v):\n"  # 75
            "    match v:\n"  # 76
            "        case [f]: f()\n"  # 77
            "def declared_and_assigned():\n"  # 78
            "    def f(): pass\n"  # 79
            "    f()\n"  # 80 the nested declaration, as before
            "    f = 1\n"  # 81
        )
        found = links(analyze({"main.py": source}, "python"))
        local = ("unresolved", None, None, "local_binding")
        module = ("linked", "main.py::f", CALL_LEXICAL_NAME, None)
        expected = {
            5: local,
            7: local,
            10: local,
            15: module,
            19: local,
            21: local,
            22: local,
            23: module,
            25: module,
            29: module,
            33: local,
            36: local,
            39: local,
            42: local,
            45: module,
            48: local,
            49: module,
            52: local,
            55: local,
            58: local,
            62: module,
            64: module,
            69: module,
            72: module,
            74: local,
            77: local,
            80: ("linked", "main.py::declared_and_assigned.f", CALL_LEXICAL_NAME, None),
        }
        self.assertEqual(
            {line: found[("main.py", line, "f")] for line in expected}, expected
        )
        self.assertEqual(found[("main.py", 8, "json.dumps")], local)
        self.assertEqual(found[("main.py", 53, "json.dumps")], local)
        self.assertEqual(
            found[("main.py", 30, "self.m")],
            ("linked", "main.py::C.m", CALL_SELF_METHOD, None),
        )
        # An import in a function is resolved as an import, as before.
        imported = links(
            analyze(
                {
                    "m.py": "def g(): pass\n",
                    "main.py": "def run():\n    from m import g\n    g()\n",
                },
                "python",
            )
        )
        self.assertEqual(imported[("main.py", 3, "g")][:2], ("linked", "m.py::g"))

    def test_python_super_inherited_self_and_package_imports(self):
        files = {
            "pkg/__init__.py": "from .core import echo\n",
            "pkg/core.py": (
                "def echo(): pass\n"
                "class Base:\n"
                "    def run(self): pass\n"
                "    def help(self): pass\n"
                "class Mid(Base):\n"
                "    def run(self): pass\n"
            ),
            "pkg/types.py": "def convert(): pass\n",
            "pkg/util.py": "def tidy(): pass\n",
            "pkg/app.py": (
                "from . import types\n"
                "from .core import Mid, Base\n"
                "class Leaf(Mid):\n"
                "    def run(self):\n"
                "        super().run()\n"  # 5: Mid's run
                "        super().help()\n"  # 6: inherited by Mid from Base
                "        self.help()\n"  # 7: inherited
                "        types.convert()\n"  # 8: a submodule
                "        def inner():\n"
                "            super().run()\n"  # 10: not directly in a method
                "class Free(object):\n"
                "    def go(self):\n"
                "        super().go()\n"  # 13: object declares nothing here
                "        self.missing()\n"  # 14: declared nowhere
                "class Outside(Unknown):\n"
                "    def go(self):\n"
                "        super().go()\n"  # 17: a base outside the repository
                "        self.go2()\n"  # 18: same
            ),
            "tests/test_echo.py": (
                "import pkg.util\n"
                "pkg.echo()\n"  # 2: `import pkg.util` binds pkg
                "pkg.util.tidy()\n"  # 3
            ),
        }
        found = links(analyze(files, "python"))
        app = {
            (line, callee): value
            for (file, line, callee), value in found.items()
            if file == "pkg/app.py" and callee != "super"
        }
        self.assertEqual(
            app,
            {
                (5, "super().run"): (
                    "linked",
                    "pkg/core.py::Mid.run",
                    PYTHON_CALL_SUPER,
                    None,
                ),
                (6, "super().help"): (
                    "linked",
                    "pkg/core.py::Base.help",
                    PYTHON_CALL_SUPER,
                    None,
                ),
                (7, "self.help"): (
                    "linked",
                    "pkg/core.py::Base.help",
                    PYTHON_CALL_SELF_INHERITED,
                    None,
                ),
                (8, "types.convert"): (
                    "linked",
                    "pkg/types.py::convert",
                    CALL_MODULE_ATTRIBUTE,
                    None,
                ),
                (10, "super().run"): ("unresolved", None, None, "outside_class"),
                (13, "super().go"): ("unresolved", None, None, "not_declared_on_type"),
                (14, "self.missing"): (
                    "unresolved",
                    None,
                    None,
                    "not_declared_in_class",
                ),
                (17, "super().go"): ("unresolved", None, None, "base_not_resolved"),
                (18, "self.go2"): ("unresolved", None, None, "base_not_resolved"),
            },
        )
        self.assertEqual(
            found[("tests/test_echo.py", 2, "pkg.echo")][:2],
            ("linked", "pkg/core.py::echo"),
        )
        self.assertEqual(
            found[("tests/test_echo.py", 3, "pkg.util.tidy")][:2],
            ("linked", "pkg/util.py::tidy"),
        )

    def test_python_overload_signatures_yield_to_the_implementation(self):
        result = analyze(
            {
                "lib.py": (
                    "import typing\n"
                    "import typing as t\n"
                    "from typing import overload\n"
                    "@overload\n"
                    "def one(x: int) -> int: ...\n"
                    "@t.overload\n"
                    "def one(x: str) -> str: ...\n"
                    "def one(x): return x\n"
                    "@typing.overload\n"
                    "def stub(x: int) -> int: ...\n"
                    "@typing.overload\n"
                    "def stub(x: str) -> str: ...\n"
                    "@overload\n"
                    "def two(x: int) -> int: ...\n"
                    "if flag:\n"
                    "    def two(x): return 1\n"
                    "else:\n"
                    "    def two(x): return 2\n"
                    "@typing_extensions.overload\n"
                    "def three(x: int) -> int: ...\n"
                    "@typing.overload\n"
                    "def three(x: str) -> str: ...\n"
                    "def three(x): return x\n"
                    "@cache\n"
                    "def plain(): pass\n"
                    "def plain(): pass\n"
                ),
                "main.py": (
                    "from lib import one, stub, two, plain, three\n"
                    "def go():\n"
                    "    one(1)\n"
                    "    stub(1)\n"
                    "    two(1)\n"
                    "    plain()\n"
                    "    three(1)\n"
                ),
            },
            "python",
        )
        definitions = {d.id: d for d in result.definitions}
        self.assertEqual(
            [(d.line, d.overload) for d in result.definitions if d.symbol == "one"],
            [(5, True), (7, True), (8, None)],
        )
        found = links(result)
        self.assertEqual(
            found[("main.py", 3, "one")][:3],
            ("linked", "lib.py::one", CALL_IMPORTED_NAME),
        )
        self.assertEqual(
            definitions[
                next(
                    r.target_id
                    for r in resolve_calls(result)
                    if r.target_id and definitions[r.target_id].symbol == "one"
                )
            ].line,
            8,
        )
        self.assertEqual(
            found[("main.py", 7, "three")][:2], ("linked", "lib.py::three")
        )
        with self.assertRaises(ValueError):
            replace(result.definitions[0], overload=False)
        for line, callee in ((4, "stub"), (5, "two"), (6, "plain")):
            with self.subTest(callee=callee):
                self.assertEqual(found[("main.py", line, callee)][0], "ambiguous")
        serialized = result.to_dict()["definitions"]
        self.assertEqual(
            [
                ("overload" in d, d.get("overload"))
                for d in serialized
                if d["symbol"] == "one"
            ],
            [(True, True), (True, True), (False, None)],
        )

    def test_python_reexports_are_followed_to_the_declaring_module(self):
        result = analyze(
            {
                "pkg/__init__.py": (
                    "from .core import run, Tool as Kit\n"
                    "from .relay import hop\n"
                    "from . import core\n"
                    "try:\n"
                    "    from .fast import speed\n"
                    "except ImportError:\n"
                    "    from .core import speed\n"
                    "from .loop import spin\n"
                    "def setup():\n"
                    "    from .core import late\n"
                    "class Holder:\n"
                    "    from .core import held\n"
                    "import pkg.core as twice\n"
                    "from .core import twice\n"
                ),
                "pkg/core.py": (
                    "def run(): pass\nclass Tool: pass\n"
                    "def speed(): pass\ndef late(): pass\ndef held(): pass\n"
                    "def twice(): pass\n"
                ),
                "pkg/fast.py": "def speed(): pass\n",
                # Two steps: pkg re-exports what pkg.relay re-exports.
                "pkg/relay.py": "from pkg.core import run as hop\n",
                "pkg/loop.py": "from pkg import spin\n",
                "main.py": (
                    "import pkg\n"
                    "from pkg import run, Kit\n"
                    "def go():\n"
                    "    pkg.run()\n"
                    "    run()\n"
                    "    Kit()\n"
                    "    pkg.hop()\n"
                    "    pkg.speed()\n"
                    "    pkg.spin()\n"
                    "    pkg.core()\n"
                    "    pkg.late()\n"
                    "    pkg.held()\n"
                    "    pkg.twice()\n"
                ),
            },
            "python",
        )
        found = links(result)
        for line, callee, target in (
            (4, "pkg.run", "pkg/core.py::run"),
            (5, "run", "pkg/core.py::run"),
            (6, "Kit", "pkg/core.py::Tool"),
            (7, "pkg.hop", "pkg/core.py::run"),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.py", line, callee)],
                    ("linked", target, CALL_PYTHON_REEXPORT, None),
                )
        for line, callee in (
            # Bound by two imports.
            (8, "pkg.speed"),
            # A cycle of re-exports.
            (9, "pkg.spin"),
            # `from . import core` binds a module.
            (10, "pkg.core"),
            # Imports in a function or class body are not module attributes.
            (11, "pkg.late"),
            (12, "pkg.held"),
            # Bound twice, once as a module.
            (13, "pkg.twice"),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.py", line, callee)],
                    ("unresolved", None, None, "module_attribute_not_declared"),
                )

    def test_javascript_imports_are_not_reexports(self):
        result = analyze(
            {
                "lib.js": "export function make() {}\n",
                "index.js": "import { make } from './lib.js';\n",
                "main.js": (
                    "import { make } from './index.js';\nfunction go() { make(); }\n"
                ),
            },
            "javascript",
        )
        self.assertEqual(
            links(result)[("main.js", 2, "make")],
            ("unresolved", None, None, "imported_name_not_declared"),
        )

    def test_import_bindings_follow_aliases_and_module_attributes(self):
        result = analyze(
            {
                "lib/__init__.py": "",
                "lib/tools.py": "def work(): pass\nclass Tool: pass\n",
                "main.py": (
                    "from lib.tools import work as run, Tool\n"
                    "import lib.tools\n"
                    "import lib.tools as t\n"
                    "from lib.tools import gone\n"
                    "import json\n"
                    "def go():\n"
                    "    run()\n"
                    "    Tool()\n"
                    "    lib.tools.work()\n"
                    "    t.work()\n"
                    "    gone()\n"
                    "    json.dumps()\n"
                ),
                "shadow.py": (
                    "from lib.tools import work\ndef work(): pass\ndef go():\n    work()\n"
                ),
            },
            "python",
        )
        found = links(result)
        for line, callee, target, rule in (
            (7, "run", "lib/tools.py::work", CALL_IMPORTED_NAME),
            (8, "Tool", "lib/tools.py::Tool", CALL_IMPORTED_NAME),
            (9, "lib.tools.work", "lib/tools.py::work", CALL_MODULE_ATTRIBUTE),
            (10, "t.work", "lib/tools.py::work", CALL_MODULE_ATTRIBUTE),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.py", line, callee)], ("linked", target, rule, None)
                )
        self.assertEqual(
            found[("main.py", 11, "gone")],
            ("unresolved", None, None, "imported_name_not_declared"),
        )
        self.assertEqual(
            found[("main.py", 12, "json.dumps")],
            ("unresolved", None, None, "module_attribute_not_declared"),
        )
        self.assertEqual(
            found[("shadow.py", 4, "work")],
            ("ambiguous", None, None, "multiple_bindings"),
        )

    def test_calls_on_locals_bound_to_a_call_whose_return_annotation_names_a_class(
        self,
    ):
        files = {
            "lib.py": (
                "import typing as t\n"
                "class Base:\n"
                "    def up(self): pass\n"
                "class Ctx(Base):\n"
                "    def run(self): pass\n"
                "    def child(self) -> 'Ctx': return Ctx()\n"
                "    def maybe(self) -> t.Optional['Ctx']: return None\n"
                "def make() -> Ctx: return Ctx()\n"
                "def plain(): return Ctx()\n"
                "def text() -> str: return ''\n"
            ),
            "main.py": (
                "from lib import Ctx, make, plain, text\n"
                "def f(c: Ctx):\n"
                "    a = make()\n"
                "    a.run(); a.up()\n"  # 4: declared, inherited
                "    b = c.child()\n"
                "    b.run()\n"  # 6: a method's return annotation
                "    d = b.maybe()\n"
                "    d.run()\n"  # 8: through a receiver that is one too
                "    e = plain()\n"
                "    e.run()\n"  # 10: no annotation
                "    s = text()\n"
                "    s.upper()\n"  # 12: not a repository class
                "    g = make()\n"
                "    g = plain()\n"
                "    g.run()\n"  # 15: two assignments
                "    h: Ctx | None = None\n"
                "    h = Ctx()\n"
                "    h.run()\n"  # 18: None, then the class
                "    a.missing()\n"  # 19
            ),
        }
        found = links(analyze(files, "python"))
        run = ("linked", "lib.py::Ctx.run", PYTHON_CALL_RESULT_RECEIVER, None)
        self.assertEqual(found[("main.py", 4, "a.run")], run)
        self.assertEqual(
            found[("main.py", 4, "a.up")],
            ("linked", "lib.py::Base.up", PYTHON_CALL_RESULT_RECEIVER, None),
        )
        self.assertEqual(found[("main.py", 6, "b.run")], run)
        self.assertEqual(found[("main.py", 8, "d.run")], run)
        for line, callee in ((10, "e.run"), (12, "s.upper"), (15, "g.run")):
            with self.subTest(line=line):
                self.assertEqual(found[("main.py", line, callee)][0], "unresolved")
        self.assertEqual(
            found[("main.py", 18, "h.run")],
            ("linked", "lib.py::Ctx.run", CALL_PYTHON_TYPED_RECEIVER, None),
        )
        self.assertEqual(
            found[("main.py", 19, "a.missing")],
            ("unresolved", None, None, "not_declared_on_type"),
        )

    def test_calls_through_instance_attributes_whose_class_is_declared(self):
        files = {
            "lib.py": (
                "import typing as t\n"
                "class Cmd:\n"
                "    def run(self): pass\n"
                "class Ctx:\n"
                "    command: Cmd\n"  # 5: declared in the class body
                "    parent: t.Optional['Ctx']\n"
                "    def __init__(self, command: Cmd, other: Cmd, n: int):\n"
                "        self.command = command\n"
                "        self.kept = command\n"  # 9: inferred from a parameter
                "        self.made = Cmd()\n"
                "        self.mixed = command\n"
                "        self.count = n\n"
                "        self.later = None\n"
                "        self.typed: t.Optional[Cmd] = None\n"
                "    def reset(self):\n"
                "        self.mixed = 3\n"  # 16: another assignment
                "        self.later = Cmd()\n"
                "    def go(self):\n"
                "        self.command.run()\n"  # 19
                "        self.parent.command.run()\n"
                "        self.kept.run()\n"  # 21
                "        self.made.run()\n"
                "        self.mixed.run()\n"  # 23: unknown
                "        self.count.run()\n"  # 24: not a class
                "        self.later.run()\n"  # 25: None, then the class
                "        self.typed.run()\n"
                "        self.command.missing()\n"  # 27
                "    @staticmethod\n"
                "    def s(self):\n"
                "        self.command.run()\n"  # 30: not an instance
                "class Sub(Ctx):\n"
                "    def more(self):\n"
                "        self.command.run()\n"  # 33: an inherited attribute
                "def use(c: Ctx):\n"
                "    c.command.run()\n"  # 35: from a typed parameter
                "    c.nothing.run()\n"
                "class Holder:\n"
                "    wrapped: Cmd[int]\n"
                "    items: list[Cmd]\n"
                "    def go(self):\n"
                "        self.wrapped.run()\n"  # 41: a generic class
                "        self.items.run()\n"  # 42: no repository class
            ),
        }
        found = links(analyze(files, "python"))
        run = ("linked", "lib.py::Cmd.run", PYTHON_CALL_ATTRIBUTE_RECEIVER, None)
        for line, callee in (
            (19, "self.command.run"),
            (20, "self.parent.command.run"),
            (21, "self.kept.run"),
            (22, "self.made.run"),
            (25, "self.later.run"),
            (26, "self.typed.run"),
            (33, "self.command.run"),
            (35, "c.command.run"),
            (41, "self.wrapped.run"),
        ):
            with self.subTest(line=line):
                self.assertEqual(found[("lib.py", line, callee)], run)
        for line, callee in (
            (23, "self.mixed.run"),
            (24, "self.count.run"),
            (30, "self.command.run"),
            (36, "c.nothing.run"),
            (42, "self.items.run"),
        ):
            with self.subTest(line=line):
                self.assertNotEqual(found[("lib.py", line, callee)][0], "linked")
        self.assertEqual(
            found[("lib.py", 27, "self.command.missing")],
            ("unresolved", None, None, "not_declared_on_type"),
        )

    def test_calls_on_parameters_and_locals_whose_binding_names_a_class(self):
        files = {
            "lib.py": (
                "class Base:\n"
                "    def up(self): pass\n"
                "class Other:\n"
                "    def up(self): pass\n"
                "class Formatter(Base):\n"
                "    def write(self): pass\n"
                "class Both(Formatter, Other): pass\n"
                "class Ext(Exception): pass\n"
                "def make(): return Formatter()\n"
                "class Left(Base): pass\n"
                "class Right(Base):\n"
                "    def up(self): pass\n"
                "class Diamond(Left, Right): pass\n"
                "class Plain(object):\n"
                "    def up(self): pass\n"
                "class FromPlain(Plain): pass\n"
            ),
            "pkg/__init__.py": "from lib import Formatter\n",
            "main.py": (
                "import typing as t\n"
                "import lib\n"
                "from lib import Formatter, Both, Ext, make, Diamond, FromPlain\n"
                "from pkg import Formatter as F2\n"
                "def f(a: Formatter, b: 'Formatter', c: t.Optional[Formatter],\n"
                "      d: Formatter | None, e: lib.Formatter, g: F2, h, *i: Formatter,\n"
                "      j: list[Formatter] = None, k: Both = None, n: Ext = None,\n"
                "      o: str = '', p: Formatter = None, q: Formatter = None):\n"
                "    a.write(); b.write(); c.write(); d.write()\n"  # 9
                "    e.write(); g.write(); a.up()\n"  # 10
                "    h.write(); j.write(); k.up(); n.with_traceback(None)\n"  # 11
                "    o.upper(); a.missing()\n"  # 12
                "    x = Formatter()\n"
                "    x.write()\n"  # 14
                "    y: Formatter = make()\n"
                "    y.write()\n"  # 16
                "    z = make()\n"
                "    z.write()\n"  # 18
                "    w = Formatter()\n"
                "    w = make()\n"
                "    w.write()\n"  # 21
                "    p = None\n"
                "    p.write()\n"  # 23
                "    for v in []: pass\n"
                "    v.write()\n"  # 25
                "    def inner(q):\n"
                "        q.write()\n"  # 27: inner's own q
                "    def inner2():\n"
                "        a.write()\n"  # 29: the enclosing parameter
                "    lambda a: a.write()\n"  # 30
                "def g(Formatter: object):\n"
                "    u = Formatter()\n"
                "    u.write()\n"  # 33: a local name, not the class
                "def m(a: Formatter):\n"
                "    import os\n"
                "    a.write()\n"  # 36: an import in the body
                "def r(h, dd: Diamond, fp: FromPlain):\n"
                "    h.write()\n"  # 38: before h is rebound
                "    h = Formatter()\n"
                "    [s.write() for s in [Formatter()]]\n"  # 40
                "    dd.up(); fp.up(); fp.nothing()\n"  # 41
            ),
        }
        found = links(analyze(files, "python"))
        write = ("linked", "lib.py::Formatter.write", CALL_PYTHON_TYPED_RECEIVER, None)
        for line, callee in (
            (9, "a.write"),
            (9, "b.write"),
            (9, "c.write"),
            (9, "d.write"),
            (10, "e.write"),
            (10, "g.write"),
            (14, "x.write"),
            (16, "y.write"),
            (29, "a.write"),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(found[("main.py", line, callee)], write)
        self.assertEqual(
            found[("main.py", 10, "a.up")],
            ("linked", "lib.py::Base.up", CALL_PYTHON_TYPED_RECEIVER, None),
        )
        local = ("unresolved", None, None, "local_binding")
        for line, callee in (
            (11, "h.write"),
            (11, "j.write"),
            (18, "z.write"),
            (21, "w.write"),
            (23, "p.write"),
            (25, "v.write"),
            (27, "q.write"),
            (30, "a.write"),
            (33, "u.write"),
            (36, "a.write"),
            (38, "h.write"),
            (40, "s.write"),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(found[("main.py", line, callee)], local)
        self.assertEqual(
            found[("main.py", 12, "a.missing")],
            ("unresolved", None, None, "not_declared_on_type"),
        )
        # `str` and `make` name no repository class: the call is a local's.
        self.assertEqual(found[("main.py", 12, "o.upper")], local)
        # Both(Formatter, Other): Formatter's base Base comes before Other.
        self.assertEqual(
            found[("main.py", 11, "k.up")],
            ("linked", "lib.py::Base.up", CALL_PYTHON_TYPED_RECEIVER, None),
        )
        # Diamond's resolution order (Left, Right, Base) is not depth first.
        self.assertEqual(
            found[("main.py", 41, "dd.up")], ("unresolved", None, None, "diamond")
        )
        # `object` declares nothing a class calls by name.
        self.assertEqual(
            found[("main.py", 41, "fp.up")],
            ("linked", "lib.py::Plain.up", CALL_PYTHON_TYPED_RECEIVER, None),
        )
        self.assertEqual(
            found[("main.py", 41, "fp.nothing")],
            ("unresolved", None, None, "not_declared_on_type"),
        )
        # A base outside the repository may declare the method.
        self.assertEqual(
            found[("main.py", 11, "n.with_traceback")],
            ("unresolved", None, None, "base_not_resolved"),
        )

    def test_typed_receivers_need_one_class_the_annotation_names(self):
        files = {
            "lib.py": (
                "class Formatter:\n"
                "    def write(self): pass\n"
                "class Nested:\n"
                "    class write: pass\n"
            ),
            "lib2.py": "class Formatter:\n    def write(self): pass\n",
            "twice.py": (
                "import sys\n"
                "if sys.flags.debug:\n"
                "    class Twice:\n"
                "        def write(self): pass\n"
                "else:\n"
                "    class Twice:\n"
                "        def write(self): pass\n"
                "def f(t: Twice):\n"
                "    t.write()\n"  # 9: two classes named Twice
            ),
            "main.py": (
                "try:\n"
                "    import lib as L\n"
                "except ImportError:\n"
                "    import lib2 as L\n"
                "from lib import Formatter, Nested\n"
                "class Formatter:\n"
                "    def write(self): pass\n"
                "def f(a: L.Formatter, b: Formatter, n: Nested):\n"
                "    a.write()\n"  # 9: L is one of two modules
                "    b.write()\n"  # 10: imported and declared here
                "    n.write()\n"  # 11: a nested class, not a method
            ),
            "outer.py": (
                "from lib import Formatter\n"
                "class Outer:\n"
                "    class Formatter:\n"
                "        def write(self): pass\n"
                "    def m(self, a: Formatter):\n"
                "        a.write()\n"  # 6: the annotation sees Outer.Formatter
                "def make():\n"
                "    class Local:\n"
                "        def write(self): pass\n"
                "    def use(a: Local):\n"
                "        a.write()\n"  # 11: a class of the enclosing function
            ),
        }
        found = links(analyze(files, "python"))
        # Each is left to the other rules, which see a parameter.
        for key in (
            ("twice.py", 9, "t.write"),
            ("main.py", 9, "a.write"),
            ("main.py", 10, "b.write"),
            ("outer.py", 6, "a.write"),
        ):
            with self.subTest(key=key):
                self.assertEqual(
                    found[key], ("unresolved", None, None, "local_binding")
                )
        self.assertEqual(
            found[("outer.py", 11, "a.write")],
            ("linked", "outer.py::make.Local.write", CALL_PYTHON_TYPED_RECEIVER, None),
        )
        self.assertEqual(
            found[("main.py", 11, "n.write")],
            ("unresolved", None, None, "not_declared_on_type"),
        )


@unittest.skipUnless(TYPESCRIPT_READY, "Provision the typescript extra")
class TypeScriptCallTests(unittest.TestCase):
    def test_default_imports_call_the_default_export(self):
        result = analyze(
            {
                "fn.ts": "export default async function delay() {}\n",
                "late.ts": "function wait() {}\nexport default wait;\n",
                "clause.ts": "function pause() {}\nexport { pause as default };\n",
                "anon.ts": "export default () => {};\n",
                "none.ts": "export function other() {}\n",
                "fwd.ts": "export { default } from './fn';\n",
                "named.ts": "export function wait() {}\n",
                "fwd2.ts": "function wait() {}\nexport { wait as default } from './named';\n",
                "iface.ts": "export default interface Shape {}\n",
                "nested.ts": (
                    "function wait() {}\n"
                    "if (true) { function wait() {} }\n"
                    "export default wait;\n"
                ),
                "main.ts": (
                    "import delay from './fn';\n"
                    "import wait from './late';\n"
                    "import pause from './clause';\n"
                    "import anon from './anon';\n"
                    "import other from './none';\n"
                    "import again from './fwd';\n"
                    "import shape from './iface';\n"
                    "import viaFn from './fwd2';\n"
                    "delay(); wait(); pause(); anon(); other(); again();\n"
                    "shape(); viaFn();\n"
                ),
            },
            "typescript",
        )
        found = links(result)
        for callee, target in (
            ("delay", "fn.ts::delay"),
            ("wait", "late.ts::wait"),
            ("pause", "clause.ts::pause"),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.ts", 9, callee)],
                    ("linked", target, CALL_IMPORTED_NAME, None),
                )
        for line, callee, target in (
            (9, "again", "fn.ts::delay"),
            (10, "viaFn", "named.ts::wait"),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.ts", line, callee)],
                    ("linked", target, CALL_JS_REEXPORT, None),
                )
        for line, callee in ((9, "anon"), (9, "other"), (10, "shape")):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.ts", line, callee)],
                    ("unresolved", None, None, "imported_name_not_declared"),
                )
        defaults = {(d.file, d.line) for d in result.definitions if d.default_export}
        self.assertEqual(
            defaults,
            {
                ("fn.ts", 1),
                ("late.ts", 1),
                ("clause.ts", 1),
                ("nested.ts", 1),
                ("iface.ts", 1),
            },
        )

    def test_static_methods_of_a_class_named_by_the_callee(self):
        result = analyze(
            {
                "err.ts": (
                    "export class NonError {\n"
                    "  static wrap(x: unknown) {}\n"
                    "  plain() {}\n"
                    "}\n"
                    "export default class Ky { static create() {} }\n"
                    "export function helper() {}\n"
                ),
                "main.ts": (
                    "import { NonError } from './err';\n"
                    "import Ky, { helper } from './err';\n"
                    "class Local { static make() {} run() { this.run(); } }\n"
                    "NonError.wrap(1); NonError.plain(); Ky.create(); Local.make();\n"
                    "helper.call(null); NonError.wrap.call(null, 1);\n"
                    "function f(NonError: any) { NonError.wrap(2); }\n"
                    "function g() { const Local = other; Local.make(); }\n"
                    "Missing.wrap(); Local.x.make();\n"
                ),
            },
            "typescript",
        )
        found = links(result)
        for line, callee, target in (
            (4, "NonError.wrap", "err.ts::NonError.wrap"),
            (4, "Ky.create", "err.ts::Ky.create"),
            (4, "Local.make", "main.ts::Local.make"),
        ):
            with self.subTest(callee=callee, line=line):
                self.assertEqual(
                    found[("main.ts", line, callee)],
                    ("linked", target, CALL_JS_STATIC_METHOD, None),
                )
        for line, callee, reason in (
            (4, "NonError.plain", "static_not_declared"),
            (5, "helper.call", "attribute_call"),
            (5, "NonError.wrap.call", "attribute_call"),
            (6, "NonError.wrap", "attribute_call"),
            (7, "Local.make", "attribute_call"),
            (8, "Missing.wrap", "attribute_call"),
            (8, "Local.x.make", "attribute_call"),
        ):
            with self.subTest(callee=callee, line=line):
                self.assertEqual(
                    found[("main.ts", line, callee)], ("unresolved", None, None, reason)
                )
        # `this` is no name a scope declares.
        self.assertEqual(
            [s.binding for s in result.call_sites if s.callee == "this.run"], [None]
        )
        statics = {d.symbol for d in result.definitions if d.static}
        self.assertEqual(statics, {"NonError.wrap", "Ky.create", "Local.make"})

    def test_anonymous_function_expressions_take_their_variable_name(self):
        result = analyze(
            {
                "gen.ts": (
                    "function leaf() {}\n"
                    "export const walk = function* () { leaf(); };\n"
                    "const plain = function () { leaf(); };\n"
                    "const named = function inner() { leaf(); };\n"
                    "walk(); plain(); named();\n"
                ),
            },
            "typescript",
        )
        callers = {
            (s.line, s.callee): next(
                (d.symbol for d in result.definitions if d.id == s.caller_id), None
            )
            for s in result.call_sites
        }
        self.assertEqual(callers[(2, "leaf")], "walk")
        self.assertEqual(callers[(3, "leaf")], "plain")
        # A named function expression is not given the variable's name.
        self.assertIsNone(callers[(4, "leaf")])
        found = links(result)
        self.assertEqual(found[("gen.ts", 5, "walk")][:2], ("linked", "gen.ts::walk"))
        self.assertEqual(found[("gen.ts", 5, "plain")][:2], ("linked", "gen.ts::plain"))

    def test_new_links_to_the_constructor_the_class_declares(self):
        result = analyze(
            {
                "lib.ts": (
                    "export class Ky { constructor(x: number) {} }\n"
                    "export class Plain {}\n"
                    "export class Sub extends Ky {}\n"
                    "export class Over {\n"
                    "  constructor(x: number);\n"
                    "  constructor(x: any) {}\n"
                    "}\n"
                    "export function Legacy() { const o = { constructor() {} }; }\n"
                ),
                "main.ts": (
                    "import { Ky, Plain, Sub, Over, Legacy } from './lib';\n"
                    "new Ky(1); new Plain(); new Sub(2); new Over(3); new Legacy();\n"
                    "Ky(3);\n"
                ),
            },
            "typescript",
        )
        found = links(result)
        for callee, target in (
            ("Ky", "lib.ts::Ky.constructor"),
            ("Plain", "lib.ts::Plain"),
            ("Sub", "lib.ts::Sub"),
            ("Over", "lib.ts::Over"),
            ("Legacy", "lib.ts::Legacy"),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    [
                        v[:3]
                        for (f, line, c), v in found.items()
                        if (f, line, c) == ("main.ts", 2, callee)
                    ],
                    [("linked", target, CALL_IMPORTED_NAME)],
                )
        # Only a construction runs the constructor.
        self.assertEqual(found[("main.ts", 3, "Ky")][:2], ("linked", "lib.ts::Ky"))

    def test_typescript_bindings_constructors_and_this_methods(self):
        result = analyze(
            {
                "lib.ts": "export function make() {}\nexport class Box {}\n",
                "main.ts": (
                    "import { make as build, Box } from './lib';\n"
                    "import * as lib from './lib';\n"
                    "import Def from './lib';\n"
                    "class Shelf {\n"
                    "  add() { this.store(); new Box(); }\n"
                    "  store() {}\n"
                    "}\n"
                    "export const go = () => { build(); lib.make(); Def(); obj?.x(); };\n"
                ),
            },
            "typescript",
        )
        found = links(result)
        for line, callee, target, rule in (
            (5, "this.store", "main.ts::Shelf.store", CALL_SELF_METHOD),
            (5, "Box", "lib.ts::Box", CALL_IMPORTED_NAME),
            (8, "build", "lib.ts::make", CALL_IMPORTED_NAME),
            (8, "lib.make", "lib.ts::make", CALL_MODULE_ATTRIBUTE),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.ts", line, callee)], ("linked", target, rule, None)
                )
        # lib.ts exports no default.
        self.assertEqual(
            found[("main.ts", 8, "Def")],
            ("unresolved", None, None, "imported_name_not_declared"),
        )
        self.assertNotIn(("main.ts", 8, "obj.x"), found)
        kinds = {(s.callee, s.kind) for s in result.call_sites}
        self.assertIn(("Box", "new"), kinds)

    def test_typescript_private_names_name_the_innermost_class(self):
        result = analyze(
            {
                "main.ts": (
                    "class Outer {\n"
                    "  #run() {}\n"
                    "  go(o: Outer) {\n"
                    "    this.#run();\n"  # 4
                    "    o.#run();\n"  # 5: a private name, whatever `o` is
                    "    o.inner.#run();\n"  # 6
                    "    o.run();\n"  # 7: not a private name, and Outer has no run
                    "    class Inner {\n"
                    "      #other() {}\n"
                    "      m(o: Outer) { o.#run(); o.#other(); }\n"  # 10
                    "    }\n"
                    "  }\n"
                    "}\n"
                ),
            },
            "typescript",
        )
        found = links(result)
        run = "main.ts::Outer.#run"
        self.assertEqual(
            found[("main.ts", 4, "this.#run")], ("linked", run, CALL_SELF_METHOD, None)
        )
        for line, callee in ((5, "o.#run"), (6, "o.inner.#run")):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.ts", line, callee)],
                    ("linked", run, CALL_PRIVATE_NAME, None),
                )
        self.assertEqual(
            found[("main.ts", 7, "o.run")],
            ("unresolved", None, None, "not_declared_on_type"),
        )
        # Inner declares no #run: the name is Outer's, which is not followed.
        self.assertEqual(
            found[("main.ts", 10, "o.#run")],
            ("unresolved", None, None, "not_declared_in_class"),
        )
        self.assertEqual(
            found[("main.ts", 10, "o.#other")],
            ("linked", "main.ts::Outer.go.Inner.#other", CALL_PRIVATE_NAME, None),
        )

    def test_receivers_typed_by_their_declarations(self):
        result = analyze(
            {
                "base.ts": (
                    "export class Base {\n"
                    "  route(p: string) { return this }\n"
                    "  hidden() {}\n"
                    "  shared() {}\n"
                    "}\n"
                ),
                "app.ts": (
                    "import { Base } from './base';\n"
                    "import { Ext } from 'external';\n"
                    "export class App extends Base {\n"
                    "  hidden = () => 1;\n"
                    "  constructor(private shared: number, n: number) { super() }\n"
                    "  run() {}\n"
                    "  static make() {}\n"
                    "}\n"
                    "export class Wrapped extends Ext {}\n"
                    "export class Merged { a() {} }\n"
                    "export interface Merged { b(): void }\n"
                    "export class Lone {}\n"
                    "export class Store<T> { get(): T {} }\n"
                    "export class Impl implements Base {}\n"
                    "export class P extends Q {}\n"
                    "export class Q extends P {}\n"
                ),
                "main.ts": (
                    "import { App, Wrapped, Merged, Lone, Impl, P, Store } from './app';\n"
                    "import { Base } from './base';\n"
                    "interface Shape { area(): number }\n"
                    "const app = new App(1);\n"
                    "app.route('/'); app.run();\n"  # 5
                    "function f(a: App, b?: Base, g: Map<string, App>, s: Shape) {\n"
                    "  a.route('x'); b.route('y'); g.get('k'); s.area();\n"  # 7
                    "  a.hidden(); a.shared(); a.make(); a.missing();\n"  # 8
                    "}\n"
                    "let w: Wrapped = new Wrapped(); w.go();\n"  # 10
                    "const m = new Merged(); m.a();\n"  # 11
                    "const l = new Lone(); l.toString();\n"  # 12
                    "function h() {\n"
                    "  class App { own() {} }\n"
                    "  const local = new App(); local.own(); local.run();\n"  # 15
                    "  const made = make(); made.run();\n"  # 16
                    "  const { d } = { d: app }; d.run();\n"  # 17
                    "}\n"
                    "const i = new Impl(); i.route();\n"  # 19
                    "const p = new P(); p.zz();\n"  # 20
                    "function t(st: Store<number>) { st.get() }\n"  # 21
                    "const fn = new Lone(); fn();\n"  # 22
                ),
                "shadow.ts": (
                    "import { App } from './app';\n"
                    "function k(q: App) {\n"
                    "  type App = { route(): void };\n"
                    "  q.route();\n"
                    "}\n"
                ),
            },
            "typescript",
        )
        found = links(result)
        for line, callee, target in (
            (5, "app.route", "base.ts::Base.route"),
            (5, "app.run", "app.ts::App.run"),
            (7, "a.route", "base.ts::Base.route"),
            (7, "b.route", "base.ts::Base.route"),
            (15, "local.own", "main.ts::h.App.own"),
            (21, "st.get", "app.ts::Store.get"),
            # An interface declares the method signature called.
            (7, "s.area", "main.ts::Shape.area"),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.ts", line, callee)],
                    ("linked", target, CALL_JS_TYPED_RECEIVER, None),
                )
        for line, callee, reason in (
            # A field hides the method App inherits.
            (8, "a.hidden", "field_not_method"),
            (8, "a.shared", "field_not_method"),
            # A static method is not an instance's.
            (8, "a.make", "not_declared_on_type"),
            (8, "a.missing", "not_declared_on_type"),
            # Ext may declare go.
            (10, "w.go", "base_not_resolved"),
            (12, "l.toString", "not_declared_on_type"),
            (15, "local.run", "not_declared_on_type"),
            # Map is no repository class, Merged has members its interface
            # declares, and `made` and `d` write no type.
            (7, "g.get", "attribute_call"),
            (11, "m.a", "attribute_call"),
            (16, "made.run", "attribute_call"),
            (17, "d.run", "attribute_call"),
            (19, "i.route", "not_declared_on_type"),
            (20, "p.zz", "base_not_resolved"),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.ts", line, callee)], ("unresolved", None, None, reason)
                )
        # Only `x.m()` reads the type of `x`.
        self.assertEqual(
            [s.receiver_type for s in result.call_sites if s.callee == "fn"], [None]
        )
        # A type alias may be the App that `q` is annotated with.
        self.assertEqual(
            found[("shadow.ts", 4, "q.route")],
            ("unresolved", None, None, "attribute_call"),
        )
        apps = {d.symbol: d.properties for d in result.definitions}
        self.assertEqual(apps["App"], ("hidden", "shared"))
        self.assertIsNone(apps["Base"])
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)
        # Unset fields are left out of artifacts.
        self.assertNotIn('"properties": null', result.to_json())
        self.assertNotIn('"receiver_binding": null', result.to_json())
        site = dict(id="c", file="a.ts", callee="x.m", kind="call")
        site.update(line=1, column=0, end_line=1, end_column=5)
        for bad in (
            dict(receiver_binding=(1, 0)),
            dict(receiver_type="T", receiver_binding=(0, 0)),
            dict(receiver_type="T", receiver_binding=(1,)),
            dict(receiver_type="T", receiver_binding=(1, -1)),
            dict(receiver_type="T", receiver_binding=[1, 0]),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CallSite(**site, **bad)
        CallSite(**site, receiver_type="T", receiver_binding=())
        owner = next(d for d in result.definitions if d.symbol == "App")
        for bad in ((), ("",), ["x"], (1,)):
            with self.subTest(properties=bad), self.assertRaises(ValueError):
                replace(owner, properties=bad)

    def test_receivers_typed_by_interfaces(self):
        result = analyze(
            {
                "router.ts": (
                    "export interface Router<T> {\n"
                    "  add(path: string, h: T): void\n"
                    "  handler: () => void\n"
                    "}\n"
                    "export interface Named extends Router<string> { name(): string }\n"
                    "export interface Wide extends Named, External { wide(): void }\n"
                    "export interface Loop extends Back {}\n"
                    "export interface Back extends Loop {}\n"
                    "export class Impl implements Router<string> { add() {} handler = () => {} }\n"
                ),
                "main.ts": (
                    "import type { Router, Named, Wide, Loop } from './router';\n"
                    "import { Impl } from './router';\n"
                    "interface Local { run(): void }\n"
                    "interface Twice { a(): void }\n"
                    "interface Twice { b(): void }\n"  # 5
                    "function f(r: Router<string>, n: Named, w: Wide, l: Local, t: Twice) {\n"
                    "  r.add('/', 'x'); n.add('/', 'y'); n.name(); w.add('/', 'z');\n"
                    "  w.wide(); l.run(); t.a(); r.handler(); r.gone();\n"
                    "}\n"
                    "function g(o: Router<string> | undefined, p: null | Local, q: Local | Impl) {\n"  # 10
                    "  o.add('/', 'u'); p.run(); q.run();\n"
                    "}\n"
                    "function h(z: Loop) { z.spin(); Local.run() }\n"
                    "let late: Router<number> | undefined = undefined;\n"
                    "late.add('/', 1);\n"  # 15
                ),
                "bases.ts": (
                    "interface A { m(): void }\n"
                    "interface B extends A {}\n"
                    "interface C extends A {}\n"
                    "interface D extends B, C {}\n"
                    "interface E extends B, Tail {}\n"  # 5
                    "interface Tail { t(): void }\n"
                    "class Base { inner: Tail; go() {} }\n"
                    "interface X extends Base {}\n"
                    "const Anon = class Hidden { m() {} };\n"
                    "function f(d: D, e: E, x: X, h: Hidden) {\n"  # 10
                    "  d.m(); e.t(); x.go(); x.inner.t(); h.m();\n"
                    "}\n"
                ),
                "param.ts": (
                    "interface Local { run(): void }\n"
                    "function k<Local>(v: Local) { v.run() }\n"
                ),
                "inner.ts": (
                    "interface Local { run(): void }\n"
                    "namespace N { export interface Local { other(): void } }\n"
                    "function m(l: Local) { l.run() }\n"
                ),
            },
            "typescript",
        )
        found = links(result)
        for line, callee, target in (
            # An imported interface, also through `extends`.
            (7, "r.add", "router.ts::Router.add"),
            (7, "n.add", "router.ts::Router.add"),
            (7, "n.name", "router.ts::Named.name"),
            (8, "w.wide", "router.ts::Wide.wide"),
            # A top-level interface of the file.
            (8, "l.run", "main.ts::Local.run"),
            # `T | undefined` and `T | null` hold a T where called.
            (11, "o.add", "router.ts::Router.add"),
            (11, "p.run", "main.ts::Local.run"),
            (15, "late.add", "router.ts::Router.add"),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(
                    found[("main.ts", line, callee)],
                    ("linked", target, CALL_JS_TYPED_RECEIVER, None),
                )
        for line, callee, reason in (
            # A base that is no repository interface may declare it.
            (7, "w.add", "base_not_resolved"),
            (13, "z.spin", "base_not_resolved"),
            # A property of a function type, and nothing declared.
            (8, "r.handler", "not_declared_on_type"),
            (8, "r.gone", "not_declared_on_type"),
            # Merged declarations, a union of two types, and an interface
            # used as a value.
            (8, "t.a", "attribute_call"),
            (11, "q.run", "attribute_call"),
            (13, "Local.run", "attribute_call"),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(
                    found[("main.ts", line, callee)], ("unresolved", None, None, reason)
                )
        # Every interface extended is searched, and one method reached
        # twice is one target.
        for callee, target in (("d.m", "bases.ts::A.m"), ("e.t", "bases.ts::Tail.t")):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("bases.ts", 11, callee)],
                    ("linked", target, CALL_JS_TYPED_RECEIVER, None),
                )
        # An interface extending a class, and a class expression's name,
        # which no enclosing scope declares.
        for callee, reason in (
            ("x.go", "base_not_resolved"),
            ("x.inner.t", "receiver_type_unknown"),
            ("h.m", "attribute_call"),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("bases.ts", 11, callee)], ("unresolved", None, None, reason)
                )
        # A type parameter of that name shadows the interface.
        self.assertEqual(
            found[("param.ts", 2, "v.run")],
            ("unresolved", None, None, "attribute_call"),
        )
        # An interface of that name in a namespace may be the one meant.
        self.assertEqual(
            found[("inner.ts", 3, "l.run")],
            ("unresolved", None, None, "attribute_call"),
        )

    def test_a_binding_that_declares_no_callable_links_nothing(self):
        result = analyze({"main.ts": "enum E { A }\nE();\n"}, "typescript")
        self.assertEqual(
            links(result)[("main.ts", 2, "E")],
            ("unresolved", None, None, "local_binding"),
        )

    def test_declarations_exported_under_another_name(self):
        result = analyze(
            {
                "base.ts": (
                    "class Hono { route() {} static make() {} }\n"
                    "function helper() {}\n"
                    "function twice() {}\n"
                    "function inner() { function hidden() {} }\n"
                    "export function both() {}\n"
                    "function other() {}\n"
                    "const arrow = () => {}\n"
                    "export { Hono as HonoBase, helper as help, Hono as default }\n"
                    "export { helper as again, twice as twice, other as both }\n"
                    "export { helper as help, hidden as shown, arrow as run }\n"
                    "export { both as alsoBoth }\n"
                    "export { helper as fromRelay } from './relay';\n"
                ),
                "relay.ts": (
                    "import { helper } from './base';\nexport { helper as relayed }\n"
                ),
                "main.ts": (
                    "import { HonoBase, help, again, both } from './base';\n"
                    "import * as m from './base';\n"
                    "import { relayed } from './relay';\n"
                    "export class App extends HonoBase {}\n"
                    "const a = new App(); a.route(); help(); new HonoBase();\n"  # 5
                    "m.help(); HonoBase.make(); again(); both(); relayed();\n"  # 6
                ),
            },
            "typescript",
        )
        found = links(result)
        for line, callee, target, rule in (
            (5, "a.route", "base.ts::Hono.route", CALL_JS_TYPED_RECEIVER),
            (5, "help", "base.ts::helper", CALL_IMPORTED_NAME),
            (5, "HonoBase", "base.ts::Hono", CALL_IMPORTED_NAME),
            (6, "m.help", "base.ts::helper", CALL_MODULE_ATTRIBUTE),
            (6, "HonoBase.make", "base.ts::Hono.make", CALL_JS_STATIC_METHOD),
            (6, "again", "base.ts::helper", CALL_IMPORTED_NAME),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.ts", line, callee)], ("linked", target, rule, None)
                )
        # A name declared and also exported for another declaration is
        # ambiguous; an imported name exported again is followed.
        self.assertEqual(found[("main.ts", 6, "both")][:2], ("ambiguous", None))
        self.assertEqual(
            found[("main.ts", 6, "relayed")],
            ("linked", "base.ts::helper", CALL_JS_REEXPORT, None),
        )
        aliases = {d.symbol: d.export_aliases for d in result.definitions}
        self.assertEqual(aliases["Hono"], ("HonoBase",))
        self.assertEqual(aliases["helper"], ("help", "again"))
        self.assertEqual(aliases["other"], ("both",))
        self.assertEqual(aliases["arrow"], ("run",))
        self.assertEqual(aliases["both"], ("alsoBoth",))
        # Exporting a name as itself, or as `default`, adds no alias, and
        # neither does `export { a as b } from` or a nested declaration.
        self.assertIsNone(aliases["twice"])
        self.assertIsNone(aliases["inner.hidden"])
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)
        self.assertNotIn('"export_aliases": null', result.to_json())
        for bad in ((), ("",), ["x"], (1,)):
            with self.subTest(export_aliases=bad), self.assertRaises(ValueError):
                replace(result.definitions[0], export_aliases=bad)

    def test_imports_exported_again(self):
        result = analyze(
            {
                "src/render.ts": (
                    "export function render() {}\n"
                    "export default function hydrate() {}\n"
                    "export function unused() {}\n"
                ),
                "src/index.ts": (
                    "import { render as r, unused } from './render';\n"
                    "import hyd from './render';\n"
                    "import * as ns from './render';\n"
                    "export { r as render, r, hyd as hydrate, ns };\n"
                    "export { r as render };\n"
                    "export default r;\n"
                    "function local() {}\n"
                    "export { local as alsoLocal };\n"
                    "export { unused as fromRender } from './render';\n"
                ),
                "main.ts": (
                    "import { render, r, hydrate, ns, unused } from './src/index';\n"
                    "import dflt from './src/index';\n"
                    "import * as idx from './src/index';\n"
                    "render(); r(); hydrate(); dflt(); idx.render();\n"  # 4
                    "ns.render(); unused();\n"  # 5
                ),
            },
            "typescript",
        )
        found = links(result)
        for callee, target in (
            ("render", "src/render.ts::render"),
            ("r", "src/render.ts::render"),
            ("hydrate", "src/render.ts::hydrate"),
            ("dflt", "src/render.ts::render"),
            ("idx.render", "src/render.ts::render"),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.ts", 4, callee)],
                    ("linked", target, CALL_JS_REEXPORT, None),
                )
        # A namespace exported again is not followed, and an import that is
        # not exported again exports nothing.
        self.assertEqual(found[("main.ts", 5, "ns.render")][0], "unresolved")
        self.assertEqual(
            found[("main.ts", 5, "unused")],
            ("unresolved", None, None, "imported_name_not_declared"),
        )
        imports = {(i.file, i.line): i.reexported_as for i in result.imports}
        self.assertEqual(
            imports[("src/index.ts", 1)],
            (("r", "render"), ("r", "r"), ("r", "default")),
        )
        self.assertEqual(imports[("src/index.ts", 2)], (("hyd", "hydrate"),))
        self.assertEqual(imports[("src/index.ts", 3)], (("ns", "ns"),))
        self.assertIsNone(imports[("main.ts", 1)])
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)
        self.assertNotIn('"reexported_as": null', result.to_json())
        item = next(i for i in result.imports if i.reexported_as)
        for bad in ((), (("r",),), (("x", "y"),), ((r"r", ""),), [("r", "s")]):
            with self.subTest(reexported_as=bad), self.assertRaises(ValueError):
                replace(item, reexported_as=bad)

    def test_javascript_receivers_bound_to_new(self):
        result = analyze(
            {
                "plain.js": (
                    "class Js { go() {} }\n"
                    "const j = new Js(); j.go();\n"
                    "class F { x = 1; static y = 2; #z = 3 }\n"
                ),
            },
            "javascript",
        )
        self.assertEqual(
            links(result)[("plain.js", 2, "j.go")],
            ("linked", "plain.js::Js.go", CALL_JS_TYPED_RECEIVER, None),
        )
        properties = {d.symbol: d.properties for d in result.definitions}
        self.assertEqual(properties["F"], ("x",))

    def test_member_chain_aliases_are_followed_four_deep(self):
        result = analyze(
            {
                "n.ts": (
                    "class N {\n"
                    "  next: N;\n"
                    "  run() {}\n"
                    "}\n"
                    "export function go() {\n"
                    "  const a = new N();\n"
                    "  const b = a.next;\n"
                    "  const c = b.next;\n"
                    "  const d = c.next;\n"
                    "  const e = d.next;\n"
                    "  e.run();\n"
                    "  const f = e.next;\n"
                    "  f.run();\n"
                    "}\n"
                ),
            },
            "typescript",
        )
        found = links(result)
        self.assertEqual(
            found[("n.ts", 11, "e.run")],
            ("linked", "n.ts::N.run", CALL_JS_MEMBER_CHAIN, None),
        )
        self.assertEqual(found[("n.ts", 13, "f.run")][0], "unresolved")

    def test_receivers_typed_through_fields_and_destructuring(self):
        result = analyze(
            {
                "req.ts": (
                    "export class Req {\n"
                    "  header(name: string): string\n"
                    "  header(): Record<string, string>\n"
                    "  header(name?: string): any { return name }\n"
                    "  static parse(): Req\n"
                    "  static parse(s?: string): Req { return new Req() }\n"
                    "}\n"
                    "export function f(a: string): void\n"
                    "export function f(a: number): void\n"
                    "export function f(a: any) {}\n"
                    "export function g(): void\n"
                ),
                "base.ts": (
                    "import { Req } from './req';\n"
                    "export class Base {\n"
                    "  req: Req;\n"
                    "  get head(): Req { return this.req }\n"
                    "  alt: Req;\n"
                    "  make(): Req { return this.req }\n"
                    "}\n"
                ),
                "main.ts": (
                    "import { Base } from './base';\n"
                    "import { Req, f } from './req';\n"
                    "import { Ext } from 'external';\n"
                    "interface Shape { area(): number }\n"
                    "class Svc { run() {} }\n"
                    "export class Ctx extends Base {\n"
                    "  made = new Svc();\n"
                    "  untyped = 1;\n"
                    "  ext: Ext;\n"
                    "  shape: Shape;\n"
                    "  #hidden: Svc;\n"
                    "  static svc: Svc;\n"
                    "  constructor(private svc: Svc, plain: Svc) { super() }\n"
                    "  go() {\n"
                    "    this.svc.run(); this.made.run(); this.req.header(); this.head.header();\n"
                    "    this.untyped.x(); this.ext.y(); this.shape.area(); this.#hidden.run();\n"
                    "    this.go.call(); super.req.header();\n"
                    "    const run = () => this.svc.run();\n"
                    "    function inner() { this.svc.run() }\n"
                    "    const { svc } = this; svc.run();\n"
                    "    const { req: r } = this; r.header();\n"
                    "  }\n"
                    "  static s() { this.svc.run() }\n"
                    "}\n"
                    "function use(c: Ctx) {\n"
                    "  c.req.header('x'); c.made.run(); c.svc.run();\n"
                    "  const { made } = c; made.run();\n"
                    "  const h = c.req; h.header();\n"
                    "  const same = c; same.go();\n"
                    "  c.plain.run(); c.nope.run();\n"
                    "  f(1); Req.parse();\n"
                    "}\n"
                ),
                "extra.ts": (
                    "import { Base } from './base';\n"
                    "import { Req } from './req';\n"
                    "enum Mode { run }\n"
                    "class Svc { run() {} }\n"
                    "export class Sub extends Base {\n"
                    "  alt = 1;\n"
                    "  mode: Mode;\n"
                    "  #hidden: Svc;\n"
                    "  get #g(): Req { return this.req }\n"
                    "  go() {\n"
                    "    this.alt.header(); this.mode.run(); this.make.header();\n"
                    "    const self = this; self.go();\n"
                    "    const p = this.#hidden; p.run();\n"
                    '    const { "req": q } = this; q.header();\n'
                    "    const { req: t }: Base = this; t.header();\n"
                    "  }\n"
                    "}\n"
                    "class Outer {\n"
                    "  x = new Svc();\n"
                    "  m() { return class { x: Req; go() { this.x.header() } } }\n"
                    "}\n"
                    "class Merged { a() { const self = this; self.a() } }\n"
                    "interface Merged { b(): void }\n"
                ),
                "tp.ts": (
                    "import { Req } from './req';\n"
                    "class Box<Req> { v: Req; go() { this.v.header() } }\n"
                    "function tp<Req>(s: Req) { s.header() }\n"
                ),
            },
            "typescript",
        )
        found = links(result)
        for line, callee, target in (
            # Fields annotated or constructed, parameter properties, an
            # inherited field typed in its own file, and a getter.
            (15, "this.svc.run", "main.ts::Svc.run"),
            (15, "this.made.run", "main.ts::Svc.run"),
            (15, "this.req.header", "req.ts::Req.header"),
            (15, "this.head.header", "req.ts::Req.header"),
            # An arrow function keeps the method's `this`.
            (18, "this.svc.run", "main.ts::Svc.run"),
            (20, "svc.run", "main.ts::Svc.run"),
            (21, "r.header", "req.ts::Req.header"),
            (26, "c.req.header", "req.ts::Req.header"),
            (26, "c.made.run", "main.ts::Svc.run"),
            (26, "c.svc.run", "main.ts::Svc.run"),
            (27, "made.run", "main.ts::Svc.run"),
            (28, "h.header", "req.ts::Req.header"),
            # A field typed with an interface.
            (16, "this.shape.area", "main.ts::Shape.area"),
        ):
            with self.subTest(line=line, callee=callee):
                # The implementation runs, not an overload signature.
                self.assertEqual(
                    found[("main.ts", line, callee)],
                    ("linked", target, CALL_JS_MEMBER_CHAIN, None),
                )
        for line, callee, reason in (
            # Untyped, external, a method, a parameter that is no property,
            # and nothing declared.
            (16, "this.untyped.x", "receiver_type_unknown"),
            (16, "this.ext.y", "receiver_type_unknown"),
            (17, "this.go.call", "receiver_type_unknown"),
            (30, "c.plain.run", "receiver_type_unknown"),
            (30, "c.nope.run", "receiver_type_unknown"),
            # Private names, `this` of a plain function or a static method,
            # and an alias without a member on the way are not followed.
            (16, "this.#hidden.run", "attribute_call"),
            (19, "this.svc.run", "attribute_call"),
            (23, "this.svc.run", "attribute_call"),
            (29, "same.go", "attribute_call"),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(
                    found[("main.ts", line, callee)], ("unresolved", None, None, reason)
                )
        self.assertEqual(
            found[("main.ts", 31, "f")],
            ("linked", "req.ts::f", CALL_IMPORTED_NAME, None),
        )
        self.assertEqual(
            found[("main.ts", 31, "Req.parse")],
            ("linked", "req.ts::Req.parse", CALL_JS_STATIC_METHOD, None),
        )
        targets = {(d.file, d.symbol, d.line): d for d in result.definitions}
        self.assertEqual(
            [targets[("req.ts", "Req.header", n)].overload for n in (2, 3, 4)],
            [True, True, None],
        )
        self.assertTrue(targets[("req.ts", "Req.parse", 5)].static)
        # Only a static overload is static.
        self.assertFalse(targets[("req.ts", "Req.header", 2)].static)
        fields = {d.symbol: d.fields for d in result.definitions if d.kind == "Class"}
        self.assertEqual(
            fields["Ctx"],
            (("made", "Svc"), ("ext", "Ext"), ("shape", "Shape"), ("svc", "Svc")),
        )
        self.assertEqual(
            fields["Base"], (("req", "Req"), ("head", "Req"), ("alt", "Req"))
        )
        steps = {
            (s.line, s.callee): (s.receiver_type, s.receiver_steps)
            for s in result.call_sites
        }
        self.assertEqual(steps[(15, "this.req.header")], ("this", ("req",)))
        self.assertEqual(steps[(28, "h.header")], ("Ctx", ("req",)))
        for file, line, callee, found_as in (
            # A field that hides a typed one, an enum, a method, and a type
            # parameter that shares a class's name.
            (
                "extra.ts",
                11,
                "this.alt.header",
                ("unresolved", None, None, "receiver_type_unknown"),
            ),
            (
                "extra.ts",
                11,
                "this.mode.run",
                ("unresolved", None, None, "receiver_type_unknown"),
            ),
            (
                "extra.ts",
                11,
                "this.make.header",
                ("unresolved", None, None, "receiver_type_unknown"),
            ),
            (
                "tp.ts",
                2,
                "this.v.header",
                ("unresolved", None, None, "receiver_type_unknown"),
            ),
            ("tp.ts", 3, "s.header", ("unresolved", None, None, "attribute_call")),
            (
                "extra.ts",
                12,
                "self.go",
                ("linked", "extra.ts::Sub.go", CALL_JS_MEMBER_CHAIN, None),
            ),
            # A merged class may have members its body does not declare.
            (
                "extra.ts",
                22,
                "self.a",
                ("unresolved", None, None, "receiver_type_unknown"),
            ),
            # Private names, quoted keys, an annotated pattern and the
            # `this` of a class expression are not followed.
            ("extra.ts", 13, "p.run", ("unresolved", None, None, "attribute_call")),
            ("extra.ts", 14, "q.header", ("unresolved", None, None, "attribute_call")),
            ("extra.ts", 15, "t.header", ("unresolved", None, None, "attribute_call")),
            (
                "extra.ts",
                20,
                "this.x.header",
                ("unresolved", None, None, "attribute_call"),
            ),
        ):
            with self.subTest(file=file, line=line, callee=callee):
                self.assertEqual(found[(file, line, callee)], found_as)
        self.assertIsNone(targets[("req.ts", "g", 11)].overload)
        self.assertEqual(fields["Sub"], (("mode", "Mode"),))
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)

    def test_javascript_fields_type_chains(self):
        result = analyze(
            {
                "plain.js": (
                    "class B { run() {} }\n"
                    "class A { b = new B(); go() { this.b.run() } }\n"
                    "class C { static b = new B(); go() { this.b.run() } }\n"
                ),
            },
            "javascript",
        )
        found = links(result)
        self.assertEqual(
            found[("plain.js", 2, "this.b.run")],
            ("linked", "plain.js::B.run", CALL_JS_MEMBER_CHAIN, None),
        )
        # A static field is no instance's.
        self.assertEqual(
            found[("plain.js", 3, "this.b.run")],
            ("unresolved", None, None, "receiver_type_unknown"),
        )


class CallSchemaTests(unittest.TestCase):
    def test_call_sites_need_capability_and_same_file_callers(self):
        base = dict(
            repository_id="r",
            snapshot_id="s",
            status="succeeded",
            definitions=(),
            coverage=(FileCoverage("a.py", "python", "analyzed"),),
            diagnostics=(),
            analyzer="tree-sitter",
            analyzer_versions=(),
            schema_version="3",
        )
        site = CallSite("c1", "a.py", "f", "call", 1, 0, 1, 3)
        with self.assertRaisesRegex(ValueError, "capability"):
            AnalysisResult(**base, call_sites=(site,))
        with self.assertRaisesRegex(ValueError, "same file"):
            AnalysisResult(
                **base,
                capabilities=("call_sites", "definitions"),
                call_sites=(CallSite("c2", "a.py", "f", "call", 1, 0, 1, 3, "x"),),
            )
        current = AnalysisResult(
            **base, capabilities=("call_sites", "definitions"), call_sites=(site,)
        )
        self.assertEqual(AnalysisResult.from_json(current.to_json()), current)
        for bad in (dict(kind="await"), dict(callee=""), dict(caller_id="")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CallSite(
                    **{
                        **dict(
                            id="c",
                            file="a.py",
                            callee="f",
                            kind="call",
                            line=1,
                            column=0,
                            end_line=1,
                            end_column=3,
                        ),
                        **bad,
                    }
                )


@unittest.skipUnless(
    PYTHON_READY and TYPESCRIPT_READY and importlib.util.find_spec("google.protobuf"),
    "Provision the python, typescript and scip extras",
)
class CallDifferentialTests(unittest.TestCase):
    def test_linked_calls_agree_with_scip_references_and_are_deterministic(self):
        """Independent check: compiler-backed SCIP indexes of the same fixtures."""
        for language in ("python", "typescript"):
            with self.subTest(language=language):
                result = analyze_repository(FIXTURES / language, repository_id="r")
                self.assertEqual(
                    resolve_calls(result),
                    resolve_calls(
                        analyze_repository(FIXTURES / language, repository_id="r")
                    ),
                )
                graph = deepthought.compose_graph(
                    [
                        deepthought.ingest_scip(
                            FIXTURES / "scip" / f"{language}.scip",
                            repository_id=language,
                        )
                    ]
                )
                scip_defs = {n.id: n.definition for n in graph.definitions}
                referenced = {
                    (r.reference.file, r.reference.line, r.reference.column): scip_defs[
                        r.target_ids[0]
                    ]
                    for r in graph.references
                    if r.resolution == "resolved"
                }
                sites = {s.id: s for s in result.call_sites}
                definitions = {d.id: d for d in result.definitions}
                linked = [r for r in resolve_calls(result) if r.status == "linked"]
                self.assertEqual(len(linked), 3)
                for resolution in linked:
                    site = sites[resolution.call_id]
                    target = definitions[resolution.target_id]
                    text = (FIXTURES / language / site.file).read_text()
                    line = text.splitlines()[site.line - 1]
                    column = line.index(site.callee.split(".")[-1], site.column)
                    expected = referenced[(site.file, site.line, column)]
                    self.assertEqual(expected.file, target.file)
                    self.assertTrue(target.line <= expected.line <= target.end_line)


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI")
    and importlib.util.find_spec("neo4j")
    and PYTHON_READY,
    "Explicit isolated Neo4j test settings, neo4j and python extras required",
)
class CallNeo4jIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from deepthought.storage.graph import (
            Neo4jConnectionManager,
            WriteCoordinator,
            setup_schema,
        )

        self.repo = "https://github.com/deepthought-integration/" + uuid4().hex
        self.manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
        )
        self.driver = await self.manager.connect()
        self.database = self.manager.database
        await setup_schema(self.driver, database=self.database)
        self.coordinator = WriteCoordinator(self.driver, database=self.database)

    async def asyncTearDown(self):
        try:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=self.repo
                )
                await result.consume()
        finally:
            await self.manager.close()

    async def test_callers_and_callees_queries_return_name_matched_edges(self):
        from deepthought.analysis_tools import TreeSitterTool
        from deepthought.graph_queries import GraphQueryService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text(
                "def helper(): pass\ndef run():\n    helper()\nhelper()\n"
            )
            # The parser's own rules, which bind calls in files no index covers.
            output = await TreeSitterTool(root).analyze(self.repo, scip_indexes=())
        result = await self.coordinator.write_tool_output(
            "tree-sitter", output, self.repo
        )
        self.assertTrue(result.success, result.errors)
        helper = next(
            n.properties["qualified_name"]
            for n in output.nodes
            if n.properties.get("name") == "helper"
        )
        service = GraphQueryService(self.driver, self.database)
        callers = await service.get_function_callers(
            helper, self.repo, snapshot_id=result.snapshot_id
        )
        observed = sorted(
            (
                item["element"].get("name") or item["element"]["qualified_name"],
                item["relationship"]["properties"]["rule_id"],
                item["relationship"]["properties"]["evidence_type"],
            )
            for item in callers["items"]
        )
        self.assertEqual(
            observed,
            [
                ("(top level of file)", CALL_LEXICAL_NAME, "name_matched"),
                ("run", CALL_LEXICAL_NAME, "name_matched"),
            ],
        )
        # Each caller is named with its file, line span and signature; the
        # code outside every declaration is the top level of its file.
        self.assertEqual(
            sorted(
                (
                    item["element"]["name"],
                    item["element"]["file_path"],
                    item["element"]["start_line"],
                    item["element"]["end_line"],
                    item["element"]["signature"],
                    item["element"]["top_level"],
                    item["relationship"]["properties"]["line"],
                    item["source_roles"],
                )
                for item in callers["items"]
            ),
            [
                ("(top level of file)", "main.py", None, None, None, True, 4, []),
                ("run", "main.py", 2, 3, "def run():", False, 3, []),
            ],
        )
        self.assertEqual(
            registered_rules()[CALL_LEXICAL_NAME].evidence_type, "name_matched"
        )
        run = next(
            n.properties["qualified_name"]
            for n in output.nodes
            if n.properties.get("name") == "run"
        )
        callees = await service.get_function_calls(
            run, self.repo, snapshot_id=result.snapshot_id
        )
        self.assertEqual([i["element"]["name"] for i in callees["items"]], ["helper"])
        # Name matches never claim completeness: unresolved calls may exist.
        self.assertNotIn("calls", callees["capabilities"])

    async def test_call_sites_of_a_snapshot_without_unlinked_calls_are_unknown(self):
        from deepthought.analysis_tools import TreeSitterTool
        from deepthought.graph_queries import GraphQueryService

        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "main.py").write_text("def f(): pass\nf()\n")
            output = await TreeSitterTool(Path(directory)).analyze(self.repo)
        # As written before unlinked calls were stored.
        del output.metadata.custom_metrics["unresolved_call_sites"]
        result = await self.coordinator.write_tool_output(
            "tree-sitter", output, self.repo
        )
        self.assertTrue(result.success, result.errors)
        sites = await GraphQueryService(self.driver, self.database).get_call_sites(
            "f", self.repo, snapshot_id=result.snapshot_id
        )
        self.assertEqual(sites["status"], "unsupported_or_unobserved")
        self.assertEqual(sites["counts"]["linked"], 1)

    async def test_queries_anchor_on_a_declaration_by_place(self):
        from deepthought.analysis_tools import TreeSitterTool
        from deepthought.graph_queries import GraphQueryService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text(
                "class A:\n    def run(self): pass\n"
                "class B:\n    def run(self): pass\n"
                "def go():\n    a = A()\n    a.run()\n    b = B()\n    b.run()\n"
                "    a.run()\n"
                "class C:\n    def m(self):\n        return 1\n"
            )
            (root / "z.py").write_text("def h(y):\n    y.run()\n")
            (root / "dup.py").write_text(
                "if x:\n    def dup(): pass\nelse:\n    def dup(): pass\ndup()\n"
            )
            (root / "one.ts").write_text("class P { a() {} b() {} }\n")
            output = await TreeSitterTool(root).analyze(self.repo, scip_indexes=())
        result = await self.coordinator.write_tool_output(
            "tree-sitter", output, self.repo
        )
        self.assertTrue(result.success, result.errors)
        service = GraphQueryService(self.driver, self.database)
        snapshot = result.snapshot_id
        qualified = {
            (n.properties["file_path"], n.properties["start_line"]): n.properties[
                "qualified_name"
            ]
            for n in output.nodes
            if "Symbol" in n.labels
        }

        def lines(response):
            return [(i["file_path"], i["line"], i["status"]) for i in response["items"]]

        everything = await service.get_call_sites(
            "run", self.repo, snapshot_id=snapshot
        )
        self.assertEqual(
            lines(everything),
            [
                ("main.py", 7, "linked"),
                ("main.py", 9, "linked"),
                ("main.py", 10, "linked"),
                ("z.py", 2, "unresolved"),
            ],
        )
        # One declaration, by place or by qualified name, keeps only its calls.
        for target in ("main.py:2", qualified[("main.py", 2)]):
            only_a = await service.get_call_sites(
                "run", self.repo, snapshot_id=snapshot, target=target
            )
            self.assertEqual(
                lines(only_a), [("main.py", 7, "linked"), ("main.py", 10, "linked")]
            )
            self.assertTrue(
                all(i["target"] == qualified[("main.py", 2)] for i in only_a["items"])
            )
            # The unresolved call of the name could be a call of it.
            self.assertEqual(
                only_a["counts"], {"linked": 2, "ambiguous": 0, "unresolved": 1}
            )
        # An ambiguous call stays for each declaration it may call; an
        # unresolved one is only counted.
        for place in ("dup.py:2", "dup.py:4"):
            dup = await service.get_call_sites(
                "dup", self.repo, snapshot_id=snapshot, target=place
            )
            self.assertEqual(lines(dup), [("dup.py", 5, "ambiguous")])
            self.assertEqual(
                dup["counts"], {"linked": 0, "ambiguous": 1, "unresolved": 0}
            )
        # A place inside a body names the innermost declaration around it.
        callees = await service.get_function_calls(
            "main.py:8", self.repo, snapshot_id=snapshot
        )
        self.assertEqual(
            sorted(i["element"]["name"] for i in callees["items"]),
            ["A", "A.run", "A.run", "B", "B.run"],
        )
        callers = await service.get_function_callers(
            "main.py:4", self.repo, snapshot_id=snapshot
        )
        self.assertEqual(
            [
                (i["element"]["name"], i["relationship"]["properties"]["line"])
                for i in callers["items"]
            ],
            [("go", 9)],
        )
        impact = await service.get_impact("main.py:2", self.repo, snapshot_id=snapshot)
        self.assertEqual([i["element"]["name"] for i in impact["items"]], ["go"])
        owner = await service.get_owner("main.py:2", self.repo, snapshot_id=snapshot)
        self.assertEqual([i["element"]["name"] for i in owner["items"]], ["A"])
        # Inside nested declarations, the innermost one.
        inner = await service.get_owner("main.py:13", self.repo, snapshot_id=snapshot)
        self.assertEqual([i["element"]["name"] for i in inner["items"]], ["C"])
        # Several declarations named on one line are ambiguous; a line with
        # none around it names nothing.
        with self.assertRaisesRegex(ValueError, "ambiguous place one.ts:1"):
            await service.get_function_calls(
                "one.ts:1", self.repo, snapshot_id=snapshot
            )
        with self.assertRaisesRegex(ValueError, "no declaration at main.py:99"):
            await service.get_impact("main.py:99", self.repo, snapshot_id=snapshot)
        with self.assertRaisesRegex(ValueError, "no declaration at dup.py:5"):
            await service.get_call_sites(
                "dup", self.repo, snapshot_id=snapshot, target="dup.py:5"
            )

    async def test_call_sites_count_linked_and_unlinked_calls_of_a_name(self):
        from deepthought.analysis_tools import TreeSitterTool
        from deepthought.graph_queries import GraphQueryService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text(
                "def run(): pass\n"
                "class A:\n    def run(self): pass\n"
                "class B:\n    def run(self): pass\n"
                "def go(x):\n    run()\n    x.run()\n    missing()\n"
                "x.run()\n"
            )
            (root / "other.py").write_text("def f(y):\n    y.run()\n")
            (root / "tests").mkdir()
            (root / "tests" / "test_run.py").write_text(
                "def test_run(y):\n    y.run()\n"
            )
            (root / "dup.py").write_text(
                "if x:\n    def dup(): pass\nelse:\n    def dup(): pass\ndup()\n"
            )
            # The parser's own rules, which bind calls in files no index covers.
            output = await TreeSitterTool(root).analyze(self.repo, scip_indexes=())
        result = await self.coordinator.write_tool_output(
            "tree-sitter", output, self.repo
        )
        self.assertTrue(result.success, result.errors)
        service = GraphQueryService(self.driver, self.database)
        sites = await service.get_call_sites(
            "run", self.repo, snapshot_id=result.snapshot_id
        )
        self.assertEqual(sites["status"], "succeeded")
        self.assertEqual(
            sites["counts"], {"linked": 1, "ambiguous": 0, "unresolved": 4}
        )
        self.assertEqual(
            [
                (i["file_path"], i["line"], i["callee"], i["status"], i["reason"])
                for i in sites["items"]
            ],
            [
                ("main.py", 7, "run", "linked", None),
                ("main.py", 8, "x.run", "unresolved", "local_binding"),
                ("main.py", 10, "x.run", "unresolved", "attribute_call"),
                ("other.py", 2, "y.run", "unresolved", "local_binding"),
                ("tests/test_run.py", 2, "y.run", "unresolved", "local_binding"),
            ],
        )
        # Each call names the function or file around it, and whether that
        # is a test.
        self.assertEqual(
            [
                (
                    i["caller"]["name"],
                    i["caller"]["kind"],
                    i["caller"]["start_line"],
                    i["caller"]["end_line"],
                    i["source_roles"],
                )
                for i in sites["items"]
            ],
            [
                ("go", "Function", 6, 9, []),
                ("go", "Function", 6, 9, []),
                ("(top level of file)", "File", None, None, []),
                ("f", "Function", 1, 2, []),
                ("test_run", "Function", 1, 2, ["test"]),
            ],
        )
        self.assertTrue(
            sites["items"][0]["caller"]["qualified_name"].endswith(
                next(
                    "::go::" + n.properties["definition_id"]
                    for n in output.nodes
                    if n.properties.get("name") == "go"
                )
            )
        )
        self.assertTrue(
            sites["items"][0]["target"].endswith(
                "::run::"
                + next(
                    n.properties["definition_id"]
                    for n in output.nodes
                    if n.properties.get("name") == "run"
                )
            )
        )
        scoped = await service.get_call_sites(
            "run",
            self.repo,
            snapshot_id=result.snapshot_id,
            file_path="other.py",
            limit=5,
        )
        self.assertEqual(
            scoped["counts"], {"linked": 0, "ambiguous": 0, "unresolved": 1}
        )
        missing = await service.get_call_sites(
            "missing", self.repo, snapshot_id=result.snapshot_id
        )
        self.assertEqual(
            [
                (i["line"], i["status"], i["reason"], i["resolver"])
                for i in missing["items"]
            ],
            [(9, "unresolved", "unbound_name", "tree-sitter")],
        )
        dup = await service.get_call_sites(
            "dup", self.repo, snapshot_id=result.snapshot_id
        )
        self.assertEqual(dup["counts"], {"linked": 0, "ambiguous": 1, "unresolved": 0})
        self.assertEqual(len(dup["items"][0]["candidates"]), 2)
        # The caller holds each unlinked call by a structural edge.
        async with self.driver.session(database=self.database) as session:
            record = await (
                await session.run(
                    "MATCH (c {repo_url:$repo,snapshot_id:$snapshot})"
                    "-[r:HAS_UNRESOLVED_CALL]->(u:UnresolvedCall {name:'missing'}) "
                    "RETURN c.name AS caller,r.rule_id AS rule,r.evidence_type AS evidence",
                    repo=self.repo,
                    snapshot=result.snapshot_id,
                )
            ).single()
        self.assertEqual(
            dict(record),
            {
                "caller": "go",
                "rule": "tree-sitter.call-site-unlinked",
                "evidence": "resolved",
            },
        )


if __name__ == "__main__":
    unittest.main()
