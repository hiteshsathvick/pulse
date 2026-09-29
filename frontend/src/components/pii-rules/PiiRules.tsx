"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Alert, Button, EmptyState, Input, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import { listMyOrgs } from "@/lib/orgs-api";
import {
  createPiiRule,
  deletePiiRule,
  listPiiRules,
  type CreatePiiRuleResult,
  type PiiAction,
} from "@/lib/pii-rules-api";

const SELECT_CLASS = "rounded border px-3 py-2";

const ACTION_LABEL: Record<PiiAction, string> = {
  hash: "Hash (deterministic, non-reversible)",
  drop: "Drop (delete the property entirely)",
};

export function PiiRules({ orgId, projectId }: { orgId: string; projectId: string }) {
  const { accessToken } = useAuth();
  const queryClient = useQueryClient();

  const [propertyKey, setPropertyKey] = useState("");
  const [action, setAction] = useState<PiiAction>("hash");
  const [lastCreated, setLastCreated] = useState<CreatePiiRuleResult | null>(null);

  const orgsQuery = useQuery({
    queryKey: ["orgs"],
    queryFn: () => listMyOrgs(accessToken!),
    enabled: !!accessToken,
  });
  const role = orgsQuery.data?.find((org) => org.id === orgId)?.role;

  const listKey = ["pii-rules", orgId, projectId];
  const rulesQuery = useQuery({
    queryKey: listKey,
    queryFn: () => listPiiRules(accessToken!, orgId, projectId),
    // The backend requires Admin, not just any member -- don't fire the list
    // request for a role that would only get a 403 back.
    enabled: !!accessToken && (role === "owner" || role === "admin"),
  });

  const createMutation = useMutation({
    mutationFn: () =>
      createPiiRule(accessToken!, orgId, projectId, {
        property_key: propertyKey.trim(),
        action,
      }),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: listKey });
      setPropertyKey("");
      setLastCreated(result);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (ruleId: string) => deletePiiRule(accessToken!, orgId, projectId, ruleId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: listKey }),
  });

  if (orgsQuery.isLoading) return <Spinner />;

  // Reached the URL directly without being an Admin or Owner -- explain why,
  // rather than a blank page or a 403 the person never asked for.
  if (role !== "owner" && role !== "admin") {
    return (
      <Alert>
        Only an organization Admin or Owner can manage PII rules. Ask an Admin or Owner of this
        organization, or have your role changed.
      </Alert>
    );
  }

  function handleCreate(event: React.FormEvent) {
    event.preventDefault();
    if (!propertyKey.trim() || createMutation.isPending) return;
    setLastCreated(null);
    createMutation.mutate();
  }

  function handleDelete(rule: { id: string; property_key: string }) {
    if (window.confirm(`Stop scrubbing "${rule.property_key}"? Future events will keep it as-is.`)) {
      setLastCreated(null);
      deleteMutation.mutate(rule.id);
    }
  }

  return (
    <div className="flex max-w-xl flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">PII rules</h1>
        <p className="mt-1 text-sm text-gray-500">
          Scrub a property from every future event as it&apos;s ingested, and retroactively from
          already-archived events for this project.
        </p>
      </div>

      <form onSubmit={handleCreate} className="flex flex-col gap-3 rounded border p-4">
        <h2 className="font-medium">New rule</h2>

        <label className="flex flex-col gap-1 text-sm">
          <span>Property key</span>
          <Input
            value={propertyKey}
            onChange={(event) => setPropertyKey(event.target.value)}
            placeholder="e.g. email"
            disabled={createMutation.isPending}
          />
        </label>

        <label className="flex flex-col gap-1 text-sm">
          <span>Action</span>
          <select
            className={SELECT_CLASS}
            value={action}
            onChange={(event) => setAction(event.target.value as PiiAction)}
            disabled={createMutation.isPending}
          >
            {Object.entries(ACTION_LABEL).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>

        {createMutation.isError && <Alert>{createMutation.error.message}</Alert>}

        <div>
          <Button type="submit" disabled={!propertyKey.trim() || createMutation.isPending}>
            {createMutation.isPending ? "Creating…" : "Create rule"}
          </Button>
        </div>
      </form>

      {lastCreated && <CreatedReport result={lastCreated} />}
      {deleteMutation.isError && <Alert>{deleteMutation.error.message}</Alert>}

      {rulesQuery.isLoading && <Spinner />}
      {rulesQuery.isError && <Alert>Failed to load PII rules.</Alert>}
      {rulesQuery.data && rulesQuery.data.length === 0 && (
        <EmptyState>No PII rules yet.</EmptyState>
      )}
      <ul className="flex flex-col gap-2">
        {rulesQuery.data?.map((rule) => (
          <li key={rule.id} className="flex items-center justify-between rounded border p-3">
            <div>
              <p className="font-medium">{rule.property_key}</p>
              <p className="text-sm text-gray-500">{ACTION_LABEL[rule.action]}</p>
            </div>
            <Button
              variant="danger"
              onClick={() => handleDelete(rule)}
              disabled={deleteMutation.isPending}
            >
              Delete
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function CreatedReport({ result }: { result: CreatePiiRuleResult }) {
  // Not <Alert>: it renders a <p>, and this report's content (another
  // paragraph plus a list) isn't valid inside one.
  return (
    <div role="status" className="text-green-700">
      <p className="font-medium">Rule created.</p>
      {result.archive_rewrite ? (
        <ul className="mt-1 list-inside list-disc text-sm">
          <li>
            Archive: {result.archive_rewrite.entries_scrubbed} entr
            {result.archive_rewrite.entries_scrubbed === 1 ? "y" : "ies"} scrubbed across{" "}
            {result.archive_rewrite.objects_rewritten} object
            {result.archive_rewrite.objects_rewritten === 1 ? "" : "s"}.
          </li>
          {result.archive_rewrite.unreadable_objects > 0 && (
            <li className="text-amber-700">
              {result.archive_rewrite.unreadable_objects} archive object
              {result.archive_rewrite.unreadable_objects === 1 ? "" : "s"} could not be read and
              were left unscrubbed.
            </li>
          )}
        </ul>
      ) : (
        <p className="mt-1 text-sm text-amber-700">
          The rule is created and enforced for new events, but the archive could not be reached to
          scrub already-stored events retroactively.
        </p>
      )}
    </div>
  );
}
