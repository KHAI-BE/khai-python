"""Request/response models mirroring the Khai ingest contract.

These are intentionally plain dataclasses (no pydantic dependency) so the SDK
stays lightweight. They mirror ``src/api/schemas/ingest.py`` in the backend,
which is the canonical OpenAPI contract the SDK is generated against.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utc_now_iso() -> str:
    """Current UTC time in ISO-8601 with an explicit offset (e.g. ``2026-09-18T10:30:00.123456+00:00``)."""
    return datetime.now(timezone.utc).isoformat()


def _drop_none(d: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``d`` without keys whose value is ``None`` (recursively for dicts)."""
    out: Dict[str, Any] = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, dict):
            v = _drop_none(v)
        out[k] = v
    return out


@dataclass
class IngestTurn:
    """A single chatbot turn to send to Khai for evaluation.

    ``user_query``, ``agent_response`` and ``session_id`` are required; the rest
    are optional. Extra platform-specific fields go in ``metadata``.

    Idempotency: ``response_id`` and ``timestamp`` are filled in **at creation**
    when the caller does not supply them. The backend deduplicates on
    ``response_id`` (falling back to ``session_id`` + ``timestamp``), so fixing
    both values before the first send guarantees that a retry -- whether by the
    transport's backoff loop or from the fire-and-forget buffer after an outage --
    is recognised as the same turn instead of being stored twice. Pass your
    platform's own reply id (Dialogflow ``responseId``, Lex, Copilot Studio) as
    ``response_id`` when you have one; otherwise a UUID4 is generated.
    """

    user_query: str
    agent_response: str
    session_id: str
    agent_id: Optional[str] = None
    timestamp: Optional[str] = None
    response_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if not self.response_id:
            self.response_id = str(uuid.uuid4())
        if not self.timestamp:
            self.timestamp = _utc_now_iso()

    def to_payload(self) -> Dict[str, Any]:
        """Serialize to the JSON body the ingest endpoint expects."""
        meta: Dict[str, Any] = dict(self.metadata or {})
        if self.response_id is not None:
            meta.setdefault("response_id", self.response_id)
        if self.agent_id is not None:
            meta.setdefault("agent_id", self.agent_id)
        body: Dict[str, Any] = {
            "user_query": self.user_query,
            "agent_response": self.agent_response,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "timestamp": self.timestamp,
            "metadata": meta or None,
        }
        return _drop_none(body)


@dataclass
class IngestResult:
    """The evaluation outcome for an accepted turn."""

    trust_score: float = 0.0
    issues: List[Any] = field(default_factory=list)
    passed: bool = True
    guardrails_fired: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IngestResult":
        return cls(
            trust_score=d.get("trust_score", 0.0),
            issues=d.get("issues", []) or [],
            # server serializes the field under its alias "pass"
            passed=d.get("pass", d.get("passed", True)),
            guardrails_fired=d.get("guardrails_fired", []) or [],
        )


@dataclass
class IngestResponse:
    """Response for a single ingest call.

    ``status`` is ``evaluated`` (with ``result``), ``skipped`` (with ``reason``),
    or ``duplicate`` (with ``message_id``).
    """

    status: str
    result: Optional[IngestResult] = None
    reason: Optional[str] = None
    agent_id: Optional[str] = None
    message: Optional[str] = None
    message_id: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None

    @property
    def evaluated(self) -> bool:
        return self.status == "evaluated"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"

    @property
    def duplicate(self) -> bool:
        return self.status == "duplicate"

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IngestResponse":
        result = d.get("result")
        return cls(
            status=d.get("status", "unknown"),
            result=IngestResult.from_dict(result) if isinstance(result, dict) else None,
            reason=d.get("reason"),
            agent_id=d.get("agent_id"),
            message=d.get("message"),
            message_id=d.get("message_id"),
            raw=d,
        )


@dataclass
class BatchSummary:
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped_duplicates: int = 0

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BatchSummary":
        return cls(
            total=d.get("total", 0),
            passed=d.get("passed", 0),
            failed=d.get("failed", 0),
            skipped_duplicates=d.get("skipped_duplicates", 0),
        )


@dataclass
class BatchResponse:
    status: str
    results: List[Dict[str, Any]] = field(default_factory=list)
    summary: BatchSummary = field(default_factory=BatchSummary)
    raw: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BatchResponse":
        return cls(
            status=d.get("status", "unknown"),
            results=d.get("results", []) or [],
            summary=BatchSummary.from_dict(d.get("summary", {}) or {}),
            raw=d,
        )
