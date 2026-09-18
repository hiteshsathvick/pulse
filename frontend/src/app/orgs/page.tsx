"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { type FormEvent, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { Alert, Button, EmptyState, Input, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import { createOrg, listMyOrgs } from "@/lib/orgs-api";

function OrgsList() {
  const { accessToken } = useAuth();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");

  const orgsQuery = useQuery({
    queryKey: ["orgs"],
    queryFn: () => listMyOrgs(accessToken!),
    enabled: !!accessToken,
  });

  const createOrgMutation = useMutation({
    mutationFn: () => createOrg(accessToken!, name, slug),
    onSuccess: () => {
      setName("");
      setSlug("");
      queryClient.invalidateQueries({ queryKey: ["orgs"] });
    },
  });

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    createOrgMutation.mutate();
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-8">
      <div>
        <h1 className="text-2xl font-semibold">Organizations</h1>
        {orgsQuery.isLoading && <Spinner />}
        {orgsQuery.isError && <Alert>Failed to load organizations.</Alert>}
        {orgsQuery.data?.length === 0 && <EmptyState>No organizations yet.</EmptyState>}
        <ul className="mt-4 flex flex-col gap-2">
          {orgsQuery.data?.map((org) => (
            <li key={org.id} className="rounded border p-3">
              <Link href={`/orgs/${org.id}/projects`} className="font-medium underline">
                {org.name}
              </Link>
              <span className="ml-2 text-sm text-gray-500">{org.role}</span>
            </li>
          ))}
        </ul>
      </div>

      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <h2 className="text-lg font-medium">Create an organization</h2>
        <Input
          placeholder="Name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          required
        />
        <Input
          placeholder="Slug"
          value={slug}
          onChange={(event) => setSlug(event.target.value)}
          required
        />
        {createOrgMutation.isError && (
          <Alert>
            {createOrgMutation.error instanceof Error
              ? createOrgMutation.error.message
              : "failed to create organization"}
          </Alert>
        )}
        <Button type="submit" disabled={createOrgMutation.isPending} className="self-start">
          {createOrgMutation.isPending ? "Creating…" : "Create"}
        </Button>
      </form>
    </div>
  );
}

export default function OrgsPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <OrgsList />
      </AppShell>
    </ProtectedRoute>
  );
}
