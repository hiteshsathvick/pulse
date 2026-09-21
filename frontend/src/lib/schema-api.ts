import { authedFetch, toApiError } from "./api";

export type SchemaStatus = "active" | "deprecated" | "hidden";
export type PropertyType = "string" | "number" | "bool" | "datetime";

export type EventSchema = {
  id: string;
  event_name: string;
  status: SchemaStatus;
  first_seen_at: string;
  volume_estimate: number;
};

export type PropertySchema = {
  id: string;
  event_schema_id: string | null;
  key: string;
  inferred_type: PropertyType;
  is_pii: boolean;
  status: SchemaStatus;
};

export async function listEventSchemas(
  accessToken: string,
  orgId: string,
  projectId: string
): Promise<EventSchema[]> {
  const response = await authedFetch(
    `/api/v1/orgs/${orgId}/projects/${projectId}/schema/events`,
    accessToken
  );
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function listPropertySchemas(
  accessToken: string,
  orgId: string,
  projectId: string,
  eventSchemaId: string
): Promise<PropertySchema[]> {
  const response = await authedFetch(
    `/api/v1/orgs/${orgId}/projects/${projectId}/schema/events/${eventSchemaId}/properties`,
    accessToken
  );
  if (!response.ok) throw await toApiError(response);
  return response.json();
}
