"""Explicit registry; discovering a tool never runs its analysis."""

from pathlib import Path

from .base import AnalysisTool, ToolCapability


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, type[AnalysisTool]] = {}

    def register(self, tool_class: type[AnalysisTool]):
        if not isinstance(tool_class, type) or not issubclass(tool_class, AnalysisTool):
            raise TypeError("Registered tools must implement AnalysisTool")
        metadata = tool_class(Path(".")).metadata
        if metadata.name in self._tools:
            raise ValueError(f"Tool already registered: {metadata.name}")
        self._tools[metadata.name] = tool_class
        return tool_class

    def get_tool(self, name: str, repo_path: Path) -> AnalysisTool | None:
        tool_class = self._tools.get(name)
        return None if tool_class is None else tool_class(repo_path)

    def list_tools(self):
        return [self._tools[name](Path(".")).metadata for name in sorted(self._tools)]

    def get_tools_for_capability(self, capability: ToolCapability, repo_path: Path):
        tools = [self._tools[name](repo_path) for name in sorted(self._tools)]
        return [tool for tool in tools if tool.has_capability(capability)]

    def get_tools_for_language(self, language: str, repo_path: Path):
        tools = [self._tools[name](repo_path) for name in sorted(self._tools)]
        return [tool for tool in tools if tool.supports_language(language)]


tool_registry = ToolRegistry()


def register_tool(tool_class: type[AnalysisTool]):
    return tool_registry.register(tool_class)
