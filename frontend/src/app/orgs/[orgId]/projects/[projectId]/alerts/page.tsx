"use client";

import { useParams } from "next/navigation";
import { AlertList } from "@/components/alerts/AlertList";
import { AppShell } from "@/components/AppShell";
import { ProtectedRoute } from "@/components/ProtectedRoute";

function AlertsHome() {
  const { orgId, projectId } = useParams<{ orgId: string; projectId: string }>();

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6">
      <h1 className="text-2xl font-semibold">Alerts</h1>
      <AlertList orgId={orgId} projectId={projectId} />
    </div>
  );
}

export default function AlertsPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <AlertsHome />
      </AppShell>
    </ProtectedRoute>
  );
}
