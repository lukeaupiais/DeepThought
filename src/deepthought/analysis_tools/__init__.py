"""AnalysisTool plugin port; optional grammars remain lazily provisioned."""

from .base import AnalysisTool, ToolCapability, ToolMetadata
from .contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from .registry import ToolRegistry, register_tool, tool_registry
from .tree_sitter import TreeSitterTool

__all__ = [
    "AnalysisTool",
    "NodeData",
    "RelationshipData",
    "StandardizedOutput",
    "ToolCapability",
    "ToolExecutionMetadata",
    "ToolMetadata",
    "ToolRegistry",
    "TreeSitterTool",
    "register_tool",
    "tool_registry",
]
