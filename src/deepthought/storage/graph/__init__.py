"""Optional Neo4j persistence ported from DeepThought's original graph layer."""

from .connection import Neo4jConnectionManager
from .schema import setup_schema
from .write_coordinator import WriteCoordinator, WriteCoordinatorConfig, WriteResult
from .writer import BatchConfig, GraphWriter, WriteStats

__all__ = [
    "BatchConfig",
    "GraphWriter",
    "Neo4jConnectionManager",
    "WriteCoordinator",
    "WriteCoordinatorConfig",
    "WriteResult",
    "WriteStats",
    "setup_schema",
]
