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
from deepthought import GraphSnapshot, QueryRequest, analyze_repository, compose_graph, ingest_scip, query_graph
root = pathlib.Path(sys.argv[1])
result = analyze_repository(root / 'python', repository_id='core', languages=('python',))
assert result.status == 'failed', result
assert {d.code for d in result.diagnostics} == {'grammar_unavailable'}, result.diagnostics
result = ingest_scip(root / 'scip/python.scip', repository_id='core')
assert result.status == 'failed'
assert {d.code for d in result.diagnostics} == {'protocol_unavailable'}, result.diagnostics
graph = compose_graph([result])
assert graph.status == 'failed'
assert GraphSnapshot.from_json(graph.to_json()) == graph
query = query_graph(graph, QueryRequest('definitions', 'core'))
assert query.status in {'failed', 'unavailable'} and not query.matches
assert query.to_dict()['analysis_status'] == 'failed'
for name in ('tree_sitter', 'google.protobuf', 'neo4j', 'torch', 'sentence_transformers'):
    assert name not in sys.modules, name
from hashlib import sha256
import subprocess, tempfile
from deepthought import AnalysisResult, check_source_consistency
from deepthought.models import FileCoverage
content = (root / 'python/main.py').read_bytes()
source = AnalysisResult('core', 'core-snapshot', 'succeeded', (),
    (FileCoverage('python/main.py', 'python', 'analyzed', sha256(content).hexdigest()),),
    (), 'tree-sitter', ())
with tempfile.TemporaryDirectory() as directory:
    artifact = pathlib.Path(directory) / 'analysis.json'
    artifact.write_text(source.to_json())
    expected = check_source_consistency(root, source)
    result = subprocess.run([str(pathlib.Path(sys.executable).parent / 'deepthought'),
        'check-source', str(root), str(artifact), '--repository-id', 'core'],
        capture_output=True, check=True, timeout=10)
    assert expected.status == 'unchanged'
    assert result.stdout == (expected.to_json() + '\n').encode(), result
print('Verified core-only installation, consistency CLI and explicit missing-extra diagnostics')
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
            command = ["sudo", "-n", "--", "bwrap"]
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
        ]
        if args.privileged:
            # Set up namespaces as root, then drop host credentials/capabilities.
            # bwrap --uid/--gid requires a user namespace on Ubuntu's version.
            command += [
                "/usr/bin/setpriv",
                "--reuid",
                str(os.getuid()),
                "--regid",
                str(os.getgid()),
                "--clear-groups",
                "--no-new-privs",
                "--bounding-set=-all",
            ]
        command += [str(interpreter)]
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
