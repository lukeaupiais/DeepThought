"""The engine installs its pinned tools into its cache when they are not
installed, and fails when it cannot."""

import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest

from deepthought import go_callgraph, js_callgraph, tool_provisioning, vue_sfc

# A stand-in npm: installs "<package>@<version>" under --prefix, and a jelly
# executable for Jelly; each run is logged. NPM_FAIL makes it fail.
NPM = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
with open(os.environ["TOOL_LOG"], "a") as log:
    log.write("npm " + " ".join(sys.argv[1:]) + "\n")
if os.environ.get("NPM_FAIL"):
    sys.stderr.write("network is unreachable\n")
    sys.exit(1)
prefix = Path(sys.argv[sys.argv.index("--prefix") + 1])
package, _, version = sys.argv[-1].rpartition("@")
directory = prefix / "node_modules" / package
directory.mkdir(parents=True)
(directory / "package.json").write_text(json.dumps({"version": version}))
if package == "@cs-au-dk/jelly":
    binary = prefix / "node_modules" / ".bin" / "jelly"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\necho " + version + "\n")
    binary.chmod(0o755)
"""

# A stand-in go: "install <path>@<version>" writes the command into $GOBIN,
# "version -m <binary>" reports the module the binary names.
GO = r"""#!/usr/bin/env python3
import os, sys
from pathlib import Path
with open(os.environ["TOOL_LOG"], "a") as log:
    log.write("go " + " ".join(sys.argv[1:]) + " GOFLAGS=" + os.environ.get("GOFLAGS", "") + "\n")
if sys.argv[1] == "install":
    path, _, version = sys.argv[2].rpartition("@")
    module = path.split("/cmd/")[0]
    binary = Path(os.environ["GOBIN"]) / path.rsplit("/", 1)[1]
    binary.write_text("mod " + module + " " + version + "\n")
    binary.chmod(0o755)
elif sys.argv[1] == "version":
    print(sys.argv[3] + ": go1.24")
    print("\t" + Path(sys.argv[3]).read_text())
"""


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.log = self.directory / "log"
        self.log.touch()
        self.cache = self.directory / "cache"
        self.environ = {
            "PATH": str(self.bin),
            "DEEPTHOUGHT_CACHE": str(self.cache),
            "TOOL_LOG": str(self.log),
            "GOFLAGS": "-mod=readonly",
        }
        python = shutil.which("python3")
        for name, text in {"npm": NPM, "go": GO, "node": "#!/bin/sh\n"}.items():
            path = self.bin / name
            path.write_text(text.replace("/usr/bin/env python3", python, 1))
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def runs(self):
        return self.log.read_text().splitlines()

    def test_vue_parser_is_installed_once_into_the_cache(self):
        node, package = vue_sfc.locate(self.environ)
        self.assertEqual(node, str(self.bin / "node"))
        self.assertTrue(str(package).startswith(str(self.cache / "tools" / "npm")))
        self.assertEqual(
            json.loads((package / "package.json").read_text())["version"],
            vue_sfc.VUE_COMPILER_SFC_VERSION,
        )
        self.assertEqual(vue_sfc.locate(self.environ)[1], package)
        (run,) = self.runs()
        self.assertIn("--ignore-scripts", run)
        self.assertTrue(run.endswith(vue_sfc.VUE_COMPILER_SFC_TOOL))

    def test_jelly_is_installed_once_into_the_cache(self):
        binary = js_callgraph.locate_jelly(self.environ)
        self.assertEqual(binary.name, "jelly")
        self.assertTrue(str(binary).startswith(str(self.cache / "tools" / "npm")))
        self.assertEqual(js_callgraph.locate_jelly(self.environ), binary)
        installs = [r for r in self.runs() if r.startswith("npm")]
        self.assertEqual(len(installs), 1)
        self.assertTrue(installs[0].endswith(js_callgraph.JELLY_TOOL))

    def test_a_jelly_of_another_version_on_path_is_not_used(self):
        other = self.bin / "jelly"
        other.write_text("#!/bin/sh\necho 0.1.0\n")
        other.chmod(0o755)
        self.assertNotEqual(js_callgraph.locate_jelly(self.environ), other)

    def test_the_pinned_jelly_on_path_is_used(self):
        on_path = self.bin / "jelly"
        on_path.write_text(f"#!/bin/sh\necho {js_callgraph.JELLY_VERSION}\n")
        on_path.chmod(0o755)
        self.assertEqual(js_callgraph.locate_jelly(self.environ), on_path)
        self.assertEqual(self.runs(), [])

    def test_callgraph_is_built_once_into_the_cache(self):
        binary = go_callgraph.locate_go_callgraph(self.environ)
        self.assertEqual(binary.name, "callgraph")
        self.assertTrue(str(binary).startswith(str(self.cache / "tools" / "go")))
        self.assertEqual(go_callgraph.locate_go_callgraph(self.environ), binary)
        installs = [r for r in self.runs() if r.startswith("go install")]
        self.assertEqual(
            installs,
            [
                "go install golang.org/x/tools/cmd/callgraph@"
                f"{go_callgraph.GO_CALLGRAPH_VERSION} GOFLAGS="
            ],
        )

    def test_a_failed_install_is_an_error_and_leaves_nothing(self):
        environ = {**self.environ, "NPM_FAIL": "1"}
        with self.assertRaisesRegex(vue_sfc.VueUnavailable, "network is unreachable"):
            vue_sfc.locate(environ)
        with self.assertRaisesRegex(js_callgraph.JellyUnavailable, "failed"):
            js_callgraph.locate_jelly(environ)
        npm = self.cache / "tools" / "npm"
        self.assertEqual(
            [p for p in npm.rglob("*") if p.is_file()] if npm.exists() else [], []
        )

    def test_a_missing_installer_is_an_error(self):
        (self.bin / "npm").unlink()
        (self.bin / "go").unlink()
        with self.assertRaisesRegex(vue_sfc.VueUnavailable, "needs npm"):
            vue_sfc.locate(self.environ)
        with self.assertRaisesRegex(js_callgraph.JellyUnavailable, "needs npm"):
            js_callgraph.locate_jelly(self.environ)
        with self.assertRaisesRegex(go_callgraph.GoCallgraphUnavailable, "needs go"):
            go_callgraph.locate_go_callgraph(self.environ)

    def test_a_configured_tool_is_never_replaced(self):
        environ = {
            **self.environ,
            js_callgraph.JELLY_ENVIRONMENT_VARIABLE: str(self.directory / "none"),
        }
        with self.assertRaisesRegex(js_callgraph.JellyUnavailable, "names no file"):
            js_callgraph.locate_jelly(environ)
        self.assertEqual(self.runs(), [])

    def test_the_cache_follows_the_environment(self):
        self.assertEqual(
            tool_provisioning.tools_directory({"DEEPTHOUGHT_CACHE": "/c"}),
            Path("/c/tools"),
        )
        self.assertEqual(
            tool_provisioning.tools_directory({"XDG_CACHE_HOME": "/x"}),
            Path("/x/deepthought/tools"),
        )


if __name__ == "__main__":
    unittest.main()
