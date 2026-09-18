"""Contract tests: the SDK's request payloads conform to the committed OpenAPI
schema (``khai-python/openapi/khai.json``), which the backend generates from its
Pydantic models. This is what keeps the hand-written SDK in lockstep with the
server contract.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from khai import IngestTurn

_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "openapi", "khai.json")


def _spec() -> Dict[str, Any]:
    with open(_SPEC_PATH) as fh:
        return json.load(fh)


def _schema(spec: Dict[str, Any], name: str) -> Dict[str, Any]:
    return spec["components"]["schemas"][name]


def test_spec_documents_ingest_endpoints_with_security() -> None:
    spec = _spec()
    for path in ("/api/v1/ingest", "/api/v1/ingest/batch"):
        assert path in spec["paths"], f"{path} missing from OpenAPI spec"
        post = spec["paths"][path]["post"]
        assert post.get("security"), f"{path} has no security scheme"
    assert "ClientApiKey" in spec["components"].get("securitySchemes", {})


def test_turn_payload_keys_are_documented() -> None:
    spec = _spec()
    props = _schema(spec, "IngestTurnRequest")["properties"]
    payload = IngestTurn(
        user_query="q",
        agent_response="a",
        session_id="s",
        agent_id="agent-1",
        timestamp="2026-08-04T00:00:00Z",
        metadata={"language": "en"},
    ).to_payload()
    for key in payload:
        assert key in props, f"SDK sent undocumented top-level field: {key}"


def test_required_content_fields_present_in_schema() -> None:
    spec = _spec()
    props = _schema(spec, "IngestTurnRequest")["properties"]
    for required in ("user_query", "agent_response", "session_id"):
        assert required in props


def test_metadata_fields_match_schema() -> None:
    spec = _spec()
    meta_props = _schema(spec, "IngestMetadata")["properties"]
    # The response_id the SDK folds into metadata must be a documented field.
    assert "response_id" in meta_props
    payload = IngestTurn("q", "a", "s", agent_id="x", response_id="r1").to_payload()
    assert payload["metadata"]["response_id"] == "r1"
