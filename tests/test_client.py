"""Unit tests for the Khai SDK ergonomic client and transport.

Transport-level retry/backoff tests drive a real ``Transport`` over an
``httpx.MockTransport`` with an injected fake ``sleep`` (no wall-clock waits).
Client-level tests inject a ``FakeTransport`` to exercise batching, redaction,
fire-and-forget queuing and buffer-on-failure without any network.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List

import httpx
import pytest

from khai import (
    IngestTurn,
    KhaiAPIError,
    KhaiAuthError,
    KhaiClient,
    KhaiConfigError,
    KhaiConnectionError,
    KhaiRateLimitError,
)
from khai._transport import Transport


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeTransport:
    """In-memory stand-in for ``Transport`` that records posts."""

    def __init__(self) -> None:
        self.posts: List[Dict[str, Any]] = []
        self.fail_connection = False
        self.response: Dict[str, Any] = {"status": "evaluated", "result": {"trust_score": 0.9, "pass": True}}
        self._lock = threading.Lock()
        self.closed = False

    def post_json(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if self.fail_connection:
            raise KhaiConnectionError("simulated network failure")
        with self._lock:
            self.posts.append({"path": path, "body": body})
        return self.response

    def close(self) -> None:
        self.closed = True


def make_client(**kwargs: Any) -> tuple[KhaiClient, FakeTransport]:
    ft = FakeTransport()
    return KhaiClient(api_key="k", _transport=ft, **kwargs), ft


def _mock_transport(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_requires_api_key() -> None:
    with pytest.raises(KhaiConfigError):
        KhaiClient(api_key="")


def test_batch_size_limit() -> None:
    client, _ = make_client()
    turns = [IngestTurn("q", "a", "s") for _ in range(101)]
    with pytest.raises(KhaiConfigError):
        client.ingest_batch(turns)


# --------------------------------------------------------------------------- #
# Payload / contract shape
# --------------------------------------------------------------------------- #
def test_ingest_turn_builds_documented_payload() -> None:
    client, ft = make_client()
    resp = client.ingest_turn(
        "How do I pay?", "Pay online.", "chat-1", agent_id="agent-9", response_id="r1"
    )
    assert resp.evaluated
    assert resp.result.trust_score == 0.9
    body = ft.posts[0]["body"]
    assert body["user_query"] == "How do I pay?"
    assert body["agent_response"] == "Pay online."
    assert body["session_id"] == "chat-1"
    assert body["agent_id"] == "agent-9"
    # response_id folds into metadata
    assert body["metadata"]["response_id"] == "r1"
    # timestamp is always present (auto-stamped when the caller omits it) and
    # carries an explicit UTC offset so the backend never guesses the zone.
    assert body["timestamp"].endswith("+00:00")
    # metadata is None -> dropped unless something folded into it
    assert "language" not in body["metadata"]


def test_batch_wraps_conversations() -> None:
    client, ft = make_client()
    ft.response = {"status": "evaluated", "results": [], "summary": {"total": 2}}
    out = client.ingest_batch(
        [IngestTurn("q1", "a1", "s", agent_id="a"), IngestTurn("q2", "a2", "s", agent_id="a")]
    )
    assert out.summary.total == 2
    assert ft.posts[0]["path"] == "/api/v1/ingest/batch"
    assert len(ft.posts[0]["body"]["conversations"]) == 2


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def test_redactor_applied_before_send() -> None:
    client, ft = make_client()

    def redact(payload: Dict[str, Any]) -> Dict[str, Any]:
        payload["user_query"] = "[REDACTED]"
        return payload

    client.set_redactor(redact)
    client.ingest_turn("secret@example.com wants a refund", "ok", "s", agent_id="a")
    assert ft.posts[0]["body"]["user_query"] == "[REDACTED]"


# --------------------------------------------------------------------------- #
# Fire-and-forget + buffer-on-failure
# --------------------------------------------------------------------------- #
def test_async_is_non_blocking_and_flushes() -> None:
    client, ft = make_client()
    for i in range(5):
        client.ingest_turn_async("q", "a", "s", agent_id="a", response_id=str(i))
    client.close()
    assert len(ft.posts) == 5
    assert ft.closed is True


def test_buffer_on_failure_then_retry_delivers() -> None:
    client, ft = make_client()
    ft.fail_connection = True
    client.ingest_turn_async("q", "a", "s", agent_id="a")
    client.flush()  # send fails -> buffered
    assert ft.posts == []
    # Network recovers; a subsequent flush drains the buffer.
    ft.fail_connection = False
    client.flush()
    assert len(ft.posts) == 1
    client.close()


def test_async_errors_surface_via_callback() -> None:
    seen: List[Exception] = []
    client, ft = make_client(on_error=lambda exc, turn: seen.append(exc))

    def boom(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        raise KhaiAuthError("bad key", status_code=401)

    ft.post_json = boom  # type: ignore[assignment]
    client.ingest_turn_async("q", "a", "s", agent_id="a")
    client.close()
    assert len(seen) == 1
    assert isinstance(seen[0], KhaiAuthError)


# --------------------------------------------------------------------------- #
# Transport: retry / backoff / error mapping
# --------------------------------------------------------------------------- #
def test_retries_on_5xx_then_succeeds() -> None:
    calls = {"n": 0}
    sleeps: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"status": "evaluated"})

    t = Transport(
        "https://api.test", "k", client=_mock_transport(handler), sleep=sleeps.append, backoff_factor=0.1
    )
    out = t.post_json("/api/v1/ingest", {"user_query": "q"})
    assert out["status"] == "evaluated"
    assert calls["n"] == 3
    # backoff grows exponentially: 0.1, 0.2
    assert sleeps == [0.1, 0.2]


def test_rate_limit_exhausts_retries_and_raises() -> None:
    sleeps: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down", "hint": "retry later"})

    t = Transport(
        "https://api.test", "k", client=_mock_transport(handler), sleep=sleeps.append, max_retries=2
    )
    with pytest.raises(KhaiRateLimitError) as ei:
        t.post_json("/api/v1/ingest", {"user_query": "q"})
    assert ei.value.status_code == 429
    assert ei.value.hint == "retry later"
    assert len(sleeps) == 2  # retried max_retries times


def test_auth_error_is_not_retried() -> None:
    sleeps: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    t = Transport("https://api.test", "k", client=_mock_transport(handler), sleep=sleeps.append)
    with pytest.raises(KhaiAuthError):
        t.post_json("/api/v1/ingest", {"user_query": "q"})
    assert sleeps == []  # no backoff on a non-retryable status


def test_network_error_retries_then_raises_connection_error() -> None:
    sleeps: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    t = Transport(
        "https://api.test", "k", client=_mock_transport(handler), sleep=sleeps.append, max_retries=2
    )
    with pytest.raises(KhaiConnectionError):
        t.post_json("/api/v1/ingest", {"user_query": "q"})
    assert len(sleeps) == 2


def test_auth_header_is_attached() -> None:
    seen: Dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"status": "evaluated"})

    t = Transport("https://api.test", "secret-key", client=_mock_transport(handler))
    t.post_json("/api/v1/ingest", {"user_query": "q"})
    assert seen["auth"] == "Bearer secret-key"


# --------------------------------------------------------------------------- #
# Idempotency: response_id / timestamp fixed at creation, stable across retries
# --------------------------------------------------------------------------- #
def test_turn_auto_generates_response_id_and_timestamp() -> None:
    t = IngestTurn("q", "a", "s")
    assert t.response_id and len(t.response_id) == 36  # uuid4
    assert t.timestamp and t.timestamp.endswith("+00:00")
    assert IngestTurn("q", "a", "s").response_id != t.response_id


def test_turn_keeps_caller_supplied_ids() -> None:
    t = IngestTurn("q", "a", "s", response_id="platform-77", timestamp="2026-09-18T10:00:00Z")
    assert t.response_id == "platform-77"
    assert t.timestamp == "2026-09-18T10:00:00Z"
    body = t.to_payload()
    assert body["metadata"]["response_id"] == "platform-77"
    assert body["timestamp"] == "2026-09-18T10:00:00Z"


def test_transport_retry_resends_identical_payload() -> None:
    """A 503 then 200: both attempts must carry the same response_id and timestamp."""
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"status": "evaluated"})

    client = KhaiClient(api_key="k", base_url="https://api.test", _client=_mock_transport(handler))
    # Inject a no-wait sleep so the test does not block on backoff.
    client._transport._sleep = lambda _s: None  # type: ignore[attr-defined]
    client.ingest_turn("q", "a", "s", agent_id="a")
    assert len(bodies) == 2
    assert bodies[0]["metadata"]["response_id"] == bodies[1]["metadata"]["response_id"]
    assert bodies[0]["timestamp"] == bodies[1]["timestamp"]


def test_buffered_turn_keeps_original_identity_after_outage() -> None:
    client, ft = make_client()
    ft.fail_connection = True
    client.ingest_turn_async("q", "a", "s", agent_id="a")
    client.flush()  # buffered
    ft.fail_connection = False
    client.flush()  # delivered
    client.close()
    body = ft.posts[0]["body"]
    assert len(body["metadata"]["response_id"]) == 36
    assert body["timestamp"].endswith("+00:00")


# --------------------------------------------------------------------------- #
# Config: env fallback, https enforcement
# --------------------------------------------------------------------------- #
def test_api_key_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KHAI_API_KEY", "from-env")
    monkeypatch.setenv("KHAI_BASE_URL", "https://uat.example")
    ft = FakeTransport()
    client = KhaiClient(_transport=ft)
    client.ingest_turn("q", "a", "s")
    assert ft.posts  # constructed and usable without explicit args


def test_missing_api_key_names_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KHAI_API_KEY", raising=False)
    with pytest.raises(KhaiConfigError) as ei:
        KhaiClient()
    assert "KHAI_API_KEY" in str(ei.value)


@pytest.mark.parametrize("url", ["http://api.getkhai.ai", "ftp://x", "api.getkhai.ai", "http://"])
def test_non_https_base_url_rejected(url: str) -> None:
    with pytest.raises(KhaiConfigError):
        KhaiClient(api_key="k", base_url=url, _transport=FakeTransport())


@pytest.mark.parametrize("url", ["http://localhost:8080", "http://127.0.0.1:8080", "https://api.getkhai.ai"])
def test_localhost_http_and_https_allowed(url: str) -> None:
    KhaiClient(api_key="k", base_url=url, _transport=FakeTransport()).close()


# --------------------------------------------------------------------------- #
# Bounded queue / buffer
# --------------------------------------------------------------------------- #
def test_queue_full_drops_and_reports() -> None:
    from khai import KhaiQueueFullError

    seen: List[Exception] = []
    ft = FakeTransport()
    gate = threading.Event()

    def slow_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        gate.wait(timeout=5)  # hold the worker so the queue fills
        return ft.response

    ft.post_json = slow_post  # type: ignore[assignment]
    client = KhaiClient(api_key="k", _transport=ft, max_queue_size=2, on_error=lambda e, t: seen.append(e))
    for _ in range(6):
        client.ingest_turn_async("q", "a", "s", agent_id="a")
    gate.set()
    client.close()
    # 1 in flight + 2 queued survive; the rest were dropped and reported.
    assert seen and all(isinstance(e, KhaiQueueFullError) for e in seen)
    assert len(seen) >= 3


def test_retry_buffer_is_bounded_and_evicts_oldest() -> None:
    from khai import KhaiQueueFullError

    seen: List[tuple[Exception, IngestTurn]] = []
    client, ft = make_client(max_queue_size=2, on_error=lambda e, t: seen.append((e, t)))
    ft.fail_connection = True
    for i in range(3):
        client.ingest_turn_async("q", "a", "s", agent_id="a", response_id=f"r{i}")
        client.flush()  # each one fails and is buffered
    drops = [t.response_id for e, t in seen if isinstance(e, KhaiQueueFullError)]
    assert drops == ["r0"]  # oldest evicted once the third arrives
    ft.fail_connection = False
    client.close()
    assert sorted(p["body"]["metadata"]["response_id"] for p in ft.posts) == ["r1", "r2"]


def test_close_is_idempotent() -> None:
    client, ft = make_client()
    client.close()
    client.close()
    assert ft.closed is True


# --------------------------------------------------------------------------- #
# Version / User-Agent
# --------------------------------------------------------------------------- #
def test_user_agent_carries_package_version() -> None:
    from khai import __version__

    seen: Dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, json={"status": "evaluated"})

    Transport("https://api.test", "k", client=_mock_transport(handler)).post_json("/api/v1/ingest", {})
    assert seen["ua"] == f"khai-python-sdk/{__version__}"


# --------------------------------------------------------------------------- #
# Error envelopes: {"error"} from handlers, {"detail"} from FastAPI dependencies
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status,body,exc,expected",
    [
        (401, {"detail": "Invalid or inactive API key."}, KhaiAuthError, "Invalid or inactive API key."),
        (429, {"detail": "Rate limit exceeded. Reduce request rate and retry shortly."}, KhaiRateLimitError, "Rate limit exceeded"),
        (400, {"error": "Missing required fields", "hint": "send session_id"}, KhaiAPIError, "Missing required fields"),
        (422, {"detail": [{"loc": ["body", "conversations"], "msg": "Input should be a valid list"}]}, KhaiAPIError, "conversations: Input should be a valid list"),
        (500, "not json", KhaiAPIError, "request failed"),
    ],
)
def test_error_message_extracted_from_both_envelopes(status, body, exc, expected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    t = Transport("https://api.test", "k", client=_mock_transport(handler), sleep=lambda _s: None, max_retries=0)
    with pytest.raises(exc) as ei:
        t.post_json("/api/v1/ingest", {})
    assert expected in str(ei.value)
    if isinstance(body, dict) and "hint" in body:
        assert ei.value.hint == body["hint"]
