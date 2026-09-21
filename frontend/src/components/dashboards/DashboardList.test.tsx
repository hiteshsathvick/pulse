import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as dashboardsApi from "@/lib/dashboards-api";
import * as orgsApi from "@/lib/orgs-api";
import { DashboardList } from "./DashboardList";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, replace: vi.fn() }) }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "token" }) }));
vi.mock("@/lib/dashboards-api", () => ({ listDashboards: vi.fn(), createDashboard: vi.fn() }));
vi.mock("@/lib/orgs-api", () => ({ listMyOrgs: vi.fn() }));

const listDashboards = vi.mocked(dashboardsApi.listDashboards);
const createDashboard = vi.mocked(dashboardsApi.createDashboard);

const summary = (id: string, name: string, over: Partial<dashboardsApi.DashboardSummary> = {}) => ({
  id,
  name,
  shared_scope: "org" as const,
  created_by: "u1",
  updated_at: "2026-09-20T00:00:00Z",
  item_count: 3,
  can_edit: true,
  ...over,
});

function setRole(role: orgsApi.MembershipRole) {
  vi.mocked(orgsApi.listMyOrgs).mockResolvedValue([
    { id: "org-1", name: "Acme", slug: "acme", retention_days: 90, role },
  ]);
}

function renderList() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DashboardList orgId="org-1" projectId="proj-1" />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  setRole("member");
  listDashboards.mockResolvedValue([]);
});

describe("DashboardList", () => {
  it("lists dashboards with their size and who can see them, marking read-only ones", async () => {
    listDashboards.mockResolvedValue([
      summary("d1", "Growth", { item_count: 3 }),
      summary("d2", "Solo", { item_count: 1, shared_scope: "private" }),
      summary("d3", "Their board", { can_edit: false }),
    ]);
    renderList();

    expect(await screen.findByText("Growth")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Growth/ })).toHaveAttribute(
      "href",
      "/orgs/org-1/projects/proj-1/dashboards/d1"
    );
    expect(screen.getByRole("link", { name: /Growth/ })).toHaveTextContent(
      "3 insights · Shared with your organization"
    );
    expect(screen.getByRole("link", { name: /Solo/ })).toHaveTextContent("1 insight · Only you");
    expect(screen.getByRole("link", { name: /Their board/ })).toHaveTextContent("Read-only");
    expect(screen.getByRole("link", { name: /Growth/ })).not.toHaveTextContent("Read-only");
  });

  it("says so when there are none", async () => {
    renderList();
    expect(await screen.findByText("No dashboards yet.")).toBeInTheDocument();
  });

  it("creates a dashboard with the chosen sharing, then opens it", async () => {
    createDashboard.mockResolvedValue({ id: "new-id" } as dashboardsApi.Dashboard);
    renderList();

    await userEvent.type(screen.getByLabelText("New dashboard name"), "Weekly growth");
    await userEvent.selectOptions(screen.getByLabelText("Who can see it"), "org");
    await userEvent.click(screen.getByRole("button", { name: "Create dashboard" }));

    await waitFor(() => expect(createDashboard).toHaveBeenCalledTimes(1));
    expect(createDashboard).toHaveBeenCalledWith("token", "org-1", "proj-1", {
      name: "Weekly growth",
      shared_scope: "org",
    });
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("/orgs/org-1/projects/proj-1/dashboards/new-id")
    );
  });

  it("new dashboards are private unless you choose otherwise", async () => {
    createDashboard.mockResolvedValue({ id: "x" } as dashboardsApi.Dashboard);
    renderList();
    await userEvent.type(screen.getByLabelText("New dashboard name"), "Scratch");
    await userEvent.click(screen.getByRole("button", { name: "Create dashboard" }));
    await waitFor(() => expect(createDashboard).toHaveBeenCalled());
    expect(createDashboard.mock.calls[0][3]).toEqual({ name: "Scratch", shared_scope: "private" });
  });

  it("a blank name is refused, and an API failure is shown", async () => {
    renderList();
    await userEvent.click(screen.getByRole("button", { name: "Create dashboard" }));
    expect(await screen.findByText("Give the dashboard a name.")).toBeInTheDocument();
    expect(createDashboard).not.toHaveBeenCalled();

    createDashboard.mockRejectedValue(new Error("Requires at least the member role"));
    await userEvent.type(screen.getByLabelText("New dashboard name"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Create dashboard" }));
    expect(await screen.findByText("Requires at least the member role")).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });

  it("a viewer isn't offered the create form (the API would refuse it)", async () => {
    setRole("viewer");
    listDashboards.mockResolvedValue([summary("d1", "Shared with me", { can_edit: false })]);
    renderList();
    expect(await screen.findByText("Shared with me")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create dashboard" })).not.toBeInTheDocument();
  });
});
