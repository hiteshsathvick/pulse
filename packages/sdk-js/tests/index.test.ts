import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LocalBuffer } from "../src/buffer.js";
import { PulseClient, init } from "../src/index.js";

let client: PulseClient | undefined;

function eventIdsSentIn(fetchMock: ReturnType<typeof vi.fn>, callIndex: number): string[] {
  const call = fetchMock.mock.calls[callIndex]!;
  const body = JSON.parse(call[1].body) as { batch: { event_id: string }[] };
  return body.batch.map((e) => e.event_id);
}

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers();
});

afterEach(() => {
  client?.destroy();
  client = undefined;
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("offline buffering", () => {
  it("tracked events are durable in localStorage even before any flush attempt", () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => new Promise(() => {}))); // never resolves
    client = init({ apiHost: "http://api.test", writeKey: "k", flushAtSize: 1000 });

    client.track("button clicked");

    // Read via a fresh LocalBuffer, independent of the client -- the event
    // must already be on disk, not just held in an in-memory queue.
    expect(new LocalBuffer().read()).toHaveLength(1);
  });

  it("events queued while every flush attempt fails remain fully queued", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
    client = init({ apiHost: "http://api.test", writeKey: "k", flushAtSize: 1 });

    client.track("button clicked");
    await vi.advanceTimersByTimeAsync(0);

    expect(new LocalBuffer().read()).toHaveLength(1);
  });
});

describe("flush triggers", () => {
  it("flushes immediately once the buffer reaches flushAtSize", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);
    client = init({ apiHost: "http://api.test", writeKey: "k", flushAtSize: 2 });

    client.track("a");
    expect(fetchMock).not.toHaveBeenCalled();
    client.track("b");
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(new LocalBuffer().read()).toHaveLength(0);
  });

  it("flushes on the interval even below the size threshold", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);
    client = init({
      apiHost: "http://api.test",
      writeKey: "k",
      flushAtSize: 1000,
      flushIntervalMs: 5000,
    });

    client.track("a");
    expect(fetchMock).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(5000);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(new LocalBuffer().read()).toHaveLength(0);
  });
});

describe("retry and idempotency", () => {
  it("retries a failed flush with the same event_id, and stops retrying once it succeeds", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false })
      .mockResolvedValueOnce({ ok: true });
    vi.stubGlobal("fetch", fetchMock);
    client = init({ apiHost: "http://api.test", writeKey: "k", flushAtSize: 1 });

    client.track("button clicked");
    await vi.advanceTimersByTimeAsync(0); // first attempt: fails

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const firstAttemptId = eventIdsSentIn(fetchMock, 0)[0];
    expect(new LocalBuffer().read()).toHaveLength(1); // not removed on failure

    await vi.advanceTimersByTimeAsync(1000); // backoff elapses: retry

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(eventIdsSentIn(fetchMock, 1)[0]).toBe(firstAttemptId); // same id, not regenerated
    expect(new LocalBuffer().read()).toHaveLength(0); // removed once it succeeds

    await vi.advanceTimersByTimeAsync(30_000); // no further retries once empty
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("no loss on unload", () => {
  it("attempts a keepalive flush on pagehide", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);
    client = init({ apiHost: "http://api.test", writeKey: "k", flushAtSize: 1000 });

    client.track("button clicked");
    window.dispatchEvent(new Event("pagehide"));
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ keepalive: true }),
    );
  });

  it("keeps the event in localStorage if the unload-time send fails or is cut short", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("cut short")));
    client = init({ apiHost: "http://api.test", writeKey: "k", flushAtSize: 1000 });

    client.track("button clicked");
    window.dispatchEvent(new Event("pagehide"));
    await vi.advanceTimersByTimeAsync(0);

    // The next page load's client would read this same storage key and
    // still see the event -- that's the real "no loss" guarantee, not the
    // success of the unload-time network call itself.
    expect(new LocalBuffer().read()).toHaveLength(1);
  });
});

describe("identify and page", () => {
  it("identify persists the user id for subsequent track calls, replacing the anonymous id", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => new Promise(() => {})));
    client = init({ apiHost: "http://api.test", writeKey: "k", flushAtSize: 1000 });

    client.identify("user_42", { plan: "pro" });
    client.track("button clicked");

    const events = new LocalBuffer().read();
    expect(events[0]!.event).toBe("$identify");
    expect(events[0]!.user_id).toBe("user_42");
    expect(events[0]!.anonymous_id).toBeUndefined();
    expect(events[1]!.user_id).toBe("user_42");
  });

  it("page() emits a page viewed event", () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => new Promise(() => {})));
    client = init({ apiHost: "http://api.test", writeKey: "k", flushAtSize: 1000 });

    client.page("Pricing");

    const events = new LocalBuffer().read();
    expect(events[0]!.event).toBe("page viewed");
    expect(events[0]!.properties).toEqual({ name: "Pricing" });
  });
});
