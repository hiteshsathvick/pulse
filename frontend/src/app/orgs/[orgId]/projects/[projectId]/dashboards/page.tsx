"use client";

import { useParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { DashboardList } from "@/components/dashboards/DashboardList";
import { ProtectedRoute } from "@/components/ProtectedRoute";

function DashboardsHome() {
  const { orgId, projectId } = useParams<{ orgId: string; projectId: string }>();

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6">
      <h1 className="text-2xl font-semibold">Dashboards</h1>
      <DashboardList orgId={orgId} projectId={projectId} />
    </div>
  );
}

export default function DashboardsPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <DashboardsHome />
      </AppShell>
    </ProtectedRoute>
  );
}
