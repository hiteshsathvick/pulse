"use client";

import { useParams } from "next/navigation";
import { AlertDetail } from "@/components/alerts/AlertDetail";
import { AppShell } from "@/components/AppShell";
import { ProtectedRoute } from "@/components/ProtectedRoute";

function AlertDetailHome() {
  const { orgId, projectId, alertId } = useParams<{
    orgId: string;
    projectId: string;
    alertId: string;
  }>();

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6">
      <AlertDetail orgId={orgId} projectId={projectId} alertId={alertId} />
    </div>
  );
}

export default function AlertDetailPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <AlertDetailHome />
      </AppShell>
    </ProtectedRoute>
  );
}
