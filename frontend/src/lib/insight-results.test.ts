import { describe, expect, it } from "vitest";
import {
  formatBucket,
  formatValue,
  groupFunnel,
  hasPeriodStarted,
  pivotRetention,
  pivotTrend,
} from "./insight-results";

describe("formatValue", () => {
  it("drops float noise, keeps whole numbers whole, groups thousands", () => {
    expect(formatValue(88.97999999999999)).toBe("88.98");
    expect(formatValue(7)).toBe("7");
    expect(formatValue(1234567)).toBe("1,234,567");
    expect(formatValue(0.1 + 0.2)).toBe("0.3");
  });
});

describe("hasPeriodStarted", () => {
  const now = new Date("2026-01-12T00:00:00Z");
  it("is true once the offset period's start has passed, false while it is still ahead", () => {
    expect(hasPeriodStarted("2026-01-05T00:00:00+00:00", 0, "week", now)).toBe(true);
    expect(hasPeriodStarted("2026-01-05T00:00:00+00:00", 1, "week", now)).toBe(true); // starts exactly now
    expect(hasPeriodStarted("2026-01-05T00:00:00+00:00", 2, "week", now)).toBe(false);
    expect(hasPeriodStarted("2026-01-10T00:00:00+00:00", 3, "day", now)).toBe(false);
    expect(hasPeriodStarted("2026-01-10T00:00:00+00:00", 2, "day", now)).toBe(true);
  });
});

describe("formatBucket", () => {
  it("drops a midnight time and keeps a meaningful one, without timezone shifting", () => {
    expect(formatBucket("2026-01-05T00:00:00+00:00")).toBe("2026-01-05");
    expect(formatBucket("2026-01-05")).toBe("2026-01-05");
    expect(formatBucket("2026-01-05T13:00:00+00:00")).toBe("2026-01-05 13:00");
  });
});

describe("pivotTrend", () => {
  it("single series when there is no breakdown, sorted by bucket", () => {
    const { data, series } = pivotTrend([
      { bucket: "2026-01-02T00:00:00+00:00", value: 5 },
      { bucket: "2026-01-01T00:00:00+00:00", value: 3 },
    ]);
    expect(series).toEqual(["value"]);
    expect(data).toEqual([
      { bucket: "2026-01-01", value: 3 },
      { bucket: "2026-01-02", value: 5 },
    ]);
  });

  it("one series per breakdown value, null labelled, gaps left undefined", () => {
    const { data, series } = pivotTrend([
      { bucket: "2026-01-01T00:00:00+00:00", breakdown: "ios", value: 3 },
      { bucket: "2026-01-01T00:00:00+00:00", breakdown: null, value: 1 },
      { bucket: "2026-01-02T00:00:00+00:00", breakdown: "ios", value: 4 },
    ]);
    expect(series).toEqual(["ios", "(none)"]);
    expect(data).toEqual([
      { bucket: "2026-01-01", ios: 3, "(none)": 1 },
      { bucket: "2026-01-02", ios: 4 },
    ]);
  });
});

describe("groupFunnel", () => {
  it("splits rows into one ordered step list per breakdown value", () => {
    const step = (s: string, users: number, breakdown?: string) => ({
      step: s,
      users,
      conversion_pct: users,
      drop_off: 0,
      ...(breakdown === undefined ? {} : { breakdown }),
    });
    const groups = groupFunnel([
      step("a", 10, "ios"),
      step("b", 5, "ios"),
      step("a", 8, "web"),
      step("b", 2, "web"),
    ]);
    expect(groups.map((g) => g.breakdown)).toEqual(["ios", "web"]);
    expect(groups[0].steps.map((s) => s.step)).toEqual(["a", "b"]);
    expect(groupFunnel([step("a", 1), step("b", 1)])).toHaveLength(1);
  });
});

describe("pivotRetention", () => {
  it("builds a cohort × offset grid, sorted by cohort, with unobserved cells undefined", () => {
    const { offsets, cohorts } = pivotRetention([
      { cohort_period: "2026-01-08T00:00:00+00:00", cohort_size: 4, period_offset: 0, retained: 4, retention_pct: 100 },
      { cohort_period: "2026-01-01T00:00:00+00:00", cohort_size: 10, period_offset: 0, retained: 10, retention_pct: 100 },
      { cohort_period: "2026-01-01T00:00:00+00:00", cohort_size: 10, period_offset: 1, retained: 5, retention_pct: 50 },
    ]);
    expect(offsets).toEqual([0, 1]);
    expect(cohorts.map((c) => c.period)).toEqual([
      "2026-01-01T00:00:00+00:00",
      "2026-01-08T00:00:00+00:00",
    ]);
    expect(cohorts[0].cells).toEqual([
      { retained: 10, pct: 100 },
      { retained: 5, pct: 50 },
    ]);
    expect(cohorts[1].cells).toEqual([{ retained: 4, pct: 100 }, undefined]);
  });

  it("is empty for no rows", () => {
    expect(pivotRetention([])).toEqual({ offsets: [], cohorts: [] });
  });
});
