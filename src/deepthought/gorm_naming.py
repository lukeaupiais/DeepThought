"""GORM's default table and column names and field tag settings, from
GORM's own naming and tag-parsing code.

``gormnames/main.go`` calls ``schema.NamingStrategy{}.TableName`` for each
model type name, and ``schema.ParseTagSetting`` and
``NamingStrategy{}.ColumnName`` for each requested struct field. It is
built inside a scratch copy of the analyzed module's
go.mod and go.sum, so the go command selects the gorm.io/gorm and
github.com/jinzhu/inflection versions that module selects (downloading
them when the module cache lacks them). Only the helper and the published
GORM packages it imports are compiled and run; nothing of the analyzed
module is. The binary is cached per (helper source, go.mod, go.sum, Go
toolchain).

A module whose graph selects no gorm.io/gorm (the helper never adds a
requirement), or whose helper cannot be built (a go.mod the go command
rejects outside its directory, no network for a download), gets no
default names, and the reason is returned for a warning.
"""

from hashlib import sha256
from importlib import resources
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .go_facts import _cache_directory, toolchain


def _first_line(text, fallback):
    lines = (text or "").strip().splitlines()
    return lines[0][:300] if lines else fallback


def build(go, module_directory, timeout=600):
    """(path of the helper binary for this module, None), or (None, reason)."""
    source = resources.files(__package__).joinpath("gormnames/main.go").read_bytes()
    directory = Path(module_directory)
    go_mod = (directory / "go.mod").read_bytes()
    go_sum = (
        (directory / "go.sum").read_bytes() if (directory / "go.sum").is_file() else b""
    )
    version = subprocess.run(
        [go, "version"], capture_output=True, text=True, timeout=60, check=True
    ).stdout.strip()
    key = sha256(b"\0".join([source, go_mod, go_sum, version.encode()])).hexdigest()[
        :16
    ]
    binary = _cache_directory() / f"gormnames-{key}"
    if binary.is_file():
        return binary, None
    binary.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        GOFLAGS="-mod=mod", GOTOOLCHAIN="auto", GOWORK="off", GOTELEMETRY="off"
    )
    with tempfile.TemporaryDirectory(prefix="deepthought-gormnames-") as scratch:
        (Path(scratch) / "go.mod").write_bytes(go_mod)
        (Path(scratch) / "go.sum").write_bytes(go_sum)
        (Path(scratch) / "main.go").write_bytes(source)
        output = Path(scratch) / "gormnames"
        try:
            # The module's own graph must select GORM: the build must not
            # add a requirement of its own.
            subprocess.run(
                [go, "list", "-m", "gorm.io/gorm"],
                cwd=scratch,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
                env=environment,
            )
        except subprocess.CalledProcessError as error:
            return None, "the module graph selects no gorm.io/gorm: " + _first_line(
                error.stderr, str(error.returncode)
            )
        except (OSError, subprocess.SubprocessError) as error:
            return None, f"gorm.io/gorm not listed: {type(error).__name__}"
        try:
            subprocess.run(
                [go, "build", "-trimpath", "-o", str(output), "main.go"],
                cwd=scratch,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
                env=environment,
            )
        except subprocess.CalledProcessError as error:
            return None, "gorm naming helper did not build: " + _first_line(
                error.stderr, str(error.returncode)
            )
        except (OSError, subprocess.SubprocessError) as error:
            return None, f"gorm naming helper did not build: {type(error).__name__}"
        staged = binary.with_name(binary.name + f".{os.getpid()}")
        shutil.copy2(output, staged)
        os.replace(staged, binary)
    return binary, None


def lookup(root, module_directory, tables=(), fields=(), *, go=None, timeout=600):
    """({"tables": {type name: GORM's default table name}, "fields": [...]},
    None) for the module at ``module_directory`` (relative to ``root``), or
    ({}, reason). ``fields`` are (struct tag, field name) pairs; each gets
    {"column", "prefix", "ignored", "embedded", "typed"} as GORM's own
    ParseTagSetting and NamingStrategy read them."""
    go = go or toolchain()
    if go is None:
        return {}, "the Go toolchain is not installed"
    if not tables and not fields:
        return {"tables": {}, "fields": []}, None
    try:
        binary, reason = build(go, Path(root) / module_directory, timeout)
    except (OSError, subprocess.SubprocessError) as error:
        return {}, f"gorm naming helper did not build: {type(error).__name__}"
    if binary is None:
        return {}, reason
    try:
        run = subprocess.run(
            [str(binary)],
            input=json.dumps(
                {"tables": sorted(set(tables)), "fields": [list(f) for f in fields]}
            ),
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        return json.loads(run.stdout), None
    except subprocess.CalledProcessError as error:
        return {}, "gorm naming helper failed: " + _first_line(
            error.stderr, str(error.returncode)
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return {}, f"gorm naming helper failed: {type(error).__name__}"


def table_names(root, module_directory, names, *, go=None, timeout=600):
    """({type name: GORM's default table name}, None) for the module at
    ``module_directory`` (relative to ``root``), or ({}, reason)."""
    if not names:
        return {}, None
    found, reason = lookup(root, module_directory, tables=names, go=go, timeout=timeout)
    return found.get("tables", {}), reason
