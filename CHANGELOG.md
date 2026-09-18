# Changelog

All notable changes to the Khai Python SDK. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow SemVer.

## [Unreleased]

### Added
- Release pipeline (`.github/workflows/release.yml`): a `v*` tag builds once,
  publishes to TestPyPI, smoke-installs from there, then publishes to PyPI via
  trusted publishing (OIDC, no stored tokens) behind a required reviewer, and
  creates a GitHub Release with the artifacts.
- `IngestTurn` now fills `response_id` (UUID4) and `timestamp` (UTC ISO-8601) at
  creation when the caller omits them, so retries are deduplicated server-side
  instead of stored twice.
- `KHAI_API_KEY` and `KHAI_BASE_URL` environment fallbacks; `api_key` and
  `base_url` are optional constructor arguments.
- `max_queue_size` (default 10,000) bounds the fire-and-forget queue and the
  retry buffer. Drops are reported via `on_error` as the new `KhaiQueueFullError`.
- Open clients are flushed once at interpreter exit.
- `khai.__version__` is the single source of the version (hatch dynamic
  version from `src/khai/_version.py`) and is stamped into the `User-Agent`.

### Changed
- Distribution name is `khai-sdk` (install with `pip install khai-sdk`, then
  `import khai`). PyPI rejects `khai` as confusable with the existing `khal`
  project, so the shorter name is not available.
- `base_url` must be `https://`; `http://` is accepted only for localhost.
- `close()` is idempotent.

### Fixed
- Error messages from the backend's auth and rate-limit layer were lost: those
  responses use FastAPI's `{"detail": ...}` envelope, and the SDK only read
  `{"error": ...}`. A rejected key surfaced as "request failed"; it now
  surfaces as "Invalid or inactive API key." 422 validation lists are
  flattened into one message.

## [0.1.0] - 2026-08-04

Initial hand-written SDK: `ingest_turn`, `ingest_batch`, `ingest_turn_async`,
`set_redactor`, typed errors, retries with exponential backoff.
