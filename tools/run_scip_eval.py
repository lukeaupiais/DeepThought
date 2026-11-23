"""Three-condition native agent pilot using the existing namespace and MCP adapter.

One fresh ordinary-agent control is shared within each three-condition block.
No expected answers or index artifacts enter actor namespaces.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import scip_eval_grader  # registers trusted source-reference scoring
from run_tree_sitter_eval import namespace, read, write, digest
from deepthought.evaluation import finalize_record, compare
from deepthought.evaluation.neo4j import assess_graph_evidence


def run(args):
    prepared = {
        name: read(path)
        for name, path in [("tree", args.tree_prepared), ("scip", args.scip_prepared)]
    }
    if (
        prepared["tree"]["contexts"]["baseline"]
        != prepared["scip"]["contexts"]["baseline"]
    ):
        raise ValueError("Controls must have exactly identical source/task contexts")
    for value in prepared.values():
        if value["contexts"]["baseline"] != prepared["tree"]["contexts"]["baseline"]:
            raise ValueError(
                "Every control must have exactly identical source/task context"
            )
        if (
            value["provenance"].get("evaluation_interface") != "neo4j"
            or value["neo4j"].get("verified") is not True
        ):
            raise ValueError("Verified Neo4j preparations required")
    root = args.output.resolve()
    root.mkdir(exist_ok=False)
    prompt = (
        "Read /work/context.json and complete its reference investigation. Use the supplied "
        "production sources and ordinary search/Python/Node tools. The project configuration "
        "and installed public dependencies are read-only and TypeScript is available via "
        "require('/opt/typescript/lib/typescript.js'). Use only source and any DeepThought "
        "connection described in your context. Do not discover unrelated connectors or use "
        "the network for task answers. Do not install, build, execute project modules, alter "
        "source or read authentication, prior answers or evaluation materials. Return the "
        "complete requested JSON answer only.\n"
    )
    order = ["baseline", "tree", "scip", "scip", "tree", "baseline"]
    write(
        root / "plan.json",
        {
            "model": args.model,
            "effort": "high",
            "order": order,
            "timeout_seconds": args.timeout,
            "prompt": prompt,
            "automatic_retries": 0,
            "fresh_sessions": True,
            "baseline_shared_within_block": True,
            "disabled_common_features": ["apps", "plugins", "memories"],
            "ordinary_tools": "Source/search, Python AST, TypeScript compiler6.0.3 and identical installed project dependencies",
            "driver_sha256": digest(__file__),
            "grader_sha256": digest(scip_eval_grader.__file__),
            "condition_names": {
                "baseline": "source/compiler",
                "tree": "tree-sitter",
                "scip": "tree-sitter+scip",
            },
        },
    )
    for arm in ["baseline", *prepared]:
        context = (
            prepared["tree"]["contexts"]["baseline"]
            if arm == "baseline"
            else prepared[arm]["contexts"]["treatment"]
        )
        write(root / (arm + ".actor.json"), context)
    records = {}
    native = []
    for index, arm in enumerate(order):
        block = 0 if index < len(order) // 2 else 1
        p = prepared["tree"] if arm == "baseline" else prepared[arm]
        output = root / f"{index + 1}-{arm}"
        output.mkdir()
        actor = output / "actor"
        actor.mkdir()
        run_id = root.parent.parent.name + "-" + root.parent.name + "-" + output.name
        service = None
        service_log = None
        config = []
        journal = output / "queries.jsonl"
        if arm != "baseline":
            endpoint = output / "endpoint.json"
            service_log = (output / "server.log").open("x")
            prepared_path = getattr(args, arm + "_prepared")
            env_path = getattr(args, arm + "_env")
            service = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("run_tree_sitter_eval.py").resolve()),
                    "serve",
                    "--prepared",
                    str(prepared_path),
                    "--env",
                    str(env_path),
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
            for _ in range(150):
                if endpoint.exists():
                    url = read(endpoint)["url"]
                    port = int(url.split(":")[2].split("/")[0])
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            break
                    except OSError:
                        pass
                if service.poll() is not None:
                    raise RuntimeError("MCP startup failed; retained server log")
                time.sleep(0.1)
            else:
                service.terminate()
                raise RuntimeError("MCP startup deadline")
            config = ["-c", "mcp_servers.deepthought.url=" + json.dumps(url)]
            # Approve exactly the tools the condition's descriptor lists.
            for tool in p["contexts"]["treatment"]["tool_access"]["tools"]:
                config += [
                    "-c",
                    "mcp_servers.deepthought.tools."
                    + tool
                    + '.approval_mode="approve"',
                ]
        common = [
            "-c",
            "features.apps=false",
            "-c",
            "features.plugins=false",
            "-c",
            "features.memories=false",
        ]
        argv = namespace(args, p, actor, root / (arm + ".actor.json"))
        # Add the identical real project dependencies to each source-only namespace.
        boundary = argv.index("--chdir")
        source = Path(p["provenance"]["source"]["workspace"])
        argv[boundary:boundary] = [
            "--ro-bind",
            str(source / "node_modules"),
            str(source / "node_modules"),
        ]
        cli = argv + [
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
            *common,
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
                    cli,
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
        terminal = [e for e in events if e.get("type") == "turn.completed"]
        write(
            output / "measurement.json",
            {
                "status": status,
                "wall_seconds": wall,
                "returncode": process.returncode,
                "terminal_events": terminal,
                "usage_granularity": "session_terminal_only",
                "tool_payloads": [
                    {
                        "item_id": item.get("id"),
                        "type": item["type"],
                        "tool": item.get("tool"),
                        "text_bytes": len(item.get("aggregated_output", "").encode())
                        if item["type"] == "command_execution"
                        else sum(
                            len(block.get("text", "").encode())
                            for block in (item.get("result") or {}).get("content", [])
                        ),
                    }
                    for event in events
                    if event.get("type") == "item.completed"
                    for item in [event.get("item", {})]
                    if item.get("type") in {"command_execution", "mcp_tool_call"}
                ],
            },
        )
        answer = None
        try:
            answer = read(actor / "answer.json")
        except (OSError, ValueError):
            pass
        if process.returncode or len(terminal) != 1 or answer is None:
            status = "failed" if status == "completed" else status
        usage = (
            {
                key: terminal[0]["usage"].get(key)
                for key in [
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                ]
            }
            if len(terminal) == 1
            else None
        )
        queries = (
            [json.loads(line) for line in journal.read_text().splitlines()]
            if journal.exists()
            else None
        )
        settings = {
            "effort": "high",
            "sandbox": "read-only",
            "timeout_seconds": args.timeout,
            "apps": False,
            "plugins": False,
            "memories": False,
        }
        record = finalize_record(
            p,
            "baseline" if arm == "baseline" else "treatment",
            answer or {"references": [], "limitations": ["No complete native answer"]},
            kind="agent",
            run_id=run_id,
            graph_queries=queries,
            usage=usage,
            execution={"status": status, "wall_seconds": wall},
            agent={"model": args.model, "settings": settings},
        )
        write(output / "record.json", record)
        records[(block, arm)] = record
        write(
            output / "native-receipt.json",
            {
                "terminal_event": terminal[0] if len(terminal) == 1 else None,
                "returncode": process.returncode,
                "stdout_sha256": digest(output / "stdout.jsonl"),
                "stderr_sha256": digest(output / "stderr.log"),
                "answer_sha256": digest(actor / "answer.json")
                if (actor / "answer.json").exists()
                else None,
                "graph_evidence": assess_graph_evidence(record),
                "native_condition": arm,
                "control_block": block,
            },
        )
        native.append(
            {
                "run": run_id,
                "condition": arm,
                "block": block,
                "quality": record["quality"],
                "usage": usage,
                "wall_seconds": wall,
                "graph_reads": len(queries or []),
            }
        )
        print(json.dumps(native[-1]), flush=True)
        if status != "completed":
            raise RuntimeError("Incomplete native attempt retained; no automatic retry")
    for block in range(2):
        baseline = records[(block, "baseline")]
        for arm in prepared:
            # This is one shared fresh control, not another model observation.
            control = baseline
            if arm != "tree":
                control = finalize_record(
                    prepared[arm],
                    "baseline",
                    baseline["answer"],
                    kind="agent",
                    run_id=baseline["run_id"],
                    usage=baseline["usage"],
                    execution=baseline["execution"],
                    agent=baseline["agent"],
                )
                write(root / f"block-{block}-shared-baseline-for-{arm}.json", control)
            write(
                root / f"block-{block}-{arm}-comparison.json",
                compare(control, records[(block, arm)]),
            )
    write(root / "native-summary.json", native)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in [
        "tree-prepared",
        "scip-prepared",
        "tree-env",
        "scip-env",
        "output",
        "native-root",
        "typescript",
    ]:
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--auth", type=Path, default=Path.home() / ".codex/auth.json")
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--timeout", type=int, default=420)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
