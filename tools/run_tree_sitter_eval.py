"""Native-agent public pilot driver. Run from an installed DeepThought environment.

The operator provisions a dedicated, database-enforced read-only Neo4j snapshot
with prepare_neo4j before running. Private prepared files and journals stay outside
the actor namespace. Behavioral findings require independent rubric review.
"""

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from deepthought.evaluation import (
    Grader,
    compare,
    finalize_record,
    grade,
    register_grader,
    validate_scenario,
)


def declaration_scenario(scenario):
    value = deepcopy(scenario)
    value["grader"] = "declarations-v1"
    return value


def validate_expected(scenario):
    validate_scenario(declaration_scenario(scenario))


def score(scenario, answer):
    # Exact declarations are machine-scored. Narrative quality is deliberately
    # separate and assessed against the source-backed, pre-run locked rubric.
    return grade(declaration_scenario(scenario), answer)


register_grader(
    "refactor-navigation-v1",
    Grader(
        validate_expected,
        score,
        "Return one JSON object with claims [{file,symbol,kind,line}], "
        "relationships [] and limitations [strings]. Put the requested "
        "source-cited behavioral review in limitations. Claims are only the "
        "requested explicit declarations. Use Class, Function, Method, "
        "Interface, Enum, TypeAlias or Namespace. Python class-scope functions "
        "are Method, nested functions are Function. Async uses the same labels. "
        "TypeScript function declarations including overload signatures are "
        "Function. Join named lexical scopes with dots. Line is 1-based start "
        "of declaration syntax excluding decorators. Distinguish declarations "
        "from behavior, which must be established by reading source.",
    ),
)


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def environment(path):
    for line in Path(path).read_text().splitlines():
        key, value = line.split("=", 1)
        os.environ[key] = value


def namespace(args, prepared, output, context):
    source = Path(prepared["provenance"]["source"]["workspace"])
    argv = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--clearenv",
        "--ro-bind",
        "/usr/lib",
        "/usr/lib",
        "--ro-bind",
        "/usr/share",
        "/usr/share",
        "--dir",
        "/usr/bin",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/home/pilot/.codex",
        "--dir",
        "/work",
        "--ro-bind",
        str(args.native_root),
        "/opt/codex",
        "--ro-bind",
        str(args.auth),
        "/home/pilot/.codex/auth.json",
        "--ro-bind",
        str(context),
        "/work/context.json",
        "--bind",
        str(output),
        "/work/output",
    ]
    for name in (
        "bash",
        "sh",
        "cat",
        "sed",
        "rg",
        "find",
        "head",
        "tail",
        "wc",
        "nl",
        "sort",
        "cut",
        "tr",
        "uniq",
        "xargs",
        "ls",
        "pwd",
        "python",
        "python3",
        "node",
    ):
        path = Path("/usr/bin") / name
        if path.exists():
            argv += ["--ro-bind", str(path.resolve()), str(path)]
    for name in (
        "/etc/ssl",
        "/etc/ca-certificates/extracted",
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/passwd",
    ):
        if Path(name).exists():
            argv += ["--ro-bind", name, name]
    for name in ("/usr/lib/go", "/usr/lib/gcc", "/usr/lib/rustlib"):
        if Path(name).exists():
            argv += ["--tmpfs", name]
    if args.typescript:
        argv += ["--ro-bind", str(args.typescript), "/opt/typescript"]
    argv += ["--dir", str(source)]
    for name in prepared["scenario"]["source"]["files"]:
        argv += ["--ro-bind", str(source / name), str(source / name)]
    for key, value in {
        "HOME": "/home/pilot",
        "CODEX_HOME": "/home/pilot/.codex",
        "PATH": "/usr/bin:/bin:/opt/codex/bin",
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }.items():
        argv += ["--setenv", key, value]
    return argv + ["--chdir", str(source), "--"]


def serve(args):
    from deepthought.graph_mcp import create_server

    environment(args.env)
    prepared = read(args.prepared)
    Path(args.journal).touch(exist_ok=False)
    os.environ.update(
        DEEPTHOUGHT_EVAL_INPUT_ID=prepared["input_id"],
        DEEPTHOUGHT_EVAL_RUN_ID=args.run_id,
        DEEPTHOUGHT_EVAL_QUERY_LOG=str(args.journal),
        DEEPTHOUGHT_ALLOW_DATABASE_SCOPE_CYPHER="1",
    )
    server = create_server()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server.settings.host = "127.0.0.1"
    server.settings.port = port
    write(args.endpoint, {"url": f"http://127.0.0.1:{port}/mcp"})
    server.run(transport="streamable-http")


def run(args):
    prepared = read(args.prepared)
    if (
        prepared.get("provenance", {}).get("evaluation_interface") != "neo4j"
        or prepared.get("neo4j", {}).get("verified") is not True
    ):
        raise ValueError("Native tool evaluation requires a verified Neo4j preparation")
    root = args.output.resolve()
    root.mkdir(exist_ok=args.resume)
    prompt = (
        "Read /work/context.json and perform its investigation. You have the full "
        "selected production source tree read-only. Use ordinary source search, "
        "Python ast, and optionally the TypeScript compiler parser via "
        "require('/opt/typescript/lib/typescript.js') when present. "
        "If DeepThought MCP is configured, consult its declaration graph while "
        "investigating, then verify behavioral assertions from source. "
        "Do not install, build, execute repository modules, modify source, read "
        "authentication or unrelated files, or seek answers on the network. "
        "Return only the complete JSON answer.\n"
    )
    if args.resume:
        previous = read(root / "plan.json")
        if (
            previous["model"] != args.model
            or previous["timeout_seconds"] != args.timeout
            or previous["prompt"] != prompt
        ):
            raise ValueError("Cannot resume changed model, timeout or prompt")
    plan_path = root / ("resume-plan.json" if args.resume else "plan.json")
    write(
        plan_path,
        {
            "model": args.model,
            "effort": "high",
            "order": ["baseline", "treatment", "treatment", "baseline"],
            "timeout_seconds": args.timeout,
            "prompt": prompt,
            "driver_sha256": digest(__file__),
            "baseline": "Same source/search/Python AST/optional TypeScript compiler, no graph",
            "treatment": "Same tools and production MCP -> GraphQueryService -> read-only Neo4j",
            "automatic_retries": 0,
            "semantic_grading": "Locked source rubric, independent blinded reviewer",
        },
    )
    contexts = {}
    for arm, context in prepared["contexts"].items():
        contexts[arm] = root / (arm + ".actor.json")
        if args.resume:
            if read(contexts[arm]) != context:
                raise ValueError("Cannot resume changed actor inputs")
        else:
            write(contexts[arm], context)
    records = []
    for index, arm in enumerate(["baseline", "treatment", "treatment", "baseline"]):
        output = root / f"{index + 1}-{arm}"
        if args.resume and (output / "record.json").exists():
            records.append(read(output / "record.json"))
            continue
        output.mkdir()
        actor_output = output / "actor"
        actor_output.mkdir()
        run_id = root.name + "-" + output.name
        service = None
        service_log = None
        config = []
        journal = output / "queries.jsonl"
        if arm == "treatment":
            endpoint = output / "endpoint.json"
            service_log = (output / "server.log").open("x")
            service = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "serve",
                    "--prepared",
                    str(args.prepared),
                    "--env",
                    str(args.env),
                    "--journal",
                    str(journal),
                    "--run-id",
                    run_id,
                    "--endpoint",
                    str(endpoint),
                ],
                stdout=service_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            for _ in range(100):
                if endpoint.exists():
                    url = read(endpoint)["url"]
                    port = int(url.split(":")[2].split("/")[0])
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            break
                    except OSError:
                        pass
                if service.poll() is not None:
                    raise RuntimeError("MCP startup failed; see retained server.log")
                time.sleep(0.1)
            else:
                service.terminate()
                raise RuntimeError("MCP startup deadline exceeded")
            config = ["-c", "mcp_servers.deepthought.url=" + json.dumps(url)]
            for name in (
                "queryGraph",
                "getCodeContext",
                "getFileStructure",
                "searchCodeElements",
            ):
                config += [
                    "-c",
                    f'mcp_servers.deepthought.tools.{name}.approval_mode="approve"',
                ]
        argv = namespace(args, prepared, actor_output, contexts[arm]) + [
            "/opt/codex/bin/codex",
            "--no-daemon",
            "-a",
            "never",
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--json",
            "--model",
            args.model,
            "-c",
            'model_reasoning_effort="high"',
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            *config,
            "-o",
            "/work/output/answer.json",
            "-",
        ]
        print(run_id + " starting", flush=True)
        started = time.monotonic()
        status = "completed"
        try:
            with (
                (output / "stdout.jsonl").open("x") as stdout,
                (output / "stderr.log").open("x") as stderr,
            ):
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    start_new_session=True,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                )
                try:
                    process.communicate(prompt, timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    status = "timed_out"
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        finally:
            wall = time.monotonic() - started
            if service is not None:
                service.terminate()
                service.wait(timeout=10)
                service_log.close()
        events = [
            json.loads(line)
            for line in (output / "stdout.jsonl").read_text().splitlines()
            if line.strip()
        ]
        terminal = [event for event in events if event.get("type") == "turn.completed"]
        answer = None
        try:
            answer = read(actor_output / "answer.json")
        except (OSError, ValueError):
            pass
        if process.returncode or len(terminal) != 1 or answer is None:
            status = "failed" if status == "completed" else status
        write(
            output / "measurement.json",
            {
                "wall_seconds": wall,
                "status": status,
                "returncode": process.returncode,
                "terminal_events": terminal,
            },
        )
        usage = (
            {
                key: terminal[0]["usage"].get(key)
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                )
            }
            if len(terminal) == 1
            else None
        )
        queries = (
            [json.loads(line) for line in journal.read_text().splitlines()]
            if journal.exists()
            else None
        )
        record = finalize_record(
            prepared,
            arm,
            answer
            or {
                "claims": [],
                "relationships": [],
                "limitations": ["No complete answer"],
            },
            kind="agent",
            run_id=run_id,
            graph_queries=queries,
            usage=usage,
            execution={"status": status, "wall_seconds": wall},
            agent={
                "model": args.model,
                "settings": {
                    "effort": "high",
                    "sandbox": "read-only",
                    "timeout_seconds": args.timeout,
                },
            },
        )
        write(output / "record.json", record)
        write(
            output / "native-receipt.json",
            {
                "returncode": process.returncode,
                "terminal_event": terminal[0] if len(terminal) == 1 else None,
                "stdout_sha256": digest(output / "stdout.jsonl"),
                "stderr_sha256": digest(output / "stderr.log"),
                "answer_sha256": digest(actor_output / "answer.json")
                if (actor_output / "answer.json").exists()
                else None,
                "mcp_tool_calls": sum(
                    e.get("type") == "item.completed"
                    and e.get("item", {}).get("type") == "mcp_tool_call"
                    for e in events
                ),
            },
        )
        records.append(record)
        print(
            json.dumps(
                {
                    "run": run_id,
                    "status": status,
                    "quality": record["quality"],
                    "usage": usage,
                    "wall_seconds": wall,
                    "graph_reads": len(queries or []),
                }
            ),
            flush=True,
        )
        if status != "completed":
            raise RuntimeError(
                "Incomplete native run retained; automatic retries disabled"
            )
    for name, baseline, treatment in [
        ("pair-1", records[0], records[1]),
        ("pair-2", records[3], records[2]),
    ]:
        write(root / (name + ".json"), compare(baseline, treatment))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    actor = commands.add_parser("run")
    actor.add_argument("--prepared", type=Path, required=True)
    actor.add_argument("--env", type=Path, required=True)
    actor.add_argument("--output", type=Path, required=True)
    actor.add_argument("--native-root", type=Path, required=True)
    actor.add_argument("--auth", type=Path, default=Path.home() / ".codex/auth.json")
    actor.add_argument("--typescript", type=Path)
    actor.add_argument("--model", default="gpt-6.1-sol")
    actor.add_argument("--timeout", type=int, default=420)
    actor.add_argument(
        "--resume",
        action="store_true",
        help="Continue unchanged inputs after inspecting retained records",
    )
    server = commands.add_parser("serve")
    for name in ("prepared", "env", "journal", "endpoint"):
        server.add_argument("--" + name, type=Path, required=True)
    server.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if args.command == "serve":
        serve(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
