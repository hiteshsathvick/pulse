import { describe, expect, it } from "vitest";
import {
  addDays,
  describeRange,
  isValidRange,
  resolveRange,
  todayInTimezone,
  withRange,
} from "./dashboard-range";
import type { InsightSpec } from "./insight-spec";

describe("addDays", () => {
  it("does calendar arithmetic across month, year and leap boundaries", () => {
    expect(addDays("2026-09-22", -6)).toBe("2026-09-16");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
    expect(addDays("2028-03-01", -1)).toBe("2028-02-29"); // leap year
    expect(addDays("2026-01-01", -1)).toBe("2025-12-31");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2026-09-22", 0)).toBe("2026-09-22");
  });
});

describe("todayInTimezone", () => {
  // 20:30 UTC on the 22nd is already the 23rd in India, still the 22nd in
  // California (PDT, UTC-7) -- the same instant is a different calendar date.
  const instant = new Date("2026-09-22T20:30:00Z");

  it("is the calendar date in the given timezone, not the browser's", () => {
    expect(todayInTimezone("UTC", instant)).toBe("2026-09-22");
    expect(todayInTimezone("Asia/Kolkata", instant)).toBe("2026-09-23");
    expect(todayInTimezone("America/Los_Angeles", instant)).toBe("2026-09-22");
    expect(todayInTimezone("Pacific/Kiritimati", instant)).toBe("2026-09-23"); // UTC+14
  });

  it("falls back to the UTC date for an unknown timezone rather than throwing", () => {
    expect(todayInTimezone("Not/AZone", instant)).toBe("2026-09-22");
  });
});

describe("resolveRange", () => {
  const instant = new Date("2026-09-22T20:30:00Z");

  it("a relative range counts today, so 7 days is today plus the six before it", () => {
    expect(resolveRange({ type: "relative", days: 7 }, "UTC", instant)).toEqual({
      from: "2026-09-16",
      to: "2026-09-22",
    });
    expect(resolveRange({ type: "relative", days: 1 }, "UTC", instant)).toEqual({
      from: "2026-09-22",
      to: "2026-09-22",
    });
    expect(resolveRange({ type: "relative", days: 30 }, "UTC", instant)).toEqual({
      from: "2026-08-24",
      to: "2026-09-22",
    });
  });

  it("resolves 'today' in the project's timezone", () => {
    expect(resolveRange({ type: "relative", days: 3 }, "Asia/Kolkata", instant)).toEqual({
      from: "2026-09-21",
      to: "2026-09-23",
    });
    expect(resolveRange({ type: "relative", days: 3 }, "America/Los_Angeles", instant)).toEqual({
      from: "2026-09-20",
      to: "2026-09-22",
    });
  });

  it("an absolute range is used as-is, whatever the timezone or clock", () => {
    const range = { type: "absolute", from: "2026-01-01", to: "2026-01-31" } as const;
    expect(resolveRange(range, "Asia/Kolkata", instant)).toEqual({
      from: "2026-01-01",
      to: "2026-01-31",
    });
  });
});

describe("withRange", () => {
  const range = { from: "2026-09-01", to: "2026-09-22" };

  const specs: InsightSpec[] = [
    {
      kind: "trend",
      version: 1,
      events: ["signup"],
      measure: "count",
      filters: [{ key: "platform", op: "eq", value: "ios" }],
      breakdown: "country",
      range: { from: "2026-01-01", to: "2026-01-31", tz: "Asia/Kolkata" },
      granularity: "week",
    },
    {
      kind: "funnel",
      version: 1,
      steps: [{ event: "a" }, { event: "b" }],
      window: { value: 3, unit: "hour" },
      filters: [],
      breakdown: null,
      range: { from: "2026-01-01", to: "2026-01-31", tz: "project" },
    },
    {
      kind: "retention",
      version: 1,
      born_event: "signup",
      return_event: "login",
      period: "day",
      periods: 14,
      range: { from: "2026-01-01", to: "2026-01-31", tz: "project" },
    },
  ];

  it.each(specs)("replaces only from/to on a $kind spec and leaves the rest as saved", (spec) => {
    const result = withRange(spec, range);
    expect(result).toEqual({ ...spec, range: { ...spec.range, ...range } });
    expect(result.range.tz).toBe(spec.range.tz);
  });

  it("does not mutate the saved spec", () => {
    const spec = specs[0];
    withRange(spec, range);
    expect(spec.range.from).toBe("2026-01-01");
  });
});

describe("isValidRange / describeRange", () => {
  it("accepts 1-366 whole relative days and ordered absolute dates only", () => {
    expect(isValidRange({ type: "relative", days: 1 })).toBe(true);
    expect(isValidRange({ type: "relative", days: 366 })).toBe(true);
    for (const days of [0, -3, 367, 1.5, Number.NaN]) {
      expect(isValidRange({ type: "relative", days })).toBe(false);
    }
    expect(isValidRange({ type: "absolute", from: "2026-01-01", to: "2026-01-01" })).toBe(true);
    expect(isValidRange({ type: "absolute", from: "2026-02-01", to: "2026-01-01" })).toBe(false);
    expect(isValidRange({ type: "absolute", from: "", to: "2026-01-01" })).toBe(false);
  });

  it("describes a range for humans", () => {
    expect(describeRange({ type: "relative", days: 30 })).toBe("Last 30 days");
    expect(describeRange({ type: "absolute", from: "2026-01-01", to: "2026-01-31" })).toBe(
      "2026-01-01 to 2026-01-31"
    );
  });
});
