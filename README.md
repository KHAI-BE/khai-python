# Khai Python SDK

Stream chatbot turns to Khai for evaluation from any cloud — no access granted to Khai.

## Install

```bash
pip install khai
```

## Quickstart

Set your key in the environment rather than in source:

```bash
export KHAI_API_KEY="khai_..."
```

```python
from khai import KhaiClient

khai = KhaiClient()  # reads KHAI_API_KEY; base_url defaults to https://api.getkhai.ai

resp = khai.ingest_turn(
    user_query="How do I pay my bill?",
    agent_response="You can pay online at ...",
    session_id="chat-123",
    agent_id="YOUR_AGENT_ID",
)
print(resp.status, resp.result.trust_score if resp.evaluated else resp.reason)
```

## Every turn is idempotent

You do not have to think about retries. When you omit them, the SDK fills in a
`timestamp` (UTC, ISO-8601) and a UUID `response_id` **when the turn is created**,
and keeps them for every retry. The backend deduplicates on `response_id`, so a
turn resent after a timeout, a 5xx, or a network outage is answered with
`status: duplicate` instead of being stored twice.

If your platform already has a per-reply id (Dialogflow `responseId`, Lex,
Copilot Studio), pass it as `response_id` and the same guarantee holds even
across restarts of your own process.

## Fire-and-forget

`ingest_turn_async` queues the turn and returns immediately, so it never blocks
your chatbot's response path. Failed deliveries are buffered and retried. Both
the queue and the buffer are capped at `max_queue_size` (default 10,000 turns);
when full, the SDK drops a turn and reports it through `on_error` as
`KhaiQueueFullError` rather than growing memory during an outage. Open clients
are flushed once at interpreter exit.

```python
with KhaiClient() as khai:
    khai.ingest_turn_async(
        user_query=user_msg,
        agent_response=bot_reply,
        session_id=session_id,
        agent_id="YOUR_AGENT_ID",
    )
    # ... keep serving; turns flush in the background, and on close().
```

## Batch

```python
from khai import IngestTurn

khai.ingest_batch([
    IngestTurn(user_query="...", agent_response="...", session_id="s1", agent_id="a1"),
    IngestTurn(user_query="...", agent_response="...", session_id="s1", agent_id="a1"),
])
```

## Redact PII before sending

```python
def redact(payload: dict) -> dict:
    payload["user_query"] = mask_emails(payload["user_query"])
    return payload

khai.set_redactor(redact)
```

## Errors

All errors derive from `KhaiError`:

| Error | Meaning |
|---|---|
| `KhaiAuthError` | API key missing/invalid (401/403) |
| `KhaiRateLimitError` | Per-key rate limit exceeded (429) |
| `KhaiAPIError` | Other non-success status |
| `KhaiConnectionError` | Network/timeout (already retried) |
| `KhaiQueueFullError` | Fire-and-forget queue/buffer full; a turn was dropped (via `on_error`) |
| `KhaiConfigError` | Bad client configuration (missing key, non-HTTPS base URL) |

## Configuration

| Setting | Argument | Environment variable | Default |
|---|---|---|---|
| API key | `api_key` | `KHAI_API_KEY` | required |
| Base URL | `base_url` | `KHAI_BASE_URL` | `https://api.getkhai.ai` |
| Request timeout (s) | `timeout` | — | `10.0` |
| Retries on 429/5xx/network | `max_retries` | — | `3` |
| Queue / buffer cap | `max_queue_size` | — | `10000` |

`base_url` must be `https://`; plain `http://` is accepted only for `localhost`.
