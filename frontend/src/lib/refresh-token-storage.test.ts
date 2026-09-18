import { beforeEach, describe, expect, it } from "vitest";
import {
  clearStoredRefreshToken,
  getStoredRefreshToken,
  setStoredRefreshToken,
} from "./refresh-token-storage";

beforeEach(() => {
  localStorage.clear();
});

describe("refresh-token-storage", () => {
  it("returns null when nothing has been stored", () => {
    expect(getStoredRefreshToken()).toBeNull();
  });

  it("round-trips a stored token", () => {
    setStoredRefreshToken("abc123");
    expect(getStoredRefreshToken()).toBe("abc123");
  });

  it("clears a stored token", () => {
    setStoredRefreshToken("abc123");
    clearStoredRefreshToken();
    expect(getStoredRefreshToken()).toBeNull();
  });
});
