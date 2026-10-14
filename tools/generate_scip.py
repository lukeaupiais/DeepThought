"""Regenerate vendored SCIP types with protoc and a private descriptor pool.

The source schema is Sourcegraph SCIP (Apache-2.0). A private pool allows the
engine to coexist with another host's SCIP bindings without global collisions.
"""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCHEMA = SRC / "deepthought/_scip/scip.proto"
subprocess.run(
    ["protoc", "-I", str(SRC), f"--python_out={SRC}", str(SCHEMA)], check=True
)
generated = SCHEMA.with_name("scip_pb2.py")
text = generated.read_text()
old = "_descriptor_pool.Default().AddSerializedFile"
if text.count(old) != 1:
    raise RuntimeError("Unsupported protoc generator output")
text = text.replace(old, "_descriptor_pool.DescriptorPool().AddSerializedFile")
text = "# Modified: private descriptor pool for host interoperability.\n" + text
generated.write_text(text)
