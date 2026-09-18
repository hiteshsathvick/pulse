import { authedFetch, toApiError } from "./api";

export type MembershipRole = "owner" | "admin" | "member" | "viewer";

export type Org = {
  id: string;
  name: string;
  slug: string;
  retention_days: number;
  role: MembershipRole;
};

export async function listMyOrgs(accessToken: string): Promise<Org[]> {
  const response = await authedFetch("/api/v1/orgs", accessToken);
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function createOrg(accessToken: string, name: string, slug: string): Promise<Org> {
  const response = await authedFetch("/api/v1/orgs", accessToken, {
    method: "POST",
    body: JSON.stringify({ name, slug }),
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}
