"use client";

import { useParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { BillingPage } from "@/components/billing/BillingPage";
import { ProtectedRoute } from "@/components/ProtectedRoute";

function BillingHome() {
  const { orgId } = useParams<{ orgId: string }>();
  return <BillingPage orgId={orgId} />;
}

export default function OrgBillingPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <BillingHome />
      </AppShell>
    </ProtectedRoute>
  );
}
