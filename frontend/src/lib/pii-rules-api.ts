import { authedFetch, toApiError } from "./api";

// Mirrors backend/pulse/api/pii_rules.py.
export type PiiAction = "hash" | "drop";

export type PiiRule = {
  id: string;
  property_key: string;
  action: PiiAction;
  created_at: string;
};

export type ArchiveRewriteSummary = {
  entries_scrubbed: number;
  objects_rewritten: number;
  unreadable_objects: number;
};

export type CreatePiiRuleResult = PiiRule & {
  // null only if the retroactive archive rewrite failed outright (e.g. the
  // object store was unreachable) -- the rule is still created and enforced
  // for new events either way.
  archive_rewrite: ArchiveRewriteSummary | null;
};

export async function listPiiRules(
  accessToken: string,
  orgId: string,
  projectId: string
): Promise<PiiRule[]> {
  const response = await authedFetch(
    `/api/v1/orgs/${orgId}/projects/${projectId}/pii-rules`,
    accessToken
  );
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function createPiiRule(
  accessToken: string,
  orgId: string,
  projectId: string,
  body: { property_key: string; action: PiiAction }
): Promise<CreatePiiRuleResult> {
  const response = await authedFetch(
    `/api/v1/orgs/${orgId}/projects/${projectId}/pii-rules`,
    accessToken,
    { method: "POST", body: JSON.stringify(body) }
  );
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function deletePiiRule(
  accessToken: string,
  orgId: string,
  projectId: string,
  ruleId: string
): Promise<void> {
  const response = await authedFetch(
    `/api/v1/orgs/${orgId}/projects/${projectId}/pii-rules/${ruleId}`,
    accessToken,
    { method: "DELETE" }
  );
  if (!response.ok) throw await toApiError(response);
}
