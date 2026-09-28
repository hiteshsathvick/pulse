import { authedFetch, toApiError } from "./api";

// Mirrors backend/pulse/api/deletion.py.
export type ArchiveErasure = {
  entries_removed: number;
  objects_rewritten: number;
  objects_deleted: number;
  unreadable_objects: number;
};

export type DeleteSubjectResult = {
  rollup_buckets_recomputed: number;
  rollup_verified: boolean;
  archive: ArchiveErasure;
};

export async function deleteSubject(
  accessToken: string,
  orgId: string,
  projectId: string,
  body: { user_id?: string; anonymous_id?: string }
): Promise<DeleteSubjectResult> {
  const response = await authedFetch(
    `/api/v1/orgs/${orgId}/projects/${projectId}/subjects/delete`,
    accessToken,
    { method: "POST", body: JSON.stringify(body) }
  );
  if (!response.ok) throw await toApiError(response);
  return response.json();
}
