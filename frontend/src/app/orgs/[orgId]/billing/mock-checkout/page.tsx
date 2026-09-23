"use client";

import { useParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { MockCheckoutPage } from "@/components/billing/MockCheckoutPage";
import { ProtectedRoute } from "@/components/ProtectedRoute";

function MockCheckoutHome() {
  const { orgId } = useParams<{ orgId: string }>();
  return <MockCheckoutPage orgId={orgId} />;
}

export default function OrgMockCheckoutPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <MockCheckoutHome />
      </AppShell>
    </ProtectedRoute>
  );
}
