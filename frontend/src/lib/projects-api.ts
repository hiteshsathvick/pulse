import { authedFetch, toApiError } from "./api";

export type Project = {
  id: string;
  org_id: string;
  name: string;
  slug: string;
  timezone: string;
};

export async function listProjects(accessToken: string, orgId: string): Promise<Project[]> {
  const response = await authedFetch(`/api/v1/orgs/${orgId}/projects`, accessToken);
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function createProject(
  accessToken: string,
  orgId: string,
  name: string,
  slug: string
): Promise<Project> {
  const response = await authedFetch(`/api/v1/orgs/${orgId}/projects`, accessToken, {
    method: "POST",
    body: JSON.stringify({ name, slug }),
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function getProject(
  accessToken: string,
  orgId: string,
  projectId: string
): Promise<Project> {
  const response = await authedFetch(`/api/v1/orgs/${orgId}/projects/${projectId}`, accessToken);
  if (!response.ok) throw await toApiError(response);
  return response.json();
}
