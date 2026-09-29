"use client";

import { useParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { PiiRules } from "@/components/pii-rules/PiiRules";

function PiiRulesHome() {
  const { orgId, projectId } = useParams<{ orgId: string; projectId: string }>();
  return <PiiRules orgId={orgId} projectId={projectId} />;
}

export default function PiiRulesPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <PiiRulesHome />
      </AppShell>
    </ProtectedRoute>
  );
}
