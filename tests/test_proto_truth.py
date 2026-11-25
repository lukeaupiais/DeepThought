import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import proto_truth  # noqa: E402

GO = os.environ.get("DEEPTHOUGHT_GO") or shutil.which("go")
PROTOC = importlib.util.find_spec("grpc_tools") is not None

PROTO = """syntax = "proto3";
package shop.v1;

import "google/api/annotations.proto";

message Item { string id = 1; }

service Catalog {
  rpc Get(Item) returns (Item) {
    option (google.api.http) = {
      get: "/v1/items/{id}"
      additional_bindings { post: "/v1/items:get" body: "*" }
    };
  }
  rpc Watch(Item) returns (stream Item) {
    option (google.api.http).post = "/v1/items:watch";
  }
  rpc Chat(stream Item) returns (stream Item);
}
"""


@unittest.skipUnless(GO and PROTOC, "go and grpcio-tools")
class ProtoTruthTests(unittest.TestCase):
    def test_engine_agrees_with_the_oracle(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "proto").mkdir(parents=True)
            (repo / "proto" / "catalog.proto").write_text(PROTO)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            report = proto_truth.compare(repo, proto_truth.build_oracle(tmp))
        self.assertEqual(report["rpcs"], {"engine": 3, "oracle": 3, "agree": 3})
        self.assertEqual(report["http"], {"engine": 3, "oracle": 3, "agree": 3})
        self.assertEqual(report["misreads"], 0)


if __name__ == "__main__":
    unittest.main()
