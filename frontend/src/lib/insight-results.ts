import type { FunnelRow, RetentionRow, TrendRow } from "./insights-api";

export const NO_BREAKDOWN_LABEL = "(none)";
export const SINGLE_SERIES = "value";

// Bucket strings are already local to the project's timezone (the query engine
// buckets server-side), so trim the string rather than round-tripping through
// Date, which would re-shift them into the browser's timezone.
export function formatBucket(bucket: string): string {
  const [date, rest = ""] = bucket.split("T");
  const time = rest.slice(0, 5);
  return time && time !== "00:00" ? `${date} ${time}` : date;
}

// Summing decimal properties yields float noise (88.97999999999999); counts
// stay whole. Two decimals is plenty for a chart/table read-out.
export function formatValue(value: number): string {
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

const PERIOD_MS = { day: 86_400_000, week: 7 * 86_400_000 } as const;

// The query engine returns a row for every cohort × offset, so a cohort born
// last week still has offsets 1..N with 0 retained. Those aren't "nobody came
// back", they haven't happened yet -- showing 0% would misstate retention.
export function hasPeriodStarted(
  cohortPeriod: string,
  offset: number,
  period: "day" | "week",
  now: Date = new Date()
): boolean {
  return new Date(cohortPeriod).getTime() + offset * PERIOD_MS[period] <= now.getTime();
}

export type TrendChartData = {
  data: Array<Record<string, string | number>>;
  series: string[];
};

export function pivotTrend(rows: TrendRow[]): TrendChartData {
  const hasBreakdown = rows.some((r) => r.breakdown !== undefined);
  const series: string[] = [];
  const byBucket = new Map<string, Record<string, string | number>>();

  for (const row of [...rows].sort((a, b) => a.bucket.localeCompare(b.bucket))) {
    const name = hasBreakdown ? (row.breakdown ?? NO_BREAKDOWN_LABEL) : SINGLE_SERIES;
    if (!series.includes(name)) series.push(name);
    const label = formatBucket(row.bucket);
    const point = byBucket.get(row.bucket) ?? { bucket: label };
    point[name] = row.value;
    byBucket.set(row.bucket, point);
  }
  return { data: [...byBucket.values()], series };
}

export type FunnelGroup = { breakdown: string | null; steps: FunnelRow[] };

export function groupFunnel(rows: FunnelRow[]): FunnelGroup[] {
  const groups = new Map<string, FunnelGroup>();
  for (const row of rows) {
    const key = row.breakdown ?? "";
    const group = groups.get(key) ?? { breakdown: row.breakdown ?? null, steps: [] };
    group.steps.push(row);
    groups.set(key, group);
  }
  return [...groups.values()];
}

export type RetentionCohort = {
  period: string;
  size: number;
  cells: Array<{ retained: number; pct: number } | undefined>;
};

export type RetentionGrid = { offsets: number[]; cohorts: RetentionCohort[] };

export function pivotRetention(rows: RetentionRow[]): RetentionGrid {
  const maxOffset = rows.reduce((max, r) => Math.max(max, r.period_offset), -1);
  const offsets = Array.from({ length: maxOffset + 1 }, (_, i) => i);
  const cohorts = new Map<string, RetentionCohort>();

  for (const row of rows) {
    const cohort = cohorts.get(row.cohort_period) ?? {
      period: row.cohort_period,
      size: row.cohort_size,
      cells: new Array(offsets.length).fill(undefined),
    };
    cohort.cells[row.period_offset] = { retained: row.retained, pct: row.retention_pct };
    cohorts.set(row.cohort_period, cohort);
  }
  return {
    offsets,
    cohorts: [...cohorts.values()].sort((a, b) => a.period.localeCompare(b.period)),
  };
}
