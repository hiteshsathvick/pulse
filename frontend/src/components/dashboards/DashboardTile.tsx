"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { InsightResult } from "@/components/insights/InsightResult";
import { Alert, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import { withRange, type ResolvedRange } from "@/lib/dashboard-range";
import type { DashboardItem } from "@/lib/dashboards-api";
import { runInsightQuery } from "@/lib/insights-api";

// Each tile fetches for itself, so a slow or failing insight never blocks or
// breaks the others on the page.
export function DashboardTile({
  orgId,
  projectId,
  insight,
  range,
  refreshCount,
}: {
  orgId: string;
  projectId: string;
  insight: DashboardItem["insight"];
  range: ResolvedRange;
  // 0 on first load (the server cache may answer); every Refresh (or
  // auto-refresh tick) increments it, which both changes the query key and
  // asks the server to bypass its cache.
  refreshCount: number;
}) {
  const { accessToken } = useAuth();
  const spec = useMemo(() => withRange(insight.spec, range), [insight.spec, range]);

  const result = useQuery({
    queryKey: ["dashboard-tile", orgId, projectId, insight.id, spec, refreshCount],
    queryFn: () =>
      runInsightQuery(accessToken!, orgId, projectId, spec, { refresh: refreshCount > 0 }),
    enabled: !!accessToken,
    // Keep showing the previous chart while a new range/refresh loads, instead
    // of flashing empty.
    placeholderData: keepPreviousData,
  });

  return (
    <section
      aria-label={insight.name}
      className="flex h-full flex-col gap-2 overflow-hidden rounded border p-3"
    >
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="truncate font-medium">{insight.name}</h3>
        {result.isFetching && result.data && (
          <span className="shrink-0 text-xs text-gray-500">Refreshing…</span>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {result.isPending && <Spinner />}
        {result.isError && <Alert>{result.error.message}</Alert>}
        {result.data && !result.isError && <InsightResult result={result.data} />}
      </div>
    </section>
  );
}
