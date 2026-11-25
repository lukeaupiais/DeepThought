"""Types a signature names, from SCIP references (deepthought.signature_types)."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools.tree_sitter import TreeSitterTool

READY = importlib.util.find_spec("tree_sitter") and all(
    importlib.util.find_spec(name)
    for name in ("tree_sitter_python", "tree_sitter_typescript")
)

TYPESCRIPT = {
    "package.json": '{"name": "guide", "version": "1.0.0", "private": true}\n',
    "tsconfig.json": '{"compilerOptions": {"strict": true}, "include": ["**/*.ts"]}\n',
    "pb/guide_pb.d.ts": "export class Point {\n  getX(): number;\n}\n"
    "export class Feature {\n  getName(): string;\n}\n",
    "pb/guide_grpc_pb.d.ts": 'import { Point, Feature } from "./guide_pb";\n'
    "export class GuideClient {\n"
    "  getFeature(request: Point, callback: "
    "(error: Error | null, response: Feature) => void): void;\n"
    "}\n",
    "client.ts": 'import { GuideClient } from "./pb/guide_grpc_pb";\n'
    'import { Point, Feature } from "./pb/guide_pb";\n'
    "\n"
    "export function lookup(client: GuideClient, point: Point, "
    "done: (f: Feature) => void): void {\n"
    "  client.getFeature(point, (error, feature) => done(feature));\n"
    "}\n"
    "\n"
    "export const named = (client: GuideClient, x: number): void => {\n"
    "  const point: Point = new Point();\n"
    "  lookup(client, point, (f) => console.log(f.getName(), x));\n"
    "};\n",
}
PYTHON = {
    "pyproject.toml": '[project]\nname = "p"\nversion = "0"\n',
    "pkg/__init__.py": "",
    "pkg/models.py": "class Point:\n    x: int = 0\n\n\nclass Feature:\n"
    '    name: str = ""\n',
    "pkg/use.py": "from typing import Optional\n"
    "from pkg.models import Point, Feature\n"
    "\n"
    "\n"
    "def lookup(point: Point, default: Optional[Feature] = None) -> Feature:\n"
    "    def inner(p: Point) -> int:\n"
    "        return p.x\n"
    "    inner(point)\n"
    "    return default or Feature()\n"
    "\n"
    "\n"
    "def named(x: int) -> str:\n"
    "    found: Feature = lookup(Point())\n"
    "    return found.name\n"
    "\n"
    "\n"
    "class Service:\n"
    '    def get(self, point: "Point") -> list[Feature]:\n'
    "        return [lookup(point)]\n",
}


def named_types(files, language):
    with tempfile.TemporaryDirectory() as directory:
        for path, content in files.items():
            (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
            (Path(directory) / path).write_text(content)
        output = asyncio.run(
            TreeSitterTool(directory).analyze("r", languages={language})
        )
    names = {
        n.properties["qualified_name"]: n.properties["name"]
        for n in output.nodes
        if "Symbol" in n.labels
    }
    edges = [r for r in output.relationships if r.type == "SIGNATURE_USES"]
    return output, sorted(
        (
            names[r.source_match["qualified_name"]],
            names[r.target_match["qualified_name"]],
            r.properties["line"],
            r.properties["rule_id"],
        )
        for r in edges
    )


@unittest.skipUnless(READY, "Provision the python and typescript grammars")
class SignatureTypeTests(unittest.TestCase):
    def test_typescript_parameters_results_and_callbacks(self):
        output, found = named_types(TYPESCRIPT, "typescript")
        rule = "scip.signature-type-reference"
        self.assertEqual(
            found,
            [
                ("GuideClient.getFeature", "Feature", 3, rule),
                ("GuideClient.getFeature", "Point", 3, rule),
                ("lookup", "Feature", 4, rule),
                ("lookup", "GuideClient", 4, rule),
                ("lookup", "Point", 4, rule),
                # A type named only in the body is not in the signature.
                ("named", "GuideClient", 8, rule),
            ],
        )
        self.assertEqual(output.metadata.custom_metrics["signature_types"], 6)

    def test_python_annotations_defaults_and_nested_functions(self):
        _, found = named_types(PYTHON, "python")
        self.assertEqual(
            [row[:3] for row in found],
            [
                ("Service.get", "Feature", 18),
                # A string annotation resolves too.
                ("Service.get", "Point", 18),
                ("lookup", "Feature", 5),
                ("lookup", "Point", 5),
                # The nested function's signature is its own, not lookup's.
                ("lookup.inner", "Point", 6),
            ],
        )


if __name__ == "__main__":
    unittest.main()
