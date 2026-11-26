"""The modules a Go module requires, downloaded for scip-go.

scip-go type-checks a package with ``go/packages``, which reads a required
module from the module cache: a module not there leaves the package's types
unknown, and calls into it bind nowhere. Before scip-go runs, each Go module
of the workspace has the go command download what it needs to build and test
its packages (``go mod download``) into a module cache in a scratch
directory, so the cache of the machine running the engine is neither read
nor changed. The go command reads its usual configuration (``GOPROXY``,
``GONOSUMDB``, ``GOPRIVATE``), so a mirror works. Downloading runs no code of
any module.

The workspace is left as it was: the go command is given a copy of the
module's ``go.mod`` and ``go.sum`` (``-modfile``), so checksums it adds go to
the copy. A module with ``vendor/modules.txt`` builds from its vendor
directory and needs nothing downloaded.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

# How the go command names a module it could not load: "go: path@version: ".
_NAMED = re.compile(r"^go: (\S+@v\S+): ", re.MULTILINE)


def download(root, modules, scratch, *, environ=None, timeout=600):
    """Download what each of ``modules`` (workspace-relative directories of a
    go.mod, ``.`` for the root) requires into a module cache under
    ``scratch``.

    Returns ``{"modcache", "modules": {module: entry}}``. Each entry has
    ``status``: ``none`` (nothing required), ``present`` (vendored),
    ``downloaded``, ``partial`` (some required modules could not be
    downloaded) or ``failed``; ``go_sum_sha256`` (None without a go.sum);
    ``downloaded``, the number of modules in the cache for it; ``failed``,
    each ``path@version`` that could not be downloaded; and ``detail``, the go
    command's output when it failed.
    """
    root = Path(root)
    scratch = Path(scratch)
    modcache = scratch / "modcache"
    modcache.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ if environ is None else environ)
    go = shutil.which("go", path=env.get("PATH"))
    # Downloaded files are left writable so that the scratch directory can be
    # removed; each module is downloaded on its own, as scip-go indexes it.
    env.update(GOMODCACHE=str(modcache), GOFLAGS="-modcacherw", GOWORK="off")
    found = {}
    for number, module in enumerate(modules):
        directory = root / module
        go_sum = directory / "go.sum"
        entry = {
            "status": "failed",
            "go_sum_sha256": (
                hashlib.sha256(go_sum.read_bytes()).hexdigest()
                if go_sum.is_file()
                else None
            ),
            "downloaded": 0,
            "failed": [],
            "detail": None,
        }
        found[module] = entry
        if not (directory / "go.mod").is_file():
            entry["status"] = "none"
            continue
        if (directory / "vendor" / "modules.txt").is_file():
            entry["status"] = "present"
            continue
        if go is None:
            entry["detail"] = "go is not installed"
            continue
        copy = scratch / "modfiles" / str(number)
        copy.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(directory / "go.mod", copy / "go.mod")
        if go_sum.is_file():
            shutil.copyfile(go_sum, copy / "go.sum")
        try:
            result = subprocess.run(
                [go, "mod", "download", "-json", f"-modfile={copy / 'go.mod'}"],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            entry["detail"] = "go mod download timed out"
            continue
        listed = _objects(result.stdout)
        entry["downloaded"] = sum(1 for o in listed if not o.get("Error"))
        # A module whose go.mod cannot be read stops the go command before it
        # lists anything; it names the module in its message instead.
        entry["failed"] = sorted(
            {f"{o.get('Path')}@{o.get('Version')}" for o in listed if o.get("Error")}
            | set(_NAMED.findall(result.stderr))
        )
        if result.returncode == 0:
            entry["status"] = "downloaded" if listed else "none"
        else:
            entry["status"] = "partial" if entry["downloaded"] else "failed"
            entry["detail"] = (result.stderr or result.stdout)[-4000:]
    return {"modcache": str(modcache), "modules": found}


def _objects(text):
    """The JSON objects ``go mod download -json`` prints one after another."""
    decoder = json.JSONDecoder()
    objects, index = [], 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return objects
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError:
            return objects
        if isinstance(value, dict):
            objects.append(value)
