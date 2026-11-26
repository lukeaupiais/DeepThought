"""The engine's node helpers read all of a large standard input that
arrives slower than they read it. ``readFileSync(0)`` fails with EAGAIN on
such a pipe once it is momentarily empty."""

import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from deepthought import ts_facts, ts_modules, vue_sfc

PACKAGE = Path(ts_modules.__file__).parent


def _slowly(command, data, chunk=1 << 16):
    """(return code, stdout, stderr) of ``command`` fed ``data`` a chunk at
    a time, with a pause after each, so the pipe is often empty."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def feed():
        try:
            for start in range(0, len(data), chunk):
                process.stdin.write(data[start : start + chunk])
                process.stdin.flush()
                time.sleep(0.005)
        except BrokenPipeError:
            pass
        finally:
            process.stdin.close()

    errors = []
    writer = threading.Thread(target=feed)
    reader = threading.Thread(target=lambda: errors.append(process.stderr.read()))
    writer.start()
    reader.start()
    out = process.stdout.read()
    process.wait(timeout=300)
    writer.join()
    reader.join()
    return process.returncode, out, errors[0].decode("utf-8", "replace")


class SlowStdinTests(unittest.TestCase):
    def test_vue_blocks_reads_every_component(self):
        try:
            node, package = vue_sfc.locate()
        except vue_sfc.VueUnavailable as error:
            self.skipTest(str(error))
        source = '<script setup lang="ts">\nconst a: number = 1\n</script>\n'
        data = json.dumps({"sources": [source] * 20000}).encode()
        code, out, err = _slowly(
            [node, str(PACKAGE / "vuesfc" / "blocks.js"), str(package)], data
        )
        self.assertEqual(code, 0, err[-500:])
        results = json.loads(out)["results"]
        self.assertEqual(len(results), 20000)
        self.assertEqual(results[-1]["blocks"][0]["lang"], "ts")

    def test_module_resolution_reads_its_whole_request(self):
        try:
            node, typescript = ts_modules.locate_typescript()
        except ts_modules.TypeScriptUnavailable as error:
            self.skipTest(str(error))
        with tempfile.TemporaryDirectory() as root:
            # Files that do not exist are not read; the request is large.
            files = [f"missing/{n:06d}/module.ts" for n in range(60000)]
            data = json.dumps({"root": root, "files": files}).encode()
            code, out, err = _slowly(
                [node, str(PACKAGE / "tsmodules" / "resolve.js"), typescript], data
            )
        self.assertEqual(code, 0, err[-500:])
        # One JSON row per line; none for files that do not exist.
        self.assertEqual([json.loads(row) for row in out.splitlines()], [])

    def test_ts_facts_reads_every_wanted_file(self):
        try:
            node, typescript = ts_facts.node(), ts_facts.typescript()
        except (ts_facts.TypeScriptMissing, OSError) as error:
            self.skipTest(str(error))
        with tempfile.TemporaryDirectory() as root:
            data = "".join(f"missing/{n:06d}/module.ts\n" for n in range(60000))
            code, _, err = _slowly(
                [
                    node,
                    str(PACKAGE / "tsfacts" / "main.js"),
                    str(typescript),
                    root,
                    "[]",
                ],
                data.encode(),
            )
        self.assertEqual(code, 0, err[-500:])


if __name__ == "__main__":
    unittest.main()
