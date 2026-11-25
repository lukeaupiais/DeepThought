"""ContractCoverage nodes: one per contract family and language present,
saying whether the family is read in that language."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

READY = (
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_python")
    and importlib.util.find_spec("tree_sitter_rust")
)

FILES = {
    "api/app.py": (
        "from flask import Flask\n"
        "import requests\n"
        "app = Flask(__name__)\n"
        "@app.route('/orders')\n"
        "def orders():\n"
        "    return requests.get('http://stock/items').text\n"
        "@app.route('/items')\n"
        "def items():\n"
        "    return ''\n"
    ),
    "tools/src/main.rs": 'fn main() { println!("hi"); }\n',
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class ContractCoverageTests(unittest.TestCase):
    def test_one_statement_per_family_and_language(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in FILES.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            output = asyncio.run(ServiceContractTool(root).analyze("r"))
        found = {
            (n.properties["family"], n.properties["language"]): n.properties
            for n in output.nodes
            if "ContractCoverage" in n.labels
        }
        languages = {language for _, language in found}
        self.assertEqual(languages, {"python", "rust"})
        self.assertEqual(len(found), 7 * 2)
        requests = found[("http_requests", "python")]
        self.assertEqual(
            {k: requests[k] for k in ("status", "files", "sites", "linked")},
            {"status": "read", "files": 1, "sites": 1, "linked": 1},
        )
        self.assertEqual(found[("http_routes", "python")]["sites"], 2)
        rust = found[("http_requests", "rust")]
        self.assertEqual(
            (rust["status"], rust["reason"], rust["sites"]),
            ("not_read", "no_reader", 0),
        )
        self.assertNotIn("reason", requests)
        self.assertNotIn("linked", found[("http_routes", "python")])


if __name__ == "__main__":
    unittest.main()
