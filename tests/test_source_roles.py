"""Test and example roles of repository paths."""

import re
import unittest

from deepthought.source_roles import ROLE_PATTERNS, exclusion_pattern, source_roles

PATHS = {
    "src/main.py": [],
    "src/testing/util.py": [],
    "src/contest.py": [],
    "latest/x.go": [],
    "tests/test_x.py": ["test"],
    "pkg/test_x.py": ["test"],
    "test_x.py": ["test"],
    "pkg/x_test.py": ["test"],
    "conftest.py": ["test"],
    "src/pkg/conftest.py": ["test"],
    "app/tests.py": ["test"],
    "app/mytests.py": [],
    "app/tests_util.py": [],
    "app/conftest_helpers.py": [],
    "pkg/x_test.go": ["test"],
    "src/test/java/a/ATest.java": ["test"],
    "web/__tests__/a.ts": ["test"],
    "web/a.test.tsx": ["test"],
    "web/a.spec.mjs": ["test"],
    "spec/models/user_spec.rb": ["test"],
    "pkg/testdata/in.go": ["test"],
    "examples/demo.py": ["example"],
    "docs/samples/a.java": ["example"],
    "examples/tests/test_demo.py": ["example", "test"],
    "web/a.test.json": [],
    "_examples/rest/main.go": ["example"],
    "src/main/java/a/LatestValue.java": [],
    "src/main/java/a/Contest.java": [],
    "lib/src/a/FooTest.java": ["test"],
    "lib/src/a/FooTests.kt": ["test"],
    "src/Cart.Tests/CartServiceTests.cs": ["test"],
    "src/Cart.UnitTests/Helpers.cs": ["test"],
    "src/Cart.Api/Program.cs": [],
    "benchmarks/run.ts": [],
}


class SourceRoleTests(unittest.TestCase):
    def test_paths_have_their_roles(self):
        for path, roles in PATHS.items():
            with self.subTest(path=path):
                self.assertEqual(source_roles(path), roles)
        self.assertEqual(source_roles(None), [])
        self.assertEqual(source_roles(""), [])

    def test_exclusion_pattern_matches_what_the_roles_classify(self):
        # Neo4j's =~ matches a whole string, as fullmatch does here.
        for selected in (["test"], ["example"], ["test", "example"]):
            pattern = exclusion_pattern(selected)
            for path, roles in PATHS.items():
                with self.subTest(selected=selected, path=path):
                    self.assertEqual(
                        bool(re.fullmatch(pattern, path)),
                        bool(set(roles) & set(selected)),
                    )
        self.assertIsNone(exclusion_pattern(None))
        for bad in ([], ["docs"], "test", 1):
            with self.assertRaises(ValueError):
                exclusion_pattern(bad)

    def test_patterns_use_syntax_java_regex_shares(self):
        # Neo4j compiles these with java.util.regex: keep to groups,
        # alternation, classes and escapes both engines read alike.
        for pattern in ROLE_PATTERNS.values():
            self.assertNotRegex(pattern, r"\(\?P|\(\?<[^=!]|\\[AZz]")


if __name__ == "__main__":
    unittest.main()
