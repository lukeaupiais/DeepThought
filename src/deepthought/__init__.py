"""Local source analysis. Importing the SDK does not load analyzer dependencies."""

from .engine import analyze_repository
from .models import AnalysisResult
from .graph import GraphSnapshot, compose_graph
from ._version import __version__ as __version__
from .scip import ingest_scip

__all__ = [
    "AnalysisResult",
    "GraphSnapshot",
    "analyze_repository",
    "ingest_scip",
    "compose_graph",
]
