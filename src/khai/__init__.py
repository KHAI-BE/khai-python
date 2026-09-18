"""Khai Python SDK — stream chatbot turns to Khai for evaluation."""

from ._version import __version__
from .client import KhaiClient
from .errors import (
    KhaiAPIError,
    KhaiAuthError,
    KhaiConfigError,
    KhaiConnectionError,
    KhaiError,
    KhaiQueueFullError,
    KhaiRateLimitError,
)
from .models import (
    BatchResponse,
    BatchSummary,
    IngestResponse,
    IngestResult,
    IngestTurn,
)

__all__ = [
    "KhaiClient",
    "IngestTurn",
    "IngestResponse",
    "IngestResult",
    "BatchResponse",
    "BatchSummary",
    "KhaiError",
    "KhaiConfigError",
    "KhaiAPIError",
    "KhaiAuthError",
    "KhaiRateLimitError",
    "KhaiConnectionError",
    "KhaiQueueFullError",
    "__version__",
]
