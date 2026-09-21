"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { DashboardView } from "@/components/dashboards/DashboardView";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { Alert, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import { getDashboard } from "@/lib/dashboards-api";

function SavedDashboard() {
  const { accessToken } = useAuth();
  const { orgId, projectId, dashboardId } = useParams<{
    orgId: string;
    projectId: string;
    dashboardId: string;
  }>();

  const dashboardQuery = useQuery({
    queryKey: ["dashboard", orgId, projectId, dashboardId],
    queryFn: () => getDashboard(accessToken!, orgId, projectId, dashboardId),
    enabled: !!accessToken,
  });

  if (dashboardQuery.isLoading) return <Spinner />;
  if (dashboardQuery.isError) {
    // A private dashboard someone else made reads as "not found" by design.
    return <Alert>This dashboard doesn&apos;t exist, or you don&apos;t have access to it.</Alert>;
  }
  if (!dashboardQuery.data) return null;

  return (
    <div className="flex w-full flex-col gap-4">
      <div className="flex flex-col gap-1">
        <Link
          href={`/orgs/${orgId}/projects/${projectId}/dashboards`}
          className="text-sm underline"
        >
          All dashboards
        </Link>
        <h1 className="text-2xl font-semibold">{dashboardQuery.data.name}</h1>
      </div>
      {/* Keyed by id so opening a different dashboard remounts with its own state. */}
      <DashboardView
        key={dashboardQuery.data.id}
        orgId={orgId}
        projectId={projectId}
        dashboard={dashboardQuery.data}
      />
    </div>
  );
}

export default function SavedDashboardPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <SavedDashboard />
      </AppShell>
    </ProtectedRoute>
  );
}
