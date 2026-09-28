"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Alert, Button, Input, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import { deleteSubject, type DeleteSubjectResult } from "@/lib/deletion-api";
import { listMyOrgs } from "@/lib/orgs-api";

const CONFIRM_PHRASE = "DELETE";

export function SubjectDeletion({ orgId, projectId }: { orgId: string; projectId: string }) {
  const { accessToken } = useAuth();
  const [userId, setUserId] = useState("");
  const [anonymousId, setAnonymousId] = useState("");
  const [confirmText, setConfirmText] = useState("");

  const orgsQuery = useQuery({
    queryKey: ["orgs"],
    queryFn: () => listMyOrgs(accessToken!),
    enabled: !!accessToken,
  });
  const role = orgsQuery.data?.find((org) => org.id === orgId)?.role;

  const mutation = useMutation({
    mutationFn: () =>
      deleteSubject(accessToken!, orgId, projectId, {
        user_id: userId.trim() || undefined,
        anonymous_id: anonymousId.trim() || undefined,
      }),
    onSuccess: () => {
      setUserId("");
      setAnonymousId("");
      setConfirmText("");
    },
  });

  if (orgsQuery.isLoading) return <Spinner />;

  // Reached the URL directly without being an Owner -- explain why, rather
  // than a blank page or a 403 the person never asked for.
  if (role !== "owner") {
    return (
      <Alert>
        Only an organization Owner can delete a subject&apos;s data. Ask an Owner of this
        organization, or have your role changed.
      </Alert>
    );
  }

  const hasIdentifier = userId.trim() !== "" || anonymousId.trim() !== "";
  const confirmed = confirmText === CONFIRM_PHRASE;

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!hasIdentifier || !confirmed || mutation.isPending) return;
    mutation.mutate();
  }

  return (
    <div className="flex max-w-xl flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Delete a subject&apos;s data</h1>
        <p className="mt-1 text-sm text-gray-500">
          Removes every event for this user or anonymous ID from this project: the stored events,
          their contribution to the hourly rollup, and the raw archive. This is the single most
          destructive action in the app and cannot be undone.
        </p>
      </div>

      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium">User ID</span>
          <Input
            value={userId}
            onChange={(event) => setUserId(event.target.value)}
            placeholder="e.g. user_1234"
            disabled={mutation.isPending}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium">Anonymous ID</span>
          <Input
            value={anonymousId}
            onChange={(event) => setAnonymousId(event.target.value)}
            placeholder="e.g. a pre-identify device ID"
            disabled={mutation.isPending}
          />
        </label>
        {!hasIdentifier && (
          <p className="text-sm text-gray-500" role="note">
            Provide a User ID, an Anonymous ID, or both.
          </p>
        )}

        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium">
            Type <span className="font-mono">{CONFIRM_PHRASE}</span> to confirm
          </span>
          <Input
            value={confirmText}
            onChange={(event) => setConfirmText(event.target.value)}
            disabled={!hasIdentifier || mutation.isPending}
            aria-label={`Type ${CONFIRM_PHRASE} to confirm`}
          />
        </label>

        <Button
          type="submit"
          variant="danger"
          disabled={!hasIdentifier || !confirmed || mutation.isPending}
          className="self-start rounded border px-3 py-2"
        >
          {mutation.isPending ? "Deleting…" : "Delete this subject's data"}
        </Button>
      </form>

      {mutation.isError && <Alert>{mutation.error.message}</Alert>}
      {mutation.isSuccess && <SuccessReport result={mutation.data} />}
    </div>
  );
}

function SuccessReport({ result }: { result: DeleteSubjectResult }) {
  // Not <Alert>: it renders a <p>, and this report's content (another
  // paragraph plus a list) isn't valid inside one.
  return (
    <div role="status" className="text-green-700">
      <p className="font-medium">Deletion complete.</p>
      <ul className="mt-1 list-inside list-disc text-sm">
        <li>
          Rollup: {result.rollup_buckets_recomputed} bucket
          {result.rollup_buckets_recomputed === 1 ? "" : "s"} recomputed
          {result.rollup_verified ? "" : " (still converging — check back shortly)"}.
        </li>
        <li>
          Archive: {result.archive.entries_removed} entr
          {result.archive.entries_removed === 1 ? "y" : "ies"} removed across{" "}
          {result.archive.objects_rewritten + result.archive.objects_deleted} object
          {result.archive.objects_rewritten + result.archive.objects_deleted === 1 ? "" : "s"}.
        </li>
        {result.archive.unreadable_objects > 0 && (
          <li className="text-amber-700">
            {result.archive.unreadable_objects} archive object
            {result.archive.unreadable_objects === 1 ? "" : "s"} could not be read and were left
            untouched — this deletion is incomplete for those.
          </li>
        )}
      </ul>
    </div>
  );
}
