import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Dashboard } from "@/lib/dashboards-api";
import * as dashboardsApi from "@/lib/dashboards-api";
import * as insightsApi from "@/lib/insights-api";
import type { InsightSpec } from "@/lib/insight-spec";
import { DashboardEditor } from "./DashboardEditor";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, replace: vi.fn() }) }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "token" }) }));
vi.mock("@/lib/insights-api", () => ({ listInsights: vi.fn(), runInsightQuery: vi.fn() }));
vi.mock("@/lib/dashboards-api", () => ({ updateDashboard: vi.fn(), deleteDashboard: vi.fn() }));

const updateDashboard = vi.mocked(dashboardsApi.updateDashboard);
const deleteDashboard = vi.mocked(dashboardsApi.deleteDashboard);

const SPEC: InsightSpec = {
  kind: "trend",
  version: 1,
  events: ["e"],
  measure: "count",
  filters: [],
  breakdown: null,
  range: { from: "2026-01-01", to: "2026-01-31", tz: "project" },
  granularity: "day",
};

const insight = (id: string, name: string) => ({
  id,
  name,
  kind: "trend" as const,
  spec: SPEC,
  created_by: "u",
  created_at: "",
  updated_at: "",
});

const dashboard: Dashboard = {
  id: "d1",
  name: "Growth",
  layout: { columns: 12 },
  default_range: { type: "relative", days: 30 },
  shared_scope: "private",
  created_by: "u1",
  created_at: "",
  updated_at: "",
  can_edit: true,
  items: [
    { id: "i-a", insight: insight("a", "Insight A"), position: { x: 0, y: 0, w: 6, h: 3 } },
    { id: "i-b", insight: insight("b", "Insight B"), position: { x: 6, y: 0, w: 6, h: 3 } },
  ],
};

const onCancel = vi.fn();
const onSaved = vi.fn();

function renderEditor() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DashboardEditor
        orgId="org-1"
        projectId="proj-1"
        dashboard={dashboard}
        customSeed={{ from: "2026-08-24", to: "2026-09-22" }}
        onCancel={onCancel}
        onSaved={onSaved}
      />
    </QueryClientProvider>
  );
}

const save = () => userEvent.click(screen.getByRole("button", { name: "Save dashboard" }));

function tileNames(): string[] {
  const list = within(screen.getByRole("region", { name: "Tiles" })).getByRole("list");
  return within(list)
    .getAllByRole("listitem")
    .map((li) => (li.textContent ?? "").replace(/trend.*/, "").trim().replace(/Insight (\w).*/, "Insight $1"));
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(insightsApi.listInsights).mockResolvedValue([
    insight("a", "Insight A"),
    insight("b", "Insight B"),
    insight("c", "Insight C"),
    insight("d", "Insight D"),
  ]);
  updateDashboard.mockImplementation(async () => ({ ...dashboard, name: "saved" }));
});

describe("DashboardEditor", () => {
  it("lists the tiles in order, and offers only insights not already on the dashboard", async () => {
    renderEditor();
    expect(tileNames()).toEqual(["Insight A", "Insight B"]);
    const add = await screen.findByLabelText("Add an insight");
    await waitFor(() =>
      expect(within(add).getAllByRole("option").map((o) => o.textContent)).toEqual([
        "Choose an insight…",
        "Insight C",
        "Insight D",
      ])
    );
  });

  it("adding an insight sends just the new layout, packed after the existing tiles", async () => {
    renderEditor();
    await screen.findByRole("option", { name: "Insight C" }); // the list loads after first render
    await userEvent.selectOptions(screen.getByLabelText("Add an insight"), "c");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(tileNames()).toEqual(["Insight A", "Insight B", "Insight C"]);
    await save();

    await waitFor(() => expect(updateDashboard).toHaveBeenCalledTimes(1));
    expect(updateDashboard).toHaveBeenCalledWith("token", "org-1", "proj-1", "d1", {
      items: [
        { insight_id: "a", position: { x: 0, y: 0, w: 6, h: 3 } },
        { insight_id: "b", position: { x: 6, y: 0, w: 6, h: 3 } },
        { insight_id: "c", position: { x: 0, y: 3, w: 6, h: 3 } },
      ],
    });
  });

  it("moving, resizing and removing tiles are reflected in the saved layout", async () => {
    renderEditor();
    await userEvent.click(screen.getByRole("button", { name: "Move Insight B up" }));
    expect(tileNames()).toEqual(["Insight B", "Insight A"]);
    await userEvent.selectOptions(screen.getByLabelText("Size of Insight B"), "large");
    await save();

    await waitFor(() => expect(updateDashboard).toHaveBeenCalledTimes(1));
    expect(updateDashboard.mock.calls[0][4]).toEqual({
      items: [
        { insight_id: "b", position: { x: 0, y: 0, w: 12, h: 4 } },
        { insight_id: "a", position: { x: 0, y: 4, w: 6, h: 3 } },
      ],
    });

    updateDashboard.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "Remove Insight A" }));
    await save();
    await waitFor(() => expect(updateDashboard).toHaveBeenCalledTimes(1));
    expect(updateDashboard.mock.calls[0][4]).toEqual({
      items: [{ insight_id: "b", position: { x: 0, y: 0, w: 12, h: 4 } }],
    });
  });

  it("the first tile can't move up and the last can't move down", () => {
    renderEditor();
    expect(screen.getByRole("button", { name: "Move Insight A up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move Insight B down" })).toBeDisabled();
  });

  it("renaming, sharing and the default range send only those fields, not the layout", async () => {
    renderEditor();
    const name = screen.getByLabelText("Dashboard name");
    await userEvent.clear(name);
    await userEvent.type(name, "Weekly growth");
    await userEvent.selectOptions(screen.getByLabelText(/Who can see it/), "org");
    await userEvent.selectOptions(screen.getByLabelText("Default date range"), "7");
    await save();

    await waitFor(() => expect(updateDashboard).toHaveBeenCalledTimes(1));
    expect(updateDashboard.mock.calls[0][4]).toEqual({
      name: "Weekly growth",
      shared_scope: "org",
      default_range: { type: "relative", days: 7 },
    });
  });

  it("a custom default range starts from the dates being viewed", async () => {
    renderEditor();
    await userEvent.selectOptions(screen.getByLabelText("Default date range"), "custom");
    expect(screen.getByLabelText("Default date range from")).toHaveValue("2026-08-24");
    expect(screen.getByLabelText("Default date range to")).toHaveValue("2026-09-22");
    await save();
    await waitFor(() => expect(updateDashboard).toHaveBeenCalledTimes(1));
    expect(updateDashboard.mock.calls[0][4]).toEqual({
      default_range: { type: "absolute", from: "2026-08-24", to: "2026-09-22" },
    });
  });

  it("hands the saved dashboard back to the page", async () => {
    renderEditor();
    await userEvent.type(screen.getByLabelText("Dashboard name"), "!");
    await save();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ name: "saved" })));
  });

  it("with nothing changed it says so and sends nothing", async () => {
    renderEditor();
    await save();
    expect(await screen.findByText("Nothing has changed.")).toBeInTheDocument();
    expect(updateDashboard).not.toHaveBeenCalled();
  });

  it("a blank name is refused with a reason", async () => {
    renderEditor();
    await userEvent.clear(screen.getByLabelText("Dashboard name"));
    await save();
    expect(await screen.findByText("Give the dashboard a name.")).toBeInTheDocument();
    expect(updateDashboard).not.toHaveBeenCalled();
  });

  it("surfaces an API failure and does not report success", async () => {
    updateDashboard.mockRejectedValue(new Error("You don't have permission to change this dashboard"));
    renderEditor();
    await userEvent.type(screen.getByLabelText("Dashboard name"), "!");
    await save();
    expect(await screen.findByText("You don't have permission to change this dashboard")).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("Cancel leaves without saving", async () => {
    renderEditor();
    await userEvent.type(screen.getByLabelText("Dashboard name"), "!");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalled();
    expect(updateDashboard).not.toHaveBeenCalled();
  });

  it("deletes only after confirmation, then returns to the list", async () => {
    deleteDashboard.mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, "confirm");
    renderEditor();

    confirm.mockReturnValueOnce(false);
    await userEvent.click(screen.getByRole("button", { name: "Delete dashboard" }));
    expect(deleteDashboard).not.toHaveBeenCalled();

    confirm.mockReturnValueOnce(true);
    await userEvent.click(screen.getByRole("button", { name: "Delete dashboard" }));
    await waitFor(() => expect(deleteDashboard).toHaveBeenCalledWith("token", "org-1", "proj-1", "d1"));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/orgs/org-1/projects/proj-1/dashboards"));
    confirm.mockRestore();
  });

  it("points to the insight builder when the project has no saved insights", async () => {
    vi.mocked(insightsApi.listInsights).mockResolvedValue([]);
    renderEditor();
    const link = await screen.findByRole("link", { name: "Build one" });
    expect(link).toHaveAttribute("href", "/orgs/org-1/projects/proj-1/insights/new");
  });
});
