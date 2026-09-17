import type { QueuedEvent } from "./buffer.js";

export interface TransportOptions {
  apiHost: string;
  writeKey: string;
}

/** fetch({ keepalive: true }), not navigator.sendBeacon -- sendBeacon cannot
 * send custom headers at all, and /ingest auth is the same X-API-Key header
 * every other call site uses. keepalive survives page teardown the same way
 * sendBeacon does, while keeping one auth mechanism everywhere. Its real
 * constraint: a shared ~64KB in-flight body budget per origin, which is why
 * the caller keeps flush batches bounded. */
export async function sendBatch(
  events: QueuedEvent[],
  options: TransportOptions,
  keepalive = false,
): Promise<boolean> {
  try {
    const response = await fetch(`${options.apiHost}/ingest`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": options.writeKey,
      },
      body: JSON.stringify({ batch: events }),
      keepalive,
    });
    return response.ok;
  } catch {
    // Network error, CORS failure, offline -- treated the same as a non-2xx:
    // the caller retries with backoff, nothing is removed from the buffer.
    return false;
  }
}
