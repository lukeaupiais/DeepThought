"""Independent analysis-tool interface, ported from the original MIT module.

See licenses/deepthought-original-MIT.txt. Tools produce facts and have no
knowledge of databases; importing this module provisions no optional analyzer.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .contracts import StandardizedOutput, _text


class ToolCapability(Enum):
    AST_EXTRACTION = "ast_extraction"
    SYMBOL_INDEXING = "symbol_indexing"
    TYPE_INFERENCE = "type_inference"
    CALL_GRAPH = "call_graph"
    DATA_FLOW = "data_flow"
    CONTROL_FLOW = "control_flow"
    DEPENDENCY_ANALYSIS = "dependency_analysis"
    SEMANTIC_ANALYSIS = "semantic_analysis"
    SECURITY_SCANNING = "security_scanning"
    QUALITY_METRICS = "quality_metrics"


@dataclass
class ToolMetadata:
    name: str
    version: str
    capabilities: set[ToolCapability]
    supported_languages: set[str]
    requires_binary: bool
    binary_name: str | None
    performance_class: str

    def __post_init__(self):
        _text(self.name, "tool name")
        _text(self.version, "tool version")
        if not isinstance(self.capabilities, set) or any(
            not isinstance(c, ToolCapability) for c in self.capabilities
        ):
            raise ValueError("Capabilities must be ToolCapability values")
        if (
            not isinstance(self.supported_languages, set)
            or not self.supported_languages
        ):
            raise ValueError("Supported languages must be a nonempty set")
        for language in self.supported_languages:
            _text(language, "language")
            if language != language.lower():
                raise ValueError("Language names must be lowercase")
        if type(self.requires_binary) is not bool:
            raise ValueError("requires_binary must be boolean")
        if self.binary_name is not None:
            _text(self.binary_name, "binary_name")
        if self.requires_binary and not self.binary_name:
            raise ValueError("Required binary must be named")
        if self.performance_class not in {"fast", "medium", "slow", "unmeasured"}:
            raise ValueError("Unsupported performance class")


class AnalysisTool(ABC):
    """Repository-scoped independent producer of portable analysis records."""

    def __init__(self, repo_path: Path):
        self.repo_path = Path(repo_path)
        self.metadata = self._get_metadata()
        if not isinstance(self.metadata, ToolMetadata):
            raise TypeError("Tools must declare ToolMetadata")

    @abstractmethod
    def _get_metadata(self) -> ToolMetadata:
        raise NotImplementedError

    @abstractmethod
    async def check_availability(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def analyze(
        self,
        repo_url: str,
        languages: set[str] | None = None,
        file_patterns: list[str] | None = None,
        skip_patterns: list[str] | None = None,
    ) -> StandardizedOutput:
        raise NotImplementedError

    @abstractmethod
    async def get_symbol_definitions(self) -> dict[str, dict]:
        raise NotImplementedError

    async def cleanup(self):
        """Release owned state; the default tool owns no external resources."""

    def supports_language(self, language: str) -> bool:
        return (
            "*" in self.metadata.supported_languages
            or language.lower() in self.metadata.supported_languages
        )

    def has_capability(self, capability: ToolCapability) -> bool:
        return capability in self.metadata.capabilities

    def get_performance_estimate(self, file_count: int) -> dict:
        """Preserve the interface without inventing per-file performance numbers."""
        if type(file_count) is not int or file_count < 0:
            raise ValueError("File count must be a nonnegative integer")
        return {
            "time_estimate_seconds": None,
            "memory_estimate_mb": None,
            "performance_class": self.metadata.performance_class,
            "basis": "unmeasured",
        }
