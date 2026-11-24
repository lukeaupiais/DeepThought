"""Pinned pyright: the definitions of Python calls a SCIP index gets wrong.

scip-python 0.6.8 names some Python targets wrongly (every name a package
re-exports through ``from .m import *`` gets one symbol of ``m``; a method
named like a builtin, ``cookies.set()``, can be recorded as the builtin) and
records no occurrence in code it treats as unreachable. Those calls are
answered by pyright's own go-to-definition, run as a language server, rather
than by hand-written rules. Pyright is a required dependency wherever
Python calls are bound: a missing or wrong-version executable is an error,
never a silent fallback.
"""

import json
import os
from pathlib import Path
import select
import shutil
import subprocess
from urllib.parse import unquote

PYRIGHT_NAME = "pyright"
PYRIGHT_VERSION = "1.1.409"
PYRIGHT_ENVIRONMENT_VARIABLE = "DEEPTHOUGHT_PYRIGHT"
PYRIGHT_TOOL = f"{PYRIGHT_NAME}@{PYRIGHT_VERSION}"
# Seconds one language-server request may take before the run fails.
_TIMEOUT = 120


class PyrightUnavailable(RuntimeError):
    """The pinned pyright cannot be run."""


def locate_pyright(environ=None):
    """``pyright-langserver`` beside a ``pyright`` that reports the pinned
    version: ``$DEEPTHOUGHT_PYRIGHT`` (the ``pyright`` executable) if set,
    else PATH. Raises ``PyrightUnavailable`` otherwise."""
    environ = os.environ if environ is None else environ
    configured = environ.get(PYRIGHT_ENVIRONMENT_VARIABLE)
    found = configured or shutil.which(PYRIGHT_NAME, path=environ.get("PATH"))
    if not found:
        raise PyrightUnavailable(
            f"pyright {PYRIGHT_VERSION} is required to bind Python calls; "
            f"install it or set ${PYRIGHT_ENVIRONMENT_VARIABLE}"
        )
    executable = Path(found)
    server = executable.with_name("pyright-langserver")
    if not (executable.is_file() and server.is_file()):
        raise PyrightUnavailable(f"no pyright-langserver beside {executable}")
    version = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True, timeout=60
    ).stdout.split()
    if version[-1:] != [PYRIGHT_VERSION]:
        raise PyrightUnavailable(
            f"pyright {PYRIGHT_VERSION} is required, found {' '.join(version)}"
        )
    return server


class PyrightDefinitions:
    """A pyright language server over ``root``: ``definitions(file, line,
    column)`` gives where pyright says the name at that position is defined.
    Positions are 1-based lines and UTF-16 columns, as LSP counts them.
    Use as a context manager; the server stops on exit."""

    tool = PYRIGHT_TOOL

    def __init__(self, root, server=None):
        self.root = Path(root).resolve()
        self.server = Path(server) if server is not None else locate_pyright()
        self._process = None
        self._next = 0
        self._opened = set()

    def __enter__(self):
        self._process = subprocess.Popen(
            [str(self.server), "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=self.root,
        )
        uri = self.root.as_uri()
        self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": uri,
                "workspaceFolders": [{"uri": uri, "name": self.root.name}],
                "capabilities": {"general": {"positionEncodings": ["utf-16"]}},
            },
        )
        self._send({"method": "initialized", "params": {}})
        return self

    def __exit__(self, *exc):
        process, self._process = self._process, None
        if process is not None:
            process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()
        return False

    def definitions(self, file, line, column):
        """(file relative to root or None for a definition outside it, line,
        UTF-16 column) for each definition pyright gives, in its order."""
        path = self.root / file
        uri = path.as_uri()
        if file not in self._opened:
            self._opened.add(file)
            self._send(
                {
                    "method": "textDocument/didOpen",
                    "params": {
                        "textDocument": {
                            "uri": uri,
                            "languageId": "python",
                            "version": 1,
                            "text": path.read_bytes().decode("utf-8", "replace"),
                        }
                    },
                }
            )
        found = self._request(
            "textDocument/definition",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line - 1, "character": column},
            },
        )
        # Pyright answers with a list of LSP Locations.
        places = [
            (
                self._relative(item["uri"]),
                item["range"]["start"]["line"] + 1,
                item["range"]["start"]["character"],
            )
            for item in found or ()
        ]
        return places

    def _relative(self, uri):
        prefix = self.root.as_uri() + "/"
        if not uri.startswith(prefix):
            return None
        return unquote(uri[len(prefix) :])

    def _send(self, message):
        body = json.dumps({"jsonrpc": "2.0", **message}).encode("utf-8")
        self._process.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self._process.stdin.flush()

    def _read(self):
        stream = self._process.stdout
        if not select.select([stream], [], [], _TIMEOUT)[0]:
            raise PyrightUnavailable("pyright did not answer in time")
        length = None
        while True:
            header = stream.readline()
            if not header:
                raise PyrightUnavailable("pyright stopped")
            header = header.strip()
            if not header:
                break
            key, _, value = header.decode("ascii").partition(":")
            if key.lower() == "content-length":
                length = int(value)
        if length is None:
            raise PyrightUnavailable("pyright sent a message without a length")
        return json.loads(stream.read(length))

    def _request(self, method, params):
        self._next += 1
        ident = self._next
        self._send({"id": ident, "method": method, "params": params})
        while True:
            message = self._read()
            if "method" in message and "id" in message:
                # A request from the server (configuration, progress): an
                # empty answer leaves pyright's defaults.
                self._send({"id": message["id"], "result": None})
            elif message.get("id") == ident:
                if "error" in message:
                    raise PyrightUnavailable(
                        f"pyright {method} failed: {message['error']}"
                    )
                return message.get("result")
