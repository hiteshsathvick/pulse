"use client";

import { useParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { InsightBuilder } from "@/components/insights/InsightBuilder";
import { ProtectedRoute } from "@/components/ProtectedRoute";

function NewInsight() {
  const { orgId, projectId } = useParams<{ orgId: string; projectId: string }>();
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6">
      <h1 className="text-2xl font-semibold">New insight</h1>
      <InsightBuilder orgId={orgId} projectId={projectId} />
    </div>
  );
}

export default function NewInsightPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <NewInsight />
      </AppShell>
    </ProtectedRoute>
  );
}
