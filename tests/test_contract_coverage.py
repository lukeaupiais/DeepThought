"""EdgeCoverage nodes of the contract families: one per (edge family,
protocol, language present), saying whether it is linked in that language."""

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
            (
                n.properties["family"],
                n.properties["protocol"],
                n.properties["language"],
            ): n.properties
            for n in output.nodes
            if "EdgeCoverage" in n.labels
        }
        self.assertEqual({key[2] for key in found}, {"python", "rust"})
        self.assertEqual(len(found), 8 * 2)
        requests = found[("REQUESTS", "http", "python")]
        self.assertEqual(
            {
                k: requests[k]
                for k in (
                    "status",
                    "files",
                    "files_linked",
                    "sites",
                    "linked",
                    "ambiguous",
                    "unresolved",
                )
            },
            {
                "status": "linked",
                "files": 1,
                "files_linked": 1,
                "sites": 1,
                "linked": 1,
                "ambiguous": 0,
                "unresolved": 0,
            },
        )
        self.assertEqual(len(requests["resolver"]), 1)
        self.assertTrue(requests["resolver"][0].startswith("service-contracts@"))
        self.assertNotIn("reason", requests)
        routes = found[("HANDLES", "http", "python")]
        self.assertEqual((routes["status"], routes["sites"]), ("linked", 2))
        self.assertNotIn("linked", routes)
        # A reader that ran and found nothing.
        grpc = found[("REQUESTS", "grpc", "python")]
        self.assertEqual((grpc["status"], grpc["sites"]), ("no_sites", 0))
        rust = found[("REQUESTS", "http", "rust")]
        self.assertEqual(
            (rust["status"], rust["reason"], rust["resolver"], rust["files_linked"]),
            ("not_linked", "not_supported", [], 0),
        )


if __name__ == "__main__":
    unittest.main()
