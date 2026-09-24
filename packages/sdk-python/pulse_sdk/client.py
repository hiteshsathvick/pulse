import json
import secrets
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from types import TracebackType
from typing import Any

PropertyValue = str | float | bool | None


def _traceparent() -> str:
    """A W3C `traceparent` value (version 00, sampled). Zero runtime
    dependencies is a property of this SDK, so it only originates the trace id
    -- /ingest continues it and the ingest worker joins the same trace via the
    Redis stream. The SDK's own span is never exported."""
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    return f"00-{trace_id}-{span_id}-01"


class PulseClient:
    """A thin, zero-runtime-dependency client for POST /ingest.

    Buffers in memory and flushes at a small size threshold or via an
    explicit flush()/close() -- deliberately no local persistence and no
    retry/backoff, unlike the browser SDK. A Python server process
    restarting is a different failure mode than a browser tab closing, and
    this is meant to be embedded in someone else's server, where pulling in
    a background thread or a third-party dependency isn't free.
    """

    def __init__(
        self,
        write_key: str,
        api_host: str,
        *,
        flush_at_size: int = 20,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._write_key = write_key
        self._api_host = api_host.rstrip("/")
        self._flush_at_size = flush_at_size
        self._timeout_seconds = timeout_seconds
        self._buffer: list[dict[str, Any]] = []

    def capture(
        self,
        event: str,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
        properties: dict[str, PropertyValue] | None = None,
    ) -> str:
        """Queues one event and returns its event_id. Auto-flushes once the
        buffer reaches flush_at_size; otherwise call flush() (or close(), or
        exit the `with` block) when done."""
        if not user_id and not anonymous_id:
            raise ValueError("at least one of user_id or anonymous_id is required")

        event_id = str(uuid.uuid4())
        self._buffer.append(
            {
                "event_id": event_id,
                "event": event,
                "user_id": user_id,
                "anonymous_id": anonymous_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "properties": properties,
            }
        )
        if len(self._buffer) >= self._flush_at_size:
            self.flush()
        return event_id

    def flush(self) -> bool:
        """Sends and clears everything currently buffered. Returns whether
        the request succeeded; on failure the buffer is left untouched so a
        caller can retry by calling flush() again -- no retry/backoff of its
        own, per "thin"."""
        if not self._buffer:
            return True

        payload = json.dumps({"batch": self._buffer}).encode()
        request = urllib.request.Request(
            f"{self._api_host}/ingest",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self._write_key,
                "traceparent": _traceparent(),
            },
        )
        try:
            urllib.request.urlopen(request, timeout=self._timeout_seconds)
        except urllib.error.URLError:
            return False

        self._buffer.clear()
        return True

    def close(self) -> None:
        self.flush()

    def __enter__(self) -> "PulseClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
