import { EmptyState } from "@/components/ui";
import { formatBucket, hasPeriodStarted, pivotRetention } from "@/lib/insight-results";
import type { RetentionRow } from "@/lib/insights-api";

export function RetentionResult({
  rows,
  period,
  now,
}: {
  rows: RetentionRow[];
  period: "day" | "week";
  now?: Date;
}) {
  if (rows.length === 0) return <EmptyState>No users were born in this range.</EmptyState>;

  const { offsets, cohorts } = pivotRetention(rows);

  return (
    <div className="overflow-x-auto">
      <table className="text-left text-sm">
        <thead>
          <tr className="border-b">
            <th className="py-2 pr-4 font-medium">Cohort</th>
            <th className="py-2 pr-4 font-medium">Users</th>
            {offsets.map((offset) => (
              <th key={offset} className="px-2 py-2 text-center font-medium">
                {offset}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {cohorts.map((cohort) => (
            <tr key={cohort.period} className="border-b">
              <td className="py-2 pr-4 whitespace-nowrap">{formatBucket(cohort.period)}</td>
              <td className="py-2 pr-4">{cohort.size.toLocaleString()}</td>
              {cohort.cells.map((cell, offset) =>
                cell === undefined || !hasPeriodStarted(cohort.period, offset, period, now) ? (
                  <td
                    key={offset}
                    title="Not observable yet"
                    className="px-2 py-2 text-center text-gray-300"
                  >
                    —
                  </td>
                ) : (
                  <td
                    key={offset}
                    title={`${cell.retained.toLocaleString()} of ${cohort.size.toLocaleString()} users`}
                    className="px-2 py-2 text-center"
                    style={{
                      backgroundColor: `rgba(37, 99, 235, ${Math.min(1, cell.pct / 100)})`,
                      color: cell.pct > 55 ? "#ffffff" : "#111827",
                    }}
                  >
                    {cell.pct.toFixed(1)}%
                  </td>
                ),
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
