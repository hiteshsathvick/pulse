"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { listMyOrgs } from "@/lib/orgs-api";
import { listProjects } from "@/lib/projects-api";

/** Pulse has no server-side "active org" concept (unlike a session that
 * remembers your last org) -- which org/project you're looking at is
 * purely the URL's own /orgs/[orgId]/projects/[projectId] segments. This
 * is two plain <select> dropdowns, not a fancier menu component: it's the
 * simplest thing that lets you jump between orgs/projects you're a member
 * of, and everything it needs (the org list, the project list) is already
 * driven by real endpoints via TanStack Query. */
export function OrgProjectSwitcher() {
  const { accessToken } = useAuth();
  const router = useRouter();
  const params = useParams<{ orgId?: string; projectId?: string }>();
  const orgId = params.orgId;
  const projectId = params.projectId;

  const orgsQuery = useQuery({
    queryKey: ["orgs"],
    queryFn: () => listMyOrgs(accessToken!),
    enabled: !!accessToken,
  });

  const projectsQuery = useQuery({
    queryKey: ["projects", orgId],
    queryFn: () => listProjects(accessToken!, orgId!),
    enabled: !!accessToken && !!orgId,
  });

  function handleOrgChange(nextOrgId: string) {
    router.push(`/orgs/${nextOrgId}/projects`);
  }

  function handleProjectChange(nextProjectId: string) {
    router.push(`/orgs/${orgId}/projects/${nextProjectId}`);
  }

  return (
    <div className="flex items-center gap-2 text-sm">
      <select
        aria-label="Organization"
        className="rounded border px-2 py-1"
        value={orgId ?? ""}
        onChange={(event) => handleOrgChange(event.target.value)}
      >
        <option value="" disabled>
          Select org…
        </option>
        {orgsQuery.data?.map((org) => (
          <option key={org.id} value={org.id}>
            {org.name}
          </option>
        ))}
      </select>
      {orgId && (
        <select
          aria-label="Project"
          className="rounded border px-2 py-1"
          value={projectId ?? ""}
          onChange={(event) => handleProjectChange(event.target.value)}
        >
          <option value="" disabled>
            Select project…
          </option>
          {projectsQuery.data?.map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}
