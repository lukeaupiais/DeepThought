"""The script of Vue single-file components (``.vue``), as Vue's own parser
reads it.

``vuesfc/blocks.js`` asks the pinned ``@vue/compiler-sfc`` (the parser Vue's
build tools and its language tools use) where each component's
``<script>`` and ``<script setup>`` blocks are and what language they are
written in. The engine reads a component as TypeScript: its source with
every byte outside those blocks blanked (line breaks kept), so offsets,
lines and columns inside a block are the component's own. The template and
styles are not read. A block written with ``src`` has no content in the
component and is left out.

The package is a required dependency of reading ``.vue`` files: a missing
or different version raises ``VueUnavailable``.
"""

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess

VUE_COMPILER_SFC_VERSION = "3.5.45"
VUE_COMPILER_SFC_TOOL = f"@vue/compiler-sfc@{VUE_COMPILER_SFC_VERSION}"
ENVIRONMENT_VARIABLE = "DEEPTHOUGHT_VUE_COMPILER_SFC"
SUFFIX = ".vue"
_HELPER = Path(__file__).with_name("vuesfc") / "blocks.js"
# Block languages the TSX grammar reads; the others are read as TypeScript,
# which JavaScript is a subset of for the grammar.
_JSX = frozenset({"tsx", "jsx"})


class VueUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ScriptBlock:
    start: int  # UTF-8 byte offset of the content
    end: int
    lang: str | None
    setup: bool


@dataclass(frozen=True)
class Component:
    blocks: tuple
    errors: tuple

    @property
    def jsx(self):
        return any(block.lang in _JSX for block in self.blocks)


def _version(package):
    try:
        return json.loads((package / "package.json").read_text("utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return None


def locate(environ=None):
    """(node, the @vue/compiler-sfc package directory):
    ``$DEEPTHOUGHT_VUE_COMPILER_SFC`` if set, else the package installed
    globally beside node at the pinned version, else the pinned version that
    the engine installs into its cache. Raises ``VueUnavailable`` when none
    can be had."""
    from .tool_provisioning import ToolInstallFailed, npm_package

    environ = os.environ if environ is None else environ
    node = shutil.which("node", path=environ.get("PATH"))
    if node is None:
        raise VueUnavailable(
            f"reading .vue files needs node and {VUE_COMPILER_SFC_TOOL}"
        )
    configured = environ.get(ENVIRONMENT_VARIABLE)
    if configured:
        package = Path(configured)
        version = _version(package)
        if version is None:
            raise VueUnavailable(
                f"${ENVIRONMENT_VARIABLE} names no {VUE_COMPILER_SFC_TOOL} "
                f"package: {package}"
            )
    else:
        package = (
            Path(node).resolve().parent.parent / "lib/node_modules/@vue/compiler-sfc"
        )
        version = _version(package)
        if version != VUE_COMPILER_SFC_VERSION:
            try:
                installed = npm_package(
                    "@vue/compiler-sfc", VUE_COMPILER_SFC_VERSION, environ
                )
            except ToolInstallFailed as error:
                raise VueUnavailable(
                    f"reading .vue files needs {VUE_COMPILER_SFC_TOOL}: {error}"
                ) from None
            package = installed / "node_modules/@vue/compiler-sfc"
            version = _version(package)
    if version != VUE_COMPILER_SFC_VERSION:
        raise VueUnavailable(
            f"@vue/compiler-sfc {version} found, {VUE_COMPILER_SFC_VERSION} required"
        )
    return node, package


_components = {}


def read(sources, environ=None, timeout=600):
    """The Component of each of ``sources`` (bytes), in order, read in one
    run of Vue's parser; components read before are kept by content."""
    sources = list(sources)
    keys = [sha256(source).hexdigest() for source in sources]
    missing = {
        key: source for key, source in zip(keys, sources) if key not in _components
    }
    if missing:
        node, package = locate(environ)
        texts = [source.decode("utf-8") for source in missing.values()]
        completed = subprocess.run(
            [node, str(_HELPER), str(package)],
            input=json.dumps({"sources": texts}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        if completed.returncode:
            raise VueUnavailable("Vue's parser failed: " + completed.stderr)
        results = json.loads(completed.stdout)["results"]
        for key, result in zip(missing, results):
            _components[key] = Component(
                tuple(
                    ScriptBlock(b["start"], b["end"], b["lang"], b["setup"])
                    for b in result["blocks"]
                ),
                tuple(result["errors"]),
            )
    return [_components[key] for key in keys]


def component(source, environ=None):
    return read([source], environ)[0]


def script_source(source, component):
    """``source`` with every byte outside the component's script blocks
    blanked: line breaks are kept, so every offset, line and column of a
    block is unchanged."""
    masked = bytearray(b if b in b"\r\n" else 0x20 for b in source)
    for block in component.blocks:
        masked[block.start : block.end] = source[block.start : block.end]
    return bytes(masked)


def is_component(file):
    return str(file).endswith(SUFFIX)


def prefetch(root, files, environ=None):
    """Read the components ``files`` (workspace-relative) name in one run
    of Vue's parser, so that reading each later is a lookup."""
    sources = []
    for file in files:
        try:
            sources.append((Path(root) / file).read_bytes())
        except OSError:
            continue
    if sources:
        read(sources, environ)
