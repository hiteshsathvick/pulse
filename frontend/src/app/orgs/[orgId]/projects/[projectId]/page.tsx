"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { Alert, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import { getProject } from "@/lib/projects-api";

function ProjectHome() {
  const { accessToken } = useAuth();
  const { orgId, projectId } = useParams<{ orgId: string; projectId: string }>();

  const projectQuery = useQuery({
    queryKey: ["project", orgId, projectId],
    queryFn: () => getProject(accessToken!, orgId, projectId),
    enabled: !!accessToken,
  });

  if (projectQuery.isLoading) return <Spinner />;
  if (projectQuery.isError) return <Alert>Failed to load project.</Alert>;
  if (!projectQuery.data) return null;

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-2">
      <h1 className="text-2xl font-semibold">{projectQuery.data.name}</h1>
      <p className="text-sm text-gray-500">
        {projectQuery.data.slug} · {projectQuery.data.timezone}
      </p>
      <p className="mt-6 text-gray-500">
        Insights, dashboards, and schema management land in later phases — this page confirms auth,
        org, and project context all resolve correctly.
      </p>
    </div>
  );
}

export default function ProjectPage() {
  return (
    <ProtectedRoute>
      <AppShell>
        <ProjectHome />
      </AppShell>
    </ProtectedRoute>
  );
}
