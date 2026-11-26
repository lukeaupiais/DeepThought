import io
import os
from pathlib import Path
import queue
import shutil
import stat
import tempfile
import unittest

from deepthought.pyright_calls import (
    PYRIGHT_ENVIRONMENT_VARIABLE,
    PYRIGHT_VERSION,
    PyrightDefinitions,
    PyrightUnavailable,
    _read_messages,
    locate_pyright,
)


def _pinned():
    try:
        return locate_pyright()
    except PyrightUnavailable:
        return None


def _script(directory, name, text):
    path = Path(directory) / name
    path.write_text("#!/bin/sh\n" + text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class ReadMessagesTests(unittest.TestCase):
    def test_messages_arriving_together_are_each_delivered(self):
        # Two messages in one write land in the stream's buffer together;
        # both must reach the reader, then why the stream ended.
        def frame(body):
            return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)

        stream = io.BufferedReader(
            io.BytesIO(frame(b'{"method": "log"}') + frame(b'{"id": 1, "result": 2}'))
        )
        messages = queue.Queue()
        _read_messages(stream, messages)
        self.assertEqual(messages.get_nowait(), {"method": "log"})
        self.assertEqual(messages.get_nowait(), {"id": 1, "result": 2})
        self.assertIsInstance(messages.get_nowait(), PyrightUnavailable)


class LocateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)

    def test_missing_pyright_is_an_error(self):
        with self.assertRaisesRegex(PyrightUnavailable, "is required"):
            locate_pyright({"PATH": self.directory})

    def test_the_language_server_must_sit_beside_it(self):
        executable = _script(
            self.directory, "pyright", f"echo pyright {PYRIGHT_VERSION}\n"
        )
        with self.assertRaisesRegex(PyrightUnavailable, "pyright-langserver"):
            locate_pyright({PYRIGHT_ENVIRONMENT_VARIABLE: str(executable)})

    def test_only_the_pinned_version_is_used(self):
        server = _script(self.directory, "pyright-langserver", "exit 0\n")
        executable = _script(self.directory, "pyright", "echo pyright 1.1.300\n")
        with self.assertRaisesRegex(PyrightUnavailable, "found pyright 1.1.300"):
            locate_pyright({PYRIGHT_ENVIRONMENT_VARIABLE: str(executable)})
        executable.write_text(f"#!/bin/sh\necho pyright {PYRIGHT_VERSION}\n")
        self.assertEqual(
            locate_pyright({PYRIGHT_ENVIRONMENT_VARIABLE: str(executable)}), server
        )
        self.assertEqual(
            locate_pyright({"PATH": self.directory + os.pathsep + "/usr/bin"}), server
        )


@unittest.skipUnless(_pinned(), f"Provision pyright {PYRIGHT_VERSION}")
class PyrightDefinitionsTests(unittest.TestCase):
    def test_definitions_through_star_re_exports_and_outside_the_root(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("from ._models import *\n")
        (root / "pkg" / "_models.py").write_text(
            "__all__ = ['Request', 'Response']\n"
            "class Request: pass\n"
            "class Response:\n"
            "    def set(self): pass\n"
        )
        (root / "main.py").write_text(
            'import pkg\ne = "\U0001f600"; r = pkg.Response()\nr.set()\nlen([])\n'
        )
        with PyrightDefinitions(root) as pyright:
            # UTF-16 columns: the emoji counts two.
            response = pyright.definitions("main.py", 2, 18)
            method = pyright.definitions("main.py", 3, 2)
            builtin = pyright.definitions("main.py", 4, 0)
        self.assertEqual(response, [("pkg/_models.py", 3, 6)])
        self.assertEqual(method, [("pkg/_models.py", 4, 8)])
        self.assertTrue(builtin)
        self.assertTrue(all(file is None for file, _, _ in builtin))


if __name__ == "__main__":
    unittest.main()
