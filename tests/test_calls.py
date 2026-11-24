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
    CALL_JS_REEXPORT,
    CALL_JS_STATIC_METHOD,
    CALL_LEXICAL_NAME,
    CALL_MODULE_ATTRIBUTE,
    CALL_PYTHON_REEXPORT,
    CALL_PYTHON_TYPED_RECEIVER,
    CALL_PRIVATE_NAME,
    CALL_SELF_METHOD,
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
                    "    o.run();\n"  # 7: not a private name
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
            ("unresolved", None, None, "attribute_call"),
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
            output = await TreeSitterTool(root).analyze(self.repo)
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
                ("main.py", CALL_LEXICAL_NAME, "name_matched"),
                ("run", CALL_LEXICAL_NAME, "name_matched"),
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


if __name__ == "__main__":
    unittest.main()
