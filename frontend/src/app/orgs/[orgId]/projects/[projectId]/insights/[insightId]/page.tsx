"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { InsightBuilder } from "@/components/insights/InsightBuilder";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { Alert, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import { getInsight } from "@/lib/insights-api";

function SavedInsight() {
  const { accessToken } = useAuth();
  const { orgId, projectId, insightId } = useParams<{
    orgId: string;
    projectId: string;
    insightId: string;
  }>();

  const insightQuery = useQuery({
    queryKey: ["insight", orgId, projectId, insightId],
    queryFn: () => getInsight(accessToken!, orgId, projectId, insightId),
    enabled: !!accessToken,
  });

  if (insightQuery.isLoading) return <Spinner />;
  if (insightQuery.isError) return <Alert>Failed to load this insight.</Alert>;
  if (!insightQuery.data) return null;

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6">
      <h1 className="text-2xl font-semibold">{insightQuery.data.name}</h1>
      {/* Keyed by id: opening a different saved insight remounts the builder
          with that insight's spec instead of reusing the previous one's draft. */}
      <InsightBuilder
        key={insightQuery.data.id}
        orgId={orgId}
        projectId={projectId}
        initial={insightQuery.data}
      />
    </div>
  );
}

export default function SavedInsightPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <SavedInsight />
      </AppShell>
    </ProtectedRoute>
  );
}
