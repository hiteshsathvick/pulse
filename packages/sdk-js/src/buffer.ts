export type PropertyValue = string | number | boolean | null;

export interface QueuedEvent {
  event_id: string;
  event: string;
  user_id?: string;
  anonymous_id?: string;
  timestamp: string;
  properties?: Record<string, PropertyValue>;
}

const DEFAULT_STORAGE_KEY = "pulse_buffer_v1";
const DEFAULT_MAX_BUFFERED = 1000;

/** A localStorage-backed queue -- events are durable the instant enqueue()
 * returns, before any network attempt is ever made. That's what makes
 * offline buffering and "no loss on unload" real rather than aspirational:
 * a page can be closed mid-flush and the events are still on disk for the
 * next page load to pick up. */
export class LocalBuffer {
  private readonly storageKey: string;
  private readonly maxSize: number;

  constructor(storageKey: string = DEFAULT_STORAGE_KEY, maxSize: number = DEFAULT_MAX_BUFFERED) {
    this.storageKey = storageKey;
    this.maxSize = maxSize;
  }

  read(): QueuedEvent[] {
    try {
      const raw = localStorage.getItem(this.storageKey);
      if (!raw) return [];
      const parsed: unknown = JSON.parse(raw);
      return Array.isArray(parsed) ? (parsed as QueuedEvent[]) : [];
    } catch {
      return [];
    }
  }

  private write(events: QueuedEvent[]): void {
    try {
      localStorage.setItem(this.storageKey, JSON.stringify(events));
    } catch {
      // localStorage full or unavailable (private browsing, quota) -- there
      // is nothing more we can do; enqueue() never throws for this.
    }
  }

  enqueue(event: QueuedEvent): void {
    const events = this.read();
    events.push(event);
    if (events.length > this.maxSize) {
      // Oldest evicted first: a prolonged API outage must not grow
      // localStorage without bound. Not in the DoD's literal wording, but a
      // necessary safety valve.
      const dropped = events.length - this.maxSize;
      events.splice(0, dropped);
      console.warn(
        `[pulse] local buffer exceeded ${this.maxSize} events; dropped ${dropped} oldest`,
      );
    }
    this.write(events);
  }

  peek(count: number): QueuedEvent[] {
    return this.read().slice(0, count);
  }

  remove(eventIds: string[]): void {
    if (eventIds.length === 0) return;
    const toRemove = new Set(eventIds);
    this.write(this.read().filter((event) => !toRemove.has(event.event_id)));
  }

  size(): number {
    return this.read().length;
  }
}
