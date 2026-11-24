"""Unit checks for the task-generic A/B runner (tools/run_ab_eval.py)."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import run_ab_eval  # noqa: E402
from deepthought.evaluation import _tool_access  # noqa: E402


class OrderTests(unittest.TestCase):
    def test_milestone_counterbalancing(self):
        B, T = "baseline", "treatment"
        self.assertEqual(run_ab_eval.order(1, 3), [B, T, T, B, B, T])
        self.assertEqual(run_ab_eval.order(2, 3), [T, B, B, T, T, B])
        self.assertEqual(run_ab_eval.order(3, 1), [B, T])
        for position in (1, 2):
            arms = run_ab_eval.order(position, 3)
            self.assertEqual(arms.count(B), arms.count(T))

    def test_task_position_reads_the_milestone_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text(json.dumps({"tasks": [{"id": "U1"}, {"id": "U2"}]}))
            self.assertEqual(
                run_ab_eval.task_position({"id": "agent-ab-1-U2"}, path), 2
            )
            with self.assertRaises(ValueError):
                run_ab_eval.task_position({"id": "agent-ab-1-Z9"}, path)


class AgentTests(unittest.TestCase):
    def test_treatment_approves_the_milestone_tools(self):
        argv = run_ab_eval.codex_argv("m", "high", run_ab_eval.APPROVED_TOOLS)
        approved = [
            a.split(".")[3]
            for a in argv
            if a.startswith("mcp_servers.deepthought.tools.")
        ]
        for name in (
            "getImpact",
            "getFunctionCallers",
            "getComponentImpact",
            "diffSnapshots",
            "explainEdge",
        ):
            self.assertIn(name, approved)
        self.assertNotIn("queryGraph", approved)
        self.assertEqual(argv[-3:], ["-o", "/work/output/answer.json", "-"])
        baseline = run_ab_eval.codex_argv("m", "high", ())
        self.assertFalse(any("mcp_servers" in a for a in baseline))
        self.assertIn("read-only", baseline)
        self.assertIn("--ephemeral", baseline)

    def test_approved_tools_exist_in_the_mcp_server(self):
        source = (ROOT / "src" / "deepthought" / "graph_mcp.py").read_text()
        for name in run_ab_eval.APPROVED_TOOLS:
            self.assertIn(f"async def {name}(", source)

    def test_tool_access_is_a_valid_descriptor_without_task_hints(self):
        _tool_access(run_ab_eval.TOOL_ACCESS)
        text = run_ab_eval.TOOL_ACCESS["help"] + run_ab_eval.PROMPT
        for hint in ("InheritedFlags", "Mount", "GetCart", "isError", "omission"):
            self.assertNotIn(hint, text)
        self.assertEqual(
            run_ab_eval.TOOL_ACCESS["tools"], list(run_ab_eval.APPROVED_TOOLS)
        )

    def test_receipts_count_graph_and_shell_use(self):
        events = [
            {
                "type": "item.completed",
                "item": {"type": "mcp_tool_call", "tool": "getImpact"},
            },
            {
                "type": "item.completed",
                "item": {"type": "mcp_tool_call", "tool": "getImpact"},
            },
            {"type": "item.completed", "item": {"type": "command_execution"}},
            {"type": "turn.completed", "usage": {}},
        ]
        self.assertEqual(
            run_ab_eval.receipts(events),
            {"mcp_tool_calls": {"getImpact": 2}, "commands": 1},
        )


if __name__ == "__main__":
    unittest.main()
