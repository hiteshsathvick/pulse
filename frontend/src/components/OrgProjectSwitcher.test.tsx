import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { OrgProjectSwitcher } from "./OrgProjectSwitcher";

const push = vi.fn();
let mockParams: { orgId?: string; projectId?: string } = {};
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useParams: () => mockParams,
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ accessToken: "token" }),
}));

vi.mock("@/lib/orgs-api", () => ({
  listMyOrgs: vi.fn().mockResolvedValue([
    { id: "org-1", name: "Acme", slug: "acme", retention_days: 90, role: "owner" },
  ]),
}));

vi.mock("@/lib/projects-api", () => ({
  listProjects: vi.fn().mockResolvedValue([
    { id: "proj-1", org_id: "org-1", name: "Web", slug: "web", timezone: "UTC" },
  ]),
}));

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  push.mockClear();
  mockParams = {};
});

describe("OrgProjectSwitcher", () => {
  it("lists the user's orgs and navigates to its projects on selection", async () => {
    renderWithClient(<OrgProjectSwitcher />);
    const orgSelect = await screen.findByRole("option", { name: "Acme" });
    expect(orgSelect).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Organization"), "org-1");
    expect(push).toHaveBeenCalledWith("/orgs/org-1/projects");
  });

  it("also shows a project dropdown once an org is selected via the URL", async () => {
    mockParams = { orgId: "org-1" };
    renderWithClient(<OrgProjectSwitcher />);

    await waitFor(() => expect(screen.getByLabelText("Project")).toBeInTheDocument());
    await userEvent.selectOptions(await screen.findByLabelText("Project"), "proj-1");
    expect(push).toHaveBeenCalledWith("/orgs/org-1/projects/proj-1");
  });
});
