"""Typed error hierarchy for the Khai SDK.

All errors raised by the SDK derive from :class:`KhaiError`, so a caller can
``except KhaiError`` to catch everything the client can raise.
"""

from __future__ import annotations

from typing import Any, Optional


class KhaiError(Exception):
    """Base class for every error the SDK raises."""


class KhaiConfigError(KhaiError):
    """Invalid client configuration (e.g. missing API key or base URL)."""


class KhaiAPIError(KhaiError):
    """The server returned a non-success HTTP status.

    ``status_code`` is the HTTP status; ``payload`` is the parsed error body
    when the server returned the sanitized ``{"error", "hint"}`` envelope.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        payload: Optional[Any] = None,
        hint: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.hint = hint


class KhaiAuthError(KhaiAPIError):
    """The API key was missing, malformed, or rejected (HTTP 401/403)."""


class KhaiRateLimitError(KhaiAPIError):
    """The per-key rate limit was exceeded (HTTP 429)."""


class KhaiConnectionError(KhaiError):
    """The request could not be delivered (network error / timeout)."""


class KhaiQueueFullError(KhaiError):
    """A fire-and-forget turn was dropped because the in-memory queue or retry
    buffer reached ``max_queue_size``. Surfaced through ``on_error`` so the
    host application can count drops; the SDK never blocks the caller or grows
    memory without bound during a prolonged outage."""
