"""HTTP transport for the Khai SDK.

Thin wrapper over ``httpx`` that adds the ``Authorization`` header, retries on
transient failures (429 + 5xx + network errors) with exponential backoff, and
maps error responses onto the SDK's typed error hierarchy.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

import httpx

from ._version import __version__
from .errors import (
    KhaiAPIError,
    KhaiAuthError,
    KhaiConnectionError,
    KhaiRateLimitError,
)

# Status codes worth retrying: rate-limit + transient server errors.
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _extract_error(payload: Any) -> "tuple[str, Optional[str]]":
    """Pull a human-readable message (and optional hint) out of an error body.

    The backend uses two envelopes: handler-level errors return
    ``{"error": ..., "hint": ...}``; dependency-level errors (auth, rate limit)
    come from FastAPI's ``HTTPException`` as ``{"detail": ...}`` where
    ``detail`` is a string, or a list of validation dicts for a 422.
    """
    default = "request failed"
    if not isinstance(payload, dict):
        return default, None
    hint = payload.get("hint")
    err = payload.get("error")
    if err:
        return str(err), hint
    detail = payload.get("detail")
    if isinstance(detail, str) and detail:
        return detail, hint
    if isinstance(detail, list) and detail:
        parts = []
        for item in detail:
            if isinstance(item, dict):
                loc = ".".join(str(x) for x in item.get("loc", []) if x != "body")
                msg = item.get("msg", "")
                parts.append(f"{loc}: {msg}" if loc else str(msg))
            else:
                parts.append(str(item))
        return "; ".join(p for p in parts if p) or default, hint
    return default, hint


class Transport:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        client: Optional[httpx.Client] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"khai-python-sdk/{__version__}",
        }

    def post_json(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST ``body`` as JSON to ``path``; retry transient failures.

        Raises a typed :class:`~khai.errors.KhaiError` subclass on failure.
        """
        url = f"{self._base_url}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.post(url, json=body, headers=self.headers)
            except httpx.HTTPError as exc:
                last_exc = KhaiConnectionError(f"request to {path} failed: {exc}")
                if attempt < self._max_retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise last_exc from exc

            if resp.status_code in _RETRY_STATUS and attempt < self._max_retries:
                self._sleep(self._backoff(attempt))
                continue

            return self._handle_response(resp)

        # Exhausted retries on network errors.
        assert last_exc is not None
        raise last_exc

    def _backoff(self, attempt: int) -> float:
        return self._backoff_factor * (2 ** attempt)

    def _handle_response(self, resp: httpx.Response) -> Dict[str, Any]:
        if 200 <= resp.status_code < 300:
            try:
                return resp.json()
            except ValueError:
                return {}

        payload: Any = None
        try:
            payload = resp.json()
        except ValueError:
            payload = None

        message, hint = _extract_error(payload)

        status = resp.status_code
        if status in (401, 403):
            raise KhaiAuthError(message, status_code=status, payload=payload, hint=hint)
        if status == 429:
            raise KhaiRateLimitError(message, status_code=status, payload=payload, hint=hint)
        raise KhaiAPIError(message, status_code=status, payload=payload, hint=hint)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
