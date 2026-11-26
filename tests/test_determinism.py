"""Analysis output is byte-identical across processes, paths and file order.

Each case analyzes copies of one workspace in fresh interpreters with
different PYTHONHASHSEED values, absolute roots, working directories and file
creation orders (see tools/check_determinism.py). Everything the engine
derives (artifact, call/import/supertype/impl resolutions, composed graph,
standardized store input) must be byte-identical between them.
"""

import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import check_determinism  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
GRAMMARS = ("python", "typescript", "javascript", "go", "java", "rust")
READY = importlib.util.find_spec("tree_sitter") and all(
    importlib.util.find_spec(f"tree_sitter_{g}") for g in GRAMMARS
)

GO = {
    "go.mod": "module example.com/app\n\ngo 1.22\n",
    "main.go": 'package main\n\nimport "example.com/app/util"\n\n'
    "func main() { util.Run(); helper() }\nfunc helper() {}\n",
    "util/run.go": "package util\n\ntype Runner struct{}\n\n"
    "func (r *Runner) Go() {}\nfunc Run() { (&Runner{}).Go() }\n",
}
JAVA = {
    "src/a/Base.java": "package a;\npublic class Base { public void run() {} }\n",
    "src/a/Shape.java": "package a;\npublic interface Shape { int area(); }\n",
    "src/b/Square.java": "package b;\nimport a.Base;\nimport a.Shape;\n"
    "public class Square extends Base implements Shape {\n"
    "  public int area() { run(); return helper(2); }\n"
    "  static int helper(int x) { return x * x; }\n}\n",
}
RUST = {
    "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
    "src/lib.rs": "pub mod shapes;\nuse crate::shapes::{Area, Square};\n"
    "pub fn total(s: &Square) -> u32 { s.area() + shapes::unit() }\n",
    "src/shapes.rs": "pub trait Named {}\npub trait Area: Named { fn area(&self) -> u32; }\n"
    "pub struct Square(pub u32);\nimpl Named for Square {}\n"
    "impl Area for Square { fn area(&self) -> u32 { self.side() } }\n"
    "impl Square { fn side(&self) -> u32 { Self::scale(self.0) } "
    "fn scale(x: u32) -> u32 { x } }\npub fn unit() -> u32 { 1 }\n",
}


def write(directory, files):
    for name, content in files.items():
        path = Path(directory) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


@unittest.skipUnless(READY, "Every language grammar is required")
class DeterminismTest(unittest.TestCase):
    def assertDeterministic(self, workspace, languages):
        digests, dumps = check_determinism.run_variants(workspace, languages, runs=2)
        if digests[0] != digests[1]:
            self.fail(
                check_determinism.first_difference(
                    json.loads(dumps[0]), json.loads(dumps[1])
                )
            )
        return json.loads(dumps[0])

    def test_polyglot_workspace_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            # Python imports resolve from the workspace root.
            shutil.copytree(FIXTURES / "python", directory, dirs_exist_ok=True)
            for language in ("typescript", "javascript"):
                shutil.copytree(FIXTURES / language, Path(directory, language))
            write(Path(directory, "go"), GO)
            write(Path(directory, "java"), JAVA)
            write(Path(directory, "rust"), RUST)
            document = self.assertDeterministic(Path(directory), GRAMMARS)
        # The workspace exercises linking in every language, so agreement is
        # not agreement on empty output.
        files = {s["id"]: s["file"] for s in document["analysis"]["call_sites"]}
        linked = {
            Path(files[r["call_id"]]).suffix
            for r in document["calls"]
            if r["status"] == "linked"
        }
        self.assertLessEqual({".py", ".ts", ".js", ".go", ".java", ".rs"}, linked)
        self.assertTrue(document["imports"] and document["supertypes"])
        self.assertTrue(document["impls"] and document["graph"])
        # Calls in the tool's output are bound from the pinned SCIP indexers.
        rules = {
            r["properties"]["rule_id"]
            for r in document["standardized_output"]["relationships"]
            if r["type"] == "CALLS"
        }
        self.assertIn("scip.call-occurrence", rules)

    def test_first_difference_names_the_path(self):
        left = {"calls": [{"status": "linked", "target_id": "a"}]}
        right = {"calls": [{"status": "linked", "target_id": "b"}]}
        self.assertEqual(
            check_determinism.first_difference(left, right),
            ("$.calls[0].target_id", "a", "b"),
        )
        self.assertEqual(
            check_determinism.first_difference([1], [1, 2]), ("$.length", 1, 2)
        )
        self.assertIsNone(check_determinism.first_difference(left, left))

    def test_timing_fields_are_the_only_ones_ignored(self):
        self.assertEqual(
            check_determinism._strip_timing(
                {"metadata": {"execution_time_seconds": 1.5, "tool": "x"}}
            ),
            {"metadata": {"tool": "x"}},
        )


if __name__ == "__main__":
    unittest.main()
