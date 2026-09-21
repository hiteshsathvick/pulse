import { authedFetch, toApiError } from "./api";
import type { DashboardRange } from "./dashboard-range";
import type { Position } from "./dashboard-layout";
import type { InsightKind, InsightSpec } from "./insight-spec";

export type DashboardScope = "private" | "org";

export type DashboardItem = {
  id: string;
  insight: { id: string; name: string; kind: InsightKind; spec: InsightSpec };
  position: Position;
};

export type DashboardSummary = {
  id: string;
  name: string;
  shared_scope: DashboardScope;
  created_by: string;
  updated_at: string;
  item_count: number;
  can_edit: boolean;
};

export type Dashboard = {
  id: string;
  name: string;
  layout: { columns: number };
  default_range: DashboardRange;
  shared_scope: DashboardScope;
  created_by: string;
  created_at: string;
  updated_at: string;
  // Decided by the server, so the UI never re-implements the sharing rules.
  can_edit: boolean;
  items: DashboardItem[];
};

export type DashboardPatch = {
  name?: string;
  shared_scope?: DashboardScope;
  default_range?: DashboardRange;
  items?: { insight_id: string; position: Position }[];
};

function path(orgId: string, projectId: string, suffix = ""): string {
  return `/api/v1/orgs/${orgId}/projects/${projectId}/dashboards${suffix}`;
}

export async function listDashboards(
  accessToken: string,
  orgId: string,
  projectId: string
): Promise<DashboardSummary[]> {
  const response = await authedFetch(path(orgId, projectId), accessToken);
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function getDashboard(
  accessToken: string,
  orgId: string,
  projectId: string,
  dashboardId: string
): Promise<Dashboard> {
  const response = await authedFetch(path(orgId, projectId, `/${dashboardId}`), accessToken);
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function createDashboard(
  accessToken: string,
  orgId: string,
  projectId: string,
  body: { name: string; shared_scope: DashboardScope }
): Promise<Dashboard> {
  const response = await authedFetch(path(orgId, projectId), accessToken, {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function updateDashboard(
  accessToken: string,
  orgId: string,
  projectId: string,
  dashboardId: string,
  patch: DashboardPatch
): Promise<Dashboard> {
  const response = await authedFetch(path(orgId, projectId, `/${dashboardId}`), accessToken, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function deleteDashboard(
  accessToken: string,
  orgId: string,
  projectId: string,
  dashboardId: string
): Promise<void> {
  const response = await authedFetch(path(orgId, projectId, `/${dashboardId}`), accessToken, {
    method: "DELETE",
  });
  if (!response.ok) throw await toApiError(response);
}
