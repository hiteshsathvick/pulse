"use client";

import { useParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { SubjectDeletion } from "@/components/privacy/SubjectDeletion";

function PrivacyHome() {
  const { orgId, projectId } = useParams<{ orgId: string; projectId: string }>();

  return <SubjectDeletion orgId={orgId} projectId={projectId} />;
}

export default function PrivacyPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <PrivacyHome />
      </AppShell>
    </ProtectedRoute>
  );
}
