import builtins
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deepthought import ingest_scip


class ScipDependencyTest(unittest.TestCase):
    def test_missing_protobuf_fails_instead_of_returning_no_facts(self):
        real_import = builtins.__import__

        def without_protobuf(name, *args, **kwargs):
            if name.startswith("google.protobuf"):
                raise ImportError("No module named 'google.protobuf'")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as root:
            index = Path(root) / "index.scip"
            index.write_bytes(b"")
            with mock.patch("builtins.__import__", side_effect=without_protobuf):
                with self.assertRaisesRegex(RuntimeError, "protobuf"):
                    ingest_scip(index, repository_id="r")


if __name__ == "__main__":
    unittest.main()
