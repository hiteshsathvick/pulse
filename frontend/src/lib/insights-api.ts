import { authedFetch, toApiError } from "./api";
import type { InsightKind, InsightSpec } from "./insight-spec";

export type Insight = {
  id: string;
  name: string;
  kind: InsightKind;
  spec: InsightSpec;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type TrendRow = { bucket: string; value: number; breakdown?: string | null };
export type FunnelRow = {
  step: string;
  users: number;
  conversion_pct: number;
  drop_off: number;
  breakdown?: string | null;
};
export type RetentionRow = {
  cohort_period: string;
  cohort_size: number;
  period_offset: number;
  retained: number;
  retention_pct: number;
};

export type QueryResult =
  // `approximate`: a unique-user count over a large range came from a sketch, not an
  // exact scan. Counts are never approximate.
  | { kind: "trend"; results: TrendRow[]; cached: boolean; approximate: boolean }
  | { kind: "funnel"; results: FunnelRow[]; cached: boolean }
  | { kind: "retention"; results: RetentionRow[]; cached: boolean; period: "day" | "week" };

function insightsPath(orgId: string, projectId: string): string {
  return `/api/v1/orgs/${orgId}/projects/${projectId}/insights`;
}

export async function listInsights(
  accessToken: string,
  orgId: string,
  projectId: string
): Promise<Insight[]> {
  const response = await authedFetch(insightsPath(orgId, projectId), accessToken);
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function getInsight(
  accessToken: string,
  orgId: string,
  projectId: string,
  insightId: string
): Promise<Insight> {
  const response = await authedFetch(`${insightsPath(orgId, projectId)}/${insightId}`, accessToken);
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function createInsight(
  accessToken: string,
  orgId: string,
  projectId: string,
  name: string,
  spec: InsightSpec
): Promise<Insight> {
  const response = await authedFetch(insightsPath(orgId, projectId), accessToken, {
    method: "POST",
    body: JSON.stringify({ name, spec }),
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function updateInsight(
  accessToken: string,
  orgId: string,
  projectId: string,
  insightId: string,
  changes: { name?: string; spec?: InsightSpec }
): Promise<Insight> {
  const response = await authedFetch(`${insightsPath(orgId, projectId)}/${insightId}`, accessToken, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function deleteInsight(
  accessToken: string,
  orgId: string,
  projectId: string,
  insightId: string
): Promise<void> {
  const response = await authedFetch(`${insightsPath(orgId, projectId)}/${insightId}`, accessToken, {
    method: "DELETE",
  });
  if (!response.ok) throw await toApiError(response);
}

// The spec's own `kind` picks the endpoint, so a saved spec runs unchanged.
export async function runInsightQuery(
  accessToken: string,
  orgId: string,
  projectId: string,
  spec: InsightSpec,
  // `refresh` skips the server's short result cache (and re-fills it), so a
  // dashboard's Refresh shows current data rather than up-to-a-minute-old data.
  options: { refresh?: boolean } = {}
): Promise<QueryResult> {
  const response = await authedFetch(
    `/api/v1/orgs/${orgId}/projects/${projectId}/query/${spec.kind}${options.refresh ? "?refresh=true" : ""}`,
    accessToken,
    { method: "POST", body: JSON.stringify(spec) }
  );
  if (!response.ok) throw await toApiError(response);
  const body = await response.json();
  const result = { kind: spec.kind, results: body.results, cached: body.cached };
  // The retention grid needs the period length to tell "0% retained" from
  // "that period hasn't happened yet"; the response rows don't carry it.
  if (spec.kind === "retention") return { ...result, period: spec.period } as QueryResult;
  if (spec.kind === "trend") return { ...result, approximate: body.approximate === true } as QueryResult;
  return result as QueryResult;
}

// Phase 18: translates only, never executes -- the caller shows `spec` to
// the user and, if they want to run it, passes it straight to
// runInsightQuery above. Mirrors the backend's NLQueryResponse
// (pulse/api/query.py) exactly.
export type NLQueryResponse = {
  status: "ok" | "clarify";
  spec: InsightSpec | null;
  message: string | null;
  warnings: string[];
};

export async function translateNLQuery(
  accessToken: string,
  orgId: string,
  projectId: string,
  question: string
): Promise<NLQueryResponse> {
  const response = await authedFetch(
    `/api/v1/orgs/${orgId}/projects/${projectId}/query/nl`,
    accessToken,
    { method: "POST", body: JSON.stringify({ question }) }
  );
  if (!response.ok) throw await toApiError(response);
  return response.json();
}
