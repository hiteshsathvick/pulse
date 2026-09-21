"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Alert, Button, EmptyState, Input, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import {
  createDashboard,
  listDashboards,
  type DashboardScope,
} from "@/lib/dashboards-api";
import { listMyOrgs } from "@/lib/orgs-api";

const SCOPE_LABEL = { private: "Only you", org: "Shared with your organization" } as const;

export function DashboardList({ orgId, projectId }: { orgId: string; projectId: string }) {
  const { accessToken } = useAuth();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [scope, setScope] = useState<DashboardScope>("private");
  const [nameError, setNameError] = useState(false);

  const listKey = ["dashboards", orgId, projectId];
  const dashboardsQuery = useQuery({
    queryKey: listKey,
    queryFn: () => listDashboards(accessToken!, orgId, projectId),
    enabled: !!accessToken,
  });

  // Viewers can't create dashboards (the API refuses), so don't offer the form.
  // Shares the switcher's ["orgs"] query, so this is usually already cached.
  const orgsQuery = useQuery({
    queryKey: ["orgs"],
    queryFn: () => listMyOrgs(accessToken!),
    enabled: !!accessToken,
  });
  const role = orgsQuery.data?.find((org) => org.id === orgId)?.role;
  const canCreate = role !== "viewer";

  const createMutation = useMutation({
    mutationFn: () =>
      createDashboard(accessToken!, orgId, projectId, { name: name.trim(), shared_scope: scope }),
    onSuccess: (dashboard) => {
      void queryClient.invalidateQueries({ queryKey: listKey });
      router.push(`/orgs/${orgId}/projects/${projectId}/dashboards/${dashboard.id}`);
    },
  });

  function handleCreate(event: React.FormEvent) {
    event.preventDefault();
    if (!name.trim()) {
      setNameError(true);
      return;
    }
    setNameError(false);
    createMutation.mutate();
  }

  return (
    <div className="flex flex-col gap-6">
      {canCreate && (
        <form onSubmit={handleCreate} className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">New dashboard name</span>
            <Input
              value={name}
              maxLength={200}
              placeholder="e.g. Weekly growth"
              onChange={(e) => {
                setNameError(false);
                setName(e.target.value);
              }}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">Who can see it</span>
            <select
              className="rounded border px-3 py-2"
              value={scope}
              onChange={(e) => setScope(e.target.value as DashboardScope)}
            >
              <option value="private">Only me</option>
              <option value="org">Everyone in my organization</option>
            </select>
          </label>
          <Button type="submit" disabled={createMutation.isPending}>
            Create dashboard
          </Button>
        </form>
      )}
      {nameError && <Alert>Give the dashboard a name.</Alert>}
      {createMutation.isError && <Alert>{createMutation.error.message}</Alert>}

      {dashboardsQuery.isLoading && <Spinner />}
      {dashboardsQuery.isError && <Alert>Failed to load dashboards.</Alert>}
      {dashboardsQuery.data &&
        (dashboardsQuery.data.length === 0 ? (
          <EmptyState>No dashboards yet.</EmptyState>
        ) : (
          <ul className="flex flex-col divide-y rounded border">
            {dashboardsQuery.data.map((dashboard) => (
              <li key={dashboard.id}>
                <Link
                  href={`/orgs/${orgId}/projects/${projectId}/dashboards/${dashboard.id}`}
                  className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 hover:bg-gray-50"
                >
                  <span className="font-medium">{dashboard.name}</span>
                  <span className="text-sm text-gray-500">
                    {dashboard.item_count} {dashboard.item_count === 1 ? "insight" : "insights"} ·{" "}
                    {SCOPE_LABEL[dashboard.shared_scope]}
                    {!dashboard.can_edit && " · Read-only"}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        ))}
    </div>
  );
}
