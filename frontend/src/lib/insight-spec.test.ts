import { describe, expect, it } from "vitest";
import {
  buildSpec,
  defaultDraft,
  defaultRange,
  specToDraft,
  switchKind,
  type Draft,
  type InsightSpec,
} from "./insight-spec";

const NOW = new Date(2026, 8, 21); // 2026-09-21, local time

function draftOf(kind: Draft["kind"], patch: Partial<Draft> = {}): Draft {
  return { ...defaultDraft(kind, NOW), from: "2026-01-01", to: "2026-01-31", ...patch };
}

function specOf(draft: Draft): InsightSpec {
  const result = buildSpec(draft);
  if (result.errors) throw new Error(`expected a valid spec, got: ${result.errors.join("; ")}`);
  return result.spec;
}

function errorsOf(draft: Draft): string[] {
  const result = buildSpec(draft);
  if (!result.errors) throw new Error("expected validation errors");
  return result.errors;
}

describe("defaultRange", () => {
  it("covers the 30 days ending today", () => {
    expect(defaultRange(NOW)).toEqual({ from: "2026-08-22", to: "2026-09-21" });
  });
});

describe("buildSpec — valid drafts emit the exact spec the API accepts", () => {
  it("trend: trims, drops blank rows, defaults breakdown to null", () => {
    const spec = specOf(
      draftOf("trend", {
        events: [" checkout completed ", "", "signup"],
        filters: [
          { key: " platform ", op: "eq", value: "ios" },
          { key: "", op: "eq", value: "" },
        ],
        granularity: "week",
      })
    );
    expect(spec).toEqual({
      kind: "trend",
      version: 1,
      events: ["checkout completed", "signup"],
      measure: "count",
      filters: [{ key: "platform", op: "eq", value: "ios" }],
      breakdown: null,
      range: { from: "2026-01-01", to: "2026-01-31", tz: "project" },
      granularity: "week",
    });
  });

  it("trend: a property measure is encoded as `<type>:<property>`", () => {
    const spec = specOf(
      draftOf("trend", {
        events: ["purchase"],
        measureType: "property_sum",
        measureProperty: "revenue",
        breakdown: "country",
      })
    );
    expect(spec).toMatchObject({ measure: "property_sum:revenue", breakdown: "country" });
  });

  it("funnel", () => {
    const spec = specOf(
      draftOf("funnel", { steps: ["signup", " ", "activate", "purchase"], windowValue: "14" })
    );
    expect(spec).toEqual({
      kind: "funnel",
      version: 1,
      steps: [{ event: "signup" }, { event: "activate" }, { event: "purchase" }],
      window: { value: 14, unit: "day" },
      filters: [],
      breakdown: null,
      range: { from: "2026-01-01", to: "2026-01-31", tz: "project" },
    });
  });

  it("retention carries no filters or breakdown (the API has none for it)", () => {
    const spec = specOf(
      draftOf("retention", {
        bornEvent: "signup",
        returnEvent: "signup",
        filters: [{ key: "x", op: "eq", value: "y" }],
        breakdown: "country",
      })
    );
    expect(spec).toEqual({
      kind: "retention",
      version: 1,
      born_event: "signup",
      return_event: "signup",
      period: "week",
      periods: 8,
      range: { from: "2026-01-01", to: "2026-01-31", tz: "project" },
    });
  });
});

describe("buildSpec — invalid drafts are rejected with a reason, never emitted", () => {
  it("trend with no events / a property measure without a property", () => {
    expect(errorsOf(draftOf("trend", { events: ["", " "] }))).toContain(
      "Choose at least one event."
    );
    expect(
      errorsOf(draftOf("trend", { events: ["a"], measureType: "property_avg", measureProperty: " " }))
    ).toContain("Choose the property to aggregate.");
  });

  it("bad or reversed date range", () => {
    expect(errorsOf(draftOf("trend", { events: ["a"], from: "", to: "2026-01-31" }))).toContain(
      "Choose a start and end date."
    );
    expect(
      errorsOf(draftOf("trend", { events: ["a"], from: "2026-02-01", to: "2026-01-01" }))
    ).toContain("The start date must not be after the end date.");
  });

  it("a half-filled filter row", () => {
    expect(
      errorsOf(draftOf("trend", { events: ["a"], filters: [{ key: "platform", op: "eq", value: "" }] }))
    ).toContain("Each filter needs both a property and a value.");
  });

  it("funnel with fewer than two steps or a bad window", () => {
    expect(errorsOf(draftOf("funnel", { steps: ["signup", ""] }))).toContain(
      "A funnel needs at least two steps."
    );
    for (const windowValue of ["0", "-1", "abc", "1.5", ""]) {
      expect(errorsOf(draftOf("funnel", { steps: ["a", "b"], windowValue }))).toContain(
        "The conversion window must be a whole number above 0."
      );
    }
  });

  it("retention missing events or with periods outside 1-52", () => {
    const missing = errorsOf(draftOf("retention"));
    expect(missing).toContain("Choose the event that starts a cohort.");
    expect(missing).toContain("Choose the event that counts as coming back.");
    for (const periods of ["0", "53", "abc", ""]) {
      expect(
        errorsOf(draftOf("retention", { bornEvent: "a", returnEvent: "a", periods }))
      ).toContain("Periods must be a whole number from 1 to 52.");
    }
    expect(
      specOf(draftOf("retention", { bornEvent: "a", returnEvent: "a", periods: "52" }))
    ).toMatchObject({ periods: 52 });
  });
});

describe("specToDraft", () => {
  const specs: InsightSpec[] = [
    {
      kind: "trend",
      version: 1,
      events: ["checkout completed", "signup"],
      measure: "property_sum:revenue",
      filters: [{ key: "platform", op: "contains", value: "io" }],
      breakdown: "country",
      range: { from: "2026-01-01", to: "2026-01-31", tz: "Asia/Kolkata" },
      granularity: "month",
    },
    {
      kind: "funnel",
      version: 1,
      steps: [{ event: "a" }, { event: "b" }, { event: "c" }],
      window: { value: 12, unit: "hour" },
      filters: [{ key: "k", op: "neq", value: "v" }],
      breakdown: null,
      range: { from: "2026-02-01", to: "2026-02-28", tz: "project" },
    },
    {
      kind: "retention",
      version: 1,
      born_event: "signup",
      return_event: "login",
      period: "day",
      periods: 14,
      range: { from: "2026-03-01", to: "2026-03-31", tz: "project" },
    },
  ];

  it.each(specs)("round-trips a saved $kind spec unchanged (a saved insight reloads)", (spec) => {
    expect(specOf(specToDraft(spec, NOW))).toEqual(spec);
  });
});

describe("switchKind", () => {
  it("keeps the range, filters and breakdown, and carries the first event across", () => {
    const start = draftOf("trend", {
      events: ["signup"],
      filters: [{ key: "platform", op: "eq", value: "ios" }],
      breakdown: "country",
    });

    const funnel = switchKind(start, "funnel");
    expect(funnel).toMatchObject({
      kind: "funnel",
      steps: ["signup", ""],
      from: "2026-01-01",
      to: "2026-01-31",
      breakdown: "country",
      filters: start.filters,
    });

    const retention = switchKind(funnel, "retention");
    expect(retention).toMatchObject({ kind: "retention", bornEvent: "signup", returnEvent: "signup" });
  });

  it("does not overwrite an event the user already picked in the target kind", () => {
    const start = draftOf("trend", { events: ["signup"], steps: ["other", ""] });
    expect(switchKind(start, "funnel").steps[0]).toBe("other");
  });
});
