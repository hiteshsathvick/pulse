"use client";

import { useParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { InsightBuilder } from "@/components/insights/InsightBuilder";
import { NLQueryBox } from "@/components/insights/NLQueryBox";
import { ProtectedRoute } from "@/components/ProtectedRoute";

function NewInsight() {
  const { orgId, projectId } = useParams<{ orgId: string; projectId: string }>();
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6">
      <h1 className="text-2xl font-semibold">New insight</h1>
      <NLQueryBox orgId={orgId} projectId={projectId} />
      <p className="text-center text-xs text-gray-500">or build one manually</p>
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
