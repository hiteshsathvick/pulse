import { LocalBuffer, type PropertyValue, type QueuedEvent } from "./buffer.js";
import { generateId } from "./ids.js";
import { sendBatch } from "./transport.js";

export type { PropertyValue, QueuedEvent } from "./buffer.js";

export interface PulseOptions {
  apiHost: string;
  writeKey: string;
  /** Time trigger: how often a pending buffer is flushed regardless of size. */
  flushIntervalMs?: number;
  /** Size trigger: enqueue() flushes immediately once the buffer reaches this. */
  flushAtSize?: number;
  /** Per-request cap, mirroring the backend's own ingest_max_batch_size default. */
  maxBatchSize?: number;
  /** Safety valve: oldest events are dropped once the local buffer exceeds this. */
  maxBufferedEvents?: number;
}

const DEFAULT_FLUSH_INTERVAL_MS = 10_000;
const DEFAULT_FLUSH_AT_SIZE = 20;
const DEFAULT_MAX_BATCH_SIZE = 500;
const MIN_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;

const ANONYMOUS_ID_KEY = "pulse_anonymous_id";
const USER_ID_KEY = "pulse_user_id";

export class PulseClient {
  private readonly buffer: LocalBuffer;
  private readonly apiHost: string;
  private readonly writeKey: string;
  private readonly flushAtSize: number;
  private readonly maxBatchSize: number;
  private readonly intervalHandle: ReturnType<typeof setInterval>;
  private backoffMs = MIN_BACKOFF_MS;
  private backoffHandle: ReturnType<typeof setTimeout> | null = null;
  private flushing = false;

  constructor(options: PulseOptions) {
    this.apiHost = options.apiHost.replace(/\/$/, "");
    this.writeKey = options.writeKey;
    this.flushAtSize = options.flushAtSize ?? DEFAULT_FLUSH_AT_SIZE;
    this.maxBatchSize = options.maxBatchSize ?? DEFAULT_MAX_BATCH_SIZE;
    this.buffer = new LocalBuffer(undefined, options.maxBufferedEvents);

    const flushIntervalMs = options.flushIntervalMs ?? DEFAULT_FLUSH_INTERVAL_MS;
    this.intervalHandle = setInterval(() => void this.flush(), flushIntervalMs);

    if (typeof window !== "undefined") {
      window.addEventListener("pagehide", () => this.flushOnUnload());
      window.addEventListener("beforeunload", () => this.flushOnUnload());
    }
  }

  private getOrCreateAnonymousId(): string {
    let id = localStorage.getItem(ANONYMOUS_ID_KEY);
    if (!id) {
      id = generateId();
      localStorage.setItem(ANONYMOUS_ID_KEY, id);
    }
    return id;
  }

  /** Persists userId for every subsequent track()/page() call, and emits a
   * $identify event carrying traits -- the common SDK convention, and it
   * needs no server-side concept beyond the existing events table. */
  identify(userId: string, traits?: Record<string, PropertyValue>): void {
    localStorage.setItem(USER_ID_KEY, userId);
    this.track("$identify", traits);
  }

  /** Sugar for track("page viewed", ...) -- the exact event name Phase 6's
   * own fixture generator already uses. */
  page(name?: string, properties?: Record<string, PropertyValue>): void {
    this.track("page viewed", { ...(name !== undefined ? { name } : {}), ...properties });
  }

  track(eventName: string, properties?: Record<string, PropertyValue>): void {
    const userId = localStorage.getItem(USER_ID_KEY) ?? undefined;
    const event: QueuedEvent = {
      event_id: generateId(),
      event: eventName,
      user_id: userId,
      anonymous_id: userId ? undefined : this.getOrCreateAnonymousId(),
      timestamp: new Date().toISOString(),
      properties,
    };
    this.buffer.enqueue(event);
    if (this.buffer.size() >= this.flushAtSize) {
      void this.flush();
    }
  }

  /** Sends everything currently buffered (up to maxBatchSize). event_id is
   * never regenerated across attempts -- a retried batch is deduped
   * server-side by the ingestion worker exactly as designed, so a failed
   * flush followed by a successful one never double-counts. */
  async flush(): Promise<void> {
    if (this.flushing) return;
    const events = this.buffer.peek(this.maxBatchSize);
    if (events.length === 0) return;

    this.flushing = true;
    try {
      const ok = await sendBatch(events, { apiHost: this.apiHost, writeKey: this.writeKey });
      if (ok) {
        this.buffer.remove(events.map((event) => event.event_id));
        this.backoffMs = MIN_BACKOFF_MS;
      } else {
        this.scheduleRetry();
      }
    } finally {
      this.flushing = false;
    }
  }

  private scheduleRetry(): void {
    if (this.backoffHandle !== null) return;
    this.backoffHandle = setTimeout(() => {
      this.backoffHandle = null;
      void this.flush();
    }, this.backoffMs);
    this.backoffMs = Math.min(this.backoffMs * 2, MAX_BACKOFF_MS);
  }

  /** Best-effort only -- the real "no loss" guarantee is that these events
   * are already durable in localStorage before this call, not that this
   * particular network attempt succeeds. If it fails or is cut short by the
   * unload itself, they're still on disk for the next page load's flush. */
  private flushOnUnload(): void {
    const events = this.buffer.peek(this.maxBatchSize);
    if (events.length === 0) return;
    void sendBatch(
      events,
      { apiHost: this.apiHost, writeKey: this.writeKey },
      true,
    ).then((ok) => {
      if (ok) this.buffer.remove(events.map((event) => event.event_id));
    });
  }

  /** Stops the interval timer -- mainly for tests; a real page teardown
   * doesn't need this since the browser reclaims the timer itself. */
  destroy(): void {
    clearInterval(this.intervalHandle);
    if (this.backoffHandle !== null) clearTimeout(this.backoffHandle);
  }
}

export function init(options: PulseOptions): PulseClient {
  return new PulseClient(options);
}
