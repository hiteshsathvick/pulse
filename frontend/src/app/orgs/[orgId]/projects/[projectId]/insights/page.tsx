"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { InsightList } from "@/components/insights/InsightList";
import { ProtectedRoute } from "@/components/ProtectedRoute";

function InsightsHome() {
  const { orgId, projectId } = useParams<{ orgId: string; projectId: string }>();

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold">Insights</h1>
        <Link
          href={`/orgs/${orgId}/projects/${projectId}/insights/new`}
          className="rounded bg-black px-3 py-2 text-white"
        >
          New insight
        </Link>
      </div>
      <InsightList orgId={orgId} projectId={projectId} />
    </div>
  );
}

export default function InsightsPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <InsightsHome />
      </AppShell>
    </ProtectedRoute>
  );
}
