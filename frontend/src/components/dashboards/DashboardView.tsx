"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Alert, Button, EmptyState, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import { describeRange, resolveRange, type DashboardRange } from "@/lib/dashboard-range";
import type { Dashboard } from "@/lib/dashboards-api";
import { getProject } from "@/lib/projects-api";
import { DashboardEditor } from "./DashboardEditor";
import { DashboardTile } from "./DashboardTile";
import { RangePicker } from "./RangePicker";

const AUTO_REFRESH_OPTIONS = [
  { label: "Off", ms: 0 },
  { label: "Every minute", ms: 60_000 },
  { label: "Every 5 minutes", ms: 300_000 },
] as const;

const SCOPE_LABEL = { private: "Only you", org: "Shared with your organization" } as const;

export function DashboardView({
  orgId,
  projectId,
  dashboard,
}: {
  orgId: string;
  projectId: string;
  dashboard: Dashboard;
}) {
  const { accessToken } = useAuth();
  const queryClient = useQueryClient();

  // A brand-new (empty) dashboard opens straight in the editor for someone who
  // can edit it; everyone else, and every populated dashboard, opens to view.
  const [mode, setMode] = useState<"view" | "edit">(
    dashboard.can_edit && dashboard.items.length === 0 ? "edit" : "view"
  );
  // The range being *looked at*. It starts at the dashboard's saved default,
  // and anyone (including a read-only viewer) can change it for their own view
  // without saving anything.
  const [viewRange, setViewRange] = useState<DashboardRange>(dashboard.default_range);
  const [refreshCount, setRefreshCount] = useState(0);
  const [autoRefreshMs, setAutoRefreshMs] = useState<number>(0);

  const projectQuery = useQuery({
    queryKey: ["project", orgId, projectId],
    queryFn: () => getProject(accessToken!, orgId, projectId),
    enabled: !!accessToken,
  });
  const timezone = projectQuery.data?.timezone;

  const resolved = useMemo(
    () => (timezone ? resolveRange(viewRange, timezone) : null),
    [viewRange, timezone]
  );

  useEffect(() => {
    if (autoRefreshMs === 0) return;
    const id = setInterval(() => setRefreshCount((c) => c + 1), autoRefreshMs);
    return () => clearInterval(id);
  }, [autoRefreshMs]);

  if (projectQuery.isError) return <Alert>Failed to load the project.</Alert>;
  if (!resolved) return <Spinner />;

  if (mode === "edit") {
    return (
      <DashboardEditor
        orgId={orgId}
        projectId={projectId}
        dashboard={dashboard}
        customSeed={resolved}
        onCancel={() => setMode("view")}
        onSaved={(saved) => {
          queryClient.setQueryData(["dashboard", orgId, projectId, saved.id], saved);
          setViewRange(saved.default_range);
          setMode("view");
        }}
      />
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-4">
        <RangePicker
          label="Date range"
          value={viewRange}
          onChange={setViewRange}
          customSeed={resolved}
        />
        <Button type="button" variant="secondary" onClick={() => setRefreshCount((c) => c + 1)}>
          Refresh
        </Button>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Auto-refresh</span>
          <select
            className="rounded border px-3 py-2"
            value={autoRefreshMs}
            onChange={(e) => setAutoRefreshMs(Number(e.target.value))}
          >
            {AUTO_REFRESH_OPTIONS.map((option) => (
              <option key={option.ms} value={option.ms}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        {dashboard.can_edit && (
          <Button type="button" onClick={() => setMode("edit")}>
            Edit
          </Button>
        )}
      </div>

      <p className="text-sm text-gray-500">
        {SCOPE_LABEL[dashboard.shared_scope]} · {describeRange(viewRange)} ({resolved.from} to{" "}
        {resolved.to})
        {!dashboard.can_edit && " · Read-only: you can view this dashboard but not change it."}
      </p>

      {dashboard.items.length === 0 ? (
        <EmptyState>
          {dashboard.can_edit
            ? "This dashboard has no insights yet. Press Edit to add some."
            : "This dashboard has no insights yet."}
        </EmptyState>
      ) : (
        // One column on small screens; on md+ the saved grid placement is used.
        <div className="grid grid-cols-1 gap-4 md:auto-rows-[140px] md:grid-cols-12">
          {dashboard.items.map((item) => (
            <div
              key={item.id}
              className="min-h-[20rem] md:min-h-0 md:[grid-column:var(--col)] md:[grid-row:var(--row)]"
              style={
                {
                  "--col": `${item.position.x + 1} / span ${item.position.w}`,
                  "--row": `${item.position.y + 1} / span ${item.position.h}`,
                } as React.CSSProperties
              }
            >
              <DashboardTile
                orgId={orgId}
                projectId={projectId}
                insight={item.insight}
                range={resolved}
                refreshCount={refreshCount}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
