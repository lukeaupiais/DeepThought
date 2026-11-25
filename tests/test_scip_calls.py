"""Call targets read from SCIP indexes (deepthought.scip_calls)."""

import asyncio
import dataclasses
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

import deepthought
import deepthought.calls
from deepthought.analysis_tools.tree_sitter import TreeSitterTool
from deepthought.engine import analyze_repository
from deepthought import scip_calls
import test_scip_tool as fixtures

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

TS = (
    "class Box { constructor() {} }\n"
    "function helper() { return 1 }\n"
    "function over(a: string): void;\n"
    "function over(a: number): void;\n"
    "function over(a: any) {}\n"
    "function main() { const é = new Box(); helper(); over(1);"
    " function inner() {} inner(); console.log(); helper();"
    " const hh = helper; hh(); }\n"
    "export { helper as aliasName };\n"
)
# A name the index links to a declaration of another name: not suspect in
# TypeScript, where re-exports rename.
TS_ALIAS = 'import { aliasName } from "./a";\naliasName();\n'
# Not in the index: its calls keep the parser's rules.
TS_UNCOVERED = 'import { helper } from "./a";\nexport function b() { helper(); }\n'
PY_MODULE = "def thing():\n    pass\n\ndef other():\n    pass\n"
PY = (
    "from m import thing\n"
    "class A:\n"
    "    def x(self):\n"
    "        pass\n"
    "    def y(self):\n"
    '        t = "\U0001f600"; self.x()\n'
    "        thing()\n"
    "        other_name()\n"
    '        len("a")\n'
    "        self.z()\n"
    "        here()\n"
    "def here():\n"
    "    pass\n"
    "class B(A):\n"
    "    def w(self, obj):\n"
    "        self.x()\n"
    "        obj.x()\n"
    "        def inner():\n"
    "            self.x()\n"
    "        inner()\n"
)
BUILTIN = "scip-python python builtins 3 "
EXTERNAL = "scip-typescript npm typescript 5 lib/Console#log()."


class FakePyright:
    """Answers definitions from a table keyed by (file, line, UTF-16
    column), recording what it was asked."""

    tool = "pyright@1.1.409"

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def definitions(self, file, line, column):
        self.asked.append((file, line, column))
        return self.answers.get((file, line, column), [])


def _column(line, text, encoding, start=0):
    """Where ``text`` begins in ``line``, in ``encoding`` columns."""
    offset = line.index(text, start)
    if encoding == "utf-16":
        return len(line[:offset].encode("utf-16-le")) // 2
    return len(line[:offset].encode("utf-8"))


@unittest.skipUnless(fixtures.PROTOBUF, "Provision the scip extra")
class ScipCallTests(unittest.TestCase):
    def setUp(self):
        from deepthought._scip import scip_pb2

        self.wire = scip_pb2
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "ws"
        self.root.mkdir()
        self.indexes = Path(temporary.name)

    def index(self, name, tool, documents, encoding):
        """Write an index whose documents are (path, [(line, text, symbol,
        definition, nth)]) and return it ingested, with a receipt."""
        wire = self.wire
        index = wire.Index()
        index.metadata.tool_info.name = tool
        index.metadata.tool_info.version = "1.0"
        for path, occurrences in documents:
            lines = (self.root / path).read_text().split("\n")
            document = index.documents.add(relative_path=path)
            document.position_encoding = encoding
            column_encoding = {2: "utf-16"}.get(encoding, "utf-8")
            if encoding == 0 and tool == "scip-python":
                column_encoding = "utf-16"
            for line, text, symbol, definition, nth in occurrences:
                start = -1
                for _ in range(nth + 1):
                    start = lines[line].index(text, start + 1)
                column = _column(lines[line], text, column_encoding, start)
                width = len(text.encode("utf-16-le")) // 2
                document.occurrences.add(
                    symbol=symbol,
                    range=[line, column, column + width],
                    symbol_roles=wire.Definition if definition else 0,
                )
        path = self.indexes / f"{name}.scip"
        path.write_bytes(index.SerializeToString())
        return deepthought.ingest_scip(path, repository_id="r"), f"sha256:{name}"

    def typescript(self):
        (self.root / "a.ts").write_text(TS)
        (self.root / "b.ts").write_text(TS_UNCOVERED)
        (self.root / "c.ts").write_text(TS_ALIAS)
        p = "scip-typescript npm demo 1 a.ts/"
        return self.index(
            "ts",
            "scip-typescript",
            [
                (
                    "a.ts",
                    [
                        (0, "Box", p + "Box#", True, 0),
                        (0, "constructor", p + "Box#`<constructor>`().", True, 0),
                        (1, "helper", p + "helper().", True, 0),
                        (2, "over", p + "over().", True, 0),
                        (3, "over", p + "over().", True, 0),
                        (4, "over", p + "over().", True, 0),
                        (5, "inner", "local 3", True, 0),
                        (5, "Box", p + "Box#", False, 0),
                        (5, "helper", p + "helper().", False, 0),
                        (5, "over", p + "over().", False, 0),
                        (5, "inner", "local 3", False, 1),
                        (5, "log", EXTERNAL, False, 0),
                        # The second helper() has no occurrence.
                        # `hh` is a variable: no declaration to link.
                        (5, "hh", "local 4", True, 0),
                        (5, "hh", "local 4", False, 1),
                    ],
                ),
                ("c.ts", [(1, "aliasName", p + "helper().", False, 0)]),
            ],
            2,
        )

    def targets(self, result, binding):
        names = {d.id: (d.file, d.symbol, d.line) for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        rows = []
        for r in binding.resolutions:
            site = sites[r.call_id]
            rows.append(
                (
                    site.file,
                    site.callee,
                    r.status,
                    names[r.target_id][1:] if r.target_id else r.reason,
                    r.rule_id if r.status == "linked" else None,
                    binding.producers.get(r.call_id),
                )
            )
        return sorted(rows, key=str)

    def test_typescript_rules_encodings_and_uncovered_files(self):
        ingested = self.typescript()
        result = analyze_repository(
            self.root, repository_id="r", languages=("typescript",)
        )
        binding = scip_calls.bind_calls(result, [ingested], self.root)
        tool = ("scip-typescript@1.0", "sha256:ts")
        self.assertEqual(
            self.targets(result, binding),
            sorted(
                [
                    # A class constructed links to its constructor; the
                    # column after "é" is read in UTF-16 code units.
                    (
                        "a.ts",
                        "Box",
                        "linked",
                        ("Box.constructor", 1),
                        scip_calls.SCIP_CALL_CONSTRUCTOR,
                        tool,
                    ),
                    (
                        "a.ts",
                        "helper",
                        "linked",
                        ("helper", 2),
                        scip_calls.SCIP_CALL_OCCURRENCE,
                        tool,
                    ),
                    # A covered call with no occurrence is not given the
                    # parser's target.
                    ("a.ts", "helper", "unresolved", "scip_no_occurrence", None, tool),
                    (
                        "a.ts",
                        "over",
                        "linked",
                        ("over", 5),
                        scip_calls.SCIP_CALL_OVERLOAD_IMPLEMENTATION,
                        tool,
                    ),
                    (
                        "a.ts",
                        "inner",
                        "linked",
                        ("main.inner", 6),
                        scip_calls.SCIP_CALL_LOCAL_SYMBOL,
                        tool,
                    ),
                    ("a.ts", "console.log", "unresolved", "scip_external", None, tool),
                    (
                        "a.ts",
                        "hh",
                        "unresolved",
                        "scip_target_not_declaration",
                        None,
                        tool,
                    ),
                    (
                        "c.ts",
                        "aliasName",
                        "linked",
                        ("helper", 2),
                        scip_calls.SCIP_CALL_OCCURRENCE,
                        tool,
                    ),
                    # b.ts is not covered: the parser's import rule links it.
                    (
                        "b.ts",
                        "helper",
                        "linked",
                        ("helper", 2),
                        "tree-sitter.call-imported-name",
                        None,
                    ),
                ],
                key=str,
            ),
        )
        # Where both link, they agree; the parser links the second helper()
        # that the index has nothing for. Uncovered b.ts is not compared.
        self.assertEqual(
            [d.row() for d in binding.disagreements],
            [
                "a.ts\t6\thelper\t-\t0\ta.ts\t2\tscip_no_occurrence\t"
                "tree-sitter.call-lexical-name"
            ],
        )

    def test_scored_with_an_index_and_disagreements_written(self):
        self.typescript()
        import ground_truth

        index = self.indexes / "ts.scip"
        truth = self.indexes / "truth.tsv"
        truth.write_text("a.ts\t6\thelper\ta.ts\t2\tcallable\n")
        written = self.indexes / "disagreements.tsv"
        out = io.StringIO()
        with redirect_stdout(out):
            ground_truth.main(
                [
                    "score",
                    str(self.root),
                    str(truth),
                    "--language",
                    "typescript",
                    "--scip",
                    str(index),
                    "--disagreements",
                    str(written),
                ]
            )
        report = json.loads(out.getvalue())
        self.assertEqual(
            (report["confirmed"], report["by_rule"]["scip.call-occurrence"]),
            (1, {"confirmed": 1, "not_indexed": 1}),
        )
        self.assertEqual(
            written.read_text().splitlines(),
            [
                "# " + "\t".join(ground_truth.DISAGREEMENT_COLUMNS),
                "a.ts\t6\thelper\t-\t0\ta.ts\t2\tscip_no_occurrence\t"
                "tree-sitter.call-lexical-name",
            ],
        )

    def test_calls_on_results_are_recorded_for_the_index_only(self):
        if True:
            root = self.root
            files = {
                "a.ts": "class K { m() {} }\n"
                "function make() { return new K() }\n"
                "class S extends K { n() { super.m(); } }\n"
                "make().m(); new K().m(); xs[0].m(); a?.b(); make()?.m();"
                " (make()).m(); make().m.call(); f?.();\n"
                # Receivers of other shapes are not recorded.
                "(a as any)().m(); new (f())().m(); (a as any)[0].m(); f?.().m();\n",
                "b.py": "def f():\n    pass\n"
                "f().g()\nxs[0].m()\na.b().c.d()\n(f()).g()\n",
            }
            for name, text in files.items():
                Path(root, name).write_text(text)
            result = analyze_repository(
                root, repository_id="r", languages=("typescript", "python")
            )
            parsed = {r.call_id: r for r in deepthought.calls.resolve_calls(result)}
            chained = sorted(
                (s.file, s.callee, s.callee_at, parsed[s.id].status)
                for s in result.call_sites
                if any(mark in s.callee for mark in ("(", "[", "?", "super"))
            )
            # No name-binding rule reads them: each is left unlinked.
            self.assertEqual(
                chained,
                sorted(
                    [
                        ("a.ts", "super.m", (3, 32), "unresolved"),
                        ("a.ts", "make().m", (4, 7), "unresolved"),
                        ("a.ts", "new K().m", (4, 20), "unresolved"),
                        ("a.ts", "xs[].m", (4, 31), "unresolved"),
                        ("a.ts", "a?.b", (4, 39), "unresolved"),
                        ("a.ts", "make()?.m", (4, 52), "unresolved"),
                        ("a.ts", "make().m", (4, 66), "unresolved"),
                        ("a.ts", "make().m.call", (4, 80), "unresolved"),
                        ("a.ts", "f?.().m", (5, 60), "unresolved"),
                        ("b.py", "f().g", (3, 4), "unresolved"),
                        ("b.py", "xs[].m", (4, 6), "unresolved"),
                        ("b.py", "a.b().c.d", (5, 8), "unresolved"),
                        ("b.py", "f().g", (6, 6), "unresolved"),
                    ]
                ),
            )
            p = "scip-typescript npm demo 1 a.ts/"
            ingested = self.index(
                "chain",
                "scip-typescript",
                [
                    (
                        "a.ts",
                        [
                            (0, "K", p + "K#", True, 0),
                            (0, "m", p + "K#m().", True, 0),
                            (2, "m()", p + "K#m().", False, 0),
                            (3, "m()", p + "K#m().", False, 0),
                            (3, "m()", p + "K#m().", False, 1),
                        ],
                    )
                ],
                2,
            )
            binding = scip_calls.bind_calls(result, [ingested], root)
            sites = {s.id: s for s in result.call_sites}
            # The index binds them.
            self.assertEqual(
                sorted(
                    (sites[r.call_id].callee, sites[r.call_id].line)
                    for r in binding.resolutions
                    if r.rule_id == scip_calls.SCIP_CALL_OCCURRENCE
                ),
                [("make().m", 4), ("new K().m", 4), ("super.m", 3)],
            )

    def test_indexes_of_one_indexer_share_global_symbols(self):
        (self.root / "a.ts").write_text("export function helper() {}\n")
        (self.root / "c.ts").write_text(
            'import { helper } from "./a";\nhelper();\nfunction k() {}\nk();\n'
        )
        p = "scip-typescript npm demo 1 a.ts/"
        first = self.index(
            "first",
            "scip-typescript",
            [("a.ts", [(0, "helper", p + "helper().", True, 0)])],
            2,
        )
        # A second pass over files outside the first's project; its local
        # symbol does not reach the first index's documents.
        second = self.index(
            "second",
            "scip-typescript",
            [
                (
                    "c.ts",
                    [
                        (1, "helper", p + "helper().", False, 0),
                        (2, "k", "local 0", True, 0),
                        (3, "k", "local 0", False, 0),
                    ],
                )
            ],
            2,
        )
        result = analyze_repository(
            self.root, repository_id="r", languages=("typescript",)
        )
        binding = scip_calls.bind_calls(result, [first, second], self.root)
        self.assertEqual(
            [row[1:5] for row in self.targets(result, binding)],
            [
                ("helper", "linked", ("helper", 1), scip_calls.SCIP_CALL_OCCURRENCE),
                ("k", "linked", ("k", 3), scip_calls.SCIP_CALL_LOCAL_SYMBOL),
            ],
        )

    def test_impact_follows_calls_bound_from_an_index(self):
        import ground_truth

        (self.root / "a.ts").write_text(
            "class P { m() {} }\nfunction use(p: any) { p.m(); }\n"
        )
        q = "scip-typescript npm demo 1 a.ts/"
        self.index(
            "impact",
            "scip-typescript",
            [
                (
                    "a.ts",
                    [
                        (0, "P", q + "P#", True, 0),
                        (0, "m", q + "P#m().", True, 0),
                        (1, "use", q + "use().", True, 0),
                        (1, "m()", q + "P#m().", False, 0),
                    ],
                )
            ],
            2,
        )
        calls = self.indexes / "calls.tsv"
        calls.write_text("a.ts\t2\tm\ta.ts\t1\tcallable\n")
        impact = self.indexes / "impact.tsv"
        impact.write_text("a.ts\t1\tm\ta.ts\t2\tuse\t1\n")

        def scored(*extra):
            out = io.StringIO()
            with redirect_stdout(out):
                ground_truth.main(
                    [
                        "score",
                        str(self.root),
                        str(calls),
                        "--language",
                        "typescript",
                        "--impact",
                        str(impact),
                        *extra,
                    ]
                )
            found = json.loads(out.getvalue())["impact"]
            return found["confirmed"], found["missed"]

        # The parser leaves `p.m()` open; the index links it.
        self.assertEqual(scored(), (0, 1))
        self.assertEqual(scored("--scip", str(self.indexes / "impact.scip")), (1, 0))

    def test_without_indexes_the_parser_resolves_every_call(self):
        (self.root / "a.ts").write_text(TS)
        result = analyze_repository(
            self.root, repository_id="r", languages=("typescript",)
        )
        binding = scip_calls.bind_calls(result, (), self.root)
        self.assertEqual(
            binding.resolutions, tuple(deepthought.calls.resolve_calls(result))
        )
        self.assertEqual((binding.producers, binding.disagreements), ({}, ()))

    def test_python_suspect_targets_are_answered_by_pyright(self):
        (self.root / "m.py").write_text(PY_MODULE)
        (self.root / "c.py").write_text(PY)
        m = "scip-python python demo 1 m/"
        c = "scip-python python demo 1 c/"
        ingested = self.index(
            "py",
            "scip-python",
            [
                (
                    "m.py",
                    [
                        (0, "thing", m + "thing().", True, 0),
                        (3, "other", m + "other().", True, 0),
                    ],
                ),
                (
                    "c.py",
                    [
                        (1, "A", c + "A#", True, 0),
                        (2, "x", c + "A#x().", True, 0),
                        (4, "y", c + "A#y().", True, 0),
                        # scip-python 0.6.8 names a builtin for self.x()
                        # and follows a re-export to the wrong function.
                        (5, "x", BUILTIN + "x().", False, 0),
                        (6, "thing", m + "other().", False, 0),
                        # A different name with no import explaining it.
                        (7, "other_name", m + "other().", False, 0),
                        # Builtins the class does not declare stay external.
                        (8, "len", BUILTIN + "len().", False, 0),
                        (9, "z", BUILTIN + "z().", False, 0),
                        # The written name, declared in the file.
                        (10, "here", c + "here().", False, 0),
                        (11, "here", c + "here().", True, 0),
                        (13, "B", c + "B#", True, 0),
                        (14, "w", c + "B#w().", True, 0),
                        # Declared by a base class: suspect.
                        (15, "x", BUILTIN + "x().", False, 0),
                        # Not called on self: an external target stands.
                        (16, "x", BUILTIN + "x().", False, 0),
                        # self in a function nested in a method.
                        (18, "x", BUILTIN + "x().", False, 0),
                    ],
                ),
            ],
            0,
        )
        result = analyze_repository(self.root, repository_id="r", languages=("python",))
        with self.assertRaisesRegex(ValueError, "requires pyright"):
            scip_calls.bind_calls(result, [ingested], self.root)
        lines = PY.splitlines()

        def at(line, text, start=0):
            return ("c.py", line, _column(lines[line - 1], text, "utf-16", start))

        x6, x16, x19 = at(6, "x()"), at(16, "x()"), at(19, "x()")
        pyright = FakePyright(
            {
                x6: [("c.py", 3, 8)],
                x16: [("c.py", 3, 8)],
                x19: [("c.py", 3, 8)],
                at(7, "thing"): [("m.py", 1, 4)],
                at(20, "inner"): [("c.py", 18, 12)],
            }
        )
        binding = scip_calls.bind_calls(result, [ingested], self.root, pyright)
        rows = {(row[1], row[2]): row for row in self.targets(result, binding)}
        tool = ("scip-python@1.0", "sha256:py")
        answered = ("pyright@1.1.409", "sha256:py")
        # Pyright answers the suspect targets and the call with no
        # occurrence; columns are UTF-16, after the emoji too.
        self.assertEqual(x6[2], len('        t = "') + 2 + len('"; self.'))
        self.assertEqual(
            sorted(pyright.asked),
            sorted(
                [x6, x16, x19, at(7, "thing"), at(8, "other_name"), at(20, "inner")]
            ),
        )
        self.assertEqual(
            rows[("thing", "linked")][3:],
            (("thing", 1), scip_calls.PYRIGHT_CALL_DEFINITION, answered),
        )
        self.assertEqual(
            rows[("inner", "linked")][3:],
            (("B.w.inner", 18), scip_calls.PYRIGHT_CALL_DEFINITION, answered),
        )
        # Pyright gives nothing: unresolved, from pyright.
        self.assertEqual(
            rows[("other_name", "unresolved")][3:],
            (scip_calls.PYRIGHT_NO_DEFINITION, None, answered),
        )
        self.assertEqual(rows[("len", "unresolved")][3], "scip_external")
        self.assertEqual(rows[("self.z", "unresolved")][3], "scip_external")
        self.assertEqual(rows[("obj.x", "unresolved")][3], "scip_external")
        self.assertEqual(
            rows[("here", "linked")][3:],
            (("here", 12), scip_calls.SCIP_CALL_OCCURRENCE, tool),
        )
        sites = {s.id: s for s in result.call_sites}
        self.assertEqual(
            sorted(
                (sites[r.call_id].callee, binding.resolver(r.call_id))
                for r in binding.resolutions
                if sites[r.call_id].callee in {"thing", "here", "len"}
            ),
            [("here", "scip"), ("len", "scip"), ("thing", "pyright")],
        )
        # Inherited, and from a function nested in a method, too.
        self.assertEqual(
            [row[3:] for row in self.targets(result, binding) if row[1] == "self.x"],
            [(("A.x", 3), scip_calls.PYRIGHT_CALL_DEFINITION, answered)] * 3,
        )
        # Pyright and the parser agree on every call here.
        self.assertEqual(binding.disagreements, ())

    def test_nested_functions_sharing_a_symbol_are_answered_by_pyright(self):
        # scip-python 0.6.8 names a function nested in a method by the
        # class alone, so both `decorator`s are one symbol.
        source = (
            "class G:\n"
            "    def command(self):\n"
            "        def decorator(f):\n"
            "            return f\n"
            "        return decorator(1)\n"
            "    def group(self):\n"
            "        def decorator(f):\n"
            "            return f\n"
            "        return decorator(2)\n"
        )
        (self.root / "g.py").write_text(source)
        g = "scip-python python demo 1 g/"
        ingested = self.index(
            "nested",
            "scip-python",
            [
                (
                    "g.py",
                    [
                        (0, "G", g + "G#", True, 0),
                        (1, "command", g + "G#command().", True, 0),
                        (2, "decorator", g + "G#decorator().", True, 0),
                        (4, "decorator", g + "G#decorator().", False, 0),
                        (5, "group", g + "G#group().", True, 0),
                        (6, "decorator", g + "G#decorator().", True, 0),
                        (8, "decorator", g + "G#decorator().", False, 0),
                    ],
                ),
            ],
            0,
        )
        result = analyze_repository(self.root, repository_id="r", languages=("python",))
        with self.assertRaisesRegex(ValueError, "requires pyright"):
            scip_calls.bind_calls(result, [ingested], self.root)
        pyright = FakePyright(
            {("g.py", 5, 15): [("g.py", 3, 12)], ("g.py", 9, 15): [("g.py", 7, 12)]}
        )
        binding = scip_calls.bind_calls(result, [ingested], self.root, pyright)
        answered = ("pyright@1.1.409", "sha256:nested")
        self.assertEqual(
            self.targets(result, binding),
            [
                (
                    "g.py",
                    "decorator",
                    "linked",
                    ("G.command.decorator", 3),
                    scip_calls.PYRIGHT_CALL_DEFINITION,
                    answered,
                ),
                (
                    "g.py",
                    "decorator",
                    "linked",
                    ("G.group.decorator", 7),
                    scip_calls.PYRIGHT_CALL_DEFINITION,
                    answered,
                ),
            ],
        )

    def test_typescript_symbols_with_several_declarations_stay_ambiguous(self):
        (self.root / "t.ts").write_text("function f() {}\nfunction f() {}\nf();\n")
        f = "scip-typescript npm demo 1 t.ts/f()."
        ingested = self.index(
            "several",
            "scip-typescript",
            [
                (
                    "t.ts",
                    [(0, "f", f, True, 0), (1, "f", f, True, 0), (2, "f", f, False, 0)],
                )
            ],
            2,
        )
        result = analyze_repository(
            self.root, repository_id="r", languages=("typescript",)
        )
        binding = scip_calls.bind_calls(result, [ingested], self.root)
        (resolution,) = binding.resolutions
        self.assertEqual(
            (resolution.status, resolution.reason, len(resolution.candidates)),
            ("ambiguous", "multiple_declarations", 2),
        )

    def test_pyright_external_and_non_declaration_answers(self):
        (self.root / "m.py").write_text(PY_MODULE)
        (self.root / "c.py").write_text(
            "from m import thing\nthing()\nthing()\nthing()\n"
        )
        m = "scip-python python demo 1 m/"
        ingested = self.index(
            "pyx",
            "scip-python",
            [
                (
                    "m.py",
                    [
                        (0, "thing", m + "thing().", True, 0),
                        (3, "other", m + "other().", True, 0),
                    ],
                ),
                (
                    "c.py",
                    [
                        (1, "thing", m + "other().", False, 0),
                        (2, "thing", m + "other().", False, 0),
                    ],
                ),
            ],
            0,
        )
        result = analyze_repository(self.root, repository_id="r", languages=("python",))
        pyright = FakePyright(
            {
                # Outside the repository, and a position no declaration
                # of that name holds (the blank line after `pass`).
                ("c.py", 2, 0): [(None, 1, 0)],
                ("c.py", 3, 0): [("m.py", 2, 4)],
            }
        )
        # A site with no position for its callee's name.
        result = dataclasses.replace(
            result,
            call_sites=tuple(
                dataclasses.replace(s, callee_at=None) if s.line == 4 else s
                for s in result.call_sites
            ),
        )
        binding = scip_calls.bind_calls(result, [ingested], self.root, pyright)
        self.assertEqual(
            [r[3] for r in self.targets(result, binding)],
            [
                scip_calls.PYRIGHT_EXTERNAL,
                # Nothing to ask pyright.
                scip_calls.PYRIGHT_NO_DEFINITION,
                scip_calls.PYRIGHT_NOT_DECLARATION,
            ],
        )
        self.assertEqual(sorted(pyright.asked), [("c.py", 2, 0), ("c.py", 3, 0)])

    def test_python_calls_through_locals_follow_them_with_pyright(self):
        (self.root / "a.py").write_text(
            "class Segment:\n"  # 1
            "    @classmethod\n"
            "    def line(cls):\n"  # 3
            "        return cls()\n"
            "    def split(self):\n"  # 5
            "        _S = Segment\n"
            "        return _S()\n"  # 7
            "class Console:\n"
            "    def get_style(self): pass\n"  # 9
            "def render(console: Console, given):\n"
            "    get = console.get_style\n"  # 11
            "    get()\n"
            "    twice = console.get_style\n"  # 13
            "    twice = console.get_style\n"
            "    twice()\n"  # 15
            "    given()\n"
            "    made = given()\n"  # 17
            "    made()\n"
            "    def plain(cls): return cls()\n"  # 19
            "    plain(1)\n"
            "    typed: object = console.get_style\n"  # 21
            "    typed()\n"
            "    first = second = console.get_style\n"  # 23
            "    first()\n"
            "    made_attr = render(console, given).get_style\n"  # 25
            "    made_attr()\n"
            "class Meta(type):\n"  # 27
            "    @staticmethod\n"
            "    def make(cls): return cls()\n"  # 29
            "G = Segment\n"
            "class Holder:\n"  # 31
            "    made = Segment\n"
            "    def use(self):\n"  # 33
            "        G()\n"
            "        self.made()\n"  # 35
            "def factory():\n"
            "    class Local:\n"  # 37
            "        made = Segment\n"
            "        def use(self):\n"  # 39
            "            self.made()\n"
        )
        locals_ = [
            (2, "cls", "local 0", True, 0),
            (3, "cls", "local 0", False, 0),
            (5, "_S", "local 1", True, 0),
            (6, "_S", "local 1", False, 0),
            (10, "get", "local 2", True, 0),
            (11, "get", "local 2", False, 0),
            (12, "twice", "local 3", True, 0),
            (13, "twice", "local 3", True, 0),
            (14, "twice", "local 3", False, 0),
            (9, "given", "local 4", True, 0),
            (15, "given", "local 4", False, 0),
            (16, "made", "local 5", True, 0),
            (17, "made", "local 5", False, 0),
            (18, "cls", "local 6", True, 0),
            (18, "cls", "local 6", False, 1),
            (20, "typed", "local 7", True, 0),
            (21, "typed", "local 7", False, 0),
            (22, "first", "local 8", True, 0),
            (23, "first", "local 8", False, 0),
            (24, "made_attr", "local 9", True, 0),
            (25, "made_attr", "local 9", False, 0),
            (28, "cls", "local 10", True, 0),
            (28, "cls", "local 10", False, 1),
            (29, "G", "local 11", True, 0),
            (33, "G", "local 11", False, 0),
            (31, "made", "local 12", True, 0),
            (34, "made", "local 12", False, 0),
            (37, "made", "local 13", True, 0),
            (39, "made", "local 13", False, 0),
        ]
        ingested = self.index("aliases", "scip-python", [("a.py", locals_)], 0)
        result = analyze_repository(self.root, repository_id="r", languages=("python",))
        pyright = FakePyright(
            {
                # Pyright gives each local's own place, then the value's.
                ("a.py", 4, 15): [("a.py", 3, 13)],
                ("a.py", 7, 15): [("a.py", 6, 8)],
                ("a.py", 6, 13): [("a.py", 1, 6)],
                ("a.py", 12, 4): [("a.py", 11, 4)],
                ("a.py", 11, 18): [("a.py", 9, 8)],
                ("a.py", 15, 4): [("a.py", 13, 4), ("a.py", 14, 4)],
                ("a.py", 16, 4): [("a.py", 10, 29)],
                ("a.py", 18, 4): [("a.py", 17, 4)],
                ("a.py", 19, 27): [("a.py", 19, 14)],
                ("a.py", 22, 4): [("a.py", 21, 4)],
                ("a.py", 21, 28): [("a.py", 9, 8)],
                ("a.py", 24, 4): [("a.py", 23, 4)],
                ("a.py", 23, 29): [("a.py", 9, 8)],
                ("a.py", 26, 4): [("a.py", 25, 4)],
                ("a.py", 25, 39): [("a.py", 9, 8)],
                ("a.py", 29, 26): [("a.py", 29, 13)],
                ("a.py", 34, 8): [("a.py", 30, 0)],
                ("a.py", 30, 4): [("a.py", 1, 6)],
                ("a.py", 35, 13): [("a.py", 32, 4)],
                ("a.py", 32, 11): [("a.py", 1, 6)],
                ("a.py", 40, 17): [("a.py", 38, 8)],
                ("a.py", 38, 15): [("a.py", 1, 6)],
            }
        )
        binding = scip_calls.bind_calls(result, [ingested], self.root, pyright)
        rows = {
            (s.callee, s.line): (r.status, r.target_id, r.rule_id, r.reason)
            for r in binding.resolutions
            for s in result.call_sites
            if s.id == r.call_id
        }
        symbols = {d.id: d.symbol for d in result.definitions}

        def target(callee, line):
            status, target_id, rule, reason = rows[(callee, line)]
            return (symbols.get(target_id), rule) if status == "linked" else reason

        # A classmethod's first parameter is its class.
        self.assertEqual(
            target("cls", 4),
            ("Segment", scip_calls.PYRIGHT_CALL_CLASS_PARAMETER),
        )
        # A local assigned once from a name or a dotted name is that name.
        self.assertEqual(target("_S", 7), ("Segment", scip_calls.PYRIGHT_CALL_ALIAS))
        for callee, line in (("get", 12), ("typed", 22)):
            self.assertEqual(
                target(callee, line),
                ("Console.get_style", scip_calls.PYRIGHT_CALL_ALIAS),
            )
        # Assigned twice, a parameter, a call's result: still the local.
        # So is a first parameter named `cls` of a function no classmethod.
        for callee, line in (
            ("twice", 15),
            ("given", 16),
            ("made", 18),
            ("cls", 19),
            # Nor are two names assigned at once, an attribute of a call's
            # result, and the `cls` of a method decorated otherwise.
            ("first", 24),
            ("made_attr", 26),
            ("cls", 29),
            # A module global or class attribute can be replaced elsewhere.
            ("G", 34),
            ("self.made", 35),
            ("self.made", 40),
        ):
            self.assertEqual(target(callee, line), scip_calls.PYRIGHT_NOT_DECLARATION)

    def test_python_import_alias_and_constructor_names_are_not_suspect(self):
        (self.root / "m.py").write_text(
            "class Thing:\n    def __init__(self):\n        pass\n"
            "from typing import overload\n"
            "@overload\n"
            "def ov(a: int) -> int: ...\n"
            "@overload\n"
            "def ov(a: str) -> str: ...\n"
            "def ov(a):\n"
            "    return a\n"
        )
        (self.root / "c.py").write_text(
            "from m import Thing as T\nfrom m import Thing\nT()\nThing()\n"
            "from m import ov\nov(1)\n"
        )
        m = "scip-python python demo 1 m/"
        ingested = self.index(
            "alias",
            "scip-python",
            [
                (
                    "m.py",
                    [
                        (0, "Thing", m + "Thing#", True, 0),
                        (1, "__init__", m + "Thing#__init__().", True, 0),
                        # scip-python names the group by its first signature.
                        (5, "ov", m + "ov().", True, 0),
                    ],
                ),
                (
                    "c.py",
                    [
                        (2, "T", m + "Thing#__init__().", False, 0),
                        (3, "Thing", m + "Thing#__init__().", False, 0),
                        (5, "ov", m + "ov().", False, 0),
                    ],
                ),
            ],
            0,
        )
        result = analyze_repository(self.root, repository_id="r", languages=("python",))
        binding = scip_calls.bind_calls(result, [ingested], self.root)
        self.assertEqual(
            [(row[1], row[3], row[4]) for row in self.targets(result, binding)],
            [
                ("T", ("Thing.__init__", 2), scip_calls.SCIP_CALL_OCCURRENCE),
                ("Thing", ("Thing.__init__", 2), scip_calls.SCIP_CALL_OCCURRENCE),
                ("ov", ("ov", 9), scip_calls.SCIP_CALL_OVERLOAD_IMPLEMENTATION),
            ],
        )

    def test_class_and_constructor_together_and_ambiguous_targets(self):
        (self.root / "a.ts").write_text(
            "class K { constructor() {} k() {} }\n"
            "function f() {}\n"
            "function g() {}\n"
            "new K(); f(); f(); K(); new K(); new K(); new K();\n"
            "class O { constructor(a: string); constructor(a: any) {} }\n"
            'new O("o");\n'
            "class P { m() {} }\n"
            "function use(p: any) { p.m(); f(); }\n"
            "class Q { constructor() {} }\n"
        )
        p = "scip-typescript npm demo 1 a.ts/"
        ingested = self.index(
            "two",
            "scip-typescript",
            [
                (
                    "a.ts",
                    [
                        (0, "K", p + "K#", True, 0),
                        (0, "constructor", p + "K#`<constructor>`().", True, 0),
                        (1, "f", p + "f().", True, 0),
                        (2, "g", p + "g().", True, 0),
                        (3, "K", p + "K#", False, 0),
                        (3, "K", p + "K#`<constructor>`().", False, 0),
                        # Two declarations at one occurrence: ambiguous.
                        (3, "f", p + "f().", False, 1),
                        (3, "f", p + "g().", False, 1),
                        # A class called without `new` is the class.
                        (3, "K", p + "K#", False, 1),
                        # A class and its constructor beside a third target.
                        (3, "K", p + "K#", False, 2),
                        (3, "K", p + "K#`<constructor>`().", False, 2),
                        (3, "K", p + "g().", False, 2),
                        (4, "O", p + "O#", True, 0),
                        (4, "constructor", p + "O#`<constructor>`().", True, 0),
                        (4, "constructor", p + "O#`<constructor>`().", True, 1),
                        (5, "O", p + "O#", False, 0),
                        # A class with another class's constructor.
                        (3, "K", p + "K#", False, 3),
                        (3, "K", p + "Q#`<constructor>`().", False, 3),
                        (0, "k", p + "K#k().", True, 0),
                        (8, "Q", p + "Q#", True, 0),
                        (8, "constructor", p + "Q#`<constructor>`().", True, 0),
                        # A class with a method that is not a constructor.
                        (3, "K", p + "K#", False, 4),
                        (3, "K", p + "K#k().", False, 4),
                        (6, "P", p + "P#", True, 0),
                        (6, "m", p + "P#m().", True, 0),
                        # The parser leaves p.m() open, and links f().
                        (7, "m", p + "P#m().", False, 0),
                        (7, "f", p + "g().", False, 1),
                    ],
                )
            ],
            2,
        )
        result = analyze_repository(
            self.root, repository_id="r", languages=("typescript",)
        )
        binding = scip_calls.bind_calls(result, [ingested], self.root)
        names = {d.id: d.symbol for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = sorted(
            (
                sites[r.call_id].line,
                sites[r.call_id].column,
                r.status,
                names.get(r.target_id),
                r.rule_id,
                tuple(sorted(names[c] for c in r.candidates)),
            )
            for r in binding.resolutions
        )
        occurrence = scip_calls.SCIP_CALL_OCCURRENCE
        constructor = scip_calls.SCIP_CALL_CONSTRUCTOR
        self.assertEqual(
            [row[2:] for row in found],
            [
                ("linked", "K.constructor", constructor, ()),
                ("unresolved", None, None, ()),
                ("ambiguous", None, None, ("f", "g")),
                ("linked", "K", occurrence, ()),
                ("ambiguous", None, None, ("K", "K.constructor", "g")),
                ("ambiguous", None, None, ("K", "Q.constructor")),
                ("ambiguous", None, None, ("K", "K.k")),
                # Of overloaded constructors, the implementation.
                ("linked", "O.constructor", constructor, ()),
                ("linked", "P.m", occurrence, ()),
                ("linked", "g", occurrence, ()),
            ],
        )
        self.assertEqual(
            sorted(d.row() for d in binding.disagreements)[-2:],
            [
                "a.ts\t8\tf\ta.ts\t3\ta.ts\t2\tdifferent_target\t"
                "tree-sitter.call-lexical-name",
                "a.ts\t8\tp.m\ta.ts\t7\t-\t0\tparser_attribute_call\t"
                "scip.call-occurrence",
            ],
        )
        (implementation,) = [
            d.id
            for d in result.definitions
            if d.symbol == "O.constructor" and not d.overload
        ]
        self.assertIn(implementation, {r.target_id for r in binding.resolutions})

    def test_graph_edges_name_the_rule_producer_and_receipt(self):
        ingested = self.typescript()
        output = asyncio.run(
            TreeSitterTool(self.root).analyze(
                "r", languages=["typescript"], scip_indexes=[ingested]
            )
        )
        calls = sorted(
            (
                r.properties["callee"],
                r.properties["rule_id"],
                r.properties["evidence_type"],
                r.properties.get("producer"),
                r.properties.get("receipt"),
            )
            for r in output.relationships
            if r.type == "CALLS"
        )
        self.assertEqual(
            calls,
            [
                (
                    "Box",
                    "scip.call-constructor",
                    "resolved",
                    "scip-typescript@1.0",
                    "sha256:ts",
                ),
                (
                    "aliasName",
                    "scip.call-occurrence",
                    "resolved",
                    "scip-typescript@1.0",
                    "sha256:ts",
                ),
                (
                    "helper",
                    "scip.call-occurrence",
                    "resolved",
                    "scip-typescript@1.0",
                    "sha256:ts",
                ),
                (
                    "helper",
                    "tree-sitter.call-imported-name",
                    "name_matched",
                    None,
                    None,
                ),
                (
                    "inner",
                    "scip.call-local-symbol",
                    "resolved",
                    "scip-typescript@1.0",
                    "sha256:ts",
                ),
                (
                    "over",
                    "scip.call-overload-implementation",
                    "resolved",
                    "scip-typescript@1.0",
                    "sha256:ts",
                ),
            ],
        )
        self.assertEqual(
            output.metadata.custom_metrics["call_resolution_by_resolver"],
            {
                "scip": {
                    "linked": 5,
                    "unresolved:scip_external": 1,
                    "unresolved:scip_no_occurrence": 1,
                    "unresolved:scip_target_not_declaration": 1,
                },
                "tree-sitter": {"linked": 1},
            },
        )
        unresolved = sorted(
            (
                n.properties["callee"],
                n.properties["resolver"],
                n.properties["reason"],
                n.properties.get("producer"),
            )
            for n in output.nodes
            if "UnresolvedCall" in n.labels
        )
        self.assertEqual(
            unresolved,
            [
                ("console.log", "scip", "scip_external", "scip-typescript@1.0"),
                ("helper", "scip", "scip_no_occurrence", "scip-typescript@1.0"),
                (
                    "hh",
                    "scip",
                    "scip_target_not_declaration",
                    "scip-typescript@1.0",
                ),
            ],
        )


class CalleeAtTests(unittest.TestCase):
    def test_callee_at_is_where_the_last_name_starts(self):
        with tempfile.TemporaryDirectory() as root:
            files = {
                "a.ts": "class C { #p() {} m() { this.#p(); a.b.f(); f()(); new a.K(); } }\n",
                "b.py": "x.y.z()\nsuper().__init__()\ng()()\n",
                "c.go": "package c\n\nfunc F() { a.B(); F(); func() {}() }\n",
            }
            for name, text in files.items():
                Path(root, name).write_text(text)
            result = analyze_repository(
                root, repository_id="r", languages=("typescript", "python", "go")
            )
            found = sorted(
                (s.file, s.callee, s.callee_at)
                for s in result.call_sites
                if s.file in files
            )
            self.assertEqual(
                found,
                sorted(
                    [
                        ("a.ts", "this.#p", (1, 29)),
                        ("a.ts", "a.b.f", (1, 39)),
                        ("a.ts", "f", (1, 44)),
                        ("a.ts", "a.K", (1, 57)),
                        ("b.py", "x.y.z", (1, 4)),
                        ("b.py", "super().__init__", (2, 8)),
                        ("b.py", "super", (2, 0)),
                        ("b.py", "g", (3, 0)),
                        ("c.go", "a.B", (3, 13)),
                        ("c.go", "F", (3, 18)),
                    ]
                ),
            )
            restored = type(result).from_json(result.to_json())
            self.assertEqual(
                [s.callee_at for s in restored.call_sites],
                [s.callee_at for s in result.call_sites],
            )
            site = result.call_sites[0]
            for wrong in ((0, 1), (1, -1), (1,), [1, 2], (1, True)):
                with self.assertRaises(ValueError):
                    type(site)(**{**site.__dict__, "callee_at": wrong})

    def test_awaited_calls_with_type_arguments_are_recorded(self):
        # The grammar reads `await a.f<T>()` as a call of `await a.f`.
        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.ts").write_text(
                "async function h(c) {\n"
                "  await c.req.parseBody<{ a: string }>();\n"
                "  await f<T>(); await g(); x.y<T>();\n"
                "}\n"
            )
            result = analyze_repository(
                root, repository_id="r", languages=("typescript",)
            )
            self.assertEqual(
                sorted((s.callee, s.callee_at) for s in result.call_sites),
                [
                    ("c.req.parseBody", (2, 14)),
                    ("f", (3, 8)),
                    ("g", (3, 22)),
                    ("x.y", (3, 29)),
                ],
            )


if __name__ == "__main__":
    unittest.main()
