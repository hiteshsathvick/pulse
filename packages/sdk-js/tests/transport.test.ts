import { afterEach, describe, expect, it, vi } from "vitest";
import type { QueuedEvent } from "../src/buffer.js";
import { sendBatch } from "../src/transport.js";

const event: QueuedEvent = {
  event_id: "e1",
  event: "button clicked",
  timestamp: new Date().toISOString(),
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("sendBatch", () => {
  it("posts to {apiHost}/ingest with the write key as X-API-Key, never a custom header sendBeacon could not have sent", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);

    const ok = await sendBatch([event], { apiHost: "http://api.test", writeKey: "pulse_write_x" });

    expect(ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/ingest",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "X-API-Key": "pulse_write_x" }),
        body: JSON.stringify({ batch: [event] }),
        keepalive: false,
      }),
    );
  });

  it("passes keepalive through for the unload path", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);

    await sendBatch([event], { apiHost: "http://api.test", writeKey: "k" }, true);

    expect(fetchMock).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ keepalive: true }),
    );
  });

  it("returns false, not a thrown error, on a non-2xx response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
    const ok = await sendBatch([event], { apiHost: "http://api.test", writeKey: "k" });
    expect(ok).toBe(false);
  });

  it("returns false, not a thrown error, on a network failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );
    const ok = await sendBatch([event], { apiHost: "http://api.test", writeKey: "k" });
    expect(ok).toBe(false);
  });
});
