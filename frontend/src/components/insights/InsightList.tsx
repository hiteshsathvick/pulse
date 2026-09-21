"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Alert, EmptyState, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import { listInsights } from "@/lib/insights-api";

const KIND_LABELS = { trend: "Trend", funnel: "Funnel", retention: "Retention" } as const;

export function InsightList({ orgId, projectId }: { orgId: string; projectId: string }) {
  const { accessToken } = useAuth();
  const insightsQuery = useQuery({
    queryKey: ["insights", orgId, projectId],
    queryFn: () => listInsights(accessToken!, orgId, projectId),
    enabled: !!accessToken,
  });

  if (insightsQuery.isLoading) return <Spinner />;
  if (insightsQuery.isError) return <Alert>Failed to load insights.</Alert>;

  const insights = insightsQuery.data ?? [];
  if (insights.length === 0) {
    return <EmptyState>No saved insights yet. Create one to see it here.</EmptyState>;
  }

  return (
    <ul className="flex flex-col divide-y rounded border">
      {insights.map((insight) => (
        <li key={insight.id}>
          <Link
            href={`/orgs/${orgId}/projects/${projectId}/insights/${insight.id}`}
            className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 hover:bg-gray-50"
          >
            <span className="font-medium">{insight.name}</span>
            <span className="text-sm text-gray-500">
              {KIND_LABELS[insight.kind]} · updated{" "}
              {new Date(insight.updated_at).toLocaleDateString()}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}
