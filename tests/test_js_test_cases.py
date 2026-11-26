"""JavaScript and TypeScript test cases as declarations, and their calls."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.calls import resolve_calls
from deepthought.ts_modules import with_compiler_modules

READY = all(
    importlib.util.find_spec(m)
    for m in ("tree_sitter", "tree_sitter_javascript", "tree_sitter_typescript")
)

SPEC = """\
import { describe, it, test, expect } from "vitest";
import { add } from "./add";

describe("add", () => {
  it("adds 'two' numbers", () => {
    expect(add(1, 2)).toBe(3);
  });
  it.each([[1, 1]])("adds %i", (a, b) => {
    add(a, b);
  });
  test.skipIf(process.env.CI)(`skips`, function () {
    add(0, 0);
  });
  it('doesn\\'t throw', async () => {
    add(2, 2);
  });
  it("adds", () => add(3, 3));
  it("adds", () => add(4, 4));
});
test("top", () => add(5, 5));
await Promise.all([it("a", () => add(6, 6)), it("b", () => add(7, 7))]);
"""

NOT_TESTS = """\
import { add } from "./add";

const kept = test("assigned", () => add(1, 1));
runner.it("member", () => add(1, 1));
it(title, () => add(1, 1));
it(`with ${title}`, () => add(1, 1));
it("tab\\tname", () => add(1, 1));
it.todo("later");
it.sometimes("unknown modifier", () => add(1, 1));
check("other function", () => add(1, 1));
it()("called result", () => add(1, 1));
it("no function", options);
"""


def analyze(files):
    with tempfile.TemporaryDirectory() as directory:
        for name, content in files.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        # JavaScript and TypeScript imports are the compiler's.
        return with_compiler_modules(
            analyze_repository(
                directory,
                repository_id="fixture",
                languages=("javascript", "typescript"),
            ),
            directory,
        )


@unittest.skipUnless(READY, "Provision the javascript and typescript extras")
class TestCaseTests(unittest.TestCase):
    def test_test_cases_and_suites_are_declarations_at_their_call(self):
        result = analyze(
            {
                "add.ts": "export function add(a: number, b: number) { return a + b }\n",
                "add.test.ts": SPEC,
            }
        )
        found = [
            (d.kind, d.symbol, d.line, d.column, d.end_line, d.end_column)
            for d in result.definitions
            if d.file == "add.test.ts"
        ]
        symbols = [(k, s) for k, s, *_ in found]
        self.assertEqual(
            [s for _, s in symbols],
            [
                'describe("add")',
                'describe("add").it("adds \'two\' numbers")',
                'describe("add").it.each("adds %i")',
                'describe("add").test.skipIf("skips")',
                'describe("add").it("doesn\'t throw")',
                'describe("add").it("adds")',
                'describe("add").it("adds")',
                'test("top")',
                'it("a")',
                'it("b")',
            ],
        )
        self.assertEqual([k for k, _ in symbols], ["TestSuite"] + ["Test"] * 9)
        # Each spans its whole call, from the line naming it.
        spans = {(s, line, end) for _, s, line, _, end, _ in found}
        self.assertIn(('describe("add")', 4, 19), spans)
        self.assertIn(('describe("add").it.each("adds %i")', 8, 10), spans)
        self.assertIn(('test("top")', 20, 20), spans)
        # Two cases of one name stay two declarations.
        ids = [d.id for d in result.definitions if d.symbol.endswith('it("adds")')]
        self.assertEqual(len(set(ids)), 2)

    def test_calls_inside_a_test_case_have_it_as_their_caller(self):
        result = analyze(
            {
                "add.ts": "export function add(a: number, b: number) { return a + b }\n",
                "add.test.ts": SPEC,
            }
        )
        symbols = {d.id: d.symbol for d in result.definitions}
        callers = {
            (site.callee, site.line): symbols.get(site.caller_id)
            for site in result.call_sites
            if site.file == "add.test.ts"
        }
        self.assertEqual(
            callers[("add", 6)], 'describe("add").it("adds \'two\' numbers")'
        )
        self.assertEqual(callers[("add", 9)], 'describe("add").it.each("adds %i")')
        self.assertEqual(callers[("add", 12)], 'describe("add").test.skipIf("skips")')
        self.assertEqual(callers[("add", 20)], 'test("top")')
        # A test case can be an element of a list of them.
        self.assertEqual(
            [
                symbols.get(site.caller_id)
                for site in sorted(result.call_sites, key=lambda c: c.column)
                if site.file == "add.test.ts"
                and site.line == 21
                and site.callee == "add"
            ],
            ['it("a")', 'it("b")'],
        )
        # A test case's own call, and its modifiers' calls, are its parent's.
        self.assertEqual(callers[("it", 5)], 'describe("add")')
        self.assertEqual(callers[("it.each", 8)], 'describe("add")')
        self.assertEqual(callers[("test.skipIf", 11)], 'describe("add")')
        self.assertIsNone(callers[("describe", 4)])
        self.assertIsNone(callers[("test", 20)])
        # A test case is never a call's target.
        definitions = {d.id: d for d in result.definitions}
        targets = {
            definitions[r.target_id].kind
            for r in resolve_calls(result)
            if r.target_id is not None
        }
        self.assertEqual(targets, {"Function"})

    def test_other_calls_with_callbacks_declare_nothing(self):
        result = analyze(
            {
                "add.ts": "export function add(a: number, b: number) { return a + b }\n",
                "other.test.ts": NOT_TESTS,
            }
        )
        self.assertEqual(
            [d.symbol for d in result.definitions if d.file == "other.test.ts"],
            [],
        )
        sites = [
            s
            for s in result.call_sites
            if s.file == "other.test.ts" and s.callee == "add"
        ]
        self.assertEqual(len(sites), 8)
        self.assertTrue(all(s.caller_id is None for s in sites))

    def test_javascript_test_cases_too(self):
        result = analyze(
            {
                "lib.js": "export function run() {}\n",
                "lib.test.js": (
                    "import { run } from './lib.js';\n"
                    "suite('lib', function () {\n"
                    "  specify('runs', () => { run(); });\n"
                    "  context('nested', () => {\n"
                    "    bench(`fast`, () => run());\n"
                    "  });\n"
                    "});\n"
                ),
            }
        )
        self.assertEqual(
            [(d.kind, d.symbol) for d in result.definitions if d.file == "lib.test.js"],
            [
                ("TestSuite", 'suite("lib")'),
                ("Test", 'suite("lib").specify("runs")'),
                ("TestSuite", 'suite("lib").context("nested")'),
                ("Test", 'suite("lib").context("nested").bench("fast")'),
            ],
        )


if __name__ == "__main__":
    unittest.main()
