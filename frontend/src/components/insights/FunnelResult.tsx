import { EmptyState } from "@/components/ui";
import { groupFunnel, NO_BREAKDOWN_LABEL } from "@/lib/insight-results";
import type { FunnelRow } from "@/lib/insights-api";

function formatPct(pct: number): string {
  return `${pct.toFixed(1)}%`;
}

export function FunnelResult({ rows }: { rows: FunnelRow[] }) {
  if (rows.length === 0) return <EmptyState>No users entered this funnel.</EmptyState>;

  const groups = groupFunnel(rows);
  const showBreakdown = rows.some((r) => r.breakdown !== undefined);

  return (
    <div className="flex flex-col gap-8">
      {groups.map((group) => (
        <section key={group.breakdown ?? ""} className="flex flex-col gap-3">
          {showBreakdown && (
            <h3 className="text-sm font-medium text-gray-600">
              {group.breakdown ?? NO_BREAKDOWN_LABEL}
            </h3>
          )}
          <ol className="flex flex-col gap-3">
            {group.steps.map((step, index) => (
              <li key={`${index}-${step.step}`} className="flex flex-col gap-1">
                <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
                  <span className="font-medium">
                    {index + 1}. {step.step}
                  </span>
                  <span className="text-gray-600">
                    {step.users.toLocaleString()} users · {formatPct(step.conversion_pct)}
                    {index > 0 && ` · ${step.drop_off.toLocaleString()} dropped`}
                  </span>
                </div>
                <div
                  className="h-6 w-full rounded bg-gray-100"
                  role="img"
                  aria-label={`${step.step}: ${formatPct(step.conversion_pct)} of the first step`}
                >
                  <div
                    className="h-6 rounded bg-blue-600"
                    style={{ width: `${Math.min(100, Math.max(0, step.conversion_pct))}%` }}
                  />
                </div>
              </li>
            ))}
          </ol>
        </section>
      ))}
    </div>
  );
}
