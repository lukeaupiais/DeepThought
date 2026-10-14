"""Installed distribution acceptance; run against a built wheel, not PYTHONPATH."""

from importlib.metadata import distribution
from pathlib import Path
import unittest

import deepthought


class PackagingTests(unittest.TestCase):
    def test_loaded_sdk_is_from_installed_distribution(self):
        package = distribution("deepthought-analysis-engine")
        self.assertEqual(
            Path(deepthought.__file__).parent, package.locate_file("deepthought")
        )
        self.assertEqual(deepthought.__version__, package.version)
        entrypoints = [
            (e.name, e.value)
            for e in package.entry_points
            if e.group == "console_scripts"
        ]
        self.assertIn(("deepthought", "deepthought.cli:main"), entrypoints)

    def test_wheel_contains_schema_and_required_notices(self):
        package = distribution("deepthought-analysis-engine")
        paths = [str(item) for item in package.files]
        for suffix in (
            "deepthought/NOTICE",
            "deepthought/_scip/scip.proto",
            "licenses/LICENSE",
            "deepthought-original-MIT.txt",
        ):
            self.assertTrue(any(path.endswith(suffix) for path in paths), suffix)


if __name__ == "__main__":
    unittest.main()
