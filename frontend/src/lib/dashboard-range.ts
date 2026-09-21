import type { InsightSpec } from "./insight-spec";

// Mirrors backend/pulse/dashboards/schemas.py.
export type DashboardRange =
  | { type: "relative"; days: number }
  | { type: "absolute"; from: string; to: string };

export type ResolvedRange = { from: string; to: string };

export const RANGE_PRESET_DAYS = [7, 14, 30, 90] as const;
export const MAX_RELATIVE_DAYS = 366;

// Pure calendar arithmetic on YYYY-MM-DD strings, done in UTC so the result
// never depends on the browser's timezone or a DST change.
export function addDays(isoDate: string, delta: number): string {
  const [y, m, d] = isoDate.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + delta)).toISOString().slice(0, 10);
}

// "Today" as a calendar date in the *project's* timezone. The query engine
// buckets and bounds by project-local dates, so a "last 30 days" computed from
// the browser's clock would be off by a day near midnight for anyone whose
// timezone differs from the project's.
export function todayInTimezone(timezone: string, now: Date = new Date()): string {
  try {
    // en-CA formats as YYYY-MM-DD.
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(now);
  } catch {
    return now.toISOString().slice(0, 10);
  }
}

// A relative range counts today: `days: 7` is today and the six days before it.
export function resolveRange(
  range: DashboardRange,
  timezone: string,
  now: Date = new Date()
): ResolvedRange {
  if (range.type === "absolute") return { from: range.from, to: range.to };
  const to = todayInTimezone(timezone, now);
  return { from: addDays(to, -(range.days - 1)), to };
}

// A saved insight carries its own fixed dates; on a dashboard the dashboard's
// range wins. Only from/to are replaced -- a per-insight timezone override, the
// funnel window, retention periods and everything else stay as saved.
export function withRange(spec: InsightSpec, range: ResolvedRange): InsightSpec {
  return { ...spec, range: { ...spec.range, from: range.from, to: range.to } };
}

export function describeRange(range: DashboardRange): string {
  return range.type === "relative" ? `Last ${range.days} days` : `${range.from} to ${range.to}`;
}

export function isValidRange(range: DashboardRange): boolean {
  if (range.type === "relative") {
    return Number.isInteger(range.days) && range.days >= 1 && range.days <= MAX_RELATIVE_DAYS;
  }
  const iso = /^\d{4}-\d{2}-\d{2}$/;
  return iso.test(range.from) && iso.test(range.to) && range.from <= range.to;
}
