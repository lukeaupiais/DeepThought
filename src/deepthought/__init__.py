"""Local source analysis. Importing the SDK does not load analyzer dependencies."""

from .engine import analyze_repository
from .models import AnalysisResult
from .scip import ingest_scip

__all__ = ["AnalysisResult", "analyze_repository", "ingest_scip"]
__version__ = "0.1.0a3"
