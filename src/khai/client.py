"""The ergonomic Khai client.

``KhaiClient`` is the hand-written layer on top of the generated contract. It
gives callers:

* ``ingest_turn`` / ``ingest_batch`` — simple synchronous calls that return
  typed responses and raise typed errors.
* ``ingest_turn_async`` — fire-and-forget: the turn is queued and sent from a
  background worker so it never blocks the caller's chatbot loop. Turns that
  fail to deliver (network errors) are buffered and retried on the next flush.
* ``set_redactor`` — a hook to strip PII from every payload before it leaves
  the process.

Every turn is made idempotent at creation (see :class:`~khai.models.IngestTurn`):
``response_id`` and ``timestamp`` are fixed before the first send, so retries by
the transport or from the buffer are deduplicated server-side.

The fire-and-forget queue and the retry buffer are bounded by ``max_queue_size``.
When either is full the newest turn is dropped and reported through
``on_error`` as :class:`~khai.errors.KhaiQueueFullError`; memory in the host
process never grows without limit during an outage.

The client is a context manager; exiting it flushes pending turns and closes
the underlying HTTP connection. Live clients are also flushed at interpreter
exit, so turns queued just before a process ends are not silently lost.
"""

from __future__ import annotations

import atexit
import os
import queue
import threading
import weakref
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

from ._transport import Transport
from .errors import KhaiConfigError, KhaiConnectionError, KhaiError, KhaiQueueFullError
from .models import BatchResponse, IngestResponse, IngestTurn

_INGEST_PATH = "/api/v1/ingest"
_BATCH_PATH = "/api/v1/ingest/batch"
_BATCH_MAX = 100

DEFAULT_BASE_URL = "https://api.getkhai.ai"
API_KEY_ENV = "KHAI_API_KEY"
BASE_URL_ENV = "KHAI_BASE_URL"
DEFAULT_MAX_QUEUE_SIZE = 10_000

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Clients still open at interpreter exit get one best-effort flush.
_LIVE_CLIENTS: "weakref.WeakSet[KhaiClient]" = weakref.WeakSet()


def _flush_live_clients_at_exit() -> None:
    for client in list(_LIVE_CLIENTS):
        try:
            client.close()
        except Exception:
            pass


atexit.register(_flush_live_clients_at_exit)


def _validate_base_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme == "https" and parts.hostname:
        return base_url
    if parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS:
        return base_url  # local development only
    raise KhaiConfigError(
        "base_url must be an https:// URL (plain http is allowed only for localhost); "
        f"got {base_url!r}"
    )

Redactor = Callable[[Dict[str, Any]], Dict[str, Any]]
ErrorHandler = Callable[[Exception, IngestTurn], None]


class KhaiClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE,
        on_error: Optional[ErrorHandler] = None,
        _client: Optional[httpx.Client] = None,
        _transport: Optional[Transport] = None,
    ) -> None:
        """Create a client.

        ``api_key`` falls back to the ``KHAI_API_KEY`` environment variable and
        ``base_url`` to ``KHAI_BASE_URL`` (default ``https://api.getkhai.ai``),
        so production code never has to embed the key in source.
        """
        api_key = api_key or os.environ.get(API_KEY_ENV) or ""
        if not api_key:
            raise KhaiConfigError(
                f"api_key is required: pass api_key=... or set the {API_KEY_ENV} environment variable"
            )
        base_url = base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
        base_url = _validate_base_url(base_url)
        if max_queue_size < 1:
            raise KhaiConfigError("max_queue_size must be >= 1")

        self._transport = _transport or Transport(
            base_url,
            api_key,
            timeout=timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            client=_client,
        )
        self._redactor: Optional[Redactor] = None
        self._on_error = on_error
        self._max_queue_size = max_queue_size

        # Fire-and-forget machinery (lazily started on first async call).
        # Both containers are bounded by max_queue_size; see ingest_turn_async
        # and _buffer_turn for the drop policy.
        self._queue: "queue.Queue[IngestTurn]" = queue.Queue(maxsize=max_queue_size)
        self._buffer: Deque[IngestTurn] = deque(maxlen=max_queue_size)
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._closed = False
        _LIVE_CLIENTS.add(self)

    # -- configuration -------------------------------------------------------

    def set_redactor(self, fn: Optional[Redactor]) -> None:
        """Install a function applied to every payload dict before it is sent.

        The function receives the JSON-ready payload and must return a payload
        dict. Use it to remove or mask PII client-side.
        """
        self._redactor = fn

    # -- synchronous API -----------------------------------------------------

    def ingest_turn(
        self,
        user_query: str,
        agent_response: str,
        session_id: str,
        *,
        agent_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        response_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> IngestResponse:
        """Send one turn and wait for the evaluation result."""
        turn = IngestTurn(
            user_query=user_query,
            agent_response=agent_response,
            session_id=session_id,
            agent_id=agent_id,
            timestamp=timestamp,
            response_id=response_id,
            metadata=metadata,
        )
        return self._send_turn(turn)

    def ingest_batch(self, turns: List[IngestTurn]) -> BatchResponse:
        """Send up to 100 turns in a single request."""
        if len(turns) > _BATCH_MAX:
            raise KhaiConfigError(f"batch may contain at most {_BATCH_MAX} turns")
        body = {"conversations": [self._payload(t) for t in turns]}
        data = self._transport.post_json(_BATCH_PATH, body)
        return BatchResponse.from_dict(data)

    # -- fire-and-forget API -------------------------------------------------

    def ingest_turn_async(
        self,
        user_query: str,
        agent_response: str,
        session_id: str,
        *,
        agent_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        response_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Queue a turn for background delivery and return immediately.

        Never blocks on the network and never raises a delivery error to the
        caller; failures are surfaced via the ``on_error`` callback (if set)
        or retried from the buffer on the next :meth:`flush`. If the queue is
        already holding ``max_queue_size`` turns the new turn is dropped and
        reported as :class:`~khai.errors.KhaiQueueFullError`.
        """
        turn = IngestTurn(
            user_query=user_query,
            agent_response=agent_response,
            session_id=session_id,
            agent_id=agent_id,
            timestamp=timestamp,
            response_id=response_id,
            metadata=metadata,
        )
        self._ensure_worker()
        try:
            self._queue.put_nowait(turn)
        except queue.Full:
            self._report(
                KhaiQueueFullError(
                    f"ingest queue full ({self._max_queue_size} turns); dropping turn"
                ),
                turn,
            )

    def flush(self) -> None:
        """Block until all queued turns are delivered (or buffered), then retry
        any buffered failures once."""
        if self._worker is not None:
            self._queue.join()
        self._retry_buffer()

    def close(self) -> None:
        """Flush, stop the worker, and close the HTTP connection. Idempotent."""
        if self._closed:
            return
        self._closed = True
        _LIVE_CLIENTS.discard(self)
        if self._worker is not None:
            self._queue.join()
            self._stop.set()
            self._worker.join(timeout=5.0)
            self._worker = None
        self._retry_buffer()
        self._transport.close()

    def __enter__(self) -> "KhaiClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- internals -----------------------------------------------------------

    def _payload(self, turn: IngestTurn) -> Dict[str, Any]:
        payload = turn.to_payload()
        if self._redactor is not None:
            payload = self._redactor(payload)
        return payload

    def _send_turn(self, turn: IngestTurn) -> IngestResponse:
        data = self._transport.post_json(_INGEST_PATH, self._payload(turn))
        return IngestResponse.from_dict(data)

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="khai-ingest", daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while True:
            try:
                turn = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                self._send_turn(turn)
            except KhaiConnectionError:
                self._buffer_turn(turn)
            except KhaiError as exc:
                self._report(exc, turn)
            finally:
                self._queue.task_done()

    def _buffer_turn(self, turn: IngestTurn) -> None:
        """Park a turn that failed on a network error for the next flush.

        The buffer is a bounded deque: appending to a full buffer evicts the
        oldest turn, which is reported as dropped so the host can count it.
        """
        with self._lock:
            evicted = self._buffer[0] if len(self._buffer) == self._buffer.maxlen else None
            self._buffer.append(turn)
        if evicted is not None:
            self._report(
                KhaiQueueFullError(
                    f"retry buffer full ({self._max_queue_size} turns); dropped oldest turn"
                ),
                evicted,
            )

    def _retry_buffer(self) -> None:
        with self._lock:
            pending = list(self._buffer)
            self._buffer.clear()
        for turn in pending:
            try:
                self._send_turn(turn)
            except KhaiConnectionError:
                self._buffer_turn(turn)
            except KhaiError as exc:
                self._report(exc, turn)

    def _report(self, exc: Exception, turn: IngestTurn) -> None:
        if self._on_error is not None:
            try:
                self._on_error(exc, turn)
            except Exception:
                pass
