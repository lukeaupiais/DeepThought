"""Task-generic native-agent A/B runner for milestone task scenarios.

The successor to run_tree_sitter_eval.py for any scenario whose grader is
registered (tools/ab_graders.py for agent-ab-1). Same isolation: each session is
a fresh, ephemeral Codex CLI process in a bubblewrap namespace with the pinned
source bound read-only, the grading truth kept outside it, native receipts and
a graph read journal retained per session. No automatic retries: an incomplete
session is recorded and the run goes on.

    python tools/run_ab_eval.py prepare SCENARIO WORKSPACE PREPARED [--tool NAME ...]
    python tools/run_ab_eval.py run --prepared PREPARED --env ENV --output DIR \
        --native-root CODEX_ROOT [--repetitions 3] [--timeout 600]

prepare extracts with tree-sitter plus any further --tool, imports one snapshot
into the Neo4j database named by DEEPTHOUGHT_NEO4J_* and verifies the readback.
Make that database read-only before run. run uses the agent CLI on the
operator's own subscription; it never calls a paid model API itself.
"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ab_graders  # noqa: E402,F401  (registers the milestone graders)
from run_tree_sitter_eval import digest, namespace, read, write  # noqa: E402
from deepthought.evaluation import compare, finalize_record  # noqa: E402

MILESTONE = Path(__file__).resolve().parents[1] / "evals/milestones/agent-ab-1.json"

# Treatment-arm MCP tools approved without a prompt. The first five are the
# milestone's impact, callers, component, snapshot and explain tools; the rest
# locate declarations and snapshots so those can be called.
APPROVED_TOOLS = (
    "getImpact",
    "getFunctionCallers",
    "getComponentImpact",
    "diffSnapshots",
    "explainEdge",
    "searchCodeElements",
    "getCodeContext",
    "getFileStructure",
    "getFunctionCalls",
    "listSnapshots",
    "listComponents",
)

TOOL_ACCESS = {
    "command": "mcp:deepthought",
    "help": (
        "Production MCP over a read-only Neo4j snapshot of this repository at the "
        "pinned commit. searchCodeElements, getFileStructure and getCodeContext "
        "locate declarations. getFunctionCallers and getFunctionCalls return "
        "stored CALLS edges; getImpact returns transitive dependents of a "
        "declaration, including callers across service contracts, with depth and "
        "evidence; listComponents and getComponentImpact work at service and "
        "package level; listSnapshots and diffSnapshots compare snapshots; "
        "explainEdge resolves the rule that produced an edge. Every edge carries "
        "its evidence type and rule ID. Ordinary source tools remain available."
    ),
    "tools": list(APPROVED_TOOLS),
}

PROMPT = (
    "Read /work/context.json and perform its task. The repository it names is "
    "the working directory, read-only, at a pinned commit. Use ordinary source "
    "search (rg, find, sed, cat), Python and Node, and optionally the TypeScript "
    "compiler parser via require('/opt/typescript/lib/typescript.js') when "
    "present. If DeepThought MCP is configured, you may use it, then confirm what "
    "you report from source. Do not install, build or execute repository code, "
    "modify files, read authentication or unrelated files, or use the network. "
    "Return only the JSON answer the context's instructions ask for.\n"
)

USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def order(position, repetitions):
    """Session arms for the task at 1-based position in the milestone list.

    Odd positions run B T T B B T, even positions the mirror image, and so on
    for more repetitions, so neither arm always goes first.
    """
    block = ["baseline", "treatment", "treatment", "baseline"]
    arms = []
    for i in range(repetitions):
        pair = block[(i % 2) * 2 : (i % 2) * 2 + 2]
        arms += pair
    if position % 2 == 0:
        arms = ["treatment" if a == "baseline" else "baseline" for a in arms]
    return arms


def task_position(scenario, milestone=MILESTONE):
    tid = scenario["id"].rsplit("-", 1)[-1]
    ids = [task["id"] for task in read(milestone)["tasks"]]
    if tid not in ids:
        raise ValueError(f"{scenario['id']} is not a task of {milestone}")
    return ids.index(tid) + 1


def codex_argv(model, effort, approved):
    config = []
    for name in approved:
        config += [
            "-c",
            f'mcp_servers.deepthought.tools.{name}.approval_mode="approve"',
        ]
    return [
        "/opt/codex/bin/codex",
        "--no-daemon",
        "-a",
        "never",
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--json",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        *config,
        "-o",
        "/work/output/answer.json",
        "-",
    ]


def receipts(events):
    """Count native tool use from the agent's JSON event stream."""
    tools, commands = Counter(), 0
    for event in events:
        item = event.get("item", {}) if event.get("type") == "item.completed" else {}
        if item.get("type") == "mcp_tool_call":
            tools[item.get("tool") or item.get("name") or "unknown"] += 1
        elif item.get("type") == "command_execution":
            commands += 1
    return {"mcp_tool_calls": dict(sorted(tools.items())), "commands": commands}


def prepare(args):
    from deepthought.evaluation.neo4j import prepare_neo4j
    from deepthought.graph_cli import connection_settings

    if args.output.exists():
        raise SystemExit("Output exists; keep earlier preparations separately")
    scenario = json.loads(args.scenario.read_text())
    prepared = prepare_neo4j(
        scenario,
        args.workspace,
        settings=connection_settings(),
        additional_tools=tuple(args.tool or ()),
        tool_access=TOOL_ACCESS,
        verification_timeout=args.verification_timeout,
    )
    write(args.output, prepared)
    print(json.dumps({"output": str(args.output), "neo4j": prepared["neo4j"]}))


def start_service(args, output, journal, run_id):
    endpoint = output / "endpoint.json"
    log = (output / "server.log").open("x")
    service = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("run_tree_sitter_eval.py").resolve()),
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
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(300):
        if endpoint.exists():
            url = read(endpoint)["url"]
            port = int(url.split(":")[2].split("/")[0])
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    return service, log, url
            except OSError:
                pass
        if service.poll() is not None:
            raise RuntimeError("MCP startup failed; see retained server.log")
        time.sleep(0.1)
    service.terminate()
    raise RuntimeError("MCP startup deadline exceeded")


def session(args, prepared, root, contexts, index, arm):
    output = root / f"{index + 1}-{arm}"
    output.mkdir()
    actor_output = output / "actor"
    actor_output.mkdir()
    run_id = root.name + "-" + output.name
    journal = output / "queries.jsonl"
    service = log = None
    config = []
    if arm == "treatment":
        service, log, url = start_service(args, output, journal, run_id)
        config = ["-c", "mcp_servers.deepthought.url=" + json.dumps(url)]
    argv = namespace(args, prepared, actor_output, contexts[arm])
    argv += codex_argv(args.model, args.effort, APPROVED_TOOLS if service else ())
    argv[-3:-3] = config
    print(run_id + " starting", flush=True)
    started = time.monotonic()
    status, returncode = "completed", None
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
                process.communicate(PROMPT, timeout=args.timeout)
            except subprocess.TimeoutExpired:
                status = "timed_out"
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            returncode = process.returncode
    finally:
        wall = time.monotonic() - started
        if service is not None:
            service.terminate()
            service.wait(timeout=10)
            log.close()
    events = []
    for line in (output / "stdout.jsonl").read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    terminal = [e for e in events if e.get("type") == "turn.completed"]
    answer = None
    try:
        answer = read(actor_output / "answer.json")
    except (OSError, ValueError):
        pass
    if returncode or len(terminal) != 1 or answer is None:
        status = "failed" if status == "completed" else status
    usage = (
        {key: terminal[0]["usage"].get(key) for key in USAGE_KEYS}
        if len(terminal) == 1
        else None
    )
    queries = (
        [json.loads(line) for line in journal.read_text().splitlines() if line]
        if journal.exists()
        else None
    )
    record = finalize_record(
        prepared,
        arm,
        answer if isinstance(answer, dict) else {},
        kind="agent",
        run_id=run_id,
        graph_queries=queries,
        usage=usage,
        execution={"status": status, "wall_seconds": wall},
        agent={
            "model": args.model,
            "settings": {
                "cli": args.cli_version,
                "effort": args.effort,
                "sandbox": "read-only",
                "timeout_seconds": args.timeout,
                "approved_tools": list(APPROVED_TOOLS),
            },
        },
    )
    write(output / "record.json", record)
    write(
        output / "native-receipt.json",
        {
            "returncode": returncode,
            "terminal_event": terminal[0] if len(terminal) == 1 else None,
            "stdout_sha256": digest(output / "stdout.jsonl"),
            "stderr_sha256": digest(output / "stderr.log"),
            "answer_sha256": digest(actor_output / "answer.json")
            if (actor_output / "answer.json").exists()
            else None,
            "graph_reads": len(queries or []),
            **receipts(events),
        },
    )
    print(
        json.dumps(
            {
                "run": run_id,
                "status": status,
                "quality_pass": record["quality"].get("quality_pass"),
                "f1": record["quality"].get("f1"),
                "usage": usage,
                "wall_seconds": round(wall, 1),
                "graph_reads": len(queries or []),
            }
        ),
        flush=True,
    )
    return record


def run(args):
    prepared = read(args.prepared)
    if (
        prepared.get("provenance", {}).get("evaluation_interface") != "neo4j"
        or prepared.get("neo4j", {}).get("verified") is not True
    ):
        raise ValueError("A/B runs need a verified Neo4j preparation")
    arms = order(task_position(prepared["scenario"], args.milestone), args.repetitions)
    root = args.output.resolve()
    root.mkdir(exist_ok=args.resume)
    plan = {
        "model": args.model,
        "cli": args.cli_version,
        "effort": args.effort,
        "order": arms,
        "timeout_seconds": args.timeout,
        "prompt": PROMPT,
        "tool_access": TOOL_ACCESS,
        "driver_sha256": digest(__file__),
        "graders_sha256": digest(Path(__file__).with_name("ab_graders.py")),
        "automatic_retries": 0,
    }
    if args.resume:
        if read(root / "plan.json") != plan:
            raise ValueError("Cannot resume a changed plan")
    else:
        write(root / "plan.json", plan)
    contexts = {}
    for arm, context in prepared["contexts"].items():
        contexts[arm] = root / (arm + ".actor.json")
        if args.resume:
            if read(contexts[arm]) != context:
                raise ValueError("Cannot resume changed actor inputs")
        else:
            write(contexts[arm], context)
    records = []
    for index, arm in enumerate(arms):
        done = root / f"{index + 1}-{arm}" / "record.json"
        if args.resume and done.exists():
            records.append(read(done))
            continue
        if args.resume and done.parent.exists():
            raise ValueError(
                f"{done.parent} is incomplete; inspect it, it is not retried"
            )
        records.append(session(args, prepared, root, contexts, index, arm))
    baselines = [r for r in records if r["arm"] == "baseline"]
    treatments = [r for r in records if r["arm"] == "treatment"]
    for n, (b, t) in enumerate(zip(baselines, treatments), 1):
        write(root / f"pair-{n}.json", compare(b, t))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("scenario", type=Path)
    prep.add_argument("workspace", type=Path)
    prep.add_argument("output", type=Path)
    prep.add_argument(
        "--tool", action="append", help="extraction tool after tree-sitter"
    )
    prep.add_argument("--verification-timeout", type=float, default=30)
    actor = commands.add_parser("run")
    actor.add_argument("--prepared", type=Path, required=True)
    actor.add_argument("--env", type=Path, required=True)
    actor.add_argument("--output", type=Path, required=True)
    actor.add_argument("--native-root", type=Path, required=True)
    actor.add_argument("--auth", type=Path, default=Path.home() / ".codex/auth.json")
    actor.add_argument("--typescript", type=Path)
    actor.add_argument("--milestone", type=Path, default=MILESTONE)
    actor.add_argument("--model", required=True)
    actor.add_argument("--cli-version", required=True, help="recorded in every record")
    actor.add_argument("--effort", default="high")
    actor.add_argument("--repetitions", type=int, default=3)
    actor.add_argument("--timeout", type=int, default=600)
    actor.add_argument(
        "--resume",
        action="store_true",
        help="Continue unchanged inputs after inspecting retained records",
    )
    args = parser.parse_args(argv)
    if args.command == "prepare":
        prepare(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
