# @pulse/sdk-js

The Pulse browser ingestion SDK: `identify`/`track`/`page`, backed by a durable local buffer so
events survive a closed tab, not just a slow network.

## Install

Standalone package for now -- not published, not linked into `frontend/` via a workspace. Build it
locally:

```bash
cd packages/sdk-js
npm install
npm run build   # emits dist/index.js + dist/index.d.ts
npm test
```

## Usage

```ts
import { init } from "@pulse/sdk-js";

const pulse = init({
  apiHost: "https://your-pulse-instance.example.com",
  writeKey: "pulse_write_...", // a project's write key -- see the console's Keys page
});

pulse.identify("user_123", { plan: "pro" });
pulse.track("checkout completed", { revenue: 49.99 });
pulse.page("Pricing");
```

## How delivery actually works

- Every `track`/`identify`/`page` call writes to `localStorage` **before** any network attempt --
  that's what makes offline buffering and "no loss on unload" real. A closed tab or a network
  outage never loses a queued event; it's picked up by the next flush, even across a page reload.
- Flushes trigger on a size threshold (`flushAtSize`, default 20), a time interval
  (`flushIntervalMs`, default 10s), and on `pagehide`/`beforeunload` via `fetch({ keepalive: true })`
  -- not `navigator.sendBeacon`, which cannot send the `X-API-Key` header `/ingest` requires. The
  unload-time send is still only best-effort: if it's cut short by the unload itself, the event
  stays in `localStorage` and goes out on the next page load instead.
- A failed flush retries with exponential backoff (1s up to 30s) and never regenerates `event_id` --
  a retried batch is deduped server-side by the ingestion worker (Phase 8), so a flush that
  eventually succeeds after failing never double-counts.
- The local buffer is capped (`maxBufferedEvents`, default 1000); if the API is down long enough to
  fill it, the oldest events are dropped with a console warning rather than growing `localStorage`
  without bound.

## Demo

`demo/index.html` loads the built SDK directly (`../dist/index.js`) and exercises every method
against a real running Pulse instance -- open it after `npm run build`, plug in a real API host and
write key (create one via the console's project Keys page), and click through the buttons.
