"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { type FormEvent, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { Alert, Button, EmptyState, Input, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import { listMyOrgs } from "@/lib/orgs-api";
import { createProject, listProjects } from "@/lib/projects-api";

function ProjectsList() {
  const { accessToken } = useAuth();
  const { orgId } = useParams<{ orgId: string }>();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");

  const projectsQuery = useQuery({
    queryKey: ["projects", orgId],
    queryFn: () => listProjects(accessToken!, orgId),
    enabled: !!accessToken,
  });

  // Billing is Admin+ on the API too -- hiding the link for anyone else
  // avoids a click that only leads to a 403. Shares the switcher's ["orgs"]
  // query, so this is usually already cached.
  const orgsQuery = useQuery({
    queryKey: ["orgs"],
    queryFn: () => listMyOrgs(accessToken!),
    enabled: !!accessToken,
  });
  const role = orgsQuery.data?.find((org) => org.id === orgId)?.role;
  const canViewBilling = role === "owner" || role === "admin";

  const createProjectMutation = useMutation({
    mutationFn: () => createProject(accessToken!, orgId, name, slug),
    onSuccess: () => {
      setName("");
      setSlug("");
      queryClient.invalidateQueries({ queryKey: ["projects", orgId] });
    },
  });

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    createProjectMutation.mutate();
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-8">
      <div>
        <div className="flex items-center justify-between gap-4">
          <h1 className="text-2xl font-semibold">Projects</h1>
          {canViewBilling && (
            <Link href={`/orgs/${orgId}/billing`} className="text-sm underline">
              Billing
            </Link>
          )}
        </div>
        {projectsQuery.isLoading && <Spinner />}
        {projectsQuery.isError && <Alert>Failed to load projects.</Alert>}
        {projectsQuery.data?.length === 0 && <EmptyState>No projects yet.</EmptyState>}
        <ul className="mt-4 flex flex-col gap-2">
          {projectsQuery.data?.map((project) => (
            <li key={project.id} className="rounded border p-3">
              <Link href={`/orgs/${orgId}/projects/${project.id}`} className="font-medium underline">
                {project.name}
              </Link>
              <span className="ml-2 text-sm text-gray-500">{project.timezone}</span>
            </li>
          ))}
        </ul>
      </div>

      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <h2 className="text-lg font-medium">Create a project</h2>
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
        {createProjectMutation.isError && (
          <Alert>
            {createProjectMutation.error instanceof Error
              ? createProjectMutation.error.message
              : "failed to create project"}
          </Alert>
        )}
        <Button type="submit" disabled={createProjectMutation.isPending} className="self-start">
          {createProjectMutation.isPending ? "Creating…" : "Create"}
        </Button>
      </form>
    </div>
  );
}

export default function ProjectsPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <ProjectsList />
      </AppShell>
    </ProtectedRoute>
  );
}
