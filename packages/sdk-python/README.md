# pulse-sdk

A thin, zero-runtime-dependency Python client for `POST /ingest`, meant to be embedded in someone
else's server. Buffers in memory and flushes at a size threshold or on `flush()`/`close()` -- no
local persistence, no retry/backoff. That's a deliberate difference from `@pulse/sdk-js`: a Python
server process restarting is a different failure mode than a browser tab closing, and this is meant
to drop into an arbitrary server without pulling in a background thread or a third-party dependency.

## Install

```bash
cd packages/sdk-python
pip install -e .[dev]
pytest
```

## Usage

```python
from pulse_sdk import PulseClient

with PulseClient("pulse_write_...", "https://your-pulse-instance.example.com") as client:
    client.capture("checkout completed", user_id="user_123", properties={"revenue": 49.99})
    # auto-flushes at flush_at_size (default 20), or on `with` exit / an explicit client.flush()
```

`flush()` returns whether the request succeeded. On failure, the buffer is left untouched --
call `flush()` again to retry; this client does not retry on its own.
