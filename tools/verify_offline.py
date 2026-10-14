"""Run installed-artifact acceptance with networking disabled and inputs read-only.

Provision a virtualenv outside the checkout first. Bubblewrap must be installed;
--privileged uses sudo on CI hosts that restrict unprivileged namespaces.
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


PROBE = r"""
import errno, os, pathlib, socket, subprocess, sys
assert os.geteuid() != 0, 'Acceptance must run as an unprivileged user'
assert not (pathlib.Path(sys.argv[1]) / 'pyproject.toml').exists(), 'Checkout is visible'
fixture = pathlib.Path(sys.argv[2])
try:
    with fixture.open('ab'):
        pass
except OSError as exc:
    assert exc.errno in (errno.EROFS, errno.EACCES), exc
else:
    raise AssertionError('Fixture is writable')
with socket.socket() as connection:
    connection.settimeout(1)
    try:
        connection.connect(('192.0.2.1', 9))
    except OSError as exc:
        assert exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH), exc
    else:
        raise AssertionError('External networking is reachable')
subprocess.run([str(pathlib.Path(sys.executable).parent / 'deepthought'), '--help'], check=True, stdout=subprocess.DEVNULL)
print('Verified installed CLI, hidden checkout, read-only fixtures and isolated network', flush=True)
"""

CORE = r"""
import pathlib, sys
import deepthought
from deepthought import analyze_repository, ingest_scip
root = pathlib.Path(sys.argv[1])
result = analyze_repository(root / 'python', repository_id='core', languages=('python',))
assert result.status == 'failed', result
assert {d.code for d in result.diagnostics} == {'grammar_unavailable'}, result.diagnostics
result = ingest_scip(root / 'scip/python.scip', repository_id='core')
assert result.status == 'failed'
assert {d.code for d in result.diagnostics} == {'protocol_unavailable'}, result.diagnostics
for name in ('tree_sitter', 'google.protobuf', 'neo4j', 'torch', 'sentence_transformers'):
    assert name not in sys.modules, name
print('Verified core-only installation and explicit missing-extra diagnostics')
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--privileged", action="store_true")
    parser.add_argument("--core-only", action="store_true")
    parser.add_argument("--hide-path", type=Path, action="append", default=[])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    # Keep the venv path rather than resolving its interpreter symlink.
    interpreter = args.python.absolute()
    if interpreter.is_relative_to(root):
        parser.error("The installed virtualenv must be outside the checkout")
    if shutil.which("bwrap") is None:
        parser.error("Provision bubblewrap before offline acceptance")
    with tempfile.TemporaryDirectory(prefix="deepthought-acceptance-") as directory:
        base = Path(directory)
        tests, scratch = base / "tests", base / "scratch"
        shutil.copytree(
            root / "tests", tests, ignore=shutil.ignore_patterns("__pycache__")
        )
        scratch.mkdir()
        command = ["bwrap"]
        if args.privileged:
            command = [
                "sudo",
                "--",
                "bwrap",
                "--uid",
                str(os.getuid()),
                "--gid",
                str(os.getgid()),
            ]
        command += [
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--bind",
            str(scratch),
            str(scratch),
        ]
        for hidden in [root, *args.hide_path]:
            command += ["--tmpfs", str(hidden.absolute())]
        command += [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--clearenv",
            "--setenv",
            "PATH",
            f"{interpreter.parent}:/usr/bin:/bin",
            "--setenv",
            "TMPDIR",
            str(scratch),
            "--setenv",
            "HOME",
            str(scratch),
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--chdir",
            str(tests),
            "--",
            str(interpreter),
        ]
        subprocess.run(
            command + ["-c", PROBE, str(root), str(tests / "fixtures/expected.json")],
            check=True,
        )
        if args.core_only:
            subprocess.run(command + ["-c", CORE, str(tests / "fixtures")], check=True)
        else:
            subprocess.run(
                command + ["-m", "unittest", "discover", "-s", ".", "-v"], check=True
            )


if __name__ == "__main__":
    main()
