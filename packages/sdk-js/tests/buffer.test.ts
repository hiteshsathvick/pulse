import { beforeEach, describe, expect, it } from "vitest";
import { LocalBuffer, type QueuedEvent } from "../src/buffer.js";

function event(id: string): QueuedEvent {
  return { event_id: id, event: "button clicked", timestamp: new Date().toISOString() };
}

beforeEach(() => {
  localStorage.clear();
});

describe("LocalBuffer", () => {
  it("persists enqueued events across a fresh instance reading the same key", () => {
    new LocalBuffer("test-key").enqueue(event("a"));
    // A brand new instance -- like a page reload -- reading the same
    // storage key must see what a prior instance wrote.
    const reloaded = new LocalBuffer("test-key");
    expect(reloaded.read().map((e) => e.event_id)).toEqual(["a"]);
  });

  it("removes only the given event ids", () => {
    const buffer = new LocalBuffer("test-key");
    buffer.enqueue(event("a"));
    buffer.enqueue(event("b"));
    buffer.enqueue(event("c"));

    buffer.remove(["b"]);

    expect(buffer.read().map((e) => e.event_id)).toEqual(["a", "c"]);
  });

  it("peek returns at most `count` events without removing them", () => {
    const buffer = new LocalBuffer("test-key");
    buffer.enqueue(event("a"));
    buffer.enqueue(event("b"));

    const peeked = buffer.peek(1);

    expect(peeked.map((e) => e.event_id)).toEqual(["a"]);
    expect(buffer.size()).toBe(2);
  });

  it("evicts the oldest events once the buffer exceeds its max size", () => {
    const buffer = new LocalBuffer("test-key", 3);
    buffer.enqueue(event("a"));
    buffer.enqueue(event("b"));
    buffer.enqueue(event("c"));
    buffer.enqueue(event("d"));

    expect(buffer.read().map((e) => e.event_id)).toEqual(["b", "c", "d"]);
  });

  it("tolerates corrupted storage instead of throwing", () => {
    localStorage.setItem("test-key", "not json");
    const buffer = new LocalBuffer("test-key");
    expect(buffer.read()).toEqual([]);
  });
});
