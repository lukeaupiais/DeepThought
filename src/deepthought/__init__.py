"""Local source analysis. Importing the SDK does not load analyzer dependencies."""

from .consistency import SourceConsistency, check_source_consistency
from .engine import analyze_repository
from .models import AnalysisResult
from .graph import GraphSnapshot, compose_graph
from ._version import __version__ as __version__
from .scip import ingest_scip
from .query import QueryRequest, QueryResult, query_graph

__all__ = [
    "AnalysisResult",
    "SourceConsistency",
    "check_source_consistency",
    "GraphSnapshot",
    "analyze_repository",
    "ingest_scip",
    "compose_graph",
    "QueryRequest",
    "QueryResult",
    "query_graph",
]
