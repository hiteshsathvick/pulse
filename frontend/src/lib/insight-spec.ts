// Mirrors backend/pulse/query/spec.py. The builder edits a string-typed
// `Draft` (what form inputs actually hold) and `buildSpec` is the only place
// that turns it into a spec -- validating the same rules the backend enforces,
// so a spec that leaves here is one the API will accept.

export type Granularity = "hour" | "day" | "week" | "month";
export type FilterOp = "eq" | "neq" | "contains";
export type Filter = { key: string; op: FilterOp; value: string };
export type DateRange = { from: string; to: string; tz: string };

export type TrendSpec = {
  kind: "trend";
  version: 1;
  events: string[];
  measure: string;
  filters: Filter[];
  breakdown: string | null;
  range: DateRange;
  granularity: Granularity;
};

export type FunnelSpec = {
  kind: "funnel";
  version: 1;
  steps: { event: string }[];
  window: { value: number; unit: "hour" | "day" };
  filters: Filter[];
  breakdown: string | null;
  range: DateRange;
};

export type RetentionSpec = {
  kind: "retention";
  version: 1;
  born_event: string;
  return_event: string;
  period: "day" | "week";
  periods: number;
  range: DateRange;
};

export type InsightSpec = TrendSpec | FunnelSpec | RetentionSpec;
export type InsightKind = InsightSpec["kind"];

export const INSIGHT_KINDS: { kind: InsightKind; label: string }[] = [
  { kind: "trend", label: "Trend" },
  { kind: "funnel", label: "Funnel" },
  { kind: "retention", label: "Retention" },
];

export type MeasureType = "count" | "unique_users" | "property_sum" | "property_avg";

export type Draft = {
  kind: InsightKind;
  from: string;
  to: string;
  tz: string;
  filters: Filter[];
  breakdown: string;
  events: string[];
  measureType: MeasureType;
  measureProperty: string;
  granularity: Granularity;
  steps: string[];
  windowValue: string;
  windowUnit: "hour" | "day";
  bornEvent: string;
  returnEvent: string;
  period: "day" | "week";
  periods: string;
};

export const MAX_RETENTION_PERIODS = 52;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function isoDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function defaultRange(now: Date = new Date(), days = 30): { from: string; to: string } {
  const from = new Date(now);
  from.setDate(from.getDate() - days);
  return { from: isoDate(from), to: isoDate(now) };
}

export function defaultDraft(kind: InsightKind = "trend", now: Date = new Date()): Draft {
  const range = defaultRange(now);
  return {
    kind,
    from: range.from,
    to: range.to,
    tz: "project",
    filters: [],
    breakdown: "",
    events: [""],
    measureType: "count",
    measureProperty: "",
    granularity: "day",
    steps: ["", ""],
    windowValue: "7",
    windowUnit: "day",
    bornEvent: "",
    returnEvent: "",
    period: "week",
    periods: "8",
  };
}

// Switching kind keeps what the kinds share (range, filters, breakdown) and
// carries an already-picked event across, instead of resetting the whole form.
export function switchKind(draft: Draft, kind: InsightKind): Draft {
  if (kind === draft.kind) return draft;
  const firstEvent =
    draft.kind === "trend"
      ? draft.events[0]
      : draft.kind === "funnel"
        ? draft.steps[0]
        : draft.bornEvent;
  const next = { ...draft, kind };
  if (firstEvent) {
    if (kind === "trend" && !draft.events[0]) next.events = [firstEvent];
    if (kind === "funnel" && !draft.steps[0]) next.steps = [firstEvent, draft.steps[1] ?? ""];
    if (kind === "retention" && !draft.bornEvent) {
      next.bornEvent = firstEvent;
      next.returnEvent = draft.returnEvent || firstEvent;
    }
  }
  return next;
}

export function specToDraft(spec: InsightSpec, now: Date = new Date()): Draft {
  const draft: Draft = {
    ...defaultDraft(spec.kind, now),
    from: spec.range.from,
    to: spec.range.to,
    tz: spec.range.tz,
  };
  if (spec.kind === "trend") {
    const [type, ...rest] = spec.measure.split(":");
    const property = rest.join(":");
    return {
      ...draft,
      events: spec.events,
      filters: spec.filters,
      breakdown: spec.breakdown ?? "",
      granularity: spec.granularity,
      measureType: type as MeasureType,
      measureProperty: property,
    };
  }
  if (spec.kind === "funnel") {
    return {
      ...draft,
      steps: spec.steps.map((s) => s.event),
      windowValue: String(spec.window.value),
      windowUnit: spec.window.unit,
      filters: spec.filters,
      breakdown: spec.breakdown ?? "",
    };
  }
  return {
    ...draft,
    bornEvent: spec.born_event,
    returnEvent: spec.return_event,
    period: spec.period,
    periods: String(spec.periods),
  };
}

export type BuildResult = { spec: InsightSpec; errors?: undefined } | { spec?: undefined; errors: string[] };

function buildRange(draft: Draft, errors: string[]): DateRange {
  if (!DATE_RE.test(draft.from) || !DATE_RE.test(draft.to)) {
    errors.push("Choose a start and end date.");
  } else if (draft.from > draft.to) {
    errors.push("The start date must not be after the end date.");
  }
  return { from: draft.from, to: draft.to, tz: draft.tz || "project" };
}

function buildFilters(draft: Draft, errors: string[]): Filter[] {
  const filters: Filter[] = [];
  for (const f of draft.filters) {
    const key = f.key.trim();
    // A fully blank row is just an unfilled slot, not a mistake.
    if (!key && !f.value.trim()) continue;
    if (!key || !f.value.trim()) {
      errors.push("Each filter needs both a property and a value.");
      continue;
    }
    filters.push({ key, op: f.op, value: f.value });
  }
  return filters;
}

function nonBlank(values: string[]): string[] {
  return values.map((v) => v.trim()).filter((v) => v.length > 0);
}

function parsePositiveInt(raw: string): number | null {
  const trimmed = raw.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const n = Number(trimmed);
  return n > 0 ? n : null;
}

export function buildSpec(draft: Draft): BuildResult {
  const errors: string[] = [];
  const range = buildRange(draft, errors);

  if (draft.kind === "trend") {
    const events = nonBlank(draft.events);
    if (events.length === 0) errors.push("Choose at least one event.");

    let measure: string = draft.measureType;
    if (draft.measureType === "property_sum" || draft.measureType === "property_avg") {
      const property = draft.measureProperty.trim();
      if (!property) errors.push("Choose the property to aggregate.");
      measure = `${draft.measureType}:${property}`;
    }

    const filters = buildFilters(draft, errors);
    if (errors.length > 0) return { errors };
    return {
      spec: {
        kind: "trend",
        version: 1,
        events,
        measure,
        filters,
        breakdown: draft.breakdown.trim() || null,
        range,
        granularity: draft.granularity,
      },
    };
  }

  if (draft.kind === "funnel") {
    const steps = nonBlank(draft.steps);
    if (steps.length < 2) errors.push("A funnel needs at least two steps.");
    const windowValue = parsePositiveInt(draft.windowValue);
    if (windowValue === null) errors.push("The conversion window must be a whole number above 0.");
    const filters = buildFilters(draft, errors);
    if (errors.length > 0 || windowValue === null) return { errors };
    return {
      spec: {
        kind: "funnel",
        version: 1,
        steps: steps.map((event) => ({ event })),
        window: { value: windowValue, unit: draft.windowUnit },
        filters,
        breakdown: draft.breakdown.trim() || null,
        range,
      },
    };
  }

  const born = draft.bornEvent.trim();
  const ret = draft.returnEvent.trim();
  if (!born) errors.push("Choose the event that starts a cohort.");
  if (!ret) errors.push("Choose the event that counts as coming back.");
  const periods = parsePositiveInt(draft.periods);
  if (periods === null || periods > MAX_RETENTION_PERIODS) {
    errors.push(`Periods must be a whole number from 1 to ${MAX_RETENTION_PERIODS}.`);
  }
  if (errors.length > 0 || periods === null) return { errors };
  return {
    spec: {
      kind: "retention",
      version: 1,
      born_event: born,
      return_event: ret,
      period: draft.period,
      periods,
      range,
    },
  };
}
