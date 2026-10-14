"""Prevent the acceptance oracle from accepting empty, false or ambiguous facts."""

import ast
import copy
import json
import unittest

from check_observations import FIXTURES, validate


class ObservationChecks(unittest.TestCase):
    def setUp(self):
        self.expected = json.loads((FIXTURES / "expected.json").read_text())[
            "languages"
        ]["python"]
        self.valid = {
            "definitions": [
                dict(item, id=f"definition-{i}")
                for i, item in enumerate(self.expected["definitions"])
            ],
            "relationships": copy.deepcopy(self.expected["required_relationships"]),
            "unresolved": copy.deepcopy(self.expected["unresolved"]),
            "coverage": [
                {"file": file, "status": "analyzed"}
                for file in sorted({d["file"] for d in self.expected["definitions"]})
            ],
        }

    def test_complete_observations_pass(self):
        self.assertEqual(validate(self.valid, self.expected), [])

    def test_empty_output_does_not_pass(self):
        self.assertTrue(validate({key: [] for key in self.valid}, self.expected))

    def test_type_annotation_cannot_be_instantiation(self):
        self.valid["relationships"].append(self.expected["forbidden_relationships"][0])
        self.assertTrue(
            any(
                "Forbidden" in failure
                for failure in validate(self.valid, self.expected)
            )
        )

    def test_same_named_functions_cannot_share_identity(self):
        self.valid["definitions"][-1]["id"] = self.valid["definitions"][-2]["id"]
        self.assertTrue(
            any(
                "colliding" in failure
                for failure in validate(self.valid, self.expected)
            )
        )

    def test_missing_alias_target_is_detected(self):
        self.valid["relationships"].pop(1)
        self.assertTrue(
            any(
                "Missing relationship" in failure
                for failure in validate(self.valid, self.expected)
            )
        )

    def test_unresolved_symbol_must_be_disclosed(self):
        self.valid["unresolved"] = []
        self.assertTrue(
            any(
                "unresolved" in failure
                for failure in validate(self.valid, self.expected)
            )
        )

    def test_missing_file_coverage_is_detected(self):
        self.valid["coverage"].pop()
        self.assertTrue(
            any(
                "coverage" in failure for failure in validate(self.valid, self.expected)
            )
        )

    def test_invalid_collection_is_not_empty_success(self):
        self.valid["definitions"] = None
        self.assertTrue(validate(self.valid, self.expected))

    def test_malformed_document_is_rejected(self):
        self.assertTrue(validate([], self.expected))
        self.valid["relationships"] = [None]
        self.assertTrue(validate(self.valid, self.expected))

    def test_typescript_expected_names_and_locations_match_source(self):
        import re

        expected = json.loads((FIXTURES / "expected.json").read_text())["languages"][
            "typescript"
        ]
        for definition in expected["definitions"]:
            lines = (
                (FIXTURES / "typescript" / definition["file"]).read_text().splitlines()
            )
            keyword = "class" if definition["kind"] == "Class" else "function"
            pattern = rf"^export {keyword} {re.escape(definition['symbol'])}\b"
            self.assertRegex(lines[definition["line"] - 1], pattern)

    def test_python_expected_locations_agree_with_independent_ast(self):
        for expected in self.expected["definitions"]:
            tree = ast.parse((FIXTURES / "python" / expected["file"]).read_text())
            candidates = [
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef))
            ]
            matches = [
                node
                for node in candidates
                if node.name == expected["symbol"] and node.lineno == expected["line"]
            ]
            self.assertEqual(len(matches), 1, expected)


if __name__ == "__main__":
    unittest.main()
